"""정상 범위 · 경고선 · 엣지 0 확률을 한 부트스트랩 경로 묶음에서 낸다.

일별(봉이 하루보다 길면 봉별) 수익률을 정상(stationary) 블록 부트스트랩으로 섞되, 경로마다 백테스트 표본부터
블록으로 다시 뽑고 그 안에서 경로를 뽑는다(중첩 이중 부트스트랩: 큰 날이 안 뽑힌 표본은 그 날을 한 번만
뺀다). 누적은 복리(-100%에서 멈춤), 구간은 30일 하나를 0에서 시작한다.
"""
from __future__ import annotations

import numpy as np
import pandas as pd

from blackclip.sim.warning_line import SENSITIVITY

PATHS = 10_000
"""경로 수."""

SEED = 7
"""난수 씨앗(같은 입력이면 같은 숫자, 「두 번 돌려 같은 숫자」). 재현 계약은 SEED · BUDGET · 난수를 부르는 순서(묶음마다
바깥 블록 -> 안쪽 블록) · numpy 판을 한데 묶은 것이고, BUDGET을 바꾸면 같은 숫자가 나온다고 보장하지 않는다."""

BUDGET = 2_000_000
"""한 묶음에서 만드는 바깥 인덱스 칸 수 상한(메모리: 경로 x 백테스트 길이). 재현 계약의 일부(SEED 참고)."""


def periods(returns: pd.Series, bar_s: int) -> tuple[pd.Series, int]:
    """봉별 수익률을 하루(UTC) 단위로 묶는다. 봉이 하루 이상이면 그대로.

    Args:
        returns: 봉별 수익률(시각 인덱스: 시간대가 있으면 UTC로 바꿔 하루를 가르고, 없으면 UTC로 본다).
        bar_s: 봉 길이(초).

    Returns:
        (기간별 수익률, 기간 길이(초)).
    """
    if bar_s >= 86400:
        return returns, bar_s
    idx = returns.index if returns.index.tz is None else returns.index.tz_convert("UTC")
    return (1 + returns).groupby(idx.floor("D")).prod() - 1, 86400


def _blocks(rng: np.random.Generator, n: int, rows: int, length: int, block: int) -> np.ndarray:
    """정상 블록 부트스트랩 인덱스: 칸마다 확률 1/block로 새 블록, 아니면 다음 칸(끝에서 처음으로 돎).

    Args:
        rng: 난수 생성기.
        n: 표본 길이.
        rows: 경로 수.
        length: 경로 길이.
        block: 평균 블록 길이.

    Returns:
        (rows, length) 정수 배열.
    """
    new = rng.random((rows, length)) < 1.0 / block
    new[:, 0] = True
    start = rng.integers(0, n, (rows, length))
    pos = np.arange(length)
    head = np.maximum.accumulate(np.where(new, pos, 0), axis=1)
    return (np.take_along_axis(start, head, axis=1) + pos - head) % n


def paths(rng: np.random.Generator, n: int, horizon: int, block: int) -> np.ndarray:
    """중첩 블록 부트스트랩 경로 인덱스(경로마다 표본 n개를 블록으로 다시 뽑고 그 안에서 horizon개).

    두 단계에서 블록이 끊기므로 실효 블록은 block의 약 절반이다(band가 2L을 넘긴다).

    Args:
        rng: 난수 생성기(부르는 순서가 숫자를 정한다).
        n: 표본 길이.
        horizon: 경로 길이.
        block: 평균 블록 길이.

    Returns:
        (PATHS, horizon) 원래 표본 인덱스.
    """
    rows = max(1, BUDGET // n)
    out = []
    for done in range(0, PATHS, rows):
        k = min(rows, PATHS - done)
        outer = _blocks(rng, n, k, n, block)
        out.append(np.take_along_axis(outer, _blocks(rng, n, k, horizon, block), axis=1))
    return np.concatenate(out)


def _compound(steps: np.ndarray) -> np.ndarray:
    """경로별 복리 누적 수익. 한 번 -100% 이하가 되면 그 뒤로 -100%에 머문다(파산은 되살아나지 않음).

    Args:
        steps: (경로, 기간) 수익률.

    Returns:
        같은 꼴의 누적 수익(-1 이상).
    """
    return np.cumprod(np.maximum(1 + steps, 0.0), axis=1) - 1


def band(r: np.ndarray, horizon: int, block: int, rng: np.random.Generator) -> dict[str, object]:
    """한 블록 길이로 정상 범위(5% · 95%) · 동시 하한 경고선 · 엣지 0이어도 범위 안일 확률을 낸다.

    경고선: 경로마다 구간 안 누적 수익의 최솟값(0에서 시작)을 모아, 작은 쪽에서 round(한 달 확률 × 경로 수)번째
    값(calibrate_line과 같은 동시 하한선).

    Args:
        r: 기간별 수익률(보수적 비용).
        horizon: 구간 길이(기간 수, 30일).
        block: 평균 블록 길이(기간 수).
        rng: 난수 생성기.

    Returns:
        {"low", "high": 기간 1~horizon 누적 분위수 배열, "lines": 민감도 → 선, "edge0": 기간별 범위 안 비율}.
    """
    idx = paths(rng, len(r), horizon, 2 * block)  # 중첩은 실효 블록을 약 절반으로 줄이므로 2L을 넘겨 실효 ≈ L
    cum = _compound(r[idx])
    low, high = np.percentile(cum, 5, axis=0), np.percentile(cum, 95, axis=0)
    mins = np.sort(np.minimum(cum.min(axis=1), 0.0))
    lines = {name: float(mins[max(0, round(rate * PATHS) - 1)]) for name, rate in SENSITIVITY.items()}
    zero = _compound((r - r.mean())[idx])  # 같은 경로에서 평균만 뺌 = 엣지 0
    return {"low": low, "high": high, "lines": lines, "edge0": ((zero >= low) & (zero <= high)).mean(axis=0)}

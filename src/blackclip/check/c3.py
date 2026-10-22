"""시도 수: 비용 0 샤프를 「N번 시도했다면」 깎는다.

점검은 시도 목록(훅 기록)을 쓰지 않아 Deflated Sharpe 원문 척도(시도 간 샤프 표준편차)를 못 쓴다.
그래서 표본 잡음 척도에 독립 가정을 둔 근사만 쓰고, 과하게 깎일 수 있다고 적는다.
"""
from __future__ import annotations

import math
from statistics import NormalDist

import pandas as pd

from blackclip.check.backtest import metrics

GAMMA = 0.5772156649
"""오일러-마스케로니 상수(최댓값 기댓값 근사)."""

TABLE: tuple[int, ...] = (10, 50, 100, 1_000, 10_000)
"""「N번 시도했다면」 표의 N(10 · 50 · 100에 예시 1,920 · 실측 166,496 규모를 덮는 둘을 더함)."""

INDEPENDENT = "독립 가정 · 과하게 깎일 수 있음"
"""표에 늘 붙이는 꼬리표(시도끼리 상관을 안 재서)."""


def emax(k: int) -> float:
    """독립 시도 k개 중 최댓값의 기댓값(표준정규 단위, 표준 라이브러리만).

    Args:
        k: 독립 시도 수.

    Returns:
        기댓값(k ≤ 1이면 0).
    """
    if k <= 1:
        return 0.0
    inv = NormalDist().inv_cdf
    # 위쪽 꼬리를 -inv(1/k)로: 1 - 1/k는 k가 크면 1.0으로 반올림돼 inv_cdf가 죽는다
    return -(1 - GAMMA) * inv(1 / k) - GAMMA * inv(1 / (k * math.e))


def _zero_at(sr: float, scale: float) -> int | None:
    """시도 수 깎임이 샤프를 0 이하로 만드는 가장 작은 정수 시도 수를 찾는다(2의 거듭제곱으로 범위를 잡고 이분 탐색).

    Args:
        sr: 비용 0 샤프(0 초과).
        scale: 샤프 표준오차(연환산).

    Returns:
        emax(k) * scale >= sr 인 가장 작은 k(2^40까지 찾고 그래도 안 닿으면 None).
    """
    for p in range(1, 41):
        if emax(2 ** p) * scale >= sr:
            lo, hi = 2 ** (p - 1), 2 ** p  # lo는 안 닿고(p=1은 emax(1)=0) hi는 닿는다
            while hi - lo > 1:
                mid = (lo + hi) // 2
                lo, hi = (lo, mid) if emax(mid) * scale >= sr else (mid, hi)
            return hi
    return None


def deflate(returns: pd.Series, bar_s: int, trials: tuple[int, str] | None) -> dict:
    """시도 수 숫자(가드레일 파일에도 그대로 남김).

    Args:
        returns: 비용 0으로 다시 돌린 봉별 수익률(거래비용 효과와 섞지 않음).
        bar_s: 봉 길이(초).
        trials: 연결 파일 TRIALS(없으면 None).

    Returns:
        {"sharpe_cost0", "bars", "trials", "source", "note", "method", "adjusted_low", "adjusted_high",
         "unverified", "table"([[시도 수, 깎인 샤프], …] 정수 순), "zero_at_trials"}(소수 6자리, 깎을 수 없으면 adjusted_low · table · zero_at_trials None).
    """
    sr = metrics(returns, bar_s)["sharpe"]
    n = len(returns)
    scale = math.sqrt(365.25 * 86400 / bar_s / n)  # 샤프 표준오차(연환산, 봉 n개)
    k, why = trials if trials else (None, None)
    live = sr > 0
    return {"sharpe_cost0": round(sr, 6), "bars": n, "trials": k, "source": "고객이 밝힌 값" if k else None,
            "note": why, "method": "독립 가정 근사", "unverified": True, "adjusted_high": None,
            "adjusted_low": round(sr - emax(k) * scale, 6) if k and live else None,
            "table": [[t, round(sr - emax(t) * scale, 6)] for t in TABLE] if live else None,  # 정수 순 목록(파일 sort_keys가 순서를 안 섞음)
            "zero_at_trials": _zero_at(sr, scale) if live else None}


def c3(returns: pd.Series, bar_s: int, trials: tuple[int, str] | None) -> dict[str, str]:
    """비용 0 수익률의 샤프를 시도 수로 깎은 값을 보인다.

    Args:
        returns: 비용 0으로 다시 돌린 봉별 수익률.
        bar_s: 봉 길이(초).
        trials: 연결 파일 TRIALS(없으면 None).

    Returns:
        {"state": 하향(범위) · 모름 · 해당 없음, "text": 근거}.
    """
    d = deflate(returns, bar_s, trials)
    base = f"비용 0 샤프 {d['sharpe_cost0']:.2f}({d['bars']:,}봉)"
    if d["table"] is None:
        return {"state": "해당 없음", "text": f"{base} · 0 이하라 깎을 것이 없음"}
    table = " · ".join(f"{t:,}회면 {v:.2f}" for t, v in d["table"])
    zero = d["zero_at_trials"]
    tail = f"샤프가 0 이하가 되는 시도 수 약 {zero:,}개" if zero else "시도 2^40개로도 0 이하가 안 됨"
    text = f"N번 시도했다면({INDEPENDENT}): {table} · {tail}"
    if d["trials"] is None:
        return {"state": "모름", "text": f"{base} · 시도 수 기록 없음(훅 기록 전) · {text}"}
    return {"state": "하향(범위)",
            "text": f"샤프 {d['sharpe_cost0']:.2f} → {d['adjusted_low']:.2f} ~ 모름 (시도 {d['trials']:,}개: 고객이 밝힌 값 · "
                    f"최솟값(미검증) · {d['note']}) · 아래쪽은 {INDEPENDENT} · 위쪽은 시도끼리 상관을 재야 나옴 · {base} · {text}"}

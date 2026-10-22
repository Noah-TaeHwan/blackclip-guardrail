"""거래비용: 보수적 비용으로 다시 돌려 기준(고객 보고값 또는 비용 0)과 맞댄다."""
from __future__ import annotations

from pathlib import Path

import pandas as pd

from blackclip.check.backtest import metrics, simulate
from blackclip.check.link import Link
from blackclip.check.source import scan

CONSERVATIVE_BP = 30
"""보수적 비용(편도 bp, 코인 표: 수수료 + 스프레드 절반 + 슬리피지)."""

SHRINK = 0.8
"""보수적 연 수익률이 기준의 이 배수보다 작으면 축소."""

GAP = 0.2
"""고객 보고값과 비용 0으로 다시 돌린 값이 큰 쪽 크기의 이 비율 이상 벌어지면 재현부터 확인하라고 적는다."""

MID: list[tuple[str, str, str | None]] = [
    (r"High['\"]?\]?\s*\+\s*\S*Low", "고가 · 저가 평균", None),
    (r"(?i)(?<![a-z0-9])mid_?price", "mid 가격", None),
    (r"(?i)(?<![a-z0-9])mid_?fill", "mid 체결", None),
]
"""중간가 체결 가정으로 볼 만한 줄(신호 함수만으로는 못 찾아 고객 코드에서 보일 때만 「발견」).
이진 탐색 · 볼린저의 흔한 변수 `mid`는 중간가가 아니라 따로 넣지 않는다."""


def c2(link: Link, prices: dict[str, pd.DataFrame], weights: dict[str, pd.Series], bar_s: int,
       files: list[Path]) -> dict[str, str]:
    """비용 0과 보수적 비용으로 다시 돌려 연 수익률을 맞댄다.

    Args:
        link: 연결 파일(result_cagr가 있으면 그것이 기준).
        prices: 종목 → 가격표.
        weights: 목표 비중.
        bar_s: 봉 길이(초).
        files: 정적 검사할 파일(중간가 체결 가정 찾기).

    Returns:
        {"state": 유지 · 축소 · 소멸 · 해당 없음, "text": 근거}.
    """
    zero = metrics(simulate(prices, weights, link.exec_).returns, bar_s)["cagr"]
    high = metrics(simulate(prices, weights, link.exec_, CONSERVATIVE_BP).returns, bar_s)["cagr"]
    base, label = (link.result_cagr, "고객 보고값") if link.result_cagr is not None else (zero, "비용 0으로 다시 돌린 값")
    if base <= 0:
        state = "해당 없음"
    elif high <= 0:
        state = "소멸"
    elif high < base * SHRINK:
        state = "축소"
    else:
        state = "유지"
    text = f"연 {base:.1%} → {high:.1%} (보수적 비용 편도 {CONSERVATIVE_BP}bp) · 기준: {label}"
    if link.result_cagr is not None:  # 보고값과 비용 0 재실행이 다르면 줄어든 몫을 비용 탓으로만 읽지 않게
        text += f" · 비용 0으로 다시 돌린 값 {zero:.1%}"
        big = max(abs(base), abs(zero))
        if big > 0 and abs(base - zero) >= GAP * big:
            text += " · 보고값과 다시 돌린 값 차이 큼, 재현부터 확인"
    if state == "해당 없음":
        text += " · 기준 수익이 0 이하라 비교하지 않음"
    mids: dict[str, str] = {}
    for where, name in scan(files, link.repo, MID):
        mids.setdefault(where, name)  # 한 줄이 여러 패턴에 걸려도 한 곳
    if mids:
        text += " · 중간가 체결 가정 발견: " + " · ".join(f"{w} {name}" for w, name in list(mids.items())[:3])
        if len(mids) > 3:
            text += f" 외 {len(mids) - 3}곳"
    return {"state": state, "text": text}

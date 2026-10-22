"""국면 편중: 왕복 거래 수와 진입 때 국면(상승 · 횡보 · 하락)으로 표본이 고른지 본다.

국면은 종목 자기 종가의 200일 이동평균 위 · 아래와 그 이동평균의 20일 변화 부호로 나눈다(MA200 규칙에
새 % 문턱을 더하지 않으려는 추천값, 근거 문서 없음).
"""
from __future__ import annotations

import math

import pandas as pd

from blackclip.check.c2 import CONSERVATIVE_BP
from blackclip.check.trades import Trade

MIN_TRADES = 30
"""이보다 적으면 거래 부족(판단 보류 30건과 같은 숫자)."""

MIN_SHARE = 0.1
"""한 국면이라도 max(MIN_TRADES, 분류된 거래 × 이 비율)보다 적으면 국면 편중."""

REGIMES: tuple[str, ...] = ("상승", "횡보", "하락")
"""국면 이름(화면 순서)."""


def regimes(close: pd.Series, bar_s: int) -> pd.Series:
    """봉마다 국면을 매긴다. 200일 + 20일이 안 찬 봉은 NaN.

    Args:
        close: 종가.
        bar_s: 봉 길이(초).

    Returns:
        "상승" · "횡보" · "하락" 또는 NaN.
    """
    ma = close.rolling(max(1, round(200 * 86400 / bar_s))).mean()
    slope = ma - ma.shift(max(1, round(20 * 86400 / bar_s)))
    up = (close > ma) & (slope > 0)
    down = (close < ma) & (slope < 0)
    return pd.Series("횡보", index=close.index).mask(up, "상승").mask(down, "하락").where(slope.notna())


def regime_stats(trades: list[Trade], prices: dict[str, pd.DataFrame], bar_s: int) -> dict[str, dict[str, float]]:
    """국면별 거래 수와 거래 1건 평균 수익률(진입 봉 국면 기준).

    Args:
        trades: 왕복 거래(보수적 비용 반영).
        prices: 종목 → 가격표.
        bar_s: 봉 길이(초).

    Returns:
        {"상승" · "횡보" · "하락": {"trades", "mean"(거래가 있을 때만)}, "분류 안 됨": {"trades"}}.
    """
    tags = {s: regimes(p["Close"], bar_s) for s, p in prices.items()}
    groups: dict[str, list[float]] = {r: [] for r in REGIMES}
    skipped = 0
    for t in trades:
        tag = tags[t.symbol].get(t.entry)
        if isinstance(tag, str):
            groups[tag].append(t.ret)
        else:
            skipped += 1
    out: dict[str, dict[str, float]] = {r: {"trades": len(v), **({"mean": sum(v) / len(v)} if v else {})}
                                        for r, v in groups.items()}  # 0건이면 평균 키 없음(모르면 모름)
    out["분류 안 됨"] = {"trades": skipped}
    return out


def c4(stats: dict[str, dict[str, float]]) -> dict[str, str]:
    """거래 부족 → 국면 편중 → 충분 순서로 판정한다(세 국면이 모두 문턱 밑이면 편중이 아니라 거래 부족).

    Args:
        stats: regime_stats 결과.

    Returns:
        {"state": 충분 · 국면 편중 · 거래 부족, "text": 근거}.
    """
    classified = sum(stats[r]["trades"] for r in REGIMES)
    total = classified + stats["분류 안 됨"]["trades"]
    floor = max(MIN_TRADES, MIN_SHARE * classified)
    parts = " · ".join(f"{r} {stats[r]['trades']:,}({stats[r]['trades'] / classified:.0%}, 평균 {stats[r]['mean']:+.2%})"
                       if stats[r]["trades"] else f"{r} 0" for r in REGIMES)
    need = math.ceil(floor)  # 거래 수는 정수라 30.5 밑이면 「31건보다 적음」이 맞다
    text = (f"왕복 거래 {total:,}건 중 분류된 {classified:,}건 기준 {parts} · 국면을 못 나눈 거래(200일선 + 20일이 차기 전 진입) "
            f"{stats['분류 안 됨']['trades']:,}건 · "
            f"거래 1건 = 보수적 비용 왕복 {2 * CONSERVATIVE_BP}bp 뺌 · 국면 = 종목 200일선 위 · 아래와 20일 기울기")
    if classified < MIN_TRADES:  # classified <= total
        return {"state": "거래 부족", "text": f"국면을 나눈 거래 {classified:,}건 < {MIN_TRADES}건 · {text}"}
    thin = [r for r in REGIMES if stats[r]["trades"] < floor]
    if len(thin) == len(REGIMES):  # 고르게 적은 것은 편중이 아니라 부족
        return {"state": "거래 부족", "text": f"국면마다 {need:,}건이 안 됨 · {text}"}
    if thin:
        return {"state": "국면 편중", "text": f"{' · '.join(thin)} 거래가 {need:,}건보다 적음 · {text}"}
    return {"state": "충분", "text": text}

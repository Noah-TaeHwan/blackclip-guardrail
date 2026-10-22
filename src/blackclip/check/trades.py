"""목표 비중에서 왕복 거래(사고 판 한 번)를 뽑는다.

다시 돌린 체결 목록은 비중이 바뀐 봉마다라서 거래 수가 아니다. 종목마다 체결 봉 비중(shift(LAG) 뒤)을 따라
0 → 0 아님이면 진입, 0이 되거나 부호가 바뀌면 청산으로 센다. 크기만 바뀐 것(0.2 → 0.1)은 같은 거래다.
"""
from __future__ import annotations

from typing import NamedTuple

import pandas as pd

from blackclip.check.backtest import FILL, LAG, MIN_CHANGE


class Trade(NamedTuple):
    """왕복 거래 하나.

    Attributes:
        symbol: 종목.
        entry: 진입 체결 봉 시각(UTC).
        exit: 청산 체결 봉 시각(UTC).
        bars: 들고 있던 봉 수.
        ret: 방향 × (청산가 / 진입가 − 1) − 왕복 비용(크기는 보지 않음).
    """

    symbol: str
    entry: pd.Timestamp
    exit: pd.Timestamp
    bars: int
    ret: float


def round_trips(prices: dict[str, pd.DataFrame], weights: dict[str, pd.Series], exec_: str,
                cost_bp: float) -> list[Trade]:
    """종목마다 왕복 거래를 뽑는다. 끝까지 열린 거래는 뺀다.

    Args:
        prices: 종목 → 가격표.
        weights: 종목 → 목표 비중(그 종목 가격표 시각).
        exec_: 체결 규칙("next_open" · "close").
        cost_bp: 편도 비용(bp). 거래 1건 수익률에서 두 번 뺀다.

    Returns:
        청산 시각 · 종목 순 거래 목록.
    """
    out: list[Trade] = []
    for sym in sorted(weights):
        held = weights[sym].shift(LAG[exec_]).fillna(0.0).to_numpy()
        fill = prices[sym][FILL[exec_]].to_numpy()
        index = weights[sym].index
        side, start = 0, 0
        for i, w in enumerate(held):
            s = 0 if abs(w) <= MIN_CHANGE else (1 if w > 0 else -1)
            if s == side:
                continue
            if side:
                out.append(Trade(sym, index[start], index[i], i - start,
                                 side * (fill[i] / fill[start] - 1) - 2 * cost_bp / 1e4))
            side, start = s, i
    return sorted(out, key=lambda t: (t.exit, t.symbol))

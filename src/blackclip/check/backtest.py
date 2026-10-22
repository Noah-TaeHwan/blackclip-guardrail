"""목표 비중을 체결 규칙대로 옮겨 봉별 수익률과 체결 목록을 만든다(여러 종목)."""
from __future__ import annotations

import math
from collections import defaultdict
from dataclasses import dataclass
from decimal import Decimal

import numpy as np
import pandas as pd

from blackclip.retro.fills import Execution

LAG: dict[str, int] = {"next_open": 1, "close": 0}
"""신호 봉에서 체결 봉까지."""

FILL: dict[str, str] = {"next_open": "Open", "close": "Close"}
"""체결가 열."""

MIN_CHANGE: float = 1e-9
"""이보다 작은 비중 변화는 부동소수 오차로 보고 체결로 세지 않는다."""


@dataclass(frozen=True)
class Run:
    """한 번 다시 돌린 결과.

    Attributes:
        returns: 봉별 포트폴리오 수익률(모든 종목 시각의 합집합, 오름차순).
        executions: 비중이 바뀐 체결(시각 · 종목 · 방향 순, qty = 바뀐 비중, price = 체결가).
    """

    returns: pd.Series
    executions: list[Execution]


def simulate(prices: dict[str, pd.DataFrame], weights: dict[str, pd.Series], exec_: str, cost_bp: float = 0.0) -> Run:
    """비중을 체결가에서 다음 체결가까지 들고 간다고 보고 수익률을 셈한다(봉마다 목표 비중으로 다시 맞추는 포트폴리오).

    비용은 실제로 사고판 양에 매긴다: 목표 비중과, 지난 봉 수익으로 흘러간 실제 비중의 차이.
    체결 목록은 목표 비중이 바뀐 봉만 남긴다(고객 백테스트도 다시 맞추기 매매는 거래로 안 적음).

    Args:
        prices: 종목 → OHLC(UTC 시각).
        weights: 종목 → 목표 비중(그 종목 가격표 시각).
        exec_: "next_open" 또는 "close".
        cost_bp: 편도 비용(bp, 사고판 비중 1당).

    Returns:
        Run.
    """
    active: dict[str, pd.Series] = {}
    moves: dict[str, pd.Series] = {}
    execs: list[Execution] = []
    for sym in sorted(weights):
        f = prices[sym][FILL[exec_]]
        a = weights[sym].shift(LAG[exec_]).fillna(0.0)
        active[sym], moves[sym] = a, (f.shift(-1) / f - 1).fillna(0.0)
        delta = a.diff().fillna(a)
        changed = delta.abs() > MIN_CHANGE
        for i, (t, d, price) in enumerate(zip(a.index[changed], delta[changed], f[changed])):
            execs.append(Execution(t.to_pydatetime(), sym, "buy" if d > 0 else "sell",
                                   Decimal(str(round(abs(float(d)), 10))), Decimal(str(float(price))),
                                   Decimal(0), "", f"bar{i}"))
    # shortcut: 종목 시각에 빈 봉이 있으면 그 봉은 비중 0으로 보고 사고판 것으로 센다, 빈 봉 많은 데이터가 오면 앞 값 채우기로
    held = pd.DataFrame(active).sort_index().fillna(0.0)
    move = pd.DataFrame(moves).reindex(held.index).fillna(0.0)
    gross = (held * move).sum(axis=1)
    drifted = (held * (1 + move)).div(1 + gross, axis=0).replace([np.inf, -np.inf], np.nan).fillna(0.0)
    traded = (held - drifted.shift(1).fillna(0.0)).abs().sum(axis=1)
    execs.sort(key=lambda e: (e.time, e.symbol, e.side))
    return Run(gross - traded * cost_bp / 1e4, execs)


def metrics(returns: pd.Series, bar_s: int) -> dict[str, float]:
    """연 수익률과 샤프(연환산은 봉 길이로). 자산이 한 번이라도 0 이하가 되면 연 수익률 -100%(파산).

    Args:
        returns: 봉별 수익률.
        bar_s: 봉 길이(초).

    Returns:
        {"cagr", "sharpe"}.
    """
    year = 365.25 * 86400
    eq = (1 + returns).cumprod()
    years = max((returns.index[-1] - returns.index[0]).total_seconds() / year, 1e-9)
    sd = float(returns.std())
    last = float(eq.iloc[-1])
    ruined = bool((eq <= 0).any())  # 음수 자산을 계속 곱하면 다시 살아난 것처럼 보인다
    return {"cagr": -1.0 if ruined else last ** (1 / years) - 1,
            "sharpe": float(returns.mean()) / sd * math.sqrt(year / bar_s) if sd > 0 else 0.0}


def _bars(execs: list[Execution], prices: dict[str, pd.DataFrame]) -> dict[tuple[str, str], list[int]]:
    """체결을 (종목, 방향)별 봉 번호(그 시각을 담은 봉) 목록으로 바꾼다.

    Args:
        execs: 체결 목록(종목은 prices에 있어야 함).
        prices: 종목 → 가격표.

    Returns:
        (종목, 방향) → 오름차순 봉 번호.
    """
    out: dict[tuple[str, str], list[int]] = defaultdict(list)
    for e in execs:
        pos = int(prices[e.symbol].index.searchsorted(pd.Timestamp(e.time).tz_convert("UTC"), side="right")) - 1
        if pos >= 0:  # 첫 봉 앞 체결은 짝이 없다
            out[(e.symbol, e.side)].append(pos)
    return {k: sorted(v) for k, v in out.items()}


def count_matches(a: list[Execution], b: list[Execution], prices: dict[str, pd.DataFrame], tolerance: int = 1) -> int:
    """같은 종목 · 방향 체결을 봉 번호 차이 tolerance 안에서 짝지은 수(한 체결은 한 번만).

    두 목록을 봉 번호 순으로 놓고 앞에서부터 짝짓는다(허용 폭이 같은 1차원 짝짓기는 이것이 최대).

    Args:
        a: 체결 목록.
        b: 체결 목록.
        prices: 종목 → 가격표(봉 번호를 정함).
        tolerance: 허용 봉 차이.

    Returns:
        짝 수.
    """
    xa, xb = _bars(a, prices), _bars(b, prices)
    hit = 0
    for key in xa.keys() & xb.keys():
        x, y, i, j = xa[key], xb[key], 0, 0
        while i < len(x) and j < len(y):
            if abs(x[i] - y[j]) <= tolerance:
                hit, i, j = hit + 1, i + 1, j + 1
            elif x[i] < y[j]:
                i += 1
            else:
                j += 1
    return hit

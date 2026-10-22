"""같은 기간 백테스트: 거래소 기록 가격으로 전체 이력부터 신호를 다시 돌려 실전 구간만 자른다."""
from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field, replace
from datetime import datetime, timedelta
from typing import Any

import pandas as pd

from blackclip.binance.client import BinanceError
from blackclip.binance.klines import fetch_klines
from blackclip.check.backtest import Run, simulate
from blackclip.check.c2 import CONSERVATIVE_BP
from blackclip.check.datadiff import INVALID_SYMBOL, _ms
from blackclip.check.link import INTERVALS
from blackclip.retro.fills import Execution


@dataclass(frozen=True)
class LiveRun:
    """실전 구간으로 자른 같은 기간 백테스트.

    Attributes:
        frames: 종목 → 바이낸스 봉(전체 이력).
        zero: 실전 구간 봉별 수익률, 비용 0(실행 축 기준: 수수료 · 체결가 차이는 따로 잰다).
        cons: 실전 구간 봉별 수익률, 보수적 비용(전략 축: 정상 범위와 같은 비용 가정).
        executions: 실전 구간 체결(비용 0 판, qty는 비중 변화).
        target: 종목 → 마지막으로 닫힌 봉의 목표 비중(자주 보는 시계의 포지션 대조).
    """

    frames: dict[str, pd.DataFrame]
    zero: pd.Series
    cons: pd.Series
    executions: list[Execution]
    target: dict[str, float] = field(default_factory=dict)


def binance_frames(client: Any, symbols: list[str], bar_s: int, start: datetime,
                   end: datetime) -> tuple[dict[str, pd.DataFrame], list[str]]:
    """가드레일 종목의 바이낸스 공개 봉을 [start, end)로 받는다.

    shortcut: 매일 전체 이력을 다시 받는다(캐시 없음), 시간봉 · 종목 많은 고객이 오면 .blackclip/bars 캐시로.

    Args:
        client: market 속성과 get이 있는 공개 봉 클라이언트.
        symbols: 종목.
        bar_s: 봉 길이(초, INTERVALS에 있어야 함).
        start: 시작(포함).
        end: 끝(제외, 오늘 0시 UTC).

    Returns:
        (종목 → 봉, 봉이 없거나 바이낸스에 없는 종목).

    Raises:
        KeyError: 봉 길이를 바이낸스 단위로 못 바꿀 때.
        BinanceError · OSError: 받기 실패(호출자가 확인 불가로).
    """
    interval = INTERVALS[bar_s]
    got: dict[str, pd.DataFrame] = {}
    absent: list[str] = []
    for sym in symbols:
        try:
            k = fetch_klines(client, sym, interval, _ms(start), _ms(end))
        except BinanceError as exc:
            if exc.code != INVALID_SYMBOL:
                raise
            absent.append(sym)
            continue
        k = k[k.index + pd.Timedelta(seconds=bar_s) <= pd.Timestamp(end)]  # 끝이 end를 넘는 봉은 진행 중(3d · 1w)
        if len(k):
            got[sym] = k
        else:
            absent.append(sym)
    return got, absent


def live_run(signals: Callable[[dict[str, pd.DataFrame]], dict[str, pd.Series]], frames: dict[str, pd.DataFrame],
             exec_: str, bar_s: int, live_start: datetime, end: datetime) -> LiveRun:
    """신호를 전체 이력으로 돌리고 실전 구간 [live_start, end)만 남긴다.

    Args:
        signals: 표 → 확인한 목표 비중(Runner.signals(frames=…)).
        frames: 종목 → 바이낸스 봉.
        exec_: 체결 규칙.
        bar_s: 봉 길이(초). 종가 체결은 수익률 · 체결 시각을 실제 체결 시각(봉 시작 + bar_s)으로 옮겨 자른다.
        live_start: 실전 시작(UTC).
        end: 끝(제외, UTC).

    Returns:
        LiveRun.
    """
    weights = signals(frames)
    # 끝 시각에 마지막 봉 종가로 한 줄을 붙여 마지막 봉의 체결가 → 종가 구간도 든다(실제 손익과 같은 마감 종가).
    # 붙인 줄(끝 시각)의 체결 · 수익률은 아래에서 잘린다.
    priced = {}
    for s, f in frames.items():
        tail = f.iloc[[-1]].copy()
        tail[["Open", "High", "Low", "Close"]] = float(f["Close"].iloc[-1])
        tail.index = pd.DatetimeIndex([pd.Timestamp(end)])
        priced[s] = pd.concat([f, tail]) if f.index[-1] < pd.Timestamp(end) else f
    held = {s: w.reindex(priced[s].index).ffill() for s, w in weights.items()}
    zero = simulate(priced, held, exec_)
    cons = simulate(priced, held, exec_, CONSERVATIVE_BP)
    if exec_ == "close":  # 종가 체결은 봉이 끝날 때 일어난다 — 실제 체결 · 실제 손익과 같은 시각으로
        step = pd.Timedelta(seconds=bar_s)
        zero = Run(zero.returns.set_axis(zero.returns.index + step),
                   [replace(e, time=e.time + timedelta(seconds=bar_s)) for e in zero.executions])
        cons = Run(cons.returns.set_axis(cons.returns.index + step), cons.executions)
    lo, hi = pd.Timestamp(live_start), pd.Timestamp(end)

    def cut(r: pd.Series) -> pd.Series:
        """실전 구간만.

        Args:
            r: 봉별 수익률.

        Returns:
            잘린 수익률.
        """
        return r[(r.index >= lo) & (r.index < hi)]

    # 체결은 끝 시각(포함)까지 둔다: 종가 체결의 BT 시각(봉 끝)에 자정 직전 실제 체결이 짝을 찾도록. 기간 자르기는 build_retro가 한다.
    target = {s: 0.0 if w.empty or pd.isna(w.iloc[-1]) else float(w.iloc[-1]) for s, w in held.items()}
    return LiveRun(frames, cut(zero.returns), cut(cons.returns),
                   [e for e in zero.executions if live_start <= e.time <= end], target)

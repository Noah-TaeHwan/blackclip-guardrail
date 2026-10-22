"""실제 손익: 실전 시작 잔고 · 포지션과 그 뒤 체결 · 펀딩비로 하루 끝마다 손익을 다시 셈한다(손익 3줄의 「실거래」).

실제 손익 = 출발 포지션을 실전 시작일 UTC 종가로 평가한 값에서 출발 + 이후 체결 현금 흐름 + 펀딩비 + 하루 끝 종가 평가.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from decimal import Decimal

import pandas as pd

from blackclip.retro.fills import Execution, Funding

FLAT_SHARE = Decimal("0.01")
"""포지션이 그 거래 최대 크기의 이 비율 이하면 0으로 본다(수수료 · 수량 단위 부스러기). 왕복 거래 끝 판정에만 쓴다."""


@dataclass(frozen=True)
class Actual:
    """실제 손익.

    Attributes:
        pnl: 하루 끝(UTC 0시) → 출발(실전 시작일 UTC 종가 평가)부터 누적 손익(거래 통화).
        exits: 왕복 거래가 끝난 시각(시각 순, 기간 끝 전).
        trips: 거래 1건 수익(청산 시각, 수익) — 백테스트 `round_trips`와 같은 정의: 방향 × (닫는 체결가 / 연 체결가 − 1)
            − (연 체결 수수료율 + 닫는 체결 수수료율). 실전 전 포지션으로 시작한 거래는 연 값이 없어 exits에만 있다(청산 − 표본 = 제외 수).
        problem: 계산을 믿을 수 없는 까닭(점검 불가 사유) 또는 None.
        capital: 기준 자본(손익 %의 분모, 거래 통화). `blackclip live --capital`을 줬으면 그 값, 아니면 출발 자산
            (현금 + 출발 포지션 × 실전 시작일 UTC 종가). 못 셌으면 None.
    """

    pnl: pd.Series
    exits: list[datetime]
    problem: str | None
    capital: Decimal | None
    trips: list[tuple[datetime, float]] = field(default_factory=list)


def _close_before(closes: dict[str, pd.Series], sym: str, t: datetime) -> Decimal | None:
    """t 전에 시작한 마지막 봉의 종가.

    Args:
        closes: 종목 → 종가(봉 시작 시각 인덱스).
        sym: 종목.
        t: 시각(제외).

    Returns:
        종가, 없으면 None.
    """
    c = closes.get(sym)
    before = c[c.index < pd.Timestamp(t)] if c is not None else None
    return None if before is None or before.empty else Decimal(str(float(before.iloc[-1])))


def actual_pnl(fills: list[Execution], funding: list[Funding], closes: dict[str, pd.Series], snap: dict,
               ends: list[datetime], end: datetime, spot: bool) -> Actual:
    """하루 끝마다 출발 포지션 평가 + 이후 체결 현금 흐름 + 펀딩비 + 보유분 × 그 직전 봉 종가로 손익을 낸다.

    받은 코인으로 낸 수수료(BTCUSDT의 BTC)는 포지션에서, 거래 통화로 낸 수수료는 현금에서 뺀다.
    다른 자산(BNB 등) 수수료는 `<자산><거래 통화>` 종가로 환산한다(환산한 금액만 추정). 그 종가가 없으면 점검 불가.

    Args:
        fills: 실제 체결(이 전략 종목만).
        funding: 펀딩비(이 전략 종목만).
        closes: 종목 → 바이낸스 종가(봉 시작 시각 인덱스). 수수료 자산 환산용 `<자산><거래 통화>`도 여기.
        snap: notify.read_live 결과(quote · taken_at · start · cash · positions · capital). capital이 있으면 분모만 바꾸고
            손익(분자) · 출발 포지션 평가는 그대로다.
        ends: 손익을 낼 하루 끝 시각들(오름차순, 첫 값은 출발 시각 start).
        end: 기간 끝(제외). 이 뒤의 체결 · 펀딩비는 보지 않는다.
        spot: 현물이면 True(가진 양보다 많이 팔면 점검 불가).

    Returns:
        Actual.
    """
    quote, since, start = snap["quote"], snap["taken_at"], snap["start"]
    pos0: dict[str, Decimal] = {s: q for s, q in snap["positions"].items() if q}
    problem = None
    flows = []  # (시각, 종목, 포지션 변화, 현금 변화, 체결가, 수수료율 — 환산 못 한 수수료는 0, 그날은 이미 점검 불가)
    for e in sorted((e for e in fills if since <= e.time < end), key=lambda e: e.time):
        sign = Decimal(1) if e.side == "buy" else Decimal(-1)
        base_fee = e.fee if e.fee_asset and e.symbol == e.fee_asset + quote else Decimal(0)
        cash_fee = e.fee if e.fee_asset == quote else Decimal(0)
        if e.fee and e.fee_asset and not base_fee and not cash_fee:
            rate = _close_before(closes, e.fee_asset + quote, e.time)
            if rate is None:
                problem = problem or f"기록 불완전: {e.fee_asset} 수수료를 {quote}로 환산할 종가 없음"
            else:
                cash_fee = e.fee * rate
        flows.append((e.time, e.symbol, sign * e.qty - base_fee, -sign * e.qty * e.price - cash_fee, e.price,
                      (cash_fee + base_fee * e.price) / (e.qty * e.price) if e.qty * e.price else Decimal(0)))
    funds = [f for f in funding if since <= f.time < end and f.asset == quote]
    exits: list[datetime] = []
    trips: list[tuple[datetime, float]] = []
    for sym in sorted(set(pos0) | {f[1] for f in flows}):
        pos = pos0.get(sym, Decimal(0))
        peak = abs(pos)
        side = 0 if not pos else (1 if pos > 0 else -1)
        opened: tuple[Decimal, Decimal] | None = None  # 연 체결가 · 수수료율(실전 전 포지션으로 시작한 거래는 None)
        for t, _, dpos, _, px, rate in (f for f in flows if f[1] == sym):
            pos += dpos
            if spot and pos < 0:  # 부스러기 허용 전에 본다
                problem = problem or f"기록 불완전: {sym}를 가진 양보다 많이 팖(출발 잔고 + 받은 체결 기준 · 빠진 체결로 보임)"
            peak = max(peak, abs(pos))
            now = 0 if abs(pos) <= peak * FLAT_SHARE else (1 if pos > 0 else -1)
            if side and now != side:
                exits.append(t)
                if opened is not None:  # 청산 고리 하나에서 exits · trips를 함께 셈(거래 수가 어긋나지 않게)
                    trips.append((t, float(side * (px / opened[0] - 1) - (opened[1] + rate))))
                peak = abs(pos) if now else Decimal(0)
                opened = None
            if now and now != side:  # 열림(평평 → 열림) 또는 반전: 크기 변화는 연 값을 바꾸지 않음
                opened = (px, rate)
            side = now
    c0 = {s: _close_before(closes, s, start) for s in pos0}
    if any(v is None for v in c0.values()):
        problem = problem or "기록 불완전: 출발 포지션을 평가할 실전 시작일 종가 없음"
    base0 = sum((q * (c0[s] or 0) for s, q in pos0.items()), Decimal(0))
    fixed = snap.get("capital")  # 고객이 정한 기준 자본이 있으면 분모로, 없으면 계좌 잔고 전체 + 출발 포지션
    capital = fixed if fixed is not None else snap["cash"] + base0  # 선물 cash = 지갑 − Σ 포지션 × 진입가라 같은 식
    values = []
    for t_end in ends:
        total = (sum((f[3] for f in flows if f[0] < t_end), Decimal(0))
                 + sum((f.amount for f in funds if f.time < t_end), Decimal(0)) - base0)
        for sym in set(pos0) | {f[1] for f in flows}:
            pos = pos0.get(sym, Decimal(0)) + sum((f[2] for f in flows if f[1] == sym and f[0] < t_end), Decimal(0))
            if not pos:
                continue
            c = _close_before(closes, sym, t_end)
            if c is None:
                problem = problem or f"기록 불완전: {sym} 종가 없음"
                continue
            total += pos * c
        values.append(float(total))
    return Actual(pd.Series(values, index=pd.DatetimeIndex(ends, tz="UTC"), dtype=float), sorted(exits), problem,
                  capital if capital > 0 else None, sorted(trips))

"""구간 수익: 확인마다 이은 단계 손익, 바깥 흐름(입출금 · 지갑 이동 · 코인 입금)은 손익에서 뺀다.

보유 손익 = Σ 이전 보유 × (지금 가격 − 이전 가격) + 펀딩비. 체결 손익 = Σ 두 확인 사이 체결 부호 수량 × (지금 가격 − 체결가) − 수수료
(수수료는 체결 몫). 체결은 주문으로 묶지 않고 하나하나 제 시각에 센다(확인 경계를 넘는 부분 체결).
바깥 흐름 = 자산 변화 − 보유 손익 − 체결 손익.
단계 수익률 r = 보유 손익 / 지난 확인 자산 + 체결 손익 / (지난 확인 자산 + max(0, 흐름)). 같은 시간에 입금해 바로 산 체결은 입금까지
나누고, 출금하며 한 체결은 지난 자산으로 나눈다. 구간 수익 = Π max(1 + r, 0) − 1, 복리로 이어 저장된 경고선(check.band._compound,
「구간마다 0에서 복리 누적」)과 같은 방식이다. 손실 뒤 출금이나 같은 단계 입금이 구간 수익을 흔들지 않는다.
매일 대조의 체결 기준 %와 섞지 않는다. 금액은 Decimal로 끝까지 센다(가격 출처는 확인마다 하나, 그 가격을 함께 남긴다).
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal

from blackclip.retro.fills import Execution, Funding

CAPTURED = "구간 기준 잡음"
"""30일 구간의 첫 확인(또는 다시 잡음)에서 기준 자산을 잡았을 때 근거에 붙는 말."""

NOT_DIVIDED = "체결을 다 받지 못해 이번 단계는 나누지 않음(다음 확인에서 이어 셈)"
"""두 확인 사이 체결을 다 받지 못했을 때(손익 · 최저 그대로, 경고 안 함)."""


@dataclass(frozen=True)
class Mark:
    """한 번의 확인에서 본 계좌.

    Attributes:
        at: 확인 시각(UTC, 체결 받기의 끝과 같음).
        cash: 거래 통화(notify.snapshot의 현금, 선물은 지갑 − Σ 포지션 × 진입가).
        hold: 종목 → 보유 수량(선물은 부호 있는 포지션). 가드레일 종목이 모두 있다(안 들고 있으면 0, notify.snapshot과 같음).
        price: 종목 → 이 확인에서 쓴 지금 가격(출처 하나). hold와 같은 종목이 모두 있다.
    """

    at: datetime
    cash: Decimal
    hold: dict[str, Decimal]
    price: dict[str, Decimal]

    def to_json(self) -> dict:
        """상태 파일에 쓸 모양.

        Returns:
            {"at", "cash", "hold", "price"}(숫자는 글자).
        """
        return {"at": self.at.isoformat(), "cash": str(self.cash), "hold": {s: str(q) for s, q in self.hold.items()},
                "price": {s: str(p) for s, p in self.price.items()}}

    @staticmethod
    def from_json(d: dict) -> Mark:
        """상태 파일 모양에서 되살린다.

        Args:
            d: to_json 결과.

        Returns:
            Mark.
        """
        return Mark(datetime.fromisoformat(d["at"]), Decimal(d["cash"]), {s: Decimal(q) for s, q in d["hold"].items()},
                    {s: Decimal(p) for s, p in d["price"].items()})


def equity(m: Mark) -> Decimal:
    """지금 자산 = 현금 + Σ 보유 × 지금 가격.

    Args:
        m: 확인.

    Returns:
        거래 통화 금액.
    """
    return m.cash + sum((q * m.price[s] for s, q in m.hold.items()), Decimal(0))


def step(prev: Mark, now: Mark, fills: list[Execution], funding: list[Funding],
         quote: str) -> tuple[Decimal, Decimal, Decimal]:
    """두 확인 사이의 보유 손익 · 체결 손익 · 바깥 흐름.

    Args:
        prev: 지난 확인.
        now: 이번 확인(종목이 prev와 같아야 함).
        fills: 체결 하나하나(주문으로 묶지 않은 것, read_executions(per_fill=True), 이 전략 종목, [prev.at, now.at) 밖은 무시).
        funding: 펀딩비(거래 통화만, [prev.at, now.at) 밖은 무시).
        quote: 거래 통화.

    Returns:
        (보유 손익, 체결 손익, 바깥 흐름).
    """
    hold = sum((q * (now.price[s] - prev.price[s]) for s, q in prev.hold.items()), Decimal(0))
    hold += sum((f.amount for f in funding if f.asset == quote and prev.at <= f.time < now.at), Decimal(0))
    fill = Decimal(0)
    for e in fills:
        if not prev.at <= e.time < now.at:
            continue
        sign = Decimal(1) if e.side == "buy" else Decimal(-1)
        fill += sign * e.qty * (now.price[e.symbol] - e.price)
        if e.fee_asset == quote:
            fill -= e.fee
        elif e.fee_asset and e.fee_asset + quote in now.price:  # 지켜보는 자산(받은 코인 · 다른 가드레일 종목)으로 낸 수수료는 이번 가격으로
            fill -= e.fee * now.price[e.fee_asset + quote]
        # shortcut: 지켜보는 자산 밖(가드레일 종목이 아닌 BNB 등)으로 낸 수수료는 자산 변화에도 안 보여 빼지 않음(미검증), 그 자산을 셈에 넣을 때 고침
    return hold, fill, equity(now) - equity(prev) - hold - fill


def window_return(win: dict) -> float | None:
    """구간 수익 = Π max(1 + 단계 수익률, 0) − 1(칸의 "growth" − 1, 복리).

    Args:
        win: 상태 파일 "window" 칸.

    Returns:
        비율, 지금 자산(칸의 마지막 확인)이 0 이하면 None(확인 불가, 전액 출금은 손실 경보가 아니다).
    """
    return float(Decimal(win["growth"]) - 1) if equity(Mark.from_json(win["mark"])) > 0 else None


def advance(win: dict | None, pstart: datetime, mark: Mark, moves: tuple[list[Execution], list[Funding]] | None,
            quote: str) -> tuple[dict, str | None]:
    """이번 확인으로 구간을 잇는다.

    구간이 바뀌었거나(period 처음이 다름) 처음이거나 종목이 바뀌었으면 지금 자산을 기준으로 다시 잡는다(growth 1 · 최저 0).
    단계 수익률 r = 보유 손익 / 지난 자산 + 체결 손익 / (지난 자산 + max(0, 흐름)), 분모가 0 이하인 항은 0.
    growth에 max(1 + r, 0)을 곱한다(−100%에서 멈춤, 되살아나지 않음). 손익 · 흐름은 분모와 상관없이 그대로 더한다.
    두 확인 사이 체결을 다 받지 못했으면(moves None) 아무것도 바꾸지 않는다(지난 확인이 그대로 남아 다음에 이어 셈).

    Args:
        win: 상태 파일 "window" 칸(없으면 None).
        pstart: 이번 30일 구간의 처음(daily.reconcile.period).
        mark: 이번 확인.
        moves: (체결, 펀딩비) 또는 None(받은 범위가 [지난 확인, 이번 확인)을 못 덮음).
        quote: 거래 통화.

    Returns:
        (새 "window" 칸, 근거에 붙일 말 CAPTURED · NOT_DIVIDED 또는 None).
    """
    if not win or win.get("start") != pstart.isoformat() or set(win["mark"]["hold"]) != set(mark.hold):
        return ({"start": pstart.isoformat(), "base": str(equity(mark)), "pnl": "0", "flows": "0",
                 "growth": "1", "low": 0.0, "mark": mark.to_json()}, CAPTURED)
    if moves is None:
        return win, NOT_DIVIDED
    prev = Mark.from_json(win["mark"])
    hold, fill, flow = step(prev, mark, moves[0], moves[1], quote)
    before = equity(prev)
    fill_base = before + max(flow, Decimal(0))  # 같은 시간에 들어온 입금으로 산 체결도 분모에 든다
    r = (hold / before if before > 0 else Decimal(0)) + (fill / fill_base if fill_base > 0 else Decimal(0))
    growth = Decimal(win["growth"]) * max(1 + r, Decimal(0))
    return {**win, "pnl": str(Decimal(win["pnl"]) + hold + fill), "flows": str(Decimal(win["flows"]) + flow),
            "growth": str(growth), "low": min(float(win["low"]), float(growth - 1)), "mark": mark.to_json()}, None

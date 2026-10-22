"""바이낸스 현물 · USDⓈ-M 선물 체결과 선물 펀딩비를 받아 회고 CSV 줄로 바꾼다."""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from decimal import Decimal
from typing import Any, Callable

from blackclip.binance.client import SHAPE_TEXT, BinanceError

COLUMNS = ("time", "symbol", "side", "qty", "price", "fee", "fee_asset", "order_id", "trade_id")
"""받은 체결 CSV 열(회고 형식)."""

FUNDING_COLUMNS = ("time", "symbol", "amount", "asset", "tran_id")
"""받은 펀딩비 CSV 열(회고 펀딩비 형식 + 중복 막기용 tran_id)."""

DAY_MS = 86_400_000
"""하루(ms)."""

WINDOW_MS = {"spot": DAY_MS, "futures": 7 * DAY_MS}
"""한 번 조회 기간 상한: 현물 myTrades 24시간(실호출 -1127 확인), 선물 userTrades 7일(공식 문서)."""

FUNDING_WINDOW_MS = 7 * DAY_MS
"""income 한 번 기간. 상한 문장이 없어 문서의 기본 범위(7일)로 나눈다."""

FUTURES_HISTORY_MS = 90 * DAY_MS
"""선물 체결 · income 과거 상한 「3개월」(공식 문서). 정확한 경계는 미검증이라 90일로 잡는다."""

PAGE = 1000
"""한 페이지 최대 개수(myTrades · userTrades · income)."""

PATHS = {"spot": "/api/v3/myTrades", "futures": "/fapi/v1/userTrades"}
"""체결 조회 경로."""


@dataclass(frozen=True)
class Fetched:
    """받은 결과.

    Attributes:
        rows: CSV 줄(시각 순).
        start_ms: 실제로 받은 시작(포함).
        end_ms: 받은 끝(제외).
        lost: 상한 때문에 못 받은 [시작, 끝). 없으면 None.
    """

    rows: list[dict[str, str]]
    start_ms: int
    end_ms: int
    lost: tuple[int, int] | None = None


def iso(ms: int) -> str:
    """ms 시각을 ISO 8601 UTC 문자열로.

    Args:
        ms: 유닉스 ms.

    Returns:
        예: "2017-07-12T13:19:09.590+00:00".
    """
    return datetime.fromtimestamp(ms / 1000, timezone.utc).isoformat(timespec="milliseconds")


def _clip(start: int, end: int) -> tuple[int, tuple[int, int] | None]:
    """선물 과거 상한으로 시작을 자른다.

    Args:
        start: 바라는 시작.
        end: 끝.

    Returns:
        (자른 시작, 못 받은 범위 또는 None).
    """
    floor = end - FUTURES_HISTORY_MS
    return (floor, (start, floor)) if start < floor else (start, None)


def _paged(client: Any, path: str, params: dict[str, Any], start: int, end: int, window: int, id_key: str) -> list[dict]:
    """[start, end)를 window로 나눠 받는다. 한 창에서 1000건이 차면 그 창을 반으로 나눠 다시 받고 id로 중복을 뺀다.

    바이낸스가 창 안 「처음 1000건」을 주든 「최근 1000건」을 주든 빠지지 않게, 이어 받지 않고 나눈다.

    Args:
        client: get(path, params)를 가진 클라이언트.
        path: 경로.
        params: 고정 쿼리.
        start: 시작(포함, ms).
        end: 끝(제외, ms).
        window: 한 번 기간(ms).
        id_key: 중복을 가를 열(id · tranId).

    Returns:
        받은 줄(원래 모양).
    """
    out: list[dict] = []
    seen: set = set()
    todo = [(s, min(s + window, end)) for s in range(start, end, window)][::-1]
    while todo:
        s, e = todo.pop()
        page = client.get(path, {**params, "startTime": s, "endTime": e - 1, "limit": PAGE})
        if not isinstance(page, list) or not all(isinstance(x, dict) and type(x.get(id_key)) is int for x in page):
            raise BinanceError(0, None, SHAPE_TEXT)
        new = [x for x in page if x[id_key] not in seen]
        seen.update(x[id_key] for x in new)
        out += new
        if len(page) >= PAGE:
            if e - s <= 1:
                raise BinanceError(0, None, f"{params.get('symbol', '펀딩비')} {iso(s)}: 한 시각에 {PAGE}건이 넘어 다 받지 못함(확인 불가)")
            m = (s + e) // 2
            todo += [(m, e), (s, m)]
    return out


SPOT_FIELDS: dict[str, type] = {"time": int, "symbol": str, "isBuyer": bool, "qty": str, "price": str, "commission": str,
                                "commissionAsset": str, "orderId": int, "id": int}
"""현물 myTrades 줄에서 쓰는 칸 → 타입(bool은 int로 치지 않음)."""

FUTURES_FIELDS: dict[str, type] = {**{k: v for k, v in SPOT_FIELDS.items() if k != "isBuyer"}, "side": str}
"""선물 userTrades 줄에서 쓰는 칸 → 타입."""

FUNDING_FIELDS: dict[str, type] = {"time": int, "symbol": str, "income": str, "asset": str, "tranId": int}
"""선물 income(FUNDING_FEE) 줄에서 쓰는 칸 → 타입."""

DECIMALS = ("qty", "price", "commission", "income")
"""숫자 글이어야 하는 칸(바이낸스는 금액을 글로 준다)."""


def _decimal(s: str) -> bool:
    """숫자로 읽히는 유한한 글인지.

    Args:
        s: 칸 값.

    Returns:
        읽히면 True.
    """
    try:
        return Decimal(s).is_finite()
    except ArithmeticError:  # InvalidOperation
        return False


def _rows(raw: list[dict], fields: dict[str, type], make: Callable[[dict], dict]) -> list[dict]:
    """받은 줄을 회고 CSV 줄로 바꾼다. 쓰는 칸이 빠졌거나 모양이 다르면 추측하지 않고 조회 실패로 멈춘다.

    Args:
        raw: `_paged`가 받은 줄(원래 모양).
        fields: 쓰는 칸 → 타입(`SPOT_FIELDS` · `FUTURES_FIELDS` · `FUNDING_FIELDS`).
        make: 한 줄을 바꾸는 함수.

    Returns:
        바꾼 줄.

    Raises:
        BinanceError: 칸이 없거나 타입이 다르거나 숫자 글이 아니거나 선물 side가 BUY · SELL이 아닐 때(`SHAPE_TEXT`).
    """
    for t in raw:
        if (not all(type(t.get(k)) is v for k, v in fields.items()) or not all(_decimal(t[k]) for k in DECIMALS if k in fields)
                or ("side" in fields and t["side"] not in ("BUY", "SELL"))):
            raise BinanceError(0, None, SHAPE_TEXT)
    try:
        return [make(t) for t in raw]
    except (KeyError, TypeError, AttributeError, ValueError, ArithmeticError):  # ArithmeticError: Decimal이 숫자가 아닌 글을 받음
        raise BinanceError(0, None, SHAPE_TEXT) from None


def fetch_trades(client: Any, symbol: str, start_ms: int, end_ms: int) -> Fetched:
    """종목 하나의 체결을 받는다. 현물은 isBuyer, 선물은 side로 방향을 정한다.

    Args:
        client: market이 "spot" 또는 "futures"인 클라이언트.
        symbol: 종목(BTCUSDT).
        start_ms: 시작(포함).
        end_ms: 끝(제외).

    Returns:
        Fetched.
    """
    lost = None
    if client.market == "futures":
        start_ms, lost = _clip(start_ms, end_ms)
    raw = _paged(client, PATHS[client.market], {"symbol": symbol}, start_ms, end_ms, WINDOW_MS[client.market], "id")
    spot = client.market == "spot"
    rows = _rows(raw, SPOT_FIELDS if spot else FUTURES_FIELDS, lambda t: {
        "time": iso(t["time"]),
        "symbol": t["symbol"],
        "side": ("buy" if t["isBuyer"] else "sell") if spot else t["side"].lower(),
        "qty": t["qty"],
        "price": t["price"],
        # 선물 문서 예시의 commission이 음수라 부호를 믿지 않고 비용으로 적는다(실응답 미검증, 근거 없는 안심을 피하는 쪽)
        "fee": t["commission"] if spot else format(abs(Decimal(t["commission"])), "f"),
        "fee_asset": t["commissionAsset"],
        "order_id": str(t["orderId"]),
        "trade_id": str(t["id"]),
    })
    return Fetched(sorted(rows, key=lambda r: (r["time"], r["trade_id"])), start_ms, end_ms, lost)


def fetch_funding(client: Any, start_ms: int, end_ms: int) -> Fetched:
    """USDⓈ-M 선물 펀딩비(income FUNDING_FEE)를 모든 종목에 걸쳐 받는다.

    Args:
        client: market이 "futures"인 클라이언트.
        start_ms: 시작(포함).
        end_ms: 끝(제외).

    Returns:
        Fetched(rows는 FUNDING_COLUMNS).
    """
    start_ms, lost = _clip(start_ms, end_ms)
    raw = _paged(client, "/fapi/v1/income", {"incomeType": "FUNDING_FEE"}, start_ms, end_ms, FUNDING_WINDOW_MS, "tranId")
    rows = _rows(raw, FUNDING_FIELDS, lambda x: {"time": iso(x["time"]), "symbol": x["symbol"], "amount": x["income"], "asset": x["asset"],
                                 "tran_id": str(x["tranId"])})
    return Fetched(sorted(rows, key=lambda r: (r["time"], r["tran_id"])), start_ms, end_ms, lost)

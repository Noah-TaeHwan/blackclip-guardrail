"""체결 CSV와 펀딩비 CSV를 읽는다. 같은 주문의 부분 체결은 하나로 묶는다."""
from __future__ import annotations

import csv
import re
from collections import defaultdict
from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal, InvalidOperation
from pathlib import Path

COLUMNS: tuple[str, ...] = ("time", "symbol", "side", "qty", "price", "fee", "order_id")
"""체결 CSV 필수 열. 선택 열: fee_asset(비면 수수료를 합계에 넣지 않음) ·
trade_id(같은 주문 안에서 가격 · 수량 · 시각이 같은 두 체결을 구분, 중복 줄 검사에만 쓰임)."""

FUNDING_COLUMNS: tuple[str, ...] = ("time", "symbol", "amount", "asset")
"""펀딩비 CSV 필수 열(바이낸스 선물 수익 내역의 FUNDING_FEE를 옮긴 것)."""

_SIDES: dict[str, str] = {"buy": "buy", "sell": "sell", "매수": "buy", "매도": "sell"}
"""방향 표기 → buy/sell."""


class FillsError(ValueError):
    """CSV를 믿고 쓸 수 없을 때 내는 오류. 메시지에 파일 이름 · 행 번호 · 열 이름을 담는다."""


@dataclass(frozen=True)
class Execution:
    """주문 하나의 체결 묶음(부분 체결을 합친 것).

    Attributes:
        time: 첫 체결 시각(시간대 있음).
        symbol: 정규화한 종목(예: BTCUSDT).
        side: "buy" 또는 "sell".
        qty: 수량 합.
        price: 수량 가중 평균가.
        fee: 수수료 합. fee_asset이 ""이면 여러 자산이 섞인 합이므로 쓰지 않는다.
        fee_asset: 수수료 자산. 비었거나 부분 체결마다 달랐으면 ""(모름).
        order_id: 주문 번호(없으면 "행N").
    """

    time: datetime
    symbol: str
    side: str
    qty: Decimal
    price: Decimal
    fee: Decimal
    fee_asset: str
    order_id: str


@dataclass(frozen=True)
class Funding:
    """선물 펀딩비 한 줄.

    Attributes:
        time: 시각(시간대 있음).
        symbol: 정규화한 종목.
        amount: 금액(받으면 +, 내면 -).
        asset: 자산(예: USDT).
    """

    time: datetime
    symbol: str
    amount: Decimal
    asset: str


def normalize_symbol(raw: str) -> str:
    """종목 표기를 BTCUSDT 꼴로 맞춘다.

    정산 자산 꼬리(:USDT)만 떼고 구분 기호를 뺀다. 만기가 붙은 선물(-241227)은 만기를 남겨 무기한과 섞이지 않게 한다.

    Args:
        raw: 원래 표기(BTC/USDT, btc-usdt, ccxt 선물 BTC/USDT:USDT · BTC/USDT:USDT-241227 등).

    Returns:
        대문자 종목(구분 기호 없음). 빈 입력이면 "".
    """
    s = re.sub(r":[A-Za-z]+(?=-|$)", "", raw.strip())
    return re.sub(r"[/_-]", "", s).upper()


def _symbol(raw: str, name: str, row: int, forms: dict[str, dict[str, str]]) -> str:
    """종목을 정규화하고, 비어 있거나 같은 종목이 현물 · 선물 표기로 섞이면 행 번호와 함께 멈춘다.

    현물 표기는 슬래시가 있고 ":"가 없는 꼴(BTC/USDT), 선물 표기는 ":"가 있는 꼴(BTC/USDT:USDT)이다.
    슬래시 없는 BTCUSDT · btc-usdt는 어느 시장인지 알 수 없어 검사하지 않는다.

    Args:
        raw: 원래 표기.
        name: 파일 이름(오류 메시지용).
        row: 파일의 행 번호(머리줄 = 1).
        forms: 종목별로 지금까지 본 표기({"현물": 원래 표기, "선물": 원래 표기}). 호출마다 갱신한다.

    Returns:
        정규화한 종목(비어 있지 않음).
    """
    symbol = normalize_symbol(raw)
    if not symbol:
        raise FillsError(f"{name} {row}행 symbol: 비어 있습니다")
    raw = raw.strip()
    market = "선물" if ":" in raw else "현물" if "/" in raw else None
    if market:
        seen = forms.setdefault(symbol, {})
        seen.setdefault(market, raw)
        if len(seen) == 2:
            raise FillsError(f"{name} {row}행 symbol: 같은 종목이 현물 표기({seen['현물']})와 선물 표기({seen['선물']})로 섞여 있습니다. 현물과 선물은 파일을 나눠 주세요")
    return symbol


def _time(raw: str, name: str, row: int) -> datetime:
    """ISO 8601 시각을 읽는다. 시간대가 없으면 멈춘다.

    Args:
        raw: 시각 문자열.
        name: 파일 이름(오류 메시지용).
        row: 파일의 행 번호(머리줄 = 1).

    Returns:
        시간대가 있는 datetime.
    """
    try:
        t = datetime.fromisoformat(raw.strip().replace("Z", "+00:00"))
    except ValueError as exc:
        raise FillsError(f"{name} {row}행 time: 시각을 읽을 수 없습니다({raw})") from exc
    if t.tzinfo is None:
        raise FillsError(f"{name} {row}행 time: 시간대가 없습니다({raw}). 예: 2026-10-13T15:58:00+00:00")
    return t


def _num(rec: dict[str, str], col: str, name: str, row: int, positive: bool = False) -> Decimal:
    """숫자 열을 Decimal로 읽는다.

    Args:
        rec: CSV 한 행.
        col: 열 이름.
        name: 파일 이름(오류 메시지용).
        row: 행 번호.
        positive: True면 0보다 커야 한다.

    Returns:
        Decimal 값.
    """
    try:
        value = Decimal(rec[col].strip())
    except InvalidOperation as exc:
        raise FillsError(f"{name} {row}행 {col}: 숫자가 아닙니다({rec[col]})") from exc
    if not value.is_finite() or (positive and value <= 0):
        raise FillsError(f"{name} {row}행 {col}: {'0보다 큰 ' if positive else ''}숫자여야 합니다({rec[col]})")
    return value


def _rows(path: Path, needed: tuple[str, ...]) -> list[tuple[int, dict[str, str]]]:
    """CSV를 읽어 (파일 행 번호, 행) 목록을 돌려준다. 필수 열이 없거나 열 이름이 겹치거나 칸 수가 다르면 멈춘다.

    Args:
        path: CSV 경로.
        needed: 필수 열.

    Returns:
        (행 번호, 행) 목록. 행 번호는 파일의 실제 줄 번호다(빈 줄도 센다).

    Raises:
        FillsError: 열 · 칸이 맞지 않거나 csv 모듈이 읽지 못할 때(칸이 너무 김 등).
    """
    out: list[tuple[int, dict[str, str]]] = []
    try:
        with open(path, newline="", encoding="utf-8-sig") as f:
            reader = csv.DictReader(f)
            fields = reader.fieldnames or []
            missing = [c for c in needed if c not in fields]
            if missing:
                raise FillsError(f"{path.name}: 필수 열이 없습니다: {', '.join(missing)}")
            dups = sorted({c for c in fields if fields.count(c) > 1})  # DictReader는 뒤쪽 값만 남기므로 막는다
            if dups:
                raise FillsError(f"{path.name}: 머리줄에 같은 열 이름이 두 번 있습니다: {', '.join(dups)}")
            for rec in reader:
                if None in rec or None in rec.values():
                    raise FillsError(f"{path.name} {reader.line_num}행: 칸 수가 머리줄과 다릅니다")
                out.append((reader.line_num, rec))
    except csv.Error as exc:  # ValueError가 아니라 호출자가 못 잡음(칸이 너무 김 등), 경로 없이 종류만
        raise FillsError(f"{path.name}: CSV를 읽을 수 없습니다({type(exc).__name__})") from None
    return out


def read_executions(path: Path, per_fill: bool = False) -> list[Execution]:
    """체결 CSV를 읽어 주문 단위 체결 목록을 시각 순으로 돌려준다.

    Args:
        path: 체결 CSV 경로(실제 체결 또는 백테스트 거래 목록).
        per_fill: True면 묶지 않고 줄마다 하나(order_id는 「행N」). 자주 보는 시계의 단계 손익은 확인 경계를 넘는
            부분 체결을 제 시각에 세야 해서 이것을 쓴다(blackclip fetch의 fills.csv는 줄 = 체결 하나, trade_id).

    Returns:
        Execution 목록(시각 오름차순). (종목, order_id)가 같은 줄은 하나로 묶는다(per_fill이면 안 묶음).
        같은 종목이 현물 표기(BTC/USDT)와 선물 표기(BTC/USDT:USDT)로 섞인 파일은 멈춘다.
    """
    groups: dict[tuple[str, str], list[tuple[int, dict[str, str]]]] = defaultdict(list)
    seen: dict[tuple[str, ...], int] = {}
    forms: dict[str, dict[str, str]] = {}
    for row, rec in _rows(path, COLUMNS):
        line = tuple(v.strip() for v in rec.values())
        if line in seen:  # 기간을 나눠 받은 내보내기를 이어 붙이면 경계 줄이 두 번 들어간다
            raise FillsError(f"{path.name} {row}행: {seen[line]}행과 완전히 같은 줄입니다(중복). "
                             "같은 가격 · 수량의 진짜 두 체결이면 trade_id 열을 넣어 구분하세요")
        seen[line] = row
        symbol = _symbol(rec["symbol"], path.name, row, forms)
        groups[(symbol, f"행{row}" if per_fill else rec["order_id"].strip() or f"행{row}")].append((row, rec))
    out: list[Execution] = []
    for (symbol, order_id), items in groups.items():
        first_row = items[0][0]
        side = ""
        for row, rec in items:
            s = _SIDES.get(rec["side"].strip().lower())
            if s is None:
                raise FillsError(f"{path.name} {row}행 side: buy · sell · 매수 · 매도 중 하나여야 합니다({rec['side']})")
            if side and s != side:
                raise FillsError(f"{path.name} {first_row}행과 {row}행 주문 {order_id}: 한 주문에 방향이 둘입니다")
            side = s
        times = [_time(rec["time"], path.name, row) for row, rec in items]
        qtys = [_num(rec, "qty", path.name, row, positive=True) for row, rec in items]
        prices = [_num(rec, "price", path.name, row, positive=True) for row, rec in items]
        fees = [_num(rec, "fee", path.name, row) for row, rec in items]
        assets = {(rec.get("fee_asset") or "").strip().upper() for _, rec in items}
        total = sum(qtys, Decimal(0))
        out.append(Execution(
            time=min(times),
            symbol=symbol,
            side=side,
            qty=total,
            price=sum((q * p for q, p in zip(qtys, prices)), Decimal(0)) / total,
            fee=sum(fees, Decimal(0)),
            fee_asset=assets.pop() if len(assets) == 1 else "",
            order_id=order_id,
        ))
    return sorted(out, key=lambda e: e.time)


def read_funding(path: Path) -> list[Funding]:
    """펀딩비 CSV를 읽는다. 완전히 같은 줄이 두 번 나오거나 같은 종목이 현물 · 선물 표기로 섞이면 멈춘다.

    Args:
        path: 펀딩비 CSV 경로.

    Returns:
        Funding 목록(시각 오름차순).
    """
    out: list[Funding] = []
    seen: dict[tuple[str, ...], int] = {}
    forms: dict[str, dict[str, str]] = {}
    for row, rec in _rows(path, FUNDING_COLUMNS):
        line = tuple(v.strip() for v in rec.values())
        if line in seen:  # 기간을 나눠 받은 펀딩비 파일을 이어 붙이면 합계가 부풀어 오른다
            raise FillsError(f"{path.name} {row}행: {seen[line]}행과 완전히 같은 줄입니다(중복). "
                             "기간을 나눠 받은 파일을 이어 붙였는지 확인하세요")
        seen[line] = row
        asset = rec["asset"].strip().upper()
        if not asset:
            raise FillsError(f"{path.name} {row}행 asset: 비어 있습니다")
        out.append(Funding(
            time=_time(rec["time"], path.name, row),
            symbol=_symbol(rec["symbol"], path.name, row, forms),
            amount=_num(rec, "amount", path.name, row),
            asset=asset,
        ))
    return sorted(out, key=lambda f: f.time)

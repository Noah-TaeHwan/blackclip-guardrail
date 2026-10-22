"""회고 숫자를 계산하고 마크다운 한 장으로 낸다.

회고는 전략을 다시 돌리지 않고 가격 데이터도 없다. 그래서 잰 것(체결가 차이 · 수량 비율 ·
수수료 · 펀딩비 · 짝 없는 체결)만 숫자로 내고, 백테스트에만 있는 거래의 손익 영향은
「점검 불가」로 둔다. 실행 쪽 원인은 모두 「추정」이다. 금액은 거래 통화(quote) 하나로만
더하고, 다른 자산은 합계에서 빼고 건수로 남긴다.
"""
from __future__ import annotations

import statistics
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from decimal import Decimal

from blackclip.retro.fills import Execution, Funding
from blackclip.retro.match import DEFAULT_WINDOW, MatchResult, Pair, match

SLIP_LIMIT_BP: Decimal = Decimal(20)
"""체결가 차이 허용치(bp) [제안값]. 넘은 건수를 따로 센다."""

HOLD_TRADES: int = 30
"""실전 시작 이후 거래가 이보다 적으면 판단 보류와 남은 건수를 쓴다."""

QTY_TOLERANCE: Decimal = Decimal("0.1")
"""수량 비율이 짝들의 중앙값에서 이만큼(10%) 넘게 벗어나면 따로 센다 [제안값]."""


@dataclass(frozen=True)
class Retro:
    """회고 한 장에 들어갈 숫자.

    Attributes:
        start: 기간 시작(포함, UTC). 실전 시작일보다 앞서지 않는다.
        end: 기간 끝(제외, UTC).
        live_start: 실전 시작 시각(UTC).
        capital: 자본(거래 통화, 실전 시작일 잔고).
        quote: 거래 통화(예: USDT). 금액은 이 자산으로만 더한다.
        window: 매칭 창.
        executions: 기간 안 실제 체결 묶음 수 = 짝 수 + 실제에만 있는 체결 수(짝은 백테스트 시각으로 기간을 정한다).
        backtest_executions: 기간 안 백테스트 체결 수 = 짝 수 + 백테스트에만 있는 체결 수 + 체결 파일 범위 밖 수 + 마지막 체결 뒤(구분 불가) 수.
        trades: 기간 안 거래 수(실제 체결 묶음 ÷ 2, 내림).
        trades_since_live: 실전 시작부터 기간 끝까지 거래 수(판단 보류 30건 기준, 실제 체결 시각으로 누적).
        result: 짝짓기 결과(기간 안 것만). backtest_only는 체결 파일 범위 안의 놓친 주문 후보만 담는다(기간 안 실제 체결이 없고 fills_until도 없으면 추정하지 않고 표에 확인 불가로 쓴다).
        beyond_fills: 체결 파일 범위 밖(fills_until 이후 · fills_from 앞)인 짝 없는 백테스트 체결(놓친 주문이 아니라 점검 불가).
        after_last_fill: 마지막 실제 체결의 날(UTC) 다음 날 0시 이후인 짝 없는 백테스트 체결. 봇이 멈췄는지 체결 파일이 끝났는지 구분할 수 없어
            놓친 주문으로도 범위 밖으로도 세지 않는다(점검 불가). fills_until을 안 줬을 때만 채운다.
        fills_until: 고객이 --fills-until로 말해 준, 체결 파일이 이 시각 전의 체결을 모두 담았다는 끝(제외, UTC). 안 줬으면 None.
        fills_until_source: fills_until을 어디서 받았나("--fills-until" 또는 "blackclip fetch").
        fills_from: 체결 파일이 이 시각부터의 체결을 담았다는 처음(포함, UTC, blackclip fetch 받은 범위). 모르면 None.
        edge_excluded: 기간 안 실제 체결이지만 기간 밖 백테스트와 짝지어져 센 곳에서 뺀 수.
        actual_range: 넣은 실제 체결 파일 전체의 (처음, 마지막) 시각(UTC). 비었으면 None.
        backtest_range: 넣은 백테스트 파일 전체의 (처음, 마지막) 시각(UTC). 비었으면 None.
        slip_bp: 짝마다 체결가 차이(bp, 손해 = 음수).
        slip_cost: 거래 통화 시장 짝의 체결가 차이 금액 합(손해 = 음수).
        slip_unconverted: 다른 통화 시장이라 금액 합에서 뺀 짝 수.
        qty_ratios: 짝마다 실제 수량 ÷ 백테스트 수량.
        fee_actual: 실제로 낸 수수료(거래 통화, 음수).
        fee_backtest: 백테스트가 가정한 수수료(거래 통화, 음수).
        fee_estimated: 받은 자산으로 낸 수수료를 체결가로 환산한 실제 체결 수(추정).
        fee_unconverted: 자산이 비었거나 섞였거나 다른 시장이라 뺀 실제 체결 수.
        fee_backtest_unconverted: 같은 이유로 뺀 백테스트 체결 수.
        funding_total: 거래 통화 펀딩비 합. 파일이 없으면 None.
        funding_unconverted: 다른 자산이라 뺀 펀딩비 줄 수.
    """

    start: datetime
    end: datetime
    live_start: datetime
    capital: Decimal
    quote: str
    window: timedelta
    executions: int
    backtest_executions: int
    trades: int
    trades_since_live: int
    result: MatchResult
    beyond_fills: list[Execution]
    after_last_fill: list[Execution]
    fills_until: datetime | None
    fills_until_source: str
    fills_from: datetime | None
    edge_excluded: int
    actual_range: tuple[datetime, datetime] | None
    backtest_range: tuple[datetime, datetime] | None
    slip_bp: list[Decimal]
    slip_cost: Decimal
    slip_unconverted: int
    qty_ratios: list[Decimal]
    fee_actual: Decimal
    fee_backtest: Decimal
    fee_estimated: int
    fee_unconverted: int
    fee_backtest_unconverted: int
    funding_total: Decimal | None
    funding_unconverted: int


def _sign(side: str) -> Decimal:
    """방향 부호: 매수 +1, 매도 -1.

    Args:
        side: "buy" 또는 "sell".

    Returns:
        Decimal 부호.
    """
    return Decimal(1) if side == "buy" else Decimal(-1)


def slip_bp(pair: Pair) -> Decimal:
    """짝 하나의 체결가 차이를 bp로 낸다. 손해(비싸게 삼 · 싸게 팖)는 음수.

    Args:
        pair: 백테스트 체결과 실제 체결의 짝.

    Returns:
        bp 값.
    """
    b, a = pair.backtest.price, pair.actual.price
    return -_sign(pair.actual.side) * (a - b) / b * Decimal(10000)


def fee_in_quote(e: Execution, quote: str) -> tuple[Decimal | None, bool]:
    """수수료를 거래 통화 금액으로 바꾼다.

    Args:
        e: 체결.
        quote: 거래 통화(예: USDT).

    Returns:
        (금액, 추정 여부). 수수료가 0이면 자산과 시장에 상관없이 (0, False). 거래 통화로 냈으면
        그대로, 받은 기초 자산(BTCUSDT의 BTC)으로 냈으면 그 체결가로 환산하고 추정 표시.
        자산이 비었거나 섞였거나(""), 다른 자산이거나, 거래 통화 시장이 아니면 (None, False).
    """
    if e.fee == 0:
        return Decimal(0), False
    if not e.symbol.endswith(quote) or e.fee_asset == "":
        return None, False
    if e.fee_asset == quote:
        return e.fee, False
    if e.symbol == e.fee_asset + quote:
        return e.fee * e.price, True
    return None, False


def next_day_utc(t: datetime) -> datetime:
    """시각이 든 UTC 날의 다음 날 0시를 돌려준다.

    Args:
        t: 시간대가 있는 시각.

    Returns:
        다음 날 00:00 UTC.
    """
    t = t.astimezone(timezone.utc)
    return datetime(t.year, t.month, t.day, tzinfo=timezone.utc) + timedelta(days=1)


def _range(items: list[Execution]) -> tuple[datetime, datetime] | None:
    """체결 목록의 (처음, 마지막) 시각을 UTC로 낸다.

    Args:
        items: 파일에서 읽은 체결 전체.

    Returns:
        (처음, 마지막). 비었으면 None.
    """
    times = [e.time.astimezone(timezone.utc) for e in items]
    return (min(times), max(times)) if times else None


def build_retro(backtest: list[Execution], actual: list[Execution], funding: list[Funding] | None,
                capital: Decimal, end: datetime, live_start: datetime, days: int = 30,
                window: timedelta = DEFAULT_WINDOW, quote: str = "USDT", fills_until: datetime | None = None,
                fills_until_source: str = "--fills-until", fills_from: datetime | None = None) -> Retro:
    """기간 안 체결을 짝짓고 회고 숫자를 계산한다.

    기간은 [max(실전 시작, 끝 − days), 끝)이다(실전 구간만 잘라 비교).
    기간 가장자리에서 짝이 깨지지 않게 매칭 창만큼 바깥까지 짝지은 뒤 기간으로 자른다.
    실전 시작 전으로는 넓히지 않는다. 기간 안 집합은 하나다: 짝(백테스트 시각이 기간 안) ·
    기간 안 짝 없는 백테스트 · 기간 안 짝 없는 실제. 체결 수 · 체결가 차이 · 수량 비율 · 수수료 · 표가
    모두 이 집합을 쓴다. 짝 없는 백테스트는 셋 중 하나다: 놓친 주문(추정, result.backtest_only) · beyond_fills ·
    after_last_fill. fills_until(고객이 --fills-until로 한 말)이 있으면 그 시각 전은 놓친 주문, 이후는
    beyond_fills다. 없으면 마지막 실제 체결의 날 다음 날 0시 UTC 전은 놓친 주문, 이후는 after_last_fill이다
    (--end는 체결 파일 끝을 말해 주지 않는다). 실제 체결이 하나도 없고 fills_until도 없으면 나눌 끝이 없어 전부 backtest_only에 남고,
    render가 결론은 확인 불가로, 표의 원인은 「확인 불가(기간 안 실제 체결 없음)」로 쓴다. fills_until이 있으면 실제 체결이 없어도 그 앞 거래는 놓친 주문(추정)이다.
    fills_from(blackclip fetch가 받은 범위 처음)이 있으면 그 앞 짝 없는 백테스트도 beyond_fills다(받지 않은 기간이라 점검 불가).
    펀딩비는 백테스트 종목 것만 더한다(fetch의 funding.csv는 계좌 전체 종목).

    Raises:
        ValueError: 자본이 0 이하이거나 끝 시각이 실전 시작보다 앞이거나 같을 때.

    Args:
        backtest: 백테스트 체결 목록.
        actual: 실제 체결 목록.
        funding: 펀딩비 목록. 파일이 없으면 None.
        capital: 자본(거래 통화).
        end: 기간 끝(제외).
        live_start: 실전 시작 시각.
        days: 기간 날수.
        window: 매칭 창.
        quote: 거래 통화.
        fills_until: 체결 파일이 이 시각 전의 체결을 모두 담았다고 고객이 말한 끝(제외). None이면 어디까지인지 모르는 것으로 보고, 마지막 실제 체결의 날 뒤는 구분 불가로 둔다.
            마지막 실제 체결보다 앞서는지는 여기서 검사하지 않는다(blackclip.cli가 거른다).
        fills_until_source: fills_until을 어디서 받았나("--fills-until" 또는 "blackclip fetch"). 근거 줄에 그대로 쓴다.
        fills_from: 체결 파일이 이 시각부터 담았다는 처음(포함). None이면 처음은 따지지 않는다.

    Returns:
        Retro.
    """
    if capital <= 0:
        raise ValueError("자본은 0보다 커야 합니다")
    if end <= live_start:
        raise ValueError("끝 시각이 실전 시작보다 뒤여야 합니다")
    end, live_start = end.astimezone(timezone.utc), live_start.astimezone(timezone.utc)
    start = max(live_start, end - timedelta(days=days))
    lo, hi = max(live_start, start - window), end + window

    def inside(t: datetime) -> bool:
        """시각이 기간 안(시작 포함, 끝 제외)인지 본다.

        Args:
            t: 확인할 시각(UTC).

        Returns:
            기간 안이면 True.
        """
        return start <= t < end

    raw = match([e for e in backtest if lo <= e.time < hi], [e for e in actual if lo <= e.time < hi], window)
    pairs = [p for p in raw.pairs if inside(p.backtest.time)]
    unpaired = [e for e in raw.backtest_only if inside(e.time)]
    fills_until = fills_until and fills_until.astimezone(timezone.utc)
    covered_until = fills_until or (next_day_utc(max(e.time for e in actual)) if actual else None)
    late = [e for e in unpaired if covered_until is not None and e.time >= covered_until]
    fills_from = fills_from and fills_from.astimezone(timezone.utc)
    early = [e for e in unpaired if fills_from and e.time < fills_from]
    result = MatchResult(
        pairs=pairs,
        backtest_only=[e for e in unpaired if (covered_until is None or e.time < covered_until) and not (fills_from and e.time < fills_from)],
        actual_only=[e for e in raw.actual_only if inside(e.time)],
    )
    act = [p.actual for p in pairs] + result.actual_only
    bt = [p.backtest for p in pairs] + unpaired
    in_quote = [p for p in pairs if p.actual.symbol.endswith(quote)]
    act_fees = [fee_in_quote(e, quote) for e in act]
    bt_fees = [fee_in_quote(e, quote) for e in bt]
    symbols = {e.symbol for e in backtest}
    funds = [] if funding is None else [f for f in funding if inside(f.time) and f.symbol in symbols]
    return Retro(
        start=start,
        end=end,
        live_start=live_start,
        capital=capital,
        quote=quote,
        window=window,
        executions=len(act),
        backtest_executions=len(bt),
        trades=len(act) // 2,
        trades_since_live=sum(1 for e in actual if live_start <= e.time < end) // 2,
        result=result,
        beyond_fills=early + (late if fills_until else []),
        after_last_fill=[] if fills_until else late,
        fills_until=fills_until,
        fills_until_source=fills_until_source,
        fills_from=fills_from,
        edge_excluded=sum(1 for p in raw.pairs if inside(p.actual.time) and not inside(p.backtest.time)),
        actual_range=_range(actual),
        backtest_range=_range(backtest),
        slip_bp=[slip_bp(p) for p in pairs],
        slip_cost=sum((-_sign(p.actual.side) * (p.actual.price - p.backtest.price) * p.actual.qty for p in in_quote), Decimal(0)),
        slip_unconverted=len(pairs) - len(in_quote),
        qty_ratios=[p.actual.qty / p.backtest.qty for p in pairs],
        fee_actual=-sum((amt for amt, _ in act_fees if amt is not None), Decimal(0)),
        fee_backtest=-sum((amt for amt, _ in bt_fees if amt is not None), Decimal(0)),
        fee_estimated=sum(1 for _, est in act_fees if est),
        fee_unconverted=sum(1 for amt, _ in act_fees if amt is None),
        fee_backtest_unconverted=sum(1 for amt, _ in bt_fees if amt is None),
        funding_total=None if funding is None else sum((f.amount for f in funds if f.asset == quote), Decimal(0)),
        funding_unconverted=sum(1 for f in funds if f.asset != quote),
    )


def _pct(amount: Decimal, capital: Decimal) -> str:
    """금액을 자본 대비 %p 문자열로 바꾼다.

    Args:
        amount: 금액(손해 = 음수).
        capital: 자본.

    Returns:
        예: "-0.40%p".
    """
    return f"{amount / capital * 100 + 0:+.2f}%p"


def _r1(x: Decimal) -> Decimal:
    """bp 값을 표에 찍는 대로 소수 한 자리로 반올림한다(-0.0은 0.0으로).

    Args:
        x: bp 값.

    Returns:
        반올림한 Decimal.
    """
    return x.quantize(Decimal("0.1")) + 0


def _bp(x: Decimal) -> str:
    """bp 값을 소수 한 자리로 쓴다(-0.0은 +0.0으로).

    Args:
        x: bp 값.

    Returns:
        예: "-5.6bp".
    """
    return f"{_r1(x):+.1f}bp"


def _fee_side(amount: Decimal, ok: int, total: int, capital: Decimal) -> str:
    """수수료 한쪽을 쓴다. 체결이 없거나 환산된 체결이 하나도 없으면 숫자 대신 점검 불가다.

    Args:
        amount: 환산한 수수료 합(손해 = 음수).
        ok: 환산한 체결 수.
        total: 그쪽 체결 수.
        capital: 자본.

    Returns:
        "-0.04%p" · "점검 불가(체결 없음)" · "점검 불가(환산 안 함 N건)".
    """
    if not total:
        return "점검 불가(체결 없음)"
    return _pct(amount, capital) if ok else f"점검 불가(환산 안 함 {total}건)"


def _span(rng: tuple[datetime, datetime] | None) -> str:
    """파일 범위를 월/일로 쓴다.

    Args:
        rng: (처음, 마지막) UTC 시각. 비었으면 None.

    Returns:
        예: "09/02 ~ 09/28" 또는 "없음".
    """
    return "없음" if rng is None else f"{rng[0]:%m/%d} ~ {rng[1]:%m/%d}"


def _side(e: Execution) -> str:
    """방향을 한국어로 쓴다.

    Args:
        e: 체결.

    Returns:
        "매수" 또는 "매도".
    """
    return "매수" if e.side == "buy" else "매도"


def _price(p: Decimal) -> str:
    """가격을 유효숫자 6자리 이상, 소수 둘째 자리 이상으로 쓴다(SHIB 같은 작은 가격도 보이게).

    Args:
        p: 가격.

    Returns:
        예: "61054.17", "0.0000234500".
    """
    return f"{p:.{max(2, 5 - p.adjusted())}f}"


def _when(e: Execution) -> str:
    """표에 쓸 시각(UTC, 월/일 시:분).

    Args:
        e: 체결.

    Returns:
        예: "10/13 06:58".
    """
    return f"{e.time.astimezone(timezone.utc):%m/%d %H:%M}"


def _execution_line(r: Retro) -> str:
    """결론의 실행 줄. 기간 안 실제 체결이 없고 fills_until도 없으면 놓친 주문을 추정하지 않는다(fills_until이 있으면 그 앞 거래는 놓친 주문 추정). 다만 체결 파일 범위 밖 ·
    마지막 체결 뒤(구분 불가) 백테스트 거래는 실제 체결이 없어도 줄에 센다(멈춘 봇이 가려지지 않게).
    이 둘은 놓친 주문과 따로 센다. 「확인 불가」는 실제 체결도 fills_until도 없을 때만 쓴다:
    fills_until을 받았는데 기간 안 거래가 모두 없으면(쉬는 기간) 「점검 불가(기간 안 백테스트 · 실제 거래 없음)」다.

    Args:
        r: Retro.

    Returns:
        한 줄 문자열.
    """
    late = ""
    if r.beyond_fills:
        late += f" · 체결 파일 범위 밖 {len(r.beyond_fills)}건(점검 불가)"
    if r.after_last_fill:
        late += f" · 마지막 체결 뒤 {len(r.after_last_fill)}건(봇이 멈췄는지 체결 파일이 끝났는지 구분 불가 · 점검 불가)"
    if r.executions == 0 and not (r.fills_until and r.result.backtest_only):
        if late:
            return f"실행 · 기간 안 실제 체결 없음{late}"
        return "실행 · 점검 불가(기간 안 백테스트 · 실제 거래 없음)" if r.fills_until else "실행 · 확인 불가(기간 안 실제 체결 없음)"
    if r.backtest_executions == 0:
        return "실행 · 점검 불가(기간 안 백테스트 거래 없음)"
    missed, extra = r.result.backtest_only, r.result.actual_only
    line = f"실행 · 백테스트에만 있는 거래 {len(missed)}건"
    if missed:
        line += "(놓친 주문으로 추정) · 손익 영향은 점검 불가(가격 데이터 없음)"
    return line + late + f" · 실제에만 있는 거래 {len(extra)}건" + ("(원인 모름)" if extra else "")


def render(r: Retro) -> str:
    """회고 숫자를 결론 → 근거 → 대응 순서의 마크다운 한 장으로 낸다.

    Args:
        r: Retro.

    Returns:
        마크다운 문자열.
    """
    period = f"{r.start:%Y-%m-%d} ~ {(r.end - timedelta(seconds=1)):%Y-%m-%d} (UTC)"
    if r.slip_bp:
        over = sum(1 for x in r.slip_bp if _r1(x) < -SLIP_LIMIT_BP)
        slip_line = (f"체결가 차이(원인은 추정) · {_pct(r.slip_cost, r.capital)} · 중앙값 {_bp(statistics.median(r.slip_bp))}"
                     f" · 가장 나쁜 짝 {_bp(min(r.slip_bp))} · {SLIP_LIMIT_BP}bp 넘음 {over}건")
        if r.slip_unconverted:
            slip_line += f" · {r.quote} 아닌 시장 {r.slip_unconverted}건은 금액 합에서 뺌"
        med = statistics.median(r.qty_ratios)
        off = sum(1 for q in r.qty_ratios if abs(q / med - 1) > QTY_TOLERANCE)
        med_text = f"{med:.2f}" if med >= Decimal("0.1") else f"{med:.2g}"
        qty_line = f"수량 · 실제 ÷ 백테스트 비율 중앙값 {med_text} · 중앙값에서 {QTY_TOLERANCE:.0%} 넘게 다른 짝 {off}건"
    else:
        slip_line = "체결가 차이 · 짝지은 체결 없음"
        qty_line = "수량 · 짝지은 체결 없음"
    act_ok, bt_ok = r.executions - r.fee_unconverted, r.backtest_executions - r.fee_backtest_unconverted
    fee_line = (f"수수료 · 실제 {_fee_side(r.fee_actual, act_ok, r.executions, r.capital)}"
                f" · 백테스트 가정 {_fee_side(r.fee_backtest, bt_ok, r.backtest_executions, r.capital)}")
    if not act_ok or not bt_ok:
        fee_line += " · 차이 점검 불가"
    elif act_ok < r.executions or bt_ok < r.backtest_executions:
        fee_line += f" · 차이는 일부만 비교(실제 {act_ok}/{r.executions}건 · 백테스트 {bt_ok}/{r.backtest_executions}건 환산)"
    else:
        fee_line += f" · 차이 {_pct(r.fee_actual - r.fee_backtest, r.capital)}"
    if r.fee_estimated:
        fee_line += f" · 받은 자산으로 낸 {r.fee_estimated}건은 체결가로 환산(추정)"
    if r.funding_total is None:
        fund_line = "펀딩비 · 점검 불가(펀딩비 파일 없음)"
    elif r.backtest_range is None:  # 펀딩비는 백테스트 종목으로 거르는데 그 종목이 없다
        fund_line = "펀딩비 · 점검 불가(백테스트 종목 없음)"
    else:
        fund_line = f"펀딩비 · {_pct(r.funding_total, r.capital)}"
        if r.funding_unconverted:
            fund_line += f" · {r.quote} 아닌 {r.funding_unconverted}줄은 환산 안 함"
    since = f"실전 시작({r.live_start:%m/%d}) 이후 거래 {r.trades_since_live}건"
    if r.trades_since_live < HOLD_TRADES:
        strategy = f"전략: 판단 보류 · {since} · {HOLD_TRADES}건까지 {HOLD_TRADES - r.trades_since_live}건"
    else:
        strategy = f"전략: 점검 불가(정상 범위 없음, 이 회고는 실행만 본다) · {since}"
    lines = [
        f"# 실행 회고 · {period}",
        "",
        "## 결론",
        f"- {_execution_line(r)}",
        f"- {slip_line}",
        f"- {qty_line}",
        f"- {fee_line}",
        f"- {fund_line}",
        f"- {strategy}",
        "",
        "## 근거",
        f"- {period} · 실전 시작 {r.live_start:%Y-%m-%d %H:%M} UTC · 자본 {r.capital} {r.quote} · %p는 자본 대비",
        f"- 이 기간 실제 체결 {r.executions}건 · 거래 {r.trades}건(체결 ÷ 2) · 백테스트 체결 {r.backtest_executions}건",
        f"- 짝지은 체결 {len(r.result.pairs)}건 · 백테스트에만 {len(r.result.backtest_only)}건"
        + (f" · 체결 파일 범위 밖 {len(r.beyond_fills)}건" if r.beyond_fills else "")
        + (f" · 마지막 체결 뒤 {len(r.after_last_fill)}건" if r.after_last_fill else "")
        + f" · 실제에만 {len(r.result.actual_only)}건 · 매칭 창 {r.window.total_seconds() / 60:.0f}분",
    ]
    outside = sum(1 for p in r.result.pairs if not r.start <= p.actual.time < r.end)
    if outside or r.edge_excluded:
        lines.append("- 실제 체결은 짝의 백테스트 시각 기준으로 셈"
                     + (f" · 기간 밖 시각의 실제 체결 {outside}건 포함" if outside else "")
                     + (f" · 기간 밖 백테스트와 짝지은 실제 체결 {r.edge_excluded}건 제외" if r.edge_excluded else ""))
    told = "blackclip fetch 기록에서 읽음" if r.fills_until_source == "blackclip fetch" else "--fills-until로 받음"
    frm = f"{r.fills_from:%m/%d %H:%M}부터 " if r.fills_from else ""
    cover = (f" · 체결 파일이 {frm}{r.fills_until - timedelta(seconds=1):%m/%d %H:%M}까지 담겼다고 {told}" if r.fills_until
             else " · 체결 파일이 어디까지 담겼는지 받지 않음(마지막 체결 뒤 백테스트 거래는 구분 불가로 둠)")
    lines += [
        f"- 체결 파일 범위 {_span(r.actual_range)} · 백테스트 파일 범위 {_span(r.backtest_range)} (UTC, 넣은 파일 전체 기준){cover}",
        "- 체결가 차이 = 백테스트가 가정한 가격 대비 실제 체결가. 손해(비싸게 삼 · 싸게 팖)를 음수로 적는다.",
        "- 봇을 고치지 않았으므로 봇이 그 순간 본 가격은 모른다. 실행 쪽 원인은 모두 추정이다.",
        "",
        "| 시각(UTC) | 종목 | 백테스트 | 실제 | 차이 | 원인(추정) |",
        "| --- | --- | --- | --- | --- | --- |",
    ]
    rows: list[tuple[datetime, str]] = []
    for p in r.result.pairs:
        b, a, bp = p.backtest, p.actual, slip_bp(p)
        shown = _r1(bp)
        cause = ("비싸게 체결" if b.side == "buy" else "싸게 체결") if shown < 0 else "-"
        rows.append((b.time, f"| {_when(b)} | {b.symbol} | {_side(b)} {b.qty} · {_price(b.price)} | {_when(a)} · {_side(a)} {a.qty} · {_price(a.price)} | {_bp(bp)} | {cause} |"))
    # 짝 없는 백테스트는 모두 표에 보인다(근거 개수와 같게). 기간 안 실제 체결도 fills_until도 없으면 놓친 주문이라고 추정하지 않는다.
    for label, items in (("놓친 주문(추정)" if r.executions or r.fills_until else "확인 불가(기간 안 실제 체결 없음)", r.result.backtest_only),
                         ("체결 파일 범위 밖", r.beyond_fills), ("마지막 체결 뒤(구분 불가)", r.after_last_fill)):
        rows += [(b.time, f"| {_when(b)} | {b.symbol} | {_side(b)} {b.qty} · {_price(b.price)} | 없음 | 점검 불가 | {label} |") for b in items]
    for a in r.result.actual_only:
        rows.append((a.time, f"| {_when(a)} | {a.symbol} | 없음 | {_when(a)} · {_side(a)} {a.qty} · {_price(a.price)} | 점검 불가 | 실제에만 있음(원인 모름) |"))
    lines += [row for _, row in sorted(rows, key=lambda x: x[0])]
    lines += [
        "",
        "## 대응",
        "- 가드레일이 아직 없어 적힌 대응이 없습니다. 이 회고는 체결 내역과 백테스트 거래 목록만 썼고, 무엇을 할지는 고객이 정합니다.",
    ]
    return "\n".join(lines) + "\n"

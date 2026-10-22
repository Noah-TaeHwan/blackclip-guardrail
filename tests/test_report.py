"""회고 숫자와 한 장: 부호 · 기간 · 실전 시작 · 수수료 · 펀딩비 · 수량 · 판단 보류 문구 · 순서."""
from datetime import datetime, timedelta, timezone
from decimal import Decimal

import pytest

from blackclip.retro.fills import Execution, Funding
from blackclip.retro.match import Pair
from blackclip.retro.report import build_retro, fee_in_quote, render, slip_bp

UTC = timezone.utc
END = datetime(2026, 10, 15, 0, 0, tzinfo=UTC)
LIVE = datetime(2026, 9, 1, 0, 0, tzinfo=UTC)
CAP = Decimal("10000")


def ex(t, side="buy", price="61000", fee="1", fee_asset="USDT", oid="x", qty="0.12", symbol="BTCUSDT"):
    """테스트용 체결 하나를 만든다."""
    return Execution(t, symbol, side, Decimal(qty), Decimal(price), Decimal(fee), fee_asset, oid)


def test_slip_sign_buy_and_sell():
    """비싸게 산 것 · 싸게 판 것은 음수(손해)다."""
    t = END - timedelta(days=1)
    assert slip_bp(Pair(ex(t, price="61020"), ex(t, price="61054"))) < 0
    assert slip_bp(Pair(ex(t, "sell", "63410"), ex(t, "sell", "63380"))) < 0
    assert slip_bp(Pair(ex(t, price="61054"), ex(t, price="61020"))) > 0


def test_period_filter_last_30_days():
    """기간 밖 체결은 세지 않는다."""
    old = ex(END - timedelta(days=40), oid="old")
    new = ex(END - timedelta(days=2), oid="new")
    r = build_retro([old, new], [new], None, CAP, END, LIVE)
    assert r.executions == 1 and not r.result.backtest_only


def test_before_live_start_not_missed():
    """실전 시작 전 백테스트 거래는 놓친 주문이 아니다(실전 구간만 비교)."""
    live = END - timedelta(days=10)
    early = ex(END - timedelta(days=20), side="sell", oid="early")
    b, a = ex(END - timedelta(days=2), oid="b"), ex(END - timedelta(days=2), oid="a")
    r = build_retro([early, b], [a], None, CAP, END, live)
    assert r.start == live and not r.result.backtest_only
    assert "백테스트에만 있는 거래 0건" in render(r)


def test_trades_counted_since_live_start():
    """30건 판단은 30일 창이 아니라 실전 시작부터 누적으로 센다."""
    acts = [ex(END - timedelta(days=60) + timedelta(hours=12 * i), oid=str(i)) for i in range(100)]
    r = build_retro([], acts, None, CAP, END, END - timedelta(days=60))
    assert r.trades_since_live == 50 and r.trades < 50
    assert "실전 시작(08/16) 이후 거래 50건" in render(r)


def test_edge_pair_not_broken():
    """기간 끝 바로 앞 백테스트와 끝 바로 뒤 체결은 짝으로 맺고 놓친 주문으로 세지 않는다."""
    b = ex(END - timedelta(seconds=10), side="sell", oid="b")
    a = ex(END + timedelta(seconds=5), side="sell", oid="a")
    r = build_retro([b, ex(END - timedelta(days=1), oid="b2")], [a, ex(END - timedelta(days=1), oid="a2")], None, CAP, END, LIVE)
    assert len(r.result.pairs) == 2 and not r.result.backtest_only


def test_no_actual_fills_is_unconfirmed():
    """기간 안 실제 체결이 없으면 놓친 주문을 추정하지 않고 확인 불가로 쓴다."""
    page = render(build_retro([ex(END - timedelta(days=1))], [], None, CAP, END, LIVE))
    assert "확인 불가(기간 안 실제 체결 없음" in page and "놓친 주문" not in page
    assert "파일과 기간을 확인" not in page
    assert "- 실행 · 확인 불가(기간 안 실제 체결 없음)" in page.splitlines()


def test_no_backtest_is_not_checkable():
    """기간 안 백테스트 거래가 없으면 「0건」이 아니라 점검 불가다."""
    page = render(build_retro([], [ex(END - timedelta(days=1))], None, CAP, END, LIVE))
    assert "점검 불가(기간 안 백테스트 거래 없음" in page


def test_quantity_ratio_shown():
    """짝은 맺었어도 수량 비율을 결론에 적는다(10배 줄어도 보이게)."""
    t = END - timedelta(days=1)
    r = build_retro([ex(t, qty="1.0")], [ex(t, qty="0.1")], None, CAP, END, LIVE)
    assert "수량 · 실제 ÷ 백테스트 비율 중앙값 0.10" in render(r)


def test_small_quantity_ratio_shown():
    """중앙값이 0.1 아래면 0.00으로 뭉개지 않고 유효숫자 두 자리로 쓴다."""
    t = END - timedelta(days=1)
    r = build_retro([ex(t, qty="30")], [ex(t, qty="0.12")], None, CAP, END, LIVE)
    assert "비율 중앙값 0.004" in render(r)


def test_quantity_off_median_counted():
    """비율이 중앙값에서 10% 넘게 벗어난 짝을 센다(자본 차이로 생긴 고른 비율은 세지 않음)."""
    t = END - timedelta(days=3)
    bts = [ex(t + timedelta(hours=5 * i), qty="1", oid=f"b{i}") for i in range(3)]
    acts = [ex(t + timedelta(hours=5 * i), qty=q, oid=f"a{i}") for i, q in enumerate(["0.5", "0.5", "0.1"])]
    assert "중앙값에서 10% 넘게 다른 짝 1건" in render(build_retro(bts, acts, None, CAP, END, LIVE))


def test_fee_assets():
    """수수료: 거래 통화는 그대로, 받은 기초 자산은 체결가로 환산(추정), 빈 자산 · 다른 자산은 뺀다."""
    t = END - timedelta(days=1)
    acts = [ex(t, fee="1", oid="a"),
            ex(t + timedelta(hours=1), fee="0.0001", fee_asset="BTC", price="60000", oid="b"),
            ex(t + timedelta(hours=2), fee="0.5", fee_asset="", oid="c"),
            ex(t + timedelta(hours=3), fee="0.001", fee_asset="BNB", oid="d")]
    r = build_retro([], acts, None, CAP, END, LIVE)
    assert r.fee_actual == Decimal("-7") and r.fee_estimated == 1 and r.fee_unconverted == 2
    page = render(r)
    assert "체결가로 환산(추정)" in page and "백테스트 가정 점검 불가(체결 없음) · 차이 점검 불가" in page


def test_backtest_fee_unconverted_counted():
    """백테스트 수수료 자산이 비었으면 말없이 빼지 않고 건수로 쓰며 차이는 숫자로 내지 않는다."""
    t = END - timedelta(days=1)
    page = render(build_retro([ex(t, fee_asset="")], [ex(t, fee="1")], None, CAP, END, LIVE))
    assert "백테스트 가정 점검 불가(환산 안 함 1건) · 차이 점검 불가" in page
    assert " · 차이 -" not in page and " · 차이 +" not in page


def test_actual_fee_in_bnb_partial_compare():
    """실제 수수료를 BNB로 냈으면 차이는 일부만 비교라고 쓴다."""
    t = END - timedelta(days=1)
    page = render(build_retro([ex(t, fee="1")], [ex(t, fee="0.001", fee_asset="BNB")], None, CAP, END, LIVE))
    assert "실제 점검 불가(환산 안 함 1건) · 백테스트 가정 -0.01%p · 차이 점검 불가" in page


def test_fee_compared_with_backtest():
    """수수료 줄은 실제 · 백테스트 가정 · 차이를 함께 쓴다."""
    t = END - timedelta(days=1)
    r = build_retro([ex(t, fee="1")], [ex(t, fee="3")], None, CAP, END, LIVE)
    assert "수수료 · 실제 -0.03%p · 백테스트 가정 -0.01%p · 차이 -0.02%p" in render(r)


def test_other_quote_not_added():
    """거래 통화가 아닌 시장 · 펀딩비 금액은 합계에 더하지 않고 건수로 남긴다."""
    t = END - timedelta(days=1)
    b = ex(t, symbol="ETHBTC", price="0.04", fee_asset="BTC", fee="0.0001")
    a = ex(t, symbol="ETHBTC", price="0.0402", fee_asset="BTC", fee="0.0001")
    f = [Funding(t, "ETHBTC", Decimal("-1.25"), "USDT"), Funding(t, "ETHBTC", Decimal("-0.0004"), "BTC")]
    r = build_retro([b], [a], f, CAP, END, LIVE)
    assert r.slip_cost == 0 and r.slip_unconverted == 1 and r.fee_actual == 0 and r.fee_unconverted == 1
    assert r.funding_total == Decimal("-1.25") and r.funding_unconverted == 1


def test_slip_cost_value_and_sign():
    """체결가 차이 금액은 (실제 - 백테스트) × 수량의 손해 부호로 정확히 나온다(손계산 -4.08)."""
    t = END - timedelta(days=1)
    r = build_retro([ex(t, price="61020", qty="0.12")], [ex(t, price="61054", qty="0.12")], None, CAP, END, LIVE)
    assert r.slip_cost == Decimal("-4.08")


def test_missing_funding_marks_only_that_line():
    """펀딩비 파일이 없으면 그 줄만 점검 불가이고 나머지는 나온다."""
    t = END - timedelta(days=1)
    page = render(build_retro([ex(t)], [ex(t, price="61054")], None, CAP, END, LIVE))
    assert "펀딩비 · 점검 불가(펀딩비 파일 없음)" in page
    assert "체결가 차이" in page and "수수료" in page


def test_funding_summed_in_period():
    """기간 안 펀딩비만 더한다."""
    f = [Funding(END - timedelta(days=1), "BTCUSDT", Decimal("-1.25"), "USDT"),
         Funding(END - timedelta(days=60), "BTCUSDT", Decimal("-9"), "USDT")]
    assert build_retro([ex(END - timedelta(days=60))], [], f, CAP, END, LIVE).funding_total == Decimal("-1.25")


def test_few_trades_hold_wording():
    """거래 30건 전이면 판단 보류와 남은 건수만 쓰고, 판정 · 권유 말이나 예시 전략 숫자를 쓰지 않는다."""
    t = END - timedelta(days=1)
    acts = [ex(t + timedelta(hours=i), oid=str(i)) for i in range(6)]
    page = render(build_retro([], acts, None, CAP, END, LIVE))
    assert "전략: 판단 보류 · 실전 시작(09/01) 이후 거래 3건 · 30건까지 27건" in page
    for banned in ("정상이에요", "괜찮", "안전", "사세요", "파세요", "끄세요", "1,000건", "확인해 보세요", "파일과 기간을 확인"):
        assert banned not in page


def test_many_trades_not_checkable():
    """30건이 넘어도 정상 범위가 없으니 판단 보류가 아니라 점검 불가다."""
    acts = [ex(END - timedelta(days=20) + timedelta(hours=6 * i), oid=str(i)) for i in range(62)]
    page = render(build_retro([], acts, None, CAP, END, LIVE))
    assert "전략: 점검 불가(정상 범위 없음, 이 회고는 실행만 본다)" in page


def test_backtest_only_wording_and_estimate_tag():
    """놓친 주문은 「백테스트에만 있는 거래 N건(놓친 주문으로 추정)」이고 손익 영향은 점검 불가다."""
    b = ex(END - timedelta(days=2), side="sell", price="62140", oid="b")
    a = ex(END - timedelta(days=1), oid="a")  # 체결 파일이 b의 날을 덮는다(파일 범위 밖이면 놓친 주문이 아님)
    page = render(build_retro([b], [a], None, CAP, END, LIVE))
    assert "백테스트에만 있는 거래 1건(놓친 주문으로 추정)" in page
    assert "점검 불가(가격 데이터 없음" in page
    assert "실제에만 있는 거래 1건(원인 모름)" in page.split("## 근거")[0]


def test_small_price_visible():
    """가격이 아주 작은 종목도 표에서 0.00으로 뭉개지지 않는다."""
    t = END - timedelta(days=1)
    r = build_retro([ex(t, price="0.00002345", symbol="SHIBUSDT")], [ex(t, price="0.00002351", symbol="SHIBUSDT")], None, CAP, END, LIVE)
    assert "0.0000234500" in render(r)


def test_table_has_symbol_actual_time_and_korean_side():
    """짝 표는 종목 열과 실제 체결 시각을 쓰고 방향은 한국어로 쓴다."""
    t = END - timedelta(days=1)
    bts = [ex(t, oid="b1"), ex(t + timedelta(hours=1), symbol="ETHUSDT", price="3400", qty="2", oid="b2")]
    acts = [ex(t + timedelta(minutes=1), oid="a1"), ex(t + timedelta(hours=1), symbol="ETHUSDT", price="3400", qty="2", oid="a2")]
    page = render(build_retro(bts, acts, None, CAP, END, LIVE))
    rows = [line for line in page.splitlines() if line.startswith("| ")]
    assert any("| BTCUSDT |" in line for line in rows) and any("| ETHUSDT |" in line for line in rows)
    assert any("매수" in line for line in rows)
    assert "10/14 00:01 · 매수" in page


def test_cause_follows_rounded_bp():
    """반올림해서 +0.0bp로 찍히는 짝은 원인을 비싸게 체결이라고 쓰지 않는다."""
    t = END - timedelta(days=1)
    page = render(build_retro([ex(t, price="61000")], [ex(t, price="61000.1")], None, CAP, END, LIVE))
    assert "| +0.0bp | - |" in page and "비싸게 체결" not in page


def test_capital_and_end_inputs_checked():
    """자본이 0 이하거나 끝이 실전 시작보다 앞 · 같으면 빈 장을 내지 않고 거부한다."""
    with pytest.raises(ValueError, match="자본은 0보다 커야 합니다"):
        build_retro([], [], None, Decimal(0), END, LIVE)
    with pytest.raises(ValueError, match="자본은 0보다 커야 합니다"):
        build_retro([], [], None, Decimal("-1"), END, LIVE)
    with pytest.raises(ValueError, match="끝 시각이 실전 시작보다 뒤여야 합니다"):
        build_retro([], [], None, CAP, LIVE, END)


def test_page_order_conclusion_evidence_response():
    """한 장은 결론 → 근거 → 대응 순서다."""
    page = render(build_retro([], [], None, CAP, END, LIVE))
    assert page.index("## 결론") < page.index("## 근거") < page.index("## 대응")


def _rows(page):
    """표의 데이터 행을 칸 목록으로 쪼갠다(머리줄 · 구분줄 제외)."""
    return [line.strip("| ").split(" | ") for line in page.splitlines() if line.startswith("| ") and not line.startswith(("| 시각", "| ---"))]


def test_period_counts_tie_out_by_backtest_time():
    """기간은 짝의 백테스트 시각이 정한다: 근거의 실제 체결 수 = 표의 짝 + 실제에만이고, 기간 밖 백테스트와 짝지은 체결은 어디에도 안 든다."""
    start = END - timedelta(days=30)
    bts = [ex(start + timedelta(days=1), oid="b1", price="60000"),
           ex(start + timedelta(days=2), "sell", "61000", oid="b2"),
           ex(END - timedelta(minutes=10), price="62000", oid="b3", symbol="ETHUSDT"),  # 기간 끝 안
           ex(start - timedelta(hours=1), price="50000", oid="b0", symbol="XRPUSDT"),  # 기간 시작 밖
           ex(start + timedelta(days=5), "sell", oid="bm", symbol="SOLUSDT")]  # 짝 없음
    acts = [ex(start + timedelta(days=1, minutes=1), price="60060", oid="a1"),
            ex(start + timedelta(days=2, minutes=1), "sell", "60939", oid="a2"),
            ex(END + timedelta(minutes=20), price="62062", oid="a3", symbol="ETHUSDT"),  # 기간 끝 밖이지만 짝의 백테스트가 안
            ex(start + timedelta(minutes=30), price="50500", oid="a0", symbol="XRPUSDT"),  # 기간 안이지만 짝의 백테스트가 밖
            ex(start + timedelta(days=6), oid="ax", symbol="LTCUSDT")]  # 짝 없음
    r = build_retro(bts, acts, None, CAP, END, LIVE)
    pairs, only_a, only_b = len(r.result.pairs), len(r.result.actual_only), len(r.result.backtest_only)
    assert (pairs, only_a, only_b, r.edge_excluded) == (3, 1, 1, 1)
    assert r.executions == pairs + only_a == 4 and r.backtest_executions == pairs + only_b + len(r.beyond_fills) + len(r.after_last_fill) == 4
    assert r.slip_cost == Decimal("-21.96") and len(r.slip_bp) == 3  # 밖 짝(XRP -60)은 빠진다
    assert r.fee_actual == Decimal("-4") and r.fee_backtest == Decimal("-4")  # 수수료도 같은 집합: a3는 들고 a0는 뺀다
    page = render(r)
    assert "이 기간 실제 체결 4건 · 거래 2건(체결 ÷ 2) · 백테스트 체결 4건" in page
    rows = _rows(page)
    assert sum(1 for c in rows if c[3] != "없음") == r.executions and sum(1 for c in rows if c[2] != "없음") == r.backtest_executions
    assert "XRPUSDT" not in page
    assert "- 실제 체결은 짝의 백테스트 시각 기준으로 셈 · 기간 밖 시각의 실제 체결 1건 포함 · 기간 밖 백테스트와 짝지은 실제 체결 1건 제외" in page


def test_edge_excluded_line_only_when_nonzero():
    """기간 밖 백테스트와 짝지은 실제 체결이 없으면 그 근거 줄을 쓰지 않는다."""
    t = END - timedelta(days=1)
    page = render(build_retro([ex(t)], [ex(t)], None, CAP, END, LIVE))
    assert "기간 밖 백테스트와 짝지은" not in page


def test_backtest_after_last_fill_day_is_not_distinguishable():
    """fills_until이 없으면 체결 파일 마지막 날 다음 날부터의 짝 없는 백테스트는 놓친 주문도 범위 밖도 아닌 「마지막 체결 뒤(구분 불가)」다. 마지막 날은 통째로 덮인 것으로 본다."""
    t = END - timedelta(days=10)
    bts = [ex(t, oid="b1"), ex(t + timedelta(hours=20), "sell", oid="b2"), ex(t + timedelta(hours=25), "sell", oid="b3")]
    r = build_retro(bts, [ex(t + timedelta(minutes=1), oid="a1")], None, CAP, END, LIVE)
    assert [e.order_id for e in r.result.backtest_only] == ["b2"] and [e.order_id for e in r.after_last_fill] == ["b3"] and not r.beyond_fills
    assert r.backtest_executions == len(r.result.pairs) + len(r.result.backtest_only) + len(r.beyond_fills) + len(r.after_last_fill) == 3
    page = render(r)
    head = page.split("## 근거")[0]
    assert "백테스트에만 있는 거래 1건(놓친 주문으로 추정) · 손익 영향은 점검 불가(가격 데이터 없음) · 마지막 체결 뒤 1건(봇이 멈췄는지 체결 파일이 끝났는지 구분 불가 · 점검 불가) · 실제에만 있는 거래 0건" in head
    assert "짝지은 체결 1건 · 백테스트에만 1건 · 마지막 체결 뒤 1건 · 실제에만 0건" in page
    assert "체결 파일 범위 밖" not in page and page.count("놓친 주문(추정)") == 1
    row = next(c for c in _rows(page) if c[0] == "10/06 01:00")
    assert row[3:] == ["없음", "점검 불가", "마지막 체결 뒤(구분 불가)"]


def test_only_after_last_fill_backtest_never_says_missed_order():
    """백테스트 거래가 전부 마지막 체결 뒤면 「놓친 주문」이라는 말이 한 장 어디에도 없다."""
    t = END - timedelta(days=10)
    page = render(build_retro([ex(t, oid="b1"), ex(t + timedelta(days=3), oid="b2")], [ex(t, oid="a1")], None, CAP, END, LIVE))
    assert "놓친 주문" not in page and "마지막 체결 뒤 1건(" in page and "체결 파일 범위 밖" not in page


def test_unpaired_backtest_classes_tie_out():
    """짝 없는 백테스트는 놓친 주문 · 체결 파일 범위 밖 · 마지막 체결 뒤(구분 불가) 중 하나이고, 짝 + 셋의 합이 기간 안 백테스트 체결 수와 같다. 범위 밖은 fills_until을 줬을 때만, 구분 불가는 안 줬을 때만 나온다."""
    t = END - timedelta(days=10)
    bts = [ex(t, oid="b1"), ex(t + timedelta(hours=20), "sell", oid="b2"), ex(t + timedelta(hours=25), "sell", oid="b3")]
    acts = [ex(t + timedelta(minutes=1), oid="a1")]
    for fills_until, missed, beyond, after in ((None, ["b2"], [], ["b3"]), (t + timedelta(hours=22), ["b2"], ["b3"], []), (END, ["b2", "b3"], [], [])):
        r = build_retro(bts, acts, None, CAP, END, LIVE, fills_until=fills_until)
        assert ([e.order_id for e in r.result.backtest_only], [e.order_id for e in r.beyond_fills], [e.order_id for e in r.after_last_fill]) == (missed, beyond, after)
        assert r.backtest_executions == len(r.result.pairs) + len(missed) + len(beyond) + len(after) == 3


def _tied_to_table(r, page):
    """근거의 개수가 표와 같다: 백테스트 칸이 찬 줄 = 기간 안 백테스트 체결 수, 실제 칸이 찬 줄 = 기간 안 실제 체결 수."""
    rows = _rows(page)
    assert sum(1 for c in rows if c[2] != "없음") == r.backtest_executions
    assert sum(1 for c in rows if c[3] != "없음") == r.executions


def _stopped_bot_period(fills_until=None):
    """실제 체결은 09/02 하나뿐이고 30일 기간(09/15~10/14)에는 체결이 없다. 기간 안 백테스트 거래는 10/10 · 10/14 두 건."""
    t = LIVE + timedelta(days=1)
    bts = [ex(t, oid="b0"), ex(END - timedelta(days=5), "sell", oid="b1"), ex(END - timedelta(days=1), oid="b2")]
    return build_retro(bts, [ex(t, oid="a0")], None, CAP, END, LIVE, fills_until=fills_until)


def test_period_without_actual_fills_keeps_trades_after_last_fill_visible():
    """기간에 실제 체결이 없어도 마지막 체결 뒤 백테스트 거래는 확인 불가 문장 뒤에 숨지 않고, 표와 근거 개수가 같다."""
    r = _stopped_bot_period()
    assert r.executions == 0 and len(r.after_last_fill) == 2 and not r.result.backtest_only
    page = render(r)
    assert "- 실행 · 기간 안 실제 체결 없음 · 마지막 체결 뒤 2건(봇이 멈췄는지 체결 파일이 끝났는지 구분 불가 · 점검 불가)" in page.splitlines()
    assert "확인 불가(기간 안 실제 체결 없음)" not in page and "놓친 주문" not in page
    _tied_to_table(r, page)


def test_period_without_actual_fills_and_fills_until_estimates_missed():
    """fills_until을 받았으면 기간에 실제 체결이 없어도 그 앞 거래는 놓친 주문(추정), 뒤 거래는 범위 밖이다. 개수는 표와 같다."""
    r = _stopped_bot_period(fills_until=END - timedelta(days=3))
    assert r.executions == 0 and [e.order_id for e in r.result.backtest_only] == ["b1"] and [e.order_id for e in r.beyond_fills] == ["b2"]
    page = render(r)
    assert ("- 실행 · 백테스트에만 있는 거래 1건(놓친 주문으로 추정) · 손익 영향은 점검 불가(가격 데이터 없음)"
            " · 체결 파일 범위 밖 1건(점검 불가) · 실제에만 있는 거래 0건") in page.splitlines()
    assert "| 없음 | 점검 불가 | 놓친 주문(추정) |" in page and "확인 불가(기간 안 실제 체결 없음)" not in page
    _tied_to_table(r, page)


def test_coverage_line_uses_whole_file_ranges():
    """근거에 넣은 파일 전체의 범위(월/일, UTC)를 쓴다. 기간으로 자른 범위가 아니다."""
    old, t = END - timedelta(days=50), END - timedelta(days=2)
    r = build_retro([ex(old, oid="o"), ex(t, oid="b")], [ex(old + timedelta(hours=1), oid="p"), ex(t, oid="a")], None, CAP, END, LIVE)
    assert "- 체결 파일 범위 08/26 ~ 10/13 · 백테스트 파일 범위 08/26 ~ 10/13 (UTC, 넣은 파일 전체 기준)" in render(r)


def test_coverage_line_says_none_for_empty_side():
    """한쪽 파일이 비었으면 그쪽 범위는 「없음」이다."""
    t = END - timedelta(days=1)
    assert "- 체결 파일 범위 없음 · 백테스트 파일 범위 10/14 ~ 10/14 (UTC, 넣은 파일 전체 기준)" in render(build_retro([ex(t)], [], None, CAP, END, LIVE))
    assert "- 체결 파일 범위 10/14 ~ 10/14 · 백테스트 파일 범위 없음 (UTC, 넣은 파일 전체 기준)" in render(build_retro([], [ex(t)], None, CAP, END, LIVE))


def test_percent_point_basis_stated():
    """%p가 자본 대비라는 것을 근거 첫 줄에 쓴다."""
    assert "자본 10000 USDT · %p는 자본 대비" in render(build_retro([], [], None, CAP, END, LIVE))


def test_zero_fee_counts_as_converted_whatever_the_asset():
    """수수료가 0이면 자산이 비었든 다른 자산이든 시장이 달라도 0으로 센다."""
    for asset in ("", "BNB", "USDT"):
        assert fee_in_quote(ex(END, fee="0", fee_asset=asset), "USDT") == (Decimal(0), False)
    assert fee_in_quote(ex(END, fee="0", fee_asset="", symbol="ETHBTC"), "USDT") == (Decimal(0), False)
    assert fee_in_quote(ex(END, fee="0.001", fee_asset="BNB"), "USDT") == (None, False)


def _trade_pairs(n, bt_fee, bt_asset, act_fee, act_asset):
    """n쌍(백테스트 + 실제)을 5시간 간격으로 만든다."""
    t = END - timedelta(days=5)
    bts = [ex(t + timedelta(hours=5 * i), oid=f"b{i}", fee=bt_fee, fee_asset=bt_asset) for i in range(n)]
    acts = [ex(t + timedelta(hours=5 * i, minutes=3), oid=f"a{i}", fee=act_fee, fee_asset=act_asset) for i in range(n)]
    return bts, acts


def _fee_line(page):
    """결론의 수수료 줄을 돌려준다."""
    return next(line for line in page.split("## 근거")[0].splitlines() if line.startswith("- 수수료"))


def test_all_bnb_fees_have_no_headline_number():
    """수수료를 전부 BNB로 냈으면 실제 쪽은 숫자를 내지 않는다. 비워 둔 백테스트 자산(수수료 0)이 비교를 끄지도 않는다(반복 C)."""
    bts, acts = _trade_pairs(6, "0", "", "0.0004", "BNB")
    line = _fee_line(render(build_retro(bts, acts, None, CAP, END, LIVE)))
    assert line == "- 수수료 · 실제 점검 불가(환산 안 함 6건) · 백테스트 가정 +0.00%p · 차이 점검 불가"
    assert "실제 +0.00%p" not in line and "일부만" not in line
    bts, acts = _trade_pairs(6, "5", "USDT", "0.0004", "BNB")  # 백테스트 수수료가 있으면 줄 어디에도 +0.00%p가 없다
    line = _fee_line(render(build_retro(bts, acts, None, CAP, END, LIVE)))
    assert line == "- 수수료 · 실제 점검 불가(환산 안 함 6건) · 백테스트 가정 -0.30%p · 차이 점검 불가"


def test_partial_fee_compare_shows_counts_of_both_sides():
    """일부만 환산했으면 금액은 내고 어느 쪽이 몇 건 환산됐는지 쓴다."""
    t = END - timedelta(days=1)
    acts = [ex(t, oid="a1", fee="5"), ex(t + timedelta(hours=5), oid="a2", fee="0.001", fee_asset="BNB")]
    bts = [ex(t + timedelta(hours=10), oid="b1", fee="3"), ex(t + timedelta(hours=15), oid="b2", fee="3"),
           ex(t + timedelta(hours=20), oid="b3", fee="0.001", fee_asset="BNB")]
    line = _fee_line(render(build_retro(bts, acts, None, CAP, END, LIVE)))
    assert line == "- 수수료 · 실제 -0.05%p · 백테스트 가정 -0.06%p · 차이는 일부만 비교(실제 1/2건 · 백테스트 2/3건 환산)"


def test_slip_over_limit_uses_rounded_bp_shown_in_table():
    """20bp 넘음은 표에 찍힌 반올림 값으로 센다: 표에 -20.0bp로 찍힌 짝은 넘음이 아니다."""
    def page_for(*actual_prices):
        t = END - timedelta(days=5)
        bts = [ex(t + timedelta(hours=5 * i), oid=f"b{i}", price="60000") for i, _ in enumerate(actual_prices)]
        acts = [ex(t + timedelta(hours=5 * i, minutes=1), oid=f"a{i}", price=p) for i, p in enumerate(actual_prices)]
        return render(build_retro(bts, acts, None, CAP, END, LIVE))
    page = page_for("60120.06", "60120")  # -20.01bp · -20.00bp, 둘 다 -20.0bp로 찍힌다
    assert page.count("-20.0bp") >= 2 and "20bp 넘음 0건" in page
    assert "20bp 넘음 1건" in page_for("60120.06", "60120.4")  # -20.07bp는 -20.1bp로 찍힌다


def _crashed_bot():
    """봇이 10/05에 멎은 모습: 실제 체결은 10/05까지, 짝 없는 백테스트 거래가 10/09에 있다(마지막 체결 날 다음 날 뒤)."""
    t = END - timedelta(days=10)
    return [ex(t, oid="b1"), ex(t + timedelta(days=4), "sell", oid="b2")], [ex(t + timedelta(minutes=1), oid="a1")]


def test_fills_until_none_leaves_trades_after_last_fill_undistinguishable():
    """fills_until을 안 주면 마지막 체결의 날(UTC) 뒤 백테스트 거래는 구분 불가이고 근거에 체결 파일 끝을 받지 않았다고 쓴다."""
    bts, acts = _crashed_bot()
    r = build_retro(bts, acts, None, CAP, END, LIVE)
    assert r.fills_until is None and [e.order_id for e in r.after_last_fill] == ["b2"] and not r.beyond_fills and not r.result.backtest_only
    page = render(r)
    assert "놓친 주문" not in page and "마지막 체결 뒤 1건(봇이 멈췄는지 체결 파일이 끝났는지 구분 불가 · 점검 불가)" in page
    assert "(UTC, 넣은 파일 전체 기준) · 체결 파일이 어디까지 담겼는지 받지 않음(마지막 체결 뒤 백테스트 거래는 구분 불가로 둠)" in page


def test_fills_until_given_makes_later_backtest_a_missed_order():
    """fills_until을 주면 그 시각 전의 짝 없는 백테스트 거래는 놓친 주문(추정)이다. 봇이 멎은 경우도 놓치지 않는다."""
    bts, acts = _crashed_bot()
    r = build_retro(bts, acts, None, CAP, END, LIVE, fills_until=END)
    assert [e.order_id for e in r.result.backtest_only] == ["b2"] and not r.beyond_fills
    assert r.backtest_executions == len(r.result.pairs) + len(r.result.backtest_only) + len(r.beyond_fills) + len(r.after_last_fill) == 2
    page = render(r)
    assert "백테스트에만 있는 거래 1건(놓친 주문으로 추정)" in page and "체결 파일 범위 밖" not in page and "마지막 체결 뒤" not in page
    assert "(UTC, 넣은 파일 전체 기준) · 체결 파일이 10/14 23:59까지 담겼다고 --fills-until로 받음" in page
    assert "어디까지 담겼는지 받지 않음" not in page


def test_fills_until_is_the_exact_cutoff_not_a_day():
    """컷오프는 fills_until 그 시각이다(날로 올리지 않음): 그 시각과 같거나 뒤면 범위 밖, 앞이면 놓친 주문 후보다. 근거는 fills_until - 1초의 날을 쓴다."""
    t = END - timedelta(days=10)
    cut = END - timedelta(days=3)
    bts = [ex(t, oid="b1"), ex(cut - timedelta(minutes=1), "sell", oid="early"), ex(cut, "sell", price="62000", oid="at_cut")]
    r = build_retro(bts, [ex(t + timedelta(minutes=1), oid="a1")], None, CAP, END, LIVE, fills_until=cut)
    assert [e.order_id for e in r.result.backtest_only] == ["early"] and [e.order_id for e in r.beyond_fills] == ["at_cut"]
    assert "체결 파일이 10/11 23:59까지 담겼다고 --fills-until로 받음" in render(r)


def test_fee_line_empty_side_is_not_a_number():
    """한쪽 체결이 없으면 그쪽 수수료는 숫자가 아니라 점검 불가이고, 차이도 점검 불가다."""
    t = END - timedelta(days=1)
    page = render(build_retro([ex(t, fee="3")], [], None, CAP, END, LIVE))
    line = next(x for x in page.splitlines() if x.startswith("- 수수료"))
    assert line == "- 수수료 · 실제 점검 불가(체결 없음) · 백테스트 가정 -0.03%p · 차이 점검 불가"


def test_basis_sentence_when_counted_actual_is_outside_period():
    """기간 밖 시각의 실제 체결을 셌으면 그 까닭을 근거에 쓴다."""
    b = ex(END - timedelta(seconds=10), side="sell", oid="b")
    a = ex(END + timedelta(seconds=5), side="sell", oid="a")
    page = render(build_retro([b], [a], None, CAP, END, LIVE))
    assert "- 실제 체결은 짝의 백테스트 시각 기준으로 셈 · 기간 밖 시각의 실제 체결 1건 포함" in page


def test_fills_until_source_named():
    """체결 파일 끝을 어디서 받았는지 근거에 쓴다."""
    t = END - timedelta(days=1)
    page = render(build_retro([ex(t)], [ex(t)], None, CAP, END, LIVE, fills_until=END, fills_until_source="blackclip fetch"))
    assert "체결 파일이 10/14 23:59까지 담겼다고 blackclip fetch 기록에서 읽음" in page


def test_idle_period_with_fills_until_not_unknown():
    """fills_until을 받았는데 기간 안 거래가 모두 없으면(쉬는 기간) 확인 불가가 아니라 점검 불가다. 근거 줄과 모순하지 않는다."""
    page = render(build_retro([], [], None, CAP, END, LIVE, fills_until=END))
    assert "- 실행 · 점검 불가(기간 안 백테스트 · 실제 거래 없음)" in page.splitlines()
    assert "확인 불가(기간 안 실제 체결 없음)" not in page and "체결 파일이 10/14 23:59까지 담겼다고" in page


def test_backtest_before_fills_from_is_out_of_range():
    """받은 범위 처음(fills_from) 앞의 짝 없는 백테스트 거래는 놓친 주문이 아니라 체결 파일 범위 밖(점검 불가)이다."""
    frm = END - timedelta(days=10)
    early, late = ex(END - timedelta(days=20), "sell", oid="early"), ex(END - timedelta(days=5), "sell", oid="late")
    pair_b, pair_a = ex(END - timedelta(days=3), oid="b"), ex(END - timedelta(days=3), oid="a")
    r = build_retro([early, late, pair_b], [pair_a], None, CAP, END, LIVE, fills_from=frm, fills_until=END,
                    fills_until_source="blackclip fetch")
    assert [e.order_id for e in r.beyond_fills] == ["early"] and [e.order_id for e in r.result.backtest_only] == ["late"]
    assert r.backtest_executions == len(r.result.pairs) + len(r.result.backtest_only) + len(r.beyond_fills) + len(r.after_last_fill)
    page = render(r)
    assert "체결 파일이 10/05 00:00부터 10/14 23:59까지 담겼다고 blackclip fetch 기록에서 읽음" in page
    assert "| 09/25 00:00 | BTCUSDT | 매도 0.12 · 61000.00 | 없음 | 점검 불가 | 체결 파일 범위 밖 |" in page
    _tied_to_table(r, page)


def test_funding_only_for_backtest_symbols():
    """펀딩비는 백테스트 종목 것만 더한다(계좌 전체 펀딩비 파일에서 다른 종목 줄은 뺀다)."""
    t = END - timedelta(days=1)
    f = [Funding(t, "BTCUSDT", Decimal("-1"), "USDT"), Funding(t, "ETHUSDT", Decimal("-50"), "USDT")]
    r = build_retro([ex(t, oid="b")], [ex(t, oid="a")], f, CAP, END, LIVE)
    assert r.funding_total == Decimal("-1")


def test_funding_without_backtest_symbols_cannot_be_checked():
    """백테스트 종목이 하나도 없으면 펀딩비를 거를 종목이 없어 +0.00%p가 아니라 점검 불가다."""
    page = render(build_retro([], [], [Funding(END - timedelta(days=1), "BTCUSDT", Decimal("-1"), "USDT")], CAP, END, LIVE))
    assert "- 펀딩비 · 점검 불가(백테스트 종목 없음)" in page.splitlines()

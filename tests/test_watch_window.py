"""구간 수익: 보유 손익 · 체결 손익 · 바깥 흐름 · 단계 수익률의 복리 · 구간 처음 · 받기 틈 · 부분 체결 · 관측 종목 수수료."""
from datetime import datetime, timedelta, timezone
from decimal import Decimal as D

from blackclip.retro.fills import Execution, Funding, read_executions
from blackclip.watch.window import CAPTURED, NOT_DIVIDED, Mark, advance, equity, step, window_return

UTC = timezone.utc
T0 = datetime(2025, 1, 23, 1, 0, tzinfo=UTC)
T1 = T0 + timedelta(hours=1)
P0 = datetime(2025, 1, 19, tzinfo=UTC)


def mark(at, cash, btc, price):
    return Mark(at, D(cash), {"BTCUSDT": D(btc)}, {"BTCUSDT": D(price)})


def buy(qty, price, fee, fee_asset, at=T0 + timedelta(minutes=30)):
    return Execution(at, "BTCUSDT", "buy", D(qty), D(price), D(fee), fee_asset, "1")


def test_deposit_and_withdrawal_are_flows_not_pnl():
    """가격 그대로 입금 · 출금 · 코인 입금: 손익 0, 흐름 ±(원장 시험 1 · 2)."""
    prev = mark(T0, "4000", "0.1", "60000")
    assert step(prev, mark(T1, "5000", "0.1", "60000"), [], [], "USDT") == (D(0), D(0), D(1000))
    assert step(prev, mark(T1, "3000", "0.1", "60000"), [], [], "USDT") == (D(0), D(0), D(-1000))
    assert step(prev, mark(T1, "4000", "0.2", "60000"), [], [], "USDT") == (D(0), D(0), D(6000))


def test_price_only_move_is_pnl():
    """보유 0.1 BTC, 60000 → 66000: 보유 손익 +600, 체결 손익 0, 흐름 0(원장 시험 3)."""
    assert step(mark(T0, "4000", "0.1", "60000"), mark(T1, "4000", "0.1", "66000"), [], [], "USDT") == (D(600), D(0), D(0))


def test_one_fill_between_checks():
    """사이 체결 1건: 체결가 → 지금 가격 몫 − 수수료가 체결 손익, 흐름 0. 수수료를 거래 통화 · 받은 코인으로 낸 두 경우(원장 시험 4)."""
    prev = mark(T0, "10000", "0", "60000")
    assert step(prev, mark(T1, "3893.9", "0.1", "62000"), [buy("0.1", "61000", "6.1", "USDT")], [], "USDT") == (D(0), D("93.9"), D(0))
    assert step(prev, mark(T1, "3900", "0.0999", "62000"), [buy("0.1", "61000", "0.0001", "BTC")], [], "USDT") == (D(0), D("93.8"), D(0))
    late = buy("0.1", "61000", "6.1", "USDT", at=T1)  # 이번 확인 시각 이후 체결은 다음 단계 몫
    assert step(prev, mark(T1, "10000", "0", "62000"), [late], [], "USDT") == (D(0), D(0), D(0))


def test_fee_in_another_watched_asset_is_pnl():
    """가드레일 종목 BTCUSDT · BNBUSDT, USDT 900 + BNB 1개(100): BTC 1개를 100에 사고 수수료 BNB 0.01개, 가격 그대로 →
    체결 손익 −1 · 흐름 0(지켜보는 BNB라 이번 가격으로 뺌, 수수료는 체결 몫)."""
    px = {"BTCUSDT": D(100), "BNBUSDT": D(100)}
    prev = Mark(T0, D(900), {"BTCUSDT": D(0), "BNBUSDT": D(1)}, px)
    now = Mark(T1, D(800), {"BTCUSDT": D(1), "BNBUSDT": D("0.99")}, px)
    fill = Execution(T0 + timedelta(minutes=30), "BTCUSDT", "buy", D(1), D(100), D("0.01"), "BNB", "1")
    assert step(prev, now, [fill], [], "USDT") == (D(0), D(-1), D(0))


def test_partial_fills_across_checks_count_each_at_its_own_time(tmp_path):
    """한 주문이 00:30 1개@100 · 01:30 1개@150으로 체결, 가격 01:00 100 · 02:00 50, 기준 1000: 줄마다 읽으면 손익 −150 ·
    흐름 0, 첫 단계 0 + 둘째 단계 보유 −50 / 1000 + 체결 −100 / 1000 = −15%. 주문으로 묶으면(첫 체결 시각 하나) 두 번째 체결이
    다음 단계에서 빠진다."""
    p = tmp_path / "fills.csv"
    p.write_text("time,symbol,side,qty,price,fee,fee_asset,order_id,trade_id\n"
                 "2025-01-23T00:30:00+00:00,BTCUSDT,buy,1,100,0,USDT,7,1\n"
                 "2025-01-23T01:30:00+00:00,BTCUSDT,buy,1,150,0,USDT,7,2\n", encoding="utf-8")
    assert len(read_executions(p)) == 1 and len(read_executions(p, per_fill=True)) == 2
    fills = read_executions(p, per_fill=True)
    t = datetime(2025, 1, 23, tzinfo=UTC)
    win, _ = advance(None, P0, mark(t, "1000", "0", "100"), None, "USDT")
    win, _ = advance(win, P0, mark(t + timedelta(hours=1), "900", "1", "100"), (fills, []), "USDT")
    win, _ = advance(win, P0, mark(t + timedelta(hours=2), "750", "2", "50"), (fills, []), "USDT")
    assert (D(win["pnl"]), D(win["flows"]), window_return(win), win["low"]) == (D(-150), D(0), -0.15, -0.15)


def test_funding_is_pnl_not_flow():
    """선물 펀딩비(지갑에 들어옴)는 보유 손익, 흐름 아님."""
    f = Funding(T0 + timedelta(minutes=10), "BTCUSDT", D("-3"), "USDT")
    assert step(mark(T0, "1000", "0.1", "60000"), mark(T1, "997", "0.1", "60000"), [], [f], "USDT") == (D(-3), D(0), D(0))


def test_window_return_compounds_step_returns():
    """기준 10000 → 출금 2000 + 가격 +600: 600 / 10000 = +6%, 다음 단계 지난 자산 8600에서 가격 −860: −860 / 8600 = −10%,
    1.06 × 0.9 − 1 = −4.6%(최저도 −4.6%, 합이면 −4%). 단계마다 지난 확인 자산으로 나눠 곱한다(복리)."""
    win, note = advance(None, P0, mark(T0, "4000", "0.1", "60000"), None, "USDT")
    assert note == CAPTURED and win["base"] == "10000.0" and win["growth"] == "1" and window_return(win) == 0.0
    win, note = advance(win, P0, mark(T1, "2000", "0.1", "66000"), ([], []), "USDT")
    assert note is None and window_return(win) == 0.06 and win["low"] == 0.0 and D(win["flows"]) == -2000
    win, _ = advance(win, P0, mark(T1 + timedelta(hours=1), "2000", "0.1", "57400"), ([], []), "USDT")
    assert window_return(win) == -0.046 and win["low"] == -0.046
    assert (D(win["pnl"]), D(win["flows"])) == (D(-260), D(-2000))


def test_two_steps_of_minus_ten_percent_compound_to_minus_nineteen():
    """한 단계 −10%씩 두 번: 0.9 × 0.9 − 1 = −19%(합이면 −20%)."""
    win, _ = advance(None, P0, mark(T0, "0", "1", "10000"), None, "USDT")
    win, _ = advance(win, P0, mark(T1, "0", "1", "9000"), ([], []), "USDT")
    win, _ = advance(win, P0, mark(T1 + timedelta(hours=1), "0", "1", "8100"), ([], []), "USDT")  # −900 / 9000 = −10%
    assert window_return(win) == -0.19 and win["low"] == -0.19


def test_withdrawal_after_loss_is_not_a_loss():
    """가격 −10% 뒤 보유의 0.9를 가격 그대로 출금: 구간 수익 −10% 그대로, 최저 −10%, 흐름 −8100(옛 식은 −1000 / 1900 = −52.6%)."""
    win, _ = advance(None, P0, mark(T0, "0", "1", "10000"), None, "USDT")
    win, _ = advance(win, P0, mark(T1, "0", "1", "9000"), ([], []), "USDT")
    assert window_return(win) == -0.1 and win["low"] == -0.1
    win, _ = advance(win, P0, mark(T1 + timedelta(hours=1), "0", "0.1", "9000"), ([], []), "USDT")
    assert window_return(win) == -0.1 and win["low"] == -0.1
    assert (D(win["pnl"]), D(win["flows"])) == (D(-1000), D(-8100))


def test_full_withdrawal_after_loss_is_unconfirmed_not_alarm():
    """같은 손실 뒤 전액 출금: 지금 자산 0이라 구간 수익은 없음(확인 불가), 최저는 −10% 그대로."""
    win, _ = advance(None, P0, mark(T0, "0", "1", "10000"), None, "USDT")
    win, _ = advance(win, P0, mark(T1, "0", "1", "9000"), ([], []), "USDT")
    win, _ = advance(win, P0, mark(T1 + timedelta(hours=1), "0", "0", "9000"), ([], []), "USDT")
    assert window_return(win) is None and win["low"] == -0.1 and D(win["flows"]) == -9000


def test_deposit_in_the_same_step_does_not_hide_a_loss():
    """가격 −15%와 입금 10000이 한 단계: 보유 손익 −1500 / 지난 자산 10000 = −15%(옛 식은 −7.5%), 흐름 +10000."""
    win, _ = advance(None, P0, mark(T0, "0", "1", "10000"), None, "USDT")
    win, _ = advance(win, P0, mark(T1, "10000", "1", "8500"), ([], []), "USDT")
    assert window_return(win) == -0.15 and win["low"] == -0.15
    assert (D(win["pnl"]), D(win["flows"])) == (D(-1500), D(10000))


def test_deposit_then_trade_in_the_same_step_divides_by_the_deposit_too():
    """자산 1000 + 입금 9000으로 1개를 10000에 사고 가격 9500: 체결 손익 −500 / (1000 + 9000) = −5%(지난 자산만으로 나누면 −50%)."""
    fill = buy("1", "10000", "0", "USDT")
    win, _ = advance(None, P0, mark(T0, "1000", "0", "10000"), None, "USDT")
    win, _ = advance(win, P0, mark(T1, "0", "1", "9500"), ([fill], []), "USDT")
    assert round(window_return(win), 4) == -0.05 and round(win["low"], 4) == -0.05
    assert (D(win["pnl"]), D(win["flows"])) == (D(-500), D(9000))


def test_dust_account_deposit_then_trade_is_not_a_huge_loss():
    """0.01 USDT + 입금 10000으로 1개를 10000에 사고 가격 9900: −100 / (0.01 + 10000) = 약 −1%(지난 자산만으로 나누면 −1,000,000%)."""
    fill = buy("1", "10000", "0", "USDT")
    win, _ = advance(None, P0, mark(T0, "0.01", "0", "10000"), None, "USDT")
    win, _ = advance(win, P0, mark(T1, "0.01", "1", "9900"), ([fill], []), "USDT")
    assert round(window_return(win), 4) == -0.01 and round(win["low"], 4) == -0.01
    assert D(win["flows"]) == 10000


def test_withdrawal_and_trade_in_the_same_step_divides_by_previous_equity():
    """자산 10000(현금 8000 + 0.2개 × 10000)에서 3000 출금하고 0.5개를 10000에 사고 가격 9000: 보유 −200 / 10000 = −2%,
    체결 −500 / 10000 = −5%(흐름이 음수면 지난 자산으로, 합쳐 −7%). 지난 자산 + 흐름(7000)으로 나누면 −9.1%."""
    fill = Execution(T0 + timedelta(minutes=30), "BTCUSDT", "buy", D("0.5"), D(10000), D(0), "USDT", "1")
    win, _ = advance(None, P0, mark(T0, "8000", "0.2", "10000"), None, "USDT")
    win, _ = advance(win, P0, mark(T1, "0", "0.7", "9000"), ([fill], []), "USDT")
    assert window_return(win) == -0.07 and win["low"] == -0.07
    assert (D(win["pnl"]), D(win["flows"])) == (D(-700), D(-3000))


def test_loss_past_minus_one_stops_at_minus_one_and_does_not_revive():
    """숏 1개(현금 20000, 진입 10000, 자산 10000)에서 가격 25000 + 입금 10000: 보유 −15000 / 10000 = −150%라 −100%에서 멈춤(복리 바닥),
    가격이 10000으로 돌아와 +300%여도 되살아나지 않음(check.band._compound와 같음)."""
    win, _ = advance(None, P0, mark(T0, "20000", "-1", "10000"), None, "USDT")
    win, _ = advance(win, P0, mark(T1, "30000", "-1", "25000"), ([], []), "USDT")  # 자산 5000 > 0이라 값이 있음
    assert window_return(win) == -1.0 and win["low"] == -1.0
    win, _ = advance(win, P0, mark(T1 + timedelta(hours=1), "30000", "-1", "10000"), ([], []), "USDT")
    assert window_return(win) == -1.0


def test_new_period_first_check_recaptures_base():
    """구간이 바뀐 첫 확인: 지난 구간 손익 · 수익 · 최저를 버리고 지금 자산을 기준으로(growth 1)."""
    win, _ = advance(None, P0, mark(T0, "4000", "0.1", "60000"), None, "USDT")
    win, _ = advance(win, P0, mark(T1, "4000", "0.1", "54000"), ([], []), "USDT")
    assert window_return(win) == -0.06 and win["low"] == -0.06  # −600 / 10000
    nxt, note = advance(win, P0 + timedelta(days=30), mark(T1 + timedelta(hours=1), "4000", "0.1", "50000"), ([], []), "USDT")
    assert note == CAPTURED and nxt["base"] == "9000.0" and nxt["low"] == 0.0 and nxt["pnl"] == "0"
    assert nxt["growth"] == "1" and nxt["flows"] == "0" and window_return(nxt) == 0.0


def test_empty_account_has_no_return():
    """잔고 0인 계좌: 지금 자산 0 → 구간 수익 없음(확인 불가), 입금이 들어오면 그 단계는 0을 더하고(지난 자산 0) 그 뒤로 셈."""
    win, _ = advance(None, P0, mark(T0, "0", "0", "60000"), None, "USDT")
    assert window_return(win) is None and equity(Mark.from_json(win["mark"])) == 0
    win, _ = advance(win, P0, mark(T1, "1000", "0", "60000"), ([], []), "USDT")
    assert window_return(win) == 0.0 and win["flows"] == "1000"


def test_fetch_gap_does_not_advance_and_next_step_spans_both_hours():
    """받기 틈: 나누지 않고 그대로(경고 안 함) → 다음 확인이 두 시간을 한 단계로 나눠도 한 번에 나눈 것과 같음."""
    win, _ = advance(None, P0, mark(T0, "10000", "0", "60000"), None, "USDT")
    fill = [buy("0.1", "61000", "6.1", "USDT")]
    same, note = advance(win, P0, mark(T1, "3893.9", "0.1", "62000"), None, "USDT")
    assert note == NOT_DIVIDED and same == win
    later = mark(T1 + timedelta(hours=1), "3893.9", "0.1", "63000")
    a, _ = advance(win, P0, later, (fill, []), "USDT")
    assert D(a["pnl"]) == D("193.9") and D(a["flows"]) == 0
    assert D(a["growth"]) == D("1.01939") and window_return(a) == 0.01939  # 체결 손익 193.9 / (10000 + 0)

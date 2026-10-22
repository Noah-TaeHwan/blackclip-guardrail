"""포지션 대조 · 유예 · 봉 끝 · BT 목표 캐시 · 양쪽 1% 문턱 · 있음/없음이 바뀐 시각."""
from datetime import datetime, timedelta, timezone
from decimal import Decimal as D

from conftest import KlineClient, make_prices
from test_daily_run import LIVE, setup

from blackclip.daily.run import _guards
from blackclip.retro.fills import Execution
from blackclip.watch.position import bar_end, bt_target, compare, flip_times, grace_s, held
from blackclip.watch.window import Mark

UTC = timezone.utc
SELL = datetime(2025, 1, 23, tzinfo=UTC)  # 예시 신호(10 > 30일선)가 01/22 봉에서 꺼짐 → next_open 01/23 0시 매도


def test_bar_end_and_grace():
    """봉 끝은 UTC로 나뉜 직전 경계(경계 그 순간이면 그 봉이 막 닫힘), 유예는 max(1시간, 봉/4)."""
    assert bar_end(SELL, 86400) == SELL
    assert bar_end(SELL - timedelta(seconds=1), 86400) == SELL - timedelta(days=1)
    assert bar_end(datetime(2025, 1, 23, 5, 30, tzinfo=UTC), 14400) == datetime(2025, 1, 23, 4, tzinfo=UTC)
    assert [grace_s(b) for b in (3600, 14400, 43200, 86400)] == [3600, 3600, 10800, 21600]


def test_held_counts_only_one_percent_or_more():
    """자산 10000에서 BTC 평가 96(1% 미만)은 없음, 120은 보유, 선물 음수는 -1."""
    m = lambda q: Mark(SELL, D("10000") - D(q) * 60000, {"BTCUSDT": D(q)}, {"BTCUSDT": D(60000)})
    assert held(m("0.0016")) == {"BTCUSDT": 0} and held(m("0.002")) == {"BTCUSDT": 1} and held(m("-0.002")) == {"BTCUSDT": -1}
    assert held(m("0")) == {"BTCUSDT": 0}


def test_compare_spot_grace_boundary():
    """BT 보유 · 실제 없음: 바뀐 지 6시간(일봉 유예) 전까지는 정상(따라오는 중), 6시간째부터 경고, 바뀐 시각이 없으면 바로 경고."""
    w, ch = {"BTCUSDT": 1.0}, {"BTCUSDT": SELL}
    assert compare(w, ch, {"BTCUSDT": 0}, SELL + timedelta(hours=5, minutes=59), 86400, False) == (
        "정상", "포지션 따라오는 중: BTCUSDT BT 보유 · 실제 없음(목표 포지션이 01/23 00:00 UTC에 바뀜 · 유예 6시간)")
    assert compare(w, ch, {"BTCUSDT": 0}, SELL + timedelta(hours=6), 86400, False) == ("경고", "포지션 다름: BTCUSDT BT 보유 · 실제 없음")
    assert compare(w, {"BTCUSDT": None}, {"BTCUSDT": 0}, SELL, 86400, False)[0] == "경고"
    assert compare({"BTCUSDT": 0.0}, {"BTCUSDT": None}, {"BTCUSDT": 0}, SELL, 86400, False) == ("정상", "포지션 맞음: BTCUSDT 없음")


def test_bt_side_uses_the_same_one_percent_rule():
    """BT 목표 0.5% · 실제 0.5%(held = 없음): 둘 다 없음이라 맞음(유예가 끝났어도 경고 아님)."""
    have = held(Mark(SELL, D("9950"), {"BTCUSDT": D("0.001")}, {"BTCUSDT": D(50000)}))
    assert have == {"BTCUSDT": 0}
    assert compare({"BTCUSDT": 0.005}, {"BTCUSDT": None}, have, SELL, 86400, False) == ("정상", "포지션 맞음: BTCUSDT 없음")


def test_grace_counts_from_the_flip_not_from_size_changes():
    """시간봉 next_open 목표 20% → 30% → 40%(22:00 · 23:00 · 00:00 체결), 실제는 계속 없음: 유예는 처음 보유가 된 22:00부터라
    00:05에는 2시간이 지나 경고. 크기만 바뀐 23:00 · 00:00 체결로 유예가 다시 시작되지 않는다."""
    t0 = datetime(2025, 1, 22, 22, tzinfo=UTC)
    ex = [Execution(t0 + timedelta(hours=i), "BTCUSDT", "buy", D(q), D(100), D(0), "", "") for i, q in enumerate(("0.2", "0.1", "0.1"))]
    ch = flip_times(ex, {"BTCUSDT": 0.4}, False)
    assert ch == {"BTCUSDT": t0}
    assert compare({"BTCUSDT": 0.4}, ch, {"BTCUSDT": 0}, t0 + timedelta(hours=2, minutes=5), 3600, False) == (
        "경고", "포지션 다름: BTCUSDT BT 보유 · 실제 없음")
    assert flip_times(ex[1:], {"BTCUSDT": 0.4}, False) == {"BTCUSDT": None}  # 실전 구간 안에서 있음/없음이 안 바뀜
    short = [Execution(t0, "BTCUSDT", "sell", D("0.6"), D(100), D(0), "", "")]  # 롱 0.3 → 숏 0.3
    assert flip_times(short, {"BTCUSDT": -0.3}, False) == {"BTCUSDT": t0} and flip_times(short, {"BTCUSDT": -0.3}, True) == {"BTCUSDT": None}


def test_compare_futures_presence_only():
    """선물은 있음/없음만: 목표 숏 · 실제 롱도 「있음」끼리라 정상, 목표 있음 · 실제 없음은 경고, 꼬리에 방향 미검증."""
    assert compare({"BTCUSDT": -1.0}, {"BTCUSDT": None}, {"BTCUSDT": 1}, SELL, 3600, True) == (
        "정상", "포지션 맞음: BTCUSDT 있음 · 선물은 있음/없음만 봄(방향은 미검증)")
    assert compare({"BTCUSDT": 1.0}, {"BTCUSDT": None}, {"BTCUSDT": 0}, SELL, 3600, True) == (
        "경고", "포지션 다름: BTCUSDT BT 있음 · 실제 없음 · 선물은 있음/없음만 봄(방향은 미검증)")


def test_bt_target_runs_only_when_a_new_bar_closed(tmp_path, monkeypatch, capsys):
    """01/23 00:30: 목표 0 · 바뀐 시각 01/23 0시. 같은 봉 안(05:00)은 캐시만, 01/24 0시가 지나면 다시 돌림."""
    root, _ = setup(tmp_path, monkeypatch, capsys)
    g = _guards(root)[0]["s1"]
    kc = KlineClient({"BTCUSDT": make_prices()})
    w, ch, cache = bt_target(root, "s1", g, LIVE, lambda m: kc, SELL + timedelta(minutes=30), None)
    assert w == {"BTCUSDT": 0.0} and ch == {"BTCUSDT": SELL} and cache["bar_end"] == SELL.isoformat()
    n = len(kc.calls)
    assert bt_target(root, "s1", g, LIVE, lambda m: kc, SELL + timedelta(hours=5), cache)[:2] == (w, ch) and len(kc.calls) == n
    w2, _, cache2 = bt_target(root, "s1", g, LIVE, lambda m: kc, SELL + timedelta(days=1, seconds=5), cache)
    assert len(kc.calls) > n and cache2["bar_end"] == (SELL + timedelta(days=1)).isoformat() and w2 == {"BTCUSDT": 0.0}
    w3, ch3, _ = bt_target(root, "s1", g, LIVE, lambda m: kc, SELL - timedelta(hours=1), None)  # 01/22 23시: 아직 보유 목표
    assert w3 == {"BTCUSDT": 1.0} and ch3 == {"BTCUSDT": datetime(2024, 12, 23, tzinfo=UTC)}

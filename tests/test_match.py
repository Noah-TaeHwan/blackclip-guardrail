"""짝짓기: 시간대 · 한 번만 세기 · 가까운 쪽 · 매칭 창 · 창 문자열."""
from datetime import datetime, timedelta, timezone
from decimal import Decimal

import pytest

from blackclip.retro.fills import Execution
from blackclip.retro.match import match, parse_window

UTC = timezone.utc
KST = timezone(timedelta(hours=9))


def ex(t, side="buy", symbol="BTCUSDT", price="61000", oid="x"):
    """테스트용 체결 하나를 만든다."""
    return Execution(t, symbol, side, Decimal("0.12"), Decimal(price), Decimal("1"), "USDT", oid)


def test_match_across_timezones():
    """백테스트는 KST, 실제는 UTC여도 같은 순간이면 짝이 된다."""
    b = ex(datetime(2026, 10, 8, 9, 31, tzinfo=KST))
    a = ex(datetime(2026, 10, 8, 0, 33, tzinfo=UTC), price="61054")
    r = match([b], [a])
    assert len(r.pairs) == 1 and not r.backtest_only and not r.actual_only


def test_backtest_only_counted_once():
    """실제 체결이 없는 백테스트 체결은 한 번만 남는다."""
    b = ex(datetime(2026, 10, 13, 15, 58, tzinfo=UTC), side="sell")
    r = match([b], [])
    assert r.backtest_only == [b] and r.pairs == []


def test_actual_used_once_nearest_wins():
    """실제 체결 하나는 가장 가까운 백테스트 체결 하나에만 쓰인다."""
    t = datetime(2026, 10, 8, 0, 0, tzinfo=UTC)
    b1, b2 = ex(t, oid="b1"), ex(t + timedelta(minutes=30), oid="b2")
    a = ex(t + timedelta(minutes=25), oid="a")
    r = match([b1, b2], [a])
    assert [p.backtest.order_id for p in r.pairs] == ["b2"]
    assert [e.order_id for e in r.backtest_only] == ["b1"]


def test_outside_window_and_other_side_not_matched():
    """창 밖이거나 방향 · 종목이 다르면 짝이 아니다."""
    t = datetime(2026, 10, 8, 0, 0, tzinfo=UTC)
    r = match([ex(t)], [ex(t + timedelta(hours=3)), ex(t, side="sell"), ex(t, symbol="ETHUSDT")])
    assert not r.pairs and len(r.actual_only) == 3


def test_most_pairs_first():
    """가까운 쪽부터 욕심내다 짝을 덜 맺지 않는다: 가능한 짝이 둘이면 둘 다 맺는다."""
    t = datetime(2026, 10, 8, 0, 0, tzinfo=UTC)
    bts = [ex(t, oid="b1"), ex(t + timedelta(minutes=50), oid="b2")]
    acts = [ex(t - timedelta(minutes=110), oid="a1"), ex(t + timedelta(minutes=40), oid="a2")]
    r = match(bts, acts)
    assert [(p.backtest.order_id, p.actual.order_id) for p in r.pairs] == [("b1", "a1"), ("b2", "a2")]
    assert not r.backtest_only and not r.actual_only


@pytest.mark.parametrize("text, minutes", [("90m", 90), ("2h", 120), ("1d", 1440)])
def test_parse_window(text, minutes):
    """매칭 창 문자열을 읽는다."""
    assert parse_window(text) == timedelta(minutes=minutes)


def test_parse_window_rejects_bad():
    """모르는 단위는 오류다."""
    with pytest.raises(ValueError):
        parse_window("2w")


def test_greedy_would_lose_a_pair():
    """가까운 쪽부터 잡으면 한 짝을 놓치는 데이터에서도 두 짝을 맺는다."""
    t = datetime(2026, 10, 8, 0, 0, tzinfo=UTC)
    bts = [ex(t, oid="b1"), ex(t + timedelta(minutes=10), oid="b2")]
    acts = [ex(t + timedelta(minutes=11), oid="a1"), ex(t + timedelta(minutes=20), oid="a2")]
    r = match(bts, acts, window=timedelta(minutes=15))
    assert [(p.backtest.order_id, p.actual.order_id) for p in r.pairs] == [("b1", "a1"), ("b2", "a2")]


def test_gap_equal_to_window_pairs():
    """간격이 창과 정확히 같으면 짝이 된다(경계 포함)."""
    t = datetime(2026, 10, 8, 0, 0, tzinfo=UTC)
    r = match([ex(t)], [ex(t + timedelta(hours=2))], window=timedelta(hours=2))
    assert len(r.pairs) == 1


def test_pairs_ordered_by_symbol_on_same_time():
    """백테스트 시각이 같은 여러 종목의 짝은 종목 순으로 나온다."""
    t = datetime(2026, 10, 8, 0, 0, tzinfo=UTC)
    syms = ["SOLUSDT", "BTCUSDT", "ETHUSDT"]
    r = match([ex(t, symbol=s) for s in syms], [ex(t + timedelta(minutes=1), symbol=s) for s in syms])
    assert [p.backtest.symbol for p in r.pairs] == ["BTCUSDT", "ETHUSDT", "SOLUSDT"]


def test_same_time_actual_order_does_not_change_pairs():
    """같은 시각의 실제 체결 두 건은 CSV 행 순서가 바뀌어도 같은 짝을 맺는다."""
    t = datetime(2026, 10, 8, 0, 0, tzinfo=UTC)
    bts = [ex(t, oid="b1"), ex(t, oid="b2")]
    a1, a2 = ex(t, price="61000", oid="a1"), ex(t, price="61100", oid="a2")

    def pairs_of(acts):
        """짝을 (백테스트 번호, 실제 가격) 목록으로 바꾼다."""
        return [(p.backtest.order_id, str(p.actual.price)) for p in match(bts, acts).pairs]

    assert pairs_of([a1, a2]) == pairs_of([a2, a1])


def test_parse_window_rejects_zero_and_non_ascii_digits():
    """0 창과 ASCII가 아닌 숫자는 한국어 오류로 거부한다."""
    for text in ("0m", "²h"):
        with pytest.raises(ValueError, match="매칭 창은"):
            parse_window(text)

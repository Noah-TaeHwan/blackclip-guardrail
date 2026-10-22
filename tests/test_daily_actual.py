"""실제 손익: 출발 잔고 평가 + 현금 흐름 + 평가 · 받은 코인 수수료 · 다른 자산 수수료 환산 · 펀딩비 · 왕복 거래 · 기준 자본."""
from datetime import datetime, timezone
from decimal import Decimal as D

import pandas as pd
import pytest

from blackclip.daily.actual import actual_pnl
from blackclip.retro.fills import Execution, Funding

UTC = timezone.utc
SNAP = {"quote": "USDT", "taken_at": datetime(2026, 9, 22, 12, tzinfo=UTC), "start": datetime(2026, 9, 23, tzinfo=UTC),
        "cash": D("10000"), "positions": {}}
ENDS = [datetime(2026, 9, d, tzinfo=UTC) for d in (24, 25, 26)]
END = ENDS[-1]
CLOSES = {"BTCUSDT": pd.Series([61000.0, 62000.0, 63500.0],
                               index=pd.date_range("2026-09-23", periods=3, freq="D", tz="UTC"))}


def ex(day, hour, side, qty, price, fee, asset, oid):
    return Execution(datetime(2026, 9, day, hour, tzinfo=UTC), "BTCUSDT", side, D(qty), D(price), D(fee), asset, oid)


BUY = ex(23, 1, "buy", "0.1", "60000", "0.0001", "BTC", "1")
SELL = ex(25, 2, "sell", "0.0999", "63000", "6.2937", "USDT", "2")


def test_base_fee_reduces_position_and_closes_trade():
    """BTC로 낸 수수료만큼 포지션이 줄어 0.0999를 다 팔면 거래 하나가 끝난다 · 하루 끝 손익 · 출발 자산."""
    a = actual_pnl([BUY, SELL], [], CLOSES, SNAP, ENDS, END, spot=True)
    assert a.pnl.tolist() == pytest.approx([93.9, 193.8, 287.4063])
    assert a.exits == [SELL.time] and a.problem is None and a.capital == D("10000")


def test_funding_added_after_its_time():
    """선물 펀딩비(USDT)는 그 시각 뒤 하루 끝부터 더한다, 다른 자산은 안 더함."""
    f = [Funding(datetime(2026, 9, 24, 8, tzinfo=UTC), "BTCUSDT", D("-1.5"), "USDT"),
         Funding(datetime(2026, 9, 24, 16, tzinfo=UTC), "BTCUSDT", D("-9"), "BNB")]
    a = actual_pnl([BUY, SELL], f, CLOSES, SNAP, ENDS, END, spot=False)
    assert a.pnl.tolist() == pytest.approx([93.9, 192.3, 285.9063])


def test_spot_sell_more_than_held_is_incomplete():
    """출발 잔고 0인데 팔면 점검 불가 사유, 선물은 숏이라 괜찮음."""
    a = actual_pnl([SELL], [], CLOSES, SNAP, ENDS, END, spot=True)
    assert a.problem == "기록 불완전: BTCUSDT를 가진 양보다 많이 팖(출발 잔고 + 받은 체결 기준 · 빠진 체결로 보임)"
    assert actual_pnl([SELL], [], CLOSES, SNAP, ENDS, END, spot=False).problem is None


def test_flip_counts_exit_and_fills_before_snapshot_ignored():
    """롱 → 숏 전환은 청산 하나, 잔고를 받기 전 체결은 안 봄(잔고에 이미 들어 있음)."""
    before = ex(22, 1, "buy", "5", "1", "0", "USDT", "0")
    long_, flip = ex(23, 1, "buy", "1", "60000", "0", "USDT", "1"), ex(24, 1, "sell", "2", "61000", "0", "USDT", "2")
    a = actual_pnl([before, long_, flip], [], CLOSES, SNAP, ENDS, END, spot=False)
    assert a.exits == [flip.time]
    assert a.pnl.tolist() == pytest.approx([1000.0, 0.0, -1500.0])


def test_opening_position_valued_at_start_day_close():
    """출발 BTC 0.1을 실전 시작일(9/23) UTC 종가 61000으로 평가해 출발 자산에 넣고, 판 뒤 손익은 그 값에서 셈."""
    snap = {**SNAP, "taken_at": datetime(2026, 9, 23, 12, tzinfo=UTC), "start": datetime(2026, 9, 24, tzinfo=UTC),
            "positions": {"BTCUSDT": D("0.1")}}
    sell = ex(25, 2, "sell", "0.1", "63000", "0", "USDT", "2")
    a = actual_pnl([sell], [], CLOSES, snap, ENDS, END, spot=True)
    assert a.capital == D("16100") and a.problem is None
    assert a.pnl.tolist() == pytest.approx([0.0, 100.0, 200.0]) and a.exits == [sell.time]


def test_capital_override_is_the_denominator():
    """같은 체결이라도 기준 자본을 주면 분모가 그 값: 계좌에 전략 밖 현금이 있어도 % 가 전략 몫 기준. 손익 · 출발 포지션 평가는 그대로."""
    wide = {**SNAP, "cash": D("100000")}  # 전략 밖 돈이 섞인 계좌 잔고 전체
    a = actual_pnl([BUY, SELL], [], CLOSES, wide, ENDS, END, spot=True)
    b = actual_pnl([BUY, SELL], [], CLOSES, {**wide, "capital": D("10000")}, ENDS, END, spot=True)
    assert a.capital == D("100000") and b.capital == D("10000")
    assert a.pnl.tolist() == b.pnl.tolist() == pytest.approx([93.9, 193.8, 287.4063])
    assert a.pnl.iloc[-1] / float(a.capital) == pytest.approx(0.002874063)
    assert b.pnl.iloc[-1] / float(b.capital) == pytest.approx(0.02874063)
    snap = {**SNAP, "taken_at": datetime(2026, 9, 23, 12, tzinfo=UTC), "start": datetime(2026, 9, 24, tzinfo=UTC),
            "positions": {"BTCUSDT": D("0.1")}, "capital": D("5000")}  # 출발 BTC 평가액 6100을 분모에 넣지 않고 5000을 씀
    c = actual_pnl([ex(25, 2, "sell", "0.1", "63000", "0", "USDT", "2")], [], CLOSES, snap, ENDS, END, spot=True)
    assert c.capital == D("5000") and c.problem is None and c.pnl.tolist() == pytest.approx([0.0, 100.0, 200.0])


def test_fill_after_end_not_counted():
    """기간 끝(1/2 0시) 뒤 매도는 어제 손익 · 거래 수 어디에도 안 들어감."""
    a = actual_pnl([BUY, SELL], [], CLOSES, SNAP, ENDS[:2], ENDS[1], spot=True)
    assert a.exits == [] and a.pnl.tolist() == pytest.approx([93.9, 193.8])


def test_oversell_within_dust_share_is_incomplete():
    """1개 사고 1.005개 팔면 부스러기 허용(1%) 안이어도 현물 초과 매도."""
    buy, sell = ex(23, 1, "buy", "1", "100", "0", "USDT", "1"), ex(24, 1, "sell", "1.005", "100", "0", "USDT", "2")
    assert actual_pnl([buy, sell], [], CLOSES, SNAP, ENDS, END, spot=True).problem is not None


def test_other_asset_fee_converted_or_incomplete():
    """BNB 수수료 1개는 BNBUSDT 종가 600으로 환산해 손익에서 뺌, 종가가 없으면 점검 불가."""
    buy = ex(23, 1, "buy", "1", "100", "1", "BNB", "1")
    closes = {"BTCUSDT": pd.Series([100.0, 100.0, 100.0], index=CLOSES["BTCUSDT"].index),
              "BNBUSDT": pd.Series([600.0], index=pd.DatetimeIndex([pd.Timestamp("2026-09-22", tz="UTC")]))}
    a = actual_pnl([buy], [], closes, SNAP, ENDS, END, spot=True)
    assert a.problem is None and a.pnl.tolist() == pytest.approx([-600.0] * 3)
    b = actual_pnl([buy], [], {"BTCUSDT": closes["BTCUSDT"]}, SNAP, ENDS, END, spot=True)
    assert b.problem == "기록 불완전: BNB 수수료를 USDT로 환산할 종가 없음"


def test_trips_match_backtest_definition():
    """백테스트 round_trips와 같은 정의 — 방향 × (닫은 값/연 값 − 1) − 왕복 수수료율, 크기 변화 무시."""
    fills = [ex(23, 1, "buy", "0.1", "60000", "6", "USDT", "1"),       # 롱 열기 · 수수료율 0.001
             ex(23, 5, "buy", "0.1", "62000", "0", "USDT", "2"),       # 분할 진입(연 값 그대로)
             ex(23, 9, "sell", "0.1", "63000", "0", "USDT", "3"),      # 분할 청산(아직 안 닫힘)
             ex(23, 13, "sell", "0.1", "63000", "12.6", "USDT", "4"),  # 닫기 · 수수료율 0.002 → 0.05 − 0.003
             ex(24, 1, "sell", "0.1", "63000", "0", "USDT", "5"),      # 숏 열기
             ex(24, 5, "buy", "0.15", "60000", "0", "USDT", "6"),      # 반전: 숏 닫고 롱 0.05 열기
             ex(24, 9, "sell", "0.0499", "61200", "0", "USDT", "7")]   # 잔여 0.0001 ≤ 1% → 닫힘
    a = actual_pnl(fills, [], CLOSES, SNAP, ENDS, END, spot=False)
    assert [t for t, _ in a.trips] == [fills[3].time, fills[5].time, fills[6].time]
    assert [r for _, r in a.trips] == pytest.approx([0.047, 1 - 60000 / 63000, 0.02])


def test_trips_count_equals_day_trades():
    """E의 거래 수 = Day.trades와 같은 청산 목록. 실전 전 포지션의 청산은 exits에만(연 값이 없음)."""
    fills = [ex(23, 1, "buy", "0.1", "60000", "0", "USDT", "1"), ex(23, 5, "sell", "0.1", "61000", "0", "USDT", "2")]
    a = actual_pnl(fills, [], CLOSES, SNAP, ENDS, END, spot=True)
    assert len(a.trips) == len(a.exits) == 1
    b = actual_pnl([ex(23, 1, "sell", "0.1", "61000", "0", "USDT", "9")], [], CLOSES,
                   {**SNAP, "positions": {"BTCUSDT": D("0.1")}}, ENDS, END, spot=True)
    assert len(b.exits) == 1 and b.trips == []


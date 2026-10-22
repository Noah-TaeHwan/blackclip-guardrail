"""같은 기간 백테스트: 바이낸스 봉 받기 · 실전 구간 자르기 · 두 비용."""
from datetime import datetime, timezone

import pandas as pd
from conftest import KlineClient, make_prices

from blackclip.daily.live import binance_frames, live_run

START = datetime(2026, 8, 1, tzinfo=timezone.utc)
LIVE = datetime(2026, 9, 1, tzinfo=timezone.utc)
END = datetime(2026, 9, 21, tzinfo=timezone.utc)


def test_binance_frames_until_end_and_absent():
    """[start, end) 봉만, 없는 종목은 따로."""
    p = make_prices(60, start="2026-08-01")
    got, absent = binance_frames(KlineClient({"BTCUSDT": p}), ["BTCUSDT", "ZZZUSDT"], 86400, START, END)
    assert absent == ["ZZZUSDT"]
    assert got["BTCUSDT"].index[0] == pd.Timestamp("2026-08-01", tz="UTC")
    assert got["BTCUSDT"].index[-1] == pd.Timestamp("2026-09-20", tz="UTC")


def test_live_run_cuts_and_two_costs():
    """늘 들고 있는 신호: 실전 구간 체결 없음, 비용 0 수익 = 종가 변화, 다시 맞출 것 없어 두 비용이 같음."""
    p = make_prices(60, start="2026-08-01")
    frames = {"BTCUSDT": p}
    run = live_run(lambda f: {s: pd.Series(1.0, index=df.index) for s, df in f.items()}, frames, "close", 86400,
                   LIVE, END)
    c = p["Close"]
    want = (c / c.shift(1) - 1).fillna(0.0)
    want = want[(want.index >= pd.Timestamp(LIVE)) & (want.index < pd.Timestamp(END))]
    assert len(run.zero) == 20 and run.zero.index[0] == pd.Timestamp(LIVE)
    pd.testing.assert_series_equal(run.zero, want, check_names=False, check_freq=False)  # 시각을 밀면 freq 표지만 사라짐
    pd.testing.assert_series_equal(run.cons, run.zero)
    assert run.executions == []


def test_live_run_keeps_live_executions_only():
    """실전 시작 뒤에 신호가 바뀌면 그 체결만 남음(비중 변화 0.5)."""
    p = make_prices(60, start="2026-08-01")
    w = pd.Series(0.0, index=p.index)
    w[p.index >= pd.Timestamp("2026-09-10", tz="UTC")] = 0.5
    run = live_run(lambda f: {"BTCUSDT": w}, {"BTCUSDT": p}, "close", 86400, LIVE, END)
    assert [(e.time.date().isoformat(), e.side, str(e.qty)) for e in run.executions] == [("2026-09-11", "buy", "0.5")]
    assert run.cons.sum() < run.zero.sum()


def test_last_bar_valued_at_close_like_actual():
    """다음 시가 체결 · 마지막 봉 시가 110 · 종가 121이면 같은 기간 BT도 종가까지 +21%."""
    idx = pd.date_range("2026-01-01", periods=3, freq="D", tz="UTC")
    p = pd.DataFrame({"Open": [90.0, 100.0, 110.0], "High": [100.0, 110.0, 121.0], "Low": [90.0, 100.0, 110.0],
                      "Close": [100.0, 110.0, 121.0], "Volume": [1.0, 1.0, 1.0]}, index=idx)
    run = live_run(lambda f: {"X": pd.Series(1.0, index=idx)}, {"X": p}, "next_open", 86400,
                   datetime(2026, 1, 2, tzinfo=timezone.utc), datetime(2026, 1, 4, tzinfo=timezone.utc))
    assert len(run.zero) == 2 and abs(float((1 + run.zero).prod() - 1) - 0.21) < 1e-12


def test_close_exec_matches_buy_and_hold_from_live_start():
    """종가 체결 · 늘 들고 있음이면 실전 시작 직전 봉 종가부터 끝 직전 봉 종가까지 그대로, 끝 줄이 붙는 갈래도 지남."""
    p = make_prices(60, start="2026-08-01")
    p = p[p.index < pd.Timestamp(END)]  # 바이낸스처럼 end 앞에서 끊어 끝 줄 붙이는 갈래를 지나게
    run = live_run(lambda f: {s: pd.Series(1.0, index=df.index) for s, df in f.items()}, {"BTCUSDT": p}, "close",
                   86400, LIVE, END)
    c = p["Close"]
    hold = float(c[pd.Timestamp(END) - pd.Timedelta(days=1)] / c[pd.Timestamp(LIVE) - pd.Timedelta(days=1)] - 1)
    assert abs(float((1 + run.zero).prod() - 1) - hold) < 1e-12 and run.zero.index[0] == pd.Timestamp(LIVE)


def test_binance_frames_drops_unfinished_bar():
    """끝이 end를 넘는 진행 중 봉은 버림(3d · 1w)."""
    p = make_prices(3, start="2026-08-31", freq="7D")  # 월요일 시작 주봉 셋, 마지막(09-14) 봉은 09-21에 끝남
    end = datetime(2026, 9, 20, tzinfo=timezone.utc)
    got, absent = binance_frames(KlineClient({"BTCUSDT": p}), ["BTCUSDT"], 604800, START, end)
    assert absent == [] and list(got["BTCUSDT"].index) == list(p.index[:2])

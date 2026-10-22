"""check.trades · check.c4: 왕복 거래 세기 · 국면 편중."""
import numpy as np
import pandas as pd
import pytest

from blackclip.check.c4 import REGIMES, c4, regime_stats, regimes
from blackclip.check.trades import Trade, round_trips


def _frame(opens):
    idx = pd.date_range("2024-01-01", periods=len(opens), freq="D", tz="UTC")
    return pd.DataFrame({"Open": opens, "High": opens, "Low": opens, "Close": opens}, index=idx, dtype=float)


def _trips(weights, opens=None, exec_="next_open", cost_bp=0.0):
    opens = opens or [10.0 + i for i in range(len(weights))]
    p = {"A": _frame(opens)}
    return round_trips(p, {"A": pd.Series(weights, p["A"].index, dtype=float)}, exec_, cost_bp)


def test_round_trip_next_open_entry_exit_prices():
    """다음 봉 시가 체결: 신호 봉 1에 사고 3에 0이면 봉 2에 들어가 봉 4에 나온다."""
    t = _trips([0, 1, 1, 0, 0, 0])
    assert len(t) == 1 and t[0].bars == 2 and t[0].ret == pytest.approx(14 / 12 - 1)


def test_resize_is_same_trade_flip_is_two():
    """크기만 바뀐 것(1 → 0.5)은 한 거래, 부호가 바뀌면(0.5 → -1) 청산 + 새 진입."""
    t = _trips([0, 1, 0.5, -1, -1, 0, 0], exec_="close")
    assert [(x.bars, round(x.ret, 6)) for x in t] == [(2, round(13 / 11 - 1, 6)), (2, round(-(15 / 13 - 1), 6))]


def test_open_trade_at_end_dropped_and_cost_twice():
    """끝까지 열린 거래는 빼고, 비용은 편도 × 2."""
    t = _trips([0, 1, 0, 1, 1], exec_="close", cost_bp=30)
    assert len(t) == 1 and t[0].ret == pytest.approx(12 / 11 - 1 - 0.006)


def test_float_noise_not_a_trade():
    """부동소수 오차만큼의 비중(1e-12)은 거래가 아니다."""
    assert _trips([0, 1e-12, 1e-12, 0], exec_="close") == []


def test_no_trades_when_flat():
    """신호가 한 번도 안 나면 거래 0건."""
    assert _trips([0.0] * 10) == []


def _trend(n, drift, seed):
    rng = np.random.default_rng(seed)
    return pd.Series(100 * np.exp(np.cumsum(rng.normal(drift, 0.01, n))),
                     index=pd.date_range("2020-01-01", periods=n, freq="D", tz="UTC"))


def test_regimes_three_kinds_and_warmup():
    """오르는 구간은 상승, 내리는 구간은 하락, 앞 200 + 20일은 NaN."""
    up, down = _trend(400, 0.003, 1), _trend(400, -0.003, 2)
    assert regimes(up, 86400).iloc[-1] == "상승" and regimes(down, 86400).iloc[-1] == "하락"
    assert regimes(up, 86400).iloc[:219].isna().all()


def _stats(up, side, down, skipped=0, mean=0.001):
    s = {r: {"trades": n, "mean": mean} for r, n in (("상승", up), ("횡보", side), ("하락", down))}
    s["분류 안 됨"] = {"trades": skipped}
    return s


def test_c4_states():
    """거래 부족 → 국면 편중 → 충분(예시 4,880건 중 하락 110건 = 편중)."""
    assert c4(_stats(10, 5, 5))["state"] == "거래 부족"
    r = c4(_stats(3870, 900, 110))
    assert r["state"] == "국면 편중" and r["text"].startswith("하락 거래가 488건보다 적음")
    assert "상승 3,870(79%, 평균 +0.10%)" in r["text"] and "보수적 비용 왕복 60bp" in r["text"]
    assert c4(_stats(50, 40, 35))["state"] == "충분"


def test_c4_evenly_thin_is_too_few_not_skew():
    """세 국면이 모두 30건 밑이면(예: 25 · 13 · 23건) 편중이 아니라 거래 부족."""
    r = c4(_stats(25, 13, 23))
    assert r["state"] == "거래 부족" and r["text"].startswith("국면마다 30건이 안 됨 · 왕복 거래 61건 중 ")


def test_c4_unclassified_counts_as_too_few():
    """거래는 많아도 국면을 나눈 거래가 30건 밑이면 거래 부족(백테스트가 200일보다 짧음)."""
    r = c4(_stats(10, 5, 0, skipped=200))
    assert r["state"] == "거래 부족" and "국면을 나눈 거래 15건 < 30건" in r["text"]


def test_regime_stats_by_entry_bar():
    """국면은 진입 봉 기준, 앞부분 진입은 분류 안 됨."""
    close = _trend(300, 0.004, 4)
    p = {"A": pd.DataFrame({"Open": close, "High": close, "Low": close, "Close": close})}
    w = pd.Series(0.0, index=close.index)
    w.iloc[[10, 250]] = 1.0  # 봉 11 · 251에 진입(next_open), 다음 봉에 청산
    st = regime_stats(round_trips(p, {"A": w}, "next_open", 30), p, 86400)
    assert st["분류 안 됨"]["trades"] == 1 and st["상승"]["trades"] == 1


# --- 변이(mutant)를 죽이는 시험 ---


def _daily(values):
    return pd.Series(values, index=pd.date_range("2020-01-01", periods=len(values), freq="D", tz="UTC"), dtype=float)


def _ohlc(close):
    return pd.DataFrame({"Open": close, "High": close, "Low": close, "Close": close})


def test_regimes_sideways_when_price_and_slope_disagree():
    """가격이 내리는 200일선 위이거나 오르는 선 아래면 상승 · 하락이 아니라 횡보다."""
    above_falling = _daily([200 - 0.5 * i for i in range(300)])
    above_falling.iloc[-1] += 80
    below_rising = _daily([100 + 0.5 * i for i in range(300)])
    below_rising.iloc[-1] -= 80
    for close, price_above, slope_sign in ((above_falling, True, -1), (below_rising, False, 1)):
        ma = close.rolling(200).mean()
        slope = ma.iloc[-1] - ma.iloc[-21]
        assert (close.iloc[-1] > ma.iloc[-1]) == price_above and slope * slope_sign > 0  # 전제 확인
        assert regimes(close, 86400).iloc[-1] == "횡보"


def test_regimes_warmup_boundary_daily_and_hourly():
    """일봉은 인덱스 218까지 NaN, 219부터 국면(200 + 20 − 1). 시간봉은 4,800 + 480 − 1 = 5,279부터."""
    daily = regimes(_trend(400, 0.003, 1), 86400)
    assert pd.isna(daily.iloc[218]) and daily.iloc[219] in REGIMES
    s = _trend(6000, 0.0001, 3)
    s.index = pd.date_range("2020-01-01", periods=6000, freq="h", tz="UTC")
    hourly = regimes(s, 3600)
    assert pd.isna(hourly.iloc[5278]) and hourly.iloc[5279] in REGIMES


def test_c4_trade_count_boundaries():
    """나눈 거래 30건은 거래 부족이 아니고 29건은 부족. 세 국면이 모두 정확히 30건이면 충분."""
    r30 = c4(_stats(30, 0, 0))
    assert r30["state"] == "국면 편중" and not r30["text"].startswith("국면을 나눈 거래")
    r29 = c4(_stats(29, 0, 0))
    assert r29["state"] == "거래 부족" and r29["text"].startswith("국면을 나눈 거래 29건 < 30건")
    assert c4(_stats(30, 30, 30))["state"] == "충분"
    assert c4(_stats(30, 30, 29))["state"] == "국면 편중"


def test_c4_ten_percent_floor_boundary():
    """분류된 거래 310건이면 바닥은 31건: 31건은 충분, 30건은 편중(문구도 31건)."""
    assert c4(_stats(150, 129, 31))["state"] == "충분"
    r = c4(_stats(150, 130, 30))
    assert r["state"] == "국면 편중" and r["text"].startswith("하락 거래가 31건보다 적음")


def test_c4_floor_printed_rounded_up_and_basis_stated():
    """바닥이 30.5건이면 「31건보다 적음」(30건 국면이 「30건보다 적음」으로 읽히지 않게). 퍼센트 분모를 적는다."""
    r = c4(_stats(30, 30, 245))
    assert r["state"] == "국면 편중" and r["text"].startswith("상승 · 횡보 거래가 31건보다 적음")
    assert "분류된 305건 기준" in r["text"]
    r = c4(_stats(3870, 900, 110, skipped=120))
    assert "왕복 거래 5,000건 중 분류된 4,880건 기준 상승 3,870(79%" in r["text"]


def test_c4_floor_uses_classified_not_total():
    """10% 바닥은 분류된 거래 수 기준: 분류 310건 · 분류 안 됨 1,000건이면 바닥 31건(전체 1,310건 기준 131건이 아님)."""
    assert c4(_stats(150, 129, 31, skipped=1000))["state"] == "충분"


def test_round_trips_sorted_by_exit_not_entry():
    """A가 먼저 들어가 늦게 나오고 B가 늦게 들어가 먼저 나오면 청산 순으로 B → A(진입 순 · 종목 순이면 A → B)."""
    p = {"A": _frame([10.0 + i for i in range(10)]), "B": _frame([10.0 + i for i in range(10)])}
    idx = p["A"].index
    w = {"A": pd.Series([0, 1, 1, 1, 1, 1, 1, 0, 0, 0], idx, dtype=float),
         "B": pd.Series([0, 0, 0, 1, 0, 0, 0, 0, 0, 0], idx, dtype=float)}
    got = [(t.symbol, t.entry, t.exit) for t in round_trips(p, w, "close", 0.0)]
    assert got == [("B", idx[3], idx[4]), ("A", idx[1], idx[7])]


def test_round_trips_sorted_by_exit_then_symbol():
    """같은 청산 봉이면 종목 순, 청산 시각이 다르면 시각 순(종목 순서와 상관없이)."""
    p = {"B": _frame([10.0 + i for i in range(10)]), "A": _frame([10.0 + i for i in range(10)])}
    idx = p["A"].index
    w = {"B": pd.Series([0, 1, 0, 0, 1, 0, 0, 0, 0, 0], idx, dtype=float),
         "A": pd.Series([0, 1, 0, 0, 0, 0, 1, 0, 0, 0], idx, dtype=float)}
    got = [(t.symbol, t.exit) for t in round_trips(p, w, "close", 0.0)]
    assert got == [("A", idx[2]), ("B", idx[2]), ("B", idx[5]), ("A", idx[7])]


def test_regime_stats_uses_entry_regime_not_exit_regime():
    """진입 봉이 상승이고 청산 봉이 하락인 거래는 상승으로 센다."""
    close = _daily(100 * np.exp(np.cumsum([0.004] * 300 + [-0.02] * 200)))
    tags = regimes(close, 86400)
    entry, exit_ = close.index[299], close.index[499]
    assert tags[entry] == "상승" and tags[exit_] == "하락"  # 전제 확인
    st = regime_stats([Trade("A", entry, exit_, 200, -0.5)], {"A": _ohlc(close)}, 86400)
    assert st["상승"] == {"trades": 1, "mean": -0.5} and st["하락"] == {"trades": 0}  # 0건이면 평균 키 없음


def test_regime_stats_mean_is_net_of_round_trip_cost():
    """국면 평균은 총수익률 − 왕복 60bp(편도 30bp × 2)의 손 계산과 같다."""
    close = _daily(100 * 1.01 ** np.arange(300))
    p = {"A": _ohlc(close)}
    w = pd.Series(0.0, index=close.index)
    w.iloc[[250, 251, 270]] = 1.0  # 거래 둘: 250 → 252(2봉) · 270 → 271(1봉)
    st = regime_stats(round_trips(p, {"A": w}, "close", 30), p, 86400)
    assert st["상승"]["trades"] == 2
    assert st["상승"]["mean"] == pytest.approx(((1.01 ** 2 - 1 - 0.006) + (1.01 - 1 - 0.006)) / 2)

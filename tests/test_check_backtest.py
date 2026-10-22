"""check.backtest: 여러 종목 백테스트."""
from datetime import timedelta
from decimal import Decimal

import numpy as np
import pandas as pd
import pytest

from blackclip.check.backtest import count_matches, metrics, simulate
from blackclip.retro.fills import Execution


def _frame(opens, closes=None):
    idx = pd.date_range("2024-01-01", periods=len(opens), freq="D", tz="UTC")
    closes = closes or opens
    return pd.DataFrame({"Open": opens, "High": opens, "Low": opens, "Close": closes}, index=idx, dtype=float)


def test_simulate_next_open_holds_from_fill_to_next_fill():
    """다음 봉 시가에 체결하고 다음 체결가까지 들고 간다."""
    p = {"A": _frame([10.0, 11, 12, 13])}
    run = simulate(p, {"A": pd.Series([1.0, 1, 0, 0], p["A"].index)}, "next_open")
    assert np.allclose(run.returns.to_numpy(), [0, 12 / 11 - 1, 13 / 12 - 1, 0])


def test_simulate_close_fills_same_bar():
    """close 규칙은 신호 봉 종가에 체결한다."""
    p = {"A": _frame([1.0, 1, 1, 1], closes=[10.0, 11, 12, 13])}
    run = simulate(p, {"A": pd.Series([1.0, 0, 0, 0], p["A"].index)}, "close")
    assert np.allclose(run.returns.to_numpy(), [11 / 10 - 1, 0, 0, 0])


def test_executions_from_weight_changes():
    """비중이 바뀐 체결 봉마다 체결 하나(next_open은 한 봉 뒤)."""
    p = {"A": _frame([10.0, 11, 12, 13, 14, 15, 16])}
    run = simulate(p, {"A": pd.Series([0, 1, 1, 0, -1, -1, 0], p["A"].index, dtype=float)}, "next_open")
    got = [(e.time.day, e.side, e.qty, e.price) for e in run.executions]
    assert got == [(3, "buy", Decimal("1.0"), Decimal("12.0")), (5, "sell", Decimal("1.0"), Decimal("14.0")),
                   (6, "sell", Decimal("1.0"), Decimal("15.0"))]
    assert {e.symbol for e in run.executions} == {"A"}
    assert len({e.order_id for e in run.executions}) == 3


def test_cost_charged_on_change():
    """비중이 바뀐 봉에서 |바뀐 양| × bp를 뺀다."""
    p = {"A": _frame([10.0, 10, 10, 10])}
    run = simulate(p, {"A": pd.Series([1.0, 1, 0, 0], p["A"].index)}, "next_open", cost_bp=30)
    assert np.allclose(run.returns.to_numpy(), [0, -0.003, 0, -0.003])


def test_portfolio_sums_symbols():
    """종목별 비중 수익률을 더한다."""
    a, b = _frame([10.0, 10, 11]), _frame([10.0, 10, 9])
    run = simulate({"A": a, "B": b}, {"A": pd.Series([0.5, 0.5, 0], a.index), "B": pd.Series([0.5, 0.5, 0], b.index)},
                   "next_open")
    assert np.allclose(run.returns.to_numpy(), [0, 0.5 * 0.1 + 0.5 * -0.1, 0])


def test_metrics_flat_is_zero():
    """수익률 0이면 연 수익률 0 · 샤프 0."""
    r = pd.Series(0.0, pd.date_range("2024-01-01", periods=10, freq="D", tz="UTC"))
    assert metrics(r, 86400) == {"cagr": 0.0, "sharpe": 0.0}


def _ex(t, side="buy", sym="A"):
    return Execution(t, sym, side, Decimal(1), Decimal(1), Decimal(0), "", f"o{t:%d%H%M}{side}")


def test_count_matches_within_one_bar():
    """같은 종목 · 방향이면 ±1봉 안에서 하나씩 짝짓는다."""
    p = {"A": _frame([1.0] * 10)}
    t0 = p["A"].index[3].to_pydatetime()
    a = [_ex(t0 + timedelta(minutes=5))]
    assert count_matches(a, [_ex(t0 + timedelta(days=1))], p) == 1
    assert count_matches(a, [_ex(t0 + timedelta(days=2))], p) == 0
    assert count_matches(a, [_ex(t0, side="sell")], p) == 0
    assert count_matches(a + a, [_ex(t0)], p) == 1


def test_rebalance_back_to_target_costs():
    """비중이 그대로여도 가격이 움직이면 실제 비중이 흘러가서, 다시 맞추는 매매에 비용이 든다."""
    p = {"A": _frame([100.0, 110, 100, 110, 100, 110])}
    w = {"A": pd.Series(0.5, p["A"].index)}
    gap = simulate(p, w, "next_open", cost_bp=30).returns - simulate(p, w, "next_open").returns
    assert gap.iloc[1] == pytest.approx(-0.5 * 0.003)          # 처음 사는 봉
    assert (gap.iloc[2:-1] < 0).all()                            # 그 뒤로도 다시 맞추기 비용


def test_metrics_ruin_stays_ruined():
    """자산이 한 번 0 아래로 가면 뒤에 곱이 양수로 돌아와도 연 -100%(파산)."""
    r = pd.Series([-2.0, -2.0, 0.0], pd.date_range("2024-01-01", periods=3, freq="D", tz="UTC"))
    assert metrics(r, 86400)["cagr"] == -1.0


def test_float_noise_makes_no_phantom_execution():
    """비중이 부동소수 오차만큼 달라진 봉은 체결로 안 센다."""
    p = {"A": _frame([10.0, 11, 12, 13])}
    run = simulate(p, {"A": pd.Series([0.3, 0.1 + 0.2, 0.3, 0.3], p["A"].index)}, "next_open")
    assert [(e.time.day, e.side, e.qty) for e in run.executions] == [(2, "buy", Decimal("0.3"))]

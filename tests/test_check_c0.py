"""check.c0: 원래 거래 목록과 다시 돌린 체결 맞대기(재현성)."""
from datetime import timedelta

import pytest
from conftest import make_prices

from blackclip.check.backtest import simulate
from blackclip.check.c0 import c0
from blackclip.check.link import Link, LinkError


def _setup():
    prices = {"BTCUSDT": make_prices(300)}
    c = prices["BTCUSDT"]["Close"]
    weights = {"BTCUSDT": (c.rolling(5).mean() > c.rolling(20).mean()).astype(float)}
    return prices, simulate(prices, weights, "next_open")


def _write(tmp_path, execs, shift=timedelta(minutes=5)):
    rows = ["time,symbol,side,qty,price,fee,order_id"]
    rows += [f"{(e.time + shift).isoformat()},{e.symbol},{e.side},{e.qty},{e.price},0,o{i}" for i, e in enumerate(execs)]
    p = tmp_path / "t.csv"
    p.write_text("\n".join(rows) + "\n", encoding="utf-8")
    return p


def _link(tmp_path, trades):
    return Link("s1", tmp_path / "link.py", tmp_path, None, "next_open", "spot", trades,
                "t.csv" if trades else None, None)


def test_c0_without_trades(tmp_path):
    """원래 거래 목록이 없으면 재현 확인 전."""
    prices, run = _setup()
    r = c0(_link(tmp_path, None), prices, run, 86400)
    assert r["state"] == "재현 확인 전" and "TRADES" in r["text"]


def test_c0_match_with_in_bar_times(tmp_path):
    """원래 목록이 봉 안 시각(+5분)으로 적혀도 같은 봉이면 일치."""
    prices, run = _setup()
    n = len(run.executions)
    assert n >= 4
    r = c0(_link(tmp_path, _write(tmp_path, run.executions)), prices, run, 86400)
    assert r["state"] == "일치"
    assert f"원래 체결 {n:,}건 중 {n:,}건" in r["text"] and "(100.0%)" in r["text"]


def test_c0_mismatch_when_original_missing_rows(tmp_path):
    """원래 목록이 다시 돌린 체결의 절반뿐이면 불일치(반대쪽 비율도 봄)."""
    prices, run = _setup()
    half = run.executions[: len(run.executions) // 2]
    assert c0(_link(tmp_path, _write(tmp_path, half)), prices, run, 86400)["state"] == "불일치"


def test_c0_mismatch_when_only_original_side_is_low(tmp_path):
    """다시 돌린 체결은 전부 원래 목록에 있지만(다시 돌린 쪽 100%) 원래 목록에 다시 돌림이 못 만든 거래가 끼어 있으면
    불일치(원래 쪽 비율도 문턱을 넘어야 한다: 일부만 재현한 걸 일치로 읽으면 안 됨)."""
    prices, run = _setup()
    n = len(run.executions)
    p = _write(tmp_path, run.executions)
    start = prices["BTCUSDT"].index[0]
    extra = n // 4 + 1  # 원래 쪽 비율 n/(n+extra) < 95%
    with p.open("a", encoding="utf-8") as f:
        for i in range(extra):
            f.write(f"{(start + timedelta(days=i * 3, hours=5)).isoformat()},BTCUSDT,buy,1,1,0,extra{i}\n")
    r = c0(_link(tmp_path, p), prices, run, 86400)
    assert r["state"] == "불일치"
    assert f"원래 체결 {n + extra:,}건 중 {n:,}건" in r["text"] and f"다시 돌린 체결 {n:,}건 중 100.0%" in r["text"]


def test_c0_mismatch_when_times_far(tmp_path):
    """다른 봉(3일 뒤)에 적혀 있으면 불일치."""
    prices, run = _setup()
    r = c0(_link(tmp_path, _write(tmp_path, run.executions, shift=timedelta(days=3))), prices, run, 86400)
    assert r["state"] == "불일치"


def test_c0_trades_outside_period(tmp_path):
    """가격표 기간 밖이면 불일치가 아니라 재현 확인 전과 이유."""
    prices, run = _setup()
    r = c0(_link(tmp_path, _write(tmp_path, run.executions, shift=timedelta(days=3650))), prices, run, 86400)
    assert r["state"] == "재현 확인 전" and "기간" in r["text"]


def test_c0_partly_outside_counts_left(tmp_path):
    """일부만 기간 밖이면 뺀 건수를 적는다."""
    prices, run = _setup()
    p = _write(tmp_path, run.executions)
    with p.open("a", encoding="utf-8") as f:
        f.write("2030-01-01T00:00:00+00:00,BTCUSDT,buy,1,1,0,late\n")
    r = c0(_link(tmp_path, p), prices, run, 86400)
    assert r["state"] == "일치" and "밖 1건 뺌" in r["text"]


def test_c0_bad_trades_file_raises_linkerror(tmp_path):
    """원래 목록 파일이 깨졌으면 레포 기준 이름을 넣은 LinkError(종료 코드 2)."""
    prices, run = _setup()
    p = tmp_path / "t.csv"
    p.write_text("time,symbol\n1,2\n", encoding="utf-8")
    with pytest.raises(LinkError, match="TRADES t.csv"):
        c0(_link(tmp_path, p), prices, run, 86400)


def test_c0_range_is_per_symbol(tmp_path):
    """다른 종목의 기간이 넓어도, 그 종목 가격표 밖 체결은 빼고 센다(첫 봉과 짝짓지 않음)."""
    from decimal import Decimal

    from blackclip.check.backtest import Run
    from blackclip.retro.fills import Execution

    prices = {"AAAUSDT": make_prices(2, start="2024-01-10"), "BBBUSDT": make_prices(20)}
    t = prices["AAAUSDT"].index[0].to_pydatetime()
    run = Run(prices["BBBUSDT"]["Close"] * 0, [Execution(t, "AAAUSDT", "buy", Decimal(1), Decimal(1), Decimal(0), "", "bar0")])
    p = tmp_path / "t.csv"
    p.write_text("time,symbol,side,qty,price,fee,order_id\n2024-01-01T00:00:00+00:00,AAAUSDT,buy,1,1,0,a\n", encoding="utf-8")
    r = c0(_link(tmp_path, p), prices, run, 86400)
    assert r["state"] == "재현 확인 전"

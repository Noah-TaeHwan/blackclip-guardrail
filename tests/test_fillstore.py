"""받은 체결 쌓기: 중복 없이 · 시각 순 · 회고가 바로 읽음 · 받은 범위는 이어질 때만 넓힘."""
from datetime import datetime

import pytest

from blackclip.binance.trades import COLUMNS, FUNDING_COLUMNS
from blackclip.fillstore import coverage_span, merge_csv, read_coverage, update_coverage
from blackclip.retro.fills import read_executions, read_funding


def row(t, tid, side="buy", sym="BTCUSDT"):
    """받은 체결 한 줄."""
    return {"time": t, "symbol": sym, "side": side, "qty": "0.001", "price": "60000.00", "fee": "0.00000100",
            "fee_asset": "BTC", "order_id": f"o{tid}", "trade_id": str(tid)}


A = row("2026-10-15T07:00:00.000+00:00", 1)
B = row("2026-10-15T08:00:00.000+00:00", 2, "sell")


def test_merge_writes_header_even_when_empty(tmp_path):
    """받은 것이 없어도 머리줄만 있는 파일을 만든다(회고가 「체결 0건」으로 읽게)."""
    p = tmp_path / "fills.csv"
    assert merge_csv(p, COLUMNS, [], ("symbol", "trade_id")) == 0
    assert p.read_text(encoding="utf-8") == ",".join(COLUMNS) + "\n"


def test_merge_dedupes_on_rerun(tmp_path):
    """같은 줄을 다시 받으면 더하지 않고, 새 줄만 시각 순으로 더한다."""
    p = tmp_path / "fills.csv"
    assert merge_csv(p, COLUMNS, [B, A], ("symbol", "trade_id")) == 2
    assert merge_csv(p, COLUMNS, [A, B, A], ("symbol", "trade_id")) == 0
    c = row("2026-10-15T07:30:00.000+00:00", 3)
    assert merge_csv(p, COLUMNS, [c], ("symbol", "trade_id")) == 1
    times = [line.split(",")[0] for line in p.read_text(encoding="utf-8").splitlines()[1:]]
    assert times == sorted(times) and len(times) == 3
    assert not list(tmp_path.glob("*.tmp"))


def test_merge_refuses_other_columns(tmp_path):
    """열이 다른 파일에는 덧붙이지 않는다."""
    p = tmp_path / "fills.csv"
    p.write_text("time,symbol\n", encoding="utf-8")
    with pytest.raises(ValueError, match="열이 다릅니다"):
        merge_csv(p, COLUMNS, [A], ("symbol", "trade_id"))


def test_merge_refuses_unreadable_csv_in_one_line(tmp_path):
    """손으로 고친 fills.csv의 칸이 csv 모듈 한도(131072자)를 넘으면 csv.Error traceback 대신 ValueError 한 줄(경로 없이 파일 이름만)."""
    p = tmp_path / "fills.csv"
    text = ",".join(COLUMNS) + "\n" + "2026-10-15T07:00:00.000+00:00,BTCUSDT,buy,1,1,0,BTC,o1," + "9" * 200_000 + "\n"
    p.write_text(text, encoding="utf-8")
    with pytest.raises(ValueError, match=r"^fills\.csv: CSV를 읽을 수 없습니다\(Error\)$") as exc:
        merge_csv(p, COLUMNS, [A], ("symbol", "trade_id"))
    assert str(tmp_path) not in str(exc.value) and p.read_text(encoding="utf-8") == text  # 쓰기 전에 멈춤


def test_retro_reads_merged_files(tmp_path):
    """쌓은 파일을 회고가 그대로 읽는다(받는 쪽에서 방향 · 수수료 자산을 맞춤)."""
    p = tmp_path / "fills.csv"
    merge_csv(p, COLUMNS, [A, B], ("symbol", "trade_id"))
    assert [(e.side, e.fee_asset) for e in read_executions(p)] == [("buy", "BTC"), ("sell", "BTC")]
    f = tmp_path / "funding.csv"
    merge_csv(f, FUNDING_COLUMNS, [{"time": A["time"], "symbol": "BTCUSDT", "amount": "-0.0125", "asset": "USDT", "tran_id": "9"}], ("tran_id",))
    assert read_funding(f)[0].amount < 0


def test_coverage_extends_only_when_contiguous(tmp_path):
    """이어 받으면 처음은 그대로 두고 끝만 늘린다. 끝보다 뒤에서 시작하면(빈틈) 새로 잡는다."""
    update_coverage(tmp_path, "BTCUSDT", "2026-09-15T00:00:00.000+00:00", "2026-10-15T00:00:00.000+00:00")
    update_coverage(tmp_path, "BTCUSDT", "2026-10-15T00:00:00.000+00:00", "2026-10-16T00:00:00.000+00:00")
    assert read_coverage(tmp_path)["BTCUSDT"] == {"from": "2026-09-15T00:00:00.000+00:00", "until": "2026-10-16T00:00:00.000+00:00"}
    update_coverage(tmp_path, "BTCUSDT", "2026-10-20T00:00:00.000+00:00", "2026-10-21T00:00:00.000+00:00")
    assert read_coverage(tmp_path)["BTCUSDT"]["from"] == "2026-10-20T00:00:00.000+00:00"


def test_coverage_span_needs_every_symbol(tmp_path):
    """종목 모두가 받은 범위에 있어야 하고, 가장 늦은 처음 · 가장 이른 끝을 돌려준다."""
    update_coverage(tmp_path, "BTCUSDT", "2026-09-15T00:00:00.000+00:00", "2026-10-15T00:00:00.000+00:00")
    update_coverage(tmp_path, "ETHUSDT", "2026-09-20T00:00:00.000+00:00", "2026-10-14T00:00:00.000+00:00")
    cov = read_coverage(tmp_path)
    assert coverage_span(cov, ["BTCUSDT", "ETHUSDT"]) == (datetime.fromisoformat("2026-09-20T00:00:00+00:00"),
                                                          datetime.fromisoformat("2026-10-14T00:00:00+00:00"))
    assert coverage_span(cov, ["BTCUSDT", "SOLUSDT"]) is None
    assert coverage_span(cov, []) is None


def test_read_coverage_entry_shape(tmp_path):
    """항목이 {"from", "until"} 시각 문자열이 아니면 파일 · 종목 이름을 넣은 ValueError."""
    import pytest

    from blackclip.fillstore import read_coverage

    (tmp_path / "coverage.json").write_text('{"ETHUSDT": 1}', encoding="utf-8")
    with pytest.raises(ValueError, match="ETHUSDT"):
        read_coverage(tmp_path)

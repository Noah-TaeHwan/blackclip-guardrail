"""blackclip retro: 예시로 끝까지 · 펀딩비 없음 · 입력 오류는 코드 2와 한 줄."""
import json
import shlex
from pathlib import Path

import pytest

from blackclip.cli import main

EX = Path(__file__).resolve().parents[1] / "examples" / "retro"
BASE = ["retro", "--fills", str(EX / "fills.csv"), "--backtest", str(EX / "backtest.csv"), "--live-start", "2026-09-23T00:00:00+00:00"]
END = ["--end", "2026-10-15T00:00:00+00:00"]
FILLS_UNTIL = ["--fills-until", "2026-10-15T00:00:00+00:00"]


def test_example_end_to_end(tmp_path):
    """예시 파일로 회고 한 장이 나온다. --fills-until이 체결 파일이 담긴 끝(10/14 23:59까지)이므로 10/13 백테스트 매도는 놓친 주문(추정)이다."""
    out = tmp_path / "retro.md"
    code = main(BASE + ["--capital", "37000", "--funding", str(EX / "funding.csv"), "--out", str(out)] + END + FILLS_UNTIL)
    page = out.read_text(encoding="utf-8")
    assert code == 0
    assert "# 실행 회고 · 2026-09-23 ~ 2026-10-14 (UTC)" in page
    assert "백테스트에만 있는 거래 1건(놓친 주문으로 추정)" in page and "체결 파일 범위 밖" not in page
    assert "| 10/13 06:58 | BTCUSDT | 매도 0.12 · 62140.00 | 없음 | 점검 불가 | 놓친 주문(추정) |" in page
    assert "체결 파일이 10/14 23:59까지 담겼다고 --fills-until로 받음" in page and "마지막 체결 뒤" not in page
    assert "펀딩비 · -0.00%p" in page
    assert "| 10/08 00:31 | BTCUSDT | 매수 0.12 · 61020.00 | 10/08 00:31 · 매수 0.12 · 61054.17 | -5.6bp | 비싸게 체결 |" in page
    assert "| 09/24 06:59 | BTCUSDT | 매도 0.12 · 63410.00 | 09/24 07:00 · 매도 0.12 · 63380.00 | -4.7bp | 싸게 체결 |" in page


def test_without_funding_still_renders(capsys):
    """펀딩비 파일 없이도 나오고, 그 줄만 점검 불가다."""
    assert main(BASE + ["--capital", "37000"] + END) == 0
    assert "펀딩비 · 점검 불가(펀딩비 파일 없음)" in capsys.readouterr().out


def test_bad_input_exit_2(tmp_path, capsys):
    """시간대 없는 시각이면 코드 2와 행 번호가 든 한 줄을 낸다."""
    bad = tmp_path / "bad.csv"
    bad.write_text("time,symbol,side,qty,price,fee,order_id\n2026-10-08 09:31,BTCUSDT,buy,1,1,0,x\n", encoding="utf-8")
    code = main(["retro", "--fills", str(bad), "--backtest", str(EX / "backtest.csv"), "--capital", "37000", "--live-start", "2026-09-23T00:00:00+00:00"])
    err = capsys.readouterr().err
    assert code == 2 and "2행 time" in err and err.count("\n") == 1


@pytest.mark.parametrize("extra", [
    ["--capital", "0"],
    ["--capital", "abc"],
    ["--capital", "NaN"],
    ["--capital", "37000", "--days", "0"],
    ["--capital", "37000", "--out", "/nonexistent-dir-blackclip/retro.md"],
    ["--capital", "37000", "--days", "999999"],
    ["--capital", "1e-999999999"],
])
def test_bad_arguments_exit_2(extra, capsys):
    """자본 · 기간 · 결과 경로가 잘못되면 트레이스백 없이 코드 2와 한 줄."""
    assert main(BASE + extra + END) == 2
    err = capsys.readouterr().err
    assert err.startswith("blackclip retro: ") and err.count("\n") == 1 and "Traceback" not in err


def test_non_utf8_file_names_the_argument(tmp_path, capsys):
    """UTF-8이 아닌 체결 파일이면 어느 인자인지 적은 한 줄로 끝낸다."""
    cp = tmp_path / "cp949.csv"
    cp.write_bytes("time,symbol,side,qty,price,fee,order_id\n2026-10-08T00:31:00+00:00,비트,buy,1,1,0,x\n".encode("cp949"))
    code = main(["retro", "--fills", str(cp), "--backtest", str(EX / "backtest.csv"), "--capital", "37000", "--live-start", "2026-09-23T00:00:00+00:00"] + END)
    err = capsys.readouterr().err
    assert code == 2 and "--fills" in err and "UTF-8" in err and err.count("\n") == 1


def test_out_same_as_input_is_refused(tmp_path, capsys):
    """결과 경로가 입력 체결 파일과 같으면 읽기 전에 멈추고 입력을 건드리지 않는다."""
    fills = tmp_path / "fills.csv"
    fills.write_bytes((EX / "fills.csv").read_bytes())
    before = fills.read_bytes()
    argv = ["retro", "--fills", str(fills), "--backtest", str(EX / "backtest.csv"), "--capital", "37000", "--live-start", "2026-09-23T00:00:00+00:00", "--out", str(fills)] + END
    assert main(argv) == 2
    assert "--out: 입력 파일과 같은 경로입니다" in capsys.readouterr().err
    assert fills.read_bytes() == before


@pytest.mark.parametrize("quote", ["", "  "])
def test_blank_quote_is_refused(quote, capsys):
    """빈 거래 통화는 거절한다(모든 짝을 세는 버그 방지)."""
    assert main(BASE + ["--capital", "37000", "--quote", quote] + END) == 2
    assert "--quote: 비어 있습니다" in capsys.readouterr().err


def test_default_end_message_says_how_end_is_chosen(capsys):
    """--end를 안 주고 기본 끝(실제 · 백테스트 중 마지막 체결 다음 날)이 실전 시작보다 앞서면, 기본 규칙을 적어 알린다."""
    argv = ["retro", "--fills", str(EX / "fills.csv"), "--backtest", str(EX / "backtest.csv"), "--capital", "37000", "--live-start", "2026-11-01T00:00:00+00:00"]
    assert main(argv) == 2
    err = capsys.readouterr().err
    assert "--end(주지 않으면 실제 · 백테스트 체결 중 마지막 체결 다음 날 0시 UTC, 체결이 없으면 지금)" in err and "실전 시작보다 뒤여야" in err


def test_live_start_without_timezone_exit_2(capsys):
    """실전 시작 시각에 시간대가 없으면 코드 2와 예시가 든 한 줄."""
    argv = ["retro", "--fills", str(EX / "fills.csv"), "--backtest", str(EX / "backtest.csv"), "--capital", "37000", "--live-start", "2026-09-23"] + END
    assert main(argv) == 2
    err = capsys.readouterr().err
    assert "--live-start: 시간대가 없습니다" in err and err.count("\n") == 1


def test_end_not_after_live_start_exit_2(capsys):
    """--end가 실전 시작과 같거나 앞서면 코드 2와 한 줄."""
    argv = BASE + ["--capital", "37000", "--end", "2026-09-23T00:00:00+00:00"]
    assert main(argv) == 2
    assert "--end: 실전 시작보다 뒤여야 합니다" in capsys.readouterr().err


def test_bad_window_exit_2(capsys):
    """매칭 창이 90m · 2h · 1d 꼴이 아니면 코드 2와 한 줄."""
    assert main(BASE + ["--capital", "37000", "--window", "3x"] + END) == 2
    err = capsys.readouterr().err
    assert err.startswith("blackclip retro: ") and err.count("\n") == 1


def test_missing_fills_file_exit_2(tmp_path, capsys):
    """없는 체결 파일이면 코드 2와 한 줄(트레이스백 없음)."""
    code = main(["retro", "--fills", str(tmp_path / "nope.csv"), "--backtest", str(EX / "backtest.csv"), "--capital", "37000", "--live-start", "2026-09-23T00:00:00+00:00"] + END)
    err = capsys.readouterr().err
    assert code == 2 and err.startswith("blackclip retro: ") and err.count("\n") == 1 and "Traceback" not in err


def _b1(tmp_path):
    """체결 내보내기는 09-03에서 끝나고 백테스트 목록은 09-28까지 가는 파일 한 쌍을 쓰고 인자를 돌려준다."""
    head = "time,symbol,side,qty,price,fee,order_id,fee_asset\n"
    (tmp_path / "f.csv").write_text(head + "2026-09-02T00:01:00Z,BTCUSDT,buy,0.01,60010,0.6,1001,USDT\n2026-09-03T00:01:00Z,BTCUSDT,sell,0.01,60990,0.61,1002,USDT\n", encoding="utf-8")
    (tmp_path / "b.csv").write_text(head + "".join(f"{t},BTCUSDT,{s},0.01,60000,0,{i},USDT\n" for i, (t, s) in enumerate([
        ("2026-09-02T00:00:00+00:00", "buy"), ("2026-09-03T00:00:00+00:00", "sell"), ("2026-09-20T00:00:00+00:00", "buy"),
        ("2026-09-25T00:00:00+00:00", "sell"), ("2026-09-28T00:00:00+00:00", "buy")])), encoding="utf-8")
    return ["retro", "--fills", str(tmp_path / "f.csv"), "--backtest", str(tmp_path / "b.csv"), "--capital", "10000", "--live-start", "2026-09-01T00:00:00+00:00"]


def test_b1_without_flags_does_not_call_a_stopped_bot_missed_orders(tmp_path, capsys):
    """플래그 없이 돌리면 체결 파일 끝이 마지막 체결 날이므로 그 뒤 백테스트 거래 3건은 놓친 주문이 아니라 구분 불가다. 기본 끝은 두 파일 중 마지막 체결 다음 날이다."""
    assert main(_b1(tmp_path)) == 0
    page = capsys.readouterr().out
    assert page.startswith("# 실행 회고 · 2026-09-01 ~ 2026-09-28 (UTC)") and "백테스트 체결 5건" in page
    assert "놓친 주문" not in page and "체결 파일 범위 밖" not in page
    assert "마지막 체결 뒤 3건(봇이 멈췄는지 체결 파일이 끝났는지 구분 불가 · 점검 불가)" in page.split("## 근거")[0]
    assert page.count("| 없음 | 점검 불가 | 마지막 체결 뒤(구분 불가) |") == 3
    assert ("- 체결 파일 범위 09/02 ~ 09/03 · 백테스트 파일 범위 09/02 ~ 09/28 (UTC, 넣은 파일 전체 기준)"
            " · 체결 파일이 어디까지 담겼는지 받지 않음(마지막 체결 뒤 백테스트 거래는 구분 불가로 둠)") in page


def test_fills_until_past_the_export_makes_missed_orders(tmp_path, capsys):
    """--fills-until을 체결 파일 끝보다 뒤로 주면 그 전의 백테스트 거래 3건은 놓친 주문(추정)이고, 그렇게 받았다고 근거에 쓴다."""
    assert main(_b1(tmp_path) + ["--fills-until", "2026-10-01T00:00:00+00:00"]) == 0
    page = capsys.readouterr().out
    assert "백테스트에만 있는 거래 3건(놓친 주문으로 추정) · 손익 영향은 점검 불가(가격 데이터 없음) · 실제에만 있는 거래 0건" in page
    assert "(UTC, 넣은 파일 전체 기준) · 체결 파일이 09/30 23:59까지 담겼다고 --fills-until로 받음" in page
    assert page.count("| 놓친 주문(추정) |") == 3 and "마지막 체결 뒤" not in page and "체결 파일 범위 밖" not in page


def test_fills_until_at_the_export_end_makes_out_of_range(tmp_path, capsys):
    """--fills-until이 체결 파일의 마지막 체결 시각과 같으면(경계는 통과) 그 뒤 거래 3건은 체결 파일 범위 밖(점검 불가)이다."""
    assert main(_b1(tmp_path) + ["--fills-until", "2026-09-03T00:01:00+00:00"]) == 0
    page = capsys.readouterr().out
    assert "백테스트에만 있는 거래 0건 · 체결 파일 범위 밖 3건(점검 불가) · 실제에만 있는 거래 0건" in page
    assert page.count("| 체결 파일 범위 밖 |") == 3 and "놓친 주문" not in page and "마지막 체결 뒤" not in page
    assert "체결 파일이 09/03 00:00까지 담겼다고 --fills-until로 받음" in page


def test_end_alone_past_the_export_is_not_a_missed_order(tmp_path, capsys):
    """--end만 체결 파일 끝보다 뒤로 줘도 체결 파일이 거기까지 담겼다는 말이 아니다: 그 사이 백테스트 거래 3건은 구분 불가이고 놓친 주문이 아니다."""
    assert main(_b1(tmp_path) + ["--end", "2026-10-01T00:00:00+00:00"]) == 0
    page = capsys.readouterr().out
    assert page.startswith("# 실행 회고 · 2026-09-01 ~ 2026-09-30 (UTC)") and "놓친 주문" not in page
    assert "마지막 체결 뒤 3건(" in page and "백테스트 체결 5건" in page and "--fills-until로 받음" not in page


def test_fills_until_in_another_timezone_is_shown_in_utc(tmp_path, capsys):
    """+09:00으로 준 --fills-until도 UTC로 바꿔 근거에 쓴다: 10/01 09:00(+09:00)은 09/30 23:59(UTC)까지다."""
    assert main(_b1(tmp_path) + ["--fills-until", "2026-10-01T09:00:00+09:00"]) == 0
    assert "체결 파일이 09/30 23:59까지 담겼다고 --fills-until로 받음" in capsys.readouterr().out


def _long_tail(tmp_path):
    """_b1에 마지막 체결(09/03)보다 70일 가까이 뒤인 백테스트 거래 2건(11/10 · 11/15)을 더한다: 기본 끝(11/16)의 30일 기간에 체결이 하나도 안 든다."""
    argv = _b1(tmp_path)
    with (tmp_path / "b.csv").open("a", encoding="utf-8") as f:
        f.write("2026-11-10T00:00:00+00:00,BTCUSDT,sell,0.01,60000,0,5,USDT\n2026-11-15T00:00:00+00:00,BTCUSDT,buy,0.01,60000,0,6,USDT\n")
    return argv


def test_backtest_far_past_the_last_fill_stays_visible(tmp_path, capsys):
    """멈춘 봇의 뒤로 백테스트가 30일 넘게 이어져 기간에 실제 체결이 없어도 그 거래 2건은 확인 불가 문장 뒤에 숨지 않는다: 결론 · 근거 개수 · 표가 같다."""
    assert main(_long_tail(tmp_path)) == 0
    page = capsys.readouterr().out
    assert page.startswith("# 실행 회고 · 2026-10-17 ~ 2026-11-15 (UTC)")
    assert "- 실행 · 기간 안 실제 체결 없음 · 마지막 체결 뒤 2건(봇이 멈췄는지 체결 파일이 끝났는지 구분 불가 · 점검 불가)" in page.splitlines()
    assert "확인 불가(기간 안 실제 체결 없음)" not in page and "놓친 주문" not in page
    assert "- 이 기간 실제 체결 0건 · 거래 0건(체결 ÷ 2) · 백테스트 체결 2건" in page
    assert "- 짝지은 체결 0건 · 백테스트에만 0건 · 마지막 체결 뒤 2건 · 실제에만 0건 · 매칭 창 120분" in page
    assert page.count("| 없음 | 점검 불가 | 마지막 체결 뒤(구분 불가) |") == 2 and "| 11/10 00:00 |" in page and "| 11/15 00:00 |" in page


def test_backtest_far_past_the_last_fill_with_fills_until_says_out_of_range(tmp_path, capsys):
    """같은 모양에서 --fills-until이 거래보다 앞서면 두 거래는 체결 파일 범위 밖(점검 불가)이라고 쓰고 표에도 보인다."""
    assert main(_long_tail(tmp_path) + ["--fills-until", "2026-11-01T00:00:00+00:00"]) == 0
    page = capsys.readouterr().out
    assert "- 실행 · 기간 안 실제 체결 없음 · 체결 파일 범위 밖 2건(점검 불가)" in page.splitlines()
    assert page.count("| 없음 | 점검 불가 | 체결 파일 범위 밖 |") == 2 and "마지막 체결 뒤" not in page and "놓친 주문" not in page
    assert "- 짝지은 체결 0건 · 백테스트에만 0건 · 체결 파일 범위 밖 2건 · 실제에만 0건" in page


def test_fills_until_earlier_than_last_fill_exit_2(tmp_path, capsys):
    """--fills-until이 마지막 실제 체결보다 앞이면 코드 2와 한 줄(체결 시각을 적는다)."""
    assert main(_b1(tmp_path) + ["--fills-until", "2026-09-03T00:00:59+00:00"]) == 2
    err = capsys.readouterr().err
    assert "--fills-until: 마지막 실제 체결(2026-09-03T00:01:00+00:00)보다 앞입니다" in err and err.count("\n") == 1


def test_fills_until_without_timezone_exit_2(tmp_path, capsys):
    """--fills-until에 시간대가 없으면 코드 2와 한 줄."""
    assert main(_b1(tmp_path) + ["--fills-until", "2026-10-01T00:00:00"]) == 2
    err = capsys.readouterr().err
    assert "--fills-until: 시간대가 없습니다" in err and err.count("\n") == 1


def test_default_end_without_actual_fills_follows_backtest_file(tmp_path, capsys):
    """실제 체결이 없어도 기본 끝은 백테스트 파일의 마지막 체결 다음 날이고, 놓친 주문을 추정하지 않는다."""
    argv = _b1(tmp_path)
    (tmp_path / "f.csv").write_text("time,symbol,side,qty,price,fee,order_id,fee_asset\n", encoding="utf-8")
    assert main(argv) == 0
    page = capsys.readouterr().out
    assert page.startswith("# 실행 회고 · 2026-09-01 ~ 2026-09-28 (UTC)")
    assert "확인 불가(기간 안 실제 체결 없음)" in page and "체결 파일 범위 없음" in page and "놓친 주문" not in page
    assert "체결 파일이 어디까지 담겼는지 받지 않음" in page


def test_default_end_without_any_fills_is_now(tmp_path, capsys):
    """두 파일 모두 체결이 없으면 기본 끝은 지금이고, 빈 장 대신 확인 불가를 쓴다."""
    argv = _b1(tmp_path)
    for name in ("f.csv", "b.csv"):
        (tmp_path / name).write_text("time,symbol,side,qty,price,fee,order_id,fee_asset\n", encoding="utf-8")
    assert main(argv) == 0
    assert "확인 불가(기간 안 실제 체결 없음)" in capsys.readouterr().out


def test_readme_command_reproduces_the_example_page(tmp_path):
    """README의 명령을 그대로 돌리면 examples/retro/retro.md와 바이트까지 같다."""
    root = EX.parents[1]
    command = next(line for line in (EX / "README.md").read_text(encoding="utf-8").splitlines() if line.startswith("    uv run blackclip retro"))
    argv = shlex.split(command)[3:]
    out = tmp_path / "retro.md"
    assert main([str(root / a) if a.startswith("examples/") else a for a in argv] + ["--out", str(out)]) == 0
    assert out.read_bytes() == (EX / "retro.md").read_bytes()


FILLS_HEAD = "time,symbol,side,qty,price,fee,fee_asset,order_id,trade_id\n"
BT_HEAD = "time,symbol,side,qty,price,fee,order_id\n"
RETRO = ["--capital", "1000", "--live-start", "2026-09-01T00:00:00+00:00"]


def _fetched(tmp_path, cov):
    """blackclip fetch가 쓴 모양의 폴더(체결 1건 · 받은 범위)와 백테스트 파일(같은 매수 + 10/10 매도)을 만든다."""
    d = tmp_path / "spot"
    d.mkdir()
    (d / "fills.csv").write_text(FILLS_HEAD + "2026-09-10T01:00:00.000+00:00,BTCUSDT,buy,0.001,60000,0.000001,BTC,o1,1\n", encoding="utf-8")
    (d / "coverage.json").write_text(json.dumps(cov), encoding="utf-8")
    bt = tmp_path / "bt.csv"
    bt.write_text(BT_HEAD + "2026-09-10T01:00:00+00:00,BTCUSDT,buy,0.001,60000,0,b1\n"
                            "2026-10-10T01:00:00+00:00,BTCUSDT,sell,0.001,61000,0,b2\n", encoding="utf-8")
    return d / "fills.csv", bt


def test_retro_reads_fetch_coverage(tmp_path, capsys):
    """blackclip fetch가 받은 범위가 있으면 체결 파일 끝으로 쓰고, 멈춘 봇의 거래는 놓친 주문(추정)이다."""
    fills, bt = _fetched(tmp_path, {"BTCUSDT": {"from": "2026-09-01T00:00:00.000+00:00", "until": "2026-10-15T12:00:00.000+00:00"}})
    assert main(["retro", "--fills", str(fills), "--backtest", str(bt)] + RETRO) == 0
    page = capsys.readouterr().out
    assert "blackclip fetch 기록에서 읽음" in page and "백테스트에만 있는 거래 1건(놓친 주문으로 추정)" in page


def test_retro_coverage_missing_symbol_warns(tmp_path, capsys):
    """받은 범위에 백테스트 종목이 없으면 끝을 정하지 않고 한 줄 알린다."""
    fills, bt = _fetched(tmp_path, {"ETHUSDT": {"from": "2026-09-01T00:00:00.000+00:00", "until": "2026-10-15T12:00:00.000+00:00"}})
    assert main(["retro", "--fills", str(fills), "--backtest", str(bt)] + RETRO) == 0
    out = capsys.readouterr()
    assert "BTCUSDT" in out.err and "체결 파일이 어디까지 담겼는지 받지 않음" in out.out


def test_retro_coverage_starting_late_counts_out_of_range(tmp_path, capsys):
    """받은 범위가 기간 시작보다 늦게 시작해도 버리지 않는다. 그 앞 짝 없는 백테스트 거래는 놓친 주문이 아니라 범위 밖이다."""
    fills, bt = _fetched(tmp_path, {"BTCUSDT": {"from": "2026-10-01T00:00:00.000+00:00", "until": "2026-10-15T12:00:00.000+00:00"}})
    bt.write_text(bt.read_text(encoding="utf-8") + "2026-09-20T01:00:00+00:00,BTCUSDT,sell,0.001,61000,0,b3\n", encoding="utf-8")
    assert main(["retro", "--fills", str(fills), "--backtest", str(bt)] + RETRO) == 0
    out = capsys.readouterr()
    assert out.err == ""
    assert "체결 파일이 10/01 00:00부터 10/15 11:59까지 담겼다고 blackclip fetch 기록에서 읽음" in out.out
    assert "| 09/20 01:00 | BTCUSDT | 매도 0.001 · 61000.00 | 없음 | 점검 불가 | 체결 파일 범위 밖 |" in out.out
    assert "백테스트에만 있는 거래 1건(놓친 주문으로 추정)" in out.out  # 10/10 매도(받은 범위 안)만 놓친 주문


def test_retro_filters_funding_to_backtest_symbols(tmp_path, capsys):
    """fetch의 funding.csv(계좌 전체 종목)에서 백테스트 종목이 아닌 펀딩비는 더하지 않는다."""
    fills, bt = _fetched(tmp_path, {"BTCUSDT": {"from": "2026-09-01T00:00:00.000+00:00", "until": "2026-10-15T12:00:00.000+00:00"}})
    funding = tmp_path / "funding.csv"
    funding.write_text("time,symbol,amount,asset\n2026-10-01T00:00:00+00:00,BTCUSDT,-1,USDT\n"
                       "2026-10-01T00:00:00+00:00,ETHUSDT,-500,USDT\n", encoding="utf-8")
    assert main(["retro", "--fills", str(fills), "--backtest", str(bt), "--funding", str(funding)] + RETRO) == 0
    assert "펀딩비 · -0.10%p" in capsys.readouterr().out


def test_coverage_only_for_fetched_fills_csv(tmp_path, capsys):
    """같은 폴더의 다른 CSV(일부만 담았을 수 있음)에는 받은 범위를 쓰지 않는다."""
    fills, bt = _fetched(tmp_path, {"BTCUSDT": {"from": "2026-09-01T00:00:00.000+00:00", "until": "2026-10-15T12:00:00.000+00:00"}})
    subset = fills.with_name("subset.csv")
    subset.write_text(fills.read_text(encoding="utf-8"), encoding="utf-8")
    assert main(["retro", "--fills", str(subset), "--backtest", str(bt)] + RETRO) == 0
    assert "체결 파일이 어디까지 담겼는지 받지 않음" in capsys.readouterr().out


def test_coverage_names_normalized(tmp_path, capsys):
    """받은 범위의 거래소 원문 이름(BTCUSDT_261225)을 회고처럼 정규화해 맞댄다."""
    cov = {"BTCUSDT_261225": {"from": "2026-09-01T00:00:00.000+00:00", "until": "2026-10-15T12:00:00.000+00:00"}}
    fills, bt = _fetched(tmp_path, cov)
    for f in (fills, bt):
        f.write_text(f.read_text(encoding="utf-8").replace("BTCUSDT", "BTCUSDT_261225"), encoding="utf-8")
    assert main(["retro", "--fills", str(fills), "--backtest", str(bt)] + RETRO) == 0
    assert "blackclip fetch 기록에서 읽음" in capsys.readouterr().out


def test_explicit_fills_until_wins(tmp_path, capsys):
    """--fills-until을 주면 받은 범위보다 그 말을 쓴다."""
    fills, bt = _fetched(tmp_path, {"BTCUSDT": {"from": "2026-09-01T00:00:00.000+00:00", "until": "2026-10-15T12:00:00.000+00:00"}})
    assert main(["retro", "--fills", str(fills), "--backtest", str(bt), "--fills-until", "2026-10-11T00:00:00+00:00"] + RETRO) == 0
    assert "--fills-until로 받음" in capsys.readouterr().out


@pytest.mark.parametrize("broken", [
    "{",  # 깨진 JSON
    "[]",  # 최상위가 목록
    '{"BTCUSDT": {"from": "2026-09-01T00:00:00.000+00:00"}}',  # until 없음
    '{"BTCUSDT": {"from": "2026-09-01T00:00:00", "until": "2026-10-15T12:00:00"}}',  # 시간대 없음
])
def test_corrupt_coverage_one_line(tmp_path, capsys, broken):
    """깨진 coverage.json은 traceback 없이 코드 2와 파일 이름이 든 한 줄로 끝난다."""
    fills, bt = _fetched(tmp_path, {})
    (fills.parent / "coverage.json").write_text(broken, encoding="utf-8")
    assert main(["retro", "--fills", str(fills), "--backtest", str(bt)] + RETRO) == 2
    err = capsys.readouterr().err
    assert "coverage.json" in err and err.count("\n") == 1


def test_corrupt_coverage_bypassed_by_fills_until(tmp_path, capsys):
    """--fills-until을 주면 coverage.json을 읽지 않아 깨진 파일에 막히지 않는다."""
    fills, bt = _fetched(tmp_path, {})
    (fills.parent / "coverage.json").write_text("{", encoding="utf-8")
    assert main(["retro", "--fills", str(fills), "--backtest", str(bt), "--fills-until", "2026-10-11T00:00:00+00:00"] + RETRO) == 0
    assert "--fills-until로 받음" in capsys.readouterr().out


def test_retro_inverted_coverage_not_used(tmp_path, capsys):
    """종목별 받은 범위가 겹치지 않으면(BTC 08/01~08/20 · ETH 09/15~10/15) 한 줄 경고하고 받은 범위를 쓰지 않는다. 같은 거래를 두 번 세지 않는다."""
    d = tmp_path / "spot"
    d.mkdir()
    (d / "fills.csv").write_text(FILLS_HEAD + "2026-10-10T01:00:00+00:00,ETHUSDT,buy,0.1,3000,0,USDT,1,1\n", encoding="utf-8")
    (d / "coverage.json").write_text(json.dumps({
        "BTCUSDT": {"from": "2026-08-01T00:00:00.000+00:00", "until": "2026-08-20T00:00:00.000+00:00"},
        "ETHUSDT": {"from": "2026-09-15T00:00:00.000+00:00", "until": "2026-10-15T12:00:00.000+00:00"}}), encoding="utf-8")
    bt = tmp_path / "bt.csv"
    bt.write_text(BT_HEAD + "2026-10-10T01:00:00+00:00,ETHUSDT,buy,0.1,3000,0,b1\n"
                            "2026-09-12T01:00:00+00:00,BTCUSDT,sell,0.1,60000,0,b2\n", encoding="utf-8")
    assert main(["retro", "--fills", str(d / "fills.csv"), "--backtest", str(bt), "--capital", "1000",
                 "--live-start", "2026-08-01T00:00:00+00:00"]) == 0
    out = capsys.readouterr()
    assert len(out.err.splitlines()) == 1 and "겹치지 않아" in out.err
    assert "범위 밖 2건" not in out.out and "blackclip fetch 기록에서 읽음" not in out.out
    rows = [line for line in out.out.splitlines() if line.startswith("| 09/12")]
    assert len(rows) == 1


def test_corrupt_coverage_list_one_line_for_status(tmp_path, monkeypatch, capsys):
    """coverage.json 최상위가 목록이면 connect --status도 코드 2와 파일 이름이 든 한 줄이다."""
    import blackclip.cli as cli
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(cli, "default_store", lambda: type("S", (), {"name": "mem", "load": lambda self, n: None})())
    spot = tmp_path / ".blackclip" / "fills" / "binance-main" / "spot"
    spot.mkdir(parents=True)
    (tmp_path / ".blackclip" / "connections.json").write_text('{"binance": {"main": {}}}', encoding="utf-8")
    (spot / "coverage.json").write_text("[]", encoding="utf-8")
    assert main(["connect", "--status"]) == 2
    err = capsys.readouterr().err
    assert len(err.splitlines()) == 1 and "coverage.json" in err


def test_out_symlink_is_replaced_not_followed(tmp_path):
    """레포가 심은 retro.md 링크는 따라가 쓰지 않고(레포 밖 파일 그대로) 링크 자리가 일반 파일 회고가 되며, setup이 그 회고를 본다."""
    from blackclip.notify import first_retro
    outside = tmp_path / "victim.txt"
    outside.write_text("keep me", encoding="utf-8")
    folder = tmp_path / "repo" / ".blackclip" / "strategies" / "s"
    folder.mkdir(parents=True)
    link = folder / "retro.md"
    link.symlink_to(outside)
    assert main(BASE + ["--capital", "37000", "--out", str(link)] + END) == 0
    assert outside.read_text(encoding="utf-8") == "keep me"
    assert not link.is_symlink() and link.read_text(encoding="utf-8").startswith("# 실행 회고 · ")
    assert first_retro(tmp_path / "repo" / ".blackclip")

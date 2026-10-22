"""cli: fetch · connect --status 연결과 입력 오류."""
import urllib.error

import pytest
from conftest import CREDS, MemStore, maker, routes

import blackclip.cli as cli
from blackclip.binance.client import BinanceError, Client
from blackclip.binance.trades import COLUMNS
from blackclip.cli import main
from blackclip.keystore import Credentials
from blackclip.workspace import ensure


def test_fetch_and_status_through_cli(tmp_path, monkeypatch, capsys):
    """명령으로 받고 상태를 본다. 현재 폴더에 .blackclip이 생긴다."""
    monkeypatch.chdir(tmp_path)
    store = MemStore({"binance:main": CREDS})
    monkeypatch.setattr(cli, "default_store", lambda: store)
    monkeypatch.setattr(cli, "_make_client", maker(routes()))
    assert main(["fetch", "--market", "spot", "--symbols", "btcusdt"]) == 0
    assert "spot BTCUSDT: 새로" in capsys.readouterr().out
    assert main(["connect", "--status"]) == 0
    assert "binance main: 키 있음" in capsys.readouterr().out
    assert (tmp_path / ".blackclip" / ".gitignore").exists()


def test_fetch_unreadable_fills_csv_one_line(tmp_path, monkeypatch, capsys):
    """fills.csv가 csv 모듈이 못 읽는 꼴이어도 traceback 없이 그 종목만 「받지 못함」 한 줄 · 코드 4(다른 오류와 같은 길)."""
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(cli, "default_store", lambda: MemStore({"binance:main": CREDS}))
    monkeypatch.setattr(cli, "_make_client", maker(routes()))
    d = tmp_path / ".blackclip" / "fills" / "binance-main" / "spot"
    d.mkdir(parents=True)
    (d / "fills.csv").write_text(",".join(COLUMNS) + "\n" + "x," * 7 + "9" * 200_000 + "\n", encoding="utf-8")
    assert main(["fetch", "--market", "spot", "--symbols", "btcusdt"]) == 4
    out, err = capsys.readouterr()
    assert "spot BTCUSDT: 받지 못함(fills.csv: CSV를 읽을 수 없습니다(Error))" in out
    assert "Traceback" not in err and str(tmp_path) not in out + err


def test_fetch_bad_input_exit_2(tmp_path, monkeypatch, capsys):
    """종목 모양이 이상하거나 --since에 시간대가 없으면 코드 2와 한 줄."""
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(cli, "default_store", lambda: MemStore({"binance:main": CREDS}))
    assert main(["fetch", "--symbols", "BTC/USDT"]) == 2
    assert "--symbols" in capsys.readouterr().err
    assert main(["fetch", "--symbols", "BTCUSDT", "--since", "2026-10-01"]) == 2
    assert "--since: 시간대가 없습니다" in capsys.readouterr().err
    assert main(["fetch", "--symbols", "BTCUSDT", "--since", "2999-01-01T00:00:00Z"]) == 2
    assert "--since: 지금보다 뒤입니다" in capsys.readouterr().err


def test_fetch_accepts_dated_futures_symbol(tmp_path, monkeypatch, capsys):
    """만기 선물처럼 밑줄이 든 거래소 원문 종목도 받는다."""
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(cli, "default_store", lambda: MemStore({"binance:main": CREDS}))
    monkeypatch.setattr(cli, "_make_client", maker(routes(trades=())))
    assert main(["fetch", "--market", "futures", "--symbols", "BTCUSDT_261225"]) == 0
    assert "futures BTCUSDT_261225: 새로 0건" in capsys.readouterr().out


def test_fetch_invalid_key_one_line(tmp_path, monkeypatch, capsys):
    """키가 틀려 권한 조회가 401(-2015)이면 traceback 없이 한 줄과 코드 2로 끝나고, 키 값은 어느 출력에도 없다."""
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(cli, "default_store", lambda: MemStore({"binance:main": CREDS}))

    def answer(path, q):
        if path == "/sapi/v1/account/apiRestrictions":
            raise BinanceError(401, -2015, "Invalid API-key, IP, or permissions for action.")
        return []

    monkeypatch.setattr(cli, "_make_client", maker(answer))
    assert main(["fetch", "--symbols", "BTCUSDT"]) == 2
    out, err = capsys.readouterr()
    assert "-2015" in err and err.startswith("blackclip fetch: ") and len(err.splitlines()) == 1
    assert CREDS.api_key not in out + err and CREDS.secret_key not in out + err


def test_fetch_offline_one_line(tmp_path, monkeypatch, capsys):
    """네트워크가 끊겨 권한 조회가 URLError면 traceback 없이 한 줄과 코드 2로 끝난다."""
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(cli, "default_store", lambda: MemStore({"binance:main": CREDS}))

    def answer(path, q):
        raise urllib.error.URLError("offline")

    monkeypatch.setattr(cli, "_make_client", maker(answer))
    assert main(["fetch", "--symbols", "BTCUSDT"]) == 2
    err = capsys.readouterr().err
    assert err.startswith("blackclip fetch: ") and len(err.splitlines()) == 1


def test_fetch_key_with_newline_not_printed(tmp_path, monkeypatch, capsys):
    """저장된 키에 개행이 있어도 실제 Client가 요청 전에 막아 한 줄과 코드 2로 끝나고, 키는 어느 출력에도 없다."""
    monkeypatch.chdir(tmp_path)
    bad = Credentials("A" * 60 + "WXYZ\n", "S" * 64)
    monkeypatch.setattr(cli, "default_store", lambda: MemStore({"binance:main": bad}))

    def no_network(url, headers):
        raise AssertionError("키 모양이 틀렸는데 요청을 보냈습니다")

    monkeypatch.setattr(cli, "_make_client", lambda m, t, c: Client(m, t, c, transport=no_network))
    assert main(["fetch", "--symbols", "BTCUSDT"]) == 2
    out, err = capsys.readouterr()
    assert err.startswith("blackclip fetch: ") and len(err.splitlines()) == 1
    assert "A" * 60 not in out + err and "S" * 64 not in out + err


def test_connections_list_one_line_for_fetch_and_status(tmp_path, monkeypatch, capsys):
    """connections.json 최상위가 목록이면 fetch · connect --status 둘 다 traceback 없이 코드 2와 한 줄."""
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(cli, "default_store", lambda: MemStore({"binance:main": CREDS}))
    monkeypatch.setattr(cli, "_make_client", maker(routes()))
    (tmp_path / ".blackclip").mkdir()
    (tmp_path / ".blackclip" / "connections.json").write_text("[]", encoding="utf-8")
    for argv, head in ((["fetch", "--symbols", "BTCUSDT"], "blackclip fetch: "), (["connect", "--status"], "blackclip connect: ")):
        assert main(argv) == 2
        err = capsys.readouterr().err
        assert err.startswith(head) and "connections.json" in err and len(err.splitlines()) == 1


def test_status_unknown_permission_or_broken_coverage_one_line(tmp_path, monkeypatch, capsys):
    """connect --status는 모르는 permission.status · 깨진 coverage.json도 코드 2와 한 줄로 끝낸다."""
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(cli, "default_store", lambda: MemStore({"binance:main": CREDS}))
    root = tmp_path / ".blackclip"
    (root / "fills" / "binance-main" / "spot").mkdir(parents=True)
    (root / "connections.json").write_text('{"binance": {"main": {"permission": {"status": "odd", "checked_at": "x"}}}}', encoding="utf-8")
    assert main(["connect", "--status"]) == 2
    assert len(capsys.readouterr().err.splitlines()) == 1
    (root / "connections.json").write_text('{"binance": {"main": {}}}', encoding="utf-8")
    (root / "fills" / "binance-main" / "spot" / "coverage.json").write_text("{", encoding="utf-8")
    assert main(["connect", "--status"]) == 2
    assert len(capsys.readouterr().err.splitlines()) == 1


def test_fetch_refuses_when_another_fetch_runs(tmp_path, monkeypatch, capsys):
    """다른 fetch가 같은 작업 폴더를 잡고 있으면 코드 2와 한 줄."""
    from blackclip.workspace import ensure, lock

    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(cli, "default_store", lambda: MemStore({"binance:main": CREDS}))
    monkeypatch.setattr(cli, "_make_client", maker(routes()))
    with lock(ensure(tmp_path), "fetch"):
        assert main(["fetch", "--market", "spot", "--symbols", "BTCUSDT"]) == 2
    assert "다른 blackclip fetch" in capsys.readouterr().err


def test_connect_port_out_of_range(tmp_path, monkeypatch, capsys):
    """--port가 1024~65535 밖이면 화면을 열기 전에 코드 2."""
    monkeypatch.chdir(tmp_path)
    assert main(["connect", "--open", "--port", "70000"]) == 2
    assert "1024~65535" in capsys.readouterr().err


@pytest.mark.parametrize("text", ['{"binance": []}', '{"binance": 5}', '{"binance": null}', '{"binance": {"main": []}}'])
def test_non_object_binance_stops_with_a_line(tmp_path, monkeypatch, capsys, text):
    """binance 칸 · 계정 칸이 객체가 아니면 fetch · connect --status는 traceback 없이 종료 2와 한 줄, 파일은 그대로."""
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(cli, "default_store", lambda: MemStore({"binance:main": CREDS}))
    monkeypatch.setattr(cli, "_make_client", maker(routes()))
    root = ensure(tmp_path)
    (root / "connections.json").write_text(text, encoding="utf-8")
    for args in (["fetch", "--market", "spot", "--symbols", "btcusdt"], ["connect", "--status"]):
        assert main(args) == 2, args
        err = capsys.readouterr().err
        assert "칸이 객체가 아니라" in err and "Traceback" not in err, args
    assert (root / "connections.json").read_text(encoding="utf-8") == text


@pytest.mark.parametrize("perm", ["[]", "5", "null", '"x"'])
def test_non_object_permission_stops_connect_status(tmp_path, monkeypatch, capsys, perm):
    """계정의 permission 칸이 객체가 아니면 connect --status는 traceback 없이 종료 2와 한 줄(fetch는 이 칸을 새로 적으니 영향 없음)."""
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(cli, "default_store", lambda: MemStore({"binance:main": CREDS}))
    root = ensure(tmp_path)
    (root / "connections.json").write_text('{"binance": {"main": {"permission": %s}}}' % perm, encoding="utf-8")
    assert main(["connect", "--status"]) == 2
    err = capsys.readouterr().err
    assert "permission 칸이 객체가 아니라" in err and "Traceback" not in err


@pytest.mark.parametrize("perm", ['{"status": ["x"], "checked_at": "2026-10-16T00:00:00"}',
                                  '{"status": "read_only", "checked_at": 5}', '{"status": 3, "checked_at": "2026-10-16T00:00:00"}'])
def test_wrong_shaped_permission_values_are_one_line_exit_2(tmp_path, monkeypatch, capsys, perm):
    """permission 안 status가 목록 · 숫자이거나 checked_at이 숫자여도 connect --status는 traceback 없이 종료 2와 한 줄."""
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(cli, "default_store", lambda: MemStore({"binance:main": CREDS}))
    root = ensure(tmp_path)
    (root / "connections.json").write_text('{"binance": {"main": {"permission": %s}}}' % perm, encoding="utf-8")
    assert main(["connect", "--status"]) == 2
    err = capsys.readouterr().err
    assert err.startswith("blackclip connect: ") and "Traceback" not in err and err.count("\n") == 1

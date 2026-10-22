"""보는 포인트: 명령만으로 check → save → live(--capital 없음) → watch 두 번(같은 시간이면 보낼 것 없음) → daily와 따로 기록."""
import json
from datetime import datetime, timezone

from conftest import CREDS, TG_TOKEN, FakeClient, FakeTelegram, KlineClient, MemStore, make_prices, routes, write_link
from test_check_report import _body
from test_daily_run import bt_executions, trades_from

import blackclip.cli as cli
import blackclip.watch.line as wl
from blackclip.cli import main
from blackclip.keystore import Credentials, account_name
from blackclip.notify import TELEGRAM_ACCOUNT
from blackclip.telegram import Bot
from blackclip.workspace import load_connections, save_connections


class FixedNow(datetime):
    at = datetime(2024, 12, 19, 12, 0, tzinfo=timezone.utc)

    @classmethod
    def now(cls, tz=None):
        return cls.at


def test_watch_end_to_end(tmp_path, monkeypatch, capsys):
    """watch 종료 0 · 텔레그램 한 통 → 같은 시각 다시는 안 보냄 → daily는 자기 글을 따로 보내고 notified에 두 키가 함께 남음."""
    monkeypatch.chdir(tmp_path)
    write_link(tmp_path, _body())
    store = MemStore({account_name("binance", "main"): CREDS, TELEGRAM_ACCOUNT: Credentials(TG_TOKEN, "")})
    tg = FakeTelegram()
    trades = trades_from(bt_executions())
    monkeypatch.setattr(wl, "LINE_PATHS", 2000)
    monkeypatch.setattr(cli, "default_store", lambda: store)
    monkeypatch.setattr(cli, "_telegram_bot", lambda token: Bot(token, tg))
    monkeypatch.setattr(cli, "_make_client", lambda m, testnet, creds: KlineClient({"BTCUSDT": make_prices()}, m)
                        if creds is None else FakeClient(m, testnet, routes(trades=trades)))
    monkeypatch.setattr(cli, "datetime", FixedNow)
    assert main(["check", "s1", "--no-binance"]) == 0
    assert main(["save", "s1", "--start", "2024-12-20"]) == 0
    assert main(["live", "s1"]) == 0
    root = tmp_path / ".blackclip"
    conns = load_connections(root)
    conns["telegram"] = {"chat_id": 4242, "bot": "@bc_test_bot"}
    save_connections(root, conns)
    FixedNow.at = datetime(2025, 1, 23, 7, 0, tzinfo=timezone.utc)
    capsys.readouterr()
    assert main(["watch"]) == 0
    out = capsys.readouterr().out.splitlines()
    assert out[0].startswith("binance main · 권한 조회만")
    assert any(x.startswith("s1: 정상 · 가드레일 선 정상 · 구간 기준 잡음 · ") and x.endswith("포지션 맞음: BTCUSDT 없음") for x in out)
    assert out[-1] == "텔레그램: 보냄" and len(tg.sent()) == 1
    assert main(["watch"]) == 0
    assert capsys.readouterr().out.splitlines()[-1] == "텔레그램: 바뀐 상태 없음 · 안 보냄" and len(tg.sent()) == 1
    FixedNow.at = datetime(2025, 1, 24, 0, 10, tzinfo=timezone.utc)
    assert main(["daily"]) == 0, capsys.readouterr().out
    assert len(tg.sent()) > 1 and tg.sent()[1].splitlines()[1].startswith("s1 · 실전 ")
    assert set(json.loads((root / "notified.json").read_text())) == {"s1", "watch:s1"}
    assert all(TG_TOKEN not in x for x in out + tg.sent())

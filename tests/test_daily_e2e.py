"""보는 포인트 「예시 전략이 끝까지 가나」: check → save → live → notify → daily → 텔레그램(가짜) 한 줄로."""
from datetime import datetime, timezone

from conftest import CREDS, TG_TOKEN, FakeClient, FakeTelegram, KlineClient, MemStore, make_prices, routes, write_link
from test_check_report import _body
from test_daily_run import bt_executions, trades_from

import blackclip.cli as cli
from blackclip.cli import main
from blackclip.keystore import Credentials, account_name
from blackclip.notify import TELEGRAM_ACCOUNT
from blackclip.telegram import Bot
from blackclip.workspace import load_connections, save_connections


class FixedNow(datetime):
    at = datetime(2024, 12, 19, 12, 0, tzinfo=timezone.utc)  # live는 12/19 낮(대조는 12/20부터), daily는 2/3

    @classmethod
    def now(cls, tz=None):
        return cls.at


def test_example_strategy_end_to_end(tmp_path, monkeypatch, capsys):
    """명령만으로 끝까지: 저장 → 실전 시작 잔고(조회 키) → 알림 설정 → daily 종료 0 → 텔레그램 두 통(상태 바뀜 + 매일 묶음)."""
    monkeypatch.chdir(tmp_path)
    write_link(tmp_path, _body())
    store = MemStore({account_name("binance", "main"): CREDS, TELEGRAM_ACCOUNT: Credentials(TG_TOKEN, "")})
    tg = FakeTelegram()
    trades = trades_from(bt_executions()[:-1])
    monkeypatch.setattr(cli, "default_store", lambda: store)
    monkeypatch.setattr(cli, "_telegram_bot", lambda token: Bot(token, tg))
    monkeypatch.setattr(cli, "_make_client", lambda m, testnet, creds: KlineClient({"BTCUSDT": make_prices()}, m)
                        if creds is None else FakeClient(m, testnet, routes(trades=trades)))
    monkeypatch.setattr(cli, "datetime", FixedNow)
    FixedNow.at = datetime(2024, 12, 19, 12, 0, tzinfo=timezone.utc)
    assert main(["check", "s1", "--no-binance"]) == 0
    assert main(["save", "s1", "--start", "2024-12-20"]) == 0
    assert main(["live", "s1"]) == 0
    FixedNow.at = datetime(2025, 2, 3, 12, 0, tzinfo=timezone.utc)
    root = tmp_path / ".blackclip"
    conns = load_connections(root)
    conns["telegram"] = {"chat_id": 4242, "bot": "@bc_test_bot"}
    save_connections(root, conns)
    assert main(["notify", "--quiet", "매일"]) == 0
    capsys.readouterr()
    assert main(["daily"]) == 0
    out = capsys.readouterr().out.splitlines()
    assert out[0].startswith("binance main · 권한 조회만")
    assert any(x.startswith("s1: 경고 · 실제 ") and x.endswith("저장 .blackclip/strategies/s1/daily/2025-02-02.md") for x in out)
    assert out[-1] == "텔레그램: 보냄"
    sent = tg.sent()
    assert len(sent) == 2 and sent[0].splitlines()[1] == "s1 · 실전 45일째 · 거래 0건"
    assert sent[1].startswith("매일 묶음 · 2025-02-02 UTC 마감 · 전략 1개")
    assert all(TG_TOKEN not in x for x in out + sent)

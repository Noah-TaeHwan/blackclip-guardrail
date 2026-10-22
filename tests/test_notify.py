"""알림 설정 · 텔레그램 토큰 폼 · 대화 잇기 · 실전 시작 잔고 파일 · 기준 자본 · 헬스체크."""
import json
from datetime import datetime, timezone
from decimal import Decimal as D

import pytest
from conftest import CREDS, TG_TOKEN, FakeClient, FakeTelegram, MemStore, private_update, routes, swap_for_link

import blackclip.cli as cli
import blackclip.notify as notify_mod
from blackclip.cli import main
from blackclip.keystore import Credentials, KeystoreError, account_name
from blackclip.notify import (CAPITAL_DROPPED_TEXT, CAPITAL_TEXT, NO_HEALTHCHECK, TELEGRAM_ACCOUNT, link_telegram,
                              parse_capital, ping, read_live, set_notify, settings, snapshot, status_lines, write_live)
from blackclip.telegram import Bot
from blackclip.workspace import WorkspaceError, ensure

NOW = datetime(2026, 10, 16, 0, 10, tzinfo=timezone.utc)


def _root(tmp_path):
    (tmp_path / ".git").mkdir(exist_ok=True)
    return ensure(tmp_path)


def test_settings_default_daily_line(tmp_path):
    """기본은 매일 한 줄, 실행 경고 꺼짐, 대화 연결 전."""
    s = settings(_root(tmp_path))
    assert (s["quiet"], s["healthcheck"], s["execution_alerts"], s["chat_id"]) == ("매일", None, False, None)


def test_changes_only_without_healthcheck_warns(tmp_path):
    """바뀔 때만 + 헬스체크 없음 → 한 줄 경고, 주소를 넣으면 사라지고 주소 값은 안 보임."""
    root = _root(tmp_path)
    assert NO_HEALTHCHECK in set_notify(root, quiet="바뀔때만")
    lines = set_notify(root, healthcheck="https://hc-ping.com/abc-123")
    assert NO_HEALTHCHECK not in lines and "헬스체크: 주소 있음" in lines and not any("hc-ping" in x for x in lines)
    assert json.loads((root / "connections.json").read_text())["telegram"]["healthcheck"] == "https://hc-ping.com/abc-123"
    with pytest.raises(WorkspaceError):
        set_notify(root, healthcheck="http://plain.example/x")
    with pytest.raises(WorkspaceError):
        set_notify(root, quiet="가끔")


def test_default_sensitivity_line_and_bad_value(tmp_path):
    """notify 줄에 save 기본 민감도(고른 값, 없으면 보통), 모르는 민감도는 거절하고 그대로."""
    root = _root(tmp_path)
    assert "기본 민감도: 보통 · 1년 내내 보면 헛경보 약 20%(한 달 약 1.84%) · blackclip save가 --sensitivity 없을 때" in status_lines(root)
    assert "기본 민감도: 민감 · 1년 내내 보면 헛경보 약 46%(한 달 약 5%) · blackclip save가 --sensitivity 없을 때" in \
        set_notify(root, sensitivity="민감")
    with pytest.raises(WorkspaceError):
        set_notify(root, sensitivity="아주민감")
    assert json.loads((root / "connections.json").read_text())["defaults"] == {"sensitivity": "민감"}


def test_execution_alerts_need_a_daily_file(tmp_path):
    """첫 회고(daily 파일) 전에는 실행 경고를 못 켬."""
    root = _root(tmp_path)
    with pytest.raises(WorkspaceError, match="첫 회고"):
        set_notify(root, execution_alerts=True)
    d = root / "strategies" / "s1" / "daily"
    d.mkdir(parents=True)
    (d / "2026-10-15.md").write_text("x", encoding="utf-8")
    assert "실행 경고 텔레그램: 켜짐" in set_notify(root, execution_alerts=True)


def test_telegram_link_never_prints_token(tmp_path):
    """숫자를 보낸 개인 대화를 잇고 확인 글을 보냄 · 출력 · 파일에 토큰 없음."""
    root, out = _root(tmp_path), []
    store = MemStore({TELEGRAM_ACCOUNT: Credentials(TG_TOKEN, "")})
    fake = FakeTelegram([private_update("482913", 4242)])
    code = link_telegram(root, store, lambda t: Bot(t, fake), out.append, NOW, code="482913")
    assert code == 0
    assert out[0] == "텔레그램에서 @bc_test_bot에게 이 숫자를 보내 주세요: 482913 (15분 안)"
    assert out[-1] == "연결됨 · 텔레그램 @bc_test_bot · 확인 글을 보냈어요"
    t = json.loads((root / "connections.json").read_text())["telegram"]
    assert (t["chat_id"], t["bot"], t["linked_at"]) == (4242, "@bc_test_bot", "2026-10-16T00:10:00+00:00")
    assert fake.sent() == ["BlackClip Guardrail 연결을 확인했어요. 상태가 바뀔 때와 매일 묶음(고른 경우)이 이 대화로 와요."]
    assert not any(TG_TOKEN in x for x in out) and TG_TOKEN not in (root / "connections.json").read_text()
    assert "대화 연결됨(@bc_test_bot)" in status_lines(root, store)[0] and "끝 Zq9x" in status_lines(root, store)[0]


def test_status_lines_link_hint_follows_token(tmp_path):
    """「대화 연결 전」 다음 걸음은 토큰이 있다고 확인될 때만 `connect --telegram`(대화 잇기만).
    토큰이 없거나 · 보관을 읽지 못했거나 · 보관을 안 주어 모를 때는 예전 글 그대로 `connect --open --telegram`(토큰 넣는 화면)."""
    class Broken(MemStore):
        def load(self, account):
            raise KeystoreError("읽지 못함")

    root = _root(tmp_path)
    had = MemStore({TELEGRAM_ACCOUNT: Credentials(TG_TOKEN, "")})
    assert status_lines(root, had)[0] == "텔레그램: 대화 연결 전 · blackclip connect --telegram · 토큰 있음(메모리 · 끝 Zq9x)"
    assert status_lines(root, MemStore())[0] == "텔레그램: 대화 연결 전 · blackclip connect --open --telegram · 토큰 없음"
    assert status_lines(root, Broken())[0] == "텔레그램: 대화 연결 전 · blackclip connect --open --telegram · 토큰 읽기 실패(읽지 못함)"
    assert status_lines(root)[0] == "텔레그램: 대화 연결 전 · blackclip connect --open --telegram"


def test_telegram_link_no_token_or_timeout(tmp_path):
    """토큰 없으면 2, 숫자가 안 오면 1(대화 번호 안 적음)."""
    root, out = _root(tmp_path), []
    assert link_telegram(root, MemStore(), lambda t: Bot(t, FakeTelegram()), out.append, NOW) == 2
    store = MemStore({TELEGRAM_ACCOUNT: Credentials(TG_TOKEN, "")})
    now = [0.0]

    def clock():
        now[0] += 400
        return now[0]

    assert link_telegram(root, store, lambda t: Bot(t, FakeTelegram([])), out.append, NOW, code="1", clock=clock) == 1
    assert settings(root)["chat_id"] is None
    # 토큰 없음은 넣는 화면으로, 토큰이 이미 있는 다음 걸음(시간 지남 · 연결 전)은 대화 잇기만으로
    assert out[0] == "텔레그램 토큰이 없습니다 · blackclip connect --open --telegram"
    assert out[-1] == "시간 안에 숫자가 오지 않아 대화를 잇지 못했어요 · blackclip connect --telegram으로 다시"
    assert status_lines(root, store)[0] == "텔레그램: 대화 연결 전 · blackclip connect --telegram · 토큰 있음(메모리 · 끝 Zq9x)"


def _guardrail(root, market="spot"):
    """소비자가 읽는 칸을 모두 채운 가드레일(workspace.guardrail_problem을 통과)."""
    (root / "strategies" / "s1").mkdir(parents=True, exist_ok=True)
    doc = {"version": 1, "live_start": "2026-09-23",
           "target": {"market": market, "symbols": ["BTCUSDT", "SOLUSDT"], "bar_seconds": 3600, "exec": "next_open",
                      "start": "2024-01-01", "link_sha256": "0" * 64},
           "normal_range": {"drawn": False}, "warning_line": {"line": -0.072, "sensitivity": "민감", "lines": {"민감": -0.072}},
           "judgment_hold": {"min_trades": 30, "hold_if_edge0_at_least": 0.5, "edge0_in_range": None},
           "trade_frequency": {"low": 7, "high": 17}, "thesis": {"rule": "논지 없음"}, "period": {"mean": 0.0001, "std": 0.01}}
    (root / "strategies" / "s1" / "guardrail.v1.yaml").write_text(json.dumps(doc, ensure_ascii=False), encoding="utf-8")


def test_live_file_roundtrip_and_refusals(tmp_path):
    """가드레일이 있어야 적고, 출발 시각은 받은 날의 UTC 마감(다음 날 0시) · 파일은 Git 제외 자리."""
    root = _root(tmp_path)
    with pytest.raises(WorkspaceError, match="가드레일이 없습니다"):
        write_live(root, "s1", "main", "USDT", NOW, D("1"), {})
    _guardrail(root)
    with pytest.raises(WorkspaceError):
        write_live(root, "s1", "main", "US DT", NOW, D("1"), {})
    p = write_live(root, "s1", "main", "usdt", NOW, D("10000.5"), {"BTCUSDT": D("0.1"), "SOLUSDT": D("0")})
    assert read_live(root, "s1") == {"account": "main", "quote": "USDT", "taken_at": NOW,
                                     "start": datetime(2026, 10, 17, tzinfo=timezone.utc), "cash": D("10000.5"),
                                     "positions": {"BTCUSDT": D("0.1"), "SOLUSDT": D("0")}, "capital": None}
    assert p.name == "live.json" and "live.json" not in (root / ".gitignore").read_text()
    p.write_text("{깨짐", encoding="utf-8")
    assert read_live(root, "s1") is None


def test_live_json_does_not_follow_parent_link(tmp_path, tmp_path_factory, monkeypatch):
    """판 확인을 지난 뒤 strategies/s1이 레포 밖 폴더 링크가 되어도 live.json은 레포 밖에 생기지 않는다."""
    root = _root(tmp_path)
    _guardrail(root)
    outside = tmp_path_factory.mktemp("outside")
    real = notify_mod.account_key

    def key_then_swap(*a, **k):  # 판 확인 다음, 쓰기 앞에서 부름
        got = real(*a, **k)
        swap_for_link(root / "strategies" / "s1", outside)
        return got

    monkeypatch.setattr(notify_mod, "account_key", key_then_swap)
    with pytest.raises(WorkspaceError, match="링크"):
        write_live(root, "s1", "main", "USDT", NOW, D("1"), {})
    assert list(outside.iterdir()) == []


def test_live_json_guardrail_check_does_not_follow_parent_link(tmp_path, tmp_path_factory):
    """strategies/s1이 가드레일이 든 레포 밖 폴더 링크이면 그 판을 세지 않아 「가드레일이 없습니다」로 멈추고 live.json은 안 생긴다."""
    root = _root(tmp_path)
    outside = tmp_path_factory.mktemp("outside")
    _guardrail(root)
    (root / "strategies" / "s1").rename(outside / "s1")
    (root / "strategies" / "s1").symlink_to(outside / "s1", target_is_directory=True)
    with pytest.raises(WorkspaceError, match="가드레일이 없습니다"):
        write_live(root, "s1", "main", "USDT", NOW, D("1"), {})
    assert [p.name for p in (outside / "s1").iterdir()] == ["guardrail.v1.yaml"]


def test_read_live_does_not_follow_parent_link(tmp_path, tmp_path_factory):
    """strategies/s1이 레포 밖 폴더 링크이면 그 안의 live.json은 읽지 않고 None."""
    root = _root(tmp_path)
    _guardrail(root)
    write_live(root, "s1", "main", "USDT", NOW, D("1"), {})
    assert read_live(root, "s1") is not None
    outside = tmp_path_factory.mktemp("outside")
    (root / "strategies" / "s1").rename(outside / "s1")
    (root / "strategies" / "s1").symlink_to(outside / "s1", target_is_directory=True)
    assert (outside / "s1" / "live.json").is_file() and read_live(root, "s1") is None


def test_live_capital_is_stored_and_must_be_positive(tmp_path):
    """기준 자본은 live.json에만 「capital」로 적히고 읽힘(잔고 · 포지션은 그대로), 0 이하 · NaN · 무한대는 거절."""
    root = _root(tmp_path)
    _guardrail(root)
    p = write_live(root, "s1", "main", "USDT", NOW, D("10000"), {"BTCUSDT": D("0")}, D("2500.5"))
    live = read_live(root, "s1")
    assert live["capital"] == D("2500.5") and live["cash"] == D("10000") and live["positions"] == {"BTCUSDT": D("0")}
    assert json.loads(p.read_text(encoding="utf-8"))["capital"] == "2500.5"
    for bad in (D("0"), D("-1"), D("NaN"), D("Infinity")):
        with pytest.raises(WorkspaceError, match="--capital"):
            write_live(root, "s1", "main", "USDT", NOW, D("10000"), {}, bad)
    assert read_live(root, "s1")["capital"] == D("2500.5")  # 거절한 뒤에도 파일은 그대로
    for text in ("0", "-5", "abc", "", "NaN", "inf"):
        with pytest.raises(WorkspaceError) as exc:
            parse_capital(text)
        assert str(exc.value) == CAPITAL_TEXT
    assert parse_capital(" 1e3 ") == D("1000")
    doc = json.loads(p.read_text(encoding="utf-8"))
    p.write_text(json.dumps({**doc, "capital": "0"}), encoding="utf-8")  # 손으로 0 이하로 고친 파일은 깨진 것
    assert read_live(root, "s1") is None


def test_snapshot_spot_and_futures():
    """현물은 free + locked, 선물은 양방향 포지션 합 · 현금 = 지갑 − Σ 포지션 × 진입가."""
    def spot(path, q):
        assert path == "/api/v3/account" and q == {"omitZeroBalances": "true"}
        return {"balances": [{"asset": "USDT", "free": "1000", "locked": "5"}, {"asset": "BTC", "free": "0.1", "locked": "0"},
                             {"asset": "ETH", "free": "2", "locked": "0"}]}

    assert snapshot(FakeClient("spot", False, spot), "spot", ["BTCUSDT", "SOLUSDT"], "USDT") == (
        D("1005"), {"BTCUSDT": D("0.1"), "SOLUSDT": D("0")})

    def fut(path, q):
        if path == "/fapi/v2/positionRisk":
            return [{"symbol": "BTCUSDT", "positionAmt": "0.2", "entryPrice": "60000", "positionSide": "LONG"},
                    {"symbol": "BTCUSDT", "positionAmt": "-0.05", "entryPrice": "61000", "positionSide": "SHORT"},
                    {"symbol": "ETHUSDT", "positionAmt": "1", "entryPrice": "3000", "positionSide": "BOTH"}]
        assert path == "/fapi/v2/balance"
        return [{"asset": "USDT", "balance": "20000"}, {"asset": "BNB", "balance": "1"}]

    assert snapshot(FakeClient("futures", False, fut), "futures", ["BTCUSDT"], "USDT") == (
        D("11050"), {"BTCUSDT": D("0.15")})
    with pytest.raises(WorkspaceError):
        snapshot(FakeClient("spot", False, spot), "spot", ["BTCBUSD"], "USDT")


def test_live_cli(tmp_path, monkeypatch, capsys):
    """blackclip live: 조회 키로 잔고를 받아 한 줄 · 코드 0, 가드레일 · 키가 없으면 2(주문 경로 안 부름)."""
    monkeypatch.chdir(tmp_path)
    root = _root(tmp_path)
    made = []
    monkeypatch.setattr(cli, "default_store", lambda: MemStore({account_name("binance", "main"): CREDS}))
    monkeypatch.setattr(cli, "_make_client", lambda m, t, c: made.append(FakeClient(m, t, routes())) or made[-1])
    assert main(["live", "s1"]) == 2
    _guardrail(root)
    assert main(["live", "s1"]) == 0
    out = capsys.readouterr().out.strip()
    assert out.startswith("실전 시작 잔고 받음 · s1 · 계좌 main · 포지션 없음 · ") and out.endswith(
        ".blackclip/strategies/s1/live.json(Git 제외)")
    assert read_live(root, "s1")["cash"] == D("10000")
    assert [p for c in made for p, _ in c.calls] == ["/sapi/v1/account/apiRestrictions", "/api/v3/account"]
    monkeypatch.setattr(cli, "default_store", lambda: MemStore())
    assert main(["live", "s1"]) == 2


def test_live_cli_capital(tmp_path, monkeypatch, capsys):
    """live --capital: 잔고 · 포지션은 그대로 받고 capital만 더해 적음, 0 이하 · 숫자 아님은 받기 전에 한 줄 오류 종료 2."""
    monkeypatch.chdir(tmp_path)
    root = _root(tmp_path)
    _guardrail(root)
    made = []
    monkeypatch.setattr(cli, "default_store", lambda: MemStore({account_name("binance", "main"): CREDS}))
    monkeypatch.setattr(cli, "_make_client", lambda m, t, c: made.append(FakeClient(m, t, routes())) or made[-1])
    for bad in ("0", "-5", "abc", "nan"):
        assert main(["live", "s1", f"--capital={bad}"]) == 2
        assert capsys.readouterr().err.splitlines() == [f"blackclip live: {CAPITAL_TEXT}"]
    assert made == [] and read_live(root, "s1") is None  # 키 보관 · 바이낸스를 부르기 전에 거절
    assert main(["live", "s1", "--capital", "2500"]) == 0
    out = capsys.readouterr().out
    assert "2500" not in out and CAPITAL_DROPPED_TEXT not in out  # 금액은 화면에 안 나옴, 처음엔 알림 없음
    live = read_live(root, "s1")
    assert live["capital"] == D("2500") and live["cash"] == D("10000")
    assert [p for c in made for p, _ in c.calls] == ["/sapi/v1/account/apiRestrictions", "/api/v3/account"]
    assert main(["live", "s1"]) == 0  # 안 주면 다시 적을 때 capital 없음(현금 + 출발 포지션을 씀)
    assert read_live(root, "s1")["capital"] is None
    assert capsys.readouterr().out.count(CAPITAL_DROPPED_TEXT) == 1  # 지운 것을 알림
    assert main(["live", "s1"]) == 0
    assert CAPITAL_DROPPED_TEXT not in capsys.readouterr().out  # 지울 값이 없으면 알림 없음


def test_ping_https_only_and_failure_line():
    """주소 없으면 아무것도 안 함, 실패하면 종류만."""
    got = []
    assert ping(None) is None
    assert ping("https://hc-ping.com/x", got.append) is None and got == ["https://hc-ping.com/x"]

    def boom(url):
        raise OSError("no route to hc-ping.com/x")

    assert ping("https://hc-ping.com/x", boom) == "헬스체크 신호를 못 보냄(OSError)"


def test_saved_sensitivity_of_another_type_falls_back_to_normal(tmp_path):
    """connections.json의 기본 민감도가 배열 · 객체 · null · 숫자면 보통(고치기 전에는 배열 · 객체에서 TypeError)."""
    from blackclip.notify import chosen_sensitivity
    root = _root(tmp_path)
    for bad in ([], {}, None, 3, "아주민감"):
        (root / "connections.json").write_text(json.dumps({"defaults": {"sensitivity": bad}}))
        assert chosen_sensitivity(root) == "보통"


def test_defaults_of_another_type_is_refused_before_saving(tmp_path):
    """defaults 칸 자체가 [] · null · 숫자면 민감도 저장 전에 WorkspaceError, 파일은 그대로(2판은 TypeError)."""
    from blackclip.notify import set_notify
    root = _root(tmp_path)
    for bad in ([], None, 42):
        text = json.dumps({"defaults": bad})
        (root / "connections.json").write_text(text)
        with pytest.raises(WorkspaceError, match="defaults 칸이 객체가 아니라"):
            set_notify(root, sensitivity="보통")
        assert (root / "connections.json").read_text() == text


@pytest.mark.parametrize("text", ['{"telegram": []}', '{"telegram": null}', '{"telegram": 3}'])
def test_cli_notify_stops_on_non_object_telegram(tmp_path, monkeypatch, capsys, text):
    """telegram 칸이 객체가 아니면 notify는 traceback 없이 종료 2와 한 줄(보기 · 바꾸기 모두), 파일은 그대로."""
    root = _root(tmp_path)
    (root / "connections.json").write_text(text)
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(cli, "default_store", lambda: MemStore())
    for args in (["notify"], ["notify", "--quiet", "바뀔때만"]):
        assert main(args) == 2
        err = capsys.readouterr().err
        assert "telegram 칸이 객체가 아니라" in err and "Traceback" not in err
    assert (root / "connections.json").read_text() == text
    with pytest.raises(WorkspaceError, match="telegram 칸이 객체가 아니라"):
        settings(root)


def test_link_skips_backlog_whatever_the_clock(tmp_path):
    """숫자를 띄우기 전에 봇에 밀려 있던 글(같은 숫자 · 맥 시계가 어긋나도)로 잇지 않고, 띄운 뒤 온 글로만 잇는다.
    시작 기준은 텔레그램 업데이트 순서라 맥 시계와 상관없다."""
    root, out = _root(tmp_path), []
    store = MemStore({TELEGRAM_ACCOUNT: Credentials(TG_TOKEN, "")})
    fake = FakeTelegram([private_update("482913", 77, update_id=8)],
                        backlog=[private_update("482913", 66, update_id=6), private_update("482913", 66, update_id=7)])
    assert link_telegram(root, store, lambda t: Bot(t, fake), out.append, NOW, code="482913") == 0
    assert settings(root)["chat_id"] == 77 and fake.calls[1][2]["offset"] == "-1"

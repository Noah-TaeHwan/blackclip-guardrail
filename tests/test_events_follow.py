"""키 없는 감시: 어댑터로 봇 로그를 이벤트로 · 읽은 자리부터 이어 읽기 · 「봇이 안 보여요」 · 5분 예약."""
import json
import os
import plistlib
from datetime import datetime, timedelta, timezone

import pytest
from conftest import TG_TOKEN, FakeTelegram, MemStore

import blackclip.cli as cli
from blackclip.cli import main
from blackclip.events import EVENTS, STATE, EventsError, ingest, source_path
from blackclip.follow import FSTATE, approve, run_follow
from blackclip.keystore import Credentials
from blackclip.notify import TELEGRAM_ACCOUNT
from blackclip.schedule import FOLLOW_S, follow_plist, install, label, remove
from blackclip.telegram import Bot
from blackclip.workspace import ensure, load_connections, save_connections

NOW = datetime(2026, 10, 23, 3, 0, tzinfo=timezone.utc)

ADAPTER = '''SOURCE = "bot/decisions.jsonl"
import json, sys
for line in sys.stdin:
    d = json.loads(line)
    print(json.dumps({"kind": "heartbeat", "time": d["t"]}))
'''
"""시험 봇 로그 한 줄({"t": 시각}) → heartbeat 하나."""


def setup_repo(tmp_path, adapter=ADAPTER, name="bot1"):
    """레포 · 작업 폴더 · 어댑터 · 빈 봇 로그."""
    (tmp_path / ".git").mkdir()
    root = ensure(tmp_path)
    (root / "strategies" / name).mkdir(parents=True)
    (root / "strategies" / name / "adapter.py").write_text(adapter, encoding="utf-8")
    (tmp_path / "bot").mkdir()
    log = tmp_path / "bot" / "decisions.jsonl"
    log.write_text("", encoding="utf-8")
    return root, log


def line(t):
    """봇 로그 한 줄."""
    return json.dumps({"t": t.isoformat().replace("+00:00", "Z")}) + "\n"


def test_reads_only_new_complete_lines_and_restarts_on_new_file(tmp_path):
    """끝 줄바꿈이 없는 줄은 다음에 · 읽은 자리부터 이어 읽음 · 파일이 바뀌거나(inode) 짧아지면 처음부터."""
    root, log = setup_repo(tmp_path)
    t0 = NOW - timedelta(hours=3)
    log.write_text(line(t0) + line(t0 + timedelta(hours=1)) + '{"t": "2026-10-23T', encoding="utf-8")
    got = ingest(root, "bot1", NOW)
    assert (got["new_lines"], got["new_events"], got["last"]) == (2, 2, "2026-10-23T01:00:00Z")
    with open(log, "a", encoding="utf-8") as f:
        f.write('01:30:00Z"}\n' + line(t0 + timedelta(hours=2)))
    got = ingest(root, "bot1", NOW)
    assert (got["new_lines"], got["new_events"], got["last"], got["counts"]) == (2, 2, "2026-10-23T02:00:00Z", {"heartbeat": 4})
    assert got["offset"] == log.stat().st_size
    assert ingest(root, "bot1", NOW)["new_lines"] == 0  # 새 줄 없음 → 어댑터도 안 띄움
    assert len((root / "strategies" / "bot1" / EVENTS).read_text().splitlines()) == 4
    log.unlink()
    log.write_text(line(t0) * 2, encoding="utf-8")  # 다른 파일(봇이 로그를 새로 만듦)
    assert ingest(root, "bot1", NOW)["new_lines"] == 2
    log.write_text("", encoding="utf-8")  # 같은 파일을 비우고 더 짧게 다시 씀 → 읽은 자리보다 짧음
    with open(log, "a", encoding="utf-8") as f:
        f.write(line(t0))
    assert ingest(root, "bot1", NOW)["new_lines"] == 1


def test_bad_event_lines_are_counted_not_kept(tmp_path):
    """JSON이 아님 · 시간대 없는 시각 · 10분 넘게 미래 · 필수 칸 없음 · side 모양 · 숫자 아님은 형식 오류로 세고 버린다."""
    adapter = '''SOURCE = "bot/decisions.jsonl"
import sys
sys.stdin.read()
print("not json")
print('{"kind": "heartbeat", "time": "2026-10-23T01:00:00"}')
print('{"kind": "heartbeat", "time": "2026-10-23T03:30:00Z"}')
print('{"kind": "fill", "time": "2026-10-23T01:00:00Z", "symbol": "BTC/USDT", "side": "buy", "qty": "0.001"}')
print('{"kind": "fill", "time": "2026-10-23T01:00:00Z", "symbol": "BTC/USDT", "side": "BUY", "qty": "1", "price": "1"}')
print('{"kind": "equity", "time": "2026-10-23T01:00:00Z", "value": "NaN"}')
print('{"kind": "fill", "time": "2026-10-23T10:00:00+09:00", "symbol": "BTC/USDT", "side": "sell", "qty": "0.001", "price": "81000"}')
'''
    root, log = setup_repo(tmp_path, adapter)
    log.write_text(line(NOW), encoding="utf-8")
    got = ingest(root, "bot1", NOW)
    assert (got["new_events"], got["new_bad"], got["counts"], got["last"]) == (1, 6, {"fill": 1}, "2026-10-23T01:00:00Z")
    kept = json.loads((root / "strategies" / "bot1" / EVENTS).read_text())
    assert kept["time"] == "2026-10-23T01:00:00Z" and kept["price"] == "81000"  # 시각은 UTC로 맞춤, 나머지 칸은 그대로


def test_adapter_failure_keeps_read_position_and_gets_no_engine_env(tmp_path, monkeypatch):
    """어댑터가 실패하면 확인 불가(EventsError)이고 읽은 자리를 옮기지 않는다(줄을 잃지 않음).
    어댑터는 엔진 환경 변수를 받지 않는다(고객 코드는 엔진과 같은 환경에서 돌지 않는다)."""
    root, log = setup_repo(tmp_path, 'SOURCE = "bot/decisions.jsonl"\nimport sys\nsys.exit("boom")\n')
    log.write_text(line(NOW), encoding="utf-8")
    with pytest.raises(EventsError, match="어댑터가 실패했습니다\\(종료 1\\) · boom"):
        ingest(root, "bot1", NOW)
    assert not (root / "strategies" / "bot1" / STATE).exists()
    monkeypatch.setenv("BC_TESTNET_SECRET_KEY", "x")
    (root / "strategies" / "bot1" / "adapter.py").write_text(
        'SOURCE = "bot/decisions.jsonl"\nimport json, os, sys\nsys.stdin.read()\n'
        'print(json.dumps({"kind": "heartbeat", "time": "2026-10-23T02:00:00Z", "env": sorted(os.environ)}))\n')
    assert ingest(root, "bot1", NOW)["new_events"] == 1
    env = json.loads((root / "strategies" / "bot1" / EVENTS).read_text())["env"]
    assert "BC_TESTNET_SECRET_KEY" not in env and "HOME" not in env


def test_source_is_read_without_running_the_adapter(tmp_path):
    """SOURCE는 ast로만 읽는다(어댑터 맨 위 코드가 돌지 않음). 없으면 그 한 줄로 멈춘다."""
    root, _ = setup_repo(tmp_path, 'SOURCE = "bot/decisions.jsonl"\nopen("ran", "w")\n')
    assert source_path(root, "bot1") == tmp_path / "bot" / "decisions.jsonl" and not (tmp_path / "ran").exists()
    (root / "strategies" / "bot1" / "adapter.py").write_text("import sys\n")
    with pytest.raises(EventsError, match='맨 위에 SOURCE = "<봇 로그 경로>" 한 줄이 있어야 합니다'):
        source_path(root, "bot1")


def tick(root, tg, now):
    """가짜 텔레그램으로 시계 한 번."""
    store = MemStore({TELEGRAM_ACCOUNT: Credentials(TG_TOKEN, "")})
    return run_follow(root, store, lambda token: Bot(token, tg), now)


def link_chat(root):
    """텔레그램 대화가 이어진 상태(connect --telegram 뒤)."""
    conns = load_connections(root)
    conns["telegram"] = {"chat_id": 4242, "bot": "@bc_test_bot"}
    save_connections(root, conns)


def test_quiet_bot_alert_once_then_back(tmp_path):
    """선 90분: 마지막 이벤트가 3시간 전이면 「봇이 안 보여요」 한 통 · 같은 상태는 다시 안 보냄 · 이벤트가 오면 「다시 보여요」."""
    root, log = setup_repo(tmp_path)
    log.write_text(line(NOW - timedelta(hours=3)), encoding="utf-8")
    approve(root, "bot1", 90, NOW)
    link_chat(root)
    tg = FakeTelegram()
    code, out = tick(root, tg, NOW)
    assert code == 0 and out == ["bot1: 안 보임 · 마지막 이벤트 2026-10-23 00:00 UTC · 3시간 0분 전 · 선 90분(승인한 값)",
                                 "텔레그램: 보냄"]
    assert tg.sent() == ["봇이 안 보여요 · bot1 · 키 없는 감시\n"
                         "마지막 이벤트 2026-10-23 00:00 UTC · 3시간 0분 전 · 선 90분(승인한 값)\n"
                         "봇이 돌고 있는지 확인해 주세요 · Guardrail은 봇을 멈추거나 주문하지 않아요"]
    assert tick(root, tg, NOW + timedelta(minutes=5))[1][-1] == "텔레그램: 바뀐 상태 없음 · 안 보냄"
    with open(log, "a", encoding="utf-8") as f:
        f.write(line(NOW + timedelta(minutes=9)))
    tick(root, tg, NOW + timedelta(minutes=10))
    assert len(tg.sent()) == 2 and tg.sent()[1].startswith("봇이 다시 보여요 · bot1 · 키 없는 감시\n")
    assert all(TG_TOKEN not in x for x in tg.sent())


def test_first_normal_not_sent_and_unconfirmed_needs_two_in_a_row(tmp_path):
    """처음 본 정상은 안 보냄 · 확인 불가(어댑터 실패)는 두 번 연속일 때만 보냄(종료 4) · 대화 전이면 안 보낸다고 적음."""
    root, log = setup_repo(tmp_path)
    log.write_text(line(NOW - timedelta(minutes=10)), encoding="utf-8")
    approve(root, "bot1", 90, NOW)
    tg = FakeTelegram()
    assert tick(root, tg, NOW) == (0, ["bot1: 정상 · 마지막 이벤트 2026-10-23 02:50 UTC · 10분 전 · 선 90분(승인한 값)",
                                       "텔레그램: 바뀐 상태 없음 · 안 보냄"])
    (root / "strategies" / "bot1" / "adapter.py").write_text('SOURCE = "bot/decisions.jsonl"\nraise SystemExit(3)\n')
    with open(log, "a", encoding="utf-8") as f:
        f.write(line(NOW))
    code, out = tick(root, tg, NOW + timedelta(minutes=5))
    assert code == 4 and out[0] == "bot1: 확인 불가 · 어댑터가 실패했습니다(종료 3)" and out[1] == "텔레그램: 확인 불가 1회째 · 다음 확인도 그러면 보냄(bot1)"
    assert tick(root, tg, NOW + timedelta(minutes=10))[1][1] == "텔레그램: 대화 연결 전이라 안 보냄 · blackclip connect --telegram"
    link_chat(root)
    tick(root, tg, NOW + timedelta(minutes=15))
    assert tg.sent() == ["확인 불가 · bot1 · 키 없는 감시\n어댑터가 실패했습니다(종료 3)\n"
                         "blackclip events bot1로 어댑터와 봇 로그를 확인해 주세요"]
    assert json.loads((root / "strategies" / "bot1" / FSTATE).read_text()) == {"sent": "확인 불가", "streak": 3}


def test_cli_events_follow_and_schedule(tmp_path, monkeypatch, capsys):
    """명령으로: events(이벤트 확인) → follow <전략> --quiet-minutes(선 승인) → schedule에 5분 예약이 붙음 · follow(시계) 종료 0."""
    _, log = setup_repo(tmp_path)
    log.write_text(line(NOW - timedelta(hours=3)), encoding="utf-8")
    monkeypatch.chdir(tmp_path)
    assert main(["follow", "--quiet-minutes", "90"]) == 2  # 전략 없이 선만은 안 됨
    assert main(["events", "bot1"]) == 0
    out = capsys.readouterr().out.splitlines()
    assert out[1] == "이번에 읽은 줄 1 · 새 이벤트 1 · 형식 오류 0" and out[2] == "쌓인 이벤트: heartbeat 1 · 형식 오류 모두 0"
    assert out[3].startswith("빠진 종류: fill · position · equity · ")
    assert main(["follow", "bot1", "--quiet-minutes", "4"]) == 2  # 5분 아래는 예약이 못 지킴
    assert main(["follow", "bot1", "--quiet-minutes", "90"]) == 0
    calls = []
    monkeypatch.setattr("blackclip.schedule.install",
                        lambda repo, h, m, follow=False, daily=True: calls.append((follow, daily)) or ["걸었어요"])
    assert main(["schedule"]) == 0 and calls == [(True, False)]  # 선만 있고 저장한 가드레일 없음: follow만
    (tmp_path / ".blackclip" / "strategies" / "bot1" / "guardrail.v1.yaml").write_text("{}")
    assert main(["schedule"]) == 0 and calls[-1] == (True, True)
    monkeypatch.setattr(cli, "default_store", lambda: MemStore())
    capsys.readouterr()
    assert main(["follow"]) == 0
    assert capsys.readouterr().out.splitlines()[0].startswith("bot1: 안 보임 · 마지막 이벤트 ")


def test_follow_plist_and_install_remove(tmp_path):
    """follow면 세 번째 plist(5분마다 StartInterval, 로그 follow.log) · 지우기는 건 적 없는(3 · 파일 없음) follow 줄을 내지 않음."""
    d = plistlib.loads(follow_plist(tmp_path, "/venv/bin/blackclip"))
    assert d == {"Label": label(tmp_path, "follow"), "ProgramArguments": ["/venv/bin/blackclip", "follow"],
                 "WorkingDirectory": str(tmp_path), "StartInterval": FOLLOW_S,
                 "StandardOutPath": str(tmp_path / ".blackclip" / "follow.log"),
                 "StandardErrorPath": str(tmp_path / ".blackclip" / "follow.log")}
    calls, agents, repo = [], tmp_path / "LaunchAgents", tmp_path / "repo"
    lines = install(repo, 9, 10, agents, lambda a: calls.append(a) or 0, "darwin", "/venv/bin/blackclip", follow=True)
    assert calls[-1] == ["launchctl", "bootstrap", f"gui/{os.getuid()}", str(agents / f"{label(repo, 'follow')}.plist")]
    assert lines[2].startswith("5분마다 blackclip follow를 걸었어요")
    assert remove(repo, agents, lambda a: 0)[1][-1] == "키 없이 지켜보는 시계 예약을 지웠어요"
    assert remove(repo, agents, lambda a: 3) == (True, ["걸린 매일 대조 예약이 없어요", "걸린 자주 보는 시계 예약이 없어요"])
    calls.clear()
    lines = install(repo, 9, 10, agents, lambda a: calls.append(a) or (3 if a[1] == "bootout" else 0), "darwin",
                    "/venv/bin/blackclip", follow=True, daily=False)
    assert [a[-1] for a in calls if a[1] == "bootstrap"] == [str(agents / f"{label(repo, 'follow')}.plist")]
    assert lines[1:] == ["매일 대조 · 자주 보는 시계는 안 걸었어요 · 가드레일을 저장한 전략이 생기면 blackclip schedule을 다시",
                         "로그: .blackclip/follow.log · 끄기: blackclip schedule --remove"]
    cron = install(repo, 9, 10, platform="linux", exe="/b", follow=True, daily=False)
    assert cron[0] == f"이 운영체제는 자동으로 걸지 않아요 · crontab -e에 한 줄: */5 * * * * cd {repo} && /b follow >> .blackclip/follow.log 2>&1"
    assert cron[-1] == "crontab에 이미 넣은 daily · watch 줄이 있으면 지워요"


def test_follow_only_install_drops_daily_and_watch_from_before(tmp_path):
    """셋을 건 뒤 선만 남아 다시 걸면 매일 대조 · 자주 보는 시계를 내리고 plist를 지움(남으면 「저장된 전략이 없습니다」로 계속 돎)."""
    calls, agents, repo = [], tmp_path / "LaunchAgents", tmp_path / "repo"
    install(repo, 9, 10, agents, lambda a: 0, "darwin", "/b", follow=True)
    lines = install(repo, 9, 10, agents, lambda a: calls.append(a) or 0, "darwin", "/b", follow=True, daily=False)
    gone = {f"gui/{os.getuid()}/{label(repo, k)}" for k in ("daily", "watch")}
    assert gone <= {a[-1] for a in calls if a[1] == "bootout"}
    assert [k for k in ("daily", "watch", "follow") if (agents / f"{label(repo, k)}.plist").exists()] == ["follow"]
    assert lines[2:4] == ["매일 대조 예약을 지웠어요", "자주 보는 시계 예약을 지웠어요"]


def test_schedule_remove_does_not_read_strategy_folders(tmp_path, monkeypatch):
    """--remove는 전략 폴더를 읽지 않는다: strategies를 못 읽어도 예약을 지움(0.1.0과 같음)."""
    root, _ = setup_repo(tmp_path)
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr("blackclip.schedule.remove", lambda repo: (True, ["지웠어요"]))
    (root / "strategies").chmod(0)
    try:
        assert main(["schedule", "--remove"]) == 0
    finally:
        (root / "strategies").chmod(0o755)


ECHO = '''SOURCE = "bot/decisions.jsonl"
import sys
for line in sys.stdin:
    print(line.strip())
'''
"""봇 로그 줄을 그대로 이벤트로 내는 어댑터(모양이 틀린 이벤트를 시험하려고)."""


def test_odd_kind_and_out_of_range_time_are_bad_lines_and_follow_keeps_going(tmp_path):
    """kind가 문자열이 아니거나(리스트 · dict) 시각을 UTC로 못 바꾸면(0001년 +09:00) 형식 오류로 세고 follow는 그대로 돈다."""
    root, log = setup_repo(tmp_path, ECHO)
    log.write_text("".join(json.dumps(e) + "\n" for e in (
        {"kind": [], "time": "2026-10-23T02:00:00Z"}, {"kind": {}, "time": "2026-10-23T02:00:00Z"},
        {"kind": "heartbeat", "time": "0001-01-01T00:00:00+09:00"}, {"kind": "heartbeat", "time": "2026-10-23T02:50:00Z"})),
        encoding="utf-8")
    approve(root, "bot1", 90, NOW)
    code, lines = run_follow(root, MemStore(), lambda tok: None, NOW)
    assert (code, lines[0]) == (0, "bot1: 정상 · 마지막 이벤트 2026-10-23 02:50 UTC · 10분 전 · 선 90분(승인한 값)")
    st = json.loads((root / "strategies" / "bot1" / STATE).read_text())
    assert (st["bad"], st["counts"]) == (3, {"heartbeat": 1})


def test_big_first_log_is_read_to_the_end_in_one_check(tmp_path, monkeypatch):
    """처음 붙인 로그가 한 번에 넘기는 크기(READ_CAP)보다 커도 한 번의 확인에서 끝까지 따라잡고 그 마지막 시각으로 판정한다."""
    monkeypatch.setattr("blackclip.events.READ_CAP", 100)
    root, log = setup_repo(tmp_path)
    log.write_text("".join(line(NOW - timedelta(hours=h)) for h in range(30, 0, -1)) + line(NOW - timedelta(minutes=10)),
                   encoding="utf-8")
    approve(root, "bot1", 90, NOW)
    code, lines = run_follow(root, MemStore(), lambda tok: None, NOW)
    assert lines[0] == "bot1: 정상 · 마지막 이벤트 2026-10-23 02:50 UTC · 10분 전 · 선 90분(승인한 값)"
    st = json.loads((root / "strategies" / "bot1" / STATE).read_text())
    assert (st["offset"], st["counts"]) == (log.stat().st_size, {"heartbeat": 31})


def test_line_longer_than_read_cap_is_one_bad_line_and_skipped(tmp_path, monkeypatch):
    """줄바꿈 없이 READ_CAP을 넘는 줄은 형식 오류 하나로 세고 건너뛴다(읽은 자리가 멈추지 않음) · 아직 안 끝난 긴 줄은 끝날 때까지 기다림."""
    monkeypatch.setattr("blackclip.events.READ_CAP", 100)
    root, log = setup_repo(tmp_path)
    log.write_text("x" * 250 + "\n" + line(NOW - timedelta(minutes=5)), encoding="utf-8")
    got = ingest(root, "bot1", NOW)
    assert (got["new_bad"], got["new_events"], got["last"], got["offset"]) == (1, 1, "2026-10-23T02:55:00Z", log.stat().st_size)
    size = log.stat().st_size
    with open(log, "a", encoding="utf-8") as f:
        f.write("y" * 250)
    assert (ingest(root, "bot1", NOW)["new_bad"], json.loads((root / "strategies" / "bot1" / STATE).read_text())["offset"]) == (0, size)
    with open(log, "a", encoding="utf-8") as f:
        f.write("\n")
    assert ingest(root, "bot1", NOW)["new_bad"] == 1


def test_future_last_from_a_clock_that_went_back_is_dropped(tmp_path):
    """시계가 되돌아가 상태의 마지막 시각이 지금 + 10분보다 미래면 버린다(그 값이 「안 보임」을 영영 막지 않게) · 모양이 틀린 값도."""
    root, log = setup_repo(tmp_path)
    log.write_text(line(NOW + timedelta(days=1)), encoding="utf-8")
    assert ingest(root, "bot1", NOW + timedelta(days=1))["last"] == "2026-10-24T03:00:00Z"  # 시계가 하루 앞서 있던 때
    with open(log, "a", encoding="utf-8") as f:
        f.write(line(NOW - timedelta(hours=3)))
    assert ingest(root, "bot1", NOW)["last"] == "2026-10-23T00:00:00Z"
    approve(root, "bot1", 90, NOW)
    assert run_follow(root, MemStore(), lambda tok: None, NOW)[1][0].startswith("bot1: 안 보임 · ")
    p = root / "strategies" / "bot1" / STATE
    p.write_text(json.dumps({**json.loads(p.read_text()), "last": 5}))
    assert ingest(root, "bot1", NOW)["last"] is None
    p.write_text(json.dumps({**json.loads(p.read_text()), "counts": {"heartbeat": "x"}}))
    with pytest.raises(EventsError, match="모양이 다릅니다"):  # follow에서는 그 전략만 확인 불가(다른 전략은 그대로)
        ingest(root, "bot1", NOW)


def test_log_growing_while_reading_stops_at_the_start_size(tmp_path, monkeypatch):
    """읽는 사이 봇이 조각마다 READ_CAP 넘게 덧붙여도 한 번의 확인은 시작할 때 본 크기에서 돌아온다(follow 잠금을 계속 쥐지 않게).
    판정은 시작 시점까지의 마지막 시각, 덧붙은 줄은 다음 확인에서."""
    import blackclip.events as ev
    monkeypatch.setattr(ev, "READ_CAP", 60)
    root, log = setup_repo(tmp_path)
    log.write_text("".join(line(NOW - timedelta(minutes=m)) for m in (50, 40, 30, 20)), encoding="utf-8")
    start, real, calls = log.stat().st_size, ev._convert, []

    def growing(*args):
        calls.append(1)
        assert len(calls) < 20, "한 번의 확인이 돌아오지 않음"
        with open(log, "a", encoding="utf-8") as f:
            f.write(line(NOW - timedelta(minutes=5)) * 2)  # 30바이트 줄 둘 = READ_CAP
        return real(*args)

    monkeypatch.setattr(ev, "_convert", growing)
    got = ingest(root, "bot1", NOW)
    assert (got["offset"] <= start, got["new_events"], got["last"]) == (True, 4, "2026-10-23T02:40:00Z")
    monkeypatch.setattr(ev, "_convert", real)
    assert ingest(root, "bot1", NOW)["last"] == "2026-10-23T02:55:00Z"

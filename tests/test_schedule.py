"""blackclip schedule: launchd plist 둘(매일 대조 · 자주 보는 시계) · 올리기 · 지우기 · 다른 운영체제."""
import os
import plistlib

import pytest

from blackclip.cli import main
from blackclip.schedule import install, label, plist, remove, watch_minute, watch_plist


def test_plist_keys(tmp_path):
    """매일 대조: 실행 파일 절대 경로 + daily, 매일 시 · 분, 로그 daily.log. 자주 보는 시계: watch, 매시 M분(달력 예약), 로그 watch.log."""
    d = plistlib.loads(plist(tmp_path, "/venv/bin/blackclip", 9, 10))
    assert d == {"Label": label(tmp_path), "ProgramArguments": ["/venv/bin/blackclip", "daily"],
                 "WorkingDirectory": str(tmp_path), "StartCalendarInterval": {"Hour": 9, "Minute": 10},
                 "StandardOutPath": str(tmp_path / ".blackclip" / "daily.log"),
                 "StandardErrorPath": str(tmp_path / ".blackclip" / "daily.log")}
    assert label(tmp_path).startswith("com.blackclip.daily.") and len(label(tmp_path)) == len("com.blackclip.daily.") + 10
    w = plistlib.loads(watch_plist(tmp_path, "/venv/bin/blackclip", 40))
    assert w == {"Label": label(tmp_path, "watch"), "ProgramArguments": ["/venv/bin/blackclip", "watch"],
                 "WorkingDirectory": str(tmp_path), "StartCalendarInterval": {"Minute": 40},
                 "StandardOutPath": str(tmp_path / ".blackclip" / "watch.log"),
                 "StandardErrorPath": str(tmp_path / ".blackclip" / "watch.log")}
    assert label(tmp_path, "watch") == "com.blackclip.watch." + label(tmp_path)[len("com.blackclip.daily."):]
    assert [watch_minute(m) for m in (10, 0, 30, 45)] == [40, 30, 0, 15]  # 매일 대조 분과 겹치지 않음


def test_install_bootout_then_bootstrap_both_and_remove(tmp_path):
    """둘 다 먼저 내리고(없어도 됨) 쓰고 올림, 지우기는 둘 다 내리고 파일 삭제 · 예약마다 한 줄."""
    calls, agents = [], tmp_path / "LaunchAgents"
    repo = tmp_path / "repo"
    lines = install(repo, 9, 10, agents, lambda a: calls.append(a) or 0, "darwin", "/venv/bin/blackclip")
    dom = f"gui/{os.getuid()}"
    assert calls == [["launchctl", "bootout", f"{dom}/{label(repo)}"],
                     ["launchctl", "bootstrap", dom, str(agents / f"{label(repo)}.plist")],
                     ["launchctl", "bootout", f"{dom}/{label(repo, 'watch')}"],
                     ["launchctl", "bootstrap", dom, str(agents / f"{label(repo, 'watch')}.plist")]]
    assert lines[0].startswith("매일 09:10(이 컴퓨터 시간) blackclip daily를 걸었어요")
    assert lines[1] == ("매시 40분 blackclip watch를 걸었어요 · 전략별 계좌(--capital 없음)인 전략만 · "
                        "맥이 잠든 동안은 1시간 점검도 멈춤 · 깨어나면 한 번 돎")
    assert plistlib.loads((agents / f"{label(repo, 'watch')}.plist").read_bytes())["StartCalendarInterval"] == {"Minute": 40}
    assert remove(repo, agents, lambda a: 0) == (True, ["매일 대조 예약을 지웠어요", "자주 보는 시계 예약을 지웠어요"])
    assert remove(repo, agents, lambda a: 3) == (True, ["걸린 매일 대조 예약이 없어요", "걸린 자주 보는 시계 예약이 없어요"])


def test_remove_reports_what_launchctl_bootout_really_did(tmp_path):
    """bootout 종료 코드를 예약마다 본다: 0 지움, 3 = launchd에 없음(파일만 있었으면 파일만 지움), 그 밖(5)은 확인 불가 ·
    파일을 남김 · 실패로 돌려줌. 「없어요」는 파일도 없고 3일 때만."""
    repo, agents = tmp_path / "repo", tmp_path / "LaunchAgents"
    agents.mkdir()
    (agents / f"{label(repo)}.plist").write_bytes(b"x")
    (agents / f"{label(repo, 'watch')}.plist").write_bytes(b"x")
    rc = {label(repo): 3, label(repo, "watch"): 5}
    assert remove(repo, agents, lambda a: rc[a[2].rsplit("/", 1)[1]]) == (False, [
        "매일 대조 예약 파일을 지웠어요 · launchd에는 올라가 있지 않았어요(종료 3)",
        f"자주 보는 시계 예약 확인 불가(종료 5) · 확인: launchctl print gui/{os.getuid()}/{label(repo, 'watch')}"])
    assert [p.name for p in agents.iterdir()] == [f"{label(repo, 'watch')}.plist"]
    assert remove(repo, tmp_path / "none", lambda a: 5)[0] is False  # 파일이 없어도 3이 아니면 「없어요」라 하지 않음


def test_cli_remove_failure_exits_nonzero(tmp_path, monkeypatch, capsys):
    """blackclip schedule --remove: 지웠는지 확인 못 한 예약이 있으면(remove가 False) 화면 줄은 그대로 · 종료 2(종료 코드 규칙대로)."""
    (tmp_path / ".git").mkdir()
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr("blackclip.schedule.remove", lambda repo: (False, ["매일 대조 예약 확인 불가(종료 5) · 확인: …"]))
    assert main(["schedule", "--remove"]) == 2
    assert capsys.readouterr().out == "매일 대조 예약 확인 불가(종료 5) · 확인: …\n"


def test_install_bootstrap_failure_raises(tmp_path):
    """bootstrap이 실패하면 OSError(어느 plist인지)."""
    with pytest.raises(OSError, match="launchctl bootstrap이 실패했습니다\\(com.blackclip.watch."):
        install(tmp_path, 9, 10, tmp_path / "A", lambda a: 5 if a[1] == "bootstrap" and "watch" in a[3] else 0, "darwin",
                "/x/blackclip")


def test_other_os_prints_crontab_lines(tmp_path):
    """macOS가 아니면 설치하지 않고 crontab 두 줄."""
    lines = install(tmp_path, 0, 10, tmp_path / "A", lambda a: pytest.fail("설치하면 안 됨"), "linux", "/x/blackclip")
    assert lines == [f"이 운영체제는 자동으로 걸지 않아요 · crontab -e에 한 줄: 10 0 * * * cd {tmp_path} && /x/blackclip daily "
                     ">> .blackclip/daily.log 2>&1",
                     f"자주 보는 시계도 걸려면 한 줄 더: 40 * * * * cd {tmp_path} && /x/blackclip watch >> .blackclip/watch.log 2>&1"]

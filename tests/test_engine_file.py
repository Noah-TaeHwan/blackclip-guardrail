"""엔진 위치 파일: scan · setup · connect가 지금 실행 중인 진입점의 실제 경로를 사용자 설정 폴더에
적고(내 것 · 0600 · 링크 아닌 폴더), 훅 래퍼가 PATH 다음에 같은 조건일 때만 읽음."""
import os
import stat
import subprocess
import sys
from pathlib import Path

from conftest import MemStore

import blackclip.cli as cli
import blackclip.schedule as schedule
from blackclip.cli import main
from test_plugin import WRAPPER, _bin


def _argv0(monkeypatch, path):
    """지금 실행 중인 진입점을 path로(콘솔 스크립트가 받는 sys.argv[0])."""
    monkeypatch.setattr(sys, "argv", [str(path), "setup"])


def test_remember_engine_writes_once_0600(tmp_path, monkeypatch):
    """절대 경로 한 줄 · 0600 · 같은 값이면 다시 쓰지 않음 · 링크 자리는 링크를 바꿔치기(너머 파일 그대로)."""
    exe = _bin(tmp_path) / "blackclip"
    _argv0(monkeypatch, exe)
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "xdg"))
    path = schedule.remember_engine()
    assert path == tmp_path / "xdg" / "blackclip" / "engine" and path.read_text() == f"{exe}\n"
    assert stat.S_IMODE(path.stat().st_mode) == 0o600 and stat.S_IMODE(path.parent.stat().st_mode) == 0o700
    ino = path.stat().st_ino
    assert schedule.remember_engine() == path and path.stat().st_ino == ino
    outside = tmp_path / "outside"
    outside.write_text("keep\n")
    path.unlink()
    path.symlink_to(outside)
    assert schedule.remember_engine() == path and not path.is_symlink() and outside.read_text() == "keep\n"


def test_same_content_with_loose_mode_is_rewritten(tmp_path, monkeypatch):
    """내용이 같아도 0644 · 0666이면 0600으로 다시 씀(고치기 전에는 같은 내용이면 그대로 둬 남이 바꿀 수 있는 권한이 남음)."""
    exe = _bin(tmp_path) / "blackclip"
    _argv0(monkeypatch, exe)
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "xdg"))
    path = schedule.remember_engine()
    for loose in (0o644, 0o666):
        path.chmod(loose)
        assert schedule.remember_engine() == path and stat.S_IMODE(path.stat().st_mode) == 0o600


def test_records_the_running_entry_point_not_another_install(tmp_path, monkeypatch):
    """같은 파이썬 옆 · PATH에 다른 blackclip이 있어도 지금 실행 중인 진입점을 적음(고치기 전에는 sys.executable 옆을 적음).

    진입점이 상대 경로 · 이름이 blackclip 아님(pytest · python -m)이면 적지 않음."""
    for d in ("other", "running"):
        (tmp_path / d).mkdir()
    other, running = _bin(tmp_path / "other"), _bin(tmp_path / "running")
    monkeypatch.setattr(sys, "executable", str(other / "python"))
    monkeypatch.setenv("PATH", f"{other}:/usr/bin:/bin")
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "xdg"))
    _argv0(monkeypatch, running / "blackclip")
    assert schedule.remember_engine().read_text() == f"{running / 'blackclip'}\n"
    monkeypatch.chdir(running)
    for arg in ("./blackclip", "blackclip", str(tmp_path / "pytest"), ""):
        monkeypatch.setattr(sys, "argv", [arg])
        assert schedule.remember_engine() is None


def test_remember_engine_refuses_odd_places(tmp_path, monkeypatch):
    """설정 폴더가 링크 · 그룹이 쓸 수 있음 · 상대 XDG_CONFIG_HOME이면 아무것도 안 적고 None(고치기 전에는 링크 너머에 씀).

    실행 안 되는 진입점도 None. 다른 사용자 소유는 root 없이 못 만들어 시험하지 않음(검사 코드는 같은 줄)."""
    exe = _bin(tmp_path) / "blackclip"
    _argv0(monkeypatch, exe)
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()
    (tmp_path / "xdg").mkdir()
    (tmp_path / "xdg" / "blackclip").symlink_to(elsewhere, target_is_directory=True)
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "xdg"))
    assert schedule.remember_engine() is None and list(elsewhere.iterdir()) == []
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "xdg2"))
    (tmp_path / "xdg2" / "blackclip").mkdir(parents=True)
    (tmp_path / "xdg2" / "blackclip").chmod(0o770)
    assert schedule.remember_engine() is None and not (tmp_path / "xdg2" / "blackclip" / "engine").exists()
    monkeypatch.setenv("XDG_CONFIG_HOME", "rel")
    monkeypatch.chdir(tmp_path)
    assert schedule.remember_engine() is None and not (tmp_path / "rel").exists()
    plain = tmp_path / "p" / "blackclip"
    plain.parent.mkdir()
    plain.write_text("")
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "xdg3"))
    _argv0(monkeypatch, plain)
    assert schedule.remember_engine() is None and not (tmp_path / "xdg3").exists()


def test_scan_setup_connect_remember_but_others_do_not(tmp_path, monkeypatch, capsys):
    """scan · setup · connect(--status)는 사용자 설정 폴더의 engine 파일을 적고(setup은 레포에 아무것도 안 만듦), notify는 안 적음."""
    exe = _bin(tmp_path) / "blackclip"
    _argv0(monkeypatch, exe)
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "xdg"))
    monkeypatch.setattr(cli, "default_store", MemStore)  # connect --status가 진짜 키체인을 안 부르게
    repo = tmp_path / "repo"
    (repo / ".git").mkdir(parents=True)
    monkeypatch.chdir(repo)
    engine = tmp_path / "xdg" / "blackclip" / "engine"
    assert main(["notify"]) == 0 and not engine.exists()
    for argv in (["scan"], ["setup"], ["connect", "--status"]):
        engine.unlink(missing_ok=True)
        assert main(argv) == 0 and engine.read_text() == f"{exe}\n", argv


def test_remember_engine_never_raises_without_a_home(tmp_path, monkeypatch):
    """HOME도 passwd 항목도 없어 Path.home()이 RuntimeError를 내도 remember_engine은 예외 없이 None."""
    exe = _bin(tmp_path) / "blackclip"
    _argv0(monkeypatch, exe)
    monkeypatch.delenv("XDG_CONFIG_HOME", raising=False)

    def no_home(cls):
        raise RuntimeError("Could not determine home directory.")

    monkeypatch.setattr(Path, "home", classmethod(no_home))
    assert schedule.remember_engine() is None


def _hook(event, cwd, env):
    """래퍼를 정한 환경으로 한 번."""
    return subprocess.run([str(WRAPPER), event], input=b"{}", capture_output=True, cwd=cwd, timeout=30, env=env)


def test_wrapper_runs_the_engine_named_in_the_engine_file(tmp_path):
    """레포 .venv 엔진: engine 파일이 없으면 안 돌리고, 고객 엔진이 적은 engine 파일이 가리키면 돌림. PATH가 먼저."""
    repo, home = tmp_path / "repo", tmp_path / "home"
    (repo / ".venv").mkdir(parents=True)
    home.mkdir()
    marker = tmp_path / "ran"
    exe = _bin(repo / ".venv", f'#!/bin/sh\ncat >/dev/null\necho "$@" >> "{marker}"\n') / "blackclip"
    env = {"PATH": "/usr/bin:/bin", "HOME": str(home), "CLAUDE_PROJECT_DIR": str(repo)}
    assert _hook("PreToolUse", repo, env).returncode == 0 and not marker.exists()
    (home / ".config" / "blackclip").mkdir(parents=True)
    (home / ".config" / "blackclip" / "engine").write_text(f"{exe}\n")
    assert _hook("PreToolUse", repo, env).returncode == 0 and marker.read_text() == "hook PreToolUse\n"
    xdg = tmp_path / "xdg" / "blackclip"
    xdg.mkdir(parents=True)
    (xdg / "engine").write_text(f"{exe}")  # 줄바꿈 없어도 읽음
    assert _hook("SessionStart", repo, {**env, "XDG_CONFIG_HOME": str(tmp_path / "xdg")}).returncode == 0
    assert marker.read_text().splitlines()[-1] == "hook SessionStart"
    (tmp_path / "p").mkdir()
    first = _bin(tmp_path / "p", f'#!/bin/sh\ncat >/dev/null\necho path >> "{marker}"\n')
    _hook("PreToolUse", repo, {**env, "PATH": f"{first}:/usr/bin:/bin"})
    assert marker.read_text().splitlines()[-1] == "path"


def test_wrapper_refuses_odd_engine_files(tmp_path):
    """engine 파일이 링크 · 안에 상대 경로 · 가리키는 것이 실행 안 됨 · 폴더 · 그룹/남 쓰기 · 설정 폴더가 링크 · 그룹 쓰기 ·
    상대 XDG_CONFIG_HOME이면 안 돌림(못 찾았어요)."""
    repo, home = tmp_path / "repo", tmp_path / "home"
    repo.mkdir()
    marker = tmp_path / "ran"
    (tmp_path / "e").mkdir()
    exe = _bin(tmp_path / "e", f'#!/bin/sh\ncat >/dev/null\ntouch "{marker}"\n') / "blackclip"
    conf = home / ".config" / "blackclip"
    conf.mkdir(parents=True)
    real = tmp_path / "real-engine-file"
    real.write_text(f"{exe}\n")
    plain = tmp_path / "plain"
    plain.write_text("#!/bin/sh\n")
    env = {"PATH": "/usr/bin:/bin", "HOME": str(home)}
    cases = [("link", None), ("relative", "e/bin/blackclip\n"), ("not-exec", f"{plain}\n"), ("dir", f"{tmp_path / 'e'}\n")]
    for name, text in cases:
        (conf / "engine").unlink(missing_ok=True)
        if text is None:
            (conf / "engine").symlink_to(real)
        else:
            (conf / "engine").write_text(text)
        r = _hook("SessionStart", tmp_path, env)
        assert r.returncode == 0 and "못 찾았어요" in r.stdout.decode() and not marker.exists(), name
    (conf / "engine").write_text(f"{exe}\n")
    for mode in (0o666, 0o620):  # 그룹 · 남이 쓸 수 있는 engine 파일(고치기 전에는 읽음)
        (conf / "engine").chmod(mode)
        assert "못 찾았어요" in _hook("SessionStart", tmp_path, env).stdout.decode() and not marker.exists(), oct(mode)
    (conf / "engine").chmod(0o600)
    conf.chmod(0o775)  # 그룹이 쓸 수 있는 설정 폴더
    assert "못 찾았어요" in _hook("SessionStart", tmp_path, env).stdout.decode() and not marker.exists()
    conf.chmod(0o700)
    assert _hook("SessionStart", tmp_path, env).returncode == 0 and marker.exists()  # 조건이 맞으면 돎(대조)
    marker.unlink()
    moved = home / "real-conf"
    conf.rename(moved)
    conf.symlink_to(moved, target_is_directory=True)  # 설정 폴더가 링크(고치기 전에는 따라감)
    assert "못 찾았어요" in _hook("SessionStart", tmp_path, env).stdout.decode() and not marker.exists()
    conf.unlink()
    moved.rename(conf)
    (conf / "engine").unlink()
    planted = repo / "cfg" / "blackclip"
    planted.mkdir(parents=True)
    (planted / "engine").write_text(f"{exe}\n")
    r = _hook("SessionStart", repo, {**env, "XDG_CONFIG_HOME": "cfg"})
    assert "못 찾았어요" in r.stdout.decode() and not marker.exists()
    assert os.access(exe, os.X_OK)


def test_wrapper_ignores_a_relative_home(tmp_path):
    """HOME이 상대 경로(.)면 engine 파일 자리도 ~/.local/bin 자리도 안 읽음(연 레포가 .config · .local을 심어도).

    대조: 같은 심기를 HOME 절대 경로로 돌리면 돎(심기가 먹히는 모양이라는 것). 시험 HOME은 늘 임시 폴더."""
    repo = tmp_path / "repo"
    repo.mkdir()
    marker = tmp_path / "ran"
    (tmp_path / "e").mkdir()
    exe = _bin(tmp_path / "e", f'#!/bin/sh\ncat >/dev/null\ntouch "{marker}"\n') / "blackclip"
    conf = repo / ".config" / "blackclip"
    conf.mkdir(parents=True)
    (conf / "engine").write_text(f"{exe}\n")  # git checkout 모양: 내 것 · 0644 · 폴더 0755
    env = {"PATH": "/usr/bin:/bin", "CLAUDE_PROJECT_DIR": str(repo)}
    r = _hook("SessionStart", repo, {**env, "HOME": "."})
    assert r.returncode == 0 and "못 찾았어요" in r.stdout.decode() and not marker.exists(), "engine"
    assert _hook("SessionStart", repo, {**env, "HOME": str(repo)}).returncode == 0 and marker.exists()
    marker.unlink()
    (conf / "engine").unlink()
    (repo / ".local").mkdir()
    _bin(repo / ".local", f'#!/bin/sh\ncat >/dev/null\ntouch "{marker}"\n')
    r = _hook("SessionStart", repo, {**env, "HOME": "."})
    assert r.returncode == 0 and "못 찾았어요" in r.stdout.decode() and not marker.exists(), ".local/bin"
    assert _hook("SessionStart", repo, {**env, "HOME": str(repo)}).returncode == 0 and marker.exists()


def test_wrapper_refuses_a_writable_or_linked_engine_target(tmp_path):
    """engine 파일이 가리키는 실행 파일이 그룹 · 남이 쓸 수 있거나(0777 · 0775 · 0757) 링크면 안 돌림.

    다른 사용자 소유는 root 없이 못 만들어 시험하지 않음(검사 코드는 같은 줄). 대조: 내 것 0755는 돎. 시험 HOME은 임시 폴더."""
    home, marker = tmp_path / "home", tmp_path / "ran"
    (tmp_path / "e").mkdir()
    exe = _bin(tmp_path / "e", f'#!/bin/sh\ncat >/dev/null\ntouch "{marker}"\n') / "blackclip"
    conf = home / ".config" / "blackclip"
    conf.mkdir(parents=True)
    (conf / "engine").write_text(f"{exe}\n")
    env = {"PATH": "/usr/bin:/bin", "HOME": str(home)}
    for mode in (0o777, 0o775, 0o757):
        exe.chmod(mode)
        assert "못 찾았어요" in _hook("SessionStart", tmp_path, env).stdout.decode() and not marker.exists(), oct(mode)
    exe.chmod(0o755)
    link = tmp_path / "link-to-blackclip"
    link.symlink_to(exe)
    (conf / "engine").write_text(f"{link}\n")
    assert "못 찾았어요" in _hook("SessionStart", tmp_path, env).stdout.decode() and not marker.exists(), "link"
    (conf / "engine").write_text(f"{exe}\n")
    assert _hook("SessionStart", tmp_path, env).returncode == 0 and marker.exists()


def test_remember_engine_writes_the_resolved_path_of_a_linked_entry_point(tmp_path, monkeypatch):
    """진입점이 링크(예: ~/.local/bin/blackclip → 가상환경)면 링크를 풀어 일반 파일 경로를 적음."""
    (tmp_path / "real").mkdir()
    real = _bin(tmp_path / "real")
    (tmp_path / "bin").mkdir()
    (tmp_path / "bin" / "blackclip").symlink_to(real / "blackclip")
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "xdg"))
    _argv0(monkeypatch, tmp_path / "bin" / "blackclip")
    path = schedule.remember_engine()
    assert path.read_text() == f"{os.path.realpath(real / 'blackclip')}\n" and not path.is_symlink()

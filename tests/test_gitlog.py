"""git 커밋 되살리기: 파일마다 --follow · 가지를 안 섞음 · 남의 레포 설정이 아무 프로그램도
못 띄움 · 상한 · 못 세면 이유."""
import os
import subprocess
import time
from pathlib import Path

import pytest
from conftest import GIT_ENV, commit, git

from blackclip import gitlog, hook


@pytest.fixture
def repo(tmp_path):
    """a.py 2커밋 → b.py로 이름 바꿈 → b.py 1커밋 · notes.txt · other.py 1커밋, 그리고 실험 가지에 b.py 1커밋."""
    r = tmp_path / "repo"
    r.mkdir()
    git(r, "init", "-q", "-b", "main")
    commit(r, "one", {"a.py": "import pandas\n"})
    commit(r, "two", {"a.py": "import pandas\nx = 1\n"})
    git(r, "mv", "a.py", "b.py")
    git(r, "commit", "-q", "-m", "rename")
    commit(r, "three", {"b.py": "import pandas\nx = 2\n", "notes.txt": "n\n", "other.py": "y = 1\n"})
    git(r, "checkout", "-q", "-b", "exp")
    commit(r, "try", {"b.py": "import pandas\nx = 3\n"})
    git(r, "checkout", "-q", "main")
    return r


def _msgs(repo, commits):
    """커밋 해시 → 커밋 글 집합(시험 읽기용)."""
    out = subprocess.run(["/usr/bin/git", "log", "--all", "--format=%H %s"], cwd=repo, env=GIT_ENV, capture_output=True,
                         text=True, check=True).stdout
    names = dict(line.split(" ", 1) for line in out.splitlines())
    return {names[c] for c in commits}


def test_counts_follow_renames_and_branches(repo):
    """b.py = 옛 이름 a.py 시절 2 + 이름 바꿈 1 + 3 + 실험 가지 1 = 5커밋, other.py 1, 없는 파일은 빈 집합."""
    touched, state = gitlog.history(repo, ["b.py", "other.py", "missing.py"])
    assert state == "ok" and {p: len(c) for p, c in touched.items()} == {"b.py": 5, "other.py": 1, "missing.py": 0}
    assert gitlog.count(touched, ["b.py", "other.py"]) == 5


def test_rename_on_one_branch_does_not_move_the_other(tmp_path):
    """main에는 a.py가 남고 실험 가지만 a.py → b.py. main 줄기의 a.py 고침이 b.py로 가지 않고, 실험 가지 고침이 a.py로 오지 않음.

    고치기 전(한 별명 표)에는 a.py 1(m3만) · b.py 4(one · two · ren · e1)로 섞었다(main의 one · two가 실험 가지 이름으로 감)."""
    r = tmp_path / "r"
    r.mkdir()
    git(r, "init", "-q", "-b", "main")
    commit(r, "one", {"a.py": "1\n"})
    commit(r, "two", {"a.py": "1\n2\n"})
    git(r, "checkout", "-q", "-b", "exp")
    git(r, "mv", "a.py", "b.py")
    git(r, "commit", "-q", "-m", "ren")
    commit(r, "e1", {"b.py": "1\n2\n3\n"})
    git(r, "checkout", "-q", "main")
    commit(r, "m3", {"a.py": "1\n2\n5\n"})
    touched, state = gitlog.history(r, ["a.py"])
    assert state == "ok" and _msgs(r, touched["a.py"]) == {"one", "two", "m3", "ren"}  # ren = 실험 가지가 a.py를 건드린 시도
    git(r, "checkout", "-q", "exp")
    touched, _ = gitlog.history(r, ["b.py"])
    assert _msgs(r, touched["b.py"]) == {"one", "two", "ren", "e1"}  # main의 m3는 안 섞임


def test_file_names_are_literal_pathspecs(tmp_path):
    """`s[1].py` · `s?.py` 이름이 glob으로 읽혀 `s1.py` · `sa.py` 커밋을 세지 않음(2판은 셋 · 셋을 셈)."""
    r = tmp_path / "r"
    r.mkdir()
    git(r, "init", "-q", "-b", "main")
    commit(r, "lit", {"s[1].py": "1\n", "s?.py": "1\n"})
    for i in range(2):
        commit(r, f"x{i}", {"s1.py": f"{i}\n", "sa.py": f"{i}\n"})
    touched, state = gitlog.history(r, ["s[1].py", "s?.py", "s1.py"])
    assert state == "ok" and {p: _msgs(r, c) for p, c in touched.items()} == {
        "s[1].py": {"lit"}, "s?.py": {"lit"}, "s1.py": {"x0", "x1"}}


def test_reused_path_stops_at_creation_and_detached_head_counts(tmp_path):
    """지운 뒤 같은 이름으로 새로 만든 파일은 새 파일 커밋만(고치기 전에는 5 · 지운 커밋까지), 가지 없는 detached HEAD도 셈(고치기 전에는 0)."""
    r = tmp_path / "r"
    r.mkdir()
    git(r, "init", "-q", "-b", "main")
    commit(r, "o1", {"a.py": "old\n"})
    commit(r, "o2", {"a.py": "old\nold2\n"})
    commit(r, "del", {"a.py": None, "z.txt": "z\n"})
    commit(r, "n1", {"a.py": "brand\nnew\nfile\n"})
    commit(r, "n2", {"a.py": "brand\nnew\nfile\nmore\n"})
    touched, state = gitlog.history(r, ["a.py"])
    assert state == "ok" and _msgs(r, touched["a.py"]) == {"n1", "n2"}
    git(r, "checkout", "-q", "--detach")
    git(r, "branch", "-q", "-D", "main")
    touched, state = gitlog.history(r, ["a.py"])
    assert state == "ok" and _msgs(r, touched["a.py"]) == {"n1", "n2"}


def test_parse_stops_at_creation_and_reads_sha256():
    """A에서 멈추고 그 커밋을 만든 커밋으로 · 64자 해시도 커밋 · A가 없으면 만든 커밋 모름."""
    h1, h2, h3 = "a" * 64, "b" * 40, "c" * 40
    out = f"\0{h1}\0\nM\0x.py\0\0{h2}\0\nR100\0w.py\0x.py\0\0{h3}\0\nA\0w.py\0\0{'d' * 40}\0\nM\0w.py\0"
    assert gitlog.parse(out) == ({h1, h2, h3}, h3)
    assert gitlog.parse(f"\0{h1}\0\nM\0x.py\0") == ({h1}, None)


def test_parse_reads_paths_by_position_not_by_shape():
    """레포가 정한 경로 이름 「A」 · 40자 16진수를 상태 글자 · 커밋으로 읽지 않음(최솟값 약속)."""
    h1, h2, h3, fake = "a" * 40, "b" * 40, "c" * 40, "e" * 40
    out = f"\0{h1}\0\nR100\0A\0x.py\0\0{h2}\0\nM\0{fake}\0\0{h3}\0\nA\0w.py\0"
    assert gitlog.parse(out) == ({h1, h2, h3}, h3)


def test_broken_dot_git_does_not_count_the_parent_repo(tmp_path):
    """하위 폴더의 .git이 비었으면 git이 부모 레포로 올라가 그 숫자를 「ok」로 내지 않음."""
    parent = tmp_path / "home"
    parent.mkdir()
    git(parent, "init", "-q", "-b", "main")
    commit(parent, "one", {"child/x.py": "x = 1\n"})
    commit(parent, "two", {"child/x.py": "x = 2\n"})
    (parent / "child" / ".git").mkdir()
    touched, state = gitlog.history(parent / "child", ["x.py"])
    assert touched == {} and state.startswith("git 실패(종료 ")


def test_caps_give_partial_state(repo, monkeypatch):
    """출력 바이트 상한 · 파일 수 상한 · 시간 상한에 닿으면 정상 숫자 대신 불완전 상태와 거기까지 센 것."""
    monkeypatch.setattr(gitlog, "MAX_OUT", 10)
    assert gitlog.history(repo, ["b.py"]) == ({}, "출력 상한")
    monkeypatch.setattr(gitlog, "MAX_OUT", 4 << 20)
    monkeypatch.setattr(gitlog, "MAX_FILES", 1)
    touched, state = gitlog.history(repo, ["b.py", "other.py"])
    assert state == "파일 수 상한(1개까지만 셈)" and list(touched) == ["b.py"]
    assert gitlog.history(repo, ["b.py"], timeout=0) == ({}, "시간 초과")


def test_untrusted_repo_config_runs_nothing(repo, tmp_path, monkeypatch):
    """레포 .git/config · .gitattributes · 물려받은 GIT_* · PATH의 . 에 심은 프로그램이 하나도 돌지 않고 숫자는 그대로.

    대조: 같은 레포에서 무력화 없이 `git log -p`를 돌리면 textconv가 표지를 만든다(심은 설정이 살아 있음)."""
    marker = tmp_path / "ran"
    evil = tmp_path / "evil.sh"
    evil.write_text(f'#!/bin/sh\ntouch "{marker}"\ncat "$1" 2>/dev/null\nexit 0\n')
    evil.chmod(0o755)
    for key, value in (("core.fsmonitor", evil), ("core.pager", evil), ("pager.log", evil), ("diff.external", evil),
                       ("diff.evil.textconv", evil), ("diff.evil.command", evil), ("log.showSignature", "true"),
                       ("gpg.program", evil), ("log.showRoot", "false"), ("core.hooksPath", tmp_path)):
        git(repo, "config", key, str(value))
    (repo / ".gitattributes").write_text("*.py diff=evil\n")
    (repo / "git").write_text(evil.read_text())
    (repo / "git").chmod(0o755)
    monkeypatch.chdir(repo)
    monkeypatch.setenv("PATH", f".:{os.environ['PATH']}")
    for k in ("GIT_EXTERNAL_DIFF", "GIT_PAGER", "PAGER", "GIT_SSH_COMMAND"):
        monkeypatch.setenv(k, str(evil))
    monkeypatch.setenv("GIT_DIR", str(tmp_path / "nowhere"))
    touched, state = gitlog.history(repo, ["b.py", "other.py"])
    assert not marker.exists() and state == "ok"
    assert {p: len(c) for p, c in touched.items()} == {"b.py": 5, "other.py": 1}  # showRoot=false여도 첫 커밋을 셈
    subprocess.run(["/usr/bin/git", "log", "-p", "-1", "--", "b.py"], cwd=repo, env=GIT_ENV, capture_output=True)
    assert marker.exists()


def test_reasons_when_it_cannot_count(tmp_path, monkeypatch):
    """레포 아님 · 고정 PATH에 git 없음 · git 실패는 숫자 대신 이유."""
    assert gitlog.history(tmp_path, ["a.py"]) == ({}, "git 레포 아님")
    (tmp_path / ".git").write_text("gitdir: /nowhere\n")
    touched, state = gitlog.history(tmp_path, ["a.py"])
    assert touched == {} and state.startswith("git 실패(종료 ")
    monkeypatch.setattr(hook, "TRUSTED_PATH", str(tmp_path))
    assert gitlog.history(tmp_path, ["a.py"]) == ({}, "git 없음")


def test_env_drops_inherited_git_variables(monkeypatch):
    """물려받은 GIT_* 는 빼고 전역 · 시스템 설정을 막는 값만."""
    monkeypatch.setenv("GIT_EXTERNAL_DIFF", "x")
    monkeypatch.setenv("GIT_CONFIG_COUNT", "1")
    e = gitlog.env()
    assert "GIT_EXTERNAL_DIFF" not in e and "GIT_CONFIG_COUNT" not in e
    assert (e["GIT_CONFIG_GLOBAL"], e["GIT_CONFIG_NOSYSTEM"], e["GIT_PAGER"], e["PATH"]) == ("/dev/null", "1", "cat", hook.TRUSTED_PATH)
    for cmd in (gitlog.FOLLOW, gitlog.OTHERS):
        assert cmd[0] == "log" and {"--no-ext-diff", "--no-textconv"} <= set(cmd)
    # 표지 시험이 못 잡는 무력화 옵션이 빠지는 회귀를 여기서 잡음
    assert {"--no-pager", "--no-lazy-fetch", "--literal-pathspecs", "core.fsmonitor=false",
            "log.showSignature=false", "log.mailmap=false"} <= set(gitlog.SAFE)
    assert "GIT_CEILING_DIRECTORIES" not in e
    assert gitlog.env(Path("/a/b"))["GIT_CEILING_DIRECTORIES"] == str(Path("/a/b").resolve().parent)


def test_mailmap_fifo_in_repo_config_does_not_stall_history(repo, tmp_path):
    """.git/config의 mailmap.file이 FIFO여도 git이 열어 막히지 않음(log.mailmap=false) — 10초 마감 한참 전에 끝나 숫자 그대로."""
    fifo = tmp_path / "mailmap.fifo"
    os.mkfifo(fifo)
    git(repo, "config", "mailmap.file", str(fifo))
    t0 = time.monotonic()
    touched, state = gitlog.history(repo, ["b.py"])
    assert state == "ok" and len(touched["b.py"]) == 5
    assert time.monotonic() - t0 < 5

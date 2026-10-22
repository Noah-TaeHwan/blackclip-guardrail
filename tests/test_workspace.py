"""작업 폴더: Git 레포 꼭대기에 만들기 · 자기 .gitignore · 연결 상태 파일."""
import shutil
import subprocess

import pytest

from blackclip.workspace import (GITIGNORE, WorkspaceError, account_key, ensure, fills_dir, find_root,
                                 load_connections, save_connections, versions)


def test_ensure_creates_at_git_top(tmp_path):
    """레포 안 아래 폴더에서 불러도 레포 꼭대기에 만든다."""
    (tmp_path / ".git").mkdir()
    sub = tmp_path / "a" / "b"
    sub.mkdir(parents=True)
    root = ensure(sub)
    assert root == (tmp_path / ".blackclip").resolve()
    assert (root / ".gitignore").read_text(encoding="utf-8") == GITIGNORE


def test_ensure_keeps_custom_gitignore(tmp_path):
    """고객이 고친 .gitignore는 덮어쓰지 않는다."""
    root = ensure(tmp_path)
    (root / ".gitignore").write_text("custom\n", encoding="utf-8")
    assert ensure(tmp_path) == root
    assert (root / ".gitignore").read_text(encoding="utf-8") == "custom\n"


def test_find_root_stops_at_git_top(tmp_path):
    """레포 밖(위)의 .blackclip은 쓰지 않는다."""
    (tmp_path / ".blackclip").mkdir()
    repo = tmp_path / "repo"
    (repo / ".git").mkdir(parents=True)
    assert find_root(repo) is None


@pytest.mark.skipif(shutil.which("git") is None, reason="git 없음")
def test_gitignore_keeps_only_strategy_records(tmp_path):
    """전략 기록 세 가지만 Git에 남고 체결 · 연결 상태 · 그 밖은 빠진다."""
    subprocess.run(["git", "init", "-q", str(tmp_path)], check=True)
    root = ensure(tmp_path)
    s = root / "strategies" / "trend"
    s.mkdir(parents=True)
    for name in ("check.md", "guardrail.v1.yaml", "record.md", "daily.csv"):
        (s / name).write_text("x", encoding="utf-8")
    f = fills_dir(root, "binance", "main", "spot")
    (f / "fills.csv").write_text("x", encoding="utf-8")
    (root / "connections.json").write_text("{}", encoding="utf-8")

    def ignored(p):
        return subprocess.run(["git", "-C", str(tmp_path), "check-ignore", "-q", str(p)]).returncode == 0

    assert not any(ignored(s / n) for n in ("check.md", "guardrail.v1.yaml", "record.md"))
    assert ignored(s / "daily.csv") and ignored(f / "fills.csv") and ignored(root / "connections.json")


def test_account_key():
    """계정 이름은 소문자 · 숫자 · 밑줄. 테스트넷은 꼬리를 붙여 실계좌와 섞이지 않게 한다."""
    assert account_key("main", False) == "main"
    assert account_key("sub_1", True) == "sub_1-testnet"
    with pytest.raises(WorkspaceError, match="--account"):
        account_key("Main!", False)
    with pytest.raises(WorkspaceError, match="--account"):
        account_key("main\n", False)


def test_connections_roundtrip(tmp_path):
    """연결 상태는 JSON으로 쓰고 읽는다. 임시 파일이 남지 않는다."""
    root = ensure(tmp_path)
    assert load_connections(root) == {}
    save_connections(root, {"binance": {"main": {"symbols": {"spot": ["BTCUSDT"]}}}})
    assert load_connections(root)["binance"]["main"]["symbols"]["spot"] == ["BTCUSDT"]
    assert not list(root.glob("*.tmp"))


def test_broken_connections_named(tmp_path):
    """깨진 연결 상태 파일은 파일 이름을 대고 멈춘다."""
    root = ensure(tmp_path)
    (root / "connections.json").write_text("{", encoding="utf-8")
    with pytest.raises(WorkspaceError, match="connections.json"):
        load_connections(root)


@pytest.mark.parametrize("raw", [b"[]", b'"x"', "{}".encode("utf-16")])
def test_connections_not_object_or_not_utf8_named(tmp_path, raw):
    """연결 상태 파일 최상위가 객체가 아니거나 UTF-8이 아니면 파일 이름을 대고 멈춘다."""
    root = ensure(tmp_path)
    (root / "connections.json").write_bytes(raw)
    with pytest.raises(WorkspaceError, match="connections.json"):
        load_connections(root)


def test_ensure_upgrades_old_gitignore(tmp_path):
    """우리가 쓴 옛 .gitignore는 새 내용으로, 고객이 머리줄을 지운 파일은 그대로."""
    from blackclip.workspace import GITIGNORE, ensure

    (tmp_path / ".git").mkdir()
    gi = tmp_path / ".blackclip" / ".gitignore"
    gi.parent.mkdir()
    gi.write_text("# blackclip이 만든 파일. 전략 기록 세 가지만 커밋하고 나머지(체결 · 연결 상태 · 이력)는 Git에서 뺀다.\n*\n",
                  encoding="utf-8")
    ensure(tmp_path)
    assert gi.read_text(encoding="utf-8") == GITIGNORE
    gi.write_text("*\n", encoding="utf-8")
    ensure(tmp_path)
    assert gi.read_text(encoding="utf-8") == "*\n"


def test_write_atomic_unique_tmp_and_cleanup(tmp_path, monkeypatch):
    """임시 파일 이름이 고정이 아니고(동시 쓰기), 실패하면 임시 파일을 남기지 않는다."""
    import os

    from blackclip.workspace import write_atomic

    target = tmp_path / "a.json"
    write_atomic(target, "1")
    assert target.read_text(encoding="utf-8") == "1"
    assert not (tmp_path / "a.json.tmp").exists()

    def boom(src, dst):
        raise OSError("disk")

    monkeypatch.setattr(os, "replace", boom)
    try:
        write_atomic(target, "2")
    except OSError:
        pass
    assert sorted(p.name for p in tmp_path.iterdir()) == ["a.json"]


def test_lock_blocks_second_holder(tmp_path):
    """같은 이름 잠금을 두 번 잡으면 두 번째가 한 줄로 멈춘다."""
    import pytest

    from blackclip.workspace import WorkspaceError, lock

    with lock(tmp_path, "fetch"):
        with pytest.raises(WorkspaceError, match="다른 blackclip fetch"):
            with lock(tmp_path, "fetch"):
                pass
    with lock(tmp_path, "fetch"):
        pass


def test_lock_waits_then_gives_up_or_gets_it(tmp_path):
    """wait_s: 잡혀 있으면 1초씩 다시 해 보고, 그 사이 풀리면 잡고, 끝까지 잡혀 있으면 LockBusy(WorkspaceError)."""
    import fcntl

    import pytest

    from blackclip.workspace import LockBusy, WorkspaceError, lock

    naps = []
    with lock(tmp_path, "daily"):
        with pytest.raises(LockBusy, match="다른 blackclip daily"):
            with lock(tmp_path, "daily", wait_s=3, sleep=naps.append):
                pass
    assert naps == [1, 1, 1] and issubclass(LockBusy, WorkspaceError)
    held = open(tmp_path / "daily.lock", "w")
    fcntl.flock(held, fcntl.LOCK_EX | fcntl.LOCK_NB)
    naps.clear()
    with lock(tmp_path, "daily", wait_s=5, sleep=lambda s: (naps.append(s), held.close())):
        pass
    assert naps == [1]


def test_versions_ignores_other_names(tmp_path):
    """guardrail.v숫자.yaml만 판으로 센다(check.save의 경로 glob 시험을 workspace.versions로 옮김)."""
    (tmp_path / ".git").mkdir()
    folder = ensure(tmp_path) / "strategies" / "s1"
    folder.mkdir(parents=True)
    for name in ("guardrail.v1.yaml", "guardrail.v10.yaml", "guardrail.v2.yaml.bak", "guardrail.vx.yaml"):
        (folder / name).write_text("{}", encoding="utf-8")
    assert versions(tmp_path / ".blackclip", "s1") == [1, 10]

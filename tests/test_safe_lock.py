"""잠금 파일도 공용 안전 쓰기 규칙: 심은 링크의 대상을 비우지 않고, FIFO에서 멈추지 않는다."""
import os
import signal

import pytest

from blackclip.workspace import LockBusy, WorkspaceError, ensure, lock


@pytest.fixture
def root(tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "xdg"))
    (tmp_path / ".git").mkdir()
    return ensure(tmp_path)


@pytest.fixture
def two_seconds():
    """FIFO를 막히는 open으로 열면 영원히 멈추므로 2초 뒤 TimeoutError로 바꿔 실패로 보이게 한다."""
    def boom(*_):
        raise TimeoutError("잠금 열기가 2초 넘게 멈춤")
    old = signal.signal(signal.SIGALRM, boom)
    signal.alarm(2)
    yield
    signal.alarm(0)
    signal.signal(signal.SIGALRM, old)


def test_lock_keeps_planted_link_target(root, tmp_path):
    target = tmp_path / "keep.txt"
    target.write_text("keep\n")
    (root / "record.lock").symlink_to(target)
    with pytest.raises(WorkspaceError):
        with lock(root, "record"):
            pass
    assert target.read_text() == "keep\n"


def test_lock_on_fifo_stops_without_waiting(root, two_seconds):
    os.mkfifo(root / "record.lock")
    with pytest.raises(WorkspaceError):
        with lock(root, "record"):
            pass


def test_lock_still_locks_and_keeps_content(root):
    (root / "record.lock").write_text("note\n")
    with lock(root, "record"):
        with pytest.raises(LockBusy):
            with lock(root, "record"):
                pass
    assert (root / "record.lock").read_text() == "note\n"


def test_lock_refuses_dangling_link_and_creates_nothing(root, tmp_path):
    """가리키는 파일이 없는 링크도 따라가 만들지 않는다(open_or_create: 열기 O_NOFOLLOW → ELOOP)."""
    target = tmp_path / "made-by-link.txt"
    (root / "record.lock").symlink_to(target)
    with pytest.raises(WorkspaceError):
        with lock(root, "record"):
            pass
    assert not target.exists()

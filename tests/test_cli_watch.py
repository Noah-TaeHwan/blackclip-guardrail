"""blackclip watch 명령: 매일 대조가 잠금을 잡고 있으면 이번 확인을 건너뜀 · 도움말."""
from contextlib import contextmanager

from blackclip.cli import main
from blackclip.workspace import ensure, lock


def test_watch_skips_while_daily_holds_the_lock(tmp_path, monkeypatch, capsys):
    """daily 잠금이 잡혀 있으면 종료 0 · 건너뛴다는 한 줄(상태 · 텔레그램 없음)."""
    (tmp_path / ".git").mkdir()
    monkeypatch.chdir(tmp_path)
    root = ensure(tmp_path)
    with lock(root, "daily"):
        assert main(["watch"]) == 0
    out = capsys.readouterr().out
    assert out.startswith("blackclip watch: 이번 확인은 건너뜀(다른 blackclip daily가 이 작업 폴더에서 돌고 있습니다") and out.rstrip().endswith("다음 시간에 다시")


def test_daily_waits_up_to_600_seconds_for_the_lock(tmp_path, monkeypatch):
    """daily는 daily 잠금을 600초까지 기다려 잡는다: 자주 보는 시계와 겹쳐도 그날 대조를 건너뛰지 않는다(기다리지 않으면 종료 2로 잃음)."""
    (tmp_path / ".git").mkdir()
    monkeypatch.chdir(tmp_path)
    asked = []

    @contextmanager
    def record(root, name, wait_s=0, **_):
        asked.append((name, wait_s))
        yield

    monkeypatch.setattr("blackclip.cli.lock", record)
    monkeypatch.setattr("blackclip.cli.default_store", lambda: None)
    monkeypatch.setattr("blackclip.daily.run.run_daily", lambda *a, **k: (0, ["끝"]))
    assert main(["daily"]) == 0
    assert asked == [("daily", 600)]


def test_help_lists_watch(capsys):
    """도움말에 watch가 보인다."""
    assert main(["--help"]) == 0
    assert "watch" in capsys.readouterr().out

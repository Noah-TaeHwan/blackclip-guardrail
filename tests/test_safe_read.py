"""레포 파일 안전 읽기 · 쓰기: 상위 폴더 링크 · FIFO · 장치 · 깊은 JSON · 거대 수 · 내부 모양 · 권한."""
import json
import os
import threading
import time

import pytest

from blackclip.workspace import (WorkspaceError, append_line, ensure, exists_regular, read_json, read_jsonl,
                                 write_in)


@pytest.fixture(autouse=True)
def _home(monkeypatch, tmp_path):
    monkeypatch.setenv("HOME", str(tmp_path / "home"))


def _root(tmp_path):
    (tmp_path / ".git").mkdir(exist_ok=True)
    root = ensure(tmp_path)
    (root / "strategies" / "s1").mkdir(parents=True, exist_ok=True)
    return root


def _outside(tmp_path):
    out = tmp_path / "outside"
    out.mkdir()
    (out / "days.jsonl").write_text('{"keep": 1}\n')
    return out


OPS = {
    "read_json": lambda root: read_json(root, "strategies/s1/daily/days.jsonl"),
    "read_jsonl": lambda root: read_jsonl(root, "strategies/s1/daily/days.jsonl", 1 << 20),
    "append_line": lambda root: append_line(root, "strategies/s1/daily/days.jsonl", "x\n"),
    "write_in": lambda root: write_in(root, "strategies/s1/daily/planted.md", "x"),
    "exists_regular": lambda root: exists_regular(root, "strategies/s1/daily/days.jsonl"),
}


@pytest.mark.parametrize("op", sorted(OPS))
def test_parent_folder_link_is_not_followed(tmp_path, op):
    """마지막 파일이 아니라 중간 폴더(daily/)가 링크여도 레포 밖을 읽거나 쓰지 않는다."""
    root, out = _root(tmp_path), _outside(tmp_path)
    (root / "strategies" / "s1" / "daily").symlink_to(out, target_is_directory=True)
    if op == "exists_regular":
        assert OPS[op](root) is False
    else:
        with pytest.raises(WorkspaceError, match="상위 폴더가 링크"):
            OPS[op](root)
    assert sorted(p.name for p in out.iterdir()) == ["days.jsonl"] and (out / "days.jsonl").read_text() == '{"keep": 1}\n'


def test_workspace_root_link_is_refused(tmp_path):
    real = _root(tmp_path)
    link = tmp_path / "linked"
    link.symlink_to(real, target_is_directory=True)
    with pytest.raises(WorkspaceError, match="작업 폴더가 링크"):
        read_json(link, "connections.json")


@pytest.mark.parametrize("kind", ["fifo", "fifo_with_reader", "device_link", "file_link"])
def test_append_refuses_non_regular_without_blocking(tmp_path, kind):
    """state.log · days.jsonl 자리에 FIFO(읽는 쪽 있음 · 없음) · 장치 링크 · 파일 링크가 있으면 멈추지 않고 거절."""
    root = _root(tmp_path)
    target = root / "state.log"
    reader = None
    if kind.startswith("fifo"):
        os.mkfifo(target)
        if kind == "fifo_with_reader":
            reader = os.open(target, os.O_RDONLY | os.O_NONBLOCK)
    elif kind == "device_link":
        target.symlink_to("/dev/null")
    else:
        (tmp_path / "elsewhere").write_text("keep\n")
        target.symlink_to(tmp_path / "elsewhere")
    done = []
    th = threading.Thread(target=lambda: done.append(pytest.raises(WorkspaceError, append_line, root, "state.log", "x\n")),
                          daemon=True)
    th.start()
    th.join(2)
    if reader is not None:
        os.close(reader)
    assert done, "append_line이 2초 안에 끝나지 않음(FIFO에서 멈춤)"
    if kind == "file_link":
        assert (tmp_path / "elsewhere").read_text() == "keep\n"


def test_append_and_write_make_regular_files(tmp_path):
    root = _root(tmp_path)
    append_line(root, "strategies/s1/daily/days.jsonl", "a\n", make=True)
    append_line(root, "strategies/s1/daily/days.jsonl", "b\n")
    p = root / "strategies" / "s1" / "daily" / "days.jsonl"
    assert p.read_text() == "a\nb\n" and oct(p.stat().st_mode & 0o777) == "0o600"
    target = tmp_path / "elsewhere.md"
    target.write_text("keep\n")
    (root / "strategies" / "s1" / "after.md").symlink_to(target)
    write_in(root, "strategies/s1/after.md", "new\n")
    after = root / "strategies" / "s1" / "after.md"
    assert target.read_text() == "keep\n" and not after.is_symlink() and after.read_text() == "new\n"
    assert exists_regular(root, "strategies/s1/after.md") and not exists_regular(root, "strategies/s1/none.md")


@pytest.mark.parametrize("rel", ["../x.json", "/etc/hosts", "strategies/../../x"])
def test_only_relative_paths_inside_root(tmp_path, rel):
    with pytest.raises(WorkspaceError, match="상대 경로"):
        read_json(_root(tmp_path), rel)


@pytest.mark.parametrize("make, why", [
    (lambda p: p.write_text("[" * 200_000 + "]" * 200_000), "중첩이 너무 깊음"),
    (lambda p: p.write_text('{"x": ' + "9" * 5000 + "}"), "숫자가 너무 김"),
    (lambda p: p.write_bytes(b"\xff\xfe{}"), "UTF-8이 아닙니다"),
    (lambda p: p.write_text(" " * ((1 << 20) + 1)), "바이트를 넘어 읽지 않습니다"),
])
def test_read_json_refuses_bad_content(tmp_path, make, why):
    root = _root(tmp_path)
    make(root / "x.json")
    with pytest.raises(WorkspaceError, match=why):
        read_json(root, "x.json")


def test_read_json_last_link_fifo_missing(tmp_path):
    root = _root(tmp_path)
    (tmp_path / "o.json").write_text("{}")
    (root / "l.json").symlink_to(tmp_path / "o.json")
    with pytest.raises(WorkspaceError, match="심볼릭 링크이거나 일반 파일이 아니라"):
        read_json(root, "l.json")
    os.mkfifo(root / "f.json")
    t0 = time.monotonic()
    with pytest.raises(WorkspaceError):
        read_json(root, "f.json")
    assert time.monotonic() - t0 < 2
    with pytest.raises(FileNotFoundError):
        read_json(root, "none.json")


@pytest.mark.skipif(os.geteuid() == 0, reason="root는 권한을 무시함")
def test_read_json_unreadable_is_one_error(tmp_path):
    root = _root(tmp_path)
    (root / "x.json").write_text("{}")
    (root / "x.json").chmod(0)
    with pytest.raises(WorkspaceError):
        read_json(root, "x.json")


def test_read_jsonl_skips_bad_lines(tmp_path):
    root = _root(tmp_path)
    (root / "x.jsonl").write_text('{"a": 1}\n' + "[" * 200_000 + "\n" + '{"b": ' + "9" * 5000 + "}\n" + '{"c": 2}\n{"d": ')
    assert read_jsonl(root, "x.jsonl", 1 << 22) == [{"a": 1}, {"c": 2}]
    assert read_jsonl(root, "none.jsonl", 1 << 20) == []
    with pytest.raises(WorkspaceError, match="바이트를 넘어"):
        read_jsonl(root, "x.jsonl", 10)

# ── 옛 읽기 4곳 · state.log · 내부 모양 ───────────────────────────────────────────────
from datetime import date  # noqa: E402

from blackclip.cli import main  # noqa: E402
from blackclip.daily.messages import load_notified  # noqa: E402
from blackclip.daily.reconcile import Day  # noqa: E402
from blackclip.daily.run import _guards, _log_state  # noqa: E402
from blackclip.watch.state import read_state  # noqa: E402
from blackclip.workspace import guardrail_problem, read_guardrail, versions  # noqa: E402

GOOD = {"version": 1, "live_start": "2026-09-23",
        "target": {"market": "spot", "symbols": ["BTCUSDT"], "bar_seconds": 3600, "exec": "next_open",
                   "start": "2024-01-01", "link_sha256": "0" * 64},
        "normal_range": {"drawn": True, "low": -0.037, "high": 0.081, "daily_low": [-0.01] * 30, "daily_high": [0.02] * 30},
        "warning_line": {"line": -0.072, "sensitivity": "민감", "lines": {"민감": -0.072, "보통": -0.09, "둔감": -0.11}},
        "judgment_hold": {"min_trades": 30, "hold_if_edge0_at_least": 0.2, "edge0_in_range": [0.5, 0.9]},
        "trade_frequency": {"low": 7, "high": 17}, "thesis": {"rule": "논지 없음"}, "period": {"mean": 0.0001, "std": 0.01},
        "response": {"execution_warning": "", "warning_line": ""}}  # 3판 작성 때 실제 소비자 셋에 넣어 확인


@pytest.mark.parametrize("doc, why", [
    ({}, "target"),
    ({**GOOD, "target": {**GOOD["target"], "symbols": "BTCUSDT"}}, "target.symbols"),
    ({**GOOD, "warning_line": {"line": "-7%", "sensitivity": "민감"}}, "warning_line.line"),
    ({**GOOD, "normal_range": {**GOOD["normal_range"], "daily_low": [0.0] * 29}}, "normal_range.daily_low"),
    ({**GOOD, "live_start": "9/23"}, "live_start"),
    ({**GOOD, "judgment_hold": {**GOOD["judgment_hold"], "min_trades": True}}, "judgment_hold.min_trades"),
    ({**GOOD, "match_window_bars": []}, "match_window_bars"),  # daily의 int(...)가 터지는 값
    ({**GOOD, "match_window_bars": "1"}, "match_window_bars"),
    ({**GOOD, "match_window_bars": 0}, "match_window_bars"),
    ({**GOOD, "match_window_bars": True}, "match_window_bars"),
])
def test_guardrail_inner_shape_is_one_line(tmp_path, monkeypatch, capsys, doc, why):
    """최상위가 객체여도 소비자가 쓰는 칸이 틀리면 KeyError가 아니라 한 줄 · 종료 2."""
    assert guardrail_problem(GOOD) is None and why in guardrail_problem(doc)
    root = _root(tmp_path)
    (root / "strategies" / "s1" / "guardrail.v1.yaml").write_text(json.dumps(doc, ensure_ascii=False))
    with pytest.raises(WorkspaceError, match=why):
        read_guardrail(root, "s1")
    monkeypatch.chdir(tmp_path)
    assert main(["live", "s1"]) == 2
    err = capsys.readouterr().err.strip().splitlines()
    assert len(err) == 1 and err[0].startswith("blackclip live: ") and why in err[0]


@pytest.mark.parametrize("extra", [{}, {"match_window_bars": 1}, {"match_window_bars": 3}], ids=["missing", "1", "3"])
def test_guardrail_match_window_bars_is_optional_but_a_positive_int_when_given(extra):
    """없으면 통과(소비자 daily의 기본값 1), 1 이상의 정수면 통과."""
    assert guardrail_problem({**GOOD, **extra}) is None


def test_good_guardrail_runs_through_consumers():
    """검사를 통과한 가드레일은 소비자가 KeyError 없이 읽고, 소비자가 읽는 키를 하나라도 빼면 검사가 그 칸을 댄다."""
    import pandas as pd
    from blackclip.daily.reconcile import line_cause, strategy_cause, thesis_cause
    assert guardrail_problem(GOOD) is None
    strategy_cause(GOOD, -0.01, 22, 12)
    thesis_cause(GOOD["thesis"]["rule"], None)
    line_cause(pd.Series([0.0, -0.01]), GOOD["warning_line"])
    for path in (("judgment_hold", "edge0_in_range"), ("judgment_hold", "hold_if_edge0_at_least"), ("thesis", "rule"),
                 ("target", "exec"), ("target", "start"), ("target", "link_sha256"), ("period", "std"),
                 ("warning_line", "lines"), ("version",)):
        doc = json.loads(json.dumps(GOOD))
        parent = doc
        for k in path[:-1]:
            parent = parent[k]
        del parent[path[-1]]
        assert ".".join(path) in (guardrail_problem(doc) or ""), path
    assert "target.exec" in guardrail_problem({**GOOD, "target": {**GOOD["target"], "exec": "open"}})
    assert "target.start" in guardrail_problem({**GOOD, "target": {**GOOD["target"], "start": "2026-10-01"}})
    assert "target.market" in guardrail_problem({**GOOD, "target": {**GOOD["target"], "market": "margin"}})


def test_old_reads_refuse_parent_links(tmp_path):
    """옛 읽기 4곳(_guards · read_state · load_notified · live)이 strategies/ · 전략 폴더 링크를 따르지 않음."""
    root = _root(tmp_path)
    out = tmp_path / "out"
    (out / "s2").mkdir(parents=True)
    (out / "s2" / "guardrail.v1.yaml").write_text(json.dumps(GOOD))
    (out / "s2" / "watch.json").write_text('{"line": {}}')
    (root / "strategies" / "s2").symlink_to(out / "s2", target_is_directory=True)
    assert versions(root, "s2") == [] and read_guardrail(root, "s2") is None
    assert "s2" not in _guards(root)[0]
    assert read_state(root, "s2") == ({}, True)


def test_guardrail_versions_skip_links(tmp_path):
    root = _root(tmp_path)
    (tmp_path / "o.yaml").write_text(json.dumps(GOOD))
    (root / "strategies" / "s1" / "guardrail.v1.yaml").symlink_to(tmp_path / "o.yaml")
    (root / "strategies" / "s1" / "guardrail.v2.yaml").write_text(json.dumps(GOOD))
    assert versions(root, "s1") == [2] and read_guardrail(root, "s1")[0] == 2


def test_notified_inner_shape(tmp_path):
    """notified.json = 전략 → {칸: 글}. {"s1": []}는 plan_messages에서 AttributeError였음 → 빈 dict."""
    root = _root(tmp_path)
    for text in ('{"s1": []}', '{"s1": {"전략": 3}}', "[]", "[" * 200_000):
        (root / "notified.json").write_text(text)
        assert load_notified(root) == {}
    (root / "notified.json").write_text('{"s1": {"전략": "정상"}}')
    assert load_notified(root) == {"s1": {"전략": "정상"}}
    (root / "notified.json").unlink()
    os.mkfifo(root / "notified.json")
    assert load_notified(root) == {}


def test_watch_state_links_fifo_deep(tmp_path):
    root = _root(tmp_path)
    (tmp_path / "o.json").write_text('{"line": {}}')
    (root / "strategies" / "s1" / "watch.json").symlink_to(tmp_path / "o.json")
    assert read_state(root, "s1") == ({}, True)
    (root / "strategies" / "s1" / "watch.json").unlink()
    (root / "strategies" / "s1" / "watch.json").write_text("[" * 200_000)
    assert read_state(root, "s1") == ({}, True)
    assert read_state(root, "nope") == ({}, False)


def _day(states, name="s1"):
    return Day(name, date(2026, 10, 14), 22, 3, "h", states, {k: "" for k in states}, {}, None)


ST = {"전략": "정상", "실행": "정상", "봇": "정상", "선": "정상", "논지": "해당 없음"}


def test_state_log_line_shapes(tmp_path):
    """상태 줄 · 기록 줄만 받고, states 안 값이 글이 아닌 줄 · 모르는 줄은 건너뜀."""
    root = _root(tmp_path)
    (root / "state.log").write_text(
        json.dumps({"day": "2026-10-13", "strategy": "s1", "states": ST}, ensure_ascii=False) + "\n"
        + '{"day": "2026-10-14", "strategy": "s1", "states": {"전략": 1}}\n'
        + '{"day": "2026-10-14", "record": "중지", "strategy": "s1"}\n'
        + '["x"]\n')
    _log_state(root, _day(ST))  # 마지막 「유효한 상태 줄」과 같음 → 안 씀
    assert len((root / "state.log").read_text().splitlines()) == 4
    _log_state(root, _day({**ST, "봇": "경고"}))
    assert len((root / "state.log").read_text().splitlines()) == 5


def test_state_log_link_or_fifo_is_refused(tmp_path):
    root = _root(tmp_path)
    (tmp_path / "elsewhere").write_text("keep\n")
    (root / "state.log").symlink_to(tmp_path / "elsewhere")
    with pytest.raises(WorkspaceError):
        _log_state(root, _day(ST))
    assert (tmp_path / "elsewhere").read_text() == "keep\n"
    (root / "state.log").unlink()
    os.mkfifo(root / "state.log")
    t0 = time.monotonic()
    with pytest.raises(WorkspaceError):
        _log_state(root, _day(ST))
    assert time.monotonic() - t0 < 2


def test_write_in_is_private_and_synced(tmp_path, monkeypatch):
    """새 파일 · 바꾼 0600 파일 모두 umask 022에서도 0600이고, 바꿔치기마다 새 내용을 담은 임시 파일을 fsync한 **뒤에** os.replace한다.

    fsync 횟수만이 아니라 fsync한 fd의 정체(임시 파일)와 **fsync가 불린 그 순간의 내용**, 호출 순서까지 본다(안전 쓰기가 맡아 온 단언).
    """
    root = _root(tmp_path)
    calls = []  # ("fsync", fd의 inode, fsync 순간 그 파일의 내용) · ("replace", 바꿔치기 직전 임시 파일의 inode, 그 내용)
    real_fsync, real_replace = os.fsync, os.replace

    def fsync(fd):
        ino = os.fstat(fd).st_ino
        # fd는 쓰기 전용이라 직접 읽을 수 없다: 같은 inode의 임시 파일을 이름(.tmp)으로 찾아 지금 바이트를 읽는다(파일 위치는 안 건드림)
        tmp = next(p for p in root.rglob("*.tmp") if p.stat().st_ino == ino)
        calls.append(("fsync", ino, tmp.read_bytes().decode()))
        real_fsync(fd)

    def replace(src, dst, **kw):
        fd = os.open(src, os.O_RDONLY, dir_fd=kw["src_dir_fd"])
        try:
            calls.append(("replace", os.fstat(fd).st_ino, os.read(fd, 100).decode()))
        finally:
            os.close(fd)
        real_replace(src, dst, **kw)

    monkeypatch.setattr(os, "fsync", fsync)
    monkeypatch.setattr(os, "replace", replace)
    old_mask = os.umask(0o022)
    try:
        write_in(root, "strategies/s1/daily/new.md", "a", make=True)
        old = root / "strategies" / "s1" / "old.md"
        old.write_text("x")
        old.chmod(0o600)
        write_in(root, "strategies/s1/old.md", "bb")
    finally:
        os.umask(old_mask)
    new = root / "strategies" / "s1" / "daily" / "new.md"
    assert (new.read_text(), old.read_text()) == ("a", "bb")
    assert (new.stat().st_mode & 0o777, old.stat().st_mode & 0o777) == (0o600, 0o600)
    assert [c[0] for c in calls] == ["fsync", "replace", "fsync", "replace"]  # 쓸 때마다 fsync가 바꿔치기보다 먼저, 한 번씩
    (_, fsynced_a, at_sync_a), (_, tmp_a, text_a), (_, fsynced_b, at_sync_b), (_, tmp_b, text_b) = calls
    assert (at_sync_a, at_sync_b) == ("a", "bb")  # fsync가 불린 순간 이미 새 내용이 다 쓰여 있었다(나중에 고쳐 쓰는 변이를 잡음)
    assert (text_a, text_b) == ("a", "bb")  # 바꿔치기 직전에도 같은 내용
    assert (fsynced_a, fsynced_b) == (tmp_a, tmp_b)  # fsync한 fd가 곧 바꿔치기할 임시 파일
    assert (new.stat().st_ino, old.stat().st_ino) == (fsynced_a, fsynced_b)  # 그 파일이 그대로 제 자리를 차지함

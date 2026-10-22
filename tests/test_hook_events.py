"""훅 이벤트: Bash 시작 · 끝(성공 · 실패 · 백그라운드) · 편집 기록 · 깨진 입력 · 가벼운 import."""
import io
import json
import os
import signal
import subprocess
import sys
import time

import pytest
from pathlib import Path

from blackclip import hook
from blackclip.cli import main

SRC = str(Path(__file__).resolve().parents[1] / "src")


def _repo(tmp_path, blackclip=True):
    """git 레포 하나(+ .blackclip)를 만든다."""
    subprocess.run(["git", "init", "-q", str(tmp_path)], check=True)
    subprocess.run(["git", "-C", str(tmp_path), "-c", "user.email=t@t", "-c", "user.name=t", "commit", "-q",
                    "--allow-empty", "-m", "init"], check=True)
    if blackclip:
        (tmp_path / ".blackclip").mkdir()
    return tmp_path


def _bash(cwd, cmd, tid="toolu_1", **extra):
    """Bash 훅 입력(실측 덤프 칸 모양)."""
    return {"session_id": "s-1", "cwd": str(cwd), "hook_event_name": "PreToolUse", "tool_name": "Bash",
            "tool_input": {"command": cmd, "description": "x"}, "tool_use_id": tid, **extra}


def _rows(repo):
    """runs.jsonl 줄들."""
    p = repo / ".blackclip" / "runs" / "runs.jsonl"
    return [json.loads(x) for x in p.read_text().splitlines()] if p.exists() else []


def test_backtest_start_and_end_success(tmp_path):
    """규칙에 맞는 명령: 시작(가린 명령 · 레포 상대 cwd · git 해시) + 끝(성공은 종료 코드 칸 없음 → code null)."""
    repo = _repo(tmp_path)
    (repo / "strat").mkdir()
    pre = _bash(repo / "strat", "API_KEY=k python3 backtest.py")
    assert hook.handle("PreToolUse", pre) is None
    post = {**pre, "hook_event_name": "PostToolUse", "duration_ms": 4043,
            "tool_response": {"stdout": "sharpe 1.2", "stderr": "", "interrupted": False, "isImage": False}}
    hook.handle("PostToolUse", post)
    start, end = _rows(repo)
    head = subprocess.run(["git", "-C", str(repo), "rev-parse", "--short", "HEAD"], capture_output=True,
                          text=True).stdout.strip()
    assert start | {"t": None} == {"v": 1, "ev": "start", "t": None, "id": "toolu_1", "session": "s-1", "cwd": "strat",
                                   "git": head, "cmd": "API_KEY=*** python3 backtest.py"}
    assert end | {"t": None} == {"v": 1, "ev": "end", "t": None, "id": "toolu_1", "ok": True, "code": None,
                                 "interrupt": False, "ms": 4043}


def test_failure_parses_exit_code_and_interrupt(tmp_path):
    """PostToolUseFailure: error 첫 줄 「Exit code N」, 그 줄이 없으면 code null, is_interrupt."""
    repo = _repo(tmp_path)
    fail = {**_bash(repo, "python3 sweep_fail.py"), "hook_event_name": "PostToolUseFailure",
            "error": "Exit code 3\nboom", "is_interrupt": False, "duration_ms": 1068}
    hook.handle("PreToolUse", _bash(repo, "python3 sweep_fail.py"))
    hook.handle("PreToolUse", _bash(repo, "python3 sweep_fail.py", "toolu_2"))
    hook.handle("PostToolUseFailure", fail)
    hook.handle("PostToolUseFailure", {**fail, "tool_use_id": "toolu_2", "error": "spawn failed", "is_interrupt": True})
    a, b = _rows(repo)[2:]
    assert (a["ok"], a["code"], a["interrupt"], a["ms"]) == (False, 3, False, 1068)
    assert (b["ok"], b["code"], b["interrupt"]) == (False, None, True)
    assert hook.exit_code("Exit code -9\n") == -9 and hook.exit_code(None) is None


def test_non_backtest_and_background(tmp_path):
    """규칙 밖 명령은 시작도 끝도 안 씀. 백그라운드로 띄운 명령의 끝은 bg true(띄운 순간)."""
    repo = _repo(tmp_path)
    hook.handle("PreToolUse", _bash(repo, "ls"))
    hook.handle("PostToolUse", {**_bash(repo, "ls"), "tool_response": {"stdout": ""}})
    assert _rows(repo) == []
    hook.handle("PreToolUse", _bash(repo, "python3 backtest.py"))
    hook.handle("PostToolUse", {**_bash(repo, "python3 backtest.py"),
                                "tool_response": {"stdout": "", "backgroundTaskId": "b1", "interrupted": False}})
    assert _rows(repo)[1]["bg"] is True


def test_end_only_after_a_written_start(tmp_path):
    """끝은 시작 표지가 있을 때만, 한 번만: 시작 없는 끝 · 같은 id 두 번째 끝 · 이상한 id는 안 씀."""
    repo = _repo(tmp_path)
    post = {**_bash(repo, "python3 backtest.py"), "tool_response": {"stdout": ""}}
    hook.handle("PostToolUse", post)
    assert _rows(repo) == []
    hook.handle("PreToolUse", _bash(repo, "python3 backtest.py"))
    assert (repo / ".blackclip" / "runs" / "open" / "toolu_1").exists()
    hook.handle("PostToolUse", post)
    hook.handle("PostToolUseFailure", {**post, "error": "Exit code 1"})
    assert [r["ev"] for r in _rows(repo)] == ["start", "end"]
    assert not (repo / ".blackclip" / "runs" / "open" / "toolu_1").exists()
    hook.handle("PreToolUse", _bash(repo, "python3 backtest.py", tid="../../x"))
    assert len(_rows(repo)) == 2


def test_long_cwd_and_ids_still_make_a_start_line(tmp_path):
    """레포 상대 cwd a×200/b×200 · 세션 36자 · id 30자에서도 시작 줄이 512바이트 안에 써지고 끝이 짝을 맞춤."""
    repo = _repo(tmp_path)
    deep = repo / ("a" * 200) / ("b" * 200)
    deep.mkdir(parents=True)
    pre = {**_bash(deep, "python3 backtest.py", tid="toolu_" + "9" * 24), "session_id": "s" * 36}
    hook.handle("PreToolUse", pre)
    hook.handle("PostToolUse", {**pre, "tool_response": {"stdout": ""}})
    start, end = _rows(repo)
    raw = (repo / ".blackclip" / "runs" / "runs.jsonl").read_bytes().splitlines()
    assert all(len(r) + 1 <= hook.LIMIT for r in raw)
    assert start["cwd"] == "…" + "b" * 114 and start["cmd"] == "python3 backtest.py" and end["id"] == start["id"]


def test_no_blackclip_folder_writes_nothing(tmp_path):
    """작업 폴더가 없으면 아무것도 만들지 않음(셋업 전 실행은 잃음)."""
    repo = _repo(tmp_path, blackclip=False)
    (repo / "s.py").write_text("x = 1\n")
    hook.handle("PreToolUse", _bash(repo, "python3 backtest.py"))
    hook.handle("PostToolUse", {**_bash(repo, "x"), "tool_name": "Write", "tool_input": {"file_path": str(repo / "s.py")}})
    assert not (repo / ".blackclip").exists()


def test_edit_records_repo_py_and_ipynb_only(tmp_path):
    """Edit · Write(file_path) · NotebookEdit(notebook_path): 레포 안 .py · .ipynb만, 상대 경로 + 디스크 sha256."""
    repo = _repo(tmp_path)
    (repo / "s").mkdir()
    (repo / "s" / "a.py").write_text("x = 2\n")
    (repo / "n.ipynb").write_text("{}")
    (repo / "README.md").write_text("r")
    (repo / ".blackclip" / "link.py").write_text("y")
    base = {"session_id": "s-1", "cwd": str(repo), "hook_event_name": "PostToolUse", "tool_use_id": "toolu_e"}
    hook.handle("PostToolUse", {**base, "tool_name": "Edit", "tool_input": {"file_path": str(repo / "s" / "a.py")}})
    hook.handle("PostToolUse", {**base, "tool_name": "NotebookEdit", "tool_input": {"notebook_path": "n.ipynb"}})
    hook.handle("PostToolUse", {**base, "tool_name": "Write", "tool_input": {"file_path": str(repo / "README.md")}})
    hook.handle("PostToolUse", {**base, "tool_name": "Write",
                                "tool_input": {"file_path": str(repo / ".blackclip" / "link.py")}})
    hook.handle("PostToolUse", {**base, "tool_name": "Edit", "tool_input": {"file_path": str(repo / "gone.py")}})
    rows = _rows(repo)
    assert [(r["ev"], r["path"]) for r in rows] == [("edit", "s/a.py"), ("edit", "n.ipynb")]
    assert rows[0]["sha256"] == "4205c4809ab1b080fd32b6bf9640e5feaa6d1b69bf9fa684954ab710157ec141"
    assert "content" not in rows[0] and "x = 2" not in json.dumps(rows)


def test_cli_hook_never_fails_and_stays_silent(tmp_path, monkeypatch, capsysbinary):
    """`blackclip hook`: 깨진 stdin · 빈 stdin · 모르는 이벤트 · 이벤트 없음 모두 종료 0, SessionStart 밖 stdout 0바이트."""
    monkeypatch.chdir(tmp_path)
    for raw, argv in [(b"not json", ["hook", "PreToolUse"]), (b"", ["hook", "PostToolUse"]), (b"[1]", ["hook", "Bogus"]),
                      (b'{"tool_name": "Bash", "tool_input": 5}', ["hook", "PreToolUse"]), (b"{}", ["hook"])]:
        monkeypatch.setattr(sys, "stdin", io.TextIOWrapper(io.BytesIO(raw)))
        assert main(argv) == 0
    assert capsysbinary.readouterr().out == b""


def test_hook_path_does_not_import_heavy_modules():
    """훅 경로는 pandas · numpy · check · daily · watch를 불러오지 않는다(훅마다 파이썬이 새로 뜸)."""
    code = ("import io, sys; sys.stdin = io.TextIOWrapper(io.BytesIO(b'{}')); from blackclip.cli import main\n"
            "for e in ('SessionStart', 'PreToolUse', 'PostToolUse', 'PostToolUseFailure'): main(['hook', e])\n"
            "print([m for m in ('pandas', 'numpy', 'blackclip.check', 'blackclip.daily', 'blackclip.watch') "
            "if m in sys.modules], file=sys.stderr)")
    r = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True, check=True,
                       env={**os.environ, "PYTHONPATH": SRC})  # 설치본이 아니라 이 레포 src
    assert r.stderr.strip() == "[]"


def _child_cpu():
    """끝난 하위 프로세스들이 쓴 CPU 시간 합(초)."""
    import resource
    r = resource.getrusage(resource.RUSAGE_CHILDREN)
    return r.ru_utime + r.ru_stime


def test_run_hook_restores_the_alarm_handler(monkeypatch, capsysbinary):
    """run_hook이 끝나면 SIGALRM 처리기가 앞 것으로 돌아온다(정상 · 시간 제한에 걸림 둘 다)."""
    def mine(signum, frame):
        pass

    before = signal.signal(signal.SIGALRM, mine)
    try:
        monkeypatch.setattr(sys, "stdin", io.TextIOWrapper(io.BytesIO(b"{}")))
        assert hook.run_hook("PostToolUse") == 0 and signal.getsignal(signal.SIGALRM) is mine
        monkeypatch.setattr(hook, "BUDGET_S", 0.05)
        monkeypatch.setattr(hook, "handle", lambda event, data: time.sleep(1))
        monkeypatch.setattr(sys, "stdin", io.TextIOWrapper(io.BytesIO(b"{}")))
        assert hook.run_hook("PostToolUse") == 0 and signal.getsignal(signal.SIGALRM) is mine
    finally:
        signal.signal(signal.SIGALRM, before)


def test_huge_command_through_cli_finishes_fast(tmp_path):
    """42,000바이트 명령 payload를 `blackclip hook PreToolUse`에 넣어도 2초 안에 종료 0 · stdout 0바이트."""
    repo = _repo(tmp_path)
    code = "import sys; from blackclip.cli import main; sys.exit(main(['hook', 'PreToolUse']))"
    for cmd in ("python " * 6000, "python3 backtest.py " + "python " * 6000):
        t, w = _child_cpu(), time.perf_counter()  # CPU 시간으로 잼: 파이썬 시작이 load에 따라 벽시계로 1초를 넘어도 덜 흔들림
        r = subprocess.run([sys.executable, "-c", code], input=json.dumps(_bash(repo, cmd)).encode(), capture_output=True,
                           env={**os.environ, "PYTHONPATH": SRC}, timeout=30)
        assert (r.returncode, r.stdout) == (0, b"") and _child_cpu() - t < 2.0 and time.perf_counter() - w < 10
    (start,) = _rows(repo)
    assert start["cmd"].startswith("python3 backtest.py python ") and start["cmd"].endswith("…")


def test_run_hook_gives_up_after_its_own_budget(monkeypatch, capsysbinary):
    """async 훅은 timeout이 강제되지 않으므로 엔진이 스스로 멈춘다(BUDGET_S): 넘으면 조용히 0."""
    assert hook.BUDGET_S == 2.0
    monkeypatch.setattr(hook, "BUDGET_S", 0.2)
    monkeypatch.setattr(hook, "handle", lambda event, data: time.sleep(5))
    monkeypatch.setattr(sys, "stdin", io.TextIOWrapper(io.BytesIO(b"{}")))
    t = time.perf_counter()
    assert hook.run_hook("PostToolUse") == 0
    assert time.perf_counter() - t < 2 and capsysbinary.readouterr().out == b""


@pytest.mark.parametrize("case", ["alarm_before_disarm", "no_setitimer"])
def test_run_hook_never_leaks_from_the_timer_edges(monkeypatch, capsysbinary, case):
    """시간 제한 해제 자리에서도 예외가 새지 않고 종료 0 · stdout 0: SIGALRM이 해제 직전에 오거나, setitimer가 없는 OS(윈도)."""
    real = signal.setitimer

    def fire_then_disarm(which, secs, *rest):
        if secs == 0:  # 해제 호출 직전에 알람이 도착
            signal.raise_signal(signal.SIGALRM)
        return real(which, secs, *rest)

    if case == "no_setitimer":
        monkeypatch.delattr(signal, "setitimer")
    else:
        monkeypatch.setattr(signal, "setitimer", fire_then_disarm)
    monkeypatch.setattr(hook, "handle", lambda event, data: None)
    monkeypatch.setattr(sys, "stdin", io.TextIOWrapper(io.BytesIO(b"{}")))
    assert hook.run_hook("PostToolUse") == 0
    assert capsysbinary.readouterr().out == b""


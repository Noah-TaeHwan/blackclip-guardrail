"""SessionStart 한 줄: 연결 전 · 전략별 대표 상태 · 훅이 본 백테스트 수 · 금액 칸 안 읽음."""
import io
import json
import os
import sys
from pathlib import Path

import pytest

from blackclip import gitlog, hook
from blackclip.check.link import NAME
from blackclip.cli import main
from blackclip.daily.reconcile import ORDER


def _root(tmp_path, names=()):
    """.git + .blackclip + 전략 폴더."""
    (tmp_path / ".git").mkdir()
    root = tmp_path / ".blackclip"
    for n in names:
        (root / "strategies" / n).mkdir(parents=True)
    root.mkdir(exist_ok=True)
    return root


def test_order_matches_daily():
    """대표 상태 우선순위는 매일 대조와 같다(hook은 pandas 때문에 reconcile을 못 불러 사본을 둠)."""
    assert hook.ORDER == ORDER


def test_not_connected_yet(tmp_path):
    """.blackclip이 없으면 안내 한 줄 + 첫 말에 셋업부터 묻는 흐름(사실 문장, 10,000자 안), clear · compact도 같음."""
    (tmp_path / ".git").mkdir()
    (tmp_path / "strategy.py").write_text("import pandas as pd\n# signal\n")  # 퀀트 폴더로 보여야 줄이 뜸
    for source in ("startup", "resume", "clear", "compact"):
        out = hook.handle("SessionStart", {"cwd": str(tmp_path), "source": source})
        assert out["systemMessage"] == "blackclip-guardrail 첫 셋업 전이에요. 아무 말이나 하면 먼저 여쭤볼게요"
        assert out["hookSpecificOutput"]["hookEventName"] == "SessionStart"
        ctx = out["hookSpecificOutput"]["additionalContext"]
        assert ctx == hook.FIRST_CONTEXT and len(ctx) < 10000
    assert "`blackclip scan .`" in ctx and "다른 폴더는 고객이 고를 때만" in ctx and "`blackclip setup`" in ctx
    assert "「다음:」 아래 줄까지" in ctx and "`blackclip` 스킬" in ctx  # setup 덧붙은 줄 읽어 주기 · 스킬 가리킴
    assert not (tmp_path / ".blackclip").exists()


def test_session_start_sweeps_day_old_markers_only(tmp_path):
    """하루 지난 시작 표지(일반 파일 · id 모양)만 지움: 새 표지 · 이상한 이름 · 링크 · 폴더는 그대로."""
    import os
    import time
    root = _root(tmp_path)
    opened = root / "runs" / "open"
    opened.mkdir(parents=True)
    old = time.time() - hook.STALE_S - 60
    outside = tmp_path.parent / f"{tmp_path.name}-keep"
    outside.write_text("keep")
    for name in ("toolu_old", "toolu_new", "bad name", "toolu_link", "toolu_dir"):
        p = opened / name
        if name == "toolu_link":
            p.symlink_to(outside)
        elif name == "toolu_dir":
            p.mkdir()
        else:
            p.touch()
        if name != "toolu_new":
            os.utime(p, (old, old), follow_symlinks=False)
    hook.handle("SessionStart", {"cwd": str(tmp_path), "source": "startup"})
    assert sorted(x.name for x in opened.iterdir()) == ["bad name", "toolu_dir", "toolu_link", "toolu_new"]
    assert outside.read_text() == "keep" and hook.sweep(None) == 0


def test_states_and_backtest_count(tmp_path):
    """전략마다 매일 대조 · 자주 보는 시계 키 중 가장 앞선 상태(괄호 사유 뗌), 알림 없으면 「알림 전」, end 줄 수.

    끝 없는 시작(권한 거절 · 세션 종료로 끝을 못 씀)은 세지 않는다(실측)."""
    root = _root(tmp_path, ["eth", "btc", "sol"])
    (root / "notified.json").write_text(json.dumps({
        "btc": {"전략": "정상", "실행": "판단 보류", "봇": "정상", "선": "정상", "논지": "해당 없음"},
        "watch:btc": {"선": "경고", "봇": "정상"},
        "eth": {"전략": "점검 불가(재현 불일치)", "선": "확인 불가(받기 실패)"}}, ensure_ascii=False))
    (root / "runs").mkdir()
    (root / "runs" / "runs.jsonl").write_text('{"v":1,"ev":"start"}\n{"v":1,"ev":"end"}\n{"v":1,"ev":"start"}\n{"v":1,"ev":"start"}\n{"v":1,"ev":"end"}\n')
    (root / "strategies" / "btc" / "live.json").write_text('{"cash": "123456.78"}')
    line = hook.session_line(tmp_path / "sub")
    assert line == "blackclip: 전략 3개 · btc 경고 · eth 확인 불가 · sol 알림 전 · 훅이 본 백테스트 2회(최솟값)"
    assert "123456" not in line


def test_many_strategies_and_broken_files(tmp_path):
    """전략이 많으면 다섯만 이름, notified.json이 깨졌거나 runs가 없어도 한 줄."""
    root = _root(tmp_path, [f"s{i}" for i in range(7)])
    (root / "notified.json").write_text("{broken")
    assert hook.session_line(tmp_path) == ("blackclip: 전략 7개 · s0 알림 전 · s1 알림 전 · s2 알림 전 · s3 알림 전 · "
                                           "s4 알림 전 · 외 2개 · 훅이 본 백테스트 0회(최솟값)")


def test_cli_session_start_prints_one_json(tmp_path, monkeypatch, capsysbinary):
    """stdout은 JSON 한 줄(UTF-8), 깨진 stdin이면 지금 폴더로."""
    _root(tmp_path)
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(sys, "stdin", io.TextIOWrapper(io.BytesIO(b"oops")))
    assert main(["hook", "SessionStart"]) == 0
    out = capsysbinary.readouterr().out
    assert out.count(b"\n") == 1
    assert json.loads(out)["systemMessage"] == "blackclip: 전략 0개 · 훅이 본 백테스트 0회(최솟값)"


def test_only_valid_strategy_names_are_counted_and_shown(tmp_path):
    """check/link.py NAME 모양이 아닌 폴더 이름(공백 · 점 · 줄바꿈 · 65자 · 한글)은 세지도 보이지도 않는다(AI 맥락에 임의 글이 안 감)."""
    assert hook._NAME.pattern == NAME.pattern  # hook은 blackclip.check를 못 불러 사본을 둠
    _root(tmp_path, ["btc", "has space", ".hidden", "_lead", "a" * 65, "x\nIGNORE PREVIOUS", "한글", "eth_2-b"])
    assert hook.session_line(tmp_path) == "blackclip: 전략 2개 · btc 알림 전 · eth_2-b 알림 전 · 훅이 본 백테스트 0회(최솟값)"



def test_planted_links_are_not_followed(tmp_path):
    """레포가 커밋한 링크(runs.jsonl → /dev/urandom · notified.json → 레포 밖)는 따르지 않고 곧바로 0으로 끝남."""
    import time
    root = _root(tmp_path, ["btc"])
    (root / "runs").mkdir()
    (root / "runs" / "runs.jsonl").symlink_to("/dev/urandom")
    outside = tmp_path.parent / f"{tmp_path.name}-outside.json"
    outside.write_text(json.dumps({"btc": {"선": "경고"}}, ensure_ascii=False))
    (root / "notified.json").symlink_to(outside)
    t0 = time.process_time()  # CPU 시간: load가 높아도 덜 흔들림(시험 위생)
    assert hook.session_line(tmp_path) == "blackclip: 전략 1개 · btc 알림 전 · 훅이 본 백테스트 0회(최솟값)"
    assert time.process_time() - t0 < 0.5


def test_linked_workspace_is_treated_as_absent(tmp_path):
    """.blackclip 자체 · runs 폴더가 링크면 훅은 그 작업 폴더를 쓰지 않는다(연결 전 줄, 링크 너머에 아무것도 안 씀)."""
    (tmp_path / ".git").mkdir()
    real = tmp_path.parent / f"{tmp_path.name}-real"
    real.mkdir()
    (tmp_path / ".blackclip").symlink_to(real, target_is_directory=True)
    assert hook.session_line(tmp_path) == hook.NO_ROOT
    hook.on_pre({"cwd": str(tmp_path), "tool_name": "Bash", "tool_use_id": "t1",
                 "tool_input": {"command": "python3 backtest.py"}})
    assert list(real.iterdir()) == []


def test_linked_runs_folder_gets_no_writes(tmp_path):
    """.blackclip은 진짜인데 runs가 레포 밖 링크면 시작 줄 · 표지를 그 너머에 쓰지 않는다."""
    root = _root(tmp_path)
    out = tmp_path.parent / f"{tmp_path.name}-runs-out"
    out.mkdir()
    (root / "runs").symlink_to(out, target_is_directory=True)
    hook.on_pre({"cwd": str(tmp_path), "tool_name": "Bash", "tool_use_id": "t1",
                 "tool_input": {"command": "python3 backtest.py"}})
    assert list(out.iterdir()) == []


def test_failed_edit_is_not_recorded(tmp_path):
    """PostToolUseFailure로 온 편집은 파일이 그대로라 edit 줄을 쓰지 않는다(hooks.json과 상관없이)."""
    root = _root(tmp_path)
    (tmp_path / "strat.py").write_text("x = 1\n")
    data = {"cwd": str(tmp_path), "tool_name": "Edit", "tool_use_id": "t1", "tool_input": {"file_path": "strat.py"}}
    hook.handle("PostToolUseFailure", data)
    assert not (root / "runs" / "runs.jsonl").exists()
    hook.handle("PostToolUse", data)
    assert '"ev":"edit"' in (root / "runs" / "runs.jsonl").read_text()


def test_planted_git_in_repo_is_never_run(tmp_path, monkeypatch):
    """PATH에 . · 빈 칸이 있어도 시작 줄의 git 해시는 레포가 심은 git이 아니라 고정 PATH의 git으로."""
    root = _root(tmp_path)
    marker = tmp_path.parent / f"{tmp_path.name}-git-ran"
    (tmp_path / "git").write_text(f'#!/bin/sh\ntouch "{marker}"\necho deadbee\n')
    (tmp_path / "git").chmod(0o755)
    monkeypatch.chdir(tmp_path)
    for path in (".:/usr/bin:/bin", ":/usr/bin:/bin", "/usr/bin::/bin"):
        monkeypatch.setenv("PATH", path)
        hook.on_pre({"cwd": str(tmp_path), "tool_name": "Bash", "tool_use_id": "t1",
                     "tool_input": {"command": "python3 backtest.py"}})
        assert not marker.exists(), path
    assert (root / "runs" / "runs.jsonl").exists()


WRAP = '#!/bin/sh\nPATH="${PATH#*:}"\nexport PATH\nexec git "$@"\n'
"""git 래퍼(mise · asdf 심 꼴): 제 칸(PATH 맨 앞)을 빼고 PATH에서 git을 다시 찾는다."""

DROP = '#!/bin/sh\nPATH=$(printf %s "$PATH" | sed "s#$(dirname "$0")##")\nexport PATH\nexec git "$@"\n'
"""제 칸을 PATH 어디서든 지우는 래퍼: 사용자 PATH에 빈 칸이 없어도 지운 자리가 빈 칸(지금 폴더)이 된다."""


@pytest.mark.parametrize("case", ["래퍼 + .", "래퍼 + 빈 칸", "레포 안 절대 칸", "하위 .blackclip", "/.vol 별칭", "제 칸 지우는 래퍼"])
def test_planted_git_never_runs_whatever_the_user_path(case, tmp_path, monkeypatch):
    """git은 고정 PATH에서만 찾고 git에도 그 PATH만 넘겨, 사용자 PATH가 어떤 꼴이어도 레포가 심은 git은 훅 시작 줄 ·
    git 훑기 어디서도 돌지 않는다."""
    repo, marker = tmp_path / "repo", tmp_path / "git-ran"
    for d in (repo, repo / "bin"):
        d.mkdir(parents=True, exist_ok=True)
        (d / "git").write_text(f'#!/bin/sh\ntouch "{marker}"\necho deadbee\n')
        (d / "git").chmod(0o755)
    (repo / ".git").mkdir()
    cwd = repo / "sub" if case == "하위 .blackclip" else repo
    (cwd / ".blackclip").mkdir(parents=True)
    shim = tmp_path / "shim"
    shim.mkdir()
    (shim / "git").write_text(DROP if case == "제 칸 지우는 래퍼" else WRAP)
    (shim / "git").chmod(0o755)
    if case == "/.vol 별칭" and not Path("/.vol").is_dir():
        pytest.skip("/.vol은 macOS에만 있음")
    st = os.stat(repo / "bin")
    first = {"래퍼 + .": f"{shim}:.", "래퍼 + 빈 칸": f"{shim}:", "레포 안 절대 칸": str(repo / "bin"),
             "하위 .blackclip": str(repo / "bin"), "/.vol 별칭": f"/.vol/{st.st_dev}/{st.st_ino}", "제 칸 지우는 래퍼": str(shim)}
    monkeypatch.chdir(cwd)
    monkeypatch.setenv("PATH", f"{first[case]}:/usr/bin:/bin")
    hook.on_pre({"cwd": str(cwd), "tool_name": "Bash", "tool_use_id": "t1", "tool_input": {"command": "python3 backtest.py"}})
    assert not marker.exists(), "훅 시작 줄"
    gitlog.history(cwd, ["a.py"])
    assert not marker.exists(), "git 훑기"

def test_linked_strategies_folder_is_not_listed(tmp_path):
    """strategies 폴더가 레포 밖 링크면 그 안 이름을 세션 줄에 보이지 않는다."""
    root = _root(tmp_path)
    out = tmp_path.parent / f"{tmp_path.name}-strats"
    (out / "evil").mkdir(parents=True)
    (root / "strategies").symlink_to(out, target_is_directory=True)
    assert hook.session_line(tmp_path) == "blackclip: 전략 0개 · 훅이 본 백테스트 0회(최솟값)"

"""플러그인 파일: 매니페스트 · hooks.json 모양 · 래퍼(PATH에 엔진 있음 · 없음 · 고장) · 스킬의 명령 목록."""
import json
import re
import stat
import subprocess
import sys
from pathlib import Path

import blackclip
from blackclip.cli import build_parser

TOP = Path(__file__).resolve().parents[1]
PLUGIN = TOP / "plugin"
WRAPPER = PLUGIN / "hooks" / "blackclip-hook"


def _bin(tmp_path, body=None):
    """PATH에 둘 폴더: blackclip 심(이 레포 src의 cli.main을 부름) 또는 body로 만든 가짜."""
    d = tmp_path / "bin"
    d.mkdir()
    shim = d / "blackclip"
    shim.write_text(body or (f'#!/bin/sh\nexec "{sys.executable}" -c "import sys; sys.path.insert(0, \'{TOP / "src"}\'); '
                             'from blackclip.cli import main; sys.exit(main(sys.argv[1:]))" "$@"\n'))
    shim.chmod(0o755)
    return d


def _run(event, payload, path, cwd, **env):
    """래퍼를 PATH(와 HOME · CLAUDE_PROJECT_DIR)를 정해 돌린다."""
    return subprocess.run([str(WRAPPER), event], input=json.dumps(payload).encode(), capture_output=True, cwd=cwd,
                          env={"PATH": f"{path}:/usr/bin:/bin", "HOME": str(cwd), **env}, timeout=30)


def test_manifest_and_hooks_shape():
    """이름 · 판은 패키지와 같고, 훅 넷은 모두 래퍼 exec form · 3초, Pre는 동기 · Post 둘은 async."""
    m = json.loads((PLUGIN / ".claude-plugin" / "plugin.json").read_text())
    assert (m["name"], m["version"]) == ("blackclip-guardrail", blackclip.__version__) and m["author"]["name"]
    h = json.loads((PLUGIN / "hooks" / "hooks.json").read_text())["hooks"]
    got = {ev: (g["matcher"], hk["args"], hk.get("async", False)) for ev, groups in h.items() for g in groups
           for hk in g["hooks"]}
    assert got == {"SessionStart": ("startup|resume|clear|compact", ["SessionStart"], False),
                   "PreToolUse": ("Bash", ["PreToolUse"], False),
                   "PostToolUse": ("Bash|Edit|Write|NotebookEdit", ["PostToolUse"], True),
                   "PostToolUseFailure": ("Bash", ["PostToolUseFailure"], True)}
    assert {(hk["command"], hk["timeout"]) for groups in h.values() for g in groups for hk in g["hooks"]} == \
        {("${CLAUDE_PLUGIN_ROOT}/hooks/blackclip-hook", 3)}
    assert stat.S_IMODE(WRAPPER.stat().st_mode) == 0o755


def test_wrapper_without_engine(tmp_path):
    """PATH=/usr/bin:/bin · ~/.local/bin 어디에도 없으면 SessionStart만 「못 찾았어요」, 나머지는 조용히, 늘 0."""
    r = _run("SessionStart", {"cwd": str(tmp_path)}, "/usr/bin", tmp_path, CLAUDE_PROJECT_DIR=str(tmp_path))
    assert r.returncode == 0
    assert json.loads(r.stdout)["systemMessage"].startswith("blackclip 엔진을 못 찾았어요")
    r = _run("PreToolUse", {"tool_name": "Bash", "tool_input": {"command": "python3 backtest.py"}}, "/usr/bin", tmp_path)
    assert (r.returncode, r.stdout) == (0, b"")


def test_wrapper_finds_engine_in_home_local_bin(tmp_path):
    """PATH에 없으면 $HOME/.local/bin/blackclip(uv tool · pipx 기본 자리)으로 찾아 기록."""
    (tmp_path / ".git").mkdir()
    (tmp_path / ".blackclip").mkdir()
    (tmp_path / ".local").mkdir()
    _bin(tmp_path / ".local")
    p = {"cwd": str(tmp_path), "tool_name": "Bash", "tool_input": {"command": "python3 backtest.py"}, "tool_use_id": "t1"}
    assert _run("PreToolUse", p, "/usr/bin", tmp_path, CLAUDE_PROJECT_DIR=str(tmp_path)).returncode == 0
    rows = [json.loads(x) for x in (tmp_path / ".blackclip" / "runs" / "runs.jsonl").read_text().splitlines()]
    assert [r["id"] for r in rows] == ["t1"]


def test_wrapper_never_runs_an_engine_inside_the_opened_repo(tmp_path):
    """연 레포의 .venv/bin/blackclip은 PATH · ~/.local/bin에 없을 때도 실행하지 않는다(남의 레포가 심은 파일)."""
    repo, home = tmp_path / "repo", tmp_path / "home"
    (repo / ".venv").mkdir(parents=True)
    home.mkdir()
    marker = tmp_path / "ran"
    _bin(repo / ".venv", f'#!/bin/sh\ncat >/dev/null\ntouch "{marker}"\n')
    for event in ("SessionStart", "PreToolUse"):
        r = subprocess.run([str(WRAPPER), event], input=b"{}", capture_output=True, cwd=repo, timeout=30,
                           env={"PATH": "/usr/bin:/bin", "HOME": str(home), "CLAUDE_PROJECT_DIR": str(repo)})
        assert r.returncode == 0
    assert not marker.exists()


def test_wrapper_ignores_relative_path_entries(tmp_path):
    """PATH의 빈 칸 · . · 상대 폴더로 찾은 blackclip(연 레포 안 파일)은 실행하지 않는다."""
    repo, home = tmp_path / "repo", tmp_path / "home"
    repo.mkdir()
    home.mkdir()
    marker = tmp_path / "ran"
    planted = f'#!/bin/sh\ncat >/dev/null\ntouch "{marker}"\n'
    for d in (repo, repo / "bin"):
        d.mkdir(exist_ok=True)
        (d / "blackclip").write_text(planted)
        (d / "blackclip").chmod(0o755)
    for path in (".:/usr/bin:/bin", ":/usr/bin:/bin", "/usr/bin::/bin", "bin:/usr/bin:/bin"):
        r = subprocess.run([str(WRAPPER), "PreToolUse"], input=b"{}", capture_output=True, cwd=repo, timeout=30,
                           env={"PATH": path, "HOME": str(home), "CLAUDE_PROJECT_DIR": str(repo)})
        assert r.returncode == 0, path
        assert not marker.exists(), path


def test_wrapper_lookup_order_path_then_home_local(tmp_path):
    """순서 고정: 둘 다 있으면 PATH, PATH에 없으면 ~/.local/bin. ~/.local/bin 자리의 폴더는 엔진으로 치지 않음."""
    for d in ("p", ".local"):
        (tmp_path / d).mkdir()
        _bin(tmp_path / d, f'#!/bin/sh\ncat >/dev/null\necho \'{{"systemMessage": "{d}"}}\'\n')
    got = [json.loads(_run("SessionStart", {}, path, tmp_path).stdout)["systemMessage"]
           for path in (tmp_path / "p" / "bin", "/usr/bin")]
    assert got == ["p", ".local"]
    (tmp_path / ".local" / "bin" / "blackclip").unlink()
    (tmp_path / ".local" / "bin" / "blackclip").mkdir()
    assert "못 찾았어요" in json.loads(_run("SessionStart", {}, "/usr/bin", tmp_path).stdout)["systemMessage"]


def test_wrapper_with_engine_records_and_stays_silent(tmp_path):
    """엔진이 있으면 stdin을 그대로 넘겨 기록, Bash 훅 stdout 0바이트."""
    (tmp_path / ".git").mkdir()
    (tmp_path / ".blackclip").mkdir()
    p = {"cwd": str(tmp_path), "tool_name": "Bash", "tool_input": {"command": "python3 backtest.py"}, "tool_use_id": "t1"}
    path = _bin(tmp_path)
    pre = _run("PreToolUse", p, path, tmp_path)
    post = _run("PostToolUse", {**p, "tool_response": {"stdout": "ok"}}, path, tmp_path)
    assert (pre.returncode, pre.stdout, post.returncode, post.stdout) == (0, b"", 0, b"")
    rows = [json.loads(x) for x in (tmp_path / ".blackclip" / "runs" / "runs.jsonl").read_text().splitlines()]
    assert [(r["ev"], r["id"]) for r in rows] == [("start", "t1"), ("end", "t1")]
    s = _run("SessionStart", {"cwd": str(tmp_path)}, path, tmp_path)
    assert json.loads(s.stdout)["systemMessage"] == "blackclip: 전략 0개 · 훅이 본 백테스트 1회(최솟값)"


def test_wrapper_hides_stdout_of_a_failing_engine(tmp_path):
    """엔진이 additionalContext JSON을 stdout에 내고 종료 2여도 래퍼는 0 · SessionStart 밖 stdout 0바이트."""
    path = _bin(tmp_path, '#!/bin/sh\ncat >/dev/null\n'
                          'echo \'{"hookSpecificOutput": {"hookEventName": "PostToolUse", "additionalContext": "LEAK"}}\'\n'
                          'echo boom >&2\nexit 2\n')
    for event in ("PreToolUse", "PostToolUse", "PostToolUseFailure"):
        r = _run(event, {"tool_name": "Bash"}, path, tmp_path)
        assert (r.returncode, r.stdout) == (0, b"")
    r = _run("SessionStart", {}, path, tmp_path)
    assert r.returncode == 0 and b"LEAK" in r.stdout  # SessionStart 출력은 엔진 것을 그대로(맥락 한 줄이 그 길)


def test_skill_lists_only_real_subcommands():
    """스킬 본문의 `blackclip <명령>`은 cli에 있는 하위 명령만(status · chart · record가 생겨 예외 없음)."""
    text = (PLUGIN / "skills" / "blackclip" / "SKILL.md").read_text(encoding="utf-8")
    assert text.startswith("---\nname: blackclip\ndescription: ")
    real = set(build_parser()._subparsers._group_actions[0].choices)
    named = set(re.findall(r"`blackclip ([a-z]+)", text))
    assert named and named <= real
    assert {"status", "chart", "record", "dashboard"} <= named
    assert {"scan", "setup"} <= named and "@BotFather" in text and "`secrets`" in text  # 첫 셋업 순서 · 봇 안내
    assert "첫 회고" in text and "--days 90" in text and "실전 회고" not in text  # 석 달 첫 회고
    assert "「다음:」 아래에 붙는 줄" in text and "회고 없는 연결 전략" in text and "읽어 준다" in text  # setup 덧붙은 줄을 고객에게
    assert "blackclip/engine" in text and "레포 안을 뒤져" in text  # PATH에 없을 때 엔진 위치 파일로
    assert "`/`로 시작하고 `blackclip`으로 끝나는 절대 경로 한 줄일 때만" in text  # 모양이 다르면 돌리지 않고 여쭘


def test_dashboard_help_says_view_and_connect():
    """`blackclip --help`의 dashboard 줄은 읽기 전용이라 하지 않는다: 연결 POST 셋을 받는다."""
    text = " ".join(build_parser().format_help().split())
    assert "보기와 연결 화면: 이 컴퓨터 브라우저로 전략 상태 보기" in text and "읽기 전용" not in text


def test_skill_names_status_chart_record():
    """명령 · 「chart는 Bash로 돌리지 않음」 · 작은따옴표 인용 · record.md 공개 알림 · ④ 줄 먼저 · --decline · 「dashboard도 Bash로 돌리지 않음」.

    「어떻게 돼 가?」는 status 한 번으로 답하고 일별 파일 · days.jsonl을 직접 읽지 않는다(10-19 도그푸드).
    """
    text = (PLUGIN / "skills" / "blackclip" / "SKILL.md").read_text(encoding="utf-8")
    for must in ("blackclip status <전략>", "! blackclip chart <전략>", "blackclip chart`를 Bash로 돌리지 않는다",
                 "blackclip record <전략> --state", "--reason '…'", "고객 말은 작은따옴표로 감싸고",
                 "공개 레포면 이유 글이 보인다", "지난 전략에서 먼저 볼 것", "blackclip setup --decline",
                 "`blackclip status <전략>` 한 번으로 답한다", "직접 읽지 않는다", "종료 4",
                 "connect --status` · `blackclip notify`를 덧붙여 돌리지 않는다",
                 "`days.jsonl`을 열지 않는다", "파일로 대신 읽지 않는다", "끈 전략 줄 하나만 낸다",  # 출처 줄 · 종료 2 · 끈 전략
                 "--reason='…'", "record.md 머리 줄을 읽지 못해 덮어쓰지 않습니다",
                 "! blackclip dashboard", "`blackclip dashboard`를 Bash로 돌리지 않는다",  # 끝나지 않는 서버라 AI가 돌리면 멈춤
                 "서버 하나 · 기본 포트 8765 하나다", "포트가 이미 쓰이고 있으면",  # 한 서버 · 한 포트, 쓰이면 두 길(판별 프로브 없음)
                 "--port <다른 번호>를 붙여 다시 실행합니다", "그 창을 쓰고, 아니면 --port <다른 번호>로 띄워 주세요",  # 두 CLI 줄(cli.py)
                 "15분 뒤 닫히고 종료 1", "네가 Bash로 돌려도 된다",  # connect --open은 저장 뒤 끝나는 한 번용
                 "`blackclip connect --telegram`을 **백그라운드로** 돌려", "숫자를 읽어 고객에게 바로 전하고",  # 끝나기 전에 숫자 전달
                 "대시보드 회고 화면",  # 첫 회고를 --out으로 두면 회고 화면에 그 파일 위치가 보임
                 "저장된 토큰으로 대화만 잇는다", "--open(화면) · --status(상태) · --telegram(대화 잇기) 가운데 하나를 함께 씁니다",  # connect --telegram 규칙(cli.py 글)
                 "텔레그램 토큰이 없습니다 · blackclip connect --open --telegram"):  # 토큰이 없을 때 글(notify.py)
        assert must in text, must
    assert "8766" not in text and "읽기 전용" not in text  # 포트 둘 안내가 없어짐 · 대시보드는 이제 보기와 연결
    assert re.search(r"^\| `blackclip dashboard [^|]*\| ", text, re.M) and text.count("`blackclip dashboard`를 Bash로 돌리지 않는다") == 1  # 명령 표 한 줄 · 규칙 한 번
    assert text.count("\n- 「보수적 비용 기준 점수」 = 내보내기의 cost_one_way_bp, c3_* 칸이다(CSV와 JSON 둘 다).\n") == 1  # 정한 글 그대로 한 줄(덧붙인 행동 지시 없음)
    assert "`blackclip status`는 아직 없다" not in text and '--reason "' not in text and "비싸게 체결" not in text
    assert "daily/`의 가장 최근 파일" not in text
    head = text.split("---")[1]
    for word in ("끌래", "그만 쓸래", "중지", "은퇴", "전략 기록"):
        assert word in head, word

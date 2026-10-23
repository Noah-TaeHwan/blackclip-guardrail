"""blackclip setup: 단계 판정 · 다음 할 일 하나 · 체결 기록 갈래(첫 회고) · 읽어서 맞는 모양만 끝 ·
예약 미확인 · 전략 이름 거름 · 링크 안 따름 · 읽기만."""
import json
import os
from datetime import datetime, timezone

import pytest

from blackclip import stages
from blackclip.cli import main
from blackclip.notify import read_live
from blackclip.schedule import label
from blackclip.workspace import WorkspaceError, ensure, load_connections

LIVE = {"account": "main", "quote": "USDT", "taken_at": "2026-10-16T00:00:00+00:00", "start": "2026-10-16T00:00:00+00:00",
        "cash": "1000", "positions": {}}
"""blackclip live가 쓰는 모양(notify.read_live가 받는 최소)."""


def _fills(root, rows):
    """blackclip fetch가 쓴 것처럼 coverage.json과 체결 줄 rows개."""
    d = root / "fills" / "binance-main" / "spot"
    d.mkdir(parents=True)
    (d / "coverage.json").write_text(json.dumps({"BTCUSDT": {"from": "2026-09-16T00:00:00+00:00",
                                                             "until": "2026-10-16T00:00:00+00:00"}}))
    (d / "fills.csv").write_text("time,symbol,trade_id\n" + "".join(f"t{i},BTCUSDT,{i}\n" for i in range(rows)))


def _conns(root, **kv):
    """connections.json에 칸을 더한다."""
    p = root / "connections.json"
    data = json.loads(p.read_text()) if p.exists() else {}
    data.update(kv)
    p.write_text(json.dumps(data, ensure_ascii=False))


def _check_md(folder, ok=True):
    """blackclip check가 쓰는 check.md 모양(추천 가드레일 줄이 민감도 · 점검 불가)."""
    state = "민감도 보통" if ok else "점검 불가(C3를 셀 수 없음)"
    (folder / "check.md").write_text(f"# 점검 · {folder.name}\n\n| 점검 | 상태 | 근거 |\n| --- | --- | --- |\n"
                                     f"| 추천 가드레일 | {state} | x |\n")


GUARD = {"format": 1, "target": {"market": "spot", "symbols": ["BTCUSDT"], "bar_seconds": 86400},
         "warning_line": {"sensitivity": "보통", "line": -0.15, "lines": {"민감": -0.13, "보통": -0.15, "둔감": -0.19}},
         "period": {"mean": -0.0009, "std": 0.013}, "trade_frequency": {"days": 7, "low": 0, "high": 2}}
"""blackclip save가 쓰는 가드레일 중 watch · daily가 읽는 칸(나머지 칸은 생략)."""


def _guard(folder, version=1, **over):
    """가드레일 판 하나(GUARD + 전략 이름 · 판 번호, over로 칸을 바꿈)."""
    (folder / f"guardrail.v{version}.yaml").write_text(json.dumps(
        {**GUARD, "strategy": folder.name, "version": version, **over}, ensure_ascii=False))


def test_fresh_folder_starts_with_scan_and_writes_nothing(tmp_path, monkeypatch, capsys):
    """.blackclip이 없으면 「훑기」부터, 레포에는 아무것도 만들지 않음."""
    (tmp_path / ".git").mkdir()
    monkeypatch.chdir(tmp_path)
    assert main(["setup"]) == 0
    out = capsys.readouterr().out.splitlines()
    assert out[0] == "셋업: 8단계 중 0단계 끝 · 지금 「훑기」"
    assert out[2:4] == ["갈래: 체결 기록 모름(아직 안 받음)", "다음: blackclip scan . (연 폴더만 실행 없이 훑음, 다른 폴더는 고객이 고를 때만)"]
    assert not (tmp_path / ".blackclip").exists()


def test_steps_in_order_with_fills_branch(tmp_path, monkeypatch):
    """훑기 → 키 → 체결(있음) → 연결 → 첫 회고(retro.md) → 점검 → 가드레일 → 실전 시작 → 고르기 → 텔레그램 → 예약 → 끝(9단계)."""
    monkeypatch.setattr(stages.sys, "platform", "darwin")
    (tmp_path / ".git").mkdir()
    root = ensure(tmp_path)
    agents = tmp_path / "agents"
    agents.mkdir()
    seen = []

    def step():
        j = stages.judge(root, agents)
        seen.append(j["now"])
        return j

    step()
    (root / "map.json").write_text("{}")
    step()
    _conns(root, binance={"main": {"permission": {"status": "read_only"}}})
    assert step()["next"].startswith("blackclip fetch --symbols")
    _fills(root, 37)
    j = step()
    assert j["branch"] == "있음" and j["next"].startswith("실전에서 돌리는 전략만 먼저")
    s = root / "strategies" / "trend"
    s.mkdir(parents=True)
    (s / "link.py").write_text("")
    j = step()
    assert j["next"].startswith("blackclip retro --fills .blackclip/fills/binance-main/spot/fills.csv --backtest ")
    assert "--days 90 --out .blackclip/strategies/trend/retro.md (체결 기록 37건이라 첫 회고부터 해요)" in j["next"]
    (s / "retro.md").write_text("# 실행 회고 · 2026-09-16 ~ 2026-10-15 (UTC)\n")
    assert step()["next"] == "blackclip check trend"
    _check_md(s)
    step()
    _guard(s)
    j = step()
    assert "blackclip live trend --capital <이 전략 몫>" in j["next"] and "Ruling" not in j["next"]
    (s / "live.json").write_text(json.dumps(LIVE))
    assert step()["next"].startswith("blackclip connect --open --choose")
    _conns(root, defaults={"sensitivity": "보통"})
    assert "@BotFather" in step()["next"]
    _conns(root, telegram={"chat_id": 1})
    assert step()["next"].startswith("blackclip schedule")
    (agents / f"{label(tmp_path)}.plist").write_text("")
    j = step()
    assert seen == ["훑기", "조회 키", "체결 기록", "전략 연결", "첫 회고", "점검", "가드레일", "실전 시작", "예약 · 알림",
                    "예약 · 알림", "예약 · 알림", None]
    assert stages.render(j)[0] == "셋업: 9단계 중 9단계 끝"


def test_no_fills_branch_and_most_advanced_strategy(tmp_path, monkeypatch):
    """받았는데 체결 0건이면 「없음」 갈래(첫 회고 단계 없음), 전략이 여럿이면 가장 앞선 것(같으면 이름순 첫째)을 민다."""
    monkeypatch.setattr(stages.sys, "platform", "linux")
    (tmp_path / ".git").mkdir()
    root = ensure(tmp_path)
    (root / "map.json").write_text("{}")
    _conns(root, binance={"main": {"permission": {"status": "unknown"}}})
    _fills(root, 0)
    j = stages.judge(root)
    assert (j["branch"], j["now"], len(j["steps"])) == ("없음", "전략 연결", 8)
    assert j["next"].startswith("map.json 전략 후보 중 하나로")
    for name, checked in (("b", True), ("a", False), ("c", True)):
        (root / "strategies" / name).mkdir(parents=True)
        (root / "strategies" / name / "link.py").write_text("")
        if checked:
            _check_md(root / "strategies" / name)
    j = stages.judge(root)
    assert (j["now"], j["next"], j["strategy"]) == ("가드레일", "민감도를 먼저 고르기: blackclip connect --open --choose, "
                                                              "그다음 blackclip save b", "b")


def test_d81_fills_present_runs_retro_before_check(tmp_path):
    """체결 있음 + 연결 파일이면 다음 행동이 실제 `blackclip retro`(고치기 전에는 notes에만 붙이고 다음이 check)."""
    (tmp_path / ".git").mkdir()
    root = ensure(tmp_path)
    (root / "map.json").write_text("{}")
    _conns(root, binance={"main": {"permission": {"status": "read_only"}}})
    _fills(root, 37)
    (root / "strategies" / "trend").mkdir(parents=True)
    (root / "strategies" / "trend" / "link.py").write_text("")
    j = stages.judge(root)
    assert j["now"] == "첫 회고" and j["next"].startswith("blackclip retro ")
    (root / "strategies" / "trend" / "retro.md").write_text("손으로 만든 파일\n")  # 회고 머리줄이 아니면 근거 아님
    assert stages.judge(root)["now"] == "첫 회고"


def test_completion_needs_valid_files_and_schedule_is_tristate(tmp_path, monkeypatch):
    """빈 check.md · 점검 불가 check.md · {} 가드레일 · {} live.json은 끝이 아님, 리눅스 예약은 「미확인」(고치기 전에는 8단계 끝)."""
    monkeypatch.setattr(stages.sys, "platform", "linux")
    (tmp_path / ".git").mkdir()
    root = ensure(tmp_path)
    (root / "map.json").write_text("{}")
    _conns(root, binance={"main": {"permission": {"status": "read_only"}}}, defaults={"sensitivity": "보통"},
           telegram={"chat_id": 1})
    _fills(root, 0)
    s = root / "strategies" / "t"
    s.mkdir(parents=True)
    (s / "link.py").write_text("")
    (s / "check.md").write_text("")
    (s / "guardrail.v1.yaml").write_text("{}")
    (s / "live.json").write_text("{}")
    assert stages.judge(root)["now"] == "점검"
    _check_md(s, ok=False)
    j = stages.judge(root)
    assert j["now"] == "점검" and j["next"] == "check.md의 추천 가드레일이 점검 불가 · 연결 파일을 고친 뒤 blackclip check t"
    _check_md(s)
    assert stages.judge(root)["now"] == "가드레일"
    _guard(s)
    assert stages.judge(root)["now"] == "실전 시작"
    (s / "live.json").write_text(json.dumps(LIVE))
    j = stages.judge(root)
    head = stages.render(j)[0]
    assert head == "셋업: 8단계 중 7단계 끝 · 지금 「예약 · 알림」(미확인)" and "crontab 줄을 걸었는지 직접 확인" in j["next"]


def _ready(tmp_path):
    """훑기 · 조회 키 · 체결 기록(없음 갈래)까지 끝난 작업 폴더."""
    (tmp_path / ".git").mkdir()
    root = ensure(tmp_path)
    (root / "map.json").write_text("{}")
    _conns(root, binance={"main": {"permission": {"status": "read_only"}}})
    _fills(root, 0)
    return root


def test_unusable_guardrail_is_not_done(tmp_path):
    """watch가 쓰는 칸이 없거나 숫자가 아니거나 NaN이면 「가드레일」에 남음(2판은 warning_line.line 칸만 봐 끝)."""
    root = _ready(tmp_path)
    s = root / "strategies" / "s"
    s.mkdir(parents=True)
    (s / "link.py").write_text("")
    _check_md(s)
    bad = [{"warning_line": {"line": None}}, {"warning_line": {**GUARD["warning_line"], "line": "bad"}},
           {"warning_line": {**GUARD["warning_line"], "line": float("nan")}}, {"target": {"market": "spot"}},
           {"warning_line": {**GUARD["warning_line"], "sensitivity": "아주민감"}}, {"period": {"mean": 0, "std": float("inf")}}]
    for over in bad:
        _guard(s, **over)
        assert stages.judge(root)["now"] == "가드레일", over
    (s / "guardrail.v1.yaml").write_text(json.dumps({"format": 1, "strategy": "s", "version": 1, "warning_line": {"line": None}}))
    assert stages.judge(root)["now"] == "가드레일"
    _guard(s)
    assert stages.judge(root)["now"] == "실전 시작"


def test_guardrail_sensitivity_of_another_type_stays_at_the_step(tmp_path):
    """warning_line.sensitivity가 [] · {}면 예외 없이 「가드레일」에 남음(3판은 dict 멤버십에서 TypeError로 setup이 멈춤)."""
    root = _ready(tmp_path)
    s = root / "strategies" / "s"
    s.mkdir(parents=True)
    (s / "link.py").write_text("")
    _check_md(s)
    for bad in ([], {}):
        _guard(s, warning_line={**GUARD["warning_line"], "sensitivity": bad})
        assert stages.judge(root)["now"] == "가드레일", bad


def test_live_file_is_read_within_a_cap(tmp_path):
    """live.json은 상한(1MiB) 안의 일반 파일만 읽음 — 맞는 JSON 뒤에 공백 1MiB를 붙이면 끝이 아님(2판은 통째로 읽어 끝)."""
    root = _ready(tmp_path)
    s = root / "strategies" / "s"
    s.mkdir(parents=True)
    (s / "link.py").write_text("")
    _check_md(s)
    _guard(s)
    (s / "live.json").write_text(json.dumps(LIVE) + " " * (1 << 20))
    assert stages.judge(root)["now"] == "실전 시작"
    (s / "live.json").write_text(json.dumps(LIVE))
    assert stages.judge(root)["now"] == "예약 · 알림"


def test_linked_workspace_reads_nothing_not_even_a_fallback_name(tmp_path):
    """.blackclip이 링크면 파일을 하나도 안 읽고 처음 상태 — 레포가 2판의 대체 이름 폴더를 링크로 심어도 안 따름."""
    real = tmp_path / "real"
    (real / ".git").mkdir(parents=True)
    root = ensure(real)
    (root / "map.json").write_text("{}")
    (root / "connections.json").write_text(json.dumps({"binance": {"main": {"permission": {"status": "x"}}}}))
    repo = tmp_path / "repo"
    (repo / ".git").mkdir(parents=True)
    (repo / ".blackclip").symlink_to(root, target_is_directory=True)
    (repo / ".blackclip-링크라-안-읽음").symlink_to(root, target_is_directory=True)
    j = stages.judge(repo / ".blackclip")
    assert j["done"] == [False] * 8 and j["now"] == "훑기" and j["next"].startswith("레포의 .blackclip 심볼릭 링크를 치운 뒤")


def test_strategy_names_from_the_repo_are_filtered(tmp_path):
    """`s;id` 같은 레포 폴더 이름은 다음 명령에 안 들어감(고치기 전에는 `blackclip check s;id`를 냄)."""
    (tmp_path / ".git").mkdir()
    root = ensure(tmp_path)
    (root / "strategies" / "s;id").mkdir(parents=True)
    (root / "strategies" / "s;id" / "link.py").write_text("")
    j = stages.judge(root)
    assert all("s;id" not in line for line in stages.render(j))
    assert j["strategy"] is None and any("이름 모양이 다른 전략 폴더 1개" in n for n in j["notes"])


def test_linked_state_files_and_workspace_are_not_followed(tmp_path):
    """connections.json이 레포 밖 파일 링크면 안 읽고 메모(고치기 전에는 따라가 고른 값으로 침), .blackclip이 링크면 아무것도 안 읽음."""
    (tmp_path / ".git").mkdir()
    root = ensure(tmp_path)
    outside = tmp_path / "outside.json"
    outside.write_text(json.dumps({"defaults": {"sensitivity": "민감"}, "binance": {"main": {"permission": {"status": "x"}}}}))
    (root / "connections.json").symlink_to(outside)
    j = stages.judge(root)
    assert j["now"] == "훑기" and j["done"][1] is False and "심볼릭 링크" in j["notes"][0]
    other = tmp_path / "repo2"
    (other / ".git").mkdir(parents=True)
    (other / ".blackclip").symlink_to(root, target_is_directory=True)
    (root / "map.json").write_text("{}")
    j = stages.judge(other / ".blackclip")
    assert j["done"][0] is False and j["notes"][0].startswith("작업 폴더 .blackclip이 심볼릭 링크")


def test_broken_connections_file_is_a_note_not_a_crash(tmp_path):
    """깨진 connections.json은 한 줄 메모, 판정은 이어 감."""
    (tmp_path / ".git").mkdir()
    root = ensure(tmp_path)
    (root / "connections.json").write_text("{broken")
    j = stages.judge(root)
    assert j["now"] == "훑기" and j["notes"][0].startswith("연결 상태 파일을 못 읽음(")


DEEP = "[" * 200000
"""1MiB 상한 아래인데 json.loads가 RecursionError를 내는 깊은 중첩(레포가 심을 수 있음)."""


def test_deeply_nested_state_files_do_not_crash_setup(tmp_path, monkeypatch, capsys):
    """coverage · 가드레일 · live.json이 깊게 중첩돼도 traceback 없이 종료 0, 맞는 모양이 아니니 끝으로 안 침."""
    (tmp_path / ".git").mkdir()
    root = ensure(tmp_path)
    (root / "map.json").write_text("{}")
    _conns(root, binance={"main": {"permission": {"status": "read_only"}}})
    _fills(root, 0)
    (root / "fills" / "binance-main" / "spot" / "coverage.json").write_text(DEEP)
    for name, deep_guard in (("s", True), ("t", False)):  # s: 가드레일이 깊음, t: 가드레일은 맞고 live.json이 깊음
        d = root / "strategies" / name
        d.mkdir(parents=True)
        (d / "link.py").write_text("")
        _check_md(d)
        if deep_guard:
            (d / "guardrail.v1.yaml").write_text(DEEP)
        else:
            _guard(d)
            (d / "live.json").write_text(DEEP)
    assert read_live(root, "t") is None
    monkeypatch.chdir(tmp_path)
    assert main(["setup"]) == 0
    assert "Traceback" not in capsys.readouterr().err
    j = stages.judge(root)
    assert (j["strategy"], j["done"]) == ("t", [True, True, False, True, True, True, False, False])


def test_deeply_nested_connections_is_a_note_not_a_crash(tmp_path, monkeypatch, capsys):
    """connections.json이 깊게 중첩되면 load_connections는 WorkspaceError(다른 JSON 깨짐과 같은 문체), setup은 메모 한 줄에 종료 0."""
    (tmp_path / ".git").mkdir()
    root = ensure(tmp_path)
    (root / "connections.json").write_text(DEEP)
    with pytest.raises(WorkspaceError, match="중첩"):
        load_connections(root)
    monkeypatch.chdir(tmp_path)
    assert main(["setup"]) == 0
    out = capsys.readouterr().out
    assert "연결 상태 파일을 못 읽음(" in out and "중첩" in out


@pytest.mark.skipif(os.geteuid() == 0, reason="root는 권한 없는 폴더도 읽음")
def test_unreadable_strategies_folder_exits_2_with_a_short_message(tmp_path, monkeypatch, capsys):
    """읽기 권한 없는 폴더는 traceback이 아니라 한국어 한 줄과 종료 2."""
    (tmp_path / ".git").mkdir()
    sdir = ensure(tmp_path) / "strategies"
    sdir.mkdir()
    monkeypatch.chdir(tmp_path)
    sdir.chmod(0)
    try:
        assert main(["setup"]) == 2
    finally:
        sdir.chmod(0o755)
    err = capsys.readouterr().err
    assert err.startswith("blackclip setup: 작업 폴더를 읽지 못했습니다(") and "Traceback" not in err


def test_guardrail_numbers_that_watch_compares_must_be_numbers(tmp_path):
    """trade_frequency low · high(없음 None은 허용: 청산 없는 주)와 warning_line.lines 모든 값이 유한한 숫자여야 끝."""
    root = _ready(tmp_path)
    s = root / "strategies" / "s"
    s.mkdir(parents=True)
    (s / "link.py").write_text("")
    _check_md(s)
    lines = GUARD["warning_line"]["lines"]
    bad = [{"trade_frequency": {"days": 7, "low": "0", "high": 2}}, {"trade_frequency": {"days": 7, "low": 0, "high": float("nan")}},
           {"warning_line": {**GUARD["warning_line"], "lines": {**lines, "extra": "x"}}}]
    for over in bad:
        _guard(s, **over)
        assert stages.judge(root)["now"] == "가드레일", over
    _guard(s, trade_frequency={"days": 7, "low": None, "high": None})
    assert stages.judge(root)["now"] == "실전 시작"


def test_non_object_state_values_count_as_not_chosen(tmp_path, monkeypatch, capsys):
    """defaults · telegram · binance가 객체가 아니면 예외 없이 「고르지 않음 · 연결 안 됨 · 키 없음」, 종료 0."""
    monkeypatch.setattr(stages.sys, "platform", "linux")
    (tmp_path / ".git").mkdir()
    root = ensure(tmp_path)
    (root / "map.json").write_text("{}")
    _fills(root, 0)
    s = root / "strategies" / "s"
    s.mkdir(parents=True)
    (s / "link.py").write_text("")
    _check_md(s)
    _guard(s)
    (s / "live.json").write_text(json.dumps(LIVE))
    monkeypatch.chdir(tmp_path)
    _conns(root, binance=5, defaults="x", telegram=[1])
    assert main(["setup"]) == 0 and stages.judge(root)["now"] == "조회 키"
    _conns(root, binance={"main": {"permission": {"status": "read_only"}}})
    assert main(["setup"]) == 0 and stages.judge(root)["next"].startswith("blackclip connect --open --choose")
    _conns(root, defaults={"sensitivity": "보통"})
    assert main(["setup"]) == 0 and stages.judge(root)["next"] == stages.BOTFATHER
    capsys.readouterr()


def test_cli_with_linked_workspace_reads_nothing_and_writes_nothing(tmp_path, monkeypatch, capsys):
    """CLI(main)로 불러도 .blackclip이 링크면 종료 0, 치우라는 안내, 상태 파일 읽기 함수는 한 번도 안 불리고 레포는 그대로."""
    real = tmp_path / "real"
    (real / ".git").mkdir(parents=True)
    root = ensure(real)
    (root / "map.json").write_text("{}")
    _conns(root, binance={"main": {"permission": {"status": "read_only"}}})
    repo = tmp_path / "repo"
    (repo / ".git").mkdir(parents=True)
    (repo / ".blackclip").symlink_to(root, target_is_directory=True)

    def boom(*args, **kwargs):
        raise AssertionError("링크된 작업 폴더는 읽지 않음")

    for fn in ("_read_regular", "_regular", "load_connections", "read_live", "has_retro"):
        monkeypatch.setattr(stages, fn, boom)
    before = sorted(p.name for p in repo.iterdir())
    monkeypatch.chdir(repo)
    assert main(["setup"]) == 0
    out = capsys.readouterr().out
    assert out.splitlines()[0] == "셋업: 8단계 중 0단계 끝 · 지금 「훑기」" and "레포의 .blackclip 심볼릭 링크를 치운 뒤" in out
    assert sorted(p.name for p in repo.iterdir()) == before


@pytest.mark.parametrize("over", [
    {"target": {**GUARD["target"], "bar_seconds": 10**400}},
    {"warning_line": {**GUARD["warning_line"], "line": 10**400}},
    {"warning_line": {**GUARD["warning_line"], "lines": {"민감": -0.13, "보통": -0.15, "둔감": 10**400}}},
    {"period": {"mean": 10**400, "std": 0.013}},
    {"trade_frequency": {"days": 7, "low": 0, "high": 10**400}},
], ids=["bar_seconds", "line", "lines", "period", "trade_frequency"])
def test_huge_integer_in_guardrail_stays_at_the_step(tmp_path, monkeypatch, capsys, over):
    """309자리를 넘는 정수는 float로 못 바꿔 OverflowError였음 — 유한한 숫자가 아니므로 「가드레일」에 남고 setup은 종료 0."""
    root = _ready(tmp_path)
    s = root / "strategies" / "s"
    s.mkdir(parents=True)
    (s / "link.py").write_text("")
    _check_md(s)
    _guard(s, **over)
    assert stages.judge(root)["now"] == "가드레일"
    monkeypatch.chdir(tmp_path)
    assert main(["setup"]) == 0
    assert "Traceback" not in capsys.readouterr().err


NOW = datetime(2026, 10, 16, 15, 30, tzinfo=timezone.utc)
"""judge에 넣는 지금(시험용 시계). 석 달 전 0시 UTC = 2026-07-18T00:00:00+00:00."""


def test_fills_step_suggests_since_three_months_ago(tmp_path):
    """「체결 기록」 다음 할 일은 지난 석 달(90일 전 0시 UTC)부터 받기 — 시계는 judge에 넣음, 같은 날이면 몇 시든 같은 값."""
    (tmp_path / ".git").mkdir()
    root = ensure(tmp_path)
    (root / "map.json").write_text("{}")
    _conns(root, binance={"main": {"permission": {"status": "read_only"}}})
    want = ("blackclip fetch --symbols <봇이 거래하는 종목>(예: BTCUSDT) --since 2026-07-18T00:00:00+00:00 "
            "(지난 석 달 체결을 받아 체결 기록이 있는지에 따라 다음 길을 정해요)")
    assert stages.judge(root, now=NOW)["next"] == want
    assert stages.judge(root, now=NOW.replace(hour=0, minute=0))["next"] == want
    assert "지난 30일" not in stages.judge(root, now=NOW)["next"]


def _bots(tmp_path, names, retro=()):
    """체결 있음 갈래에서 names 전략을 모두 연결하고, retro에 든 이름만 회고(retro.md)를 둔다."""
    (tmp_path / ".git").mkdir()
    root = ensure(tmp_path)
    (root / "map.json").write_text("{}")
    _conns(root, binance={"main": {"permission": {"status": "read_only"}}})
    _fills(root, 5)
    for n in names:
        d = root / "strategies" / n
        d.mkdir(parents=True)
        (d / "link.py").write_text("")
        if n in retro:
            (d / "retro.md").write_text("# 실행 회고 · 2026-07-18 ~ 2026-10-15 (UTC)\n")
    return root


def test_first_retro_step_is_done_by_one_retro_and_notes_the_rest(tmp_path):
    """회고 하나면 「첫 회고」 끝 — 설계대로 연구용 전략을 이어서 연결해도 셋업이 되돌아가지 않고, 회고 없는 연결 전략은 알림 줄로만."""
    root = _bots(tmp_path, ["a", "b", "c"])
    (root / "strategies" / "loose").mkdir()  # link.py 없음: 연결된 전략이 아님
    (root / "strategies" / "loose" / "daily").mkdir()
    j = stages.judge(root, now=NOW)
    assert (j["now"], j["strategy"], j["done"][4]) == ("첫 회고", "a", False)
    assert "--days 90 --out .blackclip/strategies/a/retro.md" in j["next"] and not j["notes"]
    assert stages.render(j)[0] == "셋업: 9단계 중 4단계 끝 · 지금 「첫 회고」"
    d = root / "strategies" / "b" / "daily"  # daily 파일도 같은 근거(first_retro와 같음)
    d.mkdir()
    (d / "2026-10-15.md").write_text("x")
    j = stages.judge(root, now=NOW)
    assert (j["now"], j["done"][4]) == ("점검", True)
    assert j["notes"] == ["회고 없는 연결 전략 2개(a, c): 실전 봇이면 blackclip retro로 더하고, 연구용이면 그대로 둠"]
    for n in ("r1", "r2", "r3", "r4"):  # 연구용 전략을 더 연결해도 단계는 그대로, 알림은 다섯까지 이름
        (root / "strategies" / n).mkdir()
        (root / "strategies" / n / "link.py").write_text("")
    j = stages.judge(root, now=NOW)
    assert j["now"] == "점검" and j["notes"][0].startswith("회고 없는 연결 전략 6개(a, c, r1, r2, r3 등)")


def test_first_retro_gate_for_alerts_stays_any_one(tmp_path):
    """실행 경고를 여는 notify.first_retro와 셋업 「첫 회고」 단계가 같은 기준(연결된 전략 중 하나라도 회고)."""
    from blackclip.notify import first_retro
    root = _bots(tmp_path, ["a", "b"], retro=["a"])
    assert first_retro(root) is True and stages.judge(root, now=NOW)["done"][4] is True


def test_guardrail_step_asks_to_choose_sensitivity_first(tmp_path):
    """민감도를 안 골랐으면 「가드레일」 다음 할 일이 고르는 화면 먼저, 골랐으면 save만."""
    root = _bots(tmp_path, ["s"], retro=["s"])
    _check_md(root / "strategies" / "s")
    j = stages.judge(root, now=NOW)
    assert (j["now"], j["next"]) == ("가드레일", "민감도를 먼저 고르기: blackclip connect --open --choose, 그다음 blackclip save s")
    _conns(root, defaults={"sensitivity": "민감"})
    assert stages.judge(root, now=NOW)["next"] == "blackclip save s"


def test_no_fills_yet_does_not_read_retro_folders(tmp_path, monkeypatch):
    """회고는 체결 있음 갈래에서만 본다 — 체결을 받기 전에 daily/를 못 읽어도 setup은 그대로 간다."""
    if os.geteuid() == 0:
        pytest.skip("root는 권한을 무시함")
    monkeypatch.setattr(stages.sys, "platform", "linux")
    (tmp_path / ".git").mkdir()
    root = ensure(tmp_path)
    (root / "map.json").write_text("{}")
    d = root / "strategies" / "a"
    (d / "daily").mkdir(parents=True)
    (d / "link.py").write_text("")
    (d / "daily").chmod(0)
    try:
        j = stages.judge(root)
    finally:
        (d / "daily").chmod(0o755)
    assert (j["branch"], j["now"]) == (None, "조회 키")


def test_key_step_points_to_keyless_first(tmp_path, monkeypatch):
    """조회 키 단계의 다음 할 일: 키 없이 붙이기를 먼저. 선(follow.json)이 있으면 키 없는 길의 남은 것(예약 → 텔레그램)을 하나씩,
    다 붙이면 「키 없이 지켜보기 끝」이고 「지금 「조회 키」」는 없다(키는 고를 때만)."""
    monkeypatch.setattr(stages.sys, "platform", "darwin")
    agents = tmp_path / "agents"
    agents.mkdir()
    (tmp_path / ".git").mkdir()
    root = ensure(tmp_path)
    (root / "map.json").write_text("{}")
    j = stages.judge(root, agents)
    assert j["now"] == "조회 키" and j["next"].startswith("돌고 있는 봇이 있으면 키 없이 먼저 붙이기(")
    assert j["next"].index("blackclip follow") < j["next"].index("blackclip connect --open")
    assert j["keyless"] is None
    s = root / "strategies" / "bot1"
    s.mkdir(parents=True)
    (s / "follow.json").write_text('{"quiet_minutes": 90}')
    j = stages.judge(root, agents)
    assert stages.render(j)[0] == "셋업: 키 없이 지켜보기(bot1) · 남은 것 예약 · 텔레그램"
    assert j["next"] == "blackclip schedule (5분마다 blackclip follow)"
    (agents / f"{label(tmp_path, 'follow')}.plist").write_bytes(b"")
    assert stages.judge(root, agents)["next"].startswith(stages.BOTFATHER)
    _conns(root, telegram={"chat_id": 1})
    lines = stages.render(stages.judge(root, agents))
    assert lines[0] == "셋업: 키 없이 지켜보기(bot1) 끝"
    assert lines[1].startswith("고를 때만(실제 계좌와 맞대 보기): 훑기 끝 · 조회 키 전")
    assert lines[3] == "다음: 실제 계좌와도 맞대 보려면(고를 때만) blackclip connect --open"
    assert not any("지금 「조회 키」" in line for line in lines)

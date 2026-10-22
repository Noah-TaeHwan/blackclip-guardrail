"""blackclip chart: D 목록 · C 게이지 · 색 묶음 · NO_COLOR."""
import copy
import json
import os
import re
import time
from datetime import timedelta

import pytest
from facts import SPARK, START, record, write_facts
from facts import guardrail as guardrail_of

from blackclip.cli import main
from blackclip.screen.paint import PALETTE, cells
from blackclip.states import TRACKED

D = [
    "━━ D. 스파크라인 목록  실전 4개 한눈에",
    "",
    "  전략          9/23 ~ 10/14              손익  실제-BT   상태",
    "  breakout_v2   ▁▂▃▂▃▅▃▅▆▅▅▆▅▆▆▅▆▆▆···   +0.4%  10/11까지 ⬚ 확인 불가(체결 내역을 못 받음)",
    "  trend_10_50   ▆▆▅▆▆▅▅▅▅▅▅▄▅▄▄▄▄▄▄▃▁▁   -4.6%  -2.7%p    ▣ 경고 · 실행 문제로 추정",
    "  grid_v1       ▆▅▆▅▅▅▄▅▄▃▄▃▃▃▃▂▃▂▂▂▂▁   -0.8%  -0.3%p    ⬓ 판단 보류",
    "  trend_ema_v7  ▁▁▁▂▂▂▃▃▃▃▄▃▄▅▄▅▆▅▆▆▆▆   +1.2%  -0.2%p    ✓ 정상",
    "",
    "  줄마다 자기 눈금(모양만 본다, 크기는 옆 숫자) · 점(·)은 매일 대조 기록이 없거나 실제 손익을 모르는 날 · 실전 시작부터",
]

C = [
    "━━ C. 게이지 한 줄  지금 어디쯤 · 이번 구간 기준",
    "",
    "  trend_10_50 ▕┃────●──┣━━◆━━━━━━━━━━━━━━━━━┫▏ 범위 아래 · 경고선까지 2.6%p · 경고 · 실행 문제로 추정",
    "  ┃ 경고선 -7.2%  ● 실제 -4.6%  ┣━┫ 정상 범위 -3.2% ~ +6.9%(22일째)  ◆ 같은 기간 BT -1.9%",
]


@pytest.fixture(autouse=True)
def _env(monkeypatch, tmp_path):
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    monkeypatch.setenv("COLUMNS", "120")
    monkeypatch.setenv("LINES", "40")


def _chart(tmp_path, monkeypatch, capsys, *args, no_color="1"):
    monkeypatch.chdir(tmp_path)
    if no_color is None:
        monkeypatch.delenv("NO_COLOR", raising=False)
    else:
        monkeypatch.setenv("NO_COLOR", no_color)
    code = main(["chart", *args])
    out = capsys.readouterr()
    return code, out.out, out.err


def test_chart_d_golden_without_color(tmp_path, monkeypatch, capsys):
    write_facts(tmp_path)
    code, out, err = _chart(tmp_path, monkeypatch, capsys)
    assert (code, err) == (0, "") and out.splitlines() == D and "\x1b" not in out


def test_chart_c_golden_is_the_last_block(tmp_path, monkeypatch, capsys):
    write_facts(tmp_path)
    code, out, _ = _chart(tmp_path, monkeypatch, capsys, "trend_10_50")
    assert code == 0 and out.splitlines()[-4:] == C


def test_chart_color_groups_escapes(tmp_path, monkeypatch, capsys):
    """색을 켜도 글자는 같고, 같은 모양 칸은 한 이스케이프."""
    write_facts(tmp_path)
    _, plain, _ = _chart(tmp_path, monkeypatch, capsys)
    _, colored, _ = _chart(tmp_path, monkeypatch, capsys, no_color=None)
    import re
    assert re.sub(r"\x1b\[[0-9;]*m", "", colored) == plain
    row = colored.splitlines()[4]  # trend_10_50 줄
    assert row.count("\x1b[0;") <= 6
    assert len(colored.encode()) < 3 * len(plain.encode())


def test_chart_narrow_terminal_cuts_d_rows(tmp_path, monkeypatch, capsys):
    write_facts(tmp_path)
    monkeypatch.setenv("COLUMNS", "60")
    _, out, _ = _chart(tmp_path, monkeypatch, capsys)
    assert all(cells(line) <= 60 for line in out.splitlines())
    assert out.splitlines()[3].endswith("…")


def test_chart_live_without_records_vs_before_live(tmp_path, monkeypatch, capsys):
    """live.json이 있으면 「실전 중 · 기록 아직 없음」, 없으면 「실전 시작 전」."""
    root = write_facts(tmp_path, ("grid_v1",))
    (root / "strategies" / "grid_v1" / "daily" / "days.jsonl").unlink()
    assert _chart(tmp_path, monkeypatch, capsys, "grid_v1")[1] == \
        "grid_v1 · 실전 중 · 매일 대조 기록 아직 없음(첫 blackclip daily 뒤에 보임)\n"
    (root / "strategies" / "grid_v1" / "live.json").unlink()
    assert _chart(tmp_path, monkeypatch, capsys, "grid_v1")[1] == "grid_v1 · 실전 시작 전 · 점검 결과는 blackclip check grid_v1\n"
    (root / "strategies" / "grid_v1" / "guardrail.v1.yaml").unlink()
    assert _chart(tmp_path, monkeypatch, capsys)[1] == "실전 전략 없음 · blackclip save <전략> 뒤 blackclip live <전략>\n"


OFF = "끈 전략 {n}개 · 자세히는 blackclip status"


def _stop(tmp_path, monkeypatch, capsys, *names, state="중지"):
    for n in names:
        monkeypatch.chdir(tmp_path)
        assert main(["record", n, "--state", state]) == 0
    capsys.readouterr()


def test_chart_d_leaves_stopped_strategies_out_with_one_line(tmp_path, monkeypatch, capsys):
    """전체 보기 목록 · 「실전 N개」에서 끈 전략(중지 · 은퇴)은 빠지고 끝에 한 줄. 이름을 준 보기는 그대로 그림."""
    write_facts(tmp_path, ("grid_v1", "trend_10_50", "breakout_v2"))
    _stop(tmp_path, monkeypatch, capsys, "trend_10_50")
    _stop(tmp_path, monkeypatch, capsys, "breakout_v2", state="은퇴")
    code, out, err = _chart(tmp_path, monkeypatch, capsys)
    lines = out.splitlines()
    assert (code, err) == (0, "") and lines[0] == "━━ D. 스파크라인 목록  실전 1개 한눈에"
    assert "trend_10_50" not in out and "breakout_v2" not in out and "grid_v1" in out
    assert lines[-1] == OFF.format(n=2) and "\x1b" not in out
    code, named, _ = _chart(tmp_path, monkeypatch, capsys, "trend_10_50")
    assert code == 0 and "A. " in named


def test_chart_d_all_stopped_says_no_live_and_off_line(tmp_path, monkeypatch, capsys):
    """전부 끄면 NO_LIVE 줄 + 끈 전략 줄."""
    write_facts(tmp_path, ("grid_v1", "trend_10_50"))
    _stop(tmp_path, monkeypatch, capsys, "grid_v1", "trend_10_50")
    code, out, _ = _chart(tmp_path, monkeypatch, capsys)
    assert code == 0 and out.splitlines() == ["실전 전략 없음 · blackclip save <전략> 뒤 blackclip live <전략>", OFF.format(n=2)]


def _break_record(root, name):
    """record.md를 읽을 수 없게 덮는다(tests/test_record.py 틀): 그 전략은 「기록 손상」으로 끈 전략 줄에 간다."""
    (root / "strategies" / name / "record.md").write_text("garbage", encoding="utf-8")


def test_chart_overview_broken_exits_4(tmp_path, monkeypatch, capsys):
    """전체 보기는 기록 손상 전략이 하나라도 있으면 status처럼 종료 4. 나머지는 평소대로 그리고, 이름을 준 보기는 그대로 0."""
    root = write_facts(tmp_path, ("grid_v1", "trend_10_50", "breakout_v2"))
    _break_record(root, "grid_v1")
    code, out, err = _chart(tmp_path, monkeypatch, capsys)
    lines = out.splitlines()
    assert (code, err) == (4, "") and lines[0] == "━━ D. 스파크라인 목록  실전 2개 한눈에" and lines[-1] == OFF.format(n=1)
    assert "grid_v1" not in out
    assert _chart(tmp_path, monkeypatch, capsys, "grid_v1")[0] == 0 and _chart(tmp_path, monkeypatch, capsys, "trend_10_50")[0] == 0


def test_chart_overview_all_off_broken_exits_4(tmp_path, monkeypatch, capsys):
    """전부 꺼짐 + 기록 손상(조기 반환)도 종료 4. 손상이 아닌 중지만 있으면 0."""
    root = write_facts(tmp_path, ("grid_v1",))
    _break_record(root, "grid_v1")
    code, out, err = _chart(tmp_path, monkeypatch, capsys)
    assert (code, err) == (4, "") and out.splitlines() == ["실전 전략 없음 · blackclip save <전략> 뒤 blackclip live <전략>", OFF.format(n=1)]
    write_facts(tmp_path / "stopped", ("grid_v1",))
    _stop(tmp_path / "stopped", monkeypatch, capsys, "grid_v1")
    assert _chart(tmp_path / "stopped", monkeypatch, capsys)[0] == 0


def test_chart_bad_days_file_is_one_line(tmp_path, monkeypatch, capsys):
    root = write_facts(tmp_path, ("grid_v1",))
    p = root / "strategies" / "grid_v1" / "daily" / "days.jsonl"
    p.unlink()
    p.symlink_to(root / "strategies" / "grid_v1" / "guardrail.v1.yaml")
    code, out, err = _chart(tmp_path, monkeypatch, capsys, "grid_v1")
    assert code == 2 and out == "" and err.startswith("blackclip chart: ") and err.count("\n") == 1


def _c_block(out):
    """전략 화면은 A · B 뒤에 C가 오므로 C 블록만 뗀다(머리 줄이 좁은 터미널에서 접혀도 접두 「━━ C.」로 찾음)."""
    lines = out.splitlines()
    return lines[next(i for i, line in enumerate(lines) if line.startswith("━━ C.")):]


# --- 추가 시험(구현이 고른 동작을 묶음) ---


def _days(root, name):
    return root / "strategies" / name / "daily" / "days.jsonl"


def _append(root, name, rec):
    with _days(root, name).open("a", encoding="utf-8") as f:
        f.write(json.dumps(rec, ensure_ascii=False) + "\n")


def _failed(name, day, k):
    """대조가 실패한 날(nums 비어 있음)의 줄."""
    why = "확인 불가(대조하지 못함 · KeyError)"
    rec = record(name, 22)
    rec.update(day=day, live_day=k, trades=0, head="실제 모름 · 같은 기간 BT 모름", rep=why, nums={},
               states={t: why for t in TRACKED})
    return rec


def test_chart_failed_last_day_is_not_shown_as_current(tmp_path, monkeypatch, capsys):
    """마지막 날이 대조 실패면 C는 옛 숫자를 지금처럼 그리지 않고, D는 그 날을 빈 점으로 두고 손익에 「까지」를 붙인다."""
    root = write_facts(tmp_path, ("trend_10_50",))
    p = _days(root, "trend_10_50")
    p.write_text("".join(p.read_text(encoding="utf-8").splitlines(keepends=True)[1:]), encoding="utf-8")  # 22일을 넘지 않게 첫날을 뺌
    _append(root, "trend_10_50", _failed("trend_10_50", "2026-10-15", 23))
    _, out, _ = _chart(tmp_path, monkeypatch, capsys, "trend_10_50")
    assert out.splitlines()[0].startswith("━━ A.") and "10/15(23일째)" in out.splitlines()[2]  # A · B는 앞 날들을 그림
    assert out.splitlines()[-1] == "trend_10_50 · 이번 구간 숫자 없음(마지막 매일 대조: 확인 불가(대조하지 못함 · KeyError))"
    a, b = (part.splitlines() for part in out.split("━━ B."))  # C는 한 줄이라 머리 줄이 없음
    assert any(x.endswith("━ 실제 모름") for x in a)  # A 라벨은 마지막 날 값을 모름으로(옛 숫자를 지금처럼 말하지 않음)
    assert any(x.endswith("합계(10/14까지) -2.7%p") for x in b)  # B 합계는 마지막으로 확인된 날을 붙임
    assert not any("나머지 -2.1%p" in x for x in b)  # 나머지 · 조금씩 라벨은 모름
    _, out, _ = _chart(tmp_path, monkeypatch, capsys)
    lines = out.splitlines()
    assert "9/24 ~ 10/15" in lines[2]
    assert "▆▅▆▆▅▅▅▅▅▅▄▅▄▄▄▄▄▄▃▁▁·" in lines[3] and "-4.6%  10/14까지 ⬚ 확인 불가(대조하지 못함" in lines[3]


def test_chart_d_groups_days_over_22_into_22_cells(tmp_path, monkeypatch, capsys):
    """44일이면 22칸에 이틀씩 묶고, 칸마다 묶음의 마지막 날 값을 그린다(첫날 값은 드러나지 않음)."""
    root = write_facts(tmp_path, ("trend_10_50",))
    recs = [json.loads(x) for x in _days(root, "trend_10_50").read_text(encoding="utf-8").splitlines()]
    rows = []
    for k, r in enumerate(recs):
        for j in (0, 1):
            x = copy.deepcopy(r)
            x["day"], x["live_day"] = (START + timedelta(days=2 * k + j)).isoformat(), 2 * k + j + 1
            if j == 0:
                x["nums"]["all"]["actual"] = 9.0  # 묶음의 첫날: 보이면 눈금이 무너짐
            rows.append(x)
    _days(root, "trend_10_50").write_text("".join(json.dumps(x, ensure_ascii=False) + "\n" for x in rows), encoding="utf-8")
    _, out, _ = _chart(tmp_path, monkeypatch, capsys)
    lines = out.splitlines()
    assert "9/23 ~ 11/5" in lines[2] and lines[3].startswith("  trend_10_50   " + SPARK["trend_10_50"] + "   -4.6%")


def test_chart_d_lists_strategy_without_records_under_the_table(tmp_path, monkeypatch, capsys):
    root = write_facts(tmp_path)
    _days(root, "grid_v1").unlink()
    _, out, _ = _chart(tmp_path, monkeypatch, capsys)
    lines = out.splitlines()
    assert lines[0].endswith("실전 3개 한눈에") and not any(x.startswith("  grid_v1") for x in lines)
    assert lines[-2:] == ["", "grid_v1 · 실전 중 · 매일 대조 기록 아직 없음(첫 blackclip daily 뒤에 보임)"]


def test_chart_c_unknown_actual_has_no_dot(tmp_path, monkeypatch, capsys):
    """실제를 모르는 전략(breakout_v2): 게이지에 ● 없이 나머지만, 꼬리는 모름으로."""
    write_facts(tmp_path)
    _, out, _ = _chart(tmp_path, monkeypatch, capsys, "breakout_v2")
    lines = out.splitlines()
    assert "●" not in lines[-2] and lines[-2].endswith("▏ 범위 모름 · 경고선까지 모름 · 확인 불가(체결 내역을 못 받음)")
    assert lines[-1].startswith("  ┃ 경고선 -7.2%  ● 실제 모름  ┣━┫ 정상 범위 ") and lines[-1].endswith("  ◆ 같은 기간 BT +0.1%")


GAUGE = "▕┃────●──┣━━◆━━━━━━━━━━━━━━━━━┫▏"
LONG = "trend_" + "0" * 58  # 전략 이름 모양에서 가장 긴 64글자


def _long_name(root, long=LONG):
    """trend_10_50의 폴더 · 기록 · 가드레일을 64글자 이름으로 옮겨 유효한 긴 이름 전략을 만든다."""
    src = root / "strategies" / "trend_10_50"
    dst = root / "strategies" / long
    src.rename(dst)
    for f in (dst / "daily" / "days.jsonl", dst / "guardrail.v1.yaml"):
        f.write_text(f.read_text(encoding="utf-8").replace("trend_10_50", long), encoding="utf-8")


def _plain(text):
    return re.sub(r"\x1b\[[0-9;]*m", "", text)


@pytest.mark.parametrize("no_color", ["1", None])
@pytest.mark.parametrize("name, w", [("trend_10_50", 120), ("trend_10_50", 64), ("trend_10_50", 58), ("trend_10_50", 55),
                                      ("trend_10_50", 46), ("trend_10_50", 45), ("trend_10_50", 34), (LONG, 120), (LONG, 99),
                                      (LONG, 80), (LONG, 45)])
def test_chart_c_never_exceeds_terminal_width(tmp_path, monkeypatch, capsys, name, w, no_color):
    """어떤 폭에서도 모든 줄이 폭 이하이고, 게이지 글자는 잘리지 않고 한 줄에 온전히 있다."""
    root = write_facts(tmp_path, ("trend_10_50",))
    if name == LONG:
        _long_name(root)
    monkeypatch.setenv("COLUMNS", str(w))
    code, out, err = _chart(tmp_path, monkeypatch, capsys, name, no_color=no_color)
    assert (code, err) == (0, "") and out.strip()
    lines = _plain(out).splitlines()
    assert all(cells(line) <= w for line in lines), [(cells(x), x) for x in lines if cells(x) > w]
    assert sum(GAUGE in line for line in lines) == 1  # 자르거나 두 줄로 쪼개지 않음


def test_chart_c_moves_name_and_tail_to_own_lines_when_narrow(tmp_path, monkeypatch, capsys):
    write_facts(tmp_path)
    monkeypatch.setenv("COLUMNS", "64")  # 이름 + 게이지는 들어가지만 꼬리는 남은 폭에 안 들어감
    _, out, _ = _chart(tmp_path, monkeypatch, capsys, "trend_10_50")
    lines = _c_block(out)
    assert lines[2] == "  trend_10_50 " + GAUGE and lines[3] == "  범위 아래 · 경고선까지 2.6%p · 경고 · 실행 문제로 추정"
    monkeypatch.setenv("COLUMNS", "45")  # 이름 + 게이지도 안 들어가서 이름이 따로 한 줄
    _, out, _ = _chart(tmp_path, monkeypatch, capsys, "trend_10_50")
    lines = _c_block(out)
    i = lines.index("  trend_10_50")  # 머리 줄도 접히므로 위치는 이름 줄로 찾는다
    assert lines[i + 1] == "  " + GAUGE


@pytest.mark.parametrize("w", [33, 30, 20])
def test_chart_c_folds_to_numbers_when_gauge_cannot_fit(tmp_path, monkeypatch, capsys, w):
    """32칸 게이지 + 여백 2칸(34칸)보다 좁으면 게이지 대신 숫자 · 설명(게이지 자르기 금지)."""
    write_facts(tmp_path)
    monkeypatch.setenv("COLUMNS", str(w))
    _, out, _ = _chart(tmp_path, monkeypatch, capsys, "trend_10_50")
    lines = _c_block(out)  # A · B는 그림 글자(┃ ● 등)를 쓰므로 C 블록만 본다
    assert all(cells(line) <= w for line in out.splitlines()) and not any(g in "".join(lines) for g in "▕▏┃●┣┫◆")
    text = " ".join(x.strip() for x in lines)
    for part in ("trend_10_50", "경고선 -7.2%", "실제 -4.6%", "-3.2% ~ +6.9%(22일째)", "같은 기간 BT -1.9%", "경고선까지 2.6%p"):
        assert part in text, part


@pytest.mark.parametrize("no_color", ["1", None])
@pytest.mark.parametrize("field", ["title", "rep", "pos", "failed_rep"])
def test_chart_escapes_repo_text_and_chart_glyphs(tmp_path, monkeypatch, capsys, field, no_color):
    """레포 글(rep · title · pos)의 ESC와 인쇄 가능한 그림 글자(█ ━ ◆)는 D · C 모두 평문 \\uXXXX로, 엔진이 그린 글자는 그대로."""
    root = write_facts(tmp_path, ("trend_10_50",))
    rec = record("trend_10_50", 22)
    bad = "레포\x1b[31m█━◆"
    if field == "failed_rep":  # 마지막 날 대조 실패 한 줄에도 같은 경계
        rec = _failed("trend_10_50", "2026-10-15", 23)
        rec["rep"] = f"확인 불가({bad})"
    elif field == "rep":
        rec["rep"] = f"경고({bad})"
    elif field == "title":
        rec["title"] = bad
    else:
        rec["nums"]["pos"] = bad
    _append(root, "trend_10_50", rec)
    esc = "레포\\u001b[31m\\u2588\\u2501\\u25c6"
    for args, shown in (((), field != "pos"), (("trend_10_50",), True)):  # D는 상태 칸 글만 보이고 pos는 C에만 있음
        _, out, _ = _chart(tmp_path, monkeypatch, capsys, *args, no_color=no_color)
        assert "레포\x1b" not in out and "█━◆" not in out, (args, out)  # 원문 그대로 나가지 않음
        assert (esc in out) is shown, (args, out)  # 지워지지 않고 평문으로 남음
        if field != "failed_rep":
            assert "▕" in out or not args  # C의 게이지와 D의 스파크라인 같은 엔진 글자는 그대로
            assert ("┃" in out and "●" in out) or not args
            assert "▆" in out or args


def test_chart_unknown_strategy_and_missing_workspace(tmp_path, monkeypatch, capsys):
    assert _chart(tmp_path, monkeypatch, capsys)[0] == 2  # .blackclip 없음
    write_facts(tmp_path, ("grid_v1",))
    assert _chart(tmp_path, monkeypatch, capsys, "nope") == (2, "", "blackclip chart: 전략이 없습니다(nope) · blackclip chart로 목록\n")
    code, _, err = _chart(tmp_path, monkeypatch, capsys, "../x")
    assert code == 2 and "전략 이름 모양이 다릅니다" in err


# --- A 범위 차트 · B 차이 면적 ---

A_HEAD = ["━━ A. 범위 차트  지금 괜찮나 · 언제부터 벗어났나", "",
          "trend_10_50 · 실전 시작부터 9/23 ~ 10/14(22일째) · 띠 · 선은 구간(30일) 기준 · ▣ 경고 · 실행 문제로 추정", ""]
A_LABELS = ["▇ 범위 상단 +6.9%(22일째) · 한 달 +8.1%", "╌ 같은 기간 BT -1.9% · 범위 안",
            "▁ 범위 하단 -3.2%(22일째) · 한 달 -3.7%", "━ 실제 -4.6% · 범위 아래",
            "차이(실제 − BT 비용 0) -2.7%p · 실행 문제로 추정", "┄ 경고선 -7.2% · 넘으면 중단 검토(2.6%p 남음)"]
A_LEGEND = "░ 정상 범위(한 달 90% · 그날까지)   ━ 실제   ╌ 같은 기간 BT   ┄ 경고선   ● 놓친 주문(추정)"
B_HEAD = ["━━ B. 차이 차트  실제 − 같은 기간 BT(비용 0) · 어디서 얼마나 샜나", ""]
B_LABELS = ["◂ 체결가 차이 · 수수료 -0.6%p(실전 내내 조금씩 · 잰 것)",
            "◂ 나머지 -2.1%p(추정 · 놓친 주문 1건 · 10/13 15:58 UTC 매도)", "합계 -2.7%p"]


def _blocks(out):
    """머리 줄은 좁은 터미널에서 접히므로(B 머리 65칸) 접두 「━━ A.」 · 「━━ B.」 · 「━━ C.」로 찾는다."""
    lines = out.splitlines()
    a0, b0, c0 = (next(i for i, line in enumerate(lines) if line.startswith(f"━━ {k}.")) for k in "ABC")
    return lines[a0:b0], lines[b0:c0], lines


def _col(line, ch):
    """줄에서 글자 ch가 있는 칸 번호(넓은 글자 고려)."""
    return cells(line[:line.index(ch)])


def test_chart_a_shape_without_color(tmp_path, monkeypatch, capsys):
    write_facts(tmp_path)
    code, out, _ = _chart(tmp_path, monkeypatch, capsys, "trend_10_50")
    assert code == 0 and out.strip()
    a, _, _ = _blocks(out)
    assert a[:4] == A_HEAD and A_LEGEND in a and "\x1b" not in out
    body = [line for line in a if "┤" in line or "│" in line]
    assert len(body) == 16 and all(cells(line) <= 120 for line in out.splitlines())  # 렌더된 줄 전체
    for label in A_LABELS:  # 라벨이 48칸까지라 120칸에서도 그림 밑 한 줄씩(줄 = 라벨 · 방향과 다르게 한 것 12)
        assert any(line.endswith(label) for line in a), label
    assert any(line.strip().startswith("0% ┤") for line in a)
    assert "░" in "".join(body)  # 색 없으면 띠 안 = 무늬
    warn = next(line for line in body if "┄" in line)
    assert warn.count("┄") >= 60  # 경고선은 그림 폭 전체
    miss = next(line for line in body if "●" in line)
    x_miss = _col(miss, "●") - 6  # 축 6칸 뒤 그림 칸
    assert round(x_miss * 22 / 66) == 20  # 10/13 = 21번째 날(0부터 20)
    axis = next(i for i, line in enumerate(a) if line.lstrip().startswith("└"))
    assert a[axis + 1].lstrip().startswith("9/23") and "10/14" in a[axis + 1]


def test_chart_a_actual_ends_below_band_and_bt_inside(tmp_path, monkeypatch, capsys):
    """마지막 칸: 실제(━ 또는 꺾임)는 띠 아래, BT(╌)는 띠 안 줄에 있음(같은 숫자를 그림으로)."""
    write_facts(tmp_path)
    _, out, _ = _chart(tmp_path, monkeypatch, capsys, "trend_10_50")
    a, _, _ = _blocks(out)
    body = [line[:72] for line in a if "┤" in line or "│" in line]
    last = [line[71] if len(line) > 71 else " " for line in body]
    band_rows = [i for i, ch in enumerate(last) if ch in "░▁▂▃▄▅▆▇"]
    act = max(i for i, ch in enumerate(last) if ch in "━┓┗┃●")
    bt = next(i for i, line in enumerate(body) if line.rstrip().endswith("╌") or (len(line) > 71 and line[71] == "╌"))
    assert act > max(band_rows) and min(band_rows) <= bt <= max(band_rows)


def test_chart_a_narrow_terminal_moves_labels_below(tmp_path, monkeypatch, capsys):
    """64칸이면 라벨은 그림 밑으로, 범례(90칸)는 두 줄로 — 모든 줄 64칸 이하, 낱말은 그대로."""
    from blackclip.screen.paint import wrap
    write_facts(tmp_path)
    monkeypatch.setenv("COLUMNS", "64")
    code, out, _ = _chart(tmp_path, monkeypatch, capsys, "trend_10_50")
    assert code == 0 and out.strip()  # 오류로 빈 출력이면 아래 all([])가 그냥 참이 됨
    a, _, _ = _blocks(out)
    assert all(cells(line) <= 64 for line in out.splitlines())
    legend = wrap(A_LEGEND, 64)
    k = a.index(legend[0])
    assert a[k:k + len(legend)] == legend
    tail = a[k - len(A_LABELS) - 1:k]
    assert all(any(line.strip() == lab for line in tail) for lab in A_LABELS)


def test_chart_every_line_fits(tmp_path, monkeypatch, capsys):
    """64 · 120칸에서 목록(D)과 전략 화면(A · B · C)의 모든 줄이 터미널 폭 이하."""
    write_facts(tmp_path)
    for cols in ("64", "120"):
        monkeypatch.setenv("COLUMNS", cols)
        for args in ((), ("trend_10_50",), ("breakout_v2",)):
            code, out, _ = _chart(tmp_path, monkeypatch, capsys, *args)
            assert code == 0 and out.strip(), (cols, args)
            assert all(cells(line) <= int(cols) for line in out.splitlines()), (cols, args)


def test_chart_without_any_strategy_fits_a_narrow_terminal(tmp_path, monkeypatch, capsys):
    """실전 전략이 하나도 없다는 안내도 터미널 폭(40칸) 이하, 종료 0, 낱말은 하나도 잃지 않는다."""
    from blackclip.screen.chart import NO_LIVE
    from blackclip.workspace import ensure
    (tmp_path / ".git").mkdir()
    ensure(tmp_path)
    monkeypatch.setenv("COLUMNS", "40")
    code, out, err = _chart(tmp_path, monkeypatch, capsys)
    lines = out.splitlines()
    assert (code, err) == (0, "") and len(lines) > 1 and all(cells(x) <= 40 for x in lines)
    assert [w for w in out.split() if w != "·"] == [w for w in NO_LIVE.split() if w != "·"]  # wrap은 줄머리에 놓인 「·」 하나만 접는다


def test_chart_a_second_period_marks_restart(tmp_path, monkeypatch, capsys):
    """35일째 — x축은 실전 시작부터, 31일째(10/23)에 구간 경계 ┊, 줄은 폭 안."""
    from datetime import date  # json · timedelta · record는 파일 위에서 import
    root = write_facts(tmp_path, ("trend_10_50",))
    base = record("trend_10_50", 22)
    extra = []
    for k in range(23, 36):
        r = json.loads(json.dumps(base))
        r.update(day=(date(2026, 9, 23) + timedelta(days=k - 1)).isoformat(), live_day=k)
        if k >= 31:
            r["nums"].update(period_start="2026-10-23", d=k - 30, actual=round(-0.001 * (k - 30), 6), bt=0.0)
        extra.append(r)
    with (root / "strategies" / "trend_10_50" / "daily" / "days.jsonl").open("a") as f:
        f.write("".join(json.dumps(r, ensure_ascii=False) + "\n" for r in extra))
    _, out, _ = _chart(tmp_path, monkeypatch, capsys, "trend_10_50")
    a = out.split("━━ B.")[0].splitlines()
    assert "실전 시작부터 9/23 ~ 10/27(35일째)" in a[2]
    assert sum("┊" in line for line in a) >= 10 and all(cells(line) <= 120 for line in out.splitlines())


def test_chart_b_two_areas(tmp_path, monkeypatch, capsys):
    """조금씩(체결가 차이 + 수수료, ░)은 처음부터, 한 번에(나머지, █)는 놓친 주문이 생긴 21 · 22일째 칸에만."""
    write_facts(tmp_path)
    _, out, _ = _chart(tmp_path, monkeypatch, capsys, "trend_10_50")
    _, b, _ = _blocks(out)
    assert b[:2] == B_HEAD
    for label in B_LABELS:
        assert any(line.rstrip().endswith(label) for line in b), label
    body = [line[:72] for line in b if "┤" in line or "│" in line or "┼" in line]
    assert len(body) == 8  # 0%p 줄 포함 8행(구현 규칙과 같은 수)
    fast = {i for line in body for i, ch in enumerate(line) if ch == "█"}
    slow = {i for line in body for i, ch in enumerate(line) if ch == "░"}
    assert fast and min(fast) - 6 >= 60 and slow and min(slow) - 6 <= 3


def test_chart_b_zero_positive_and_offsetting(tmp_path, monkeypatch, capsys):
    """차이가 모두 0이면 면적 없이 한 줄, 양수 기여는 0%p 위 · 음수는 아래(상쇄되면 둘 다), 합계는 gap."""
    import json
    root = write_facts(tmp_path, ("grid_v1",))
    p = root / "strategies" / "grid_v1" / "daily" / "days.jsonl"
    recs = [json.loads(x) for x in p.read_text().splitlines()]

    def b_block(vals):
        for r in recs:
            r["nums"].update(vals)
            r["nums"]["all"].update(vals)
        p.write_text("".join(json.dumps(r, ensure_ascii=False) + "\n" for r in recs))
        _, out, _ = _chart(tmp_path, monkeypatch, capsys, "grid_v1")
        lines = out.splitlines()
        return lines[lines.index(B_HEAD[0]):lines.index(C[0])]

    zero = b_block({"fee": 0.0, "slip": 0.0, "rest": 0.0, "gap": 0.0})
    assert "  차이 0.0%p · 그릴 면적 없음(잰 차이 · 나머지 모두 0)" in zero and not any("┼" in x for x in zero)
    mixed = b_block({"fee": -0.001, "slip": 0.0, "rest": 0.004, "gap": 0.003})
    body = [x for x in mixed if "┤" in x or "│" in x or "┼" in x]
    z = next(i for i, x in enumerate(body) if "┼" in x)
    assert any("█" in x for x in body[:z]) and any("░" in x for x in body[z + 1:])
    assert any(x.rstrip().endswith("합계 +0.3%p") for x in mixed)
    none = b_block({"fee": None, "slip": None, "rest": None, "gap": None})
    assert "  차이를 낸 날 없음(실행 칸을 확인하지 못함)" in none


def test_chart_a_without_band_and_without_missed(tmp_path, monkeypatch, capsys):
    """가드레일이 범위를 그리지 않았으면 띠 · 범위 라벨 없이, 놓친 주문이 없으면 ● 없이."""
    root = write_facts(tmp_path, ("grid_v1",))
    import json
    p = root / "strategies" / "grid_v1" / "daily" / "days.jsonl"
    recs = [json.loads(x) for x in p.read_text().splitlines()]
    for r in recs:
        r["nums"].update(lo=None, hi=None, pos=None)
    p.write_text("".join(json.dumps(r, ensure_ascii=False) + "\n" for r in recs))
    _, out, _ = _chart(tmp_path, monkeypatch, capsys, "grid_v1")
    a = out.split("━━ B.")[0]
    assert "░" not in a and "●" not in a and "범위 상단" not in a and "정상 범위 그리지 않음" in a


# --- 추가 시험: 레포 글 평문 · 폭 · 가드레일 · 빈 곳 ---


def _strategy_view(tmp_path, name="trend_10_50"):
    """직접 호출용: (기록, 가드레일) — 터미널 COLUMNS와 무관하게 width 인자만 따르는지 보려고."""
    from blackclip.daily.days import read_days
    from blackclip.workspace import read_guardrail
    root = write_facts(tmp_path, (name,))
    return read_days(root, name), read_guardrail(root, name)[1]


@pytest.mark.parametrize("no_color", ["1", None])
@pytest.mark.parametrize("name", ["trend_10_50", "breakout_v2", "grid_v1", LONG])
def test_chart_a_b_never_exceed_terminal_width(tmp_path, monkeypatch, capsys, name, no_color):
    """폭 1~200 어디서도 A · B(와 전체 화면)의 모든 줄이 폭 이하 — 그림을 자르지 않고 접거나 숫자로 바꿈."""
    root = write_facts(tmp_path, ("trend_10_50", "breakout_v2", "grid_v1"))
    if name == LONG:
        _long_name(root)
    for w in (1, 2, 5, 10, 20, 29, 30, 33, 34, 40, 55, 64, 73, 80, 99, 120, 122, 133, 134, 140, 200):
        monkeypatch.setenv("COLUMNS", str(w))
        code, out, err = _chart(tmp_path, monkeypatch, capsys, name, no_color=no_color)
        assert (code, err) == (0, ""), (w, err)
        bad = [(cells(x), x) for x in _plain(out).splitlines() if cells(x) > w]
        assert not bad, (w, bad)


def test_chart_a_b_follow_the_width_argument_not_the_terminal(tmp_path):
    """chart_a · chart_b를 직접 부르면 COLUMNS(120)가 아니라 넘긴 width를 따른다."""
    from blackclip.screen.chart import chart_a, chart_b
    recs, g = _strategy_view(tmp_path)
    for w in (40, 64, 100):
        for lines in (chart_a("trend_10_50", recs, g, False, w), chart_b("trend_10_50", recs, False, w)):
            assert lines and all(cells(x) <= w for x in lines), (w, [x for x in lines if cells(x) > w])


@pytest.mark.parametrize("no_color", ["1", None])
@pytest.mark.parametrize("w", [120, 160])
@pytest.mark.parametrize("field", ["title", "rep", "pos", "bt_pos", "side"])
def test_chart_a_b_escape_repo_text_and_chart_glyphs(tmp_path, monkeypatch, capsys, field, w, no_color):
    """A · B에 나가는 레포 글(rep · title · pos · bt_pos · 놓친 주문 side)의 ESC와 █ ━ ◆는 평문 \\uXXXX, 엔진 글자는 그대로."""
    root = write_facts(tmp_path, ("trend_10_50",))
    rec = record("trend_10_50", 22)
    rec.update(day="2026-10-15", live_day=23)
    bad = "레포\x1b[31m█━◆"
    if field == "rep":
        rec["rep"] = f"경고({bad})"
    elif field == "title":
        rec["title"] = bad
    elif field == "side":
        rec["nums"]["missed"] = [{"t": "2026-10-13T15:58:00+00:00", "symbol": "BTCUSDT", "side": bad}]
    else:
        rec["nums"][field] = bad
    _append(root, "trend_10_50", rec)
    monkeypatch.setenv("COLUMNS", str(w))
    _, out, _ = _chart(tmp_path, monkeypatch, capsys, "trend_10_50", no_color=no_color)
    esc = "레포\\u001b[31m\\u2588\\u2501\\u25c6"
    assert "레포\x1b" not in out and "█━◆" not in _plain(out), out  # 원문 그대로 나가지 않음
    assert esc in _plain(out), out  # 지워지지 않고 평문으로 남음
    plain = _plain(out)
    assert "┄" in plain and "━ 실제" in plain and "◂ 나머지" in plain  # 엔진이 그린 글자는 그대로


def test_chart_a_b_labels_sit_beside_the_figure_when_wide(tmp_path, monkeypatch, capsys):
    """A는 122칸, B는 134칸부터 라벨이 그림 줄 끝에 붙는다(겹치면 아래로 밀림 — 골든의 위에서 아래 순서)."""
    write_facts(tmp_path)
    monkeypatch.setenv("COLUMNS", "134")
    _, out, _ = _chart(tmp_path, monkeypatch, capsys, "trend_10_50")
    a, b, _ = _blocks(out)
    assert all(cells(x) <= 134 for x in out.splitlines())
    side = [(i, label) for label in A_LABELS for i, x in enumerate(a) if x.endswith(label)]
    assert [label for _, label in side] == A_LABELS  # 라벨마다 한 줄, 순서 그대로
    rows = [i for i, _ in side]
    first_body = next(i for i, x in enumerate(a) if "│" in x or "┤" in x)
    assert rows == sorted(set(rows)) and rows[0] == first_body  # 위에서 아래로, 첫 라벨은 맨 위 그림 줄
    assert "●" in a[rows[3]][:72] and "┄" in a[rows[5]][:72]  # 실제 라벨은 실제 선 줄, 경고선 라벨은 선 줄
    on_b = [(i, label) for label in B_LABELS for i, x in enumerate(b) if x.endswith(label)]
    assert [label for _, label in on_b] == B_LABELS and sorted(i for i, _ in on_b) == [i for i, _ in on_b]
    assert "└" in b[on_b[-1][0]]  # 합계 라벨은 8행 밑 축 줄에 놓임
    monkeypatch.setenv("COLUMNS", "133")  # B가 한 칸 모자라면 밑으로
    _, out, _ = _chart(tmp_path, monkeypatch, capsys, "trend_10_50")
    _, b, _ = _blocks(out)
    assert not any(x.endswith(B_LABELS[1]) and ("│" in x or "┤" in x) for x in b) and any(x == "  " + B_LABELS[1] for x in b)


def test_chart_a_b_color_matches_plain_text(tmp_path, monkeypatch, capsys):
    """색을 켜도 글자 칸은 같다: 배경 칸(░ █)은 색으로, 범례 띠 글자는 ░ 대신 ▇. 이스케이프는 띠 배경 · 굵게를 쓴다."""
    write_facts(tmp_path)
    _, plain, _ = _chart(tmp_path, monkeypatch, capsys, "trend_10_50")
    _, colored, _ = _chart(tmp_path, monkeypatch, capsys, "trend_10_50", no_color=None)
    norm = lambda s: re.sub(r"[░█]", " ", s).rstrip()
    want = [norm(x) for x in plain.splitlines()]
    got = [norm(_plain(x).replace("▇ 정상 범위", "░ 정상 범위")) for x in colored.splitlines()]
    assert got == want
    a = colored.split("━━ B.")[0]
    assert f"48;5;{PALETTE['blue_dim']}" in a and "\x1b[0;1m" in a and "░" not in a  # 띠 배경, 실제는 굵게
    assert f"48;5;{PALETTE['gray_dim']}" in colored and f"48;5;{PALETTE['orange_dim']}" in colored  # B의 조금씩 · 한 번에 배경


def test_chart_a_b_without_any_number_say_so(tmp_path, monkeypatch, capsys):
    """대조 실패 줄뿐이면 A · B · C 모두 그릴 것이 없다고 한 줄씩 말한다."""
    root = write_facts(tmp_path, ("trend_10_50",))
    _days(root, "trend_10_50").unlink()
    _append(root, "trend_10_50", _failed("trend_10_50", "2026-09-23", 1))
    code, out, _ = _chart(tmp_path, monkeypatch, capsys, "trend_10_50")
    lines = out.splitlines()
    assert code == 0 and "  그릴 숫자 없음(매일 대조를 확인하지 못함)" in lines
    assert "  차이를 낸 날 없음(실행 칸을 확인하지 못함)" in lines
    assert not any("│" in x or "┤" in x for x in lines) and lines[-1].startswith("trend_10_50 · 이번 구간 숫자 없음")


def test_chart_a_missing_days_stay_blank_and_are_not_bridged(tmp_path, monkeypatch, capsys):
    """기록이 없는 날(8~14일째)은 그 칸에 띠도 선도 경고선도 안 그리고, 앞뒤 날을 잇지 않는다(그림이 없으면 통과하지 않음)."""
    root = write_facts(tmp_path, ("trend_10_50",))
    p = _days(root, "trend_10_50")
    keep = [x for k, x in enumerate(p.read_text(encoding="utf-8").splitlines(keepends=True), 1) if not 8 <= k <= 14]
    p.write_text("".join(keep), encoding="utf-8")
    _, out, _ = _chart(tmp_path, monkeypatch, capsys, "trend_10_50")
    a, _, _ = _blocks(out)
    body = [x for x in a if "┤" in x or "│" in x]
    assert len(body) == 16 and sum(x.count("┄") for x in body) == 66 - 21  # 그림 행이 있고, 경고선은 기록 있는 날 칸에만
    assert "━" in "".join(x[6:6 + 21] for x in body) and "━" in "".join(x[6 + 42:] for x in body)  # 앞뒤 날은 그려짐
    for x in body:
        assert x[6 + 21:6 + 42].strip() == "", x  # 8일째(0부터 7번째)는 21번째 칸부터, 15일째는 42번째 칸 앞
    assert not any(ch in "".join(x[6 + 21:6 + 43] for x in body) for ch in "┓┗┛┏┃")  # 빈 날 둘레로 꺾임 글자가 새지 않음


def test_chart_a_b_group_days_beyond_the_plot_width(tmp_path, monkeypatch, capsys):
    """90일(3구간)은 그림 폭(58 · 66칸)보다 많아 칸마다 묶음의 마지막 날을 그린다(날짜마다 값이 다르게 +5% · -5%).

    구간 경계 ┊가 둘, 마지막 날짜가 보이고, 칸마다 그림 행(위 · 아래 절반)이 그 칸 묶음의 마지막 날 값과 같다.
    """
    root = write_facts(tmp_path, ("trend_10_50",))
    base = record("trend_10_50", 22)
    rows = []
    for k in range(1, 91):
        r = json.loads(json.dumps(base))
        r.update(day=(START + timedelta(days=k - 1)).isoformat(), live_day=k)
        r["nums"].update(period_start=(START + timedelta(days=(k - 1) // 30 * 30)).isoformat(), d=(k - 1) % 30 + 1,
                         actual=0.05 if k % 2 else -0.05, bt=None, missed=[])  # 이웃한 날은 반대쪽
        rows.append(r)
    _days(root, "trend_10_50").write_text("".join(json.dumps(r, ensure_ascii=False) + "\n" for r in rows), encoding="utf-8")
    last = START + timedelta(days=89)
    for w, p in (("64", 58), ("120", 66)):
        monkeypatch.setenv("COLUMNS", w)
        _, out, _ = _chart(tmp_path, monkeypatch, capsys, "trend_10_50")
        a, _, _ = _blocks(out)
        assert all(cells(x) <= int(w) for x in out.splitlines())
        body = [x for x in a if "┤" in x or "│" in x]
        cols = {i for x in body for i, ch in enumerate(x) if ch == "┊"}
        assert len(cols) == 2, (w, cols)
        assert f"{last.month}/{last.day}" in next(x for x in a if x.lstrip().startswith("9/23"))
        assert "실전 시작부터 실제" in "\n".join(a)  # 구간이 둘 이상이면 라벨 맨 위
        for x in range(p):
            day = [i for i in range(90) if i * p // 90 == x][-1]  # 그 칸이 맡은 날들 중 마지막 날
            row = next(y for y, line in enumerate(body) if len(line) > 6 + x and line[6 + x] in "━┗┏")
            assert (row < 8) == (day % 2 == 0), (w, x, day, row)  # 짝수 번째(0부터) 날 = k가 홀수 = +5% = 위 절반


def test_chart_view_picks_the_last_day_of_each_group():
    """묶음 칸 배치: 90일을 58칸에 배치하면 칸마다 그 묶음의 마지막 날 기록."""
    from blackclip.screen.chart import _view
    rows = []
    for k in range(90):
        rec = record("trend_10_50", 22)
        rec.update(day=(START + timedelta(days=k)).isoformat(), live_day=k + 1)
        rows.append(rec)
    _, n, columns, _ = _view(rows, 58)
    assert n == 90 and len(columns) == 58
    assert [columns[i]["live_day"] for i in (0, 28, 57)] == [2, 45, 90]


def test_chart_named_strategy_with_broken_guardrail_is_one_line(tmp_path, monkeypatch, capsys):
    """A가 가드레일을 읽으므로 깨진 가드레일은 status처럼 한 줄 오류 · 종료 2(전략별 격리는 전체 보기)."""
    root = write_facts(tmp_path, ("grid_v1",))
    p = root / "strategies" / "grid_v1" / "guardrail.v1.yaml"
    doc = json.loads(p.read_text(encoding="utf-8"))
    del doc["warning_line"]
    p.write_text(json.dumps(doc), encoding="utf-8")
    code, out, err = _chart(tmp_path, monkeypatch, capsys, "grid_v1")
    assert code == 2 and out == "" and err.startswith("blackclip chart: ") and "가드레일 모양이 다릅니다(warning_line" in err
    assert err.count("\n") == 1


def test_chart_a_without_band_says_why_when_guardrail_did_not_draw(tmp_path, monkeypatch, capsys):
    """가드레일이 범위를 그리지 않았으면(drawn false) 그 사유를 머리 아래 한 줄로."""
    root = write_facts(tmp_path, ("grid_v1",))
    p = root / "strategies" / "grid_v1" / "guardrail.v1.yaml"
    doc = json.loads(p.read_text(encoding="utf-8"))
    doc["normal_range"].update(drawn=False, reason="표본이 모자람")
    p.write_text(json.dumps(doc), encoding="utf-8")
    recs = [json.loads(x) for x in _days(root, "grid_v1").read_text(encoding="utf-8").splitlines()]
    for r in recs:
        r["nums"].update(lo=None, hi=None, pos=None)
    _days(root, "grid_v1").write_text("".join(json.dumps(r, ensure_ascii=False) + "\n" for r in recs), encoding="utf-8")
    _, out, _ = _chart(tmp_path, monkeypatch, capsys, "grid_v1")
    assert "정상 범위 그리지 않음(표본이 모자람)" in out.split("━━ B.")[0].splitlines()


# --- 재현 사례 ---


def _a_rows(lines):
    """A 그림 줄 16개(앞 6칸이 눈금인 줄)."""
    return [x for x in lines if "│" in x[:6] or "┤" in x[:6]]


@pytest.mark.parametrize("name", ["trend_10_50", "trend_ema_v7", "grid_v1", "breakout_v2"])
def test_chart_a_side_labels_sit_on_their_value_rows(name):
    """폭 160에서 라벨은 각 값(마지막 날)의 줄에 놓이고, 겹칠 때만 아래로 밀린다(골든 순서를 다른 전략에 고정하지 않음)."""
    from blackclip.screen.chart import chart_a
    recs = [record(name, k) for k in range(1, 23)]
    lines = chart_a(name, recs, guardrail_of(name), False, 160)
    body = _a_rows(lines)
    assert len(body) == 16
    last = recs[-1]["nums"]
    if last["actual"] is None:  # 실제를 모르는 전략은 줄 번호를 모르는 라벨이 있어 라벨이 그림 밑에 놓임
        assert not any("━ 실제" in x[74:] for x in body)
        return
    actual_row = next(i for i, x in enumerate(body) if len(x) > 71 and x[71] in "━┓┗┃●┛┏")
    label_rows = {lab: next(i for i, x in enumerate(body) if lab in x[74:]) for lab in ("▇ 범위 상단", "╌ 같은 기간 BT", "▁ 범위 하단", "━ 실제", "┄ 경고선")}
    assert 0 <= label_rows["━ 실제"] - actual_row <= 2, (name, actual_row, label_rows)
    assert label_rows["▇ 범위 상단"] <= label_rows["▁ 범위 하단"] and len(set(label_rows.values())) == 5  # 겹침은 풀림
    warn_row = next(i for i, x in enumerate(body) if len(x) > 71 and x[71] == "┄")
    assert 0 <= label_rows["┄ 경고선"] - warn_row <= 2


def test_chart_a_side_labels_follow_values_not_a_fixed_order():
    """BT가 범위 상단보다 위에 있으면 BT 라벨이 위에 온다(골든 순서 고정 없음). 아래 모드는 골든 순서 그대로."""
    from blackclip.screen.chart import chart_a
    recs = [record("trend_10_50", k) for k in range(1, 23)]
    recs[-1]["nums"]["bt"] = 0.075  # 상단 +6.9%보다 높음
    lines = chart_a("trend_10_50", recs, guardrail_of("trend_10_50"), False, 160)
    body = _a_rows(lines)
    rows = {lab: next(i for i, x in enumerate(body) if lab in x[74:]) for lab in ("▇ 범위 상단", "╌ 같은 기간 BT")}
    assert rows["╌ 같은 기간 BT"] < rows["▇ 범위 상단"]
    below = chart_a("trend_10_50", recs, guardrail_of("trend_10_50"), False, 100)
    heads = ("▇ 범위 상단", "╌ 같은 기간 BT", "▁ 범위 하단", "━ 실제", "차이(실제", "┄ 경고선")
    order = [next(i for i, x in enumerate(below) if x.lstrip().startswith(lab)) for lab in heads]
    assert order == sorted(order)  # 그림 밑에 놓일 때는 골든 순서


def test_chart_a_warning_line_uses_each_days_value():
    """경고선은 날마다 그날 line 행에 그리고, 세로 범위는 모든 날의 line을 담는다(앞 11일 -11%, 뒤 11일 -7.2%)."""
    from blackclip.daily.days import valid
    from blackclip.screen.chart import chart_a
    recs = [record("trend_10_50", k) for k in range(1, 23)]
    for rec in recs[:11]:
        rec["nums"]["line"] = -0.11
    assert all(valid(rec) for rec in recs)
    body = _a_rows(chart_a("trend_10_50", recs, guardrail_of("trend_10_50"), False, 120))
    left = [i for i, x in enumerate(body) if x[6:7] == "┄"]
    right = [i for i, x in enumerate(body) if len(x) > 71 and x[71] == "┄"]
    assert len(left) == 1 and len(right) == 1 and left[0] > right[0]  # 앞쪽 선이 더 아래
    assert left[0] == 15  # -11%는 세로 범위의 바닥(모든 날의 line이 범위에 듦)
    assert body[left[0]].count("┄") + body[right[0]].count("┄") == 66  # 날마다 한 칸씩 66칸 전부


def test_chart_b_last_failed_after_zero_says_until_when(tmp_path, monkeypatch, capsys):
    """앞 기록이 모두 0이고 마지막 날 대조가 실패면 「차이 0」에 마지막 확인일 `{M/D}까지`를 붙인다(C는 그대로)."""
    root = write_facts(tmp_path, ("grid_v1",))
    p = _days(root, "grid_v1")
    recs = [json.loads(x) for x in p.read_text(encoding="utf-8").splitlines()]
    for r in recs:
        r["nums"].update(fee=0.0, slip=0.0, funding=None, rest=0.0, gap=0.0)
        r["nums"]["all"].update(fee=0.0, slip=0.0, rest=0.0, gap=0.0)
    p.write_text("".join(json.dumps(r, ensure_ascii=False) + "\n" for r in recs), encoding="utf-8")
    _, out, _ = _chart(tmp_path, monkeypatch, capsys, "grid_v1")
    assert "  차이 0.0%p · 그릴 면적 없음(잰 차이 · 나머지 모두 0)" in out.splitlines()  # 마지막 날을 알면 지금처럼
    _append(root, "grid_v1", _failed("grid_v1", "2026-10-15", 23))
    _, out, _ = _chart(tmp_path, monkeypatch, capsys, "grid_v1")
    lines = out.splitlines()
    assert "  차이 0.0%p · 그릴 면적 없음(잰 차이 · 나머지 모두 0)" not in lines
    assert "  차이(10/14까지) 0.0%p · 그릴 면적 없음(잰 차이 · 나머지 모두 0)" in lines
    assert not any("차이 0.0%p" in x for x in lines)  # 마지막 날을 모르는데 0을 단정하는 줄이 없음
    assert lines[-1].startswith("grid_v1 · 이번 구간 숫자 없음")  # C는 그대로


def test_chart_b_slow_is_unknown_when_fee_or_slip_is_unknown():
    """수수료 · 체결가 차이 중 하나라도 모르면 그날 조금씩은 모름(칸 빔 · 라벨 모름). 펀딩비만 없음 = 0."""
    from blackclip.daily.days import valid
    from blackclip.screen.chart import _split, chart_b
    rec = record("trend_10_50", 22)
    rec["nums"].update(fee=None, slip=-0.004, funding=None)
    assert valid(rec) and _split(rec["nums"]) == (None, -0.021)
    lines = chart_b("trend_10_50", [rec], False, 120)
    label = next(x for x in lines if "체결가 차이 · 수수료" in x)
    assert label == "  ◂ 체결가 차이 · 수수료 모름(실전 내내 조금씩 · 잰 것)"
    assert not any("░" in x for x in lines if "│" in x or "┤" in x or "┼" in x)  # 조금씩 면적 없음, 나머지(█)만
    assert any("█" in x for x in lines)
    both = record("trend_10_50", 22)
    both["nums"].update(fee=-0.002, slip=-0.004, funding=None)
    assert _split(both["nums"])[0] == pytest.approx(-0.006)  # 펀딩비 None은 0


def test_chart_accepted_missed_time_that_cannot_convert_to_utc_is_one_line(tmp_path, monkeypatch, capsys):
    """모양 검사를 통과하지만 UTC로 바꿀 수 없는 놓친 주문 시각은 traceback 없이 한 줄 · 종료 2."""
    root = write_facts(tmp_path, ("trend_10_50",))
    rec = record("trend_10_50", 22)
    rec["nums"]["missed"][0]["t"] = "0001-01-01T00:00:00+01:00"
    from blackclip.daily.days import valid
    assert valid(rec)
    _days(root, "trend_10_50").write_text(json.dumps(rec, ensure_ascii=False) + "\n", encoding="utf-8")
    code, out, err = _chart(tmp_path, monkeypatch, capsys, "trend_10_50")
    assert code == 2 and out == "" and err.startswith("blackclip chart: ") and err.count("\n") == 1


# --- 일부만 모르는 날을 「차이 0」으로 단정하지 않음 ---


def _zero_recs(last=None, early=None, late=None):
    """22일 기록: 모든 날 기여 · gap이 0, `early`는 앞 11일 · `late`는 뒤 11일 · `last`는 마지막 날에 덮을 nums(검사를 지나야 함)."""
    from blackclip.daily.days import valid
    recs = [record("trend_10_50", k) for k in range(1, 23)]
    for i, rec in enumerate(recs):
        rec["nums"].update(fee=0.0, slip=0.0, funding=None, rest=0.0, gap=0.0, missed=[], missed_n=0)
        rec["nums"].update((early or {}) if i < 11 else (late or {}))
    recs[-1]["nums"].update(last or {})
    assert all(valid(rec) for rec in recs)
    return recs


@pytest.mark.parametrize("width", [64, 120])
@pytest.mark.parametrize("last, unknown", [
    ({"fee": None, "slip": -0.004, "rest": 0.0, "gap": None}, "체결가 차이 · 수수료 모름"),
    ({"fee": -0.002, "slip": None, "rest": 0.0, "gap": None}, "체결가 차이 · 수수료 모름"),
    ({"fee": 0.0, "slip": 0.0, "rest": None, "gap": None}, "나머지 모름"),  # fast만 모름
    ({"fee": None, "slip": None, "rest": None, "gap": None}, "나머지 모름"),  # 둘 다 모름
])
def test_chart_b_zero_shortcut_does_not_claim_zero_when_last_day_is_partly_unknown(last, unknown, width):
    """마지막 날에 모르는 기여가 있으면 「차이 0」에 마지막 확인일을 붙이고 그 아래 라벨에 「모름」을 보인다."""
    from blackclip.screen.chart import chart_b
    lines = chart_b("trend_10_50", _zero_recs(last), False, width)
    assert all(cells(line) <= width for line in lines)
    assert not any("차이 0.0%p" in line for line in lines)  # 날짜 없이 0을 단정하는 줄 없음
    k = next(i for i, line in enumerate(lines) if "차이(10/13까지) 0.0%p" in line)
    assert any(unknown in line for line in lines[k + 1:])  # 줄 아래 라벨이 모르는 쪽을 「모름」이라 함
    assert any(line.strip().startswith("합계") for line in lines[k + 1:]) and not any("┼" in line for line in lines)


@pytest.mark.parametrize("width", [64, 120])
def test_chart_b_zero_shortcut_says_unknown_when_no_day_has_both_contributions(width):
    """두 기여를 모두 확인한 날이 하나도 없으면(그러나 전부 (모름, 모름)은 아님) 「차이 모름」 한 줄(라벨은 그 아래)."""
    from blackclip.screen.chart import chart_b
    recs = _zero_recs(early={"fee": None, "slip": None, "gap": None}, late={"rest": None, "gap": None})  # 앞 11일 slow 모름 · 뒤 11일 fast 모름
    lines = chart_b("trend_10_50", recs, False, width)
    assert all(cells(line) <= width for line in lines)
    k = lines.index("  차이 모름 · 그릴 면적 없음(두 기여를 모두 확인한 날 없음)")
    assert any("나머지 모름" in line for line in lines[k + 1:])
    assert not any("차이 0.0%p" in line or "까지) 0.0%p" in line for line in lines)


def test_chart_b_zero_shortcut_keeps_plain_sentence_when_last_day_is_fully_known():
    """앞 날에 모르는 기여가 있었어도 마지막 날 두 기여를 다 알면 지금 문구 그대로, 라벨 없음."""
    from blackclip.screen.chart import chart_b
    lines = chart_b("trend_10_50", _zero_recs(early={"fee": None, "slip": None}), False, 120)
    assert lines[-1] == "  차이 0.0%p · 그릴 면적 없음(잰 차이 · 나머지 모두 0)" and not any("모름" in line for line in lines)


# --- 전체 보기는 days.jsonl을 못 읽은 전략만 확인 불가(종료 4), 이름을 준 보기는 한 줄 · 종료 2 ---


def _break_days_link(d):
    d.unlink()
    d.symlink_to(d.parent / "elsewhere")


def _break_days_fifo(d):
    d.unlink()
    os.mkfifo(d)


BAD_DAYS = {
    "link": (_break_days_link, "심볼릭 링크"),
    "fifo": (_break_days_fifo, "일반 파일이 아니라"),
    "all_not_json": (lambda d: d.write_text("깨진 줄\n{ not json\n"), "읽을 수 있는 줄이 없습니다"),
    "all_wrong_shape": (lambda d: d.write_text('{"v": 1, "day": [1]}\n["x"]\n'), "읽을 수 있는 줄이 없습니다"),
}


def _unreadable_row(lines):
    """「확인 불가」 줄이 시작하는 자리부터 끝까지를 한 글로(wrap이 나눈 줄의 들여쓰기를 걷어 이음). D 표 줄은 두 칸 들여써서 구분됨."""
    i = next(i for i, x in enumerate(lines) if x.startswith("trend_10_50"))
    return i, " ".join(x.strip() for x in lines[i:])


@pytest.mark.parametrize("cols", ["120", "40"])
@pytest.mark.parametrize("how", sorted(BAD_DAYS))
def test_chart_all_isolates_the_strategy_whose_days_cannot_be_read(tmp_path, monkeypatch, capsys, how, cols):
    """days.jsonl 링크 · FIFO · 읽을 줄이 하나도 없는 파일(JSON이 안 풀림 · 모양이 다름): 종료 4 · 다른 전략 D 줄은 그대로 · 끝에 「확인 불가」 줄 · 모든 줄이 폭 이하."""
    break_it, msg = BAD_DAYS[how]
    root = write_facts(tmp_path, ("trend_10_50", "grid_v1"))
    break_it(_days(root, "trend_10_50"))
    monkeypatch.setenv("COLUMNS", cols)
    t0 = time.monotonic()
    code, out, err = _chart(tmp_path, monkeypatch, capsys)
    assert time.monotonic() - t0 < 5
    lines = out.splitlines()
    assert code == 4 and err == "" and "\x1b" not in out and all(cells(x) <= int(cols) for x in lines)
    assert lines[0].endswith("실전 1개 한눈에") and any(x.startswith("  grid_v1") for x in lines) and not any(x.startswith("  trend_10_50") for x in lines)
    i, row = _unreadable_row(lines)
    assert lines[i - 1] == "" and "확인 불가(" in row and msg in row  # D 목록과 한 줄 띄우고, 사유(WorkspaceError 메시지)가 이어짐
    assert _chart(tmp_path, monkeypatch, capsys, "trend_10_50")[0] == 2  # 이름을 준 보기는 그대로 한 줄 · 종료 2


def test_chart_all_every_strategy_unreadable_prints_only_the_rows(tmp_path, monkeypatch, capsys):
    """읽을 수 있는 전략이 없으면 D 목록 없이 확인 불가 줄들만(이름순), 종료 4."""
    root = write_facts(tmp_path, ("trend_10_50", "grid_v1"))
    for n in ("trend_10_50", "grid_v1"):
        _days(root, n).write_text("깨진 줄\n")
    code, out, _ = _chart(tmp_path, monkeypatch, capsys)
    lines = out.splitlines()
    assert code == 4 and "━━ D." not in out and lines[0].startswith("grid_v1") and not lines[0].startswith("  ")
    assert [x.split()[0] for x in lines if not x.startswith(" ")] == ["grid_v1", "trend_10_50"]


def test_chart_all_unreadable_row_follows_the_waiting_rows(tmp_path, monkeypatch, capsys):
    """D 목록 · 기다리는 전략 줄 뒤에 확인 불가 줄: 한 전략은 기록 없음(기다림), 한 전략은 읽지 못함."""
    root = write_facts(tmp_path, ("trend_10_50", "grid_v1", "breakout_v2"))
    _days(root, "grid_v1").unlink()
    _days(root, "trend_10_50").write_text("깨진 줄\n")
    code, out, _ = _chart(tmp_path, monkeypatch, capsys)
    lines = out.splitlines()
    wait = lines.index("grid_v1 · 실전 중 · 매일 대조 기록 아직 없음(첫 blackclip daily 뒤에 보임)")
    assert code == 4 and lines[wait - 1] == "" and lines[wait + 1].startswith("trend_10_50") and wait + 1 == _unreadable_row(lines)[0]


@pytest.mark.parametrize("how", ["all_not_json", "all_wrong_shape"])
def test_chart_named_strategy_with_no_readable_line_is_one_line(tmp_path, monkeypatch, capsys, how):
    """이름을 준 보기에서 읽을 줄이 하나도 없는 days.jsonl은 「기록 아직 없음」이 아니라 한 줄 오류 · 종료 2."""
    root = write_facts(tmp_path, ("grid_v1",))
    BAD_DAYS[how][0](_days(root, "grid_v1"))
    code, out, err = _chart(tmp_path, monkeypatch, capsys, "grid_v1")
    assert code == 2 and out == "" and err.startswith("blackclip chart: ") and "읽을 수 있는 줄이 없습니다" in err and err.count("\n") == 1


def test_chart_all_reason_rule_oserror_type_only_and_plain_glyphs(tmp_path, monkeypatch, capsys):
    """사유: OSError는 종류 이름만, WorkspaceError는 메시지 그대로이되 그림 글자 · ESC는 평문(레포 글 경계)."""
    import blackclip.screen.chart as chart_mod
    from blackclip.screen.status import FORBIDDEN
    from blackclip.workspace import WorkspaceError
    write_facts(tmp_path, ("trend_10_50", "grid_v1"))
    real = chart_mod.read_days

    def flaky(root_, name):
        if name == "trend_10_50":
            raise OSError("EIO /Users/secret-customer/repo/.blackclip")
        return real(root_, name)

    monkeypatch.setattr(chart_mod, "read_days", flaky)
    code, out, _ = _chart(tmp_path, monkeypatch, capsys)
    assert code == 4 and out.splitlines()[-1] == "trend_10_50 · 확인 불가(OSError)" and "secret-customer" not in out

    def glyphs(root_, name):
        if name == "trend_10_50":
            raise WorkspaceError("█━ \x1b[31m")
        return real(root_, name)

    monkeypatch.setattr(chart_mod, "read_days", glyphs)
    code, out, _ = _chart(tmp_path, monkeypatch, capsys)
    assert code == 4 and out.splitlines()[-1] == "trend_10_50 · 확인 불가(\\u2588\\u2501 \\u001b[31m)"
    assert not (set(out.splitlines()[-1]) & FORBIDDEN)


E_HEAD = ["━━ E. 거래 단위  전략이 망가졌나 · 날짜가 아니라 거래 수로", "",
          "trend_10_50 · 실전 내내 청산 37건 · 수익 표본 37건 · 거래 순서 →", ""]
E_TAIL = ["판단에 필요한 거래 수 · 37건 · 30건 선을 넘음",
          "거래 1건 평균 · 실제 -0.62% · 백테스트 +0.27%",
          "엣지가 없어도 정상 범위 안일 확률 79% → 판단 보류",
          "거래 빈도 · 최근 7일 11건 · 백테스트 기준 7~17건",
          "점 하나가 실제 거래 1건(사고 판 한 번 · 백테스트와 같은 정의 · 실제 수수료, 백테스트 띠는 보수적 비용)",
          "거래가 적을 때는 평균이 크게 흔들려 날짜가 아니라 거래 수로 본다"]
E_LEGEND = "● 실제 거래 1건   ━ 실제 평균(쌓일수록 안정)   ░ 백테스트 거래 가운데 80%   ╌ 백테스트 평균   ┆ 30건 선"


def test_chart_e_trades(tmp_path, monkeypatch, capsys):
    write_facts(tmp_path)
    code, out, _ = _chart(tmp_path, monkeypatch, capsys, "trend_10_50", "--trades")
    lines = out.splitlines()
    assert code == 0 and lines[:4] == E_HEAD and lines[-6:] == E_TAIL and E_LEGEND in lines
    body = [line for line in lines if "┤" in line or "│" in line]
    assert sum(line.count("●") for line in body) == 37 and any("┆" in line for line in body) and "░" in "".join(body)
    assert all(cells(line) <= 120 for line in lines) and "\x1b" not in out
    monkeypatch.setenv("COLUMNS", "60")
    code, narrow, _ = _chart(tmp_path, monkeypatch, capsys, "trend_10_50", "--trades")
    assert code == 0 and narrow.strip()  # 빈 출력이면 아래 all([])가 그냥 참
    assert all(cells(line) <= 60 for line in narrow.splitlines())


def test_chart_e_sample_spans_periods_and_caps(tmp_path, monkeypatch, capsys):
    """표본 = 실전 시작부터(구간마다 마지막 줄을 이어 붙임). 구간 기록 상한(2,000) · 표시 상한(E_CAP)으로 빠진 수를 머리에."""
    import json
    import blackclip.screen.chart as chart_mod
    from facts import record
    root = write_facts(tmp_path, ("trend_10_50",))
    r = record("trend_10_50", 22)
    r.update(day="2026-10-24", live_day=32)
    r["trades"] = 2538  # Day.trades는 실전 시작부터 누적: 앞 구간 37 + 이 구간 2,501(기록에는 지어낸 2건만 남았다고 봄)
    r["nums"].update(period_start="2026-10-23", trade_rets=[0.01, 0.02], trade_mean=0.015, trade_n=2501, trade_skip=0)
    with (root / "strategies" / "trend_10_50" / "daily" / "days.jsonl").open("a") as f:
        f.write(json.dumps(r, ensure_ascii=False) + "\n")
    code, out, _ = _chart(tmp_path, monkeypatch, capsys, "trend_10_50", "--trades")
    lines = out.splitlines()
    assert code == 0 and "trend_10_50 · 실전 내내 청산 2,538건 · 수익 표본 2,538건 · 거래 순서 →" in lines
    assert "빠진 거래 2,499건 · 기록 상한(구간마다 2,000)에 걸린 구간 10/23~ · 점 · 평균은 남은 39건 기준" in lines
    assert sum(line.count("●") for line in lines if "┤" in line or "│" in line) == 39
    assert not any("┆" in line for line in lines) and "┆ 30건 선" not in out  # 빠진 거래가 있으면 30건 선을 안 그림
    monkeypatch.setattr(chart_mod, "E_CAP", 30)
    _, out, _ = _chart(tmp_path, monkeypatch, capsys, "trend_10_50", "--trades")
    assert ("빠진 거래 2,508건 · 기록 상한(구간마다 2,000)에 걸린 구간 10/23~ · 표시 상한 30건 · 점 · 평균은 남은 30건 기준"
            in out.splitlines())


def test_failed_day_is_safe_everywhere(tmp_path, monkeypatch, capsys):
    """대조 실패 줄(nums={})이 마지막이거나 유일한 줄이어도 status · chart 목록 · A~C · E · record 중지가
    종료 0 · 빈 출력 아님 · 「숫자 없음」 안내 줄(빈 min() · KeyError 없음)."""
    import json
    from facts import record
    from blackclip.states import TRACKED
    root = write_facts(tmp_path, ("trend_10_50", "grid_v1"))
    why = "확인 불가(대조하지 못함 · KeyError)"
    r = record("trend_10_50", 22)
    r.update(day="2026-10-15", live_day=23, trades=0, head="실제 모름 · 같은 기간 BT 모름", rep=why, nums={},
             states={k: why for k in TRACKED})
    with (root / "strategies" / "trend_10_50" / "daily" / "days.jsonl").open("a") as f:
        f.write(json.dumps(r, ensure_ascii=False) + "\n")
    (root / "strategies" / "grid_v1" / "daily" / "days.jsonl").write_text(
        json.dumps({**r, "strategy": "grid_v1"}, ensure_ascii=False) + "\n")
    monkeypatch.chdir(tmp_path)
    for args in (["status"], ["status", "trend_10_50"], ["chart"], ["chart", "trend_10_50"], ["chart", "grid_v1"],
                 ["chart", "trend_10_50", "--trades"], ["chart", "grid_v1", "--trades"]):
        assert main(args) == 0, args
        assert capsys.readouterr().out.strip(), args
    main(["chart", "grid_v1"])
    out = capsys.readouterr().out
    assert "그릴 숫자 없음(매일 대조를 확인하지 못함)" in out and f"grid_v1 · 이번 구간 숫자 없음(마지막 매일 대조: {why})" in out
    assert main(["record", "grid_v1", "--state", "중지"]) == 0
    page = (root / "strategies" / "grid_v1" / "after.md").read_text()
    assert "- 근거 없음(매일 대조에 숫자 없음 — 숫자를 쓰지 않음)" in page


def test_chart_e_old_guardrail_and_no_trades(tmp_path, monkeypatch, capsys):
    """분위 없는 예전 가드레일 = 띠 없이 평균 선만, 거래 0건 = 한 줄, --trades만 = 종료 2."""
    import json
    root = write_facts(tmp_path, ("trend_10_50", "grid_v1"))
    gp = root / "strategies" / "trend_10_50" / "guardrail.v1.yaml"
    g = json.loads(gp.read_text())
    del g["basis"]["trade_p10"], g["basis"]["trade_p90"]
    gp.write_text(json.dumps(g, ensure_ascii=False))
    _, out, _ = _chart(tmp_path, monkeypatch, capsys, "trend_10_50", "--trades")
    assert "░" not in out and "백테스트 거래 가운데 80%는 다음 가드레일 저장부터(blackclip save --reason)" in out
    assert _chart(tmp_path, monkeypatch, capsys, "grid_v1", "--trades")[1] == \
        "grid_v1 · 거래 1건 수익 기록 없음 · 매일 대조가 다음에 셈\n"
    assert _chart(tmp_path, monkeypatch, capsys, "--trades")[0] == 2


def test_trade_sample_joins_periods_and_ignores_failed_days():
    """구간마다 마지막 줄만(앞 줄 무시) 시작 순서로 이어 붙이고, 옛 줄(trade_n 없음)은 기록된 수로 세며,
    대조 실패 줄(period_start 없음)은 어느 구간에도 안 든다. 자르면 가장 최근 것이 남는다."""
    from blackclip.screen.data import trade_sample

    def line(day, start, trades, **nums):
        return {"day": day, "trades": trades, "nums": {"period_start": start, **nums} if start else {}}
    recs = [line("2026-09-30", "2026-09-23", 1, trade_rets=[9.0], trade_n=1),
            line("2026-10-14", "2026-09-23", 3, trade_rets=[0.1, 0.2], trade_n=2, trade_skip=1),
            line("2026-10-30", "2026-10-23", 7, trade_rets=[0.3, 0.4], trade_n=9, trade_skip=0),  # 기록 상한에 걸림
            line("2026-11-01", None, 0)]
    assert trade_sample(recs, 99) == {"rets": [0.1, 0.2, 0.3, 0.4], "kept": 4, "exits": 7, "n": 11, "skip": 1, "capped": ["2026-10-23"]}
    assert trade_sample(recs, 3)["rets"] == [0.2, 0.3, 0.4] and trade_sample(recs, 3)["kept"] == 4
    old = [line("2026-10-14", "2026-09-23", 2, trade_rets=[0.1, 0.2])]  # 옛 줄: trade_n · trade_skip 없음
    assert trade_sample(old, 9) == {"rets": [0.1, 0.2], "kept": 2, "exits": 2, "n": 2, "skip": 0, "capped": []}
    assert trade_sample([line("2026-10-14", None, 0)], 9) == {"rets": [], "kept": 0, "exits": 0, "n": 0, "skip": 0, "capped": []}


def test_chart_e_color_and_every_width_fit(tmp_path):
    """E: 색을 켜면 이스케이프가 들어가고, 모든 폭에서 줄이 폭 안 · 거래가 하나뿐이거나 30건 미만이어도 그려진다."""
    from blackclip.daily.days import read_days
    from blackclip.screen.chart import chart_e
    root = write_facts(tmp_path, ("trend_10_50",))
    recs, g = read_days(root, "trend_10_50"), guardrail_of("trend_10_50")
    esc = re.compile(r"\x1b\[[0-9;]*m")
    assert any("\x1b" in x for x in chart_e("trend_10_50", recs, g, True, 100))
    for w in (1, 5, 29, 30, 40, 72, 73, 100, 130):
        assert all(cells(esc.sub("", x)) <= w for x in chart_e("trend_10_50", recs, g, True, w)), w
    for rets, dots, line in (([0.01], 1, False), ([0.01, -0.02, 0.03], 3, False)):
        short = copy.deepcopy(recs)
        short[-1]["nums"].update(trade_rets=rets, trade_n=len(rets), trade_mean=sum(rets) / len(rets))
        short[-1]["trades"] = len(rets)
        out = chart_e("trend_10_50", short, g, False, 100)
        assert sum(x.count("●") for x in out if "┤" in x or "│" in x) == dots
        assert any("┆" in x for x in out) is line and "30건 전 · 판단 보류" in "\n".join(out)


def test_chart_e_says_closes_missing_from_the_daily_records(tmp_path, monkeypatch, capsys):
    """청산 수(Day.trades) − 표본 − 뺀 수가 양수면(구간 마지막 날 daily가 안 돌았거나 체결을 못 읽음) 머리 아래에 그 수를 말한다."""
    import json
    from facts import record
    root = write_facts(tmp_path, ("trend_10_50",))
    ok = _chart(tmp_path, monkeypatch, capsys, "trend_10_50", "--trades")[1]
    assert "매일 대조 기록에 없는" not in ok
    r = record("trend_10_50", 22)
    r.update(day="2026-10-24", live_day=32, trades=80)
    r["nums"].update(period_start="2026-10-23", trade_rets=[0.01] * 6, trade_mean=0.01, trade_n=6, trade_skip=0)
    with (root / "strategies" / "trend_10_50" / "daily" / "days.jsonl").open("a") as f:
        f.write(json.dumps(r, ensure_ascii=False) + "\n")
    lines = _chart(tmp_path, monkeypatch, capsys, "trend_10_50", "--trades")[1].splitlines()
    assert "trend_10_50 · 실전 내내 청산 80건 · 수익 표본 43건 · 거래 순서 →" in lines
    assert lines[lines.index("매일 대조 기록에 없는 청산 37건") - 1].startswith("trend_10_50 ·")
    assert not any("┆" in line for line in lines) and "┆ 30건 선" not in "\n".join(lines)  # 기록에 없는 청산이 있으면 30번째 점이 30번째 거래가 아니라 30건 선을 안 그림


def test_chart_e_non_positive_min_trades_is_a_guardrail_error_not_a_traceback(tmp_path, monkeypatch, capsys):
    """min_trades가 0 · 음수인 가드레일은 읽을 때 거절(종료 2 · 한 줄), E가 IndexError로 죽지 않는다."""
    import json
    root = write_facts(tmp_path, ("trend_10_50",))
    gp = root / "strategies" / "trend_10_50" / "guardrail.v1.yaml"
    for bad in (-1, 0):
        g = json.loads(gp.read_text())
        g["judgment_hold"]["min_trades"] = bad
        gp.write_text(json.dumps(g, ensure_ascii=False))
        code, out, err = _chart(tmp_path, monkeypatch, capsys, "trend_10_50", "--trades")
        assert code == 2 and out == "" and "judgment_hold.min_trades" in err and "Traceback" not in err


def test_e_keeps_sample_when_last_line_has_none():
    """같은 구간 마지막 줄의 trade_rets가 None이어도(체결을 못 읽은 날) 앞선 줄의 표본을 쓴다. 청산 수 exits는 마지막 줄 그대로."""
    from blackclip.screen.data import trade_sample

    def line(day, trades, **nums):
        return {"day": day, "trades": trades, "nums": {"period_start": "2026-09-23", **nums}}
    recs = [line("2026-10-13", 37, trade_rets=[0.1] * 37, trade_n=37, trade_skip=0),
            line("2026-10-14", 38, trade_rets=None, trade_n=None, trade_skip=None)]
    got = trade_sample(recs, 99)
    assert got["rets"] == [0.1] * 37 and got["n"] == 37 and got["skip"] == 0 and got["exits"] == 38


def test_chart_e_draws_37_when_last_line_has_none(tmp_path, monkeypatch, capsys):
    """화면에서도 37건을 그린다(「거래 1건 수익 기록 없음」이 아님)."""
    import json
    from facts import record
    root = write_facts(tmp_path, ("trend_10_50",))
    p = root / "strategies" / "trend_10_50" / "daily" / "days.jsonl"
    rows = [json.loads(x) for x in p.read_text().splitlines()]
    rows[-1]["nums"].update(trade_rets=[0.01 * (i % 5 - 2) for i in range(37)], trade_n=37, trade_skip=0, trade_mean=0.0)
    nxt = record("trend_10_50", 22)
    nxt.update(day="2026-10-15", live_day=23)
    nxt["nums"].update(trade_rets=None, trade_mean=None, trade_n=None, trade_skip=None)
    p.write_text("".join(json.dumps(r, ensure_ascii=False) + "\n" for r in [*rows, nxt]))
    out = _chart(tmp_path, monkeypatch, capsys, "trend_10_50", "--trades")[1]
    assert "수익 기록 없음" not in out and "수익 표본 37건" in out


def test_chart_off_line_counts_stopped_without_guardrail_board(tmp_path, monkeypatch, capsys):
    """chart 목록도 가드레일 판 파일이 없는 끈 전략을 「끈 전략 N개」에 센다(status 전체 보기와 같은 기준)."""
    root = write_facts(tmp_path, ("grid_v1", "trend_10_50"))
    _stop(tmp_path, monkeypatch, capsys, "grid_v1")
    (root / "strategies" / "grid_v1" / "guardrail.v1.yaml").unlink()
    (root / "strategies" / "notes").write_text("", encoding="utf-8")
    code, out, _ = _chart(tmp_path, monkeypatch, capsys)
    assert code == 0 and out.splitlines()[-1] == OFF.format(n=1)


def test_trade_sample_takes_n_and_skip_from_the_sample_line():
    """trade_n · trade_skip도 표본 줄(trade_rets가 있는 마지막 줄) 값이다. 뒤의 None 줄 값이 아님."""
    from blackclip.screen.data import trade_sample

    def line(day, **nums):
        return {"day": day, "trades": 60, "nums": {"period_start": "2026-09-23", **nums}}
    recs = [line("2026-10-13", trade_rets=[0.1] * 30, trade_n=50, trade_skip=2),
            line("2026-10-14", trade_rets=None, trade_n=None, trade_skip=None)]
    got = trade_sample(recs, 99)
    assert (got["n"], got["skip"], got["kept"], got["capped"]) == (50, 2, 30, ["2026-09-23"])


def test_chart_named_stopped_without_board_says_why(tmp_path, monkeypatch, capsys):
    """판 없는 끈 전략을 chart 이름으로 주면 종료 2 그대로, 글만 끈 전략이라 판이 없다고 말하고 status로 안내한다."""
    root = write_facts(tmp_path, ("grid_v1", "trend_10_50"))
    _stop(tmp_path, monkeypatch, capsys, "grid_v1")
    (root / "strategies" / "grid_v1" / "guardrail.v1.yaml").unlink()
    assert _chart(tmp_path, monkeypatch, capsys, "grid_v1") == \
        (2, "", "blackclip chart: 끈 전략이라 가드레일 판이 없습니다(grid_v1), blackclip status grid_v1로 보세요\n")
    assert _chart(tmp_path, monkeypatch, capsys, "nope")[2].startswith("blackclip chart: 전략이 없습니다(nope)")

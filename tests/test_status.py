"""blackclip status: 늘 평문 · 결론 두 줄 먼저 · 엔진 숫자만."""
import json
import os
import subprocess
import sys
import time
from datetime import date

import pytest
from facts import record, write_facts

from blackclip.cli import main
from blackclip.daily.days import append_day
from blackclip.daily.reconcile import Day
from blackclip.screen.status import FORBIDDEN
from blackclip.states import TRACKED
from blackclip.workspace import WorkspaceError

ONE = [
    "trend_10_50 · 경고 · 실행 문제로 추정 · 10/14 기준 · 실전 22일째 · 거래 37건",
    "실제 -4.6% · 같은 기간 BT -1.9% · 정상 범위 -3.2% ~ +6.9%(22일째 · 보수적 비용 기준)",
    "- 전략: 판단 보류 · 같은 기간 BT -1.9%(정상 범위 -3.2% ~ +6.9%(22일째 · 보수적 비용 기준) 안) · 엣지가 없어도 범위 안일 확률 79%",
    "- 실행: 경고 · 실제 − BT(비용 0) -2.7%p = 수수료 -0.2%p + 체결가 차이 -0.4%p(중앙값 -5.3bp · 최대 -9.1bp · 20bp 넘음 0건) + "
    "나머지 -2.1%p(추정 · 원인 후보: 백테스트에만 있는 거래 1건(놓친 주문으로 추정 · 처음 10/13 15:58 UTC BTCUSDT 매도))",
    "- 봇: 정상 · 체결 10/14분까지 받음 · 최근 7일 11건(백테스트 기준 7~17건, 청산 시각)",
    "- 가드레일 선: 정상 · 실제 -4.6%(이번 구간 최저 -4.6%) · 경고선 -7.2%(민감도 민감 · 하루 1회 확인 기준)",
    "거래 빈도: 최근 7일 11건 · 백테스트 기준 7~17건(청산 시각)",
    "가드레일 대응(내가 정해 둔 것): 실행 경고 → 주문 코드부터 확인. 전략은 그대로 둔다 · 경고선 → 중단 검토. 멈출지는 내가 정한다",
    "근거: 이번 구간 2026-09-23 ~ 2026-10-14 UTC(30일씩 끊음) · 한 달 정상 범위 -3.7% ~ +8.1%(90% · 보수적 비용 기준) · "
    "경고선 -7.2%(민감도 민감) · 가드레일 v1",
    "그림: ! blackclip chart trend_10_50",
]

ALL = [
    "실전 4개 · 10/14 기준(매일 대조 기록) · 숫자는 실전 시작부터 · 그림은 ! blackclip chart",
    "breakout_v2 · 확인 불가(체결 내역을 못 받음) · 실제 +0.4%(10/11까지) · 실제−BT(비용 0) 모름 · 거래 33건",
    "trend_10_50 · 경고 · 실행 문제로 추정 · 실제 -4.6% · 실제−BT(비용 0) -2.7%p · 거래 37건",
    "grid_v1 · 판단 보류 · 실제 -0.8% · 실제−BT(비용 0) -0.3%p · 거래 12건",
    "trend_ema_v7 · 정상 · 실제 +1.2% · 실제−BT(비용 0) -0.2%p · 거래 41건",
]


@pytest.fixture(autouse=True)
def _home(monkeypatch, tmp_path):
    monkeypatch.setenv("HOME", str(tmp_path / "home"))


def _run(tmp_path, monkeypatch, capsys, *args):
    monkeypatch.chdir(tmp_path)
    code = main(["status", *args])
    out = capsys.readouterr()
    return code, out.out.splitlines(), out.err.strip().splitlines()


def test_status_one_strategy_golden(tmp_path, monkeypatch, capsys):
    write_facts(tmp_path)
    assert _run(tmp_path, monkeypatch, capsys, "trend_10_50") == (0, ONE, [])


def test_status_all_golden(tmp_path, monkeypatch, capsys):
    write_facts(tmp_path)
    assert _run(tmp_path, monkeypatch, capsys) == (0, ALL, [])


def test_status_all_lines_say_their_own_day(tmp_path, monkeypatch, capsys):
    root = write_facts(tmp_path)
    p = root / "strategies" / "grid_v1" / "daily" / "days.jsonl"
    p.write_text("".join(line + "\n" for line in p.read_text().splitlines()[:-2]))
    _, out, _ = _run(tmp_path, monkeypatch, capsys)
    assert out[3] == "grid_v1 · 판단 보류 · 실제 -0.6% · 실제−BT(비용 0) -0.3%p · 거래 11건 · 10/12 기준"


@pytest.mark.parametrize("no_color", [None, "1"])
def test_status_is_plain_text(tmp_path, monkeypatch, capsys, no_color):
    """AI에게 가는 status 출력에 ESC · 그림 글자 0, 레포 글의 ESC는 \\u001b 꼴로."""
    if no_color:
        monkeypatch.setenv("NO_COLOR", no_color)
    else:
        monkeypatch.delenv("NO_COLOR", raising=False)
    root = write_facts(tmp_path)
    p = root / "strategies" / "trend_10_50" / "daily" / "days.jsonl"
    last = record("trend_10_50", 22)
    last["lines"]["실행"] += " \x1b[31m빨강\x1b[0m"
    with p.open("a") as f:
        f.write(json.dumps(last, ensure_ascii=False) + "\n")
    for args in ((), ("trend_10_50",), ("grid_v1",), ("breakout_v2",)):
        code, out, _ = _run(tmp_path, monkeypatch, capsys, *args)
        text = "\n".join(out)
        assert code == 0 and not (set(text) & FORBIDDEN), (args, set(text) & FORBIDDEN)
    assert "\\u001b[31m빨강" in "\n".join(_run(tmp_path, monkeypatch, capsys, "trend_10_50")[1])


def test_status_and_chart_do_not_import_pandas(tmp_path):
    """계획서 Global Constraints: status · chart(전체 · 전략 보기)가 pandas · numpy를 부르지 않는다(chart도 묶음)."""
    write_facts(tmp_path)
    src = os.path.join(os.path.dirname(__file__), "..", "src")
    code = ("import sys; from blackclip.cli import main; main(['status']); main(['status', 'trend_10_50']); "
            "main(['chart']); main(['chart', 'trend_10_50']); main(['chart', 'trend_10_50', '--trades']); "
            "print([m for m in ('pandas', 'numpy') if m in sys.modules], file=sys.stderr)")
    r = subprocess.run([sys.executable, "-c", code], cwd=tmp_path, capture_output=True, text=True, check=True,
                       env={**os.environ, "PYTHONPATH": src, "HOME": str(tmp_path / "home")})
    assert r.stderr.strip().splitlines()[-1] == "[]"


BAD_FILES = [
    (lambda d: (d / "daily" / "days.jsonl").unlink() or (d / "daily" / "days.jsonl").symlink_to(d / "guardrail.v1.yaml"),
     "심볼릭 링크"),
    (lambda d: (d / "daily" / "days.jsonl").unlink() or os.mkfifo(d / "daily" / "days.jsonl"), "일반 파일이 아니라"),
    (lambda d: (d / "guardrail.v1.yaml").write_text("[" * 200_000), "중첩이 너무 깊음"),
    (lambda d: (d / "guardrail.v1.yaml").write_text('{"normal_range": ' + "9" * 5000 + "}"), "숫자가 너무 김"),
    (lambda d: (d / "guardrail.v1.yaml").write_text("[]"), "가드레일 모양이 다릅니다"),
    (lambda d: (d / "guardrail.v1.yaml").write_text("{}"), "가드레일 모양이 다릅니다(target)"),
    # Path.rename이 새 경로(참)를 돌려줘 `or`로 이으면 링크가 안 만들어짐 → 리스트로 둘 다 실행
    (lambda d: [(d / "daily").rename(d / "moved"), (d / "daily").symlink_to(d / "moved", target_is_directory=True)],
     "상위 폴더가 링크"),
]
"""한 전략의 레포 파일을 깨뜨리는 7가지(단독 보기는 한 줄 · 종료 2, 전체 보기는 그 전략만 확인 불가 · 종료 4)."""


@pytest.mark.parametrize("break_it, msg", BAD_FILES)
def test_status_bad_repo_files_are_one_line(tmp_path, monkeypatch, capsys, break_it, msg):
    root = write_facts(tmp_path, ("trend_10_50",))
    break_it(root / "strategies" / "trend_10_50")
    t0 = time.monotonic()
    code, out, err = _run(tmp_path, monkeypatch, capsys, "trend_10_50")
    assert time.monotonic() - t0 < 5
    assert code == 2 and out == [] and len(err) == 1 and err[0].startswith("blackclip status: ") and msg in err[0]


def test_status_wrong_shape_lines_and_huge_numbers_fall_back(tmp_path, monkeypatch, capsys):
    """줄 모양이 틀렸거나 숫자가 너무 크면 그 줄만 빠져 앞날이 보임(지어내지 않음)."""
    root = write_facts(tmp_path, ("trend_10_50",))
    bad = record("trend_10_50", 22)
    bad["day"], bad["nums"]["actual"] = "2026-10-15", 10 ** 400
    with (root / "strategies" / "trend_10_50" / "daily" / "days.jsonl").open("a") as f:
        f.write(json.dumps(bad, ensure_ascii=False) + "\n" + json.dumps({"v": 1, "day": [1]}) + "\n")
    _, out, _ = _run(tmp_path, monkeypatch, capsys, "trend_10_50")
    assert out[0].endswith("· 10/14 기준 · 실전 22일째 · 거래 37건")


@pytest.mark.skipif(os.geteuid() == 0, reason="root는 권한을 무시함")
def test_status_unreadable_days_is_one_line(tmp_path, monkeypatch, capsys):
    root = write_facts(tmp_path, ("trend_10_50",))
    (root / "strategies" / "trend_10_50" / "daily" / "days.jsonl").chmod(0)
    code, _, err = _run(tmp_path, monkeypatch, capsys, "trend_10_50")
    assert code == 2 and len(err) == 1


def test_status_without_records_or_workspace(tmp_path, monkeypatch, capsys):
    assert _run(tmp_path, monkeypatch, capsys)[0] == 2  # .blackclip 없음
    root = write_facts(tmp_path, ("grid_v1",))
    (root / "strategies" / "grid_v1" / "daily" / "days.jsonl").unlink()
    code, out, _ = _run(tmp_path, monkeypatch, capsys, "grid_v1")
    assert code == 0 and out == ["grid_v1 · 실전 중 · 매일 대조 기록 아직 없음(첫 blackclip daily 뒤에 보임) · 가드레일 v1 · 실전 시작일 2026-09-23"]
    (root / "strategies" / "grid_v1" / "live.json").unlink()  # 실전 여부와 기록 유무를 가름
    assert _run(tmp_path, monkeypatch, capsys, "grid_v1")[1] == ["grid_v1 · 실전 시작 전 · 가드레일 v1 · 점검 결과는 blackclip check grid_v1"]
    assert _run(tmp_path, monkeypatch, capsys, "nope")[0] == 2
    code, _, err = _run(tmp_path, monkeypatch, capsys, "../x")
    assert code == 2 and "전략 이름 모양이 다릅니다" in err[0]


def test_status_second_period_shows_whole_run(tmp_path, monkeypatch, capsys):
    """31일째부터는 head(이번 구간) 다음에 실전 시작부터 한 줄, 전체 줄은 실전 시작부터 숫자."""
    root = write_facts(tmp_path, ("trend_10_50",))
    r = record("trend_10_50", 22)
    r.update(day="2026-10-24", live_day=32)
    r["nums"].update(period_start="2026-10-23", d=2)
    r["nums"]["all"].update(actual=-0.051, bt=-0.02, gap=-0.031)
    with (root / "strategies" / "trend_10_50" / "daily" / "days.jsonl").open("a") as f:
        f.write(json.dumps(r, ensure_ascii=False) + "\n")
    _, out, _ = _run(tmp_path, monkeypatch, capsys, "trend_10_50")
    assert out[2] == "실전 시작부터(9/23 ~ 10/24): 실제 -5.1% · 같은 기간 BT -2.0% · 실제 − BT(비용 0) -3.1%p"
    _, out, _ = _run(tmp_path, monkeypatch, capsys)
    assert out[1] == "trend_10_50 · 경고 · 실행 문제로 추정 · 실제 -5.1% · 실제−BT(비용 0) -3.1%p · 거래 37건"


def _failed_day(root):
    """grid_v1에 10/15 대조 실패한 날(nums 비어 있음)을 덧붙이고 이유 글을 돌려준다."""
    why = "확인 불가(대조하지 못함 · KeyError)"
    append_day(root, Day("grid_v1", date(2026, 10, 15), 23, 0, "실제 모름 · 같은 기간 BT 모름", {k: why for k in TRACKED},
                         {k: "blackclip daily 출력 · 연결 파일 확인" for k in TRACKED}, {}, None))
    return why


def test_status_failed_last_day_is_shown_as_that_day(tmp_path, monkeypatch, capsys):
    """마지막 날이 대조 실패면 옛 정상 숫자가 아니라 그 실패한 날로 말한다."""
    root = write_facts(tmp_path, ("grid_v1",))
    why = _failed_day(root)
    _, out, _ = _run(tmp_path, monkeypatch, capsys, "grid_v1")
    assert out[0] == f"grid_v1 · {why} · 10/15 기준 · 실전 23일째 · 거래 0건" and out[1] == "실제 모름 · 같은 기간 BT 모름"
    assert not any(line.startswith("거래 빈도") for line in out)  # 실패한 날엔 n7이 없어 빈도 줄을 안 지어냄
    assert "가드레일 대응(내가 정해 둔 것): 실행 경고 → 적어 둔 것 없음 · 경고선 → 적어 둔 것 없음" in out
    assert out[-2].startswith("근거: 이번 구간 2026-09-23 ~ 2026-10-15 UTC(30일씩 끊음) · ")  # 구간 시작은 앞 정상 날들에서


def test_status_all_failed_last_day_keeps_last_known_actual_with_its_day(tmp_path, monkeypatch, capsys):
    """전체 줄: 실제는 구간에서 값이 있는 마지막 날 값 + (M/D까지), 차이는 모름, 줄 끝 날짜는 실패한 날."""
    root = write_facts(tmp_path, ("grid_v1",))
    why = _failed_day(root)
    _, out, _ = _run(tmp_path, monkeypatch, capsys)
    assert out == ["실전 1개 · 10/15 기준(매일 대조 기록) · 숫자는 실전 시작부터 · 그림은 ! blackclip chart",
                   f"grid_v1 · {why} · 실제 -0.8%(10/14까지) · 실제−BT(비용 0) 모름 · 거래 0건"]


def test_status_without_drawn_range_says_so(tmp_path, monkeypatch, capsys):
    """그리지 않은 가드레일: 근거 줄이 한 달 범위 대신 이유를 적는다."""
    root = write_facts(tmp_path, ("trend_10_50",))
    gp = root / "strategies" / "trend_10_50" / "guardrail.v1.yaml"
    g = json.loads(gp.read_text())
    g["normal_range"] = {"days": 30, "drawn": False, "reason": "백테스트 왕복 거래 12건 · 기간 80일"}
    gp.write_text(json.dumps(g, ensure_ascii=False))
    _, out, _ = _run(tmp_path, monkeypatch, capsys, "trend_10_50")
    assert "정상 범위 그리지 않음(백테스트 왕복 거래 12건 · 기간 80일) · 경고선 -7.2%(민감도 민감) · 가드레일 v1" in out[-2]


def test_status_no_guardrail_anywhere(tmp_path, monkeypatch, capsys):
    write_facts(tmp_path, ())
    assert _run(tmp_path, monkeypatch, capsys) == (0, ["가드레일을 저장한 전략 없음 · blackclip check <전략> → blackclip save <전략>"], [])


@pytest.mark.parametrize("field", ["head", "rep", "title", "states", "lines", "response", "sensitivity", "reason", "error"])
def test_status_escapes_chart_glyphs_from_repo_text(tmp_path, monkeypatch, capsys, field):
    """scan.show가 못 거르는 인쇄 가능한 그림 글자(█ ━)도 레포 글 · 가드레일 글 · 이름 오류에서 평문 \\uXXXX로."""
    root = write_facts(tmp_path, ("trend_10_50",))
    rec = record("trend_10_50", 22)
    if field in ("head", "title"):
        rec[field] += " █━"
    elif field == "rep":
        rec["rep"] = "경고(█━)"
    elif field == "states":
        rec["states"]["실행"] = "경고(█━)"
    elif field == "lines":
        rec["lines"]["실행"] += " █━"
    elif field == "response":
        rec["response"]["execution_warning"] += " █━"
    if field in ("sensitivity", "reason"):
        gp = root / "strategies" / "trend_10_50" / "guardrail.v1.yaml"
        g = json.loads(gp.read_text())
        if field == "sensitivity":
            g["warning_line"]["sensitivity"] = "민감█━"
        else:
            g["normal_range"] = {"drawn": False, "reason": "자료 부족█━"}
        gp.write_text(json.dumps(g, ensure_ascii=False))
    (root / "strategies" / "trend_10_50" / "daily" / "days.jsonl").write_text(json.dumps(rec, ensure_ascii=False) + "\n")
    monkeypatch.chdir(tmp_path)
    code = main(["status", "█━" if field == "error" else "trend_10_50"])
    cap = capsys.readouterr()
    assert code == (2 if field == "error" else 0)
    assert not (set(cap.out + cap.err) & FORBIDDEN), (field, cap.out, cap.err)
    assert "\\u2588\\u2501" in cap.out + cap.err  # 지워지지 않고 평문으로 남음


# --- 전체 보기는 한 전략을 못 읽어도 나머지를 보인다(종료 4) ---

GRID_ROW = "grid_v1 · 판단 보류 · 실제 -0.8% · 실제−BT(비용 0) -0.3%p · 거래 12건"


@pytest.mark.parametrize("break_it, msg", BAD_FILES)
def test_status_all_isolates_the_broken_strategy(tmp_path, monkeypatch, capsys, break_it, msg):
    """7가지 깨뜨리기를 두 전략 중 한 전략에만: 종료 4 · 다른 전략 행은 그대로 · 깨진 전략은 「확인 불가(사유)」 행 하나 · ESC · 그림 글자 0."""
    root = write_facts(tmp_path, ("trend_10_50", "grid_v1"))
    break_it(root / "strategies" / "trend_10_50")
    t0 = time.monotonic()
    code, out, err = _run(tmp_path, monkeypatch, capsys)
    assert time.monotonic() - t0 < 5
    assert code == 4 and err == []
    assert out[:2] == ["실전 2개 · 10/14 기준(매일 대조 기록) · 숫자는 실전 시작부터 · 그림은 ! blackclip chart", GRID_ROW]
    assert len(out) == 3 and out[2].startswith("trend_10_50 · 확인 불가(") and out[2].endswith(")") and msg in out[2]
    assert not (set("\n".join(out)) & FORBIDDEN)
    assert _run(tmp_path, monkeypatch, capsys, "trend_10_50")[0] == 2  # 이름을 준 보기는 그대로 한 줄 · 종료 2


def test_status_all_broken_rows_follow_the_normal_rows_in_name_order(tmp_path, monkeypatch, capsys):
    """깨진 전략 둘은 정상 행들 뒤에 이름순, 정상 행 순서(급한 상태 먼저)는 그대로."""
    root = write_facts(tmp_path, ("trend_10_50", "grid_v1", "breakout_v2", "trend_ema_v7"))
    for n in ("trend_ema_v7", "breakout_v2"):
        (root / "strategies" / n / "guardrail.v1.yaml").write_text("{}")
    code, out, _ = _run(tmp_path, monkeypatch, capsys)
    assert code == 4 and out[0].startswith("실전 4개 · 10/14 기준")
    assert [x.split(" · ")[0] for x in out[1:]] == ["trend_10_50", "grid_v1", "breakout_v2", "trend_ema_v7"]
    assert out[3].startswith("breakout_v2 · 확인 불가(") and out[4].startswith("trend_ema_v7 · 확인 불가(")


def test_status_all_broken_strategy_counts_as_live_only_with_live_json(tmp_path, monkeypatch, capsys):
    """머리 줄의 「실전 N개」: 확인 불가 전략은 기록 유무를 알 수 없어 live.json이 있을 때만 센다."""
    root = write_facts(tmp_path, ("trend_10_50", "grid_v1"))
    (root / "strategies" / "trend_10_50" / "guardrail.v1.yaml").write_text("{}")
    assert _run(tmp_path, monkeypatch, capsys)[1][0].startswith("실전 2개 · ")
    (root / "strategies" / "trend_10_50" / "live.json").unlink()
    code, out, _ = _run(tmp_path, monkeypatch, capsys)
    assert code == 4 and out[0].startswith("실전 1개 · ") and out[2].startswith("trend_10_50 · 확인 불가(")


def test_status_all_every_strategy_broken_does_not_claim_no_records(tmp_path, monkeypatch, capsys):
    """읽을 수 있는 전략이 없으면 머리 줄이 「기록 아직 없음」이라 단정하지 않고(읽지 못했을 뿐) 깨진 행들만 이어진다."""
    root = write_facts(tmp_path, ("trend_10_50", "grid_v1"))
    for n in ("trend_10_50", "grid_v1"):
        (root / "strategies" / n / "daily" / "days.jsonl").write_text("깨진 줄\n")
    code, out, _ = _run(tmp_path, monkeypatch, capsys)
    assert code == 4 and out[0] == "실전 2개 · 읽을 수 있는 매일 대조 기록 없음 · 그림은 ! blackclip chart"
    assert [x.split(" · ")[0] for x in out[1:]] == ["grid_v1", "trend_10_50"] and all("확인 불가(" in x for x in out[1:])


@pytest.mark.parametrize("body", ['깨진 줄\n{ not json\n', '{"v": 1, "day": [1]}\n["x"]\n'], ids=["not_json", "wrong_shape"])
def test_status_days_file_with_no_readable_line_is_an_error_not_no_record(tmp_path, monkeypatch, capsys, body):
    """읽을 줄이 하나도 없는 days.jsonl은 「기록 아직 없음」이 아니라 오류 — 단독 보기는 한 줄 · 종료 2, 전체 보기는 그 행 · 종료 4."""
    root = write_facts(tmp_path, ("trend_10_50", "grid_v1"))
    (root / "strategies" / "trend_10_50" / "daily" / "days.jsonl").write_text(body)
    code, out, err = _run(tmp_path, monkeypatch, capsys, "trend_10_50")
    assert code == 2 and out == [] and len(err) == 1 and "days.jsonl에 읽을 수 있는 줄이 없습니다" in err[0]
    code, out, _ = _run(tmp_path, monkeypatch, capsys)
    assert code == 4 and out[1] == GRID_ROW and "days.jsonl에 읽을 수 있는 줄이 없습니다" in out[2] and not any("아직 없음" in x for x in out)


def test_status_all_reason_rule_oserror_type_only_and_plain_glyphs(tmp_path, monkeypatch, capsys):
    """사유: OSError는 종류 이름만(원문에 경로가 들 수 있음), WorkspaceError는 메시지 그대로이되 그림 글자 · ESC는 평문(status는 늘 평문)."""
    import blackclip.screen.status as status_mod
    write_facts(tmp_path, ("trend_10_50", "grid_v1"))
    real = status_mod.read_days

    def flaky(root_, name):
        if name == "trend_10_50":
            raise OSError("EIO /Users/secret-customer/repo/.blackclip")
        return real(root_, name)

    monkeypatch.setattr(status_mod, "read_days", flaky)
    code, out, _ = _run(tmp_path, monkeypatch, capsys)
    assert code == 4 and out[2] == "trend_10_50 · 확인 불가(OSError)" and "secret-customer" not in "\n".join(out)

    def glyphs(root_, name):
        if name == "trend_10_50":
            raise WorkspaceError("█━ \x1b[31m")
        return real(root_, name)

    monkeypatch.setattr(status_mod, "read_days", glyphs)
    code, out, _ = _run(tmp_path, monkeypatch, capsys)
    assert code == 4 and out[2] == "trend_10_50 · 확인 불가(\\u2588\\u2501 \\u001b[31m)" and not (set("\n".join(out)) & FORBIDDEN)


def test_status_named_off_strategy_is_the_one_off_line(tmp_path, monkeypatch, capsys):
    """끈 전략(중지 · 은퇴 · 기록 손상)을 이름으로 주면 전체 보기의 끈 전략 줄 하나만(숫자 화면 없음) · 종료 0, 기록 손상은 4 · 평문."""
    write_facts(tmp_path, ("grid_v1", "trend_10_50"))
    monkeypatch.chdir(tmp_path)
    assert main(["record", "grid_v1", "--state", "은퇴"]) == 0
    capsys.readouterr()
    code, out, err = _run(tmp_path, monkeypatch, capsys, "grid_v1")
    assert (code, out, err) == (0, [f"grid_v1 · 은퇴 · {date.today().isoformat()} · 끈 뒤 회고 있음"], [])
    assert _run(tmp_path, monkeypatch, capsys)[1][-1] == out[0]  # 전체 보기의 끈 전략 줄과 같은 글
    rec = tmp_path / ".blackclip" / "strategies" / "grid_v1" / "record.md"
    rec.write_text("<<<<<<< HEAD\n" + rec.read_text() + "=======\n>>>>>>> origin/main\n")
    code, out, _ = _run(tmp_path, monkeypatch, capsys, "grid_v1")
    assert code == 4 and out == ["grid_v1 · 기록 손상 · 전략 기록(record.md)을 읽지 못함 · 대조 멈춤"]
    assert not (set("\n".join(out)) & FORBIDDEN)
    assert _run(tmp_path, monkeypatch, capsys, "trend_10_50")[1][0].startswith("trend_10_50 · 경고")  # 실전 전략은 그대로 자세한 화면


def test_status_all_off_head_does_not_claim_no_records(tmp_path, monkeypatch, capsys):
    """전략이 모두 꺼졌으면 머리 줄이 「매일 대조 기록 아직 없음」이라 단정하지 않고 끈 전략 수만 말한다."""
    write_facts(tmp_path, ("grid_v1", "trend_10_50"))
    monkeypatch.chdir(tmp_path)
    for n in ("grid_v1", "trend_10_50"):
        assert main(["record", n, "--state", "중지"]) == 0
    capsys.readouterr()
    code, out, _ = _run(tmp_path, monkeypatch, capsys)
    assert code == 0 and out[0] == "실전 0개 · 끈 전략 2개 · 그림은 ! blackclip chart" and len(out) == 3, out


def test_stopped_strategy_without_guardrail_board(tmp_path, monkeypatch, capsys):
    """가드레일 판 파일이 없는 끈 전략도 이름으로 주면 끈 전략 줄(종료 0), 전체 보기에도 그 줄. 빈 파일 strategies/notes는 전략이 아님."""
    root = write_facts(tmp_path, ("grid_v1", "trend_10_50"))
    monkeypatch.chdir(tmp_path)
    assert main(["record", "grid_v1", "--state", "중지"]) == 0
    capsys.readouterr()
    (root / "strategies" / "grid_v1" / "guardrail.v1.yaml").unlink()
    (root / "strategies" / "notes").write_text("", encoding="utf-8")
    want = f"grid_v1 · 중지 · {date.today().isoformat()} · 끈 뒤 회고 있음"
    assert _run(tmp_path, monkeypatch, capsys, "grid_v1") == (0, [want], [])
    code, out, _ = _run(tmp_path, monkeypatch, capsys)
    assert code == 0 and out[-1] == want and not any(x.startswith("notes") for x in out), out
    assert _run(tmp_path, monkeypatch, capsys, "notes")[0] == 2  # 폴더가 아니라 전략이 아님

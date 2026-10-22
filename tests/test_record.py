"""전략 기록: 머리 줄 · 고객 메모 · state.log · 끈 전략 멈춤 · 계좌 귀속 · 손상 · 이어서 마치기."""
import json
import os
import re
import subprocess
import sys
from datetime import date

import pytest
from facts import record, write_facts

import blackclip.record as rec_mod
from blackclip.cli import main
from blackclip.daily.run import _guards
from blackclip.notify import read_live
from blackclip.record import (
    AFTER,
    MEMO,
    RECORD,
    RecordError,
    after_event,
    after_page,
    after_values,
    current_state,
    lesson,
    lessons,
    read_record,
    stopped,
    write_record,
)
from blackclip.screen.status import FORBIDDEN
from blackclip.watch.state import pick

STOPS = "이 전략의 매일 대조 · 실시간 감시 · 텔레그램을 멈춤(다른 전략은 그대로)"
KEPT = "기록: .blackclip/strategies/grid_v1/record.md(git에 올라감 · 공개 레포면 이유 글도 보임)"


@pytest.fixture(autouse=True)
def _home(monkeypatch, tmp_path):
    monkeypatch.setenv("HOME", str(tmp_path / "home"))


def _rec(tmp_path, monkeypatch, capsys, *args):
    monkeypatch.chdir(tmp_path)
    code = main(["record", *args])
    out = capsys.readouterr()
    return code, out.out.splitlines(), out.err.strip().splitlines()


def _root(tmp_path):
    return tmp_path / ".blackclip"


def _folder(tmp_path, name="grid_v1"):
    return _root(tmp_path) / "strategies" / name


def _log(tmp_path, name="grid_v1"):
    return [x for x in map(json.loads, (_root(tmp_path) / "state.log").read_text().splitlines())
            if x.get("strategy") == name and "record" in x]


def test_stop_writes_record_state_log_and_says_what_stops(tmp_path, monkeypatch, capsys):
    """① 골든 줄: 실전 → 중지 · 멈추는 것 · git으로 가는 기록."""
    write_facts(tmp_path)
    code, out, err = _rec(tmp_path, monkeypatch, capsys, "grid_v1", "--state", "중지", "--reason", "횡보장이 끝나 그리드가 안 맞음")
    today = date.today().isoformat()
    assert (code, err) == (0, [])
    assert out[:2] == [f"grid_v1 · 실전 → 중지 · {today}", STOPS] and out[-1] == KEPT
    text = (_folder(tmp_path) / RECORD).read_text()
    assert text.startswith("<!-- blackclip record v1 {") and "- 바꾼 이유: 횡보장이 끝나 그리드가 안 맞음" in text and MEMO in text
    assert _log(tmp_path) == [{"day": today, "record": "중지", "strategy": "grid_v1"}]
    assert current_state(_root(tmp_path), "grid_v1") == "중지"
    assert not (set("\n".join(out)) & FORBIDDEN)


def test_no_reason_is_written_as_not_recorded(tmp_path, monkeypatch, capsys):
    write_facts(tmp_path, ("grid_v1",))
    _rec(tmp_path, monkeypatch, capsys, "grid_v1", "--state", "중지")
    assert "- 바꾼 이유: 이유 미기록" in (_folder(tmp_path) / RECORD).read_text()


def test_stopped_strategy_leaves_daily_and_watch(tmp_path, monkeypatch, capsys):
    """끈 전략은 stopped에 들고(대조 · 감시 루프에서 빠짐), _guards에는 남음(계좌 귀속용). 중지 · 은퇴는 status 종료 코드를 올리지 않음."""
    root = write_facts(tmp_path)
    _rec(tmp_path, monkeypatch, capsys, "grid_v1", "--state", "중지", "--reason", "거래가 너무 적음")
    _rec(tmp_path, monkeypatch, capsys, "breakout_v2", "--state", "은퇴")
    today = date.today().isoformat()
    assert stopped(root) == {"grid_v1": ("중지", today), "breakout_v2": ("은퇴", today)}
    guards, _ = _guards(root)
    assert set(guards) == {"grid_v1", "breakout_v2", "trend_10_50", "trend_ema_v7"}
    monkeypatch.chdir(tmp_path)
    assert main(["status"]) == 0
    out = capsys.readouterr().out.splitlines()
    assert out[0].startswith("실전 2개 · ") and out[0].endswith("· 끈 전략 2개")
    assert out[-2].startswith(f"breakout_v2 · 은퇴 · {today}") and out[-1].startswith(f"grid_v1 · 중지 · {today}")
    assert not any(x.startswith(("grid_v1 · 실전", "breakout_v2 · 실전", "grid_v1 · 정상", "breakout_v2 · 정상")) for x in out[1:-2])


def test_stopped_ignores_non_folder_entries(tmp_path, monkeypatch, capsys):
    """strategies/ 아래 빈 파일(notes)이나 다른 폴더를 가리키는 링크(x)는 전략이 아니니 stopped에 들지 않는다(거짓 「기록 손상」 없음)."""
    root = write_facts(tmp_path, ("grid_v1",))
    (root / "strategies" / "notes").write_text("", encoding="utf-8")
    (root / "strategies" / "x").symlink_to(root / "strategies" / "grid_v1", target_is_directory=True)
    assert stopped(root) == {}
    _rec(tmp_path, monkeypatch, capsys, "grid_v1", "--state", "중지")
    p = _folder(tmp_path) / RECORD
    p.write_text("<<<<<<< HEAD\n" + p.read_text() + "=======\n>>>>>>> origin/main\n")
    assert stopped(root) == {"grid_v1": ("기록 손상", "")}  # 진짜 폴더의 손상은 그대로 잡힘


def test_stopped_strategy_still_counts_for_shared_account(tmp_path, monkeypatch, capsys):
    """같은 계좌의 두 전략 중 하나를 꺼도(봇 · 잔여 포지션은 남을 수 있음) 남은 전략을 혼자 계좌로 보지 않는다."""
    root = write_facts(tmp_path, ("grid_v1", "trend_10_50"))
    _rec(tmp_path, monkeypatch, capsys, "grid_v1", "--state", "중지")
    guards, _ = _guards(root)
    watched, skipped, mixed = pick(guards, {n: read_live(root, n) for n in guards})
    assert "trend_10_50" not in watched
    assert any(n == "trend_10_50" and "섞인 계좌" in why for n, why in skipped)


def test_broken_record_keeps_strategy_paused(tmp_path, monkeypatch, capsys):
    """git 충돌 표지로 깨진 record.md는 자동 재개하지 않고 「기록 손상」으로 멈춘 채 화면에 보임 · status 종료 4."""
    root = write_facts(tmp_path, ("grid_v1", "trend_10_50"))
    _rec(tmp_path, monkeypatch, capsys, "grid_v1", "--state", "중지")
    p = _folder(tmp_path) / RECORD
    p.write_text("<<<<<<< HEAD\n" + p.read_text() + "=======\n# 다른 컴퓨터\n>>>>>>> origin/main\n")
    assert current_state(root, "grid_v1") == "기록 손상" and stopped(root)["grid_v1"] == ("기록 손상", "")
    monkeypatch.chdir(tmp_path)
    assert main(["status"]) == 4
    assert capsys.readouterr().out.splitlines()[-1] == "grid_v1 · 기록 손상 · 전략 기록(record.md)을 읽지 못함 · 대조 멈춤"
    code, _, err = _rec(tmp_path, monkeypatch, capsys, "grid_v1", "--state", "실전")
    assert code == 2 and "덮어쓰지 않습니다" in err[0] and p.read_text().startswith("<<<<<<< HEAD")


def test_stop_can_resume_but_retire_cannot(tmp_path, monkeypatch, capsys):
    """규칙 표: 중지 → 실전은 됨(state.log 한 줄), 은퇴는 되돌리지 않음."""
    write_facts(tmp_path)
    assert _rec(tmp_path, monkeypatch, capsys, "grid_v1", "--state", "실전")[1] == ["바뀐 것 없음 · grid_v1 · 실전"]
    _rec(tmp_path, monkeypatch, capsys, "grid_v1", "--state", "중지")
    code, out, _ = _rec(tmp_path, monkeypatch, capsys, "grid_v1", "--state", "실전", "--decision", "주문 코드 고침")
    assert code == 0 and out[1] == "이 전략의 매일 대조 · 실시간 감시 · 텔레그램을 다시 켬(다음 예약부터) · 끈 뒤 회고는 남음"
    assert current_state(_root(tmp_path), "grid_v1") == "실전"
    code, out, _ = _rec(tmp_path, monkeypatch, capsys, "grid_v1", "--state", "은퇴")
    assert out[1:3] == [STOPS, "은퇴는 되돌리지 않음 · 다시 쓰려면 새 이름"]
    code, _, err = _rec(tmp_path, monkeypatch, capsys, "grid_v1", "--state", "실전")
    assert code == 2 and err == ["blackclip record: 은퇴한 전략은 되돌리지 않습니다 · 다시 쓰려면 새 이름으로"]
    assert [x["record"] for x in _log(tmp_path)] == ["중지", "실전", "은퇴"]


@pytest.mark.parametrize("fail_at, next_day", [("record.md", False), ("state.log", False), ("state.log", True)])
def test_record_resumes_after_each_failed_step(tmp_path, monkeypatch, capsys, fail_at, next_day):
    """이유 · 결정이 있는 같은 요청을 다시 하면(다음 날이어도) 사건을 늘리지 않고 빠진 단계만 마침.
    after.md 단계와 그 실패는 따로 시험한다."""
    write_facts(tmp_path, ("grid_v1",))
    args = ("grid_v1", "--state", "중지", "--reason", "횡보장이 끝남", "--decision", "봇 끔")
    real_write, real_append = rec_mod.write_in, rec_mod.append_line

    def boom(*a, **k):
        raise OSError("디스크 가득")

    if fail_at == "state.log":
        monkeypatch.setattr(rec_mod, "append_line", boom)
    else:
        monkeypatch.setattr(rec_mod, "write_in", lambda root, rel, *a, **k: boom() if str(rel).endswith(fail_at)
                            else real_write(root, rel, *a, **k))
    code, out, err = _rec(tmp_path, monkeypatch, capsys, *args)
    assert code == 2 and err[0].endswith("같은 명령을 다시 하면 이어서 마칩니다")
    monkeypatch.setattr(rec_mod, "write_in", real_write)
    monkeypatch.setattr(rec_mod, "append_line", real_append)
    today = date.today().isoformat()
    p = _folder(tmp_path) / RECORD
    if next_day and p.exists():  # 다음 날 다시: 사건 날짜만 어제로
        p.write_text(p.read_text().replace(today, "2000-01-01"))
    code, out, _ = _rec(tmp_path, monkeypatch, capsys, *args)
    assert code == 0
    if fail_at == "state.log":
        assert out == ["이어서 마침 · grid_v1 · 중지 · state.log"]
    else:
        assert out[0] == f"grid_v1 · 실전 → 중지 · {today}"
    doc, _ = read_record(_root(tmp_path), "grid_v1")
    assert len(doc["events"]) == 1 and len(_log(tmp_path)) == 1 and current_state(_root(tmp_path), "grid_v1") == "중지"


def test_record_refuses_to_grow_past_cap_and_keeps_file(tmp_path, monkeypatch, capsys):
    """쓰기 전에 최종 바이트를 재고, 상한을 넘으면 기존 record.md · state.log를 그대로 두고 종료 2."""
    write_facts(tmp_path, ("grid_v1",))
    monkeypatch.setattr(rec_mod, "CAP", 8192)
    assert _rec(tmp_path, monkeypatch, capsys, "grid_v1", "--state", "중지", "--reason", "가" * 400)[0] == 0
    p = _folder(tmp_path) / RECORD
    before = p.read_text()
    code, _, err = _rec(tmp_path, monkeypatch, capsys, "grid_v1", "--state", "실전", "--reason", "나" * 500,
                        "--decision", "다" * 500)
    assert code == 2 and "상한" in err[0] and p.read_text() == before and len(_log(tmp_path)) == 1


def test_customer_memo_survives_and_broken_head_is_not_overwritten(tmp_path, monkeypatch, capsys):
    write_facts(tmp_path)
    _rec(tmp_path, monkeypatch, capsys, "grid_v1", "--state", "실전", "--decision", "실전 시작")  # 같은 상태 + 글 = 사건만
    p = _folder(tmp_path) / RECORD
    assert not _log(tmp_path)
    p.write_text(p.read_text() + "고객이 쓴 메모: 10/20 다시 볼 것\n")
    _rec(tmp_path, monkeypatch, capsys, "grid_v1", "--state", "중지", "--reason", "범위 밖이 길어짐")
    text = p.read_text()
    assert text.endswith(MEMO + "\n고객이 쓴 메모: 10/20 다시 볼 것\n") and "- 바꾼 이유: 범위 밖이 길어짐" in text
    p.write_text("# 손으로 다시 쓴 기록\n")
    code, _, err = _rec(tmp_path, monkeypatch, capsys, "grid_v1", "--state", "실전")
    assert code == 2 and "머리 줄" in err[0] and p.read_text() == "# 손으로 다시 쓴 기록\n"


@pytest.mark.parametrize("head", [
    '{"events": ' + "[" * 50_000 + "]" * 50_000 + ', "lesson": null}',  # 100KB < CAP라 HEAD 정규식을 지나 파서 재귀까지
    '{"events": [{"day": ' + "9" * 5000 + ', "state": "중지"}], "lesson": null}',
    '{"events": "중지", "lesson": null}',
    '{"events": [{"day": "2026-10-16", "state": "꺼짐", "reason": null, "decision": null}], "lesson": null}',
    '{"events": [{"day": "2026-10-16", "state": "중지", "reason": "\\u001b[31m", "decision": null}], "lesson": null}',
])
def test_read_record_refuses_bad_heads(tmp_path, head):
    root = write_facts(tmp_path, ("grid_v1",))
    (root / "strategies" / "grid_v1" / RECORD).write_text(f"<!-- blackclip record v1 {head} -->\n# x\n")
    with pytest.raises(RecordError):
        read_record(root, "grid_v1")
    assert current_state(root, "grid_v1") == "기록 손상"


def test_read_record_links_fifo_size_permission_and_parent_link(tmp_path):
    root = write_facts(tmp_path, ("grid_v1",))
    p = root / "strategies" / "grid_v1" / RECORD
    outside = tmp_path / "outside.md"
    outside.write_text('<!-- blackclip record v1 {"events": [], "lesson": null} -->\n')
    p.symlink_to(outside)
    with pytest.raises(RecordError):
        read_record(root, "grid_v1")
    p.unlink()
    os.mkfifo(p)
    with pytest.raises(RecordError):
        read_record(root, "grid_v1")
    p.unlink()
    p.write_text(outside.read_text() + "x" * (256 << 10))
    with pytest.raises(RecordError):
        read_record(root, "grid_v1")
    if os.geteuid() != 0:
        p.write_text(outside.read_text())
        p.chmod(0)
        with pytest.raises(RecordError):
            read_record(root, "grid_v1")
        p.chmod(0o600)
    p.unlink()
    (tmp_path / "away").mkdir()
    (root / "strategies" / "grid_v1").rename(tmp_path / "away" / "grid_v1")
    (root / "strategies" / "grid_v1").symlink_to(tmp_path / "away" / "grid_v1", target_is_directory=True)
    (tmp_path / "away" / "grid_v1" / RECORD).write_text(outside.read_text())
    with pytest.raises(RecordError):  # 전략 폴더 자체가 링크
        read_record(root, "grid_v1")
    assert read_record(root, "none") is None


def test_record_refuses_bad_names_text_and_links(tmp_path, monkeypatch, capsys):
    root = write_facts(tmp_path, ("grid_v1",))
    for args in (("../x", "--state", "중지"), ("nope", "--state", "중지"),
                 ("grid_v1", "--state", "중지", "--reason", "두\n줄"),
                 ("grid_v1", "--state", "중지", "--reason", "/Users/someone/secret 때문"),
                 ("grid_v1", "--state", "중지", "--reason", "가" * 501)):
        code, out, err = _rec(tmp_path, monkeypatch, capsys, *args)
        assert code == 2 and out == [] and len(err) == 1 and err[0].startswith("blackclip record: "), args
    target = tmp_path / "elsewhere.log"
    target.write_text("keep\n")
    (root / "state.log").unlink()
    (root / "state.log").symlink_to(target)
    code, _, err = _rec(tmp_path, monkeypatch, capsys, "grid_v1", "--state", "중지")
    assert code == 2 and target.read_text() == "keep\n" and err[0].endswith("같은 명령을 다시 하면 이어서 마칩니다")


@pytest.mark.parametrize("text", ["api_key=DUMMYDUMMYDUMMYDUMMYDUMMY", "https://example.invalid/x",
                                  "봇 " + "123456789:" + "T" * 31 + "Zq9x"])
def test_record_refuses_secret_or_address_text(tmp_path, monkeypatch, capsys, text):
    """커밋되는 record.md에 키 · 토큰 · 주소 모양 글이 남지 않게 --reason · --decision에서 거절(종료 2, 아무것도 안 씀)."""
    root = write_facts(tmp_path, ("grid_v1",))
    before = [(root / "state.log").read_bytes(), (_folder(tmp_path) / RECORD).exists()]
    for opt in ("--reason", "--decision"):
        code, out, err = _rec(tmp_path, monkeypatch, capsys, "grid_v1", "--state", "중지", opt, text)
        assert code == 2 and out == [] and len(err) == 1, (opt, err)
        assert err[0] == f"blackclip record: {opt}에 키 · 토큰 · 주소 모양 글이 있음 · 커밋되는 record.md에 남으므로 말로 적어 주세요"
    assert [(root / "state.log").read_bytes(), (_folder(tmp_path) / RECORD).exists()] == before
    assert not (_folder(tmp_path) / AFTER).exists()


def test_record_does_not_import_pandas(tmp_path):
    """record 명령(끄기 · 같은 요청 · 다시 켜기)과 끈 전략이 든 status가 pandas · numpy를 부르지 않는다."""
    write_facts(tmp_path, ("grid_v1",))
    src = os.path.join(os.path.dirname(__file__), "..", "src")
    code = ("import sys; from blackclip.cli import main; "
            "codes = [main(['record', 'grid_v1', '--state', '중지', '--reason', 'x']), "
            "main(['record', 'grid_v1', '--state', '중지', '--reason', 'x']), main(['record', 'grid_v1', '--state', '실전']), "
            "main(['status'])]; "
            "print(codes, [m for m in ('pandas', 'numpy') if m in sys.modules], file=sys.stderr)")
    r = subprocess.run([sys.executable, "-c", code], cwd=tmp_path, capture_output=True, text=True, check=True,
                       env={**os.environ, "PYTHONPATH": src, "HOME": str(tmp_path / "home")})
    assert r.stderr.strip().splitlines()[-1] == "[0, 0, 0, 0] []"


def test_resume_finds_missing_state_log_line_after_same_day_flip_flop(tmp_path, monkeypatch, capsys):
    """같은 날 중지 → 실전 → 중지에서 세 번째 state.log 덧붙이기가 실패해도 같은 요청이 그 줄을 이어서 덧붙인다(그날 첫 「중지」 줄에 속지 않음)."""
    write_facts(tmp_path, ("grid_v1",))
    assert _rec(tmp_path, monkeypatch, capsys, "grid_v1", "--state", "중지")[0] == 0
    assert _rec(tmp_path, monkeypatch, capsys, "grid_v1", "--state", "실전")[0] == 0
    real = rec_mod.append_line
    monkeypatch.setattr(rec_mod, "append_line", lambda *a, **k: (_ for _ in ()).throw(OSError("디스크 가득")))
    assert _rec(tmp_path, monkeypatch, capsys, "grid_v1", "--state", "중지")[0] == 2
    monkeypatch.setattr(rec_mod, "append_line", real)
    code, out, _ = _rec(tmp_path, monkeypatch, capsys, "grid_v1", "--state", "중지")
    assert code == 0 and out == ["이어서 마침 · grid_v1 · 중지 · state.log"]
    assert [x["record"] for x in _log(tmp_path)] == ["중지", "실전", "중지"]
    assert _rec(tmp_path, monkeypatch, capsys, "grid_v1", "--state", "중지")[1] == ["바뀐 것 없음 · grid_v1 · 중지"]


GRID_LESSON = "거래비용 · 체결가 차이 -0.1%p · 수수료 -0.2%p"
TREND_LESSON = "거래비용 · 체결가 차이 -0.4%p · 수수료 -0.2%p · 놓친 주문 1건(추정) 나머지 -2.1%p"
GRID_PAGE = """# 끈 뒤 회고 · grid_v1

실전 2026-09-23 ~ 10-16(중지) · 거래 12건 · 거래소 조회 키로 매일 받은 체결 내역으로 계산 · 재현성 확인 전

## ① 실전 기간 전체
- 실제 -0.8% · 같은 기간 백테스트 -0.5% · 차이 -0.3%p
- 전략: ⬓ 판단 보류 · 거래 12건(30건 전이라 판단하지 않음)
- 실행: 잰 것 · 체결가 차이 -0.1%p(중앙값 -5.3bp, 불리한 쪽) · 수수료 -0.2%p / 원인 후보 없음
- 봇: ✓ 정상 · 체결 내역 못 받은 날 없음

## ② 그 기간의 경고와 고객 결정
- 09/23 실전 시작 · 가드레일 v1 · 민감도 민감
- 09/29 봇 판단 보류 → 정상
- 10/16 중지 · 고객 결정
- 기록 없는 날: 없음

## ③ 끈 이유(고객)
이유 미기록

## ④ 다음 점검에서 먼저 볼 것
거래비용 · 체결가 차이 -0.1%p · 수수료 -0.2%p(9/23~10/14 일별 기록)
미관측 10/15~10/16(마지막 일별 기록 뒤 끈 날까지)

엔진은 숫자와 점검 항목만 써요. 설명 문장은 고객의 AI가 만들어요. 실행 쪽 원인 후보는 추정이에요.
"""


def _stop_doc(state="중지", reason=None):
    return {"events": [{"day": "2026-10-16", "state": state, "reason": reason, "decision": None}], "lesson": None}


def test_lesson_table():
    """잰 것 음수 → 거래비용, 놓친 주문 → 나머지와 함께, 전략 경고 날 → 시도 수 · 국면, 0이면 없음, 기록 · 숫자가 없으면 근거 없음."""
    assert lesson([record("grid_v1", k) for k in range(1, 23)]) == GRID_LESSON
    assert lesson([record("trend_10_50", k) for k in range(1, 23)]) == TREND_LESSON
    warn = [record("grid_v1", k) for k in range(1, 23)]
    for r in warn[-3:]:
        r["states"]["전략"] = "경고"
    assert lesson(warn) == GRID_LESSON + " · 시도 수 · 국면(같은 기간 백테스트가 정상 범위 아래였던 날 3일)"
    flat = [record("grid_v1", 22)]
    flat[0]["nums"]["all"].update(fee=0.0, slip=0.0)
    assert lesson(flat) == "없음(잰 차이 · 전략 쪽 경고 없음)"
    assert lesson([]) == "근거 없음(매일 대조 기록 없음)"
    blank = [{**record("grid_v1", 22), "nums": {}}]
    assert lesson(blank) == "근거 없음(마지막 매일 대조에 숫자 없음)"


def test_lesson_counts_missed_order_from_first_period():
    """놓친 주문이 첫 구간에만 있었어도(이번 구간 값 0) 실전 시작부터 값으로 ④에 남는다."""
    r = record("trend_10_50", 22)
    r["nums"].update(missed_n=0, missed=[], rest=0.0, period_start="2026-10-23")
    assert "놓친 주문 1건(추정) 나머지 -2.1%p" in lesson([r])


def test_after_page_grid_v1_golden(tmp_path):
    """② 모양을 FACTS 숫자로: 판단 보류 그대로 · 잰 것과 원인 후보 가름 · 미관측 끝 기간."""
    root = write_facts(tmp_path, ("grid_v1",))
    page, notes = after_page(root, "grid_v1", _stop_doc(), date(2026, 10, 16))
    assert (page, notes) == (GRID_PAGE, [])


def test_after_page_marks_unreceived_missing_and_leading_days(tmp_path):
    """확인 불가 기간은 봇 줄의 「못 받은 기간」, 중간 · 앞쪽 빈 날은 「기록 없는 날」."""
    root = write_facts(tmp_path, ("breakout_v2",))
    p = root / "strategies" / "breakout_v2" / "daily" / "days.jsonl"
    lines = p.read_text().splitlines()
    p.write_text("".join(x + "\n" for i, x in enumerate(lines) if i not in (0, 4, 5)))
    page, _ = after_page(root, "breakout_v2", _stop_doc("은퇴", "키를 다시 못 만듦"), date(2026, 10, 16))
    assert "- 봇: ⬚ 확인 불가 · 못 받은 기간 10/12~10/14" in page
    assert "- 실행: ⬚ 확인 불가(체결 내역을 못 받음)" in page
    assert "- 기록 없는 날: 09/23 · 09/27~09/28(2일)" in page
    assert "- 10/12 실행 정상 → 확인 불가(체결 내역을 못 받음)" in page
    assert "\n## ③ 끈 이유(고객)\n키를 다시 못 만듦\n" in page


def test_after_page_second_period_whole_first_then_parts(tmp_path):
    """①은 실전 기간 전체 요약 먼저, 구간별은 보조 한 줄."""
    root = write_facts(tmp_path, ("trend_10_50",))
    r = record("trend_10_50", 22)
    r.update(day="2026-10-24", live_day=32)
    r["nums"].update(period_start="2026-10-23", d=2, actual=-0.005, bt=-0.001, gap=-0.004)
    r["nums"]["all"].update(actual=-0.051, bt=-0.02, gap=-0.031, funding=None)
    gp = root / "strategies" / "trend_10_50" / "guardrail.v1.yaml"  # 선물이면 펀딩비 「모름」을 숨기지 않음
    g = json.loads(gp.read_text())
    g["target"]["market"] = "futures"
    gp.write_text(json.dumps(g, ensure_ascii=False))
    with (root / "strategies" / "trend_10_50" / "daily" / "days.jsonl").open("a") as f:
        f.write(json.dumps(r, ensure_ascii=False) + "\n")
    page, _ = after_page(root, "trend_10_50", _stop_doc(), date(2026, 10, 25))
    body = page.split("## ① 실전 기간 전체\n")[1].splitlines()
    assert body[0] == "- 실제 -5.1% · 같은 기간 백테스트 -2.0% · 차이 -3.1%p"
    assert any(x.startswith("- 구간별(보조): 9/23~10/14 실제 -4.6% · 백테스트 -1.9% · 차이 -2.7%p / 10/23~10/24 실제 -0.5%")
               for x in body)
    assert "펀딩비 모름(실전 시작부터 합은 첫 30일 구간 안에서만 냄)" in page


def test_after_page_survives_bad_state_log(tmp_path):
    root = write_facts(tmp_path, ("grid_v1",))
    (root / "state.log").unlink()
    (root / "state.log").symlink_to(root / "strategies" / "grid_v1" / "guardrail.v1.yaml")
    page, notes = after_page(root, "grid_v1", _stop_doc(), date(2026, 10, 16))
    assert notes and "state.log를 읽지 못함" in page and GRID_LESSON in page


def test_after_page_survives_unreadable_guardrail(tmp_path, monkeypatch, capsys):
    """가드레일이 없거나 깨져도 끄기는 끝나고, 실전 시작일은 기록 첫날로 대신하며 못 읽은 것을 적는다."""
    root = write_facts(tmp_path, ("grid_v1", "trend_10_50"))
    gp = root / "strategies" / "grid_v1" / "guardrail.v1.yaml"
    gp.unlink()
    page, notes = after_page(root, "grid_v1", _stop_doc(), date(2026, 10, 16))
    assert "- 가드레일을 읽지 못함(" in page and ") · 실전 시작일 · 판 모름" in page and notes and "가드레일을 읽지 못함" in notes[0]
    assert "\n실전 2026-09-23 ~ 10-16(중지) · 거래 12건 · 거래소 조회 키로" in page and "(9/23~10/14 일별 기록)" in page
    assert "- 기록 없는 날: 없음" in page and "재현 확인 전" not in page
    gp = root / "strategies" / "trend_10_50" / "guardrail.v1.yaml"
    gp.write_text("{깨짐")
    code, out, _ = _rec(tmp_path, monkeypatch, capsys, "trend_10_50", "--state", "중지")
    assert code == 0 and "(실전 9/23~10/14 · 거래 37건 · 미관측 10/15~" in out[3]
    assert "가드레일을 읽지 못함" in (_folder(tmp_path, "trend_10_50") / AFTER).read_text()


def test_stop_without_records_finishes(tmp_path, monkeypatch, capsys):
    """첫 daily 전(기록 0줄) · 기록을 못 읽음이어도 중지는 끝나고, 회고는 근거 없음 · 미관측으로."""
    root = write_facts(tmp_path, ("grid_v1", "trend_10_50"))
    (root / "strategies" / "grid_v1" / "daily" / "days.jsonl").unlink()
    code, out, _ = _rec(tmp_path, monkeypatch, capsys, "grid_v1", "--state", "중지")
    today = date.today()
    assert code == 0 and out[3] == f"다음 점검에서 먼저 볼 것: 근거 없음(매일 대조 기록 없음)(실전 9/23~ · 매일 대조 기록 없음 · 미관측 9/23~{today.month}/{today.day})"
    page = (_folder(tmp_path) / AFTER).read_text()
    assert "- 근거 없음(매일 대조 기록 없음 — 숫자를 쓰지 않음)" in page and f"- 기록 없는 날: 09/23~{today:%m/%d}" in page
    os.mkfifo(root / "strategies" / "trend_10_50" / "daily" / "x")
    p = root / "strategies" / "trend_10_50" / "daily" / "days.jsonl"
    p.unlink()
    p.symlink_to(root / "strategies" / "trend_10_50" / "daily" / "x")
    assert _rec(tmp_path, monkeypatch, capsys, "trend_10_50", "--state", "중지")[0] == 0
    assert "매일 대조 기록을 읽지 못함" in (_folder(tmp_path, "trend_10_50") / AFTER).read_text()


def test_stop_writes_after_md_and_lesson_into_record(tmp_path, monkeypatch, capsys):
    """① 다섯 줄 · status ③ 꼬리 · ④가 git으로 가는 record.md에."""
    write_facts(tmp_path)
    code, out, _ = _rec(tmp_path, monkeypatch, capsys, "grid_v1", "--state", "중지")
    today = date.today()
    assert code == 0 and out == [f"grid_v1 · 실전 → 중지 · {today.isoformat()}", STOPS,
                                 "끈 뒤 회고 한 장: .blackclip/strategies/grid_v1/after.md",
                                 f"다음 점검에서 먼저 볼 것: {GRID_LESSON}(실전 9/23~10/14 · 거래 12건 · 미관측 10/15~{today.month}/{today.day})",
                                 KEPT]
    assert (_folder(tmp_path) / AFTER).read_text().startswith("# 끈 뒤 회고 · grid_v1\n")
    assert f"- 다음 점검에서 먼저 볼 것: {GRID_LESSON}" in (_folder(tmp_path) / RECORD).read_text()
    gi = (_root(tmp_path) / ".gitignore").read_text()
    assert "!strategies/*/record.md" in gi and "after.md" not in gi
    monkeypatch.chdir(tmp_path)
    main(["status"])
    assert capsys.readouterr().out.splitlines()[-1] == f"grid_v1 · 중지 · {today.isoformat()} · 끈 뒤 회고 있음"


def test_retire_right_after_stop_appends_one_line(tmp_path, monkeypatch, capsys):
    """규칙 표: 바로 앞 사건이 중지(그때 쓴 회고가 있음)면 은퇴 줄만 덧붙임."""
    write_facts(tmp_path, ("grid_v1",))
    _rec(tmp_path, monkeypatch, capsys, "grid_v1", "--state", "중지")
    before = (_folder(tmp_path) / AFTER).read_text()
    _rec(tmp_path, monkeypatch, capsys, "grid_v1", "--state", "은퇴", "--reason", "다시 안 씀")
    after = (_folder(tmp_path) / AFTER).read_text()
    assert after.startswith(before) and after[len(before):] == f"\n{date.today():%m/%d} 은퇴 · 고객 결정 · 이유: 다시 안 씀\n"


def test_retire_after_resume_rewrites_after_md(tmp_path, monkeypatch, capsys):
    """중지 → 실전 → 은퇴는 옛 중지 회고에 줄만 덧붙이지 않고 새로 쓴다 — 다시 켠 사건 · 은퇴가 ②에, 덧붙인 은퇴 줄은 없음."""
    write_facts(tmp_path, ("grid_v1",))
    _rec(tmp_path, monkeypatch, capsys, "grid_v1", "--state", "중지")
    _rec(tmp_path, monkeypatch, capsys, "grid_v1", "--state", "실전")
    before = (_folder(tmp_path) / AFTER).read_text()
    assert "실전 · 고객 결정" not in before
    _rec(tmp_path, monkeypatch, capsys, "grid_v1", "--state", "은퇴", "--reason", "다시 안 씀")
    after = (_folder(tmp_path) / AFTER).read_text()
    two = after.split("## ② ")[1].split("## ③ ")[0]
    assert "실전 · 고객 결정" in two and "은퇴 · 고객 결정" in two, after
    assert not after.startswith(before) and not re.search(r"(?m)^[0-9]{2}/[0-9]{2} 은퇴 · 고객 결정", after)
    assert "다시 안 씀" in after.split("## ③ ")[1]


def test_retire_after_stop_keeps_unreadable_after_md(tmp_path, monkeypatch, capsys):
    """중지 직후 은퇴에서 after.md를 못 읽으면(상한 넘음) 새로 쓰지도 덧붙이지도 않고 그대로 둔다(고객이 덧붙인 글 보존) — 은퇴 기록은 끝남."""
    write_facts(tmp_path, ("grid_v1",))
    _rec(tmp_path, monkeypatch, capsys, "grid_v1", "--state", "중지")
    (_folder(tmp_path) / AFTER).write_bytes(b"x" * ((1 << 20) + 1))
    before = (_folder(tmp_path) / AFTER).read_bytes()
    code, out, err = _rec(tmp_path, monkeypatch, capsys, "grid_v1", "--state", "은퇴", "--reason", "다시 안 씀")
    assert (code, err) == (0, []) and out[0].startswith("grid_v1 · 중지 → 은퇴")
    assert (_folder(tmp_path) / AFTER).read_bytes() == before
    assert "다시 안 씀" in (_folder(tmp_path) / RECORD).read_text() and _log(tmp_path)[-1]["record"] == "은퇴"
    assert current_state(_root(tmp_path), "grid_v1") == "은퇴"


def test_retire_straight_from_live_writes_fresh_after_md(tmp_path, monkeypatch, capsys):
    """실전 → 은퇴인데 옛 회고 파일이 남아 있어도(바로 앞 사건이 중지가 아님) 은퇴 사건까지 담아 새로 쓴다."""
    write_facts(tmp_path, ("grid_v1",))
    (_folder(tmp_path) / AFTER).write_text("옛 회고\n", encoding="utf-8")
    _rec(tmp_path, monkeypatch, capsys, "grid_v1", "--state", "은퇴", "--reason", "그만")
    after = (_folder(tmp_path) / AFTER).read_text()
    assert after.startswith("# 끈 뒤 회고 · grid_v1\n") and "옛 회고" not in after and "은퇴 · 고객 결정" in after


def test_after_md_failure_then_retry_finishes(tmp_path, monkeypatch, capsys):
    """after.md 쓰기가 실패하면 record.md · state.log는 그대로(아무것도 안 바뀜), 같은 명령을 다시 하면 셋 다."""
    write_facts(tmp_path, ("grid_v1",))
    real = rec_mod.write_in

    def fail_after(root, rel, *a, **k):
        if str(rel).endswith(AFTER):
            raise OSError("디스크 가득")
        return real(root, rel, *a, **k)

    monkeypatch.setattr(rec_mod, "write_in", fail_after)
    code, _, err = _rec(tmp_path, monkeypatch, capsys, "grid_v1", "--state", "중지", "--reason", "그만")
    assert code == 2 and err[0].endswith("같은 명령을 다시 하면 이어서 마칩니다")
    assert not (_folder(tmp_path) / RECORD).exists() and not _log(tmp_path)
    monkeypatch.setattr(rec_mod, "write_in", real)
    assert _rec(tmp_path, monkeypatch, capsys, "grid_v1", "--state", "중지", "--reason", "그만")[0] == 0
    assert (_folder(tmp_path) / AFTER).is_file() and current_state(_root(tmp_path), "grid_v1") == "중지"
    assert len(_log(tmp_path)) == 1


def test_retire_append_is_idempotent_on_retry(tmp_path, monkeypatch, capsys):
    """은퇴 줄을 덧붙인 뒤 record.md 쓰기가 실패해도, 같은 명령을 다시 하면 은퇴 줄은 한 번만."""
    write_facts(tmp_path, ("grid_v1",))
    _rec(tmp_path, monkeypatch, capsys, "grid_v1", "--state", "중지")
    real = rec_mod.write_in

    def fail_record(root, rel, *a, **k):
        if str(rel).endswith(RECORD):
            raise OSError("디스크 가득")
        return real(root, rel, *a, **k)

    monkeypatch.setattr(rec_mod, "write_in", fail_record)
    assert _rec(tmp_path, monkeypatch, capsys, "grid_v1", "--state", "은퇴", "--reason", "다시 안 씀")[0] == 2
    monkeypatch.setattr(rec_mod, "write_in", real)
    assert _rec(tmp_path, monkeypatch, capsys, "grid_v1", "--state", "은퇴", "--reason", "다시 안 씀")[0] == 0
    assert (_folder(tmp_path) / AFTER).read_text().count("은퇴 · 고객 결정") == 1
    assert current_state(_root(tmp_path), "grid_v1") == "은퇴"


def test_retire_retry_next_day_keeps_one_line(tmp_path, monkeypatch, capsys):
    """은퇴 줄을 쓴 날 record.md가 실패하고 다음 날 다시 해도 은퇴 줄은 처음 날짜 하나."""
    write_facts(tmp_path, ("grid_v1",))
    _rec(tmp_path, monkeypatch, capsys, "grid_v1", "--state", "중지")
    real = rec_mod.write_in

    def fail_record(root, rel, *a, **k):
        if str(rel).endswith(RECORD):
            raise OSError("디스크 가득")
        return real(root, rel, *a, **k)

    monkeypatch.setattr(rec_mod, "write_in", fail_record)
    assert _rec(tmp_path, monkeypatch, capsys, "grid_v1", "--state", "은퇴", "--reason", "다시 안 씀")[0] == 2
    after = _folder(tmp_path) / AFTER  # 어제 쓴 은퇴 줄처럼 날짜를 바꿔 둠(다음 날 재시도 흉내)
    after.write_text(after.read_text().replace(f"{date.today():%m/%d} 은퇴", "10/16 은퇴"))
    monkeypatch.setattr(rec_mod, "write_in", real)
    assert _rec(tmp_path, monkeypatch, capsys, "grid_v1", "--state", "은퇴", "--reason", "다시 안 씀")[0] == 0
    text = (_folder(tmp_path) / AFTER).read_text()
    assert text.count("은퇴 · 고객 결정") == 1 and "10/16 은퇴" in text


def test_same_memo_on_same_state_never_writes_state_log(tmp_path, monkeypatch, capsys):
    """상태가 그대로인 메모 명령을 두 번 해도 state.log에는 줄이 생기지 않는다(가짜 「실전」 사건 금지)."""
    write_facts(tmp_path, ("grid_v1",))
    for _ in range(2):
        assert _rec(tmp_path, monkeypatch, capsys, "grid_v1", "--state", "실전", "--decision", "실전 시작")[0] == 0
    assert _log(tmp_path) == []


def test_after_md_write_replaces_planted_link_and_fifo(tmp_path, monkeypatch, capsys):
    """after.md는 쓰기만: 심은 링크 · FIFO는 따라가지 않고 그 자리를 바꿈, 링크면 은퇴 때 덧붙이지 않고 새로 씀."""
    write_facts(tmp_path)
    target = tmp_path / "elsewhere.md"
    target.write_text("keep\n")
    (_folder(tmp_path) / AFTER).symlink_to(target)
    os.mkfifo(_folder(tmp_path, "trend_10_50") / AFTER)
    assert _rec(tmp_path, monkeypatch, capsys, "grid_v1", "--state", "중지")[0] == 0
    assert _rec(tmp_path, monkeypatch, capsys, "trend_10_50", "--state", "중지")[0] == 0
    assert target.read_text() == "keep\n" and not (_folder(tmp_path) / AFTER).is_symlink()
    assert (_folder(tmp_path, "trend_10_50") / AFTER).is_file()
    (_folder(tmp_path) / AFTER).unlink()
    (_folder(tmp_path) / AFTER).symlink_to(target)
    assert _rec(tmp_path, monkeypatch, capsys, "grid_v1", "--state", "은퇴")[0] == 0
    assert target.read_text() == "keep\n"


def test_lessons_newest_first_max_three(tmp_path, monkeypatch, capsys):
    write_facts(tmp_path)
    for n in ("grid_v1", "breakout_v2", "trend_ema_v7", "trend_10_50"):
        _rec(tmp_path, monkeypatch, capsys, n, "--state", "중지")
    root = _root(tmp_path)
    for n, d in (("grid_v1", "2026-10-16"), ("breakout_v2", "2026-10-15"), ("trend_ema_v7", "2026-10-14"),
                 ("trend_10_50", "2026-10-17")):
        p = root / "strategies" / n / RECORD
        p.write_text(p.read_text().replace(date.today().isoformat(), d))
    got = lessons(root, exclude="trend_10_50")
    assert got[0] == "지난 전략에서 먼저 볼 것(최근 3개까지):"
    assert [g[2:].split("(")[0] for g in got[1:]] == ["grid_v1", "breakout_v2", "trend_ema_v7"]
    assert got[1] == f"- grid_v1(10/16 중지): {GRID_LESSON}"
    (root / "strategies" / "breakout_v2" / RECORD).write_text("깨짐\n")
    assert len(lessons(root, exclude="x")) == 4
    assert lessons(root, exclude="x")[1].startswith("- trend_10_50(10/17 중지): ")


def test_next_check_prints_the_lesson_line(tmp_path, monkeypatch, capsys):
    """④: 다른 전략을 record --state 중지로 끄면, 그 뒤 새 전략 check 출력 끝에 ④ 줄(check.md에는 없음)."""
    from conftest import write_link
    from test_check_report import _body
    write_facts(tmp_path, ("grid_v1",))
    assert _rec(tmp_path, monkeypatch, capsys, "grid_v1", "--state", "중지")[0] == 0
    write_link(tmp_path, _body(), name="s1")
    monkeypatch.chdir(tmp_path)
    assert main(["check", "s1", "--no-binance"]) == 0
    out = capsys.readouterr().out.splitlines()
    want = ["지난 전략에서 먼저 볼 것(최근 3개까지):", f"- grid_v1({date.today():%m/%d} 중지): {GRID_LESSON}"]
    i = out.index(want[0])
    assert out[i:i + 2] == want and out[-1].startswith("저장: ") and not out[0].startswith("지난 전략")
    assert want[1] not in (_root(tmp_path) / "strategies" / "s1" / "check.md").read_text()


def test_malformed_guardrail_checks_does_not_stop_the_stop(tmp_path, monkeypatch, capsys):
    """가드레일 checks 칸이 목록이어도(공용 검사는 안 봄) 끄기는 끝나고 기간 줄은 재현성만 뺀다."""
    root = write_facts(tmp_path, ("grid_v1",))
    gp = root / "strategies" / "grid_v1" / "guardrail.v1.yaml"
    g = json.loads(gp.read_text())
    g["checks"] = ["C0"]
    gp.write_text(json.dumps(g, ensure_ascii=False))
    code, _, err = _rec(tmp_path, monkeypatch, capsys, "grid_v1", "--state", "중지")
    assert (code, err) == (0, [])
    assert (_folder(tmp_path) / RECORD).is_file()
    page = (_folder(tmp_path) / AFTER).read_text()
    assert "거래소 조회 키로 매일 받은 체결 내역으로 계산\n" in page and "(C0)" not in page


@pytest.mark.parametrize("how", ["fifo", "garbage"])
def test_unreadable_days_is_not_written_as_no_records(tmp_path, monkeypatch, capsys, how):
    """일별 기록을 읽지 못함은 「기록 없음」과 다른 글로, 미관측 기간도 적지 않는다(record 출력 · record.md · after.md ④ · 다음 check 끝 줄)."""
    root = write_facts(tmp_path, ("grid_v1",))
    p = root / "strategies" / "grid_v1" / "daily" / "days.jsonl"
    if how == "fifo":
        os.mkfifo(p.parent / "x")
        p.unlink()
        p.symlink_to(p.parent / "x")
    else:
        p.write_text("{깨짐\n[1]\n")
    code, out, _ = _rec(tmp_path, monkeypatch, capsys, "grid_v1", "--state", "중지")
    want = "근거 없음(매일 대조 기록을 읽지 못함)"
    assert code == 0 and out[3] == f"다음 점검에서 먼저 볼 것: {want}(실전 9/23~ · 매일 대조 기록을 읽지 못함)"
    assert f"- 다음 점검에서 먼저 볼 것: {want}\n" in (_folder(tmp_path) / RECORD).read_text()
    page = (_folder(tmp_path) / AFTER).read_text()
    four = page.split("## ④ 다음 점검에서 먼저 볼 것\n")[1]
    assert four.startswith(want + "\n") and "미관측" not in four and "매일 대조 기록 없음" not in four
    assert lessons(root, exclude="x")[1] == f"- grid_v1({date.today():%m/%d} 중지): {want}"


def test_same_request_recreates_missing_after_md(tmp_path, monkeypatch, capsys):
    """복구: 같은 요청에서 after.md가 사라졌으면 다시 만들고 「이어서 마침」에 after.md를 적는다."""
    write_facts(tmp_path, ("grid_v1",))
    _rec(tmp_path, monkeypatch, capsys, "grid_v1", "--state", "중지")
    (_folder(tmp_path) / AFTER).unlink()
    code, out, _ = _rec(tmp_path, monkeypatch, capsys, "grid_v1", "--state", "중지")
    assert code == 0 and out == ["이어서 마침 · grid_v1 · 중지 · after.md"]
    assert (_folder(tmp_path) / AFTER).read_text().startswith("# 끈 뒤 회고 · grid_v1\n") and len(_log(tmp_path)) == 1


def test_retire_line_check_needs_the_exact_appended_shape(tmp_path, monkeypatch, capsys):
    """③ 이유 글이 우연히 「MM/DD 은퇴 · 고객 결정」으로 시작해도 은퇴 줄은 덧붙는다(오탐 없음)."""
    write_facts(tmp_path, ("grid_v1",))
    _rec(tmp_path, monkeypatch, capsys, "grid_v1", "--state", "중지", "--reason", "10/16 은퇴 · 고객 결정 흉내")
    _rec(tmp_path, monkeypatch, capsys, "grid_v1", "--state", "은퇴", "--reason", "정말 끝")
    text = (_folder(tmp_path) / AFTER).read_text()
    assert text.endswith(f"\n{date.today():%m/%d} 은퇴 · 고객 결정 · 이유: 정말 끝\n")


def test_failed_last_day_does_not_hide_trades_and_lesson(tmp_path, monkeypatch, capsys):
    """마지막 날 대조가 실패해(nums={} · trades 0) 끄는 흔한 흐름에서도 ④ · 거래 수 · 근거 괄호는 ①과 같은 줄
    (nums.all이 있는 마지막 기록)을 읽는다. 마지막 기록 날짜(괄호 끝 · 미관측 시작)는 실패한 마지막 줄 그대로."""
    root = write_facts(tmp_path, ("grid_v1",))
    p = root / "strategies" / "grid_v1" / "daily" / "days.jsonl"
    bad = {**json.loads(p.read_text().splitlines()[-1]), "day": "2026-10-15", "trades": 0, "nums": {}}
    p.write_text(p.read_text() + json.dumps(bad, ensure_ascii=False) + "\n")
    code, out, _ = _rec(tmp_path, monkeypatch, capsys, "grid_v1", "--state", "중지")
    today = date.today()
    assert code == 0 and out[3] == (f"다음 점검에서 먼저 볼 것: {GRID_LESSON}(실전 9/23~10/15 · 거래 12건 · 미관측 10/16~{today.month}/{today.day})"), out
    head = (_folder(tmp_path) / AFTER).read_text().splitlines()[2]
    assert " · 거래 12건 · " in head, head
    assert f"- 다음 점검에서 먼저 볼 것: {GRID_LESSON}" in (_folder(tmp_path) / RECORD).read_text()


def test_no_line_with_all_numbers_keeps_the_no_basis_text(tmp_path):
    """nums.all가 있는 줄이 하나도 없으면 지금 글(마지막 줄 기준) 그대로 — 근거 없음."""
    r = {**record("grid_v1", 22), "nums": {}}
    assert lesson([r]) == "근거 없음(마지막 매일 대조에 숫자 없음)"


def test_after_page_never_writes_bt_abbreviation(tmp_path):
    """after.md ①의 전략 줄과 ④(경고 날 있는 기록)가 「BT」 약어를 쓰지 않는다 — 「같은 기간 백테스트」.
    한글이 이어 붙은 「BT가」도 잡도록 영문자 경계로 본다."""
    root = write_facts(tmp_path, ("trend_ema_v7",))
    p = root / "strategies" / "trend_ema_v7" / "daily" / "days.jsonl"
    recs = [json.loads(x) for x in p.read_text().splitlines()]
    for r in recs[-3:]:
        r["states"]["전략"] = "경고"
    p.write_text("".join(json.dumps(r, ensure_ascii=False) + "\n" for r in recs), encoding="utf-8")
    page, _ = after_page(root, "trend_ema_v7", _stop_doc(), date(2026, 10, 16))
    one = next(x for x in page.splitlines() if x.startswith("- 전략: "))
    assert "같은 기간 백테스트" in one and "같은 기간 백테스트가 정상 범위 아래였던 날 3일" in page, page
    assert not re.search(r"(?<![A-Za-z])BT(?![A-Za-z])", page), one


def test_after_start_is_engine_start_not_guardrail_date(tmp_path):
    """회고 시작일은 기록 가운데 nums.all.start가 있는 가장 최근 줄(지금 실전의 시작). 가드레일 날짜(9/20)가 아님."""
    root = write_facts(tmp_path, ("trend_10_50",))
    d = root / "strategies" / "trend_10_50"
    gp = d / "guardrail.v1.yaml"
    g = json.loads(gp.read_text())
    g["live_start"] = "2026-09-20"
    gp.write_text(json.dumps(g, ensure_ascii=False))
    p = d / "daily" / "days.jsonl"
    rows = [json.loads(x) for x in p.read_text().splitlines()]
    rows[0]["nums"] = {}  # 첫 줄은 대조 실패 줄
    rows[1]["nums"]["all"]["start"] = "2026-09-21"  # 앞 줄은 더 이른 날(옛 실전의 시작) · 가장 최근 줄(9/23)이 이겨야 함
    p.write_text("".join(json.dumps(r, ensure_ascii=False) + "\n" for r in rows))
    page, _ = after_page(root, "trend_10_50", _stop_doc(), date(2026, 10, 16))
    assert "실전 2026-09-23 ~ 10-16(중지)" in page and "- 09/23 실전 시작 · 가드레일 v1" in page
    assert "기록 없는 날: 없음" in page and "09/20" not in page


# --- 끈 뒤 회고를 값으로 가름 · 웹용 기준 사건 · after.md 쓰기 세 길은 바이트 그대로 ---

RETIRED_PAGE = GRID_PAGE.replace("10-16(중지)", "10-18(은퇴)").replace("- 10/16 중지 · 고객 결정\n", "- 10/16 중지 · 고객 결정\n- 10/18 은퇴 · 고객 결정\n").replace(
    "이유 미기록", "X").replace("미관측 10/15~10/16", "미관측 10/15~10/18")
"""고치기 전 코드가 만든 grid_v1 「중지(10/16, 이유 없음) → after.md 지움 → 은퇴(10/18, 이유 X)」 새 회고(스냅샷으로 확인한 글)."""


def _events(*states):
    """날 오름차순 사건 목록: (상태, 이유) 짝마다 하루씩."""
    return [{"day": f"2026-10-{16 + i:02d}", "state": st, "reason": why, "decision": None} for i, (st, why) in enumerate(states)]


def test_after_values_render_golden(tmp_path):
    """값(JSON으로 오가도 같음)으로 짠 글 = 지금 after_page, 값 이름은 웹 회고 칸이 읽는 것과 같다."""
    root = write_facts(tmp_path, ("grid_v1",))
    v = after_values(root, "grid_v1", _stop_doc(), date(2026, 10, 16))
    assert set(v) == {"name", "top", "one", "two", "reason", "four", "notes"}
    assert v["top"] == {"state": "중지", "day": "2026-10-16", "start": "2026-09-23", "trades": 12, "c0": "재현 확인 전"}
    one = v["one"]
    assert set(one) == {"status", "why", "actual", "bt", "gap", "segments", "strategy", "exec", "bot"}
    assert (one["status"], one["why"], one["segments"]) == ("ok", None, []) and one["gap"] == pytest.approx(-0.003)
    assert one["strategy"] == {"state": "판단 보류", "hold": True, "trades": 12, "min_trades": 30,
                               "line": one["strategy"]["line"]}
    assert one["exec"]["kind"] == "ok" and one["exec"]["missed_n"] == 0 and one["exec"]["first_missed"] is None
    assert one["exec"]["market"] == "spot" and one["exec"]["slip_med_bp"] == -5.3
    assert one["bot"] == {"state": "정상", "lost": []}
    two = v["two"]
    assert set(two) == {"start", "guard_why", "log_why", "timeline", "gaps"}
    assert two["start"] == {"day": "2026-09-23", "version": 1, "sensitivity": "민감"} and two["gaps"] == {"status": "ok", "runs": []}
    assert two["timeline"] == [{"day": "2026-09-29", "kind": "change", "field": "봇", "before": "판단 보류", "after": "정상"},
                               {"day": "2026-10-16", "kind": "decision", "state": "중지", "decision": None}]
    assert v["reason"] is None
    assert v["four"] == {"lesson": GRID_LESSON, "basis": {"first": "2026-09-23", "last": "2026-10-14"},
                         "unseen": {"first": "2026-10-15", "last": "2026-10-16", "after_last": True}, "bot_check": False}
    assert v["notes"] == []
    assert rec_mod._after_text("grid_v1", json.loads(json.dumps(v))) == GRID_PAGE == after_page(root, "grid_v1", _stop_doc(), date(2026, 10, 16))[0]


def test_after_values_names_what_could_not_be_read(tmp_path):
    """읽지 못한 것은 값에 사유로 남고(notes에도), 숫자 칸은 비며, 글은 지금과 같다."""
    root = write_facts(tmp_path, ("grid_v1",))
    days = root / "strategies" / "grid_v1" / "daily" / "days.jsonl"
    days.unlink()
    days.symlink_to(root / "strategies" / "grid_v1" / "guardrail.v1.yaml")
    (root / "strategies" / "grid_v1" / "guardrail.v1.yaml").rename(root / "g.bak")
    v = after_values(root, "grid_v1", _stop_doc("은퇴", "키를 잃음"), date(2026, 10, 16))
    assert v["one"]["status"] == "unread" and v["one"]["why"] and v["one"]["strategy"] is None and v["one"]["actual"] is None
    assert v["two"]["start"] is None and v["two"]["guard_why"] and v["two"]["gaps"] == {"status": "unread", "runs": []}
    assert v["reason"] == "키를 잃음" and v["four"]["basis"] is None and v["four"]["unseen"] is None
    assert len(v["notes"]) == 2 and rec_mod._after_text("grid_v1", v) == after_page(root, "grid_v1", _stop_doc("은퇴", "키를 잃음"), date(2026, 10, 16))[0]


@pytest.mark.parametrize("events, base_i, retire_i", [
    ([("중지", None)], 0, None),
    ([("은퇴", "끝")], 0, None),
    ([("실전", None), ("중지", "이유")], 1, None),
    ([("중지", "이유 하나"), ("은퇴", "이유 둘")], 0, 1),
    ([("중지", "a"), ("실전", None), ("은퇴", "b")], 2, None),
    ([("실전", None), ("은퇴", "b")], 1, None),
    ([("실전", None), ("중지", "a"), ("은퇴", "b")], 1, 2),
    ([("중지", "a"), ("중지", "c")], 1, None),
])
def test_after_event_is_function_of_events(tmp_path, events, base_i, retire_i):
    """기준 사건은 사건 목록만으로 정한다 — 마지막이 은퇴이고 바로 앞이 중지면 (중지, 은퇴), 아니면 (마지막, None). after.md 유무와 무관."""
    doc = {"events": _events(*events), "lesson": None}
    expected = (doc["events"][base_i], None if retire_i is None else doc["events"][retire_i])
    assert after_event(doc) == expected
    root = write_facts(tmp_path, ("grid_v1",))
    after = root / "strategies" / "grid_v1" / AFTER
    assert after_event(doc) == expected  # after.md 없음
    after.write_text("옛 회고\n", encoding="utf-8")
    assert after_event(doc) == expected  # 일반 파일로 있음
    after.unlink()
    after.symlink_to(root / "state.log")
    assert after_event(doc) == expected  # 링크
    assert doc["events"] == _events(*events)  # 입력을 바꾸지 않음


def test_write_after_appends_one_line_when_the_stop_page_exists_byte_for_byte(tmp_path):
    """_write_after 길 ① 파일 있음: 중지 회고(= GRID_PAGE) 뒤에 은퇴 줄만 덧붙인다. 같은 요청을 되풀이해도 그대로(멱등)."""
    root = write_facts(tmp_path, ("grid_v1",))
    page = root / "strategies" / "grid_v1" / AFTER
    write_record(root, "grid_v1", "중지", None, None, date(2026, 10, 16))
    assert page.read_text(encoding="utf-8") == GRID_PAGE
    write_record(root, "grid_v1", "은퇴", "X", None, date(2026, 10, 18))
    assert page.read_bytes() == (GRID_PAGE + "\n10/18 은퇴 · 고객 결정 · 이유: X\n").encode()
    write_record(root, "grid_v1", "은퇴", "X", None, date(2026, 10, 18))
    assert page.read_bytes().count("은퇴 · 고객 결정 · 이유".encode()) == 1


def test_write_after_writes_a_fresh_retire_page_when_the_stop_page_is_gone_byte_for_byte(tmp_path):
    """_write_after 길 ② 파일 없음: after.md 유무로 가르는 건 엔진 그대로라, 지운 뒤 은퇴하면 은퇴 기준 새 회고가 생긴다."""
    root = write_facts(tmp_path, ("grid_v1",))
    page = root / "strategies" / "grid_v1" / AFTER
    write_record(root, "grid_v1", "중지", None, None, date(2026, 10, 16))
    page.unlink()
    write_record(root, "grid_v1", "은퇴", "X", None, date(2026, 10, 18))
    assert page.read_text(encoding="utf-8") == RETIRED_PAGE


def test_write_after_leaves_an_unreadable_stop_page_alone_byte_for_byte(tmp_path):
    """_write_after 길 ③ 읽기 실패(상한 1MiB 넘음): 새로 쓰지도 덧붙이지도 않고 고객이 덧붙인 글 그대로."""
    root = write_facts(tmp_path, ("grid_v1",))
    page = root / "strategies" / "grid_v1" / AFTER
    write_record(root, "grid_v1", "중지", None, None, date(2026, 10, 16))
    page.write_bytes(page.read_bytes() + b"x" * (1 << 20))
    before = page.read_bytes()
    write_record(root, "grid_v1", "은퇴", "X", None, date(2026, 10, 18))
    assert page.read_bytes() == before
    assert current_state(root, "grid_v1") == "은퇴"


def test_after_values_raises_overflow_for_a_missed_time_that_cannot_convert_to_utc(tmp_path):
    """docstring의 Raises: 모양 검사를 지나지만 UTC로 바꿀 수 없는 놓친 주문 시각이 ①의 처음 후보를 고를 때 OverflowError로 올라온다(after_page도 같음)."""
    root = write_facts(tmp_path, ("trend_10_50",))
    p = root / "strategies" / "trend_10_50" / "daily" / "days.jsonl"
    recs = [json.loads(x) for x in p.read_text(encoding="utf-8").splitlines()]
    recs[-1]["nums"]["all"]["missed"] = [{"t": "0001-01-01T00:00:00+01:00", "symbol": "BTCUSDT", "side": "sell"}]
    p.write_text("".join(json.dumps(r, ensure_ascii=False) + "\n" for r in recs), encoding="utf-8")
    for fn in (after_values, after_page):
        with pytest.raises(OverflowError):
            fn(root, "trend_10_50", _stop_doc(), date(2026, 10, 16))

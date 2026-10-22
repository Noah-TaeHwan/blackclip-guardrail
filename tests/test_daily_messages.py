"""보낼 글 정하기: 상태 바뀜 · 매일 한 줄 · 바뀔 때만 · 매주 · 실행 문 · 못 보낸 것 다시."""
from datetime import date

from blackclip.daily.messages import broken_message, broken_note, load_notified, plan_messages, save_notified
from blackclip.daily.reconcile import Day
from blackclip.record import BROKEN

S = {"전략": "판단 보류", "실행": "경고", "봇": "정상", "선": "정상", "논지": "해당 없음"}


def day(states=S, week=None):
    return Day("s1", date(2026, 10, 18), 25, 40, "실제 +1.0% · 같은 기간 BT +0.5% · 정상 범위 그리지 않음",
               dict(states), {k: "근거" for k in S}, {"execution_warning": "", "warning_line": ""}, week)


DAILY = {"quiet": "매일", "execution_alerts": False}
CHANGES = {"quiet": "바뀔때만", "execution_alerts": False}


def test_first_run_sends_change_and_bundle():
    """보낸 적 없으면 상태 바뀜 한 통 + 매일 묶음."""
    msgs, stale = plan_messages([day()], {}, DAILY, [])
    assert [sent for _, sent in msgs] == [{"s1": S}, {}] and stale == []
    assert msgs[1][0].startswith("매일 묶음 · 2026-10-18 UTC 마감 · 전략 1개")


def test_same_states_no_change_message():
    """상태가 그대로면 상태 바뀜 없음 · 매일 한 줄이면 묶음만, 바뀔 때만이면 아무것도."""
    assert [n for _, n in plan_messages([day()], {"s1": S}, DAILY, [])[0]] == [{}]
    assert plan_messages([day()], {"s1": S}, CHANGES, []) == ([], [])


def test_execution_change_ignored_until_enabled():
    """실행 경고를 안 켰으면 실행 칸만 바뀐 것은 알리지 않음, 켜면 알림."""
    prev = {**S, "실행": "정상"}
    assert plan_messages([day()], {"s1": prev}, CHANGES, []) == ([], [])
    on = {"quiet": "바뀔때만", "execution_alerts": True}
    assert [n for _, n in plan_messages([day()], {"s1": prev}, on, [])[0]] == [{"s1": S}]


def test_weekly_line_goes_even_when_quiet():
    """바뀔 때만이어도 매주 회고 한 줄은 간다."""
    msgs = plan_messages([day(week="s1 이번 주(7일): 실제 +1.0%")], {"s1": S}, CHANGES, [])
    assert msgs == ([("매주 회고\ns1 이번 주(7일): 실제 +1.0%", {})], [])


def test_unsent_change_is_resent_next_day(tmp_path):
    """보낸 상태는 보낸 뒤에만 적으므로, 못 보낸 바뀜은 다음 실행에서 다시 나온다 · 깨진 파일은 빈 것으로."""
    assert load_notified(tmp_path) == {}
    msgs, _ = plan_messages([day()], load_notified(tmp_path), CHANGES, [])
    assert [n for _, n in msgs] == [{"s1": S}]  # 오늘 보내기 실패 → save_notified 안 함
    assert [n for _, n in plan_messages([day()], load_notified(tmp_path), CHANGES, [])[0]] == [{"s1": S}]
    save_notified(tmp_path, {"s1": S})
    assert plan_messages([day()], load_notified(tmp_path), CHANGES, []) == ([], [])
    (tmp_path / "notified.json").write_text("{깨짐", encoding="utf-8")
    assert load_notified(tmp_path) == {}


def test_broken_texts_follow_r11_9():
    """손상 글 세 줄과 묶음 끝 한 줄(문구 글자 그대로)."""
    assert broken_message("s2").splitlines() == [
        "기록 손상: s2의 매일 대조가 멈췄어요", "전략 기록(record.md)을 읽지 못했어요",
        "git 충돌이면 풀고, 아니면 record.md를 지운 뒤 blackclip record로 다시 적어 주세요"]
    assert broken_note("s2") == "기록 손상 s2: 매일 대조가 멈춰 있어요"


def test_broken_goes_once_even_when_quiet_and_without_days():
    """손상 글은 notified에 그 키가 없을 때만, 바뀔때만에서도, 대조가 돈 전략이 없어도 간다. days가 비면 묶음 · 매주 회고는 없다."""
    sent = {"record:s2": {"기록": BROKEN}}
    msgs, stale = plan_messages([], {}, DAILY, [], broken=("s2",))
    assert msgs == [(broken_message("s2"), sent)] and stale == []
    assert plan_messages([], sent, DAILY, [], broken=("s2",)) == ([], [])
    msgs, _ = plan_messages([day()], {"s1": S}, CHANGES, [], broken=("s2",))
    assert msgs == [(broken_message("s2"), sent)]


def test_bundle_ends_with_broken_note_after_notes():
    """묶음이 가는 날은 broken마다 끝에 한 줄(notes 뒤), 이미 알렸어도 붙는다. 복구한 키는 지울 키로 돌아온다."""
    msgs, stale = plan_messages([day()], {"s1": S, "record:s2": {"기록": BROKEN}, "record:s3": {"기록": BROKEN}},
                                DAILY, ["받기 실패"], broken=("s2",))
    assert [n for _, n in msgs] == [{}] and stale == ["record:s3"]
    assert msgs[0][0].splitlines()[-2:] == ["받기 실패", broken_note("s2")]

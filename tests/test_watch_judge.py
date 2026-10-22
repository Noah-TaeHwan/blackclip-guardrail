"""자주 보는 시계 판정 글: 선 · 봇 · 터미널 한 줄 · 텔레그램 세 줄 · 섞인 계좌 한 번."""
from datetime import datetime, timedelta, timezone

from blackclip.daily.reconcile import bot_cause
from blackclip.watch.judge import BROKEN, MIXED_STATES, bot_state, line_state, message, plan_watch, terminal_line
from blackclip.watch.window import CAPTURED, NOT_DIVIDED

UTC = timezone.utc
NOTE = "1시간 확인 기준 · 보정은 독립 정규 가정(미검증)"


def win(pnl="0", flows="0", low=0.0, base="10000", growth="1", cash="10000"):
    mark = {"at": "2025-01-23T01:00:00+00:00", "cash": cash, "hold": {}, "price": {}}
    return {"start": "2025-01-19T00:00:00+00:00", "base": base, "pnl": pnl, "flows": flows, "growth": growth, "low": low, "mark": mark}


def test_line_states():
    """처음은 「구간 기준 잡음」, 최저가 선 이하면 경고, 흐름이 보이면 입출금 추정, 틈 · 깨짐 · 지금 자산 0은 확인 불가."""
    assert line_state(win(), CAPTURED, False, -0.239, "보통") == (
        "정상", f"구간 기준 잡음 · 구간 수익 +0.0%(이번 구간 최저 +0.0%) · 경고선 -23.9%(민감도 보통 · {NOTE})")
    st, ln = line_state(win("-2800", "-1000", -0.28, growth="0.72"), None, False, -0.239, "보통")
    assert st == "경고" and ln.startswith("구간 수익 -28.0%(이번 구간 최저 -28.0%)") and ln.endswith("입출금 추정 · 손익에서 뺌(기준 대비 -10.0%)")
    assert line_state(win("5", "5"), None, False, -0.239, "보통")[1].endswith(f" · {NOTE})")  # 0.05%는 안 보임
    assert line_state(win(), NOT_DIVIDED, False, -0.239, "보통") == ("확인 불가", NOT_DIVIDED)
    assert line_state(win(), CAPTURED, True, -0.239, "보통") == ("확인 불가", BROKEN)
    assert line_state(win(cash="0"), CAPTURED, False, -0.239, "보통") == (
        "확인 불가", "지금 자산이 0 이하라 구간 수익을 셀 수 없음(입금되면 다음 확인부터)")


def test_bot_state_combines_flow_frequency_and_position():
    """받은 범위 글은 시각까지, 빈도 정상 + 포지션 다름 = 경고, 체결을 못 받았으면 포지션은 안 붙임."""
    now = datetime(2025, 1, 23, 7, 0, tzinfo=UTC)
    got = "체결 01/23 07:00 UTC까지 받음 · 받은 체결 없음"
    cause = bot_cause(now - timedelta(days=40), now, [], now, now - timedelta(days=40), [], {"low": 0, "high": 3}, 34, got)
    assert cause == ("정상", "체결 01/23 07:00 UTC까지 받음 · 받은 체결 없음 · 최근 7일 0건(백테스트 기준 0~3건, 청산 시각)")
    assert bot_state(cause, ("경고", "포지션 다름: BTCUSDT BT 보유 · 실제 없음")) == (
        "경고", cause[1] + " · 포지션 다름: BTCUSDT BT 보유 · 실제 없음")
    stale = bot_cause(now - timedelta(days=40), now - timedelta(hours=3), [], now, now - timedelta(days=40), [], {"low": 0, "high": 3}, 34, got)
    assert bot_state(stale, ("정상", "포지션 맞음: BTCUSDT 없음")) == stale and stale[0] == "확인 불가"
    assert "UTC까지만 받음 · 3시간째 못 받음 · " in stale[1]  # 1시간 확인은 하루 미만 공백을 시간으로(매일 대조는 「N일째」 그대로)
    assert "· 2일째 못 받음 · " in bot_cause(now - timedelta(days=40), now - timedelta(hours=30), [], now, now - timedelta(days=40),
                                             [], {"low": 0, "high": 3}, 34, got)[1]


def test_terminal_line_and_three_line_message():
    """터미널 한 줄, 텔레그램은 세 줄(결론 · 선 · 봇), 선 경고면 대응을 붙임."""
    st = {"선": "경고", "봇": "정상"}
    ln = {"선": "구간 수익 -25.0%(이번 구간 최저 -25.0%) · 경고선 -23.9%", "봇": "체결 01/23 07:00 UTC까지 받음"}
    assert terminal_line("s1", st, ln) == ("s1: 경고 · 가드레일 선 경고 · 구간 수익 -25.0%(이번 구간 최저 -25.0%) · 경고선 -23.9% · "
                                          "봇 정상 · 체결 01/23 07:00 UTC까지 받음")
    text = message("s1", st, ln, {"warning_line": "중단 검토"})
    assert text.splitlines() == ["경고 · 가드레일 선 · s1 · 자주 보는 시계",
                                 "가드레일 선: 경고 · 구간 수익 -25.0%(이번 구간 최저 -25.0%) · 경고선 -23.9% · 가드레일 대응(내가 정해 둔 것): 중단 검토",
                                 "봇: 정상 · 체결 01/23 07:00 UTC까지 받음"]


def test_message_stays_three_lines_when_response_has_newlines():
    """warning_line이 여러 줄(YAML 블록)이어도 텔레그램은 세 줄, 줄바꿈은 공백 하나로."""
    st = {"선": "경고", "봇": "정상"}
    ln = {"선": "근거", "봇": "근거"}
    text = message("s1", st, ln, {"warning_line": "첫째\n  둘째\n"})
    assert len(text.splitlines()) == 3 and text.splitlines()[1].endswith("가드레일 대응(내가 정해 둔 것): 첫째 둘째")
    assert message("s1", st, ln, {"warning_line": " \n "}).splitlines()[1].endswith(": 적어 둔 것 없음")


def test_plan_watch_sends_changes_only_and_mixed_once():
    """처음은 보냄, 같은 상태면 안 보냄, 섞인 계좌는 처음 한 번 한 통(매일 대조 키 s1은 건드리지 않음)."""
    res = [("a", {"선": "정상", "봇": "정상"}, {"선": "x", "봇": "y"}, {})]
    mixed = {("main", "spot"): ["b", "c"]}
    first = plan_watch(res, mixed, {"s1": {"전략": "정상"}})
    assert [list(k) for _, k in first] == [["watch:a"], ["watch:b", "watch:c"]]
    assert first[1][0].splitlines()[1] == "다른 전략과 계좌를 같이 써요: binance main spot(b, c)"
    sent = {"s1": {"전략": "정상"}, "watch:a": {"선": "정상", "봇": "정상"}, "watch:b": MIXED_STATES, "watch:c": MIXED_STATES}
    assert plan_watch(res, mixed, sent) == []

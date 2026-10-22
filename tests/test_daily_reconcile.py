"""매일 대조 판정: 원인 칸마다 상태 · 근거, 대표 상태, 파일 · 메시지 글."""
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal as D

import pandas as pd

from blackclip.daily.actual import Actual
from blackclip.daily.live import LiveRun
from blackclip.daily.reconcile import (Day, bot_cause, bundle, capital_basis, change_message, execution_cause,
                                       execution_numbers, line_cause, period, reconcile, render_md, representative,
                                       strategy_cause, thesis_cause, triggered)
from blackclip.retro.fills import Execution
from blackclip.retro.match import MatchResult, Pair
from blackclip.retro.report import build_retro

UTC = timezone.utc
LIVE = datetime(2026, 9, 23, tzinfo=UTC)
END = datetime(2026, 10, 15, tzinfo=UTC)  # 실전 22일째, 막 끝난 날 10/14(수)


def guard(drawn=True, edge0=0.79, rule="논지 없음", market="spot"):
    """가드레일 파일 내용(계약 키만, 값은 지어냄)."""
    nr = ({"days": 30, "drawn": True, "coverage": 0.9, "low": -0.037, "high": 0.081,
           "daily_low": [-0.001 * (i + 1) for i in range(30)], "daily_high": [0.003 * (i + 1) for i in range(30)]}
          if drawn else {"days": 30, "drawn": False, "reason": "백테스트 왕복 거래 7건 · 기간 399일(하한 100건 · 365일)"})
    return {"live_start": "2026-09-23", "normal_range": nr,
            "judgment_hold": {"min_trades": 30, "hold_if_edge0_at_least": 0.5,
                              "edge0_in_range": [edge0] * 30 if drawn else None},
            "trade_frequency": {"days": 7, "low": 7, "high": 17},
            "warning_line": {"line": -0.072, "sensitivity": "민감"},
            "thesis": {"rule": rule}, "target": {"market": market, "symbols": ["BTCUSDT"]},
            "response": {"execution_warning": "주문 코드 확인", "warning_line": ""}}


def test_period_cuts_every_30_days():
    """1~30일은 첫 구간, 31일째는 새 구간 1일째."""
    assert period(LIVE, LIVE + timedelta(days=1)) == (LIVE, 1)
    assert period(LIVE, LIVE + timedelta(days=30)) == (LIVE, 30)
    assert period(LIVE, LIVE + timedelta(days=31)) == (LIVE + timedelta(days=30), 1)


def test_strategy_hold_warn_and_normal():
    """30건 전 · 엣지 0 확률 ≥ 50%면 판단 보류, 범위 아래는 보류여도 경고, 다 아니면 정상."""
    g = guard()
    assert strategy_cause(g, -0.019, 22, 37) == (
        "판단 보류", "같은 기간 BT -1.9%(정상 범위 -2.2% ~ +6.6%(22일째 · 보수적 비용 기준) 안) · 엣지가 없어도 범위 안일 확률 79%")
    assert strategy_cause(g, -0.03, 22, 5)[0] == "경고"
    assert strategy_cause(g, -0.03, 22, 5)[1].endswith("아래")
    assert strategy_cause(guard(edge0=0.4), 0.09, 22, 40) == (
        "정상", "같은 기간 BT +9.0%(정상 범위 -2.2% ~ +6.6%(22일째 · 보수적 비용 기준) 위) · 실전 거래 40건 · 엣지가 없어도 범위 안일 확률 40%")
    st, ln = strategy_cause(guard(drawn=False), 0.01, 22, 3)
    assert st == "판단 보류" and ln == "같은 기간 BT +1.0%(정상 범위 그리지 않음) · 실전 거래 3건(30건 전) · 엣지 0 확률 모름"


def ex(t, side, price, qty="0.1", fee="0", asset="USDT", oid="1"):
    return Execution(t, "BTCUSDT", side, D(qty), D(price), D(fee), asset, oid)


def test_execution_measured_parts_and_rest():
    """잰 것(수수료 · 체결가 차이) %p와 나머지(추정 · 원인 후보)를 나누고, 백테스트에만 있는 거래가 있으면 경고."""
    t1, t2 = datetime(2026, 10, 1, tzinfo=UTC), datetime(2026, 10, 13, 15, 58, tzinfo=UTC)
    bt = [ex(t1, "buy", "60000", "1"), ex(t2, "sell", "61000", "1", oid="2")]
    act = [ex(t1 + timedelta(minutes=5), "buy", "60030", fee="6", oid="9")]
    r = build_retro(bt, act, None, D(10000), END, LIVE, 30, timedelta(days=1), "USDT", fills_until=END)
    st, ln = execution_cause(r, D(10000), -0.027, False, True, D(0))
    assert st == "경고"
    assert ln == ("실제 − BT(비용 0) -2.7%p = 수수료 -0.1%p + 체결가 차이 +0.0%p(중앙값 -5.0bp · 최대 -5.0bp · 20bp 넘음 0건) + "
                  "나머지 -2.6%p(추정 · 원인 후보: 백테스트에만 있는 거래 1건(놓친 주문으로 추정 · 처음 10/13 15:58 UTC BTCUSDT 매도))")
    st2, ln2 = execution_cause(r, D(10000), -0.027, True, False, D(0))
    assert "펀딩비 확인 불가(받은 범위가 구간을 못 덮음)" in ln2


def test_bot_stale_coverage_is_unconfirmed():
    """받은 끝이 오늘 0시보다 3.5일 앞이면 확인 불가 · 4일째, 기록 없는 종목도 확인 불가."""
    st, ln = bot_cause(LIVE, END - timedelta(days=3, hours=12), [], END, LIVE, [], {"low": 7, "high": 17}, 22)
    assert st == "확인 불가" and "10/11 12:00 UTC까지만 받음 · 4일째 못 받음" in ln
    assert bot_cause(None, None, ["BTCUSDT"], END, LIVE, [], {"low": 7, "high": 17}, 22)[0] == "확인 불가"


def test_bot_too_few_trades_warns():
    """최근 7일 거래가 기준보다 적으면 봇 멈춤 의심, 많으면 폭주 의심, 7일 전은 판단 보류."""
    exits = [END - timedelta(days=1, hours=h) for h in range(3)]
    assert bot_cause(LIVE, END, [], END, LIVE, exits, {"low": 7, "high": 17}, 22) == (
        "경고", "체결 10/14분까지 받음 · 최근 7일 3건(백테스트 기준 7~17건, 청산 시각) · 적음(봇 멈춤 의심)")
    many = [END - timedelta(hours=h + 1) for h in range(20)]
    assert bot_cause(LIVE, END, [], END, LIVE, many, {"low": 7, "high": 17}, 22)[1].endswith("많음(폭주 의심)")
    eleven = [END - timedelta(hours=h + 1) for h in range(11)]
    assert bot_cause(LIVE, END, [], END, LIVE, eleven, {"low": 7, "high": 17}, 22)[0] == "정상"
    assert bot_cause(LIVE, END, [], END, LIVE, [], {"low": 7, "high": 17}, 3)[0] == "판단 보류"


def test_line_cause_uses_period_minimum():
    """이번 구간 최저가 경고선 이하면 경고(지금은 회복했어도)."""
    cum = pd.Series([-0.01, -0.08, -0.03])
    assert line_cause(cum, {"line": -0.072, "sensitivity": "민감"}) == (
        "경고", "실제 -3.0%(이번 구간 최저 -8.0%) · 경고선 -7.2%(민감도 민감 · 하루 1회 확인 기준)")
    assert line_cause(pd.Series([0.01]), {"line": -0.072, "sensitivity": "보통"})[0] == "정상"


def test_thesis_states():
    """논지 없음은 해당 없음, 60일 넘으면 경고."""
    assert thesis_cause("논지 없음", 90) == ("해당 없음", "논지 없음")
    rule = "횡보 60일 넘게 이어지면 「전제와 안 맞는 장」 알림"
    assert thesis_cause(rule, 61)[0] == "경고" and thesis_cause(rule, 60)[0] == "정상" and thesis_cause(rule, None)[0] == "확인 불가"


def test_representative_order_and_skip():
    """확인 불가 > 점검 불가 > 경고 > 판단 보류 > 정상, 해당 없음은 빠짐, 실행을 빼고도 볼 수 있음."""
    s = {"전략": "판단 보류", "실행": "경고", "봇": "정상", "선": "정상", "논지": "해당 없음"}
    assert representative(s) == ("경고", "실행")
    assert representative(s, ("실행",)) == ("판단 보류", "전략")
    assert representative({**s, "봇": "점검 불가(섞인 기록)"}) == ("점검 불가(섞인 기록)", "봇")


def _live(cons_total=-0.019):
    idx = pd.date_range(LIVE, END - timedelta(days=1), freq="D")
    r = pd.Series(0.0, index=idx)
    r.iloc[-1] = cons_total
    return LiveRun({}, r.copy(), r.copy(), [])


def kw(**over):
    """reconcile 인자(기본: 실전 시작 9/23 · 받은 범위 빈틈없음 · 펀딩비 0)."""
    base_ = dict(end=END, live_start=LIVE, live=_live(), actual=None, retro=None, cov_from=LIVE, cov_until=END,
                 missing=[], funding_ok=True, funding_total=D(0), mixed=False, link_changed=False, side_days=None)
    return {**base_, **over}


def test_reconcile_without_capital_judges_strategy_only():
    """실전 시작 잔고를 모르면 전략만 판정, 나머지는 점검 불가(실전 시작 잔고 모름)."""
    day = reconcile("s1", guard(), **kw(cov_from=None, cov_until=None))
    assert day.states == {"전략": "판단 보류", "실행": "점검 불가(실전 시작 잔고 모름)",
                          "봇": "점검 불가(실전 시작 잔고 모름)", "선": "점검 불가(실전 시작 잔고 모름)", "논지": "해당 없음"}
    assert day.day == date(2026, 10, 14) and day.live_day == 22 and day.week is None
    assert day.head == "실제 모름 · 같은 기간 BT -1.9% · 정상 범위 -2.2% ~ +6.6%(22일째 · 보수적 비용 기준)"


def test_reconcile_mixed_and_link_changed():
    """섞인 기록이면 실행 · 봇 · 선이 점검 불가(섞인 기록), 연결 파일이 바뀌면 전략이 점검 불가."""
    act = Actual(pd.Series([0.0], index=pd.DatetimeIndex([END])), [], None, D(10000))
    day = reconcile("s1", guard(), **kw(actual=act, mixed="섞인 사유 글", link_changed=True))
    assert day.states["전략"] == "점검 불가(연결 파일 바뀜)"
    assert day.states["실행"] == day.states["봇"] == day.states["선"] == "점검 불가(섞인 기록)"
    assert day.lines["실행"] == day.lines["봇"] == "섞인 사유 글"  # 사유는 부르는 쪽(run_daily)이 정한 글 그대로


def test_messages_hide_execution_until_enabled_and_show_response():
    """실행 경고를 안 켰으면 실행 줄은 안내만 · 대표 상태에서 빠짐, 켜면 실행 경고와 대응을 보임."""
    day = Day("trend_10_50", date(2026, 10, 14), 22, 37, "실제 -4.6% · 같은 기간 BT -1.9% · 정상 범위 -3.7% ~ +8.1%(22일째)",
              {"전략": "판단 보류", "실행": "경고", "봇": "정상", "선": "정상", "논지": "해당 없음"},
              {"전략": "a", "실행": "b", "봇": "c", "선": "d", "논지": "논지 없음"},
              {"execution_warning": "주문 코드 확인", "warning_line": ""}, None)
    off = change_message(day, False).splitlines()
    assert off[0] == "판단 보류 · 전략"
    assert "실행: 첫 회고를 본 뒤 켬(blackclip notify --execution-alerts on) · 숫자는 daily 파일에" in off
    assert not any(x.startswith("논지") for x in off)
    on = change_message(day, True).splitlines()
    assert on[0] == "경고 · 실행 문제로 추정" and "실행: 경고 · b" in on
    assert "가드레일 대응(내가 정해 둔 것): 실행 경고 → 주문 코드 확인" in on
    assert on[-1] == "자세히: 컴퓨터에서 .blackclip/strategies/trend_10_50/daily/2026-10-14.md"
    b = bundle([day], False, []).splitlines()
    assert b[0] == "매일 묶음 · 2026-10-14 UTC 마감 · 전략 1개" and b[1].startswith("trend_10_50: 판단 보류 · 실전 22일째 · 거래 37건 · ")
    md = render_md(day, 1)
    assert "결론: 경고 · 실전 22일째 · 거래 37건" in md and "- 실행: 경고 · b" in md
    for text in (md, "\n".join(on)):
        for bad in ("전략은 정상 범위", "문제없", "팔아", "꺼라", "거품 뺀"):
            assert bad not in text


def test_triggered_responses_follow_change_message_rule():
    """어느 대응이 걸렸는지의 규칙은 한 곳(`triggered`)이다: 실행 칸 경고 → execution_warning, 선 칸 경고 → warning_line(실행 → 선 순서).

    텔레그램 글(`change_message`)은 그 위에 「실행 경고를 켰는지」만 걸러 내고, 화면은 거르지 않는다. 글 바이트는 그대로다.
    """
    quiet = {"전략": "정상", "실행": "정상", "봇": "정상", "선": "정상", "논지": "해당 없음"}
    for exe, line, want in (("정상", "정상", []), ("경고", "정상", ["execution_warning"]), ("정상", "경고", ["warning_line"]),
                            ("경고", "경고", ["execution_warning", "warning_line"])):
        assert triggered(quiet | {"실행": exe, "선": line}) == want, (exe, line)
    assert triggered(quiet | {"실행": "확인 불가(체결 내역을 못 받음)", "선": "판단 보류", "봇": "경고", "전략": "경고"}) == []  # 다른 칸의 경고 · 괄호 사유는 걸린 대응이 아님
    assert triggered(quiet | {"실행": "경고(사유 글)"}) == ["execution_warning"]  # 괄호 사유는 떼고 읽음(states.base)
    resp = {"execution_warning": "주문 코드 확인", "warning_line": "중단 검토\n멈출지는 내가 정한다"}

    def lines(states, on):
        day = Day("s1", date(2026, 10, 14), 22, 37, "head", states, {k: "근거" for k in states}, resp, None)
        return [x for x in change_message(day, on).splitlines() if x.startswith("가드레일 대응")]

    both = quiet | {"실행": "경고", "선": "경고"}
    reply = "가드레일 대응(내가 정해 둔 것): "
    assert lines(both, True) == [reply + "실행 경고 → 주문 코드 확인 · 경고선 → 중단 검토 멈출지는 내가 정한다"]
    assert lines(both, False) == [reply + "경고선 → 중단 검토 멈출지는 내가 정한다"]  # 실행 경고를 안 켰으면 실행 대응만 빠진다
    assert lines(quiet | {"실행": "경고"}, True) == [reply + "실행 경고 → 주문 코드 확인"] and lines(quiet | {"실행": "경고"}, False) == []
    assert lines(quiet | {"선": "경고"}, True) == lines(quiet | {"선": "경고"}, False) == [reply + "경고선 → 중단 검토 멈출지는 내가 정한다"]
    assert lines(quiet, True) == []


def test_multiline_response_keeps_daily_lines():
    """대응 글이 여러 줄이어도 변화 글의 대응 줄 · 매일 파일의 대응 줄은 한 줄(줄바꿈은 공백 하나로)."""
    day = Day("s1", date(2026, 10, 14), 22, 37, "head", {"전략": "정상", "실행": "경고", "봇": "정상", "선": "경고", "논지": "해당 없음"},
              {k: "근거" for k in ("전략", "실행", "봇", "선", "논지")},
              {"execution_warning": "주문\n코드 확인", "warning_line": "첫째\n  둘째"}, None)
    on = change_message(day, True).splitlines()
    assert "가드레일 대응(내가 정해 둔 것): 실행 경고 → 주문 코드 확인 · 경고선 → 첫째 둘째" in on
    assert "가드레일 대응(내가 정해 둔 것): 실행 경고 → 주문 코드 확인 · 경고선 → 첫째 둘째" in render_md(day, 1).splitlines()


def test_second_period_uses_period_start_equity():
    """최초 자산 100 · 둘째 구간 처음 200 · 끝 220이면 실제 +10% = BT +10% · 실행 차이 0."""
    end = LIVE + timedelta(days=40)
    idx = pd.date_range(LIVE, end - timedelta(days=1), freq="D")
    r = pd.Series(0.0, index=idx)
    r.iloc[-1] = 0.10
    act = Actual(pd.Series([0.0, 100.0, 120.0], index=pd.DatetimeIndex([LIVE, LIVE + timedelta(days=30), end])), [], None, D(100))
    rt = build_retro([], [], None, D(100), end, LIVE + timedelta(days=30), 30, timedelta(days=1), "USDT", fills_until=end)
    day = reconcile("s1", guard(), **kw(end=end, live=LiveRun({}, r.copy(), r.copy(), []), actual=act, retro=rt,
                                        cov_until=end))
    assert day.head.startswith("실제 +10.0% · 같은 기간 BT +10.0%")
    assert day.lines["실행"].startswith("실제 − BT(비용 0) +0.0%p")


def test_funding_counted_without_fills_in_period():
    """이번 구간 체결 0건 · 펀딩비 −10 · 자산 100이면 펀딩비 −10.0%p, 나머지 0."""
    act = Actual(pd.Series([0.0, -10.0], index=pd.DatetimeIndex([LIVE, END])), [], None, D(100))
    rt = build_retro([], [], None, D(100), END, LIVE, 30, timedelta(days=1), "USDT", fills_until=END)
    zero = _live(0.0)
    day = reconcile("s1", guard(market="futures"), **kw(live=zero, actual=act, retro=rt, funding_total=D(-10)))
    assert "펀딩비 -10.0%p" in day.lines["실행"] and "나머지 +0.0%p" in day.lines["실행"]


def test_fills_missing_from_start_are_unconfirmed():
    """잔고를 9/23에 받았는데 체결은 10/2부터만 있으면 봇 · 실행 · 선 확인 불가, 실제 손익은 모름."""
    act = Actual(pd.Series([0.0, 0.0], index=pd.DatetimeIndex([LIVE, END])), [], None, D(10000))
    day = reconcile("s1", guard(), **kw(actual=act, cov_from=LIVE + timedelta(days=9), need_from=LIVE - timedelta(hours=12)))
    assert day.states["봇"] == day.states["선"] == "확인 불가" and day.states["실행"].startswith("확인 불가")
    assert "10/02 00:00 UTC부터만 받음" in day.lines["봇"] and day.head.startswith("실제 모름")
    assert representative(day.states)[0] == "확인 불가"


def test_capital_basis_line_only_in_daily_file():
    """손익 줄 아래 기준 자본 출처 한 줄은 daily 파일에만: 정했으면 금액 없이 「고객이 정한 금액」, 아니면 계좌 잔고 전체 + 출발 포지션."""
    day = reconcile("s1", guard(), **kw())
    snap = {"quote": "USDT", "capital": None}
    assert capital_basis(None) is None and "기준 자본" not in render_md(day, 1)
    own = render_md(day, 1, capital_basis({**snap, "capital": D("7654.32")}))
    assert own.count("기준 자본:") == 1 and "\n기준 자본: 고객이 정한 금액\n" in own and "7654" not in own
    auto = render_md(day, 1, capital_basis(snap))
    assert auto.count("기준 자본:") == 1 and (
        "\n기준 자본: 계좌 USDT 잔고 전체 + 출발 포지션(전략 밖 돈이 있으면 %가 작게 나옴 · `blackclip live --capital`로 정함)\n" in auto)
    assert auto.index(day.head) < auto.index("기준 자본:") < auto.index("- 전략:")
    assert "기준 자본" not in change_message(day, True) and "기준 자본" not in bundle([day], True, [])


def test_bundle_line_keeps_cost_basis():
    """매일 묶음 한 줄에도 정상 범위의 비용 가정."""
    day = reconcile("s1", guard(), **kw())
    assert "보수적 비용 기준" in bundle([day], False, []).splitlines()[1]


def test_line_judged_from_actual_pnl_without_backtest():
    """같은 기간 BT를 못 냈어도(live 없음) 실제 손익이 있으면 선은 그 손익으로 판정하고, 전략 칸만 확인 불가."""
    why = "신호 함수를 돌리지 못함(SignalError)"
    act = Actual(pd.Series([0.0, -900.0], index=pd.DatetimeIndex([LIVE, END])), [], None, D(10000))
    day = reconcile("s1", guard(), **kw(live=None, actual=act, why=why))
    assert day.states["전략"] == f"확인 불가({why})"
    assert day.states["선"] == "경고" and day.lines["선"].startswith("실제 -9.0%(이번 구간 최저 -9.0%) · 경고선 -7.2%")
    assert day.head.startswith("실제 -9.0% · 같은 기간 BT 모름")


def test_line_unconfirmed_without_backtest_and_without_actual_pnl():
    """BT도 없고 실제 손익 줄도 비었으면 선은 확인 불가(빈 손익을 「정상 +0.0%」로 내지 않음)."""
    why = "매일 대조는 1일 이하 봉만 · 점검(blackclip check)은 그대로"
    act = Actual(pd.Series([], index=pd.DatetimeIndex([], tz="UTC"), dtype=float), [], None, D(10000))
    day = reconcile("s1", guard(), **kw(live=None, actual=act, why=why))
    assert day.states["선"] == f"확인 불가({why})" and day.lines["선"] == why


def test_line_warning_shows_in_title_when_not_headline():
    """선이 경고인데 대표 칸이 선이 아니면 글 첫 줄 · 묶음 전략 줄에 「경고선 경고」가 한 번 붙고, 선이 대표 칸이면 안 붙는다."""
    states = {"전략": "확인 불가(x)", "실행": "정상", "봇": "정상", "선": "경고", "논지": "해당 없음"}
    lines = {k: "근거" for k in states}
    day = Day("s1", date(2026, 10, 14), 22, 3, "실제 -9.0% · 같은 기간 BT 모름", states, lines, {}, None)
    assert change_message(day, True).splitlines()[0] == "확인 불가(x) · 전략 · 경고선 경고"
    assert bundle([day], True, []).splitlines()[1].count(" · 경고선 경고 · ") == 1
    head = Day("s1", date(2026, 10, 14), 22, 3, "h", {**states, "전략": "정상", "논지": "정상"}, lines, {}, None)
    assert change_message(head, True).splitlines()[0] == "경고 · 경고선"
    assert "경고선 경고" not in bundle([head], True, [])


def test_nums_all_spans_from_live_start():
    """45일째에는 이번 구간(31일째부터)과 실전 시작부터가 갈림, 22일째(첫 구간)는 같음."""
    end = LIVE + timedelta(days=45)
    idx = pd.date_range(LIVE, end - timedelta(days=1), freq="D")
    r = pd.Series(0.0, index=idx)
    r.iloc[5], r.iloc[40] = -0.02, 0.01
    n = reconcile("s1", guard(), **kw(end=end, live=LiveRun({}, r.copy(), r.copy(), []), cov_until=end)).nums
    assert n["period_start"] == (LIVE + timedelta(days=30)).date().isoformat() and n["bt"] == 0.01
    assert n["all"]["start"] == LIVE.date().isoformat() and n["all"]["bt"] == round(0.98 * 1.01 - 1, 6)
    first = reconcile("s1", guard(), **kw()).nums
    assert {k: first["all"][k] for k in ("bt", "bt0")} == {"bt": first["bt"], "bt0": first["bt0"]}


def test_execution_numbers_feed_the_text():
    """execution_numbers가 execution_cause 글의 숫자 출처: 비율 · bp · 펀딩비 None 조건 · 짝 없음."""
    t1, t2 = datetime(2026, 10, 1, tzinfo=UTC), datetime(2026, 10, 13, 15, 58, tzinfo=UTC)
    bt = [ex(t1, "buy", "60000", "1"), ex(t2, "sell", "61000", "1", oid="2")]
    act = [ex(t1 + timedelta(minutes=5), "buy", "60030", fee="6", oid="9")]
    r = build_retro(bt, act, None, D(10000), END, LIVE, 30, timedelta(days=1), "USDT", fills_until=END)
    n = execution_numbers(r, D(10000), -0.027, False, True, D(0))
    assert (n["fee"], n["slip"], n["funding"], n["slip_med_bp"], n["slip_max_bp"], n["over"]) == (-0.0006, -0.0003, None, -5.0, -5.0, 0)
    assert abs(n["rest"] - (-0.027 + 0.0006 + 0.0003)) < 1e-12
    fut = execution_numbers(r, D(10000), -0.027, True, True, D(-10))
    assert fut["funding"] == -0.001 and abs(fut["rest"] - (-0.027 + 0.0006 + 0.0003 + 0.001)) < 1e-12
    assert execution_numbers(r, D(10000), -0.027, True, False, D(-10))["funding"] is None  # 범위를 못 덮으면 모름
    none = build_retro([], [], None, D(100), END, LIVE, 30, timedelta(days=1), "USDT", fills_until=END)
    e = execution_numbers(none, D(100), 0.0, False, True, D(0))
    assert (e["slip_med_bp"], e["slip_max_bp"], e["over"]) == (None, None, 0)


def test_nums_keep_unknown_as_none():
    """실전 시작 잔고를 모르면 실제 · 차이 · 잰 것은 None, BT와 그날 범위 위치는 남고, 범위를 안 그리면 위치도 None."""
    n = reconcile("s1", guard(), **kw(cov_from=None, cov_until=None)).nums
    assert n["actual"] is None and n["gap"] is None and n["fee"] is None and n["room"] is None and n["pos"] is None
    assert n["bt"] == -0.019 and n["bt_pos"] == "안" and (n["lo"], n["hi"]) == (-0.022, 0.066) and n["d"] == 22
    assert n["missed"] == [] and n["missed_n"] is None and n["n7"] is None and n["edge0"] == 0.79
    assert n["all"]["start"] == LIVE.date().isoformat() and n["all"]["bt"] == n["bt"]
    m = reconcile("s1", guard(drawn=False), **kw(cov_from=None, cov_until=None)).nums
    assert (m["lo"], m["hi"], m["month_lo"], m["month_hi"], m["edge0"], m["bt_pos"]) == (None, None, None, None, None, None)


def test_nums_all_keeps_one_meaning_in_the_first_period():
    """선물 첫 구간: 구간 값은 펀딩비를 따로 재고 나머지에서 뺀다. nums.all은 첫 구간이어도 펀딩비 None · rest = gap − fee − slip(31일째 이후와 같은 뜻)."""
    idx = pd.date_range(LIVE, END, freq="D")
    act = Actual(pd.Series([0.0] * (len(idx) - 1) + [-10.0], index=idx), [], None, D(10000))
    rt = build_retro([], [], None, D(10000), END, LIVE, 30, timedelta(days=1), "USDT", fills_until=END)
    n = reconcile("s1", guard(market="futures"), **kw(actual=act, retro=rt, funding_total=D(-10))).nums
    assert (n["funding"], n["gap"], n["rest"]) == (-0.001, 0.018, 0.019)  # 구간: gap − fee − slip − funding
    a = n["all"]
    assert a["funding"] is None and a["gap"] == n["gap"] and a["rest"] == 0.018  # 전체: gap − fee − slip
    assert (a["actual"], a["bt"], a["bt0"], a["fee"], a["slip"]) == (n["actual"], n["bt"], n["bt0"], n["fee"], n["slip"])


def test_unconfirmed_first_line_names_column_not_cause():
    """체결을 못 받아 실행 칸이 확인 불가면 첫 줄은 칸 이름(실행)이고 「실행 문제로 추정」은 글에 없다."""
    why = "체결 내역을 못 받음"
    states = {"전략": "정상", "실행": f"확인 불가({why})", "봇": "정상", "선": "정상", "논지": "해당 없음"}
    day = Day("s1", date(2026, 10, 14), 22, 3, "h", states, {k: "근거" for k in states}, {}, None)
    text = change_message(day, True)
    assert text.splitlines()[0] == f"확인 불가({why}) · 실행" and "실행 문제로 추정" not in text


def test_warning_first_line_keeps_cause_title():
    """회귀: 경고일 때는 첫 줄이 지금처럼 「경고 · 실행 문제로 추정」."""
    states = {"전략": "정상", "실행": "경고", "봇": "정상", "선": "정상", "논지": "해당 없음"}
    day = Day("s1", date(2026, 10, 14), 22, 3, "h", states, {k: "근거" for k in states}, {"execution_warning": "확인"}, None)
    assert change_message(day, True).splitlines()[0] == "경고 · 실행 문제로 추정"

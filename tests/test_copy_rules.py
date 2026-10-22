"""문구 규칙(디자인 시스템 문구 규칙 가운데 적용하는 것): 섞인 계좌 세 줄 · 텔레그램 해요체 · 금지 낱말 · E 숫자 형식."""
import ast
import copy
import re
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path

import pandas as pd
import pytest
from facts import guardrail as guardrail_of
from facts import write_facts

import blackclip
from blackclip.daily.live import LiveRun
from blackclip.daily.messages import broken_message, broken_note, plan_messages
from blackclip.daily.reconcile import Day, bundle, change_message, reconcile
from blackclip.notify import LINKED_TEXT
from blackclip.screen.chart import chart_e
from blackclip.watch.judge import message, mixed_message
from blackclip.watch.position import compare

BANNED = ("추천", "유망", "목표가", "사세요", "파세요", "항상", "100%", "오전", "오후", "자정")
"""투자 의견 · 단정 · 시각 낱말. 「매수」 · 「매도」는 넣지 않는다: 고객 자신의 체결 방향 사실이라
투자 의견이 아니다(놓친 주문 · 포지션 방향 글이 쓴다)."""
EMOJI = re.compile("[\U0001F000-\U0001FAFF\uFE0F]")
"""이모지 범위(U+1F000~U+1FAFF · 변형 선택자 U+FE0F)."""
SRC = Path(blackclip.__file__).parent

ALLOWED = {("check/report.py", "추천"), ("stages.py", "추천"), ("screen/web.py", "100%")}
"""소스 문자열에서 금지 낱말을 허용하는 (파일, 낱말) 짝. 추천: 엔진이 제안한 값의 표 머리 「추천 가드레일」(check.md로 이미 저장돼 stages.py가
읽으므로 바꾸면 옛 파일을 못 읽음). 100%: 대시보드 CSS(screen/web.py, 차트 · 표 `width:100%`)의 `width:100%`(옛 연결 폼 HTML은 지움)."""

UTC = timezone.utc
S = {"전략": "판단 보류", "실행": "경고", "봇": "정상", "선": "정상", "논지": "해당 없음"}
LINES = {k: "근거" for k in S}
HEAD = "실제 +1.0% · 같은 기간 BT +0.5% · 정상 범위 그리지 않음"


@pytest.fixture(autouse=True)
def _home(monkeypatch, tmp_path):
    monkeypatch.setenv("HOME", str(tmp_path / "home"))


def _day(name="s1", states=S, week=None):
    return Day(name, date(2026, 10, 18), 25, 40, HEAD, dict(states), dict(LINES), {"execution_warning": "", "warning_line": ""}, week)


def _real_week() -> str:
    """실제 `reconcile()`이 만드는 매주 회고 줄(막 끝난 UTC 날이 일요일인 날, reconcile.py의 템플릿 그대로).

    Returns:
        매주 회고 한 줄.
    """
    live, end = datetime(2026, 9, 23, tzinfo=UTC), datetime(2026, 10, 19, tzinfo=UTC)  # 막 끝난 날 10/18 일요일
    zero = pd.Series(0.001, index=pd.date_range(live, end - timedelta(days=1), freq="D"))
    g = {"live_start": "2026-09-23", "normal_range": {"days": 30, "drawn": False, "reason": "거래 7건"},
         "judgment_hold": {"min_trades": 30, "hold_if_edge0_at_least": 0.5, "edge0_in_range": None},
         "trade_frequency": {"days": 7, "low": 7, "high": 17}, "warning_line": {"line": -0.072, "sensitivity": "민감"},
         "thesis": {"rule": "논지 없음"}, "target": {"market": "spot", "symbols": ["BTCUSDT"]}, "response": {}}
    day = reconcile("s1", g, end=end, live_start=live, live=LiveRun({}, zero.copy(), zero.copy(), []), actual=None, retro=None,
                    cov_from=live, cov_until=end, missing=[], funding_ok=True, funding_total=Decimal(0), mixed=False,
                    link_changed=False, side_days=None)
    assert day.week
    return day.week


def telegram_texts() -> list[tuple[str, str]]:
    """텔레그램으로 나가는 글을 실제 만드는 함수로 모두 만든다. 새 글이 생기면 여기에 한 줄만 더한다(기록 손상 글도).

    Returns:
        [(이름, 글)].
    """
    warn, hold = {**S, "선": "경고"}, {**S, "봇": "확인 불가(체결 내역을 못 받음)"}
    notes = ["체결 받기 일부 실패(binance main spot) · 컴퓨터에서 blackclip daily 출력 확인"]
    week = _real_week()
    t0 = datetime(2025, 1, 23, 0, 0, tzinfo=UTC)
    _, grace = compare({"BTCUSDT": 0.2}, {"BTCUSDT": t0}, {"BTCUSDT": 0}, t0 + timedelta(hours=1), 86400, False)
    states = {"선": "정상", "봇": "정상"}
    texts = [("change 경고", change_message(_day(states=warn), True)),
             ("change 확인 불가", change_message(_day(states=hold), True)),
             ("change 실행 꺼짐", change_message(_day(), False)),
             ("bundle 전략 2 · notes 1", bundle([_day("s1"), _day("s2", states=warn)], False, notes)),
             ("weekly", plan_messages([_day(week=week)], {"s1": S}, {"quiet": "바뀔때만", "execution_alerts": False}, [])[0][0][0]),
             ("watch 경고", message("s1", {"선": "경고", "봇": "정상"}, {"선": "구간 수익 -28.0%", "봇": "포지션 맞음: BTCUSDT 없음"},
                                  {"warning_line": "중단 검토. 멈출지는 내가 정한다"})),
             ("watch 포지션 유예", message("s1", states, {"선": "근거", "봇": grace}, {})),
             ("mixed", mixed_message({("main", "spot"): ["s2", "s3"], ("sub1", "futures"): ["s4"]})),
             ("linked", LINKED_TEXT),
             ("broken", broken_message("s2")),
             ("broken note", broken_note("s2"))]
    return texts


def test_mixed_message_names_without_count():
    """섞인 계좌 글은 새로 섞인 전략만 이름으로 말하고 수(「둘 이상」)를 말하지 않는다. 해요체 · 가운뎃점 없음."""
    one = mixed_message({("main", "spot"): ["s2"]})
    assert one.splitlines() == ["섞인 계좌라 1시간마다 보지 않고 매일 대조로만 봐요", "다른 전략과 계좌를 같이 써요: binance main spot(s2)",
                                "전략마다 계좌(서브 계정)를 나누면 1시간마다 봐요"]
    two = mixed_message({("sub1", "futures"): ["s3", "s4"], ("main", "spot"): ["s2", "s5"]})
    assert two.splitlines()[1] == "다른 전략과 계좌를 같이 써요: binance main spot(s2, s5), binance sub1 futures(s3, s4)"
    for text in (one, two):
        assert not any(w in text for w in ("둘 이상", "·", "봄"))


def test_telegram_texts_follow_copy_rules():
    """텔레그램으로 나가는 글: 금지 낱말 0 · 이모지 0 · 합쇼체(「니다」) 0."""
    texts = telegram_texts()
    assert len(texts) >= 9
    for label, text in texts:
        assert not [w for w in BANNED if w in text], (label, text)
        assert not EMOJI.search(text), (label, text)
        assert "니다" not in text, (label, text)


def _user_strings(tree: ast.AST):
    """docstring(식 문장으로 선 문자열)을 뺀 문자열 상수(f-string 조각 포함)."""
    doc = {id(n.value) for n in ast.walk(tree) if isinstance(n, ast.Expr) and isinstance(n.value, ast.Constant)}
    return [n.value for n in ast.walk(tree) if isinstance(n, ast.Constant) and isinstance(n.value, str) and id(n) not in doc]


def test_terminal_lines_have_no_banned_words():
    """src/blackclip의 사용자에게 가는 문자열(docstring 제외)에 금지 낱말 · 이모지 0. 허용은 (파일, 낱말) 짝만, 안 쓰이는 짝이 남으면 실패."""
    used, bad = set(), []
    for path in sorted(SRC.rglob("*.py")):
        rel = path.relative_to(SRC).as_posix()
        for s in _user_strings(ast.parse(path.read_text(encoding="utf-8"))):
            bad += [(rel, "이모지", s[:40]) for _ in EMOJI.findall(s)]
            for w in BANNED:
                if w in s and (rel, w) in ALLOWED:
                    used.add((rel, w))
                elif w in s:
                    bad.append((rel, w, s[:40]))
    assert not bad, bad
    assert used == ALLOWED, f"쓰이지 않는 허용: {ALLOWED - used}"


def _e_recs(tmp_path, rets):
    """지어낸 기록의 마지막 줄 표본을 rets로 바꾼 (기록, 가드레일, 평균 · 분위를 바꿀 수 있는 가드레일)."""
    from blackclip.daily.days import read_days
    root = write_facts(tmp_path, ("trend_10_50",))
    recs = read_days(root, "trend_10_50")
    recs[-1]["nums"].update(trade_rets=rets, trade_n=len(rets), trade_mean=sum(rets) / len(rets))
    recs[-1]["trades"] = len(rets)
    return recs, copy.deepcopy(guardrail_of("trend_10_50"))


def test_e_numbers_use_commas_and_no_signed_zero(tmp_path, monkeypatch):
    """E: 1,000 이상 정수는 천 단위 쉼표(쉼표 없는 네 자리 이상 수 0개, 날짜 · 시각 꼴 제외), 반올림해 0인 %는 부호 없이 0.00%."""
    monkeypatch.setenv("NO_COLOR", "1")
    recs, g = _e_recs(tmp_path, [0.01, -0.02, 0.03, -0.01] * 308 + [0.0, 0.0])  # 1,234건
    recs[-1]["nums"].update(n7=1234, tf_low=1100, tf_high=1300)
    g["judgment_hold"]["min_trades"] = 1000
    text = "\n".join(chart_e("trend_10_50", recs, g, False, 120))
    assert "1,234건" in text and "1,000건" in text and "최근 7일 1,234건 · 백테스트 기준 1,100~1,300건" in text
    assert not re.findall(r"(?<![\d,:/])(?<!\d-)\d{4,}(?!\d|,\d|-\d|:\d)", text), re.findall(r"\S*\d{4,}\S*", text)
    rows = text.splitlines()
    axis = rows[next(i for i, x in enumerate(rows) if "└" in x) + 1]  # x 축 바로 밑 번호 줄
    assert "1,000" in axis and axis.rstrip().endswith("1,234")  # 축 번호도 쉼표(첫 번호 1 · 기준선 1,000 · 마지막 1,234)
    for sign in (1, -1):
        recs, g = _e_recs(tmp_path / str(sign), [sign * 1e-5] * 40)  # 평균 +0.001% · -0.001%
        g["basis"].update(trade_mean=sign * 1e-5, trade_p10=-1e-5, trade_p90=1e-5)
        out = "\n".join(chart_e("trend_10_50", recs, g, False, 120))
        assert "+0.00%" not in out and "-0.00%" not in out and "실제 0.00%" in out and "백테스트 0.00%" in out, out
        assert "상단 0.00%" in out and "하단 0.00%" in out


def test_e_percent_has_thousands_comma(tmp_path, monkeypatch):
    """E 백분율도 천 단위 쉼표(거래 평균 1000% → +1,000.00%)."""
    monkeypatch.setenv("NO_COLOR", "1")
    recs, g = _e_recs(tmp_path, [10.0])
    text = "\n".join(chart_e("trend_10_50", recs, g, False, 120))
    assert "실제 +1,000.00%" in text and "+1000.00%" not in text, text

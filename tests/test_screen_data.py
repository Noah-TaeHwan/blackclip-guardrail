"""screen/data.py: 터미널 chart와 웹이 같이 쓰는 D · A · E 규칙. 기대값은 tests/facts.py(FACTS · 스파크라인 골든)에서 읽는다."""
import json
from datetime import date, timedelta

import pytest
from facts import DAYS, MISSED, SPARK, START, TRADE_RETS, record, write_facts
from facts import guardrail as guardrail_of

from blackclip.daily.days import read_days
from blackclip.screen.data import (
    E_CAP,
    a_marks,
    b_series,
    d_values,
    e_series,
    period_cuts,
    seen,
    spark,
    trade_sample,
)
from blackclip.workspace import WorkspaceError

LAST = (START + timedelta(days=DAYS - 1)).isoformat()  # FACTS 마지막 날 2026-10-14
BARS = "▁▂▃▄▅▆"


def _recs(tmp_path, name):
    return read_days(write_facts(tmp_path, (name,)), name)


def _glyphs(cells):
    return "".join("·" if c["level"] is None else BARS[c["level"]] for c in cells)


def _second_period(recs, upto=35, second_from=31):
    """test_chart_a_second_period_marks_restart 꼴: 22일째 줄을 이어 붙이고 31일째부터 새 구간(10/23)."""
    extra = []
    for k in range(DAYS + 1, upto + 1):
        r = json.loads(json.dumps(recs[-1]))
        r.update(day=(START + timedelta(days=k - 1)).isoformat(), live_day=k)
        if k >= second_from:
            r["nums"].update(period_start="2026-10-23", d=k - 30, actual=round(-0.001 * (k - 30), 6), bt=0.0)
        extra.append(r)
    return [*recs, *extra]


def test_spark_glyphs_match_facts_golden(tmp_path):
    """네 FACTS 전략의 칸 글자(눈금 0~5 · 값 없는 날 ·)가 SPARK 골든과 같고, 칸은 모양이 고정이다."""
    root = write_facts(tmp_path)
    for name in SPARK:
        cells = spark(read_days(root, name), date.fromisoformat(LAST))
        assert _glyphs(cells) == SPARK[name], name
        assert all(set(c) == {"day", "value", "warn", "level"} for c in cells)
        assert [c["day"] for c in cells][0] == START.isoformat() and cells[-1]["day"] == LAST


def test_spark_marks_warning_days_only_where_a_value_is_drawn(tmp_path):
    """trend_10_50은 21 · 22일째(놓친 주문)부터 경고. breakout_v2의 값 없는 칸은 경고 표시가 없다."""
    last = date.fromisoformat(LAST)
    warn = [c["warn"] for c in spark(_recs(tmp_path, "trend_10_50"), last)]
    assert warn == [False] * 20 + [True, True]
    gap = spark(_recs(tmp_path / "b", "breakout_v2"), last)[-3:]
    assert [(c["value"], c["level"], c["warn"]) for c in gap] == [(None, None, False)] * 3


def test_spark_uses_common_last_day(tmp_path):
    """마지막 날이 다른 두 전략: 이른 전략(grid_v1, 15일째까지)도 공통 끝(10/14)까지 22칸이고 남는 7칸은 빈 칸."""
    recs = _recs(tmp_path, "grid_v1")[:15]
    own = spark(recs, date.fromisoformat(recs[-1]["day"]))
    common = spark(recs, date.fromisoformat(LAST))
    assert len(own) == 15 and len(common) == 22
    assert _glyphs(common)[:15] == _glyphs(own)  # 눈금은 그려진 값의 최소 · 최대라 빈 칸이 늘어도 같다
    assert [c["day"] for c in common][-1] == LAST
    assert [(c["value"], c["level"]) for c in common[15:]] == [(None, None)] * 7


def test_spark_groups_long_runs_into_22_cells_ending_on_the_last_day(tmp_path):
    """22일을 넘으면 22칸으로 묶고 칸마다 묶음의 마지막 날을 쓴다(_spark 규칙 그대로)."""
    base = _recs(tmp_path, "trend_10_50")
    recs = [{**base[-1], "day": (START + timedelta(days=k)).isoformat(), "live_day": k + 1} for k in range(90)]
    cells = spark(recs, START + timedelta(days=89))
    assert len(cells) == 22 and cells[-1]["day"] == (START + timedelta(days=89)).isoformat()
    assert cells[0]["day"] == (START + timedelta(days=3)).isoformat()  # (0 + 1) * 90 // 22 - 1 = 3
    assert {c["level"] for c in cells} == {2}  # 값이 모두 같으면 가운데 눈금


def test_d_values_match_facts(tmp_path):
    """trend_10_50: 손익 -4.6% · 차이 -2.7%p · 기한 없음. breakout_v2: 10/12부터 체결을 못 받아 10/11까지(차이 없음)."""
    root = write_facts(tmp_path)
    d = d_values(read_days(root, "trend_10_50"))
    assert set(d) == {"pnl", "gap", "until"} and d["until"] is None
    assert d["pnl"] == pytest.approx(-0.046) and d["gap"] == pytest.approx(-0.027)
    b = d_values(read_days(root, "breakout_v2"))
    assert b == {"pnl": pytest.approx(0.004), "gap": None, "until": "2026-10-11"}
    g = d_values(read_days(root, "grid_v1"))
    assert g["pnl"] == pytest.approx(-0.008) and g["gap"] == pytest.approx(-0.003) and g["until"] is None


def test_d_values_unknown_everywhere_is_all_none(tmp_path):
    """실제 손익을 한 번도 모르면 셋 다 None(터미널은 모름 · 모름), 마지막 날만 모르면 그 앞 날까지."""
    recs = _recs(tmp_path, "trend_10_50")[:4]
    blind = json.loads(json.dumps(recs))
    for r in blind:
        r["nums"].update(actual=None, gap=None)
        r["nums"]["all"].update(actual=None, gap=None)
    assert d_values(blind) == {"pnl": None, "gap": None, "until": None}
    assert d_values([*recs[:3], blind[3]]) == {"pnl": recs[2]["nums"]["actual"], "gap": None, "until": recs[2]["day"]}


def test_a_marks_match_facts(tmp_path):
    """trend_10_50: 그날 범위가 있어 띠를 그리고, 놓친 주문은 10/13, 경고선 -7.2%, 구간은 하나뿐."""
    m = a_marks(_recs(tmp_path, "trend_10_50"), guardrail_of("trend_10_50"))
    assert set(m) == {"band", "reason", "missed", "line", "starts"}
    assert m == {"band": True, "reason": None, "missed": {"2026-10-13"}, "line": -0.072, "starts": []}
    assert MISSED["t"].startswith("2026-10-13")


def test_a_marks_starts_are_the_days_a_new_period_first_appears(tmp_path):
    """두 구간(31일째 10/23부터): starts == [10/23]. _view의 구간 경계 칸과 같은 날이다(그림 폭이 날수 이상일 때)."""
    from blackclip.screen.chart import _col, _view
    recs = _second_period(_recs(tmp_path, "trend_10_50"))
    m = a_marks(recs, guardrail_of("trend_10_50"))
    assert m["starts"] == ["2026-10-23"]
    p = 66
    cuts = _view(recs, p)[3]
    assert cuts == {_col((date.fromisoformat(d) - START).days, 35, p) for d in m["starts"]}


def test_a_marks_starts_ignore_failed_days_and_first_period(tmp_path):
    """대조 실패 날(period_start 없음)은 앞 구간을 이어받아 경계가 아니고, 첫 구간 시작도 경계가 아니다."""
    recs = _second_period(_recs(tmp_path, "trend_10_50"))
    recs[24]["nums"] = {}
    assert a_marks(recs, guardrail_of("trend_10_50"))["starts"] == ["2026-10-23"]


def test_period_cuts_skip_missing_and_failed_items_and_ignore_the_first_period():
    """None(기록 없는 칸)과 period_start 없는 항목은 앞 구간을 이어받고, 첫 구간 시작은 경계가 아니며, 같은 구간이 이어지면 경계가 아니다."""
    def at(start):
        return {"nums": {"period_start": start} if start else {}}
    a, b, c = at("A"), at("B"), at("C")
    assert period_cuts([None, a, at(None), a, None, b, b, at(None), c, b]) == [5, 8, 9]
    assert period_cuts([None, at(None), a]) == [] and period_cuts([]) == []


def test_a_marks_band_follows_daily_lo_hi(tmp_path):
    """가드레일이 drawn=False여도 days에 lo · hi가 있으면 띠(터미널 규칙), 둘 다 None이면 거짓 + 사유, 한쪽만 있으면 거짓."""
    recs = _recs(tmp_path, "grid_v1")
    g = guardrail_of("grid_v1")
    g["normal_range"].update(drawn=False, reason="표본이 모자람")
    assert a_marks(recs, g)["band"] is True and a_marks(recs, g)["reason"] is None  # 띠가 있으면 사유는 안 보임
    none = json.loads(json.dumps(recs))
    for r in none:
        r["nums"].update(lo=None, hi=None)
    got = a_marks(none, g)
    assert got["band"] is False and got["reason"] == "표본이 모자람"
    half = json.loads(json.dumps(recs))
    for i, r in enumerate(half):
        r["nums"].update(lo=None if i % 2 else r["nums"]["lo"], hi=r["nums"]["hi"] if i % 2 else None)  # 같은 날에 둘이 함께 있는 날 없음
    assert a_marks(half, g)["band"] is False
    del g["normal_range"]["reason"]
    assert a_marks(none, g)["reason"] is None


def test_a_marks_line_is_the_last_known_line(tmp_path):
    """마지막 날들의 선이 None이어도 앞 날의 경고선을 쓰고, 한 번도 없으면 None."""
    recs = _recs(tmp_path, "grid_v1")
    for r in recs[-4:]:
        r["nums"]["line"] = None
    recs[-5]["nums"]["line"] = -0.05
    assert a_marks(recs, guardrail_of("grid_v1"))["line"] == -0.05
    for r in recs:
        r["nums"]["line"] = None
    assert a_marks(recs, guardrail_of("grid_v1"))["line"] is None


def test_a_marks_unconvertible_missed_time_is_one_workspace_error(tmp_path):
    """모양 검사는 통과하지만 UTC로 바꿀 수 없는 놓친 주문 시각은 WorkspaceError(터미널은 한 줄 · 종료 2)."""
    recs = _recs(tmp_path, "trend_10_50")
    recs[-1]["nums"]["missed"][0]["t"] = "0001-01-01T00:00:00+01:00"
    with pytest.raises(WorkspaceError):
        a_marks(recs, guardrail_of("trend_10_50"))


def test_e_series_match_facts(tmp_path):
    """trend_10_50: 거래 37건이 전부 남고(빠진 것 · 안 센 청산 없음) 30건 선이 서며, 평균 -0.62% · 백테스트 +0.27% · 엣지 0 확률 79%."""
    recs = _recs(tmp_path, "trend_10_50")
    e = e_series(recs, guardrail_of("trend_10_50"))
    assert set(e) == {"rets", "kept", "exits", "n", "skip", "capped", "run", "p10", "p90", "mean", "minimum", "lost", "unseen",
                      "line30", "edge0", "n7", "tf_low", "tf_high"}
    assert (e["rets"], e["kept"], e["exits"], e["n"], e["skip"], e["capped"]) == (TRADE_RETS, 37, 37, 37, 0, [])
    assert len(e["run"]) == 37 and e["run"][0] == TRADE_RETS[0] and e["run"][-1] == pytest.approx(-0.0062, abs=5e-5)
    assert (e["p10"], e["p90"], e["mean"], e["minimum"]) == (-0.04, 0.05, 0.0027, 30)
    assert (e["lost"], e["unseen"], e["line30"]) == (0, 0, True)
    assert (e["edge0"], e["n7"], e["tf_low"], e["tf_high"]) == (0.79, 11, 7, 17)
    assert {k: e[k] for k in ("rets", "kept", "exits", "n", "skip", "capped")} == trade_sample(recs, E_CAP)


def test_e_series_without_trade_sample_has_no_series_and_no_line(tmp_path):
    """grid_v1은 거래 1건 수익 기록이 없다: 점 · 평균 없음, 30건 선 없음, 판단에 필요한 거래 수 30은 그대로."""
    e = e_series(_recs(tmp_path, "grid_v1"), guardrail_of("grid_v1"))
    assert (e["rets"], e["run"], e["line30"], e["minimum"]) == ([], [], False, 30)


def test_e_series_cap_drops_oldest_and_no_30_line(tmp_path):
    """표시 상한을 넘으면 가장 최근 것만 남고 빠진 수가 lost, 선은 안 선다."""
    e = e_series(_recs(tmp_path, "trend_10_50"), guardrail_of("trend_10_50"), cap=10)
    assert e["rets"] == TRADE_RETS[-10:] and e["kept"] == 37 and e["n"] == 37
    assert (e["lost"], e["line30"], len(e["run"])) == (27, False, 10)


def test_e_series_counts_exits_the_daily_never_saw(tmp_path):
    """기록에 없는 청산(exits - n - skip)은 unseen이고 30건 선을 막는다. 음수는 0으로(터미널은 0 이하를 같게 다룸)."""
    recs = _recs(tmp_path, "trend_10_50")
    g = guardrail_of("trend_10_50")
    recs[-1]["trades"] += 11
    e = e_series(recs, g)
    assert (e["exits"], e["unseen"], e["line30"]) == (48, 11, False)
    recs[-1]["trades"] -= 20
    assert e_series(recs, g)["unseen"] == 0


def test_e_series_old_guardrail_without_quantiles(tmp_path):
    """분위가 없는 예전 가드레일: p10 · p90 · mean은 None(띠 없이 평균 선만)."""
    g = guardrail_of("trend_10_50")
    g["basis"] = {}
    e = e_series(_recs(tmp_path, "trend_10_50"), g)
    assert (e["p10"], e["p90"], e["mean"]) == (None, None, None) and e["run"]


# --- 차트 B 값 · 「마지막으로 아는 날」 ---


def _chart_b_lines(recs):
    from blackclip.screen.chart import chart_b
    return chart_b("trend_10_50", recs, False, 200)


def test_b_series_labels_match_terminal():
    """터미널 `_b_labels` 글의 숫자 = b_series 라벨 값: 잰 것 · 나머지 · 합계 · 놓친 주문 수 · 처음 후보 · 펀딩비 유무."""
    from blackclip.screen.status import _pp
    recs = [record("trend_10_50", k) for k in range(1, DAYS + 1)]
    s = b_series(recs)
    lab = s["labels"]
    assert set(s) == {"days", "low", "high", "empty", "labels"} and s["empty"] is None
    assert set(lab) == {"slow", "fast", "gap", "upto", "funding", "missed_n", "first_missed"}
    assert (lab["slow"], lab["fast"], lab["gap"], lab["upto"], lab["funding"], lab["missed_n"]) == (pytest.approx(-0.006), -0.021, -0.027, None, False, 1)
    assert lab["first_missed"] == MISSED
    text = "\n".join(_chart_b_lines(recs))
    assert f"체결가 차이 · 수수료 {_pp(lab['slow'])}(실전 내내 조금씩 · 잰 것)" in text
    assert f"나머지 {_pp(lab['fast'])}(추정 · 놓친 주문 1건 · 10/13 15:58 UTC 매도)" in text
    assert f"합계 {_pp(lab['gap'])}" in text
    funded = [json.loads(json.dumps(r)) for r in recs]
    funded[-1]["nums"]["funding"] = -0.001
    again = b_series(funded)
    assert again["labels"]["funding"] is True and again["labels"]["slow"] == pytest.approx(-0.007)
    assert "체결가 차이 · 수수료 · 펀딩비 -0.7%p" in "\n".join(_chart_b_lines(funded))


def test_b_series_first_missed_is_the_earliest_and_count_follows_missed_n():
    """처음 후보 = 시각이 가장 이른 것(원본 dict 그대로), 건수 = missed_n(없으면 목록 길이), 건수 0이면 후보 없음."""
    recs = [record("trend_10_50", 22)]
    early = {"t": "2026-10-12T01:00:00+00:00", "symbol": "BTCUSDT", "side": "buy"}
    recs[0]["nums"].update(missed=[dict(MISSED), early], missed_n=2)
    lab = b_series(recs)["labels"]
    assert (lab["missed_n"], lab["first_missed"]) == (2, early)
    recs[0]["nums"].update(missed=[dict(MISSED)], missed_n=None)
    assert b_series(recs)["labels"]["missed_n"] == 1
    recs[0]["nums"].update(missed=[], missed_n=0)
    assert b_series(recs)["labels"]["first_missed"] is None


def test_b_series_unknown_part_is_none():
    """수수료나 체결가 차이 중 하나라도 모르면 그날 slow는 None(아는 쪽만 합치지 않음), 나머지 fast는 그대로, 펀딩비만 없으면 0."""
    a = record("trend_10_50", 21)
    a["nums"].update(fee=None, slip=-0.004, funding=None)
    b = record("trend_10_50", 22)
    b["nums"].update(fee=-0.002, slip=-0.004, funding=None)
    c = record("trend_10_50", 22)
    c["day"] = "2026-10-15"
    c["nums"].update(fee=-0.002, slip=-0.004, funding=-0.001, rest=None, gap=None)
    days = b_series([a, b, c])["days"]
    assert [(d["slow"], d["fast"]) for d in days] == [(None, -0.021), (pytest.approx(-0.006), -0.021), (pytest.approx(-0.007), None)]
    assert set(days[0]) == {"day", "slow", "fast", "gap", "start"} and days[0]["day"] == a["day"]
    assert days[2]["gap"] is None


def test_b_series_range_counts_gap_for_low_and_only_contributions_for_high():
    """low = min(0, gap들, 날마다 음수 기여 합), high = max(0, 날마다 양수 기여 합)(gap은 high에 안 듦)."""
    r = record("trend_10_50", 22)
    r["nums"].update(fee=-0.001, slip=0.0, rest=0.004, gap=0.003)
    s = b_series([r])
    assert (s["low"], s["high"]) == (-0.001, 0.004)
    r["nums"].update(fee=0.0, slip=0.0, rest=0.0, gap=-0.05)
    s = b_series([r])
    assert (s["low"], s["high"], s["empty"]) == (-0.05, 0.0, None)  # gap이 low를 늘리면 면적 없음이 아님


def _zeros(last=None, early=None, late=None):
    recs = [record("trend_10_50", k) for k in range(1, 23)]
    for i, r in enumerate(recs):
        r["nums"].update(fee=0.0, slip=0.0, funding=None, rest=0.0, gap=0.0, missed=[], missed_n=0)
        r["nums"].update((early or {}) if i < 11 else (late or {}))
    recs[-1]["nums"].update(last or {})
    return recs


def test_b_series_empty_kinds():
    """그릴 면적이 없을 때의 넷: none(차이를 낸 날 없음) · zero(마지막 날 두 기여를 앎) · zero_upto(마지막 확인일 seen_day) · unknown."""
    none = _zeros(early={"fee": None, "slip": None, "rest": None, "gap": None}, late={"fee": None, "slip": None, "rest": None, "gap": None})
    assert b_series(none)["empty"] == {"kind": "none", "seen_day": None}
    assert b_series(_zeros())["empty"] == {"kind": "zero", "seen_day": None}
    assert b_series(_zeros(early={"fee": None, "slip": None}))["empty"] == {"kind": "zero", "seen_day": None}  # 마지막 날을 알면 앞 날 모름은 상관없음
    upto = b_series(_zeros(last={"fee": None, "slip": -0.004, "rest": 0.0, "gap": None}))["empty"]
    assert upto == {"kind": "zero_upto", "seen_day": "2026-10-13"}
    unknown = b_series(_zeros(early={"fee": None, "slip": None, "gap": None}, late={"rest": None, "gap": None}))["empty"]
    assert unknown == {"kind": "unknown", "seen_day": None}


def test_b_series_upto_when_last_gap_missing():
    """마지막 날 합계(gap)를 모르면 gap을 마지막으로 확인한 날을 upto로, 라벨 gap은 그날 값. 마지막 날을 알면 upto는 None."""
    recs = [record("trend_10_50", k) for k in range(1, DAYS + 1)]
    assert b_series(recs)["labels"]["upto"] is None
    recs[-1]["nums"]["gap"] = None
    lab = b_series(recs)["labels"]
    assert (lab["gap"], lab["upto"]) == (recs[-2]["nums"]["gap"], recs[-2]["day"])
    for r in recs:
        r["nums"]["gap"] = None
    lab = b_series(recs)["labels"]
    assert (lab["gap"], lab["upto"]) == (None, None)


def test_b_series_marks_period_starts():
    """start = 앞 구간과 period_start가 달라진 첫 날(period_cuts 규칙: 대조 실패 날은 건너뜀, 첫 구간 시작은 아님)."""
    recs = _second_period([record("trend_10_50", k) for k in range(1, DAYS + 1)])
    recs[24]["nums"] = {}  # 대조 실패 날
    days = b_series(recs)["days"]
    assert [d["day"] for d in days if d["start"]] == ["2026-10-23"] and len(days) == len(recs)
    assert days[0]["start"] is False


def test_seen_matches_status_row():
    """seen(recs, "actual", True) = status._row의 「(M/D까지)」: 마지막 날 값이 있으면 날 None, 없으면 값이 있는 마지막 날, 하나도 없으면 (None, None)."""
    from blackclip.screen.status import _md, _pct, _row
    recs = [record("breakout_v2", k) for k in range(1, DAYS + 1)]  # 10/12부터 실제를 모름
    val, day = seen(recs, "actual", True)
    assert (day, val) == ("2026-10-11", recs[18]["nums"]["all"]["actual"])
    assert f"실제 {_pct(val)}({_md(day)}까지)" in _row("breakout_v2", recs, recs[-1]["day"])
    good = [record("trend_10_50", k) for k in range(1, DAYS + 1)]
    assert seen(good, "actual", True) == (good[-1]["nums"]["all"]["actual"], None)
    assert f"실제 {_pct(seen(good, 'actual', True)[0])} ·" in _row("trend_10_50", good, good[-1]["day"])
    for r in recs:
        r["nums"]["all"]["actual"] = r["nums"]["actual"] = None
    assert seen(recs, "actual", True) == (None, None) and "실제 모름" in _row("breakout_v2", recs, recs[-1]["day"])


def test_seen_whole_flag_picks_all_or_period_value_and_stays_in_the_period():
    """whole=True는 nums.all(없으면 구간 값), False는 nums 구간 값. 이번 구간 안에서만 찾는다."""
    recs = _second_period([record("trend_10_50", k) for k in range(1, DAYS + 1)])
    last = recs[-1]
    assert seen(recs, "actual", True) == (last["nums"]["all"]["actual"], None)  # 전체 값은 첫 구간 복사
    assert seen(recs, "actual", False) == (last["nums"]["actual"], None) and last["nums"]["actual"] != last["nums"]["all"]["actual"]
    recs[-1]["nums"]["slip"] = None
    val, day = seen(recs, "slip", False)
    assert (val, day) == (recs[-2]["nums"]["slip"], recs[-2]["day"])
    for r in recs[30:]:
        r["nums"]["slip"] = None
    assert seen(recs, "slip", False) == (None, None)  # 앞 구간(첫 30일)의 값은 보지 않음


BAD_TIME = "0001-01-01T00:00:00+01:00"
"""모양 검사는 통과하지만 UTC로 바꿀 수 없는 시각."""


def test_b_series_none_and_zero_do_not_convert_the_missed_time():
    """그릴 면적이 없는 none · zero는 라벨을 쓰지 않으므로 UTC로 바꿀 수 없는 놓친 주문 시각이 있어도 오류 없이 값을 돌려준다(first_missed는 None)."""
    bad = [{"t": BAD_TIME, "symbol": "BTCUSDT", "side": "sell"}]
    none = _zeros(early={"fee": None, "slip": None, "rest": None, "gap": None}, late={"fee": None, "slip": None, "rest": None, "gap": None},
                  last={"missed": bad, "missed_n": 1})
    s = b_series(none)
    assert (s["empty"]["kind"], s["low"], s["high"], s["labels"]["first_missed"], s["labels"]["missed_n"]) == ("none", None, None, None, 1)
    s = b_series(_zeros(last={"missed": bad, "missed_n": 1}))
    assert (s["empty"]["kind"], s["labels"]["first_missed"], s["labels"]["missed_n"]) == ("zero", None, 1)


@pytest.mark.parametrize("last", [
    {},  # 면적이 있는 보통 경우
    {"fee": None, "slip": -0.004, "rest": 0.0, "gap": None},  # zero_upto: 라벨을 보임
])
def test_b_series_label_path_raises_workspace_error_for_unconvertible_missed_time(last):
    """라벨을 쓰는 경로(면적이 있음 · zero_upto · unknown)에서는 변환 못 하는 시각이 WorkspaceError 한 가지로 올라온다."""
    recs = _zeros(last={**last, "missed": [{"t": BAD_TIME, "symbol": "BTCUSDT", "side": "sell"}], "missed_n": 1}) if last else \
        [record("trend_10_50", k) for k in range(1, DAYS + 1)]
    if not last:
        recs[-1]["nums"]["missed"] = [{"t": BAD_TIME, "symbol": "BTCUSDT", "side": "sell"}]
    with pytest.raises(WorkspaceError, match="UTC로 바꾸지 못했습니다"):
        b_series(recs)

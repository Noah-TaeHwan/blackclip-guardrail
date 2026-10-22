"""기계 판독 일별 기록 days.jsonl: 모양 · 날마다 마지막 줄 · 실패한 날 · 여섯 가지 입력."""
import copy
import json
import os
from dataclasses import replace
from datetime import date

import pytest
from facts import NAMES, guardrail, record, write_facts

from blackclip.daily.days import DAYS, LINE_CAP, append_day, current_period, read_days, valid
from blackclip.daily.reconcile import Day, reconcile
from blackclip.states import TRACKED
from blackclip.workspace import WorkspaceError, guardrail_problem


@pytest.fixture(autouse=True)
def _home(monkeypatch, tmp_path):
    monkeypatch.setenv("HOME", str(tmp_path / "home"))


def _path(root, name="trend_10_50"):
    return root / "strategies" / name / "daily" / DAYS


def _day_of(rec, **over):
    """지어낸 기록 한 줄에서 Day를 만든다(over로 칸을 바꿈)."""
    args = dict(strategy=rec["strategy"], day=date.fromisoformat(rec["day"]), live_day=rec["live_day"], trades=rec["trades"],
                head=rec["head"], states=rec["states"], lines=rec["lines"], response=rec["response"], week=None,
                nums=rec["nums"])
    return Day(**{**args, **over})


def _last_raw(root, name="grid_v1"):
    return _path(root, name).read_text(encoding="utf-8").splitlines()[-1]


def test_facts_lines_are_engine_shape():
    """지어낸 기록이 엔진 검사를 그대로 통과(시험이 엔진과 같은 모양을 씀)."""
    for n in NAMES:
        for k in range(1, 23):
            assert valid(record(n, k)) == record(n, k)


def test_read_days_facts_last_line(tmp_path):
    root = write_facts(tmp_path)
    recs = read_days(root, "trend_10_50")
    assert len(recs) == 22 and recs[-1]["day"] == "2026-10-14"
    n = recs[-1]["nums"]
    assert (n["actual"], n["bt"], n["gap"], n["fee"], n["slip"], n["rest"]) == (-0.046, -0.019, -0.027, -0.002, -0.004, -0.021)
    assert (recs[-1]["trades"], n["missed_n"], n["room"], n["pos"]) == (37, 1, 0.026, "아래")
    assert recs[-1]["rep"] == "경고" and recs[-1]["title"] == "실행 문제로 추정"
    assert [r["day"] for r in current_period(recs)] == [r["day"] for r in recs]


def test_read_days_keeps_last_line_per_day(tmp_path):
    root = write_facts(tmp_path, ("grid_v1",))
    again = {**record("grid_v1", 22), "trades": 13}
    with _path(root, "grid_v1").open("a") as f:
        f.write(json.dumps(again, ensure_ascii=False) + "\n")
    recs = read_days(root, "grid_v1")
    assert len(recs) == 22 and recs[-1]["trades"] == 13


@pytest.mark.parametrize("bad", [
    lambda r: {**r, "trades": True},
    lambda r: {**r, "nums": {**r["nums"], "actual": 10 ** 400}},
    lambda r: {**r, "nums": {**r["nums"], "actual": float("nan")}},
    lambda r: {**r, "nums": {**r["nums"], "missed": "x"}},
    lambda r: {**r, "states": {"전략": "정상"}},
    lambda r: {**r, "lines": []},
    lambda r: {**r, "day": "10/14"},
    lambda r: {**r, "strategy": "../x"},
    lambda r: [r],
])
def test_valid_refuses_wrong_shapes(bad):
    assert valid(bad(record("trend_10_50", 22))) is None


def test_read_days_skips_bad_lines(tmp_path):
    """깊은 JSON · 4,300자리 넘는 정수 · 잘린 끝 줄 · 모양 다른 줄은 그 줄만 빠지고 앞날이 마지막이 됨."""
    root = write_facts(tmp_path, ("grid_v1",))
    with _path(root, "grid_v1").open("a") as f:
        f.write("[" * 200_000 + "\n" + '{"v": ' + "9" * 5000 + "}\n" + '["x"]\n' + '{"v": 1, "strategy": "grid')
    recs = read_days(root, "grid_v1")
    assert len(recs) == 22 and recs[-1]["day"] == "2026-10-14"


@pytest.mark.parametrize("text", ["빈 파일", "공백 줄만"])
def test_read_days_blank_file_is_no_record(tmp_path, text):
    """없는 파일 · 빈 파일 · 공백 줄만 있는 파일은 「기록 없음」([])이다."""
    root = write_facts(tmp_path, ("grid_v1",))
    _path(root, "grid_v1").write_text("" if text == "빈 파일" else " \n\t\n\n  \n", encoding="utf-8")
    assert read_days(root, "grid_v1") == []
    _path(root, "grid_v1").unlink()
    assert read_days(root, "grid_v1") == []


@pytest.mark.parametrize("body", [
    "깨진 줄\n{ not json\n" + "[" * 200_000 + "\n",  # (a) JSON으로 풀리지 않는 줄만: read_jsonl이 건너뛰어 []와 구분이 안 됨
    '{"v": 1, "day": [1]}\n["x"]\n3\n',  # (b) JSON은 풀리지만 valid를 하나도 못 지나는 줄만
    '{"v": 1, "day": [1]}\n\n  \n{ broken',  # 둘이 섞이고 빈 줄이 낀 경우
], ids=["not_json", "wrong_shape", "mixed"])
def test_read_days_file_with_no_readable_line_is_an_error(tmp_path, body):
    """빈 줄 아닌 줄이 있는데 읽을 수 있는 줄이 0개면 「기록 없음」이 아니라 WorkspaceError(전략 폴더 · 이름 포함)."""
    root = write_facts(tmp_path, ("grid_v1",))
    _path(root, "grid_v1").write_text(body, encoding="utf-8")
    with pytest.raises(WorkspaceError) as exc:
        read_days(root, "grid_v1")
    assert str(exc.value) == f"{_path(root, 'grid_v1')}: days.jsonl에 읽을 수 있는 줄이 없습니다(깨졌거나 모양이 다른 줄만 있음)"


def test_read_days_partly_broken_file_keeps_good_lines(tmp_path):
    """줄 하나라도 읽히면 오류가 아니다 — 깨진 줄만 건너뛰고 좋은 줄은 그대로(기존 계약)."""
    root = write_facts(tmp_path, ("grid_v1",))
    good = record("grid_v1", 1)
    _path(root, "grid_v1").write_text("깨진 줄\n" + json.dumps({"v": 1, "day": [1]}) + "\n" + json.dumps(good) + "\n", encoding="utf-8")
    assert [r["day"] for r in read_days(root, "grid_v1")] == [good["day"]]


def test_read_days_refuses_link_fifo_and_huge(tmp_path):
    root = write_facts(tmp_path, ("grid_v1", "trend_10_50"))
    p = _path(root, "grid_v1")
    outside = tmp_path / "outside.jsonl"
    outside.write_text(p.read_text())
    p.unlink()
    p.symlink_to(outside)
    with pytest.raises(WorkspaceError):
        read_days(root, "grid_v1")
    p.unlink()
    os.mkfifo(p)
    with pytest.raises(WorkspaceError):
        read_days(root, "grid_v1")
    q = _path(root, "trend_10_50")
    with q.open("a") as f:
        f.write(" " * (8 << 20))
    with pytest.raises(WorkspaceError, match="바이트를 넘어"):
        read_days(root, "trend_10_50")
    assert read_days(root, "nope") == []


def test_read_days_refuses_parent_folder_link(tmp_path):
    """daily/ 폴더 자체가 레포 밖 링크면 읽지 않음."""
    root = write_facts(tmp_path, ("grid_v1",))
    d = root / "strategies" / "grid_v1" / "daily"
    d.rename(tmp_path / "moved")
    d.symlink_to(tmp_path / "moved", target_is_directory=True)
    with pytest.raises(WorkspaceError, match="상위 폴더가 링크"):
        read_days(root, "grid_v1")


def test_valid_rejects_unknown_state_name():
    """상태 값은 글이기만 하면 되는 게 아니라 ORDER의 이름(괄호 설명은 붙어도 됨)이어야 한다. facts 가드레일도 계약을 지킴."""
    from facts import NAMES, guardrail, record
    from blackclip.daily.days import valid
    from blackclip.workspace import guardrail_problem
    assert all(guardrail_problem(guardrail(n)) is None for n in NAMES)
    r = record("grid_v1", 22)
    assert valid(r) is not None
    r["states"]["전략"] = "좋음"
    assert valid(r) is None


def test_valid_accepts_thesis_not_applicable_only_in_thesis():
    """논지 없는 전략의 정상 값 「해당 없음」은 논지 칸에서만 받는다(다른 칸이면 거절)."""
    from facts import record
    from blackclip.daily.days import valid
    from blackclip.daily.reconcile import thesis_cause
    assert thesis_cause("논지 없음", None)[0] == "해당 없음"
    r = record("grid_v1", 22)
    r["states"]["논지"] = "해당 없음"
    assert valid(r) is not None
    r["states"]["전략"] = "해당 없음"
    assert valid(r) is None


def test_failed_day_is_kept_in_days_jsonl(tmp_path):
    """정상 하루 다음 날 대조가 예외로 끝나면(run_daily의 9인자 Day, nums={}) 그 줄이 유효하게 남고 읽힌다(status 화면 단언은 test_status.py)."""
    root = write_facts(tmp_path, ("grid_v1",))
    why = "확인 불가(대조하지 못함 · KeyError)"
    failed = Day("grid_v1", date(2026, 10, 15), 23, 0, "실제 모름 · 같은 기간 BT 모름", {k: why for k in TRACKED},
                 {k: "blackclip daily 출력 · 연결 파일 확인" for k in TRACKED}, {}, None)
    append_day(root, failed)
    recs = read_days(root, "grid_v1")
    assert recs[-1]["day"] == "2026-10-15" and recs[-1]["nums"] == {} and recs[-1]["rep"] == why
    assert recs[-1]["head"] == "실제 모름 · 같은 기간 BT 모름" and recs[-1]["live_day"] == 23 and recs[-1]["trades"] == 0
    assert json.loads(_path(root, "grid_v1").read_text().splitlines()[-1])["nums"] == {}


def test_valid_bounds_ints_before_isfinite():
    """4,300자리 아래의 큰 정수(10**400 같은)도 OverflowError 없이 거절."""
    r = record("trend_10_50", 22)
    assert valid({**r, "nums": {**r["nums"], "all": {**r["nums"]["all"], "actual": 10 ** 400}}}) is None
    assert valid({**r, "nums": {}}) is not None  # nums 칸은 모두 선택


def _bad_more():
    r = record("trend_10_50", 22)
    n, one = r["nums"], r["nums"]["missed"][0]
    return [
        {**r, "v": True}, {**r, "v": 2}, {**r, "nums": None}, {**r, "head": "가" * 4001}, {**r, "response": {"a": 1}},
        {**r, "response": {"execution_warning": "가" * 4001}}, {**r, "response": {"가" * 4001: "x"}},
        {**r, "nums": {**n, "d": -1}}, {**r, "nums": {**n, "d": 1.5}}, {**r, "nums": {**n, "actual": 2e6}},
        {**r, "nums": {**n, "period_start": "10/14"}}, {**r, "nums": {**n, "all": {**n["all"], "start": "x"}}},
        {**r, "nums": {**n, "missed": [one] * 51}}, {**r, "nums": {**n, "missed": [{"t": one["t"], "symbol": "BTCUSDT"}]}},
        {**r, "states": {**r["states"], "봇": "정상" * 3000}},
    ]


@pytest.mark.parametrize("i", range(len(_bad_more())))
def test_valid_refuses_more_wrong_shapes(i):
    """버전 · 필수 칸 · 글자 수 · 범위 · 날짜 · 놓친 주문 모양이 틀리면 그 줄만 거절."""
    assert valid(_bad_more()[i]) is None


def test_valid_keeps_unknown_extra_keys():
    """나중 작업이 줄에 칸을 더해도(선택 칸) 모르는 칸은 그대로 통과."""
    r = record("grid_v1", 22)
    assert valid({**r, "later": {"x": 1}}) is not None


def test_append_day_refuses_nan_and_links(tmp_path):
    """NaN은 쓰지 않고(ValueError), days.jsonl 자리가 링크면 대상을 건드리지 않는다(WorkspaceError)."""
    root = write_facts(tmp_path, ("grid_v1",))
    st, ln = {k: "정상" for k in TRACKED}, {k: "근거" for k in TRACKED}
    before = _path(root, "grid_v1").read_text(encoding="utf-8")
    with pytest.raises(ValueError):
        append_day(root, Day("grid_v1", date(2026, 10, 15), 23, 0, "h", st, ln, {}, None, {"actual": float("nan")}))
    assert _path(root, "grid_v1").read_text(encoding="utf-8") == before
    p = _path(root, "grid_v1")
    outside = tmp_path / "outside.jsonl"
    outside.write_text("keep\n")
    p.unlink()
    p.symlink_to(outside)
    with pytest.raises(WorkspaceError):
        append_day(root, Day("grid_v1", date(2026, 10, 15), 23, 0, "h", st, ln, {}, None))
    assert outside.read_text() == "keep\n"


def test_append_day_text_over_4000_stays_readable(tmp_path):
    """글 칸 하나가 4,001자여도 읽기 계약(4,000자)에 맞춰 잘라 쓰므로, 쓴 하루가 read_days에서 사라지지 않는다."""
    root = write_facts(tmp_path, ("grid_v1",))
    rec = record("grid_v1", 22)
    ln = {k: "근거" for k in TRACKED}
    ln["실행"] = "가" * 4001
    append_day(root, _day_of(rec, day=date(2026, 10, 15), lines=ln, head="나" * 4001))
    last = read_days(root, "grid_v1")[-1]
    assert last["day"] == "2026-10-15" and len(last["lines"]["실행"]) == 4000 and len(last["head"]) == 4000


def test_append_day_long_response_fits_the_line_cap(tmp_path):
    """한글 30,000자 response도 4,000자로 잘려 줄이 LINE_CAP 이하이고 valid가 받는다(response 면제 없음)."""
    root = write_facts(tmp_path, ("grid_v1",))
    rec = record("grid_v1", 22)
    append_day(root, _day_of(rec, day=date(2026, 10, 15), response={"execution_warning": "가" * 30_000}))
    raw = _last_raw(root)
    got = json.loads(raw)
    assert len(raw.encode("utf-8")) <= LINE_CAP and valid(got) is not None
    assert len(got["response"]["execution_warning"]) == 4000


def test_append_day_bounded_fields_plus_full_missed_end_under_the_cap(tmp_path):
    """글 칸이 모두 4,000자 이하여도 놓친 주문 목록까지 합치면 넘는 줄은 lines를 자르고 그래도 넘으면 missed를 비워 LINE_CAP 이하로 쓴다."""
    root = write_facts(tmp_path, ("grid_v1",))
    rec = record("grid_v1", 22)
    missed = [{"t": "2026-10-13T15:58:00+00:00", "symbol": "BTCUSDT", "side": "sell"}] * 50
    n = copy.deepcopy(rec["nums"])
    n["missed"], n["all"]["missed"], n["missed_n"] = missed, missed, 50
    ln = {k: "😀" * 4000 for k in TRACKED}
    append_day(root, _day_of(rec, day=date(2026, 10, 15), head="😀" * 4000, lines=ln, nums=n,
                             response={"execution_warning": "a" * 4000}))
    raw = _last_raw(root)
    got = json.loads(raw)
    assert len(raw.encode("utf-8")) <= LINE_CAP and valid(got) is not None
    assert all(len(v) == 2000 for v in got["lines"].values())
    assert got["nums"]["missed"] == [] and got["nums"]["all"]["missed"] == [] and got["nums"]["missed_n"] == 50  # 개수는 남음


def test_append_day_shrinks_lines_before_missed(tmp_path):
    """lines만 줄여도 LINE_CAP 안이면 놓친 주문 목록은 그대로 둔다(줄이는 순서: lines → missed)."""
    root = write_facts(tmp_path, ("grid_v1",))
    rec = record("grid_v1", 22)
    missed = [{"t": "2026-10-13T15:58:00+00:00", "symbol": "BTCUSDT", "side": "sell"}] * 50
    n = copy.deepcopy(rec["nums"])
    n["missed"], n["all"]["missed"] = missed, missed
    append_day(root, _day_of(rec, day=date(2026, 10, 15), head="가" * 4000, lines={k: "가" * 4000 for k in TRACKED}, nums=n))
    raw = _last_raw(root)
    got = json.loads(raw)
    assert len(raw.encode("utf-8")) <= LINE_CAP and valid(got) is not None
    assert all(len(v) == 2000 for v in got["lines"].values()) and len(got["nums"]["missed"]) == 50


def test_append_day_refuses_a_line_that_cannot_be_read_back(tmp_path):
    """줄여도 LINE_CAP을 넘거나 valid가 거절할 줄은 쓰지 않고 WorkspaceError(파일은 그대로)."""
    root = write_facts(tmp_path, ("grid_v1",))
    rec = record("grid_v1", 22)
    before = _path(root, "grid_v1").read_text(encoding="utf-8")
    fat = {k: "정상(" + "😀" * 3990 + ")" for k in TRACKED}  # 상태 글은 줄일 수 없어 5 × 16KB가 남음
    with pytest.raises(WorkspaceError, match="기록하지 않습니다"):
        append_day(root, _day_of(rec, day=date(2026, 10, 15), states=fat))
    with pytest.raises(WorkspaceError, match="기록하지 않습니다"):
        append_day(root, _day_of(rec, day=date(2026, 10, 15), states={**rec["states"], "전략": "좋음"}))
    assert _path(root, "grid_v1").read_text(encoding="utf-8") == before


def test_engine_long_thesis_day_stays_readable(tmp_path):
    """유효한 가드레일의 4,001자 논지 규칙으로 reconcile이 4,000자 넘는 근거를 내도 그 하루가 읽힌다."""
    from test_daily_reconcile import kw
    g = guardrail("grid_v1")
    g["thesis"]["rule"] = "가" * 4001
    assert guardrail_problem(g) is None
    day = reconcile("grid_v1", g, **kw(side_days=61))
    assert len(day.lines["논지"]) > 4000
    root = write_facts(tmp_path, ("grid_v1",))
    append_day(root, replace(day, day=date(2026, 10, 15)))
    last = read_days(root, "grid_v1")[-1]
    assert last["day"] == "2026-10-15" and len(last["lines"]["논지"]) == 4000


def _failed(day: str, k: int = 22) -> dict:
    """대조가 예외로 끝난 날의 줄(nums={})."""
    return {**record("grid_v1", k), "day": day, "nums": {}}


def test_current_period_keeps_failed_days_of_the_period():
    """정상 날 뒤 마지막 날이 대조 실패(nums={})여도 이번 구간의 정상 날 + 구간 시작 뒤의 실패한 날이 날짜순으로 나온다."""
    old = {**record("grid_v1", 3), "day": "2026-08-30", "nums": {**record("grid_v1", 3)["nums"], "period_start": "2026-08-24"}}
    recs = [old, _failed("2026-09-01", 3), record("grid_v1", 20), _failed("2026-10-13", 21), record("grid_v1", 22),
            _failed("2026-10-15")]
    assert [r["day"] for r in current_period(recs)] == ["2026-10-12", "2026-10-13", "2026-10-14", "2026-10-15"]
    assert [r["day"] for r in current_period(recs[:2])] == ["2026-08-30", "2026-09-01"]  # 옛 구간도 같은 규칙


def test_current_period_with_only_failed_days_returns_them_all():
    """period_start가 있는 줄이 하나도 없으면 실패한 줄을 모두 돌려주고, 빈 목록은 빈 목록."""
    recs = [_failed("2026-10-14"), _failed("2026-10-15")]
    assert current_period(recs) == recs and current_period([]) == []


@pytest.mark.parametrize("key,bad", [("trade_rets", "x"), ("trade_rets", [0.1, "x"]), ("trade_rets", [True]), ("trade_rets", [10 ** 7]),
                                      ("trade_mean", "x"), ("trade_n", -1), ("trade_n", 1.5), ("trade_skip", True),
                                      ("trade_n", 36)])  # 마지막은 trade_n < len(trade_rets)(37개)
def test_valid_refuses_wrong_trade_fields(key, bad):
    """거래 수익 선택 칸은 없어도 되지만 있으면 모양이 맞아야 한다(틀린 줄은 다른 칸과 같이 버림)."""
    r = record("trend_10_50", 22)
    assert valid(r) is not None and valid({**r, "nums": {**r["nums"], key: bad}}) is None
    assert valid({**r, "nums": {k: v for k, v in r["nums"].items() if not k.startswith("trade_")}}) is not None


def test_guardrail_problem_checks_optional_trade_percentiles():
    """basis.trade_p10 · p90 · trade_mean은 예전 가드레일에 없어도 통과, 있으면 숫자 또는 None만."""
    g = guardrail("trend_10_50")
    assert guardrail_problem(g) is None
    del g["basis"]["trade_p10"], g["basis"]["trade_p90"]
    assert guardrail_problem(g) is None and guardrail_problem({k: v for k, v in g.items() if k != "basis"}) is None
    for key, bad, why in (("trade_p10", "x", "basis.trade_p10"), ("trade_p90", True, "basis.trade_p90"),
                          ("trade_mean", [1], "basis.trade_mean")):
        assert guardrail_problem({**g, "basis": {**g["basis"], key: bad}}) == why
    assert guardrail_problem({**g, "basis": {**g["basis"], "trade_p10": None}}) is None
    assert guardrail_problem({**g, "basis": [1]}) == "basis"


def test_reconcile_trade_nums_follow_the_period_and_the_2000_cap(tmp_path):
    """nums.trade_*는 이번 구간(31일째부터) 청산만, 표본 전체 수는 2,000개 기록 상한을 넘어도 세고,
    실전 전 포지션 청산(trips에 없음)은 trade_skip. 그 줄은 LINE_CAP 안에 써지고 다시 읽힌다."""
    from datetime import timedelta
    from decimal import Decimal

    import pandas as pd
    from test_daily_reconcile import LIVE, guard, kw

    from blackclip.daily.actual import Actual
    from blackclip.daily.days import TRADE_KEEP
    from blackclip.daily.live import LiveRun
    end = LIVE + timedelta(days=45)
    old = [(LIVE + timedelta(days=1, hours=i), 0.01) for i in range(3)]                          # 첫 구간
    new = [(LIVE + timedelta(days=31, minutes=i), -0.123456 + i * 1e-9) for i in range(TRADE_KEEP + 3)]  # 이번 구간
    exits = [t for t, _ in old] + [LIVE + timedelta(days=30, hours=1)] + [t for t, _ in new]       # 이번 구간 청산 2,004 = 표본 2,003 + 뺀 1
    act = Actual(pd.Series([0.0, 0.0], index=pd.DatetimeIndex([LIVE, end])), sorted(exits), None, Decimal(10000), [*old, *new])
    idx = pd.date_range(LIVE, end - timedelta(days=1), freq="D")
    day = reconcile("s1", guard(), **kw(end=end, actual=act, live=LiveRun({}, pd.Series(0.0, index=idx), pd.Series(0.0, index=idx), [])))
    n = day.nums
    assert len(n["trade_rets"]) == TRADE_KEEP and n["trade_rets"] == [round(r, 6) for _, r in new][-TRADE_KEEP:]
    assert (n["trade_n"], n["trade_skip"]) == (TRADE_KEEP + 3, 1) and day.trades == len(exits)
    assert n["trade_mean"] == round(sum(n["trade_rets"]) / TRADE_KEEP, 6)
    root = write_facts(tmp_path, ("grid_v1",))
    append_day(root, Day("grid_v1", date(2026, 11, 6), 46, day.trades, day.head, day.states, day.lines, day.response, None, n))
    got = read_days(root, "grid_v1")[-1]
    assert len(_last_raw(root).encode("utf-8")) <= LINE_CAP and got["nums"]["trade_rets"] == n["trade_rets"]
    nope = reconcile("s1", guard(), **kw(end=end)).nums  # 실제 손익을 모르면 칸이 모두 None
    assert (nope["trade_rets"], nope["trade_mean"], nope["trade_n"], nope["trade_skip"]) == (None, None, None, None)


@pytest.mark.parametrize("bad", [0, -1])
def test_guardrail_problem_refuses_non_positive_min_trades(bad):
    """min_trades는 1 이상 정수(0 · 음수면 chart E의 30건 선 자리가 없음)."""
    g = guardrail("trend_10_50")
    g["judgment_hold"]["min_trades"] = bad
    assert guardrail_problem(g) == "judgment_hold.min_trades"
    g["judgment_hold"]["min_trades"] = 1
    assert guardrail_problem(g) is None

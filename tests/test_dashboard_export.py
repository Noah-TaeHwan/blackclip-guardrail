"""blackclip dashboard 내보내기: `GET /export.csv` · `/export.json`.

CSV 수식 주입(OWASP CSV Injection) · UTF-8 BOM · 모양이 틀린 선택 칸 · 절대 경로 없음 · 머리글 · 파일 이름.
숫자는 홈 줄(`dashboard.home_rows`)과 같은 기록에서 나온다는 것도 여기서 묶는다.
"""
import csv
import http.client
import io
import json
import shutil
import threading
from contextlib import contextmanager
from pathlib import Path

import pytest
from facts import NAMES, record, write_facts
from test_dashboard_pages import break_repo, edit_guardrail, edit_last, stop
from webrules import copy_violations

from blackclip import dashboard
from blackclip.connect import HEADERS
from blackclip.record import BROKEN
from blackclip.screen import status, web

BOM = b"\xef\xbb\xbf"


@pytest.fixture(autouse=True)
def _home(monkeypatch, tmp_path):
    monkeypatch.setenv("HOME", str(tmp_path / "home"))


@contextmanager
def served(root: Path):
    """포트 0 서버를 띄운다. 끝에 shutdown · server_close.

    Args:
        root: .blackclip 경로.

    Yields:
        서버(`server.token`에 접속 토큰).
    """
    server = dashboard.make_server(root, 0)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    try:
        yield server
    finally:
        server.shutdown()
        server.server_close()


def fetch(root: Path, path: str, method: str = "GET", token: bool = True) -> tuple[int, dict, bytes]:
    """요청 하나 → (상태, 머리글, 본문 바이트).

    Args:
        root: .blackclip 경로.
        path: `/export.csv` 같은 주소.
        method: GET 또는 HEAD.
        token: False면 접속 토큰을 붙이지 않는다.

    Returns:
        (상태, 머리글 dict, 본문 바이트).
    """
    with served(root) as s:
        c = http.client.HTTPConnection("127.0.0.1", s.server_address[1], timeout=5)
        c.request(method, f"{path}?t={s.token}" if token else path)
        r = c.getresponse()
        out = r.status, dict(r.getheaders()), r.read()
        c.close()
        return out


def parse_csv(body: bytes) -> list[dict]:
    """CSV 본문(BOM 포함)을 칸 이름 dict 목록으로.

    Args:
        body: 내보내기 본문.

    Returns:
        줄마다 {칸: 글}.
    """
    return list(csv.DictReader(io.StringIO(body.decode("utf-8-sig"), newline="")))


def by_name(rows: list[dict]) -> dict[str, dict]:
    """strategy 칸으로 줄을 찾기 쉽게 한다.

    Args:
        rows: 줄 목록.

    Returns:
        이름 → 줄.
    """
    return {r["strategy"]: r for r in rows}


def same_value(csv_cell: str, json_value) -> bool:
    """CSV 칸 글과 JSON 값이 같은 값인지(빈칸 = null, 숫자는 숫자로, 불리언은 True · False 글).

    Args:
        csv_cell: CSV 칸 글.
        json_value: JSON 값.

    Returns:
        같으면 True.
    """
    if json_value is None:
        return csv_cell == ""
    if isinstance(json_value, bool):
        return csv_cell == str(json_value)
    if isinstance(json_value, (int, float)):
        return float(csv_cell) == json_value
    return csv_cell == json_value


@pytest.fixture
def export_repo(tmp_path, monkeypatch):
    """FACTS 네 전략 + 은퇴한 old_v1(trend_10_50 복사). trend_10_50 가드레일에 cost · c3 · set_on을 더하고, grid_v1은 비용 0 백테스트가 다른 한 줄.

    Returns:
        .blackclip 경로.
    """
    repo = tmp_path / "repo"
    root = write_facts(repo)
    shutil.copytree(root / "strategies" / "trend_10_50", root / "strategies" / "old_v1")
    stop(repo, monkeypatch, "old_v1", "은퇴")
    edit_guardrail(root, "trend_10_50", lambda g: g.update(
        cost={"one_way_bp": 30, "note": "x"}, set_on="2026-09-22",
        c3={"sharpe_cost0": 1.25, "adjusted_low": 0.4, "trials": 100, "unverified": True}))

    def cost(rec):  # bt != bt0 (보수적 비용과 비용 0이 다른 기록)
        for nums in (rec["nums"], rec["nums"]["all"]):
            nums.update(actual=-0.008, bt0=0.002, bt=-0.001, gap=-0.010)

    edit_last(root, "grid_v1", cost)
    return root


def test_export_fields_and_numbers(export_repo):
    """행 = 전략 하나(실전 넷 + 끈 하나, 이름순), 칸 순서 고정, 숫자는 비율 그대로, JSON과 CSV가 같은 값, 차이는 실제 − 비용 0 백테스트."""
    root = export_repo
    status_code, _, csv_body = fetch(root, "/export.csv")
    assert status_code == 200 and csv_body.startswith(BOM) and not csv_body.startswith(BOM + BOM)
    status_code, _, json_body = fetch(root, "/export.json")
    assert status_code == 200 and not json_body.startswith(BOM)
    doc = json.loads(json_body)
    assert list(doc) == ["format", "version", "day", "fields", "strategies"]
    assert doc["format"] == "blackclip-guardrail-export" and doc["version"] == 1 and doc["day"] == "2026-10-14"
    assert list(doc["fields"]) == list(dashboard.EXPORT_FIELDS) and all(doc["fields"].values())
    rows = doc["strategies"]
    assert [r["strategy"] for r in rows] == sorted([*NAMES, "old_v1"]) and all(list(r) == list(dashboard.EXPORT_FIELDS) for r in rows)
    flat = parse_csv(csv_body)
    assert list(flat[0]) == list(dashboard.EXPORT_FIELDS) and [r["strategy"] for r in flat] == [r["strategy"] for r in rows]
    for c, j in zip(flat, rows, strict=True):
        assert all(same_value(c[k], j[k]) for k in dashboard.EXPORT_FIELDS), j["strategy"]
    got = by_name(rows)
    t = got["trend_10_50"]  # 비율 숫자 그대로(-4.6%가 아니라 -0.046)
    assert (t["state"], t["day"], t["until"], t["live_day"], t["trades"]) == ("경고", "2026-10-14", "2026-10-14", 22, 37)
    assert (t["actual"], t["bt"], t["bt0"], t["gap"]) == (-0.046, -0.019, -0.019, -0.027)
    assert t["reason"] == "실행 문제로 추정"  # 칩 이유 한 줄(원인 이름)
    assert (t["guardrail_version"], t["set_on"], t["live_start"]) == (1, "2026-09-22", "2026-09-23")
    assert (t["cost_one_way_bp"], t["normal_low"], t["normal_high"], t["warning_line"]) == (30.0, -0.037, 0.081, -0.072)
    assert (t["c3_sharpe_cost0"], t["c3_adjusted_low"], t["c3_trials"], t["c3_unverified"]) == (1.25, 0.4, 100, True)
    g = got["grid_v1"]  # gap은 actual − bt0(비용 0)이지 actual − bt가 아니다
    assert (g["actual"], g["bt"], g["bt0"], g["gap"]) == (-0.008, -0.001, 0.002, -0.010)
    assert g["gap"] == round(g["actual"] - g["bt0"], 6) != round(g["actual"] - g["bt"], 6)
    assert got["grid_v1"]["cost_one_way_bp"] is None and got["grid_v1"]["c3_trials"] is None  # FACTS 가드레일에는 cost · c3 · set_on이 없다
    o = got["old_v1"]  # 끈 전략: 끈 상태, 숫자 칸은 홈의 끈 표처럼 비움, 가드레일 칸은 읽히면 채움
    assert (o["state"], o["reason"], o["day"], o["actual"], o["gap"]) == ("은퇴", None, None, None, None)
    assert o["guardrail_version"] == 1 and o["warning_line"] == -0.072


def test_export_numbers_equal_home_rows(export_repo):
    """홈 줄과 내보내기 줄이 같은 기록에서 나온다: FACTS 전략마다 상태 · 이유 · 날 · 거래 · 실제 · 백테스트 · 차이가 같다."""
    root = export_repo
    rows, off, latest = dashboard.home_rows(root)
    out = by_name(dashboard.export_rows(root))
    assert {r["name"] for r in rows} == set(NAMES) and [o["name"] for o in off] == ["old_v1"]
    for r in rows:
        e = out[r["name"]]
        assert (e["state"], e["reason"], e["day"], e["trades"]) == (r["word"], r["reason"], r["day"], r["trades"]), r["name"]
        assert (e["actual"], e["bt"], e["gap"], e["bt0"], e["live_day"]) == (r["actual"], r["bt"], r["gap"], r["bt0"], r["live_day"]), r["name"]
        assert e["until"] == (r["until"] or r["day"]), r["name"]  # 홈이 「(날까지)」를 붙이지 않는 줄도 숫자의 날을 채운다
    assert max(e["day"] for e in out.values() if e["day"]) == latest
    # breakout_v2는 마지막 날 실제를 몰라 홈이 (10-11까지)를 붙인다 → 실제 · 백테스트 · 비용 0 · 차이 모두 그 날(k=19) 기록의 값
    b, same_day, last_day = out["breakout_v2"], record("breakout_v2", 19)["nums"], record("breakout_v2", 22)["nums"]
    assert b["day"] == "2026-10-14" and b["actual"] == same_day["actual"] == 0.004
    assert b["bt"] == same_day["bt"] != last_day["bt"] and b["bt0"] == same_day["bt0"] and b["gap"] == same_day["gap"]
    assert status._whole(same_day, "bt0") == b["bt0"]


def test_export_until_names_the_day_of_the_numbers(export_repo):
    """`until` = actual · bt · bt0 · gap이 가리키는 기록 날. 마지막 날과 같아도 그 날을 채우고, 기록이 없으면 비운다.

    기대값은 손으로 치지 않고 FACTS `record()`와 `home_rows`에서 끌어 온다. `breakout_v2`는 마지막 날(10-14)에 실제를 몰라 10-11 기록의 숫자를 낸다.
    """
    root = export_repo
    rows = by_name(dashboard.export_rows(root))
    home = {r["name"]: r for r in dashboard.home_rows(root)[0]}
    b = rows["breakout_v2"]
    assert b["until"] == home["breakout_v2"]["until"] == record("breakout_v2", 19)["day"] != b["day"] == record("breakout_v2", 22)["day"]
    assert b["actual"] == record("breakout_v2", 19)["nums"]["actual"] and b["bt"] == record("breakout_v2", 19)["nums"]["bt"]
    for name in ("trend_10_50", "trend_ema_v7", "grid_v1"):  # 마지막 날에 실제를 알면 until은 비지 않고 마지막 기록 날이다
        assert home[name]["until"] is None and rows[name]["until"] == rows[name]["day"] == record(name, 22)["day"], name
    assert rows["old_v1"]["until"] is None and rows["old_v1"]["day"] is None  # 끈 전략 줄: 숫자가 없으니 날도 없다
    assert dashboard.EXPORT_FIELDS[3:5] == ("day", "until")  # day 바로 뒤
    assert all(k in dashboard.FIELD_MEANINGS["until"] for k in ("actual", "bt", "bt0", "gap"))
    for key in ("actual", "bt", "bt0", "gap"):
        assert "until" in dashboard.FIELD_MEANINGS[key], key  # 숫자 뜻 글이 until 날 기준임을 말한다
    csv_rows = by_name(parse_csv(fetch(root, "/export.csv")[2]))
    assert csv_rows["breakout_v2"]["until"] == "2026-10-11" and csv_rows["breakout_v2"]["day"] == "2026-10-14"
    assert csv_rows["old_v1"]["until"] == ""


def test_export_unreadable_rows_have_no_until(tmp_path):
    """읽지 못한 전략 · 기록 없는 전략은 숫자가 없으니 `until`도 비어 있다."""
    root = write_facts(tmp_path)
    break_repo(root)
    (root / "strategies" / "trend_10_50" / "daily" / "days.jsonl").unlink()
    rows = by_name(dashboard.export_rows(root))
    assert all(rows[n]["until"] is None for n in ("grid_v1", "trend_ema_v7", "breakout_v2", "trend_10_50"))
    assert rows["trend_10_50"]["state"] is None and rows["grid_v1"]["state"] == "확인 불가"


def test_export_reads_each_guardrail_once_per_request(export_repo, monkeypatch):
    """가드레일은 전략마다 요청 하나에 한 번만 읽는다: 읽은 뒤 파일이 바뀌어도(두 번째 읽기가 실패해도) 상태 · 이유 · 가드레일 칸이 그대로다."""
    real, calls = dashboard.read_guardrail, {}

    def once(root, name):
        calls[name] = calls.get(name, 0) + 1
        if calls[name] > 1:
            raise OSError("읽은 뒤 바뀜")
        return real(root, name)

    monkeypatch.setattr(dashboard, "read_guardrail", once)
    rows = by_name(dashboard.export_rows(export_repo))
    assert set(calls) == {*NAMES, "old_v1"} and set(calls.values()) == {1}
    t = rows["trend_10_50"]
    assert (t["state"], t["reason"], t["guardrail_version"], t["warning_line"]) == ("경고", "실행 문제로 추정", 1, -0.072)
    o = rows["old_v1"]
    assert (o["state"], o["reason"], o["guardrail_version"]) == ("은퇴", None, 1)


@pytest.mark.parametrize("field, bad", [
    ("normal_range", None), ("normal_range", []), ("normal_range", "bad"),
    ("normal_range", {"days": 30, "drawn": True, "low": "x", "high": 1.0, "daily_low": [], "daily_high": []}),
    ("warning_line", None), ("warning_line", "bad"), ("warning_line", {"line": "x", "sensitivity": "민감", "lines": {}})])
def test_export_required_guardrail_field_with_bad_shape_is_an_unreadable_row(tmp_path, field, bad):
    """`normal_range` · `warning_line`은 필수 칸이라 `read_guardrail`이 모양을 거른다: 틀리면 그 전략만 확인 불가 줄(200, 가드레일 칸 · 숫자 칸 빈칸), 나머지는 그대로."""
    root = write_facts(tmp_path)
    edit_guardrail(root, "trend_10_50", lambda g: g.update({field: bad}))
    status_code, _, body = fetch(root, "/export.json")
    assert status_code == 200 and str(tmp_path).encode() not in body
    rows = by_name(json.loads(body)["strategies"])
    t = rows["trend_10_50"]
    assert t["state"] == "확인 불가" and t["reason"] == "가드레일 파일을 읽지 못했어요(링크이거나 모양이 달라요)"
    assert all(t[k] is None for k in dashboard.EXPORT_FIELDS if k not in ("strategy", "state", "reason"))
    assert rows["grid_v1"]["state"] == "판단 보류" and rows["grid_v1"]["guardrail_version"] == 1 and rows["grid_v1"]["warning_line"] == -0.072


@pytest.mark.parametrize("lead", ["=1+1", "+x", "-x", "@x", "\tx", "\rx", "\nx", "＝1", "＋1", "－1", "＠x"])
def test_csv_text_cells_cannot_become_formulas(lead):
    """글 칸이 수식 시작 글자(OWASP: = + - @ 탭 CR LF, 전각 ＝ ＋ － ＠)로 시작하면 CSV 칸이 `'`로 시작한다. 숫자 칸(음수 포함)은 숫자 그대로."""
    row = dict.fromkeys(dashboard.EXPORT_FIELDS) | {"strategy": lead, "state": lead, "reason": lead + ',"q"\nz', "actual": -0.5, "trades": 3,
                                                    "c3_unverified": False}
    got = parse_csv(dashboard.export_csv([row]))
    assert len(got) == 1 and len(got[0]) == len(dashboard.EXPORT_FIELDS)  # 쉼표 · 따옴표 · 줄바꿈이 칸을 쪼개지 않는다
    assert got[0]["strategy"] == "'" + lead and got[0]["state"] == "'" + lead and got[0]["reason"] == "'" + lead + ',"q"\nz'
    assert (got[0]["actual"], got[0]["trades"], got[0]["c3_unverified"]) == ("-0.5", "3", "False")
    assert json.loads(dashboard.export_json([row], "2026-10-14"))["strategies"][0]["state"] == lead  # JSON은 수식이 아니라 그대로


def test_csv_safe_text_is_left_alone():
    """수식 시작 글자가 아닌 글(가운데에 있는 `=` · `-`, 이미 있는 `'`)은 건드리지 않는다."""
    texts = ["정상", "a=b", "x-y", "x+y", "a@b", "'quoted", "", "2026-10-14"]
    row = dict.fromkeys(dashboard.EXPORT_FIELDS) | {"strategy": "s", "state": "정상"}
    for text in texts:
        assert parse_csv(dashboard.export_csv([row | {"reason": text}]))[0]["reason"] == text


def test_csv_injection_through_repo_text(export_repo):
    """레포 글(days.jsonl의 rep)이 수식 · 탭으로 시작해도 서버가 내보낸 CSV 칸은 `'`로 시작하고 JSON은 원래 글이다."""
    root = export_repo
    edit_last(root, "trend_ema_v7", lambda r: r.update(rep="+x(=1+1)"))
    edit_last(root, "grid_v1", lambda r: r.update(rep="\tx(\ry)"))
    flat = by_name(parse_csv(fetch(root, "/export.csv")[2]))
    assert flat["trend_ema_v7"]["state"] == "'+x" and flat["trend_ema_v7"]["reason"] == "'=1+1"
    assert flat["grid_v1"]["state"] == "'\tx" and flat["grid_v1"]["reason"] == "'\ry"
    raw = by_name(json.loads(fetch(root, "/export.json")[2])["strategies"])
    assert raw["trend_ema_v7"]["state"] == "+x" and raw["trend_ema_v7"]["reason"] == "=1+1"


def test_export_unreadable_strategy_row(tmp_path, monkeypatch):
    """깨진 전략: 상태는 확인 불가 · 이유는 경로 없는 한 줄 · 숫자 칸은 CSV 빈칸 / JSON null. 가드레일은 읽히면 채운다. 끈 전략의 기록 손상은 그 상태."""
    repo = tmp_path / "broken"
    root = write_facts(repo)
    break_repo(root)  # grid_v1 days.jsonl 깨짐 · trend_ema_v7 가드레일 깨짐 · breakout_v2 record.md 깨짐(기록 손상)
    csv_body, json_body = fetch(root, "/export.csv")[2], fetch(root, "/export.json")[2]
    for body in (csv_body, json_body):
        assert str(tmp_path).encode() not in body and b"/Users/" not in body and b"/private/" not in body
    rows = by_name(json.loads(json_body)["strategies"])
    numbers = ("day", "until", "live_day", "trades", "actual", "bt", "bt0", "gap")
    g = rows["grid_v1"]
    assert g["state"] == "확인 불가" and g["reason"] == "매일 대조 기록 파일을 읽지 못했어요(링크이거나 모양이 달라요)"
    assert all(g[k] is None for k in numbers) and g["guardrail_version"] == 1 and g["warning_line"] == -0.072  # 가드레일은 읽힘
    t = rows["trend_ema_v7"]
    assert t["state"] == "확인 불가" and t["reason"] == "가드레일 파일을 읽지 못했어요(링크이거나 모양이 달라요)"
    assert all(t[k] is None for k in (*numbers, "guardrail_version", "set_on", "live_start", "warning_line", "normal_low"))
    b = rows["breakout_v2"]
    assert (b["state"], b["reason"]) == (BROKEN, web.BROKEN_NOTE) and b["actual"] is None
    flat = by_name(parse_csv(csv_body))
    assert all(flat["grid_v1"][k] == "" for k in numbers) and flat["trend_ema_v7"]["state"] == "확인 불가"
    assert rows["trend_10_50"]["state"] == "경고" and rows["trend_10_50"]["actual"] == -0.046  # 나머지 전략은 그대로


def test_export_strategy_list_unreadable(tmp_path):
    """strategies/가 링크이면 전략 이름을 알 수 없다: 200으로 한 줄(전략 칸 비어 있음, 확인 불가, 전략 목록 사유)."""
    root = write_facts(tmp_path / "linked", ())
    (tmp_path / "elsewhere").mkdir()
    (root / "strategies").symlink_to(tmp_path / "elsewhere")
    status_code, _, body = fetch(root, "/export.json")
    assert status_code == 200 and str(tmp_path).encode() not in body
    (only,) = json.loads(body)["strategies"]
    assert only["strategy"] is None and only["state"] == "확인 불가"
    assert only["reason"] == "전략 목록 파일을 읽지 못했어요(링크이거나 모양이 달라요)" and only["actual"] is None
    (flat,) = parse_csv(fetch(root, "/export.csv")[2])
    assert flat["strategy"] == "" and flat["state"] == "확인 불가"


@pytest.mark.parametrize("bad", [None, [], "bad", 7, True])
def test_export_tolerates_bad_optional_fields(tmp_path, bad):
    """선택 칸 c3 · cost가 객체가 아니거나 안쪽 값이 숫자가 아니어도, set_on이 날이 아니어도, 그리지 않은 normal_range의 low · high는 빈칸 · null이고 예외 없음.

    `normal_range` · `warning_line`은 필수 칸이라 `read_guardrail`이 거른다(모양이 틀리면 확인 불가 줄): `test_export_required_guardrail_field_with_bad_shape_is_an_unreadable_row`.
    """
    root = write_facts(tmp_path)
    edit_guardrail(root, "trend_10_50", lambda g: g.update(c3=bad, cost=bad, set_on=bad))
    t = by_name(dashboard.export_rows(root))["trend_10_50"]
    assert all(t[k] is None for k in ("set_on", "cost_one_way_bp", "c3_sharpe_cost0", "c3_adjusted_low", "c3_trials", "c3_unverified"))
    assert t["guardrail_version"] == 1 and t["warning_line"] == -0.072 and t["actual"] == -0.046  # 나머지는 그대로
    edit_guardrail(root, "trend_10_50", lambda g: g.update(
        c3={"sharpe_cost0": "x", "adjusted_low": True, "trials": 1.5, "unverified": "yes"}, cost={"one_way_bp": float("nan")},
        set_on="2026-13-45"))
    t = by_name(dashboard.export_rows(root))["trend_10_50"]
    assert all(t[k] is None for k in ("set_on", "cost_one_way_bp", "c3_sharpe_cost0", "c3_adjusted_low", "c3_trials", "c3_unverified"))
    edit_guardrail(root, "trend_10_50", lambda g: g.update(normal_range={"days": 30, "drawn": False, "reason": "x", "low": 1.0, "high": 2.0}))
    t = by_name(dashboard.export_rows(root))["trend_10_50"]
    assert t["normal_low"] is None and t["normal_high"] is None  # 그리지 않은 범위의 숫자는 내보내지 않는다(화면과 같음)
    json.loads(fetch(root, "/export.json")[2])  # NaN 같은 JSON 밖 글자가 새지 않는다
    assert b"NaN" not in fetch(root, "/export.csv")[2]


def test_export_lone_surrogate_text_does_not_break_the_download(tmp_path):
    """JSON `\\ud800`처럼 UTF-8로 쓸 수 없는 낱 서로게이트가 레포 글에 있어도 내보내기가 500이 되지 않는다(`replace` 처리기, 본문은 UTF-8)."""
    root = write_facts(tmp_path)
    p = root / "strategies" / "trend_10_50" / "daily" / "days.jsonl"
    recs = [json.loads(x) for x in p.read_text(encoding="utf-8").splitlines()]
    recs[-1]["rep"] = "경고(\ud800x)"
    p.write_text("".join(json.dumps(r) + "\n" for r in recs), encoding="ascii")  # ensure_ascii 기본값: 낱 서로게이트가 \ud800 글자로 남는다
    for path in ("/export.csv", "/export.json"):
        status_code, _, body = fetch(root, path)
        assert status_code == 200 and "?x" in body.decode("utf-8"), path


def test_export_headers_and_filename(export_repo):
    """내려받기 머리글: 종류 · attachment · 파일 이름(날만, 레포 글 없음), 나머지 보안 머리글 다섯은 그대로. 토큰이 없으면 403(내려받기 머리글 없음)."""
    root = export_repo
    for path, mime, ext in (("/export.csv", "text/csv", "csv"), ("/export.json", "application/json", "json")):
        status_code, h, body = fetch(root, path)
        assert status_code == 200
        assert h["Content-Type"] == f"{mime}; charset=utf-8"
        assert h["Content-Disposition"] == f'attachment; filename="blackclip-guardrail-2026-10-14.{ext}"'
        assert all(h.get(k) == v for k, v in HEADERS.items() if k != "Content-Type")
        assert int(h["Content-Length"]) == len(body)
        head_status, hh, head_body = fetch(root, path, "HEAD")
        assert head_status == 200 and head_body == b"" and hh["Content-Disposition"] == h["Content-Disposition"]
        assert hh["Content-Length"] == h["Content-Length"]
        denied_status, dh, dbody = fetch(root, path, token=False)
        assert denied_status == 403 and "Content-Disposition" not in dh and all(dh.get(k) == v for k, v in HEADERS.items())
        assert b"trend_10_50" not in dbody


def test_export_filename_without_day(tmp_path):
    """기록이 하나도 없으면 날 없는 이름(`blackclip-guardrail.csv`), JSON의 day는 null."""
    repo_empty = write_facts(tmp_path / "empty", ())
    only_guardrail = write_facts(tmp_path / "nodays")
    for n in NAMES:
        (only_guardrail / "strategies" / n / "daily" / "days.jsonl").unlink()
    for root in (repo_empty, only_guardrail):
        for ext in ("csv", "json"):
            status_code, h, body = fetch(root, f"/export.{ext}")
            assert status_code == 200 and h["Content-Disposition"] == f'attachment; filename="blackclip-guardrail.{ext}"'
        assert json.loads(fetch(root, "/export.json")[2])["day"] is None
    assert json.loads(fetch(repo_empty, "/export.json")[2])["strategies"] == []
    assert [r["state"] for r in dashboard.export_rows(only_guardrail)] == [None] * 4  # 기록이 없으면 상태 빈칸
    assert parse_csv(fetch(repo_empty, "/export.csv")[2]) == []  # 머리 줄만


def test_export_day_accepts_only_iso_dates():
    """파일 이름 · day에는 `\\d{4}-\\d{2}-\\d{2}` 꼴의 날만(머리글 줄바꿈 · 따옴표 · 경로 · 비ASCII 숫자 거절), 가장 늦은 날."""
    rows = lambda *days: [{"day": d} for d in days]  # noqa: E731
    assert dashboard.export_day(rows("2026-10-11", "2026-10-14", None)) == "2026-10-14"
    assert dashboard.export_day([]) is None and dashboard.export_day(rows(None)) is None
    for bad in ('2026-10-14"\r\nSet-Cookie: x=1', "../../etc/passwd", "٢٠٢٦-١٠-١٤", "2026-10-14\n", "", 20261014):
        assert dashboard.export_day(rows(bad)) is None, bad
    assert dashboard.export_day(rows("2026-10-14", "9999-99-99x")) == "2026-10-14"


def test_export_field_meanings_follow_copy_rules():
    """JSON `fields`의 한 줄 뜻: 모든 칸에 있고, 화면 글 규칙(가운뎃점 · 합쇼체 · 금지 낱말)을 지킨다. 칸 이름은 정한 목록 그대로."""
    assert list(dashboard.EXPORT_FIELDS) == [
        "strategy", "state", "reason", "day", "until", "live_day", "trades", "actual", "bt", "bt0", "gap", "guardrail_version", "set_on", "live_start",
        "cost_one_way_bp", "normal_low", "normal_high", "warning_line", "c3_sharpe_cost0", "c3_adjusted_low", "c3_trials", "c3_unverified"]
    assert set(dashboard.FIELD_MEANINGS) == set(dashboard.EXPORT_FIELDS)
    for name, meaning in dashboard.FIELD_MEANINGS.items():
        assert copy_violations(f"<p>{meaning}</p>") == [], (name, meaning)
    assert "비용 0" in dashboard.FIELD_MEANINGS["gap"] and "보수적" in dashboard.FIELD_MEANINGS["bt"]
    assert "비어 있어요" in dashboard.FIELD_MEANINGS["strategy"]  # 전략 목록을 읽지 못하면 빈 칸

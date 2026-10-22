"""blackclip dashboard 홈: 숫자 칸과 흐름이 터미널 chart D와 같은지, 끈 전략 · 빈 상태, 문구 · 경로 규칙.

화면은 실제 서버(`make_server`, 포트 0)에서 `http.client`로 받은 HTML이다. 다른 화면 시험이 이어서 쓰는 곳:
`page_routes`(가드레일 화면 주소도 들어 있음), `export_routes`(내보내기 주소 둘), `home_cases`(가드레일 화면용 레포도 들어 있음).
가드레일 화면(`/s/<이름>`) 시험은 파일 아래쪽 「가드레일 화면」 구역이다.
"""
import http.client
import json
import math
import re
import threading
from datetime import date, timedelta
from fractions import Fraction
from html import unescape
from pathlib import Path

import pytest
from facts import NAMES, RESPONSE, START, guardrail, record, write_facts
from webrules import copy_violations, visible_text

from blackclip import dashboard, states
from blackclip.cli import main
from blackclip.daily.days import read_days
from blackclip.daily.reconcile import _where, strategy_cause
from blackclip.notify import RETRO_HEAD
from blackclip.record import stopped, write_record
from blackclip.screen import chart, data, paint, status, web
from blackclip.workspace import WorkspaceError, read_guardrail, strategy_names


@pytest.fixture(autouse=True)
def _home(monkeypatch, tmp_path):
    monkeypatch.setenv("HOME", str(tmp_path / "home"))


def get_pages(root: Path, paths) -> dict[str, str]:
    """실제 서버(127.0.0.1, 포트 0)를 띄워 주소마다 GET 200 본문을 받는다. 끝에 shutdown · server_close.

    Args:
        root: .blackclip 경로.
        paths: `/`로 시작하는 주소들(`?t=`는 여기서 붙임).

    Returns:
        주소 → 본문(utf-8).
    """
    server = dashboard.make_server(root, 0)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    try:
        out = {}
        for path in paths:
            c = http.client.HTTPConnection("127.0.0.1", server.server_address[1], timeout=5)
            c.request("GET", f"{path}?t={server.token}")
            r = c.getresponse()
            body = r.read().decode()
            c.close()
            assert r.status == 200, (path, r.status)
            out[path] = body
        return out
    finally:
        server.shutdown()
        server.server_close()


def page_routes(root: Path) -> list[str]:
    """한 레포에서 문구 · 경로 시험에 넣을 화면 주소들: 홈 + 실전 · 끈 전략(중지 · 은퇴 · 기록 손상)마다 가드레일 화면 `/s/<이름>` +
    가드레일 · record.md · retro.md 가운데 하나라도 있는 전략마다 회고 화면 `/s/<이름>/retro`(둘 다 200인 주소만).

    Args:
        root: .blackclip 경로.

    Returns:
        주소 목록. strategies/를 읽지 못하면(링크) 홈뿐이다.
    """
    try:
        names = sorted({*status.strategies(root), *stopped(root)})
        folders = root / "strategies"
        retro = sorted({*names, *(n for n in strategy_names(root) if any((folders / n / f).is_file() for f in ("record.md", "retro.md")))})
    except (WorkspaceError, OSError):
        names, retro = [], []
    return ["/", *(f"/s/{n}" for n in names), *(f"/s/{n}/retro" for n in retro)]


def export_routes(root: Path) -> list[str]:
    """경로 시험(문구 시험은 아님)에만 넣을 내보내기 주소들: `/export.csv` · `/export.json`.

    Args:
        root: .blackclip 경로.

    Returns:
        주소 목록. 읽지 못하는 레포도 한 줄짜리 내보내기(200)라 늘 둘이다.
    """
    return ["/export.csv", "/export.json"]


def stop(repo: Path, monkeypatch, name: str, state: str) -> None:
    """`blackclip record`로 전략을 끈다(after.md도 함께 써짐).

    Args:
        repo: 레포 꼭대기.
        monkeypatch: pytest monkeypatch(cwd를 레포로).
        name: 전략 이름.
        state: `중지` 또는 `은퇴`.
    """
    monkeypatch.chdir(repo)
    assert main(["record", name, "--state", state]) == 0


def edit_last(root: Path, name: str, change) -> None:
    """전략 days.jsonl의 마지막 줄만 고쳐 쓴다.

    Args:
        root: .blackclip 경로.
        name: 전략 이름.
        change: 마지막 기록 dict를 받아 제자리에서 고치는 함수.
    """
    p = root / "strategies" / name / "daily" / "days.jsonl"
    recs = [json.loads(x) for x in p.read_text(encoding="utf-8").splitlines()]
    change(recs[-1])
    p.write_text("".join(json.dumps(r, ensure_ascii=False) + "\n" for r in recs), encoding="utf-8")


def break_repo(root: Path) -> None:
    """읽기가 깨지는 세 가지를 한 레포에 넣는다: 매일 대조 기록(grid_v1) · 가드레일(trend_ema_v7) · 끈 전략 기록 손상(breakout_v2).

    Args:
        root: .blackclip 경로.
    """
    s = root / "strategies"
    (s / "grid_v1" / "daily" / "days.jsonl").write_text("{broken\n", encoding="utf-8")
    (s / "trend_ema_v7" / "guardrail.v1.yaml").write_text("{broken\n", encoding="utf-8")
    (s / "breakout_v2" / "record.md").write_text("<<<<<<< HEAD\n", encoding="utf-8")


def edit_days(root: Path, name: str, change) -> None:
    """전략 days.jsonl의 모든 줄을 고쳐 쓴다.

    Args:
        root: .blackclip 경로.
        name: 전략 이름.
        change: 기록 dict를 받아 제자리에서 고치는 함수(줄마다 한 번).
    """
    p = root / "strategies" / name / "daily" / "days.jsonl"
    recs = [json.loads(x) for x in p.read_text(encoding="utf-8").splitlines()]
    for r in recs:
        change(r)
    p.write_text("".join(json.dumps(r, ensure_ascii=False) + "\n" for r in recs), encoding="utf-8")


def edit_guardrail(root: Path, name: str, change) -> None:
    """전략의 가드레일 v1 파일을 고쳐 쓴다.

    Args:
        root: .blackclip 경로.
        name: 전략 이름.
        change: 가드레일 dict를 받아 제자리에서 고치는 함수.
    """
    p = root / "strategies" / name / "guardrail.v1.yaml"
    g = json.loads(p.read_text(encoding="utf-8"))
    change(g)
    p.write_text(json.dumps(g, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def guard_special(repo: Path, monkeypatch) -> Path:
    """가드레일 화면의 갈래를 한 레포에 모은다: trend_10_50 적대 대응 글 · grid_v1 기록 없음 · trend_ema_v7 정상 범위 없음 · breakout_v2 은퇴.

    Args:
        repo: 레포 꼭대기.
        monkeypatch: pytest monkeypatch(`blackclip record`가 cwd를 봄).

    Returns:
        .blackclip 경로.
    """
    root = write_facts(repo)
    edit_last(root, "trend_10_50", lambda r: r.update(response={"execution_warning": "<script>alert(1)</script> 합니다 · █",
                                                              "warning_line": "줄 하나\n줄 둘"}))
    (root / "strategies" / "grid_v1" / "daily" / "days.jsonl").unlink()
    edit_days(root, "trend_ema_v7", lambda r: r["nums"].update(lo=None, hi=None, month_lo=None, month_hi=None, pos=None, bt_pos=None))
    edit_guardrail(root, "trend_ema_v7", lambda g: g.update(normal_range={"days": 30, "drawn": False, "reason": "백테스트 왕복 거래 80건 · 기간 300일"}))
    stop(repo, monkeypatch, "breakout_v2", "은퇴")
    return root


def home_cases(tmp_path: Path, monkeypatch) -> dict[str, Path]:
    """문구 · 경로 시험이 도는 레포들(라벨 → .blackclip). `page_routes`가 레포마다 홈과 가드레일 화면을 모두 돈다.

    Args:
        tmp_path: 시험 임시 폴더.
        monkeypatch: pytest monkeypatch.

    Returns:
        FACTS 홈 · 깨진 전략이 든 홈(grid_v1 깨진 days.jsonl · trend_ema_v7 깨진 가드레일 · breakout_v2 기록 손상) · 빈 홈 · 끈 전략만 있는 홈(중지) ·
        가드레일 화면 갈래 모음(`guard_special`) · 회고 화면 갈래 모음(`retro_special`) · 구간이 둘인 레포 · strategies/가 링크인 홈.
    """
    out = {"FACTS": write_facts(tmp_path / "facts"), "깨진 전략": write_facts(tmp_path / "broken"),
           "빈 레포": write_facts(tmp_path / "empty", ()), "끈 전략만": write_facts(tmp_path / "stopped"),
           "가드레일 갈래": guard_special(tmp_path / "special", monkeypatch), "회고 갈래": retro_special(tmp_path / "retro"),
           "구간 둘": two_period_repo(tmp_path / "two")}
    break_repo(out["깨진 전략"])
    for n in NAMES:
        stop(tmp_path / "stopped", monkeypatch, n, "중지")
    link = write_facts(tmp_path / "linked", ())
    (tmp_path / "linked" / "elsewhere").mkdir()
    (link / "strategies").symlink_to(tmp_path / "linked" / "elsewhere")
    out["strategies가 링크"] = link
    return out


def live_rows(html: str) -> dict[str, tuple[list[str], str]]:
    """실전 표의 줄을 이름 → (숫자 칸 다섯: 실제 · 백테스트 · 차이 · 거래 · 기준 날, 줄 HTML)로.

    Args:
        html: 홈 HTML.

    Returns:
        이름 순서를 지킨 dict(줄 HTML의 접속 토큰은 `T`로 바꿔 서버 사이에 견줄 수 있음). 표 첫 번째 칸의 caption 줄(`<div class="sub">날까지</div>`)은
        터미널 글 꼴 `(날까지)`로 되돌린다.
    """
    table = re.search(r"<table.*?</table>", html, re.S)[0]
    rows = {}
    for tr in re.findall(r'<tr class="row">(.*?)</tr>', table, re.S):
        name = re.search(r'<th scope="row" class="cell cell-name">(?:<a [^>]*>)?(.*?)(?:</a>)?</th>', tr)[1]
        cells = [re.sub(r'<div class="sub">(.*?)</div>', r"(\1)", c) for c in re.findall(r'<td class="cell cell-num">(.*?)</td>', tr)]  # 값 아래 caption 줄은 `(날까지)`로 되돌려 견줌
        rows[name] = (cells, re.sub(r"\?t=[\w-]+", "?t=T", tr))  # 접속 토큰은 서버마다 다름
    return rows


D_ROW = re.compile(r"^  (\S+)\s+([▁▂▃▄▅▆·]+)\s+(\S+)\s+(\S+)")
"""터미널 D 표 줄: 이름 · 스파크라인 글자 · 손익 · 실제-BT 또는 「M/D까지」."""


def terminal_d(root: Path) -> dict[str, tuple[str, str, str]]:
    """터미널 `chart_d`(색 없음)를 그대로 돌려 전략마다 (스파크라인 글자, 손익, 실제-BT 또는 M/D까지)를 얻는다.

    Args:
        root: .blackclip 경로.

    Returns:
        이름 → 세 글.
    """
    entries = [(n, read_days(root, n)) for n in status.strategies(root)]
    return {m[1]: (m[2], m[3], m[4]) for line in chart.chart_d(entries, False, 200) if (m := D_ROW.match(line))}


def web_glyphs(tr: str) -> str:
    """홈 줄의 흐름 SVG를 터미널 글자로 되돌린다: 막대 높이 `3 * (눈금 + 1)` → `▁▂▃▄▅▆`, 점 → `·`.

    Args:
        tr: 줄 HTML.

    Returns:
        스파크라인 글자 열.
    """
    svg = re.search(r'<svg class="spark".*?</svg>', tr, re.S)[0]  # 칩 기호 SVG와 가려서 읽는다
    found = re.findall(r'<circle class="spark-gap"|<rect class="spark-bar"[^>]* height="(\d+)"', svg)
    return "".join("·" if not h else "▁▂▃▄▅▆"[int(h) // 3 - 1] for h in found)


def test_home_numbers_match_chart_d(tmp_path):
    """FACTS 네 전략: 실제 · 차이 · 「까지」 날 · 흐름 모양이 터미널 chart D 출력과 글자까지 같다."""
    root = write_facts(tmp_path)
    rows = live_rows(get_pages(root, ["/"])["/"])
    term = terminal_d(root)
    assert set(rows) == set(term) == set(NAMES)
    for name, (cells, tr) in rows.items():
        glyphs, pnl, gap = term[name]
        if gap.endswith("까지"):  # 마지막 날 실제를 몰라 차이 대신 마지막으로 안 날(M/D → YYYY-MM-DD)
            m, d = gap[:-2].split("/")
            assert cells[0] == f"{pnl}(2026-{int(m):02d}-{int(d):02d}까지)", name
            # 웹은 차이 칸이 따로라 터미널이 「까지」를 쓰는 자리에 그 날 기록의 차이(`nums.gap`)를 보인다
            assert cells[2] == web.pp(record(name, 19)["nums"]["gap"]) == "-0.1%p", name
        else:
            assert cells[0] == pnl and cells[2] == gap, name
        assert web_glyphs(tr) == glyphs, name
        assert re.search(r'<svg class="spark"[^>]* width="218" height="28" viewBox="0 0 218 28"', tr), name  # 22칸, 높이 28(가드레일 차트 상수와 별개)
    assert rows["trend_10_50"][0][0] == "-4.6%" and rows["trend_10_50"][0][2] == "-2.7%p"
    assert rows["breakout_v2"][0][0] == "+0.4%(2026-10-11까지)"
    assert rows["trend_10_50"][0][3] == "37건" and rows["trend_10_50"][0][4] == "2026-10-14"
    assert rows["trend_10_50"][1].count("spark-warn") == 2 and rows["grid_v1"][1].count("spark-warn") == 0
    assert ('aria-label="2026-09-23~2026-10-14 실전 시작부터 실제 손익 흐름이에요. 경고인 날은 2일이에요."'
            in rows["trend_10_50"][1])


def test_home_spark_uses_latest_day_across_strategies(tmp_path):
    """일찍 끝난 전략도 전체 가장 늦은 날까지 빈 점으로 이어진다(터미널 D와 같은 `last`)."""
    root = write_facts(tmp_path)
    p = root / "strategies" / "grid_v1" / "daily" / "days.jsonl"
    keep = p.read_text(encoding="utf-8").splitlines()[:-3]  # 10/11까지
    p.write_text("\n".join(keep) + "\n", encoding="utf-8")
    html = get_pages(root, ["/"])["/"]
    rows, term = live_rows(html), terminal_d(root)
    assert web_glyphs(rows["grid_v1"][1]) == term["grid_v1"][0] and term["grid_v1"][0].endswith("···")
    assert "2026-09-23~2026-10-14" in rows["grid_v1"][1]


def test_home_latest_day_counts_strategy_with_broken_guardrail(tmp_path):
    """공통 끝 날은 터미널 D와 같은 기록 집합(가드레일과 상관없이 읽힌 days.jsonl)에서 구한다.

    가장 늦은 날(10/14)을 가진 grid_v1의 가드레일만 깨지고 나머지는 10/11에서 끝난 레포: 나머지 줄의 흐름 칸 · 끝 날이 터미널 D와 같다.
    """
    root = write_facts(tmp_path)
    early = ("breakout_v2", "trend_10_50", "trend_ema_v7")
    for n in early:
        p = root / "strategies" / n / "daily" / "days.jsonl"
        p.write_text("\n".join(p.read_text(encoding="utf-8").splitlines()[:-3]) + "\n", encoding="utf-8")  # 10/11까지
    (root / "strategies" / "grid_v1" / "guardrail.v1.yaml").write_text("{broken\n", encoding="utf-8")
    rows, term = live_rows(get_pages(root, ["/"])["/"]), terminal_d(root)
    assert "가드레일 파일을 읽지 못했어요" in rows["grid_v1"][1] and term["grid_v1"][0].endswith(("▁", "▂", "▃", "▄", "▅", "▆"))
    for n in early:
        assert web_glyphs(rows[n][1]) == term[n][0] and term[n][0].endswith("···"), n
        assert "~2026-10-14 실전 시작부터" in rows[n][1], n


def test_gap_is_against_cost0_backtest(tmp_path):
    """bt != bt0인 기록 → 같은 기간 백테스트 칸은 bt(보수적), 차이 칸은 `actual − bt0`(비용 0), 칸 머리에 `(비용 0)`.

    차이의 뺄셈은 엔진이 days.jsonl에 해 둔 값(`reconcile`의 gap)이라, 이 시험은 화면이 그 칸을 `bt`로 다시 계산하지 않고 그대로 옮기는지를 본다.
    """
    root = write_facts(tmp_path)

    def cost(rec):
        for nums in (rec["nums"], rec["nums"]["all"]):
            nums.update(actual=-0.008, bt0=0.002, bt=-0.001, gap=-0.010)

    edit_last(root, "grid_v1", cost)
    html = get_pages(root, ["/"])["/"]
    cells = live_rows(html)["grid_v1"][0]
    assert cells[0] == "-0.8%" and cells[1] == "-0.1%" and cells[2] == "-1.0%p"  # actual − bt이면 -0.7%p
    assert "차이(비용 0)</th>" in html and "실제 − 같은 기간" not in html and web.DIFF_NOTE in html  # 머리는 줄이고 뜻은 표 아래 글에


def test_bt_cell_is_same_period_as_shown_actual(tmp_path):
    """마지막 날 실제를 몰라 `(날까지)`를 붙이면, 같은 기간 백테스트도 그 날의 값이다(다른 기간 숫자를 나란히 두지 않음)."""
    root = write_facts(tmp_path)
    cells = live_rows(get_pages(root, ["/"])["/"])["breakout_v2"][0]
    same_day = web.pct(record("breakout_v2", 19)["nums"]["bt"])  # k=19 = 2026-10-11
    last_day = web.pct(record("breakout_v2", 22)["nums"]["bt"])
    assert same_day != last_day and cells[0].endswith("(2026-10-11까지)") and cells[1] == same_day


def test_spark_label_counts_records_over_22_days(tmp_path):
    """22일을 넘으면 칸이 날을 묶는다: 문장의 시작 날은 첫 기록 날, 경고인 날은 칸이 아니라 기록 수다(60일, 경고 11일)."""
    root = write_facts(tmp_path, ("trend_ema_v7",))
    warn = {5, 6, 7, 20, 21, 33, 40, 41, 42, 57, 60}
    recs = []
    for k in range(1, 61):
        r = record("trend_ema_v7", min(k, 22)) | {"day": (START + timedelta(days=k - 1)).isoformat(), "live_day": k}
        if k in warn:
            r["states"] = r["states"] | {"실행": "경고"}
            r["rep"], r["title"] = "경고", states.TITLES["실행"]
        recs.append(r)
    (root / "strategies" / "trend_ema_v7" / "daily" / "days.jsonl").write_text(
        "".join(json.dumps(r, ensure_ascii=False) + "\n" for r in recs), encoding="utf-8")
    tr = live_rows(get_pages(root, ["/"])["/"])["trend_ema_v7"][1]
    last = (START + timedelta(days=59)).isoformat()
    assert f'aria-label="{START.isoformat()}~{last} 실전 시작부터 실제 손익 흐름이에요. 경고인 날은 11일이에요."' in tr
    assert len(web_glyphs(tr)) == 22 and tr.count("spark-warn") < 11  # 칸 수로 세면 모자란다(묶음의 마지막 날만 봄)


def test_home_rows_follow_engine_rank(tmp_path):
    """줄 순서 = 터미널 `blackclip status` 전체 보기 순서(`status._rank` · 이름순, 급한 것 먼저)."""
    root = write_facts(tmp_path)
    _, lines = status.status_lines(root, None)
    terminal = [x.split(" · ")[0] for x in lines[1:5]]
    assert terminal == ["breakout_v2", "trend_10_50", "grid_v1", "trend_ema_v7"]
    assert list(live_rows(get_pages(root, ["/"])["/"])) == terminal


def test_chip_reason_from_representative_column(tmp_path):
    """괄호 사유가 있으면 그 사유, 없으면 대표 칸의 웹 원인 이름, 정상이면 이유 줄이 없다."""
    root = write_facts(tmp_path)

    def bot_hold(rec):  # 봇 칸만 판단 보류인 날: 엔진과 같은 representative로 대표 칸을 정함
        rec["states"] = {"전략": "정상", "실행": "정상", "봇": "판단 보류", "선": "정상", "논지": "해당 없음"}
        rec["rep"], key = states.representative(rec["states"])
        rec["title"] = states.TITLES[key]

    edit_last(root, "grid_v1", bot_hold)
    rows = {r["name"]: r["reason"] for r in dashboard.home_rows(root)[0]}
    assert rows == {"breakout_v2": "체결 내역을 못 받음", "trend_10_50": "실행 문제로 추정",
                    "grid_v1": "봇과 체결 받기", "trend_ema_v7": None}
    html = get_pages(root, ["/"])["/"]
    live = live_rows(html)
    assert '<div class="why why-warn">실행 문제로 추정</div>' in live["trend_10_50"][1]
    assert '<div class="why">봇과 체결 받기</div>' in live["grid_v1"][1] and 'class="why' not in live["trend_ema_v7"][1]
    assert "봇 · 체결 받기" not in html and "봇과 체결 받기" in html


def test_chip_reason_unknown_title_is_none(tmp_path):
    """title이 TITLES에 없는 줄(손으로 고친 기록)은 이유 줄을 내지 않는다(없는 말을 지어내지 않음)."""
    root = write_facts(tmp_path)
    edit_last(root, "trend_10_50", lambda rec: rec.update(title="알 수 없는 원인"))
    assert {r["name"]: r["reason"] for r in dashboard.home_rows(root)[0]}["trend_10_50"] is None


def test_home_lists_broken_strategy(tmp_path, monkeypatch):
    """끈 전략 표: 중지 · 은퇴(끈 뒤 회고) · 기록 손상(안내 글)이 실전 표 밖에 있고, 실전 전략 수에 안 든다."""
    root = write_facts(tmp_path)
    stop(tmp_path, monkeypatch, "grid_v1", "중지")
    stop(tmp_path, monkeypatch, "trend_ema_v7", "은퇴")
    (root / "strategies" / "breakout_v2" / "record.md").write_text("<<<<<<< HEAD\n", encoding="utf-8")
    html = get_pages(root, ["/"])["/"]
    assert list(live_rows(html)) == ["trend_10_50"] and "실전 전략 1개" in html
    off = re.findall(r'<tr class="row">(.*?)</tr>', html.split("끈 전략 3개")[1], re.S)
    assert len(off) == 3
    by_name = {re.search(r"(breakout_v2|grid_v1|trend_ema_v7)", tr)[1]: tr for tr in off}
    assert "기록 손상" in by_name["breakout_v2"] and web.BROKEN_NOTE in by_name["breakout_v2"]
    assert "중지" in by_name["grid_v1"] and web.AFTER_NOTE in by_name["grid_v1"]
    assert "은퇴" in by_name["trend_ema_v7"] and web.AFTER_NOTE in by_name["trend_ema_v7"]
    assert all("/s/" in tr for tr in off)  # 가드레일 판이 있으면 화면 링크


def test_home_unreadable_strategy_is_row_not_error(tmp_path):
    """한 전략 days.jsonl이 깨져도 그 줄만 「확인 불가」 + web.reason, 나머지 줄은 그대로, 응답 200, 경로 없음."""
    root = write_facts(tmp_path)
    before = live_rows(get_pages(root, ["/"])["/"])
    (root / "strategies" / "grid_v1" / "daily" / "days.jsonl").write_text("{broken\n", encoding="utf-8")
    html = get_pages(root, ["/"])["/"]  # get_pages가 200을 단언
    after = live_rows(html)
    assert list(after) == ["breakout_v2", "trend_10_50", "trend_ema_v7", "grid_v1"]  # 못 읽은 전략은 맨 뒤
    cells, tr = after["grid_v1"]
    assert web.chip("확인 불가") in tr and "매일 대조 기록 파일을 읽지 못했어요(링크이거나 모양이 달라요)" in tr
    assert cells == ["모름"] * 5 and 'class="spark"' not in tr
    assert all(after[n] == before[n] for n in ("breakout_v2", "trend_10_50", "trend_ema_v7"))
    assert str(tmp_path) not in html


def test_home_unreadable_guardrail_is_row_not_error(tmp_path):
    """가드레일이 깨진 전략도 그 줄만 「확인 불가」 + 가드레일 사유."""
    root = write_facts(tmp_path)
    (root / "strategies" / "trend_ema_v7" / "guardrail.v1.yaml").write_text("{broken\n", encoding="utf-8")
    html = get_pages(root, ["/"])["/"]
    assert "가드레일 파일을 읽지 못했어요(링크이거나 모양이 달라요)" in live_rows(html)["trend_ema_v7"][1]
    assert list(live_rows(html))[:3] == ["breakout_v2", "trend_10_50", "grid_v1"] and str(tmp_path) not in html


def test_home_whole_page_failure_names_strategy_list(tmp_path, monkeypatch):
    """strategies/ 자체를 못 읽으면(링크) 한 장 「확인 불가」: 「전략 기록」은 끈 전략의 record.md를 뜻하는 글이라 「전략 목록」을 쓴다."""
    link = home_cases(tmp_path, monkeypatch)["strategies가 링크"]
    html = get_pages(link, ["/"])["/"]
    assert "확인 불가. 전략 목록 파일을 읽지 못했어요(링크이거나 모양이 달라요)" in html and "전략 기록" not in html
    assert web.reason(WorkspaceError("x"), "전략 목록") == "전략 목록 파일을 읽지 못했어요(링크이거나 모양이 달라요)"


def test_no_chip_without_records(tmp_path):
    """매일 대조 기록이 없는 전략은 칩도 숫자도 없이 안내 글만: live.json이 있으면 실전 중, 없으면 실전 시작 전."""
    root = write_facts(tmp_path)
    s = root / "strategies"
    (s / "grid_v1" / "daily" / "days.jsonl").unlink()
    (s / "trend_ema_v7" / "daily" / "days.jsonl").unlink()
    (s / "trend_ema_v7" / "live.json").unlink()
    html = get_pages(root, ["/"])["/"]
    rows = live_rows(html)
    assert web.EMPTY_LIVE in rows["grid_v1"][1] and web.EMPTY_PRE in rows["trend_ema_v7"][1]
    for n in ("grid_v1", "trend_ema_v7"):
        assert "chip" not in rows[n][1] and "<svg" not in rows[n][1] and rows[n][0] == []
    assert html.count('class="chip ') == 4 and "실전 전략 3개" in html  # 기록이 있는 breakout_v2 · trend_10_50만 칩(표와 쌓은 목록 두 곳씩)


def test_home_empty_state(tmp_path, monkeypatch):
    """전략이 없으면 글 한 줄 + 행동 하나만. 끈 전략만 있으면 빈 패널이 아니라 같은 글이 실전 칸에 있고 끈 전략 표 · 내보내기가 이어진다(215)."""
    cases = home_cases(tmp_path, monkeypatch)
    empty = get_pages(cases["빈 레포"], ["/"])["/"]
    assert web.EMPTY_ALL in empty and '<code class="cmd">blackclip save &lt;전략&gt; 뒤 blackclip live &lt;전략&gt;</code>' in empty
    assert "<table" not in empty and "export" not in empty and "실전 전략 0개" in empty
    only_off = get_pages(cases["끈 전략만"], ["/"])["/"]
    first = only_off.split("</section>")[0]
    assert "실전 전략 0개" in first and web.EMPTY_ALL in first and "<table" not in first
    assert only_off.count("<table") == 1 and "끈 전략 4개" in only_off and "/export.csv" in only_off
    assert '<div class="head"><h1 class="display">실전 전략 0개</h1>' in only_off and only_off.index("<h1") < only_off.index('<section class="panel">')  # 같은 자리
    assert '<div class="head"><h1 class="display">실전 전략 0개</h1></div><section class="panel"><p class="empty">아직 실전 전략이 없어요.' in empty
    css = web.css()
    root = css.split("}")[0]
    assert "font-family:var(--studio-font-mono)" in re.search(r"\.cmd\{[^}]*\}", css)[0] and "monospace" in root
    assert css.index("[class]{") < css.index(".cmd{")  # 뒤에 와야 [class]의 font-family:inherit을 이김


def test_no_absolute_path_in_pages_or_exports(tmp_path, monkeypatch):
    """깨진 전략이 든 레포를 포함한 모든 화면과 내보내기에 레포의 절대 경로가 없다(`page_routes` · `export_routes` 전부).

    대조군: 같은 깨짐에서 엔진 예외 글에는 실제로 경로가 들어 있다(그래서 이 시험이 샐 때 잡을 수 있다).
    """
    cases = home_cases(tmp_path, monkeypatch)
    with pytest.raises(WorkspaceError) as exc:
        read_days(cases["깨진 전략"], "grid_v1")
    assert str(tmp_path) in str(exc.value)
    banned = {str(tmp_path), str(tmp_path.resolve()), str(Path.home())}
    for label, root in cases.items():
        for path, html in get_pages(root, page_routes(root) + export_routes(root)).items():
            assert not [b for b in banned if b in html], (label, path)
            assert not re.search(r"(?<![\w.-])/(?:Users|home|private|var|tmp)/", html), (label, path)


def test_dashboard_copy_rules(tmp_path, monkeypatch):
    """렌더한 화면의 보이는 글 + `aria-label` · `<title>`이 문구 규칙을 지킨다(`page_routes`가 가드레일 화면까지 담음)."""
    for label, root in home_cases(tmp_path, monkeypatch).items():
        for path, html in get_pages(root, page_routes(root)).items():
            assert copy_violations(html) == [], (label, path)


def test_copy_violations_has_teeth():
    """검사기 자체: 규칙마다 걸리고, 날 · bp · 쉼표 수 · `<style>` · `class="quote"`는 지나간다."""
    def page(body):
        return f"<title>t</title><style>a{{width:100%}}</style><main>{body}</main>"

    for bad in ("추천해요", "곧 열려요", "점 · 점", "끊어 — 이어", "합니다", "😀", "10/11에", "12345건", "같은 기간 BT", '<svg aria-label="합니다">',
                '<a title="합니다">', '<div aria-valuetext="합니다">', '<img alt="추천">', "<p title='점 · 점'>", "<img alt='합니다'>",
                '<p class="no-quote">합니다</p>', '<p class="quote-x">합니다</p>', '<p data-class="quote">합니다</p>',
                "⚠", "✅", "❌", "☀", "⭐", "©", '<a href="/x">CSV 받기 →</a>', '<button>열기 →</button>', '<a href="/x">더 보기</a> →'):
        assert copy_violations(page(bad)), bad
    assert copy_violations(f"<title>합니다</title>{page('x')}")
    for ok in ("2026-10-14 기준", "중앙값 -5300bp", "1,234건", "1.2345%", "BTCUSDT 매수", '<p class="quote">합니다 · 12345 BT 10/11</p>',
               '<a href="/x?t=1234567">링크</a>', '<p class="a quote b">합니다</p>', "<p class='quote'>합니다</p>",
               *(web.chip(w) for w in paint.SYMBOL), web.chip("모르는 낱말")):
        assert copy_violations(page(ok)) == [], ok


# ---------------------------------------------------------------------------------------------------------------------
# 가드레일 화면 `/s/<이름>`. 기대값은 facts · 골든 · 진짜 nums / data.* 에서 읽는다(손으로 쓴 숫자는 facts와 맞춘 것만).
# ---------------------------------------------------------------------------------------------------------------------

_CHART = re.compile(r'<svg class="chart" role="img"[^>]*>.*?</svg>', re.S)
_LEGEND = re.compile(r'<ul class="legend"[^>]*>(.*?)</ul>', re.S)


def e_counts(exits: object, n: object, drawn: object) -> str:
    """차트 E 옆 한 줄: 청산 · 수익 표본 · 그린 표본 세 수와 점 · 실제 평균의 분모(그린 표본). 시험이 직접 만든 기대값."""
    return f"청산 {exits}건, 수익 표본 {n}건, 그린 표본 {drawn}건이고 점과 실제 평균은 그린 표본 {drawn}건으로 냈어요."


OLD_E_NOTES = re.compile(r"실제 평균은 그린 표본 [\d,]+건의 평균이에요|청산 [\d,]+건\s+수익 표본")
"""지운 옛 두 줄(「실제 평균은 그린 표본 N건의 평균이에요」 · 두 칸 공백 나열). 화면 어디에도 없어야 한다."""


def guard_page(root: Path, name: str) -> str:
    """가드레일 화면 하나(실제 서버, 200 단언)."""
    return get_pages(root, [f"/s/{name}"])[f"/s/{name}"]


def charts(html: str) -> list[str]:
    """화면의 차트 SVG들(`role="img"`): 차례로 A, E(E는 표본이 있을 때만)."""
    return _CHART.findall(html)


def squash(fragment: str) -> str:
    """HTML 조각의 보이는 글(태그 벗김, entity 풀림, 공백 하나로)."""
    return " ".join(unescape(re.sub(r"<[^>]+>", " ", fragment)).split())


def legends(html: str) -> list[list[str]]:
    """화면의 범례들(차례로 A, E): 항목마다 보이는 글."""
    return [[squash(li) for li in re.findall(r"<li [^>]*>(.*?)</li>", ul, re.S)] for ul in _LEGEND.findall(html)]


def pc(v: float, digits: int = 1) -> str:
    """부호 붙은 퍼센트(시험이 서식 함수를 빌리지 않고 직접 만든 기대값)."""
    return f"{v * 100:+.{digits}f}%"


def polys(svg: str, cls: str, tag: str = "polyline") -> list[list[tuple[float, float]]]:
    """SVG 안 `class="cls"` 선 · 면의 꼭짓점들(문서 순서)."""
    return [[tuple(map(float, pt.split(","))) for pt in m.split()]
            for m in re.findall(rf'<{tag} class="{cls}" points="([^"]+)"', svg)]


def test_guard_series_match_data(tmp_path):
    """trend_10_50: A 범례 · E 목록의 값이 진짜 nums · data.a_marks · data.e_series에서 나온 것이고 터미널 chart_a의 읽는 자리와 같다."""
    root = write_facts(tmp_path)
    html = guard_page(root, "trend_10_50")
    recs, g = read_days(root, "trend_10_50"), read_guardrail(root, "trend_10_50")[1]
    n, marks, e = recs[-1]["nums"], data.a_marks(recs, g), data.e_series(recs, g)
    a_items = legends(html)[0]
    assert a_items == [
        f"오늘({n['d']}일째) 정상 범위 {pc(n['lo'])}~{pc(n['hi'])}", f"30일 범위 {pc(n['month_lo'])}~{pc(n['month_hi'])}",
        f"같은 기간 백테스트 {pc(n['bt'])} 범위 {n['bt_pos']}", f"실제 {pc(n['actual'])} 범위 {n['pos']}",
        f"경고선 {pc(marks['line'])}까지 {n['room'] * 100:.1f}%p 남았어요", f"놓친 주문 {n['missed_n']}건"]
    assert a_items[2:4] == ["같은 기간 백테스트 -1.9% 범위 안", "실제 -4.6% 범위 아래"]  # facts 모듈 머리글의 숫자
    text = " ".join(visible_text(html).split())
    assert f"거래 {e['n']}건으로 판단 기준 {e['minimum']}건을 넘었어요" in text and "거래 37건으로 판단 기준 30건을 넘었어요" in text
    assert f"거래 1건 평균 실제 {pc(e['run'][-1], 2)} 백테스트 {pc(e['mean'], 2)}" in text and "실제 -0.62% 백테스트 +0.27%" in text
    assert f"엣지가 없어도 이 범위에 들어올 확률 {n['edge0']:.0%}" in text and "확률 79%" in text
    assert f"최근 7일 거래 {e['n7']}건(백테스트 기준 {e['tf_low']}~{e['tf_high']}건)" in text
    assert e_counts(e['exits'], e['n'], len(e['rets'])) in unescape(html)  # 세 수 + 점 · 평균의 분모를 한 문장으로
    head = f"실전 시작 {g['live_start']}  실전 {recs[-1]['live_day']}일째  거래 {recs[-1]['trades']}건  가드레일 v1  기준 {recs[-1]['day']}"
    assert head in html and web.chip("경고") in html and '<h1 class="display">trend_10_50</h1>' in html
    assert "<title>trend_10_50 | BlackClip Guardrail</title>" in html and "/?t=" in html


def test_guard_legend_follows_last_record_and_hides_missing(tmp_path):
    """경고선 여유가 음수면 「넘었어요」, None이면 「모름」, 한 달 범위 · 놓친 주문이 없으면 그 항목만 뺀다."""
    root = write_facts(tmp_path)
    edit_days(root, "trend_10_50", lambda r: r["nums"].update(missed=[], missed_n=0))  # 점이 하나도 없으니 항목도 없다
    edit_last(root, "trend_10_50", lambda r: r["nums"].update(room=-0.013, month_lo=None))
    items = legends(guard_page(root, "trend_10_50"))[0]
    assert "경고선을 1.3%p 넘었어요" in items and not [x for x in items if x.startswith(("30일 범위", "놓친 주문"))]
    assert items[0].startswith("오늘(22일째) 정상 범위 ")
    edit_last(root, "trend_10_50", lambda r: r["nums"].update(room=None))
    assert "경고선 여유 모름" in legends(guard_page(root, "trend_10_50"))[0]


def long_repo(tmp_path: Path) -> tuple[Path, list[dict]]:
    """61일 기록: 15일째 빈 날, 40일째 실제 모름, 31일째 · 61일째 새 구간(61일째는 하루뿐)인 trend_10_50.

    Returns:
        (.blackclip 경로, 기록들).
    """
    root = write_facts(tmp_path, ("trend_10_50",))
    recs = []
    for k in range(1, 62):
        if k == 15:
            continue
        r, p = json.loads(json.dumps(record("trend_10_50", 22))), (k - 1) // 30
        for key in ("trade_rets", "trade_mean", "trade_n", "trade_skip"):
            r["nums"].pop(key, None)
        r["day"], r["live_day"] = (START + timedelta(days=k - 1)).isoformat(), k
        r["nums"].update(period_start=(START + timedelta(days=30 * p)).isoformat(), d=k - 30 * p, actual=None if k == 40 else round(0.0005 * (k % 30), 6),
                         bt=0.0, lo=-0.02, hi=0.03, line=-0.07, missed=[], missed_n=0, room=0.05)
        recs.append(r)
    (root / "strategies" / "trend_10_50" / "daily" / "days.jsonl").write_text(
        "".join(json.dumps(r, ensure_ascii=False) + "\n" for r in recs), encoding="utf-8")
    return root, recs


def two_period_repo(tmp_path: Path) -> Path:
    """trend_10_50 22일 기록을 구간 둘로: 15일째부터 새 구간(period_start = 시작 + 14일), 이번 구간 값은 실전 시작부터 값과 다르다(회고 화면 시험).

    Returns:
        .blackclip 경로.
    """
    root = write_facts(tmp_path, ("trend_10_50",))
    cut = (START + timedelta(days=14)).isoformat()

    def split(r):
        if r["live_day"] >= 15:
            n = r["nums"]
            fee, slip, rest = round(n["fee"] / 2, 6), round(n["slip"] / 2, 6), round(n["rest"] / 2, 6)
            gap = round(fee + slip + rest, 6)
            actual = round(n["actual"] + 0.01, 6)
            n.update(period_start=cut, d=r["live_day"] - 14, fee=fee, slip=slip, rest=rest, gap=gap, actual=actual,
                     bt=round(actual - gap, 6), bt0=round(actual - gap, 6))
    edit_days(root, "trend_10_50", split)
    return root


def retro_special(repo: Path) -> Path:
    """회고 화면의 갈래를 한 레포에 모은다: 실전(구간 하나) · 중지 → 은퇴(날 · 이유 다름) · 중지 + 기록 없음 · 기록 손상 · 첫 회고만(retro.md만).

    Args:
        repo: 레포 꼭대기.

    Returns:
        .blackclip 경로.
    """
    root = write_facts(repo)
    write_record(root, "grid_v1", "중지", "맞지 않아요", "다음 달에 다시 봐요", date(2026, 10, 16))
    write_record(root, "grid_v1", "은퇴", "접기로 했어요", "접어요", date(2026, 10, 18))
    write_record(root, "trend_ema_v7", "중지", None, None, date(2026, 10, 16))
    (root / "strategies" / "trend_ema_v7" / "daily" / "days.jsonl").unlink()
    (root / "strategies" / "breakout_v2" / "record.md").write_text("<<<<<<< HEAD\n", encoding="utf-8")
    only = root / "strategies" / "only_retro"
    only.mkdir()
    (only / "link.py").write_text("print('x')\n", encoding="utf-8")
    (only / "retro.md").write_text(f"{RETRO_HEAD}2026-09-23\n", encoding="utf-8")
    return root


def expected_runs(recs: list[dict], key: str) -> list[list[int]]:
    """기록에서 `nums[key]`가 이어지는 날 번호(첫 날 0)의 묶음: 빈 날 · 값 없는 날 · 구간이 바뀌는 날에서 끊는다(터미널 chart_a 규칙을 날 단위로)."""
    runs, cur, prev = [], [], None
    for r in recs:
        i = (date.fromisoformat(r["day"]) - START).days
        if prev is not None and (i != prev[0] + 1 or r["nums"]["period_start"] != prev[1]):
            runs, cur = [*runs, *([cur] if cur else [])], []
        prev = (i, r["nums"]["period_start"])
        if r["nums"].get(key) is None:
            runs, cur = [*runs, *([cur] if cur else [])], []
        else:
            cur.append(i)
    return [*runs, *([cur] if cur else [])]


def test_a_breaks_at_period_start_and_gaps(tmp_path):
    """빈 날 · 값 없는 날 · 구간 바뀜(31일째 · 61일째)에서 선과 띠가 끊겨, polyline · polygon이 구간 · 빈 날만큼 나뉜다."""
    root, recs = long_repo(tmp_path)
    a = charts(guard_page(root, "trend_10_50"))[0]
    g = read_guardrail(root, "trend_10_50")[1]
    marks = data.a_marks(recs, g)
    assert marks["starts"] == ["2026-10-23", "2026-11-22"]
    cuts = [float(x) for x in re.findall(r'<line class="s-start" x1="([\d.]+)"', a)]
    assert len(cuts) == 2
    step = (cuts[1] - cuts[0]) / 30  # 날 하나의 폭: 30일째(0부터) · 60일째 경계선 사이

    def days_of(points):  # 꼭짓점 x → 날 번호(하루짜리 구간은 눈에 보이게 좌우로 조금 늘려 둠)
        return sorted({round((x - cuts[0]) / step) + 30 for x, _ in points})

    for key, cls in (("actual", "s-actual"), ("bt", "s-bt"), ("line", "s-line")):
        want = expected_runs(recs, key)
        assert [days_of(p) for p in polys(a, cls)] == want, cls
    assert len(expected_runs(recs, "actual")) == 5 and len(expected_runs(recs, "bt")) == 4  # 이 기록에서 끊기는 곳이 정말 있다
    bands = polys(a, "s-band", "polygon")
    assert [days_of(p) for p in bands] == expected_runs(recs, "lo") and len(bands) == 4
    for run in polys(a, "s-actual"):  # 선 하나가 빈 날(14번) 위를 건너지 않는다
        assert not (min(x for x, _ in run) < cuts[0] + (14 - 30) * step < max(x for x, _ in run))


def test_guard_draws_warning_line_and_band(tmp_path):
    """FACTS trend_10_50: A에 정상 범위 띠 · 경고선 · 같은 기간 백테스트 · 실제 · 놓친 주문 점 · 0% 기준선, E에 30건 선."""
    root = write_facts(tmp_path)
    html = guard_page(root, "trend_10_50")
    a, e = charts(html)
    for cls, tag in (("s-band", "polygon"), ("s-line", "polyline"), ("s-bt", "polyline"), ("s-actual", "polyline"), ("s-missed", "path"),
                     ("s-zero", "line")):
        assert re.search(rf'<{tag} class="{cls}" ', a), cls
    assert "s-count" in e and "s-count" not in a
    # 비율을 맞추지 않고 폭에 늘리는 차트라 점은 원이 아니라 길이 0 경로(둥근 끝 + 화면 좌표 굵기라 늘어나도 원)
    assert all('preserveAspectRatio="none"' in svg and "<circle" not in svg for svg in (a, e))
    assert re.findall(r'<path class="s-missed" d="M[\d.]+ [\d.]+h0"/>', a) and len(re.findall(r'<path class="s-dot" d="M[\d.]+ [\d.]+h0"/>', e)) == 37
    assert '<path class="s-missed" d="M14 6h0"/>' in html and '<path class="s-dot" d="M14 6h0"/>' in html  # 범례 견본도 같은 꼴
    recs = read_days(root, "trend_10_50")
    assert a.count('class="s-missed"') == len(data.a_marks(recs, read_guardrail(root, "trend_10_50")[1])["missed"]) == 1
    for svg in (a, e):  # 색은 클래스로만(E의 점 지름 변수 `style="--gaps:N"` 하나만 예외), SVG 안에 <text> 없음, 접근 이름은 해요체 한 문장
        bare = re.sub(r' style="--gaps:\d+"', "", svg)
        assert "<text" not in svg and not re.search(r'\b(?:fill|stroke|style|color)="', bare) and "#" not in svg
        label = re.search(r'aria-label="([^"]+)"', svg)[1]
        assert label.endswith("예요.") and "·" not in label and "니다" not in label


def test_guard_no_band_when_not_drawn(tmp_path):
    """그날 lo · hi가 없고 가드레일이 drawn=False면 띠를 그리지 않고, 안 · 위 · 아래 대신 이유를 말한다."""
    root = write_facts(tmp_path, ("trend_10_50",))
    reason = "백테스트 왕복 거래 80건 · 기간 300일(하한 100건 · 365일)"
    edit_days(root, "trend_10_50", lambda r: r["nums"].update(lo=None, hi=None, month_lo=None, month_hi=None, pos=None, bt_pos=None))
    edit_guardrail(root, "trend_10_50", lambda g: g.update(normal_range={"days": 30, "drawn": False, "reason": reason}))
    html = guard_page(root, "trend_10_50")
    a = charts(html)[0]
    assert "s-band" not in a and data.a_marks(read_days(root, "trend_10_50"), read_guardrail(root, "trend_10_50")[1])["band"] is False
    note = "정상 범위를 그리지 않아 안팎을 말할 수 없어요(백테스트 왕복 거래 80건, 기간 300일(하한 100건, 365일))"
    assert note in squash(html)
    items = legends(html)[0]
    assert not [x for x in items if "정상 범위" in x or x.startswith("30일 범위") or " 범위 안" in x or " 범위 아래" in x]
    assert "그리지 않았어요(백테스트 왕복 거래 80건, 기간 300일(하한 100건, 365일))" in squash(html)  # 기준 표
    assert "같은 기간 백테스트 -1.9%가 정상 범위" not in html and "정상 범위를 그리지 않아 안팎을 말할 수 없어요." in squash(html)  # 원인 셋 전략 줄


def test_guard_e_without_p10_p90(tmp_path):
    """분위가 없는 예전 가드레일: E에 80% 띠가 없고, 있을 때는 띠가 하나 있다."""
    root = write_facts(tmp_path, ("trend_10_50",))
    sentence = "백테스트 거래 가운데 80%는 다음 가드레일 저장부터 그려요"
    with_band = guard_page(root, "trend_10_50")
    assert charts(with_band)[1].count('class="s-band"') == 1 and sentence not in with_band
    edit_guardrail(root, "trend_10_50", lambda g: g["basis"].update(trade_p10=None, trade_p90=None))
    html = guard_page(root, "trend_10_50")
    assert "s-band" not in charts(html)[1] and sentence in html and "s-mean" in charts(html)[1]
    del_basis = lambda g: g.pop("basis")  # noqa: E731 - basis가 아예 없는 예전 가드레일도 같다
    edit_guardrail(root, "trend_10_50", del_basis)
    html = guard_page(root, "trend_10_50")
    assert sentence in html and "s-band" not in charts(html)[1] and "s-mean" not in charts(html)[1] and "백테스트 모름" in squash(html)


def test_guard_e_discloses_missing_samples(tmp_path, monkeypatch):
    """기록 상한 · 화면 상한 · 기록 밖 청산 · 제외한 청산을 한 줄씩 고지하고, 평균은 그린 표본 기준이라고 적는다(평소에는 고지 줄이 없다)."""
    root = write_facts(tmp_path, ("trend_10_50",))
    plain = squash(guard_page(root, "trend_10_50"))
    for word in ("기록 상한", "화면 상한", "기록에 없는 청산", "표본에서 뺐어요", "빠진 거래"):
        assert word not in plain, word
    assert e_counts(37, 37, 37) in plain and OLD_E_NOTES.search(plain) is None

    edit_last(root, "trend_10_50", lambda r: r["nums"].update(trade_n=2500))  # 기록 상한에 걸림
    html = guard_page(root, "trend_10_50")
    e = data.e_series(read_days(root, "trend_10_50"), read_guardrail(root, "trend_10_50")[1])
    assert e["lost"] == 2500 - 37 and e["capped"] == ["2026-09-23"] and e["line30"] is False
    text = squash(html)
    assert f"빠진 거래 {e['lost']:,}건" in text and "기록 상한(구간마다 2,000건)에 걸려 앞쪽 거래가 빠진 구간이 있어요: 2026-09-23부터" in text
    assert e_counts(37, "2,500", 37) in text and OLD_E_NOTES.search(text) is None and "s-count" not in charts(html)[1]
    edit_last(root, "trend_10_50", lambda r: r["nums"].update(trade_n=37))

    with monkeypatch.context() as m:  # 화면 상한에 걸림
        m.setattr(dashboard, "E_CAP", 5)
        html = guard_page(root, "trend_10_50")
    e = data.e_series(read_days(root, "trend_10_50"), read_guardrail(root, "trend_10_50")[1], 5)
    assert (e["kept"], len(e["rets"]), e["lost"], e["capped"]) == (37, 5, 32, [])
    text = squash(html)
    assert "화면 상한 5건을 넘어 앞쪽 32건은 그리지 않았어요." in text and "기록 상한" not in text
    assert e_counts(37, 37, 5) in text and OLD_E_NOTES.search(text) is None  # 평균의 분모는 그린 표본, 수익 표본 37이 아니다
    assert "빠진 거래 32건이라 점과 평균은 그린 표본 5건 기준이에요." in text and charts(html)[1].count('class="s-dot"') == 5

    edit_last(root, "trend_10_50", lambda r: r.update(trades=40))  # 청산은 40건인데 표본은 37건
    html = guard_page(root, "trend_10_50")
    assert "매일 대조 기록에 없는 청산 3건이 있어요." in squash(html) and "s-count" not in charts(html)[1]
    edit_last(root, "trend_10_50", lambda r: (r.update(trades=37), r["nums"].update(trade_skip=2)))
    html = guard_page(root, "trend_10_50")
    assert "실전 전에 연 포지션의 청산 2건은 표본에서 뺐어요." in squash(html) and "s-count" in charts(html)[1]
    edit_last(root, "trend_10_50", lambda r: r["nums"].update(trade_skip=0, trade_rets=None, trade_mean=None, trade_n=None))
    html = guard_page(root, "trend_10_50")
    assert "거래 1건 수익 기록이 아직 없어요. 다음 매일 대조부터 세어요." in html and len(charts(html)) == 1


def e_rule_n(gaps: int) -> int:
    """점 지름(간격 × `DOT_RATIO`)이 `DOT_MIN`px 아래가 되는 SVG 폭의 올림. 상수 `DOT_MIN` · `DOT_RATIO` · `_CW` · `_CPAD`에서 분수로 계산한다."""
    return math.ceil(Fraction(web.DOT_MIN) / Fraction(str(web.DOT_RATIO)) * gaps * web._CW / (web._CW - 2 * web._CPAD))


def e_rule(gaps: int, i: int = 1) -> str:
    """차트 E 바로 앞에 나오는 `<style>` 한 줄(경계 폭 N은 `e_rule_n`, 조건 안 `3.5rem` · 간격은 `.fig` 열 값과 같다).
    점을 빼는 같은 규칙이 범례의 점 견본 줄(범례 첫 줄 「실제 거래 1건」)도 뺀다."""
    col = re.search(r"\.fig\{[^}]*grid-template-columns:(\S+) minmax", web.css())[1]  # `.fig` 첫 열 값(루트 글자 크기를 따르는 rem)
    return (f"<style>@container (width < calc({col} + {web.SPACE[1]}px + {e_rule_n(gaps)}px)){{#e{i} .s-dot{{display:none}} "
            f"#e{i} .e-dots-off{{display:block}} #e{i} .legend-item:first-child{{display:none}}}}</style>")


def test_e_dot_width_follows_gaps(tmp_path):
    """차트 E SVG에 `--gaps:{k-1}`(k = 그린 표본 수)이 붙는다. 점이 하나 이하면 속성도 규칙도 없다. 차트 A · 범례 견본에는 없다."""
    root = write_facts(tmp_path, ("trend_10_50",))
    html = guard_page(root, "trend_10_50")
    a, e = charts(html)
    k = len(data.e_series(read_days(root, "trend_10_50"), read_guardrail(root, "trend_10_50")[1])["rets"])
    assert k == 37 and re.findall(r' style="--gaps:(\d+)"', html) == [str(k - 1)] and ' style="--gaps:36"' in e
    assert "--gaps" not in a and not any("--gaps" in ul for ul in _LEGEND.findall(html))  # 차트 A 점(s-missed) · 범례 견본은 지금 모양
    assert web._SWATCH["dot"] == '<path class="s-dot" d="M14 6h0"/>'

    edit_last(root, "trend_10_50", lambda r: r["nums"].update(trade_rets=[0.012, -0.004], trade_mean=0.004, trade_n=2))
    html = guard_page(root, "trend_10_50")
    assert re.findall(r' style="--gaps:(\d+)"', html) == ["1"] and charts(html)[1].count('class="s-dot"') == 2  # 점 둘 = 간격 하나

    edit_last(root, "trend_10_50", lambda r: r["nums"].update(trade_rets=[0.012], trade_mean=0.012, trade_n=1))
    html = guard_page(root, "trend_10_50")
    one = charts(html)[1]
    assert one.count('class="s-dot"') == 1 and "--gaps" not in one  # 점 하나는 간격이 없다
    assert "<style>@container" not in html and "e-dots-off" not in html.split("</style>", 1)[1] and 'id="e1"' not in html  # 규칙 · 안내 줄 · id도 없다


def test_e_dots_hidden_below_width_threshold(tmp_path):
    """E 차트마다 `<style>` 하나가 바로 앞에 나와, 점 간격이 3.75px 아래가 되는 폭에서 점을 빼고 안내 한 줄을 보인다.

    경계 `N = ceil(3.75 × gaps × 720 / 708)`는 상수에서 만든 식과 같고, 순번 id는 화면 안에서 겹치지 않으며(렌더 호출마다 1부터),
    안내 줄은 평소 숨는다. 점이 하나 이하면 규칙이 없다. 화면 글은 규칙(해요체 · 가운뎃점 없음)을 지킨다.
    """
    root = write_facts(tmp_path, ("trend_10_50",))
    html = guard_page(root, "trend_10_50")
    gaps = len(data.e_series(read_days(root, "trend_10_50"), read_guardrail(root, "trend_10_50")[1])["rets"]) - 1
    rule = e_rule(gaps)
    assert gaps == 36 and e_rule_n(gaps) == 138 and html.count("<style>@container") == 1 and html.count(rule) == 1
    assert (rule + '<div class="fig" id="e1">') in html  # 그 차트 바로 앞, 범위는 그 차트 하나(#e1)
    # 컨테이너 규칙은 컨테이너 안쪽만 고칠 수 있어 범례를 .fig 안(안내 줄 뒤)에 두고, 범례 첫 줄이 점 견본이다
    assert re.search(r'<div class="fig" id="e1">.*?<p class="e-dots-off">[^<]*</p><ul class="legend" aria-label="범례">'
                     r'<li class="legend-item"><svg class="sw"[^>]*><path class="s-dot" d="M14 6h0"/></svg><span>실제 거래 1건</span></li>.*?</ul></div>', html, re.S)
    assert re.search(r"\.fig>\.legend\{grid-column:1/-1;", web.css())
    assert html.count('id="e1"') == 1 and guard_page(root, "trend_10_50").count('id="e1"') == 1  # 전역 카운터가 없어 다시 그려도 1부터
    fig = html.split('<div class="fig" id="e1">', 1)[1].split("</svg>", 1)[1]
    assert re.match(r'<div class="xax" aria-hidden="true"><span>[^<]*</span><span>[^<]*</span></div>'
                    r'<p class="e-dots-off">점이 겹치지 않게 이 폭에서는 점은 빼고 누적 평균 선만 그렸어요.</p><ul class="legend"', fig)  # 안내 줄 · 범례는 .fig 안(컨테이너 안)에
    css = web.css()
    assert re.search(r"\.e-dots-off\{display:none;", css)  # 평소 숨음
    assert copy_violations(html) == []

    e = data.e_series(read_days(root, "trend_10_50"), read_guardrail(root, "trend_10_50")[1])
    for k in (2, 3, 38, 100, 1000, 5000):  # 간격 수마다 자기 경계
        synth = {**e, "rets": [0.001 * (j % 9 - 4) for j in range(k)]}
        synth["run"] = [sum(synth["rets"][:j + 1]) / (j + 1) for j in range(k)]
        assert e_rule(k - 1) in web.e_svg(synth, nth=1), k
    both = web.e_svg(e, nth=1) + web.e_svg(e, nth=2)  # 한 화면에 E가 둘이면 부르는 쪽이 순번을 다르게 넘긴다
    with pytest.raises(TypeError):  # 순번은 기본값 없이 부르는 쪽이 반드시 넘긴다(빠뜨리면 id가 겹침)
        web.e_svg(e)
    assert re.findall(r'id="e(\d+)"', both) == ["1", "2"] and both.count(e_rule(gaps, 1)) == 1 and both.count(e_rule(gaps, 2)) == 1


def test_dot_constants_drive_css_and_boundary(tmp_path, monkeypatch):
    """점 지름 하한 · 상한 · 간격 비율 상수(`DOT_MIN` · `DOT_MAX` · `DOT_RATIO`) 하나만 바꿔도 CSS 식 · 기본 지름 · 경계 N이 같이 따라간다.

    경계 올림은 간격 수 1~4999(`E_CAP` 5000개 점) 전부에서 분수로 계산한 값과 같다.
    """
    root = write_facts(tmp_path, ("trend_10_50",))
    e = data.e_series(read_days(root, "trend_10_50"), read_guardrail(root, "trend_10_50")[1])
    assert all(web._dots_off_width(g) == e_rule_n(g) for g in range(1, dashboard.E_CAP))
    for lo, hi, ratio in ((3, 6, 0.8), (4, 9, 0.5), (2, 5, 0.75)):
        with monkeypatch.context() as m:
            m.setattr(web, "DOT_MIN", lo)
            m.setattr(web, "DOT_MAX", hi)
            m.setattr(web, "DOT_RATIO", ratio)
            css, r = web.css(), str(ratio).removeprefix("0")
            assert (f"@supports (width:1cqw){{.s-dot{{stroke-width:clamp({lo}px, calc((100cqw - 3.5rem - var(--studio-space-2)) * 708 / 720 * {r}"
                    f" / var(--gaps, 1)), {hi}px)}}}}") in css
            assert f"stroke:var(--studio-color-ink);stroke-width:{hi};" in css  # 기본 지름 = 상한
            assert e_rule(len(e["rets"]) - 1) in web.e_svg(e, nth=1)  # 경계 N도 같은 상수에서(e_rule_n은 상수를 읽는다)


def test_cause_title_asks_customer_question(tmp_path):
    """원인 셋 패널의 보이는 제목은 고객 물음 `어디서 생긴 문제예요?`, 아래 sub 줄과 표 · 목록 접근성 이름은 그대로다."""
    root = write_facts(tmp_path, ("trend_10_50",))
    html = guard_page(root, "trend_10_50")
    assert '<h2 class="h2">어디서 생긴 문제예요?</h2><p class="sub">전략이 망가졌는지, 실행에서 새는지, 봇이 멈췄는지 따로 봐요.</p>' in html
    assert '<h2 class="h2">원인 셋</h2>' not in html and "원인 셋" not in re.sub(r"<[^>]+>", " ", html)  # 보이는 글에는 없고
    assert 'aria-label="원인 셋 표"' in html and 'aria-label="원인 셋 목록"' in html  # 안쪽 이름은 그대로
    assert copy_violations(html) == []


def cause(rec: dict, market: str | None = "spot") -> dict[str, tuple[str, str]]:
    """`web.causes` 결과를 원인 → (칩 낱말, 근거)로(시장은 가드레일 `target.market`, facts는 현물)."""
    return {q: (w, ev) for q, w, ev in web.causes(rec, market)}


def with_states(rec: dict, **kw) -> dict:
    """기록 사본의 칸 상태 · nums를 바꾼다(`전략=` · `실행=` · `봇=`은 states, `nums=`는 nums.update)."""
    r = json.loads(json.dumps(rec))
    r["nums"].update(kw.pop("nums", {}))
    r["states"].update(kw)
    return r


def test_cause_templates_follow_state(tmp_path):
    """전략 칸 상태(정상 · 경고 · 판단 보류 · 확인 불가)마다 정해진 문장. 정상인데 「거래가 더 쌓여야」는 없다."""
    ema, ten = record("trend_ema_v7", 22), record("trend_10_50", 22)
    assert ema["states"]["전략"] == "정상" and ten["states"]["전략"] == "판단 보류"
    bt = pc(ten["nums"]["bt"])
    word, ev = cause(ema)["전략이 망가졌나"]
    assert word == "정상" and ev == f"같은 기간 백테스트 {pc(ema['nums']['bt'])}가 정상 범위 안이에요." and "거래가 더 쌓여야" not in ev
    assert cause(ten)["전략이 망가졌나"] == ("판단 보류", f"같은 기간 백테스트 {bt}가 정상 범위 안이에요. "
                                         f"엣지가 없어도 이 범위에 들어올 확률이 {ten['nums']['edge0']:.0%}라 거래가 더 쌓여야 판단할 수 있어요.")
    assert cause(with_states(ten, nums={"edge0": None, "bt_pos": "위"}))["전략이 망가졌나"][1] == f"같은 기간 백테스트 {bt}가 정상 범위 위예요."
    over = cause(with_states(ten, nums={"edge0": 0.995}))["전략이 망가졌나"][1]  # 99%를 넘으면 「99% 넘게라」가 아니라 자연스럽게 잇는다
    assert over == f"같은 기간 백테스트 {bt}가 정상 범위 안이에요. 엣지가 없어도 이 범위에 들어올 확률이 99%를 넘어 거래가 더 쌓여야 판단할 수 있어요."
    assert copy_violations(f"<p>{over}</p>") == [] and "넘게라" not in over
    assert cause(with_states(ten, 전략="경고", nums={"bt_pos": "아래"}))["전략이 망가졌나"] == ("경고", f"같은 기간 백테스트 {bt}가 정상 범위 아래예요.")
    assert cause(with_states(ten, 전략="확인 불가(체결 내역을 못 받음)"))["전략이 망가졌나"] == ("확인 불가", "판단하지 않았어요(체결 내역을 못 받음).")
    assert cause(with_states(ten, 전략="점검 불가"))["전략이 망가졌나"] == ("점검 불가", "판단하지 않았어요(기록이 없어요).")
    assert cause(with_states(ten, nums={"bt_pos": None}))["전략이 망가졌나"][1] == "정상 범위를 그리지 않아 안팎을 말할 수 없어요."
    assert cause(with_states(ten, nums={"bt": None}))["전략이 망가졌나"][1].startswith("같은 기간 백테스트 모름이 정상 범위 안이에요.")
    assert [q for q, _, _ in web.causes(ten, "spot")] == ["전략이 망가졌나", "실행에서 새나", "봇이 멈췄나"]
    n = ten["nums"]
    assert cause(ten)["봇이 멈췄나"] == ("정상", f"최근 7일 거래는 {n['n7']}건이고 백테스트 기준은 {n['tf_low']}~{n['tf_high']}건이에요.")
    assert cause(with_states(ten, nums={"n7": None}))["봇이 멈췄나"][1] == "최근 7일 거래는 아직 비교하지 않았어요."
    big = cause(with_states(ten, nums={"n7": 1234, "tf_low": 1000, "tf_high": 2000, "missed_n": 1500}))  # 1,000 이상은 쉼표
    assert big["봇이 멈췄나"][1] == "최근 7일 거래는 1,234건이고 백테스트 기준은 1,000~2,000건이에요."
    assert "거래 1,500건이 놓친 주문" in big["실행에서 새나"][1]
    assert not [x for _, _, x in web.causes(with_states(ten, nums={"n7": 1234, "tf_low": 1000, "tf_high": 2000, "missed_n": 1500}), "spot")
                if copy_violations(f"<p>{x}</p>")]
    # 화면: 같은 문장이 원인 셋 표에 있다
    root = write_facts(tmp_path)
    for name in ("trend_ema_v7", "trend_10_50", "breakout_v2"):
        html = squash(guard_page(root, name))
        for q, _w, ev in web.causes(read_days(root, name)[-1], "spot"):
            assert q in html and ev in html, (name, q)
    assert "판단하지 않았어요(체결 내역을 못 받음)." in squash(guard_page(root, "breakout_v2"))


def test_cause_position_follows_engine_bt_pos(tmp_path):
    """엔진은 같은 기간 백테스트가 범위 위여도 「정상」을 낸다(`reconcile.strategy_cause`). 원인 셋 근거의 안 · 위 · 아래는
    상태가 아니라 엔진이 저장한 `bt_pos`라 A 범례와 같은 낱말이다."""
    g, last = guardrail("trend_ema_v7"), record("trend_ema_v7", 22)
    d = last["nums"]["d"]
    lo, hi = g["normal_range"]["daily_low"][d - 1], g["normal_range"]["daily_high"][d - 1]
    bt = hi + 0.01
    word, why = strategy_cause(g, bt, d, last["trades"])  # 엔진 판정: 범위 위 + 거래 충분 + 엣지 0 확률 낮음 → 정상
    assert word == "정상" and "위)" in why and _where(bt, lo, hi) == "위"
    root = write_facts(tmp_path)
    edit_last(root, "trend_ema_v7", lambda r: (r["nums"].update(bt=bt, bt_pos=_where(bt, lo, hi)), r["states"].update(전략=word)))
    html = guard_page(root, "trend_ema_v7")
    legend = next(x for x in legends(html)[0] if x.startswith("같은 기간 백테스트"))
    q, w, ev = web.causes(read_days(root, "trend_ema_v7")[-1], "spot")[0]
    assert legend == f"같은 기간 백테스트 {pc(bt)} 범위 위" and (q, w) == ("전략이 망가졌나", "정상")
    assert ev == f"같은 기간 백테스트 {pc(bt)}가 정상 범위 위예요." and ev in squash(html)


def test_cause_execution_shows_funding(tmp_path):
    """펀딩비는 가드레일 `target.market`이 선물일 때만 말한다. 엔진은 현물에도 `funding` 키를 쓰고 값은 None이다.

    선물 + 값 → 「펀딩비는 {x}%p예요」, 선물 + None → 「받은 범위가 구간을 덮지 못해 모름이에요」, 현물 → 문장 없음(키가 있어도).
    """
    ten = record("trend_10_50", 22)
    n = ten["nums"]
    assert "funding" in n and n["funding"] is None  # 엔진이 현물에 쓰는 모양: 키는 있고 값은 None
    gap = f"{n['gap'] * 100:+.1f}%p"
    base_ev = (f"실제와 비용 0 같은 기간 백테스트의 차이는 {gap}예요. 백테스트에만 있는 거래 {n['missed_n']}건이 놓친 주문으로 추정돼요. "
               f"체결가 차이 중앙값 {n['slip_med_bp']}bp, 최대 {n['slip_max_bp']}bp예요.")
    assert cause(ten, "spot")["실행에서 새나"] == ("경고", base_ev)
    assert cause(with_states(ten, nums={"funding": -0.0012}), "spot")["실행에서 새나"][1] == base_ev  # 현물인데 값이 있어도 말하지 않음
    assert cause(ten, None)["실행에서 새나"][1] == base_ev
    assert cause(ten, "futures")["실행에서 새나"][1] == base_ev + " 펀딩비는 받은 범위가 구간을 덮지 못해 모름이에요."
    assert cause(with_states(ten, nums={"funding": -0.0012}), "futures")["실행에서 새나"][1] == base_ev + " 펀딩비는 -0.1%p예요."
    quiet = with_states(ten, 실행="정상", nums={"missed_n": 0, "missed": [], "slip_med_bp": None, "slip_max_bp": None})
    assert cause(quiet, "futures")["실행에서 새나"][1] == (f"실제와 비용 0 같은 기간 백테스트의 차이는 {gap}예요. "
                                                    "펀딩비는 받은 범위가 구간을 덮지 못해 모름이에요.")
    assert cause(quiet, "spot")["실행에서 새나"][1] == f"실제와 비용 0 같은 기간 백테스트의 차이는 {gap}예요."
    assert cause(with_states(ten, nums={"gap": None}), "spot")["실행에서 새나"][1].startswith("실제와 비용 0 같은 기간 백테스트의 차이는 모름이에요.")
    # 화면: FACTS는 모두 현물이라 어느 전략 화면에도 펀딩비 문장이 없다. 선물 가드레일이면 값이 없을 때 · 있을 때 각각 문장이 나온다.
    root = write_facts(tmp_path)
    for name in NAMES:
        assert read_guardrail(root, name)[1]["target"]["market"] == "spot" and "펀딩비" not in guard_page(root, name), name
    edit_guardrail(root, "trend_10_50", lambda g: g["target"].update(market="futures"))
    assert "펀딩비는 받은 범위가 구간을 덮지 못해 모름이에요." in squash(guard_page(root, "trend_10_50"))
    edit_last(root, "trend_10_50", lambda r: r["nums"].update(funding=-0.0012))
    assert "펀딩비는 -0.1%p예요." in squash(guard_page(root, "trend_10_50"))


def test_guard_big_counts_have_commas(tmp_path):
    """거래 빈도 · 놓친 주문 수가 1,000 이상이면 천 단위 쉼표(원인 셋 · 범례 · 옆 목록), 문구 검사도 통과."""
    root = write_facts(tmp_path)
    edit_last(root, "trend_10_50", lambda r: r["nums"].update(n7=1234, tf_low=1000, tf_high=2000, missed_n=1500))
    html = guard_page(root, "trend_10_50")
    text = squash(html)
    assert "최근 7일 거래는 1,234건이고 백테스트 기준은 1,000~2,000건이에요." in text
    assert "최근 7일 거래 1,234건(백테스트 기준 1,000~2,000건)" in text
    assert "놓친 주문 1,500건" in legends(html)[0] and "백테스트에만 있는 거래 1,500건이 놓친 주문으로 추정돼요." in text
    assert copy_violations(html) == []


def test_guard_live_day_has_comma(tmp_path):
    """머리의 실전 일수도 1,000 이상이면 천 단위 쉼표(옆 거래 수와 같은 규칙)."""
    root = write_facts(tmp_path)
    edit_last(root, "trend_10_50", lambda r: r.update(live_day=1234))
    html = guard_page(root, "trend_10_50")
    assert "실전 1,234일째" in squash(html) and copy_violations(html) == []


def test_dashboard_help_has_no_middle_dot(capsys):
    """대시보드 CLI가 터미널에 내는 새 글(하위 명령 도움말)에는 가운뎃점이 없다(기존 help 글은 그대로)."""
    assert main(["--help"]) == 0
    top = capsys.readouterr().out
    line = top[top.index("    dashboard "):top.index("    schedule ")]
    assert main(["dashboard", "--help"]) == 0
    assert "127.0.0.1" in line and "·" not in line + capsys.readouterr().out


def test_guard_missed_legend_only_with_a_dot(tmp_path):
    """놓친 주문 범례는 점이 하나라도 찍힐 때만(터미널 `dots > 0`). 놓친 날의 실제 값이 없어 점이 없으면 항목도 없다."""
    root = write_facts(tmp_path)
    html = guard_page(root, "trend_10_50")
    assert charts(html)[0].count('class="s-missed"') == 1 and "놓친 주문 1건" in legends(html)[0]
    edit_days(root, "trend_10_50", lambda r: r["nums"].update(actual=None) if r["day"] == "2026-10-13" else None)  # 놓친 주문이 있는 날
    recs = read_days(root, "trend_10_50")
    marks = data.a_marks(recs, read_guardrail(root, "trend_10_50")[1])
    assert marks["missed"] == {"2026-10-13"}  # 놓친 주문은 있지만 그 날 실제 값이 없다
    html = guard_page(root, "trend_10_50")
    assert "s-missed" not in charts(html)[0] and not [x for x in legends(html)[0] if x.startswith("놓친 주문")]

def test_guard_causes_are_templates(tmp_path):
    """엔진이 만든 원인 글(`lines`)과 `head`는 화면 어디에도 없다. 한 칸에 글을 지어 넣어도 템플릿만 나온다."""
    root = write_facts(tmp_path)
    for name in NAMES:
        html = unescape(guard_page(root, name))
        rec = read_days(root, name)[-1]
        assert not [k for k, v in rec["lines"].items() if v in html], name
        assert rec["head"] not in html and "수수료" not in html and "원인 후보" not in html, name
    edit_last(root, "trend_10_50", lambda r: r["lines"].update(전략="<b>지어낸 원인</b> 합니다"))
    assert "지어낸 원인" not in guard_page(root, "trend_10_50")


def status_of(root: Path, paths: list[str], token: bool = True) -> dict[str, int]:
    """주소마다 GET 상태 코드(실제 서버, 끝에 shutdown · server_close). token=False면 `?t=` 없이."""
    server = dashboard.make_server(root, 0)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    try:
        out = {}
        for path in paths:
            c = http.client.HTTPConnection("127.0.0.1", server.server_address[1], timeout=5)
            c.request("GET", f"{path}?t={server.token}" if token else path)
            out[path] = c.getresponse().status
            c.close()
        return out
    finally:
        server.shutdown()
        server.server_close()


def test_guard_unknown_name_404(tmp_path):
    """없는 이름 · 가드레일이 없는 폴더 · 파일 · 끈 전략도 아닌 이름은 404. 있는 이름도 토큰이 없으면 403(검사가 라우트보다 먼저)."""
    root = write_facts(tmp_path, ("trend_10_50",))
    (root / "strategies" / "no_guard").mkdir()
    (root / "strategies" / "a_file").write_text("x", encoding="utf-8")
    got = status_of(root, ["/s/trend_10_50", "/s/nope", "/s/no_guard", "/s/a_file", "/s/", "/s/trend_10_50/x", "/s"])
    assert got == {"/s/trend_10_50": 200, "/s/nope": 404, "/s/no_guard": 404, "/s/a_file": 404, "/s/": 404, "/s/trend_10_50/x": 404, "/s": 404}
    assert status_of(root, ["/s/trend_10_50", "/s/nope"], token=False) == {"/s/trend_10_50": 403, "/s/nope": 403}


def test_guard_bad_name_404(tmp_path):
    """`..` · `%2e%2e` · 65자 · 모양이 다른 이름은 폴더를 찾아보기 전에 404(65자 폴더가 진짜로 있어도)."""
    root = write_facts(tmp_path, ("trend_10_50",))
    paths = ["/s/..", "/s/%2e%2e", "/s/%2E%2E%2Ftrend_10_50", "/s/" + "a" * 65, "/s/trend%5F10_50", "/s/_x", "/s/a.b", "/s/-a"]
    (root / "strategies" / ("a" * 65)).mkdir()
    (root / "strategies" / ("a" * 65) / "guardrail.v1.yaml").write_text("{}", encoding="utf-8")
    assert status_of(root, paths) == dict.fromkeys(paths, 404)


def test_guard_stopped_and_broken(tmp_path, monkeypatch):
    """끈 전략(중지 · 은퇴 · 기록 손상): 머리에 칩 대신 상태 줄 하나, 그 아래는 마지막 기록 기준으로 그대로. 읽지 못하면 사유 한 줄."""
    root = write_facts(tmp_path)
    stop(tmp_path, monkeypatch, "grid_v1", "중지")
    stop(tmp_path, monkeypatch, "trend_ema_v7", "은퇴")
    (root / "strategies" / "breakout_v2" / "record.md").write_text("<<<<<<< HEAD\n", encoding="utf-8")
    state, since = stopped(root)["grid_v1"]
    html = guard_page(root, "grid_v1")
    text = squash(html)
    assert state == "중지" and f"중지한 전략이에요({since}부터). 매일 대조는 멈춰 있어요." in text
    assert re.search(r'끈 뒤 회고는 <a class="link" href="/s/grid_v1/retro\?t=[\w-]+">회고 화면</a>에서 볼 수 있어요\.', html)  # 「회고 화면」이 링크
    assert "after.md에 있어요" not in text and "끈 뒤 회고는 회고 화면에서 볼 수 있어요." in squash(html).replace("회고 화면 에서", "회고 화면에서")
    assert (tmp_path / ".blackclip" / "strategies" / "grid_v1" / "after.md").is_file() and copy_violations(html) == []
    assert main(["record", "grid_v1"]) == 2  # 예전 안내 「blackclip record grid_v1」은 --state가 없어 종료 2였다
    assert 'class="chip' not in html.split("<h2")[0] and len(charts(html)) == 1  # 칩은 원인 셋 표에만, A는 마지막 기록 기준(grid_v1은 E 표본이 없음)
    assert "은퇴한 전략이에요(" in squash(guard_page(root, "trend_ema_v7"))
    assert ("전략 기록(record.md)을 읽지 못해 매일 대조가 멈췄어요. git 충돌이면 풀고, 아니면 record.md를 지운 뒤 blackclip record로 다시 적어 주세요."
            in squash(guard_page(root, "breakout_v2")))
    (root / "strategies" / "grid_v1" / "daily" / "days.jsonl").write_text("{broken\n", encoding="utf-8")  # 끈 전략의 기록이 안 읽힘
    html = guard_page(root, "grid_v1")
    assert "매일 대조 기록 파일을 읽지 못했어요(링크이거나 모양이 달라요)" in html and charts(html) == [] and str(tmp_path) not in html
    for f in (root / "strategies" / "trend_ema_v7").glob("guardrail.v*.yaml"):  # 가드레일 판이 없는 끈 전략도 404가 아니라 상태 줄 + 사유
        f.unlink()
    html = guard_page(root, "trend_ema_v7")
    assert "은퇴한 전략이에요(" in squash(html) and "가드레일 파일이 없어요" in html and charts(html) == []


def test_guard_without_records(tmp_path):
    """기록이 없는 실전 전략: 칩 없이 머리 + 안내 한 줄 + 가드레일 표만(차트 · 원인 셋 · 대응 없음)."""
    root = write_facts(tmp_path)
    (root / "strategies" / "grid_v1" / "daily" / "days.jsonl").unlink()
    html = guard_page(root, "grid_v1")
    text = squash(html)
    assert "매일 대조 기록이 아직 없어요. 첫 대조가 끝나면 이 화면에 그려져요." in text
    assert 'class="chip' not in html and "<svg" not in html and "원인 셋" not in text and "대응" not in text
    assert "실전 시작 2026-09-23  가드레일 v1" in html and "가드레일 v1(실돈 넣기 전에 정한 기준)" in text
    (root / "strategies" / "trend_ema_v7" / "guardrail.v1.yaml").write_text("{broken\n", encoding="utf-8")  # 가드레일이 깨짐
    html = guard_page(root, "trend_ema_v7")
    assert web.chip("확인 불가") in html and "가드레일 파일을 읽지 못했어요(링크이거나 모양이 달라요)" in html and "<table" not in html
    (root / "strategies" / "trend_10_50" / "daily" / "days.jsonl").write_text("{broken\n", encoding="utf-8")  # 기록이 깨짐: 가드레일 표는 남음
    html = guard_page(root, "trend_10_50")
    assert web.chip("확인 불가") in html and "매일 대조 기록 파일을 읽지 못했어요(링크이거나 모양이 달라요)" in html
    assert "가드레일 v1(실돈 넣기 전에 정한 기준)" in squash(html) and str(tmp_path) not in html


def test_guard_response_is_quoted_and_escaped(tmp_path, monkeypatch):
    """대응은 값마다 인용 블록: 평문화 + escape(HTML이 먹히지 않고 · 가운뎃점은 그대로), 줄바꿈은 보임, 빈 값은 인용을 만들지 않는다."""
    root = guard_special(tmp_path, monkeypatch)
    html = guard_page(root, "trend_10_50")
    quotes = re.findall(r'<blockquote class="quote">(.*?)</blockquote>', html, re.S)
    ew = "&lt;script&gt;alert(1)&lt;/script&gt; 합니다 · \\u2588"
    assert quotes == [ew, ew, "줄 하나\n줄 둘"] and "<script" not in html  # 머리 인용(걸린 실행 경고) 하나 + 아래 패널 둘(걸린 것 먼저)
    assert "실행 경고" in html and "엔진은 주문을 내거나 봇을 멈추지 않아요. 멈출지는 직접 정해요." in html
    assert copy_violations(html) == []  # 고객 글(인용)은 문구 검사 밖
    edit_last(root, "trend_10_50", lambda r: r.update(response={"execution_warning": "", "warning_line": "  "}))
    html = guard_page(root, "trend_10_50")
    assert "<blockquote" not in html and "가드레일에 적어 둔 대응이 없어요." in html and "엔진은 주문을 내거나 봇을 멈추지 않아요." in html


def set_states(root: Path, name: str, **cols: str) -> None:
    """마지막 기록의 칸 상태를 바꾸고 대표 상태(rep · title)도 엔진과 같은 `representative`로 다시 정한다.

    Args:
        root: .blackclip 경로.
        name: 전략 이름.
        **cols: 칸 이름 → 새 상태 글(예 `선="경고"`).
    """
    def change(rec):
        rec["states"] = rec["states"] | cols
        rec["rep"], key = states.representative(rec["states"])
        rec["title"] = states.TITLES[key]

    edit_last(root, name, change)


EXE, LINE = RESPONSE["execution_warning"], RESPONSE["warning_line"]
NOT_YET = '<p class="sub">아직 안 걸림</p>'


def head_quotes(html: str) -> list[str]:
    """머리 패널(첫 구역 제목 앞)의 「적어 둔 대응」 인용 글들. 라벨 바로 뒤에 인용이 붙은 것만 센다."""
    return re.findall(r'<p class="sub">적어 둔 대응</p><blockquote class="quote">(.*?)</blockquote>', html.split("<h2")[0], re.S)


def panel_order(html: str) -> list[str]:
    """아래 대응 패널의 라벨 · 「아직 안 걸림」 caption이 나오는 순서."""
    return re.findall(r'<p class="sub">(실행 경고|경고선|아직 안 걸림)</p>', html.split("대응(가드레일에 적어 둔 것)</h2>")[1])


def test_guard_head_quote_follows_triggered_response(tmp_path, monkeypatch):
    """지금 걸린 대응 인용 하나가 머리 패널의 이유 줄 바로 아래에 있다. 걸린 것이 없거나 끈 전략이면 없다.

    걸렸는지는 칩 낱말이 아니라 `reconcile.triggered`가 정한다: 대표가 확인 불가 · 점검 불가여도 걸린 대응은 보인다.
    """
    root = write_facts(tmp_path)
    html = guard_page(root, "trend_10_50")  # FACTS: 실행 경고만 걸림(경고선은 2.6%p 남음)
    assert head_quotes(html) == [EXE]
    top = html.split("<h2")[0]
    assert top.index('<p class="why why-warn">') < top.index('<p class="sub">적어 둔 대응</p>') < top.index("실전 시작 2026-09-23")  # 이유 줄 바로 아래, 사실 줄 앞
    assert html.index("적어 둔 대응") < html.index('<h2 class="h2">지금 위험한가</h2>')  # 첫 구역보다 위
    quiet = {"전략": "판단 보류", "실행": "정상", "봇": "정상", "선": "정상", "논지": "해당 없음"}
    cases = [({"선": "경고"}, [LINE]),                                # 선만 걸림
             ({"실행": "경고", "선": "경고"}, [EXE]),                   # 둘 다: 대표 칸(실행)의 대응
             ({"전략": "경고", "선": "경고"}, [LINE]),                  # 대표 칸(전략)에는 대응이 없어 걸린 것 가운데 첫째
             ({"전략": "경고", "봇": "경고"}, []),                      # 경고지만 대응이 걸린 칸은 아님
             ({}, []),                                                  # 판단 보류(전략)만
             ({"실행": "확인 불가(체결 내역을 못 받음)", "선": "경고"}, [LINE]),   # 칩이 확인 불가여도 걸린 대응은 보인다
             ({"전략": "점검 불가(연결 파일 바뀜)", "실행": "경고"}, [EXE])]    # 대표가 점검 불가여도 마찬가지
    for cols, want in cases:
        set_states(root, "trend_10_50", **(quiet | cols))
        assert head_quotes(guard_page(root, "trend_10_50")) == want, cols
    for name in ("trend_ema_v7", "grid_v1", "breakout_v2"):  # 정상 · 판단 보류 · 확인 불가(걸린 대응 없음)
        assert head_quotes(guard_page(root, name)) == [], name
    set_states(root, "trend_10_50", **(quiet | {"실행": "확인 불가(체결 내역을 못 받음)", "선": "경고"}))
    html = guard_page(root, "trend_10_50")
    top = html.split("<h2")[0]
    assert web.chip("확인 불가") in top and head_quotes(html) == [LINE]  # 확인 불가 칩 + 경고선 걸림: 머리 인용이 있다
    assert top.index('<p class="why">') < top.index('<p class="sub">적어 둔 대응</p>') < top.index("실전 시작 2026-09-23")
    set_states(root, "trend_10_50", **(quiet | {"실행": "경고", "선": "경고"}))
    edit_last(root, "trend_10_50", lambda r: r.update(response={"execution_warning": " ", "warning_line": LINE}))
    html = guard_page(root, "trend_10_50")
    assert head_quotes(html) == [] and "적어 둔 대응</p>" not in html.split("<h2")[0]  # 고른 대응 글이 비면 머리 인용 없음
    stop(tmp_path, monkeypatch, "trend_10_50", "중지")  # 끈 전략
    assert head_quotes(guard_page(root, "trend_10_50")) == []


def test_guard_response_panel_puts_triggered_first(tmp_path):
    """아래 대응 패널은 둘 다 남기되 걸린 것 먼저, 걸리지 않은 것은 라벨 뒤 caption 「아직 안 걸림」."""
    root = write_facts(tmp_path)
    assert panel_order(guard_page(root, "trend_10_50")) == ["실행 경고", "경고선", "아직 안 걸림"]
    set_states(root, "trend_10_50", 실행="정상", 선="경고")
    html = guard_page(root, "trend_10_50")
    assert panel_order(html) == ["경고선", "실행 경고", "아직 안 걸림"]
    panel = html.split("대응(가드레일에 적어 둔 것)</h2>")[1]
    assert panel.index(LINE) < panel.index(EXE) and f'<p class="sub">실행 경고</p>{NOT_YET}<blockquote class="quote">{EXE}' in panel
    set_states(root, "trend_10_50", 실행="경고", 선="경고")
    assert panel_order(guard_page(root, "trend_10_50")) == ["실행 경고", "경고선"]  # 둘 다 걸리면 caption 없음
    assert panel_order(guard_page(root, "trend_ema_v7")) == ["실행 경고", "아직 안 걸림", "경고선", "아직 안 걸림"]  # 정상이면 머리 인용 없이 패널만
    edit_last(root, "trend_10_50", lambda r: r.update(response={"execution_warning": "", "warning_line": LINE}))
    assert panel_order(guard_page(root, "trend_10_50")) == ["경고선"]  # 빈 값은 만들지 않음
    edit_last(root, "trend_10_50", lambda r: r.update(response={"memo": "손으로 적은 메모"}))
    panel = guard_page(root, "trend_10_50").split("대응(가드레일에 적어 둔 것)</h2>")[1]
    assert '<p class="sub">memo</p><blockquote class="quote">손으로 적은 메모' in panel and NOT_YET not in panel  # 걸릴 수 없는 칸에는 caption 없음


def test_screen_title_is_display_outside_panels(tmp_path):
    """화면 제목 h1은 패널 밖 `.display`다(가드레일은 이름 + 칩, 홈은 「실전 전략 N개」 + 기준 날). 머리 패널에는 이유 · 인용 · 사실 줄이 든다."""
    root = write_facts(tmp_path)
    html = guard_page(root, "trend_10_50")
    assert re.search(rf'<main[^>]*>\n<div class="head"><h1 class="display">trend_10_50</h1>{re.escape(web.chip("경고"))}</div><section class="panel"><p class="why why-warn">', html)
    assert 'class="title"' not in html and html.count("<h1") == 1
    home = get_pages(root, ["/"])["/"]
    assert re.search(r'<main[^>]*>\n<div class="head"><h1 class="display">실전 전략 4개</h1><p class="sub">기준 2026-10-14</p></div><section class="panel">', home)
    rec = record("trend_ema_v7", 22)  # 머리 패널에 넣을 것이 하나도 없으면 패널을 만들지 않는다(가드레일을 읽었고 정상이며 사실 줄 재료가 없는 가상 view)
    view = {"name": "a<b", "recs": [rec], "g": None, "off": None, "why": None, "word": "정상", "reason": None, "after": False, "version": None}
    assert web._guard_head(view, "tok") == f'<div class="head"><h1 class="display">a&lt;b</h1>{web.chip("정상")}</div>'


def test_home_stacked_values_match_table(tmp_path):
    """쌓은 목록(1024 미만)과 표(이상)는 같은 값을 다른 모양으로 보인다: 이름 · 순서 · 실제 · 백테스트 · 차이 · 거래 · 기준 날 · caption."""
    root = write_facts(tmp_path)
    html = get_pages(root, ["/"])["/"]
    rows = live_rows(html)
    ul = re.search(r'<ul class="rows narrow cq" aria-label="실전 전략 목록">(.*?)</ul>', html, re.S)[1]
    items = re.findall(r'<li class="item">(.*?)</li>', ul, re.S)
    assert len(items) == len(rows) == 4 and '<div class="scroll wide cq" role="region" aria-label="실전 전략 표"' in html
    chips = {"breakout_v2": "확인 불가", "trend_10_50": "경고", "grid_v1": "판단 보류", "trend_ema_v7": "정상"}
    for item, (name, (cells, tr)) in zip(items, rows.items()):
        assert f">{name}</a>" in item and web.chip(chips[name]) in item
        vals = [re.sub(r'<div class="sub">(.*?)</div>', r"(\1)", v) for _, v in re.findall(r"<div><dt>(.*?)</dt><dd>(.*?)</dd></div>", item, re.S)]
        assert vals == cells[:3], name
        assert f"거래 {cells[3]}" in item and f"기준 날 {cells[4]}" in item, name
        assert re.search(r'<svg class="spark"[^>]*>', item)[0] in tr  # 같은 흐름 그림
    assert "<tr" not in ul


def test_guard_cause_list_matches_table(tmp_path):
    """원인 셋도 1024 미만에서는 쌓은 목록(원인 + 칩, 근거 한 문장). 기준 표의 쌓은 목록은 따로 시험한다."""
    root = write_facts(tmp_path)
    html = guard_page(root, "trend_10_50")
    assert '<div class="scroll wide" role="region" aria-label="원인 셋 표"' in html
    ul = re.search(r'<ul class="rows narrow" aria-label="원인 셋 목록">(.*?)</ul>', html, re.S)[1]
    items = re.findall(r'<li class="item">(.*?)</li>', ul, re.S)
    got = web.causes(read_days(root, "trend_10_50")[-1], "spot")
    assert len(items) == 3 == len(got)
    for item, (q, w, ev) in zip(items, got):
        assert f'<span class="item-name">{q}</span>{web.chip(w)}' in item and f'<div class="why">{web.text(ev)}</div>' in item and "<dl" not in item, q
    assert "<tr" not in ul and 'scope="row"' not in ul


def test_guard_bad_optional_fields(tmp_path):
    """검사가 없는 선택 칸이 어긋나도 그 칸만 생략하고 200. cost가 리스트 · set_on이 없거나 날이 아님은 생략, basis가 dict가 아니면 가드레일 읽기 거절."""
    root = write_facts(tmp_path, ("trend_10_50",))
    edit_guardrail(root, "trend_10_50", lambda g: g.update(cost=[], set_on="2026-10-01"))
    html = guard_page(root, "trend_10_50")
    assert "가드레일 v1(2026-10-01 설정)  기준 2026-10-14" in html and "보수적 비용" not in html
    edit_guardrail(root, "trend_10_50", lambda g: g.update(cost={"one_way_bp": 30, "note": "x"}))
    assert "30일 범위 -3.7%~+8.1%, 보수적 비용 편도 30bp 기준" in squash(guard_page(root, "trend_10_50"))
    for bad in ({"one_way_bp": "30"}, {"one_way_bp": True}, {}, "x", None):
        edit_guardrail(root, "trend_10_50", lambda g, bad=bad: g.update(cost=bad, set_on=None))
        html = guard_page(root, "trend_10_50")
        assert "보수적 비용" not in html and "가드레일 v1  기준 2026-10-14" in html, bad
    edit_guardrail(root, "trend_10_50", lambda g: g.update(set_on="2026-13-45", cost={}))
    assert "가드레일 v1  기준" in guard_page(root, "trend_10_50")
    edit_guardrail(root, "trend_10_50", lambda g: g.update(basis="x"))  # 읽기 검사가 거절: 화면 한 장의 확인 불가
    html = guard_page(root, "trend_10_50")
    assert "가드레일 파일을 읽지 못했어요(링크이거나 모양이 달라요)" in html and web.chip("확인 불가") in html and charts(html) == []


def test_guard_criteria_list_matches_table(tmp_path, monkeypatch):
    """가드레일 기준 표도 1024 미만에서는 쌓은 목록(기준 이름 + 정한 값 한 줄). 화면의 표 상자는 모두 `wide`라 좁은 폭에 가로 스크롤 상자가 없다."""
    root = write_facts(tmp_path)
    for name in NAMES:
        html = guard_page(root, name)
        assert re.findall(r'<div class="scroll[^"]*"', html) == ['<div class="scroll wide"'] * 2, name  # 원인 셋 + 기준
    html = guard_page(root, "trend_10_50")
    assert '<div class="scroll wide" role="region" aria-label="가드레일 기준 표"' in html
    table = html.split("가드레일 v1(실돈 넣기 전에 정한 기준)")[1]
    rows = [(squash(th), squash(td)) for th, td in re.findall(r'<th scope="row"[^>]*>(.*?)</th><td[^>]*>(.*?)</td>', table, re.S)]
    ul = re.search(r'<ul class="rows narrow" aria-label="가드레일 기준 목록">(.*?)</ul>', html, re.S)[1]
    items = [(squash(k), squash(v)) for k, v in re.findall(r'<li class="item"><div class="item-head"><span class="item-name">(.*?)</span></div><div>(.*?)</div></li>', ul, re.S)]
    assert [k for k, _ in rows] == ["정상 범위", "경고선", "판단 보류", "시도 수 보정"] and items == rows
    assert "<tr" not in ul and "<dl" not in ul
    only_off = home_cases(tmp_path / "cases", monkeypatch)["끈 전략만"]  # 홈의 표 둘은 `wide cq`(패널 폭 컨테이너 쌍)
    assert set(re.findall(r'<div class="scroll[^"]*"', get_pages(only_off, ["/"])["/"])) == {'<div class="scroll wide cq"'}


def test_guard_criteria_table(tmp_path):
    """가드레일 표: 정상 범위 · 경고선 · 판단 보류 · 시도 수 보정 네 줄(NOTE의 뜻을 해요체로)."""
    root = write_facts(tmp_path, ("trend_10_50",))
    g = read_guardrail(root, "trend_10_50")[1]
    html = guard_page(root, "trend_10_50")
    table = html.split("가드레일 v1(실돈 넣기 전에 정한 기준)")[1]
    rows = {squash(th): squash(td) for th, td in re.findall(r'<th scope="row"[^>]*>(.*?)</th><td[^>]*>(.*?)</td>', table, re.S)}
    nr, wl, jh = g["normal_range"], g["warning_line"], g["judgment_hold"]
    assert rows["정상 범위"] == f"{nr['days']}일 범위 {pc(nr['low'])}~{pc(nr['high'])}"
    assert rows["경고선"] == f"{pc(wl['line'])}, 민감도 {wl['sensitivity']}"
    assert rows["판단 보류"] == f"거래 {jh['min_trades']}건 전이거나 엣지가 없어도 범위 안일 확률이 {jh['hold_if_edge0_at_least']:.0%} 이상이면 판단 보류예요."
    assert rows["시도 수 보정"] == "시도 수 보정은 정상 범위에 넣지 않고 판단 보류 옆에 숫자로만 적어요. 판단 보류 계산에는 아직 쓰지 않아요."
    assert copy_violations(html) == []


def test_guard_axis_labels_align_with_svg(tmp_path):
    """축 라벨은 HTML이고 SVG 안 좌표와 맞는다: 「0%」 눈금의 세로 위치 = 0% 기준선 y, viewBox는 고정, 눈금은 여섯 개 안쪽."""
    root = write_facts(tmp_path)
    html = guard_page(root, "trend_10_50")
    figs = re.findall(r'<div class="fig"(?: id="e\d+")?><div class="yax"[^>]*>(.*?)</div>(<svg.*?</svg>)<div class="xax"[^>]*>(.*?)</div>'
                      r'(?:<p class="e-dots-off">[^<]*</p>)?(?:<ul class="legend".*?</ul>)?</div>', html, re.S)  # E는 id · 안내 줄 · 범례가 더 있다
    assert len(figs) == 2
    for ticks, svg, _xs in figs:
        assert 'viewBox="0 0 720 240"' in svg and "<text" not in svg
        found = re.findall(r'<span class="tick" style="top:([\d.]+)%">([^<]*)</span>', ticks)
        assert 2 <= len(found) <= 6
        zero_y = float(re.search(r'<line class="s-zero"[^>]* y1="([\d.]+)"', svg)[1])
        (top,) = [float(t) for t, label in found if label in ("0%", "0.0%", "0.00%")]
        assert abs(top - zero_y / 240 * 100) < 0.05
    assert squash(figs[0][2]) == "2026-09-23 2026-10-14" and squash(figs[1][2]) == "1번째 거래 37번째 거래"

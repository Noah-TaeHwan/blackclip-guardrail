"""blackclip dashboard 회고 화면: `/s/<이름>/retro`.

큰 숫자 · 「차이가 어디서 났나」 · 차트 B · 체결 대조 요약 · 끈 뒤 칸 · 내비를 실제 서버(`make_server`, 포트 0)에서 받은 HTML로 본다.
숫자는 터미널 `status` · `chart_b` 글과 같은지 견주고, 엔진이 저장하지 않은 것(지난주 · 놓친 주문 단건 손익 · 짝 표)은 화면에 없다.
"""
import http.client
import json
import re
import threading
from datetime import date, timedelta
from html import unescape
from pathlib import Path

import pytest
from conftest import MemStore
from facts import NAMES, START, write_facts
from test_dashboard_pages import (
    break_repo,
    edit_days,
    edit_guardrail,
    edit_last,
    get_pages,
    squash,
    status_of,
    two_period_repo,
)
from webrules import copy_violations

from blackclip import dashboard
from blackclip.connect import Connect
from blackclip import record as rec_mod
from blackclip.daily.days import read_days
from blackclip.notify import RETRO_HEAD
from blackclip.record import (
    after_event,
    after_values,
    lessons,
    read_record,
    write_record,
)
from blackclip.screen import chart, data, status, web


@pytest.fixture(autouse=True)
def _home(monkeypatch, tmp_path):
    monkeypatch.setenv("HOME", str(tmp_path / "home"))


def retro_page(root: Path, name: str) -> str:
    """회고 화면 하나(실제 서버, 200 단언)."""
    return get_pages(root, [f"/s/{name}/retro"])[f"/s/{name}/retro"]


def panels(html: str, h2: str) -> list[str]:
    """`<h2 class="h2">`가 h2로 시작하는 패널들의 안쪽 HTML(패널은 겹치지 않는다)."""
    return [m[1] for m in re.finditer(r'<section class="panel">(.*?)</section>', html, re.S) if f'<h2 class="h2">{h2}' in m[1]]


def panel(html: str, h2: str) -> str:
    """패널 하나(없으면 빈 글)."""
    got = panels(html, h2)
    assert len(got) <= 1, h2
    return got[0] if got else ""


def bigs(sec: str) -> dict[str, str]:
    """숫자 패널의 큰 숫자 셋: 라벨 → 보이는 값(「까지」 줄 포함)."""
    return {squash(k): squash(v) for k, v in re.findall(r'<div><dt class="sub">(.*?)</dt><dd class="display">(.*?)</dd></div>', sec, re.S)}


def parts(sec: str) -> dict[str, str]:
    """「차이가 어디서 났나」 줄들: 이름 → 값 글."""
    return {squash(k): squash(v) for k, v in re.findall(r'<span class="item-name">(.*?)</span><span>(.*?)</span>', sec, re.S)}


def numbers_panels(html: str) -> list[str]:
    """회고 화면의 숫자 패널들(제목이 「이번 구간」 · 「실전 시작부터」로 시작)."""
    return [m[1] for m in re.finditer(r'<section class="panel">(.*?)</section>', html, re.S)
            if re.search(r'<h2 class="h2">(?:이번 구간|실전 시작부터)\(', m[1])]


def term_status(root: Path, name: str | None) -> list[str]:
    """터미널 `blackclip status` 글 줄들."""
    return status.status_lines(root, name)[1]


def off(root: Path, name: str, state: str, day: str, reason: str | None = None, decision: str | None = None) -> None:
    """`record.write_record`로 전략을 끈다(날을 정할 수 있음, after.md도 함께 써짐)."""
    write_record(root, name, state, reason, decision, date.fromisoformat(day))


def test_retro_numbers_match_terminal(tmp_path):
    """한 구간 레포 = 칸 하나, 구간 둘 레포 = 칸 둘. 큰 숫자 = 터미널 status 숫자, 「차이가 어디서 났나」 = 계약 키 경로 값, B 범례 = `_b_labels` 숫자."""
    one = write_facts(tmp_path / "one", ("trend_10_50",))
    html = retro_page(one, "trend_10_50")
    secs = numbers_panels(html)
    assert len(secs) == 1 and f"실전 시작부터({START.isoformat()}부터)" in secs[0]
    row = next(x for x in term_status(one, None) if x.startswith("trend_10_50"))
    m = re.search(r"실제 (\S+) · 실제−BT\(비용 0\) (\S+) · 거래 (\S+)건", row)
    big = bigs(secs[0])
    assert list(big) == ["같은 기간 백테스트", "실제", "차이(비용 0)"]
    assert big["실제"] == m[1] and big["차이(비용 0)"] == m[2]
    last = read_days(one, "trend_10_50")[-1]["nums"]["all"]
    p = parts(panels(html, "차이가 어디서 났나")[0])
    assert p["체결가 차이"] == web.pp(last["slip"]) and p["수수료"] == web.pp(last["fee"]) and p["나머지"] == web.pp(last["rest"])
    assert "펀딩비" not in p
    assert "원인 후보: 백테스트에만 있는 거래 1건" in squash(panels(html, "차이가 어디서 났나")[0])
    recs = read_days(one, "trend_10_50")
    lab = "\n".join(chart.chart_b("trend_10_50", recs, False, 200))
    legend = squash(re.search(r'<ul class="legend".*?</ul>', panel(html, "차이가 쌓인 모습"), re.S)[0])
    assert re.search(r"체결가 차이 · 수수료 (\S+)\(", lab)[1] in legend and re.search(r"나머지 (\S+)\(추정", lab)[1] in legend
    assert re.search(r"합계 (\S+)", lab)[1] in legend

    two = two_period_repo(tmp_path / "two")
    html2 = retro_page(two, "trend_10_50")
    secs2 = numbers_panels(html2)
    assert len(secs2) == 2 and "이번 구간(" in secs2[0] and "실전 시작부터(" in secs2[1]
    line = next(x for x in term_status(two, "trend_10_50") if x.startswith("실전 시작부터("))
    t = re.search(r"실제 (\S+) · 같은 기간 BT (\S+) · 실제 − BT\(비용 0\) (\S+)", line)
    whole, cur = bigs(secs2[1]), bigs(secs2[0])
    assert (whole["실제"], whole["같은 기간 백테스트"], whole["차이(비용 0)"]) == t.groups()
    n = read_days(two, "trend_10_50")[-1]["nums"]
    assert (cur["실제"], cur["같은 기간 백테스트"], cur["차이(비용 0)"]) == (web.pct(n["actual"]), web.pct(n["bt"]), web.pp(n["gap"]))
    ps = [parts(x) for x in panels(html2, "차이가 어디서 났나")]
    assert len(ps) == 2
    assert ps[0]["체결가 차이"] == web.pp(n["slip"]) and ps[1]["체결가 차이"] == web.pp(n["all"]["slip"]) and ps[0]["나머지"] != ps[1]["나머지"]
    sec_cur, sec_whole = panels(html2, "차이가 어디서 났나")
    assert "bp" in squash(sec_cur) and "bp" not in squash(sec_whole) and "bp" not in squash(secs2[1])  # bp 줄은 이번 구간 칸에만


def test_retro_does_not_invent_missing_sources(tmp_path):
    """엔진이 저장하지 않은 것은 화면에 없다: 지난주 칸 · 놓친 주문 단건 손익 · 시점 차 낱말, 실전 시작부터 칸에 bp."""
    for root in (write_facts(tmp_path / "a", ("trend_10_50",)), two_period_repo(tmp_path / "b")):
        html = retro_page(root, "trend_10_50")
        text = squash(html)
        for word in ("지난주", "시점 차", "시점차", "단건", "건별 손익", "주문 하나"):
            assert word not in text, word
        assert all("bp" not in squash(s) for s in numbers_panels(html))


def test_retro_fills_panel(tmp_path):
    """체결 대조: 매일 대조 요약 줄 값 = nums(터미널 status 글의 bp · 건수), 첫 회고는 retro.md 일반 파일일 때만 경로 줄, retro.md는 파싱하지 않는다."""
    root = write_facts(tmp_path, ("trend_10_50",))
    folder = root / "strategies" / "trend_10_50"
    fills = panel(retro_page(root, "trend_10_50"), "체결 대조")
    text = squash(fills)
    term = "\n".join(term_status(root, "trend_10_50"))
    bp = re.search(r"중앙값 (\S+bp) · 최대 (\S+bp)", term)
    assert f"체결가 차이 중앙값 {bp[1]}, 최대 {bp[2]}" in text
    assert "백테스트에만 있는 거래 1건" in term and len(re.findall(r'<tr class="row">', fills)) == 1
    assert "2026-10-13 15:58 UTC" in text and "BTCUSDT" in text and "매도" in text and "백테스트에만 있는 거래(놓친 주문으로 추정)" in text
    assert text.count("매일 대조는 건별 체결 짝(가격과 수량)을 저장하지 않아요.") == 1
    assert "건별 짝 표가 필요하면 AI에게 첫 회고를 부탁해 주세요." in text and "retro.md" not in text
    (folder / "retro.md").write_text(f"{RETRO_HEAD}2026-09-23\n▣ 경고 <script>alert(1)</script>\n| 짝 | 표 |\n", encoding="utf-8")
    html = retro_page(root, "trend_10_50")
    text = squash(panel(html, "체결 대조"))
    assert "첫 회고 표(백테스트 거래와 실제 체결의 짝)는 .blackclip/strategies/trend_10_50/retro.md에 있어요." in text
    assert "건별 짝 표가 필요하면" not in text and "<script>" not in html and "▣" not in html and "짝 | 표" not in html  # 파일을 파싱하지 않음
    (folder / "retro.md").unlink()
    (folder / "elsewhere.md").write_text(f"{RETRO_HEAD}x\n", encoding="utf-8")
    (folder / "retro.md").symlink_to(folder / "elsewhere.md")  # 링크는 일반 파일이 아님
    text = squash(panel(retro_page(root, "trend_10_50"), "체결 대조"))
    assert "건별 짝 표가 필요하면 AI에게 첫 회고를 부탁해 주세요." in text and "retro.md에 있어요" not in text
    (folder / "retro.md").unlink()
    (folder / "daily" / "2026-10-14.md").write_text("# 일별\n", encoding="utf-8")  # daily가 만든 md는 첫 회고 표가 아니다
    assert "retro.md에 있어요" not in squash(panel(retro_page(root, "trend_10_50"), "체결 대조"))


def test_retro_first_retro_only_strategy(tmp_path):
    """가드레일 판 · 매일 대조 기록 없이 link.py와 retro.md만 있는 전략(첫 회고만): 200, 경로 줄 · 기록이 아직 없어요 줄, 내비에 가드레일 없음."""
    root = write_facts(tmp_path, ("trend_10_50",))
    d = root / "strategies" / "only_retro"
    d.mkdir()
    (d / "link.py").write_text("print('x')\n", encoding="utf-8")
    (d / "retro.md").write_text(f"{RETRO_HEAD}2026-09-23\n", encoding="utf-8")
    html = retro_page(root, "only_retro")
    text = squash(html)
    assert "매일 대조 기록이 아직 없어요. 첫 대조가 끝나면 이 화면에 그려져요." in text
    assert "첫 회고 표(백테스트 거래와 실제 체결의 짝)는 .blackclip/strategies/only_retro/retro.md에 있어요." in text
    nav = re.search(r'<nav.*?</nav>', html, re.S)[0]
    assert "가드레일" not in nav and "회고" in nav and 'aria-current="page"' in nav and "<svg" not in html.split("체결 대조")[0].split("<main")[1]
    assert status_of(root, ["/s/only_retro"]) == {"/s/only_retro": 404}  # 가드레일 화면은 없는 전략
    assert copy_violations(html) == []


def test_retro_missed_rows_and_cap(tmp_path):
    """놓친 주문 후보: 행 수 = missed, 기록 상한에 걸리면(missed_n이 더 큼) 안내 줄, missed가 비어도 missed_n이 있으면 안내 줄."""
    root = write_facts(tmp_path, ("trend_10_50",))
    three = [{"t": f"2026-10-1{i}T0{i}:30:00+00:00", "symbol": "BTCUSDT", "side": "buy" if i % 2 else "sell"} for i in (3, 1, 2)]
    edit_days(root, "trend_10_50", lambda r: r["nums"].update(missed=[], missed_n=0))
    edit_last(root, "trend_10_50", lambda r: r["nums"].update(missed=three, missed_n=3))
    fills = panel(retro_page(root, "trend_10_50"), "체결 대조")
    rows = re.findall(r'<tr class="row">(.*?)</tr>', fills, re.S)
    assert len(rows) == 3 and "가운데" not in squash(fills)
    assert [squash(r)[:16] for r in rows] == ["2026-10-11 01:30", "2026-10-12 02:30", "2026-10-13 03:30"]  # 시각 오름차순
    assert "매수" in squash(rows[0]) and "매도" in squash(rows[1])
    edit_last(root, "trend_10_50", lambda r: r["nums"].update(missed=three, missed_n=60))
    text = squash(panel(retro_page(root, "trend_10_50"), "체결 대조"))
    assert "놓친 주문 후보 60건 가운데 3건만 기록돼 있어요." in text
    edit_last(root, "trend_10_50", lambda r: r["nums"].update(missed=[], missed_n=7))
    fills = panel(retro_page(root, "trend_10_50"), "체결 대조")
    assert "놓친 주문 후보 7건 가운데 0건만 기록돼 있어요." in squash(fills) and "<tr" not in fills
    edit_last(root, "trend_10_50", lambda r: r["nums"].update(missed=[], missed_n=0, extra=1234))
    assert "실제에만 있는 체결 1,234건(원인 모름)" in squash(panel(retro_page(root, "trend_10_50"), "체결 대조"))


def test_retro_futures_funding_row(tmp_path):
    """펀딩비 줄은 선물일 때만(값이 없으면 모름), 현물에는 없다. B 범례의 펀딩비도 같다."""
    root = write_facts(tmp_path, ("trend_10_50",))
    html = retro_page(root, "trend_10_50")
    assert "펀딩비" not in squash(html)
    edit_guardrail(root, "trend_10_50", lambda g: g["target"].update(market="futures"))
    p = parts(panels(retro_page(root, "trend_10_50"), "차이가 어디서 났나")[0])
    assert list(p) == ["체결가 차이", "수수료", "펀딩비", "나머지"] and p["펀딩비"] == "모름"
    edit_last(root, "trend_10_50", lambda r: r["nums"].update(funding=-0.0012))
    edit_last(root, "trend_10_50", lambda r: r["nums"]["all"].update(funding=-0.0012))
    html = retro_page(root, "trend_10_50")
    assert parts(panels(html, "차이가 어디서 났나")[0])["펀딩비"] == "-0.1%p"
    assert "잰 것(체결가 차이, 수수료, 펀딩비)" in squash(panel(html, "차이가 쌓인 모습"))


def b_recs(change=None, n=6) -> list[dict]:
    """B 시험용 기록 n개(trend_10_50 22일째 줄 복사, 날마다 fee -0.1% · slip -0.2% · rest +0.1%)."""
    from facts import record
    out = []
    for k in range(1, n + 1):
        r = json.loads(json.dumps(record("trend_10_50", 22)))
        for key in ("trade_rets", "trade_mean", "trade_n", "trade_skip"):
            r["nums"].pop(key, None)
        r["day"], r["live_day"] = (START + timedelta(days=k - 1)).isoformat(), k
        r["nums"].update(fee=-0.001, slip=-0.002, rest=0.001, gap=-0.002, funding=None, missed=[], missed_n=0)
        r["nums"]["all"].update(missed=[], missed_n=0)
        out.append(r)
    if change:
        change(out)
    return out


def test_b_svg_shape():
    """차트 B: 막대 두 겹 · 무늬 하나 · 합계 선 · 0%p 선 · 구간 경계, 그릴 면적이 없는 넷은 SVG 0 + 해요체 한 줄, 접근 이름은 해요체."""
    def cut(rs):
        for r in rs[3:]:
            r["nums"]["period_start"] = (START + timedelta(days=3)).isoformat()
    html = web.b_svg(data.b_series(b_recs(cut)))
    assert html.count("<svg") >= 1 and html.count("<pattern") == 1 and html.count("</pattern>") == 1
    svg = re.search(r'<svg class="chart".*?</svg>', html, re.S)[0]
    assert 'role="img"' in svg and re.search(r'aria-label="[^"]*차트예요\."', svg) and "<text" not in svg
    assert svg.count('class="s-slow"') == 6 and svg.count('class="s-fast"') == 6  # 날마다 아래로 slow(-0.3%p), 위로 fast(+0.1%p)
    assert svg.count('class="s-run"') == 2 and svg.count('class="s-zero"') == 1 and svg.count('class="s-start"') == 1  # 합계 선은 구간마다 끊김
    legend = squash(re.search(r'<ul class="legend".*?</ul>', html, re.S)[0])
    assert "잰 것(체결가 차이, 수수료) -0.3%p" in legend and "나머지(추정) +0.1%p" in legend and "합계 -0.2%p" in legend
    assert "·" not in legend and "처음 후보" not in legend
    miss = {"t": "2026-09-25T04:05:00+00:00", "symbol": "ETHUSDT", "side": "SELL"}
    html = web.b_svg(data.b_series(b_recs(lambda rs: rs[-1]["nums"].update(missed=[miss], missed_n=1))))
    assert "처음 후보 2026-09-25 04:05 UTC 매도" in squash(html)
    html = web.b_svg(data.b_series(b_recs(lambda rs: rs[-1]["nums"].update(gap=None))))
    assert "합계(2026-09-27까지) -0.2%p" in squash(html)
    html = web.b_svg(data.b_series(b_recs(lambda rs: rs[-1]["nums"].update(rest=None))))
    assert "나머지(추정) 모름" in squash(html)

    def zero(rs, **late):
        for r in rs:
            r["nums"].update(fee=0.0, slip=0.0, rest=0.0, gap=0.0)
        rs[-1]["nums"].update(**late)
    cases = {
        "none": (lambda rs: [r["nums"].update(fee=None, slip=None, rest=None) for r in rs], "차이를 낸 날이 없어요(실행 칸을 확인하지 못했어요)."),
        "zero": (zero, "차이가 0.0%p라 그릴 면적이 없어요."),
        "zero_upto": (lambda rs: zero(rs, slip=None), "2026-09-27까지 차이가 0.0%p라 그릴 면적이 없어요."),
        "unknown": (lambda rs: [r["nums"].update(fee=0.0, slip=None, rest=0.0, gap=None) for r in rs], "두 기여를 모두 확인한 날이 없어 차이를 몰라요."),
    }
    for kind, (change, line) in cases.items():
        series = data.b_series(b_recs(change))
        assert series["empty"]["kind"] == kind
        out = web.b_svg(series)
        assert "<svg" not in out and line in squash(out), kind
        assert "니다" not in out and "·" not in out
    assert "합계" in squash(web.b_svg(data.b_series(b_recs(cases["zero_upto"][0]))))  # 범례가 붙음


def test_retro_bars_have_no_caution(tmp_path):
    """회고 막대(.meter-fill)와 「차이가 어디서 났나」 줄에는 caution이 없다. caution은 차트 B의 나머지(추정) 면에만 쓴다."""
    css = web.css()
    assert "caution" not in re.search(r"\.meter-fill\{([^}]*)\}", css)[1] and "caution" not in re.search(r"\.meter\{([^}]*)\}", css)[1]
    root = two_period_repo(tmp_path)
    html = retro_page(root, "trend_10_50")
    for sec in panels(html, "차이가 어디서 났나"):
        assert "caution" not in sec and "s-fast" not in sec and "meter-fill" in sec
    assert 'class="s-fast"' in panel(html, "차이가 쌓인 모습")
    fast = re.search(r"\.s-fast\{([^}]*)\}", css)[1]
    assert "var(--studio-color-caution)" in fast and re.search(r"[\d.]+", fast.split("opacity:")[1])[0] == ".55"


def after_html(root: Path, name: str) -> str:
    """회고 화면의 끈 뒤 칸 HTML(없으면 빈 글)."""
    return panel(retro_page(root, name), "끈 뒤 회고")


def test_after_panel_for_stopped_and_retired(tmp_path):
    """중지 · 은퇴 · 중지 → 은퇴(날 · 이유 다름: 기준 = 중지 + 은퇴 한 줄): 상태 · 날 · 이유 · 결정 · 타임라인 줄 수 = after_values, ④ 첫 줄 = 저장 lesson."""
    root = write_facts(tmp_path)
    off(root, "grid_v1", "중지", "2026-10-16", "전략이 안 맞아요", "다음 달에 다시 본다")
    off(root, "trend_ema_v7", "은퇴", "2026-10-17", "접어요", "그만 둔다")
    off(root, "breakout_v2", "중지", "2026-10-16", "체결을 못 받아요", "키부터 다시 연결")
    off(root, "breakout_v2", "은퇴", "2026-10-18", "고치지 않기로 함", "접는다")
    for name, state, day, reason, decision in (("grid_v1", "중지", "2026-10-16", "전략이 안 맞아요", "다음 달에 다시 본다"),
                                               ("trend_ema_v7", "은퇴", "2026-10-17", "접어요", "그만 둔다"),
                                               ("breakout_v2", "중지", "2026-10-16", "체결을 못 받아요", "키부터 다시 연결")):
        html = after_html(root, name)
        text = squash(html)
        doc = read_record(root, name)[0]
        ev, retire = after_event(doc)
        assert (ev["state"], ev["day"]) == (state, day)
        assert f"{state}한 전략이에요({day}부터). 매일 대조는 멈춰 있어요." in text
        assert reason in squash(re.search(r'<blockquote class="quote">.*?</blockquote>', html, re.S)[0])
        base_doc = doc if retire is None else {**doc, "events": doc["events"][:-1]}
        v = after_values(root, name, base_doc, date.fromisoformat(day))
        assert html.count('<li class="tl">') == len(v["two"]["timeline"]) > 0
        assert decision in text and v["top"]["start"] in text
        assert ("은퇴 2026-10-18, 이유 고치지 않기로 함" in text) == (name == "breakout_v2") and "접는다" not in text
        saved = re.sub(r"\s·\s", ", ", doc["lesson"])
        first = squash(re.search(r'<p class="lesson">(.*?)</p>', html, re.S)[1])
        assert first == re.sub(r"(?<![\d.,:])(\d{4,})(?=건|개|회|번|일)", lambda m: f"{int(m[1]):,}", saved)
        check_line = next(x for x in lessons(root, "zz_other") if x.startswith(f"- {name}("))
        assert check_line.split("): ", 1)[1].replace(" · ", ", ") == saved  # check 출력 끝 줄과 같은 글
        assert copy_violations(retro_page(root, name)) == [] and re.search(r"\d{1,2}/\d{1,2}", re.sub(r"\d{4}-\d{2}-\d{2}", "", text)) is None
    live = retro_page(root, "trend_10_50")
    assert "끈 뒤 회고" not in live  # 실전 전략은 칸이 없다


def test_after_panel_unread_and_no_days(tmp_path, monkeypatch):
    """기록 손상 = 손상 문장, days.jsonl 없는 레포 = 「이 컴퓨터에 없어요」 줄, 읽지 못한 일별 기록 = 숫자 자리 사유(끈 뒤 칸은 그대로)."""
    root = write_facts(tmp_path)
    off(root, "grid_v1", "중지", "2026-10-16", "x", "y")
    off(root, "trend_ema_v7", "중지", "2026-10-16", "x", "y")
    off(root, "breakout_v2", "중지", "2026-10-16", "x", "y")
    (root / "strategies" / "grid_v1" / "daily" / "days.jsonl").unlink()
    assert "근거가 된 매일 대조 기록이 이 컴퓨터에 없어요." in squash(after_html(root, "grid_v1"))
    assert "근거가 된 매일 대조 기록이 이 컴퓨터에 없어요." not in squash(after_html(root, "trend_ema_v7"))
    (root / "strategies" / "trend_ema_v7" / "daily" / "days.jsonl").write_text("{broken\n", encoding="utf-8")
    html = retro_page(root, "trend_ema_v7")  # 숫자 자리는 사유 한 줄이고 끈 뒤 칸은 그대로 그려진다
    assert "중지한 전략이에요(2026-10-16부터)" in squash(panel(html, "끈 뒤 회고")) and "매일 대조 기록 파일을 읽지 못했어요" in squash(html)
    assert copy_violations(html) == []
    (root / "strategies" / "breakout_v2" / "record.md").write_text("<<<<<<< HEAD\n", encoding="utf-8")
    html = retro_page(root, "breakout_v2")
    assert web.BROKEN_FULL in squash(panel(html, "끈 뒤 회고")) and copy_violations(html) == []


def test_after_panel_same_before_and_after_file_written(tmp_path):
    """중지 → 은퇴인데 after.md가 없는 레포. 화면의 끈 뒤 칸은 사건 목록만으로 정해져, `_write_after`로 after.md가 생긴 뒤에도 같다
    (「after.md에도 있어요」 줄만 더해진다)."""
    root = write_facts(tmp_path)
    off(root, "grid_v1", "중지", "2026-10-16", "중지 이유", "중지 결정")
    off(root, "grid_v1", "은퇴", "2026-10-18", "은퇴 이유", "은퇴 결정")
    after = root / "strategies" / "grid_v1" / "after.md"
    after.unlink()
    before = after_html(root, "grid_v1")
    assert "grid_v1/after.md에도 있어요" not in before and "은퇴 2026-10-18, 이유 은퇴 이유" in squash(before)
    doc = read_record(root, "grid_v1")[0]
    rec_mod._write_after(root, "grid_v1", doc, "은퇴", date(2026, 10, 18))  # 엔진이 은퇴 기준 회고를 새로 씀
    assert after.is_file()
    later = after_html(root, "grid_v1")
    note = '끈 뒤 회고 한 장은 .blackclip/strategies/grid_v1/after.md에도 있어요.'
    assert note in squash(later)
    assert re.sub(r'<p class="sub">끈 뒤 회고 한 장은 [^<]*</p>', "", later) == before


def test_after_text_is_escaped(tmp_path):
    """이유 · 결정 · lesson에 `<script>` · 그림 글자(▣ 경고 흉내)를 넣어도 escape되고, 칩 수는 그대로다."""
    root = write_facts(tmp_path)
    off(root, "grid_v1", "중지", "2026-10-16", "이유", "결정")
    calm = after_html(root, "grid_v1")
    off(root, "trend_ema_v7", "중지", "2026-10-16", "<script>alert(1)</script> ▣ 경고", "<b>결정</b> ▣ 경고")
    p = root / "strategies" / "trend_ema_v7" / "record.md"
    p.write_text(p.read_text(encoding="utf-8").replace('"lesson": "', '"lesson": "<script>alert(2)</script> ▣ 경고 ', 1), encoding="utf-8")
    html = retro_page(root, "trend_ema_v7")
    evil = panel(html, "끈 뒤 회고")
    assert "<script" not in html and "▣" not in html and "<b>" not in evil
    assert "&lt;script&gt;alert(1)&lt;/script&gt;" in evil and "\\u25a3" in evil and "&lt;script&gt;alert(2)" in evil
    assert evil.count('class="chip') == calm.count('class="chip') and copy_violations(html) == []


def test_retro_unknown_and_bad_name_404(tmp_path):
    """없는 이름 · 모양이 다른 이름 · 근거 없는 폴더 · 파일은 404, 토큰이 없으면 403. 가드레일 · record.md · 첫 회고 가운데 하나가 있으면 200."""
    root = write_facts(tmp_path, ("trend_10_50",))
    s = root / "strategies"
    (s / "empty_dir").mkdir()
    (s / "a_file").write_text("x", encoding="utf-8")
    (s / "with_record").mkdir()
    off(root, "trend_10_50", "중지", "2026-10-16")
    (s / "with_record" / "record.md").write_text("<<<<<<< HEAD\n", encoding="utf-8")  # 읽을 수 없는 record.md도 파일이 있으면 화면(손상 문장)
    (s / "linked").symlink_to(s / "trend_10_50")  # 링크 폴더는 전략이 아니다
    paths = ["/s/trend_10_50/retro", "/s/with_record/retro", "/s/nope/retro", "/s/empty_dir/retro", "/s/a_file/retro", "/s/linked/retro", "/s//retro", "/s/retro",
             "/s/../retro", "/s/%2e%2e/retro", "/s/" + "a" * 65 + "/retro", "/s/trend%5F10_50/retro", "/s/trend_10_50/retro/x", "/s/trend_10_50/retrox", "/s/trend_10_50/"]
    got = status_of(root, paths)
    assert [p for p, c in got.items() if c == 200] == ["/s/trend_10_50/retro", "/s/with_record/retro"], got
    assert all(c == 404 for p, c in got.items() if c != 200)
    assert status_of(root, paths[:3], token=False) == dict.fromkeys(paths[:3], 403)
    linked_root = tmp_path / "other" / ".blackclip"  # strategies/가 링크이면 어떤 이름도 404
    linked_root.mkdir(parents=True)
    (linked_root / "strategies").symlink_to(s)
    assert status_of(linked_root, ["/s/trend_10_50/retro"]) == {"/s/trend_10_50/retro": 404}


def test_retro_unreadable_days_is_200_with_reason(tmp_path):
    """읽지 못한 매일 대조 기록은 숫자 · B 자리에 `web.reason` 한 줄(200). 놓친 주문 시각을 UTC로 못 바꾸면 B · 체결 대조 · 끈 뒤 칸이 각자 그 칸에만 사유."""
    root = write_facts(tmp_path)
    (root / "strategies" / "grid_v1" / "daily" / "days.jsonl").write_text("{broken\n", encoding="utf-8")
    html = retro_page(root, "grid_v1")
    assert "매일 대조 기록 파일을 읽지 못했어요(링크이거나 모양이 달라요)" in squash(html) and not numbers_panels(html) and "<svg" not in html.split("체결 대조")[0]
    assert copy_violations(html) == []
    bad = {"t": "0001-01-01T00:00:00+01:00", "symbol": "BTCUSDT", "side": "sell"}
    off(root, "trend_10_50", "중지", "2026-10-16", "이유", "결정")  # 끄는 명령은 이 값이 있으면 실패하므로 끈 뒤에 손으로 고친 기록을 흉내 낸다
    edit_last(root, "trend_10_50", lambda r: (r["nums"].update(missed=[bad], missed_n=1), r["nums"]["all"].update(missed=[bad], missed_n=1)))
    html = retro_page(root, "trend_10_50")
    text = squash(html)
    assert len(numbers_panels(html)) == 1 and parts(panels(html, "차이가 어디서 났나")[0])["수수료"] != "모름"  # 숫자 칸은 그대로
    assert "<svg" not in panel(html, "차이가 쌓인 모습") and "매일 대조 기록 파일을 읽지 못했어요" in squash(panel(html, "차이가 쌓인 모습"))
    assert "매일 대조 기록 파일을 읽지 못했어요" in squash(panel(html, "체결 대조"))
    assert "끈 뒤 회고 파일을 읽지 못했어요(OverflowError)" in squash(panel(html, "끈 뒤 회고")) and "중지한 전략이에요(2026-10-16부터)" in text
    assert copy_violations(html) == []


def nav_links(html: str) -> list[tuple[str, str]]:
    """내비의 링크들: (보이는 글, href)."""
    nav = re.search(r"<nav.*?</nav>", html, re.S)[0]
    return [(squash(t), unescape(h)) for h, t in re.findall(r'<a [^>]*href="([^"]+)"[^>]*>(.*?)</a>', nav, re.S)]


def test_nav_has_four_and_no_dead_links(tmp_path, monkeypatch):
    """모든 화면 내비 링크를 실제 서버로 따라가 200이다. 내비 넷(홈 · 가드레일 · 회고 · 연결), 가드레일 판 없는 전략은 가드레일 칸이 없고, 연결은 언제나 끝 칸."""
    root = write_facts(tmp_path)
    off(root, "grid_v1", "중지", "2026-10-16")
    off(root, "trend_ema_v7", "은퇴", "2026-10-16")
    break_repo(root)
    d = root / "strategies" / "only_retro"
    d.mkdir()
    (d / "retro.md").write_text(f"{RETRO_HEAD}x\n", encoding="utf-8")
    (root / "strategies" / "no_guard_stopped").mkdir()
    (root / "strategies" / "no_guard_stopped" / "record.md").write_text("x", encoding="utf-8")
    connect = Connect(store=MemStore(), label="main", testnet=False, title="연결", check_key=lambda c: None, check_telegram=lambda c: None)
    server = dashboard.make_server(root, 0, connect=connect)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    seen_pages = 0
    try:
        def get(path):
            c = http.client.HTTPConnection("127.0.0.1", server.server_address[1], timeout=5)
            c.request("GET", path)
            r = c.getresponse()
            body = r.read().decode()
            c.close()
            return r.status, body
        tok = f"?t={server.token}"
        routes = ["/"] + [f"/s/{n}" for n in (*NAMES,)] + [f"/s/{n}/retro" for n in (*NAMES, "only_retro", "no_guard_stopped")]
        for route in routes:
            code, html = get(route + tok)
            if code != 200:  # 가드레일 화면이 없는 이름(only_retro 등)은 라우트 목록에서 제외
                continue
            seen_pages += 1
            for text, link in nav_links(html):
                assert get(link)[0] == 200, (route, text, link)
            assert nav_links(html)[-1][0] == "연결", route
        assert seen_pages >= 9
        _, html = get(f"/s/only_retro/retro{tok}")
        assert [t for t, _ in nav_links(html)] == ["홈", "회고", "연결"]
        _, html = get(f"/s/trend_10_50/retro{tok}")
        assert [t for t, _ in nav_links(html)] == ["홈", "가드레일", "회고", "연결"] and 'aria-current="page">회고<' in html
        _, html = get(f"/s/trend_10_50{tok}")
        assert [t for t, _ in nav_links(html)] == ["홈", "가드레일", "회고", "연결"] and 'aria-current="page">가드레일<' in html
        _, html = get(f"/{tok}")
        assert [t for t, _ in nav_links(html)] == ["홈", "연결"] and 'aria-current="page">홈<' in html
        _, html = get(f"/connect{tok}")
        assert [t for t, _ in nav_links(html)] == ["홈", "연결"] and 'aria-current="page">연결<' in html
    finally:
        server.shutdown()
        server.server_close()


def test_direction_particle():
    """타임라인 문장의 방향 조사: 받침 없음 · ㄹ 받침은 「로」, 그 밖의 받침은 「으로」."""
    assert [web._ro(w) for w in ("정상", "경고", "판단 보류", "확인 불가", "해당 없음", "점검 불가")] == [
        "정상으로", "경고로", "판단 보류로", "확인 불가로", "해당 없음으로", "점검 불가로"]
    assert web._ro("칼") == "칼로"


def one_items(html: str) -> list[str]:
    """끈 뒤 칸 ① 실전 기간 전체 목록의 줄들(보이는 글)."""
    sec = re.search(r'<ul class="list" aria-label="실전 기간 전체">(.*?)</ul>', html, re.S)
    return [squash(x) for x in re.findall(r'<li class="one">(.*?)</li>', sec[1], re.S)] if sec else []


def test_after_panel_one_lines_match_values(tmp_path):
    """실전 기간 전체 줄들이 `after_values.one` · `top.c0` 값과 줄 수 · 값이 같고, 문구 규칙 위반은 0이다.
    기록이 없거나 못 읽는 전략도 끈 뒤 칸이 혼자 서서 사유 줄을 보인다."""
    root = write_facts(tmp_path)
    for n in ("trend_10_50", "grid_v1", "trend_ema_v7", "breakout_v2"):
        off(root, n, "중지", "2026-10-16", "이유", "결정")
    for n in ("trend_10_50", "breakout_v2"):
        v = after_values(root, n, read_record(root, n)[0], date(2026, 10, 16))
        one = v["one"]
        html = retro_page(root, n)
        items = one_items(panel(html, "끈 뒤 회고"))
        assert copy_violations(html) == []
        expect = 1 + len(one["segments"]) + 3 + (1 if v["top"]["c0"] else 0)  # 숫자 · 구간 · 전략 · 실행 · 봇 · 재현성
        assert len(items) == expect, (n, items)
        assert items[0] == f"실제 {web.pct(one['actual'])}, 같은 기간 백테스트 {web.pct(one['bt'])}, 차이 {web.pp(one['gap'])}"
        assert items[-1] == rec_mod.C0_WORDS[v["top"]["c0"]]
        strategy, execute, bot = items[1:4]
        assert strategy.startswith(f"전략 {web.text(one['strategy']['state'].partition('(')[0])}") and "BT" not in strategy
        assert execute.startswith("체결에서 잰 값") or execute.startswith("실행")
        assert bot.startswith("봇") and ("체결 내역을 못 받은 기간" in bot) == bool(one["bot"]["lost"])
    # 문자 그대로 값(fixture): trend_10_50
    html = retro_page(root, "trend_10_50")
    items = one_items(panel(html, "끈 뒤 회고"))
    assert items[0] == "실제 -4.6%, 같은 기간 백테스트 -1.9%, 차이 -2.7%p"
    assert items[2] == ("체결에서 잰 값: 체결가 차이 -0.4%p(중앙값 -5.3bp), 수수료 -0.2%p. 나머지 -2.1%p(추정, 원인 후보: 백테스트에만 있는 거래 1건, "
                        "처음 2026-10-13 15:58 UTC BTCUSDT 매도)")
    assert items[1] == "전략 판단 보류"  # 옛 단언은 `or`의 뒤쪽이 늘 참이라 값을 못 잡았다
    assert items[3] == "봇 정상 체결 내역을 못 받은 날은 없어요." and items[-1] == "재현성 확인 전" and len(items) == 5
    assert one_items(panel(retro_page(root, "breakout_v2"), "끈 뒤 회고")) == [
        "실제 모름, 같은 기간 백테스트 +0.1%, 차이 모름", "전략 판단 보류", "실행 확인 불가",
        "봇 확인 불가 체결 내역을 못 받은 기간 2026-10-12부터 2026-10-14까지(3일)", "재현성 확인 전"]
    # 선물이면 펀딩비 모름, 현물이면 줄에 없음
    edit_guardrail(root, "trend_10_50", lambda g: g["target"].update(market="futures"))
    off_fut = one_items(panel(retro_page(root, "trend_10_50"), "끈 뒤 회고"))[2]
    assert "펀딩비 모름" not in " ".join(items) and "수수료 -0.2%p, 펀딩비 모름" in off_fut
    # 기록이 없는 전략 · 읽지 못하는 전략: ① 사유 한 줄이 혼자 선다
    (root / "strategies" / "grid_v1" / "daily" / "days.jsonl").unlink()
    (root / "strategies" / "trend_ema_v7" / "daily" / "days.jsonl").write_text("{broken\n", encoding="utf-8")
    got = one_items(panel(retro_page(root, "grid_v1"), "끈 뒤 회고"))
    assert got[0] == "매일 대조 기록이 없어 숫자를 쓰지 않았어요." and len(got) == 2 and got[1].startswith("재현성")
    html = retro_page(root, "trend_ema_v7")
    assert one_items(panel(html, "끈 뒤 회고"))[0] == "매일 대조 기록을 읽지 못해 숫자를 쓰지 않았어요." and copy_violations(html) == []


def test_retro_whole_column_shows_extra_and_fills_label(tmp_path):
    """`nums.all.extra`는 실전 시작부터 칸에 보이고(이번 구간 칸에는 없음), 칸이 둘이면 체결 대조 요약이 이번 구간 값이라고 적힌다."""
    root = two_period_repo(tmp_path)
    edit_last(root, "trend_10_50", lambda r: (r["nums"].update(extra=2), r["nums"]["all"].update(extra=1500)))
    html = retro_page(root, "trend_10_50")
    cur, whole = panels(html, "차이가 어디서 났나")
    assert "실제에만 있는 체결" not in squash(cur) and "실제에만 있는 체결 1,500건(원인 모름)" in squash(whole)
    fills = squash(panel(html, "체결 대조"))
    assert "이번 구간(2026-10-07부터) 값이에요." in fills and "실제에만 있는 체결 2건(원인 모름)" in fills
    one = write_facts(tmp_path / "one", ("trend_10_50",))
    assert "값이에요." not in squash(panel(retro_page(one, "trend_10_50"), "체결 대조"))


def test_first_retro_path_line_needs_retro_md_head(tmp_path):
    """첫 회고 경로 줄은 retro.md 머리줄을 직접 읽어 정한다: retro.md가 일반 파일이어도 머리가 틀리고 daily/*.md가 있으면 경로 줄이 없다."""
    root = write_facts(tmp_path, ("trend_10_50",))
    folder = root / "strategies" / "trend_10_50"
    (folder / "retro.md").write_text("# 다른 글\n", encoding="utf-8")
    (folder / "daily" / "2026-10-14.md").write_text("# 일별\n", encoding="utf-8")
    text = squash(panel(retro_page(root, "trend_10_50"), "체결 대조"))
    assert "retro.md에 있어요" not in text and "건별 짝 표가 필요하면" in text
    (folder / "retro.md").write_text(f"{RETRO_HEAD}x\n", encoding="utf-8")
    assert "retro.md에 있어요" in squash(panel(retro_page(root, "trend_10_50"), "체결 대조"))


def test_period_title_uses_all_start_like_terminal(tmp_path):
    """칸 제목의 날은 터미널 status 「실전 시작부터(M/D ~ …)」와 같은 `nums.all.start`다(가드레일 live_start가 달라도)."""
    root = two_period_repo(tmp_path)
    edit_guardrail(root, "trend_10_50", lambda g: g.update(live_start="2026-09-01"))
    edit_days(root, "trend_10_50", lambda r: r["nums"]["all"].update(start="2026-09-20"))
    line = next(x for x in term_status(root, "trend_10_50") if x.startswith("실전 시작부터("))
    month, day = re.search(r"\((\d+)/(\d+) ~", line).groups()
    html = retro_page(root, "trend_10_50")
    titles = [re.search(r'<h2 class="h2">(.*?)</h2>', x)[1] for x in numbers_panels(html)]
    assert titles[1] == f"실전 시작부터(2026-{int(month):02d}-{int(day):02d}부터)" == "실전 시작부터(2026-09-20부터)"


# ── 묶음 고침 ───────────────────────────────────────────────────────────────────

def test_two_period_parts_headings_are_told_apart(tmp_path):
    """WCAG 2.4.6: 구간 칸이 둘이면 「차이가 어디서 났나」 제목 뒤에 그 칸 바로 아래 줄이 쓰는 구간 이름을 괄호로 붙여 갈라 준다.
    칸이 하나면 지금 그대로(구간 이름 없음)."""
    html = retro_page(two_period_repo(tmp_path / "two"), "trend_10_50")
    assert re.findall(r'<h2 class="h2">(차이가 어디서 났나[^<]*)</h2>', html) == ["차이가 어디서 났나(이번 구간)", "차이가 어디서 났나(실전 시작부터)"]
    names = re.findall(r'<h2 class="h2">차이가 어디서 났나\(([^)<]*)\)</h2><p class="sub">([^(<]*)\(', html)
    assert len(names) == 2 and all(a == b for a, b in names)  # 제목에 붙인 이름 = 아래 줄이 쓰는 구간 이름
    assert len(set(re.findall(r'<h2 class="h2">[^<]*</h2>', html))) == len(re.findall(r'<h2 class="h2">[^<]*</h2>', html))  # 같은 h2가 두 번 나오지 않는다
    one = retro_page(write_facts(tmp_path / "one", ("trend_10_50",)), "trend_10_50")
    assert re.findall(r'<h2 class="h2">(차이가 어디서 났나[^<]*)</h2>', one) == ["차이가 어디서 났나"]


def test_b_legend_swatch_has_the_charts_hatch(tmp_path):
    """WCAG 1.4.1: 차트 B 범례의 「잰 것」 견본은 차트 면과 같은 사선 무늬(`url(#b-hatch)`, 무늬 정의는 같은 문서 안 차트 SVG 하나)를 쓰고
    「나머지(추정)」 견본은 무늬 없는 단색이라, 흑백에서도 사선 유무로 갈린다."""
    html = retro_page(two_period_repo(tmp_path), "trend_10_50")
    legend = re.search(r'<ul class="legend".*?</ul>', panel(html, "차이가 쌓인 모습"), re.S)[0]
    swatches = re.findall(r'<svg class="sw"[^>]*>(.*?)</svg>', legend)
    assert swatches[:2] == ['<rect class="s-slow" x="1" y="1" width="26" height="10"/>', '<rect class="s-fast" x="1" y="1" width="26" height="10"/>']
    assert html.count('<pattern id="b-hatch"') == 1 and html.count('id="b-hatch"') == 1  # 정의는 문서에 하나, 견본은 그것을 가리킨다
    assert re.search(r"\.s-slow\{fill:url\(#b-hatch\)\}", web.css()) and web._SWATCH["slow"] == swatches[0]


# ── 디자인 검토 ─────────────────────────────────────────────────────────

def _h2s(html: str) -> list[str]:
    """문서 순서대로 `h2` 글들."""
    return re.findall(r'<h2 class="h2">([^<]*)</h2>', html)


def test_two_period_panels_stack_by_period(tmp_path):
    """구간 칸이 둘이면 한 격자(`g2 eq by`) 안의 문서 순서가 숫자(이번) → 기준 줄 · 비용 안내(감싸개 하나) → 차이(이번) → 숫자(실전) → 차이(실전)이다.
    좁은 폭에서 이번 구간의 「나머지」 줄이 첫 화면 안에 들면서도 비용 안내가 설명하는 「차이(비용 0)」 칸 가까이 있다. 노트 둘은 격자 밖으로 나가지 않는다."""
    html = retro_page(two_period_repo(tmp_path), "trend_10_50")
    four = [h for h in _h2s(html) if h.startswith(("이번 구간(", "실전 시작부터(", "차이가 어디서 났나"))]
    assert len(four) == 4 and four[0].startswith("이번 구간(") and four[1] == "차이가 어디서 났나(이번 구간)"
    assert four[2].startswith("실전 시작부터(") and four[3] == "차이가 어디서 났나(실전 시작부터)"
    start, end = html.index('<div class="g2 eq by">'), html.index('<section class="panel"><h2 class="h2">차이가 쌓인 모습')
    grid = html[start:end]
    assert html.count('class="g2 eq by"') == 1 and "g2 eq by" not in html[end:]
    assert re.findall(r'<(section class="panel"|div class="stack")>', grid) == ['section class="panel"', 'div class="stack"', 'section class="panel"',
                                                                                'section class="panel"', 'section class="panel"']
    notes = re.search(r'<div class="stack"><p class="sub keep">기준 [^<]*</p><p class="sub">' + re.escape(web.DIFF_NOTE) + r"</p></div>", grid)
    assert notes and 'class="sub keep">기준 ' in grid
    at = [grid.index(x) for x in ('<h2 class="h2">이번 구간(', notes[0], '<h2 class="h2">차이가 어디서 났나(이번 구간)', '<h2 class="h2">실전 시작부터(',
                                  '<h2 class="h2">차이가 어디서 났나(실전 시작부터)')]
    assert at == sorted(at) and html.count(web.DIFF_NOTE) == 1
    assert copy_violations(html) == []


def test_two_period_grid_is_two_columns_by_period_from_1024():
    """1024 이상에서는 BASE처럼 보인다 — 위 줄 = 숫자 둘(왼쪽 이번, 오른쪽 실전), 가운데 줄 = 기준 줄 · 비용 안내가 두 열에 걸침, 아래 줄 = 차이 둘.
    격자 영역으로 지정한다(문서 순서는 그대로: 숫자 이번, 노트, 차이 이번, 숫자 실전, 차이 실전). 이 규칙은 미디어 쿼리 안에만 있어 1024 미만은 한 열로 쌓인다."""
    css = web.css()
    media = re.search(r"@media \(min-width:1024px\)\{\.g2\{[^\n]*", css)[0]
    assert ".g2.eq{grid-template-columns:repeat(2,minmax(0,1fr))}" in media
    assert '.g2.by{grid-template-areas:"cn wn" "nt nt" "cd wd"}' in media  # 노트(nt)는 두 열에 걸친다
    areas = re.findall(r"\.g2\.by>:nth-child\((\d)\)\{grid-area:(\w+)\}", media)
    assert areas == [("1", "cn"), ("2", "nt"), ("3", "cd"), ("4", "wn"), ("5", "wd")]  # 문서 순서 → 영역
    assert css.count(".g2.by") == 6 and "grid-auto-flow" not in css and ".by" not in css.split("@media (min-width:1024px)")[0]


def test_one_period_retro_layout_is_unchanged(tmp_path):
    """구간 칸이 하나이면 지금 그대로다 — 숫자 한 칸 → 기준 줄 → 비용 안내 → 차이 한 칸(둘 다 한 열 `stack`), 격자 없음."""
    html = retro_page(write_facts(tmp_path, ("trend_10_50",)), "trend_10_50")
    assert "g2 eq" not in html and html.count('<div class="stack"><section class="panel">') == 2
    assert 'class="g2' not in html.split("</style>")[1]
    heads = [h for h in _h2s(html) if h.startswith(("실전 시작부터(", "차이가 어디서 났나"))]
    assert heads == [heads[0], "차이가 어디서 났나"] and heads[0].startswith("실전 시작부터(")
    i = [html.index(x) for x in (heads[0], '<p class="sub keep">기준 ', f'<p class="sub">{web.DIFF_NOTE}</p>', "차이가 어디서 났나</h2>")]
    assert i == sorted(i)


H3_AFTER = ["끈 이유", "다음 점검에서 먼저 볼 것", "실전 기간 전체", "그 기간의 경고와 고객 결정"]
"""끈 뒤 칸의 `h3` 순서: 끈 이유 → 다음 점검에서 먼저 볼 것 → 실전 기간 전체 → 그 기간의 경고와 고객 결정."""


def test_after_panel_puts_reason_and_next_check_first(tmp_path):
    """끈 뒤 칸 안 순서는 머리 문장 → 끈 이유 → 다음 점검에서 먼저 볼 것 → 실전 기간 전체 → 그 기간의 경고와 고객 결정.
    중지(grid_v1)와 중지 → 은퇴(breakout_v2) 둘 다. 「실전 N부터 M까지 거래」 줄은 실전 기간 전체 머리 문장이라 그 앞에 붙어 같이 내려가고,
    「근거는 …」 줄은 다음 점검 쪽에 남고, after.md 안내 줄은 맨 끝이다."""
    root = write_facts(tmp_path)
    off(root, "grid_v1", "중지", "2026-10-16", "전략이 안 맞아요", "다음 달에 다시 본다")
    off(root, "breakout_v2", "중지", "2026-10-16", "체결을 못 받아요", "키부터 다시 연결")
    off(root, "breakout_v2", "은퇴", "2026-10-18", "고치지 않기로 함", "접는다")
    for name, state in (("grid_v1", "중지"), ("breakout_v2", "중지")):
        html = after_html(root, name)
        assert re.findall(r'<h3 class="h2">([^<]*)</h3>', html) == H3_AFTER, name
        at = [html.index(x) for x in (f"{state}한 전략이에요(2026-10-16부터)", '<h3 class="h2">끈 이유</h3>', '<p class="lesson">', "근거는 ",
                                       "<p class=\"sub keep\">실전 ", '<h3 class="h2">실전 기간 전체</h3>', '<h3 class="h2">그 기간의 경고와 고객 결정</h3>',
                                       "after.md에도 있어요")]
        assert at == sorted(at) and len(set(at)) == len(at), (name, at)
        assert html.index('<h3 class="h2">다음 점검에서 먼저 볼 것</h3>') < html.index('<p class="lesson">')
        assert ("은퇴 2026-10-18" in html) == (name == "breakout_v2")
        if name == "breakout_v2":
            assert html.index("은퇴 2026-10-18") < html.index('<h3 class="h2">끈 이유</h3>')  # 은퇴 한 줄은 머리에 남는다
        assert copy_violations(retro_page(root, name)) == []

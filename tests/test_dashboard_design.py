"""화면 틀(screen/web.py) 토큰 · CSS · 칩 · 글 도우미 · 페이지 · 홈 뼈대.

토큰 대비는 WCAG 대비 식을 `web.TOKENS`에서 계산한다. 제품 코드에는 계산이 들 까닭이 없어 여기에 둔다.
"""
import colorsys
import math
import re
import xml.etree.ElementTree as ET
from html.parser import HTMLParser

import pytest
from facts import write_facts
from test_dashboard_pages import get_pages, home_cases, page_routes
from webrules import copy_hits

from blackclip import states
from blackclip.record import BROKEN
from blackclip.screen import chart, status, web
from blackclip.screen.paint import SYMBOL
from blackclip.workspace import WorkspaceError

DESIGN_MD = {
    "canvas": "#030304", "ink": "#F4F4F6", "muted": "#B4B4BE", "surface": "#111113", "surface-2": "#1B1B1F",
    "evidence": "#93A5FF", "evidence-wash": "rgb(147 165 255 / .16)", "evidence-line": "rgb(147 165 255 / .50)",
    "caution": "#F0B45C", "control-border": "rgb(255 255 255 / .40)", "hairline-2": "rgb(255 255 255 / .12)",
    "fill-soft": "rgb(255 255 255 / .06)", "focus": "rgb(255 255 255 / .82)", "selection": "rgb(147 165 255 / .32)",
}
"""디자인 시스템에서 옮긴 값(그 파일을 읽지 않고 상수로 묶음)."""


def _rgba(token: str) -> tuple[tuple[int, int, int], float]:
    """토큰 값(`#RRGGBB` 또는 `rgb(R G B / a)`)을 (RGB, 알파)로."""
    if token.startswith("#"):
        return (int(token[1:3], 16), int(token[3:5], 16), int(token[5:7], 16)), 1.0
    m = re.fullmatch(r"rgb\((\d+) (\d+) (\d+) / (\.?\d+(?:\.\d+)?)\)", token)
    assert m, token
    return (int(m[1]), int(m[2]), int(m[3])), float(m[4])


def _over(fg, a, bg):
    """알파 색을 불투명 바탕 위에 채널별 반올림으로 합성(디자인 시스템의 `#767679`와 같은 식)."""
    return tuple(round(f * a + b * (1 - a)) for f, b in zip(fg, bg))


def _lum(c) -> float:
    """WCAG 상대 휘도."""
    f = lambda x: x / 255 / 12.92 if x / 255 <= 0.04045 else ((x / 255 + 0.055) / 1.055) ** 2.4
    return 0.2126 * f(c[0]) + 0.7152 * f(c[1]) + 0.0722 * f(c[2])


def _cr(a, b) -> float:
    """대비 비, 소수 셋째 자리에서 버림."""
    hi, lo = max(_lum(a), _lum(b)), min(_lum(a), _lum(b))
    return math.floor((hi + 0.05) / (lo + 0.05) * 1000) / 1000


def _pairs() -> list[tuple[str, tuple, tuple, float]]:
    """대비 짝 가운데 장식(기준 없음)을 뺀 (이름, 앞, 뒤, 기준). 모두 `web.TOKENS`에서 계산한다.

    빠진 것: 기준 0인 참고 짝, 「목업 그대로」 둘(목업 값이라 제품 짝이 아님, 하나는 일부러 1.200 미달),
    TOKENS에 없는 값을 쓰는 `muted-on-glass` 한 짝과 `fill-press`(눌림) 두 짝.
    """
    t = {k: _rgba(v) for k, v in web.TOKENS.items()}
    o = {k: v[0] for k, v in t.items()}
    canvas, surface, surface2 = o["canvas"], o["surface"], o["surface-2"]
    ink, muted, evid, caut = o["ink"], o["muted"], o["evidence"], o["caution"]
    over = lambda name, bg: _over(*t[name], bg)
    band_c, band_s = over("evidence-wash", canvas), over("evidence-wash", surface)
    cb_c, cb_s2 = (over("control-border", x) for x in (canvas, surface2))
    focus_c, focus_s = over("focus", canvas), over("focus", surface)
    hover = over("fill-soft", surface)
    return [
        # 글자 4.5
        ("ink / canvas", ink, canvas, 4.5), ("ink / surface", ink, surface, 4.5), ("ink / surface-2", ink, surface2, 4.5),
        ("ink / 띠(canvas 위 wash)", ink, band_c, 4.5), ("ink / 띠(surface 위 wash)", ink, band_s, 4.5),
        ("muted / canvas", muted, canvas, 4.5), ("muted / surface", muted, surface, 4.5), ("muted / surface-2", muted, surface2, 4.5),
        ("muted / 띠(canvas 위 wash)", muted, band_c, 4.5), ("muted / 띠(surface 위 wash)", muted, band_s, 4.5),
        ("evidence / canvas", evid, canvas, 4.5), ("evidence / surface", evid, surface, 4.5),
        ("evidence / surface-2", evid, surface2, 4.5),
        ("caution / canvas", caut, canvas, 4.5), ("caution / surface", caut, surface, 4.5),
        ("caution / surface-2", caut, surface2, 4.5), ("caution / 띠(canvas 위 wash)", caut, band_c, 4.5),
        ("canvas / ink", canvas, ink, 4.5),
        ("ink / hover 행", ink, hover, 4.5), ("muted / hover 행", muted, hover, 4.5),
        ("evidence / hover 행", evid, hover, 4.5), ("caution / hover 행", caut, hover, 4.5),
        # 비글자 3.0: 칩
        ("chip 테두리 / 칩 면", cb_s2, surface2, 3.0), ("chip 테두리 / canvas", cb_s2, canvas, 3.0),
        ("chip 테두리 / surface", cb_s2, surface, 3.0), ("chip 모양 ink / surface-2", ink, surface2, 3.0),
        ("chip 모양 muted / surface-2", muted, surface2, 3.0), ("chip 모양 caution / surface-2", caut, surface2, 3.0),
        # 비글자 3.0: 차트
        ("띠 경계선 evidence / canvas", evid, canvas, 3.0), ("띠 경계선 evidence / surface", evid, surface, 3.0),
        ("백테스트 선 evidence / 띠(canvas 위)", evid, band_c, 3.0), ("백테스트 선 evidence / 띠(surface 위)", evid, band_s, 3.0),
        ("실제 선 ink / 띠(canvas 위)", ink, band_c, 3.0), ("실제 선 ink / canvas", ink, canvas, 3.0),
        ("실제 선 ink / surface", ink, surface, 3.0),
        ("누락 점 caution / 띠(canvas 위)", caut, band_c, 3.0), ("누락 점 caution / canvas", caut, canvas, 3.0),
        ("0선 점선 / 띠(canvas 위)", over("control-border", band_c), band_c, 3.0), ("0선 점선 / canvas", cb_c, canvas, 3.0),
        ("30건 선 muted / canvas", muted, canvas, 3.0), ("30건 선 muted / 띠(canvas 위)", muted, band_c, 3.0),
        ("E 실제 평균선 ink / 띠(canvas 위)", ink, band_c, 3.0),
        # 비글자 3.0: 조작
        ("포커스 링 / canvas", focus_c, canvas, 3.0), ("포커스 링 / surface", focus_s, surface, 3.0),
        ("막대 채움 ink / 길 surface-2", ink, surface2, 3.0), ("막대 길 경계 / 길 surface-2", cb_s2, surface2, 3.0),
        ("30건 눈금 ink / 길 surface-2", ink, surface2, 3.0), ("내비 활성 밑줄 evidence / surface-2", evid, surface2, 3.0),
        ("버튼 경계 / canvas", cb_c, canvas, 3.0),
    ]


def test_token_contrast_pairs():
    """글자 4.5 · 비글자 3.0 짝이 `web.TOKENS`로 모두 통과한다(장식 짝 제외)."""
    pairs = _pairs()
    anchors = {name: _cr(a, b) for name, a, b, _ in pairs}
    assert anchors["ink / canvas"] == 18.767 and anchors["chip 테두리 / 칩 면"] == 3.791  # 식이 맞는지: 알려진 값 둘
    low = [(name, _cr(a, b), need) for name, a, b, need in pairs if _cr(a, b) < need]
    assert len(pairs) == 49 and not low, f"기준 미달(짝 이름, 값, 기준): {low}"


def test_no_red_or_green_tokens():
    """불투명 색 토큰의 색상각이 빨강(0~20도 · 340~360도) · 초록(90~150도) 밖이다(채도 0.15 미만은 건너뜀)."""
    bad = []
    for name, value in web.TOKENS.items():
        rgb, alpha = _rgba(value)
        if alpha < 1:
            continue
        h, s, _ = colorsys.rgb_to_hsv(*(x / 255 for x in rgb))
        deg = h * 360
        if s >= 0.15 and (deg <= 20 or deg >= 340 or 90 <= deg <= 150):
            bad.append((name, value, round(deg)))
    assert not bad, bad


def test_tokens_match_design_md_values():
    """TOKENS가 디자인 시스템 값(`DESIGN_MD`)과 같고, :root에 `--studio-color-이름`으로 모두 나온다."""
    assert web.TOKENS == DESIGN_MD
    root = web.css().split("}")[0]
    assert all(f"--studio-color-{k}:{v};" in root for k, v in DESIGN_MD.items())


def _rule(css: str, cls: str) -> str:
    """`.cls`로 시작하는 선택자 규칙 본문들(없으면 빈 글)."""
    return "".join(m[1] for m in re.findall(rf"(\.{cls}(?![\w-])[^{{}}]*)\{{([^{{}}]*)\}}", css))


def test_css_root_rules():
    """바탕 규칙: html, html body + color-scheme + 클래스 상속 + 포커스, 글자 있는 부품은 자기 클래스에 color."""
    css = web.css()
    assert css.startswith(":root{color-scheme:dark;")
    assert "html, html body{" in css and "[class]{color:inherit;font-family:inherit}" in css
    assert ":focus-visible{outline:2px solid var(--studio-color-focus);outline-offset:2px}" in css
    for cls in ("chip", "cell", "btn", "sub", "legend", "link", "why", "display", "item-name", "item", "nav-link", "quote", "cmd", "fig", "tick", "xax", "list"):
        assert re.search(r"(?<![-\w])color\s*:", _rule(css, cls)), f".{cls}에 color가 없음"
    for token in ("space-1", "space-7", "radius-chip", "radius-row", "radius-card", "radius-panel", "radius-pill"):
        assert f"--studio-{token}:" in css
    assert "Wanted Sans" in css and "tabular-nums" in css


def test_css_responsive_rules():
    """본문 1280 가운데, 1024에서 2열, 표는 가로 스크롤 상자, 한국어 어절 유지, 조작 44px."""
    css = web.css()
    assert "max-width:1280px" in css and "@media (min-width:1024px){" in css and "overflow-x:auto" in css
    assert "word-break:keep-all" in css and "min-height:44px" in _rule(css, "hit") and "min-height:44px" in _rule(css, "btn")
    assert "padding-left:max(" in _rule(css, "wrap")  # 바깥 여백 16 이상 + 안전 영역


def test_css_type_hierarchy():
    """화면 제목 `.display` 24/32(480 이상 28/36) 500 자간 -0.02em, 구역 제목 `.h2` title 16/26 600, 표 행 머리 section 14/20 500."""
    css = web.css()
    base = re.search(r"\.display\{([^}]*)\}", css)[1]
    for want in ("margin:0", "color:var(--studio-color-ink)", "font-size:1.5rem", "line-height:2rem", "font-weight:500", "letter-spacing:-0.02em"):
        assert want in base, want
    wide = re.search(r"@media \(min-width:480px\)\{\.display\{([^}]*)\}\}", css)[1]
    assert "font-size:1.75rem" in wide and "line-height:2.25rem" in wide
    h2 = _rule(css, "h2")
    assert "font-size:1rem" in h2 and "line-height:1.625rem" in h2 and "font-weight:600" in h2 and "margin:0" in h2
    for cls in ("cell-name", "item-name"):  # 600은 구역 제목만: 행 머리는 모든 표에서 500
        rule = _rule(css, cls)
        assert "font-size:.875rem" in rule and "line-height:1.25rem" in rule and "font-weight:500" in rule and "600" not in rule, cls
    assert ".title{" not in css  # 쓰는 곳이 없어진 클래스는 지운다


def test_css_two_renders_under_and_over_1024():
    """1024 미만은 쌓은 목록, 이상은 표(`tbl-fit` 쌍 = 컨테이너 쌍이 아닌 `wide` · `narrow`). 줄 사이는 hairline-2 줄(카드 아님), 값 쌍은 `dl.pairs`."""
    css = web.css()
    assert "@media (max-width:1023.98px){.wide:not(.cq){display:none}}" in css and "@media (min-width:1024px){.narrow:not(.cq){display:none}}" in css
    assert css.index(".rows{") < css.index("@media (max-width:1023.98px)") and css.index(".scroll{") < css.index("@media (max-width:1023.98px)")  # 켜는 규칙보다 뒤
    item = _rule(css, "item")
    assert "border-bottom:1px solid var(--studio-color-hairline-2)" in item and "background" not in item and "border-radius" not in item
    assert "display:grid" in _rule(css, "pairs") and "list-style:none" in _rule(css, "rows")


def test_panels_are_inline_size_containers():
    """패널이 인라인 크기 컨테이너라 표 쌍이 창이 아니라 패널 폭을 따른다."""
    assert "container-type:inline-size" in _rule(web.css(), "panel")


def test_wide_table_switch_is_container_query():
    """컨테이너 쌍(`cq`)은 패널 폭 48rem 미만에서 표(`wide`)를 숨기고 목록(`narrow`)을, 이상에서 반대를 보인다. 48rem은 표 min-width + 패널 안쪽 여백 32px보다 크다."""
    css = web.css()
    rules = re.findall(r"^@container \(([^)]*)\)\{(.*)\}$", css, re.M)
    assert rules[:2] == [("width < 48rem", ".cq.wide{display:none}"), ("width >= 48rem", ".cq.narrow{display:none}")]  # 범위 문법: 두 쪽 사이에 빈틈이 없다
    assert "padding:var(--studio-space-4)" in _rule(css, "panel") and web.SPACE[3] == 16
    table_min = float(re.search(r"min-width:([\d.]+)rem", _rule(css, "tbl"))[1])
    assert table_min + 2 * web.SPACE[3] / 16 < 48  # 44 + 2 = 46


FIT_MIN_PX = {16: 254.63, 32: 420.25}
"""브라우저로 잰 `tbl-fit` 원인 셋 표의 최소 폭(px): 루트 글자 16px(100%) · 32px(200%). 표 칸 안쪽 여백이 px라 rem으로는 루트가 작을수록 크다. 기준 표는 9.57rem으로 더 좁다."""

FIT_PANEL_PX = 377
"""1024 창 · 루트 100%에서 가드레일 화면 원인 셋 패널(g2 2열)의 안쪽 폭(px, 브라우저 실측). 이 폭에서는 표가 그대로 보여야 한다."""


def test_fit_tables_fall_back_to_list_when_panel_is_narrower_than_the_table():
    """`tbl-fit` 쌍은 창 폭 미디어 쿼리(1024 이상 표)를 그대로 두되, 패널 내용 폭이 표 최소 폭보다 좁으면 목록을 보인다(1024 · 루트 200%에서 가로 스크롤 상자가 생기지 않게)."""
    css = web.css()
    rules = re.findall(r"^@container \(([^)]*)\)\{(.*)\}$", css, re.M)
    assert rules[3:] == [("width < 9rem", ".nw{white-space:normal}")]  # 뒤에 더한 한 줄 규칙(표 규칙은 앞 셋, `calc` 조건 규칙은 이 정규식에 안 잡힘)
    rules = rules[:3]
    assert len(rules) == 3 and rules[2][1] == ".wide:not(.cq){display:none}.narrow:not(.cq){display:grid}"
    cond = re.fullmatch(r"width < ([\d.]+)rem", rules[2][0])
    assert cond, rules[2][0]
    limit = float(cond[1])
    assert limit == math.ceil(max(px / root for root, px in FIT_MIN_PX.items()))  # 잰 값 중 rem으로 가장 큰 15.91을 올림한 16
    assert all(limit * root >= px for root, px in FIT_MIN_PX.items())  # 루트 100% · 200% 모두 이 폭이면 표가 들어간다
    assert FIT_PANEL_PX / 16 > limit  # 1024 · 100%는 표 그대로(377px = 23.6rem), 1024 · 200%는 377/32 = 11.8rem이라 목록
    assert css.index(rules[2][0]) > css.index("@media (min-width:1024px)")  # 같은 우선순위라 뒤에 와야 미디어 규칙을 이긴다
    assert "display:grid" in _rule(css, "rows")  # 목록을 다시 켜는 display가 `.rows`와 같다


class _Tree(HTMLParser):
    """열린 태그의 class 목록을 쌓아 두고, 클래스에 `cq`가 든 요소마다 (class 목록, 조상 class 목록들)을 모은다."""

    VOID = {"br", "hr", "img", "input", "meta", "link", "area", "base", "col", "embed", "source", "track", "wbr"}

    def __init__(self) -> None:
        super().__init__()
        self.stack: list[tuple[str, list[str]]] = []
        self.pairs: list[tuple[list[str], list[list[str]]]] = []

    def handle_starttag(self, tag, attrs):
        cls = (dict(attrs).get("class") or "").split()
        if "cq" in cls:
            self.pairs.append((cls, [c for _, c in self.stack]))
        if tag not in self.VOID:
            self.stack.append((tag, cls))

    def handle_startendtag(self, tag, attrs):
        self.handle_starttag(tag, attrs)
        if tag not in self.VOID:
            self.stack.pop()

    def handle_endtag(self, tag):
        names = [t for t, _ in self.stack]
        if tag in names:
            del self.stack[len(names) - 1 - names[::-1].index(tag):]


def _cq_pairs(html: str) -> list[tuple[list[str], list[list[str]]]]:
    """HTML 속 컨테이너 쌍 요소들의 (class 목록, 조상 class 목록들)."""
    tree = _Tree()
    tree.feed(html)
    return tree.pairs


def test_fit_tables_keep_media_query(tmp_path, monkeypatch):
    """`tbl-fit` 쌍(원인 셋 · 가드레일 기준)은 컨테이너 쌍이 아니라 미디어 쿼리(1024 이상 표) 그대로, 홈 표 둘은 컨테이너 쌍이다."""
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    root = write_facts(tmp_path / "facts")
    guard = get_pages(root, ["/", "/s/trend_10_50"])
    assert re.findall(r'<(?:div|ul) class="(?:scroll wide|rows narrow)[^"]*"', guard["/s/trend_10_50"]) == ['<div class="scroll wide"', '<ul class="rows narrow"'] * 2
    assert _cq_pairs(guard["/s/trend_10_50"]) == []
    assert [c for c, _ in _cq_pairs(guard["/"])] == [["scroll", "wide", "cq"], ["rows", "narrow", "cq"]]  # 실전 표 · 목록 한 쌍


def test_container_pairs_have_panel_ancestor(tmp_path, monkeypatch):
    """모든 화면에서 컨테이너 쌍의 조상에 `.panel`이 있다(없으면 컨테이너가 없어 쌍이 늘 숨은 채로 남는다)."""
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    seen = 0
    for label, root in home_cases(tmp_path, monkeypatch).items():
        for path, html in get_pages(root, page_routes(root)).items():
            for cls, ancestors in _cq_pairs(html):
                seen += 1
                assert any("panel" in a for a in ancestors), (label, path, cls)
                assert "panel" not in cls  # 쌍이 컨테이너 자신이 아니다
    assert seen >= 4  # 홈의 실전 · 끈 쌍이 실제로 있었다(빈 통과 방지)


def test_table_and_stack_pairs_agree_on_container_query(tmp_path, monkeypatch):
    """`_table`의 `cls`(`tbl-fit` 여부)와 짝 `_stack`의 `fit`이 같은 신호를 낸다 — 표가 `cq`면 목록도 `cq`, 표가 `tbl-fit`이면 목록도 `fit`.
    어긋나면 한 쪽이 창 폭, 다른 쪽이 패널 폭으로 바뀌어 표와 목록이 둘 다 보이거나 둘 다 숨는다. 함수 두 개와 실제 화면의 모든 짝(이름 `X 표` · `X 목록`)을 본다."""
    rows = [("h", "", [], "")]
    for cls, fit in (("", False), (" tbl-fit", True)):
        assert (" cq" in web._table("x", [("a", "")], [], cls)) == (" cq" in web._stack("x", rows, fit)) == (not fit)
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    tables: set[tuple[str, bool]] = set()  # (짝 이름, cq 여부)
    lists: set[tuple[str, bool]] = set()
    for label, root in home_cases(tmp_path, monkeypatch).items():
        for path, html in get_pages(root, page_routes(root)).items():
            tables |= {(name, bool(cls)) for cls, name in re.findall(r'<div class="scroll wide( cq)?" role="region" aria-label="([^"]+) 표"', html)}
            lists |= {(name, bool(cls)) for cls, name in re.findall(r'<ul class="rows narrow( cq)?" aria-label="([^"]+) 목록"', html)}
    assert {"실전 전략", "원인 셋", "가드레일 기준"} <= {n for n, _ in tables}  # 빈 통과 방지
    assert tables == lists


def test_container_is_not_the_switched_element():
    """컨테이너(`.panel` · 차트 `.fig`)는 숨기는 대상이 아니다(`display:none`이면 안의 쌍 · 점이 영영 안 보임). `container-type`은 이 둘에만 있다."""
    css = web.css()
    assert "display:none" not in _rule(css, "panel") and "display:none" not in _rule(css, "fig")
    assert css.count("container-type") == 2 and all("container-type" in _rule(css, c) for c in ("panel", "fig"))


def test_css_100_percent_only_in_width_values():
    """`100%`는 `width` · `max-width` 값에만 쓴다(문구 규칙의 금지 낱말 `100%`와 부딪히지 않게 CSS도 한곳에 모음)."""
    decls = re.findall(r"[{;]([\w-]+):([^;{}]*100%[^;{}]*)", web.css())
    assert decls and {p for p, _ in decls} <= {"width", "max-width"}, decls


def test_css_touch_boxes_and_current_nav():
    """`.nav-link` · `.hit`은 44×44 이상(가운데 맞춤), 지금 화면은 면 · 밑줄 없이 ink 600 글자뿐이다."""
    css = web.css()
    for cls in ("nav-link", "hit"):
        base = re.search(rf"\.{cls}\{{([^}}]*)\}}", css)[1]
        assert "min-width:44px" in base and "min-height:44px" in base and "justify-content:center" in base, cls
    cur = re.search(r'\.nav-link\[aria-current="page"\]\{([^}]*)\}', css)[1]
    assert "background" not in cur and "box-shadow" not in cur and "evidence" not in cur
    assert "color:var(--studio-color-ink)" in cur and "font-weight:600" in cur


def test_chart_css_keeps_height_and_round_dots():
    """차트는 어느 폭에서도 높이 180 이상, 점은 화면 좌표로 그리는 둥근 끝 길이 0 선(늘어나도 원)이다."""
    css = web.css()
    assert "min-height:180px" in _rule(css, "chart")
    dot, missed = _rule(css, "s-dot"), _rule(css, "s-missed")
    assert "fill:none" in dot and "stroke:var(--studio-color-ink)" in dot and "stroke-width:clamp(3px," in dot and "stroke-linecap:round" in dot  # 지름은 점 지름 식
    assert "fill:none" in missed and "stroke:var(--studio-color-caution)" in missed and "stroke-width:10" in missed and "stroke-linecap:round" in missed
    fixed = re.search(r"([^{}]*)\{vector-effect:non-scaling-stroke\}", css)[1].strip().split(",")
    assert {".s-dot", ".s-missed", ".s-actual", ".s-band"} <= set(fixed)  # 점 둘도 선처럼 화면 좌표 굵기
    assert web._SWATCH["dot"] == '<path class="s-dot" d="M14 6h0"/>' and web._SWATCH["missed"] == '<path class="s-missed" d="M14 6h0"/>'


def test_dot_css_formula_matches_constants():
    """차트 E 점 지름 = 간격 × 0.8을 3~6px로 묶은 식이 `.fig` 열 값 · 간격 토큰 · viewBox 상수(`_CW` · `_CPAD`)와 한 글자도 어긋나지 않는다."""
    css = web.css()
    fig, dot = _rule(css, "fig"), _rule(css, "s-dot")
    col = re.search(r"grid-template-columns:(\d+(?:\.\d+)?rem) minmax\(0,1fr\)", fig)[1]
    gap = re.search(r"column-gap:(var\(--studio-space-\d\))", fig)[1]
    assert col == "3.5rem" and gap == "var(--studio-space-2)" and web.SPACE[1] == 8  # 식 안 3.5rem · $s2는 이 두 값
    assert "container-type:inline-size" in fig  # 100cqw는 .fig 폭
    inner = web._CW - 2 * web._CPAD
    assert (inner, web._CW) == (708, 720)
    assert (web.DOT_MIN, web.DOT_MAX, web.DOT_RATIO) == (3, 6, 0.8)  # 정한 값(바꿀 때는 일부러 바꾼다)
    ratio = str(web.DOT_RATIO).removeprefix("0")  # CSS는 앞 0을 쓰지 않는 꼴(.8125rem처럼)
    formula = f"clamp({web.DOT_MIN}px, calc((100cqw - {col} - {gap}) * {inner} / {web._CW} * {ratio} / var(--gaps, 1)), {web.DOT_MAX}px)"
    assert f"@supports (width:1cqw){{.s-dot{{stroke-width:{formula}}}}}" in css and css.count("stroke-width:clamp(") == 1
    base = re.search(r"\.s-dot\{([^{}]*)\}", css)  # `cqw`를 모르는 브라우저는 @supports를 건너뛰어 기본 지름(상한) 그대로
    assert base and f"stroke-width:{web.DOT_MAX};" in base[1] and "clamp(" not in base[1] and base.start() < css.index("@supports (width:1cqw)")
    assert "stroke-linecap:round" in dot and "fill:none" in dot and ".s-missed{fill:none;stroke:var(--studio-color-caution);stroke-width:10;" in css  # 차트 A 점은 지금 모양


def test_cmd_rule_has_ruling_comment():
    """`.cmd`는 이 화면에서 유일한 고정폭 글자라 이유를 한 줄로 적는다. 공개 CSS에는 주석을 싣지 않고 파이썬 쪽에 둔다(공개 데모)."""
    import inspect

    css = web.css()
    assert "# .cmd 고정폭 예외: 명령 글자는 복사해 그대로 쳐야 해서" in inspect.getsource(web) and ".cmd{" in css
    assert css.count("font-family:var(--studio-font-mono)") == 1 and "/*" not in css  # 고정폭은 .cmd 한 곳, CSS 주석 0


def test_svg_line_classes_use_tokens_only():
    """SVG 선 클래스가 있고 색은 var()로만 부른다."""
    css = web.css()
    for cls in ("s-band", "s-bt", "s-actual", "s-line", "s-mean", "s-run", "s-zero", "s-start", "s-count", "s-dot", "s-missed"):
        assert re.search(r"(fill|stroke):var\(--studio-color-", _rule(css, cls)), cls
    assert "non-scaling-stroke" in css
    assert re.search(r"#[0-9A-Fa-f]{3,8}|rgb\(", css.split("}", 1)[1]) is None  # :root 밖에는 색 값 없음


CELLS = [{"day": "2026-10-10", "value": 0.01, "warn": False, "level": 3},
         {"day": "2026-10-11", "value": None, "warn": False, "level": None},
         {"day": "2026-10-12", "value": -0.02, "warn": True, "level": 0},
         {"day": "2026-10-13", "value": 0.03, "warn": False, "level": 5}]


def _row(name="trend_10_50", **kw):
    row = dict(name=name, live=True, word="경고", reason="실행 문제로 추정", actual=-0.046, bt=-0.019, gap=-0.027, trades=1234,
               day="2026-10-14", until=None, spark=CELLS, spark_start="2026-10-10", warn_days=1)
    return row | kw


ROWS = [_row(), _row("grid_v1", word="정상", reason=None, actual=0.012, bt=0.014, gap=-0.002, trades=12),
        _row("breakout_v2", word="확인 불가", reason="체결 내역을 못 받음 · 4일째", actual=0.004, bt=0.006, gap=None, until="2026-10-11"),
        _row("new_one", live=False, word=None, reason=None, actual=None, bt=None, gap=None, trades=None, day=None, spark=[],
             spark_start=None, warn_days=0),
        _row("lost_1", word="확인 불가", reason="가드레일 파일이 없어요", actual=None, bt=None, gap=None, trades=None, day=None, spark=[],
             spark_start=None, warn_days=0)]
OFF = [dict(name="old_a", state="중지", since="2026-10-01", after=True, guard=True),
       dict(name="old_b", state="은퇴", since="2026-09-20", after=False, guard=False),
       dict(name="old_c", state=BROKEN, since=None, after=False, guard=False)]


_STROKE = {"fill": "none", "stroke": "currentColor"}
CHIP_SHAPES = {
    "정상": ("0 0 12 12", {"fill": "none", "stroke": "currentColor", "stroke-width": "1.6", "stroke-linecap": "round", "stroke-linejoin": "round"},
           [("circle", {"cx": "6", "cy": "6", "r": "5"}), ("path", {"d": "M3.6 6.2l1.6 1.5 3.1-3.3"})]),
    "경고": ("0 0 10 10", {}, [("rect", {"x": "1", "y": "1", "width": "8", "height": "8", "rx": "1", **_STROKE, "stroke-width": "2"}),
                              ("circle", {"cx": "5", "cy": "5", "r": "1.5", "fill": "currentColor"})]),
    "판단 보류": ("0 0 10 10", {}, [("rect", {"x": "0.5", "y": "0.5", "width": "9", "height": "9", "rx": "1", **_STROKE, "stroke-width": "1"}),
                                 ("rect", {"x": "0.5", "y": "5", "width": "9", "height": "4.5", "fill": "currentColor"})]),
    "확인 불가": ("0 0 10 10", {}, [("rect", {"x": "0.5", "y": "0.5", "width": "9", "height": "9", "rx": "1", **_STROKE, "stroke-width": "1",
                                            "stroke-dasharray": "3 2"})]),
    "점검 불가": ("0 0 10 10", {}, [("rect", {"x": "0.5", "y": "0.5", "width": "9", "height": "9", "rx": "1", **_STROKE, "stroke-width": "1"}),
                                 ("line", {"x1": "1", "y1": "9", "x2": "9", "y2": "1", "stroke": "currentColor", "stroke-width": "1"})]),
}
"""디자인 시스템 좌표 그대로: 낱말 → (svg viewBox, svg 자체의 덧붙는 속성, [(자식 태그, 속성)])."""


def _glyph(html: str):
    """칩 HTML 한 조각을 XML로 읽어 (svg 요소, svg 뒤에 붙은 글)을 낸다. 칩 안에는 svg 하나와 글뿐이어야 한다(속성 순서와 상관없이 견줌)."""
    chip = ET.fromstring(html)
    assert [c.tag for c in chip] == ["svg"], html  # 낱말을 따로 span으로 감싸지 않는다
    return chip[0], chip[0].tail


def test_chip_shape_and_word():
    """다섯 낱말은 10×10 SVG 모양(디자인 시스템 좌표) + 낱말, 경고만 2px caution 클래스, 모르는 낱말은 점검 불가 모양 + escape된 낱말."""
    seen = []
    for word, (box, extra, kids) in CHIP_SHAPES.items():
        html = web.chip(word)
        svg, tail = _glyph(html)
        want = {"class": "chip-glyph", "viewBox": box, "aria-hidden": "true", "focusable": "false", **extra}
        assert svg.attrib == want, (word, svg.attrib)
        assert [(c.tag, c.attrib) for c in svg] == kids, word
        assert tail == word and "chip-sym" not in html and not any(sym in html for sym in SYMBOL.values()), html
        assert ("chip-warn" in html) == (word == "경고") and ("chip-ok" in html) == (word == "정상")
        assert ("chip-mute" in html) == (word not in ("정상", "경고"))
        seen.append(html[html.index("<svg"):html.index("</svg>")])
    assert len(set(seen)) == 5  # 라벨을 가려도 다섯 모양이 서로 다르다
    unknown = web.chip("<b>x</b> · y")
    svg = _glyph(unknown)[0]
    assert svg.attrib["viewBox"] == "0 0 10 10" and [c.tag for c in svg] == ["rect", "line"] and "chip-mute" in unknown  # 점검 불가 모양
    assert "</svg>&lt;b&gt;x&lt;/b&gt;, y</span>" in unknown and "<b>" not in unknown
    rule = _rule(web.css(), "chip-warn")
    assert "2px" in rule and "var(--studio-color-caution)" in rule
    assert "min-width:3.75rem" in _rule(web.css(), "chip") and "1px" in _rule(web.css(), "chip")  # 최소 폭 60, 안쪽 1px 경계
    glyph = _rule(web.css(), "chip-glyph")
    assert "flex:none" in glyph and "width:.625rem" in glyph and "height:.625rem" in glyph  # 10×10 (루트 100%)
    assert "chip-sym" not in web.css()


def test_reason_has_no_path_and_is_haeyo():
    """사유 글에는 경로 · 합쇼체 · 가운뎃점이 없다."""
    got = [web.reason(WorkspaceError("/Users/x/repo/.blackclip/strategies/a/days.jsonl 링크라 읽지 않습니다"), "매일 대조 기록"),
           web.reason(FileNotFoundError(2, "No such file", "/Users/x/repo/.blackclip/a"), "가드레일"),
           web.reason(PermissionError(13, "Permission denied", "/Users/x/repo/.blackclip/a"), "전략 기록")]
    assert got == ["매일 대조 기록 파일을 읽지 못했어요(링크이거나 모양이 달라요)", "가드레일 파일이 없어요",
                   "전략 기록 파일을 읽지 못했어요(PermissionError)"]
    assert not [g for g in got if "/" in g or "니다" in g or "·" in g]
    assert web.reason(ValueError("x"), "가드레일") == "가드레일 파일을 읽지 못했어요(ValueError)"


def test_num_obj_shapes():
    """num은 bool이 아닌 유한한 int · float만 float으로, obj는 dict만 통과시킨다."""
    assert web.num(True) is None and web.num("1") is None and web.num(float("nan")) is None and web.num(None) is None
    assert web.num(float("inf")) is None and web.num(10**400) is None
    assert web.num(3) == 3.0 and isinstance(web.num(3), float) and web.num(-0.5) == -0.5
    assert web.obj([]) == {} and web.obj("x") == {} and web.obj(None) == {}
    d = {"a": 1}
    assert web.obj(d) is d


def test_number_formats():
    """pct · pp · prob가 엔진과 같은 자릿수 · 부호이고, 0은 부호 없이, 99% 넘으면 `99% 넘게`."""
    assert web.pct(-0.046) == "-4.6%" and web.pct(0.012) == "+1.2%" and web.pct(-0.0001) == "0.0%" and web.pct(0.0) == "0.0%"
    assert web.pct(None) == "모름" and web.pct(True) == "모름" and web.pct(float("nan")) == "모름"
    assert web.pct(0.0125, 2) == "+1.25%" and web.pct(-0.0062, 2) == "-0.62%" and web.pct(10.0, 2) == "+1,000.00%"
    assert web.pp(-0.027) == "-2.7%p" and web.pp(0.0211) == "+2.1%p" and web.pp(-0.0004) == "0.0%p" and web.pp(None) == "모름"
    assert web.prob(0.79) == "79%" and web.prob(0.99) == "99%" and web.prob(0.999) == "99% 넘게" and web.prob(0.995) == "99% 넘게"
    assert web.prob(1.0) == "99% 넘게" and web.prob(0.004) == "0%" and web.prob(None) == "모름"
    assert "100%" not in "".join(web.prob(x / 1000) for x in range(1001))
    for v in (-0.046, 0.012, 0.3333, -0.0049, 0.0625, -2.5, 0.00051):  # 0이 아닌 값은 터미널과 같은 글자
        assert web.pct(v) == status._pct(v) and web.pp(v) == states.pp(v), v
    for v in (-0.0062, 0.0125, 0.0, 12.5, -0.00001):
        assert web.pct(v, 2) == chart._e_pct(v), v
    html = web.home([_row(trades=1234)], [], "2026-10-14", "tok").decode()
    assert "1,234건" in html


def test_text_helper():
    """text는 plain → ` · `를 `, `로 → escape. 그림 글자는 평문 \\uXXXX, 가운뎃점 낱말 없이."""
    assert web.text("a · b<c>&\"'") == "a, b&lt;c&gt;&amp;&quot;&#x27;"
    assert web.text("█ 막대") == "\\u2588 막대" and web.text(5) == "5"


def test_href_and_page_shell():
    """href는 ?t=을 붙이고 quote하며, page는 한 장 틀(doctype · ko · 다크 메타 · 내비 홈 · style 한 블록)이다."""
    assert web.href("/s/trend_10_50", "ab-_Cd") == "/s/trend_10_50?t=ab-_Cd"
    assert web.href("/s/a b&c", "t o&=") == "/s/a%20b%26c?t=t%20o%26%3D"
    raw = web.page("홈", "<p>x</p>", "tok", web.HOME_NAV)
    html = raw.decode()
    assert raw.startswith(b"<!doctype html>") and '<html lang="ko">' in html and "<p>x</p>" in html
    assert '<meta name="color-scheme" content="dark">' in html and '<meta name="theme-color" content="#030304">' in html
    assert html.count("<style>") == 1 and "<script" not in html and "<title>홈 | BlackClip Guardrail</title>" in html
    assert '<a class="nav-link" href="/?t=tok" aria-current="page">홈</a>' in html
    nav = web.page("a", "", "tok", (("홈", "/", False), ("<s>", None, False), ("회고", "/s/a_b/retro", True))).decode()
    assert '<a class="nav-link" href="/?t=tok">홈</a>' in nav and '<span class="nav-link">&lt;s&gt;</span>' in nav
    assert '<a class="nav-link" href="/s/a_b/retro?t=tok" aria-current="page">회고</a>' in nav


def test_cause_names_cover_tracked():
    """원인 이름 표는 TRACKED 다섯 칸을 모두 덮고 가운뎃점이 없다."""
    assert set(web.CAUSE) == set(states.TRACKED)
    assert not [v for v in web.CAUSE.values() if "·" in v or "—" in v]


def test_spark_svg_shape():
    """칸마다 막대 하나(높이는 눈금에 비례, 눈금 0도 보임), 값 없는 칸은 점, 경고 칸은 삼각형, 색은 클래스로만."""
    svg = web.spark_svg(CELLS)
    bars = re.findall(r'<rect class="spark-bar"[^>]* height="(\d+)"', svg)
    assert len(bars) == 3 and svg.count('class="spark-gap"') == 1 and svg.count('class="spark-warn"') == 1
    h3, h0, h5 = map(int, bars)  # 셀 순서: 눈금 3, 0, 5
    assert 0 < h0 < h3 < h5
    w = len(CELLS) * 10 - 2  # 막대 폭 8 · 간격 10
    assert f'height="28" viewBox="0 0 {w} 28"' in svg  # 가드레일 차트 상수(_CH)가 홈 흐름 막대의 높이를 덮지 않는다
    assert 'role="img"' in svg and 'fill="' not in svg and 'stroke="' not in svg and "style=" not in svg and "#" not in svg
    label = re.search(r'aria-label="([^"]+)"', svg)[1]
    assert label == "2026-10-10~2026-10-13 실전 시작부터 실제 손익 흐름이에요. 경고인 날은 1일이에요."  # 정해 둔 문장 그대로
    bottoms = {int(y) + int(h) for y, h in re.findall(r'<rect class="spark-bar" x="\d+" y="(\d+)" width="\d+" height="(\d+)"', svg)}
    tri = [int(y) for y in re.findall(r"(?<=[ML])\d+ (\d+)|H", re.search(r'<path class="spark-warn" d="([^"]+)"', svg)[1]) if y]
    assert len(bottoms) == 1 and min(tri) > bottoms.pop()  # 막대는 한 바닥 선에서 올라오고, 경고 삼각형은 그 아래
    longer = web.spark_svg(CELLS, start="2026-08-01", warn_days=11)  # 22일을 넘는 흐름: 시작 날 · 경고 일수는 부르는 쪽이 정함
    assert 'aria-label="2026-08-01~2026-10-13 실전 시작부터 실제 손익 흐름이에요. 경고인 날은 11일이에요."' in longer
    assert web.spark_svg([]) == ""
    assert not copy_hits(svg)


def test_home_table_structure():
    """홈 표: 칸 머리 여덟, 링크 `?t=`, 칩 + 이유, 숫자 서식, 기록 없는 줄, 흐름 SVG, 표 아래 글, 내보내기 링크, 바닥 글."""
    html = web.home(ROWS, [], "2026-10-14", "tok").decode()
    heads = re.findall(r'<th scope="col"[^>]*>(.*?)</th>', html)
    assert heads == ["전략", "상태", "실전 시작부터 실제", "같은 기간 백테스트", "차이(비용 0)", "거래", "기준 날", "흐름"]  # 뜻은 표 아래 DIFF_NOTE에 있다
    assert '<div class="head"><h1 class="display">실전 전략 4개</h1><p class="sub">기준 2026-10-14</p></div>' in html  # new_one은 live가 아니라 안 센다
    assert html.index("<h1") < html.index('<section class="panel">')  # 화면 제목은 패널 밖
    assert '<a class="link hit" href="/s/trend_10_50?t=tok">trend_10_50</a>' in html
    rows = [seg.split("</tr>")[0] for seg in html.split('<tr class="row">')[1:]]
    assert len(rows) == 5
    first, ok, late, new, lost = rows
    assert web.chip("경고") in first and '<div class="why why-warn">실행 문제로 추정</div>' in first
    assert "-4.6%" in first and "-1.9%" in first and "-2.7%p" in first and "1,234건" in first and "2026-10-14" in first
    assert "<svg" in first and "spark-bar" in first
    assert web.chip("정상") in ok and 'class="why' not in ok and "+1.2%" in ok and "-0.2%p" in ok
    assert '+0.4%<div class="sub">2026-10-11까지</div>' in late and "까지)" not in late and web.chip("확인 불가") in late and "체결 내역을 못 받음, 4일째" in late
    assert 'why why-warn' not in late and late.count("모름") == 1  # 차이만 모름
    assert web.EMPTY_PRE in new and "<svg" not in new and "chip" not in new and 'colspan="7"' in new
    assert web.chip("확인 불가") in lost and lost.count("모름") == 6  # 실제 · 백테스트 · 차이 · 거래 · 기준 날 · 흐름
    assert "차이는 비용을 0으로 둔 같은 기간 백테스트와 비교해요. 같은 기간 백테스트 칸은 보수적 비용 기준이에요." in html
    assert '<a class="btn btn-fill" href="/export.csv?t=tok">CSV 받기</a>' in html
    assert '<a class="btn btn-quiet" href="/export.json?t=tok">JSON 받기</a>' in html and html.count('class="btn btn-fill') == 1
    assert "이 화면은 이 컴퓨터에서만 열려요. 엔진은 주문을 내거나 봇을 멈추지 않아요." in html
    assert "끈 전략" not in html and "→" not in html
    assert copy_hits(html) == [], copy_hits(html)


def stack_items(html: str, label: str) -> list[str]:
    """`<ul class="rows narrow" aria-label=label>` 안 줄(`li.item`) HTML들(없으면 빈 목록)."""
    ul = re.search(rf'<ul class="rows narrow(?: cq)?" aria-label="{label}">(.*?)</ul>', html, re.S)
    return re.findall(r'<li class="item">(.*?)</li>', ul[1], re.S) if ul else []


def pairs_of(item: str) -> list[tuple[str, str]]:
    """줄 안 `dl.pairs`의 (라벨, 값) 목록(없으면 빈 목록)."""
    return re.findall(r"<div><dt>(.*?)</dt><dd>(.*?)</dd></div>", item, re.S)


def test_home_stacked_list_for_narrow_widths():
    """1024 미만용 쌓은 목록. 머리(이름 링크 + 칩) → 이유 → dl 세 값 → 꼬리(거래 · 기준 날 + 흐름) 순서이고, 표 머리와 같은 글을 라벨로 쓴다."""
    html = web.home(ROWS, OFF, "2026-10-14", "tok").decode()
    assert html.count('<div class="scroll wide cq" role="region"') == 2 and html.count('<ul class="rows narrow cq"') == 2  # 실전 · 끈 표마다 짝
    first, ok, late, new, lost = stack_items(html, "실전 전략 목록")
    assert [k for k, _ in pairs_of(first)] == ["실전 시작부터 실제", "같은 기간 백테스트", "차이(비용 0)"]  # 표 머리와 같은 글
    assert [v for _, v in pairs_of(first)] == ["-4.6%", "-1.9%", "-2.7%p"]
    head, why, dl, tail = (first.index(x) for x in ('<div class="item-head">', '<div class="why why-warn">', '<dl class="pairs">', '<div class="item-tail">'))
    assert head < why < dl < tail
    top = first[:why]
    assert '<a class="link hit" href="/s/trend_10_50?t=tok">trend_10_50</a>' in top and web.chip("경고") in top and 'class="item-name"' in top
    assert "실행 문제로 추정" in first[why:dl]
    foot = first[tail:]
    assert "거래 1,234건" in foot and "기준 날 2026-10-14" in foot and 'class="spark"' in foot
    assert web.chip("정상") in ok and 'class="why' not in ok and [v for _, v in pairs_of(ok)] == ["+1.2%", "+1.4%", "-0.2%p"]
    assert pairs_of(late)[0] == ("실전 시작부터 실제", '+0.4%<div class="sub">2026-10-11까지</div>')  # 표와 같은 caption 줄
    assert [v for _, v in pairs_of(late)[1:]] == ["+0.6%", "모름"] and "체결 내역을 못 받음, 4일째" in late
    assert web.EMPTY_PRE in new and "<dl" not in new and "chip" not in new and "item-tail" not in new and "spark" not in new  # 기록 없는 줄은 머리 + 안내 글만
    assert web.chip("확인 불가") in lost and [v for _, v in pairs_of(lost)] == ["모름"] * 3 and "거래 모름" in lost and "기준 날 모름" in lost
    stacked = html[html.index('<ul class="rows narrow cq"'):]
    assert "<tr" not in stacked.split("</ul>")[0] and 'scope="row"' not in stacked.split("</ul>")[0]  # 표를 읽는 시험 정규식에 두 번 걸리지 않는다
    a, b, c = stack_items(html, "끈 전략 목록")
    assert '<a class="link hit" href="/s/old_a/retro?t=tok">old_a</a>' in a and "중지" in a and web.AFTER_NOTE in a and pairs_of(a) == [("시작 날", "2026-10-01")]
    assert '<a class="link hit" href="/s/old_b/retro?t=tok">old_b</a>' in b and "은퇴" in b and "끈 뒤" not in b and pairs_of(b) == [("시작 날", "2026-09-20")]
    assert '/s/old_c/retro?t=tok' in c and BROKEN in c and web.BROKEN_NOTE in c and pairs_of(c) == [("시작 날", "모름")]
    assert copy_hits(html) == [], copy_hits(html)


def test_home_live_empty_text_and_count():
    """기록이 없는 실전 전략은 실전 중 글, 실전 시작 전이면 다른 글. 실전 전략 수는 live인 줄만 센다."""
    rows = [_row("a", live=True, word=None, reason=None, spark=[]), _row("b", live=False, word=None, reason=None, spark=[])]
    html = web.home(rows, [], None, "tok").decode()
    assert web.EMPTY_LIVE in html and web.EMPTY_PRE in html and "실전 전략 1개" in html and "기준" not in html.split("<table")[0]
    assert html.index("<h1") < html.index('<section class="panel">') and '<h1 class="display">실전 전략 1개</h1>' in html
    assert web.EMPTY_LIVE == "실전 중이고 매일 대조 기록이 아직 없어요" and web.EMPTY_PRE == "실전 시작 전이에요"


def test_home_off_table_only_when_present():
    """끈 전략 표는 끈 전략이 있을 때만: 상태 낱말 · 시작 날 · 끈 뒤 회고 · 기록 손상 글 · 가드레일 링크."""
    html = web.home(ROWS[:1], OFF, "2026-10-14", "tok").decode()
    off = html.split("끈 전략 3개")[1]
    for n in ("old_a", "old_b", "old_c"):  # 끈 전략 이름은 모두 회고 화면으로(가드레일 판 유무와 상관없이)
        assert f'<a class="link hit" href="/s/{n}/retro?t=tok">{n}</a>' in off and f"/s/{n}?t=" not in off
    assert "중지" in off and "은퇴" in off and BROKEN in off and "2026-10-01" in off and "2026-09-20" in off
    assert off.count("끈 뒤 회고가 있어요") == 2  # 표 한 번 + 쌓은 목록 한 번
    assert "전략 기록(record.md)을 읽지 못해 매일 대조가 멈췄어요" in off
    assert copy_hits(html) == []
    assert "끈 전략" not in web.home(ROWS[:1], [], "2026-10-14", "tok").decode()


def test_home_empty_state():
    """실전도 끈 전략도 0개면 글 한 줄 + 행동 하나만(표 · 내보내기 없음)."""
    html = web.home([], [], None, "tok").decode()
    assert "아직 실전 전략이 없어요." in html and "blackclip save &lt;전략&gt; 뒤 blackclip live &lt;전략&gt;" in html
    assert "<table" not in html and "export" not in html and "실전 전략 0개" in html
    assert copy_hits(html) == []


def test_home_escapes_repo_text():
    """레포에서 온 전략 이름 · 이유 · 날은 escape되고 가운뎃점이 `, `로 바뀐다."""
    row = _row("a<b>", reason="x \"y\" · z", day="2026-10-14<")
    html = web.home([row], [dict(name="o<", state="중지", since="2026-10-01<", after=False, guard=False)], "<d>", "t&k").decode()
    assert "<b>" not in html and "a&lt;b&gt;" in html and "x &quot;y&quot;, z" in html and "t%26k" in html
    assert "&lt;d&gt;" in html and "2026-10-14&lt;" in html and "o&lt;" in html and "2026-10-01&lt;" in html
    assert copy_hits(html) == []


def test_no_hex_outside_root_block():
    """렌더한 홈 HTML에서 `:root{…}` 밖 색(#hex)이 없다(`theme-color` 메타 제외)."""
    html = web.home(ROWS, OFF, "2026-10-14", "tok").decode()
    assert html.count(":root{") == 1
    rest = re.sub(r":root\{[^}]*\}", "", html).replace('<meta name="theme-color" content="#030304">', "")
    assert re.findall(r"#[0-9A-Fa-f]{3,8}", rest) == []
    assert "rgb(" not in rest and 'style="' not in rest


def test_href_only_same_origin_paths():
    """href는 「/」로 시작하고 「//」로 시작하지 않는 경로만 받는다(다른 출처 · 상대 경로 링크를 만들지 않음)."""
    assert web.href("/", "t") == "/?t=t"
    for bad in ("", "s/x", "//evil.example/x", "http://evil.example/", "javascript:x"):
        with pytest.raises(ValueError):
            web.href(bad, "t")


def test_name_link_only_for_strategy_names():
    """전략 이름 모양(hook._NAME)을 지난 이름만 링크, 그 밖은 escape된 글(링크 없음)."""
    assert web._name_link("trend_10_50", "t") == '<a class="link hit" href="/s/trend_10_50?t=t">trend_10_50</a>'
    for bad in ("../x", "/evil", "a b", "<s>", "-x", "a" * 65):
        out = web._name_link(bad, "t")
        assert "<a" not in out and "href" not in out and out == web.text(bad)


def test_link_is_ink_with_a_muted_underline():
    """앱 안 링크는 글자 ink, 밑줄 1px muted, hover 때 밑줄만 ink — 근거색(evidence)은 근거 · 선택에만 쓰고 링크에는 쓰지 않는다.
    대비는 ink/surface 17.1, muted 밑줄/surface 9.172. 같은 `.link`를 쓰는 홈 전략 이름 · 끈 전략 회고 링크 · 연결 「첫 회고」 · 「연결 화면으로 돌아가기」가 모두 바뀐다."""
    css = web.css()
    base, hover = re.search(r"\.link\{([^}]*)\}", css)[1], re.search(r"\.link:hover\{([^}]*)\}", css)[1]
    for want in ("color:var(--studio-color-ink)", "text-decoration:underline", "text-decoration-color:var(--studio-color-muted)",
                 "text-decoration-thickness:1px", "text-underline-offset:4px"):
        assert want in base, want
    assert hover == "text-decoration-color:var(--studio-color-ink)" and "evidence" not in base + hover
    t = {k: _rgba(v)[0] for k, v in web.TOKENS.items()}
    assert 17.1 <= _cr(t["ink"], t["surface"]) < 17.2 and _cr(t["muted"], t["surface"]) == 9.172

"""렌더러 정적 모드(공개 샘플 데모): 상대 링크 · 토큰 · 폼 · 연결 0 · 샘플 띠 · 정적 바닥글 · 공개 CSS 주석 0 · 랜딩과 같은 이름."""
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "demo"))
from sample import build_sample

from blackclip import dashboard as dash
from blackclip.screen import web
from blackclip.screen.web import Static, href, static_path


@pytest.fixture
def root(tmp_path):
    return build_sample(tmp_path / "repo", tmp_path)


def _pages(root):
    rows, off, day = dash.home_rows(root)
    return {"/": web.home(rows, off, day, Static("/")).decode(),
            "/s/trend_10_50": web.guard(dash.guard_view(root, "trend_10_50"), Static("/s/trend_10_50")).decode(),
            "/s/grid_v1/retro": web.retro(dash.retro_view(root, "grid_v1"), Static("/s/grid_v1/retro")).decode()}


def test_static_href_is_relative():
    assert href("/s/a/retro", Static("/s/b")) == "../a/retro/"
    assert href("/", Static("/s/a/retro")) == "../../../"
    assert href("/s/a", Static("/")) == "s/a/"
    assert static_path("/") == "index.html" and static_path("/s/a/retro") == "s/a/retro/index.html"
    for bad in ("/connect", "/export.csv", "/s/a/b", "/s/../x"):
        with pytest.raises(ValueError):
            href(bad, Static("/"))


def test_static_page_has_no_token_form_connect(root):
    for path, html in _pages(root).items():
        assert all(x not in html for x in ("?t=", "<form", "/connect", 'href="/', "<input", "<button")), path


def test_static_page_banner_and_lang(root):
    for path, html in _pages(root).items():
        assert '<html lang="ko">' in html and '<meta name="robots" content="noindex">' in html, path
        after = html.split("<body>\n", 1)[1]
        assert after.startswith('<div class="top"><p class="nav sub">' + web.STATIC_BANNER), path
        # 띠는 flex라 링크가 글 속 링크가 아니라 상자가 됨: 누름 상자 44를 지키는 nav-link(측정에서 64x20을 잡음)
        assert '<a class="nav-link" href="https://blackclip.app/">BlackClip 홈</a>' in after.split("</div>", 1)[0], path


def test_static_home_has_no_export_and_static_foot(root):
    home = _pages(root)["/"]
    assert "CSV 받기" not in home and "JSON 받기" not in home
    assert f'<p class="sub">{web.STATIC_FOOT}</p>' in home and web.FOOT not in home


def test_static_off_row_links_to_retro(root):
    """은퇴 전략을 누르면 정적 모드에서도 끈 뒤 회고로 간다."""
    home = _pages(root)["/"]
    assert 'href="s/grid_v1/retro/"' in home and 'href="s/grid_v1/"' not in home


def test_public_css_has_no_comment():
    assert "/*" not in web.css()


def test_brand_matches_landing(root):
    """화면 이름은 랜딩과 같은 「BlackClip Guardrail」(서버 · 정적 둘 다)."""
    rows, off, day = dash.home_rows(root)
    for html in (web.home(rows, off, day, "tok").decode(), _pages(root)["/"]):
        assert "<title>홈 | BlackClip Guardrail</title>" in html and '<span class="brand">BlackClip Guardrail</span>' in html
        assert "blackclip-guardrail" not in html


def test_static_brand_links_to_first_page(root):
    """정적 모드에서만 화면 이름이 첫 장 `/`(설치 줄)로 가는 상대 링크다. 깊은 쪽을 공유받은 사람도 첫 장에 닿는다."""
    want = {"/": "../", "/s/trend_10_50": "../../../", "/s/grid_v1/retro": "../../../../"}
    for path, html in _pages(root).items():
        assert f'<a class="nav-link" href="{want[path]}"><span class="brand">BlackClip Guardrail</span></a>' in html, path


def test_static_grid_retro_reads_as_demo(root):
    """정적 모드 끈 뒤 회고: 하루짜리 미관측은 「하루」, 이유 값은 「이유」 뒤에 읽히는 말, 보는 사람 맥에 없는 after.md 경로 줄은 없다."""
    html = _pages(root)["/s/grid_v1/retro"]
    assert "미관측 2026-10-10 하루(" in html and "2026-10-10부터 2026-10-10까지" not in html
    assert "은퇴 2026-10-13, 이유 다시 쓰지 않기로 했어요" in html
    assert "after.md" not in html

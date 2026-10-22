"""공개 샘플 데모 빌드(demo/build.py): 결정론 · 정적 · 링크가 모두 빌드 안 파일로 풀림 · 글 규칙 · 페이지 집합 · 호스팅 파일 · 첫 장."""
import os
import re
import shutil
import subprocess
import sys
from pathlib import Path, PurePosixPath
from urllib.parse import unquote

import pytest
from test_customer_copy import MARKERS
from webrules import copy_violations

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "demo"))
from build import INSTALL, build
from sample import build_sample

PAGES = {"index.html", "demo/index.html", "demo/s/breakout_v2/index.html", "demo/s/breakout_v2/retro/index.html",
         "demo/s/grid_v1/index.html", "demo/s/grid_v1/retro/index.html", "demo/s/trend_10_50/index.html",
         "demo/s/trend_10_50/retro/index.html", "demo/s/trend_ema_v7/index.html", "demo/s/trend_ema_v7/retro/index.html"}
"""첫 장 + 홈 + 실전 · 끈 전략마다 가드레일 · 회고(샘플: 실전 breakout_v2 · trend_10_50, 끈 grid_v1 · trend_ema_v7).
지금 배포본 16장에서 연결 1장과 데모에서 뺀 전략(차트 표본 100 · 1000, 기록 손상)의 장을 뺀 것."""


@pytest.fixture(scope="module")
def site(tmp_path_factory):
    out = tmp_path_factory.mktemp("site")
    build(out)
    return out


def _html(site):
    return {str(p.relative_to(site / "dist")): p.read_text(encoding="utf-8") for p in sorted((site / "dist").rglob("*.html"))}


def _links(html):
    return [h for h in re.findall(r'href="([^"]*)"', html) if not h.startswith("https://")]


def test_build_is_byte_deterministic(site, tmp_path):
    again = tmp_path / "again"
    build(again)
    def files(d):
        return {str(p.relative_to(d)): p.read_bytes() for p in sorted(d.rglob("*")) if p.is_file()}

    assert files(site) == files(again)


def test_build_pages_are_static(site):
    for name, html in _html(site).items():
        assert all(x not in html for x in ("?t=", "<form", "<script", "<input", "<button", "/connect", "127.0.0.1", "/Users/",
                                           "/private/", "/tmp/", 'href="/')), name
        assert not MARKERS.findall(html), (name, MARKERS.findall(html))  # 원문 HTML(소스 보기 · CSS 포함), 배포 관문 marker_hits와 같은 대상


def test_every_internal_link_resolves(site):
    """상대 링크를 페이지 위치에서 풀면 모두 빌드 안의 파일에 닿는다."""
    dist = site / "dist"
    for name, html in _html(site).items():
        for h in _links(html):
            target = (dist / PurePosixPath(name).parent / unquote(h)).resolve()
            target = target / "index.html" if h.endswith("/") else target
            assert target.is_file() and dist.resolve() in target.parents, (name, h)


def test_odd_strategy_name_round_trips(tmp_path):
    """전략 이름 모양(hook._NAME: 영문 대소문자 · 숫자 · 밑줄 · 하이픈)의 끝까지 링크와 파일 경로가 같은 이름으로 풀리고,
    모양 밖 폴더(공백 · % · 한글)는 화면에 나오지 않는다."""
    root = build_sample(tmp_path / "repo", tmp_path)
    for name in ("Trend-10_50", "a b%한"):
        shutil.copytree(root / "strategies" / "trend_10_50", root / "strategies" / name)
    build(tmp_path / "site", root)
    dist = tmp_path / "site" / "dist"
    home = (dist / "demo" / "index.html").read_text(encoding="utf-8")
    assert 'href="s/Trend-10_50/"' in home and (dist / "demo" / "s" / "Trend-10_50" / "retro" / "index.html").is_file()
    assert "a b" not in home and not (dist / "demo" / "s" / "a b%한").exists()


def test_render_runs_under_fake_security(tmp_path, monkeypatch):
    """샘플을 만들 때만이 아니라 그리는 동안에도 `security`는 가짜이고, 불리면 빌드가 멈춘다.
    진짜 security는 부르지 않는다(PATH에서 찾은 것이 가짜일 때만 부름)."""
    root = build_sample(tmp_path / "repo", tmp_path)
    mod = sys.modules["build"]
    real = mod.pages

    def pages(r):
        sec = shutil.which("security")
        assert sec and Path(sec).parent.name == "fakebin", sec
        subprocess.run([sec, "find-generic-password"], check=False)
        return real(r)

    monkeypatch.setattr(mod, "pages", pages)
    with pytest.raises(SystemExit, match="키체인"):
        build(tmp_path / "site", root)


def test_build_ko_copy_rules(site):
    for name, html in _html(site).items():
        assert copy_violations(html) == [], name


def test_build_page_set(site):
    assert set(_html(site)) == PAGES


def test_build_copies_headers(site):
    headers = (site / "dist" / "_headers").read_text(encoding="utf-8")
    assert ("Content-Security-Policy: default-src 'none'; style-src 'self' 'unsafe-inline'; img-src 'self' data:; base-uri 'none'; "
            "form-action 'none'; frame-ancestors 'none'") in headers and "X-Robots-Tag: noindex" in headers
    assert '"directory": "./dist"' in (site / "wrangler.jsonc").read_text(encoding="utf-8")


OLD = ("/demo/connect/", "/demo/s/mean_rev_v1/", "/demo/s/mean_rev_v1/retro/", "/demo/s/trend_100/", "/demo/s/trend_100/retro/",
       "/demo/s/trend_1000/", "/demo/s/trend_1000/retro/")
"""옛 배포본(16쪽)에만 있던 쪽. 이미 퍼진 링크가 있을 수 있어 404 대신 첫 장으로 301."""


def test_old_demo_paths_redirect_to_first_page(site):
    """옛 쪽은 슬래시 있고 없고 둘 다 첫 장으로 301. _redirects는 파일이 있어도 이기므로(Cloudflare 문서) 지금 쪽 · 루트를 덮는 줄 0, 별표 0."""
    rules = [r.split() for r in (site / "dist" / "_redirects").read_text(encoding="utf-8").splitlines() if r.strip() and not r.startswith("#")]
    assert {tuple(r) for r in rules} == {(p, "/", "301") for o in OLD for p in (o, o.rstrip("/"))}
    for src, *_ in rules:
        assert "*" not in src and ":" not in src and src != "/", src
        assert not (site / "dist" / src.lstrip("/") / "index.html").is_file(), src  # 지금 빌드에 있는 쪽을 가리면 안 됨


def test_first_page_has_install_and_demo_link(site):
    """루트 첫 장: 설치 명령 · 샘플 대시보드 링크 · 라이선스 한 줄, 「오픈소스」 0."""
    html = (site / "dist" / "index.html").read_text(encoding="utf-8")
    assert "uv tool install blackclip-guardrail" in html and "git+https" not in html
    assert "claude plugin install blackclip-guardrail --marketplace Noah-TaeHwan/blackclip-guardrail --scope project" in html
    assert 'href="demo/"' in html and "소스 공개(FSL-1.1-ALv2), 각 버전은 나온 지 2년 뒤 Apache-2.0으로 바뀌어요." in html
    assert "오픈소스" not in html and "<title>BlackClip Guardrail</title>" in html


def test_first_page_commands_scroll_instead_of_wrap(site):
    """390에서 설치 명령이 붙임표에서 줄바뀌지 않고 상자 안에서 가로로 밀린다. 첫 장에만(대시보드 CSS는 그대로)."""
    rule = ".cmd{box-sizing:border-box;max-width:100%;white-space:pre;overflow-x:auto}"
    assert rule in (site / "dist" / "index.html").read_text(encoding="utf-8")
    assert rule not in (site / "dist" / "demo" / "index.html").read_text(encoding="utf-8")


def test_first_page_install_lines_are_readme_lines():
    """첫 장 설치 명령 줄마다 README 「설치」 절의 코드 줄과 글자 그대로 같다(README를 바꾸면 첫 장도 같이)."""
    readme = (Path(__file__).resolve().parents[1] / "README.md").read_text(encoding="utf-8").splitlines()
    assert [c for c in INSTALL if f"    {c}" not in readme] == []


@pytest.mark.parametrize("bash", sorted({b for b in (shutil.which("bash"), "/bin/bash") if b and Path(b).exists()}))
def test_build_sh_deploy_line_pastes_back(tmp_path, bash):
    """빌드 끝 배포 줄을 터미널 글(UTF-8)로 복사해 붙여 돌려도 -c가 그 <site>다(공백 · 작은따옴표 · 한글 · %).
    macOS 기본 bash 3.2의 `printf %q`는 한글을 바이트로 쪼개 깨진 UTF-8을 낸다."""
    fake = tmp_path / "bin"
    fake.mkdir()
    for name in ("uv", "find"):  # 시험 · 빌드 · 표지 검사는 다른 시험이 본다. 여기는 마지막 줄만
        (fake / name).write_text("#!/bin/sh\n")
        (fake / name).chmod(0o755)
    site = tmp_path / "한글 it's %site"
    env = {**os.environ, "PATH": f"{fake}{os.pathsep}{os.environ['PATH']}", "LC_ALL": "en_US.UTF-8"}
    out = subprocess.run([bash, "demo/build.sh", str(site)], cwd=Path(__file__).resolve().parents[1], env=env,
                         capture_output=True, check=True).stdout
    cmd = out.decode("utf-8").splitlines()[-1].split(": ", 1)[1]  # 깨진 UTF-8이면 여기서 실패
    got = subprocess.run([bash, "-c", 'wrangler() { printf "%s" "$3"; }; ' + cmd], capture_output=True, check=True)
    assert got.stdout.decode() == f"{site}/wrangler.jsonc"

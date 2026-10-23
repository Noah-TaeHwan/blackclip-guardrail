"""공개 샘플 데모(guardrail.blackclip.app)를 정적 파일로 만든다. 레포 폴더에서 `uv run python demo/build.py --out <site>`.

`<site>/wrangler.jsonc`와 `<site>/dist/`(첫 장 `index.html`, 샘플 대시보드 `demo/**`, `_headers` · `_redirects`)를 쓴다. 배포는
`<site>`에서 BlackClip 계정 wrangler로 한다(이 스크립트는 배포하지 않음). 샘플 레포는 demo/sample.py로 임시 폴더에 만들고,
그 동안 가짜 `security`가 한 번이라도 불리면 멈춘다. 같은 커밋에서 두 번 빌드하면 바이트가 같다.
"""
import argparse
import html
import os
import re
import shutil
import sys
import tempfile
from pathlib import Path, PurePosixPath
from urllib.parse import unquote

sys.path.insert(0, str(Path(__file__).resolve().parent))
from sample import build_sample, fake_security

from blackclip import dashboard as dash
from blackclip.screen import web
from blackclip.screen.web import Static, static_path

SITE = Path(__file__).resolve().parent / "site"
"""레포에 둔 호스팅 파일(wrangler.jsonc · _headers · _redirects)."""

REPO_URL = "https://github.com/Noah-TaeHwan/blackclip-guardrail"

INSTALL = ("uv tool install blackclip-guardrail", "blackclip --version",
           ("claude plugin install blackclip-guardrail --marketplace https://github.com/Noah-TaeHwan/blackclip-guardrail.git"
            " --scope project"))
"""첫 장의 설치 명령(README 「설치」 절과 같은 줄). 마켓은 https 주소로: owner/repo 꼴은 SSH부터 시도해 GitHub SSH 키가 없으면 막힐 수 있음."""

FIRST_CSS = ".cmd{box-sizing:border-box;max-width:100%;white-space:pre;overflow-x:auto}"
"""첫 장에만 덧붙이는 규칙: 좁은 화면에서 명령이 붙임표에서 줄바뀌면 붙임표가 명령의 일부인지 헷갈려서 상자 안 가로 스크롤로."""


def first_page() -> bytes:
    """루트 첫 장: 한 줄 소개 · 하는 일 · 샘플 대시보드 링크 · 설치 명령 · 고지 · 라이선스. 대시보드와 같은 CSS를 쓴다.

    Returns:
        UTF-8 HTML 바이트.
    """
    does = ("봇 지켜보기(키 없이): 돌고 있는 봇이 이미 남기는 기록 파일을 읽어요. 기록이 정한 시간보다 오래 끊기면 텔레그램으로 「봇이 안 보여요」를 보내요.",
            "점검: 실전에 넣기 전에 재현성, 미래참조, 거래비용, 시도 수, 국면을 보고 그 전략에 맞는 가드레일(정상 범위와 경고선)을 만들어요.",
            "대조(고를 때만): 실제 계좌와도 맞대 보고 싶으면 조회 전용 키로 체결을 받아 같은 기간 백테스트와 매일 맞춰 보고, 벗어나면 텔레그램으로 알려 줘요.",
            "회고: 이미 봇을 돌리고 있었다면 지난 석 달 체결로 첫 회고 한 장부터 보여 줘요.",
            "로컬: 키, 체결, 전략 코드는 내 맥에만 있어요. 주문은 내지 않고, 경고만 하고 봇을 멈추지도 않아요.")
    cmds = "".join(f'<code class="cmd">{html.escape(c)}</code>' for c in INSTALL)
    body = (f'<div class="head"><h1 class="display">{web.BRAND}</h1></div>\n'
            '<section class="panel"><p class="empty">AI와 함께 만든 매매 전략으로 봇을 돌리고 있다면, 거래소 키 없이 그 봇이 남기는 기록을 지켜보다가 '
            '기록이 끊기면 텔레그램으로 깨워 주는 맥용 로컬 도구예요. 실돈 넣기 전 점검과 백테스트 대조도 해요.</p>'
            '<ul class="list">' + "".join(f"<li>{t}</li>" for t in does) + "</ul>"
            '<div class="actions"><a class="btn btn-fill" href="demo/">샘플 대시보드 보기</a>'
            f'<a class="btn btn-quiet" href="{REPO_URL}">GitHub에서 보기</a></div></section>\n'
            '<section class="panel"><h2 class="h2">설치</h2>'
            '<p class="sub">macOS, uv, Claude Code(봇 기록 어댑터와 전략 연결 파일을 만들 때), 텔레그램이 필요해요. '
            '바이낸스 조회 전용 API 키는 실제 계좌와 맞대 볼 때만 필요해요.</p>'
            f"{cmds}"
            '<p class="sub">blackclip을 못 찾는다고 나오면 uv tool update-shell을 돌리고 새 터미널을 열어요. '
            f'키 없이 붙이는 처음 10분 순서는 <a class="link" href="{REPO_URL}#readme">README</a>에 있어요.</p></section>\n'
            '<p class="sub">투자 자문이 아니에요. 점검 결과와 경고는 참고용 숫자이고 수익을 보장하지 않아요.</p>'
            '<p class="sub">소스 공개(FSL-1.1-ALv2), 각 버전은 나온 지 2년 뒤 Apache-2.0으로 바뀌어요.</p>')
    return ('<!doctype html>\n<html lang="ko">\n<head>\n<meta charset="utf-8">\n'
            '<meta name="viewport" content="width=device-width,initial-scale=1,viewport-fit=cover">\n'
            '<meta name="color-scheme" content="dark">\n'
            f'<meta name="theme-color" content="{web.TOKENS["canvas"]}">\n'
            f"<title>{web.BRAND}</title>\n<style>{web.css()}{FIRST_CSS}</style>\n</head>\n<body>\n"
            f'<header class="top"><nav class="nav" aria-label="화면 이동"><span class="brand">{web.BRAND}</span>'
            '<a class="nav-link" href="demo/">샘플 대시보드</a></nav></header>\n'
            f'<main class="wrap stack">\n{body}\n</main>\n</body>\n</html>\n').encode()


def _render(root: Path, path: str) -> bytes | None:
    """서버 경로 하나를 정적 모드로 그린다(홈은 언제나 있다).

    Args:
        root: `.blackclip` 경로.
        path: `/`, `/s/<이름>`, `/s/<이름>/retro`.

    Returns:
        HTML 바이트, 그 화면이 없으면 None.
    """
    ctx = Static(path)
    if path == "/":
        return web.home(*dash.home_rows(root), ctx)
    name = path.split("/")[2]
    if path.endswith("/retro"):
        view = dash.retro_view(root, name)
        return None if view is None else web.retro(view, ctx)
    view = dash.guard_view(root, name)
    return None if view is None else web.guard(view, ctx)


def pages(root: Path) -> dict[str, bytes]:
    """홈에서 시작해 렌더러가 낸 상대 링크를 따라가며(너비 우선) 모든 화면을 그린다.

    Args:
        root: `.blackclip` 경로.

    Returns:
        서버 경로 → HTML 바이트(경로 이름순).

    Raises:
        SystemExit: 링크가 가리키는 화면이 없음(죽은 링크).
    """
    out: dict[str, bytes] = {"/": web.home(*dash.home_rows(root), Static("/"))}
    todo = ["/"]
    while todo:
        here = todo.pop(0)
        base = PurePosixPath(static_path(here)).parent
        for h in re.findall(r'href="([^"]*)"', out[here].decode()):
            if h.startswith("https://"):
                continue
            parts = [p for p in (base / unquote(h)).parts if p != "."]
            while ".." in parts:  # 상대 링크를 데모 루트 기준으로 접는다
                i = parts.index("..")
                del parts[i - 1:i + 1]
            path = "/" + "/".join(parts)
            if path in out:
                continue
            body = _render(root, path)
            if body is None:
                raise SystemExit(f"{here}의 링크 {h}가 가리키는 화면이 없어요")
            out[path] = body
            todo.append(path)
    return dict(sorted(out.items()))


def build(site: Path, root: Path | None = None) -> list[Path]:
    """`<site>`에 배포할 파일을 쓴다(있던 `<site>/dist`는 지우고 새로 씀).

    Args:
        site: 출력 폴더.
        root: 그릴 `.blackclip`. None이면 demo/sample.py로 임시 폴더에 새로 만든다.

    Returns:
        쓴 파일 경로(이름순).

    Raises:
        SystemExit: 샘플을 만들거나 그리는 동안 가짜 security가 불림, 또는 죽은 링크.
    """
    dist = site / "dist"
    if dist.exists():
        shutil.rmtree(dist)
    (dist / "demo").mkdir(parents=True)
    with tempfile.TemporaryDirectory() as tmp:
        log, old = fake_security(Path(tmp))  # 샘플을 만들고 그리는 내내(build_sample도 같은 로그에 씀)
        try:
            if root is None:
                root = build_sample(Path(tmp) / "repo", Path(tmp))
            written: list[Path] = []
            for path, body in pages(root).items():
                f = dist / "demo" / static_path(path)
                f.parent.mkdir(parents=True, exist_ok=True)
                f.write_bytes(body)
                written.append(f)
        finally:
            os.environ["PATH"] = old
        if log.read_text(encoding="utf-8"):
            raise SystemExit("빌드하는 동안 키체인(security)을 불렀어요")
    (dist / "index.html").write_bytes(first_page())
    written.append(dist / "index.html")
    for name in ("_headers", "_redirects"):
        written.append(Path(shutil.copyfile(SITE / name, dist / name)))
    written.append(Path(shutil.copyfile(SITE / "wrangler.jsonc", site / "wrangler.jsonc")))
    return sorted(written)


def main() -> None:
    """인자를 읽어 빌드하고 쓴 파일 수를 찍는다."""
    p = argparse.ArgumentParser(description="공개 샘플 데모 정적 빌드")
    p.add_argument("--out", type=Path, required=True, help="출력 폴더(<out>/wrangler.jsonc, <out>/dist/)")
    files = build(p.parse_args().out.resolve())
    print(f"{len(files)}개 파일")


if __name__ == "__main__":
    main()

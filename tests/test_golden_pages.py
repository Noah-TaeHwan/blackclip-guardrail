"""한국어 대시보드 화면 골든: 샘플 레포(demo/sample.py)를 실제 서버로 받은 네 장이 바이트 그대로다(정적 모드가 서버 화면을 바꾸지 않게).

골든 갱신: `uv run python scripts/update_golden.py`(바뀐 까닭을 커밋 · PR에 적는다).
"""
import re
import sys
from pathlib import Path

import pytest
from test_dashboard_pages import get_pages

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "demo"))
from sample import build_sample

GOLDEN = Path(__file__).resolve().parent / "golden" / "ko"
PAGES = {"home": "/", "guard-trend_10_50": "/s/trend_10_50", "retro-trend_10_50": "/s/trend_10_50/retro",
         "retro-grid_v1": "/s/grid_v1/retro"}
"""골든 파일 이름 → 서버 경로(시연 흐름: 홈 → 실전 경고 전략의 가드레일 · 회고 → 은퇴 전략의 끈 뒤 회고)."""


def golden_repo(tmp: Path) -> Path:
    """골든을 그릴 샘플 레포를 만든다.

    Args:
        tmp: 빈 임시 폴더.

    Returns:
        `.blackclip` 경로.
    """
    return build_sample(tmp / "repo", tmp)


def served(root: Path) -> dict[str, bytes]:
    """네 장을 실제 서버로 받아 접속 토큰을 `TOKEN`으로 바꾼다.

    Args:
        root: `.blackclip` 경로.

    Returns:
        골든 이름 → 본문 바이트.
    """
    got = get_pages(root, PAGES.values())
    return {name: re.sub(r"\?t=[A-Za-z0-9_\-]+", "?t=TOKEN", got[path]).encode() for name, path in PAGES.items()}


@pytest.mark.parametrize("name", PAGES)
def test_server_page_matches_golden(name, tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    assert served(golden_repo(tmp_path))[name] == (GOLDEN / f"{name}.html").read_bytes(), f"{name}: 골든과 다름(의도한 변경이면 scripts/update_golden.py)"

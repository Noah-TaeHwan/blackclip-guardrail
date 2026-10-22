"""공개 샘플 데모의 지어낸 레포(demo/sample.py): 결정론 · 시연 흐름 · 키체인 0 · 남의 폴더는 지우지 않음."""
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "demo"))
from sample import MARK, build_sample

from blackclip.dashboard import home_rows


def _files(root: Path) -> dict[str, bytes]:
    return {str(p.relative_to(root)): p.read_bytes() for p in sorted(root.rglob("*")) if p.is_file()}


def test_sample_is_deterministic(tmp_path):
    """다른 두 폴더에 만든 결과의 상대 경로 · 바이트가 같다(시계 · 임시 경로가 결과에 안 들어감)."""
    a = build_sample(tmp_path / "a" / "repo", tmp_path / "a")
    b = build_sample(tmp_path / "b" / "repo", tmp_path / "b")
    assert _files(a) == _files(b) and len(_files(a)) > 10


def test_sample_has_demo_flow(tmp_path):
    """홈 → trend_10_50(실전 · 경고) → grid_v1(은퇴, 끈 뒤 회고). 끈 날은 모두 기준일 앞이다."""
    rows, off, latest = home_rows(build_sample(tmp_path / "repo", tmp_path))
    words = {r["name"]: r["word"] for r in rows}
    states = {o["name"]: (o["state"], o["since"], o["after"]) for o in off}
    assert words["trend_10_50"] == "경고" and states["grid_v1"] == ("은퇴", "2026-10-13", True)
    assert latest == "2026-10-14" and all(since <= latest for _, since, _ in states.values())


def test_sample_never_calls_security(tmp_path):
    """만드는 동안 PATH 맨 앞의 가짜 security가 한 번도 불리지 않고, 끝나면 PATH가 돌아온다."""
    import os

    before = os.environ["PATH"]
    build_sample(tmp_path / "repo", tmp_path)
    assert (tmp_path / "security.log").read_text() == "" and os.environ["PATH"] == before


def test_sample_refuses_foreign_dir(tmp_path):
    """표지 없는 비어 있지 않은 폴더면 지우지 않고 멈춘다."""
    repo = tmp_path / "repo"
    repo.mkdir()
    (repo / "keep.txt").write_text("x")
    with pytest.raises(SystemExit):
        build_sample(repo, tmp_path)
    assert (repo / "keep.txt").read_text() == "x" and not (repo / MARK).exists()

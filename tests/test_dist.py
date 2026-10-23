"""배포 묶음(휠 · sdist): 들어갈 것만 들어가고, 공개 메타데이터가 있다(공개 준비, 설치 길).

`uv build`로 임시 폴더에 실제로 만들어 본다. uv가 없으면 skip하지 않고 실패한다(설치 길의 전제).
"""
import re
import shutil
import subprocess
import tarfile
import zipfile
from email.parser import Parser
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent

WHEEL_OK = re.compile(r"blackclip/[^/].*\.py|blackclip_guardrail-[^/]+\.dist-info/.+")
"""휠에 들어가도 되는 경로: 패키지 소스(.py)와 dist-info만."""

SDIST_OK = {"README.md", "pyproject.toml", "PKG-INFO", ".gitignore", "LICENSE"}
"""sdist 꼭대기에 들어가도 되는 파일(그 밖에는 src/blackclip/ 아래 .py만). LICENSE는 라이선스를 정하면 들어감."""


@pytest.fixture(scope="module")
def dist(tmp_path_factory):
    """`uv build`로 만든 (휠, sdist) 경로."""
    uv = shutil.which("uv")
    assert uv, "uv가 PATH에 없음(설치 길의 전제)"
    out = tmp_path_factory.mktemp("dist")
    subprocess.run([uv, "build", "--out-dir", str(out), str(ROOT)], check=True, capture_output=True)
    return next(out.glob("*.whl")), next(out.glob("*.tar.gz"))


def test_dist_contents(dist):
    """허용 목록: 휠에는 패키지 .py와 dist-info만, sdist에는 src/blackclip의 .py와 README · pyproject(· PKG-INFO 등)만."""
    whl, sdist = dist
    names = zipfile.ZipFile(whl).namelist()
    assert "blackclip/__init__.py" in names and "blackclip/cli.py" in names
    assert [n for n in names if not WHEEL_OK.fullmatch(n)] == []
    with tarfile.open(sdist) as t:
        snames = [m.name.split("/", 1)[1] for m in t.getmembers() if m.isfile()]
    assert {"README.md", "pyproject.toml", "src/blackclip/__init__.py"} <= set(snames)
    assert [n for n in snames if n not in SDIST_OK and not re.fullmatch(r"src/blackclip/[^/].*\.py", n)] == []


def test_allow_lists_reject_strays():
    """허용 목록이 헛돌지 않게: 예전에 지나가던 꼴(플러그인 · 마켓 파일 · 숨은 설정 · 시험)을 막는다."""
    for n in ("plugin/hooks/hooks.json", "private.json", "blackclip/data.json", "tests/probe.py"):
        assert not WHEEL_OK.fullmatch(n), n
    for n in (".claude-plugin/marketplace.json", ".env", "tests/test_dummy.py", "src/blackclip/x.json"):
        assert n not in SDIST_OK and not re.fullmatch(r"src/blackclip/[^/].*\.py", n), n


def test_metadata(dist):
    """휠 METADATA(설치된 것이 아니라 방금 만든 것): 파이썬 하한 · 주소 둘 이상 · README가 긴 설명(markdown)."""
    whl, _ = dist
    z = zipfile.ZipFile(whl)
    meta = Parser().parsestr(z.read(next(n for n in z.namelist() if n.endswith(".dist-info/METADATA"))).decode())
    assert meta["Requires-Python"] == ">=3.11"
    assert len(meta.get_all("Project-URL") or []) >= 2
    assert meta["Description-Content-Type"] == "text/markdown"
    assert "Operating System :: MacOS" in (meta.get_all("Classifier") or [])
    head = str(meta.get_payload()).lstrip().splitlines()
    # PyPI 긴 설명은 레포 밖에서 보이므로 한국어 링크가 절대 주소여야 안 깨짐
    assert head[0] == "English | [한국어](https://github.com/Noah-TaeHwan/blackclip-guardrail/blob/main/README.ko.md)"
    assert head[2] == "# BlackClip Guardrail"

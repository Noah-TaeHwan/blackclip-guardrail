"""한국어 대시보드 화면 골든(tests/golden/ko/*.html)을 다시 쓴다. 레포 폴더에서 `uv run python scripts/update_golden.py`."""
import os
import sys
import tempfile
from pathlib import Path

TESTS = Path(__file__).resolve().parents[1] / "tests"
sys.path.insert(0, str(TESTS))
from test_golden_pages import GOLDEN, golden_repo, served

with tempfile.TemporaryDirectory() as tmp:
    os.environ["HOME"] = str(Path(tmp) / "home")
    GOLDEN.mkdir(parents=True, exist_ok=True)
    for name, body in served(golden_repo(Path(tmp))).items():
        (GOLDEN / f"{name}.html").write_bytes(body)
        print(name, len(body))

"""check.source: 정적 검사 대상 파일 · 신호 함수에서 닿는 줄."""
from blackclip.check.source import reachable_lines, scan

CODE = "X = 1\n\ndef helper(c):\n    return c\n\ndef unused(c):\n    return c.shift(-1)\n\ndef signals(p):\n    return helper(p)\n"


def test_reachable_lines_follows_calls(tmp_path):
    """signals에서 부르는 함수와 최상위 문장만, 안 부르는 함수는 뺀다."""
    f = tmp_path / "m.py"
    f.write_text(CODE, encoding="utf-8")
    lines = reachable_lines([f])
    assert {1, 3, 4, 9, 10} <= lines[f] and not {6, 7} & lines[f]
    assert reachable_lines([f], entry="nothing") is None


def test_scan_only_reachable(tmp_path):
    """only를 주면 그 줄만, 안 주면 파일 전체에서 찾는다."""
    f = tmp_path / "m.py"
    f.write_text(CODE, encoding="utf-8")
    pattern = [(r"shift\(", "당겨 씀", None)]
    assert scan([f], tmp_path, pattern, reachable_lines([f])) == []
    assert scan([f], tmp_path, pattern) == [("m.py:7", "당겨 씀")]


def test_reachable_lines_includes_class_constructor(tmp_path):
    """신호 함수가 만드는 클래스는 메서드 몸통(생성자)까지 닿는다고 본다."""
    f = tmp_path / "m.py"
    f.write_text("class S:\n    def __init__(self, p):\n        self.w = p.shift(-1)\n\n"
                 "def signals(p):\n    return S(p).w\n", encoding="utf-8")
    assert {1, 2, 3, 5, 6} <= reachable_lines([f])[f]
    assert scan([f], tmp_path, [(r"shift\(", "당겨 씀", None)], reachable_lines([f])) == [("m.py:3", "당겨 씀")]

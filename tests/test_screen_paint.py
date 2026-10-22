"""그리기 바탕: 넓은 글자 폭 · NO_COLOR · 같은 모양 묶음 · 반전 없음."""
import signal

import pytest

from blackclip.screen.paint import BG_GLYPH, PALETTE, SYMBOL, Canvas, cells, color_on, fit, pad, paint, rpad, wrap
from blackclip.screen.status import FORBIDDEN


@pytest.fixture(autouse=True)
def _home(monkeypatch, tmp_path):
    monkeypatch.setenv("HOME", str(tmp_path / "home"))


def test_cells_counts_wide_as_two_and_ambiguous_as_one():
    assert cells("한글AB▁━●") == 2 + 2 + 1 + 1 + 1 + 1 + 1
    assert fit("한글한글", 5) == "한글…" and cells(fit("abcdef", 4)) == 4
    assert pad("한", 4) == "한  " and rpad("-4.6%", 7) == "  -4.6%"


@pytest.mark.parametrize("value, on", [(None, True), ("", True), ("1", False), ("0", False), ("false", False)])
def test_no_color_present_and_not_empty_turns_color_off(monkeypatch, value, on):
    if value is None:
        monkeypatch.delenv("NO_COLOR", raising=False)
    else:
        monkeypatch.setenv("NO_COLOR", value)
    assert color_on() is on


def test_same_style_run_is_one_escape():
    s = paint([("━" * 16, "actual", None), ("╌╌", "bt", None), ("  ", None, None), ("    ", None, "band")], True)
    assert s.count("\x1b[0;") == 3 and s.endswith("\x1b[0m")
    assert s.startswith("\x1b[0;1m" + "━" * 16 + f"\x1b[0;38;5;{PALETTE['blue']}m╌╌\x1b[0m  ")
    assert f"48;5;{PALETTE['blue_dim']}m    " in s
    assert "38;5;" + PALETTE["blue_dim"] not in s  # 반전(전경을 바탕색으로) 없음


def test_no_color_uses_pattern_glyphs_and_no_escape():
    s = paint([("━━", "actual", None), ("   ", None, "band"), ("  ", None, "fast")], False)
    assert "\x1b" not in s and s == "━━" + BG_GLYPH["band"] * 3 + BG_GLYPH["fast"] * 2


def test_canvas_rows_wide_text_and_bounds():
    c = Canvas(6, 2)
    c.text(0, 0, "한a")
    c.put(5, 0, "●", "missed")
    c.put(9, 9, "x")  # 밖은 무시
    c.bg(1, 1, "band")
    rows = c.rows(False)
    assert rows[0] == "한a  ●" and rows[1] == " ░" and all(cells(r) <= 6 for r in rows)
    assert SYMBOL["점검 불가"] == "⊘"  # Menlo에 ⧄ 글리프가 없어 ⊘로 되돌림


def test_wrap_keeps_words_and_width():
    """범례(90칸)를 64칸에 → 두 줄, 낱말 그대로 · 모든 줄 64칸 이하."""
    s = "░ 정상 범위(한 달 90% · 그날까지)   ━ 실제   ╌ 같은 기간 BT   ┄ 경고선   ● 놓친 주문(추정)"
    lines = wrap(s, 64)
    assert len(lines) == 2 and all(cells(x) <= 64 for x in lines) and lines[1].startswith("  ")
    assert " ".join(x.strip() for x in lines).split() == s.split()
    assert wrap("짧음", 64) == ["짧음"] and cells(wrap("가" * 40, 20)[0]) <= 20


def test_edges_wide_clip_trailing_bg_and_wrap_fallbacks():
    """칸에 안 들어가는 넓은 글자는 버림 · 색에서도 끝 배경 칸은 유지 · 경계 순서와 모든 줄 폭 이하."""
    c = Canvas(5, 2)
    c.text(4, 0, "한")
    c.text(-1, 1, "한b")
    assert c.rows(False) == ["", " b"]
    c = Canvas(4, 1)
    c.bg(2, 0, "band")
    c.bg(3, 0, "band")
    assert c.rows(True) == [f"  \x1b[0;48;5;{PALETTE['blue_dim']}m  \x1b[0m"] and c.rows(False) == ["  ░░"]
    s = "가나다 · 라마바 · 사아자 · 차카타 · 파하"
    lines = wrap(s, 20)
    assert lines == ["가나다 · 라마바", "  사아자 · 차카타", "  파하"] and all(cells(x) <= 20 for x in lines)
    assert all(cells(x) <= 10 for x in wrap(s, 10))


def _bounded(fn, *args, seconds=2):
    """무한 반복이면 시험이 멈추지 않고 TimeoutError로 실패하게 한다."""
    def boom(*_):
        raise TimeoutError(f"{fn.__name__}{args}")
    old = signal.signal(signal.SIGALRM, boom)
    signal.alarm(seconds)
    try:
        return fn(*args)
    finally:
        signal.alarm(0)
        signal.signal(signal.SIGALRM, old)


@pytest.mark.parametrize("w", [-1, 0])
def test_wrap_nonpositive_width_is_empty(w):
    assert _bounded(wrap, "a b", w) == []


def test_wrap_narrow_widths_terminate_and_fit():
    """폭 1에서도 끝나고(이어지는 줄은 늘 1칸 이상), 모든 줄은 폭 이하."""
    assert _bounded(wrap, "a b", 1) == ["a", "b"]
    for text in ("a b", "한 한", "abc def ghi", "가가가 나나나 다", "a   b · c"):
        for w in range(1, 6):
            rows = _bounded(wrap, text, w)
            assert rows and all(cells(r) <= w for r in rows), (text, w, rows)


@pytest.mark.parametrize("text, width, expected", [
    ("abcdefghijkl tail", 8, ["abcdefg…", "  tail"]),
    ("가" * 10 + " 나나 다", 12, ["가가가가가…", "  나나 다"]),
    ("가" * 10 + " " + "나" * 10, 12, ["가가가가가…", "  나나나나…"]),
])
def test_wrap_oversize_chunk_is_fit_alone_and_the_rest_continues(text, width, expected):
    """폭보다 긴 덩이만 fit, 뒤 덩이는 다음 줄에 그대로."""
    rows = _bounded(wrap, text, width)
    assert rows == expected and all(cells(r) <= width for r in rows)


@pytest.mark.parametrize("write", ["put", "text"])
def test_canvas_overwriting_second_half_of_wide_glyph_clears_both_cells(write):
    """넓은 글자의 뒷칸을 덮으면 앞칸도 비운다(폭 2를 넘지 않음)."""
    c = Canvas(2, 1)
    c.text(0, 0, "한")
    getattr(c, write)(1, 0, "a")
    assert c.rows(False) == [" a"]


@pytest.mark.parametrize("write", ["put", "text"])
def test_canvas_overwriting_first_half_of_wide_glyph_keeps_later_columns(write):
    c = Canvas(4, 1)
    c.text(0, 0, "한")
    c.put(3, 0, "Z")
    getattr(c, write)(0, 0, "a")
    assert c.rows(False) == ["a  Z"]


def test_canvas_wide_glyph_over_two_wide_glyphs_clears_both_old_ones():
    c = Canvas(4, 1)
    c.text(0, 0, "한한")
    c.text(1, 0, "글")
    assert c.rows(False) == [" 글"] and cells(c.rows(False)[0]) <= 4


def test_canvas_text_attaches_combining_marks_to_previous_drawn_glyph():
    """Minor: 결합 문자는 직전에 그린 글자에 붙이고(칸 수 0), 서식 문자(Cf)와 못 그린 글자의 결합 문자는 버린다."""
    c = Canvas(4, 1)
    c.text(0, 0, "q\u0301한\u0301a\u200b")
    row = c.rows(False)[0]
    assert row == "q\u0301한\u0301a" and cells(row) == 4 and cells("\u0301") == 0
    c = Canvas(1, 1)
    c.text(0, 0, "ab\u0301")
    assert c.rows(False) == ["a"]
    c = Canvas(2, 1)
    c.text(1, 0, "한\u0301")
    assert c.rows(False) == [""]


def test_wrap_leading_spaces_belong_to_the_first_chunk():
    """앞 공백은 경계가 아니다: 빈 줄이 안 나오고, 앞 공백이 있는 글도 경계에서 나뉜다."""
    assert wrap("  abc def", 5) == ["  abc", "  def"]
    assert all(r.strip() for r in wrap("    abc def", 6))


def test_symbols_are_one_cell_and_distinct():
    """판정 기호 다섯은 모두 1칸 · 서로 다름 · status 금지 글자에 들고, 디자인 시스템의 「검증 중」 `◌`는 없다."""
    vals = list(SYMBOL.values())
    assert set(SYMBOL) == {"정상", "경고", "판단 보류", "확인 불가", "점검 불가"}
    assert all(cells(v) == 1 for v in vals) and len(set(vals)) == 5
    assert set(vals) <= FORBIDDEN and "◌" not in vals
    assert vals == ["✓", "▣", "⬓", "⬚", "⊘"]

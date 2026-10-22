"""색 층: 어두운 바탕 대비 · 디자인 토큰 근사 · 빨강 초록 0 · 글자 안 칠함.

대비는 WCAG 상대 휘도 비를 소수 셋째 자리에서 버린다. 계산은 제품 코드에 들 까닭이 없어 여기에 둔다.
"""
import colorsys
import re

from facts import write_facts

from blackclip.cli import main
from blackclip.screen.paint import BG, FG, PALETTE

DARK_BG = (3, 3, 4)
"""디자인 토큰 canvas(`#030304`): 어두운 터미널 바탕."""

DARK_FG = (0xF4, 0xF4, 0xF6)
"""디자인 토큰 ink(`#F4F4F6`): 어두운 터미널의 기본 전경(글자 · `━`)."""

TOKENS = {"blue": (0x93, 0xA5, 0xFF), "orange": (0xF0, 0xB4, 0x5C), "gray": (0xB4, 0xB4, 0xBE)}
"""디자인 토큰: evidence · warn · muted."""

EXPLAINED = {"blue": "147"}
"""가장 가까운 번호 대신 고른 번호: blue는 111이 `bt`가 `band` 위 2.95라 147."""

OVERLAP = [("bt", "band"), ("line", "band"), ("missed", "band"), ("muted", "band"), ("muted", "slow"), ("muted", "fast")]
"""전경이 실제로 면 위에 놓이는 짝(A 띠 · B 면 · E 띠)."""

SGR = re.compile(r"\x1b\[([0-9;]*)m")


def _rgb(n: int) -> tuple[int, int, int]:
    """xterm 256색 번호 → RGB(16~231 큐브, 232~255 회색)."""
    if n >= 232:
        v = 8 + 10 * (n - 232)
        return v, v, v
    n -= 16
    lv = [0, 95, 135, 175, 215, 255]
    return lv[n // 36], lv[n // 6 % 6], lv[n % 6]


def _lum(c: tuple[int, int, int]) -> float:
    """WCAG 상대 휘도."""
    f = lambda x: x / 255 / 12.92 if x / 255 <= 0.04045 else ((x / 255 + 0.055) / 1.055) ** 2.4
    return 0.2126 * f(c[0]) + 0.7152 * f(c[1]) + 0.0722 * f(c[2])


def _ratio(a: tuple[int, int, int], b: tuple[int, int, int]) -> float:
    """WCAG 대비 비, 소수 셋째 자리에서 버림."""
    hi, lo = sorted((_lum(a), _lum(b)), reverse=True)
    return int((hi + 0.05) / (lo + 0.05) * 1000) / 1000


def _fg(role: str) -> tuple[int, int, int]:
    return _rgb(int(PALETTE[FG[role]]))


def _bg(role: str) -> tuple[int, int, int]:
    return _rgb(int(PALETTE[BG[role]]))


def test_dark_contrast_pairs():
    """어두운 바탕에서 면 · 선 · 점이 3:1 이상으로 보인다."""
    pairs = [(f"면 {r} 대 바탕", _bg(r), DARK_BG) for r in BG]
    pairs += [(f"{f} 위 {b}", _fg(f), _bg(b)) for f, b in OVERLAP]
    pairs += [(f"기본 전경 대 면 {r}", DARK_FG, _bg(r)) for r in BG]
    pairs += [(f"색 기호 {r} 대 바탕", _fg(r), DARK_BG) for r in FG]
    low = [(name, _ratio(a, b)) for name, a, b in pairs if _ratio(a, b) < 3.0]
    assert not low, f"3.0 미달: {low} (팔레트 {PALETTE})"


def _nearest(token: tuple[int, int, int]) -> int:
    """디자인 토큰에 RGB 거리가 가장 가까운 256색 번호(16~255)."""
    return min(range(16, 256), key=lambda n: sum((x - y) ** 2 for x, y in zip(_rgb(n), token)))


def test_palette_is_nearest_or_explained():
    """blue · orange · gray는 디자인 토큰에 가장 가까운 번호이거나, 까닭을 적어 둔 대체 번호다."""
    for key, token in TOKENS.items():
        assert PALETTE[key] in (str(_nearest(token)), EXPLAINED.get(key)), f"{key}: 가장 가까운 {_nearest(token)}, 고른 {PALETTE[key]}"
    assert _ratio(_rgb(_nearest(TOKENS["blue"])), _bg("band")) < 3.0  # 대체한 까닭이 그대로인지


def test_no_red_or_green():
    """빨강 · 초록 계열 0: 채도 0.15 미만 회색은 건너뛴다."""
    for key, n in PALETTE.items():
        h, s, _ = colorsys.rgb_to_hsv(*(x / 255 for x in _rgb(int(n))))
        deg = h * 360
        if s >= 0.15:
            assert not (deg <= 20 or deg >= 340 or 90 <= deg <= 150), f"{key}={n} 색상각 {deg:.0f}"


def _colored_text(out: str) -> str:
    """SGR에 `38;5;`가 든 구간의 글만 모은다(`0` · `0;...` 은 먼저 초기화)."""
    colored, on = [], False
    for part in re.split(r"(\x1b\[[0-9;]*m)", out):
        m = SGR.fullmatch(part)
        if m:
            on = "38;5;" in m.group(1)
        elif on:
            colored.append(part)
    return "".join(colored)


def _render(tmp_path, monkeypatch, capsys, *args):
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    monkeypatch.setenv("COLUMNS", "120")
    monkeypatch.delenv("NO_COLOR", raising=False)
    assert main(["chart", *args]) == 0
    return capsys.readouterr().out


def test_no_letter_is_colored(tmp_path, monkeypatch, capsys):
    """글자(한글 음절 · ASCII 영숫자)에는 전경색이 없다. D · A · B · C · E 전부."""
    write_facts(tmp_path)
    for args in ((), ("trend_10_50",), ("trend_10_50", "--trades")):
        out = _render(tmp_path, monkeypatch, capsys, *args)
        assert "\x1b[0;" in out
        letters = re.findall(r"[가-힣A-Za-z0-9]", _colored_text(out))
        assert not letters, f"chart {' '.join(args)}: 색이 입혀진 글자 {letters[:10]}"


def test_normal_symbol_is_uncolored(tmp_path, monkeypatch, capsys):
    """D의 정상 줄 `✓`는 기본 전경(앞 SGR에 38;5 없음). 경고 `▣`는 색이 입혀진다."""
    write_facts(tmp_path)
    out = _render(tmp_path, monkeypatch, capsys)
    ok = next(x for x in out.splitlines() if "정상" in x and "✓" in x)
    assert "✓" not in _colored_text(ok)
    warn = next(x for x in out.splitlines() if "경고" in x)
    assert "▣" in _colored_text(warn)

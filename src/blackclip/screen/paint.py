"""터미널 그리기 바탕: 칸 폭 · 색 역할 · 같은 모양 한 이스케이프 · NO_COLOR.

상태 화면과 차트가 모두 여기를 거쳐 그린다. 색은 파랑 · 주황 · 회색 역할뿐이고(반전 기법 없음),
색 값은 PALETTE 한 곳에만 둔다. 글자(한글 · 영문 · 숫자)에는 색을 입히지 않는다.

색 값(웹 디자인 토큰의 근사, 어두운 바탕에서 비글자 3:1). 토큰 → 번호 → 어두운 바탕 `#030304` 대비:
blue(evidence · bt `#93A5FF`) → 147 → 10.208 / orange(warn `#F0B45C`) → 215 → 11.337 / gray(muted `#B4B4BE`) → 250 → 10.855 /
blue_dim(띠) → 25 → 3.196 / gray_dim(느린 면) → 241 → 3.379 / orange_dim(빠른 면) → 94 → 3.599(면 셋은 디자인 토큰에 없는 이 제품 전용 값).
"""
from __future__ import annotations

import os
import shutil
import unicodedata

PALETTE: dict[str, str] = {"blue": "147", "blue_dim": "25", "orange": "215", "orange_dim": "94", "gray": "250",
                           "gray_dim": "241"}
"""256색 번호. 색 값을 바꿀 땐 여기만 바꾼다."""

FG: dict[str, str] = {"evidence": "blue", "bt": "blue", "warn": "orange", "line": "orange", "missed": "orange",
                      "muted": "gray"}
"""전경 역할 → 팔레트 이름. "actual"은 색 없이 굵게(SGR 1)라 여기에 없다."""

BG: dict[str, str] = {"band": "blue_dim", "slow": "gray_dim", "fast": "orange_dim"}
"""배경 역할 → 팔레트 이름."""

BG_GLYPH: dict[str, str] = {"band": "░", "slow": "░", "fast": "█"}
"""색이 꺼졌을 때 배경 역할 칸의 빈 칸을 채우는 글자."""

SYMBOL: dict[str, str] = {"정상": "✓", "경고": "▣", "판단 보류": "⬓", "확인 불가": "⬚", "점검 불가": "⊘"}
"""대표 상태 → 상태 기호."""

_SEPS = ("   ", " · ", " ")
"""wrap이 줄을 나누는 경계, 앞일수록 먼저 쓴다."""


def _w(ch: str) -> int:
    """글자 하나의 칸 수(east_asian_width W · F = 2, 애매(A)는 1, 결합 문자 · 서식 문자는 0).

    Args:
        ch: 글자 하나.

    Returns:
        0, 1 또는 2.
    """
    if unicodedata.category(ch) in ("Mn", "Me", "Cf"):
        return 0
    return 2 if unicodedata.east_asian_width(ch) in ("W", "F") else 1


def cells(text: str) -> int:
    """글이 차지하는 칸 수.

    Args:
        text: 한 줄 글.

    Returns:
        칸 수(넓은 글자 2, 애매한 글자 1).
    """
    return sum(_w(ch) for ch in text)


def fit(text: str, width: int) -> str:
    """폭을 넘으면 `…`로 끝내 자른다.

    Args:
        text: 한 줄 글.
        width: 최대 칸 수.

    Returns:
        칸 수가 width 이하인 글.
    """
    if cells(text) <= width:
        return text
    out, used = "", 0
    for ch in text:
        if used + _w(ch) > width - 1:
            break
        out, used = out + ch, used + _w(ch)
    return out + "…" if width > 0 else ""


def pad(text: str, width: int) -> str:
    """오른쪽을 공백으로 채워 폭을 맞춘다(왼쪽 정렬, 자르지 않음).

    Args:
        text: 한 줄 글.
        width: 칸 수.

    Returns:
        글 + 공백.
    """
    return text + " " * (width - cells(text))


def rpad(text: str, width: int) -> str:
    """왼쪽을 공백으로 채워 폭을 맞춘다(오른쪽 정렬, 자르지 않음).

    Args:
        text: 한 줄 글.
        width: 칸 수.

    Returns:
        공백 + 글.
    """
    return " " * (width - cells(text)) + text


def _cut(text: str, sep: str, avail: int, lo: int) -> int:
    """sep 경계 중 앞 글이 avail칸에 들어가는 가장 오른쪽 자리.

    Args:
        text: 나눌 글.
        sep: 경계 글.
        avail: 한 줄에 쓸 수 있는 칸 수.
        lo: 경계로 볼 수 있는 가장 앞 위치(앞 공백만 있는 빈 줄이 안 나오게).

    Returns:
        경계의 시작 위치. 없으면 -1.
    """
    i = text.rfind(sep, lo)
    while i >= 0:
        if cells(text[:i]) <= avail:
            return i
        i = text.rfind(sep, lo, i)
    return -1


def _split(text: str, avail: int) -> tuple[str, str]:
    """글을 첫 줄 하나와 남는 글로 가른다(남는 글은 항상 text보다 짧다).

    Args:
        text: avail칸을 넘는 글.
        avail: 첫 줄에 쓸 수 있는 칸 수(1 이상).

    Returns:
        (첫 줄, 남는 글). 남는 글이 없으면 빈 글.
    """
    lo = len(text) - len(text.lstrip()) + 1  # 앞 공백은 경계가 아니라 첫 덩이의 일부
    for sep in _SEPS:
        i = _cut(text, sep, avail, lo)
        if i >= 0:
            return text[:i].rstrip(), text[i + len(sep):].lstrip()
    # 앞 글이 들어가는 경계가 없다: 첫 덩이가 avail보다 길거나 덩이가 하나뿐이다.
    firsts = [(i, sep) for sep in _SEPS if (i := text.find(sep, lo)) >= 0]
    if not firsts:
        return fit(text, avail), ""
    i, sep = min(firsts, key=lambda f: f[0])
    return fit(text[:i], avail), text[i + len(sep):].lstrip()


def wrap(text: str, width: int, indent: int = 2) -> list[str]:
    """폭을 넘는 글을 경계에서 나눈다(모든 줄 width 이하).

    경계는 세 칸 공백, ` · `, 한 칸 공백 순이고 앞 글이 들어가는 가장 오른쪽 자리에서 나눈다.
    경계 글자는 줄에 남기지 않고, 이어지는 줄은 앞에 indent칸 공백을 둔다(indent는 width - 1까지만,
    이어지는 줄에 글자가 늘 1칸 이상 들어가도록). 폭보다 긴 덩이는 그 덩이만 fit으로 자르고 뒤 덩이는
    다음 줄에 이어서 놓는다.

    Args:
        text: 한 줄 글.
        width: 한 줄 최대 칸 수.
        indent: 이어지는 줄의 들여쓰기 칸 수.

    Returns:
        줄 목록. width가 0 이하이면 빈 목록.
    """
    if width <= 0:
        return []
    indent = min(indent, width - 1)
    parts, avail = [], width
    while cells(text) > avail:
        head, text = _split(text, avail)
        parts.append(head)
        avail = width - indent
    if text or not parts:
        parts.append(text)
    return parts[:1] + [" " * indent + p for p in parts[1:]]


def color_on() -> bool:
    """색을 쓸지(no-color.org: NO_COLOR가 있고 비어 있지 않으면 끔; 다른 환경 변수는 보지 않는다).

    Returns:
        색을 써도 되면 True.
    """
    return not os.environ.get("NO_COLOR")


def width() -> int:
    """터미널 가로 칸 수(알 수 없으면 120).

    Returns:
        칸 수.
    """
    return shutil.get_terminal_size((120, 24)).columns


def body_width(cap: int = 72) -> int:
    """본문 폭: 터미널보다 넓게 잡지 않는다.

    Args:
        cap: 상한 칸 수.

    Returns:
        min(cap, 터미널 폭).
    """
    return min(cap, width())


def _sgr(fg: str | None, bg: str | None) -> str:
    """모양 (전경 역할, 배경 역할)의 SGR 조각.

    Args:
        fg: 전경 역할. "actual"이면 굵게.
        bg: 배경 역할.

    Returns:
        `;`로 이은 조각(예: `38;5;147;48;5;25`).
    """
    parts = []
    if fg == "actual":
        parts.append("1")
    elif fg:
        parts.append(f"38;5;{PALETTE[FG[fg]]}")
    if bg:
        parts.append(f"48;5;{PALETTE[BG[bg]]}")
    return ";".join(parts)


def paint(spans: list[tuple[str, str | None, str | None]], color: bool) -> str:
    """(글, 전경 역할, 배경 역할) 조각을 한 줄로 잇는다.

    모양이 앞 조각과 같으면 이스케이프를 더하지 않는다. 색이 꺼지면 이스케이프 없이
    배경 역할 조각의 빈 칸을 BG_GLYPH로 채운다.

    Args:
        spans: (글, 전경 역할, 배경 역할) 목록.
        color: 색을 쓸지(color_on()).

    Returns:
        한 줄.
    """
    out, cur = [], (None, None)
    for text, fg, bg in spans:
        if not text:
            continue
        if not color:
            out.append(text.replace(" ", BG_GLYPH[bg]) if bg else text)
            continue
        if (fg, bg) != cur:
            cur = (fg, bg)
            out.append(f"\x1b[0;{_sgr(fg, bg)}m" if fg or bg else "\x1b[0m")
        out.append(text)
    if cur != (None, None):
        out.append("\x1b[0m")
    return "".join(out)


class Canvas:
    """w칸 x h줄 격자: 칸마다 글자 · 전경 역할 · 배경 역할을 두고 rows로 한 번에 그린다."""

    def __init__(self, w: int, h: int) -> None:
        """빈 격자를 만든다.

        Args:
            w: 가로 칸 수.
            h: 세로 줄 수.
        """
        self.w, self.h = w, h
        self.grid = [[[" ", None, None] for _ in range(w)] for _ in range(h)]

    def _free(self, x: int, y: int) -> None:
        """(x, y)가 넓은 글자의 반쪽이면 그 글자의 두 칸을 모두 비운다(덮어쓰기 전에 부른다).

        Args:
            x: 칸 번호(범위 안).
            y: 줄 번호(범위 안).
        """
        row = self.grid[y]
        if row[x][0] == "":
            x -= 1
        if cells(row[x][0]) == 2:
            row[x][:2] = row[x + 1][:2] = [" ", None]

    def put(self, x: int, y: int, ch: str, fg: str | None = None) -> None:
        """한 칸에 글자를 둔다(범위 밖은 무시). 넓은 글자의 반쪽을 덮으면 그 글자의 두 칸이 비워진다.

        Args:
            x: 칸 번호.
            y: 줄 번호.
            ch: 한 칸 글자(넓은 글자는 text로).
            fg: 전경 역할.
        """
        if 0 <= x < self.w and 0 <= y < self.h:
            self._free(x, y)
            self.grid[y][x][:2] = [ch, fg]

    def bg(self, x: int, y: int, role: str) -> None:
        """한 칸의 배경 역할을 정한다(범위 밖은 무시).

        Args:
            x: 칸 번호.
            y: 줄 번호.
            role: 배경 역할(BG의 키).
        """
        if 0 <= x < self.w and 0 <= y < self.h:
            self.grid[y][x][2] = role

    def text(self, x: int, y: int, s: str, fg: str | None = None) -> None:
        """글을 둔다. 넓은 글자는 두 칸을 차지하고 다음 칸은 빈 자리로 남긴다.

        칸에 다 들어가지 않는 글자는 그리지 않고, 결합 문자는 직전에 그린 글자에 붙인다
        (붙일 글자가 없으면 버림). 서식 문자(Cf)는 버린다.

        Args:
            x: 시작 칸.
            y: 줄 번호.
            s: 글.
            fg: 전경 역할.
        """
        last = None
        for ch in s:
            n = _w(ch)
            if n == 0:
                if last is not None and unicodedata.category(ch) != "Cf":
                    last[0] += ch
                continue
            if 0 <= y < self.h and 0 <= x and x + n <= self.w:
                self.put(x, y, ch, fg)
                if n == 2:
                    self.put(x + 1, y, "", fg)
                last = self.grid[y][x]
            else:
                last = None
            x += n

    def rows(self, color: bool) -> list[str]:
        """격자를 줄 목록으로 그린다(끝의 모양 없는 공백은 뗀다).

        Args:
            color: 색을 쓸지(color_on()).

        Returns:
            줄마다 한 문자열.
        """
        out = []
        for row in self.grid:
            spans = [tuple(c) for c in row]
            while spans and spans[-1] == (" ", None, None):
                spans.pop()
            out.append(paint(spans, color))
        return out

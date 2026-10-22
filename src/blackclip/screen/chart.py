"""blackclip chart [전략]: 매일 대조 기록(days.jsonl)을 터미널 그림으로 그린다.
전략 없이 = D 스파크라인 목록, 전략을 주면 A 범위 차트 · B 차이 면적 · C 게이지 한 줄.

status와 달리 색과 그림 글자를 쓴다. 숫자는 daily가 저장한 것을 그대로 옮기고 여기서 판정을 새로 하지 않는다.
레포에서 온 글(전략 이름 · rep · title · pos)은 필드마다 `status.plain`을 거친 뒤 엔진 기호와 합친다: ESC와 제어 문자는 물론
인쇄 가능한 그림 글자(█ ━ ◆ 등)도 평문 `\\uXXXX`로 바뀌어, 레포 글이 차트 글자를 흉내 내지 못한다(그림 글자는 엔진이 그린 것만 나간다).
완성된 줄 전체에 plain을 걸면 게이지까지 지워지므로 그렇게 하지 않는다.
어떤 줄도 터미널 폭을 넘기지 않는다: 그림을 잘라 맞추지 않고, 라벨은 그림 밑으로, 폭이 모자라면 그림 대신 숫자로 접는다.
"""
from __future__ import annotations

import math
from datetime import date, timedelta
from pathlib import Path

from blackclip.daily.days import TRADE_KEEP, read_days
from blackclip.hook import _NAME
from blackclip.record import BROKEN, stopped
from blackclip.screen.data import (
    E_CAP,
    _split,
    _utc,
    a_marks,
    b_series,
    d_values,
    e_series,
    last_day,
    period_cuts,
    spark,
)
from blackclip.screen.paint import SYMBOL, Canvas, cells, fit, pad, paint, rpad, wrap
from blackclip.screen.paint import width as term_width
from blackclip.screen.status import (
    PARTIAL,
    _md,
    _pct,
    _pp,
    _rank,
    _why,
    plain,
    strategies,
)
from blackclip.states import base
from blackclip.workspace import WorkspaceError, exists_regular, read_guardrail

OFF_LINE = "끈 전략 {n}개 · 자세히는 blackclip status"
"""전체 보기에서 목록에 못 넣은 끈 전략(중지 · 은퇴 · 기록 손상)이 있을 때 끝에 붙는 한 줄."""

NO_LIVE = "실전 전략 없음 · blackclip save <전략> 뒤 blackclip live <전략>"
"""가드레일이 있는 전략이 하나도 없을 때의 한 줄."""

GAUGE_CELLS = 30
"""C 게이지 안쪽 칸 수."""

GAUGE_W = GAUGE_CELLS + 2
"""C 게이지 글자(▕ + 안쪽 + ▏)가 차지하는 칸 수."""

MARGIN = 2
"""C 줄 앞 들여쓰기. 터미널이 MARGIN + GAUGE_W보다 좁으면 게이지를 그리지 않는다."""

A_ROWS = 16
"""A 범위 차트의 그림 줄 수."""

B_ROWS = 8
"""B 차이 면적의 그림 줄 수(0%p 줄 포함)."""

BODY_CAP = 72
"""A · B 본문(눈금 + 그림)의 최대 칸 수."""

LEFT = 6
"""그림 앞 칸 수: y 눈금 글 4칸 + 공백 + 축 글자 1칸."""

MIN_PLOT = 24
"""그림 폭이 이보다 좁으면 그림을 그리지 않고 숫자만 보인다(그림을 잘라 맞추지 않음)."""

E_ROWS = 16
"""E 거래 단위 차트의 그림 줄 수."""

HEAD_A = "━━ A. 범위 차트  지금 괜찮나 · 언제부터 벗어났나"
HEAD_B = "━━ B. 차이 차트  실제 − 같은 기간 BT(비용 0) · 어디서 얼마나 샜나"
HEAD_E = "━━ E. 거래 단위  전략이 망가졌나 · 날짜가 아니라 거래 수로"

_BARS = "▁▂▃▄▅▆"
_ROLES = {"┃": "warn", "●": "warn", "◆": "bt", "┣": "evidence", "━": "evidence", "┫": "evidence"}
"""C 게이지 글자 → 색 역할(게이지 줄과 범례가 같은 색을 쓴다)."""
_A_ROLES = {"▇": "evidence", "▁": "evidence", "━": "actual", "╌": "bt", "┄": "line", "●": "missed"}
"""A 라벨 · 범례 글자 → 색 역할. 레포 글은 plain을 거쳐 이 글자가 없으니 글자만 보고 입혀도 속지 않는다."""

_E_ROLES = {"●": "muted", "━": "actual", "╌": "bt", "▇": "evidence", "▁": "evidence", "┆": "muted"}
"""E 라벨 · 범례 글자 → 색 역할(A와 같은 까닭으로 글자만 보고 입힘)."""

_Spans = list[tuple[str, str | None, str | None]]


def _fit_spans(spans: _Spans, w: int) -> _Spans:
    """조각들의 글이 폭을 넘으면 `fit`과 같은 자리에서 자르고 `…`를 붙인다(색 조각은 그대로 앞부분만 남음).

    Args:
        spans: (글, 전경 역할, 배경 역할) 목록.
        w: 최대 칸 수.

    Returns:
        칸 수가 w 이하인 조각들.
    """
    full = "".join(t for t, _, _ in spans)
    cut = fit(full, w)
    if cut == full:
        return spans
    keep, out = max(len(cut) - 1, 0), []
    for t, fg, bg in spans:
        out.append((t[:keep], fg, bg))
        keep = max(keep - len(t), 0)
    return out + [("…", None, None)]


def _tint(text: str, roles: dict[str, str] = _ROLES) -> _Spans:
    """글자마다 roles 색을 입힌 조각들(같은 색은 paint가 한 이스케이프로 묶는다).

    Args:
        text: 엔진이 만든 글(레포 글은 plain을 거친 것만).
        roles: 글자 → 색 역할(기본 C 게이지).

    Returns:
        글자당 조각 하나.
    """
    return [(ch, roles.get(ch), None) for ch in text]


def _state(rec: dict) -> _Spans:
    """마지막 기록의 상태 칸 조각들: `기호 대표상태`(경고면 ` · 원인`). 색은 기호 한 글자에만.

    기호 역할: 정상 = 없음, 경고 = warn, 그 밖 = muted. 뒤 공백 · 낱말 · 원인은 역할 없음.

    Args:
        rec: 기록 한 줄.

    Returns:
        (글, 전경 역할, None) 조각 둘. 레포 글(rep · title)은 plain을 거침.
    """
    b = base(rec["rep"])
    rest = f" {plain(rec['rep'])}" + (f" · {plain(rec['title'])}" if b == "경고" else "")
    return [(SYMBOL.get(b, "?"), "warn" if b == "경고" else None if b == "정상" else "muted", None), (rest, None, None)]


def _spark_spans(marks: list[dict]) -> _Spans:
    """data.spark 칸들을 그린다: 값이 없는 칸은 `·`(muted), 있는 칸은 눈금 글자(경고인 날은 warn).

    Args:
        marks: data.spark 결과.

    Returns:
        (글자, 색 역할, None) 조각들.
    """
    return [("·", "muted", None) if c["level"] is None else (_BARS[c["level"]], "warn" if c["warn"] else None, None) for c in marks]


def chart_d(entries: list[tuple[str, list[dict]]], color: bool, width: int) -> list[str]:
    """D 스파크라인 목록: 전략마다 한 줄(스파크라인 · 손익 · 실제-BT · 상태), 급한 상태가 먼저.

    Args:
        entries: (전략, 그 전략의 기록 전체 — 실전 시작부터, 날짜순) 목록. 기록이 있는 전략만이고 하나 이상이어야 한다.
        color: 색을 쓸지(paint.color_on()).
        width: 터미널 가로 칸 수.

    Returns:
        줄 목록(표 줄은 width에서 `…`로 잘리고, 머리 · 범례는 줄바꿈).
    """
    entries = sorted(entries, key=lambda e: (_rank(e[1]), e[0]))
    first, last = min(r[0]["day"] for _, r in entries), last_day(r for _, r in entries)
    nw = max(14, max(cells(plain(n)) for n, _ in entries) + 2)  # 이름이 길면 열을 넓힌다
    rows = [[("  " + pad("전략", nw) + pad(f"{_md(first)} ~ {_md(last)}", 24) + rpad("손익", 6) + "  " + pad("실제-BT", 10) + "상태",
              None, None)]]
    for name, recs in entries:
        dots, v = spark(recs, date.fromisoformat(last)), d_values(recs)
        pnl = _pct(v["pnl"])
        gap = f"{_md(v['until'])}까지" if v["until"] else _pp(v["gap"])
        rows.append([("  " + pad(plain(name), nw), None, None), *_spark_spans(dots), (" " * (24 - len(dots)), None, None),
                     (rpad(pnl, 6) + "  " + pad(gap, 10), None, None), *_state(recs[-1])])
    legend = ("  줄마다 자기 눈금(모양만 본다, 크기는 옆 숫자) · 점(·)은 매일 대조 기록이 없거나 실제 손익을 모르는 날 · 실전 시작부터")
    return [*wrap(f"━━ D. 스파크라인 목록  실전 {len(entries)}개 한눈에", width), "",
            *(paint(_fit_spans(r, width), color) for r in rows), "", *wrap(legend, width)]


def _pos(v: float | None, lo_v: float, hi_v: float) -> int | None:
    """값이 게이지 안쪽 몇 번째 칸인지(0 ~ GAUGE_CELLS - 1).

    Args:
        v: 값(비율), 없으면 None.
        lo_v: 게이지 왼쪽 끝 값.
        hi_v: 게이지 오른쪽 끝 값.

    Returns:
        칸 번호. v가 None이면 None.
    """
    if v is None:
        return None
    return min(GAUGE_CELLS - 1, max(0, round((v - lo_v) / ((hi_v - lo_v) or 1.0) * (GAUGE_CELLS - 1))))


def chart_c(name: str, recs: list[dict], color: bool, width: int | None = None) -> list[str]:
    """C 게이지 한 줄: 이번 구간의 경고선 ┃ · 실제 ● · 그날 정상 범위 ┣━┫ · 같은 기간 BT ◆.

    값은 마지막 기록의 구간 값(`actual` · `bt` · `lo` · `hi` · `line` · `room`)이다. 마지막 기록이 대조 실패(nums 비어 있음)라
    네 값이 모두 없으면 옛 숫자를 지금처럼 그리지 않고 그 사실을 한 줄로 말한다.

    Args:
        name: 전략 이름.
        recs: 이 전략의 기록(날짜순, 비어 있지 않음). 마지막 줄만 쓴다.
        color: 색을 쓸지.
        width: 터미널 가로 칸 수(None이면 지금 터미널).

    Returns:
        줄 목록(꼬리 · 범례는 폭에서 줄바꿈, 게이지 글자는 한 줄).
    """
    width = term_width() if width is None else width
    last = recs[-1]
    n = last["nums"]
    actual, bt, lo, hi, line, room = (n.get(k) for k in ("actual", "bt", "lo", "hi", "line", "room"))
    nm, rep_ = plain(name), plain(last["rep"])
    if all(v is None for v in (actual, bt, lo, line)):
        return wrap(f"{nm} · 이번 구간 숫자 없음(마지막 매일 대조: {rep_})", width)
    band = lo is not None and hi is not None
    lo_v = min(v for v in (line, actual, bt, lo) if v is not None)
    hi_v = max((v for v in (hi, actual, bt) if v is not None), default=lo_v)
    p_line, p_bt, p_actual, p_lo, p_hi = (_pos(v, lo_v, hi_v) for v in (line, bt, actual, lo, hi))
    cell = ["─"] * GAUGE_CELLS
    if band:
        cell[p_lo:p_hi + 1] = "━" * (p_hi - p_lo + 1)
        cell[p_lo], cell[p_hi] = "┣", "┫"
    for p, ch in ((p_line, "┃"), (p_bt, "◆"), (p_actual, "●")):  # 뒤에 쓴 것이 위에 덮인다
        if p is not None:
            cell[p] = ch
    where = "정상 범위 그리지 않음" if not band else f"범위 {plain(n['pos'])}" if n.get("pos") else "범위 모름"
    ahead = "경고선까지 모름" if room is None else f"경고선까지 {room * 100:.1f}%p"
    tail = f"{where} · {ahead} · {rep_}" + (f" · {plain(last['title'])}" if base(last["rep"]) == "경고" else "")
    band_txt = ("그리지 않음" if not band else f"{lo:+.1%} ~ {hi:+.1%}") + (f"({n['d']}일째)" if band and n.get("d") is not None else "")
    title = wrap("━━ C. 게이지 한 줄  지금 어디쯤 · 이번 구간 기준", width)
    if width < MARGIN + GAUGE_W:  # 게이지가 들어가지 않는 폭: 자르지 않고 숫자 · 설명으로 접는다
        return [*title, "", *wrap(f"{nm} · {tail} · 경고선 {_pct(line)} · 실제 {_pct(actual)} · 정상 범위 {band_txt} · "
                                  f"같은 기간 BT {_pct(bt)}", width)]
    gauge = [("▕", None, None), *_tint("".join(cell)), ("▏", None, None)]
    prefix = f"{' ' * MARGIN}{nm} "
    if cells(prefix) + GAUGE_W <= width:  # 이름 · 게이지가 한 줄에 들어감
        lead, first = [], [(prefix, None, None), *gauge]
    else:  # 이름은 따로 한 줄(이름이 폭보다 길면 …로 자름), 게이지는 다음 줄
        lead, first = [fit(' ' * MARGIN + nm, width)], [(' ' * MARGIN, None, None), *gauge]
    room_left = width - sum(cells(t) for t, _, _ in first)
    if cells(tail) + 1 <= room_left:  # 꼬리가 통째로 들어갈 때만 게이지 옆, 아니면 다음 줄들
        first, rest = [*first, (" " + tail, None, None)], []
    else:
        rest = wrap(' ' * MARGIN + tail, width)
    legend = f"  ┃ 경고선 {_pct(line)}  ● 실제 {_pct(actual)}  ┣━┫ 정상 범위 {band_txt}  ◆ 같은 기간 BT {_pct(bt)}"
    return [*title, "", *lead, paint(first, color), *rest, *(paint(_tint(x), color) for x in wrap(legend, width))]


_Label = tuple[str | None, str | None, str, int | None]
"""라벨 하나: (글자, 색 역할, 글, 오른쪽에 둘 때의 줄 번호 — 모르면 None)."""


def _cols(n: int, p: int) -> list[int]:
    """그림 칸마다 그 칸이 그리는 날 번호(0부터).

    날이 칸보다 적으면 `x * n // p`번째 날이 여러 칸을 차지하고, 많으면 칸마다 그 묶음의 마지막 날을 그린다.

    Args:
        n: 그릴 날수(첫 기록 날부터 마지막 날까지).
        p: 그림 폭(칸).

    Returns:
        길이 p의 날 번호 목록.
    """
    if n <= p:
        return [x * n // p for x in range(p)]
    return [((x + 1) * n + p - 1) // p - 1 for x in range(p)]


def _col(i: int, n: int, p: int) -> int:
    """날 i를 그리는 첫 칸(`_cols`의 거꾸로).

    Args:
        i: 날 번호(0부터).
        n: 그릴 날수.
        p: 그림 폭(칸).

    Returns:
        칸 번호.
    """
    return (i * p + n - 1) // n if n <= p else i * p // n


def _view(recs: list[dict], p: int) -> tuple[date, int, list[dict | None], set[int]]:
    """그림 칸 배치: 첫 날 · 날수 · 칸마다의 그날 기록(없으면 None) · 구간이 바뀌는 칸.

    기록이 없는 날과 대조 실패한 날(period_start 없음)은 앞 구간을 이어받아 구간 경계로 세지 않는다.

    Args:
        recs: 이 전략의 기록 전체(날짜순, 비어 있지 않음).
        p: 그림 폭(칸).

    Returns:
        (첫 날, 날수, 칸마다의 기록, 새 구간이 처음 보이는 칸 번호 집합).
    """
    first = date.fromisoformat(recs[0]["day"])
    n = (date.fromisoformat(recs[-1]["day"]) - first).days + 1
    by_day = {r["day"]: r for r in recs}
    col_rec = [by_day.get((first + timedelta(days=i)).isoformat()) for i in _cols(n, p)]
    return first, n, col_rec, set(period_cuts(col_rec))


def _scale(lo: float, hi: float, rows: int) -> tuple[float, float]:
    """세로 범위를 위아래 5% 넓혀 (맨 위 값, 한 줄 높이)로.

    Args:
        lo: 아래 끝 값.
        hi: 위 끝 값.
        rows: 그림 줄 수.

    Returns:
        (top, h). 줄 r(위 0)의 아래 끝은 `top - (r + 1) * h`.
    """
    if hi - lo < 1e-9:  # 한 점뿐이면 눈금이 무너지므로 ±0.5%p를 둔다
        lo, hi = lo - 0.005, hi + 0.005
    margin = (hi - lo) * 0.05
    return hi + margin, (hi - lo + 2 * margin) / rows


def _row(v: float, top: float, h: float, rows: int) -> int:
    """값 v가 있는 줄(위 0, 그림 밖이면 가장자리 줄).

    Args:
        v: 값.
        top: 맨 위 값.
        h: 한 줄 높이.
        rows: 그림 줄 수.

    Returns:
        줄 번호.
    """
    return min(rows - 1, max(0, math.floor((top - v) / h)))


def _ticks(top: float, h: float, rows: int, unit: str) -> dict[int, str]:
    """y 눈금: 범위 ÷ 간격이 5 이하인 가장 작은 간격(1 · 2 · 4 · 5 × 10의 거듭제곱)의 배수가 걸치는 줄 → 눈금 글.

    Args:
        top: 맨 위 값.
        h: 한 줄 높이.
        rows: 그림 줄 수.
        unit: 눈금 글 끝(`%` 또는 `%p`).

    Returns:
        줄 번호 → 글(4칸 이하인 것만, 0은 `0%`).
    """
    span = h * rows
    step = next(m * 10.0 ** e for e in range(-2, 9) for m in (1, 2, 4, 5) if span / (m * 10.0 ** e) <= 5)
    out, k = {}, math.ceil((top - span) / step - 1e-9)
    while k * step <= top + step * 1e-9:
        text = "0" + unit if k == 0 else f"{k * step * 100:+.0f}{unit}"
        y = _row(k * step, top, h, rows)
        if cells(text) <= 4 and y not in out:
            out[y] = text
        k += 1
    return out


def _frame(cv: Canvas, rows: int, ticks: dict[int, str], zero: int | None = None) -> None:
    """y 축: 눈금 줄은 `글 ┤`(`zero` 줄은 `┼`), 나머지 줄은 `│`.

    Args:
        cv: 그릴 격자.
        rows: 그림 줄 수.
        ticks: `_ticks` 결과.
        zero: B의 0%p 줄 번호(A는 None).
    """
    for y in range(rows):
        if y in ticks:
            cv.text(0, y, f"{ticks[y]:>4} " + ("┼" if y == zero else "┤"))
        else:
            cv.text(LEFT - 1, y, "│")


def _x_axis(cv: Canvas, y: int, first: date, n: int, p: int) -> None:
    """x 축 한 줄과 그 밑 날짜 한 줄: 7일마다(0 · 7 · 14 …) `┬`와 `{월}/{일}`, 마지막 날 글은 그림 안에 다 들어가게 당겨 붙인다.

    눈금 글이 앞 글과 겹치면 건너뛰고, 마지막 날 글은 겹치는 앞 글을 밀어낸다.

    Args:
        cv: 그릴 격자.
        y: 축 줄 번호(날짜 줄은 그 다음).
        first: 첫 날.
        n: 날수.
        p: 그림 폭(칸).
    """
    cv.text(0, y, "     └" + "─" * p)
    for i in range(0, n, 7):
        cv.put(LEFT + _col(i, n, p), y, "┬")
    shown: list[tuple[int, str]] = []
    for i in range(0, n - 1, 7):
        x, text = _col(i, n, p), _md((first + timedelta(days=i)).isoformat())
        if x + len(text) <= p and (not shown or x > shown[-1][0] + len(shown[-1][1])):
            shown.append((x, text))
    text = _md((first + timedelta(days=n - 1)).isoformat())
    x = min(_col(n - 1, n, p), p - len(text)) if (n - 1) % 7 == 0 else p - len(text)
    for x0, text0 in [*(s for s in shown if s[0] + len(s[1]) < x), (x, text)]:
        cv.text(LEFT + x0, y + 1, text0)


def _full(lab: _Label) -> str:
    """라벨 한 줄 글: `글자 글`(글자가 없으면 글만).

    Args:
        lab: 라벨.

    Returns:
        글.
    """
    return f"{lab[0]} {lab[2]}" if lab[0] else lab[2]


def _lw(labels: list[_Label]) -> int:
    """가장 넓은 라벨의 칸 수.

    Args:
        labels: 라벨 목록.

    Returns:
        칸 수.
    """
    return max(cells(_full(lab)) for lab in labels)


def _beside(labels: list[_Label], limit: int) -> list[int] | None:
    """라벨을 그림 오른쪽 줄에 둘 줄 번호: 각 라벨은 자기 값의 줄에 놓고, 겹칠 때만 위에서부터 한 줄씩 아래로 민다.

    줄이 같으면 목록 순서(골든 순서)가 앞선 것이 위다.

    Args:
        labels: 라벨 목록.
        limit: 둘 수 있는 맨 아래 줄 번호.

    Returns:
        라벨마다 줄 번호(목록 순서). 줄 번호를 모르는 라벨이 있거나 자리가 모자라면 None(그림 밑에 둠).
    """
    if any(lab[3] is None for lab in labels):
        return None
    out, prev = [0] * len(labels), -1
    for i in sorted(range(len(labels)), key=lambda i: (labels[i][3], i)):
        prev = out[i] = max(labels[i][3], prev + 1)
    return out if prev <= limit else None


def _below(labels: list[_Label], width: int, color: bool, roles: dict[str, str]) -> list[str]:
    """라벨을 그림 밑에 한 줄씩(폭을 넘으면 접음). 글자에만 색 역할을 입힌다.

    Args:
        labels: 라벨 목록.
        width: 터미널 가로 칸 수.
        color: 색을 쓸지.
        roles: 글자 → 색 역할.

    Returns:
        줄 목록.
    """
    return [paint(_tint(line, roles), color) for lab in labels for line in wrap("  " + _full(lab), width)]


def _place(cv: Canvas, x: int, y: int, lab: _Label) -> None:
    """라벨을 (x, y)부터 둔다(글자에만 색 역할).

    Args:
        cv: 그릴 격자.
        x: 시작 칸.
        y: 줄 번호.
        lab: 라벨.
    """
    if lab[0]:
        cv.text(x, y, lab[0], lab[1])
        x += 2
    cv.text(x, y, lab[2])


def _band(cv: Canvas, x: int, a: float, b: float) -> None:
    """그날 정상 범위 띠 한 칸 세로줄: 안은 배경 `band`, 위 가장자리는 덮은 비율 k/8의 `▁~▇`(전경), 아래 가장자리는 반 이상 덮으면 배경.

    Args:
        cv: 그릴 격자.
        x: 칸 번호(격자 기준).
        a: 띠 위 끝의 줄 좌표(위 0, 줄 r은 [r, r + 1)).
        b: 띠 아래 끝의 줄 좌표.
    """
    a, b = max(a, 0.0), min(b, float(A_ROWS))
    if b <= a:
        return
    t, u = int(a), min(int(b), A_ROWS - 1)
    k = round(((t + 1 if t < u else b) - a) * 8)  # 위 가장자리 칸 안을 덮은 8분의 몇
    if k >= 8:
        cv.bg(x, t, "band")
    elif k > 0:
        cv.put(x, t, "▁▂▃▄▅▆▇"[k - 1], "evidence")
    for y in range(t + 1, u):
        cv.bg(x, y, "band")
    if t < u and b - u >= 0.5:
        cv.bg(x, u, "band")


def _a_figure(cv: Canvas, col_rec: list[dict | None], cuts: set[int], top: float, h: float, missed: set[str],
              first: date, n: int) -> int:
    """A 그림 칸 채우기. 덮는 순서는 ┊ → 띠 → BT ╌ → 경고선 ┄ → 실제 ━(꺾임 ┓┃┗ ┛┃┏) → 놓친 주문 ●.

    Args:
        cv: 그릴 격자.
        col_rec: 칸마다의 그날 기록.
        cuts: 구간이 바뀌는 칸.
        top: 맨 위 값.
        h: 한 줄 높이.
        missed: 놓친 주문이 있는 날(ISO).
        first: 첫 날.
        n: 날수.

    Returns:
        그린 ●의 수.
    """
    p = len(col_rec)
    for x in cuts:
        for y in range(A_ROWS):
            cv.put(LEFT + x, y, "┊", "muted")
    num = [r["nums"] if r is not None else {} for r in col_rec]
    for x, v in enumerate(num):
        if v.get("lo") is not None and v.get("hi") is not None:
            _band(cv, LEFT + x, (top - v["hi"]) / h, (top - v["lo"]) / h)
    for x, v in enumerate(num):
        if v.get("bt") is not None:
            cv.put(LEFT + x, _row(v["bt"], top, h, A_ROWS), "╌", "bt")
    for x, v in enumerate(num):  # 경고선은 날마다 그날 값의 줄에
        if v.get("line") is not None:
            cv.put(LEFT + x, _row(v["line"], top, h, A_ROWS), "┄", "line")
    act = [None if v.get("actual") is None else _row(v["actual"], top, h, A_ROWS) for v in num]
    for x, y in enumerate(act):
        if y is None:
            continue
        prev = act[x - 1] if x > 0 and x not in cuts else None  # 구간 경계와 빈 날은 잇지 않음
        if prev is None or prev == y:
            cv.put(LEFT + x, y, "━", "actual")
            continue
        up = y < prev
        cv.put(LEFT + x, prev, "┛" if up else "┓", "actual")
        for k in range(min(prev, y) + 1, max(prev, y)):
            cv.put(LEFT + x, k, "┃", "actual")
        cv.put(LEFT + x, y, "┏" if up else "┗", "actual")
    placed = 0
    for day in missed:
        i = (date.fromisoformat(day) - first).days
        x = _col(i, n, p) if 0 <= i < n else None
        if x is not None and act[x] is not None:
            cv.put(LEFT + x, act[x], "●", "missed")
            placed += 1
    return placed


def _a_labels(recs: list[dict], band: bool, line: float | None, top: float, h: float) -> list[_Label]:
    """A 라벨: 위에서 아래로 (구간이 둘 이상이면 실전 시작부터 숫자) · 범위 상단 · BT · 범위 하단 · 실제 · 차이 · 경고선.

    숫자는 마지막 기록의 이번 구간 값이다. 값을 모르면 「모름」이고 줄 번호도 모른다(그림 밑에 둠).

    Args:
        recs: 이 전략의 기록 전체.
        band: 정상 범위 띠를 그렸는지.
        line: 그림에 그린 경고선 값(마지막 기록이 대조 실패여도 앞 기록의 선).
        top: 그림 맨 위 값.
        h: 한 줄 높이.

    Returns:
        라벨 목록(레포 글은 plain을 거침).
    """
    last = recs[-1]
    n = last["nums"]
    actual, bt, lo, hi, gap, room, d = (n.get(k) for k in ("actual", "bt", "lo", "hi", "gap", "room", "d"))
    day = f"({d}일째)" if d is not None else ""

    def row(v: float | None) -> int | None:
        return None if v is None else _row(v, top, h, A_ROWS)

    def edge(word: str, glyph: str, v: float | None, month: float | None) -> _Label:
        return glyph, "evidence", f"범위 {word} {_pct(v)}{day}" + (f" · 한 달 {_pct(month)}" if month is not None else ""), row(v)

    def inside(pos: object) -> str:  # 띠를 그렸을 때만 「범위 안 · 아래 · 위」
        return f" · 범위 {plain(str(pos))}" if band and pos else ""

    out: list[_Label] = []
    whole = n.get("all")
    if len({r["nums"].get("period_start") for r in recs} - {None}) > 1 and isinstance(whole, dict):  # 구간이 둘 이상
        out.append((None, None, f"실전 시작부터 실제 {_pct(whole.get('actual'))} · 같은 기간 BT {_pct(whole.get('bt'))}", 0))
    if band:
        out.append(edge("상단", "▇", hi, n.get("month_hi")))
    out.append(("╌", "bt", f"같은 기간 BT {_pct(bt)}" + inside(n.get("bt_pos")), row(bt)))
    if band:
        out.append(edge("하단", "▁", lo, n.get("month_lo")))
    out.append(("━", "actual", f"실제 {_pct(actual)}" + inside(n.get("pos")), row(actual)))
    why = f" · {plain(last['title'])}" if base(last["rep"]) == "경고" else ""
    out.append((None, None, f"차이(실제 − BT 비용 0) {_pp(gap)}{why}", row(actual)))
    left = "남은 폭 모름" if room is None else f"{room * 100:.1f}%p 남음" if room >= 0 else f"{-room * 100:.1f}%p 넘음"
    out.append(("┄", "line", f"경고선 {_pct(line)} · 넘으면 중단 검토({left})", row(line)))
    return out


def _legend_a(g: dict, band: bool, missed: bool, color: bool) -> str:
    """A 범례 한 줄(색이 있으면 띠 글자가 ▇, 없으면 ░ 무늬). 그리지 않은 것은 뺀다.

    Args:
        g: 가드레일 내용(정상 범위 coverage만 봄).
        band: 정상 범위 띠를 그렸는지.
        missed: ●를 그렸는지.
        color: 색을 쓸지.

    Returns:
        범례 글.
    """
    cov = g["normal_range"].get("coverage")
    since = f"한 달 {cov:.0%} · " if isinstance(cov, float) and 0 < cov <= 1 else ""
    parts = [f"{'▇' if color else '░'} 정상 범위({since}그날까지)"] if band else []
    return "   ".join([*parts, "━ 실제", "╌ 같은 기간 BT", "┄ 경고선", *(["● 놓친 주문(추정)"] if missed else [])])


def chart_a(name: str, recs: list[dict], g: dict, color: bool, width: int) -> list[str]:
    """A 범위 차트: 실전 시작부터의 그날 정상 범위 띠 · 실제 ━ · 같은 기간 BT ╌ · 경고선 ┄ · 놓친 주문 ●.

    띠 · 경고선 · 선은 30일 구간 기준이라 31일째부터 구간 경계 ┊에서 다시 시작한다. 라벨은 터미널이 넓으면 그림 오른쪽,
    아니면 그림 밑에 한 줄씩. 그림 폭이 모자라면 그림 없이 숫자만 말한다(그림을 잘라 맞추지 않음).

    Args:
        name: 전략 이름.
        recs: 이 전략의 기록 전체(날짜순, 비어 있지 않음).
        g: 가드레일 내용(read_guardrail이 모양을 검사함).
        color: 색을 쓸지(paint.color_on()).
        width: 터미널 가로 칸 수.

    Returns:
        줄 목록(모든 줄이 width 이하).
    """
    first, last = recs[0], recs[-1]
    nums = [r["nums"] for r in recs]

    def vals(key: str) -> list[float]:
        return [v for n in nums if (v := n.get(key)) is not None]

    head = wrap(HEAD_A, width)
    state = "".join(t for t, _, _ in _state(last))
    info = wrap(f"{plain(name)} · 실전 시작부터 {_md(first['day'])} ~ {_md(last['day'])}({last['live_day']}일째) · "
                f"띠 · 선은 구간(30일) 기준 · {state}", width)
    if not vals("actual") and not vals("bt"):  # 대조 실패 줄뿐
        return [*head, "", *info, "", *wrap("  그릴 숫자 없음(매일 대조를 확인하지 못함)", width)]
    marks = a_marks(recs, g)
    band, line = marks["band"], marks["line"]
    no_band = [] if band else wrap("정상 범위 그리지 않음" + (f"({plain(marks['reason'])})" if marks["reason"] else ""), width)
    high = max([0.0, *vals("hi"), *vals("actual"), *vals("bt")])
    top, h = _scale(min([*vals("lo"), *vals("actual"), *vals("bt"), *vals("line"), high]), high, A_ROWS)
    labels = _a_labels(recs, band, line, top, h)
    bw = min(BODY_CAP, width)
    p = bw - LEFT
    pre = [*head, "", *info, *no_band, ""]
    if p < MIN_PLOT:  # 그림이 들어가지 않는 폭
        return [*pre, *wrap(f"  터미널이 {LEFT + MIN_PLOT}칸보다 좁아 그림은 생략 · 숫자만", width), *_below(labels, width, color, _A_ROLES)]
    side = width >= bw + 2 + _lw(labels)
    at = _beside(labels, A_ROWS + 1) if side else None
    cv = Canvas(bw + 2 + _lw(labels) if at else bw, A_ROWS + 2)
    start, n, col_rec, cuts = _view(recs, p)
    _frame(cv, A_ROWS, _ticks(top, h, A_ROWS, "%"))
    dots = _a_figure(cv, col_rec, cuts, top, h, marks["missed"], start, n)
    _x_axis(cv, A_ROWS, start, n, p)
    for lab, y in zip(labels, at or []):
        _place(cv, bw + 2, y, lab)
    legend = wrap(_legend_a(g, band, dots > 0, color), width)
    return [*pre, *cv.rows(color), "", *([] if at else _below(labels, width, color, _A_ROLES)),
            *(paint(_tint(x, _A_ROLES), color) for x in legend)]


def _stack(cv: Canvas, x: int, parts: list[tuple[str, float]], sign: int, z: int, first: int, top: float,
           h: float) -> dict[str, int]:
    """0%p 줄 z에서 한 방향(sign +1 = 아래, -1 = 위)으로 기여를 차례로 쌓아 배경을 칠한다.

    부분마다 그 끝 값이 있는 줄까지 칠하고, 뒤 부분은 앞 부분의 끝 줄 다음부터다. 뒤 부분이 앞 부분과 같은 줄에서 끝나면 그 줄을 덮는다.

    Args:
        cv: 그릴 격자.
        x: 칸 번호(격자 기준).
        parts: (배경 역할, 부호 있는 크기) 차례대로.
        sign: 방향.
        z: 0%p 줄 번호.
        first: 첫 부분이 시작할, z에서 떨어진 줄 수(0, 반대 방향이 0%p 줄을 가졌으면 1).
        top: 맨 위 값.
        h: 한 줄 높이.

    Returns:
        역할 → 그 부분의 끝 줄 번호.
    """
    cur, total, prev, ends = first, 0.0, None, {}
    for role, v in parts:
        total += v
        end = abs(_row(total, top, h, B_ROWS) - z)  # 0%p 줄에서 몇 줄 떨어졌나
        for k in range(cur if prev is None else min(cur, end), end + 1):
            if 0 <= z + sign * k < B_ROWS:
                cv.bg(x, z + sign * k, role)
        ends[role] = z + sign * end
        cur, prev = end + 1, end
    return ends


def _b_figure(cv: Canvas, col_rec: list[dict | None], cuts: set[int], top: float, h: float, z: int) -> dict[str, int] | None:
    """B 그림 칸 채우기: ┊, 칸마다 음수 기여는 0%p 아래로 · 양수 기여는 위로 slow → fast 순서로 쌓고, 면적이 없는 0%p 줄 칸은 ─.

    Args:
        cv: 그릴 격자.
        col_rec: 칸마다의 그날 기록.
        cuts: 구간이 바뀌는 칸.
        top: 맨 위 값.
        h: 한 줄 높이.
        z: 0%p 줄 번호.

    Returns:
        마지막 칸의 역할 → 끝 줄 번호(마지막 칸에 기여가 하나도 없으면 None).
    """
    for x in cuts:
        for y in range(B_ROWS):
            cv.put(LEFT + x, y, "┊", "muted")
    ends = None
    for x, r in enumerate(col_rec):
        slow, fast = _split(r["nums"]) if r is not None else (None, None)
        got = [(role, v) for role, v in (("slow", slow), ("fast", fast)) if v is not None]
        ends = {role: z for role, _ in got} or None  # 0인 부분은 0%p 줄에서 끝남
        if ends is None:
            continue
        neg, pos = [(a, v) for a, v in got if v < 0], [(a, v) for a, v in got if v > 0]
        down = sum(-v for _, v in neg) > sum(v for _, v in pos)  # 0%p 줄은 큰 쪽이 가진다
        ends.update(_stack(cv, LEFT + x, neg, 1, z, 0 if down or not pos else 1, top, h))
        ends.update(_stack(cv, LEFT + x, pos, -1, z, 0 if not down or not neg else 1, top, h))
    for x in range(len(col_rec)):
        if cv.grid[z][LEFT + x][2] is None and cv.grid[z][LEFT + x][0] == " ":
            cv.put(LEFT + x, z, "─")
    return ends


def _b_labels(lab: dict, ends: dict[str, int] | None) -> list[_Label]:
    """B 라벨: 조금씩(체결가 차이 · 수수료) · 나머지(추정) · 합계. 숫자는 `b_series`의 라벨 값(마지막 기록의 이번 구간 값).

    줄 번호는 조금씩 라벨 = 그 끝 줄, 나머지 라벨 = fast 끝 줄, 합계 = 그 다음 줄.

    Args:
        lab: `b_series(recs)["labels"]`.
        ends: `_b_figure`가 돌려준 마지막 칸의 끝 줄(그림을 그리기 전이거나 마지막 칸에 기여가 없으면 None — 줄 번호를 모름).

    Returns:
        라벨 목록(레포 글은 plain을 거침).
    """
    cause = "원인 후보 없음"
    if lab["missed_n"]:
        cause = f"놓친 주문 {lab['missed_n']}건"
        if (m := lab["first_missed"]) is not None:
            side = {"sell": "매도", "buy": "매수"}.get(m["side"].lower(), plain(m["side"]))
            cause += f" · {'처음 ' if lab['missed_n'] > 1 else ''}{_utc(m['t']):%m/%d %H:%M} UTC {side}"
    rows = (None, None) if ends is None else (ends.get("slow"), ends.get("fast"))
    funding = " · 펀딩비" if lab["funding"] else ""
    upto = f"({_md(lab['upto'])}까지)" if lab["upto"] else ""
    return [("◂", None, f"체결가 차이 · 수수료{funding} {_pp(lab['slow'])}(실전 내내 조금씩 · 잰 것)", rows[0]),
            ("◂", None, f"나머지 {_pp(lab['fast'])}(추정 · {cause})", rows[1]),
            (None, None, f"합계{upto} {_pp(lab['gap'])}", None if rows[1] is None else rows[1] + 1)]


def chart_b(name: str, recs: list[dict], color: bool, width: int) -> list[str]:
    """B 차이 차트: 실제 − 같은 기간 BT(비용 0)가 어디서 얼마나 샜나를 두 면적으로 — 조금씩(체결가 차이 · 수수료, 잰 것)과 한 번에(나머지, 추정).

    날마다 이번 구간 누적을 0%p 줄 위아래로 쌓는다(구간 경계 ┊). 색이 없으면 조금씩은 ░, 한 번에는 █.
    그릴 면적이 없을 때 「차이 0.0%p」는 두 기여를 모두 아는 마지막 날일 때만 날짜 없이 말하고, 모르는 기여가 있으면 두 기여를
    모두 확인한 마지막 날 `(M/D까지)`를 붙여 그 아래 라벨(모르는 쪽은 「모름」)을 보이며, 그런 날이 없으면 「차이 모름」이다.

    Args:
        name: 전략 이름(그림에는 안 쓰고 A · C와 호출 모양만 맞춤).
        recs: 이 전략의 기록 전체(날짜순, 비어 있지 않음).
        color: 색을 쓸지(paint.color_on()).
        width: 터미널 가로 칸 수.

    Returns:
        줄 목록(모든 줄이 width 이하).
    """
    head = wrap(HEAD_B, width)
    series = b_series(recs)
    empty = series["empty"]
    if empty and empty["kind"] == "none":
        return [*head, "", *wrap("  차이를 낸 날 없음(실행 칸을 확인하지 못함)", width)]
    if empty and empty["kind"] == "zero":
        return [*head, "", *wrap("  차이 0.0%p · 그릴 면적 없음(잰 차이 · 나머지 모두 0)", width)]
    if empty:
        text = "  차이 모름 · 그릴 면적 없음(두 기여를 모두 확인한 날 없음)" if empty["kind"] == "unknown" else \
            f"  차이({_md(empty['seen_day'])}까지) 0.0%p · 그릴 면적 없음(잰 차이 · 나머지 모두 0)"
        return [*head, "", *wrap(text, width), *_below(_b_labels(series["labels"], None), width, color, {})]
    labels = _b_labels(series["labels"], None)
    bw = min(BODY_CAP, width)
    p = bw - LEFT
    if p < MIN_PLOT:  # 그림이 들어가지 않는 폭
        return [*head, "", *wrap(f"  터미널이 {LEFT + MIN_PLOT}칸보다 좁아 그림은 생략 · 숫자만", width), *_below(labels, width, color, {})]
    top, h = _scale(series["low"], series["high"], B_ROWS)
    z = _row(0.0, top, h, B_ROWS)
    side = width >= bw + 2 + _lw(labels)
    cv = Canvas(bw + 2 + _lw(labels) if side else bw, B_ROWS + 2)
    start, n, col_rec, cuts = _view(recs, p)
    _frame(cv, B_ROWS, {**_ticks(top, h, B_ROWS, "%p"), z: "0%p"}, z)
    labels = _b_labels(series["labels"], _b_figure(cv, col_rec, cuts, top, h, z))  # 줄 번호는 그림을 그린 뒤에 앎
    _x_axis(cv, B_ROWS, start, n, p)
    at = _beside(labels, B_ROWS + 1) if side else None
    for lab, y in zip(labels, at or []):
        _place(cv, bw + 2, y, lab)
    return [*head, "", *cv.rows(color), *([] if at else ["", *_below(labels, width, color, {})])]


def _e_pct(v: float) -> str:
    """E의 부호 붙은 백분율(소수 둘, 천 단위 쉼표): 반올림해 0이면 부호 없이 0(`+0.00%` · `-0.00%` 대신 `0.00%`).

    Args:
        v: 비율(0.01 = 1%).

    Returns:
        예: `+1.25%` · `-0.62%` · `+1,000.00%` · `0.00%`.
    """
    return "0.00%" if float(f"{v * 100:.2f}") == 0 else f"{v * 100:+,.2f}%"


def _e_axis(cv: Canvas, y: int, n: int, p: int, x_of: list[int], mark: int) -> None:
    """E x 축 한 줄과 그 밑 거래 번호 한 줄: 첫 거래(1) · 기준선(mark번째, 들어 있으면) · 마지막 거래 번호.

    Args:
        cv: 그릴 격자.
        y: 축 줄 번호(번호 줄은 그 다음).
        n: 그린 거래 수(1 이상).
        p: 그림 폭(칸).
        x_of: 거래(0부터) → 칸.
        mark: 기준선 거래 번호(1부터).
    """
    cv.text(0, y, "     └" + "─" * p)
    end = 0
    for i in dict.fromkeys([1, *([mark] if mark <= n else []), n]):
        text = f"{i:,}"
        x = min(x_of[i - 1], p - len(text))  # 마지막 번호는 그림 안에 다 들어가게 당겨 붙임
        cv.put(LEFT + x_of[i - 1], y, "┬")
        if x >= end:
            cv.text(LEFT + x, y + 1, text)
            end = x + len(text) + 1


def chart_e(name: str, recs: list[dict], g: dict, color: bool, width: int) -> list[str]:
    """E 거래 단위 차트: 실전 시작부터 거래 순서대로 거래 1건 수익 ●, 실제 누적 평균 ━, 백테스트 거래 가운데 80% 띠 · 평균 ╌, 30건 선 ┆(표본이 하나도 빠지지 않고 기록에 없는 청산도 없을 때만).

    x는 날짜가 아니라 거래 순서(칸보다 거래가 많으면 같은 칸에 겹침). y 눈금 · 라벨 배치 · 폭 규칙은 A와 같다. 기록 상한(구간마다 2,000)이나
    표시 상한(E_CAP)으로 빠진 거래가 있으면 머리에 빠진 수를 적는다. 분위가 없는 예전 가드레일은 띠 없이 평균 선만 그린다.

    Args:
        name: 전략 이름.
        recs: 이 전략의 기록 전체(날짜순, 비어 있지 않음).
        g: 가드레일 내용(read_guardrail이 모양을 검사함).
        color: 색을 쓸지(paint.color_on()).
        width: 터미널 가로 칸 수.

    Returns:
        줄 목록(모든 줄이 width 이하).
    """
    e = e_series(recs, g, E_CAP)
    rets, n, run, p10, p90, bt, minimum = (e[k] for k in ("rets", "n", "run", "p10", "p90", "mean", "minimum"))
    if not rets:
        return wrap(f"{plain(name)} · 거래 1건 수익 기록 없음 · 매일 대조가 다음에 셈", width)
    last = recs[-1]
    band = p10 is not None and p90 is not None
    info = f"{plain(name)} · 실전 내내 청산 {e['exits']:,}건 · 수익 표본 {n:,}건" + \
        (f"(실전 전 포지션 청산 {e['skip']:,}건 뺌)" if e["skip"] > 0 else "") + " · 거래 순서 →"
    lost = ""
    if e["lost"]:
        lost = f"빠진 거래 {e['lost']:,}건" + \
            (f" · 기록 상한(구간마다 {TRADE_KEEP:,})에 걸린 구간 " + " · ".join(f"{_md(d)}~" for d in e["capped"]) if e["capped"] else "") + \
            (f" · 표시 상한 {E_CAP:,}건" if e["kept"] > len(rets) else "") + f" · 점 · 평균은 남은 {len(rets):,}건 기준"
    unseen = e["unseen"]
    no_band = [] if band else wrap("백테스트 거래 가운데 80%는 다음 가드레일 저장부터(blackclip save --reason)", width)
    pre = [*wrap(HEAD_E, width), "", *wrap(info, width), *(wrap(lost, width) if lost else []),
           *(wrap(f"매일 대조 기록에 없는 청산 {unseen:,}건", width) if unseen > 0 else []), *no_band, ""]
    n7, lo7, hi7, edge0 = (e[k] for k in ("n7", "tf_low", "tf_high", "edge0"))
    tail = ["",
            f"판단에 필요한 거래 수 · {n:,}건 · " + (f"{minimum:,}건 선을 넘음" if n >= minimum else f"{minimum:,}건 전 · 판단 보류"),
            f"거래 1건 평균 · 실제 {_e_pct(run[-1])} · 백테스트 " + ("모름" if bt is None else _e_pct(bt)),
            "엣지가 없어도 정상 범위 안일 확률 " + ("모름" if edge0 is None else f"{edge0:.0%}") + f" → {plain(base(last['states']['전략']))}",
            "거래 빈도 · " + ("아직 비교 안 함" if n7 is None or lo7 is None or hi7 is None else f"최근 7일 {n7:,}건 · 백테스트 기준 {lo7:,}~{hi7:,}건"),
            "점 하나가 실제 거래 1건(사고 판 한 번 · 백테스트와 같은 정의 · 실제 수수료, 백테스트 띠는 보수적 비용)",
            "거래가 적을 때는 평균이 크게 흔들려 날짜가 아니라 거래 수로 본다"]
    tail = [x for line in tail for x in (wrap(line, width) if line else [line])]
    vals = [0.0, *rets, *run, *([p10, p90] if band else []), *([] if bt is None else [bt])]
    top, h = _scale(min(vals), max(vals), E_ROWS)

    def row(v: float) -> int:
        return _row(v, top, h, E_ROWS)

    labels: list[_Label] = [*([("▇", "evidence", f"백테스트 거래 가운데 80% 상단 {_e_pct(p90)}", row(p90))] if band else []),
                            *([("╌", "bt", f"백테스트 평균 {_e_pct(bt)}", row(bt))] if bt is not None else []),
                            *([("▁", "evidence", f"하단 {_e_pct(p10)}", row(p10))] if band else []),
                            ("━", "actual", f"실제 평균 {_e_pct(run[-1])}({len(rets):,}건)", row(run[-1]))]
    line30 = e["line30"]
    legend = wrap("   ".join(["● 실제 거래 1건", "━ 실제 평균(쌓일수록 안정)", *([f"{'▇' if color else '░'} 백테스트 거래 가운데 80%"] if band else []),
                              *(["╌ 백테스트 평균"] if bt is not None else []), *([f"┆ {minimum:,}건 선"] if line30 else [])]), width)
    bw = min(BODY_CAP, width)
    p = bw - LEFT
    if p < MIN_PLOT:  # 그림이 들어가지 않는 폭
        return [*pre, *wrap(f"  터미널이 {LEFT + MIN_PLOT}칸보다 좁아 그림은 생략 · 숫자만", width),
                *_below(labels, width, color, _E_ROLES), *tail]
    at = _beside(labels, E_ROWS + 1) if width >= bw + 2 + _lw(labels) else None
    cv = Canvas(bw + 2 + _lw(labels) if at else bw, E_ROWS + 2)
    x_of = [round(i * (p - 1) / max(len(rets) - 1, 1)) for i in range(len(rets))]
    _frame(cv, E_ROWS, _ticks(top, h, E_ROWS, "%"))
    if line30:  # 기준선을 먼저 그어 점이 위에 덮이게
        for y in range(E_ROWS):
            cv.put(LEFT + x_of[minimum - 1], y, "┆", "muted")
    for x in range(p if band else 0):
        for y in range(row(p90), row(p10) + 1):
            cv.bg(LEFT + x, y, "band")
    if bt is not None:
        for x in range(p):
            cv.put(LEFT + x, row(bt), "╌", "bt")
    for i, x in enumerate(x_of):  # 다음 거래 칸 앞까지 그 거래까지의 평균 높이를 이음
        for k in range(x, x_of[i + 1] if i + 1 < len(x_of) else x + 1):
            cv.put(LEFT + k, row(run[i]), "━", "actual")
    for i, x in enumerate(x_of):
        cv.put(LEFT + x, row(rets[i]), "●", "muted")
    _e_axis(cv, E_ROWS, len(rets), p, x_of, minimum if line30 else len(rets) + 1)
    for lab, y in zip(labels, at or []):
        _place(cv, bw + 2, y, lab)
    return [*pre, *cv.rows(color), "", *([] if at else _below(labels, width, color, _E_ROLES)),
            *(paint(_tint(x, _E_ROLES), color) for x in legend), *tail]


def _empty(root: Path, name: str) -> str:
    """기록이 없는 전략의 한 줄: live.json 유무로 「실전 중」과 「실전 시작 전」을 가른다.

    Args:
        root: .blackclip 경로.
        name: 전략 이름.

    Returns:
        한 줄.
    """
    if exists_regular(root, f"strategies/{name}/live.json"):
        return f"{plain(name)} · 실전 중 · 매일 대조 기록 아직 없음(첫 blackclip daily 뒤에 보임)"
    return f"{plain(name)} · 실전 시작 전 · 점검 결과는 blackclip check {plain(name)}"


def chart_lines(root: Path, name: str | None, color: bool, width: int, trades: bool = False) -> tuple[int, list[str]]:
    """chart 화면 줄들. 이름을 안 주면 가드레일이 있는 전략의 D 목록, 주면 그 전략의 A 범위 차트 · B 차이 면적 · C 게이지(trades면 E 거래 단위 차트만).

    전체 보기에서 days.jsonl을 읽지 못한 전략은 그 전략만 「확인 불가」 줄로 D 목록 · 기다리는 전략 줄 뒤에 두고 나머지는 평소대로 낸다
    (종료 코드 4). 기록 손상(record.md를 읽지 못함) 전략이 있어도 전체 보기는 종료 4다(status와 같게).
    전체 보기는 가드레일을 읽지 않는다. 이름을 준 보기는 그대로 오류를 올리고 종료 코드도 바꾸지 않는다.

    Args:
        root: .blackclip 경로.
        name: 전략 이름 또는 None.
        color: 색을 쓸지(paint.color_on()).
        width: 터미널 가로 칸 수(paint.width()).
        trades: True면 이름 준 보기에서 A~C 대신 E를 그린다(이름이 없으면 cli가 먼저 거절함).

    Returns:
        (종료 코드 0 또는 4, 줄 목록).

    Raises:
        WorkspaceError: 이름 모양이 다르거나 없는 전략 · strategies/가 링크 · 이름을 준 보기의 파일 문제(링크 · FIFO · 상한 · 읽을 줄이
            하나도 없는 days.jsonl) · 깨진 가드레일(A가 읽을 때).
        OSError: 이름을 준 보기에서 읽지 못함.
    """
    names = strategies(root)
    off, broken = 0, False
    if name is None:  # 끈 전략(중지 · 은퇴 · 기록 손상)은 목록 · 「실전 N개」에서 빠지고 끝에 한 줄(status 전체 보기와 같은 규칙)
        stop = stopped(root)
        off = len(stop)  # 가드레일 판 파일이 없는 끈 전략도 셈(status 전체 보기와 같은 기준)
        broken = any(v[0] == BROKEN for v in stop.values())  # 기록 손상은 종료 4(status와 같게)
        names = [n for n in names if n not in stop]
    if name is not None:
        if not _NAME.fullmatch(name):
            raise WorkspaceError(f"전략 이름 모양이 다릅니다({plain(name)})")
        if name not in names:
            if name in stopped(root):  # 판 파일이 없는 끈 전략: 「없다」가 아니라 status로 안내(막다른 길 없애기)
                raise WorkspaceError(f"끈 전략이라 가드레일 판이 없습니다({plain(name)}), blackclip status {plain(name)}로 보세요")
            raise WorkspaceError(f"전략이 없습니다({plain(name)}) · blackclip chart로 목록")
        names = [name]
    elif not names:
        return (PARTIAL if broken else 0), [*wrap(NO_LIVE, width), *wrap(OFF_LINE.format(n=off), width)] if off else wrap(NO_LIVE, width)
    got, bad = [], []
    for n in names:
        try:
            got.append((n, read_days(root, n)))
        except (WorkspaceError, OSError) as exc:
            if name is not None:
                raise
            bad += wrap(f"{plain(n)} · 확인 불가({plain(_why(exc))})", width)
    if name is not None:
        recs = got[0][1]
        if not recs:
            return 0, wrap(_empty(root, name), width)
        if (guard := read_guardrail(root, name)) is None:  # 목록을 본 뒤 폴더가 사라진 경우
            raise WorkspaceError(f"전략이 없습니다({plain(name)}) · blackclip chart로 목록")
        if trades:
            return 0, chart_e(name, recs, guard[1], color, width)
        return 0, [*chart_a(name, recs, guard[1], color, width), "", *chart_b(name, recs, color, width), "",
                   *chart_c(name, recs, color, width)]
    entries = [(n, r) for n, r in got if r]
    tail = [x for n, r in got if not r for x in wrap(_empty(root, n), width)] + bad
    body = [*(chart_d(entries, color, width) if entries else []), *([""] if entries and tail else []), *tail]
    return (PARTIAL if bad or broken else 0), [*body, *([""] if body and off else []), *(wrap(OFF_LINE.format(n=off), width) if off else [])]

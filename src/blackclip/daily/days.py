"""기계 판독 일별 기록 days.jsonl: daily가 전략마다 하루 한 줄 덧붙이고, 화면이 읽는다.

숫자와 상태는 reconcile이 낸 Day 그대로다. 읽는 쪽은 줄마다 모양을 검사해 틀린 줄만 건너뛰고, 같은 날은 마지막 줄을 쓴다.
pandas를 import하지 않는다(화면 import가 가볍게).
"""
from __future__ import annotations

import copy
import json
import math
from datetime import datetime
from pathlib import Path
from typing import TYPE_CHECKING, Any

from blackclip.hook import _NAME
from blackclip.states import TITLES, TRACKED, representative, states_ok
from blackclip.workspace import (
    WorkspaceError,
    _day,
    append_line,
    read_bytes,
    read_jsonl,
)

if TYPE_CHECKING:
    from blackclip.daily.reconcile import Day

DAYS = "days.jsonl"
"""전략 폴더 daily/ 안 파일 이름."""

CAP = 8 << 20
"""읽기 상한(바이트). 넘으면 파일 문제로 거절."""

LINE_CAP = 64 << 10
"""한 줄 상한(바이트, 줄바꿈 제외한 UTF-8). append_day가 넘는 줄은 줄여 쓰고, 줄여도 넘으면 쓰지 않는다."""

BOUND = 1e6
"""숫자 칸의 절댓값 상한(비율 · bp)."""

TRADE_KEEP = 2000
"""구간마다 nums.trade_rets에 남기는 거래 1건 수익의 최근 개수(줄 크기를 LINE_CAP 안에 둠)."""

NUM_KEYS: tuple[str, ...] = ("actual", "bt", "bt0", "gap", "fee", "slip", "funding", "rest", "lo", "hi", "month_lo",
                             "month_hi", "line", "room", "edge0", "slip_med_bp", "slip_max_bp", "trade_mean")
"""nums의 숫자 칸(숫자 또는 None). 이 밖에 정수 칸(INT_KEYS) · 글 칸 · missed · all이 있고, 모두 선택이다."""

INT_KEYS: tuple[str, ...] = ("d", "min_trades", "n7", "tf_low", "tf_high", "missed_n", "extra", "trade_n", "trade_skip")
"""nums의 정수 칸(정수 또는 None)."""

_TEXT_KEYS = ("sensitivity", "pos", "bt_pos")
_ALL_NUM_KEYS = ("actual", "bt", "bt0", "gap", "fee", "slip", "funding", "rest")
_ALL_INT_KEYS = ("missed_n", "extra")
_TEXT_MAX = 4000
_INT_MAX = 10 ** 7
_MISSED_MAX = 50
_LINES_CUT = 2000


def _num(x: Any) -> bool:
    """bool이 아닌 유한한 수이고 절댓값이 BOUND 이하인지(int는 크기를 먼저 봐서 `math.isfinite(10**400)`의 OverflowError를 피함).

    Args:
        x: 값.

    Returns:
        그렇다면 True.
    """
    if isinstance(x, bool):
        return False
    if isinstance(x, int):
        return abs(x) <= BOUND
    return isinstance(x, float) and math.isfinite(x) and abs(x) <= BOUND


def _int(x: Any) -> bool:
    """bool이 아닌 0 이상 10**7 이하 정수인지.

    Args:
        x: 값.

    Returns:
        그렇다면 True.
    """
    return isinstance(x, int) and not isinstance(x, bool) and 0 <= x <= _INT_MAX


def _text(x: Any) -> bool:
    """4,000자까지의 글인지.

    Args:
        x: 값.

    Returns:
        그렇다면 True.
    """
    return isinstance(x, str) and len(x) <= _TEXT_MAX


def _iso_time(x: Any) -> bool:
    """ISO 시각 글인지.

    Args:
        x: 값.

    Returns:
        그렇다면 True.
    """
    try:
        return _text(x) and datetime.fromisoformat(x) is not None
    except ValueError:
        return False


def _missed_ok(x: Any) -> bool:
    """놓친 주문 후보 목록(최대 50개, 칸 t · symbol · side)인지.

    Args:
        x: 값.

    Returns:
        그렇다면 True.
    """
    return isinstance(x, list) and len(x) <= _MISSED_MAX and all(
        isinstance(m, dict) and _iso_time(m.get("t")) and _text(m.get("symbol")) and _text(m.get("side")) for m in x)


def _all_ok(a: Any) -> bool:
    """nums.all(실전 시작부터의 값)의 모양: 있는 칸만 검사한다.

    Args:
        a: 값.

    Returns:
        그렇다면 True.
    """
    return (isinstance(a, dict)
            and (a.get("start") is None or _day(a["start"]) is not None)
            and all(a.get(k) is None or _num(a[k]) for k in _ALL_NUM_KEYS)
            and all(a.get(k) is None or _int(a[k]) for k in _ALL_INT_KEYS)
            and (a.get("missed") is None or _missed_ok(a["missed"])))


def _nums_ok(n: Any) -> bool:
    """nums의 모양: 모든 칸이 선택이고(없으면 None으로 읽음) 있는 칸만 검사한다.

    Args:
        n: 값.

    Returns:
        그렇다면 True.
    """
    return (isinstance(n, dict)
            and all(n.get(k) is None or _num(n[k]) for k in NUM_KEYS)
            and all(n.get(k) is None or _int(n[k]) for k in INT_KEYS)
            and all(n.get(k) is None or _text(n[k]) for k in _TEXT_KEYS)
            and (n.get("period_start") is None or _day(n["period_start"]) is not None)
            and (n.get("missed") is None or _missed_ok(n["missed"]))
            and (n.get("trade_rets") is None or (isinstance(n["trade_rets"], list) and all(_num(x) for x in n["trade_rets"])))
            and (n.get("trade_n") is None or n.get("trade_rets") is None or n["trade_n"] >= len(n["trade_rets"]))
            and (n.get("all") is None or _all_ok(n["all"])))


def valid(rec: Any) -> dict | None:
    """줄 하나의 모양을 검사한다. 맞으면 그대로, 아니면 None.

    상태는 TRACKED 다섯 칸이 모두 ORDER의 이름(괄호 사유는 붙어도 됨)이고, 논지 칸만 「해당 없음」도 받는다
    (화면의 `ORDER.index` · 기호 찾기는 「해당 없음」을 대표 상태 · 기호 계산에서 건너뜀).

    Args:
        rec: JSON 줄을 읽은 값.

    Returns:
        모양이 맞으면 rec, 아니면 None.
    """
    if not isinstance(rec, dict):
        return None
    st, ln, resp = rec.get("states"), rec.get("lines"), rec.get("response")
    ok = (_int(rec.get("v")) and rec["v"] == 1
          and isinstance(rec.get("strategy"), str) and _NAME.fullmatch(rec["strategy"]) is not None
          and _day(rec.get("day")) is not None
          and _int(rec.get("live_day")) and _int(rec.get("trades"))
          and all(_text(rec.get(k)) for k in ("head", "rep", "title"))
          and states_ok(st) and all(_text(v) for v in st.values())
          and isinstance(ln, dict) and set(ln) == set(TRACKED) and all(_text(v) for v in ln.values())
          and isinstance(resp, dict) and all(_text(k) and _text(v) for k, v in resp.items())
          and _nums_ok(rec.get("nums")))
    return rec if ok else None


def line_of(day: Day) -> dict:
    """Day를 days.jsonl 한 줄(dict)로: 대표 상태 · 이름은 states에서 여기서 낸다.

    Args:
        day: 하루 결과(nums는 비어 있어도 됨, 대조 실패한 날).

    Returns:
        v · strategy · day · live_day · trades · head · rep · title · states · lines · response · nums.
    """
    rep, key = representative(day.states)
    return {"v": 1, "strategy": day.strategy, "day": day.day.isoformat(), "live_day": day.live_day, "trades": day.trades,
            "head": day.head, "rep": rep, "title": TITLES[key], "states": dict(day.states), "lines": dict(day.lines),
            "response": dict(day.response), "nums": day.nums}


def _dump(line: dict) -> str:
    """한 줄 JSON(키 정렬 · NaN 거절) + 줄바꿈.

    Args:
        line: line_of 결과.

    Returns:
        글.
    """
    return json.dumps(line, ensure_ascii=False, sort_keys=True, allow_nan=False) + "\n"


def _clip(x: Any) -> Any:
    """줄 안의 모든 글(값 · 키)을 읽기 계약의 4,000자로 자른다. 숫자 · None은 그대로.

    Args:
        x: 줄 또는 그 안의 값.

    Returns:
        잘린 복사본.
    """
    if isinstance(x, str):
        return x[:_TEXT_MAX]
    if isinstance(x, dict):
        return {_clip(k): _clip(v) for k, v in x.items()}
    if isinstance(x, list):
        return [_clip(v) for v in x]
    return x


def _shrunk(line: dict, step: int) -> dict:
    """줄을 한 단계 더 줄인다. 0: 근거 글(lines)을 2,000자로, 1: 놓친 주문 목록(missed, nums.all.missed)을 비움(개수 missed_n은 남음).

    Args:
        line: 줄(바꾸지 않음).
        step: 단계(0 또는 1).

    Returns:
        줄인 복사본.
    """
    out = copy.deepcopy(line)
    if step == 0:
        out["lines"] = {k: v[:_LINES_CUT] for k, v in out["lines"].items()}
    else:
        nums = out["nums"]
        if nums.get("missed"):
            nums["missed"] = []
        if isinstance(nums.get("all"), dict) and nums["all"].get("missed"):
            nums["all"]["missed"] = []
    return out


def append_day(root: Path, day: Day) -> None:
    """strategies/<전략>/daily/days.jsonl 끝에 하루 한 줄을 덧붙인다(링크 · FIFO 거절, 폴더는 만듦).

    쓰기 전에 읽기 계약(read_days가 받는 줄)에 맞춘다: 모든 글을 4,000자로 자르고, UTF-8 바이트가 LINE_CAP을 넘으면
    근거 글(lines) → 놓친 주문 목록(missed) 순으로 줄이며 단계마다 다시 잰다. 그래도 넘거나 valid가 거절할 줄은
    쓰지 않는다(쓴 하루가 읽기에서 조용히 사라지는 일이 없게).

    Args:
        root: .blackclip 경로.
        day: 하루 결과.

    Raises:
        WorkspaceError: 읽기 계약에 못 맞추는 줄 · 상위 폴더 링크 · 파일이 링크 · FIFO · 장치.
        OSError: 쓰지 못함.
        ValueError: nums에 NaN · 무한대가 들어 있을 때(reconcile은 그런 값을 None으로 낸다).
    """
    line = _clip(line_of(day))
    text = _dump(line)
    for step in (0, 1):
        if len(text.encode("utf-8")) <= LINE_CAP:
            break
        line = _shrunk(line, step)
        text = _dump(line)
    if len(text.encode("utf-8")) > LINE_CAP or valid(json.loads(text)) is None:
        raise WorkspaceError(f"{day.strategy} {day.day.isoformat()}: 읽기 계약(줄 {LINE_CAP:,}바이트 · 글 {_TEXT_MAX:,}자 · 모양)에 "
                             "못 맞춰 days.jsonl에 기록하지 않습니다")
    append_line(root, f"strategies/{day.strategy}/daily/{DAYS}", text, make=True)


def read_days(root: Path, name: str) -> list[dict]:
    """전략의 일별 기록을 읽는다: 줄마다 모양 검사 → 날마다 마지막 줄 → 날짜순.

    Args:
        root: .blackclip 경로.
        name: 전략 이름(전략 이름 모양이 아니거나 파일이 없으면 빈 목록).

    Returns:
        valid를 통과한 줄들(하루 하나, 날짜 오름차순). 파일이 없거나 빈 파일 · 공백 줄만이면 빈 목록(기록 없음).

    Raises:
        WorkspaceError: 파일 문제(상위 폴더 링크 · 파일 링크 · FIFO · 8MiB 초과), 또는 빈 줄 아닌 줄이 있는데 읽을 수 있는 줄이 하나도 없을 때
            (깨졌거나 모양이 다른 줄뿐인 파일을 「기록 없음」으로 오인하지 않게).
    """
    if not _NAME.fullmatch(name):
        return []
    rel = f"strategies/{name}/daily/{DAYS}"
    last: dict[str, dict] = {}
    for rec in read_jsonl(root, rel, CAP):
        if (ok := valid(rec)) is not None:
            last[ok["day"]] = ok
    if not last:  # read_jsonl은 JSON이 안 풀리는 줄을 건너뛰어 「전부 깨짐」과 「빈 파일」이 같은 []라 같은 경로를 한 번 더 읽어 가린다
        try:
            if read_bytes(root, rel, CAP).strip():
                raise WorkspaceError(f"{root / rel}: days.jsonl에 읽을 수 있는 줄이 없습니다(깨졌거나 모양이 다른 줄만 있음)")
        except FileNotFoundError:
            pass
    return [last[d] for d in sorted(last)]


def current_period(recs: list[dict]) -> list[dict]:
    """이번 30일 구간의 줄들(게이지 · 구간 경계용): period_start가 있는 마지막 줄과 같은 구간의 줄 + 구간 시작 뒤의 대조 실패한 줄.

    대조가 예외로 끝난 날의 줄은 nums가 비어 period_start가 없다. 그래서 마지막 줄이 실패여도 앞 정상 날들이 빠지지 않게
    실패한 줄은 구간 시작일 이후이면 이번 구간에 넣는다. period_start가 있는 줄이 하나도 없으면 실패한 줄을 모두 돌려준다.

    Args:
        recs: read_days 결과.

    Returns:
        이번 구간의 줄들(날짜순). 줄이 없으면 빈 목록.
    """
    starts = [r["nums"].get("period_start") for r in recs]
    start = next((p for p in reversed(starts) if p), None)
    if start is None:
        return sorted(recs, key=lambda r: r["day"])
    return sorted((r for r, p in zip(recs, starts) if p == start or (not p and r["day"] >= start)), key=lambda r: r["day"])

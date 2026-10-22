"""전략 기록(blackclip record): 전략을 끄고 켠 날 · 이유 · 고객 결정을 .blackclip/strategies/<전략>/record.md에 남긴다.

중지 · 은퇴 때는 끈 뒤 회고 한 장 after.md(Git 제외)도 쓰고 ④ 한 줄을 record.md에 남겨 다음 check 끝에 잇는다.
record.md가 기준 파일이다: 첫 줄 머리(JSON 사건 목록) + 사람이 읽는 본문 + 고객 메모 꼬리(엔진이 그대로 둠). 끈 전략(중지 · 은퇴)은 매일 대조 ·
실시간 감시 · 텔레그램에서만 빠지고 계좌 귀속에는 남는다. 기록을 읽지 못하면(git 충돌 표지 · 링크 · 깨진 머리) 자동으로 켜지 않고
「기록 손상」으로 멈춘다. pandas · numpy를 부르지 않는다(status와 같은 묶음).
"""
from __future__ import annotations

import json
import os
import re
import stat
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from typing import Any

from blackclip.hook import _NAME
from blackclip.scan import has_abs_path, has_secret, show
from blackclip.screen.paint import SYMBOL
from blackclip.states import LABELS, TRACKED, base, pp, states_ok
from blackclip.workspace import (WorkspaceError, _day, append_line, exists_regular, lock, read_bytes, read_guardrail, read_jsonl,
                                 strategy_names, write_in)

RECORD = "record.md"
"""전략 폴더 안 기록 파일 이름."""

CAP = 256 << 10
"""record.md 읽기 · 쓰기 상한(바이트)."""

MAX_EVENTS = 1000
"""사건 수 상한."""

TEXT_MAX = 500
"""이유 · 결정 · 회고 글자 수 상한."""

STATES = ("실전", "중지", "은퇴")
"""전략 상태. 끈 전략은 중지 · 은퇴."""

BROKEN = "기록 손상"
"""record.md를 읽지 못할 때의 상태 이름(끈 것으로 취급)."""

C0_WORDS = {"일치": "재현성 일치", "불일치": "재현성 불일치", "재현 확인 전": "재현성 확인 전"}
"""가드레일의 재현성 점검 상태 → 고객 글(after.md · 끈 뒤 칸이 같이 씀). 표에 없는 값은 `재현성 <값>`."""

HEAD = re.compile(r"^<!-- blackclip record v1 (\{.*\}) -->$")
"""record.md 첫 줄."""

MEMO = "<!-- 여기부터 고객 메모(엔진이 그대로 둠) -->"
"""이 줄 아래는 고객 글이라 엔진이 다시 써도 그대로 둔다."""

AFTER = "after.md"
"""전략 폴더 안 끈 뒤 회고 파일 이름(Git 제외, 엔진은 쓰기만 · 은퇴 줄 중복 확인 때만 읽음)."""

RETIRED = "은퇴한 전략은 되돌리지 않습니다 · 다시 쓰려면 새 이름으로"
"""은퇴한 전략을 실전 · 중지로 바꾸려 할 때의 오류 글."""

HEAD_BROKEN = "record.md 머리 줄을 읽지 못해 덮어쓰지 않습니다 · git 충돌이면 풀고, 아니면 record.md를 지운 뒤 다시"
"""기록을 읽지 못한 채 쓰려 할 때의 오류 글(깨진 파일을 덮어쓰지 않음)."""

TOO_BIG = "record.md가 상한(256KiB · 사건 1,000개)을 넘게 돼 쓰지 않았습니다 · 기존 파일 그대로 · 오래된 사건 글은 고객 메모 밖으로 옮긴 뒤 다시"
"""쓸 최종 글이 상한을 넘을 때의 오류 글(아무것도 쓰지 않음)."""

_EVENT_KEYS = frozenset({"day", "state", "reason", "decision"})
"""사건 하나가 정확히 가져야 하는 칸 이름."""

_ISO = re.compile(r"[0-9]{4}-[0-9]{2}-[0-9]{2}")
"""사건 날의 모양(ASCII 숫자만, 뜻이 맞는 날인지는 fromisoformat이 따로 봄)."""

_RETIRE_LINE = re.compile(r"(?m)^[0-9]{2}/[0-9]{2} 은퇴 · 고객 결정 · 이유: ")
"""after.md에 이미 덧붙인 은퇴 줄(`MM/DD 은퇴 · 고객 결정 · 이유: …`, 날짜와 무관, 덧붙이는 줄의 모양 그대로)."""

_ONE = timedelta(days=1)
"""하루."""


class RecordError(ValueError):
    """record.md를 읽거나 쓸 수 없을 때 · 입력이 틀렸을 때(명령은 한 줄 · 종료 2)."""


def _text_ok(x: Any) -> bool:
    """이유 · 결정 · 회고 칸 값이 None 또는 TEXT_MAX 이하의 한 줄 인쇄 가능 글인지(MEMO 줄을 흉내 내지 않음).

    Args:
        x: 칸 값.

    Returns:
        맞으면 True.
    """
    return x is None or (isinstance(x, str) and len(x) <= TEXT_MAX and x.isprintable() and MEMO not in x)


def _event_ok(e: Any) -> bool:
    """사건 하나의 모양: 칸 넷 · day ISO · state ∈ STATES · 이유 · 결정 글.

    Args:
        e: 사건.

    Returns:
        맞으면 True.
    """
    if not isinstance(e, dict) or set(e) != _EVENT_KEYS or not isinstance(e["day"], str) or not _ISO.fullmatch(e["day"]):
        return False
    try:
        date.fromisoformat(e["day"])
    except ValueError:
        return False
    return e["state"] in STATES and _text_ok(e["reason"]) and _text_ok(e["decision"])


def read_record(root: Path, name: str) -> tuple[dict, str] | None:
    """record.md를 읽는다(링크 · FIFO · 상위 폴더 링크를 따르지 않고 머리 모양까지 확인).

    Args:
        root: .blackclip 경로.
        name: 전략 이름.

    Returns:
        (머리 JSON, 고객 메모 꼬리). 파일이 없으면 None. MEMO 줄이 없으면 머리 아래 본문 전체를 메모로 본다(고객 글을 잃지 않게).

    Raises:
        RecordError: 링크 · FIFO · 상한 · UTF-8 아님 · 머리 줄 없음 · JSON 깨짐 · 모양이 다름.
    """
    try:
        raw = read_bytes(root, f"strategies/{name}/{RECORD}", CAP)
    except FileNotFoundError:
        return None
    except (WorkspaceError, OSError) as exc:
        raise RecordError(f"{RECORD}를 읽지 못함({exc if isinstance(exc, WorkspaceError) else type(exc).__name__})") from None
    try:
        head, _, rest = raw.decode("utf-8").partition("\n")
    except UnicodeDecodeError:
        raise RecordError(f"{RECORD}가 UTF-8이 아님") from None
    m = HEAD.fullmatch(head)
    if m is None:
        raise RecordError(f"{RECORD} 머리 줄이 없음")
    try:
        doc = json.loads(m[1])
    except (ValueError, RecursionError):
        raise RecordError(f"{RECORD} 머리 줄 JSON이 깨짐") from None
    if (not isinstance(doc, dict) or not isinstance(doc.get("events"), list) or len(doc["events"]) > MAX_EVENTS
            or not all(_event_ok(e) for e in doc["events"]) or "lesson" not in doc or not _text_ok(doc["lesson"])):
        raise RecordError(f"{RECORD} 머리 줄 모양이 다름")
    i = rest.find(MEMO)
    if i < 0:
        return doc, rest
    memo = rest[i + len(MEMO):]
    return doc, memo[1:] if memo.startswith("\n") else memo


def _since(events: list[dict]) -> tuple[str, str]:
    """지금 상태와 그 상태가 시작된 날(끝에서부터 같은 상태가 이어진 처음 사건의 날).

    Args:
        events: 비지 않은 사건 목록(오래된 순).

    Returns:
        (상태, 날).
    """
    i = len(events) - 1
    while i > 0 and events[i - 1]["state"] == events[-1]["state"]:
        i -= 1
    return events[-1]["state"], events[i]["day"]


def _now(root: Path, name: str) -> tuple[str, str] | None:
    """전략의 지금 상태와 날. 기록 없음 · 사건 없음 = None, 읽지 못함 = (BROKEN, "").

    Args:
        root: .blackclip 경로.
        name: 전략 이름.

    Returns:
        (상태, 날) 또는 None.
    """
    try:
        got = read_record(root, name)
    except RecordError:
        return BROKEN, ""
    return _since(got[0]["events"]) if got and got[0]["events"] else None


def current_state(root: Path, name: str) -> str | None:
    """마지막 사건의 상태. 기록 없음 = None, 읽지 못함 = BROKEN(자동 재개 금지).

    Args:
        root: .blackclip 경로.
        name: 전략 이름.

    Returns:
        상태 이름 또는 None.
    """
    got = _now(root, name)
    return got[0] if got else None


def _is_folder(path: Path) -> bool:
    """링크를 따르지 않고 진짜 폴더인지.

    Args:
        path: 확인할 경로.

    Returns:
        폴더이면 True(링크 · 파일 · 없음은 False).
    """
    try:
        return stat.S_ISDIR(os.lstat(path).st_mode)
    except OSError:
        return False


def stopped(root: Path) -> dict[str, tuple[str, str]]:
    """strategies/ 아래 전략 이름 모양 폴더(링크 · 파일 제외) 중 중지 · 은퇴 · 기록 손상인 것.

    Args:
        root: .blackclip 경로.

    Returns:
        이름 → (상태, 그 상태가 시작된 날 · 손상이면 "").

    Raises:
        WorkspaceError: strategies/가 링크이거나 폴더가 아님.
    """
    out = {}
    for n in strategy_names(root):
        if _NAME.fullmatch(n) and _is_folder(root / "strategies" / n) and (got := _now(root, n)) and got[0] != "실전":
            out[n] = got
    return out


def stop_label(state: str, day: str) -> str:
    """끈 전략의 화면 글: 「중지(2026-10-17)」, 손상이면 날 없이 「기록 손상」.

    Args:
        state: stopped가 돌려준 상태.
        day: 그 날(손상이면 "").

    Returns:
        글.
    """
    return f"{state}({day})" if day else state


def render_record(name: str, doc: dict, memo: str) -> str:
    """record.md 전체 글: 머리 · 본문(최신 사건 먼저) · MEMO 줄 · 고객 메모.

    Args:
        name: 전략 이름.
        doc: 머리 JSON(사건이 하나 이상).
        memo: 고객 메모 꼬리(그대로 붙임).

    Returns:
        파일 내용.
    """
    ev = doc["events"]
    state, day = _since(ev)
    last = ev[-1]
    body = [f"# 전략 기록 · {name}", f"- 상태: {state}({day}부터)", f"- 바꾼 이유: {last['reason'] or '이유 미기록'}",
            f"- 고객 결정: {last['decision'] or '적은 것 없음'}",
            f"- 다음 점검에서 먼저 볼 것: {doc['lesson'] or '(중지 · 은퇴 때 엔진이 적음)'}", "## 바뀐 날",
            *(f"- {e['day']} · {e['state']} · 이유: {e['reason'] or '이유 미기록'} · 결정: {e['decision'] or '적은 것 없음'}"
              for e in reversed(ev))]
    head = f"<!-- blackclip record v1 {json.dumps(doc, ensure_ascii=False, sort_keys=True)} -->"
    return "\n".join([head, *body, "", MEMO]) + "\n" + memo


def _clean(label: str, x: str | None) -> str | None:
    """--reason · --decision 글을 확인한다(커밋되는 파일이라 한 줄 · 길이 · 절대 경로 · 홈 폴더 · 키 · 주소).

    Args:
        label: 옵션 이름.
        x: 글.

    Returns:
        앞뒤 공백을 뺀 글, 비면 None.

    Raises:
        RecordError: 한 줄이 아님 · TEXT_MAX 넘음 · 절대 경로 · 키 · 주소 모양 글.
    """
    x = (x or "").strip() or None
    if x is None:
        return None
    if not _text_ok(x):
        raise RecordError(f"{label}는 {TEXT_MAX}자 이하 한 줄 글이어야 합니다(줄바꿈 · 제어 문자 · 서식 문자 없음)")
    if has_abs_path(x):
        raise RecordError(f"{label}에 절대 경로가 있음 · 커밋되는 record.md에 남으므로 레포 기준 상대 경로나 말로 적어 주세요")
    if has_secret(x) or "://" in x:
        raise RecordError(f"{label}에 키 · 토큰 · 주소 모양 글이 있음 · 커밋되는 record.md에 남으므로 말로 적어 주세요")
    return x


def _logged(root: Path, name: str, ev: dict) -> bool:
    """이 전략의 state.log 마지막 record 줄이 이 사건(day · record)인지(같은 날 중지 → 실전 → 중지에서도 마지막 줄을 봄).

    Args:
        root: .blackclip 경로.
        name: 전략 이름.
        ev: 마지막 상태 전환 사건.

    Returns:
        마지막 record 줄이 이 사건이면 True.

    Raises:
        WorkspaceError: state.log가 링크 · FIFO · 상한 초과.
    """
    mine = [x for x in read_jsonl(root, "state.log", 16 << 20) if isinstance(x, dict) and x.get("strategy") == name and "record" in x]
    return bool(mine) and (mine[-1].get("day"), mine[-1]["record"]) == (ev["day"], ev["state"])


def _d(day: date, pad: bool = False) -> str:
    """날을 월/일로(월 · 일 0 안 채움, pad면 채움).

    Args:
        day: 날.
        pad: True면 「10/04」, 아니면 「10/4」.

    Returns:
        월/일 글.
    """
    return f"{day:%m/%d}" if pad else f"{day.month}/{day.day}"


def _span(first: date, last: date, pad: bool = False) -> str:
    """날 구간을 「월/일~월/일」로, 하루면 「월/일」.

    Args:
        first: 처음 날.
        last: 끝 날.
        pad: _d의 pad.

    Returns:
        구간 글.
    """
    return _d(first, pad) if first == last else f"{_d(first, pad)}~{_d(last, pad)}"


def _runs(days: list[date]) -> list[tuple[date, date]]:
    """오름차순 날들을 이어진 날 묶음(처음, 끝)으로 가른다.

    Args:
        days: 오름차순 날.

    Returns:
        묶음 목록.
    """
    out: list[list[date]] = []
    for d in days:
        if out and out[-1][1] + _ONE == d:
            out[-1][1] = d
        else:
            out.append([d, d])
    return [(a, b) for a, b in out]


def _sym(state: str) -> str:
    """상태 기호 + 상태 글(기호가 없는 상태는 「·」).

    Args:
        state: 상태(괄호 사유가 붙어도 됨).

    Returns:
        「⬓ 판단 보류」.
    """
    return f"{SYMBOL.get(base(state), '·')} {show(state)}"


def _pct(x: float | None) -> str:
    """비율을 부호 있는 퍼센트로, 없으면 「모름」.

    Args:
        x: 비율.

    Returns:
        「-0.8%」 또는 「모름」.
    """
    return "모름" if x is None else f"{x:+.1%}"


def _pp(x: float | None) -> str:
    """비율 차이를 %p로, 없으면 「모름」.

    Args:
        x: 비율 차이.

    Returns:
        「-0.3%p」 또는 「모름」.
    """
    return "모름" if x is None else pp(x)


def _why(root: Path, exc: Exception) -> str:
    """못 읽은 사유 글(이 컴퓨터 경로는 .blackclip으로 줄이고 레포 글은 show를 거침).

    Args:
        root: .blackclip 경로.
        exc: read_days · read_guardrail · read_jsonl이 낸 예외.

    Returns:
        한 줄 사유.
    """
    return show(str(exc).replace(str(root), ".blackclip") if isinstance(exc, WorkspaceError) else exc.strerror or type(exc).__name__)


def _days(root: Path, name: str) -> tuple[list[dict], str | None]:
    """일별 기록을 읽는다. 못 읽어도 끄기는 끝내야 해서 예외 대신 사유를 돌려준다.

    Args:
        root: .blackclip 경로.
        name: 전략 이름.

    Returns:
        (기록 목록 · 못 읽으면 빈 목록, 못 읽은 사유 또는 None).
    """
    # 훅 경로가 blackclip.daily를 불러오지 않게 여기서 불러옴(test_hook_events)
    from blackclip.daily.days import read_days

    try:
        return read_days(root, name), None
    except (WorkspaceError, OSError) as exc:
        return [], _why(root, exc)


def _guard(root: Path, name: str) -> tuple[tuple[int, dict] | None, str | None]:
    """가드레일을 읽는다. 없거나 못 읽어도 끄기는 끝내야 해서 예외 대신 사유를 돌려준다.

    Args:
        root: .blackclip 경로.
        name: 전략 이름.

    Returns:
        ((판, 내용) 또는 None, 못 읽은 사유 또는 None).
    """
    try:
        got = read_guardrail(root, name)
    except (WorkspaceError, OSError) as exc:
        return None, _why(root, exc)
    return got, None if got else "가드레일 파일이 없음"


def _start(recs: list[dict], got: tuple[int, dict] | None) -> date | None:
    """실전 시작일: 기록에서 nums.all.start가 있는 가장 최근 줄의 값(지금 실전의 시작), 없으면 가드레일 값, 못 읽었으면 기록 첫날, 그것도 없으면 None.

    Args:
        recs: 일별 기록.
        got: _guard가 돌려준 (판, 내용).

    Returns:
        날 또는 None.
    """
    for r in reversed(recs):
        if isinstance(a := r["nums"].get("all"), dict) and a.get("start"):
            return date.fromisoformat(a["start"])
    return date.fromisoformat(got[1]["live_start"]) if got else date.fromisoformat(recs[0]["day"]) if recs else None


def _whole(rec: dict) -> dict:
    """한 줄의 실전 시작부터 값(nums.all, 없으면 구간 값 nums).

    Args:
        rec: days.jsonl 줄.

    Returns:
        값 묶음.
    """
    n = rec["nums"]
    return n["all"] if isinstance(n.get("all"), dict) else n


def _ref(recs: list[dict]) -> dict:
    """④ · 거래 수 · 근거 괄호가 읽는 줄: ①과 같은 줄(nums.all이 있는 마지막 기록), 그런 줄이 없으면 마지막 줄.

    Args:
        recs: 비지 않은 일별 기록(날짜 오름차순).

    Returns:
        days.jsonl 줄.
    """
    return next((r for r in reversed(recs) if isinstance(r["nums"].get("all"), dict)), recs[-1])


def lesson(recs: list[dict], unread: bool = False) -> str:
    """④ 다음 점검에서 먼저 볼 것(고정 대응표): 실전 시작부터 값과 전략 경고 날로만 정한다(새 계산 없음).

    Args:
        recs: read_days 결과(날짜 오름차순, 비어 있을 수 있음).
        unread: 일별 기록을 읽지 못했는지(recs는 빈 목록). 기록이 0줄인 것과 다른 글을 낸다.

    Returns:
        한 줄 글. 기록을 읽지 못했으면 「근거 없음(매일 대조 기록을 읽지 못함)」, 기록이 없거나 nums.all이 있는 줄이 없고 마지막 줄에도 숫자가 없으면 「근거 없음(…)」, 잰 숫자가 있는데 차이도 경고도 없으면 「없음(…)」.
    """
    if unread:
        return "근거 없음(매일 대조 기록을 읽지 못함)"
    if not recs:
        return "근거 없음(매일 대조 기록 없음)"
    a = _whole(_ref(recs))
    got = [(k, a[f]) for k, f in (("체결가 차이", "slip"), ("수수료", "fee"), ("펀딩비", "funding")) if a.get(f) is not None]
    miss = a.get("missed_n") or 0
    warn = sum(base(r["states"]["전략"]) == "경고" for r in recs)
    if not (got or miss or warn):
        return "근거 없음(마지막 매일 대조에 숫자 없음)"
    neg = [f"{k} {pp(v)}" for k, v in got if pp(v).startswith("-")]  # pp가 -0.0을 +0.0으로 적으므로 글자로 가름
    parts = ["거래비용 · " + " · ".join(neg)] if neg else []
    if miss:
        parts.append(f"놓친 주문 {miss}건(추정) 나머지 {_pp(a.get('rest'))}")
    if warn:
        parts.append(f"시도 수 · 국면(같은 기간 백테스트가 정상 범위 아래였던 날 {warn}일)")
    return " · ".join(parts) or "없음(잰 차이 · 전략 쪽 경고 없음)"


def _unseen_run(recs: list[dict], start: date | None, stop: date) -> tuple[date, date] | None:
    """미관측 구간: 마지막 기록 다음 날(기록이 없으면 실전 시작일) ~ 끈 날.

    Args:
        recs: 일별 기록.
        start: 실전 시작일(없으면 None).
        stop: 끈 날.

    Returns:
        (처음 날, 끈 날), 없으면 None.
    """
    first = date.fromisoformat(recs[-1]["day"]) + _ONE if recs else start
    return (first, stop) if first is not None and first <= stop else None


def _unseen(recs: list[dict], start: date | None, stop: date) -> str | None:
    """미관측 구간 글(`_unseen_run`을 월/일로).

    Args:
        recs: 일별 기록.
        start: 실전 시작일(없으면 None).
        stop: 끈 날.

    Returns:
        「10/15~10/16」(하루면 「10/15」), 없으면 None.
    """
    run = _unseen_run(recs, start, stop)
    return None if run is None else _span(*run)


def basis(recs: list[dict], live_start: date | None, stop: date, unread: bool = False) -> str:
    """④ 근거 괄호: 실전 기간 · 거래 수 · 끈 날까지 못 본 기간.

    Args:
        recs: read_days 결과.
        live_start: 실전 시작일(가드레일을 못 읽으면 기록 첫날, 기록도 없으면 None).
        stop: 끈 날.
        unread: 일별 기록을 읽지 못했는지. 맞으면 관측된 기간을 알 수 없어 미관측을 적지 않는다.

    Returns:
        거래 수는 ①과 같은 줄(_ref)에서, 끝 날짜는 마지막 줄에서 읽는다. 「(실전 9/23~10/14 · 거래 12건 · 미관측 10/15~10/16)」, 읽지 못했으면 「(실전 9/23~ · 매일 대조 기록을 읽지 못함)」, 기록이 없으면 「(실전 9/23~ · 매일 대조 기록 없음 · 미관측 …)」.
    """
    if recs:
        parts = [f"실전 {_d(live_start)}~{_d(date.fromisoformat(recs[-1]['day']))}", f"거래 {_ref(recs)['trades']}건"]
    else:
        parts = [f"실전 {_d(live_start)}~" if live_start else "실전 시작일 모름",
                 "매일 대조 기록을 읽지 못함" if unread else "매일 대조 기록 없음"]
    if not unread and (un := _unseen(recs, live_start, stop)) is not None:
        parts.append(f"미관측 {un}")
    return f"({' · '.join(parts)})"


def after_line(root: Path, name: str, stop: date) -> str:
    """record 출력의 「다음 점검에서 먼저 볼 것」 뒤 글(④ 한 줄 + 근거 괄호).

    Args:
        root: .blackclip 경로.
        name: 전략 이름.
        stop: 끈 날.

    Returns:
        글. 기록 · 가드레일을 못 읽어도 근거 없음 · 미관측으로 낸다.
    """
    recs, bad = _days(root, name)
    return lesson(recs, bool(bad)) + basis(recs, _start(recs, _guard(root, name)[0]), stop, bool(bad))


def _changes(rows: list[tuple[str, dict]]) -> list[dict]:
    """날짜순 (날, 칸 → 상태)에서 상태 변화를 뽑는다: 첫 줄은 경고 · 확인 불가인 칸만, 그 뒤는 바뀐 칸마다.

    Args:
        rows: 날짜순 (ISO 날, states).

    Returns:
        {"day": ISO 날, "kind": "change", "field": 칸 이름, "before": 앞 상태 | None(첫 줄), "after": 뒤 상태}(상태는 레포 글 그대로).
    """
    out, prev = [], None
    for day, st in rows:
        for k in TRACKED:
            if prev is None and base(st[k]) in ("경고", "확인 불가"):
                out.append({"day": day, "kind": "change", "field": LABELS[k], "before": None, "after": st[k]})
            elif prev is not None and st[k] != prev[k]:
                out.append({"day": day, "kind": "change", "field": LABELS[k], "before": prev[k], "after": st[k]})
        prev = st
    return out


def _utc(text: str) -> datetime:
    """ISO 시각 글을 UTC 시각으로(시간대가 없으면 UTC로 봄).

    Args:
        text: 검사를 지난 ISO 시각.

    Returns:
        UTC 시각.
    """
    t = datetime.fromisoformat(text)
    return t.replace(tzinfo=UTC) if t.tzinfo is None else t.astimezone(UTC)


def _exec_line(x: dict) -> str:
    """① 실행 줄(확인 불가가 아닐 때): 잰 것(체결가 차이 · 수수료 · 펀딩비)과 원인 후보 가름.

    Args:
        x: `after_values`의 one["exec"](kind가 ok일 때): slip · fee · funding · slip_med_bp · market · missed_n · first_missed · rest.

    Returns:
        「- 실행: 잰 것 · … / 원인 후보 …」.
    """
    med = x["slip_med_bp"]
    side = "" if med is None else "(중앙값 " + f"{med:+.1f}bp" + ("" if med == 0 else ", 불리한 쪽" if med < 0 else ", 유리한 쪽") + ")"
    text = f"잰 것 · 체결가 차이 {_pp(x['slip'])}{side} · 수수료 {_pp(x['fee'])}"
    if x["funding"] is not None:
        text += f" · 펀딩비 {pp(x['funding'])}"
    elif x["market"] not in (None, "spot"):
        text += " · 펀딩비 모름(실전 시작부터 합은 첫 30일 구간 안에서만 냄)"
    n = x["missed_n"] or 0
    if not n:
        return f"- 실행: {text} / 원인 후보 없음"
    cause = f"원인 후보(추정) · 놓친 주문 {n}건"
    if (m := x["first_missed"]) is not None:
        t = _utc(m["t"])
        cause += f" · 처음 {t.month}/{t.day} {t:%H:%M} UTC {show(m['symbol'])} " + {"sell": "매도", "buy": "매수"}.get(m["side"], show(m["side"]))
    return f"- 실행: {text} / {cause} · 나머지 {_pp(x['rest'])}"


def after_event(doc: dict) -> tuple[dict, dict | None]:
    """웹 회고 화면의 기준 사건: 끈 뒤 회고가 무엇을 기준으로 하는지를 사건 목록만으로 정한다.

    마지막 사건이 은퇴이고 바로 앞이 중지면 회고 내용 · 날 · 이유는 중지 사건 기준이고 은퇴는 덧붙일 사건으로 따로 돌려준다.
    그 밖에는 마지막 사건이 기준이다. `after.md`가 있는지는 보지 않는다(은퇴 때 파일이 새로 생긴 뒤 판정이 바뀌지 않게).
    `_write_after`는 이 함수를 쓰지 않고 지금 규칙(파일 유무까지 봄) 그대로다. 그래서 `record.md`만 옮긴 노트북처럼 after.md가 없는 채로
    중지 → 은퇴를 하면 엔진이 쓴 after.md(은퇴 기준)와 이 함수의 기준(중지)이 다를 수 있다.

    기준이 중지일 때 `after_values`에 넘길 doc은 호출하는 쪽이 `{**doc, "events": doc["events"][:-1]}`로 만든다(은퇴 사건을 뺌)
    — `after_values`는 doc["events"]의 마지막 사건을 끈 사건으로 본다.

    Args:
        doc: record.md 머리 JSON. 사건이 하나 이상 있어야 한다(끈 전략만 부름).

    Returns:
        (기준 사건, 덧붙일 은퇴 사건 | None). 사건은 doc["events"]의 원소 그대로({"day", "state", "reason", "decision"}).
    """
    ev = doc["events"]
    if len(ev) > 1 and ev[-1]["state"] == "은퇴" and ev[-2]["state"] == "중지":
        return ev[-2], ev[-1]
    return ev[-1], None


def after_values(root: Path, name: str, doc: dict, stop: date) -> dict:
    """끈 뒤 회고 한 장의 값: 터미널 `after_page`와 웹 회고 칸이 같이 읽는다. 새로 계산하지 않고 읽기만 한다.

    days.jsonl · state.log · 가드레일 · record.md 사건을 읽는다. 읽지 못한 것이 있어도 끝까지 만들고 사유를 값에 남긴다.
    이스케이프 규칙: 돌려주는 dict의 글 값은 모두 레포에서 온 원문(또는 그 원문이 섞인 글)이라 화면에 내기 전에 각자 이스케이프한다.
    엔진이 정한 값은 둘뿐이다 — 날("YYYY-MM-DD" 글: day · start · first · last · seen 따위)과 열거 키(one.status · one.exec.kind ·
    two.gaps.status · two.timeline[].kind). 숫자 · 불리언 · None은 글이 아니다. 그 밖의 글(name · top.state · top.c0 · one.why · one.strategy.state/line ·
    one.exec.state/market/first_missed(t · symbol · side) · one.bot.state · two.start.sensitivity · two.guard_why/log_why ·
    two.timeline[].field/before/after/state/decision · reason · four.lesson · notes)은 모두 원문이다. 사유 글(why)은 이 컴퓨터 경로를 .blackclip으로 줄인 한 줄이다.

    Args:
        root: .blackclip 경로.
        name: 전략 이름.
        doc: record.md 머리 JSON. doc["events"]의 마지막 사건이 끈 사건이고(웹은 `after_event`로 정한 기준까지 자른 doc을 넘김), 모든 사건이 ②에 든다.
        stop: 끈 날(끈 사건의 날).

    Returns:
        {"name": 전략 이름,
        "top": {"state": 끈 상태(중지 · 은퇴), "day": 끈 날, "start": 실전 시작일 | None, "trades": 거래 수 | None(기록 없음), "c0": 가드레일 재현성 글 | None},
        "one": {"status": "ok" | "unread"(일별 기록을 못 읽음) | "no_records"(기록 없음) | "no_nums"(nums.all이 있는 줄 없음),
        "why": unread일 때 사유 | None, "actual" · "bt" · "gap": 실전 시작부터 값(ok일 때만, 아니면 None),
        "segments": 구간이 둘 이상일 때 [{"first": 구간 시작, "last": 구간 마지막 기록 날, "actual", "bt", "gap"}], 아니면 [],
        "strategy": 기록을 읽었고 기록이 있으면 {"state": 전략 칸 상태, "hold": 판단 보류라 거래 수만 말하는지, "trades": 마지막 날 거래 수,
        "min_trades": 판단에 필요한 거래 수 | None, "line": 전략 칸 근거 글} 아니면 None,
        "exec": 같은 조건에서 {"kind": "unknown"(확인 불가: "state"만 더 있음) | "no_nums"(숫자 없음) | "ok"(slip · fee · funding · rest: 실전 시작부터 값,
        slip_med_bp: 이번 구간 중앙값, market: 가드레일 시장 | None, missed_n: 놓친 주문 건수, first_missed: 시각이 가장 이른 놓친 주문
        {"t", "symbol", "side"} | None)} 아니면 None,
        "bot": 같은 조건에서 {"state": 봇 칸 상태, "lost": 체결 내역을 못 받은 기간 [{"first", "last"}]} 아니면 None},
        "two": {"start": 가드레일을 읽었으면 {"day": 실전 시작일, "version": 판 번호, "sensitivity": 민감도} 아니면 None,
        "guard_why": 가드레일을 못 읽은 사유 | None, "log_why": state.log를 못 읽은 사유 | None(못 읽으면 일별 기록의 상태로 대신함),
        "timeline": 날짜순(같은 날은 상태 변화가 결정보다 앞) [{"day", "kind": "change", "field": 칸 이름, "before": 앞 상태 | None, "after": 뒤 상태}
        또는 {"day", "kind": "decision", "state": 사건 상태, "decision": 고객 결정 | None}],
        "gaps": {"status": "ok" | "unread" | "no_start"(실전 시작일을 모름), "runs": 기록 없는 날의 이어진 구간 [{"first", "last"}](ok일 때만)}},
        "reason": 끈 이유 원문 | None(미기록),
        "four": {"lesson": 다음 점검에서 먼저 볼 것 한 줄(`lesson`), "basis": 마지막 기록이 있고 실전 시작일을 알면 {"first": 실전 시작일, "last": 마지막 기록 날} 아니면 None,
        "unseen": 못 본 기간 {"first", "last": 끈 날, "after_last": 마지막 기록 뒤인지(False면 실전 시작부터)} | None(없거나 기록을 못 읽음),
        "bot_check": 놓친 주문이 있어 봇 쪽 확인을 적는지},
        "notes": 못 읽은 것 메모(`after_page`가 돌려주는 것과 같음)}.

    Raises:
        OverflowError: 모양 검사는 통과했지만 UTC로 바꿀 수 없는 놓친 주문 시각(0001-01-01T00:00:00+01:00 같은 끝값)이 nums.all.missed에 있고
            ① 실행 줄의 처음 후보를 고를 때(`after_page`와 같음, 이 모듈의 `_utc`는 예외를 바꾸지 않는다).
        ValueError: 같은 시각이 ISO 모양이 아닐 때(days.jsonl 검사가 막으므로 손으로 고친 파일에서만).
    """
    recs, bad_days = _days(root, name)
    got, bad_guard = _guard(root, name)
    start, ev = _start(recs, got), doc["events"][-1]
    notes = [f"매일 대조 기록을 읽지 못함({bad_days})"] if bad_days else []
    if bad_guard:
        notes.append(f"가드레일을 읽지 못함({bad_guard})")
    try:
        rows = sorted(((x["day"], x["states"]) for x in read_jsonl(root, "state.log", 16 << 20)
                       if isinstance(x, dict) and x.get("strategy") == name and _day(x.get("day")) and states_ok(x.get("states"))),
                      key=lambda x: x[0])
        bad_log = None
    except (WorkspaceError, OSError) as exc:  # 상태 줄은 매일 대조 기록의 states로 대신(날마다 한 줄)
        rows, bad_log = [(r["day"], r["states"]) for r in recs], _why(root, exc)
        notes.append(f"state.log를 읽지 못함({bad_log})")
    last = recs[-1] if recs else None
    whole = next((r for r in reversed(recs) if isinstance(r["nums"].get("all"), dict)), None)
    checks = got[1].get("checks") if got else None  # 공용 가드레일 검사가 안 보는 칸이라 모양을 여기서 확인
    c0 = checks.get("C0") if isinstance(checks, dict) else None
    top = {"state": ev["state"], "day": stop.isoformat(), "start": start.isoformat() if start else None,
           "trades": _ref(recs)["trades"] if last else None, "c0": c0 if isinstance(c0, str) else None}
    # ① 실전 기간 전체
    one: dict = {"status": "ok", "why": None, "actual": None, "bt": None, "gap": None, "segments": [], "strategy": None, "exec": None, "bot": None}
    if bad_days:
        one.update(status="unread", why=bad_days)
    elif last is None:
        one["status"] = "no_records"
    elif whole is None:
        one["status"] = "no_nums"
    else:
        a = whole["nums"]["all"]
        one.update(actual=a.get("actual"), bt=a.get("bt"), gap=a.get("gap"))
        if a.get("start") and a["start"] != whole["nums"].get("period_start"):  # 구간이 둘 이상: 구간마다 마지막 기록의 구간 값
            seg = {r["nums"]["period_start"]: r for r in recs if r["nums"].get("period_start")}
            one["segments"] = [{"first": p, "last": r["day"], "actual": r["nums"].get("actual"), "bt": r["nums"].get("bt"), "gap": r["nums"].get("gap")}
                               for p, r in seg.items()]
    if last is not None and not bad_days:
        st, mt = last["states"], last["nums"].get("min_trades")
        one["strategy"] = {"state": st["전략"], "hold": base(st["전략"]) == "판단 보류" and mt is not None and last["trades"] < mt,
                           "trades": last["trades"], "min_trades": mt, "line": last["lines"]["전략"]}
        if base(st["실행"]) == "확인 불가":
            one["exec"] = {"kind": "unknown", "state": st["실행"]}
        elif whole is None:
            one["exec"] = {"kind": "no_nums"}
        else:
            a, missed = whole["nums"]["all"], whole["nums"]["all"].get("missed")
            one["exec"] = {"kind": "ok", "slip": a.get("slip"), "fee": a.get("fee"), "funding": a.get("funding"), "rest": a.get("rest"),
                           "slip_med_bp": whole["nums"].get("slip_med_bp"), "market": got[1]["target"]["market"] if got else None,
                           "missed_n": a.get("missed_n"),
                           "first_missed": min(missed, key=lambda x: _utc(x["t"])) if (a.get("missed_n") or 0) and missed else None}
        lost = _runs([date.fromisoformat(r["day"]) for r in recs if base(r["states"]["봇"]) == "확인 불가"])
        one["bot"] = {"state": st["봇"], "lost": [{"first": a.isoformat(), "last": b.isoformat()} for a, b in lost]}
    # ② 그 기간의 경고와 고객 결정
    timeline = [*_changes(rows), *({"day": e["day"], "kind": "decision", "state": e["state"], "decision": e["decision"]} for e in doc["events"])]
    timeline.sort(key=lambda m: (m["day"], m["kind"] == "decision"))
    if bad_days:
        gaps: dict = {"status": "unread", "runs": []}
    elif start is None:
        gaps = {"status": "no_start", "runs": []}
    else:
        have, end = {r["day"] for r in recs}, date.fromisoformat(last["day"]) if last else stop
        gap = [start + timedelta(n) for n in range((end - start).days + 1) if (start + timedelta(n)).isoformat() not in have]
        gaps = {"status": "ok", "runs": [{"first": a.isoformat(), "last": b.isoformat()} for a, b in _runs(gap)]}
    two = {"start": {"day": start.isoformat(), "version": got[0], "sensitivity": got[1]["warning_line"]["sensitivity"]} if got and start else None,
           "guard_why": bad_guard, "log_why": bad_log, "timeline": timeline, "gaps": gaps}
    # ④ 다음 점검에서 먼저 볼 것
    run = None if bad_days else _unseen_run(recs, start, stop)
    four = {"lesson": lesson(recs, bool(bad_days)),
            "basis": {"first": start.isoformat(), "last": last["day"]} if last and start else None,
            "unseen": None if run is None else {"first": run[0].isoformat(), "last": run[1].isoformat(), "after_last": bool(last)},
            "bot_check": bool(last and (_whole(last).get("missed_n") or 0))}
    return {"name": name, "top": top, "one": one, "two": two, "reason": ev["reason"] or None, "four": four, "notes": notes}


def _after_text(name: str, v: dict) -> str:
    """`after_values`의 값을 끈 뒤 회고 한 장(after.md) 글로 짠다. 값만 읽는다(파일 · 시각 없음).

    Args:
        name: 전략 이름(글머리).
        v: `after_values` 결과(JSON을 거쳐도 됨).

    Returns:
        after.md 글(끝 줄바꿈 포함).
    """
    t, o, w, f = v["top"], v["one"], v["two"], v["four"]
    top = [f"실전 {t['start'] or '모름'} ~ {t['day'][5:]}({t['state']})", f"거래 {t['trades']}건" if t["trades"] is not None else "거래 모름",
           "거래소 조회 키로 매일 받은 체결 내역으로 계산"]
    if t["c0"] is not None:
        top.append(show(C0_WORDS.get(t["c0"], f"재현성 {t['c0']}")))
    one: list[str]
    if o["status"] == "unread":
        one = [f"- 매일 대조 기록을 읽지 못함({o['why']}) · 근거 없음"]
    elif o["status"] == "no_records":
        one = ["- 근거 없음(매일 대조 기록 없음 — 숫자를 쓰지 않음)"]
    elif o["status"] == "no_nums":
        one = ["- 근거 없음(매일 대조에 숫자 없음 — 숫자를 쓰지 않음)"]
    else:
        one = [f"- 실제 {_pct(o['actual'])} · 같은 기간 백테스트 {_pct(o['bt'])} · 차이 {_pp(o['gap'])}"]
        if o["segments"]:
            one.append("- 구간별(보조): " + " / ".join(
                f"{_span(date.fromisoformat(g['first']), date.fromisoformat(g['last']))} 실제 {_pct(g['actual'])} · "
                f"백테스트 {_pct(g['bt'])} · 차이 {_pp(g['gap'])}" for g in o["segments"]))
    if (s := o["strategy"]) is not None:
        one.append(f"- 전략: {_sym(s['state'])} · " + (f"거래 {s['trades']}건({s['min_trades']}건 전이라 판단하지 않음)" if s["hold"]
                                                      else show(s["line"]).replace("같은 기간 BT", "같은 기간 백테스트")))  # 「BT」는 터미널만
        x = o["exec"]
        one.append(f"- 실행: {_sym(x['state'])}" if x["kind"] == "unknown" else "- 실행: 근거 없음(매일 대조에 숫자 없음)" if x["kind"] == "no_nums"
                   else _exec_line(x))
        b = o["bot"]
        one.append(f"- 봇: {_sym(b['state'])} · " + ("못 받은 기간 " + " · ".join(_span(date.fromisoformat(r["first"]), date.fromisoformat(r["last"]), True)
                                                                         for r in b["lost"]) if b["lost"] else "체결 내역 못 받은 날 없음"))
    # ② 그 기간의 경고와 고객 결정
    two = [f"- {_d(date.fromisoformat(w['start']['day']), True)} 실전 시작 · 가드레일 v{w['start']['version']} · 민감도 {show(w['start']['sensitivity'])}"
           if w["start"] else f"- 가드레일을 읽지 못함({w['guard_why']}) · 실전 시작일 · 판 모름"]
    if w["log_why"]:
        two.append(f"- state.log를 읽지 못함({w['log_why']}) · 매일 대조 기록으로만 씀")
    for m in w["timeline"]:
        md = _d(date.fromisoformat(m["day"]), True)
        if m["kind"] == "decision":
            two.append(f"- {md} {m['state']} · 고객 결정" + (f" · 결정: {show(m['decision'])}" if m["decision"] else ""))
        else:
            two.append(f"- {md} {m['field']} " + (f"{show(m['before'])} → " if m["before"] is not None else "") + show(m["after"]))
    g = w["gaps"]
    if g["status"] == "unread":
        two.append("- 기록 없는 날: 모름(매일 대조 기록을 읽지 못함)")
    elif g["status"] == "no_start":
        two.append("- 기록 없는 날: 모름(실전 시작일을 모름)")
    else:
        runs = [(date.fromisoformat(r["first"]), date.fromisoformat(r["last"])) for r in g["runs"]]
        two.append("- 기록 없는 날: " + (" · ".join(_span(a, b, True) + (f"({(b - a).days + 1}일)" if a != b else "") for a, b in runs) or "없음"))
    # ④ 다음 점검에서 먼저 볼 것
    four = [f["lesson"] + (f"({_span(date.fromisoformat(f['basis']['first']), date.fromisoformat(f['basis']['last']))} 일별 기록)" if f["basis"] else "")]
    if (un := f["unseen"]) is not None:
        four.append(f"미관측 {_span(date.fromisoformat(un['first']), date.fromisoformat(un['last']))}("
                    + ("마지막 일별 기록 뒤 끈 날까지)" if un["after_last"] else "실전 시작부터 끈 날까지)"))
    if f["bot_check"]:
        four.append("봇 쪽 확인(점검 항목 아님): 백테스트에만 있는 거래가 왜 없었는지 봇 기록에서 확인")
    page = ["# 끈 뒤 회고 · " + name, "", " · ".join(top), "", "## ① 실전 기간 전체", *one, "", "## ② 그 기간의 경고와 고객 결정", *two, "",
            "## ③ 끈 이유(고객)", show(v["reason"] or "이유 미기록"), "", "## ④ 다음 점검에서 먼저 볼 것", *four, "",
            "엔진은 숫자와 점검 항목만 써요. 설명 문장은 고객의 AI가 만들어요. 실행 쪽 원인 후보는 추정이에요."]
    return "\n".join(page) + "\n"


def after_page(root: Path, name: str, doc: dict, today: date) -> tuple[str, list[str]]:
    """끈 뒤 회고 한 장(after.md) 글: ① 실전 기간 전체 · ② 경고와 고객 결정 · ③ 끈 이유 · ④ 다음 점검에서 먼저 볼 것.

    값은 `after_values`가 읽고 이 함수는 글로 짠다(숫자 · 상태는 새로 계산하지 않음). 읽지 못한 것이 있어도 끝까지 만들고
    그 자리에 읽지 못함 · 근거 없음을 적는다.

    Args:
        root: .blackclip 경로.
        name: 전략 이름.
        doc: record.md 머리 JSON(끈 사건이 마지막 사건).
        today: 끈 날(마지막 사건의 날).

    Returns:
        (after.md 글, 못 읽은 것 메모).
    """
    v = after_values(root, name, doc, today)
    return _after_text(name, v), v["notes"]


def lessons(root: Path, exclude: str) -> list[str]:
    """다음 check 출력 끝 줄: 끈 전략의 ④를 최근 끈 순서로 최대 3개(record.md만 읽음).

    Args:
        root: .blackclip 경로.
        exclude: 빼는 전략(지금 점검하는 전략).

    Returns:
        머리 + 「- 전략(MM/DD 상태): ④」 줄들, 보여 줄 것이 없으면 빈 목록. record.md를 못 읽는 전략은 건너뜀.
    """
    try:
        names = strategy_names(root)
    except (WorkspaceError, OSError):
        return []
    rows = []
    for n in names:
        try:
            got = read_record(root, n) if n != exclude and _NAME.fullmatch(n) else None
        except RecordError:
            continue
        if got and got[0]["events"] and got[0]["lesson"] is not None and (since := _since(got[0]["events"]))[0] != "실전":
            rows.append((since[1], n, since[0], got[0]["lesson"]))
    rows.sort(key=lambda r: (-date.fromisoformat(r[0]).toordinal(), r[1]))
    return ["지난 전략에서 먼저 볼 것(최근 3개까지):",
            *(f"- {n}({day[5:7]}/{day[8:10]} {state}): {show(text)}" for day, n, state, text in rows[:3])] if rows else []


def _write_after(root: Path, name: str, doc: dict, state: str, stop: date) -> None:
    """after.md를 쓴다. 은퇴로 바뀔 때 바로 앞 사건이 중지이고 그 회고가 일반 파일로 있으면 은퇴 줄만 덧붙이고(그 줄이 이미 있으면 그대로),
    그 밖(실전 → 은퇴 · 중지 → 실전 → 은퇴)이면 새 회고를 쓴다. 덧붙일 회고를 읽지 못하면(상한 넘음 등) 건드리지 않는다.

    Args:
        root: .blackclip 경로.
        name: 전략 이름.
        doc: 끈 사건까지 담은 머리 JSON.
        state: 바뀐 상태(중지 · 은퇴).
        stop: 끈 날.

    Raises:
        WorkspaceError: 상위 폴더 링크 · 덧붙일 자리가 링크 · FIFO.
        OSError: 쓰지 못함(같은 명령을 다시 하면 이어서 마침).
    """
    rel = f"strategies/{name}/{AFTER}"
    ev = doc["events"]
    if state == "은퇴" and len(ev) > 1 and ev[-2]["state"] == "중지" and exists_regular(root, rel):  # 바로 앞이 중지일 때만 그 회고에 덧붙임
        try:
            old = read_bytes(root, rel, 1 << 20).decode("utf-8", "replace")
        except (WorkspaceError, OSError):  # 읽을 수 없으면(링크 · 상한 넘음) 새로 쓰지도 덧붙이지도 않고 그대로 둠(고객이 덧붙인 글 보존)
            return
        if not _RETIRE_LINE.search(old):
            append_line(root, rel, f"\n{stop:%m/%d} 은퇴 · 고객 결정 · 이유: {show(doc['events'][-1]['reason'] or '이유 미기록')}\n")
        return
    write_in(root, rel, after_page(root, name, doc, stop)[0])


def write_record(root: Path, name: str, state: str, reason: str | None, decision: str | None,
                 today: date) -> tuple[str, list[str]]:
    """전략 상태를 기록한다. 잠금 안에서 (중지 · 은퇴로 바뀌면 after.md →) record.md → state.log 순으로 쓰고,
    같은 요청을 다시 하면 빠진 단계만 마친다. 중지 · 은퇴로 바뀔 때 ④(lesson)를 record.md에 함께 적는다.
    일별 기록 · 가드레일 · state.log를 못 읽어도 끄기는 끝낸다(after.md에 근거 없음으로 적음).

    Args:
        root: .blackclip 경로(전략 폴더가 이미 있어야 함).
        name: 전략 이름.
        state: STATES 중 하나.
        reason: 바꾼 이유(없으면 「이유 미기록」으로 적음).
        decision: 고객 결정.
        today: 사건 날.

    Returns:
        (앞 상태 · 기록 없음은 「실전」, 이어서 마친 단계 목록). 바뀐 것도 빠진 것도 없으면 ("같음", []).
        같은 요청이라 새 사건 없이 빠진 단계만 했으면 (상태, 단계 목록).

    Raises:
        RecordError: 이름 · 글 모양 · 은퇴에서 되돌림 · 머리를 읽지 못함 · 상한 넘음(아무것도 쓰지 않음).
        WorkspaceError: 쓰기 단계의 링크 · FIFO · 잠금 중.
        OSError: 쓰기 단계의 디스크 문제(같은 명령을 다시 하면 이어서 마침).
    """
    if not _NAME.fullmatch(name) or state not in STATES:
        raise RecordError("전략 이름이나 상태가 맞지 않습니다")
    reason, decision = _clean("--reason", reason), _clean("--decision", decision)
    with lock(root, "record"):
        try:
            doc, memo = read_record(root, name) or ({"events": [], "lesson": None}, "")
        except RecordError:
            raise RecordError(HEAD_BROKEN) from None
        ev = doc["events"]
        last = ev[-1] if ev else None
        prev = last["state"] if last else "실전"
        if prev == "은퇴" and state != "은퇴":
            raise RecordError(RETIRED)
        new = {"day": today.isoformat(), "state": state, "reason": reason, "decision": decision}
        same = last is not None and (last["state"], last["reason"], last["decision"]) == (state, reason, decision)
        if (state != prev or reason or decision) and not same:  # 같은 요청의 반복은 사건을 늘리지 않음
            off = state not in ("실전", prev)  # 중지 · 은퇴로 바뀜: 회고 한 장과 ④를 새로
            doc = {**doc, "events": [*ev, new]}
            if off:
                recs, bad = _days(root, name)
                doc["lesson"] = lesson(recs, bool(bad))
            text = render_record(name, doc, memo)
            if len(text.encode("utf-8")) > CAP or len(doc["events"]) > MAX_EVENTS:
                raise RecordError(TOO_BIG)
            if off:
                _write_after(root, name, doc, state, today)  # after.md → record.md → state.log
            write_in(root, f"strategies/{name}/{RECORD}", text)
            if state != prev:
                append_line(root, "state.log", _log_line(name, new))
            return prev, []
        if last is None:
            return "같음", []
        items: list[str] = []
        if last["state"] != "실전" and not exists_regular(root, f"strategies/{name}/{AFTER}"):  # 회고가 빠졌으면 다시 만듦
            _write_after(root, name, doc, last["state"], date.fromisoformat(last["day"]))
            items.append("after.md")
        before = ev[-2]["state"] if len(ev) > 1 else "실전"
        if last["state"] != before and not _logged(root, name, last):  # 상태가 그대로인 메모 사건은 복구 대상이 아님
            append_line(root, "state.log", _log_line(name, last))
            items.append("state.log")
        return (state, items) if items else ("같음", [])


def _log_line(name: str, ev: dict) -> str:
    """state.log에 덧붙일 한 줄(daily의 states 줄과 섞이지 않게 「record」 칸).

    Args:
        name: 전략 이름.
        ev: 사건.

    Returns:
        JSON 한 줄(줄 끝 포함).
    """
    return json.dumps({"day": ev["day"], "record": ev["state"], "strategy": name}, ensure_ascii=False, sort_keys=True) + "\n"

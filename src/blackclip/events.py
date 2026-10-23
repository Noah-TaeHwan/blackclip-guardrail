"""키 없는 감시의 입력: 봇이 이미 남기는 로그를 표준 이벤트 형식으로 바꿔 쌓는다.

봇 로그를 이벤트로 바꾸는 코드(어댑터 `.blackclip/strategies/<전략>/adapter.py`)는 고객의 Claude Code가 만든다. 엔진은
- 어댑터를 실행하지 않고 읽어(ast) 맨 위의 `SOURCE = "<봇 로그 경로>"`만 꺼내고,
- 그 로그를 지난번 읽은 자리부터 줄 단위로(끝 줄바꿈이 있는 줄만) 읽어,
- 어댑터를 하위 프로세스(환경 변수 비움, 점검의 연결 코드와 같은 규칙)로 띄워 표준 입력으로 그 줄들을 넘기고,
- 표준 출력의 JSON 줄을 이벤트 형식으로 확인한 뒤 `events.jsonl`에 덧붙이고, 그다음에 읽은 자리를 적는다.
봇 로그와 봇 코드는 읽기만 한다. 덧붙인 뒤 자리를 적기 전에 죽으면 다음에 같은 줄을 한 번 더 넣는다(빠뜨리지는 않음).
"""
from __future__ import annotations

import ast
import json
import os
import re
import signal
import stat
import subprocess
import sys
from datetime import datetime, timedelta, timezone
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any

from blackclip.workspace import WorkspaceError, append_line, exists_regular, read_json, write_atomic

NAME = re.compile(r"[A-Za-z0-9][A-Za-z0-9_-]{0,63}")
"""전략 이름(check.link.NAME과 같음, pandas를 부르지 않으려고 여기 한 벌)."""

ADAPTER, EVENTS, STATE = "adapter.py", "events.jsonl", "events.state.json"
"""전략 폴더 안 파일 이름: 어댑터(고객 AI가 만듦) · 쌓인 이벤트 · 읽은 자리와 합계."""

REQUIRED = {"fill": ("symbol", "side", "qty", "price"), "position": ("symbol", "qty"), "equity": ("value",),
            "heartbeat": (), "decision": ()}
"""이벤트 종류 → time 말고 꼭 있어야 하는 칸. 그 밖의 칸(fee · fee_asset · order_id · currency · mark_price · seen_price · want)은 있으면 그대로 둔다."""

NUMBERS = ("qty", "price", "value", "fee", "mark_price", "seen_price")
"""있으면 유한한 숫자(문자열 또는 수)여야 하는 칸."""

READ_CAP = 32 << 20
"""어댑터에 한 번에 넘기는 봇 로그 바이트 상한. 한 확인 안에서 이 크기씩 끝까지 따라잡고 그다음에 판정한다.
shortcut: 몇 GB짜리 첫 로그는 그 한 번의 확인이 길어진다(그동안 다음 확인은 잠금으로 건너뜀), 그런 봇이 나오면 처음 붙일 때 끝 쪽부터."""

TIMEOUT_S = 60.0
"""어댑터 한 번을 기다리는 초."""

FUTURE = timedelta(minutes=10)
"""이 시각보다 앞선(미래) 이벤트는 형식 오류로 센다(시계가 틀린 줄 하나가 「안 보임」을 영영 막지 않게)."""

ENV: dict[str, str] = {"PYTHONIOENCODING": "utf-8", "PYTHONUTF8": "1", "PYTHONDONTWRITEBYTECODE": "1"}
"""어댑터에 넘기는 환경 변수 전부(check.runner.ENV와 같은 값, 키 · 토큰이 든 엔진 환경을 넘기지 않는다)."""


class EventsError(ValueError):
    """어댑터 · 봇 로그 · 상태 파일을 읽거나 돌리지 못했을 때(한 줄, 확인 불가의 이유로 쓴다)."""


def folder(root: Path, name: str) -> Path:
    """전략 폴더(이름 확인).

    Args:
        root: .blackclip 경로.
        name: 전략 이름.

    Returns:
        .blackclip/strategies/<전략>.

    Raises:
        EventsError: 이름 모양이 다를 때.
    """
    if not NAME.fullmatch(name):
        raise EventsError(f"전략 이름: 영문 · 숫자 · 밑줄 · 하이픈 64자까지입니다({name})")
    return root / "strategies" / name


def source_path(root: Path, name: str) -> Path:
    """어댑터 맨 위의 `SOURCE = "<경로>"`를 실행하지 않고 읽는다. 상대 경로는 레포 꼭대기 기준.

    Args:
        root: .blackclip 경로.
        name: 전략 이름.

    Returns:
        봇 로그 절대 경로.

    Raises:
        EventsError: 어댑터가 없거나 · 링크이거나 · 문법 오류이거나 · SOURCE가 문자열 하나가 아닐 때.
    """
    rel = f"strategies/{name}/{ADAPTER}"
    if not exists_regular(root, rel):
        raise EventsError(f".blackclip/{rel}가 없습니다 · 고객 AI가 이 자리에 어댑터(SOURCE + 표준 입력 → 이벤트 JSON 줄)를 만듭니다")
    try:
        tree = ast.parse((root / rel).read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, SyntaxError, ValueError) as exc:
        raise EventsError(f".blackclip/{rel}: 읽지 못했습니다({type(exc).__name__})") from None
    for node in tree.body:
        if (isinstance(node, ast.Assign) and len(node.targets) == 1 and isinstance(node.targets[0], ast.Name)
                and node.targets[0].id == "SOURCE"):
            if isinstance(node.value, ast.Constant) and isinstance(node.value.value, str) and node.value.value:
                p = Path(node.value.value).expanduser()
                return p if p.is_absolute() else root.parent / p
            break
    raise EventsError(f".blackclip/{rel}: 맨 위에 SOURCE = \"<봇 로그 경로>\" 한 줄이 있어야 합니다")


def _utc(text: Any) -> datetime | None:
    """ISO 8601 시각 → UTC. 문자열이 아니거나 · 모양이 다르거나 · 시간대가 없거나(어느 시각인지 모름) · UTC로 못 바꾸는 범위면 None.

    Args:
        text: 시각 글.

    Returns:
        UTC 시각 또는 None.
    """
    if not isinstance(text, str):
        return None
    try:
        t = datetime.fromisoformat(text.replace("Z", "+00:00"))
        return t.astimezone(timezone.utc) if t.tzinfo is not None else None
    except (ValueError, OverflowError):  # OverflowError: 0001-01-01T00:00:00+09:00처럼 UTC가 1년 앞
        return None


def check_event(obj: Any, now: datetime) -> dict[str, Any] | None:
    """어댑터 출력 한 줄(JSON 값)을 이벤트 형식으로 확인하고 시각을 UTC `...Z`로 맞춘다.

    Args:
        obj: JSON 값.
        now: 지금(UTC). 이보다 FUTURE 넘게 앞선 시각은 오류.

    Returns:
        맞춘 이벤트, 형식이 다르면 None.
    """
    if not isinstance(obj, dict) or not isinstance(obj.get("kind"), str) or obj["kind"] not in REQUIRED:
        return None
    t = _utc(obj.get("time"))
    if t is None or t > now + FUTURE:
        return None
    if any(k not in obj for k in REQUIRED[obj["kind"]]):
        return None
    for k in NUMBERS:
        if k in obj:
            try:
                if isinstance(obj[k], bool) or not Decimal(str(obj[k])).is_finite():
                    return None
            except InvalidOperation:
                return None
    if obj["kind"] == "fill" and obj["side"] not in ("buy", "sell"):
        return None
    return {**obj, "time": t.isoformat().replace("+00:00", "Z")}


def load_state(root: Path, name: str) -> dict[str, Any]:
    """읽은 자리와 합계. 없으면 처음부터.

    Args:
        root: .blackclip 경로.
        name: 전략 이름.

    Returns:
        {"dev", "ino", "offset", "counts": 종류 → 수, "bad", "last": 마지막 이벤트 시각 또는 None}.

    Raises:
        EventsError: 파일이 깨졌거나 모양이 다를 때(자리를 모르면 이벤트를 두 번 넣을 수 있어 멈춘다).
    """
    blank = {"dev": None, "ino": None, "offset": 0, "counts": {}, "bad": 0, "last": None}
    try:
        doc = read_json(root, f"strategies/{name}/{STATE}")
    except FileNotFoundError:
        return blank
    except WorkspaceError as exc:
        raise EventsError(f"읽은 자리 파일을 읽지 못했습니다({exc})") from None
    if (not isinstance(doc, dict) or not isinstance(doc.get("offset"), int) or not isinstance(doc.get("counts"), dict)
            or not isinstance(doc.get("bad", 0), int) or not all(isinstance(v, int) for v in doc["counts"].values())):
        raise EventsError(f".blackclip/strategies/{name}/{STATE}: 모양이 다릅니다 · 지우면 봇 로그를 처음부터 다시 읽습니다")
    return {**blank, **doc}


def _run_adapter(root: Path, name: str, data: bytes) -> list[str]:
    """어댑터를 하위 프로세스로 한 번 돌린다(cwd = 레포 꼭대기, 환경 변수 ENV만, -P로 레포 폴더를 import 경로 앞에 두지 않음).

    Args:
        root: .blackclip 경로.
        name: 전략 이름.
        data: 표준 입력으로 넘길 봇 로그 줄들.

    Returns:
        표준 출력 줄들.

    Raises:
        EventsError: 시간 초과 · 0이 아닌 종료 코드 · 띄우지 못함.
    """
    path = folder(root, name) / ADAPTER
    try:
        proc = subprocess.Popen([sys.executable, "-P", str(path)], cwd=root.parent, env=ENV, stdin=subprocess.PIPE,
                                stdout=subprocess.PIPE, stderr=subprocess.PIPE, start_new_session=True)
    except OSError as exc:
        raise EventsError(f"어댑터를 띄우지 못했습니다({type(exc).__name__})") from None
    try:
        out, err = proc.communicate(data, timeout=TIMEOUT_S)
    except subprocess.TimeoutExpired:
        os.killpg(proc.pid, signal.SIGKILL)  # 어댑터가 띄운 손자 프로세스까지
        proc.communicate()
        raise EventsError(f"어댑터가 {TIMEOUT_S:.0f}초 안에 끝나지 않았습니다") from None
    if proc.returncode:
        tail = next((ln for ln in reversed(err.decode("utf-8", "replace").splitlines()) if ln.strip()), "")[:160]
        raise EventsError(f"어댑터가 실패했습니다(종료 {proc.returncode}){' · ' + tail if tail else ''}")
    return out.decode("utf-8", "replace").splitlines()


def ingest(root: Path, name: str, now: datetime) -> dict[str, Any]:
    """봇 로그의 새 줄을 어댑터로 바꿔 events.jsonl에 덧붙이고 읽은 자리를 적는다. 부르는 쪽이 lock(root, "follow")을 잡는다.

    봇 로그가 다른 파일로 바뀌었거나(dev · inode) 읽은 자리보다 짧아졌으면(비우고 다시 씀) 처음부터 읽는다.

    Args:
        root: .blackclip 경로.
        name: 전략 이름.
        now: 지금(UTC).

    Returns:
        새 상태 + "source"(봇 로그 경로) · "new_lines"(이번에 넘긴 줄 수) · "new_events" · "new_bad".

    Raises:
        EventsError: 어댑터 · 봇 로그 · 상태 파일 문제(한 줄).
    """
    src = source_path(root, name)
    state = load_state(root, name)
    try:
        st = os.stat(src)
    except OSError as exc:
        raise EventsError(f"봇 로그를 찾지 못했습니다({src} · {exc.strerror})") from None
    if not stat.S_ISREG(st.st_mode):
        raise EventsError(f"봇 로그가 일반 파일이 아닙니다({src})")
    offset = state["offset"]
    # shortcut: 같은 파일을 비우고 읽은 자리 넘게 다시 쓰면 그사이 줄을 놓친다(Filebeat도 같은 한계), 그런 봇이 나오면 자리 앞 바이트 지문으로
    if (state["dev"], state["ino"]) != (st.st_dev, st.st_ino) or st.st_size < offset:
        offset = 0
    prev = _utc(state["last"])  # 시계가 되돌아가 지금 + FUTURE보다 미래인 값(또는 모양이 틀린 값)은 버림: 「안 보임」을 막지 않게
    counts, last = dict(state["counts"]), state["last"] if prev is not None and prev <= now + FUTURE else None
    lines = events = bad = 0

    def save() -> dict[str, Any]:
        new = {"dev": st.st_dev, "ino": st.st_ino, "offset": offset, "counts": counts, "bad": state["bad"] + bad,
               "last": last, "read_at": now.isoformat().replace("+00:00", "Z")}
        write_atomic(folder(root, name) / STATE, json.dumps(new, ensure_ascii=False, indent=2, sort_keys=True) + "\n")
        return new

    try:
        f = open(src, "rb")
    except OSError as exc:
        raise EventsError(f"봇 로그를 읽지 못했습니다({src} · {exc.strerror})") from None
    limit = st.st_size  # 이번 확인의 끝: 읽는 사이 덧붙은 줄은 다음 확인에서(계속 쓰는 봇에도 돌아오게)
    with f:
        while offset < limit:  # READ_CAP씩 시작 크기까지: 다 따라잡기 전의 마지막 시각으로 판정하지 않게
            try:
                f.seek(offset)
                raw = f.read(min(READ_CAP, limit - offset))
                cut = raw.rfind(b"\n") + 1  # 끝 줄바꿈이 없는 줄은 봇이 아직 쓰는 중일 수 있어 다음에
                end = _line_end(f, limit) if not cut and len(raw) == READ_CAP else None
            except OSError as exc:
                raise EventsError(f"봇 로그를 읽지 못했습니다({src} · {exc.strerror})") from None
            if cut:
                good, b = _convert(root, name, raw[:cut], now)
                if good:
                    append_line(root, f"strategies/{name}/{EVENTS}",
                                "".join(json.dumps(e, ensure_ascii=False, sort_keys=True) + "\n" for e in good))
                for e in good:
                    counts[e["kind"]] = counts.get(e["kind"], 0) + 1
                last = max([e["time"] for e in good] + ([last] if last else []), default=None)
                lines, events, bad, offset = lines + raw[:cut].count(b"\n"), events + len(good), bad + b, offset + cut
            elif end is not None:  # 줄바꿈 없이 READ_CAP을 넘는 줄: 형식 오류 하나로 세고 건너뜀
                offset, bad = end, bad + 1
            else:  # 다 읽음 · 끝은 아직 안 끝난 줄(READ_CAP을 넘는 줄이면 끝날 때까지 기다림)
                break
            save()  # 조각마다 적어 둠: 큰 로그를 읽다 죽어도 거기까지는 다시 넣지 않음
    return {**save(), "source": str(src), "new_lines": lines, "new_events": events, "new_bad": bad}


def _line_end(f: Any, limit: int) -> int | None:
    """파일의 지금 자리부터 limit 안에서 다음 줄바꿈 바로 뒤 자리.

    Args:
        f: 바이너리로 연 파일.
        limit: 이 자리까지만 본다(확인을 시작할 때의 크기).

    Returns:
        자리, 그 안에 줄바꿈이 없으면 None.
    """
    pos = f.tell()
    while pos < limit and (blk := f.read(min(1 << 20, limit - pos))):
        if (i := blk.find(b"\n")) >= 0:
            return pos + i + 1
        pos += len(blk)
    return None


def _convert(root: Path, name: str, chunk: bytes, now: datetime) -> tuple[list[dict[str, Any]], int]:
    """끝난 줄들을 어댑터로 바꿔 이벤트 형식을 확인한다.

    Args:
        root: .blackclip 경로.
        name: 전략 이름.
        chunk: 줄바꿈으로 끝나는 봇 로그 바이트.
        now: 지금(UTC).

    Returns:
        (맞는 이벤트들, 형식 오류 수).

    Raises:
        EventsError: 어댑터 실패.
    """
    good, bad = [], 0
    for line in _run_adapter(root, name, chunk):
        if not line.strip():
            continue
        try:
            ev = check_event(json.loads(line), now)
        except (ValueError, RecursionError):
            ev = None
        if ev is None:
            bad += 1
        else:
            good.append(ev)
    return good, bad


def ago(last: str, now: datetime) -> str:
    """마지막 이벤트에서 지금까지(「3시간 5분」 꼴).

    Args:
        last: UTC 시각(...Z).
        now: 지금(UTC).

    Returns:
        글.
    """
    m = max(int((now - datetime.fromisoformat(last.replace("Z", "+00:00"))).total_seconds() // 60), 0)
    return f"{m // 60}시간 {m % 60}분" if m >= 60 else f"{m}분"


def summary(name: str, got: dict[str, Any], now: datetime) -> list[str]:
    """「이벤트 확인」 화면 줄: 무엇이 들어오는지 · 빠진 종류 · 마지막 시각 · 형식 오류.

    Args:
        name: 전략 이름.
        got: ingest 결과.
        now: 지금(UTC).

    Returns:
        화면 줄.
    """
    c = got["counts"]
    kinds = " · ".join(f"{k} {c[k]}" for k in REQUIRED if c.get(k)) or "없음"
    missing = [k for k in ("fill", "position", "equity", "heartbeat") if not c.get(k)]
    out = [f"이벤트 확인 · {name} · 봇 로그 {got['source']}",
           f"이번에 읽은 줄 {got['new_lines']} · 새 이벤트 {got['new_events']} · 형식 오류 {got['new_bad']}",
           f"쌓인 이벤트: {kinds} · 형식 오류 모두 {got['bad']}"]
    if missing:
        out.append(f"빠진 종류: {' · '.join(missing)} · 없는 종류로 보는 선(포지션 크기 · 손실폭 · 거래 빈도)은 아직 못 봄")
    if got["last"]:
        out.append(f"마지막 이벤트: {got['last'][:16].replace('T', ' ')} UTC ({ago(got['last'], now)} 전)")
        out.append(f"다음: 선을 정해 승인하면 blackclip follow {name} --quiet-minutes <분>(이벤트가 그만큼 없으면 「봇이 안 보여요」)")
    else:
        out.append("마지막 이벤트: 없음 · 어댑터가 이벤트를 내지 않았습니다(봇 로그 줄 모양과 어댑터를 확인)")
    return out

"""blackclip follow: 키 없이 지켜보는 시계. 봇 로그를 이벤트로 받아 「봇이 안 보여요」를 판정하고, 상태가 바뀌면 텔레그램.

선은 고객이 승인한 「이벤트가 N분 넘게 없으면」 하나(`follow.json`). 예약 작업이 5분마다 부른다.
상태는 셋: 정상 · 안 보임 · 확인 불가(어댑터 · 봇 로그를 읽지 못함 — 안 보임과 섞지 않는다).
보내는 규칙은 자주 보는 시계와 같다: 바뀔 때만, 처음 본 정상은 보내지 않음, 확인 불가는 두 번 연속일 때만.
"""
from __future__ import annotations

import http.client
import json
from collections.abc import Callable
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from blackclip.events import NAME, EventsError, ago, folder, ingest, source_path
from blackclip.keystore import KeystoreError
from blackclip.notify import TELEGRAM_ACCOUNT, settings
from blackclip.telegram import Bot, TelegramError
from blackclip.workspace import WorkspaceError, exists_regular, read_json, write_atomic

FOLLOW, FSTATE = "follow.json", "follow.state.json"
"""전략 폴더 안: 승인한 선(Git에 올릴 전략 기록) · 마지막으로 보낸 상태와 확인 불가 연속 횟수."""

MINUTES = (5, 7 * 24 * 60)
"""선(분)으로 받는 범위: 예약이 5분마다라 5분 아래는 못 지키고, 일주일 넘는 선은 오타로 본다."""

EXIT_PARTIAL, EXIT_TELEGRAM = 4, 5
"""daily · watch와 같은 종료 코드 규칙(일부 확인 불가 · 텔레그램 실패)."""

STATES = ("정상", "안 보임", "확인 불가")


def approve(root: Path, name: str, minutes: int, now: datetime) -> list[str]:
    """고객이 승인한 선을 적는다(어댑터가 있어야 함).

    Args:
        root: .blackclip 경로.
        name: 전략 이름.
        minutes: 이벤트가 이 분 넘게 없으면 「안 보임」.
        now: 지금(UTC).

    Returns:
        화면 줄.

    Raises:
        EventsError: 이름 · 어댑터 · 범위 문제.
    """
    source_path(root, name)  # 어댑터 없이 선만 적지 않게
    if not MINUTES[0] <= minutes <= MINUTES[1]:
        raise EventsError(f"--quiet-minutes: {MINUTES[0]}~{MINUTES[1]}분으로 주세요({minutes})")
    doc = {"quiet_minutes": minutes, "approved_at": now.isoformat(timespec="seconds").replace("+00:00", "Z")}
    write_atomic(folder(root, name) / FOLLOW, json.dumps(doc, ensure_ascii=False, indent=2) + "\n")
    return [f"선을 적었어요 · {name} · 이벤트가 {minutes}분 넘게 없으면 「봇이 안 보여요」 · .blackclip/strategies/{name}/{FOLLOW}",
            "다음: blackclip schedule(5분마다 blackclip follow) · 텔레그램은 blackclip connect --telegram으로 이은 대화로 감"]


def followed(root: Path) -> list[str]:
    """선이 적힌 전략들(이름순).

    Args:
        root: .blackclip 경로.

    Returns:
        전략 이름.
    """
    base = root / "strategies"
    if not base.is_dir():
        return []
    return sorted(p.name for p in base.iterdir()
                  if NAME.fullmatch(p.name) and exists_regular(root, f"strategies/{p.name}/{FOLLOW}"))


def quiet_minutes(root: Path, name: str) -> int:
    """승인한 선(분).

    Args:
        root: .blackclip 경로.
        name: 전략 이름.

    Returns:
        분.

    Raises:
        EventsError: 없음 · 깨짐 · 범위 밖.
    """
    try:
        doc = read_json(root, f"strategies/{name}/{FOLLOW}")
    except (FileNotFoundError, WorkspaceError) as exc:
        raise EventsError(f"선 파일을 읽지 못했습니다({type(exc).__name__})") from None
    m = doc.get("quiet_minutes") if isinstance(doc, dict) else None
    if not isinstance(m, int) or isinstance(m, bool) or not MINUTES[0] <= m <= MINUTES[1]:
        raise EventsError(f".blackclip/strategies/{name}/{FOLLOW}: quiet_minutes가 {MINUTES[0]}~{MINUTES[1]} 정수가 아닙니다")
    return m


def judge(last: str | None, minutes: int, now: datetime) -> tuple[str, str]:
    """마지막 이벤트 시각으로 상태와 근거 한 줄.

    Args:
        last: 마지막 이벤트 UTC 시각(...Z) 또는 None.
        minutes: 선(분).
        now: 지금(UTC).

    Returns:
        (상태, 근거).
    """
    if last is None:
        return "확인 불가", "아직 받은 이벤트가 없습니다"
    gap = now - datetime.fromisoformat(last.replace("Z", "+00:00"))
    state = "안 보임" if gap > timedelta(minutes=minutes) else "정상"
    return state, f"마지막 이벤트 {last[:16].replace('T', ' ')} UTC · {ago(last, now)} 전 · 선 {minutes}분(승인한 값)"


def message(name: str, state: str, why: str) -> str:
    """텔레그램 세 줄(결론 · 근거 · 대응).

    Args:
        name: 전략 이름.
        state: 상태.
        why: 근거.

    Returns:
        글.
    """
    head = {"정상": "봇이 다시 보여요", "안 보임": "봇이 안 보여요", "확인 불가": "확인 불가"}[state]
    act = {"정상": "따로 할 일은 없어요",
           "안 보임": "봇이 돌고 있는지 확인해 주세요 · Guardrail은 봇을 멈추거나 주문하지 않아요",
           "확인 불가": f"blackclip events {name}로 어댑터와 봇 로그를 확인해 주세요"}[state]
    return "\n".join([f"{head} · {name} · 키 없는 감시", why, act])


def _fstate(root: Path, name: str) -> dict[str, Any]:
    """마지막으로 보낸 상태 · 확인 불가 연속 횟수(없거나 깨졌으면 처음처럼).

    Args:
        root: .blackclip 경로.
        name: 전략 이름.

    Returns:
        {"sent": 상태 또는 None, "streak": 정수}.
    """
    try:
        doc = read_json(root, f"strategies/{name}/{FSTATE}")
    except (FileNotFoundError, WorkspaceError):
        doc = {}
    doc = doc if isinstance(doc, dict) else {}
    sent = doc.get("sent") if doc.get("sent") in STATES else None
    streak = doc.get("streak") if isinstance(doc.get("streak"), int) else 0
    return {"sent": sent, "streak": streak}


def run_follow(root: Path, store: Any, make_bot: Callable[[str], Bot], now: datetime) -> tuple[int, list[str]]:
    """키 없이 지켜보는 시계를 한 번 돌린다. 부르는 쪽이 lock(root, "follow")을 잡는다.

    Args:
        root: .blackclip 경로.
        store: 키 보관(텔레그램 토큰만 읽음).
        make_bot: 토큰 → Bot.
        now: 지금(UTC).

    Returns:
        (종료 코드 0 · 4 · 5, 화면 줄).
    """
    names = followed(root)
    if not names:
        return 0, ["키 없이 지켜보는 전략이 없습니다 · blackclip events <전략> 뒤 blackclip follow <전략> --quiet-minutes <분>"]
    now = now.astimezone(timezone.utc).replace(microsecond=0)
    code, lines, msgs, first = 0, [], [], []
    for n in names:
        try:
            state, why = judge(ingest(root, n, now)["last"], quiet_minutes(root, n), now)
        except (EventsError, WorkspaceError, OSError) as exc:
            state, why = "확인 불가", str(exc)
        if state == "확인 불가":
            code = EXIT_PARTIAL
        lines.append(f"{n}: {state} · {why}")
        fs = _fstate(root, n)
        fs["streak"] = fs["streak"] + 1 if state == "확인 불가" else 0
        if state != fs["sent"] and not (fs["sent"] is None and state == "정상"):
            if state == "확인 불가" and fs["streak"] < 2:
                first.append(n)
            else:
                msgs.append((n, state, message(n, state, why)))
        write_atomic(folder(root, n) / FSTATE, json.dumps(fs, ensure_ascii=False) + "\n")
    s = settings(root)
    if first:
        lines.append(f"텔레그램: 확인 불가 1회째 · 다음 확인도 그러면 보냄({', '.join(first)})")
    if not msgs:
        if not first:
            lines.append("텔레그램: 바뀐 상태 없음 · 안 보냄")
    elif s["chat_id"] is None:
        lines.append("텔레그램: 대화 연결 전이라 안 보냄 · blackclip connect --telegram")
    else:
        try:
            creds = store.load(TELEGRAM_ACCOUNT)
            if creds is None:
                raise TelegramError("텔레그램 토큰이 없습니다 · blackclip connect --open --telegram")
            bot = make_bot(creds.api_key)
            for n, state, text in msgs:
                bot.send(s["chat_id"], text)
                fs = _fstate(root, n)
                write_atomic(folder(root, n) / FSTATE, json.dumps({**fs, "sent": state}, ensure_ascii=False) + "\n")
            lines.append("텔레그램: 보냄")
        except (TelegramError, KeystoreError, OSError, http.client.HTTPException) as exc:
            code = max(code, EXIT_TELEGRAM)
            lines.append(f"텔레그램: 못 보냄({exc if isinstance(exc, TelegramError) else type(exc).__name__}) · "
                         "바뀐 상태는 다음 확인에서 다시 보냄")
    return code, lines

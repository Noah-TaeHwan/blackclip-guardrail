"""무엇을 보낼지 정한다: 상태 바뀜(전략마다) · 매일 묶음(조용한 날 선택) · 매주 회고 · 실행 경고 문."""
from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from blackclip.daily.reconcile import TRACKED, Day, bundle, change_message
from blackclip.record import BROKEN
from blackclip.workspace import WorkspaceError, read_json, write_atomic

NOTIFIED = "notified.json"
"""마지막으로 보낸 상태(.blackclip/, Git 제외). 보낸 뒤에만 갱신한다."""


def broken_message(name: str) -> str:
    """기록(record.md)이 깨진 전략을 처음 알리는 세 줄.

    Args:
        name: 전략 이름.

    Returns:
        글.
    """
    return (f"기록 손상: {name}의 매일 대조가 멈췄어요\n전략 기록(record.md)을 읽지 못했어요\n"
            "git 충돌이면 풀고, 아니면 record.md를 지운 뒤 blackclip record로 다시 적어 주세요")


def broken_note(name: str) -> str:
    """매일 묶음 끝에 붙는 손상 한 줄.

    Args:
        name: 전략 이름.

    Returns:
        한 줄.
    """
    return f"기록 손상 {name}: 매일 대조가 멈춰 있어요"


def plan_messages(days: list[Day], notified: dict[str, dict[str, str]], s: dict[str, Any], notes: list[str],
                  broken: tuple[str, ...] | list[str] = ()) -> tuple[list[tuple[str, dict[str, dict[str, str]]]], list[str]]:
    """보낼 글과, 그 글이 가면 notified에 적을 것 · 지울 키를 정한다.

    Args:
        days: 오늘 전략마다 결과(대조가 돈 전략만, 비어도 됨).
        notified: 전략(또는 `record:전략`) → 마지막으로 보낸 상태.
        s: notify.settings 결과(quiet · execution_alerts).
        notes: 묶음 끝에 붙일 줄.
        broken: 기록이 깨진 전략 이름(이름순). 손상 글은 notified에 `record:이름`이 없을 때만 가고, quiet와 상관없다.

    Returns:
        ([(글, {notified 키: 보낸 상태})], 지울 키). 상태 바뀜 글 · 손상 글 · 묶음(또는 매주 회고) 순이고 묶음 · 회고는 `{}`.
        days가 비면 묶음 · 회고가 없다. 묶음이 가면 notes 끝에 broken마다 broken_note를 붙인다.
        지울 키는 notified의 `record:X` 가운데 X가 broken에 없는 것(고쳐진 전략, 다시 깨지면 다시 알리게).
    """
    on = bool(s["execution_alerts"])
    keys = [k for k in TRACKED if on or k != "실행"]
    out = [(change_message(d, on), {d.strategy: d.states}) for d in days
           if (prev := notified.get(d.strategy)) is None or any(prev.get(k) != d.states[k] for k in keys)]
    out += [(broken_message(n), {f"record:{n}": {"기록": BROKEN}}) for n in broken if f"record:{n}" not in notified]
    if days and s["quiet"] == "매일":
        out.append((bundle(days, on, notes + [broken_note(n) for n in broken]), {}))
    elif any(d.week for d in days):
        out.append(("매주 회고\n" + "\n".join(d.week for d in days if d.week), {}))
    return out, [k for k in notified if k.startswith("record:") and k[len("record:"):] not in broken]


def load_notified(root: Path) -> dict[str, dict[str, str]]:
    """마지막으로 보낸 상태를 읽는다(없거나 링크 · FIFO이거나 깨졌거나 모양이 다르면 빈 dict → 다음 실행에 다시 보냄).

    Args:
        root: .blackclip 경로.

    Returns:
        전략 → 칸 → 상태.
    """
    try:
        data = read_json(root, NOTIFIED)
    except (OSError, WorkspaceError):
        return {}
    ok = isinstance(data, dict) and all(
        isinstance(v, dict) and all(isinstance(k, str) and isinstance(x, str) for k, x in v.items()) for v in data.values())
    return data if ok else {}


def save_notified(root: Path, data: dict[str, dict[str, str]]) -> None:
    """마지막으로 보낸 상태를 쓴다.

    Args:
        root: .blackclip 경로.
        data: 전략 → 칸 → 상태.
    """
    write_atomic(root / NOTIFIED, json.dumps(data, ensure_ascii=False, indent=2, sort_keys=True) + "\n")

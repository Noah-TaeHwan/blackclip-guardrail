"""누구를 자주 보나 · watch 상태 파일(.blackclip/strategies/<전략>/watch.json, Git 제외: workspace.GITIGNORE의 `*`)."""
from __future__ import annotations

import json
from decimal import Decimal
from pathlib import Path
from typing import Any

from blackclip.watch.window import window_return
from blackclip.workspace import WorkspaceError, read_json, write_atomic

STATE = "watch.json"
"""전략마다 watch 상태 파일 이름(1시간 선 캐시 · 구간 기준 · BT 목표 캐시)."""

WINDOW_KEYS = frozenset({"start", "base", "pnl", "flows", "growth", "low", "mark"})
"""구간 칸에 꼭 있어야 하는 키(window.advance가 만든 모양). 하나라도 없으면 파일이 깨진 것으로 본다."""


def pick(guards: dict[str, dict], cfgs: dict[str, dict | None], lost: frozenset[str] = frozenset()
         ) -> tuple[list[str], list[tuple[str, str]], dict[tuple[str, str], list[str]]]:
    """자주 보는 시계 대상을 고른다: --capital 없이 실전 시작 잔고를 받은 전략 = 전략별 계좌.

    같은 계좌 · 같은 시장에 실전 시작 잔고를 받은 다른 전략이 있으면(--capital 유무 · 봉 길이와 상관없이) 그 계좌의 잔고는
    한 전략 것이 아니므로 섞인 계좌로 보고 뺀다(작성자 메모 1). 계좌는 모든 실전 전략으로 먼저 세고, 봉 길이는 그 뒤에 거른다.
    가드레일을 잃은(읽지 못했거나 판 파일이 링크 · FIFO · 없음이라 목록에서 빠진) 실전 전략의 계좌(lost)에 있는 전략도 시장 · 종목을 몰라 섞였을 수 있다고 보고 뺀다.

    Args:
        guards: 전략 → 가드레일(최신 판).
        cfgs: 전략 → notify.read_live 결과(없으면 None).
        lost: 가드레일을 잃은 실전 전략의 계좌 이름들(daily.run._lost_accounts).

    Returns:
        (볼 전략, [(뺀 전략, 까닭)], (계좌, 시장) → 섞인 계좌로 본 --capital 없는 전략들).
    """
    keys = {n: (cfg["account"], g["target"]["market"]) for n, g in guards.items() if (cfg := cfgs.get(n)) is not None}
    count = {k: sum(1 for v in keys.values() if v == k) for k in keys.values()}
    watched: list[str] = []
    skipped: list[tuple[str, str]] = []
    mixed: dict[tuple[str, str], list[str]] = {}
    for n, g in sorted(guards.items()):
        cfg = cfgs.get(n)
        if int(g["target"]["bar_seconds"]) > 86400:
            skipped.append((n, "봉 길이 1일 초과 · 점검(blackclip check)만"))
        elif cfg is None:
            skipped.append((n, f"실전 시작 잔고 없음 · blackclip live {n}"))
        elif cfg["capital"] is not None:
            skipped.append((n, "기준 자본(--capital) 있음 = 섞인 계좌 · 매일 대조만"))
        elif count[keys[n]] > 1:
            acct, market = keys[n]
            skipped.append((n, f"같은 계좌(binance {acct} {market})에 전략 {count[keys[n]]}개 = 섞인 계좌 · 매일 대조만"))
            mixed.setdefault(keys[n], []).append(n)
        elif cfg["account"] in lost:
            skipped.append((n, f"같은 계좌(binance {cfg['account']})에 가드레일을 읽지 못한 전략 = 섞였을 수 있음 · 매일 대조만"))
        else:
            watched.append(n)
    return watched, skipped, mixed


def _readable(win: dict) -> bool:
    """window 칸의 값이 window.advance · judge.line_state가 읽는 대로 읽히는지(mark · 숫자 칸).

    start는 글자 비교뿐이라(다르면 새로 잡음) 보지 않는다.

    Args:
        win: 상태 파일 "window" 칸(WINDOW_KEYS가 모두 있음).

    Returns:
        읽히면 True.
    """
    try:
        window_return(win)  # growth · mark(at · cash · hold · price)
        float(win["low"])
        for k in ("base", "pnl", "flows"):
            Decimal(win[k])
    except (KeyError, TypeError, ValueError, ArithmeticError, AttributeError):
        return False
    return True


def read_state(root: Path, name: str) -> tuple[dict[str, Any], bool]:
    """watch 상태 파일을 읽는다.

    Args:
        root: .blackclip 경로.
        name: 전략 이름.

    Returns:
        (내용, 깨졌는지). 없으면 ({}, False), 읽을 수 없거나(상위 폴더 링크 포함) 모양이 다르면 ({}, True). 모양이 다르다 = 객체가 아님 ·
        "line" 또는 "window" 칸이 있는데 객체가 아님 · "window" 칸에 WINDOW_KEYS가 빠짐 · "window" 안쪽 값(mark · 숫자 칸)을
        못 읽음(그대로 두면 매 시간 같은 오류로 영영 확인 불가에 「연결 확인」이라는 엉뚱한 안내만 나감). 깨진 파일은 호출자가
        새로 잡고 다음 확인부터 낫는다. "target" 칸은 잘못돼도 BT 목표만 다시 셈하면 되므로 여기서 보지 않는다.
    """
    try:
        data = read_json(root, f"strategies/{name}/{STATE}")
    except FileNotFoundError:
        return {}, False
    except (OSError, WorkspaceError):  # 상위 폴더 · 파일이 링크 · FIFO이거나 깨졌거나 1MiB 넘음
        return {}, True
    if (not isinstance(data, dict) or any(k in data and not isinstance(data[k], dict) for k in ("line", "window"))
            or not WINDOW_KEYS <= set(data.get("window", WINDOW_KEYS)) or ("window" in data and not _readable(data["window"]))):
        return {}, True
    return data, False


def write_state(root: Path, name: str, doc: dict[str, Any]) -> None:
    """watch 상태 파일을 쓴다(바꿔치기 쓰기).

    Args:
        root: .blackclip 경로.
        name: 전략 이름.
        doc: 내용(JSON으로 쓸 수 있는 값만).
    """
    write_atomic(root / "strategies" / name / STATE, json.dumps(doc, ensure_ascii=False, indent=2, sort_keys=True) + "\n")

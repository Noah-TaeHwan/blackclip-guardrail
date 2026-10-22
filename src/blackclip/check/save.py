"""blackclip save: 점검을 다시 돌려(같은 씨앗이라 check와 같은 숫자) 가드레일 파일을 남긴다.

파일은 .blackclip/strategies/<전략>/guardrail.vN.yaml이고 내용은 JSON(YAML 1.2의 부분집합,
새 의존성 없음)이다. 고객 레포에 커밋되므로 절대 경로 · 키를 넣지 않는다. 있는 판은 덮어쓰지 않는다.
"""
from __future__ import annotations

import json
import os
from datetime import date
from pathlib import Path

import numpy as np

from blackclip import __version__
from blackclip.check.guardrail import summary
from blackclip.check.link import has_abs_path
from blackclip.check.report import LABELS, CheckResult, run_check
from blackclip.workspace import _dir_fd, lock, versions, write_in

CHECKS: tuple[str, ...] = ("C0", "C1", "C2", "C3", "C4")
"""파일에 상태를 적는 점검."""


class SaveError(ValueError):
    """가드레일을 저장하지 않을 때(점검 불가 · 이미 있는 판 · 이유 없음, 종료 코드 2)."""


def _slot_taken(root: Path, name: str, slot: str) -> bool:
    """전략 폴더(폴더 FD 기준, 링크는 따라가지 않음)에 slot 이름의 무엇인가(링크 · 폴더 포함)가 있는지.

    Args:
        root: .blackclip 경로.
        name: 전략 이름.
        slot: 파일 이름(guardrail.vN.yaml).

    Returns:
        무엇이든 있으면 True, 없으면 False.

    Raises:
        WorkspaceError: 전략 폴더나 그 위가 링크이거나 폴더가 아님.
        FileNotFoundError: 전략 폴더가 없음.
    """
    fd = _dir_fd(root, ("strategies", name))
    try:
        os.lstat(slot, dir_fd=fd)
    except FileNotFoundError:
        return False
    finally:
        os.close(fd)
    return True


def document(result: CheckResult, sensitivity: str, start: date, today: date, version: int,
             reason: str | None) -> dict:
    """가드레일 파일 내용.

    Args:
        result: run_check 결과(guardrail이 있어야 함).
        sensitivity: 고른 민감도.
        start: 실전 시작일.
        today: 설정일(UTC).
        version: 판 번호.
        reason: 바꾼 이유(v2부터 필수).

    Returns:
        JSON으로 쓸 dict.
    """
    g = result.guardrail
    line = {**g["warning_line"], "sensitivity": sensitivity, "line": g["warning_line"]["lines"][sensitivity]}
    return {**g, "format": 1, "warning_line": line, "version": version, "strategy": result.name, "set_on": today.isoformat(),
            "live_start": start.isoformat(), "reason": reason, "previous": f"v{version - 1}" if version > 1 else None,
            "blackclip": __version__, "numpy": np.__version__, "match_window_bars": 1,
            "response": {"execution_warning": "", "warning_line": ""},
            "checks": {k: result.items[k]["state"] for k in CHECKS}}


def run_save(root: Path, name: str, sensitivity: str, start: date, reason: str | None,
             today: date) -> tuple[Path, dict]:
    """점검을 다시 돌려 가드레일을 다음 판 파일로 쓴다.

    Args:
        root: .blackclip 경로.
        name: 전략 이름.
        sensitivity: 민감도(SENSITIVITY 키).
        start: 실전 시작일(지난 날 · 앞날 모두 받음).
        reason: 바꾼 이유(판이 이미 있으면 필수).
        today: 설정일(UTC).

    Returns:
        (쓴 파일, 내용).

    Raises:
        LinkError: 연결 파일 · 가격표 · 신호 함수를 못 쓸 때.
        SaveError: 이유에 절대 경로 · 재현성 불일치 · 표현 불가 · 가드레일을 만들 수 없을 때 · 미래참조 점검 도중 하위 프로세스가
            끝났을 때 · 실전 시작일이 백테스트 시작보다 이를 때 · 판이 있는데 이유가 없을 때.
    """
    reason = (reason or "").strip() or None
    if reason and not reason.isprintable():  # 커밋 파일 · 화면 한 줄이 깨지지 않게
        raise SaveError("--reason에 줄바꿈 · 제어 문자 · 보이지 않는 공백 · 서식 문자가 있음 · 한 줄 글로 적어 주세요")
    if reason and has_abs_path(reason):  # 가드레일 파일은 커밋된다
        raise SaveError("--reason에 절대 경로가 있음 · 커밋되는 가드레일 파일에 남으므로 레포 기준 상대 경로나 말로 적어 주세요")
    result = run_check(root, name, None)
    if result.guardrail is None:
        raise SaveError(f"가드레일을 저장하지 않음 · {LABELS['C0']} {result.items['C0']['state']} · "
                        f"{LABELS['GUARD']} {result.items['GUARD']['state']} · blackclip check {name}로 확인")
    if result.c1_dead:  # 미래참조 점검 도중 하위 프로세스가 끝남: 점검이 끝까지 안 돌았다
        raise SaveError("가드레일을 저장하지 않았어요. 점검이 끝까지 돌지 않아 가드레일을 만들 수 없어요"
                        f"(미래참조 점검 중 고객 코드를 돌리던 하위 프로세스가 끝남). blackclip check {name}로 확인해 주세요")
    bt_start = date.fromisoformat(result.guardrail["target"]["start"])
    if start < bt_start:  # 읽는 쪽(workspace.guardrail_problem)이 target.start ≤ live_start를 요구해, 저장해 두면 daily · watch · live가 멈춘다
        raise SaveError(f"가드레일을 저장하지 않음 · 실전 시작일이 백테스트 시작({bt_start.isoformat()})보다 이릅니다 · --start를 그날 이후로 다시")
    with lock(root, "save"):
        have = versions(root, name)
        if have and not reason:
            raise SaveError(f"guardrail.v{have[-1]}.yaml이 이미 있음 · 덮어쓰지 않음 · "
                            f"바꾸려면 --reason \"이유\"를 주면 guardrail.v{have[-1] + 1}.yaml로 남김")
        version = have[-1] + 1 if have else 1
        slot = f"guardrail.v{version}.yaml"
        if _slot_taken(root, name, slot):  # 일반 파일 판만 세므로 여기 있는 것은 링크 · 폴더 등 · 번호를 재사용해 바꿔치지 않는다
            raise SaveError(f"{slot} 자리에 일반 파일이 아닌 것이 있어 저장하지 않습니다")
        doc = document(result, sensitivity, start, today, version, reason)
        try:
            text = json.dumps(doc, ensure_ascii=False, indent=2, sort_keys=True, allow_nan=False) + "\n"
        except ValueError:  # NaN · 무한대는 JSON이 아니다
            raise SaveError("가드레일을 저장하지 않음 · 숫자가 아닌 값(NaN · 무한대)이 있음") from None
        write_in(root, f"strategies/{name}/{slot}", text)
    return root / "strategies" / name / slot, doc


def render_save(doc: dict, rel: str) -> str:
    """저장 화면(결론 → 근거, 색 없음).

    Args:
        doc: 파일 내용.
        rel: 레포 기준 파일 경로.

    Returns:
        여러 줄 문자열.
    """
    lines = [(f"가드레일 v{doc['version']} 저장 · {doc['strategy']} · 실전 시작일 {doc['live_start']} · "
              f"민감도 {doc['warning_line']['sensitivity']}")]
    lines += summary(doc, doc["warning_line"]["sensitivity"])
    lines.append("점검: " + " · ".join(f"{LABELS.get(k, k)} {v}" for k, v in doc["checks"].items()))  # 점검 키 대신 이름
    if doc["reason"]:
        lines.append(f"이유: {doc['reason']}")
    lines.append(f"저장: {rel}")
    return "\n".join(lines)

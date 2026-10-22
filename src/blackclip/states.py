"""상태 이름 · 우선순위 · 대표 상태: pandas 없이 쓰는 바탕.

daily 판정(reconcile)과 기계 판독 일별 기록(days.jsonl), 화면이 같은 이름 · 같은 순서를 쓰게 여기에 둔다.
"""
from __future__ import annotations

from typing import Any

ORDER: tuple[str, ...] = ("확인 불가", "점검 불가", "경고", "판단 보류", "정상")
"""대표 상태 우선순위. 괄호 사유는 떼고 비교한다."""

TRACKED: tuple[str, ...] = ("전략", "실행", "봇", "선", "논지")
"""매일 상태를 적고 바뀌면 알리는 칸(원인 셋 + 가드레일 선 + 논지)."""

LABELS = {"전략": "전략", "실행": "실행", "봇": "봇", "선": "가드레일 선", "논지": "논지"}
"""칸 → 화면 이름."""

TITLES = {"전략": "전략 쪽", "실행": "실행 문제로 추정", "봇": "봇 · 체결 받기", "선": "경고선", "논지": "전제와 안 맞는 장"}
"""메시지 첫 줄에서 대표 상태 뒤에 붙는 말."""


def base(state: str) -> str:
    """괄호 사유를 뗀 상태 이름.

    Args:
        state: 「점검 불가(섞인 기록)」 등.

    Returns:
        「점검 불가」.
    """
    return state.split("(")[0]


def representative(states: dict[str, str], skip: tuple[str, ...] = ()) -> tuple[str, str]:
    """대표 상태와 그 칸(우선순위가 가장 앞선 것, 같으면 TRACKED 순).

    Args:
        states: 칸 → 상태.
        skip: 빼고 볼 칸(실행 경고를 아직 안 켰으면 "실행").

    Returns:
        (상태, 칸). 볼 칸이 없으면 ("정상", "전략").
    """
    ranked = [(ORDER.index(base(s)), i, s, k) for i, (k, s) in enumerate(states.items())
              if k not in skip and base(s) in ORDER]
    if not ranked:
        return "정상", "전략"
    _, _, s, k = min(ranked)
    return s, k


def states_ok(st: Any) -> bool:
    """칸 → 상태 글이 TRACKED 다섯 칸 모두 ORDER의 이름인지(state.log 줄 · days.jsonl 줄이 같은 규칙).

    논지 칸만 「해당 없음」도 받는다(논지가 없을 때 thesis_cause가 내는 정상 값, 대표 상태 계산에서 빠짐).

    Args:
        st: 줄에서 읽은 states 값.

    Returns:
        모양이 맞으면 True.
    """
    return (isinstance(st, dict) and set(st) == set(TRACKED) and all(
        isinstance(v, str) and (base(v) in ORDER or (k == "논지" and base(v) == "해당 없음")) for k, v in st.items()))


def pp(x: float) -> str:
    """%p 표기(부호 · 소수 한 자리).

    Args:
        x: 비율(0.01 = 1%p).

    Returns:
        「-2.1%p」.
    """
    return f"{round(x * 100, 1) + 0.0:+.1f}%p"  # -0.04는 「+0.0」(음수 0 안 보임)

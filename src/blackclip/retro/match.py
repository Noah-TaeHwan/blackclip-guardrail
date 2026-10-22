"""백테스트 체결과 실제 체결을 매칭 창 안에서 짝짓는다."""
from __future__ import annotations

from dataclasses import dataclass
from datetime import timedelta

from blackclip.retro.fills import Execution

DEFAULT_WINDOW: timedelta = timedelta(hours=2)
"""매칭 창 기본값 [제안값]. 가드레일이 생기면 가드레일에 적힌 값을 쓴다."""

_UNITS: dict[str, int] = {"m": 1, "h": 60, "d": 1440}
"""매칭 창 단위 → 분."""


@dataclass(frozen=True)
class Pair:
    """짝지은 백테스트 체결과 실제 체결.

    Attributes:
        backtest: 백테스트가 하려던 체결.
        actual: 실제 체결.
    """

    backtest: Execution
    actual: Execution


@dataclass(frozen=True)
class MatchResult:
    """짝짓기 결과.

    Attributes:
        pairs: 짝 목록(백테스트 시각 순).
        backtest_only: 백테스트에만 있는 체결(놓친 주문으로 추정할 후보).
        actual_only: 실제에만 있는 체결.
    """

    pairs: list[Pair]
    backtest_only: list[Execution]
    actual_only: list[Execution]


def _order_key(e: Execution) -> tuple:
    """결과 목록의 정렬 키(시각 · 종목 · 방향). 같은 키가 없으면 입력 순서와 무관하다.

    Args:
        e: 체결 하나.

    Returns:
        (시각, 종목, 방향) 튜플.
    """
    return (e.time, e.symbol, e.side)


def _fill_key(e: Execution) -> tuple:
    """묶음 안 정렬 키(시각 · 가격 · 수량 · 주문 번호). CSV 행 순서가 짝을 바꾸지 못하게 한다.

    Args:
        e: 체결 하나.

    Returns:
        (시각, 가격, 수량, 주문 번호) 튜플.
    """
    return (e.time, e.price, e.qty, e.order_id)


def parse_window(text: str) -> timedelta:
    """'90m', '2h', '1d' 꼴의 매칭 창을 읽는다.

    Args:
        text: 숫자 + 단위(m · h · d).

    Returns:
        timedelta.

    Raises:
        ValueError: 꼴이 아니거나, 숫자가 ASCII가 아니거나, 0이면.
    """
    text = text.strip().lower()
    num = text[:-1]
    if len(text) < 2 or text[-1] not in _UNITS or not (num.isascii() and num.isdigit()) or int(num) == 0:
        raise ValueError(f"매칭 창은 90m · 2h · 1d 꼴이어야 합니다({text})")
    return timedelta(minutes=int(num) * _UNITS[text[-1]])


def _match_group(bt: list[Execution], act: list[Execution], window: timedelta) -> list[tuple[int, int]]:
    """같은 종목 · 방향 안에서 짝 수가 가장 많고, 그중 시각 차이 합이 가장 작은 짝을 찾는다.

    두 목록을 시각 순으로 놓으면 서로 엇갈리지 않는 짝 중에 최적이 있으므로
    (i, j) 표를 채우는 동적 계획법으로 정확히 구한다.

    Args:
        bt: 백테스트 체결(시각 오름차순).
        act: 실제 체결(시각 오름차순).
        window: 매칭 창.

    Returns:
        (bt 번호, act 번호) 짝 목록.
    """
    # shortcut: 표 크기가 len(bt) × len(act)라 한 종목 · 방향에 한 달 수천 건이면 느려진다, 그때 창 밖 칸을 건너뛰게 바꾼다
    n, m = len(bt), len(act)
    best = [[(0, 0.0)] * (m + 1) for _ in range(n + 1)]  # (짝 수, -시각 차이 합 초) 큰 쪽이 좋다
    for i in range(n - 1, -1, -1):
        for j in range(m - 1, -1, -1):
            cand = max(best[i + 1][j], best[i][j + 1])
            gap = abs(bt[i].time - act[j].time)
            if gap <= window:
                c, d = best[i + 1][j + 1]
                cand = max(cand, (c + 1, d - gap.total_seconds()))
            best[i][j] = cand
    pairs, i, j = [], 0, 0
    while i < n and j < m:
        if best[i][j] == best[i + 1][j]:
            i += 1
        elif best[i][j] == best[i][j + 1]:
            j += 1
        else:
            pairs.append((i, j))
            i, j = i + 1, j + 1
    return pairs


def match(backtest: list[Execution], actual: list[Execution], window: timedelta = DEFAULT_WINDOW) -> MatchResult:
    """같은 종목 · 방향끼리 매칭 창 안에서 백테스트 체결과 실제 체결을 짝짓는다.

    짝 수가 가장 많게, 그중 시각 차이 합이 가장 작게 맺는다. 실제 체결 하나는 한 번만 쓰인다.

    Args:
        backtest: 백테스트 체결 목록.
        actual: 실제 체결 목록.
        window: 매칭 창(이보다 멀면 짝이 아님).

    Returns:
        MatchResult.
    """
    kinds = sorted({(e.symbol, e.side) for e in backtest + actual})
    pairs: list[Pair] = []
    used_b: set[int] = set()
    used_a: set[int] = set()
    for kind in kinds:
        bt = sorted((e for e in backtest if (e.symbol, e.side) == kind), key=_fill_key)
        act = sorted((e for e in actual if (e.symbol, e.side) == kind), key=_fill_key)
        for i, j in _match_group(bt, act, window):
            pairs.append(Pair(bt[i], act[j]))
            used_b.add(id(bt[i]))
            used_a.add(id(act[j]))
    return MatchResult(
        pairs=sorted(pairs, key=lambda p: _order_key(p.backtest)),
        backtest_only=sorted((e for e in backtest if id(e) not in used_b), key=_order_key),
        actual_only=sorted((e for e in actual if id(e) not in used_a), key=_order_key),
    )

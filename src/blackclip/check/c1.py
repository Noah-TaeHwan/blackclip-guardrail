"""미래참조: 데이터 끝을 잘라 다시 돌리고, 연결 코드가 닿는 파일을 정적 검사한다."""
from __future__ import annotations

from collections.abc import Callable
from pathlib import Path

import numpy as np
import pandas as pd

from blackclip.check.link import Link
from blackclip.check.source import reachable_lines, scan

STRONG: list[tuple[str, str, str | None]] = [
    (r"shift\(\s*(periods\s*=\s*)?-\s*\d", "다음 봉 값을 당겨 씀 shift(-n)", None),
    (r"center\s*=\s*True", "가운데 기준 이동창", None),
    (r"\.bfill\(|\.backfill\(|method\s*=\s*['\"](bfill|backfill)", "뒤 값으로 빈칸 채움", None),
    (r"\[::-1\]", "시계열 뒤집기", None),
]
"""미래 정보를 쓰는 줄로 볼 만한 패턴(나오면 의심)."""

WEAK: list[tuple[str, str, str | None]] = [
    (r"np\.(mean|std|max|min)\(|\.(mean|std|max|min)\(\)", "전체 기간 통계", r"rolling|ewm|expanding|groupby|resample"),
]
"""후보일 뿐인 패턴(메모만)."""

CUTS = 20
"""뒤쪽 절반에서 고르게 자르는 곳 수."""

EVENTS = 60
"""신호가 바뀌는 봉 중 고르게 자르는 곳 상한."""


def cut_points(n: int, changes: list[int]) -> list[int]:
    """자를 봉 번호(그 봉까지 남김): 뒤쪽 절반 고르게 CUTS곳 + 바뀌는 봉과 그 앞 봉 중 고르게 EVENTS곳까지.

    Args:
        n: 봉 수(모든 종목 시각의 합집합).
        changes: 목표 비중이 바뀌는 봉 번호.

    Returns:
        오름차순 봉 번호(난수 없음, 많아야 CUTS + EVENTS개).
    """
    base = {int(i) for i in np.linspace(n // 2, n - 1, CUTS)}
    events = sorted(set(changes) | {c - 1 for c in changes if c > 0})
    if len(events) > EVENTS:
        events = [events[int(i)] for i in np.linspace(0, len(events) - 1, EVENTS)]
    return sorted(base | set(events))


def c1(link: Link, prices: dict[str, pd.DataFrame], weights: dict[str, pd.Series], files: list[Path],
       signals_at: Callable[[pd.Timestamp], dict[str, pd.Series]]) -> dict[str, str]:
    """잘라 다시 돌린 신호가 전체로 돌린 신호와 다른지 보고, 신호 함수에서 닿는 코드의 정적 검사 후보를 붙인다.

    알려진 한계: 신호 함수가 호출 사이에 결과를 들고 있으면(캐시) 잘라 보기가 못 본다.

    Args:
        link: 연결 파일.
        prices: 종목 → 가격표(UTC).
        weights: 전체로 돌린 목표 비중.
        files: 정적 검사할 파일(source_files).
        signals_at: 그 시각까지만 남긴 가격표로 돌린 목표 비중(Runner.signals(cut=…)).

    Returns:
        {"state": 이 범위에서 통과 · 의심 · 판단 불가(신호 0건) · 점검 불가(잘린 데이터에서 오류 n/N곳), "text": 근거}.
    """
    union = pd.DatetimeIndex([])
    for p in prices.values():
        union = union.union(p.index)
    changes: set[int] = set()
    for s in weights.values():
        moved = s.index[(s.diff().fillna(s) != 0).to_numpy()]
        changes.update(int(i) for i in union.get_indexer(moved))
    reach = reachable_lines(files)
    strong = scan(files, link.repo, STRONG, reach)
    weak = scan(files, link.repo, WEAK, reach)
    points = cut_points(len(union), sorted(changes)) if changes else []
    changed: set[pd.Timestamp] = set()
    errors: list[str] = []
    for pos in points:
        cut = union[pos]
        try:
            got = signals_at(cut)
        except Exception as exc:  # 잘린 데이터에서 고객 코드가 죽는 것 자체가 발견이다
            errors.append(f"{cut:%Y-%m-%d} {getattr(exc, 'type_name', type(exc).__name__)}")
            continue
        for s, full in weights.items():
            ref = full.loc[:cut]
            if not len(ref):
                continue
            mine = got[s].reindex(ref.index).fillna(0.0) if s in got else pd.Series(0.0, index=ref.index)
            changed.update(ref.index[~np.isclose(mine.to_numpy(), ref.to_numpy())])
    if not changes:
        state, text = "판단 불가(신호 0건)", "신호가 한 번도 나오지 않아 잘라 보기로 판단할 수 없음"
    elif changed:
        state = "의심"
        text = f"데이터 끝을 잘라 다시 돌리면 {min(changed):%Y-%m-%d} 이후 신호 {len(changed):,}개가 바뀜(잘라 본 {len(points)}곳)"
    elif strong:
        state = "의심"
        text = f"잘라 다시 돌린 {len(points)}곳에서는 바뀐 신호가 없지만 정적 검사에서 미래 정보를 쓰는 줄 후보가 나옴"
    elif errors:
        state = f"점검 불가(잘린 데이터에서 오류 {len(errors)}/{len(points)}곳)"
        text = f"잘린 데이터에서 신호 함수가 죽음({errors[0]})"
    else:
        state, text = "이 범위에서 통과", f"잘라 다시 돌린 {len(points)}곳에서 바뀐 신호 없음"
    if errors and not state.startswith("점검 불가"):
        text += f" · 잘린 데이터에서 신호 함수가 죽음 {len(errors)}/{len(points)}곳({errors[0]})"
    if strong:
        text += " · 정적 검사 후보 " + " · ".join(f"{w} {label}" for w, label in strong[:3])
    if weak:
        text += f" · 전체 기간 통계 후보 {len(weak)}곳(메모, {weak[0][0]})"
    return {"state": state, "text": text}

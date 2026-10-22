"""1시간 확인 경고선: 저장된 부트스트랩 선(하루 1회) + min(0, 흉내 24회 − 흉내 1회).

가드레일 파일(guardrail.vN.yaml)은 바꾸지 않는다. 계산한 선은 watch 상태 파일에 캐시한다.
"""
from __future__ import annotations

from blackclip.sim.warning_line import calibrate_lines

LINE_PATHS = 20000
"""흉내 경로 수(처음 잰 값과 같음). 시험은 줄여 넘긴다."""

LINE_NOTE = "1시간 확인 기준 · 보정은 독립 정규 가정(미검증)"
"""선 옆에 붙이는 문구."""


def hourly_lines(g: dict, cache: dict | None, paths: int | None = None) -> tuple[dict[str, float], dict]:
    """민감도 세 칸의 1시간 확인 경고선을 낸다. 가드레일 판 · period.mean · period.std가 캐시와 같으면 다시 셈하지 않는다.

    Args:
        g: 가드레일(최신 판, version · period · warning_line.lines).
        cache: watch 상태 파일의 "line" 칸(없으면 None).
        paths: 흉내 경로 수(없으면 LINE_PATHS).

    Returns:
        (민감도 → 선, 새 캐시 {"version", "mean", "std", "lines"}).
    """
    key = {"version": g["version"], "mean": g["period"]["mean"], "std": g["period"]["std"]}
    if cache and all(cache.get(k) == v for k, v in key.items()) and isinstance(cache.get("lines"), dict):
        return dict(cache["lines"]), cache
    n = paths or LINE_PATHS
    many = calibrate_lines(key["mean"], key["std"], 24, paths=n)
    once = calibrate_lines(key["mean"], key["std"], 1, paths=n)
    # 두 분위수 차이는 흉내 잡음으로 0 위로 갈 수 있다(열 차이 2 표준편차 약 0.4~0.9%p). 자주 볼수록 선이 위로 갈 수는 없어 0에서 자름
    lines = {k: round(v + min(0.0, many[k] - once[k]), 6) for k, v in g["warning_line"]["lines"].items()}
    return lines, {**key, "lines": lines}

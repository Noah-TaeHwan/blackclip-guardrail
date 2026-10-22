"""경고선을 확인 주기에 맞춰 보정한다.

가정(미검증): 엣지가 그대로인 전략의 일별 수익은 평균 mu, 표준편차 sigma인
독립 정규분포다. 하루를 checks_per_day 조각으로 나눠 조각마다 누적 수익을 본다.
실제 수익의 두꺼운 꼬리와 자기상관은 담지 못한다.
"""
from __future__ import annotations

import math
import random

SENSITIVITY: dict[str, float] = {"민감": 0.05, "보통": 0.0184, "둔감": 0.0043}
"""민감도 세 칸의 한 달 헛경보 확률(숫자와 기본값 「보통」은 제안값)."""

DEFAULT_SENSITIVITY = "보통"
"""고르지 않으면 쓰는 칸. pandas 없이 불러오도록 여기 둔다(cli가 씀)."""


def year_false_alarm(month_rate: float) -> float:
    """한 달 헛경보 확률을 1년(12달 독립) 동안 한 번 이상 날 확률로 바꾼다.

    Args:
        month_rate: 한 달 헛경보 확률(0~1).

    Returns:
        1년 동안 헛경보가 한 번 이상 날 확률(0~1).
    """
    return 1 - (1 - month_rate) ** 12


def month_rate_for_year(year_rate: float) -> float:
    """1년 헛경보 확률에 맞는 한 달 헛경보 확률을 구한다.

    Args:
        year_rate: 1년 동안 한 번 이상 날 확률(0~1).

    Returns:
        한 달 헛경보 확률(0~1).
    """
    return 1 - (1 - year_rate) ** (1 / 12)


def _path_minimums(mu: float, sigma: float, checks_per_day: int, days: int, paths: int, seed: int) -> list[float]:
    """경로마다 확인 시점에 본 누적 수익의 최솟값을 모은다.

    Args:
        mu: 일별 평균 수익.
        sigma: 일별 수익 표준편차.
        checks_per_day: 하루에 보는 횟수.
        days: 한 달 날수.
        paths: 흉내 낼 경로 수.
        seed: 난수 씨앗.

    Returns:
        경로별 최솟값 목록(0 이하).
    """
    rng = random.Random(seed)
    step_mu = mu / checks_per_day
    step_sigma = sigma / math.sqrt(checks_per_day)
    steps = days * checks_per_day
    mins: list[float] = []
    for _ in range(paths):
        total = 0.0
        low = 0.0
        for _ in range(steps):
            total += rng.gauss(step_mu, step_sigma)
            if total < low:
                low = total
        mins.append(low)
    return mins


def false_alarm_rate(line: float, mu: float, sigma: float, checks_per_day: int, days: int = 30, paths: int = 20000, seed: int = 7) -> float:
    """주어진 선을 한 달 동안 보며 한 번이라도 닿을 확률을 흉내 내 잰다.

    Args:
        line: 누적 수익 경고선(음수, 예: -0.072).
        mu: 일별 평균 수익.
        sigma: 일별 수익 표준편차.
        checks_per_day: 하루에 보는 횟수.
        days: 한 달 날수.
        paths: 흉내 낼 경로 수.
        seed: 난수 씨앗.

    Returns:
        헛경보 확률(0~1).
    """
    mins = _path_minimums(mu, sigma, checks_per_day, days, paths, seed)
    return sum(1 for m in mins if m <= line) / len(mins)


def calibrate_line(mu: float, sigma: float, checks_per_day: int, month_rate: float, days: int = 30, paths: int = 20000, seed: int = 7) -> float:
    """한 달 헛경보 확률이 month_rate가 되는 누적 수익 경고선을 찾는다.

    Args:
        mu: 일별 평균 수익(엣지가 그대로인 전략).
        sigma: 일별 수익 표준편차.
        checks_per_day: 하루에 보는 횟수(1 = 하루 한 번, 24 = 1시간마다).
        month_rate: 목표 한 달 헛경보 확률(SENSITIVITY 값).
        days: 한 달 날수.
        paths: 흉내 낼 경로 수.
        seed: 난수 씨앗(같으면 같은 선).

    Returns:
        누적 수익률 경고선(음수).
    """
    return _at(sorted(_path_minimums(mu, sigma, checks_per_day, days, paths, seed)), month_rate)


def _at(mins: list[float], month_rate: float) -> float:
    """정렬한 경로 최솟값에서 한 달 헛경보 확률에 맞는 값(작은 쪽에서 int(확률 × 경로 수)번째).

    Args:
        mins: 오름차순 경로 최솟값.
        month_rate: 한 달 헛경보 확률.

    Returns:
        경고선(음수).
    """
    return mins[max(0, min(len(mins) - 1, int(month_rate * len(mins)) - 1))]


def calibrate_lines(mu: float, sigma: float, checks_per_day: int, days: int = 30, paths: int = 20000,
                    seed: int = 7) -> dict[str, float]:
    """민감도 세 칸의 경고선을 한 번의 경로 묶음에서 낸다(같은 씨앗 · 같은 경로, 칸마다 분위수만 다름).

    Args:
        mu: 일별 평균 수익.
        sigma: 일별 수익 표준편차.
        checks_per_day: 하루에 보는 횟수.
        days: 한 달 날수.
        paths: 흉내 낼 경로 수.
        seed: 난수 씨앗.

    Returns:
        민감도 → 누적 수익률 경고선(음수, 단순 합 · 독립 정규 가정(미검증)).
    """
    mins = sorted(_path_minimums(mu, sigma, checks_per_day, days, paths, seed))
    return {name: _at(mins, rate) for name, rate in SENSITIVITY.items()}

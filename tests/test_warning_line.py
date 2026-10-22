"""경고선 보정 시뮬레이션이 정해 둔 민감도 숫자와 맞는지 본다."""
import pytest

from blackclip.sim.warning_line import (
    SENSITIVITY,
    calibrate_line,
    false_alarm_rate,
    month_rate_for_year,
    year_false_alarm,
)


@pytest.mark.parametrize("name, year_pct", [("민감", 46.0), ("보통", 20.0), ("둔감", 5.0)])
def test_sensitivity_matches_d83(name, year_pct):
    """세 칸의 한 달 확률이 1년 헛경보 약 46% · 20% · 5%가 되는지 본다."""
    assert year_false_alarm(SENSITIVITY[name]) * 100 == pytest.approx(year_pct, abs=0.6)


def test_month_rate_round_trip():
    """1년 확률 → 한 달 확률 → 1년 확률이 제자리로 오는지 본다."""
    assert year_false_alarm(month_rate_for_year(0.20)) == pytest.approx(0.20, abs=1e-9)


def test_more_frequent_checks_push_line_lower():
    """자주 볼수록 같은 헛경보 확률을 지키려면 선이 더 아래여야 한다."""
    daily = calibrate_line(mu=0.0005, sigma=0.02, checks_per_day=1, month_rate=0.05, paths=4000)
    hourly = calibrate_line(mu=0.0005, sigma=0.02, checks_per_day=24, month_rate=0.05, paths=4000)
    assert hourly < daily < 0


def test_line_hits_target_rate_on_fresh_paths():
    """찾은 선을 새 난수로 다시 흉내 내도 헛경보 확률이 목표 근처인지 본다."""
    line = calibrate_line(mu=0.0005, sigma=0.02, checks_per_day=1, month_rate=0.05, seed=7)
    rate = false_alarm_rate(line, mu=0.0005, sigma=0.02, checks_per_day=1, seed=99)
    assert rate == pytest.approx(0.05, abs=0.01)


def test_same_seed_same_line():
    """같은 seed면 같은 선이 나오는지 본다(두 번 돌려 같은 숫자)."""
    a = calibrate_line(mu=0.0, sigma=0.02, checks_per_day=4, month_rate=0.0184, paths=4000, seed=3)
    b = calibrate_line(mu=0.0, sigma=0.02, checks_per_day=4, month_rate=0.0184, paths=4000, seed=3)
    assert a == b


def test_calibrate_lines_matches_m4_table():
    """가정(mu 0.05% · sigma 2% · 씨앗 7 · 20,000 경로)에서 세 칸이 처음 잰 표와 같다."""
    from blackclip.sim.warning_line import calibrate_lines

    hourly = calibrate_lines(0.0005, 0.02, 24)
    daily = calibrate_lines(0.0005, 0.02, 1)
    assert {k: round(v * 100, 1) for k, v in hourly.items()} == {"민감": -20.0, "보통": -24.5, "둔감": -29.6}
    assert {k: round(v * 100, 1) for k, v in daily.items()} == {"민감": -19.1, "보통": -23.7, "둔감": -28.7}
    assert daily["민감"] == calibrate_line(0.0005, 0.02, 1, SENSITIVITY["민감"])  # 한 칸짜리와 같은 분위수 규칙

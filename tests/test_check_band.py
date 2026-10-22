"""check.band: 중첩 이중 블록 부트스트랩 · 정상 범위 · 동시 하한선."""
import numpy as np
import pandas as pd
import pytest

import blackclip.check.band as band_mod
from blackclip.check.band import PATHS, _blocks, _compound, band, paths, periods


def test_periods_groups_hourly_into_days():
    """시간봉은 UTC 하루로 복리 묶음, 일봉은 그대로."""
    idx = pd.date_range("2024-01-01", periods=48, freq="h", tz="UTC")
    r, step = periods(pd.Series(0.01, index=idx), 3600)
    assert step == 86400 and len(r) == 2 and np.allclose(r.to_numpy(), 1.01 ** 24 - 1)
    daily = pd.Series(0.01, index=pd.date_range("2024-01-01", periods=5, freq="D", tz="UTC"))
    assert periods(daily, 86400)[0] is daily


def test_periods_tz_aware_groups_by_utc_day():
    """시간대가 있는 인덱스도 UTC 하루로 묶는다: 서울 00시에 시작하는 48시간은 UTC로 사흘(9 · 24 · 15시간)."""
    idx = pd.date_range("2024-01-01", periods=48, freq="h", tz="Asia/Seoul")
    r, step = periods(pd.Series(0.01, index=idx), 3600)
    assert step == 86400 and str(r.index.tz) == "UTC"
    assert list(r.index.strftime("%Y-%m-%d")) == ["2023-12-31", "2024-01-01", "2024-01-02"]
    assert np.allclose(r.to_numpy(), [1.01 ** h - 1 for h in (9, 24, 15)])


def test_blocks_runs_and_wraps():
    """블록 길이가 아주 길면 한 경로가 연속 인덱스(끝에서 처음으로 돎)."""
    idx = _blocks(np.random.default_rng(0), 10, 3, 25, 10**9)
    assert ((np.diff(idx, axis=1) % 10) == 1).all()


def test_paths_shape_and_seeded():
    """경로 수 · 길이, 같은 씨앗 · 같은 호출 순서면 같은 인덱스(BUDGET도 재현 계약의 일부라 바꾸면 보장 못 함)."""
    a = paths(np.random.default_rng(7), 500, 30, 5)
    b = paths(np.random.default_rng(7), 500, 30, 5)
    assert a.shape == (PATHS, 30) and (a == b).all() and a.min() >= 0 and a.max() < 500


@pytest.mark.parametrize("block", [4, 10, 20])
def test_band_keeps_block_length(monkeypatch, block):
    """AR 성격이 있는 수익에서 band 경로의 평균 연속 구간 길이가 명목 블록 길이 근처(15% 안).

    중첩(바깥 재표본 안에서 안쪽 블록)은 구간이 두 번 끊겨 길이가 절반이 되므로(측정: 4 -> 2.29, 10 -> 5.24,
    20 -> 10.14) band가 2L을 넘긴다. 측정: 4.23 · 9.97 · 19.14(구간 400칸, 끝에서 잘리는 몫 포함).
    """
    seen = []
    real = band_mod.paths
    monkeypatch.setattr(band_mod, "paths", lambda *a: seen.append(real(*a)) or seen[-1])
    shocks, r = np.random.default_rng(5).normal(0.0, 0.01, 1500), np.zeros(1500)
    for t in range(1, 1500):
        r[t] = 0.5 * r[t - 1] + shocks[t]
    band(r, 400, block, np.random.default_rng(7))
    idx = seen[0]
    runs = idx.shape[0] + int(((np.diff(idx, axis=1) % 1500) != 1).sum())
    assert abs(idx.size / runs / block - 1) < 0.15


def test_band_iid_false_alarm_near_nominal():
    """독립 정규 수익(엣지 그대로)에서 백테스트 40개 평균으로, 새 한 달이 선 · 하단에 닿는 비율이 명목 근처.

    가정: 일 평균 0.1% · 표준편차 2% · 백테스트 1,500일. 백테스트 하나만 보면 표본 평균 오차로 크게 흔들려서
    (씨앗 1: 민감 2.9%, 씨앗 3: 7.1%) 여러 백테스트 평균으로 잰다. 명목은 민감 5% · 보통 1.84% · 둔감
    0.43% · 하단 밖 5%. 중첩 + 2L로 잰 값(씨앗 1~40, 새 한 달 20,000경로): 민감 4.85% · 보통 1.85% ·
    둔감 0.46% · 하단 밖 4.85%.
    """
    live = np.cumprod(1 + np.random.default_rng(99).normal(0.001, 0.02, (20_000, 30)), axis=1) - 1
    lows = np.minimum(live.min(axis=1), 0)
    hits = []
    for seed in range(1, 41):
        out = band(np.random.default_rng(seed).normal(0.001, 0.02, 1500), 30, 1, np.random.default_rng(7))
        assert out["lines"]["민감"] > out["lines"]["보통"] > out["lines"]["둔감"]
        hits.append([float((lows <= out["lines"][k]).mean()) for k in ("민감", "보통", "둔감")]
                    + [float((live[:, -1] < out["low"][-1]).mean())])
    sens, mid, dull, low = np.mean(hits, axis=0)
    assert 0.04 <= sens <= 0.06 and 0.012 <= mid <= 0.024 and 0.002 <= dull <= 0.008 and 0.035 <= low <= 0.065


def test_band_edge0_lower_than_edge_kept():
    """엣지 0 경로가 범위 안일 확률은 1보다 작고, 평균이 0인 표본이면 거의 90%."""
    r = np.random.default_rng(2).normal(0.004, 0.02, 1500)
    assert band(r, 30, 1, np.random.default_rng(7))["edge0"][-1] < 0.85
    flat = np.random.default_rng(3).normal(0.0, 0.02, 1500)
    assert abs(band(flat, 30, 1, np.random.default_rng(7))["edge0"][-1] - 0.9) < 0.02


def test_ruin_is_absorbing():
    """한 기간에 -300%면 그 뒤로 -100%에 머문다(음수 자산끼리 곱해 되살아나지 않음)."""
    cum = _compound(np.array([[0.1, -3.0, -3.0, 0.5], [0.1, 0.1, 0.1, 0.1]]))
    assert np.allclose(cum[0], [0.1, -1.0, -1.0, -1.0]) and np.allclose(cum[1, -1], 1.1 ** 4 - 1)
    out = band(np.array([0.01] * 99 + [-3.003]), 30, 1, np.random.default_rng(7))
    assert out["low"].min() >= -1 and min(out["lines"].values()) >= -1 and out["high"].max() < 1
    assert (out["low"][np.argmax(out["low"] <= -1):] == -1).all()


def test_rare_big_day_does_not_loosen_line():
    """백테스트 365일에 +100% 날이 하나 끼어도 보통 선이 그 날 없는 백테스트 선보다 20% 넘게 느슨해지지 않는다.

    평균 이동을 더하던 구성은 그 날을 안쪽 · 바깥 두 번 빼서 선이 크게 내려갔다(큰 날
    1개면 보통 선 -10.8% -> -15.3%, 실제 오탐 0.12%).
    """
    r = np.random.default_rng(11).normal(0.0005, 0.01, 365)
    jump = r.copy()
    jump[200] = 1.0
    base = band(r, 30, 1, np.random.default_rng(7))["lines"]["보통"]
    assert band(jump, 30, 1, np.random.default_rng(7))["lines"]["보통"] >= 1.2 * base

"""check.guardrail: 가드레일 숫자 · 고객 말 요약."""
import json

import numpy as np
import pandas as pd
import pytest

from blackclip.check.band import SEED, band
from blackclip.check.c3 import deflate
from blackclip.check.guardrail import GuardrailError, build, prices_sha256, summary
from blackclip.check.trades import Trade
from blackclip.sim.warning_line import year_false_alarm


def _returns(n=800, freq="D", mu=0.001, seed=0):
    idx = pd.date_range("2022-01-01", periods=n, freq=freq, tz="UTC")
    return pd.Series(np.random.default_rng(seed).normal(mu, 0.02, n), index=idx)


def _trades(returns, every=5, ret=0.004, spread=0.03, seed=1):
    """every 봉마다 하나씩 끝나는 거래(길이 3봉)."""
    rng = np.random.default_rng(seed)
    idx = returns.index
    return [Trade("BTCUSDT", idx[i - 3], idx[i], 3, float(rng.normal(ret, spread))) for i in range(3, len(idx), every)]


def _build(r, trades, stats=None, bar_s=86400, trials=None):
    """build를 시도 수 숫자(비용 0 = 같은 수익률로 근사)와 함께 부른다."""
    return build(r, trades, stats or _stats(), bar_s, deflate(r, bar_s, trials))


def _stats(side_mean=-0.002):
    return {"상승": {"trades": 80, "mean": 0.006}, "횡보": {"trades": 60, "mean": side_mean},
            "하락": {"trades": 20, "mean": 0.001}, "분류 안 됨": {"trades": 0}}


def test_build_full_guardrail():
    """거래 160건 · 800일: 범위 · 세 칸 선 · 판단 보류 · 빈도 · 논지 · 근거가 다 있다."""
    r = _returns()
    g = _build(r, _trades(r))
    nr, wl, jh = g["normal_range"], g["warning_line"], g["judgment_hold"]
    assert nr["drawn"] is True and nr["low"] < 0 < nr["high"] and len(nr["daily_low"]) == 30 and nr["daily_low"][-1] == nr["low"]
    assert wl["lines"]["민감"] > wl["lines"]["보통"] > wl["lines"]["둔감"] and wl["lines"]["둔감"] <= nr["low"]
    assert wl["year_rate"] == {"민감": 0.45964, "보통": 0.19977, "둔감": 0.050397}
    assert len(jh["edge0_in_range"]) == 30 and 0 < jh["edge0_in_range"][-1] < 1
    assert jh["trades_needed"] > 100 and jh["months_to_min_trades"] == pytest.approx(30 / (160 / (799 / 30.4375)), abs=1e-6)
    assert g["trade_frequency"]["low"] == 1 and g["trade_frequency"]["high"] == 2
    assert g["thesis"]["rule"] == "횡보 60일 넘게 이어지면 「전제와 안 맞는 장」 알림"
    assert g["basis"]["block_periods"] == [2, 3, 6] and len(g["basis"]["low_by_block"]) == 3
    assert g["cost"]["note"].startswith("보수적 비용 기준 · 시도 수 보정은 범위에 넣지 않고")
    json.dumps(g, ensure_ascii=False)  # 파일에 쓸 수 있는 값만


def test_build_same_input_same_numbers():
    """같은 입력이면 같은 숫자(난수 씨앗 7 하나를 순서대로)."""
    r = _returns()
    a = json.dumps(_build(r, _trades(r)), ensure_ascii=False, sort_keys=True)
    assert a == json.dumps(_build(r, _trades(r)), ensure_ascii=False, sort_keys=True)


def test_build_too_few_trades_no_range_but_line():
    """왕복 거래 < 100건이면 범위를 그리지 않고 경고선 · 판단 보류는 보인다."""
    r = _returns()
    g = _build(r, _trades(r, every=20))
    assert g["normal_range"]["drawn"] is False and "백테스트 왕복 거래 40건" in g["normal_range"]["reason"]
    assert g["warning_line"]["lines"]["보통"] < 0 and g["judgment_hold"]["edge0_in_range"] is None
    text = " / ".join(summary(g, "보통"))
    assert "정상 범위 그리지 않음(백테스트 왕복 거래 40건 · 기간 800일(하한 100건 · 365일))" in text
    assert "범위를 그리지 않아 엣지 0 확률 모름(보류가 이어짐)" in text
    # 범위를 안 그릴 만큼 거래가 적으면 오탐률도 명목 하나가 아니라 범위(위쪽 = 국면 배수 5.4)
    assert ("한 달 기준 약 1.84%~9.9%(위쪽은 거래 수나 기간이 하한에 못 미친 백테스트 · 미검증) · 1년 내내 보면 약 20%~72% · 다른 칸"
            in text)


def test_build_refuses_no_trades_or_under_a_month():
    """거래 0건 · 30일 이하 백테스트는 가드레일을 만들지 않는다."""
    r = _returns()
    with pytest.raises(GuardrailError, match="가드레일을 만들 수 없음: 왕복 거래 0건"):
        _build(r, [])
    short = _returns(n=30)
    with pytest.raises(GuardrailError, match="가드레일을 만들 수 없음: 백테스트가 30일보다 짧음"):
        _build(short, _trades(short))


def test_build_hourly_uses_days():
    """시간봉 2년이면 하루로 묶어 30기간 = 30일, 블록은 날 수."""
    r = _returns(n=24 * 730, freq="h", mu=0.00004, seed=2)
    g = _build(r, _trades(r, every=60, ret=0.001), bar_s=3600)
    assert g["period"]["seconds"] == 86400 and len(g["normal_range"]["daily_low"]) == 30
    assert g["basis"]["block_periods"] == [1, 1, 2]  # 평균 3시간 보유 → 하루 1기간


def test_negative_edge_trades_needed_unknown():
    """거래 1건 평균이 0 이하면 엣지 0을 가릴 거래 수는 모름(None), 논지는 횡보 평균으로만."""
    r = _returns()
    g = _build(r, _trades(r, ret=-0.004), _stats(side_mean=0.001))
    assert g["judgment_hold"]["trades_needed"] is None and g["thesis"]["rule"] == "논지 없음"
    assert "거래 1건 평균이 0 이하라 엣지 0을 가릴 거래 수 모름" in " ".join(summary(g, "민감"))


def test_summary_words():
    """고객 말: 기간을 붙인 오탐률 · 다른 칸 · 필수 문구."""
    r = _returns()
    lines = summary(_build(r, _trades(r)), "보통")
    assert lines[0].startswith("앞으로 한 달 90% 정상 범위 ")
    assert "(민감도 보통: 전략이 멀쩡해도 울릴 확률 한 달 기준 약 1.84% · 1년 내내 보면 약 20% · 다른 칸 민감 " in lines[1]
    assert lines[-1] == "보수적 비용 기준 · 시도 수 보정은 범위에 넣지 않고 판단 보류 옆에 숫자로 적어요(판단 보류 계산에는 아직 안 써요)"
    assert lines[-2].startswith("논지: 횡보 60일 넘게 이어지면 「전제와 안 맞는 장」 알림(횡보 거래 60건, 평균 -0.20%)")


def test_prices_sha256_stable_and_sensitive():
    """같은 가격표면 같은 해시, 값 하나가 바뀌면 다른 해시."""
    idx = pd.date_range("2024-01-01", periods=3, freq="D", tz="UTC")
    p = {"A": pd.DataFrame({"Open": [1.0, 2, 3], "High": 3.0, "Low": 1.0, "Close": [1.0, 2, 3]}, index=idx)}
    q = {"A": p["A"].copy()}
    assert prices_sha256(p) == prices_sha256(q)
    q["A"].iloc[1, 3] = 2.5
    assert prices_sha256(p) != prices_sha256(q)


def test_build_refuses_ruined_backtest():
    """백테스트 자산이 한 번이라도 0 이하면 가드레일을 만들지 않는다(-300% 하루)."""
    r = _returns()
    r.iloc[100] = -3.003
    with pytest.raises(GuardrailError, match="가드레일을 만들 수 없음: 백테스트 자산이 0 이하로 떨어짐"):
        _build(r, _trades(r))


def test_build_refuses_bars_longer_than_a_day():
    """주봉은 하루 경로를 못 만들어 가드레일을 만들지 않고 봉 길이를 말한다."""
    r = _returns(n=401, freq="W")
    with pytest.raises(GuardrailError, match=r"가드레일을 만들 수 없어요: 봉 길이 1w\(이 버전은 하루 이하 봉만"):
        _build(r, _trades(r, every=2), bar_s=604800)


def test_c3_stored_and_shown_next_to_hold():
    """시도 수 숫자를 파일에 남기고 판단 보류 옆에 보인다."""
    r = _returns()
    g = _build(r, _trades(r), trials=(1920, "격자"))
    assert g["c3"]["trials"] == 1920 and g["c3"]["method"] == "독립 가정 근사" and g["c3"]["unverified"] is True
    hold = next(line for line in summary(g, "보통") if line.startswith("판단 보류"))
    assert f"시도 수: 시도 1,920개(고객이 밝힌 값 · 최솟값(미검증))면 비용 0 샤프 {g['c3']['sharpe_cost0']:.2f} → " in hold
    unknown = next(line for line in summary(_build(r, _trades(r)), "보통") if line.startswith("판단 보류"))
    assert "시도 수 모름: 비용 0 샤프 " in unknown and " · 0 이하가 되는 시도 수 약 " in unknown
    assert "가 0 이하" not in unknown  # 숫자 뒤 조사를 붙이지 않음


def test_constant_positive_trades_not_called_nonpositive():
    """거래 수익률이 모두 +0.39%로 같으면 「평균이 0 이하」가 아니라 「추정할 수 없음」."""
    r = _returns()
    trades = [t._replace(ret=1 / 256) for t in _trades(r)]
    g = _build(r, trades)
    assert g["judgment_hold"]["trades_needed"] is None
    text = " ".join(summary(g, "보통"))
    assert "거래 수익률이 모두 같아(또는 1건) 엣지 0을 가릴 거래 수를 추정할 수 없음" in text and "평균이 0 이하" not in text


def test_regime_wording_only_when_line_moves_with_block_length():
    """블록 길이에 따라 선이 20%보다 더 흔들리면 국면 지속 오탐률을 같이 보이고, 독립 수익이면 안 보인다."""
    rng = np.random.default_rng(2)
    iid = _returns()
    g = _build(iid, _trades(iid))
    assert max(g["warning_line"]["block_spread"].values()) < 0.2
    assert "위쪽은 국면이 이어지는 전략" not in summary(g, "보통")[1]
    assert "~" not in summary(g, "보통")[1].split("1년 내내 보면")[1]  # 범위가 아니면 1년도 숫자 하나
    seg = np.repeat(rng.choice([0.012, -0.010], size=16), 50) + rng.normal(0, 0.01, 800)  # 50일씩 오르내림이 이어짐
    r = pd.Series(seg, index=iid.index)
    g = _build(r, _trades(r))
    wl = g["warning_line"]
    assert wl["block_spread"]["보통"] > 0.3 and wl["regime_month_rate"]["보통"] == round(0.0184 * 27.0 / 5, 6)
    line = summary(g, "보통")[1]
    assert "한 달 기준 약 1.84%~9.9%(위쪽은 국면이 이어지는 전략 · 미검증) · 1년 내내 보면" in line
    year = f"{wl['year_rate']['보통']:.0%}~{year_false_alarm(wl['regime_month_rate']['보통']):.0%}"
    assert year == "20%~72%" and f"1년 내내 보면 약 {year} · 다른 칸" in line  # 한 달만 범위면 1년이 낮게 읽힘
    assert "국면 지속 모형" in wl["regime_basis"] and "160건이면 5.40배" in wl["regime_basis"]
    many = _build(r, _trades(r, every=3))  # 266건 ≥ 212건이면 2.86배
    assert many["warning_line"]["regime_month_rate"]["민감"] == round(0.05 * 14.3 / 5, 6)


def test_build_refuses_empty_or_one_bar_returns():
    """수익률이 비었거나 1봉이면 호출자 가드 대신 build가 막는다(metrics는 빈 · 1봉에서 예외)."""
    r = _returns()
    for n in (0, 1):
        short = r.iloc[:n]
        with pytest.raises(GuardrailError, match="가드레일을 만들 수 없음: 백테스트 수익률 " + f"{n}봉"):
            build(short, _trades(r), _stats(), 86400, {})  # 시도 수는 이 앞에서 안 쓴다


def test_build_refuses_all_positive_returns():
    """손실이 한 번도 없는 수익률은 경고선이 0%라 만들지 않는다(모두 양수면 선 0.0)."""
    r = _returns()
    r[:] = 0.001 + np.abs(np.random.default_rng(3).normal(0, 0.002, len(r)))
    with pytest.raises(GuardrailError, match="가드레일을 만들 수 없음: 백테스트에 손실 구간이 거의 없어 경고선이 0%"):
        _build(r, _trades(r))


def test_build_zero_bar_trades_keep_block_at_least_one():
    """보유 0봉 거래만 있어도 블록 길이는 1 이상이라 band가 0으로 나누지 않는다(block=0)."""
    r = _returns()
    zero = [t._replace(bars=0) for t in _trades(r)]
    assert _build(r, zero)["basis"]["block_periods"] == [1, 1, 2]


def test_build_refuses_when_only_the_sensitive_line_is_zero():
    """손실일이 하루뿐이라 민감 칸 선만 0.0이어도 만들지 않는다(선 0.0은 오탐률 100%)."""
    rng = np.random.default_rng(5)
    r = _returns()
    r[:] = 0.002 + np.abs(rng.normal(0, 0.001, len(r)))
    r.iloc[rng.choice(len(r), 1, replace=False)[0]] = -0.05
    gen = np.random.default_rng(SEED)  # build와 같은 순서(블록 2 -> 3 -> 6)로 가운데 선을 확인
    lines = [band(r.to_numpy(), 30, b, gen)["lines"] for b in (2, 3, 6)][1]
    assert lines["민감"] == 0.0 and lines["보통"] < 0  # 경계: 민감만 0
    with pytest.raises(GuardrailError, match="경고선이 0%"):
        _build(r, _trades(r))


def test_dull_rate_always_unverified():
    """둔감 칸 오탐률엔 늘 「미검증」(꼬리 굵은 수익률에서 명목의 2~3배), 다른 칸은 그대로."""
    r = _returns()
    g = _build(r, _trades(r))
    assert "한 달 기준 약 0.43%(미검증) · 1년 내내 보면 약 5%(미검증) · 다른 칸" in summary(g, "둔감")[1]
    assert "미검증" not in summary(g, "보통")[1]


def test_one_calendar_year_of_daily_bars_draws_range():
    """2023-01-01 ~ 12-31 일봉은 덮는 기간이 365일이라 범위를 그린다(첫 봉 ~ 마지막 봉 364일 + 봉 하나)."""
    idx = pd.date_range("2023-01-01", "2023-12-31", freq="D", tz="UTC")
    r = pd.Series(np.random.default_rng(0).normal(0.001, 0.02, len(idx)), index=idx)
    g = _build(r, _trades(r, every=2))
    assert g["normal_range"]["drawn"] is True
    short = _build(r.iloc[1:], _trades(r.iloc[1:], every=2))
    assert short["normal_range"]["drawn"] is False and "기간 364일" in short["normal_range"]["reason"]


def test_empty_side_regime_has_no_mean():
    """횡보 거래 0건이면 평균을 0으로 채우지 않는다: 파일에 mean 키 없음 · 화면 「평균 없음」."""
    r = _returns()
    stats = _stats()
    stats["횡보"] = {"trades": 0}
    g = _build(r, _trades(r), stats)
    assert g["thesis"]["regimes"]["횡보"] == {"trades": 0} and g["thesis"]["rule"] == "논지 없음"
    assert summary(g, "보통")[-2] == "논지: 논지 없음(횡보 거래 0건, 평균 없음)"


def test_basis_has_trade_percentiles():
    """E 띠: 보수적 비용 왕복 거래 수익의 10% · 90% 분위(가운데 80%), 알려진 배열과 직접 비교."""
    r = _returns()
    trades = _trades(r)
    b = _build(r, trades)["basis"]
    rets = [t.ret for t in trades]
    assert b["trade_p10"] == pytest.approx(np.percentile(rets, 10), abs=1e-6)
    assert b["trade_p90"] == pytest.approx(np.percentile(rets, 90), abs=1e-6)
    assert b["trade_p10"] < b["trade_mean"] < b["trade_p90"]


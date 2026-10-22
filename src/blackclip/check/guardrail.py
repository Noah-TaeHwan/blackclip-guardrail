"""가드레일 숫자: 정상 범위 · 경고선(민감도 세 칸) · 판단 보류 · 거래 빈도 · 논지.

기본값은 이 제품의 의견이 아니라 고객 자신의 백테스트(보수적 비용 기준)에서 나온다. 시도 수 보정은
범위에 넣지 않는다. 시도 수 숫자는 가드레일에 남기고 판단 보류 옆에 보이기만 한다(판단 보류 계산에는 아직 안 씀, NOTE와 같음).
"""
from __future__ import annotations

import hashlib
import math

import numpy as np
import pandas as pd

from blackclip.check.band import PATHS, SEED, band, periods
from blackclip.check.c2 import CONSERVATIVE_BP
from blackclip.check.c4 import MIN_TRADES
from blackclip.check.link import COLUMNS, INTERVALS
from blackclip.check.trades import Trade
from blackclip.sim.warning_line import SENSITIVITY, year_false_alarm

DAYS = 30
"""정상 범위 · 경고선 구간(일). 실전 시작일부터 30일씩 끊고 구간마다 0에서 누적."""

MIN_BAND_TRADES = 100
"""백테스트 왕복 거래가 이보다 적으면 정상 범위를 그리지 않는다(100건에서 실측)."""

MIN_BAND_DAYS = 365
"""백테스트 기간이 이보다 짧으면 정상 범위를 그리지 않는다."""

HOLD_EDGE0 = 0.5
"""엣지가 0이어도 30일 범위 안일 확률이 이 이상이면 30건을 넘어도 판단 보류."""

THESIS_DAYS = 60
"""횡보 거래 평균이 0 이하인 전략의 논지 알림: 횡보가 이 날수를 넘게 이어지면(두 달)."""

NOTE = "보수적 비용 기준 · 시도 수 보정은 범위에 넣지 않고 판단 보류 옆에 숫자로 적어요(판단 보류 계산에는 아직 안 써요)"
"""필수 문구를 지금 구현에 맞춘 것(처음 문구 「시도 수 보정은 판단 보류에 반영」은 판단 보류 계산에 넣은 뒤 되돌림)."""

BLOCK_SPREAD = 0.2
"""블록 길이 L/2 · L · 2L 사이 경고선이 L 값의 이 비율보다 더 흔들리면 국면 지속 오탐률도 같이 보인다."""

REGIME_FACTOR: tuple[tuple[int, float], tuple[int, float]] = ((212, 27.0 / 5), (10**9, 14.3 / 5))
"""(백테스트 왕복 거래 수 미만, 오탐률 배수). 국면 지속 모형(블록 50 + 이중) 동시 하한 실측: 100건 27.0% ·
212건 14.3%(명목 5%) → 212건 미만이면 5.4배, 이상이면 2.86배(다른 민감도로 배수를 옮긴 것은 미검증)."""

LIMITS = ("국면이 이어지는 전략은 범위가 좁게 나오는 한계가 남음(국면 지속 모형, 블록 + 이중에서도 "
          "백테스트 100건 24.6% · 212건 13.8%, 명목 5%) · 백테스트를 믿을 수 있고 미래가 과거와 같은 분포일 때만 의미")
"""한계 문구(국면 지속 모형으로 잰 숫자)."""


class GuardrailError(ValueError):
    """가드레일을 만들 수 없을 때(거래 0건 · 30일보다 짧음 · 자산 0 이하 · 하루보다 긴 봉 · 손실 구간 없음). 메시지는 「가드레일을 만들 수 없음: …」."""


def _r(x: float) -> float:
    """파일에 쓸 소수(6자리, 같은 입력이면 같은 글자).

    Args:
        x: 값.

    Returns:
        반올림한 float.
    """
    return round(float(x), 6)


def prices_sha256(prices: dict[str, pd.DataFrame]) -> str:
    """가격표 해시(종목 이름 · 시각 정수 · OHLC 바이트, 종목 순). npz는 zip 시각이 들어가 쓰지 않는다.

    Args:
        prices: 종목 → 가격표.

    Returns:
        16진 sha256.
    """
    h = hashlib.sha256()
    for sym in sorted(prices):
        p = prices[sym]
        h.update(sym.encode())
        h.update(p.index.as_unit("ns").asi8.tobytes())
        h.update(np.ascontiguousarray(p[list(COLUMNS)].to_numpy(dtype=float)).tobytes())
    return h.hexdigest()


def build(returns: pd.Series, trades: list[Trade], stats: dict[str, dict[str, float]], bar_s: int, c3: dict) -> dict:
    """보수적 비용 수익률 · 왕복 거래로 가드레일 숫자를 낸다(민감도 세 칸 모두, 고르는 것은 save).

    난수는 여기서 만든 default_rng(SEED) 하나를 블록 길이 L/2 → L → 2L 순서로 쓴다(같은 입력이면 같은 숫자).
    trades는 호출자가 `round_trips(..., CONSERVATIVE_BP)`로 만들어야 한다(cost.one_way_bp와 국면 문구의 왕복 비용이
    그 가정이고, 여기서는 거래에 실제로 뺀 비용을 알 수 없다).

    Args:
        returns: 보수적 비용으로 다시 돌린 봉별 수익률.
        trades: 보수적 비용 왕복 거래.
        stats: c4.regime_stats 결과(논지).
        bar_s: 봉 길이(초).
        c3: c3.deflate 결과(파일에 그대로 남김).

    Returns:
        가드레일 숫자 dict(JSON으로 쓸 수 있는 값만).

    Raises:
        GuardrailError: 하루보다 긴 봉 · 거래 0건 · 수익률 2봉 미만 · 백테스트 자산 0 이하 · 30일 이하 ·
            손실 구간이 없어 경고선이 0%일 때.
    """
    if bar_s > 86400:  # 하루 경로를 못 만들어 30일 · 하루 1회 확인과 단위가 안 맞는다
        raise GuardrailError(f"가드레일을 만들 수 없어요: 봉 길이 {INTERVALS.get(bar_s, f'{bar_s:,}초')}"
                             "(이 버전은 하루 이하 봉만 되고, 다섯 점검은 그대로 봐요)")
    if not trades:
        raise GuardrailError("가드레일을 만들 수 없음: 왕복 거래 0건")
    if len(returns) < 2:  # 표준편차 · 기간 길이를 못 구한다(metrics도 빈 · 1봉에서 예외)
        raise GuardrailError(f"가드레일을 만들 수 없음: 백테스트 수익률 {len(returns)}봉(2봉 이상 필요)")
    if bool(((1 + returns).cumprod() <= 0).any()):  # 파산한 백테스트로는 범위를 낼 수 없다
        raise GuardrailError("가드레일을 만들 수 없음: 백테스트 자산이 0 이하로 떨어짐")
    r, step = periods(returns, bar_s)
    horizon = max(1, round(DAYS * 86400 / step))
    if len(r) <= horizon:
        raise GuardrailError(f"가드레일을 만들 수 없음: 백테스트가 {DAYS}일보다 짧음")
    span = (returns.index[-1] - returns.index[0]).total_seconds() / 86400
    covered = span + bar_s / 86400  # 덮는 기간(마지막 봉 하나 포함): 1년 일봉이 365일
    hold = sum(t.bars for t in trades) / len(trades)
    mid = max(1, round(hold * bar_s / step))  # 블록 길이는 1 이상(band가 1/block으로 나눈다)
    blocks = (max(1, round(mid / 2)), mid, 2 * mid)
    rng = np.random.default_rng(SEED)
    runs = [band(r.to_numpy(dtype=float), horizon, b, rng) for b in blocks]
    main = runs[1]
    if any(v >= 0 for v in main["lines"].values()):  # 한 칸이라도(=민감) 0이면 거절 — 선 0.0은 오탐률 100%라 경고선 구실을 못 함
        raise GuardrailError("가드레일을 만들 수 없음: 백테스트에 손실 구간이 거의 없어 경고선이 0%")
    drawn = len(trades) >= MIN_BAND_TRADES and covered >= MIN_BAND_DAYS
    rets = np.array([t.ret for t in trades])
    mu, sd = float(rets.mean()), float(rets.std(ddof=1)) if len(rets) > 1 else 0.0
    exits = pd.Series(1, index=pd.DatetimeIndex([t.exit for t in trades]).floor("D")).groupby(level=0).sum()
    days = pd.date_range(returns.index[0].floor("D"), returns.index[-1].floor("D"), freq="D")
    week = exits.reindex(days, fill_value=0).rolling(7).sum().dropna().to_numpy()
    side = stats["횡보"]
    factor = next(f for limit, f in REGIME_FACTOR if len(trades) < limit)
    spread = {k: _r((max(x["lines"][k] for x in runs) - min(x["lines"][k] for x in runs)) / abs(main["lines"][k])
                    if main["lines"][k] else 0.0) for k in SENSITIVITY}
    return {
        "cost": {"one_way_bp": CONSERVATIVE_BP, "note": NOTE},
        "normal_range": ({"days": DAYS, "drawn": True, "coverage": 0.9, "low": _r(main["low"][-1]), "high": _r(main["high"][-1]),
                          "daily_low": [_r(x) for x in main["low"]], "daily_high": [_r(x) for x in main["high"]]}
                         if drawn else
                         {"days": DAYS, "drawn": False,
                          "reason": f"백테스트 왕복 거래 {len(trades):,}건 · 기간 {covered:,.0f}일(하한 {MIN_BAND_TRADES}건 · "
                                    f"{MIN_BAND_DAYS}일)"}),
        "warning_line": {"days": DAYS, "checks_per_day": 1,
                         "window": "실전 시작일부터 30일씩 끊은 구간, 구간마다 0에서 복리 누적",
                         "lines": {k: _r(v) for k, v in main["lines"].items()},
                         "month_rate": dict(SENSITIVITY),
                         "year_rate": {k: _r(year_false_alarm(v)) for k, v in SENSITIVITY.items()},
                         "block_spread": spread,
                         "regime_month_rate": {k: _r(min(1.0, v * factor)) for k, v in SENSITIVITY.items()},
                         "regime_basis": f"국면 지속 모형 동시 하한 실측(100건 27.0% · 212건 14.3%, 명목 5%)에서 "
                                         f"백테스트 왕복 {len(trades):,}건이면 {factor:.2f}배, 다른 칸으로 옮긴 것은 미검증"},
        "judgment_hold": {"min_trades": MIN_TRADES, "hold_if_edge0_at_least": HOLD_EDGE0,
                          "edge0_in_range": [_r(x) for x in main["edge0"]] if drawn else None,
                          "trades_needed": math.ceil(((1.645 + 0.842) * sd / mu) ** 2) if mu > 0 and sd > 0 else None,
                          "months_to_min_trades": _r(MIN_TRADES / (len(trades) / (span / 30.4375)))},
        "trade_frequency": {"days": 7, "basis": "청산 시각",
                            "low": int(np.percentile(week, 5, method="lower")) if len(week) else None,
                            "high": int(np.percentile(week, 95, method="higher")) if len(week) else None},
        "thesis": {"rule": (f"횡보 {THESIS_DAYS}일 넘게 이어지면 「전제와 안 맞는 장」 알림"
                            if side["trades"] and side["mean"] <= 0 else "논지 없음"),
                   "regimes": {k: {"trades": int(v["trades"]), **({"mean": _r(v["mean"])} if "mean" in v else {})}
                               for k, v in stats.items()}},
        "period": {"seconds": step, "mean": _r(r.mean()), "std": _r(r.std(ddof=1))},
        "c3": c3,
        "basis": {"paths": PATHS, "seed": SEED, "block_periods": list(blocks),
                  "low_by_block": [_r(x["low"][-1]) for x in runs],
                  "line_by_block": [{k: _r(v) for k, v in x["lines"].items()} for x in runs],
                  "trades": len(trades), "mean_hold_bars": _r(hold), "trade_mean": _r(mu), "trade_std": _r(sd),
                  "trade_p10": _r(np.percentile(rets, 10)) if len(rets) else None,
                  "trade_p90": _r(np.percentile(rets, 90)) if len(rets) else None,
                  "limits": LIMITS},
    }


def summary(g: dict, sensitivity: str) -> list[str]:
    """가드레일을 고객 말로 줄마다(결론 → 근거, 오탐률에는 기간을 붙임).

    Args:
        g: build 결과.
        sensitivity: 민감도 이름(SENSITIVITY 키).

    Returns:
        줄 목록.
    """
    nr, wl, jh, tf = g["normal_range"], g["warning_line"], g["judgment_hold"], g["trade_frequency"]
    lines = wl["lines"]
    # 선이 블록 길이에 흔들리거나 범위를 못 그릴 만큼 거래가 적으면 한 달 · 1년 모두 범위로
    why = [w for w, on in (("국면이 이어지는 전략", wl["block_spread"][sensitivity] > BLOCK_SPREAD),
                           ("거래 수나 기간이 하한에 못 미친 백테스트", not nr.get("drawn"))) if on]
    wide = bool(why)
    others = " · ".join(f"{k} {lines[k]:.1%}" for k in SENSITIVITY if k != sensitivity)  # 파일은 키 정렬이라 순서를 따로
    out = [f"앞으로 한 달 90% 정상 범위 {nr['low']:.1%} ~ {nr['high']:+.1%}" if "low" in nr
           else f"정상 범위 그리지 않음({nr['reason']})",
           f"경고선 {lines[sensitivity]:.1%}(민감도 {sensitivity}: 전략이 멀쩡해도 울릴 확률 한 달 기준 약 "
           f"{wl['month_rate'][sensitivity]:.2%}"
           + ("(미검증)" if sensitivity == "둔감" else "")  # 꼬리 굵은 수익률에서 명목의 2~3배
           + (f"~{wl['regime_month_rate'][sensitivity]:.1%}(위쪽은 {' · '.join(why)} · 미검증)" if wide else "")
           + f" · 1년 내내 보면 약 {wl['year_rate'][sensitivity]:.0%}"
           + (f"~{year_false_alarm(wl['regime_month_rate'][sensitivity]):.0%}" if wide else "")
           + ("(미검증)" if sensitivity == "둔감" else "")  # 1년도 같은 한 달 비율에서 나옴
           + f" · 다른 칸 {others})"]
    hold = f"판단 보류: 실전 거래 {jh['min_trades']}건 전"
    if jh["edge0_in_range"] is None:
        hold += " · 범위를 그리지 않아 엣지 0 확률 모름(보류가 이어짐)"
    else:
        hold += (f" · 엣지가 없어도 {g['normal_range']['days']}일 범위 안일 확률 {jh['edge0_in_range'][-1]:.0%}"
                 f"({jh['hold_if_edge0_at_least']:.0%} 이상이면 보류)")
    if jh["trades_needed"]:
        hold += f" · 엣지 0을 가리려면 약 {jh['trades_needed']:,}건"
    elif g["basis"]["trade_mean"] <= 0:
        hold += " · 거래 1건 평균이 0 이하라 엣지 0을 가릴 거래 수 모름"
    else:  # 평균은 양수인데 표준편차 0(거래 1건이거나 수익률이 모두 같음)
        hold += " · 거래 수익률이 모두 같아(또는 1건) 엣지 0을 가릴 거래 수를 추정할 수 없음"
    c3 = g["c3"]
    if c3["adjusted_low"] is not None:
        hold += (f" · 시도 수: 시도 {c3['trials']:,}개(고객이 밝힌 값 · 최솟값(미검증))면 비용 0 샤프 "
                 f"{c3['sharpe_cost0']:.2f} → {c3['adjusted_low']:.2f}(독립 가정)")
    elif c3["table"] is not None:
        zero = f"약 {c3['zero_at_trials']:,}개" if c3["zero_at_trials"] else "2^40개를 넘음"
        hold += f" · 시도 수 모름: 비용 0 샤프 {c3['sharpe_cost0']:.2f} · 0 이하가 되는 시도 수 {zero}(독립 가정)"
    else:
        hold += f" · 시도 수: 비용 0 샤프 {c3['sharpe_cost0']:.2f} · 0 이하라 깎을 것 없음"
    out.append(hold + f" · 이 빈도라면 {jh['min_trades']}건까지 약 {jh['months_to_min_trades']:.1f}개월")
    if tf["low"] is not None:
        out.append(f"거래 빈도 7일 {tf['low']}~{tf['high']}건(백테스트 가운데 90%, 청산 시각 기준)")
    side = g["thesis"]["regimes"]["횡보"]
    mean = f"평균 {side['mean']:+.2%}" if "mean" in side else "평균 없음"  # 0건이면 0으로 채우지 않음
    out.append(f"논지: {g['thesis']['rule']}(횡보 거래 {side['trades']:,}건, {mean})")
    out.append(NOTE)
    return out

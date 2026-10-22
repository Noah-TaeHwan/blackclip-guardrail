"""지어낸 일별 기록(FACTS 숫자): 여러 시험이 같이 쓴다.

trend_10_50: 실전 2026-09-23 ~ 10-14(22일) · 거래 37건 · 실제 -4.6% · 같은 기간 BT -1.9% · 차이 -2.7%p
= 나머지 -2.1%p(놓친 주문 10/13 15:58 매도로 추정) + 체결가 차이 -0.4%p(중앙값 -5.3bp · 최대 -9.1bp) + 수수료 -0.2%p.
한 달 정상 범위 -3.7% ~ +8.1%(90%), 그날 범위 = 한 달 범위 × √(d/30)(지어낸 모양 — 엔진은 가드레일 daily_low/high를 씀),
경고선 -7.2%(민감) · 엣지 0 확률 79% · 최근 7일 11건(기준 7~17건). 같은 기간 BT는 비용 0(bt0)과 보수적 비용(bt)을 같은 값으로 둔다.
다른 셋: trend_ema_v7 정상 +1.2% · breakout_v2 10/12부터 체결을 못 받음(+0.4%, 10/11까지) · grid_v1 판단 보류(거래 12건).
날마다 실제 누적은 터미널 차트 D의 스파크라인 글자를 되돌린 값이다(줄마다 6단계 ▁~▆, 한 칸 = SCALE 간격).
"""
import json
import math
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path

from blackclip.notify import write_live
from blackclip.states import TITLES, pp, representative
from blackclip.workspace import ensure

START = date(2026, 9, 23)
DAYS = 22
NAMES = ("trend_10_50", "trend_ema_v7", "breakout_v2", "grid_v1")
SPARK = {"trend_10_50": "▆▆▅▆▆▅▅▅▅▅▅▄▅▄▄▄▄▄▄▃▁▁", "trend_ema_v7": "▁▁▁▂▂▂▃▃▃▃▄▃▄▅▄▅▆▅▆▆▆▆",
         "breakout_v2": "▁▂▃▂▃▅▃▅▆▅▅▆▅▆▆▅▆▆▆···", "grid_v1": "▆▅▆▅▅▅▄▅▄▃▄▃▃▃▃▂▃▂▂▂▂▁"}
SCALE = {"trend_10_50": (-0.046, 0.01), "trend_ema_v7": (-0.003, 0.003), "breakout_v2": (-0.006, 0.002),
         "grid_v1": (-0.008, 0.002)}
END = {"trend_10_50": {"fee": -0.002, "slip": -0.004, "trades": 37, "edge0": 0.79},
       "trend_ema_v7": {"fee": -0.0015, "slip": -0.0005, "trades": 41, "edge0": 0.41},
       "breakout_v2": {"fee": -0.0008, "slip": -0.0004, "trades": 33, "edge0": 0.62},
       "grid_v1": {"fee": -0.002, "slip": -0.001, "trades": 12, "edge0": 0.83}}
TRADE_RETS = [0.025, -0.035] * 18 + [-0.0494]
"""trend_10_50 실전 내내 거래 1건 수익 37개(평균 -0.62%). 마지막 날 줄에만 둔다."""
MISSED = {"t": "2026-10-13T15:58:00+00:00", "symbol": "BTCUSDT", "side": "sell"}
RESPONSE = {"execution_warning": "주문 코드부터 확인. 전략은 그대로 둔다", "warning_line": "중단 검토. 멈출지는 내가 정한다"}


def actual(name: str, k: int) -> float | None:
    """k일째(1부터) 실제 누적: 스파크라인 글자 → 값, 「·」은 모름."""
    ch = SPARK[name][k - 1]
    if ch == "·":
        return None
    lo, step = SCALE[name]
    return round(lo + "▁▂▃▄▅▆".index(ch) * step, 6)


def record(name: str, k: int) -> dict:
    """k일째 기록 한 줄(엔진 days.jsonl과 같은 모양, 근거 글은 엔진 reconcile 글 그대로 흉내)."""
    day, e, a = START + timedelta(days=k - 1), END[name], actual(name, k)
    lo, hi = round(-0.037 * math.sqrt(k / 30), 6), round(0.081 * math.sqrt(k / 30), 6)
    trades = round(e["trades"] * k / DAYS)
    fee, slip = round(e["fee"] * k / DAYS, 6), round(e["slip"] * k / DAYS, 6)
    missed = [dict(MISSED)] if name == "trend_10_50" and k >= 21 else []
    rest = -0.021 if missed else 0.0
    rng = f"정상 범위 {lo:+.1%} ~ {hi:+.1%}({k}일째 · 보수적 비용 기준)"
    states = {"전략": "판단 보류", "실행": "경고" if missed else "정상", "봇": "정상" if k >= 7 else "판단 보류",
              "선": "정상", "논지": "해당 없음"}
    if name == "trend_ema_v7" and trades >= 30:
        states["전략"] = "정상"
    if a is None:  # breakout_v2 10/12부터: 엔진 bot_cause 글 그대로(받은 끝 10/12 0시 → 날수 = math.ceil)
        why = f"체결 내역을 10/12 00:00 UTC까지만 받음 · {k - 19}일째 못 받음 · 조회 키 다시 연결(blackclip connect --open)"
        states.update({"실행": "확인 불가(체결 내역을 못 받음)", "봇": "확인 불가", "선": "확인 불가"})
        bt = gap = fee = slip = rest = None
        lines = {"전략": f"같은 기간 BT +0.1%({rng} 안) · 엣지가 없어도 범위 안일 확률 62%", "실행": why, "봇": why, "선": why,
                 "논지": "논지 없음"}
        head, bt_show = f"실제 모름 · 같은 기간 BT +0.1% · {rng}", 0.001
    else:
        gap = round(fee + slip + rest, 6)
        bt = bt_show = round(a - gap, 6)
        p = e["edge0"]
        held = f"실전 거래 {trades}건(30건 전) · " if trades < 30 else ""
        strat = (f"같은 기간 BT {bt:+.1%}({rng} 안) · "
                 + (f"실전 거래 {trades}건 · 엣지가 없어도 범위 안일 확률 {p:.0%}" if states["전략"] == "정상"
                    else f"{held}엣지가 없어도 범위 안일 확률 {p:.0%}"))
        cand = " · 원인 후보: 백테스트에만 있는 거래 1건(놓친 주문으로 추정 · 처음 10/13 15:58 UTC BTCUSDT 매도)" if missed else ""
        got = f"체결 {day:%m/%d}분까지 받음"
        low = min(0.0, *(x for j in range(1, k + 1) if (x := actual(name, j)) is not None))
        lines = {"전략": strat,
                 "실행": (f"실제 − BT(비용 0) {pp(gap)} = 수수료 {pp(fee)} + 체결가 차이 {pp(slip)}(중앙값 -5.3bp · 최대 -9.1bp · "
                          f"20bp 넘음 0건) + 나머지 {pp(rest)}(추정{cand})"),
                 "봇": (f"{got} · 최근 7일 11건(백테스트 기준 7~17건, 청산 시각)" if k >= 7
                       else f"{got} · 실전 {k}일째라 7일 거래 빈도는 아직 비교 안 함"),
                 "선": f"실제 {a:+.1%}(이번 구간 최저 {low:+.1%}) · 경고선 -7.2%(민감도 민감 · 하루 1회 확인 기준)",
                 "논지": "논지 없음"}
        head = f"실제 {a:+.1%} · 같은 기간 BT {bt:+.1%} · {rng}"
    rep, key = representative(states)
    pos = None if a is None else ("아래" if a < lo else "위" if a > hi else "안")
    return {"v": 1, "strategy": name, "day": day.isoformat(), "live_day": k, "trades": trades, "head": head,
            "rep": rep, "title": TITLES[key], "states": states, "lines": lines, "response": dict(RESPONSE),
            "nums": {"period_start": START.isoformat(), "d": k, "actual": a, "bt": bt_show if a is not None else 0.001,
                     "bt0": bt_show, "gap": gap, "fee": fee, "slip": slip, "funding": None, "rest": rest, "lo": lo,
                     "hi": hi, "month_lo": -0.037, "month_hi": 0.081, "line": -0.072, "sensitivity": "민감",
                     "room": None if a is None else round(a + 0.072, 6), "edge0": e["edge0"], "min_trades": 30,
                     "n7": 11 if k >= 7 and a is not None else None, "tf_low": 7, "tf_high": 17,
                     "slip_med_bp": None if a is None else -5.3, "slip_max_bp": None if a is None else -9.1,
                     "missed": missed, "missed_n": len(missed), "extra": 0, "pos": pos, "bt_pos": "안",
                     "all": {"start": START.isoformat(), "actual": a, "bt": bt_show if a is not None else 0.001,
                             "bt0": bt_show, "gap": gap, "fee": fee, "slip": slip, "funding": None, "rest": rest,
                             "missed_n": len(missed), "missed": missed, "extra": 0},  # 22일 = 첫 구간 → 구간 값 복사
                     **_trade_nums(name, k)}}


def _trade_nums(name: str, k: int) -> dict:
    """E 차트 칸(trade_rets · trade_mean · trade_n · trade_skip): trend_10_50의 마지막 날만 있고 나머지는 칸이 없다."""
    if name != "trend_10_50" or k != DAYS:
        return {}
    return {"trade_rets": list(TRADE_RETS), "trade_mean": round(sum(TRADE_RETS) / len(TRADE_RETS), 6),
            "trade_n": len(TRADE_RETS), "trade_skip": 0}


def guardrail(name: str) -> dict:
    """가드레일 v1(읽는 칸만 · 지어낸 값)."""
    return {"format": 1, "strategy": name, "version": 1, "live_start": START.isoformat(),
            "checks": {"C0": "재현 확인 전", "C1": "이 범위에서 통과", "C2": "축소", "C3": "모름", "C4": "충분"},
            "target": {"market": "spot", "symbols": ["BTCUSDT"], "bar_seconds": 3600, "exec": "next_open",
                       "start": "2024-01-01", "link_sha256": "0" * 64},  # 3판 계약
            "normal_range": {"days": 30, "drawn": True, "coverage": 0.9, "low": -0.037, "high": 0.081,
                             "daily_low": [round(-0.037 * math.sqrt(k / 30), 6) for k in range(1, 31)],
                             "daily_high": [round(0.081 * math.sqrt(k / 30), 6) for k in range(1, 31)]},
            "warning_line": {"line": -0.072, "sensitivity": "민감", "lines": {"민감": -0.072, "보통": -0.09, "둔감": -0.11}},
            "judgment_hold": {"min_trades": 30, "hold_if_edge0_at_least": 0.5, "edge0_in_range": [END[name]["edge0"]]},
            "thesis": {"rule": "논지 없음"}, "period": {"mean": 0.0001, "std": 0.01},
            "trade_frequency": {"days": 7, "low": 7, "high": 17},
            "basis": {"trade_mean": 0.0027, "trade_std": 0.035, "trade_p10": -0.04, "trade_p90": 0.05}, "response": dict(RESPONSE)}


def write_facts(repo: Path, names: tuple[str, ...] = NAMES) -> Path:
    """repo를 Git 레포로 보고 .blackclip에 FACTS 전략들(가드레일 v1 · live.json · days.jsonl · state.log)을 쓴다.

    Args:
        repo: 레포 꼭대기(없으면 만듦).
        names: 쓸 전략.

    Returns:
        .blackclip 경로.
    """
    (repo / ".git").mkdir(parents=True, exist_ok=True)
    root = ensure(repo)
    log = []
    for n in names:
        d = root / "strategies" / n
        (d / "daily").mkdir(parents=True, exist_ok=True)
        (d / "guardrail.v1.yaml").write_text(json.dumps(guardrail(n), ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        write_live(root, n, "main", "USDT", datetime(2026, 9, 22, 12, tzinfo=timezone.utc), Decimal("10000"), {})
        recs = [record(n, k) for k in range(1, DAYS + 1)]
        (d / "daily" / "days.jsonl").write_text("".join(json.dumps(r, ensure_ascii=False) + "\n" for r in recs), encoding="utf-8")
        prev = None
        for r in recs:  # daily _log_state처럼 바뀐 날만
            if r["states"] != prev:
                log.append({"day": r["day"], "states": r["states"], "strategy": n})
                prev = r["states"]
    log.sort(key=lambda x: (x["day"], x["strategy"]))
    (root / "state.log").write_text("".join(json.dumps(x, ensure_ascii=False, sort_keys=True) + "\n" for x in log), encoding="utf-8")
    return root

"""매일 대조 판정: 손익 3줄 · 원인 셋(전략 · 실행 · 봇) + 가드레일 선 · 논지, 대표 상태, 파일 · 메시지 글.

숫자와 상태는 모두 여기서 코드가 낸다. 실행 쪽 원인은 「추정」을 붙이고, 잰 것(수수료 · 체결가 차이 · 펀딩비)과
원인 후보(나머지)를 나눈다.
"""
from __future__ import annotations

import math
import statistics
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal
from typing import Any

import pandas as pd

from blackclip.check.c4 import regimes
from blackclip.check.guardrail import THESIS_DAYS
from blackclip.daily.actual import Actual
from blackclip.daily.days import TRADE_KEEP
from blackclip.daily.live import LiveRun
from blackclip.retro.fills import Execution
from blackclip.retro.report import SLIP_LIMIT_BP, Retro
from blackclip.states import LABELS, ORDER, TITLES, TRACKED, base, pp, representative  # noqa: F401 (다시 내보냄)

WEEKLY_DAY = 6
"""매주 회고 한 줄을 붙이는 요일(막 끝난 UTC 날이 일요일)."""

MIXED_OVERLAP = "같은 계좌에서 종목이 겹치는 다른 전략이 있어 이 전략 몫을 나눌 수 없음(계좌를 나누면 따로 봄)"
"""섞인 기록 사유: 같은 계좌 · 시장에서 종목이 실제로 겹치는 가드레일 전략이 있음."""

MIXED_LOST = "같은 계좌에 가드레일을 읽지 못한 다른 전략이 있어 이 전략 몫을 나눌 수 없음(가드레일을 고치면 따로 봄)"
"""섞인 기록 사유: 같은 계좌에 가드레일을 읽지 못한 전략이 있음 — 그 전략의 종목은 몰라 겹친다고 단정하지 않는다."""


@dataclass(frozen=True)
class Day:
    """전략 하나의 하루 대조 결과.

    Attributes:
        strategy: 전략 이름.
        day: 막 끝난 UTC 날(파일 이름).
        live_day: 실전 며칠째(끝 제외 날수).
        trades: 실전 시작부터 왕복 거래 수(실제 체결 기준).
        head: 손익 3줄 한 줄.
        states: TRACKED 칸 → 상태(「점검 불가(사유)」처럼 괄호 사유 포함 가능).
        lines: TRACKED 칸 → 근거 한 줄.
        response: 가드레일 대응 두 칸(execution_warning · warning_line).
        week: 매주 회고 한 줄(일요일 아니면 None).
        nums: 기계 판독 숫자(days.jsonl의 nums, 모든 칸이 선택). 대조가 예외로 끝난 날은 빈 dict.
    """

    strategy: str
    day: date
    live_day: int
    trades: int
    head: str
    states: dict[str, str]
    lines: dict[str, str]
    response: dict[str, str]
    week: str | None
    nums: dict[str, Any] = field(default_factory=dict)


def period(live_start: datetime, end: datetime) -> tuple[datetime, int]:
    """이번 30일 구간의 처음과 그 구간 며칠째(실전 시작일부터 30일씩 끊음, 가드레일 30일 범위와 같은 창).

    Args:
        live_start: 실전 시작(UTC 0시).
        end: 기간 끝(제외, UTC 0시). live_start보다 뒤.

    Returns:
        (구간 처음, 1~30).
    """
    days = (end - live_start).days
    k = (days - 1) // 30
    return live_start + timedelta(days=30 * k), days - 30 * k


def _when(e: Execution) -> str:
    """체결 한 줄 표기(UTC).

    Args:
        e: 체결.

    Returns:
        「10/13 15:58 UTC BTCUSDT 매도」.
    """
    return f"{e.time.astimezone(timezone.utc):%m/%d %H:%M} UTC {e.symbol} {'매수' if e.side == 'buy' else '매도'}"


def strategy_cause(g: dict, bt_cum: float, d: int, trades: int) -> tuple[str, str]:
    """전략 축: 같은 기간 BT(보수적 비용) vs 정상 범위 · 판단 보류.

    Args:
        g: 가드레일 파일 내용.
        bt_cum: 이번 구간 같은 기간 BT 누적(보수적 비용).
        d: 구간 며칠째(1~30).
        trades: 실전 왕복 거래 수.

    Returns:
        (상태, 근거).
    """
    nr, jh = g["normal_range"], g["judgment_hold"]
    bt = f"같은 기간 BT {bt_cum:+.1%}"
    if "daily_low" in nr:
        lo, hi = nr["daily_low"][d - 1], nr["daily_high"][d - 1]
        rng = f"정상 범위 {lo:+.1%} ~ {hi:+.1%}({d}일째 · 보수적 비용 기준)"
        if bt_cum < lo:
            return "경고", f"{bt} · {rng} 아래"
        bt += f"({rng} {'위' if bt_cum > hi else '안'})"
    else:
        bt += "(정상 범위 그리지 않음)"
    p = jh["edge0_in_range"][-1] if jh["edge0_in_range"] else None
    reasons = []
    if trades < jh["min_trades"]:
        reasons.append(f"실전 거래 {trades}건({jh['min_trades']}건 전)")
    if p is None:
        reasons.append("엣지 0 확률 모름")
    elif p >= jh["hold_if_edge0_at_least"]:
        reasons.append(f"엣지가 없어도 범위 안일 확률 {p:.0%}")
    if reasons:
        return "판단 보류", f"{bt} · " + " · ".join(reasons)
    return "정상", f"{bt} · 실전 거래 {trades}건 · 엣지가 없어도 범위 안일 확률 {p:.0%}"


def execution_numbers(r: Retro, capital: Decimal, gap: float, futures: bool, funding_ok: bool,
                      funding_total: Decimal) -> dict[str, float | int | None]:
    """실행 축의 숫자(비율): 잰 것(수수료 · 체결가 차이 · 펀딩비)과 나머지, 체결가 차이 bp.

    Args:
        r: 짝짓기(build_retro).
        capital: 처음 자산(거래 통화, 실제 · BT 같은 분모).
        gap: 실제 − BT(비용 0), 비율.
        futures: 선물이면 펀딩비 칸을 낸다.
        funding_ok: 펀딩비 받은 범위가 구간을 덮는지.
        funding_total: 가드레일 종목 펀딩비 합(체결이 없어도 셈).

    Returns:
        fee · slip · funding · rest(비율, funding은 현물이거나 범위를 못 덮으면 None) · slip_med_bp · slip_max_bp
        (짝이 없으면 None) · over(체결가 차이가 SLIP_LIMIT_BP를 넘은 짝 수).
    """
    cap = float(capital)
    fee, slip = float(r.fee_actual) / cap, float(r.slip_cost) / cap
    fund = float(funding_total) / cap if futures and funding_ok else None
    return {"fee": fee, "slip": slip, "funding": fund, "rest": gap - fee - slip - (fund or 0.0),
            "slip_med_bp": float(statistics.median(r.slip_bp)) if r.slip_bp else None,
            "slip_max_bp": float(min(r.slip_bp)) if r.slip_bp else None,
            "over": sum(1 for x in r.slip_bp if x < -SLIP_LIMIT_BP)}


def execution_cause(r: Retro, capital: Decimal, gap: float, futures: bool, funding_ok: bool,
                    funding_total: Decimal) -> tuple[str, str]:
    """실행 축: 실제 − BT(비용 0)를 잰 것과 나머지(추정)로 나눈다.

    Args:
        r: 이번 구간 짝짓기(build_retro).
        capital: 이번 구간 처음 자산(거래 통화, 실제 · BT 같은 분모).
        gap: 이번 구간 실제 − BT(비용 0), 비율.
        futures: 선물이면 펀딩비 줄.
        funding_ok: 펀딩비 받은 범위가 구간을 덮는지.
        funding_total: 이번 구간 가드레일 종목 펀딩비 합(체결이 없어도 셈).

    Returns:
        (상태, 근거).
    """
    n = execution_numbers(r, capital, gap, futures, funding_ok, funding_total)
    fee, slip, over = n["fee"], n["slip"], n["over"]
    slips = (f"(중앙값 {n['slip_med_bp']:.1f}bp · 최대 {n['slip_max_bp']:.1f}bp · "
             f"{SLIP_LIMIT_BP}bp 넘음 {over}건)") if r.slip_bp else "(짝 없음)"
    parts = [f"수수료 {pp(fee)}" + (f"(환산 못 함 {r.fee_unconverted}건)" if r.fee_unconverted else ""),
             f"체결가 차이 {pp(slip)}{slips}"]
    if futures:
        parts.append(f"펀딩비 {pp(n['funding'])}" if funding_ok else "펀딩비 확인 불가(받은 범위가 구간을 못 덮음)")
    bt_only, act_only = r.result.backtest_only, r.result.actual_only
    cand = ([f"백테스트에만 있는 거래 {len(bt_only)}건(놓친 주문으로 추정 · 처음 {_when(bt_only[0])})"] if bt_only else []) + \
           ([f"실제에만 있는 거래 {len(act_only)}건"] if act_only else [])
    parts.append(f"나머지 {pp(n['rest'])}(추정" + (" · 원인 후보: " + " · ".join(cand) if cand else "") + ")")
    return ("경고" if bt_only or act_only or over else "정상"), f"실제 − BT(비용 0) {pp(gap)} = " + " + ".join(parts)


def bot_cause(cov_from: datetime | None, cov_until: datetime | None, missing: list[str], end: datetime,
              need_from: datetime, exits: list[datetime], tf: dict, live_days: int, got: str | None = None) -> tuple[str, str]:
    """봇 축: 체결이 처음부터 끝까지 들어오나 · 최근 7일 거래 빈도.

    Args:
        cov_from: 가드레일 종목 모두를 받은 처음(없으면 None).
        cov_until: 가드레일 종목 모두를 받은 끝(없으면 None).
        missing: 받은 기록이 없는 종목.
        end: 기간 끝(제외).
        need_from: 체결이 이때부터 있어야 함(실전 시작 잔고를 받은 시각).
        exits: 실제 왕복 거래 끝 시각.
        tf: 가드레일 trade_frequency(low · high).
        live_days: 실전 며칠째.
        got: 받은 범위 글(자주 보는 시계가 시각까지 넣음). 주면 하루 미만 못 받은 공백을 「N시간째」로 적는다.
            없으면 「체결 MM/DD분까지 받음」 · 「N일째」(end가 0시인 매일 대조).

    Returns:
        (상태, 근거).
    """
    if missing or cov_until is None:
        more = f" 외 {len(missing) - 3}개" if len(missing) > 3 else ""
        return "확인 불가", f"체결 받은 기록 없음({', '.join(missing[:3])}{more}) · blackclip fetch · blackclip connect --status 확인"
    if cov_from is not None and cov_from > need_from:  # 앞이 빈 기록으로 실제 손익을 내지 않는다
        return "확인 불가", (f"체결 내역을 {cov_from.astimezone(timezone.utc):%m/%d %H:%M} UTC부터만 받음 · 실전 시작 잔고를 받은 "
                            f"{need_from.astimezone(timezone.utc):%m/%d %H:%M} UTC 뒤 체결이 빠짐 · "
                            "조회 키 · 연결 확인(선물은 3개월 넘은 기록을 못 받음)")
    if cov_until < end:
        gap = (end - cov_until).total_seconds()
        n = f"{math.ceil(gap / 3600)}시간째" if got and gap < 86400 else f"{math.ceil(gap / 86400)}일째"
        return "확인 불가", (f"체결 내역을 {cov_until.astimezone(timezone.utc):%m/%d %H:%M} UTC까지만 받음 · {n} 못 받음 · "
                            "조회 키 다시 연결(blackclip connect --open)")
    got = got or f"체결 {(end - timedelta(days=1)):%m/%d}분까지 받음"
    if live_days < 7 or tf.get("low") is None:
        return "판단 보류", f"{got} · 실전 {live_days}일째라 7일 거래 빈도는 아직 비교 안 함"
    n7 = sum(1 for t in exits if end - timedelta(days=7) <= t < end)
    line = f"{got} · 최근 7일 {n7}건(백테스트 기준 {tf['low']}~{tf['high']}건, 청산 시각)"
    if n7 < tf["low"]:
        return "경고", line + " · 적음(봇 멈춤 의심)"
    if n7 > tf["high"]:
        return "경고", line + " · 많음(폭주 의심)"
    return "정상", line


def line_cause(cum: pd.Series, wl: dict) -> tuple[str, str]:
    """가드레일 선: 이번 구간 실제 손익 하루 끝 누적의 최솟값 vs 고른 민감도 경고선(하루 1회 확인).

    Args:
        cum: 이번 구간 하루 끝 실제 누적(자본 기준 비율).
        wl: 가드레일 warning_line(line · sensitivity).

    Returns:
        (상태, 근거).
    """
    worst = min(0.0, float(cum.min())) if len(cum) else 0.0
    now = float(cum.iloc[-1]) if len(cum) else 0.0
    text = f"실제 {now:+.1%}(이번 구간 최저 {worst:+.1%}) · 경고선 {wl['line']:.1%}(민감도 {wl['sensitivity']} · 하루 1회 확인 기준)"
    return ("경고" if worst <= wl["line"] else "정상"), text


def sideways_days(frames: dict[str, pd.DataFrame], bar_s: int) -> int | None:
    """종목 절반 이상이 횡보인 날이 끝에서부터 이어진 날수(c4.regimes).

    Args:
        frames: 종목 → 바이낸스 봉.
        bar_s: 봉 길이(초).

    Returns:
        날수, 국면을 셀 봉이 모자라면 None.
    """
    table = pd.DataFrame({s: regimes(f["Close"], bar_s).groupby(f.index.floor("D")).last() for s, f in frames.items()})
    known = table.notna().sum(axis=1)
    if not (known > 0).any():
        return None
    side = ((table == "횡보").sum(axis=1) * 2 >= known) & (known > 0)
    run = 0
    for v in reversed(side.tolist()):
        if not v:
            break
        run += 1
    return run


def thesis_cause(rule: str, days: int | None) -> tuple[str, str]:
    """논지: 「횡보 60일 넘게 이어지면」 규칙.

    Args:
        rule: 가드레일 thesis.rule.
        days: sideways_days 결과.

    Returns:
        (상태, 근거). 논지가 없으면 「해당 없음」(대표 상태에서 빠짐).
    """
    if rule == "논지 없음":
        return "해당 없음", "논지 없음"
    if days is None:
        return "확인 불가", "국면을 셀 바이낸스 봉이 모자람(200일 + 20일)"
    if days > THESIS_DAYS:
        return "경고", f"종목 절반 이상이 횡보인 날이 {days}일째(가드레일 논지: {rule})"
    return "정상", f"종목 절반 이상이 횡보인 날 {days}일째({THESIS_DAYS}일 넘으면 알림)"


def _r(x: float | None) -> float | None:
    """nums에 적는 값: 소수 여섯 자리로 반올림(음수 0은 0.0), 모르거나 NaN · 무한대면 None.

    Args:
        x: 값.

    Returns:
        반올림한 값 또는 None.
    """
    return None if x is None or not math.isfinite(x) else round(x, 6) + 0.0


def _compound(r: pd.Series, start: datetime) -> float:
    """start부터의 봉별 수익률을 곱한 누적 수익률.

    Args:
        r: 봉별 수익률(live.cons 또는 live.zero).
        start: 처음(포함).

    Returns:
        비율.
    """
    return float((1 + r[r.index >= pd.Timestamp(start)]).prod() - 1)


def _since(actual: Actual, start: datetime) -> tuple[pd.Series, float]:
    """start 처음 자산 기준 하루 끝 누적(비율, BT 구간 수익률과 같은 분모)과 start 때까지의 손익.

    Args:
        actual: 실제 손익.
        start: 처음(UTC 0시).

    Returns:
        (start 뒤 하루 끝 누적 비율, start 때까지 누적 손익 금액).
    """
    t0 = pd.Timestamp(start)
    at0 = float(actual.pnl[t0]) if t0 in actual.pnl.index else 0.0
    return (actual.pnl[actual.pnl.index > t0] - at0) / (float(actual.capital) + at0), at0


def _where(x: float | None, lo: float | None, hi: float | None) -> str | None:
    """값이 그날 정상 범위의 아래 · 안 · 위 어디인지(strategy_cause와 같은 비교).

    Args:
        x: 값.
        lo: 그날 범위 아래끝.
        hi: 그날 범위 위끝.

    Returns:
        「아래」 · 「안」 · 「위」, 값이나 범위를 모르면 None.
    """
    return None if x is None or lo is None or hi is None else "아래" if x < lo else "위" if x > hi else "안"


def _int_or_none(x: Any) -> int | None:
    """정수(bool 아님)면 그대로, 아니면 None(손으로 고친 가드레일의 실수 값이 days.jsonl 줄을 틀리게 만들지 않게).

    Args:
        x: 값.

    Returns:
        정수 또는 None.
    """
    return x if isinstance(x, int) and not isinstance(x, bool) else None


_SPAN_KEYS = ("actual", "bt", "bt0", "gap", "fee", "slip", "funding", "rest", "missed_n", "missed", "extra")
"""nums.all에 적는 구간 값 칸."""


def _span(start: datetime, live: LiveRun | None, actual: Actual | None, retro: Retro | None, ran: bool, futures: bool,
          funding_ok: bool, funding_total: Decimal) -> dict[str, Any]:
    """start부터 끝까지 한 구간의 기계 판독 숫자. 이번 구간과 실전 시작부터 전체를 같은 식으로 낸다. 모두 round(x, 6).

    Args:
        start: 구간 처음(UTC 0시).
        live: 같은 기간 백테스트(없으면 BT 칸은 None).
        actual: 쓸 수 있는 실제 손익(못 쓰면 None).
        retro: start부터의 짝짓기(없으면 놓친 주문 칸은 비움).
        ran: 실행 칸을 냈는지(live · actual · retro가 모두 있고 execution_cause를 부른 경우만 True).
        futures: 선물이면 펀딩비 칸.
        funding_ok: 펀딩비 받은 범위가 구간을 덮는지.
        funding_total: 펀딩비 합(거래 통화).

    Returns:
        actual · bt · bt0 · gap · fee · slip · funding · rest · slip_med_bp · slip_max_bp · missed_n · extra · missed. 못 낸 칸은 None
        (missed는 빈 목록).
    """
    out: dict[str, Any] = dict.fromkeys(("actual", "bt", "bt0", "gap", "fee", "slip", "funding", "rest", "slip_med_bp",
                                         "slip_max_bp", "missed_n", "extra"))
    out["missed"] = []
    if live is not None:
        out["bt"], out["bt0"] = _compound(live.cons, start), _compound(live.zero, start)
    cum, at0 = (_since(actual, start) if actual is not None else (None, 0.0))
    if cum is not None and len(cum):
        out["actual"] = float(cum.iloc[-1])
    if ran:
        out["gap"] = (out["actual"] or 0.0) - out["bt0"]
        ex = execution_numbers(retro, actual.capital + Decimal(str(at0)), out["gap"], futures, funding_ok, funding_total)
        out.update({k: ex[k] for k in ("fee", "slip", "funding", "rest", "slip_med_bp", "slip_max_bp")})
    if retro is not None:
        found = retro.result.backtest_only
        out["missed_n"], out["extra"] = len(found), len(retro.result.actual_only)
        out["missed"] = [{"t": e.time.astimezone(timezone.utc).isoformat(), "symbol": e.symbol, "side": e.side}
                         for e in found[:50]]
    return {k: _r(v) if isinstance(v, float) else v for k, v in out.items()}


def reconcile(name: str, g: dict, *, end: datetime, live_start: datetime, live: LiveRun | None, actual: Actual | None,
              retro: Retro | None, cov_from: datetime | None, cov_until: datetime | None, missing: list[str],
              funding_ok: bool, funding_total: Decimal, mixed: str | None, link_changed: bool, side_days: int | None,
              need_from: datetime | None = None, why: str | None = None, permanent: bool = False,
              retro_all: Retro | None = None) -> Day:
    """하루 대조를 판정한다.

    Args:
        name: 전략 이름.
        g: 가드레일 파일 내용(최신 판).
        end: 기간 끝(제외, 오늘 0시 UTC).
        live_start: 실전 시작(live.json의 출발 시각, 없으면 가드레일 live_start).
        live: 같은 기간 백테스트(못 냈으면 None).
        actual: 실제 손익(실전 시작 잔고를 모르면 None).
        retro: 이번 구간 짝짓기(못 냈으면 None).
        cov_from: 가드레일 종목 모두 받은 처음.
        cov_until: 가드레일 종목 모두 받은 끝.
        missing: 받은 기록 없는 종목.
        funding_ok: 펀딩비 받은 범위가 구간을 덮는지(현물은 True).
        funding_total: 이번 구간 가드레일 종목 펀딩비 합(거래 통화).
        mixed: 섞인 기록이면 그 사유 글(MIXED_OVERLAP · MIXED_LOST, 실행 · 봇 근거에 그대로 저장), 아니면 None(참 · 거짓 판단만 쓰므로 빈 글도 거짓).
        link_changed: 연결 파일이 저장 뒤 바뀜.
        side_days: 논지 날수.
        need_from: 체결이 이때부터 있어야 함(잔고를 받은 시각, 없으면 live_start).
        why: live가 None인 까닭(확인 불가 사유).
        permanent: 영구 사유(지원 안 하는 봉 길이)면 확인 불가 대신 점검 불가(매일 종료 코드 4를 안 냄).
        retro_all: 실전 시작부터 지금까지의 짝짓기(이번 구간이 첫 구간이 아닐 때만. 첫 구간이면 retro가 곧 이것, nums.all의 놓친 주문 · 잰 것 · 나머지).

    Returns:
        Day.
    """
    pstart, d = period(live_start, end)
    live_days = (end - live_start).days
    trades = len(actual.exits) if actual else 0
    states: dict[str, str] = {}
    lines: dict[str, str] = {}
    futures = g["target"]["market"] == "futures"
    no_live = f"{'점검 불가' if permanent else '확인 불가'}({why or '같은 기간 백테스트를 못 냄'})"
    no_acct = "점검 불가(실전 시작 잔고 모름)"
    acct_line = f"blackclip live {name}으로 실전 시작 잔고 · 포지션을 받으면 셈(조회 키)"
    capital = actual.capital if actual else None
    problem = (actual.problem or (None if capital else "기록 불완전: 출발 자산이 0 이하")) if actual else None
    bt_cum = bt_zero = None
    if live is None:
        states["전략"], lines["전략"] = no_live, why or ""
    else:
        bt_cum, bt_zero = _compound(live.cons, pstart), _compound(live.zero, pstart)
        if link_changed:
            states["전략"], lines["전략"] = ("점검 불가(연결 파일 바뀜)",
                                           f"link.py가 가드레일 저장 뒤 바뀜 · blackclip check {name} 뒤 blackclip save {name} --reason")
        else:
            states["전략"], lines["전략"] = strategy_cause(g, bt_cum, d, trades)
    bot_ran = not mixed and actual is not None
    if bot_ran:
        states["봇"], lines["봇"] = bot_cause(cov_from, cov_until, missing, end, need_from or live_start, actual.exits,
                                            g["trade_frequency"], live_days)
    cum = None
    if actual is not None and problem is None and base(states.get("봇", "")) != "확인 불가":
        cum, _ = _since(actual, pstart)  # 이번 구간 처음 자산 기준(BT 구간 수익률과 같은 분모)
    ran = False  # execution_cause를 불러 실행 칸을 냈는지(nums의 잰 것 · 나머지를 낼 수 있는 경우)
    if mixed:
        states["실행"] = states["봇"] = "점검 불가(섞인 기록)"
        lines["실행"] = lines["봇"] = mixed
    elif actual is None:
        states["실행"] = states["봇"] = no_acct
        lines["실행"] = lines["봇"] = acct_line
    else:
        if base(states["봇"]) == "확인 불가":  # 확인 불가 > 점검 불가
            states["실행"], lines["실행"] = "확인 불가(체결 내역을 못 받음)", lines["봇"]
        elif live is None or retro is None:
            states["실행"], lines["실행"] = no_live, why or ""
        elif problem:
            states["실행"], lines["실행"] = "점검 불가(기록 불완전)", problem
        else:
            _, at0 = _since(actual, pstart)
            gap = (float(cum.iloc[-1]) if cum is not None and len(cum) else 0.0) - bt_zero
            st, ln = execution_cause(retro, capital + Decimal(str(at0)), gap, futures, funding_ok, funding_total)
            states["실행"], lines["실행"] = st, ln
            ran = True
    if mixed or base(states["봇"]) == "확인 불가":
        states["선"], lines["선"] = states["봇"], lines["봇"]
    elif live is None and actual is not None and (cum is None or not len(cum)):  # 실제 손익이 있으면 BT 없이 판정
        states["선"], lines["선"] = no_live, why or ""
    elif cum is None:
        states["선"] = no_acct if actual is None else "점검 불가(기록 불완전)"
        lines["선"] = acct_line if actual is None else (problem or "")
    else:
        states["선"], lines["선"] = line_cause(cum, g["warning_line"])
    states["논지"], lines["논지"] = thesis_cause(g["thesis"]["rule"], side_days) if live else (no_live, why or "")
    nr, jh, wl, tf = g["normal_range"], g["judgment_hold"], g["warning_line"], g["trade_frequency"]
    lo, hi = (nr["daily_low"][d - 1], nr["daily_high"][d - 1]) if "daily_low" in nr else (None, None)
    rng = (f"정상 범위 {lo:+.1%} ~ {hi:+.1%}({d}일째 · 보수적 비용 기준)"
           if lo is not None else "정상 범위 그리지 않음")  # 묶음 한 줄에도 비용 가정
    a_now = float(cum.iloc[-1]) if cum is not None and len(cum) else None
    a_txt = f"실제 {a_now:+.1%}" if a_now is not None else "실제 모름"
    b_txt = f"같은 기간 BT {bt_cum:+.1%}" if bt_cum is not None else "같은 기간 BT 모름"
    head = f"{a_txt} · {b_txt} · {rng}"
    week = None
    if (end - timedelta(days=1)).weekday() == WEEKLY_DAY and live is not None:
        w0 = pd.Timestamp(max(end - timedelta(days=7), live_start))
        b7 = _compound(live.zero, w0)
        a7 = "실제 모름"
        if cum is not None:
            prev = float(actual.pnl[w0]) if w0 in actual.pnl.index else 0.0
            a7 = f"실제 {(float(actual.pnl.iloc[-1]) - prev) / (float(capital) + prev):+.1%}"
        n7 = sum(1 for t in (actual.exits if actual else []) if w0 <= pd.Timestamp(t))
        k7 = sum(1 for e in (retro.result.backtest_only if retro else []) if pd.Timestamp(e.time) >= w0)
        week = f"{name} 이번 주(7일): {a7} · BT(비용 0) {b7:+.1%} · 거래 {n7}건 · 놓친 주문 {k7}건(추정)"
    use = actual if cum is not None else None
    sp = _span(pstart, live, use, retro, ran, futures, funding_ok, funding_total)
    # nums.all은 첫 구간이든 아니든 같은 뜻: 같은 식을 실전 시작부터, 펀딩비는 None이고 rest = gap − fee − slip
    all_retro = retro if pstart == live_start else retro_all  # 첫 구간이면 구간 회고가 곧 실전 시작부터의 회고
    whole = _span(live_start, live, use, all_retro, ran and all_retro is not None, False, True, Decimal(0))
    # 거래 1건 수익(E): 이번 구간 청산만. 표본 전체 수(trade_n) · 뺀 수(trade_skip)는 목록이 2,000개로 잘려도 셈
    kept = [round(r, 6) for t, r in actual.trips if t >= pstart] if actual else None
    n_exit = sum(1 for t in actual.exits if t >= pstart) if actual else 0
    freq7 = (sum(1 for t in actual.exits if end - timedelta(days=7) <= t < end)  # bot_cause가 7일 빈도를 비교했을 때만
          if bot_ran and base(states["봇"]) != "확인 불가" and live_days >= 7 and tf.get("low") is not None else None)
    nums = {
        "period_start": pstart.date().isoformat(), "d": d,
        **{k: sp[k] for k in ("actual", "bt", "bt0", "gap", "fee", "slip", "funding", "rest")},
        "lo": _r(lo), "hi": _r(hi), "month_lo": _r(nr.get("low")), "month_hi": _r(nr.get("high")),
        "line": _r(wl["line"]), "sensitivity": wl["sensitivity"], "room": None if a_now is None else _r(a_now - wl["line"]),
        "edge0": _r(jh["edge0_in_range"][-1]) if jh["edge0_in_range"] else None, "min_trades": jh["min_trades"], "n7": freq7,
        "tf_low": _int_or_none(tf.get("low")), "tf_high": _int_or_none(tf.get("high")),
        "slip_med_bp": sp["slip_med_bp"], "slip_max_bp": sp["slip_max_bp"],
        "missed": sp["missed"], "missed_n": sp["missed_n"], "extra": sp["extra"],
        "trade_rets": None if kept is None else kept[-TRADE_KEEP:],
        "trade_mean": _r(statistics.fmean(kept[-TRADE_KEEP:])) if kept else None,
        "trade_n": None if kept is None else len(kept), "trade_skip": None if kept is None else n_exit - len(kept),
        "pos": _where(a_now, lo, hi), "bt_pos": _where(bt_cum, lo, hi),
        "all": {"start": live_start.date().isoformat(), **{k: whole[k] for k in _SPAN_KEYS}},
    }
    return Day(name, (end - timedelta(days=1)).date(), live_days, trades, head, states, lines,
               dict(g.get("response") or {}), week, nums)


def capital_basis(snap: dict | None) -> str | None:
    """손익 %의 분모(기준 자본)가 어디서 왔는지 한 줄. daily 파일에만 적고 텔레그램 글에는 넣지 않는다.

    Args:
        snap: notify.read_live 결과(없으면 None).

    Returns:
        한 줄(금액 값은 없음), 실전 시작 잔고 파일이 없으면 None.
    """
    if not snap:
        return None
    if snap.get("capital") is not None:
        return "기준 자본: 고객이 정한 금액"
    return (f"기준 자본: 계좌 {snap['quote']} 잔고 전체 + 출발 포지션(전략 밖 돈이 있으면 %가 작게 나옴 · "
            "`blackclip live --capital`로 정함)")


def response_text(v: object) -> str:
    """가드레일 대응 글(yaml 자유 글)을 한 줄로: 줄바꿈 · 겹친 공백을 공백 하나로, 비면 「적어 둔 것 없음」.

    Args:
        v: response 칸의 값(execution_warning · warning_line, 없으면 None).

    Returns:
        한 줄 글. 여러 줄이어도 텔레그램 글 · daily 파일의 줄 수가 늘지 않게 한다.
    """
    return " ".join(str(v or "").split()) or "적어 둔 것 없음"


def render_md(day: Day, match_bars: int, basis: str | None = None) -> str:
    """daily 파일 한 장(결론 → 근거 → 대응, Git 제외 파일).

    Args:
        day: 하루 결과.
        match_bars: 매칭 창(봉).
        basis: 기준 자본 출처 한 줄(capital_basis). 손익 줄 바로 아래에 적는다. 없으면 안 적음.

    Returns:
        마크다운.
    """
    rep, _ = representative(day.states)
    out = [f"# {day.strategy} · {day.day.isoformat()} 매일 대조", "",
           f"결론: {rep} · 실전 {day.live_day}일째 · 거래 {day.trades}건", "", day.head, ""]
    if basis:
        out += [basis, ""]
    out += [f"- {LABELS[k]}: {day.states[k]} · {day.lines[k]}" for k in TRACKED]
    ew, wl = response_text(day.response.get("execution_warning")), response_text(day.response.get("warning_line"))
    out += ["", f"가드레일 대응(내가 정해 둔 것): 실행 경고 → {ew} · 경고선 → {wl}"]
    if day.week:
        out += ["", day.week]
    out += ["", f"메모: 실행 쪽 원인은 추정 · 체결가 차이의 기준 가격은 같은 기간 백테스트 가격(추정) · 매칭 창 {match_bars}봉 · "
                "실제 손익은 실전 시작 잔고 · 포지션을 실전 시작일 UTC 종가로 평가해 출발 · 다른 자산 수수료 환산은 추정"]
    return "\n".join(out) + "\n"


def _line_tail(states: dict[str, str], key: str) -> str:
    """선이 경고인데 대표 칸이 선이 아니면 첫 줄 · 묶음 줄에 덧붙일 꼬리(대표 상태 · 순서는 그대로).

    Args:
        states: 칸 → 상태.
        key: 대표 칸.

    Returns:
        " · 경고선 경고" 또는 빈 글.
    """
    return " · 경고선 경고" if key != "선" and base(states["선"]) == "경고" else ""


RESPONSE_COL = {"실행": "execution_warning", "선": "warning_line"}
"""대응이 걸리는 원인 칸 → 그 칸의 가드레일 대응 칸 이름(response 키). 순서가 걸린 대응을 늘어놓는 순서다."""


def triggered(states: dict[str, str]) -> list[str]:
    """지금 걸린 가드레일 대응(response 키): 실행 칸이 경고면 `execution_warning`, 선 칸이 경고면 `warning_line`(실행 → 선 순서).

    텔레그램 글(`change_message`)과 대시보드 화면이 같은 규칙을 쓰게 한 곳에 둔다. 실행 경고 텔레그램을 켰는지는 여기서 보지 않는다
    (거르는 일은 알림을 보내는 쪽 몫이고, 화면은 걸린 것을 늘 보인다).

    Args:
        states: 칸 → 상태(`TRACKED` 다섯 칸이 모두 있음, 괄호 사유가 붙을 수 있음).

    Returns:
        걸린 response 키 목록. 없으면 빈 목록.
    """
    return [key for col, key in RESPONSE_COL.items() if base(states[col]) == "경고"]


def change_message(day: Day, execution_on: bool) -> str:
    """상태가 바뀐 전략 하나의 텔레그램 글(결론 · 근거 · 대응, 평문).

    Args:
        day: 하루 결과.
        execution_on: 실행 경고 텔레그램을 켰는지.

    Returns:
        글.
    """
    skip = () if execution_on else ("실행",)
    rep, key = representative(day.states, skip)
    why = TITLES[key] if base(rep) == "경고" else LABELS[key]  # 원인 추정 말은 경고일 때만, 아니면 칸 이름
    out = [f"{rep} · {why}{_line_tail(day.states, key)}", f"{day.strategy} · 실전 {day.live_day}일째 · 거래 {day.trades}건", day.head]
    for k in TRACKED:
        if k == "실행" and not execution_on:
            out.append("실행: 첫 회고를 본 뒤 켬(blackclip notify --execution-alerts on) · 숫자는 daily 파일에")
        elif day.states[k] != "해당 없음":
            out.append(f"{LABELS[k]}: {day.states[k]} · {day.lines[k]}")
    resp = []
    hit = triggered(day.states)
    if "execution_warning" in hit and execution_on:
        resp.append(f"실행 경고 → {response_text(day.response.get('execution_warning'))}")
    if "warning_line" in hit:
        resp.append(f"경고선 → {response_text(day.response.get('warning_line'))}")
    if resp:
        out.append("가드레일 대응(내가 정해 둔 것): " + " · ".join(resp))
    out.append(f"자세히: 컴퓨터에서 .blackclip/strategies/{day.strategy}/daily/{day.day.isoformat()}.md")
    return "\n".join(out)


def bundle(days: list[Day], execution_on: bool, notes: list[str]) -> str:
    """매일 묶음 한 통(하트비트 겸함). 매주 회고 줄이 있으면 붙인다.

    Args:
        days: 전략마다 하루 결과(같은 날).
        execution_on: 실행 경고 텔레그램을 켰는지.
        notes: 끝에 붙일 줄(받기 실패 등).

    Returns:
        글.
    """
    skip = () if execution_on else ("실행",)
    when = days[0].day.isoformat() if days else "오늘"
    out = [f"매일 묶음 · {when} UTC 마감 · 전략 {len(days)}개"]
    for d in days:
        state, key = representative(d.states, skip)
        out.append(f"{d.strategy}: {state}{_line_tail(d.states, key)} · 실전 {d.live_day}일째 · 거래 {d.trades}건 · {d.head}")
    out += [d.week for d in days if d.week]
    return "\n".join(out + notes)

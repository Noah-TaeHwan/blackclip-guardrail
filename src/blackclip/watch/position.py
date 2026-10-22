"""포지션 대조: BT 목표(종목별 보유 방향)와 실제 보유를 있음/없음 · 방향으로 맞댄다(크기는 안 봄).

BT 목표는 새 봉이 닫혔을 때만 같은 기간 백테스트(daily.live.live_run)를 다시 돌려 얻고, 그 밖에는 상태 파일 캐시를 쓴다.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path
from typing import Any

from blackclip.retro.fills import Execution
from blackclip.watch.window import Mark, equity

UTC = timezone.utc

HOLD_SHARE = Decimal("0.01")
"""그 종목 평가액(실제) · 목표 비중(BT)이 지금 자산의 이 비율 이상이면 「있음」(양쪽 같은 문턱)."""

FUTURES_NOTE = "선물은 있음/없음만 봄(방향은 미검증)"
"""선물 포지션 대조 꼬리(hedge 모드 SHORT 부호를 실계좌로 못 봄)."""


def bar_end(now: datetime, bar_s: int) -> datetime:
    """지금 직전에 닫힌 봉의 끝(= 진행 중 봉의 시작, UTC 0시 기준으로 나뉜 하루 이하 봉).

    Args:
        now: 지금(UTC).
        bar_s: 봉 길이(초, 86400 이하).

    Returns:
        시각.
    """
    return datetime.fromtimestamp(int(now.timestamp()) // bar_s * bar_s, UTC)


def grace_s(bar_s: int) -> int:
    """목표가 바뀐 뒤 실제가 따라오기를 기다리는 초 = max(1시간, 봉 길이의 1/4).

    Args:
        bar_s: 봉 길이(초).

    Returns:
        초.
    """
    return max(3600, bar_s // 4)


def held(m: Mark) -> dict[str, int]:
    """실제 보유 방향: 평가액이 지금 자산의 1% 이상이면 부호(+1 · -1), 아니면 0.

    Args:
        m: 이번 확인.

    Returns:
        종목 → +1 · 0 · -1.
    """
    eq = equity(m)
    return {s: 0 if q == 0 or abs(q * m.price[s]) < HOLD_SHARE * eq else (1 if q > 0 else -1) for s, q in m.hold.items()}


def side(weight: float, futures: bool = False) -> int:
    """BT 목표 비중 → 있음/없음 · 방향(실제와 같은 1% 문턱). 선물은 있음/없음만(1 · 0).

    Args:
        weight: 목표 비중(자산 대비, 음수 = 매도 포지션).
        futures: 선물이면 방향을 버림.

    Returns:
        +1 · 0 · -1.
    """
    if abs(weight) < float(HOLD_SHARE):
        return 0
    return 1 if futures or weight > 0 else -1


def flip_times(executions: list[Execution], target: dict[str, float], futures: bool) -> dict[str, datetime | None]:
    """종목마다 BT 있음/없음 · 방향(선물은 있음/없음)이 마지막으로 바뀐 BT 체결 시각. 크기만 바뀐 체결은 안 셈.

    체결 qty는 비중 변화라, 마지막 목표 비중에서 거꾸로 빼 가며 각 체결 앞뒤 비중을 얻는다.

    Args:
        executions: 실전 구간 BT 체결(LiveRun.executions).
        target: 종목 → 마지막 목표 비중(LiveRun.target).
        futures: 선물이면 있음/없음만.

    Returns:
        종목 → 시각, 실전 구간에서 바뀐 적이 없으면 None.
    """
    out: dict[str, datetime | None] = {}
    for s, w in target.items():
        out[s] = None
        for e in sorted((e for e in executions if e.symbol == s), key=lambda e: e.time, reverse=True):
            before = w - float(e.qty if e.side == "buy" else -e.qty)
            if side(before, futures) != side(w, futures):
                out[s] = e.time
                break
            w = before
    return out


def _word(d: int, futures: bool) -> str:
    """방향 → 화면 말.

    Args:
        d: +1 · 0 · -1.
        futures: 선물이면 있음/없음만.

    Returns:
        말.
    """
    if d == 0:
        return "없음"
    return "있음" if futures else ("보유" if d > 0 else "매도 포지션")


def compare(weights: dict[str, float], changed: dict[str, datetime | None], have: dict[str, int], now: datetime,
            bar_s: int, futures: bool) -> tuple[str, str]:
    """BT 목표와 실제 보유를 맞댄다. 다르더라도 목표가 바뀐 뒤 유예 안이면 경고하지 않는다.

    Args:
        weights: 종목 → BT 목표 비중(마지막으로 닫힌 봉).
        changed: 종목 → BT 있음/없음 · 방향이 바뀐 시각(flip_times, 종가 체결은 봉 마감 · next_open은 다음 시가), 실전 구간에 없으면 None.
        have: held 결과.
        now: 지금.
        bar_s: 봉 길이(초).
        futures: 선물이면 있음/없음만.

    Returns:
        (「정상」 또는 「경고」, 근거).
    """
    wait_s = grace_s(bar_s)
    bad, waiting, ok = [], [], []
    for s in sorted(weights):
        want = side(weights[s])
        got = have.get(s, 0)
        text = f"{s} BT {_word(want, futures)} · 실제 {_word(got, futures)}"
        if ((want != 0) == (got != 0)) if futures else want == got:
            ok.append(f"{s} {_word(want, futures)}")
        elif changed.get(s) and now < changed[s] + timedelta(seconds=wait_s):
            waiting.append(f"{text}(목표 포지션이 {changed[s]:%m/%d %H:%M} UTC에 바뀜 · 유예 {wait_s / 3600:g}시간)")
        else:
            bad.append(text)
    tail = f" · {FUTURES_NOTE}" if futures else ""
    if bad:
        return "경고", "포지션 다름: " + " · ".join(bad) + tail
    if waiting:
        return "정상", "포지션 따라오는 중: " + " · ".join(waiting) + tail
    return "정상", "포지션 맞음: " + " · ".join(ok) + tail


def bt_target(root: Path, name: str, g: dict, live_start: datetime, kline_client: Any, now: datetime,
              cache: dict | None) -> tuple[dict[str, float], dict[str, datetime | None], dict]:
    """BT 목표 비중과 목표가 바뀐 시각. 마지막으로 닫힌 봉 · 가드레일 판이 캐시와 같으면 다시 돌리지 않는다.

    Args:
        root: .blackclip 경로.
        name: 전략 이름.
        g: 가드레일(최신 판).
        live_start: 실전 시작(live.json의 start).
        kline_client: 시장 → 공개 봉 클라이언트.
        now: 지금(UTC).
        cache: 상태 파일 "target" 칸(없으면 None).

    Returns:
        (종목 → 비중, 종목 → 바뀐 시각 또는 None, 새 캐시).

    Raises:
        ValueError · KeyError · OSError · BinanceError · LinkError · ExpressError · SignalError: 봉을 못 받거나 신호 함수가
            실패했을 때(호출자가 포지션 대조만 확인 불가로).
    """
    from blackclip.check.runner import Runner
    from blackclip.daily.live import binance_frames, live_run

    t = g["target"]
    bar_s, symbols = int(t["bar_seconds"]), list(t["symbols"])
    end = bar_end(now, bar_s)
    if not (cache and cache.get("bar_end") == end.isoformat() and cache.get("version") == g["version"]):
        frames, absent = binance_frames(kline_client(t["market"]), symbols, bar_s,
                                        datetime.fromisoformat(t["start"]).replace(tzinfo=UTC), end)
        if absent:
            raise ValueError(f"바이낸스 {t['market']}에 봉 없음({', '.join(absent[:3])})")
        with Runner(root, name) as runner:
            live = live_run(lambda f: runner.signals(frames=f), frames, t["exec"], bar_s, live_start, end)
        flips = flip_times(live.executions, live.target, t["market"] == "futures")
        cache = {"bar_end": end.isoformat(), "version": g["version"], "weights": live.target,
                 "changed": {s: v.isoformat() if v else None for s, v in flips.items()}}
    return (dict(cache["weights"]), {s: datetime.fromisoformat(v) if v else None for s, v in cache["changed"].items()},
            cache)

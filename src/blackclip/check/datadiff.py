"""데이터 차이: 고객 가격표와 같은 기간 바이낸스 공개 봉을 맞댄다.

판정 없이 숫자만 낸다: 종가 차이(중앙값 · 최대 bp) · 바이낸스에 없는 봉 · 같은 신호 함수로 다시 돌린 체결 일치율.
"""
from __future__ import annotations

import http.client
from collections.abc import Callable
from typing import Any

import numpy as np
import pandas as pd

from blackclip.binance.client import BinanceError
from blackclip.binance.klines import fetch_klines
from blackclip.check.backtest import Run, count_matches, simulate
from blackclip.check.link import INTERVALS, ExpressError, Link, LinkError
from blackclip.check.runner import SignalError

INVALID_SYMBOL = -1121
"""바이낸스 오류 코드: 없는 종목."""


def _ms(t: pd.Timestamp) -> int:
    """시각을 밀리초로.

    Args:
        t: 시각(UTC).

    Returns:
        밀리초.
    """
    return int(t.timestamp() * 1000)


def data_diff(link: Link, prices: dict[str, pd.DataFrame], run: Run, bar_s: int, client: Any,
              signals: Callable[[dict[str, pd.DataFrame]], dict[str, pd.Series]]) -> dict[str, str]:
    """종목마다 같은 기간 바이낸스 봉을 받아 고객 가격표와 맞댄다.

    Args:
        link: 연결 파일(시장 · 신호 함수 · 체결 규칙).
        prices: 종목 → 고객 가격표(UTC).
        run: 고객 가격표로 다시 돌린 결과.
        bar_s: 봉 길이(초).
        client: 공개 봉을 받을 클라이언트(link.market 시장).
        signals: 표를 받아 확인한 목표 비중을 돌려주는 것(Runner.signals(frames=…)).

    Returns:
        {"state": 잰 것 · 확인 불가, "text": 숫자와 근거}.
    """
    interval = INTERVALS.get(bar_s)
    if interval is None:
        return {"state": "확인 불가", "text": f"봉 길이 {bar_s:,}초를 바이낸스 봉 단위로 바꿀 수 없음"}
    got: dict[str, pd.DataFrame] = {}
    absent: list[str] = []
    for sym, p in prices.items():
        try:
            k = fetch_klines(client, sym, interval, _ms(p.index[0]), _ms(p.index[-1]) + bar_s * 1000)
        except BinanceError as exc:
            # shortcut: 만기 선물(BTCUSDT_261225)은 정규화 이름이 달라 여기서 「없는 종목」이 된다, 만기 선물 고객이 생기면 원문 이름을 받는다
            if exc.code == INVALID_SYMBOL:
                absent.append(sym)
                continue
            # 상태 · 코드만: 서버 문구(5xx HTML의 줄바꿈 · IP 등)는 check.md에 못 넣는다. 0 · 200은 우리가 쓴 고정 문구라 그대로
            why = str(exc) if exc.status in (0, 200) else f"바이낸스 응답 {exc.status}" + (f" · 코드 {exc.code}" if exc.code is not None else "")
            return {"state": "확인 불가", "text": f"바이낸스 봉을 못 받음({why})"}
        except OSError as exc:  # 메시지에 이 컴퓨터 경로가 들 수 있어 종류 · 설명만(check.md는 커밋됨)
            return {"state": "확인 불가", "text": f"바이낸스 봉을 못 받음({type(exc).__name__}{': ' + exc.strerror if exc.strerror else ''})"}
        except (ValueError, http.client.HTTPException) as exc:  # 200인데 HTML(JSONDecodeError) · 끊긴 응답(IncompleteRead 등), 종류만
            return {"state": "확인 불가", "text": f"바이낸스 봉을 못 받음({type(exc).__name__})"}
        if len(k):
            got[sym] = k
        else:
            absent.append(sym)
    if not got:
        return {"state": "확인 불가", "text": f"바이낸스 {link.market}에 같은 종목 봉이 없음({', '.join(absent)})"}
    diffs: list[np.ndarray] = []
    missing = outside = 0
    for sym, k in got.items():
        c = prices[sym]["Close"]
        within = c.index[(c.index >= k.index[0]) & (c.index <= k.index[-1])]  # 상장 전 · 끝난 뒤는 빠진 봉이 아니다
        outside += len(c) - len(within)
        missing += len(within.difference(k.index))
        common = within.intersection(k.index)
        diffs.append(np.abs(c.loc[common].to_numpy() / k.loc[common, "Close"].to_numpy() - 1) * 1e4)
    bp = np.concatenate(diffs)
    start = min(k.index[0] for k in got.values())
    end = max(k.index[-1] for k in got.values())
    text = f"바이낸스 {link.market} {interval} 봉과 겹치는 {start:%Y-%m-%d} ~ {end:%Y-%m-%d}: "
    text += f"종가 차이 중앙값 {np.median(bp):.1f}bp · 최대 {bp.max():.1f}bp" if len(bp) else "같은 시각 봉 없음"
    text += f" · 겹치는 기간 안에서 바이낸스에 없는 봉 {missing:,}개"
    if outside:
        text += f" · 겹치는 기간 밖 고객 봉 {outside:,}개"
    text += f" · {_rerun(link, run, got, signals)}"
    if absent:
        text += f" · 바이낸스에 없는 종목 {len(absent)}개({', '.join(absent[:3])})"
    return {"state": "잰 것", "text": text}  # 「잰 것」에는 「추정」을 붙이지 않음(옛 낱말 「잼」)


def _rerun(link: Link, run: Run, got: dict[str, pd.DataFrame],
           signals: Callable[[dict[str, pd.DataFrame]], dict[str, pd.Series]]) -> str:
    """같은 신호 함수를 바이낸스 봉으로 다시 돌려 고객 데이터 체결과 얼마나 같은지 센다.

    고객 체결은 종목마다 그 종목의 바이낸스 봉 범위 안 것만 센다(늦게 상장한 종목의 상장 전 체결은 짝이 있을 수 없다).

    Args:
        link: 연결 파일.
        run: 고객 가격표로 다시 돌린 결과.
        got: 종목 → 바이낸스 봉.
        signals: 표 → 목표 비중.

    Returns:
        근거 한 토막.
    """
    try:
        weights = signals(got)
    except SignalError as exc:  # 고객 코드가 죽음: 예외 이름만 적는다(메시지에 경로 · 출력이 들 수 있음)
        return f"바이낸스 봉으로 신호 함수가 돌지 않음({exc.type_name})"
    except (LinkError, ExpressError) as exc:  # 원문에 고객 stderr · 경로가 들 수 있어 종류만
        return f"바이낸스 봉으로는 신호를 확인할 수 없음({type(exc).__name__})"
    theirs = simulate(got, weights, link.exec_).executions  # 바이낸스 봉으로 만들어 이미 각 종목 범위 안
    ours = [e for e in run.executions if e.symbol in got and got[e.symbol].index[0] <= pd.Timestamp(e.time) <= got[e.symbol].index[-1]]
    n = max(len(ours), len(theirs))
    if not n:
        return "두 데이터 모두 체결 없음"
    hit = count_matches(ours, theirs, got)
    return f"같은 신호 함수로 다시 돌린 체결 일치 {hit / n:.1%}({hit:,}/{n:,})"

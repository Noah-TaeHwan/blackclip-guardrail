"""바이낸스 공개 봉(klines) 받기. 서명 없는 요청이라 키가 필요 없다."""
from __future__ import annotations

from typing import Any

import pandas as pd

from blackclip.binance.client import BinanceError

PATH: dict[str, str] = {"spot": "/api/v3/klines", "futures": "/fapi/v1/klines"}
"""시장 → 봉 경로."""

LIMIT = 1000
"""한 번에 받는 봉 수(현물 상한 1000, 선물 상한 1500 중 작은 쪽)."""


def fetch_klines(client: Any, symbol: str, interval: str, start_ms: int, end_ms: int) -> pd.DataFrame:
    """[start_ms, end_ms) 안의 봉을 1000개씩 이어 받아 시작 시각 순으로 돌려준다.

    Args:
        client: market 속성과 get(path, params, signed)이 있는 클라이언트.
        symbol: 바이낸스 종목(BTCUSDT).
        interval: 바이낸스 봉 단위(1d 등).
        start_ms: 시작(포함, 밀리초).
        end_ms: 끝(제외, 밀리초).

    Returns:
        DataFrame[Open, High, Low, Close, Volume], 인덱스 = 봉 시작(UTC).

    Raises:
        BinanceError: 응답 모양이 다를 때(그 밖의 바이낸스 · 네트워크 오류는 그대로 올라감).
    """
    rows: dict[int, tuple[float, ...]] = {}
    cur = start_ms
    while cur < end_ms:
        page = client.get(PATH[client.market], {"symbol": symbol, "interval": interval, "startTime": cur,
                                                "endTime": end_ms - 1, "limit": LIMIT}, signed=False)
        if not isinstance(page, list):
            raise BinanceError(200, None, "봉 응답 모양이 다릅니다(목록이 아님)")
        try:
            for k in page:
                rows[int(k[0])] = (float(k[1]), float(k[2]), float(k[3]), float(k[4]), float(k[5]))
            nxt = int(page[-1][0]) + 1 if page else end_ms
        except (TypeError, ValueError, IndexError):
            raise BinanceError(200, None, "봉 응답 모양이 다릅니다") from None
        if len(page) < LIMIT or nxt <= cur:
            break
        cur = nxt
    times = sorted(rows)
    return pd.DataFrame([rows[t] for t in times], columns=["Open", "High", "Low", "Close", "Volume"],
                        index=pd.to_datetime(times, unit="ms", utc=True))

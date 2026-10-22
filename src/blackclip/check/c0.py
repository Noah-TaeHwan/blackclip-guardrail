"""재현성: 다시 돌린 체결을 고객의 원래 거래 목록과 맞댄다."""
from __future__ import annotations

import pandas as pd

from blackclip.check.backtest import Run, count_matches
from blackclip.check.link import Link, LinkError
from blackclip.retro.fills import read_executions

THRESHOLD = 0.95
"""일치 문턱(95%를 원래 · 다시 돌린 양쪽에)."""


def c0(link: Link, prices: dict[str, pd.DataFrame], run: Run, bar_s: int) -> dict[str, str]:
    """원래 거래 목록 중 가격표 기간 · 종목 안의 체결을 다시 돌린 체결과 ±1봉 안에서 짝짓는다.

    Args:
        link: 연결 파일(trades가 없으면 재현 확인 전).
        prices: 종목 → 가격표.
        run: 다시 돌린 결과.
        bar_s: 봉 길이(초, 마지막 봉의 끝을 정함).

    Returns:
        {"state": 일치 · 불일치 · 재현 확인 전, "text": 근거}.

    Raises:
        LinkError: 원래 거래 목록 파일을 읽을 수 없을 때.
    """
    if link.trades is None:
        return {"state": "재현 확인 전", "text": "연결 파일에 원래 거래 목록(TRADES)이 없어 비교할 기준이 없음"}
    try:
        original = read_executions(link.trades)
    except (OSError, ValueError) as exc:  # FillsError · UnicodeDecodeError는 ValueError
        raise LinkError(f"TRADES {link.trades_label}: {exc}") from None
    span = {s: (p.index[0], p.index[-1] + pd.Timedelta(seconds=bar_s)) for s, p in prices.items()}  # 종목마다 따로
    inside = [e for e in original if e.symbol in span and span[e.symbol][0] <= pd.Timestamp(e.time) < span[e.symbol][1]]
    left = len(original) - len(inside)
    if not inside:
        return {"state": "재현 확인 전",
                "text": f"원래 거래 목록 {len(original):,}건이 모두 가격표 기간 · 종목 밖이라 비교할 거래가 없음"}
    ours = run.executions
    hit = count_matches(inside, ours, prices)
    ra, rb = hit / len(inside), (hit / len(ours) if ours else 0.0)
    text = (f"원래 체결 {len(inside):,}건 중 {hit:,}건이 같은 종목 · 방향으로 ±1봉 안에서 맞음({ra:.1%}) · "
            f"다시 돌린 체결 {len(ours):,}건 중 {rb:.1%}")
    if left:
        text += f" · 가격표 기간 · 종목 밖 {left:,}건 뺌"
    return {"state": "일치" if ra >= THRESHOLD and rb >= THRESHOLD else "불일치", "text": text}

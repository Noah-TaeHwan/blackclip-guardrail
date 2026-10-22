"""끝-끝: 실제 Client(가짜 transport만, 네트워크 없음)로 run_fetch 기본 시작 → 쌓인 fills.csv · coverage.json → retro 기본 인자."""
import json
from datetime import datetime, timezone
from urllib.parse import parse_qs, urlsplit

from conftest import CREDS, NOW, READ_ONLY, MemStore

from blackclip.binance.client import Client
from blackclip.cli import main
from blackclip.fetch import run_fetch
from blackclip.workspace import ensure


def _ms(text):
    """ISO 시각 → 유닉스 ms."""
    return int(datetime.fromisoformat(text).timestamp() * 1000)


EXCHANGE = [{"symbol": "BTCUSDT", "id": 1, "orderId": 10, "price": "60000.00", "qty": "0.001", "commission": "0.06",
             "commissionAsset": "USDT", "time": _ms("2026-10-12T01:00:00+00:00"), "isBuyer": True}]
"""거래소에 있는 체결: 10/12 매수 하나. fetch 기본 시작(지금 − 30일 0시 = 09/15) 뒤다."""


def transport(url, headers):
    """바이낸스 대신 답한다: 권한은 조회만, myTrades는 창 안 체결."""
    path, q = urlsplit(url).path, {k: v[0] for k, v in parse_qs(urlsplit(url).query).items()}
    if path == "/sapi/v1/account/apiRestrictions":
        body = READ_ONLY
    elif path == "/api/v3/myTrades":
        body = [t for t in EXCHANGE if t["symbol"] == q["symbol"] and int(q["startTime"]) <= t["time"] <= int(q["endTime"])]
    else:
        raise AssertionError(f"모르는 경로 {path}")
    return 200, {}, json.dumps(body).encode()


def test_fetch_then_retro_with_defaults(tmp_path, capsys):
    """기본 since로 받은 범위가 근거 줄에 쓰이고, 받지 않은 기간(09/15 앞)의 백테스트 거래는 놓친 주문이 아니라 범위 밖이다."""
    root = ensure(tmp_path)
    make = lambda m, t, c: Client(m, t, c, transport=transport, clock=NOW.timestamp, sleep=lambda s: None)  # noqa: E731
    code, lines = run_fetch(root, MemStore({"binance:main": CREDS}), make, label="main", testnet=False,
                            markets=["spot"], symbols=["BTCUSDT"], since=None, now=NOW)
    assert code == 0 and any(line.startswith("spot BTCUSDT: 새로 1건 · 2026-09-15T00:00") for line in lines)
    fills = root / "fills" / "binance-main" / "spot" / "fills.csv"
    bt = tmp_path / "bt.csv"
    # 기본 회고 기간은 마지막 체결(10/12) 다음 날 − 30일 = 09/13부터라 09/14 매도는 기간 안이지만 받지 않은 기간이다
    bt.write_text("time,symbol,side,qty,price,fee,order_id\n"
                  "2026-09-14T01:00:00+00:00,BTCUSDT,sell,0.001,61000,0,b0\n"
                  "2026-10-12T01:00:00+00:00,BTCUSDT,buy,0.001,60000,0,b1\n", encoding="utf-8")
    assert main(["retro", "--fills", str(fills), "--backtest", str(bt), "--capital", "1000",
                 "--live-start", "2026-08-01T00:00:00+00:00"]) == 0
    out = capsys.readouterr()
    assert out.err == ""
    assert "# 실행 회고 · 2026-09-13 ~ 2026-10-12 (UTC)" in out.out
    assert "체결 파일이 09/15 00:00부터 10/15 11:59까지 담겼다고 blackclip fetch 기록에서 읽음" in out.out
    assert "| 09/14 01:00 | BTCUSDT | 매도 0.001 · 61000.00 | 없음 | 점검 불가 | 체결 파일 범위 밖 |" in out.out
    assert "놓친 주문" not in out.out and "짝지은 체결 1건" in out.out

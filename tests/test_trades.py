"""체결 받기: 창 나누기 · 방향 바꾸기 · 꽉 찬 페이지 이어 받기 · 선물 3개월 상한 · 펀딩비."""
import pytest

from blackclip.binance.client import BinanceError
from blackclip.binance.trades import DAY_MS, FUTURES_HISTORY_MS, fetch_funding, fetch_trades, iso

SPOT = {"symbol": "BTCUSDT", "id": 28457, "orderId": 100234, "orderListId": -1, "price": "4.00000100",
        "qty": "12.00000000", "quoteQty": "48.000012", "commission": "10.10000000", "commissionAsset": "BNB",
        "time": 1499865549590, "isBuyer": True, "isMaker": False, "isBestMatch": True}
"""현물 myTrades 한 줄(테스트넷 실호출과 같은 열)."""

FUT = {"buyer": False, "commission": "-0.07819010", "commissionAsset": "USDT", "id": 698759, "maker": False,
       "orderId": 25851813, "price": "7819.01", "qty": "0.002", "quoteQty": "15.63802", "realizedPnl": "-0.91539999",
       "side": "SELL", "positionSide": "SHORT", "symbol": "BTCUSDT", "time": 1569514978020}
"""선물 userTrades 한 줄(공식 문서 응답 예시. 실응답 미검증)."""


class Fake:
    """경로 · 쿼리를 받아 답을 고르는 클라이언트."""

    def __init__(self, market, answer):
        self.market, self.answer, self.calls = market, answer, []

    def get(self, path, params=None, signed=True):
        self.calls.append((path, dict(params)))
        return self.answer(path, params)


def test_spot_windows_are_24h():
    """현물은 24시간보다 짧은 창으로 나눠 부른다."""
    f = Fake("spot", lambda p, q: [])
    fetch_trades(f, "BTCUSDT", 0, 2 * DAY_MS + 5)
    spans = [(q["startTime"], q["endTime"]) for _, q in f.calls]
    assert spans == [(0, DAY_MS - 1), (DAY_MS, 2 * DAY_MS - 1), (2 * DAY_MS, 2 * DAY_MS + 4)]
    assert all(q["symbol"] == "BTCUSDT" and q["limit"] == 1000 for _, q in f.calls)


def test_spot_row_from_isBuyer():
    """현물은 isBuyer로 방향을 정하고 회고 CSV 열로 바꾼다."""
    f = Fake("spot", lambda p, q: [SPOT, {**SPOT, "id": 28458, "isBuyer": False}] if q["startTime"] == 0 else [])
    got = fetch_trades(f, "BTCUSDT", 0, 10)
    assert got.rows[0] == {"time": "2017-07-12T13:19:09.590+00:00", "symbol": "BTCUSDT", "side": "buy",
                           "qty": "12.00000000", "price": "4.00000100", "fee": "10.10000000", "fee_asset": "BNB",
                           "order_id": "100234", "trade_id": "28457"}
    assert got.rows[1]["side"] == "sell" and (got.start_ms, got.end_ms, got.lost) == (0, 10, None)


def test_futures_side_and_fee_cost():
    """선물은 side 열을 쓰고 수수료는 비용(절댓값)으로 적는다. 창은 7일."""
    end = FUT["time"] + 1
    f = Fake("futures", lambda p, q: [FUT] if q["startTime"] <= FUT["time"] <= q["endTime"] else [])
    got = fetch_trades(f, "BTCUSDT", end - 8 * DAY_MS, end)
    assert f.calls[0][0] == "/fapi/v1/userTrades"
    assert all(q["endTime"] - q["startTime"] < 7 * DAY_MS for _, q in f.calls)
    assert got.rows[0]["side"] == "sell" and got.rows[0]["fee"] == "0.07819010"


def test_futures_zero_fee_written_plain():
    """선물 수수료 -0.00000000은 지수 표기(0E-8) 없이 0.00000000으로 적는다."""
    zero = {**FUT, "commission": "-0.00000000"}
    f = Fake("futures", lambda p, q: [zero] if q["startTime"] <= zero["time"] <= q["endTime"] else [])
    assert fetch_trades(f, "BTCUSDT", zero["time"], zero["time"] + 1).rows[0]["fee"] == "0.00000000"


def _windowed(items, newest_first=False):
    """창 [startTime, endTime] 안 줄을 1000건까지 돌려주는 가짜 응답(newest_first면 창 안 「최근 1000건」)."""
    def answer(p, q):
        inside = [x for x in items if q["startTime"] <= x["time"] <= q["endTime"]]
        return inside[-1000:] if newest_first else inside[:1000]
    return answer


def test_full_page_continues_without_duplicates():
    """한 창에서 1000건이 차면 그 창을 반으로 나눠 다시 받고, 겹친 줄은 한 번만 남긴다."""
    items = [{**SPOT, "id": i, "time": 1000 + i} for i in range(1000)] + [{**SPOT, "id": 1000, "time": 2500}]
    f = Fake("spot", _windowed(items))
    got = fetch_trades(f, "BTCUSDT", 0, 10_000)
    assert len(got.rows) == 1001 and len({r["trade_id"] for r in got.rows}) == 1001
    assert (f.calls[1][1]["startTime"], f.calls[1][1]["endTime"]) == (0, 4999)


def test_full_page_newest_first_loses_nothing():
    """바이낸스가 창 안 「최근 1000건」만 줘도(1500건 있음) 창을 나눠 1500건 모두 받는다."""
    items = [{**SPOT, "id": i, "time": 1000 + i} for i in range(1500)]
    got = fetch_trades(Fake("spot", _windowed(items, newest_first=True)), "BTCUSDT", 0, 10_000)
    assert sorted(int(r["trade_id"]) for r in got.rows) == list(range(1500))


def test_stuck_page_raises():
    """같은 1000건만 계속 오면 다 받지 못했다고 말하고 멈춘다."""
    page = [{**SPOT, "id": i, "time": 5} for i in range(1000)]
    with pytest.raises(BinanceError, match="확인 불가"):
        fetch_trades(Fake("spot", lambda p, q: page), "BTCUSDT", 0, 10)


def test_futures_clipped_to_90_days():
    """선물은 3개월보다 앞을 부르지 않고 못 받은 범위를 돌려준다."""
    end = 200 * DAY_MS
    f = Fake("futures", lambda p, q: [])
    got = fetch_trades(f, "BTCUSDT", end - 120 * DAY_MS, end)
    floor = end - FUTURES_HISTORY_MS
    assert f.calls[0][1]["startTime"] == floor and got.start_ms == floor
    assert got.lost == (end - 120 * DAY_MS, floor)


def test_funding_rows_and_params():
    """펀딩비는 income FUNDING_FEE를 종목 없이 받아 회고 펀딩비 CSV 열로 바꾼다."""
    row = {"symbol": "BTCUSDT", "incomeType": "FUNDING_FEE", "income": "-0.01250000", "asset": "USDT",
           "info": "", "time": 1700000000000, "tranId": 9689322392, "tradeId": ""}
    end = 1700000000001
    f = Fake("futures", lambda p, q: [row] if q["startTime"] <= row["time"] <= q["endTime"] else [])
    got = fetch_funding(f, end - DAY_MS, end)
    path, q = f.calls[0]
    assert path == "/fapi/v1/income" and q["incomeType"] == "FUNDING_FEE" and "symbol" not in q
    assert got.rows == [{"time": iso(1700000000000), "symbol": "BTCUSDT", "amount": "-0.01250000",
                         "asset": "USDT", "tran_id": "9689322392"}]


def test_page_not_list_raises():
    """페이지가 리스트가 아니거나 줄이 dict가 아니면 조회 실패(BinanceError), TypeError가 아니다."""
    from blackclip.binance.client import SHAPE_TEXT
    for bad in ({"code": 0}, ["x"], [None]):
        with pytest.raises(BinanceError) as e:
            fetch_trades(Fake("spot", lambda p, q, b=bad: b), "BTCUSDT", 0, 10)
        assert str(e.value).endswith(SHAPE_TEXT)


def test_row_missing_key_raises():
    """쓰는 칸이 빠진 줄은 KeyError가 아니라 BinanceError(현물 · 선물 · 펀딩비)."""
    from blackclip.binance.client import SHAPE_TEXT
    cases = [("spot", {k: v for k, v in SPOT.items() if k != "id"}, fetch_trades),
             ("spot", {k: v for k, v in SPOT.items() if k != "commission"}, fetch_trades),
             ("futures", {k: v for k, v in FUT.items() if k != "side"}, fetch_trades),
             ("futures", {"symbol": "BTCUSDT", "tranId": 1, "time": 5}, fetch_funding)]
    for market, row, fn in cases:
        f = Fake(market, lambda p, q, r=row: [r] if q["startTime"] == 0 else [])
        with pytest.raises(BinanceError) as e:
            fn(f, "BTCUSDT", 0, 10) if fn is fetch_trades else fn(f, 0, 10)
        assert str(e.value).endswith(SHAPE_TEXT)


def test_row_wrong_type_raises():
    """칸이 있어도 모양이 다르면 추측하지 않는다 — isBuyer 글 「false」를 매수로 읽거나 symbol 객체를 CSV로 넘기지 않는다."""
    from blackclip.binance.client import SHAPE_TEXT
    cases = [("spot", {**SPOT, "isBuyer": "false"}, fetch_trades), ("spot", {**SPOT, "symbol": {}}, fetch_trades),
             ("spot", {**SPOT, "qty": 12.0}, fetch_trades), ("spot", {**SPOT, "price": "abc"}, fetch_trades),
             ("futures", {**FUT, "side": "HOLD"}, fetch_trades), ("futures", {**FUT, "time": "1569514978020"}, fetch_trades),
             ("futures", {"symbol": "BTCUSDT", "tranId": 1, "time": 5, "income": {}, "asset": "USDT"}, fetch_funding),
             ("futures", {"symbol": "BTCUSDT", "tranId": 1, "time": 5, "income": "0.1", "asset": []}, fetch_funding)]
    for market, row, fn in cases:
        f = Fake(market, lambda p, q, r=row: [r] if q["startTime"] == 0 else [])
        with pytest.raises(BinanceError) as e:
            fn(f, "BTCUSDT", 0, 10) if fn is fetch_trades else fn(f, 0, 10)
        assert str(e.value).endswith(SHAPE_TEXT), row

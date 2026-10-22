"""binance.klines · check.datadiff: 공개 봉 받기와 데이터 차이."""
import http.client
import json
import types
from pathlib import Path

import pandas as pd
import pytest
from conftest import KlineClient, make_prices

from blackclip.binance.client import BinanceError, Client
from blackclip.binance.klines import fetch_klines
from blackclip.check.backtest import simulate
from blackclip.check.datadiff import data_diff
from blackclip.check.link import ExpressError, Link, LinkError, check_prices, check_weights
from blackclip.check.runner import SignalError


def _sig(prices):
    return pd.DataFrame({s: (d["Close"].rolling(5).mean() > d["Close"].rolling(20).mean()).astype(float) / len(prices)
                         for s, d in prices.items()})


def _weights(frames):
    """Runner.signals(frames=…)를 흉내: 신호 함수를 돌려 확인한 비중."""
    return check_weights(_sig(frames), frames)


def _case(*syms, n=300, freq="D", market="spot"):
    prices, _ = check_prices({s: make_prices(n, seed=i, freq=freq) for i, s in enumerate(syms)})
    link = Link("s1", Path("link.py"), Path("."), None, "next_open", market, None, None, None)
    return link, prices, simulate(prices, _weights(prices), "next_open")


def test_fetch_klines_pages():
    """1000개씩 이어 받아 빠짐 · 겹침 없이 붙인다."""
    df = make_prices(2500, freq="h")
    client = KlineClient({"BTCUSDT": df})
    start = int(df.index[0].timestamp() * 1000)
    end = int(df.index[-1].timestamp() * 1000) + 3_600_000
    got = fetch_klines(client, "BTCUSDT", "1h", start, end)
    assert len(got) == 2500 and len(client.calls) == 3
    assert got.index[0] == df.index[0] and str(got.index.tz) == "UTC"
    assert got["Close"].tolist() == pytest.approx(df["Close"].tolist())
    assert client.calls[0][0] == "/api/v3/klines"


def test_fetch_klines_bad_shape():
    """응답 모양이 다르면 BinanceError."""
    bad = types.SimpleNamespace(market="spot", get=lambda path, params=None, signed=True: [["x"]])
    with pytest.raises(BinanceError, match="모양"):
        fetch_klines(bad, "BTCUSDT", "1d", 0, 10)


def test_data_diff_same_data_zero_bp():
    """같은 데이터면 차이 0bp · 같은 신호로 체결 100% 일치."""
    link, prices, run = _case("BTCUSDT")
    r = data_diff(link, prices, run, 86400, KlineClient(prices), _weights)
    assert r["state"] == "잰 것"
    assert "중앙값 0.0bp" in r["text"] and "바이낸스에 없는 봉 0개" in r["text"] and "일치 100.0%" in r["text"]


def test_data_diff_detects_price_gap():
    """바이낸스 종가가 0.1% 다르면 10.0bp."""
    link, prices, run = _case("BTCUSDT")
    shifted = {s: p.assign(Close=p["Close"] * 1.001) for s, p in prices.items()}
    r = data_diff(link, prices, run, 86400, KlineClient(shifted), _weights)
    assert "중앙값 10.0bp" in r["text"]


def test_data_diff_unknown_symbol():
    """바이낸스에 없는 종목은 건너뛰고 적는다."""
    link, prices, run = _case("BTCUSDT", "XYZUSDT")
    r = data_diff(link, prices, run, 86400, KlineClient({"BTCUSDT": prices["BTCUSDT"]}), _weights)
    assert r["state"] == "잰 것" and "바이낸스에 없는 종목 1개(XYZUSDT)" in r["text"]


def test_data_diff_network_error():
    """네트워크가 끊기면 확인 불가(점검은 계속)."""
    link, prices, run = _case("BTCUSDT")
    r = data_diff(link, prices, run, 86400, KlineClient(prices, fail=OSError("cannot use /Users/alice/x.sock")), _weights)
    assert r["state"] == "확인 불가" and "못 받음(OSError)" in r["text"]
    assert "/Users/alice" not in r["text"]  # check.md에 들어가므로 이 컴퓨터 경로를 적지 않는다


def test_data_diff_interval_unmappable():
    """7시간 봉은 바이낸스 단위가 없어 부르지 않고 확인 불가."""
    link, prices, run = _case("BTCUSDT", freq="7h")
    client = KlineClient(prices)
    r = data_diff(link, prices, run, 7 * 3600, client, _weights)
    assert r["state"] == "확인 불가" and "봉 길이" in r["text"] and client.calls == []


def test_data_diff_signals_crash_type_only():
    """바이낸스 봉으로 신호 함수가 죽으면 예외 이름만 적고 숫자 줄은 남긴다."""
    link, prices, run = _case("BTCUSDT")

    def boom(frames):  # Runner.signals가 고객 코드의 죽음을 SignalError(예외 이름 + 원문)로 바꿔 올린다
        raise SignalError("RuntimeError: secret /Users/alice/data.csv", "RuntimeError")

    r = data_diff(link, prices, run, 86400, KlineClient(prices), boom)
    assert r["state"] == "잰 것" and "신호 함수가 돌지 않음(RuntimeError)" in r["text"] and "/Users/alice" not in r["text"]
    assert "중앙값 0.0bp" in r["text"]  # 숫자 줄은 남는다


@pytest.mark.parametrize("exc", [LinkError("고객 stderr /opt/x/secret.py"), ExpressError("합이 1을 넘음 /Users/alice/y")])
def test_data_diff_link_express_error_fixed_sentence(exc):
    """연결 · 표현 오류는 원문 대신 종류만 적는다(원문에 고객 stderr · 경로가 들 수 있다)."""
    link, prices, run = _case("BTCUSDT")

    def bad(frames):
        raise exc

    r = data_diff(link, prices, run, 86400, KlineClient(prices), bad)
    assert r["state"] == "잰 것" and f"신호를 확인할 수 없음({type(exc).__name__})" in r["text"]
    assert "/opt" not in r["text"] and "/Users/alice" not in r["text"]


def test_data_diff_engine_bug_propagates():
    """엔진 쪽 버그(SignalError도 LinkError도 아닌 예외)는 고객 탓으로 적지 않고 그대로 올린다."""
    link, prices, run = _case("BTCUSDT")

    def bug(frames):
        raise KeyError("engine")

    with pytest.raises(KeyError):
        data_diff(link, prices, run, 86400, KlineClient(prices), bug)


def test_data_diff_late_listing_not_counted_against():
    """늦게 상장한 종목의 상장 전 고객 체결은 분모에 넣지 않는다(같은 가격이면 100.0%)."""
    link, prices, run = _case("AAAUSDT", "BBBUSDT", n=400)
    late = prices["BBBUSDT"].iloc[150:]
    pre = [e for e in run.executions if e.symbol == "BBBUSDT" and e.time < late.index[0]]
    assert pre  # 이 시험이 뜻을 가지려면 상장 전 체결이 있어야 한다
    r = data_diff(link, prices, run, 86400, KlineClient({"AAAUSDT": prices["AAAUSDT"], "BBBUSDT": late}), _weights)
    assert "일치 100.0%" in r["text"] and "겹치는 기간 밖 고객 봉 150개" in r["text"]


def test_data_diff_counts_missing_and_outside_bars():
    """빠진 봉 · 겹치는 기간 밖 봉을 센 값이 그대로 나온다."""
    link, prices, run = _case("BTCUSDT")
    c = prices["BTCUSDT"]
    got = c.iloc[40:].drop(c.index[[100, 101, 200]])  # 앞 40봉은 상장 전 · 가운데 3봉은 빠짐
    r = data_diff(link, prices, run, 86400, KlineClient({"BTCUSDT": got}), _weights)
    assert "바이낸스에 없는 봉 3개" in r["text"] and "겹치는 기간 밖 고객 봉 40개" in r["text"]


def test_data_diff_max_bp():
    """한 봉만 1% 어긋나면 중앙값 0.0bp · 최대 100.0bp."""
    link, prices, run = _case("BTCUSDT")
    c = prices["BTCUSDT"]
    odd = c.assign(Close=c["Close"].where(c.index != c.index[100], c["Close"] / 1.01))
    r = data_diff(link, prices, run, 86400, KlineClient({"BTCUSDT": odd}), _weights)
    assert "중앙값 0.0bp · 최대 100.0bp" in r["text"]


def test_data_diff_futures_path():
    """선물 시장이면 선물 봉 경로를 부른다."""
    link, prices, run = _case("BTCUSDT", market="futures")
    client = KlineClient(prices, market="futures")
    r = data_diff(link, prices, run, 86400, client, _weights)
    assert r["state"] == "잰 것" and "바이낸스 futures 1d" in r["text"]
    assert {path for path, _ in client.calls} == {"/fapi/v1/klines"}


def test_data_diff_binance_error_other_than_unknown_symbol():
    """없는 종목 말고 바이낸스 오류(429 등)는 건너뛰지 않고 확인 불가."""
    link, prices, run = _case("BTCUSDT")
    r = data_diff(link, prices, run, 86400, KlineClient(prices, fail=BinanceError(429, -1003, "Too many requests")), _weights)
    assert r["state"] == "확인 불가" and "못 받음(바이낸스 응답 429" in r["text"]


def test_data_diff_5xx_html_body_never_in_text():
    """5xx인데 본문이 줄바꿈 · IP가 든 HTML이어도 근거에는 상태 번호만 남긴다(check.md는 커밋됨, 표가 깨지지 않게)."""
    html = b"<html>\n<head><title>502 Bad Gateway</title></head>\n<body>client 203.0.113.7 | cf-ray 1234</body></html>"
    client = Client("spot", False, transport=lambda url, headers: (502, {}, html), sleep=lambda s: None)
    link, prices, run = _case("BTCUSDT")
    r = data_diff(link, prices, run, 86400, client, _weights)
    assert r == {"state": "확인 불가", "text": "바이낸스 봉을 못 받음(바이낸스 응답 502)"}


def test_data_diff_binance_error_code_kept_msg_dropped():
    """JSON 오류면 상태 · 코드까지만, 문구는 뺀다. 응답 모양 오류(200)는 우리 고정 문구라 그대로."""
    link, prices, run = _case("BTCUSDT")
    r = data_diff(link, prices, run, 86400, KlineClient(prices, fail=BinanceError(429, -1003, "Too many\nrequests 10.0.0.1")), _weights)
    assert r["text"] == "바이낸스 봉을 못 받음(바이낸스 응답 429 · 코드 -1003)"
    r = data_diff(link, prices, run, 86400, KlineClient(prices, fail=BinanceError(200, None, "봉 응답 모양이 다릅니다")), _weights)
    assert r["text"] == "바이낸스 봉을 못 받음(바이낸스 응답 200 · 봉 응답 모양이 다릅니다)"


@pytest.mark.parametrize("exc", [json.JSONDecodeError("Expecting value: <html>/Users/alice", "<html>", 0),
                                 http.client.IncompleteRead(b"x"), http.client.BadStatusLine("HTTP/9 /Users/alice")])
def test_data_diff_transport_garbage(exc):
    """200인데 HTML이거나 응답이 끊겨도 죽지 않고 확인 불가(종류만, 원문 없음)."""
    link, prices, run = _case("BTCUSDT")
    r = data_diff(link, prices, run, 86400, KlineClient(prices, fail=exc), _weights)
    assert r["state"] == "확인 불가" and f"못 받음({type(exc).__name__})" in r["text"]
    assert "/Users/alice" not in r["text"] and "html" not in r["text"]

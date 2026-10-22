"""바이낸스 클라이언트: 서명 · 주소 고정 · 요청 한도 · 시각 맞추기 · 오류에 키 없음."""
import json

import pytest

from blackclip.binance.client import BinanceError, Client, sign
from blackclip.keystore import Credentials

DOC_SECRET = "NhqPtmdSJYdKjVHjA7PZj4Mge3R5YNiP1e3UZjInClVN65XAbvqqM6A7H5fATj0j"  # gitleaks:allow
DOC_QUERY = "symbol=LTCBTC&side=BUY&type=LIMIT&timeInForce=GTC&quantity=1&price=0.1&recvWindow=5000&timestamp=1499827319559"
CREDS = Credentials("K" * 64, "S" * 64)
NOW = 1_700_000_000.0


def test_sign_matches_binance_doc_example():
    """서명은 공식 문서의 HMAC 예시와 같다(SIGNED Endpoint Examples)."""
    assert sign(DOC_SECRET, DOC_QUERY) == "c8db56825ae71d6d79447849e617115f4a920fa2acdcab2b053c4b2838bd6b71"


class FakeTransport:
    """정해 둔 응답을 차례로 돌려주고 부른 주소를 남긴다."""

    def __init__(self, *responses):
        self.responses, self.calls = list(responses), []

    def __call__(self, url, headers):
        self.calls.append((url, headers))
        return self.responses.pop(0)


def ok(obj, weight="10"):
    """200 응답."""
    return 200, {"x-mbx-used-weight-1m": weight}, json.dumps(obj).encode()


def client(t, sleep=lambda s: None, market="spot"):
    """시각이 고정된 클라이언트."""
    return Client(market, False, CREDS, transport=t, clock=lambda: NOW, sleep=sleep)


def test_signed_get_query_and_header():
    """서명 요청은 timestamp · recvWindow · signature를 붙이고 키는 머리글에만 둔다."""
    t = FakeTransport(ok([]))
    client(t).get("/api/v3/myTrades", {"symbol": "BTCUSDT"})
    url, headers = t.calls[0]
    assert url.startswith("https://api.binance.com/api/v3/myTrades?symbol=BTCUSDT&timestamp=1700000000000&recvWindow=10000&signature=")
    query, signature = url.split("?", 1)[1].rsplit("&signature=", 1)
    assert signature == sign("S" * 64, query)
    assert headers == {"X-MBX-APIKEY": "K" * 64} and "S" * 64 not in url


def test_bases_fixed():
    """주소는 시장 · 테스트넷 여부로만 정해진다."""
    assert Client("futures", True).base == "https://testnet.binancefuture.com"
    assert Client("spot", False).base == "https://api.binance.com"
    assert Client("spot", True).base == "https://testnet.binance.vision"
    assert Client("futures", False).base == "https://fapi.binance.com"


def test_429_waits_retry_after_then_succeeds():
    """429면 Retry-After만큼 기다렸다 다시 부른다."""
    slept = []
    t = FakeTransport((429, {"retry-after": "7"}, b'{"code":-1003,"msg":"Too many requests"}'), ok([1]))
    assert client(t, slept.append).get("/x") == [1]
    assert slept == [7.0]


def test_418_not_retried():
    """418(IP 차단)은 다시 부르지 않고 기다릴 시간을 말한다."""
    t = FakeTransport((418, {"retry-after": "120"}, b'{"code":-1003,"msg":"banned"}'))
    with pytest.raises(BinanceError, match="418.*120초"):
        client(t).get("/x")
    assert len(t.calls) == 1


def test_clock_skew_syncs_time_once():
    """-1021(시각 어긋남)이면 서버 시각을 받아 맞춘 뒤 한 번 더 부른다."""
    t = FakeTransport((400, {}, b'{"code":-1021,"msg":"Timestamp outside of recvWindow"}'),
                      ok({"serverTime": 1_700_000_005_000}), ok([]))
    c = client(t)
    c.get("/api/v3/myTrades", {"symbol": "BTCUSDT"})
    assert c.offset_ms == 5000
    assert t.calls[1][0] == "https://api.binance.com/api/v3/time"
    assert "timestamp=1700000005000" in t.calls[2][0]


def test_heavy_weight_pauses_to_next_minute():
    """1분 사용량이 한도의 80%를 넘으면 다음 분까지 쉰다(현물 6000)."""
    slept = []
    client(FakeTransport(ok([], weight="4900")), slept.append).get("/x")
    assert slept == [61 - NOW % 60]


def test_error_message_has_no_secret():
    """오류 메시지에는 상태 · 코드 · 거래소 문구만 있고 키 · 서명은 없다."""
    t = FakeTransport((401, {}, b'{"code":-2015,"msg":"Invalid API-key, IP, or permissions for action."}'))
    with pytest.raises(BinanceError) as e:
        client(t).get("/x")
    text = str(e.value)
    assert "-2015" in text and e.value.code == -2015
    assert "S" * 64 not in text and "K" * 64 not in text and "signature" not in text


def test_non_json_error_body():
    """JSON이 아닌 오류 본문도 상태 번호를 대고 멈춘다."""
    with pytest.raises(BinanceError, match="502"):
        client(FakeTransport((502, {}, b"<html>bad gateway</html>"))).get("/x")


def test_path_cannot_change_host():
    """경로가 /로 시작하지 않으면 호스트가 바뀔 수 있어(base@evil/x) 보내기 전에 막는다."""
    t = FakeTransport()
    with pytest.raises(BinanceError, match="경로"):
        client(t).get("@evil.example/x")
    assert t.calls == []


def test_redirect_not_followed():
    """리다이렉트를 따라가지 않아 키 머리글이 다른 호스트로 가지 않는다."""
    import urllib.request

    from blackclip.binance.client import _OPENER, _NoRedirect
    r = urllib.request.Request("https://api.binance.com/x", headers={"X-MBX-APIKEY": "K"})
    assert _NoRedirect().redirect_request(r, None, 302, "Found", {}, "https://attacker.example/") is None
    assert any(isinstance(h, _NoRedirect) for h in _OPENER.handlers)


def test_bad_key_shape_never_echoed():
    """키에 개행이 들어 있으면 요청 전에 멈추고, 오류 문구에 키가 없으며 transport는 부르지 않는다."""
    t = FakeTransport()
    bad = Credentials("A" * 60 + "WXYZ\n", "S" * 64)
    with pytest.raises(BinanceError) as exc:
        Client("spot", False, bad, transport=t, clock=lambda: NOW).get("/api/v3/myTrades", {"symbol": "BTCUSDT"})
    assert "A" * 60 not in str(exc.value) and "저장된 키 모양이 다릅니다" in str(exc.value)
    assert t.calls == []


def test_local_error_has_no_status_prefix():
    """요청을 보내지 않은 로컬 오류(status 0)는 「바이낸스 응답 0」 앞머리 없이 문구만 쓴다."""
    assert str(BinanceError(0, None, "경로는 /로 시작해야 합니다")) == "경로는 /로 시작해야 합니다"
    assert str(BinanceError(400, -1121, "Invalid symbol.")) == "바이낸스 응답 400 · 코드 -1121 · Invalid symbol."


def test_incomplete_read_becomes_oserror(monkeypatch):
    """응답이 중간에 끊기면(IncompleteRead) OSError로 바꿔 종목별 「받지 못함」 길로 보낸다."""
    import http.client

    import pytest

    import blackclip.binance.client as client_mod

    class Cut:
        status, headers = 200, {}

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

        def read(self):
            raise http.client.IncompleteRead(b"{")

    monkeypatch.setattr(client_mod._OPENER, "open", lambda req, timeout: Cut())
    with pytest.raises(OSError, match="중간에 끊겼"):
        client_mod.urllib_transport("https://api.binance.com/api/v3/time", {})


def test_incomplete_read_on_error_body_becomes_oserror(monkeypatch):
    """오류 상태(502) 응답의 본문이 중간에 끊겨도 OSError로 바꾼다(e.read()도 같은 길)."""
    import http.client
    import io
    import urllib.error

    import pytest

    import blackclip.binance.client as client_mod

    class CutBody(io.BytesIO):
        def read(self, *a):
            raise http.client.IncompleteRead(b"")

    def boom(req, timeout):
        raise urllib.error.HTTPError(req.full_url, 502, "Bad Gateway", {}, CutBody())

    monkeypatch.setattr(client_mod._OPENER, "open", boom)
    with pytest.raises(OSError, match="중간에 끊겼"):
        client_mod.urllib_transport("https://api.binance.com/api/v3/time", {})


def test_get_bad_json_is_binance_error():
    """200인데 JSON이 아니면 traceback 없이 「모양이 다름」 BinanceError(조회 실패 길)."""
    from blackclip.binance.client import SHAPE_TEXT
    with pytest.raises(BinanceError) as e:
        client(FakeTransport((200, {}, b"<html>maintenance</html>"))).get("/api/v3/myTrades", {})
    assert (e.value.status, e.value.code, str(e.value)) == (0, None, SHAPE_TEXT)


def test_sync_time_bad_shape():
    """-1021 뒤 서버 시각 응답이 모양이 다르면(숫자 아님 · 리스트) KeyError · TypeError가 아니라 BinanceError."""
    from blackclip.binance.client import SHAPE_TEXT
    for bad in ({"serverTime": "soon"}, []):
        t = FakeTransport((400, {}, b'{"code":-1021,"msg":"Timestamp outside of recvWindow"}'), ok(bad))
        with pytest.raises(BinanceError) as e:
            client(t).get("/api/v3/myTrades", {})
        assert str(e.value).endswith(SHAPE_TEXT)

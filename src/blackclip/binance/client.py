"""바이낸스 REST 조회(GET만). 키 값은 오류 · 출력에 넣지 않는다."""
from __future__ import annotations

import hashlib
import hmac
import http.client
import json
import re
import time
import urllib.error
import urllib.parse
import urllib.request
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

from blackclip.keystore import Credentials

BASES: dict[tuple[str, bool], str] = {
    ("spot", False): "https://api.binance.com",
    ("spot", True): "https://testnet.binance.vision",
    ("futures", False): "https://fapi.binance.com",
    ("futures", True): "https://testnet.binancefuture.com",
}
"""(시장, 테스트넷 여부) → 주소. 이 넷 말고는 부르지 않는다."""

WEIGHT_LIMIT = {"spot": 6000, "futures": 2400}
"""1분 REQUEST_WEIGHT 한도(exchangeInfo 실호출, 2026-10-15)."""

TIME_PATH = {"spot": "/api/v3/time", "futures": "/fapi/v1/time"}
"""서버 시각 주소."""

KEY_SHAPE = re.compile(r"[A-Za-z0-9]+")
"""바이낸스 API 키 모양(영문 · 숫자만). 개행 같은 제어 문자가 든 키는 http.client가 키 전체를 담은 ValueError를 내므로 요청 전에 막는다."""

Transport = Callable[[str, dict[str, str]], tuple[int, dict[str, str], bytes]]
"""GET 한 번: (주소, 머리글) → (상태, 소문자 머리글, 본문)."""


SHAPE_TEXT = "바이낸스 응답 모양이 달라 조회하지 못했어요"
"""200 응답이 JSON이 아니거나 쓰는 칸의 모양이 다를 때의 BinanceError 글(status 0). 모르는 모양은 추측해서 읽지 않는다."""


class BinanceError(RuntimeError):
    """바이낸스가 200이 아닌 응답을 줬거나, 요청을 보내기 전 로컬에서 멈췄거나, 응답 모양을 읽지 못했을 때(status 0). 메시지에 키 · 서명을 넣지 않는다."""

    def __init__(self, status: int, code: int | None, msg: str) -> None:
        """Args:
            status: HTTP 상태. 0이면 요청을 보내지 않은 로컬 오류라 「바이낸스 응답」 앞머리 없이 msg만 쓴다.
            code: 바이낸스 오류 코드(없으면 None).
            msg: 바이낸스 문구(200자까지) 또는 로컬 오류 문구.
        """
        super().__init__(msg if status == 0 else
                         f"바이낸스 응답 {status}" + (f" · 코드 {code}" if code is not None else "") + (f" · {msg}" if msg else ""))
        self.status, self.code = status, code


def sign(secret: str, query: str) -> str:
    """HMAC SHA256 서명.

    Args:
        secret: 비밀 키.
        query: 서명할 쿼리 문자열.

    Returns:
        16진 서명.
    """
    return hmac.new(secret.encode(), query.encode(), hashlib.sha256).hexdigest()


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    """리다이렉트를 따라가지 않는다. urllib 기본 처리는 키 머리글을 다른 호스트로 그대로 복사한다."""

    def redirect_request(self, req, fp, code, msg, headers, newurl):  # noqa: ANN001
        """늘 None을 돌려 3xx를 HTTPError로 끝낸다.

        Returns:
            None.
        """
        return None


_OPENER = urllib.request.build_opener(_NoRedirect)
"""리다이렉트를 따라가지 않는 열기 도구."""


def urllib_transport(url: str, headers: dict[str, str]) -> tuple[int, dict[str, str], bytes]:
    """표준 라이브러리로 GET 한 번(리다이렉트는 따라가지 않고 3xx 상태로 돌려준다).

    Args:
        url: 주소.
        headers: 머리글.

    Returns:
        (상태, 소문자 머리글, 본문).
    """
    req = urllib.request.Request(url, headers=headers)
    try:  # 바깥 try: 오류 상태 응답의 본문(e.read())이 끊겨도 같은 길로 잡는다
        try:
            with _OPENER.open(req, timeout=20) as r:
                return r.status, {k.lower(): v for k, v in r.headers.items()}, r.read()
        except urllib.error.HTTPError as e:
            return e.code, {k.lower(): v for k, v in e.headers.items()}, e.read()
    except http.client.HTTPException as e:  # IncompleteRead 등: 응답이 중간에 끊김 → 호출 쪽 OSError 길로
        raise OSError(f"바이낸스 응답이 중간에 끊겼습니다({type(e).__name__})") from None


def _error_body(body: bytes) -> dict:
    """오류 본문을 dict로 읽는다. JSON이 아니면 앞부분만 문구로.

    Args:
        body: 응답 본문.

    Returns:
        {"code", "msg"}를 담을 수 있는 dict.
    """
    try:
        data = json.loads(body)
        return data if isinstance(data, dict) else {}
    except ValueError:
        return {"msg": body[:200].decode("utf-8", "replace")}


@dataclass
class Client:
    """바이낸스 조회 클라이언트 하나(시장 하나).

    Attributes:
        market: "spot" 또는 "futures"(USDⓈ-M).
        testnet: 테스트넷이면 True.
        creds: 키(서명 요청에 필요).
        transport: GET 한 번(시험에서 바꿔 끼움).
        clock: 지금 시각(초).
        sleep: 기다리기.
        offset_ms: 서버 시각 - 이 컴퓨터 시각.
    """

    market: str
    testnet: bool
    creds: Credentials | None = None
    transport: Transport = urllib_transport
    clock: Callable[[], float] = time.time
    sleep: Callable[[float], None] = time.sleep
    offset_ms: int = 0

    @property
    def base(self) -> str:
        """이 클라이언트의 주소.

        Returns:
            BASES의 값.
        """
        return BASES[(self.market, self.testnet)]

    def get(self, path: str, params: dict[str, Any] | None = None, signed: bool = True) -> Any:
        """GET 하나. 429는 기다렸다 다시, -1021은 시각을 맞춰 다시(둘을 합쳐 두 번까지), 418은 바로 멈춘다.

        Args:
            path: 경로(/api/v3/myTrades 등).
            params: 쿼리.
            signed: 서명 요청이면 True.

        Returns:
            JSON 응답.
        """
        if not path.startswith("/"):  # "@evil.example/x"가 base 뒤에 붙으면 호스트가 바뀌어 키 머리글이 새 나간다
            raise BinanceError(0, None, "경로는 /로 시작해야 합니다")
        for attempt in range(3):
            query = dict(params or {})
            headers: dict[str, str] = {}
            if signed:
                if self.creds is None:
                    raise BinanceError(0, None, "키 없이 서명 요청을 보낼 수 없습니다")
                if not KEY_SHAPE.fullmatch(self.creds.api_key):  # 문구에 키를 넣지 않는다
                    raise BinanceError(0, None, "저장된 키 모양이 다릅니다 · blackclip connect --open으로 다시 넣으세요")
                query["timestamp"] = int(self.clock() * 1000) + self.offset_ms
                query["recvWindow"] = 10000
                qs = urllib.parse.urlencode(query)
                qs += "&signature=" + sign(self.creds.secret_key, qs)
                headers["X-MBX-APIKEY"] = self.creds.api_key
            else:
                qs = urllib.parse.urlencode(query)
            status, hdrs, body = self.transport(f"{self.base}{path}" + (f"?{qs}" if qs else ""), headers)
            self._pace(hdrs)
            if status == 200:
                try:
                    return json.loads(body)
                except ValueError:  # 점검 페이지 HTML 등: 「조회 실패」로
                    raise BinanceError(0, None, SHAPE_TEXT) from None
            data = _error_body(body)
            code, msg = data.get("code"), str(data.get("msg", ""))[:200]
            code = code if type(code) is int else None  # 바이낸스 코드는 정수: 다른 서버의 글자 code는 문구에 싣지 않음
            if status == 418:
                raise BinanceError(418, code, f"IP가 일시 차단됨 · {hdrs.get('retry-after', '?')}초 뒤 다시")
            if status == 429 and attempt < 2:
                self.sleep(float(hdrs.get("retry-after", "60")))
                continue
            if code == -1021 and signed and attempt < 2:
                self._sync_time()
                continue
            raise BinanceError(status, code, msg)
        raise BinanceError(0, None, "다시 부르기 횟수를 넘었습니다")

    def _sync_time(self) -> None:
        """서버 시각을 받아 offset_ms를 맞춘다."""
        got = self.get(TIME_PATH[self.market], signed=False)
        server = got.get("serverTime") if isinstance(got, dict) else None
        if type(server) is not int:
            raise BinanceError(0, None, SHAPE_TEXT)
        self.offset_ms = server - int(self.clock() * 1000)

    def _pace(self, hdrs: dict[str, str]) -> None:
        """1분 사용량이 한도의 80%를 넘으면 다음 분까지 쉰다.

        Args:
            hdrs: 응답 머리글(소문자).
        """
        used = hdrs.get("x-mbx-used-weight-1m")
        if used and int(used) >= WEIGHT_LIMIT[self.market] * 0.8:
            self.sleep(61 - self.clock() % 60)

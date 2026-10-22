"""연결 화면의 공용 부품(보안 최소선): 보안 처리기 바탕 · 요청 검사 · 폼 값 검증 · 설정 · 결과.

서버는 `dashboard.make_server` 하나다(상주 대시보드 · `blackclip connect --open` 한 번용). 127.0.0.1에만 열고, 실행마다 바뀌는 접속 토큰과
폼 CSRF 토큰을 확인한다. 키는 저장만 하고 다시 보여 주지 않는다(끝 4자리만). 권한 검사에서 거부된 키는 저장하지 않는다.
"""
from __future__ import annotations

import html
import re
import secrets
from collections.abc import Callable
from dataclasses import dataclass, field
from http.server import BaseHTTPRequestHandler
from typing import Any
from urllib.parse import parse_qs, urlsplit

from blackclip.binance.permissions import Permission
from blackclip.keystore import Credentials
from blackclip.notify import _HC, QUIET
from blackclip.sim.warning_line import SENSITIVITY
from blackclip.telegram import TOKEN

MAX_BODY = 4096
"""폼 본문 상한(바이트)."""

BAD_REQUEST_TEXT = "요청 모양이 맞지 않아요. 브라우저 주소를 다시 열어 주세요."
"""400(Host 없음 · 둘, 깨진 요청 줄 등) 본문 한 줄(`SecureHandler.send_error`)."""

_KEY = re.compile(r"^[A-Za-z0-9]{16,128}$")
"""키 모양(바이낸스 키는 영문 · 숫자 64자, 여유를 둠)."""

HEADERS = {
    "Content-Type": "text/html; charset=utf-8",
    "Cache-Control": "no-store",
    "Referrer-Policy": "same-origin",
    "X-Frame-Options": "DENY",
    "X-Content-Type-Options": "nosniff",
    "Content-Security-Policy": "default-src 'none'; style-src 'unsafe-inline'; form-action 'self'; frame-ancestors 'none'",
}
"""모든 응답에 붙는 머리글. Referrer-Policy는 same-origin: no-referrer면 브라우저가 같은 출처 폼 POST에도 Origin: null을 붙여 정상 제출이 막힌다. same-origin이면 실제 Origin이 붙고, 다른 출처로는 Referer(접속 토큰)가 나가지 않는다(CSP default-src 'none'이라 다른 출처 요청도 없음)."""


KEY_SHAPE_TEXT = "키 모양이 달라요. 영문과 숫자로 16~128자예요."
"""대시보드 연결 화면: API 키 · 시크릿 모양이 틀렸을 때 실패 줄."""

TOKEN_SHAPE_TEXT = "토큰 모양이 달라요. BotFather가 준 숫자, 콜론, 영문과 숫자를 모두 넣어 주세요."
"""대시보드 연결 화면: 봇 토큰 모양이 틀렸을 때 실패 줄."""

CHOOSE_SHAPE_TEXT = "민감도와 조용한 날을 하나씩 골라 주세요."
"""대시보드 연결 화면: 고르기 값이 없거나 모르는 값일 때 실패 줄."""

HEALTHCHECK_SHAPE_TEXT = "헬스체크 주소는 https로 시작하는 주소 하나예요. 공백 없이 300자까지예요."
"""대시보드 연결 화면: 헬스체크 주소 모양이 틀렸을 때 실패 줄."""

KINDS = {"key": "조회 키", "telegram": "텔레그램", "choose": "고르기"}
"""대시보드 연결 화면의 패널(POST `/connect/<kind>`) → 패널 이름."""


@dataclass
class Connect:
    """대시보드 서버의 연결 화면 설정. 계정은 서버가 정한다(상주는 main, 한 번용은 `--account` · `--testnet`).

    Attributes:
        store: 키 보관(`save` · `load` · `name`).
        label: 계정 이름(`--account`).
        testnet: 테스트넷 키면 True.
        title: 한 번용 화면 제목(상주 화면은 「연결」).
        check_key: 조회 키 → 권한 확인 결과(바이낸스 권한 조회).
        check_telegram: 봇 토큰(Credentials(토큰, "")) → 확인 결과(getMe).
        csrf: 폼 CSRF 토큰(서버마다 새로, 접속 토큰과 다른 값). 쓰기가 일어난 POST마다 서버 Lock 안에서 새 값으로 바뀐다.
    """

    store: Any
    label: str
    testnet: bool
    title: str
    check_key: Callable[[Credentials], Permission]
    check_telegram: Callable[[Credentials], Permission]
    csrf: str = field(default_factory=lambda: secrets.token_urlsafe(32))


@dataclass
class Saved:
    """연결 화면 저장 한 번의 결과(한 번용 서버면 `Once.result`, CLI가 읽음).

    Attributes:
        kind: `key` · `telegram` · `choose`.
        last4: 저장한 키 · 토큰의 끝 4자리(choose면 None).
        store_name: 키 보관 이름(choose면 None).
        permission: 확인 결과. 키 보관 저장이 예외를 내 저장했는지 모르면 None(choose도 None).
        state_error: 저장 뒤 일이 실패했을 때 그 사유(키 보관 예외 또는 연결 상태 기록 실패, 터미널용). 다 됐으면 None.
    """

    kind: str
    last4: str | None
    store_name: str | None
    permission: Permission | None
    state_error: str | None = None


def form_value(kind: str, form: dict[str, str]) -> Credentials | dict[str, Any] | str:
    """연결 화면 폼 값을 검증한다(대시보드 POST). 실패 줄은 해요체이고 입력 값을 담지 않는다.

    Args:
        kind: `key` · `telegram` · `choose`.
        form: 본문을 푼 값(칸마다 첫 값).

    Returns:
        key · telegram은 Credentials, choose는 `notify.change_notify`에 넘길 값 사전, 틀렸으면 실패 줄(str).
    """
    if kind == "key":
        key, secret = form.get("api_key", "").strip(), form.get("secret_key", "").strip()
        return Credentials(key, secret) if _KEY.match(key) and _KEY.match(secret) else KEY_SHAPE_TEXT
    if kind == "telegram":
        token = form.get("bot_token", "").strip()
        return Credentials(token, "") if TOKEN.fullmatch(token) else TOKEN_SHAPE_TEXT
    if form.get("sensitivity") not in SENSITIVITY or form.get("quiet") not in QUIET:
        return CHOOSE_SHAPE_TEXT
    hc = form.get("healthcheck", "").strip() or None
    if hc is not None and not _HC.fullmatch(hc):
        return HEALTHCHECK_SHAPE_TEXT
    return {"sensitivity": form["sensitivity"], "quiet": form["quiet"], "healthcheck": hc,
            "execution_alerts": form.get("execution_alerts") == "on" if "ea_shown" in form else None}


def one_host(headers: Any) -> bool:
    """Host 머리글 줄이 하나뿐인지(RFC 9112 §3.2: 없거나 둘 이상이면 400).

    Args:
        headers: 요청 머리글. HTTP/0.9 요청이면 3.13부터 표준 라이브러리가 머리글을 읽지 않고 평범한 dict를 둔다(`BaseHTTPRequestHandler.parse_request`, 겹칠 수 없음).
            3.12까지는 빈 줄까지 머리글을 읽어 Host 줄이 없는 머리글이 된다.

    Returns:
        하나면 True. dict(3.13부터의 HTTP/0.9)는 True(Host 유무는 `request_allowed`가 따로 봐 403). 3.12까지의 HTTP/0.9는 Host가 없어 False(400).
    """
    get_all = getattr(headers, "get_all", None)
    return get_all is None or len(get_all("Host") or []) == 1


def request_allowed(headers: Any, path: str, port: int, token: str) -> bool:
    """Host · Origin · 접속 토큰을 확인한다(대시보드 서버의 모든 요청, 보안 최소선).

    Args:
        headers: 요청 머리글(`.get`이 있는 것).
        path: 요청 대상(`?t=` 접속 토큰이 붙음).
        port: 서버 포트.
        token: 이번 실행의 접속 토큰.

    Returns:
        Host가 하나뿐이고 127.0.0.1 · localhost(그 포트), Origin이 없거나 같은 출처, 토큰이 같으면 True. 요청 대상이 절대 주소
        (`http://evil.example/?t=…`)이거나 깨져 urlsplit이 ValueError를 내면(예: `http://[`) False.
    """
    hosts = {f"127.0.0.1:{port}", f"localhost:{port}"}
    origin = headers.get("Origin")
    if not one_host(headers):
        return False
    try:
        target = urlsplit(path)
    except ValueError:
        return False
    # 절대 주소 꼴은 일부러 거절: RFC 9112 §3.2.2는 받아들이고 Host 대신 쓰라(MUST)지만, 이 서버는 루프백 전용이고
    # 브라우저는 origin-form만 보내며, Host 검사를 지난 요청이 Host와 다른 주인을 대게 두지 않는다.
    if target.scheme or target.netloc:
        return False
    got = parse_qs(target.query).get("t", [""])[0]
    return (headers.get("Host") in hosts
            and (origin is None or origin in {f"http://{h}" for h in hosts})
            and secrets.compare_digest(got.encode(), token.encode()))  # 바이트 비교: 비ASCII 값이면 str 비교가 TypeError


class SecureHandler(BaseHTTPRequestHandler):
    """대시보드 서버 처리기의 바탕: 모든 응답에 `HEADERS`, 요청 로그 없음, 버전 숨김, 소켓 시간 제한, 검사 순서.

    표준 라이브러리는 400 · 414 · 431 · 501 · 505를 do_*를 거치지 않고 `send_error`로 낸다(`BaseHTTPRequestHandler.parse_request` · `handle_one_request`).
    그래서 `send_error`를 여기서 덮어 `HEADERS`와 본문 한 줄짜리 짧은 문서를 붙인다(기본 처리기의 message는 요청 글이 들 수 있어 쓰지 않음).
    """

    server_version = "blackclip"
    sys_version = ""
    timeout = 30
    """요청 하나의 소켓 시간 제한(초). 느린 연결이 화면을 붙잡지 않게."""

    def log_message(self, format: str, *args: Any) -> None:  # noqa: A002
        """요청 줄에 접속 토큰이 있어 남기지 않는다."""

    def _send(self, status: int, body: bytes, extra: dict[str, str] | None = None) -> None:
        """응답 하나: 상태 줄 · `HEADERS` · 길이 · 본문(HEAD면 본문 없음).

        깨진 요청 줄(400 · 505)과 HTTP/0.9 요청은 표준 라이브러리가 버전을 기본값 `HTTP/0.9`로 둬(`default_request_version`)
        상태 줄과 머리글을 통째로 뺀다(`send_response_only` · `send_header` · `end_headers`). 여기서 `HTTP/1.0`으로 바꿔 모든 응답에 머리글이 나가게 한다(0.9 요청은 Host가
        없어 검사에서 403이라 200에 닿지 않음).

        Args:
            status: HTTP 상태.
            body: 본문.
            extra: `HEADERS`의 같은 이름 머리글을 덮을 값과 더할 머리글(나머지 보안 머리글은 그대로 나감).
        """
        if self.request_version == "HTTP/0.9":
            self.request_version = "HTTP/1.0"
        self.send_response(status)
        for k, v in (HEADERS | (extra or {})).items():
            self.send_header(k, v)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(body)

    def send_error(self, code: int, message: str | None = None, explain: str | None = None, allow: str = "GET, HEAD") -> None:
        """오류 응답 하나: `HEADERS` + `Connection: close` + 짧은 문서 하나(405면 `Allow`, RFC 9110 §15.5.6).

        문서는 `lang` · 본문 한 줄과 같은 제목 · `<main>`을 갖춘다(쪽 언어 · 쪽 제목, WCAG 3.1.1 · 2.4.2). `lang`은 쪽에 실제로 쓰인 글의 언어다: 우리 글
        (`explain`을 `message` 없이 넘긴 호출 · 400의 `BAD_REQUEST_TEXT`)은 `ko`, 표준 라이브러리가 내는 영어 글(`message`를 함께 넘긴 431의 explain · 상태의 짧은
        이름 414 · 501 · 505)은 `en`. 돌아가는 링크는 넣지 않는다: 토큰 · 경로를 본문에 쓰지 않으려고, 본문 글이 이미 다시 열라고 말한다.

        Args:
            code: HTTP 상태.
            message: 쓰지 않음(기본 처리기 호환, 요청 글이 들 수 있음). 우리 호출은 넘기지 않으므로 표준 라이브러리 호출을 가르는 데만 읽는다.
            explain: 본문 한 줄. None이면 400은 `BAD_REQUEST_TEXT`, 그 밖은 상태의 짧은 이름.
            allow: 405의 `Allow` 값(그 경로가 지금 받는 방법). 기본은 `GET, HEAD`.
        """
        ours = (explain is not None and message is None) or (explain is None and code == 400)
        text = html.escape(explain or (BAD_REQUEST_TEXT if code == 400 else self.responses.get(code, ("",))[0]))
        body = (f'<!doctype html><html lang="{"ko" if ours else "en"}"><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">'
                f"<title>{text}</title><main><p>{text}</p></main></html>").encode()
        self._send(code, body, {"Connection": "close"} | ({"Allow": allow} if code == 405 else {}))

    def _refusal(self, token: str) -> int | None:
        """보안 검사(라우트보다 먼저 부른다). Host가 없거나 둘 이상이면 400(RFC 9112 §3.2 MUST), 그 밖 검사 실패는 403.

        Args:
            token: 이번 실행의 접속 토큰.

        Returns:
            거절할 상태 코드(400 · 403), 통과면 None.
        """
        if not one_host(self.headers):
            return 400
        return None if request_allowed(self.headers, self.path, self.server.server_address[1], token) else 403

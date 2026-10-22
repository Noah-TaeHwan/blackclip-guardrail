"""텔레그램 봇으로 알림 보내기.

토큰은 요청 주소 경로(`/bot<토큰>/…`)에만 들어간다. 주소 · 토큰을 화면 · 파일 · 오류 문구에 남기지 않는다.
글은 평문(parse_mode 없음)으로 보내 이스케이프 실수가 없게 한다.
"""
from __future__ import annotations

import http.client
import json
import re
import time
import urllib.error
import urllib.parse
import urllib.request
from collections.abc import Callable
from typing import Any

API = "https://api.telegram.org"
"""Bot API 주소(공식 문서 Making requests)."""

TOKEN = re.compile(r"[0-9]{5,16}:[A-Za-z0-9_-]{30,80}")
"""봇 토큰 모양(공식 문서 예: 123456:ABC-DEF1234ghIkl-zyx57W2v1u123ew11)."""

LIMIT = 4096
"""sendMessage 글자 수 상한(공식 문서 「1-4096 characters」)."""

GAP_S = 1.0
"""한 대화에 여러 통을 보낼 때 사이 간격(초, 공식 FAQ 「one message per second」)."""

MAX_WAIT_S = 60
"""429 retry_after를 이 초까지만 기다려 한 번 다시 보낸다."""

Transport = Callable[[str, dict[str, str]], tuple[int, dict]]
"""(주소, 폼) → (HTTP 상태, JSON 본문). 시험에서 바꿔 끼운다."""


class TelegramError(ValueError):
    """텔레그램에 보내지 못했을 때. 메시지에 토큰 · 주소를 넣지 않는다."""


def urllib_transport(url: str, form: dict[str, str]) -> tuple[int, dict]:
    """폼을 POST로 보내고 JSON 본문을 읽는다.

    Args:
        url: 요청 주소(토큰이 들어 있음 · 문구에 쓰지 않는다).
        form: 폼 값.

    Returns:
        (HTTP 상태, JSON 본문). JSON이 아니면 ok False인 본문을 만든다.

    Raises:
        TelegramError: 닿지 못했을 때(예외 종류 이름만).
    """
    req = urllib.request.Request(url, data=urllib.parse.urlencode(form).encode(), method="POST")
    try:
        try:
            with urllib.request.urlopen(req, timeout=40) as resp:  # getUpdates long polling 30초보다 길게
                status, body = resp.status, resp.read()
        except urllib.error.HTTPError as exc:
            status, body = exc.code, exc.read()
    # URLError 문구에 주소가 들 수 있어 종류만 · 본문 읽다 끊김(IncompleteRead)도 같은 오류로
    except (urllib.error.URLError, OSError, http.client.HTTPException) as exc:
        raise TelegramError(f"텔레그램에 닿지 못함({type(exc).__name__})") from None
    try:
        return status, json.loads(body)
    except ValueError:
        return status, {"ok": False, "description": f"JSON이 아닌 응답(HTTP {status})"}


def chunks(text: str, limit: int = LIMIT) -> list[str]:
    """글을 줄 단위로 묶어 limit 글자 이하 조각으로 나눈다(한 줄이 limit보다 길면 그 줄을 자른다).

    Args:
        text: 보낼 글.
        limit: 조각 글자 수 상한.

    Returns:
        조각 목록(빈 글이면 빈 목록).
    """
    out: list[str] = []
    cur: str | None = None
    for line in text.splitlines():
        while len(line) > limit:
            if cur is not None:
                out.append(cur)
                cur = None
            out.append(line[:limit])
            line = line[limit:]
        if cur is None:
            cur = line
        elif len(cur) + 1 + len(line) > limit:
            out.append(cur)
            cur = line
        else:
            cur = f"{cur}\n{line}"
    if cur:
        out.append(cur)
    return [c for c in out if c.strip()]


def find_chat(updates: list[dict], code: str) -> int | None:
    """업데이트에서 1회용 숫자를 보낸 개인 대화 번호를 찾는다. 연결 시작 전에 밀린 글은 `skip_backlog`로 미리 거른다.

    Args:
        updates: getUpdates 결과.
        code: 터미널에 띄운 숫자.

    Returns:
        대화 번호, 없으면 None.
    """
    for u in updates:
        msg = u.get("message") if isinstance(u, dict) else None
        chat = msg.get("chat") if isinstance(msg, dict) else None
        if (isinstance(chat, dict) and chat.get("type") == "private" and isinstance(chat.get("id"), int)
                and str(msg.get("text", "")).strip() == code):
            return chat["id"]
    return None


class Bot:
    """봇 하나. 토큰은 이 객체 안에만 있다(repr에 안 나옴)."""

    def __init__(self, token: str, transport: Transport = urllib_transport,
                 sleep: Callable[[float], None] = time.sleep) -> None:
        """Args:
            token: 봇 토큰.
            transport: 요청 보내는 것(시험에서 가짜).
            sleep: 기다리는 것(시험에서 가짜).

        Raises:
            TelegramError: 토큰 모양이 다를 때(값은 문구에 넣지 않음).
        """
        if not TOKEN.fullmatch(token):
            raise TelegramError("봇 토큰 모양이 다릅니다(숫자:영문 · 숫자 · - · _) · BotFather가 준 토큰 전체를 넣어 주세요")
        self._token, self._transport, self._sleep = token, transport, sleep

    def __repr__(self) -> str:
        """토큰 없이 보인다.

        Returns:
            "Bot(…끝 4자리)".
        """
        return f"Bot(…{self._token[-4:]})"

    def _call(self, method: str, form: dict[str, str] | None = None) -> Any:
        """메서드 하나를 부른다. 429면 retry_after(60초 이하)만큼 기다려 한 번 다시.

        Args:
            method: getMe · getUpdates · sendMessage.
            form: 폼 값.

        Returns:
            응답 result.

        Raises:
            TelegramError: ok가 아닐 때(토큰 · 대화 번호를 가린 뒤 설명 200자).
        """
        retried = False
        while True:
            status, body = self._transport(f"{API}/bot{self._token}/{method}", form or {})
            if isinstance(body, dict) and body.get("ok") is True:
                return body.get("result")
            body = body if isinstance(body, dict) else {}
            wait = (body.get("parameters") or {}).get("retry_after")
            if status == 429 and not retried and isinstance(wait, int) and 0 < wait <= MAX_WAIT_S:
                retried = True
                self._sleep(wait)
                continue
            desc = str(body.get("description") or "").replace(self._token, "<토큰>")  # 가린 뒤에 자른다
            if (form or {}).get("chat_id"):
                desc = re.sub(rf"(?<!\d){re.escape(form['chat_id'])}(?!\d)", "<대화>", desc)
            desc = desc[:200]
            raise TelegramError(f"텔레그램 {method} 실패(HTTP {status}{' · ' + desc if desc else ''})")

    def get_me(self) -> dict:
        """토큰이 맞는지 확인한다(getMe).

        Returns:
            봇 정보(username 등).
        """
        me = self._call("getMe")
        return me if isinstance(me, dict) else {}

    def updates(self, timeout_s: int, offset: int | None = None) -> list[dict]:
        """받은 글을 가져온다(getUpdates long polling).

        Args:
            timeout_s: 기다릴 초.
            offset: 이 번호 이상만 받음 — 앞 것은 확인 처리, None이면 처음.

        Returns:
            업데이트 목록.
        """
        form = {"timeout": str(timeout_s), "allowed_updates": '["message"]'}
        if offset is not None:
            form["offset"] = str(offset)
        got = self._call("getUpdates", form)
        return got if isinstance(got, list) else []

    def send(self, chat_id: int, text: str) -> int:
        """글을 보낸다. 4096자를 넘으면 줄 단위로 나눠 1초 간격으로.

        Args:
            chat_id: 대화 번호.
            text: 글(평문).

        Returns:
            보낸 통 수.
        """
        parts = chunks(text)
        for i, part in enumerate(parts):
            if i:
                self._sleep(GAP_S)
            self._call("sendMessage", {"chat_id": str(chat_id), "text": part})
        return len(parts)


def skip_backlog(bot: Bot) -> int | None:
    """숫자를 띄우기 전에 봇에 밀려 있던 업데이트를 텔레그램 쪽에서 잊게 하고 그다음 번호를 돌려준다.

    getUpdates의 음수 offset은 큐 끝에서 그만큼만 주고 그 앞은 잊는다(-1 = 마지막 하나, 공식 문서 getUpdates offset:
    "retrieve updates starting from -offset update from the end of the updates queue. All previous updates will be
    forgotten."). 받은 마지막 하나는 그다음 번호로 부를 때 확인된다(bind_chat 첫 호출). 시작 기준이 텔레그램 업데이트
    순서라 맥 시계가 어긋나도 옛 글로 잇지 않는다(message.date를 맥 시계와 비교하면 시계가 느릴 때 뚫림).

    Args:
        bot: 봇.

    Returns:
        다음에 받을 업데이트 번호, 밀린 것이 없으면 None.
    """
    ids = [u["update_id"] for u in bot.updates(0, -1) if isinstance(u, dict) and isinstance(u.get("update_id"), int)]
    return max(ids) + 1 if ids else None


def bind_chat(bot: Bot, code: str, wait_s: float, clock: Callable[[], float] = time.monotonic,
              offset: int | None = None) -> int | None:
    """고객이 1회용 숫자를 봇에게 보낼 때까지 기다려 대화 번호를 얻는다.

    Args:
        bot: 봇.
        code: 터미널에 띄운 숫자.
        wait_s: 기다릴 초.
        clock: 단조 시계(시험에서 가짜).
        offset: 이 번호부터 받음(`skip_backlog`가 돌려준 값, None이면 처음부터).

    Returns:
        대화 번호, 시간이 지나면 None.
    """
    end = clock() + wait_s
    while (left := end - clock()) > 0:
        got = bot.updates(int(min(30, max(1, left))), offset)
        ids = [u["update_id"] for u in got if isinstance(u, dict) and isinstance(u.get("update_id"), int)]
        if ids:  # 본 업데이트를 확인해 두지 않으면 같은 것이 다시 와서 long poll이 바로 끝남
            offset = max(ids) + 1
        found = find_chat(got, code)
        if found is not None:
            return found
    return None

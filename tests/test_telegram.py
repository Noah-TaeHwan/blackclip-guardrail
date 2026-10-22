"""텔레그램 봇: 토큰 숨김 · 나누기 · 429 한 번 재시도 · 대화 잇기."""
import http.client
import urllib.request

import pytest
from conftest import TG_TOKEN, FakeTelegram, private_update

from blackclip.telegram import LIMIT, Bot, TelegramError, bind_chat, chunks, find_chat, skip_backlog, urllib_transport


def test_send_posts_plain_text_to_chat():
    """sendMessage에 chat_id · text만(평문), 주소는 /bot<토큰>/sendMessage."""
    t = FakeTelegram()
    assert Bot(TG_TOKEN, t).send(4242, "경고 · 실행 문제로 추정\n둘째 줄") == 1
    url, method, form = t.calls[0]
    assert url == f"https://api.telegram.org/bot{TG_TOKEN}/sendMessage" and method == "sendMessage"
    assert form == {"chat_id": "4242", "text": "경고 · 실행 문제로 추정\n둘째 줄"}


def test_chunks_split_on_lines_and_long_line():
    """4096자 넘는 글은 줄 단위로, 한 줄이 넘으면 그 줄을 자른다."""
    a, b = "가" * 3000, "나" * 3000
    assert chunks(f"{a}\n{b}") == [a, b]
    assert [len(c) for c in chunks("다" * (LIMIT + 10))] == [LIMIT, 10]
    assert chunks("한 줄\n\n둘") == ["한 줄\n\n둘"] and chunks("") == []


def test_send_long_text_waits_between_parts():
    """두 통이면 사이에 1초."""
    t, slept = FakeTelegram(), []
    n = Bot(TG_TOKEN, t, sleep=slept.append).send(1, ("가" * 3000) + "\n" + ("나" * 3000))
    assert n == 2 and slept == [1.0] and len(t.sent()) == 2


def test_flood_retry_once():
    """429 retry_after 3이면 3초 기다려 한 번 다시."""
    t, slept = FakeTelegram(flood=3), []
    Bot(TG_TOKEN, t, sleep=slept.append).send(1, "x")
    assert slept == [3] and len(t.sent()) == 2


def test_errors_never_contain_token():
    """실패 문구 · repr · 모양 오류에 토큰이 없다(설명에 주소가 섞여 와도 가림)."""
    t = FakeTelegram(fail="sendMessage")
    bot = Bot(TG_TOKEN, t)
    with pytest.raises(TelegramError) as exc:
        bot.send(1, "x")
    assert TG_TOKEN not in str(exc.value) and "<토큰>" in str(exc.value) and "HTTP 400" in str(exc.value)
    assert TG_TOKEN not in repr(bot) and repr(bot).endswith("Zq9x)")
    with pytest.raises(TelegramError) as bad:
        Bot("123:short")
    assert "123:short" not in str(bad.value)


def test_find_chat_needs_code_and_private():
    """숫자가 맞고 개인 대화일 때만."""
    ups = [private_update("hello", 1), private_update("482913", 2, "group"), private_update(" 482913 ", 3)]
    assert find_chat(ups, "482913") == 3
    assert find_chat([private_update("000000", 9)], "482913") is None
    assert find_chat([{"update_id": 5}], "1") is None


def test_skip_backlog_returns_next_offset():
    """숫자를 띄우기 전: offset -1 · 기다림 0으로 밀린 업데이트를 텔레그램 쪽에서 잊게 하고 그다음 번호를 돌려준다(맥 시계와 무관)."""
    t = FakeTelegram(backlog=[private_update("111111", 66, update_id=5), private_update("111111", 66, update_id=9)])
    assert skip_backlog(Bot(TG_TOKEN, t)) == 10
    assert [(f["offset"], f["timeout"]) for _, m, f in t.calls if m == "getUpdates"] == [("-1", "0")]
    assert skip_backlog(Bot(TG_TOKEN, FakeTelegram([]))) is None


def test_bind_ignores_backlog_before_start():
    """봇에 밀린 옛 메시지(같은 숫자)는 건너뛰고, 시작 뒤 온 숫자로만 잇는다(시작 번호 offset부터 받음)."""
    now = [0.0]

    def clock():
        now[0] += 10
        return now[0]

    t = FakeTelegram([private_update("111111", 77, update_id=10)], backlog=[private_update("111111", 66, update_id=9)])
    assert bind_chat(Bot(TG_TOKEN, t), "111111", 25, clock, offset=10) == 77
    assert [f.get("offset") for _, m, f in t.calls if m == "getUpdates"] == ["10"]


def test_bind_chat_polls_until_found_or_time_up():
    """찾으면 대화 번호, 시간이 지나면 None."""
    now = [0.0]

    def clock():
        now[0] += 10
        return now[0]

    assert bind_chat(Bot(TG_TOKEN, FakeTelegram([private_update("111111", 77)])), "111111", 900, clock) == 77
    t = FakeTelegram([])
    now[0] = 0.0
    assert bind_chat(Bot(TG_TOKEN, t), "111111", 25, clock) is None
    assert [f["timeout"] for _, m, f in t.calls if m == "getUpdates"] == ["15", "5"]


def test_bind_chat_confirms_seen_updates_with_offset():
    """본 업데이트는 offset으로 확인해 다음 long poll이 바로 끝나지 않음."""
    now = [0.0]

    def clock():
        now[0] += 10
        return now[0]

    t = FakeTelegram([private_update("hello", 77, update_id=7)])
    assert bind_chat(Bot(TG_TOKEN, t), "111111", 25, clock) is None
    assert [f.get("offset") for _, m, f in t.calls if m == "getUpdates"] == [None, "8"]


def test_long_description_with_token_is_masked_before_cut():
    """설명 앞이 길어 200자에서 토큰이 잘려도 토큰 조각이 남지 않음 · 대화 번호도 가림."""
    def answer(url, form):
        return 400, {"ok": False, "description": "x" * 180 + TG_TOKEN + f" chat {form.get('chat_id')}"}

    with pytest.raises(TelegramError) as exc:
        Bot(TG_TOKEN, answer).send(4242, "x")
    msg = str(exc.value)
    assert TG_TOKEN[:20] not in msg and "4242" not in msg


def test_incomplete_read_becomes_telegram_error(monkeypatch):
    """응답 본문을 읽다 끊기면(IncompleteRead) TelegramError로."""
    class Resp:
        status = 200

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

        def read(self):
            raise http.client.IncompleteRead(b"")

    monkeypatch.setattr(urllib.request, "urlopen", lambda req, timeout: Resp())
    with pytest.raises(TelegramError, match="IncompleteRead"):
        urllib_transport("https://api.telegram.org/botX/getMe", {})

"""blackclip connect 명령: `--open`은 대시보드 서버를 한 번용으로 · 포트가 쓰이면 두 길 안내 ·
`--telegram`(--open 없이)은 대화 잇기 · `--status`는 서버 없이.

실제 키체인 0: `cli.default_store`는 늘 `MemStore`(또는 하위 프로세스 안의 메모리 보관)로 바꾼다. 바이낸스 · 텔레그램은 가짜
(`maker(routes())` · `FakeTelegram`). 옛 연결 서버(지금은 없음) 시험은 대시보드 서버 시험으로 옮겼다.
CLI를 스레드로 돌리는 시험은 `cli.CONNECT_WAIT_S`를 줄여 잘못돼도 멈추지 않는다.
"""
import errno
import http.client
import json
import os
import queue
import re
import socket
import subprocess
import sys
import threading
import time
from urllib.parse import urlencode

import pytest
from conftest import CREDS, TG_TOKEN, FakeTelegram, MemStore, maker, private_update, routes
from webrules import copy_violations

import blackclip.cli as cli
import blackclip.notify as notify_mod
from blackclip import dashboard
from blackclip.binance.client import SHAPE_TEXT, BinanceError
from blackclip.binance.permissions import Permission
from blackclip.cli import main
from blackclip.connect import Saved
from blackclip.keystore import Credentials
from blackclip.notify import TELEGRAM_ACCOUNT
from blackclip.telegram import Bot, TelegramError
from blackclip.workspace import ensure

WAIT = 10
"""시험 안 기다림의 상한(초)."""

KEY, SECRET = CREDS.api_key, CREDS.secret_key
FORM_HDR = {"Content-Type": "application/x-www-form-urlencoded"}
FORMS = {"key": {"api_key": KEY, "secret_key": SECRET}, "telegram": {"bot_token": TG_TOKEN}, "choose": {"sensitivity": "둔감", "quiet": "매일"}}

BUSY_CONNECT = ("blackclip connect: {p} 포트가 이미 쓰이고 있습니다. 같은 레포에서 띄운 대시보드면 그 화면의 「연결」에서 넣습니다(main 계정). "
                "다른 계정 · 테스트넷이거나 다른 프로그램이면 --port <다른 번호>를 붙여 다시 실행합니다.")
"""포트가 쓰일 때 `blackclip connect` 안내 한 줄(두 길)."""


def req(port, method, path, body=None, headers=None):
    """요청 하나 → (상태, 머리글, 본문)."""
    c = http.client.HTTPConnection("127.0.0.1", port, timeout=WAIT)
    c.request(method, path, body=body, headers=headers or {})
    r = c.getresponse()
    out = r.status, dict(r.getheaders()), r.read().decode()
    c.close()
    return out


def free_port() -> int:
    """비어 있는 포트 하나(`--port`는 0을 받지 않음)."""
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def closed(port) -> bool:
    """그 포트에 아무도 듣지 않으면 True."""
    try:
        socket.create_connection(("127.0.0.1", port), timeout=1).close()
    except OSError:
        return True
    return False


@pytest.fixture
def ws(tmp_path, monkeypatch):
    """Git 레포 꼭대기로 옮기고 키 보관을 메모리로 · 바이낸스를 가짜로 · 한 번용 대기를 30초로 → MemStore."""
    (tmp_path / ".git").mkdir()
    monkeypatch.chdir(tmp_path)
    store = MemStore()
    monkeypatch.setattr(cli, "default_store", lambda: store)
    monkeypatch.setattr(cli, "_make_client", maker(routes()))
    monkeypatch.setattr(cli, "CONNECT_WAIT_S", 30)
    return store


class CliThread:
    """`main(argv)`를 스레드로 돌리고 capsys 출력을 모은다.

    Args:
        capsys: pytest capsys.
        argv: 명령 인자.
    """

    def __init__(self, capsys, argv):
        self.capsys, self.out, self.err, self.got = capsys, "", "", {}
        self.thread = threading.Thread(target=lambda: self.got.setdefault("code", main(argv)), daemon=True)
        self.thread.start()

    def read(self) -> str:
        """지금까지 나온 표준 출력."""
        o, e = self.capsys.readouterr()
        self.out, self.err = self.out + o, self.err + e
        return self.out

    def address(self):
        """안내 줄 셋(주소 · ssh · 닫힘)이 나올 때까지 기다린다 → (포트, 접속 토큰, kind)."""
        end = time.monotonic() + WAIT
        while time.monotonic() < end and self.thread.is_alive():
            m = re.search(r"연결 화면: http://127\.0\.0\.1:(\d+)/connect\?t=([A-Za-z0-9_-]{43,})&kind=(\w+)\n(.+)\n(.+)\n", self.read())
            if m:
                p = int(m.group(1))
                assert m.group(4) == f"화면 없는 서버면 노트북에서 ssh -L {p}:127.0.0.1:{p} <서버> 뒤 위 주소를 여세요."
                assert m.group(5) == f"{int(cli.CONNECT_WAIT_S // 60)}분 뒤 닫힙니다."
                return p, m.group(2), m.group(3)
            time.sleep(0.02)
        raise AssertionError(self.out + self.err)

    def join(self) -> int:
        """끝날 때까지 기다린다 → 종료 코드."""
        self.thread.join(WAIT)
        assert not self.thread.is_alive()
        self.read()
        return self.got["code"]


def open_page(port, token):
    """한 번용 연결 화면 GET(글 규칙 위반 0) → (본문, CSRF)."""
    status, _, body = req(port, "GET", f"/connect?t={token}")
    assert status == 200 and copy_violations(body) == [], body
    return body, re.search(r'name="csrf" value="([^"]+)"', body).group(1)


def save(port, token, kind, fields=None):
    """한 번용 화면에서 그 패널 폼을 GET → POST 한 번 → (상태, 본문)."""
    _, csrf = open_page(port, token)
    status, _, body = req(port, "POST", f"/connect/{kind}?t={token}", urlencode({"csrf": csrf, **(fields or FORMS[kind])}), FORM_HDR)
    return status, body


def test_connect_open_uses_dashboard_server_and_ends_after_save(ws, tmp_path, capsys):
    """`connect --open`: 대시보드 서버 한 번용(연결 화면 하나 · 다른 GET 404 · 다른 패널 POST 403) → 저장 → 「연결됨」 줄 · 종료 0 · 포트 닫힘.
    권한은 서버가 연결 상태에 적는다(CLI가 따로 적지 않음)."""
    run = CliThread(capsys, ["connect", "--open", "--no-browser", "--port", str(free_port())])
    p, t, kind = run.address()
    assert kind == "key" and req(p, "GET", f"/?t={t}")[0] == 404
    body, _ = open_page(p, t)
    assert "binance main 연결" in body and 'name="api_key"' in body and 'name="bot_token"' not in body
    assert save(p, t, "telegram")[0] == 403 and ws.items == {}
    status, body = save(p, t, "key")
    assert status == 200 and '끝 4자리 <span class="id">WXYZ</span>' in body and KEY not in body
    assert run.join() == 0 and closed(p)
    assert run.out.endswith("연결됨 · binance main · 권한 조회만(조회만 켜져 있음 · IP 제한 없음) · 이제 blackclip fetch --symbols BTCUSDT처럼\n")
    assert ws.items == {"binance:main": CREDS} and run.err == ""
    assert not [x for x in (KEY, SECRET) if x in run.out + run.err]  # 터미널에도 키 · 시크릿 글자 0
    perm = json.loads((tmp_path / ".blackclip" / "connections.json").read_text())["binance"]["main"]["permission"]
    assert perm["status"] == "read_only"


def test_connect_open_testnet_account_saves_under_that_name(ws, capsys):
    """`--account sub1 --testnet`: 계정은 서버가 정한다(폼에 계정 칸 없음) → `binance:sub1-testnet`, 권한 확인 불가 줄."""
    run = CliThread(capsys, ["connect", "--open", "--no-browser", "--account", "sub1", "--testnet", "--port", str(free_port())])
    p, t, _ = run.address()
    assert "binance sub1 테스트넷 연결" in open_page(p, t)[0]
    assert save(p, t, "key")[0] == 200 and run.join() == 0
    assert list(ws.items) == ["binance:sub1-testnet"] and "연결됨 · binance sub1-testnet · 권한 확인 불가" in run.out


CHILD = r'''
import sys

import blackclip.cli as cli
from blackclip.keystore import Credentials
from blackclip.notify import TELEGRAM_ACCOUNT
from blackclip.telegram import Bot


class Store:
    """메모리 키 보관(하위 프로세스 안, 실제 키체인 0)."""

    name = "메모리"

    def __init__(self, items):
        self.items = dict(items)

    def save(self, account, creds):
        self.items[account] = creds

    def load(self, account):
        return self.items.get(account)


class Client:
    """늘 조회만 켠 키라고 답하는 가짜 바이낸스."""

    testnet = False

    def get(self, path, params=None, signed=True):
        return {"enableReading": True, "ipRestrict": True}


calls = []


def transport(url, form):
    """가짜 텔레그램: getUpdates는 부모가 표준 입력으로 보낸 숫자를 받은 글로 돌려준다(부모가 숫자 줄을 먼저 읽었다는 뜻)."""
    method = url.rsplit("/", 1)[1]
    calls.append(method)
    if method == "getMe":
        return 200, {"ok": True, "result": {"username": "bc_test_bot"}}
    if method == "getUpdates":
        if form.get("offset") == "-1":  # 숫자를 띄우기 전 밀린 업데이트 비우기: 아무것도 없음
            return 200, {"ok": True, "result": []}
        text = sys.stdin.readline().strip()
        chat = {"id": 4242, "type": "private"}
        return 200, {"ok": True, "result": [{"update_id": 1, "message": {"message_id": 1, "text": text, "chat": chat}}]}
    return 200, {"ok": True, "result": {"message_id": 1}}


if sys.argv[1] == "telegram":
    cli.default_store = lambda: Store({TELEGRAM_ACCOUNT: Credentials(sys.argv[2], "")})
    cli._telegram_bot = lambda token: Bot(token, transport)
    argv = ["connect", "--telegram"]
else:
    cli.default_store = lambda: Store({})
    cli._make_client = lambda market, testnet, creds: Client()
    argv = ["connect", "--open", "--no-browser", "--port", sys.argv[2]]
code = cli.main(argv)
print("CALLS", calls, file=sys.stderr)
sys.exit(code)
'''
"""하위 프로세스 본문: `cli`의 보관 · 봇 · 바이낸스를 그 프로세스 안에서 바꿔 끼우고 명령 하나를 돌린다(환경 변수 · sys.modules 바꿔치기 없음)."""


def child(tmp_path, *args):
    """CHILD를 파이프 stdout으로 띄운다 → (프로세스, 줄 큐, 가짜 security 로그).

    PYTHONUNBUFFERED는 빼서 줄마다 flush한 줄만 바로 나가게 한다. PATH 맨 앞의 가짜 `security`는 불리면 로그에 한 줄을 쓴다.
    """
    fakebin = tmp_path / "fakebin"
    fakebin.mkdir()
    log = tmp_path / "security.log"
    (fakebin / "security").write_text(f'#!/bin/sh\necho called >> "{log}"\nexit 1\n')
    (fakebin / "security").chmod(0o755)
    env = {k: v for k, v in os.environ.items() if k != "PYTHONUNBUFFERED"}
    env |= {"PATH": f"{fakebin}{os.pathsep}{env.get('PATH', '')}", "HOME": str(tmp_path / "home"),
            "PYTHONPATH": os.path.join(os.path.dirname(__file__), "..", "src")}
    p = subprocess.Popen([sys.executable, "-c", CHILD, *args], cwd=tmp_path, env=env, stdin=subprocess.PIPE,
                         stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
    lines = queue.Queue()
    threading.Thread(target=lambda: [lines.put(x) for x in p.stdout], daemon=True).start()
    return p, lines, log


def finish(p):
    """하위 프로세스를 끝까지 기다린다 → (종료 코드, 표준 오류). stdout은 줄 큐 스레드만 읽는다(communicate가 같이 읽으면 줄을 빼앗음)."""
    p.wait(timeout=WAIT)
    return p.returncode, p.stderr.read()


def test_connect_open_output_flushes_on_pipe(tmp_path):
    """파이프 stdout(`--no-browser`로 띄운 화면 없는 서버 · AI Bash)에서 주소 줄이 저장 전에 읽힌다. 읽은 주소로 저장하면 끝남."""
    (tmp_path / ".git").mkdir()
    port = free_port()
    p, lines, log = child(tmp_path, "open", str(port))
    try:
        first = lines.get(timeout=WAIT)
        assert p.poll() is None  # 아직 저장을 기다리는 중: 줄은 끝날 때 쏟아진 것이 아니라 바로 나왔다
        t = re.fullmatch(rf"연결 화면: http://127\.0\.0\.1:{port}/connect\?t=([A-Za-z0-9_-]+)&kind=key\n", first).group(1)
        assert save(port, t, "key")[0] == 200
        code, err = finish(p)
    finally:
        p.kill()
    rest = [lines.get(timeout=WAIT) for _ in range(3)]
    assert code == 0 and rest[-1].startswith("연결됨 · binance main · 권한 조회만"), (rest, err)
    assert not (log.exists() and log.read_text())


def test_link_telegram_code_line_before_wait(tmp_path):
    """`connect --telegram`을 하위 프로세스로(stdout 파이프) → 숫자 줄이 프로세스가 끝나기 전에 읽힌다.
    부모가 그 줄의 숫자를 표준 입력으로 넘겨야 가짜 봇이 그 숫자를 받은 글로 돌려줘, 줄이 늦으면 잇기가 끝나지 않는다."""
    (tmp_path / ".git").mkdir()
    p, lines, log = child(tmp_path, "telegram", TG_TOKEN)
    try:
        first = lines.get(timeout=WAIT)
        assert p.poll() is None
        code = re.fullmatch(r"텔레그램에서 @bc_test_bot에게 이 숫자를 보내 주세요: (\d{6}) \(15분 안\)\n", first).group(1)
        p.stdin.write(code + "\n")
        p.stdin.flush()
        rc, err = finish(p)
    finally:
        p.kill()
    assert rc == 0 and lines.get(timeout=WAIT) == "연결됨 · 텔레그램 @bc_test_bot · 확인 글을 보냈어요\n", err
    assert "CALLS ['getMe', 'getUpdates', 'getUpdates', 'sendMessage']" in err  # 밀린 것 비우기 · 숫자 받기
    t = json.loads((tmp_path / ".blackclip" / "connections.json").read_text())["telegram"]
    assert (t["chat_id"], t["bot"]) == (4242, "@bc_test_bot") and not (log.exists() and log.read_text())


@pytest.mark.parametrize("argv, line", [([], "시간이 지나 연결 화면을 닫았어요(저장 안 함)\n"),
                                        (["--telegram"], "시간이 지나 연결 화면을 닫았어요(저장 안 함)\n"),
                                        (["--choose"], "시간이 지나 고르는 화면을 닫았어요(저장 안 함)\n")])
def test_connect_open_timeout_exits_1(ws, capsys, monkeypatch, argv, line):
    """저장 없이 기다림이 지나면 그 줄 · 종료 1 · 포트 닫힘 · 키 보관 그대로."""
    monkeypatch.setattr(cli, "CONNECT_WAIT_S", 0.5)
    port = free_port()
    assert main(["connect", "--open", "--no-browser", "--port", str(port), *argv]) == 1
    out = capsys.readouterr().out
    assert out.startswith(f"연결 화면: http://127.0.0.1:{port}/connect?t=") and out.endswith(line) and closed(port) and ws.items == {}


@pytest.mark.parametrize("text", ["{", '{"binance": []}', '{"binance": 5}', '{"binance": null}', '{"binance": {"main": []}}'])
def test_connect_open_state_error_exits_2(ws, tmp_path, capsys, text):
    """키를 저장한 뒤 연결 상태를 적지 못하면(깨진 JSON · binance 칸이 객체 아님) 화면은 500 「키는 저장했어요」, CLI는 한 줄 · 종료 2 ·
    파일 그대로(옛 test_cli_open_state_write_failure_one_line · test_cli_open_non_object_binance_one_line의 CLI 쪽 뜻)."""
    ensure(tmp_path)
    (tmp_path / ".blackclip" / "connections.json").write_text(text, encoding="utf-8")
    run = CliThread(capsys, ["connect", "--open", "--no-browser", "--port", str(free_port())])
    p, t, _ = run.address()
    status, body = save(p, t, "key")
    assert status == 500 and "키는 저장했어요" in body and run.join() == 2 and closed(p)
    assert run.err.startswith("blackclip connect: 키는 저장했지만 연결 상태를 적지 못했습니다(") and "Traceback" not in run.err
    assert text == "{" or "칸이 객체가 아니라" in run.err
    assert (tmp_path / ".blackclip" / "connections.json").read_text(encoding="utf-8") == text and ws.items == {"binance:main": CREDS}


@pytest.mark.parametrize("kind, argv", [("key", []), ("telegram", ["--telegram"]), ("choose", ["--choose"])])
def test_connect_open_unsure_save_exits_2(ws, capsys, monkeypatch, kind, argv):
    """보관 · 연결 상태 쓰기를 시작한 뒤 예외(저장했는지 모름, `Saved.permission` None): 한 줄 · 종료 2(성공 줄 · 다음 걸음 없음)."""
    got = []
    monkeypatch.setattr(dashboard, "serve", lambda *a, **k: got.append(k["once"].kind) or Saved(kind, None, None, None, "보관 오류"))
    assert main(["connect", "--open", "--no-browser", *argv]) == 2 and got == [kind]
    out, err = capsys.readouterr()
    assert out == "" and err == "blackclip connect: 저장했는지 확인하지 못했습니다(보관 오류) · blackclip connect --status로 확인\n"


def _busy(holder, port, root):
    """그 포트를 먼저 잡는다(대시보드 서버 · 일반 소켓) → 놓는 함수."""
    if holder == "dashboard":
        s = dashboard.make_server(root, port)
        threading.Thread(target=s.serve_forever, daemon=True).start()
        return lambda: (s.shutdown(), s.server_close())
    s = socket.socket()
    s.bind(("127.0.0.1", port))
    s.listen()
    return s.close


@pytest.mark.parametrize("holder", ["dashboard", "socket"])
def test_connect_open_when_port_in_use_exits_2_with_guidance(ws, tmp_path, capsys, holder):
    """포트를 대시보드 서버가 먼저 잡았든 다른 프로그램이 잡았든 같은 안내(두 길) 한 줄 · 종료 2 · 키 저장 0. `--account sub1` · 텔레그램 ·
    고르기도 같은 글(판별 프로브 없음)."""
    port = free_port()
    release = _busy(holder, port, ensure(tmp_path))
    try:
        for extra in ([], ["--account", "sub1"], ["--account", "sub1", "--testnet"], ["--telegram"], ["--choose"]):
            assert main(["connect", "--open", "--no-browser", "--port", str(port), *extra]) == 2, extra
            out, err = capsys.readouterr()
            assert out == "" and err == BUSY_CONNECT.format(p=port) + "\n", extra
    finally:
        release()
    assert ws.items == {}


def test_connect_open_other_port_error_keeps_old_line(ws, capsys, monkeypatch):
    """EADDRINUSE가 아닌 OSError(권한 등)는 지금 글 그대로."""
    def refuse(*a, **k):
        raise OSError(errno.EACCES, "Permission denied")
    monkeypatch.setattr(dashboard, "make_server", refuse)
    assert main(["connect", "--open", "--no-browser", "--port", "8800"]) == 2
    assert capsys.readouterr().err == "blackclip connect: 8800 포트로 화면을 열지 못했습니다(Permission denied) · --port로 다른 번호\n"


def test_connect_open_telegram_ends_after_save(ws, tmp_path, capsys, monkeypatch):
    """`--open --telegram`은 토큰 저장까지 → 결과 줄 + 다음 걸음 줄 · 종료 0 · 포트 닫힘. 대화 잇기(bind_chat)를 하지 않아
    가짜 봇 호출은 확인(getMe)뿐이고 연결 상태에 대화 칸이 없다."""
    fake = FakeTelegram([private_update("000000")])
    monkeypatch.setattr(cli, "_telegram_bot", lambda token: Bot(token, fake))
    run = CliThread(capsys, ["connect", "--open", "--telegram", "--no-browser", "--port", str(free_port())])
    p, t, kind = run.address()
    body, _ = open_page(p, t)
    assert kind == "telegram" and "텔레그램 봇 연결" in body and 'name="bot_token"' in body and 'name="api_key"' not in body
    status, body = save(p, t, "telegram")
    assert status == 200 and '끝 4자리 <span class="id">Zq9x</span>' in body and TG_TOKEN not in body
    assert run.join() == 0 and closed(p) and run.err == ""
    assert run.out.endswith("토큰 저장됨 · 텔레그램 봇 @bc_test_bot\n대화 잇기: blackclip connect --telegram\n")
    assert [m for _, m, _ in fake.calls] == ["getMe"] and ws.items == {TELEGRAM_ACCOUNT: Credentials(TG_TOKEN, "")}
    assert TG_TOKEN not in run.out + run.err and TG_TOKEN.split(":")[1] not in run.out + run.err  # 터미널에 토큰 글자 0
    assert "chat_id" not in json.loads((tmp_path / ".blackclip" / "connections.json").read_text()).get("telegram", {})


def test_connect_open_choose_ends_after_save(ws, tmp_path, capsys):
    """`--open --choose`: 고르기 패널 하나 → 저장 → 「골랐어요」 + 알림 상태 줄(blackclip notify와 같은 줄) · 종료 0
    (옛 test_cli_connect_choose의 저장 쪽)."""
    run = CliThread(capsys, ["connect", "--open", "--choose", "--no-browser", "--port", str(free_port())])
    p, t, kind = run.address()
    body, _ = open_page(p, t)
    assert kind == "choose" and "알림 고르기" in body and 'name="sensitivity"' in body and 'name="api_key"' not in body
    assert save(p, t, "choose")[0] == 200 and run.join() == 0 and closed(p)
    head, *rest = run.out.split("골랐어요\n")[1].splitlines()
    assert head.startswith("텔레그램: 대화 연결 전")
    assert "기본 민감도: 둔감 · 1년 내내 보면 헛경보 약 5%(한 달 약 0.43%) · 미검증 · blackclip save가 --sensitivity 없을 때" in rest
    assert json.loads((tmp_path / ".blackclip" / "connections.json").read_text())["defaults"] == {"sensitivity": "둔감"}


def test_connect_telegram_without_open_links_saved_token(ws, tmp_path, capsys, monkeypatch):
    """`connect --telegram`(--open 없이)은 저장된 토큰으로 대화만 잇는다(서버 없음): 숫자 줄 → 연결됨 · 종료 0."""
    ws.items[TELEGRAM_ACCOUNT] = Credentials(TG_TOKEN, "")
    fake = FakeTelegram([private_update("482913")])
    monkeypatch.setattr(cli, "_telegram_bot", lambda token: Bot(token, fake))
    monkeypatch.setattr(notify_mod.secrets, "randbelow", lambda n: 482913)
    monkeypatch.setattr(dashboard, "make_server", lambda *a, **k: pytest.fail("서버를 띄우면 안 됨"))
    assert main(["connect", "--telegram"]) == 0
    assert capsys.readouterr().out == ("텔레그램에서 @bc_test_bot에게 이 숫자를 보내 주세요: 482913 (15분 안)\n"
                                       "연결됨 · 텔레그램 @bc_test_bot · 확인 글을 보냈어요\n")
    assert json.loads((tmp_path / ".blackclip" / "connections.json").read_text())["telegram"]["chat_id"] == 4242


def test_connect_telegram_without_token_exits_2(ws, capsys, monkeypatch):
    """토큰이 없으면 link_telegram의 지금 한 줄 · 종료 2(봇을 만들지 않음)."""
    monkeypatch.setattr(cli, "_telegram_bot", lambda token: pytest.fail("봇을 만들면 안 됨"))
    assert main(["connect", "--telegram"]) == 2
    assert capsys.readouterr().out == "텔레그램 토큰이 없습니다 · blackclip connect --open --telegram\n"


def test_connect_no_mode_exits_2(ws, tmp_path, capsys):
    """`--open` · `--status` · `--telegram`이 다 없으면 한 줄 · 종료 2(작업 폴더를 만들지 않음)."""
    for argv in (["connect"], ["connect", "--account", "sub1"], ["connect", "--no-browser", "--port", "9000"]):
        assert main(argv) == 2, argv
        assert capsys.readouterr().err == "blackclip connect: --open(화면) · --status(상태) · --telegram(대화 잇기) 가운데 하나를 함께 씁니다\n"
    assert not (tmp_path / ".blackclip").exists()


def test_connect_choose_without_open_exits_2(ws, capsys):
    """`--choose`는 `--open`과 함께, `--telegram` 없이(지금 글 그대로, 옛 test_cli_connect_choose의 거절 쪽)."""
    for argv in (["connect", "--choose"], ["connect", "--status", "--choose"], ["connect", "--open", "--choose", "--telegram"],
                 ["connect", "--telegram", "--choose"]):
        assert main(argv) == 2, argv
        assert capsys.readouterr().err == "blackclip connect: --choose는 --open과 함께, --telegram 없이 씁니다(지금 값은 blackclip notify)\n"


def test_connect_status_with_telegram_is_status(ws, capsys, monkeypatch):
    """`--status`가 먼저: `--status --telegram`은 상태 줄만(잇기 · 봇 0)."""
    monkeypatch.setattr(cli, "_telegram_bot", lambda token: pytest.fail("잇기를 하면 안 됨"))
    ws.items[TELEGRAM_ACCOUNT] = Credentials(TG_TOKEN, "")
    assert main(["connect", "--status", "--telegram"]) == 0
    assert capsys.readouterr().out == "연결된 계정 없음 · blackclip connect --open\n"


def test_connect_status_needs_no_server(ws, capsys, monkeypatch):
    """`--status`는 서버 없이 지금 그대로."""
    monkeypatch.setattr(dashboard, "make_server", lambda *a, **k: pytest.fail("서버를 띄우면 안 됨"))
    monkeypatch.setattr(dashboard, "serve", lambda *a, **k: pytest.fail("서버를 띄우면 안 됨"))
    assert main(["connect", "--status"]) == 0 and capsys.readouterr().out == "연결된 계정 없음 · blackclip connect --open\n"


def test_connect_creates_workspace_dashboard_does_not(ws, tmp_path, monkeypatch):
    """`ensure()` 자리: connect는 CLI에서 작업 폴더와 .gitignore를 만들고, dashboard는 만들지 않는다(find_root만)."""
    monkeypatch.setattr(cli, "serve_dashboard", lambda *a, **k: pytest.fail("서버를 띄우면 안 됨"))
    assert main(["dashboard", "--no-browser"]) == 2 and not (tmp_path / ".blackclip").exists()
    monkeypatch.setattr(dashboard, "serve", lambda *a, **k: None)
    assert main(["connect", "--open", "--no-browser"]) == 1
    assert (tmp_path / ".blackclip" / ".gitignore").is_file()


def test_cli_import_does_not_load_dashboard(tmp_path):
    """훅 경로(`import blackclip.cli`)는 대시보드 · pandas · numpy를 부르지 않는다(dashboard는 명령 안에서 늦게 import)."""
    src = os.path.join(os.path.dirname(__file__), "..", "src")
    code = "import sys, blackclip.cli; print([m for m in ('blackclip.dashboard', 'pandas', 'numpy') if m in sys.modules])"
    r = subprocess.run([sys.executable, "-c", code], cwd=tmp_path, capture_output=True, text=True, check=True,
                       env={**os.environ, "PYTHONPATH": src, "HOME": str(tmp_path / "home")})
    assert r.stdout.strip() == "[]"


def test_key_checker_turns_odd_response_into_502(ws, tmp_path, monkeypatch):
    """남은 위험: 바이낸스가 200에 객체 아닌 JSON(목록 · 숫자 · 글)을 주면 `permissions.check`가 AttributeError를 낸다. CLI가 넘기는
    checker가 이를 ValueError로 감싸 대시보드가 502 · 쓰기 0으로 답한다(감싸기 전에는 500 「화면을 그리지 못했어요」)."""
    for odd in ([], 5, "x"):
        monkeypatch.setattr(cli, "_make_client", maker(lambda path, q, odd=odd: odd))
        with pytest.raises(BinanceError, match=SHAPE_TEXT):  # 엔진이 모양을 먼저 막는다
            cli._connect_setup(ws, "main", False, "연결").check_key(CREDS)
    conn = cli._connect_setup(ws, "main", False, "연결")
    server = dashboard.make_server(ensure(tmp_path), 0, connect=conn)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    try:
        p, t = server.server_address[1], server.token
        status, _, body = req(p, "POST", f"/connect/key?t={t}", urlencode({"csrf": conn.csrf, **FORMS["key"]}), FORM_HDR)
        assert status == 502 and dashboard.CHECK_FAIL_TEXT in body and "잠시 뒤 다시 넣어 주세요." in body and "응답 모양이 다릅니다" not in body and ws.items == {}
    finally:
        server.shutdown()
        server.server_close()


def test_telegram_checker_returns_bot_name(monkeypatch):
    """CLI가 넘기는 텔레그램 checker는 getMe만 부르고 「봇 @이름」을 돌려준다(옛 test_telegram_form_saves_token_and_shows_last4_only의 checker 쪽)."""
    fake = FakeTelegram()
    monkeypatch.setattr(cli, "_telegram_bot", lambda token: Bot(token, fake))
    check = cli._connect_setup(MemStore(), "main", False, "연결").check_telegram
    assert check(Credentials(TG_TOKEN, "")) == Permission("read_only", "봇 @bc_test_bot")
    assert [m for _, m, _ in fake.calls] == ["getMe"]


def gated_client():
    """권한 조회에서 시험이 풀 때까지 멈추는 가짜 바이낸스 → (들어옴, 풀기, 공장, 부른 수)."""
    entered, go, calls = threading.Event(), threading.Event(), []
    inner = routes()

    def answer(path, q):
        calls.append(path)
        entered.set()
        assert go.wait(WAIT)
        return inner(path, q)
    return entered, go, maker(answer), calls


class WatchedLock:
    """서버 POST Lock을 감싸 n번째로 Lock 앞에 온 쪽을 알린다(시간 대신 신호로 「serve가 Lock을 기다리는 중」을 확인)."""

    def __init__(self):
        self.lock, self.second, self.n = threading.Lock(), threading.Event(), 0

    def __enter__(self):
        self.n += 1
        if self.n == 2:
            self.second.set()
        self.lock.acquire()
        return self

    def __exit__(self, *exc):
        self.lock.release()


def capture(monkeypatch):
    """`dashboard.make_server` · `dashboard.Once`를 감싸 serve 안에서 만든 서버 · 한 번용 상태를 모은다 → (서버 목록, Once 목록)."""
    servers, onces = [], []
    real_make, real_once = dashboard.make_server, dashboard.Once
    monkeypatch.setattr(dashboard, "make_server", lambda *a, **k: servers.append(real_make(*a, **k)) or servers[-1])
    monkeypatch.setattr(dashboard, "Once", lambda *a, **k: onces.append(real_once(*a, **k)) or onces[-1])
    return servers, onces


def test_connect_open_timeout_waits_for_save_in_progress(ws, capsys, monkeypatch):
    """기다림이 끝난 순간 checker 안에 있던 저장(서버 POST Lock 안)이 끝날 때까지 serve가 Lock 앞에서 기다려 그 결과로
    끝난다(저장 출력 · 0). 요청 스레드는 daemon이라 server_close가 기다려 주지 않는다. 기다림 끝은 시간 대신 `once.event`를 켜서
    만든다(serve의 `event.wait(wait_s)`는 시간이 지나든 켜지든 같은 길로 나옴, 느린 컴퓨터에서 흔들리지 않게)."""
    entered, go, make, calls = gated_client()
    monkeypatch.setattr(cli, "_make_client", make)
    servers, onces = capture(monkeypatch)
    run = CliThread(capsys, ["connect", "--open", "--no-browser", "--port", str(free_port())])
    p, t, _ = run.address()
    servers[0].post_lock = watched = WatchedLock()
    _, csrf = open_page(p, t)
    got = {}
    a = threading.Thread(target=lambda: got.setdefault("A", req(p, "POST", f"/connect/key?t={t}",
                                                                   urlencode({"csrf": csrf, **FORMS["key"]}), FORM_HDR)[0]))
    a.start()
    try:
        assert entered.wait(WAIT)  # A는 Lock 안 checker에서 멈춤
        onces[0].event.set()  # 기다림이 끝남(시간 지남과 같은 길)
        assert watched.second.wait(WAIT)  # serve가 Lock 앞에 왔다: 고치기 전에는 오지 않고 「시간이 지나」 · 1로 끝났다
    finally:
        go.set()
    a.join(WAIT)
    assert not a.is_alive() and got == {"A": 200}
    assert run.join() == 0 and run.out.endswith("이제 blackclip fetch --symbols BTCUSDT처럼\n") and closed(p)
    assert calls == ["/sapi/v1/account/apiRestrictions"] and ws.items == {"binance:main": CREDS}


def test_post_after_serve_closed_without_save_is_403_closed_text(tmp_path, monkeypatch):
    """저장 없이 serve가 한 번용을 닫은 뒤 Lock을 얻은 POST는 403 「시간이 지나 이 창을 닫았어요 …」 · 쓰기 0
    (「이미 저장했어요」가 아님). 닫기 전에 받아 둔 연결(본문을 아직 안 보냄)로 만든다: 머리글만 보내 처리기가 본문을 기다리는 동안
    serve를 끝내고, 그 뒤 본문을 보낸다."""
    root = ensure(tmp_path)
    store = MemStore()
    servers, _ = capture(monkeypatch)
    conn = cli._connect_setup(store, "main", False, "binance main 연결")
    once, lines, up, got = dashboard.Once("key", threading.Event()), [], threading.Event(), {}
    out = lambda line: (lines.append(line), len(lines) == 3 and up.set())  # 세 줄(주소 · ssh · 닫힘 시간)을 다 내면 서버가 뜬 것
    th = threading.Thread(target=lambda: got.setdefault("r", dashboard.serve(root, 0, False, out, connect=conn, once=once)))
    th.start()
    assert up.wait(WAIT)
    server, posting = servers[0], threading.Event()
    cls, orig = server.RequestHandlerClass, server.RequestHandlerClass.do_POST
    cls.do_POST = lambda self: (posting.set(), orig(self))
    p, t = server.server_address[1], server.token
    body = urlencode({"csrf": conn.csrf, **FORMS["key"]}).encode()
    with socket.create_connection(("127.0.0.1", p), timeout=WAIT) as sock:
        sock.sendall(f"POST /connect/key?t={t} HTTP/1.1\r\nHost: 127.0.0.1:{p}\r\nContent-Type: application/x-www-form-urlencoded\r\n"
                     f"Content-Length: {len(body)}\r\n\r\n".encode())
        assert posting.wait(WAIT)  # 처리기가 받아서 본문을 기다리는 중
        once.event.set()  # 저장 없이 기다림이 끝남
        th.join(WAIT)
        assert not th.is_alive() and got == {"r": None} and once.done and once.result is None
        sock.sendall(body)
        reply = b""
        while chunk := sock.recv(65536):
            reply += chunk
    head, _, page = reply.decode().partition("\r\n\r\n")
    assert head.split()[1] == "403" and dashboard.ONCE_DONE_TEXT not in page  # 저장 안 했는데 「이미 저장했어요」면 거짓
    assert dashboard.ONCE_CLOSED_TEXT in page
    assert copy_violations(dashboard.ONCE_CLOSED_TEXT) == [] and store.items == {} and not (root / "connections.json").exists()


def test_serve_once_default_wait_is_15_minutes(tmp_path):
    """한 번용인데 wait_s를 주지 않아도 TypeError 없이 15분 기본(안내 줄 「15분 뒤 닫힙니다.」)."""
    root = ensure(tmp_path)
    lines, once, got = [], dashboard.Once("choose", threading.Event()), {}
    conn = cli._connect_setup(MemStore(), "main", False, "알림 고르기")
    th = threading.Thread(target=lambda: got.setdefault("r", dashboard.serve(root, 0, False, lines.append, connect=conn, once=once)))
    th.start()
    end = time.monotonic() + WAIT
    while len(lines) < 3 and time.monotonic() < end:
        time.sleep(0.02)
    once.event.set()
    th.join(WAIT)
    assert not th.is_alive() and lines[2] == "15분 뒤 닫힙니다." and got == {"r": None}


def test_connect_open_error_after_open_is_not_port_line(ws, capsys, monkeypatch):
    """포트를 연 뒤의 OSError(파이프가 닫혀 안내 줄을 못 씀 · BrokenPipe)는 「포트로 화면을 열지 못했습니다」가 아닌 한 줄 · 2.
    포트는 닫힌다. 여는 데 실패한 OSError는 지금 포트 글(test_connect_open_other_port_error_keeps_old_line)."""
    port = free_port()
    real = dashboard.serve

    def broken(root, port, open_browser, out, **k):
        def out2(line):
            out(line)
            raise BrokenPipeError(errno.EPIPE, "Broken pipe")
        return real(root, port, open_browser, out2, **k)
    monkeypatch.setattr(dashboard, "serve", broken)
    assert main(["connect", "--open", "--no-browser", "--port", str(port)]) == 2
    out, err = capsys.readouterr()
    assert err == "blackclip connect: 연결 화면을 띄운 뒤 멈췄습니다(Broken pipe)\n" and closed(port) and ws.items == {}


class _Transport:
    """바이낸스 transport 흉내: 권한 조회는 -1021(시각 어긋남) 한 번, 시각 조회는 주어진 본문."""

    def __init__(self, time_body):
        self.time_body, self.first = time_body, True

    def __call__(self, url, headers):
        if "/api/v3/time" in url:
            return 200, {}, self.time_body
        if self.first:
            self.first = False
            return 400, {}, b'{"code": -1021, "msg": "Timestamp for this request is outside of the recvWindow."}'
        return 200, {}, b'{"enableReading": true, "ipRestrict": true}'


@pytest.mark.parametrize("time_body, kind", [(b"{}", "KeyError"), (b"[]", "TypeError"), (b'{"serverTime": 1e999}', "OverflowError")])
def test_key_checker_shape_errors_are_502_with_reason(ws, tmp_path, monkeypatch, time_body, kind):
    """시각 맞춤(`Client._sync_time`)의 KeyError · TypeError · OverflowError(`int(inf)`)도 쓰기 전 502 · 쓰기 0이고,
    checker는 사유 글(「응답 모양이 다릅니다(종류)」)을 BinanceError로 내지만(터미널 글), 화면에는 엔진 글 대신 「잠시 뒤 다시 넣어 주세요.」가 나온다. 경로 0."""
    from blackclip.binance.client import Client
    monkeypatch.setattr(cli, "_make_client", lambda market, testnet, creds: Client(market, testnet, creds, transport=_Transport(time_body),
                                                                                  sleep=lambda s: None))
    conn = cli._connect_setup(ws, "main", False, "연결")
    with pytest.raises(BinanceError, match=SHAPE_TEXT):  # kind: 고치기 전 엔진이 내던 예외(이제 시각 맞춤이 먼저 막음)
        conn.check_key(CREDS)
    server = dashboard.make_server(ensure(tmp_path), 0, connect=conn)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    try:
        p, t = server.server_address[1], server.token
        status, _, body = req(p, "POST", f"/connect/key?t={t}", urlencode({"csrf": conn.csrf, **FORMS["key"]}), FORM_HDR)
        assert status == 502 and "잠시 뒤 다시 넣어 주세요." in body and "응답 모양이 다릅니다" not in body and ws.items == {} and str(tmp_path) not in body
        assert copy_violations(body) == []
    finally:
        server.shutdown()
        server.server_close()


def test_telegram_checker_shape_error_is_502_with_reason(ws, tmp_path, monkeypatch):
    """텔레그램 응답의 parameters가 객체 아닌 값이면 `Bot._call`이 AttributeError → 감싸기가 TelegramError로(터미널 글) → 502 · 화면에는 「잠시 뒤 다시 넣어 주세요.」 · 쓰기 0."""
    monkeypatch.setattr(cli, "_telegram_bot", lambda token: Bot(token, lambda url, form: (429, {"ok": False, "parameters": [1]})))
    conn = cli._connect_setup(ws, "main", False, "연결")
    with pytest.raises(TelegramError, match="응답 모양이 다릅니다"):
        conn.check_telegram(Credentials(TG_TOKEN, ""))
    server = dashboard.make_server(ensure(tmp_path), 0, connect=conn)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    try:
        p, t = server.server_address[1], server.token
        status, _, body = req(p, "POST", f"/connect/telegram?t={t}", urlencode({"csrf": conn.csrf, **FORMS["telegram"]}), FORM_HDR)
        assert status == 502 and "잠시 뒤 다시 넣어 주세요." in body and "응답 모양이 다릅니다" not in body and ws.items == {}
        assert copy_violations(body) == []
    finally:
        server.shutdown()
        server.server_close()

"""대시보드 연결 화면: 연결 상태 쓰기 직렬화 · 잠금 순서 · 키 보관 파일 잠금.

실제 키체인 0: 키 보관은 `MemStore` · 임시 `FileStore` · 가짜 runner를 쓴 `Keychain`만 쓴다(`default_store()`를 부르지 않음).
기다리는 곳(`Event.wait` · `join` · `Barrier`)에는 모두 시간 상한을 둬 잘못돼도 시험이 멈추지 않고 실패한다.
"""
import json
import multiprocessing
import os
import stat
import subprocess
import sys
import threading
import time
from datetime import datetime, timezone

import pytest
from conftest import CREDS, TG_TOKEN, FakeClient, FakeTelegram, MemStore, private_update, routes

from blackclip import workspace
from blackclip.fetch import record_permission, run_fetch
from blackclip.keystore import Credentials, FileStore
from blackclip.notify import TELEGRAM_ACCOUNT, link_telegram, set_notify, settings
from blackclip.binance.permissions import Permission
from blackclip.telegram import Bot
from blackclip.workspace import LockBusy, ensure, load_connections

NOW = datetime(2026, 10, 15, 12, 0, tzinfo=timezone.utc)
WAIT = 10
"""시험 안 기다림의 상한(초). 넘으면 잘못된 것으로 보고 실패한다."""


def repo(tmp_path):
    """Git 레포 꼭대기에 작업 폴더를 만든다(.gitignore 포함)."""
    (tmp_path / ".git").mkdir(exist_ok=True)
    return ensure(tmp_path)


def _slow_set(root, key, value, hold_s):
    """연결 상태 칸 하나를 잠금 안에서 천천히 바꾼다(다른 쓰는 곳과 겹치게)."""
    def fn(c):
        time.sleep(hold_s)
        c[key] = value
    workspace.update_connections(root, fn, wait_s=WAIT)


def _proc_set(root, key, value, hold_s):
    """다른 프로세스에서 `_slow_set`(multiprocessing spawn 대상)."""
    _slow_set(root, key, value, hold_s)


def _proc_filestore(path, prefix, n):
    """다른 프로세스에서 FileStore에 계정 n개를 하나씩 저장한다."""
    fs = FileStore(path)
    for i in range(n):
        fs.save(f"{prefix}{i}", Credentials(f"{prefix}{i}" * 4, "s" * 16))


def _spawn(target, *args):
    """spawn 프로세스 하나를 띄운다(macOS 기본과 같은 방식)."""
    p = multiprocessing.get_context("spawn").Process(target=target, args=args)
    p.start()
    return p


def _join(p):
    """프로세스를 시간 상한 안에 기다리고 정상 종료였는지 본다."""
    p.join(WAIT * 3)
    assert p.exitcode == 0, p.exitcode


def test_update_connections_serializes_threads_and_processes(tmp_path):
    """스레드 둘 · 프로세스 하나가 서로 다른 칸을 잠금 안에서 천천히 고쳐도 셋 다 남는다(잠금이 없으면 나중 쓰기가 앞을 덮음)."""
    root = repo(tmp_path)
    p = _spawn(_proc_set, root, "c", 3, 0.5)
    threads = [threading.Thread(target=_slow_set, args=(root, k, v, 0.3)) for k, v in (("a", 1), ("b", 2))]
    for t in threads:
        t.start()
    for t in threads:
        t.join(WAIT * 3)
        assert not t.is_alive()
    _join(p)
    conns = load_connections(root)
    assert (conns.get("a"), conns.get("b"), conns.get("c")) == (1, 2, 3)


def test_update_connections_busy_and_no_nesting(tmp_path):
    """잠금을 쥔 채 wait_s를 넘기면 LockBusy, 잠금 안에서 다시 부르면(중첩) 자기 자신을 기다려 LockBusy(flock은 열린 파일 설명마다)."""
    root = repo(tmp_path)
    with workspace.lock(root, "connections", 0):
        with pytest.raises(LockBusy):
            workspace.update_connections(root, lambda c: c.update(x=1), wait_s=0)
    assert "x" not in load_connections(root)
    seen = []
    with pytest.raises(LockBusy):
        workspace.update_connections(root, lambda c: seen.append(workspace.update_connections(root, lambda d: None, wait_s=0)), wait_s=0)
    assert seen == [] and load_connections(root) == {}


def test_apply_connections_returns_saved_dict(tmp_path):
    """apply_connections는 잠금 없이 읽기 → 고치기 → 쓰기, 쓴 dict를 돌려준다. fn이 예외면 쓰지 않는다."""
    root = repo(tmp_path)
    assert workspace.apply_connections(root, lambda c: c.update(a=1)) == {"a": 1}
    with pytest.raises(ZeroDivisionError):
        workspace.apply_connections(root, lambda c: (c.update(a=2), 1 / 0))
    assert load_connections(root) == {"a": 1}


def test_record_permission_bytes_unchanged(tmp_path):
    """record_permission(이제 update_connections · note_permission)은 지금과 같은 바이트를 쓴다."""
    root = repo(tmp_path)
    record_permission(root, "main", False, Permission("read_only", "조회만"), NOW)
    assert (root / "connections.json").read_text(encoding="utf-8") == json.dumps(
        {"binance": {"main": {"permission": {"status": "read_only", "detail": "조회만", "checked_at": "2026-10-15T12:00:00+00:00"}}}},
        ensure_ascii=False, indent=2, sort_keys=True) + "\n"


def test_fetch_keeps_concurrent_connection_changes(tmp_path):
    """체결을 받는 도중(가짜 client가 멈춤) set_notify가 민감도 · 조용한 날을 바꿔도 run_fetch 끝에 그 값이 남고 종목 칸만 갱신된다."""
    root = repo(tmp_path)
    store = MemStore({"binance:main": CREDS})
    inside, go = threading.Event(), threading.Event()
    answer = routes()

    def slow(path, q):
        if path == "/api/v3/myTrades":
            inside.set()
            assert go.wait(WAIT)
        return answer(path, q)

    got = {}
    t = threading.Thread(target=lambda: got.setdefault("r", run_fetch(
        root, store, lambda m, tn, c: FakeClient(m, tn, slow), label="main", testnet=False, markets=["spot"],
        symbols=["BTCUSDT"], since=None, now=NOW)))
    t.start()
    try:
        assert inside.wait(WAIT)
        set_notify(root, quiet="바뀔때만", sensitivity="둔감")
    finally:
        go.set()
    t.join(WAIT)
    assert not t.is_alive() and got["r"][0] == 0
    conns = load_connections(root)
    assert conns["telegram"]["quiet"] == "바뀔때만" and conns["defaults"]["sensitivity"] == "둔감"
    assert conns["binance"]["main"]["symbols"] == {"spot": ["BTCUSDT"]} and conns["binance"]["main"]["permission"]["status"] == "read_only"


class SlowTelegram(FakeTelegram):
    """getUpdates 첫 번째에서 멈추는 가짜 transport(대화 잇기 도중을 흉내)."""

    def __init__(self, updates):
        super().__init__(updates)
        self.polling, self.go = threading.Event(), threading.Event()

    def __call__(self, url, form):
        if url.endswith("/getUpdates") and not self.polling.is_set():
            self.polling.set()
            assert self.go.wait(WAIT)
        return super().__call__(url, form)


def _link_in_thread(root, store, fake):
    """link_telegram을 스레드로 돌린다 → (스레드, 출력 줄, 결과 사전)."""
    out, got = [], {}
    t = threading.Thread(target=lambda: got.setdefault("code", link_telegram(
        root, store, lambda tok: Bot(tok, fake), out.append, NOW, wait_s=60, code="482913")))
    t.start()
    return t, out, got


TG2 = "987654321:" + "N" * 31 + "Ab12"
"""바꿔 넣은 두 번째 봇 토큰(지어낸 값)."""


def test_link_aborts_when_token_replaced_midway_engine(tmp_path):
    """잇기가 숫자를 기다리는 동안 토큰이 바뀌면 대화를 적지도 확인 글을 보내지도 않고 종료 2와 한 줄."""
    root = repo(tmp_path)
    store = MemStore({TELEGRAM_ACCOUNT: Credentials(TG_TOKEN, "")})
    fake = SlowTelegram([private_update("482913", 4242)])
    t, out, got = _link_in_thread(root, store, fake)
    try:
        assert fake.polling.wait(WAIT)
        store.save(TELEGRAM_ACCOUNT, Credentials(TG2, ""))
    finally:
        fake.go.set()
    t.join(WAIT)
    assert not t.is_alive() and got["code"] == 2
    assert out[-1] == "텔레그램 토큰이 바뀌어 대화를 잇지 않았어요 · blackclip connect --telegram으로 다시"
    assert settings(root)["chat_id"] is None and fake.sent() == []
    assert not (root / "connections.json").exists()


def test_filestore_concurrent_saves_keep_both(tmp_path):
    """서로 다른 계정을 두 프로세스가 번갈아 저장해도 모두 남는다(보관 파일 잠금 안에서 읽기 → 고치기 → 쓰기)."""
    path = tmp_path / "cfg" / "secrets.json"
    procs = [_spawn(_proc_filestore, path, prefix, 15) for prefix in ("a", "b")]
    for p in procs:
        _join(p)
    fs = FileStore(path)
    assert all(fs.load(f"{x}{i}") is not None for x in "ab" for i in range(15))
    assert stat.S_IMODE(path.stat().st_mode) == 0o600
    assert sorted(p.name for p in path.parent.iterdir()) == ["secrets.json", "secrets.json.lock"]


def test_filestore_no_fixed_tmp_name(tmp_path):
    """고정 `.tmp` 이름을 쓰지 않는다: 그 자리에 심은 링크를 따라 쓰지 않고, 같은 폴더 임시 파일(0600)을 바꿔치기한다."""
    path = tmp_path / "cfg" / "secrets.json"
    path.parent.mkdir(mode=0o700)
    outside = tmp_path / "outside.txt"
    outside.write_text("keep", encoding="utf-8")
    (path.parent / "secrets.json.tmp").symlink_to(outside)
    fs = FileStore(path)
    fs.save("binance:main", CREDS)
    assert outside.read_text(encoding="utf-8") == "keep" and fs.load("binance:main") == CREDS
    assert stat.S_ISREG(os.lstat(path).st_mode) and stat.S_IMODE(path.stat().st_mode) == 0o600
    fs.delete("binance:main")
    assert fs.load("binance:main") is None


def test_filestore_load_reads_whole_file_while_saving(tmp_path):
    """FileStore.load는 잠금을 잡지 않는다: 저장이 잠금을 쥔 동안에도 읽기는 바로 끝나고 반쯤 쓴 파일을 보지 않는다(os.replace 원자 교체)."""
    path = tmp_path / "cfg" / "secrets.json"
    fs = FileStore(path)
    fs.save("binance:main", CREDS)
    import fcntl
    fd = os.open(f"{path}.lock", os.O_RDWR)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX)
        assert fs.load("binance:main") == CREDS
    finally:
        os.close(fd)


def test_lock_holder_subprocess_helper_works(tmp_path):
    """뒤 시험이 쓰는 「다른 프로세스가 connections.lock을 쥠」 도우미가 실제로 잠근다."""
    root = repo(tmp_path)
    p = hold_lock(root, 2)
    try:
        with pytest.raises(LockBusy):
            workspace.update_connections(root, lambda c: None, wait_s=0)
    finally:
        p.kill()
        p.wait(WAIT)


def hold_lock(root, seconds):
    """다른 프로세스가 `connections.lock`을 seconds초 쥐게 한다(쥔 뒤에 돌려줌).

    Returns:
        subprocess.Popen(끝에 kill · wait).
    """
    code = ("import sys,time\nfrom pathlib import Path\nfrom blackclip.workspace import lock\n"
            f"with lock(Path({str(root)!r}), 'connections', 0):\n    print('held', flush=True)\n    time.sleep({seconds})\n")
    p = subprocess.Popen([sys.executable, "-c", code], stdout=subprocess.PIPE, text=True)
    assert p.stdout.readline().strip() == "held"
    return p


def test_first_concurrent_lock_creation_never_fails(tmp_path):
    """잠금 파일이 아직 없을 때 여러 스레드가 함께 잠가도 FileNotFoundError가 없다.

    macOS 27.0.1에서 같은 이름을 dir_fd 기준 O_CREAT로 동시에 열면 커널이 ENOENT를 낸다(`workspace.open_or_create`와 같은 기록:
    8스레드 날 open 재현 300번 가운데 285번, 경로 기준 · O_EXCL 0번). 잠금이 처음 생기는 순간에도 쓰는 곳 다섯이 실패하지 않아야 한다.
    잠금 경로는 잠금 전 단계가 있어 날 open보다 덜 겹친다: 이 시험은 「한 번 더 열기」판을 늘 잡지는 못한다(덧붙이기 시험이 잡음).
    """
    for i in range(6):  # 날 open 재현에서 한 번에 약 95%가 겹침
        (tmp_path / str(i) / ".git").mkdir(parents=True)
        root = ensure(tmp_path / str(i))
        bar, errs = threading.Barrier(8, timeout=WAIT), []  # 여럿이 한꺼번에 처음 만든다

        def go(k, root=root, bar=bar, errs=errs):
            bar.wait()
            try:
                workspace.update_connections(root, lambda c: c.update({k: 1}), wait_s=WAIT)
            except OSError as exc:
                errs.append(exc)

        threads = [threading.Thread(target=go, args=(k,)) for k in "abcdefgh"]
        for t in threads:
            t.start()
        for t in threads:
            t.join(WAIT * 2)
        assert errs == [] and sorted(load_connections(root)) == list("abcdefgh"), (i, errs)



def test_first_concurrent_append_never_fails(tmp_path):
    """append_line도 같은 커널 경합을 탄다(dir_fd 기준 O_CREAT): 없는 파일에 여러 스레드가 처음 덧붙여도 실패 0 · 줄 수 그대로.

    고치기 전에는 ENOENT가 「링크 · FIFO · 장치」 WorkspaceError로 잘못 보였다.
    """
    for i in range(6):
        (tmp_path / str(i) / ".git").mkdir(parents=True)
        root = ensure(tmp_path / str(i))
        bar, errs = threading.Barrier(8, timeout=WAIT), []

        def go(k, root=root, bar=bar, errs=errs):
            bar.wait()
            try:
                workspace.append_line(root, "log.txt", f"{k}\n")
            except (OSError, workspace.WorkspaceError) as exc:
                errs.append(exc)

        threads = [threading.Thread(target=go, args=(k,)) for k in "abcdefgh"]
        for t in threads:
            t.start()
        for t in threads:
            t.join(WAIT)
        assert errs == [] and sorted((root / "log.txt").read_text().split()) == list("abcdefgh"), (i, errs)

# ── 연결 화면 GET ─────────────────────────────────────────────────────────────

import http.client  # noqa: E402
import re  # noqa: E402
import socket  # noqa: E402
from html import unescape  # noqa: E402
from pathlib import Path  # noqa: E402
from types import SimpleNamespace  # noqa: E402
from urllib.parse import urlencode  # noqa: E402

from webrules import copy_violations  # noqa: E402

from blackclip import dashboard, stages  # noqa: E402
from blackclip.connect import HEADERS, Connect, SecureHandler  # noqa: E402
from blackclip.screen import web  # noqa: E402

KEY, SECRET = "A" * 60 + "WXYZ", "S" * 64
OK = Permission("read_only", "조회만 켜져 있음")
BOT = Permission("read_only", "봇 @bc_test_bot")


@pytest.fixture(autouse=True)
def _home(monkeypatch, tmp_path):
    """judge가 보는 ~/Library/LaunchAgents를 임시 자리로."""
    monkeypatch.setenv("HOME", str(tmp_path / "home"))


@pytest.fixture
def serve(tmp_path):
    """연결 설정을 넘긴 대시보드 서버를 띄운다 → SimpleNamespace(server, port, token, connect, root, store, once). 끝에 shutdown · server_close."""
    servers = []

    def _serve(root=None, store=None, check_key=None, check_telegram=None, once=None, testnet=False):
        root = root or repo(tmp_path)
        conn = Connect(store=MemStore() if store is None else store, label="main", testnet=testnet, title="binance main 연결",
                       check_key=check_key or (lambda c: OK), check_telegram=check_telegram or (lambda c: BOT))
        server = dashboard.make_server(root, 0, connect=conn, once=once)
        threading.Thread(target=server.serve_forever, daemon=True).start()
        servers.append(server)
        return SimpleNamespace(server=server, port=server.server_address[1], token=server.token, connect=conn, root=root,
                               store=conn.store, once=once)

    yield _serve
    for s in servers:
        s.shutdown()
        s.server_close()


def req(port, method, path, body=None, headers=None):
    """요청 하나 → (상태, 머리글, 본문). headers에 Host가 있으면 http.client가 따로 붙이지 않는다."""
    c = http.client.HTTPConnection("127.0.0.1", port, timeout=WAIT)
    c.request(method, path, body=body, headers=headers or {})
    r = c.getresponse()
    out = r.status, dict(r.getheaders()), r.read().decode()
    c.close()
    return out


def raw(port, data: bytes):
    """날 바이트 요청 하나 → (상태, 머리글, 본문). 서버가 닫을 때까지 읽는다."""
    with socket.create_connection(("127.0.0.1", port), timeout=WAIT) as s:
        s.sendall(data)
        got = b""
        while chunk := s.recv(65536):
            got += chunk
    head, _, body = got.partition(b"\r\n\r\n")
    lines = head.decode("latin-1").split("\r\n")
    return int(lines[0].split()[1]), dict(x.split(": ", 1) for x in lines[1:]), body.decode()


def has_headers(h: dict) -> bool:
    """connect.HEADERS 여섯 머리글이 모두 그 값으로 붙었는지."""
    return all(h.get(k) == v for k, v in HEADERS.items())


def get(s, path="/connect"):
    """접속 토큰을 붙인 GET → (상태, 본문)."""
    status, _, body = req(s.port, "GET", f"{path}?t={s.token}")
    return status, body


def test_connect_get_refusals(serve):
    """토큰 없음 · 틀린 토큰 · 비ASCII 토큰 · 루프백 아닌 Host · 다른 Origin은 403, Host 없음은 400. 모두 머리글 여섯."""
    s = serve()
    for path, headers in (("/connect", {}), ("/connect?t=wrong", {}), ("/connect?t=%ED%95%9C", {}),
                          (f"/connect?t={s.token}", {"Host": "evil.example"}), (f"/connect?t={s.token}", {"Origin": "http://evil.example"})):
        status, h, body = req(s.port, "GET", path, headers=headers)
        assert status == 403 and has_headers(h) and s.connect.csrf not in body, (path, headers)
    status, h, _ = raw(s.port, f"GET /connect?t={s.token} HTTP/1.1\r\n\r\n".encode())
    assert status == 400 and has_headers(h)
    status, h, body = req(s.port, "GET", f"/connect?t={s.token}")
    assert status == 200 and has_headers(h) and s.connect.csrf in body


def test_connect_routes_404_without_connect_config(tmp_path):
    """connect=None이면 `/connect` · `POST /connect/*`는 404(갈래 없음)."""
    server = dashboard.make_server(repo(tmp_path), 0)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    try:
        p, t = server.server_address[1], server.token
        assert req(p, "GET", f"/connect?t={t}")[0] == 404
    finally:
        server.shutdown()
        server.server_close()


def test_one_secure_handler_without_own_send_error(serve):
    """서버 하나(dashboard.make_server)가 바탕 SecureHandler를 상속하고 send_error · 로그 · 시간 제한을 따로 두지 않는다."""
    h = serve().server.RequestHandlerClass
    assert issubclass(h, SecureHandler) and h is not SecureHandler and h.timeout == 30
    assert not {"send_error", "log_message", "timeout", "server_version", "sys_version"} & set(vars(h))


def test_binds_loopback_only_with_connect(serve):
    """연결 설정을 넘겨도 127.0.0.1에만 묶인다."""
    assert serve().server.server_address[0] == "127.0.0.1"


def test_no_new_path_reads_in_dashboard():
    """대시보드 · web 코드에 Path 읽기 · exists · is_file · glob · 맨 open( 0줄(기존 모듈 호출 · FD 함수만)."""
    src = Path(dashboard.__file__).parent
    pat = re.compile(r"read_text|\.exists\(|\.is_file\(|glob\(|(?<![\w.])open\(")
    for rel in ("dashboard.py", "screen/web.py"):
        hits = [line for line in (src / rel).read_text(encoding="utf-8").splitlines() if pat.search(line)]
        assert hits == [], (rel, hits)


def test_nav_links_on_connect_page(serve):
    """상주 연결 화면 내비는 `홈` · `연결`(지금 화면), 링크는 모두 200."""
    s = serve()
    _, body = get(s)
    nav = re.search(r"<nav.*?</nav>", body, re.S)[0]
    links = re.findall(r'<a [^>]*href="([^"]+)"[^>]*>(.*?)</a>', nav)
    assert [t for _, t in links] == ["홈", "연결"] and 'aria-current="page">연결<' in nav
    assert all(req(s.port, "GET", unescape(h))[0] == 200 for h, _ in links)


def _fills(root, rows):
    """받은 체결 흉내: fills/binance-main/spot/coverage.json + fills.csv(머리 줄 + rows줄)."""
    d = root / "fills" / "binance-main" / "spot"
    d.mkdir(parents=True, exist_ok=True)
    (d / "coverage.json").write_text(json.dumps({"BTCUSDT": {"from": "2026-07-17T00:00:00+00:00", "until": "2026-10-15T12:00:00+00:00"}}))
    (d / "fills.csv").write_text("h\n" + "x\n" * rows)


def _setup_section(body):
    """첫 셋업 패널 HTML."""
    return next(m for m in re.findall(r'<section class="panel">(.*?)</section>', body, re.S) if "첫 셋업" in m)


def test_connect_page_shows_setup_branch(serve, tmp_path):
    """갈래 셋(있음 · 없음 · 아직 안 받음) · 점검 막힘 · 단계 한 줄(SETUP_WEB), 엔진 next · notes 글 0, 문구 규칙 0."""
    s = serve()
    j = stages.judge(s.root)
    _, body = get(s)
    sec = _setup_section(body)
    assert f"셋업 {len(j['steps'])}단계 가운데 0단계까지 했어요." in sec and web.SETUP_BRANCH[None] in sec
    assert '<code class="cmd">blackclip scan .</code>' in sec and unescape(j["next"]) not in unescape(body)
    (s.root / "map.json").write_text("{}")
    _, body = get(s)
    assert web.SETUP_WEB["조회 키"] in _setup_section(body)
    _fills(s.root, 0)
    s.root.joinpath("connections.json").write_text(json.dumps({"binance": {"main": {"permission": {"status": "read_only", "detail": "x", "checked_at": "2026-10-15T12:00:00+00:00"}}}}))
    _, body = get(s)
    sec = _setup_section(body)
    assert web.SETUP_BRANCH["없음"] in sec and web.SETUP_WEB["그 밖"] in sec
    assert "2026-10-15 12:00 UTC 확인" in body and "현물 2026-07-17 00:00~2026-10-15 12:00 UTC" in unescape(re.sub(r"<[^>]+>", "", body))
    _fills(s.root, 3)
    _, body = get(s)
    assert web.SETUP_BRANCH["있음"] in _setup_section(body)
    _fills(s.root, 0)
    d = s.root / "strategies" / "s1"
    d.mkdir(parents=True)
    (d / "link.py").write_text("x")
    (d / "check.md").write_text("# 점검 · s1\n\n| 추천 가드레일 | 점검 불가 |\n")
    j = stages.judge(s.root)
    _, body = get(s)
    assert j["now"] == "점검" and '<code class="cmd">blackclip check s1</code>' in _setup_section(body)
    assert all(unescape(n) not in unescape(body) for n in j["notes"]) and unescape(j["next"]) not in unescape(body)
    assert copy_violations(body) == []


@pytest.mark.parametrize("chosen, token, linked, scheduled, want", [
    (False, False, False, False, "고르기 전"), (True, False, False, False, "토큰 없음"), (True, True, False, False, "대화 전"),
    (True, True, True, False, "예약 없음"), (True, True, True, None, "예약 모름")])
def test_setup_web_schedule_only_left(chosen, token, linked, scheduled, want):
    """「예약 · 알림」 단계의 넷 + 예약 모름 → 각 한 줄. 마지막 둘은 패널 안내가 아니라 AI에게 부탁."""
    j = {"now": "예약 · 알림", "strategy": "s1", "alerts": {"chosen": chosen, "linked": linked, "scheduled": scheduled}}
    line = web.setup_line(j, token)
    assert web.SETUP_WEB[want] in line
    assert ("아래 「" in line) == (want in ("고르기 전", "토큰 없음"))


def test_judge_alerts_follow_existing_formula(tmp_path, monkeypatch):
    """judge 반환 alerts 세 값 = 예약 · 알림 단계 done의 식(stages.py 236-240). 터미널 setup 글 바이트는 BASE와 같은
    tests/test_stages.py(바꾸지 않음)가 지킨다."""
    root = repo(tmp_path)
    agents = tmp_path / "agents"
    agents.mkdir()
    for conns, want in (({}, {"chosen": False, "linked": False}), ({"defaults": {"sensitivity": "보통"}}, {"chosen": True, "linked": False}),
                        ({"defaults": {"sensitivity": "보통"}, "telegram": {"chat_id": 1}}, {"chosen": True, "linked": True})):
        (root / "connections.json").write_text(json.dumps(conns))
        j = stages.judge(root, agents=agents)
        assert {k: j["alerts"][k] for k in ("chosen", "linked")} == want
        sched = j["alerts"]["scheduled"]
        assert sched is (None if sys.platform != "darwin" else False)
        assert j["done"][-1] == (sched if want["chosen"] and want["linked"] else False)


def _inputs(body):
    """키 · 시크릿 · 토큰 · 헬스체크 입력 칸 태그들."""
    return re.findall(r'<input[^>]*name="(?:api_key|secret_key|bot_token|healthcheck)"[^>]*>', body)


def test_connect_form_fields_are_write_only(serve):
    """키 · 시크릿 · 토큰 · 헬스체크 칸에 value 없음, 시크릿 · 토큰은 password · new-password, API 키는 text · 자동 채움 끔 · 맞춤법 끔."""
    s = serve()
    _, body = get(s)
    tags = _inputs(body)
    assert len(tags) == 4 and all("value=" not in t for t in tags)
    assert re.search(r'name="api_key" type="text" autocomplete="off" spellcheck="false"', body)
    for n in ("secret_key", "bot_token"):
        assert re.search(rf'name="{n}" type="password" autocomplete="new-password"', body)
    assert body.count(f'name="csrf" value="{s.connect.csrf}"') == 3 and 'for="api_key"' in body
    assert web.OTHER_ACCOUNT in body and all(x in body for x in web.CONNECT_FACTS)


def test_connect_page_copy_rules_get_states(serve, tmp_path):
    """빈 상태 · 키 있음 · 텔레그램 이음 · 고르기 열림(첫 회고) · 한 번용 셋을 copy_violations에(POST 상태는 아래 시험)."""
    s = serve(store=MemStore({"binance:main": Credentials(KEY, SECRET), TELEGRAM_ACCOUNT: Credentials(TG_TOKEN, "")}))
    assert copy_violations(get(s)[1]) == []
    (s.root / "connections.json").write_text(json.dumps({"telegram": {"chat_id": 7, "bot": "@b", "quiet": "바뀔때만"}, "defaults": {"sensitivity": "둔감"}}))
    (s.root / "strategies" / "s1").mkdir(parents=True)
    (s.root / "strategies" / "s1" / "retro.md").write_text("# 실행 회고 · x\n")
    _, body = get(s)
    assert copy_violations(body) == [] and "이었어요" in body and 'name="execution_alerts"' in body and '끝 4자리 <span class="id">WXYZ</span>' in body
    assert KEY not in body and SECRET not in body and TG_TOKEN not in body
    assert f'href="/s/s1/retro?t={s.token}"' in body
    for kind in ("key", "telegram", "choose"):
        o = serve(root=s.root, once=dashboard.Once(kind, threading.Event()))
        _, body = get(o)
        assert copy_violations(body) == [] and "<nav" in body and 'class="nav-link"' not in body
        assert body.count('<form class="form"') == 1 and f'action="/connect/{kind}?' in body and "연결 상태" not in body


def test_connect_errors_have_no_absolute_path(serve, tmp_path):
    """깨진 connections.json · 권한 644 FileStore · 키 보관 읽기 실패 → 화면에 절대 경로 0, 응답 200(그 칸만 사유)."""
    fs = FileStore(tmp_path / "cfg" / "secrets.json")
    fs.save("binance:main", CREDS)
    os.chmod(fs.path, 0o644)
    s = serve(store=fs)
    (s.root / "connections.json").write_text("{")
    status, body = get(s)
    banned = (str(tmp_path), str(tmp_path.resolve()))
    assert status == 200 and not [b for b in banned if b in body] and not re.search(r"(?<![\w.-])/(?:Users|home|private|var|tmp)/", body)
    assert dashboard.KEYSTORE_WHY in body and "연결 상태 파일을 읽지 못했어요" in body and copy_violations(body) == []


# ── 연결 화면 POST · CSRF · 한 번용 · .gitignore ───────────────────────────────

import blackclip.cli as cli  # noqa: E402
from blackclip.binance.client import BinanceError  # noqa: E402
from blackclip.binance.permissions import check  # noqa: E402
from blackclip.telegram import TelegramError  # noqa: E402
from test_permissions import S1, Fake as Restrictions  # noqa: E402
from test_keystore import FakeSecurity  # noqa: E402
from blackclip.keystore import Keychain, KeystoreError  # noqa: E402
from blackclip.workspace import GITIGNORE  # noqa: E402

FORMS = {"key": {"api_key": KEY, "secret_key": SECRET}, "telegram": {"bot_token": TG_TOKEN}, "choose": {"sensitivity": "보통", "quiet": "매일"}}
KEY2 = "B" * 60 + "QRST"
FORM_HDR = {"Content-Type": "application/x-www-form-urlencoded"}


def post(s, kind, fields=None, headers=None, csrf=None, token=None, path=None):
    """연결 화면 폼 하나를 보낸다 → (상태, 머리글, 본문). csrf · token을 안 주면 지금 값."""
    body = urlencode({"csrf": s.connect.csrf if csrf is None else csrf, **(FORMS[kind] if fields is None else fields)})
    return req(s.port, "POST", path or f"/connect/{kind}?t={s.token if token is None else token}", body, FORM_HDR | (headers or {}))


def conns_bytes(root):
    """connections.json 바이트(없으면 None)."""
    p = root / "connections.json"
    return p.read_bytes() if p.exists() else None


def gated():
    """시험이 풀어 줄 때까지 멈추는 가짜 checker → (들어옴, 풀기, checker, 부른 수)."""
    entered, go, calls = threading.Event(), threading.Event(), []

    def check(c):
        calls.append(c)
        entered.set()
        assert go.wait(WAIT)
        return OK
    return entered, go, check, calls


def break_pipe(server):
    """이 서버의 응답 쓰기가 BrokenPipeError가 되게 한다(클라이언트가 응답 전에 끊은 것, 가짜 wfile)."""
    cls = server.RequestHandlerClass
    orig = cls.setup

    class Broken:
        closed = False

        def write(self, b):
            raise BrokenPipeError(32, "Broken pipe")

        def flush(self):
            pass

        def close(self):
            self.closed = True

    def setup(self):
        orig(self)
        self.wfile = Broken()
    cls.setup = setup


def post_ignoring_disconnect(s, kind, **kw):
    """응답 없이 끊긴 POST(break_pipe 뒤)."""
    try:
        post(s, kind, **kw)
    except (http.client.HTTPException, ConnectionError):
        pass


def test_connect_post_refusals(serve):
    """경로 셋 × (루프백 아닌 Host · 토큰 없음 · 틀린 토큰 · 다른 포트 Origin · Origin null · CSRF 없음 · 틀린 CSRF · 길이 0 · 4097) → 400/403/413, 저장 0, 머리글 여섯."""
    s = serve()
    (s.root / "connections.json").write_text('{"x": 1}\n')
    before = conns_bytes(s.root)
    for kind in ("key", "telegram", "choose"):
        cases = [({"headers": {"Host": "evil.example"}}, 403), ({"token": ""}, 403), ({"token": "wrong"}, 403),
                 ({"headers": {"Origin": "http://127.0.0.1:9999"}}, 403), ({"headers": {"Origin": "null"}}, 403),
                 ({"csrf": ""}, 403), ({"csrf": "wrong"}, 403), ({"csrf": "한"}, 403)]
        for kw, code in cases:
            status, h, body = post(s, kind, **kw)
            assert status == code and has_headers(h), (kind, kw, status)
        for n in (0, 4097):
            prefix = f"csrf={s.connect.csrf}&pad="
            body = "" if n == 0 else prefix + "a" * (n - len(prefix))
            status, h, _ = req(s.port, "POST", f"/connect/{kind}?t={s.token}", body, FORM_HDR)
            assert status == 413 and has_headers(h), (kind, n)
        status, h, _ = raw(s.port, f"POST /connect/{kind}?t={s.token} HTTP/1.1\r\nContent-Length: 3\r\n\r\nx=1".encode())
        assert status == 400 and has_headers(h)
    assert s.store.items == {} and conns_bytes(s.root) == before


def test_browser_origin_header_saves(serve):
    """브라우저가 같은 출처 폼에 붙이는 Origin(http://127.0.0.1:포트 · localhost)으로는 저장된다."""
    s = serve()
    assert post(s, "key", headers={"Origin": f"http://127.0.0.1:{s.port}"})[0] == 200
    assert s.store.items["binance:main"] == Credentials(KEY, SECRET)
    assert post(s, "key", fields={"api_key": KEY2, "secret_key": SECRET}, headers={"Origin": f"http://localhost:{s.port}"})[0] == 200


def test_connect_csrf_differs_from_access_token_and_each_run(serve):
    """CSRF는 접속 토큰과 다른 32바이트 값이고 서버마다 다르다."""
    a, b = serve(), serve()
    assert a.connect.csrf != a.token and len(a.connect.csrf) >= 43 and a.connect.csrf != b.connect.csrf


def test_csrf_rotates_after_save_and_replay_is_refused(serve):
    """A 저장 → CSRF 새 값 → B 저장 → 옛 A 본문 재전송 403(「화면이 바뀌었어요」) · 보관 값 B."""
    s = serve()
    c0 = s.connect.csrf
    assert post(s, "key")[0] == 200 and s.connect.csrf != c0
    c1 = s.connect.csrf
    assert post(s, "key", fields={"api_key": KEY2, "secret_key": SECRET})[0] == 200
    status, _, body = post(s, "key", csrf=c0)
    assert status == 403 and dashboard.CSRF_TEXT in body and post(s, "key", csrf=c1)[0] == 403
    assert s.store.items["binance:main"] == Credentials(KEY2, SECRET)


def test_same_csrf_concurrent_posts_one_wins(serve, monkeypatch):
    """같은 CSRF로 key POST 둘(A · B)을 동시에 → 정확히 하나 200 · 하나 403, 보관 값은 200을 받은 쪽, connections.json 쓰기 한 번."""
    entered, go, check, calls = gated()
    s = serve(check_key=check)
    writes = []
    real = workspace.save_connections
    monkeypatch.setattr(workspace, "save_connections", lambda root, data: (writes.append(1), real(root, data)))
    bar, got, csrf = threading.Barrier(2, timeout=WAIT), {}, s.connect.csrf

    def send(name, key):
        bar.wait()
        got[name] = post(s, "key", fields={"api_key": key, "secret_key": SECRET}, csrf=csrf)[0]

    threads = [threading.Thread(target=send, args=a) for a in (("A", KEY), ("B", KEY2))]
    for t in threads:
        t.start()
    try:
        assert entered.wait(WAIT)
    finally:
        go.set()
    for t in threads:
        t.join(WAIT)
        assert not t.is_alive()
    assert sorted(got.values()) == [200, 403] and len(calls) == 1 and len(writes) == 1
    winner = KEY if got["A"] == 200 else KEY2
    assert s.store.items["binance:main"] == Credentials(winner, SECRET)


class WatchedLock:
    """서버 POST Lock을 감싸 몇 번째 요청이 Lock 앞에 왔는지 알린다(시험이 B가 기다리는 중임을 시간 대신 신호로 확인)."""

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


def test_delayed_replay_after_rotation_refused(serve):
    """A의 checker가 멈춘 동안 B는 서버 Lock을 기다림 → A 저장 · 새 CSRF → B는 Lock 안 비교에서 403(검사 통과 뒤 늦게 저장 0)."""
    entered, go, check, calls = gated()
    s = serve(check_key=check)
    s.server.post_lock = watched = WatchedLock()
    csrf, got = s.connect.csrf, {}
    a = threading.Thread(target=lambda: got.setdefault("A", post(s, "key", csrf=csrf)[0]))
    a.start()
    assert entered.wait(WAIT)
    b = threading.Thread(target=lambda: got.setdefault("B", post(s, "key", fields={"api_key": KEY2, "secret_key": SECRET}, csrf=csrf)[0]))
    b.start()
    try:
        assert watched.second.wait(WAIT)  # B가 서버 Lock 앞에 왔다(A는 아직 checker 안: B는 옛 CSRF로 Lock을 기다림)
    finally:
        go.set()
    for t in (a, b):
        t.join(WAIT)
        assert not t.is_alive()
    assert got == {"A": 200, "B": 403} and len(calls) == 1 and s.store.items["binance:main"] == Credentials(KEY, SECRET)


def test_partial_failure_rotates_csrf(serve):
    """깨진 connections.json: 키는 저장 · 연결 상태 기록 실패(500) 뒤에도 CSRF 새 값, 옛 CSRF 재전송 403."""
    s = serve()
    (s.root / "connections.json").write_text("{")
    c0 = s.connect.csrf
    status, _, body = post(s, "key")
    assert status == 500 and web.SAVED_PARTIAL in body and s.connect.csrf != c0
    assert post(s, "key", csrf=c0)[0] == 403


def test_saved_key_is_never_echoed(serve):
    """저장 응답 · 그 뒤 GET /connect에 키 · 시크릿 · 토큰 글자 0, 끝 4자리만, 입력 칸에 value= 0."""
    s = serve()
    _, _, saved = post(s, "key")
    _, _, saved_tg = post(s, "telegram")
    _, page = get(s)
    for body in (saved, saved_tg, page):
        assert KEY not in body and SECRET not in body and TG_TOKEN not in body
        assert all("value=" not in t for t in _inputs(body))
    assert '끝 4자리 <span class="id">WXYZ</span>, 권한 조회만, 보관 메모리.' in saved and '끝 4자리 <span class="id">Zq9x</span>, 권한 봇 확인' in saved_tg and web.TELEGRAM_NEXT in saved_tg
    assert '끝 4자리 <span class="id">WXYZ</span>' in page and '끝 4자리 <span class="id">Zq9x</span>' in page and f'href="/connect?t={s.token}"' in saved


def test_numeric_last4_passes_copy_gate(serve):
    """끝 네 자리가 숫자여도(조회 키 · 봇 토큰) 연결 화면 GET과 저장 응답이 문구 관문을 통과한다. 식별자라 쉼표를 넣지 않고
    `<span class="id">`로 감싼다(보이는 글 그대로)."""
    key, tok = "A" * 60 + "1234", TG_TOKEN[:-4] + "5678"
    s = serve(store=MemStore({"binance:main": Credentials(key, SECRET), TELEGRAM_ACCOUNT: Credentials(tok, "")}))
    _, page = get(s)
    assert copy_violations(page) == [] and '끝 4자리 <span class="id">1234</span>' in page and '끝 4자리 <span class="id">5678</span>' in page
    status, _, saved = post(s, "key", fields={"api_key": key, "secret_key": SECRET})
    assert status == 200 and copy_violations(saved) == [] and '끝 4자리 <span class="id">1234</span>, 권한 조회만' in saved
    status, _, saved_tg = post(s, "telegram", fields={"bot_token": tok})
    assert status == 200 and copy_violations(saved_tg) == [] and '끝 4자리 <span class="id">5678</span>, 권한 봇 확인' in saved_tg


def test_copy_gate_exempts_only_id_class_from_number_rules():
    """관문이 실제로 거른다: `class="id"` 안 글만 숫자 규칙(쉼표 없는 큰 수 · M/D)에서 빠지고, 그 밖의 큰 수는 그대로 걸리며,
    `class="id"` 안이어도 금지 낱말 · 합쇼체 · 가운뎃점은 걸린다."""
    assert copy_violations('<p>끝 4자리 <span class="id">1234</span>, <span class="id">12/31</span></p>') == []
    assert copy_violations("<p>1234</p>") == ["쉼표 없는 큰 수 1234"] and copy_violations("<p>12/31</p>") == ["M/D 12/31"]
    assert copy_violations('<p>1234 <span class="id">1234</span></p>') == ["쉼표 없는 큰 수 1234"]  # 밖의 것만 한 번
    assert copy_violations('<span class="id">곧</span>') == ["곧"]
    assert copy_violations('<span class="id">니다</span>') == ["니다"] and copy_violations('<span class="id">a · b</span>') == ["·"]
    assert copy_violations('<span class="idx">1234</span>') == ["쉼표 없는 큰 수 1234"]  # class 낱말이 정확히 id일 때만


def test_rejected_key_not_saved_and_server_keeps_running(serve):
    """주문 권한이 켜진 키: 폼을 다시 그리며 실패 줄(입력 값 되넣지 않음) · 저장 0 · CSRF 그대로 · 서버는 계속 돎."""
    s = serve(check_key=lambda c: Permission("rejected", "조회 말고 켜진 권한: enableSpotAndMarginTrading", enabled=("enableSpotAndMarginTrading",)))
    c0 = s.connect.csrf
    status, _, body = post(s, "key")
    assert status == 200 and dashboard.REJECTED_TEXT in body and "<form" in body and KEY not in body
    assert "현물과 마진 거래" in body  # 거절 사유(켜진 권한)가 화면 말로 보인다(옛 test_rejected_key_not_saved)
    assert s.store.items == {} and s.connect.csrf == c0 and get(s)[0] == 200


def test_checker_error_502_not_saved(serve):
    """권한을 확인하지 못하면(바이낸스 오류 · JSON 아님) 502 · 저장 0 · CSRF 그대로. 텔레그램 getMe 실패도 502."""
    def boom(c):
        raise BinanceError(401, -2015, "Invalid API-key, IP, or permissions for action.")
    s = serve(check_key=boom, check_telegram=lambda c: (_ for _ in ()).throw(ValueError("x")))
    c0 = s.connect.csrf
    status, _, body = post(s, "key")
    assert status == 502 and "저장하지 않았어요" in body and "바이낸스 응답 401" in body and "2015" not in body and s.store.items == {} and s.connect.csrf == c0
    status, _, body = post(s, "telegram")
    assert status == 502 and s.store.items == {} and copy_violations(body) == []


def _fail_line(body, kind="key"):
    """패널의 실패 줄 글(entity를 푼 것). 줄이 없으면 빈 글."""
    m = re.search(rf'<p class="fail" role="status" id="fail-{kind}">(.*?)</p>', body, re.S)
    return unescape(m.group(1)) if m else ""


def test_reading_off_key_gets_own_sentence(serve):
    """조회 권한만 꺼진 키(나머지 꺼짐)는 「주문이나 출금」이라 말하지 않고 새 글을 쓴다. 엔진 합쇼체 0 · 저장 0 · CSRF 그대로 · 칸에 aria-invalid.
    실제 `permissions.check`를 거친다(렌더러 직접 호출이 아님)."""
    s = serve(check_key=lambda c: check(Restrictions({**S1, "enableReading": False})))
    c0 = s.connect.csrf
    status, _, body = post(s, "key")
    assert status == 200 and _fail_line(body) == dashboard.READING_OFF_TEXT
    assert "주문이나 출금" not in _fail_line(body) and "니다" not in _fail_line(body) and copy_violations(body) == []  # 쪽 아래 사실 문구에는 같은 낱말이 있다
    assert 'aria-invalid="true"' in body and s.store.items == {} and s.connect.csrf == c0 and KEY not in body


def test_enabled_permission_key_lists_names_only(serve):
    """조회 말고 켜진 권한이 있는 키는 지금 앞 문장 + 켜진 권한 이름 목록(표 순서). 엔진 `detail` 글은 싣지 않고 새 필드로 짠다."""
    s = serve(check_key=lambda c: check(Restrictions({**S1, "enableWithdrawals": True, "enableSpotAndMarginTrading": True})))
    status, _, body = post(s, "key")
    assert status == 200 and _fail_line(body) == (
        f"{dashboard.REJECTED_TEXT}<br>켜진 권한: 출금, 현물과 마진 거래. 바이낸스에서 조회만 켠 키를 새로 만들어 넣어 주세요.")
    assert copy_violations(body) == [] and 'aria-invalid="true"' in body and s.store.items == {}


def test_checker_retry_exhausted_502_says_retry_later(serve):
    """다시 부르기를 다 쓴 확인 실패(BinanceError status 0)는 엔진 글 대신 「잠시 뒤 다시 넣어 주세요.」 한 문장. 입력이 틀린 게 아니라 aria-invalid 0."""
    def boom(c):
        raise BinanceError(0, None, "다시 부르기 횟수를 넘었습니다")
    s = serve(check_key=boom)
    c0 = s.connect.csrf
    status, _, body = post(s, "key")
    assert status == 502 and _fail_line(body) == f"{dashboard.CHECK_FAIL_TEXT} 잠시 뒤 다시 넣어 주세요."
    assert "니다" not in _fail_line(body) and copy_violations(body) == [] and s.store.items == {} and s.connect.csrf == c0  # status 0(코드 없음)
    assert "aria-invalid" not in body and 'aria-describedby="fail-key"' in body


def test_checker_http_error_502_shows_status_only(serve):
    """바이낸스가 준 응답은 앞 문장 끝에 응답 번호만(세 자리라 웹 숫자 규칙에 안 걸림). 바이낸스 오류 코드(네 자리 식별자)와 문구는 싣지 않는다.
    코드는 터미널의 엔진 글에만 남는다. aria-invalid 0."""
    codes = iter([BinanceError(401, -2015, "Invalid API-key, IP, or permissions for action."), BinanceError(503, None, "Service Unavailable")])

    def boom(c):
        raise next(codes)
    s = serve(check_key=boom)
    status, _, body = post(s, "key")
    assert status == 502 and _fail_line(body) == "권한을 확인하지 못해 저장하지 않았어요(바이낸스 응답 401)."
    assert "2015" not in body and "Invalid" not in body and "aria-invalid" not in body and copy_violations(body) == [] and s.store.items == {}
    status, _, body = post(s, "key")
    assert status == 502 and _fail_line(body) == "권한을 확인하지 못해 저장하지 않았어요(바이낸스 응답 503)." and "Unavailable" not in body
    assert copy_violations(body) == []


def test_telegram_checker_error_502_has_no_engine_text(serve):
    """텔레그램 확인 실패(응답 모양 오류)도 엔진 글 없이 「잠시 뒤 다시 넣어 주세요.」, copy_violations 0, aria-invalid 0."""
    def boom(c):
        raise TelegramError("텔레그램 응답 모양이 다릅니다(KeyError)")
    s = serve(check_telegram=boom)
    status, _, body = post(s, "telegram")
    assert status == 502 and _fail_line(body, "telegram") == f"{dashboard.TG_FAIL_TEXT} 잠시 뒤 다시 넣어 주세요."
    assert copy_violations(body) == [] and "응답 모양" not in body and "aria-invalid" not in body and s.store.items == {}


def test_bad_shape_400_keeps_form(serve):
    """모양이 틀리면 400 · 그 패널에 실패 줄 · 폼 그대로 · 저장 0."""
    s = serve()
    for kind, fields, text in (("key", {"api_key": "short", "secret_key": SECRET}, "키 모양이 달라요"),
                               ("telegram", {"bot_token": "abc"}, "토큰 모양이 달라요"),
                               ("choose", {"sensitivity": "아주민감", "quiet": "매일"}, "하나씩 골라 주세요"),
                               ("choose", {"sensitivity": "보통", "quiet": "매일", "healthcheck": "http://x"}, "https로 시작하는")):
        status, _, body = post(s, kind, fields=fields)
        assert status == 400 and text in body and body.count('<form class="form"') == 3, kind
        assert re.search(rf'<p class="fail" role="status" id="fail-{kind}">[^<]*{text}', body)
    assert s.store.items == {} and conns_bytes(s.root) is None


def test_key_save_records_permission_in_handler(serve):
    """키 저장은 같은 연결 잠금 안에서 권한 확인 결과를 연결 상태에 적는다(지금 test_cli_open_records_permission 값)."""
    s = serve()
    assert post(s, "key")[0] == 200
    p = load_connections(s.root)["binance"]["main"]["permission"]
    assert (p["status"], p["detail"]) == ("read_only", "조회만 켜져 있음") and re.fullmatch(r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}\+00:00", p["checked_at"])


@pytest.mark.parametrize("text", ["{", '{"binance": []}', '{"binance": {"main": []}}'])
def test_state_write_failure_after_key_save(serve, text):
    """깨진 connections.json · binance 칸이 객체 아님 → 키는 저장 · 500 글 · Saved.state_error · 파일 그대로 · 한 번용 완료."""
    once = dashboard.Once("key", threading.Event())
    s = serve(once=once)
    (s.root / "connections.json").write_text(text)
    status, _, body = post(s, "key")
    assert status == 500 and web.SAVED_PARTIAL in body and s.store.items["binance:main"] == Credentials(KEY, SECRET)
    assert once.event.wait(WAIT) and once.done and once.result.state_error and once.result.permission == OK
    assert (s.root / "connections.json").read_text() == text and copy_violations(body) == []


def _linked(root):
    """대화가 이어진 연결 상태를 쓴다."""
    (root / "connections.json").write_text(json.dumps({"telegram": {"chat_id": 42, "bot": "@old", "linked_at": "2026-10-01T00:00:00+00:00", "quiet": "매일"}}))


def test_telegram_token_replace_clears_chat(serve, monkeypatch):
    """대화가 이어진 상태에서 새 토큰 저장 → chat_id · bot · linked_at 지워짐, 다른 칸 그대로, 화면은 대화 없음.
    지운 뒤 실제 알림 발송 길(`daily.run._telegram`, 가짜 transport)을 불러도 보내지 않는다(대조: 이어져 있던 동안은 보냄)."""
    from blackclip.daily import run as daily_run

    fake = FakeTelegram()
    monkeypatch.setattr(cli, "_telegram_bot", lambda tok: Bot(tok, fake))
    s = serve(store=MemStore({TELEGRAM_ACCOUNT: Credentials(TG2, "")}), check_telegram=cli._telegram_check)
    _linked(s.root)
    assert daily_run._telegram(s.root, s.store, cli._telegram_bot, [], [], ["s1"], False)[0] == 0 and len(fake.sent()) == 1
    assert post(s, "telegram")[0] == 200
    t = load_connections(s.root)["telegram"]
    assert t == {"quiet": "매일"} and settings(s.root)["chat_id"] is None and s.store.items[TELEGRAM_ACCOUNT] == Credentials(TG_TOKEN, "")
    assert [m for _, m, _ in fake.calls] == ["sendMessage", "getMe"]
    code, lines = daily_run._telegram(s.root, s.store, cli._telegram_bot, [], [], ["s2"], False)
    assert (code, lines) == (0, ["텔레그램: 대화 연결 전이라 안 보냄 · blackclip connect --telegram"]) and len(fake.sent()) == 1
    assert "이었어요" not in get(s)[1]


def test_telegram_save_in_dashboard_only_stores_token(serve, monkeypatch):
    """대시보드 텔레그램 저장은 Bot 호출이 checker(getMe) 하나뿐, 대화 잇기 · 발송 0."""
    fake = FakeTelegram([private_update("1")])
    monkeypatch.setattr(cli, "_telegram_bot", lambda tok: Bot(tok, fake))
    s = serve(check_telegram=cli._telegram_check)
    status, _, body = post(s, "telegram")
    assert status == 200 and [m for _, m, _ in fake.calls] == ["getMe"] and "봇 확인" in body


HC = "https://hc.example/ping/DEMO-HC-ID"
"""지어낸 헬스체크 주소. 화면 · 결과에 다시 나오면 안 된다."""


def test_choose_save_writes_notify(serve):
    """test_choose.py를 옮김: 저장은 notify와 같은 자리 · 주소 다시 안 보임 · 지금 값 골라짐, 첫 회고 전 실행 경고 400, 이상한 defaults 400 · 파일 그대로."""
    s = serve()
    _, body = get(s)
    assert 'value="보통" required checked' in body and 'value="매일" required checked' in body and "execution_alerts" not in body
    status, _, body = post(s, "choose", fields={"sensitivity": "둔감", "quiet": "바뀔때만", "healthcheck": HC})
    assert status == 200 and HC not in body
    conns = load_connections(s.root)
    assert conns["defaults"] == {"sensitivity": "둔감"} and (conns["telegram"]["quiet"], conns["telegram"]["healthcheck"]) == ("바뀔때만", HC)
    _, again = get(s)
    assert HC not in again and "지금 있어요" in again and 'value="둔감" required checked' in again
    before = conns_bytes(s.root)
    status, _, body = post(s, "choose", fields={"sensitivity": "보통", "quiet": "매일", "ea_shown": "1", "execution_alerts": "on"})
    assert status == 400 and dashboard.EA_TEXT in body and conns_bytes(s.root) == before
    (s.root / "strategies" / "s1" / "daily").mkdir(parents=True)
    (s.root / "strategies" / "s1" / "daily" / "2026-10-15.md").write_text("x")
    assert 'name="execution_alerts"' in get(s)[1]
    assert post(s, "choose", fields={"sensitivity": "민감", "quiet": "매일", "ea_shown": "1", "execution_alerts": "on"})[0] == 200
    assert load_connections(s.root)["telegram"]["execution_alerts"] is True
    assert post(s, "choose", fields={"sensitivity": "민감", "quiet": "매일", "ea_shown": "1"})[0] == 200
    assert load_connections(s.root)["telegram"]["execution_alerts"] is False
    (s.root / "connections.json").write_text('{"defaults": []}')
    status, _, body = post(s, "choose")
    assert status == 400 and "연결 상태 파일을 읽지 못했어요" in body and (s.root / "connections.json").read_text() == '{"defaults": []}'


@pytest.mark.parametrize("text", ["{bad json", '{"telegram": []}', '{"telegram": null}', '{"defaults": 42}'])
def test_choose_refused_on_odd_state_keeps_file_and_serving(serve, text):
    """옛 test_broken_connections_gives_error_page_and_keeps_serving · test_defaults_of_another_type_still_shows_the_form:
    깨진 JSON · telegram 칸이 객체 아님 · defaults가 숫자 → GET은 200(못 읽은 칸만 사유, defaults 42면 보통이 골라짐),
    choose POST는 400 · 파일 바이트 그대로 · CSRF 그대로, 고친 뒤 같은 서버가 저장한다."""
    s = serve()
    (s.root / "connections.json").write_text(text)
    status, body = get(s)
    assert status == 200 and "<form" in body and str(s.root) not in body
    if text == '{"defaults": 42}':
        assert 'value="보통" required checked' in body
    c0 = s.connect.csrf
    status, _, body = post(s, "choose")
    assert status == 400 and (s.root / "connections.json").read_text() == text and s.connect.csrf == c0 and str(s.root) not in body
    (s.root / "connections.json").write_text("{}")
    assert post(s, "choose")[0] == 200 and load_connections(s.root)["defaults"] == {"sensitivity": "보통"}


@pytest.mark.skipif(os.geteuid() == 0, reason="root는 읽기 전용 폴더에도 써서 막을 수 없음")
def test_choose_unwritable_workspace_writes_nothing_then_saves(serve):
    """옛 test_unwritable_workspace_gives_error_page_without_path: 읽기 전용 .blackclip(연결 잠금 파일을 못 만듦) → 쓰기 0 · 경로 0 ·
    한 번용 미완료(event · done 꺼짐), 고친 뒤 같은 서버가 저장하고 한 번용이 끝난다."""
    once = dashboard.Once("choose", threading.Event())
    s = serve(once=once)
    s.root.chmod(0o555)
    try:
        status, _, body = post(s, "choose")
        assert status == 500 and dashboard.STATE_FAIL_TEXT in body and str(s.root) not in body
        assert not once.done and not once.event.is_set()
    finally:
        s.root.chmod(0o755)
    assert not (s.root / "connections.json").exists()
    assert post(s, "choose")[0] == 200 and once.done and once.event.wait(WAIT)


@pytest.mark.skipif(os.geteuid() == 0, reason="root는 못 읽는 폴더도 읽어서 막을 수 없음")
def test_connect_get_unreadable_strategies_has_no_path(serve):
    """옛 test_unreadable_strategies_gives_error_page_on_get: 못 읽는 strategies → GET은 200(읽지 못한 칸만 사유) · 경로 0, 고치면 그대로 200."""
    s = serve()
    (s.root / "strategies").mkdir(exist_ok=True)
    (s.root / "strategies").chmod(0)
    try:
        status, body = get(s)
        assert status == 200 and str(s.root) not in body and str(s.root.resolve()) not in body and copy_violations(body) == []
    finally:
        (s.root / "strategies").chmod(0o755)
    assert get(s)[0] == 200


def test_once_only_for_opened_kind(serve):
    """한 번용 kind=key: telegram · choose POST 403(저장 0 · once 그대로), key 저장 → result.kind key · event, 화면 · 결과에 내비 0 · 다른 패널 0."""
    once = dashboard.Once("key", threading.Event())
    s = serve(once=once)
    for kind in ("telegram", "choose"):
        status, _, body = post(s, kind)
        assert status == 403 and "이 창에서는 조회 키만 넣어요." in body, kind
    assert s.store.items == {} and conns_bytes(s.root) is None and not once.event.is_set() and not once.done
    status, _, body = post(s, "key")
    assert status == 200 and once.event.wait(WAIT) and once.result.kind == "key" and once.result.last4 == "WXYZ"
    assert web.ONCE_CLOSE in body and 'class="nav-link"' not in body and "연결 상태" not in body and "연결 화면으로 돌아가기" not in body


def test_once_refuses_second_save_with_fresh_csrf(serve):
    """한 번용 저장 뒤 GET으로 새 CSRF를 받아 다시 POST → 403 「이미 저장했어요」 · 보관 값 그대로."""
    once = dashboard.Once("key", threading.Event())
    s = serve(once=once)
    assert post(s, "key")[0] == 200
    _, page = get(s)
    assert f'value="{s.connect.csrf}"' in page
    status, _, body = post(s, "key", fields={"api_key": KEY2, "secret_key": SECRET})
    assert status == 403 and dashboard.ONCE_DONE_TEXT in body and s.store.items["binance:main"] == Credentials(KEY, SECRET)


def test_once_result_survives_broken_pipe(serve):
    """한 번용 key 저장 직후 응답을 못 써도(BrokenPipeError) once.result가 있고 event가 켜진다(CLI는 저장 결과로 끝남)."""
    once = dashboard.Once("key", threading.Event())
    s = serve(once=once)
    break_pipe(s.server)
    post_ignoring_disconnect(s, "key")
    assert once.event.wait(WAIT) and once.result.kind == "key" and once.result.state_error is None
    assert s.store.items["binance:main"] == Credentials(KEY, SECRET)


class CountingStore(MemStore):
    """save를 세고, fail이면 저장하지 않고 KeystoreError(보관 실패 흉내)."""

    def __init__(self, items=None, fail=False):
        super().__init__(items)
        self.saves, self.fail = 0, fail

    def save(self, account, creds):
        self.saves += 1
        if self.fail:
            raise KeystoreError("보관 실패(흉내)")
        super().save(account, creds)


def test_telegram_post_clears_chat_before_store(serve, monkeypatch):
    """보관이 실패하면 대화 연결은 지워지고 옛 토큰은 그대로 · 500 「확인하지 못했어요」 · CSRF 새 값.
    지우기(save_connections)가 실패하면 보관 호출 0 · 쓰기 0 · CSRF 그대로."""
    store = CountingStore({TELEGRAM_ACCOUNT: Credentials(TG2, "")}, fail=True)
    s = serve(store=store)
    _linked(s.root)
    c0 = s.connect.csrf
    status, _, body = post(s, "telegram")
    assert status == 500 and web.SAVED_UNSURE in body and settings(s.root)["chat_id"] is None
    assert store.items[TELEGRAM_ACCOUNT] == Credentials(TG2, "") and s.connect.csrf != c0
    store2 = CountingStore({TELEGRAM_ACCOUNT: Credentials(TG2, "")})
    s2 = serve(store=store2)
    _linked(s2.root)
    before, c0 = conns_bytes(s2.root), s2.connect.csrf

    def boom(root, data):
        raise OSError(5, "Input/output error")
    monkeypatch.setattr(workspace, "save_connections", boom)
    status, _, body = post(s2, "telegram")
    assert status == 500 and dashboard.STATE_FAIL_TEXT in body and store2.saves == 0 and conns_bytes(s2.root) == before
    assert s2.connect.csrf == c0


def test_store_save_error_is_uncertain_write(serve):
    """키체인 저장 뒤 검증이 예외(가짜 runner: 쓰고 나서 읽으면 없음) → 500 「확인하지 못했어요」 · Saved.state_error ·
    permission None · CSRF 새 값 · 한 번용 완료. 실제 security는 부르지 않는다."""
    calls = []

    def runner(argv, stdin):
        calls.append(argv[:2])
        return (0, "") if argv[1] == "-i" else (44, "")
    once = dashboard.Once("key", threading.Event())
    s = serve(store=Keychain(runner), once=once)
    c0 = s.connect.csrf
    status, _, body = post(s, "key")
    assert status == 500 and web.SAVED_UNSURE in body and s.connect.csrf != c0
    assert once.event.wait(WAIT) and once.done and once.result.permission is None and "저장하지 못했습니다" in once.result.state_error
    assert ["security", "-i"] in calls and copy_violations(body) == []


def test_lock_busy_writes_nothing(serve):
    """다른 프로세스가 connections.lock을 쥔 동안 key · telegram POST → 503 · 키 보관 빈 채 · connections.json 바이트 그대로 · CSRF 그대로,
    풀린 뒤 같은 폼으로 다시 내면 저장."""
    s = serve()
    _linked(s.root)
    before, c0 = conns_bytes(s.root), s.connect.csrf
    p = hold_lock(s.root, 30)  # 거절 둘(각 5초쯤)보다 길게, 끝에 kill
    try:
        for kind in ("key", "telegram"):
            status, _, body = post(s, kind)
            assert status == 503 and dashboard.BUSY_TEXT in body, kind
        assert s.store.items == {} and conns_bytes(s.root) == before and s.connect.csrf == c0
    finally:
        p.kill()
        p.wait(WAIT)
    assert post(s, "key", csrf=c0)[0] == 200 and s.store.items["binance:main"] == Credentials(KEY, SECRET)


def test_link_aborts_when_token_replaced_midway(serve):
    """잇기가 숫자를 기다리는 동안 연결 화면 telegram POST로 새 토큰 → 잇기는 대화를 적지도 보내지도 않음(종료 2).
    잇기가 먼저 끝나 옛 대화를 적은 뒤 POST가 오면 POST가 지워 끝 상태는 같다(새 토큰 · 대화 없음)."""
    store = MemStore({TELEGRAM_ACCOUNT: Credentials(TG_TOKEN, "")})
    s = serve(store=store)
    fake = SlowTelegram([private_update("482913", 4242)])
    t, _, got = _link_in_thread(s.root, store, fake)
    try:
        assert fake.polling.wait(WAIT)
        assert post(s, "telegram", fields={"bot_token": TG2})[0] == 200
    finally:
        fake.go.set()
    t.join(WAIT)
    assert not t.is_alive() and got["code"] == 2 and settings(s.root)["chat_id"] is None and fake.sent() == []
    store.save(TELEGRAM_ACCOUNT, Credentials(TG_TOKEN, ""))
    fake = SlowTelegram([private_update("482913", 4242)])
    t, _, got = _link_in_thread(s.root, store, fake)
    fake.go.set()
    t.join(WAIT)
    assert got["code"] == 0 and settings(s.root)["chat_id"] == 4242
    assert post(s, "telegram", fields={"bot_token": TG2})[0] == 200
    assert settings(s.root)["chat_id"] is None and store.items[TELEGRAM_ACCOUNT] == Credentials(TG2, "")


def test_link_aborts_after_partial_token_save(serve):
    """보관은 됐는데 응답 전에 끊긴 telegram POST 뒤에도, 진행 중이던 옛 잇기는 대화를 적지 않고 보내지 않는다."""
    store = MemStore({TELEGRAM_ACCOUNT: Credentials(TG_TOKEN, "")})
    s = serve(store=store)
    break_pipe(s.server)
    fake = SlowTelegram([private_update("482913", 4242)])
    t, _, got = _link_in_thread(s.root, store, fake)
    try:
        assert fake.polling.wait(WAIT)
        post_ignoring_disconnect(s, "telegram", fields={"bot_token": TG2})
    finally:
        fake.go.set()
    t.join(WAIT)
    assert got["code"] == 2 and settings(s.root)["chat_id"] is None and fake.sent() == [] and store.items[TELEGRAM_ACCOUNT] == Credentials(TG2, "")


def _proc_keys(path, n):
    """다른 프로세스: 같은 FileStore에 키 n개를 저장한다(잠금 순서 마지막 칸만 잡음)."""
    _proc_filestore(path, "d", n)


def test_lock_order_no_deadlock(serve, tmp_path):
    """key POST · run_fetch · set_notify · link_telegram 끝 · 다른 프로세스 FileStore.save를 섞어 돌려도 모두 시간 안에 끝나고 갱신이 모두 남는다."""
    fs = FileStore(tmp_path / "cfg" / "secrets.json")
    fs.save(TELEGRAM_ACCOUNT, Credentials(TG_TOKEN, ""))
    s = serve(store=fs)
    errors = []

    def keys():
        for i in range(3):
            for _ in range(20):
                if post(s, "key", fields={"api_key": f"K{i}" * 10, "secret_key": SECRET})[0] == 200:
                    break
            else:
                errors.append("key")

    def fetches():
        for _ in range(3):
            run_fetch(s.root, MemStore({"binance:main": CREDS}), lambda m, tn, c: FakeClient(m, tn, routes()), label="main", testnet=False,
                      markets=["spot"], symbols=["BTCUSDT"], since=None, now=NOW)

    def notifies():
        for q in ("바뀔때만", "매일", "바뀔때만"):
            set_notify(s.root, quiet=q)

    def links():
        assert link_telegram(s.root, fs, lambda tok: Bot(tok, FakeTelegram([private_update("1", 77)])), lambda x: None, NOW, wait_s=30, code="1") == 0

    proc = _spawn(_proc_keys, fs.path, 10)
    threads = [threading.Thread(target=f) for f in (keys, fetches, notifies, links)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(WAIT * 6)
        assert not t.is_alive()
    _join(proc)
    conns = load_connections(s.root)
    assert errors == [] and fs.load("binance:main") == Credentials("K2" * 10, SECRET) and all(fs.load(f"d{i}") for i in range(10))
    assert conns["binance"]["main"]["symbols"] == {"spot": ["BTCUSDT"]} and conns["telegram"]["quiet"] == "바뀔때만"
    assert conns["telegram"]["chat_id"] == 77 and conns["binance"]["main"]["permission"]["status"] == "read_only"


def _refused_409(s):
    """세 POST가 모두 409 · 쓰기 0."""
    before = conns_bytes(s.root)
    for kind in ("key", "telegram", "choose"):
        status, _, body = post(s, kind)
        assert status == 409 and dashboard.GITIGNORE_TEXT in body, kind
    assert s.store.items == {} and conns_bytes(s.root) == before


def test_post_refused_when_gitignore_changed(serve, tmp_path):
    """.gitignore 지움 · 링크(같은 내용 파일을 가리켜도) · 옛 판(우리 머리줄 · 다른 내용) → 409 · 쓰기 0."""
    s = serve()
    gi = s.root / ".gitignore"
    gi.unlink()
    _refused_409(s)
    (tmp_path / "gi").write_text(GITIGNORE)
    gi.symlink_to(tmp_path / "gi")
    _refused_409(s)
    gi.unlink()
    gi.write_text(GITIGNORE.splitlines()[0] + "\n*\n!strategies/\n")
    _refused_409(s)


def test_post_refused_when_gitignore_rules_removed(serve, tmp_path, monkeypatch):
    """우리 머리줄은 두고 `*` 줄만 지움 · 머리줄 한 줄만 → 409. 고객이 쓴 .gitignore(우리 머리줄 아님) → 저장(ensure()와 같음).
    connect --status(ensure를 부름, cli.py) 뒤 같은 POST → 저장."""
    s = serve()
    gi = s.root / ".gitignore"
    gi.write_text(GITIGNORE.replace("*\n", "", 1))
    _refused_409(s)
    gi.write_text(GITIGNORE.splitlines()[0] + "\n")
    _refused_409(s)
    monkeypatch.setattr(cli, "default_store", MemStore)  # 실제 키체인 0
    monkeypatch.chdir(s.root.parent)
    assert cli.main(["connect", "--status"]) == 0
    assert gi.read_text() == GITIGNORE
    assert post(s, "key")[0] == 200
    gi.write_text("# 내 규칙\n*\n")
    assert post(s, "telegram")[0] == 200 and gi.read_text() == "# 내 규칙\n*\n"


def test_post_is_405_outside_connect(serve):
    """POST가 되는 경로는 /connect/key · telegram · choose 셋뿐: 그 밖 POST는 405 + Allow: GET, HEAD, 그 셋의 PUT · DELETE · PATCH · OPTIONS는 405 + Allow: POST."""
    s = serve()
    for path in ("/", "/s/x", "/s/x/retro", "/export.csv", "/connect", "/connect/other", "/connect/key/x"):
        status, h, _ = req(s.port, "POST", f"{path}?t={s.token}", "csrf=x", FORM_HDR)
        assert status == 405 and h.get("Allow") == "GET, HEAD" and has_headers(h), path
    for method in ("PUT", "DELETE", "PATCH", "OPTIONS"):
        status, h, _ = req(s.port, method, f"/connect/key?t={s.token}", "csrf=x", FORM_HDR)
        assert status == 405 and h.get("Allow") == "POST", method  # 이 경로는 GET이 404라 받는 방법은 POST뿐(RFC 9110 §15.5.6)
    assert s.store.items == {}


def test_405_text_does_not_call_the_screen_read_only(serve):
    """405 본문(웹 글)은 화면이 읽기만 한다고 말하지 않는다: 연결 주소는 POST를 받는다. 문구 규칙 0."""
    s = serve()
    for method, path in (("PUT", "/connect/key"), ("POST", "/connect"), ("DELETE", "/"), ("POST", "/s/x")):
        status, _, body = req(s.port, method, f"{path}?t={s.token}", "csrf=x", FORM_HDR)
        assert "읽기" not in body and status == 405 and dashboard.METHOD_TEXT in body and copy_violations(body) == [], (method, path)


def test_connect_post_404_without_connect_config(tmp_path):
    """connect=None이면 POST /connect/* 도 404."""
    server = dashboard.make_server(repo(tmp_path), 0)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    try:
        for kind in ("key", "telegram", "choose"):
            assert req(server.server_address[1], "POST", f"/connect/{kind}?t={server.token}", "csrf=x", FORM_HDR)[0] == 404
    finally:
        server.shutdown()
        server.server_close()


def test_no_request_log_on_post(serve, capfd):
    """POST(성공 · 거절 · 413)에도 표준 출력 · 오류에 아무것도 없다(접속 토큰 · 키 0)."""
    s = serve()
    post(s, "key")
    post(s, "key", csrf="x")
    req(s.port, "POST", f"/connect/key?t={s.token}", "", FORM_HDR)
    out, err = capfd.readouterr()
    assert out == "" and err == ""


def test_connect_page_copy_rules_post_states(serve):
    """실패 줄 · 저장 한 장(상주 · 한 번용) · 부분 실패 · 거절 한 줄들(403 · 409 · 413)도 문구 규칙 0."""
    s = serve(check_key=lambda c: Permission("rejected", "x"))
    pages = [post(s, "key")[2], post(s, "key", fields={"api_key": "x", "secret_key": "y"})[2], post(s, "key", csrf="x")[2],
             req(s.port, "POST", f"/connect/key?t={s.token}", "", FORM_HDR)[2]]
    s2 = serve()
    pages += [post(s2, k)[2] for k in ("key", "telegram", "choose")]
    (s2.root / "connections.json").write_text("{")
    pages.append(post(s2, "key")[2])
    (s2.root / ".gitignore").unlink()
    pages.append(post(s2, "key")[2])
    once = dashboard.Once("telegram", threading.Event())
    s3 = serve(once=once)
    pages += [post(s3, "key")[2], post(s3, "telegram")[2]]
    for body in pages:
        assert copy_violations(body) == [], body[-400:]


# ── 쓰기를 시작한 뒤의 예외 · 보관 잠금 상한 · 한 번용 라우트 ─────────────────────

def _near_cap(root, room=40):
    """connections.json을 읽기 상한(1MiB)보다 room바이트 작게 쓴다(저장 형식 그대로). 고르기 저장 뒤에는 상한을 넘는다(경계)."""
    pad = workspace.JSON_CAP - len(json.dumps({"padding": ""}, ensure_ascii=False, indent=2, sort_keys=True) + "\n") - room
    workspace.save_connections(root, {"padding": "x" * pad})
    assert (root / "connections.json").stat().st_size == workspace.JSON_CAP - room


def test_choose_saved_even_if_reread_after_write_fails(serve, tmp_path):
    """고르기 저장 뒤 connections.json이 읽기 상한을 넘어 다시 읽기가 실패해도 저장됨 · 200 · CSRF 새 값 · 옛 CSRF 재전송 403,
    한 번용이면 once 완료 · event. 대시보드 길은 터미널용 다시 읽기(status_lines)를 부르지 않는다."""
    s = serve()
    _near_cap(s.root)
    c0 = s.connect.csrf
    status, _, body = post(s, "choose", fields={"sensitivity": "둔감", "quiet": "바뀔때만"})
    saved = json.loads((s.root / "connections.json").read_text())
    assert status == 200 and (saved["defaults"]["sensitivity"], saved["telegram"]["quiet"]) == ("둔감", "바뀔때만")
    assert (s.root / "connections.json").stat().st_size > workspace.JSON_CAP and s.connect.csrf != c0
    assert post(s, "choose", csrf=c0)[0] == 403
    once = dashboard.Once("choose", threading.Event())
    (tmp_path / "once").mkdir()
    o = serve(root=repo(tmp_path / "once"), once=once)
    _near_cap(o.root)
    assert post(o, "choose", fields={"sensitivity": "둔감", "quiet": "바뀔때만"})[0] == 200
    assert once.event.wait(WAIT) and once.done and once.result.kind == "choose"


class OddStore(MemStore):
    """보관 save가 목록 밖 예외(ValueError) 또는 글 없는 OSError를 낸다(쓴 뒤 검증에서 났을 수도 있음)."""

    def __init__(self, exc, items=None):
        super().__init__(items)
        self.exc = exc

    def save(self, account, creds):
        super().save(account, creds)
        raise self.exc


@pytest.mark.parametrize("kind", ["key", "telegram"])
@pytest.mark.parametrize("exc", [ValueError("검증 실패(흉내)"), OSError()], ids=["ValueError", "empty-OSError"])
def test_any_exception_after_store_save_is_uncertain_write(serve, kind, exc):
    """store.save가 어떤 예외든(목록 밖 · 글 없는 것) → 500 「확인하지 못했어요」 · permission None · state_error 있음 ·
    CSRF 새 값 · 한 번용 완료 · event · 옛 CSRF 재전송 403."""
    once = dashboard.Once(kind, threading.Event())
    s = serve(store=OddStore(exc), once=once)
    status, _, body = post(s, kind)
    assert status == 500 and web.SAVED_UNSURE in body
    assert once.event.wait(WAIT) and once.done and once.result.permission is None and once.result.state_error
    r = serve(root=s.root, store=OddStore(exc))
    c0 = r.connect.csrf
    assert post(r, kind)[0] == 500 and r.connect.csrf != c0 and post(r, kind, csrf=c0)[0] == 403


def test_any_exception_after_key_save_is_partial(serve, monkeypatch):
    """키 보관 뒤 연결 상태 단계가 목록 밖 예외(RuntimeError) → 500 「키는 저장했어요 …」 · 키 보관됨 · CSRF 새 값 · 한 번용 event."""
    import blackclip.fetch as fetch_mod

    def boom(*a, **k):
        raise RuntimeError("x")
    monkeypatch.setattr(fetch_mod, "note_permission", boom)
    once = dashboard.Once("key", threading.Event())
    s = serve(once=once)
    c0 = s.connect.csrf
    status, _, body = post(s, "key")
    assert status == 500 and web.SAVED_PARTIAL in body and s.store.items["binance:main"] == Credentials(KEY, SECRET)
    assert s.connect.csrf != c0 and once.event.wait(WAIT) and once.result.permission == OK and once.result.state_error


def _hold_filestore(path, seconds):
    """다른 프로세스가 FileStore 보관 잠금(`<보관>.lock`)을 seconds초 쥔다(쥔 뒤 돌려줌)."""
    code = ("import fcntl,os,time,sys\n"
            f"fd = os.open({str(path) + '.lock'!r}, os.O_RDWR | os.O_CREAT, 0o600)\nfcntl.flock(fd, fcntl.LOCK_EX)\n"
            f"print('held', flush=True)\ntime.sleep({seconds})\n")
    p = subprocess.Popen([sys.executable, "-c", code], stdout=subprocess.PIPE, text=True)
    assert p.stdout.readline().strip() == "held"
    return p


def test_filestore_lock_wait_is_bounded(serve, tmp_path):
    """다른 프로세스가 보관 잠금을 쥔 동안 key POST → 시간 안에 503(쓰기 0 · 보관 그대로 · CSRF 그대로).
    FileStore.save를 바로 부르면 KeystoreError 하위(KeystoreBusy). 풀린 뒤 같은 폼은 저장."""
    fs = FileStore(tmp_path / "cfg" / "secrets.json")
    fs.save("other", CREDS)
    s = serve(store=fs)
    before, c0 = fs.path.read_bytes(), s.connect.csrf
    p = _hold_filestore(fs.path, 30)
    try:
        t0 = time.monotonic()
        status, _, body = post(s, "key")
        assert status == 503 and dashboard.BUSY_TEXT in body and time.monotonic() - t0 < WAIT
        assert fs.path.read_bytes() == before and s.connect.csrf == c0 and fs.load("binance:main") is None
        from blackclip.keystore import KeystoreBusy

        with pytest.raises(KeystoreBusy):
            fs.save("x", CREDS)
    finally:
        p.kill()
        p.wait(WAIT)
    assert post(s, "key", csrf=c0)[0] == 200 and fs.load("binance:main") == Credentials(KEY, SECRET)


def test_once_server_serves_only_connect(serve, tmp_path):
    """한 번용 서버는 GET /connect와 그 kind POST만. 홈 · 가드레일 · 회고 · 내보내기 GET은 404(내비 있는 화면 0)."""
    s = serve(once=dashboard.Once("key", threading.Event()))
    (s.root / "strategies" / "s1").mkdir(parents=True)
    (s.root / "strategies" / "s1" / "retro.md").write_text("# 실행 회고 · x\n")
    assert get(s)[0] == 200
    for path in ("/", "/s/s1", "/s/s1/retro", "/export.csv", "/export.json"):
        assert get(s, path)[0] == 404, path
    assert get(serve(root=s.root), "/s/s1/retro")[0] == 200  # 상주 서버는 그대로


# ── 쓴 뒤 잠금을 빠져나오다 난 예외도 「확인 못함」 ───────────────────────────────

@pytest.mark.parametrize("kind", ["choose", "key", "telegram"])
def test_lock_exit_error_after_write_is_uncertain(serve, tmp_path, monkeypatch, kind):
    """저장을 마친 뒤 연결 잠금을 빠져나오다(os.close 등) OSError가 나도 「쓰기 0」 길로 가지 않는다.
    저장됨 · 500 「확인하지 못했어요」 · CSRF 새 값 · 옛 CSRF 재전송 403, 한 번용이면 once 완료 · event."""
    from contextlib import contextmanager

    real = workspace.lock

    @contextmanager
    def bad_exit(root, name, wait_s=0, sleep=time.sleep):
        with real(root, name, wait_s, sleep):
            yield
        raise OSError(9, "Bad file descriptor")
    monkeypatch.setattr(workspace, "lock", bad_exit)  # update_connections(choose)가 쓰는 이름
    monkeypatch.setattr(dashboard, "lock", bad_exit)  # connect_post(key · telegram)가 쓰는 이름

    def saved(s):
        if kind == "choose":
            return json.loads((s.root / "connections.json").read_text())["defaults"]["sensitivity"] == "둔감"
        return s.store.items.get("binance:main" if kind == "key" else TELEGRAM_ACCOUNT) is not None

    fields = {"sensitivity": "둔감", "quiet": "매일"} if kind == "choose" else None
    s = serve()
    c0 = s.connect.csrf
    status, _, body = post(s, kind, fields=fields)
    assert status == 500 and web.SAVED_UNSURE in body and saved(s) and s.connect.csrf != c0
    assert post(s, kind, fields=fields, csrf=c0)[0] == 403
    once = dashboard.Once(kind, threading.Event())
    (tmp_path / "once").mkdir()
    o = serve(root=repo(tmp_path / "once"), once=once)
    assert post(o, kind, fields=fields)[0] == 500 and saved(o)
    assert once.event.wait(WAIT) and once.done and once.result.kind == kind and once.result.state_error


# ── 묶음 고침(화면 증거 · 접근성 감사에서 나온 결함) ───────────────

KIND_IDS = ("key", "telegram", "choose")
"""연결 화면 패널 셋(화면 위에서 아래 순서)."""


def _radios(body, name):
    """고르기 폼에서 checked인 라디오 값들(name 칸)."""
    return re.findall(rf'<input type="radio" name="{name}" value="([^"]*)" required checked>', body)


def _invalid(body):
    """`aria-invalid="true"`가 붙은 입력 칸들 → [(칸 이름, aria-describedby, autofocus 유무)] (문서 순서)."""
    return [(re.search(r'name="(\w+)"', t)[1], re.search(r'aria-describedby="([^"]+)"', t)[1], " autofocus" in t)
            for t in re.findall(r'<input[^>]*aria-invalid="true"[^>]*>', body)]


def test_choose_failure_redraws_submitted_choices(serve):
    """WCAG 3.3.7: 고르기가 400이면 폼은 방금 고른 민감도 · 조용한 날로 다시 그려진다. 저장 파일 · 위쪽 상태 칸은 그대로, 헬스체크는 다시 싣지 않는다."""
    s = serve()
    status, _, body = post(s, "choose", fields={"sensitivity": "둔감", "quiet": "바뀔때만", "healthcheck": "http://not-https.example"})
    assert status == 400 and "https로 시작하는" in body and _radios(body, "sensitivity") == ["둔감"] and _radios(body, "quiet") == ["바뀔때만"]
    assert "not-https" not in body and conns_bytes(s.root) is None  # 저장 0, 쓰기 전용
    assert "<dt>민감도</dt><dd>보통" in body and "<dt>조용한 날</dt><dd>매일 한 줄" in body  # 위쪽 「연결 상태」는 저장된 값
    page = get(s)[1]
    assert _radios(page, "sensitivity") == ["보통"] and _radios(page, "quiet") == ["매일"]
    # 조작된 폼: 허용 목록 밖 값만 저장된 값으로, 허용된 값은 제출한 대로
    _, _, body = post(s, "choose", fields={"sensitivity": "아주민감", "quiet": "바뀔때만"})
    assert _radios(body, "sensitivity") == ["보통"] and _radios(body, "quiet") == ["바뀔때만"]
    _, _, body = post(s, "choose", fields={"sensitivity": "민감", "quiet": "가끔"})
    assert _radios(body, "sensitivity") == ["민감"] and _radios(body, "quiet") == ["매일"]
    assert conns_bytes(s.root) is None


def test_choose_failure_redraws_submitted_execution_alert(serve):
    """실행 경고 체크(첫 회고가 있어 칸이 보일 때)도 제출한 값으로 다시 그려진다. 저장된 켬 · 끔과 반대로 보내도 제출한 쪽."""
    s = serve()
    (s.root / "strategies" / "s1" / "daily").mkdir(parents=True)
    (s.root / "strategies" / "s1" / "daily" / "2026-10-15.md").write_text("x")
    bad = {"sensitivity": "보통", "quiet": "매일", "healthcheck": "http://x", "ea_shown": "1"}
    status, _, body = post(s, "choose", fields=bad | {"execution_alerts": "on"})  # 저장은 꺼짐인데 켜서 보냄
    assert status == 400 and 'name="execution_alerts" value="on" checked>' in body
    assert post(s, "choose", fields=bad | {"healthcheck": "", "execution_alerts": "on"})[0] == 200  # 저장: 켬
    _, _, body = post(s, "choose", fields=bad)  # 저장은 켜짐인데 끄고 보냄
    assert 'name="execution_alerts" value="on">' in body and 'value="on" checked' not in body
    assert 'value="on" checked' in get(s)[1]


def test_error_responses_are_documents(serve):
    """WCAG 3.1.1 · 2.4.2: 연결 POST 거절 응답도 lang · 본문 한 줄과 같은 제목 · main이 있다(제목이 상태 숫자가 아니다)."""
    s = serve()
    doc = re.compile(r'<!doctype html><html lang="ko"><meta charset="utf-8"><meta name="viewport" content="[^"]+">'
                     r'<title>([^<]+)</title><main><p>([^<]+)</p></main></html>')
    c0 = s.connect.csrf
    cases = [(post(s, "key", csrf="wrong"), dashboard.CSRF_TEXT), (post(s, "key", token="wrong"), dashboard.FORBIDDEN_TEXT),
             (req(s.port, "POST", f"/connect/key?t={s.token}", "", FORM_HDR), dashboard.TOO_BIG_TEXT),
             (req(s.port, "GET", f"/nope?t={s.token}"), dashboard.NOT_FOUND_TEXT),
             (req(s.port, "PUT", f"/connect/key?t={s.token}"), dashboard.METHOD_TEXT)]
    for (status, h, body), line in cases:
        m = doc.fullmatch(body)
        assert m and m[1] == m[2] == line and not m[1].isdigit() and has_headers(h), (status, body)
    assert s.connect.csrf == c0 and s.store.items == {}


def test_405_allow_lists_what_the_path_takes(serve, tmp_path):
    """RFC 9110 §15.5.6: 405의 Allow는 그 경로가 지금 받는 방법이다. POST 경로 셋은 GET이 404라 POST뿐, 그 밖은 GET, HEAD.
    한 번용 서버도 같고, 연결 설정이 없는 서버의 연결 경로는 받는 방법이 없어 전과 같다."""
    def allow(s, method, path):
        status, h, _ = req(s.port, method, f"{path}?t={s.token}", "csrf=x", FORM_HDR)
        assert status == 405, (method, path)
        return h["Allow"]

    for s in (serve(), serve(once=dashboard.Once("key", threading.Event()))):
        for path in ("/connect/key", "/connect/telegram", "/connect/choose"):
            assert [req(s.port, m, f"{path}?t={s.token}")[0] for m in ("GET", "HEAD")] == [404, 404], path  # GET이 안 되니 POST뿐
            assert {allow(s, m, path) for m in ("PUT", "DELETE", "PATCH", "OPTIONS")} == {"POST"}, path
        for path in ("/", "/connect", "/s/x", "/connect/other", "/connect/key/x"):
            assert allow(s, "POST", path) == allow(s, "PUT", path) == "GET, HEAD", path
    (tmp_path / "plain").mkdir()
    server = dashboard.make_server(repo(tmp_path / "plain"), 0)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    try:
        s = SimpleNamespace(port=server.server_address[1], token=server.token)
        assert allow(s, "PUT", "/connect/key") == "GET, HEAD"
    finally:
        server.shutdown()
        server.server_close()


@pytest.mark.parametrize("kind, fields, flagged, line", [
    ("key", {"api_key": "short", "secret_key": SECRET}, [("api_key", True), ("secret_key", False)], "키 모양이 달라요"),
    ("telegram", {"bot_token": "abc"}, [("bot_token", True)], "토큰 모양이 달라요"),
    ("choose", {"sensitivity": "보통", "quiet": "매일", "healthcheck": "http://x"}, [("healthcheck", True)], "https로 시작하는"),
    ("choose", {"sensitivity": "아주민감", "quiet": "매일"}, [], "하나씩 골라 주세요"),
    ("choose", {"sensitivity": "보통", "quiet": "매일", "ea_shown": "1", "execution_alerts": "on"}, [], "첫 회고를 본 뒤에"),
], ids=["key", "telegram", "healthcheck", "unknown-choice", "execution-alert-without-retro"])
def test_connect_failure_line_is_tied_to_its_fields(serve, kind, fields, flagged, line):
    """WCAG 3.3.1 · 1.3.1: 실패 줄에 패널별 id, 잘못된 칸(들)에 aria-invalid · aria-describedby=그 id, autofocus는 첫 칸 하나.
    조회 키는 두 칸이 모두 비워지니 둘 다. 칸을 가리키지 않는 실패(모르는 고르기 값 · 첫 회고 없는 실행 경고)는 줄 id만이고 autofocus도 없다."""
    s = serve()
    status, _, body = post(s, kind, fields=fields)
    fail = f"fail-{kind}"
    assert status == 400 and re.search(rf'<p class="fail" role="status" id="{fail}">[^<]*{line}', body) and body.count(f'id="{fail}"') == 1
    assert _invalid(body) == [(n, fail, first) for n, first in flagged] and body.count(" autofocus") == (1 if flagged else 0)
    assert all(f'id="fail-{k}"' in body for k in KIND_IDS) and all("value=" not in t for t in _inputs(body))  # 줄 id는 패널마다, 쓰기 전용은 그대로


def test_connect_failure_ties_rejected_key_and_once_screen(serve):
    """주문 권한이 켜진 키(200)도 두 칸에 같은 연결, 한 번용 화면(패널 하나)도 같다. 실패가 없는 화면에는 aria-invalid · autofocus가 없고 줄 id는 늘 있다."""
    s = serve(check_key=lambda c: Permission("rejected", "조회 말고 켜진 권한: enableWithdrawals"))
    status, _, body = post(s, "key")
    assert status == 200 and _invalid(body) == [("api_key", "fail-key", True), ("secret_key", "fail-key", False)]
    page = get(s)[1]
    assert "aria-invalid" not in page and " autofocus" not in page and all(f'id="fail-{k}"' in page for k in KIND_IDS)
    o = serve(root=s.root, once=dashboard.Once("telegram", threading.Event()))
    _, _, body = post(o, "telegram", fields={"bot_token": "abc"})
    assert _invalid(body) == [("bot_token", "fail-telegram", True)] and 'id="fail-key"' not in body


def test_connect_failure_flags_execution_alert_checkbox_when_drawn():
    """고르기 실패가 실행 경고 칸을 가리키면(칸이 그려질 때) 체크 칸에 aria-invalid · aria-describedby · autofocus. 칸이 없으면 붙일 입력이 없다."""
    view = {"title": "t", "account": "binance main", "store": "x", "key": {"held": False, "last4": None, "why": None},
            "token": {"held": False, "last4": None, "why": None}, "permission": {"status": None, "checked_at": None, "why": None},
            "coverage": {"spans": [], "why": None}, "linked": False, "setup": None, "setup_why": "x", "retros": [], "choose_why": None,
            "choose": {"sensitivity": "보통", "quiet": "매일", "execution_alerts": False, "healthcheck": False, "first_retro": True}}
    body = web.connect(view, "T", "C", "choose", False, "실패", None, "execution_alerts").decode()
    assert re.search(r'<input type="checkbox" name="execution_alerts" value="on" aria-invalid="true" aria-describedby="fail-choose" autofocus>', body)
    view["choose"]["first_retro"] = False
    assert "aria-invalid" not in web.connect(view, "T", "C", "choose", False, "실패", None, "execution_alerts").decode()


def test_connect_forms_are_named_by_their_panel_heading(serve):
    """WCAG 2.4.6: 「저장」 버튼 셋이 같은 이름이어도 폼마다 패널 제목(h2)이 이름이 된다. 버튼 글은 그대로 「저장」."""
    _, body = get(serve())
    forms = re.findall(r'<form class="form" method="post" action="/connect/(\w+)\?[^"]*" aria-labelledby="([^"]+)">', body)
    assert [k for k, _ in forms] == list(KIND_IDS) and len({i for _, i in forms}) == 3
    for (_, ident), title in zip(forms, ("조회 키", "텔레그램", "고르기")):
        assert f'<h2 class="h2" id="{ident}">{title}</h2>' in body and body.count(f'id="{ident}"') == 1
    assert body.count('type="submit">저장</button>') == 3


def test_connect_required_inputs_are_marked(serve):
    """WCAG 3.3.2: API 키 · 시크릿 · 봇 토큰은 required(라디오와 같음). 헬스체크는 선택이라 아니다."""
    _, body = get(serve())
    tags = {re.search(r'name="(\w+)"', t)[1]: t for t in _inputs(body)}
    assert {n for n, t in tags.items() if re.search(r"\brequired\b", t)} == {"api_key", "secret_key", "bot_token"}


def test_permission_checked_at_is_labelled_utc(serve):
    """권한 확인 시각은 UTC로 적힌 값(`+00:00`)이고 옆의 체결 받은 범위처럼 UTC를 붙여 보인다."""
    s = serve()
    assert post(s, "key")[0] == 200
    assert load_connections(s.root)["binance"]["main"]["permission"]["checked_at"].endswith("+00:00")
    assert re.search(r"<div class=\"sub\">\d{4}-\d{2}-\d{2} \d{2}:\d{2} UTC 확인</div>", get(s)[1])


def test_connect_coverage_times_stay_in_one_piece(serve):
    """「체결 받은 범위」의 날짜 · 시각 낱말(`2026-07-17 00:00`)은 한 덩이로 줄바꿈이 덩이 사이(「~」 뒤 · 시장 이름 뒤)에서만 일어난다. 글자는 그대로다.
    그 칸은 열이 둘 이상일 때만 둘을 차지하고(열이 하나일 때 열을 만들어 넘치지 않게), 열이 덩이보다 좁은 아주 좁은 폭(9rem 미만)에서만 덩이도 끊긴다."""
    s = serve()
    _fills(s.root, 0)
    body = get(s)[1]
    cell = re.search(r'<div class="span2"><dt>체결 받은 범위</dt><dd>(.*?)</dd></div>', body)[1]
    assert re.findall(r'<span class="nw">([^<]*)</span>', cell) == ["2026-07-17 00:00", "2026-10-15 12:00"] and "<wbr>" in cell
    assert unescape(re.sub(r"<[^>]+>", "", cell)) == "현물 2026-07-17 00:00~2026-10-15 12:00 UTC" and body.count('class="span2"') == 1
    css = web.css()
    assert ".nw{white-space:nowrap}" in css and "@container (width < 9rem){.nw{white-space:normal}}" in css
    assert "@container (width >= calc(12rem + 12px)){.span2{grid-column:span 2}}" in css
    assert web.SPACE[2] == 12 and re.search(r"\.pairs\{[^}]*gap:\S+ var\(--studio-space-3\)", css)  # 조건의 12px은 열 간격(컨테이너 조건에는 var()를 못 씀)


# ── 디자인 검토 ─────────────────────────────────────────────────────────

def _panel_of(body, kind):
    """연결 화면의 kind 패널(`key` · `telegram` · `choose`) 안쪽 HTML(제목 id `h-<kind>`)."""
    return next(m for m in re.findall(r'<section class="panel">(.*?)</section>', body, re.S) if f'id="h-{kind}"' in m)


def _before_first_input(panel):
    """패널 제목 `</h2>`부터 첫 `<input` 앞까지의 HTML."""
    return panel[panel.index("</h2>"):re.search(r"<input\b", panel).start()]


STORES = [("file", lambda tmp: FileStore(tmp / "cfg" / "secrets.json"), "이 컴퓨터에서 나만 읽을 수 있는 파일"),
          ("keychain", lambda tmp: Keychain(FakeSecurity()), "이 맥의 키체인"),  # 가짜 security: 실제 키체인 0
          ("other", lambda tmp: MemStore(), "메모리")]
"""보관 이름 세 갈래: 파일 보관 · 키체인 · 그 밖(엔진 이름 그대로)."""


def test_store_words_by_engine_class_names():
    """사람 말 보관 이름은 엔진 클래스의 `name` 속성으로 가른다(글 비교가 아님). 키체인 · 파일 보관 밖은 엔진 이름 그대로.
    엔진의 `name`(터미널 글)은 그대로다."""
    assert web.store_words(Keychain.name) == "이 맥의 키체인" and web.store_words(FileStore.name) == "이 컴퓨터에서 나만 읽을 수 있는 파일"
    assert web.store_words(MemStore.name) == "메모리" and web.store_words("다른 보관") == "다른 보관"
    assert (Keychain.name, FileStore.name) == ("macOS 키체인", "권한 600 파일")  # 터미널 글은 그대로


@pytest.mark.parametrize("label, make, words", STORES, ids=[x[0] for x in STORES])
def test_connect_panels_say_read_only_and_where_it_is_kept(serve, tmp_path, label, make, words):
    """조회 키 패널은 제목과 첫 입력 칸 사이에 「조회 권한만 켠 키」와 「{보관}에만 저장」, 텔레그램 패널은 「{보관}에만 저장」을 둔다.
    상태 줄은 「계정 …, 보관 {보관}」이고, 화면 어디에도 파일 권한 용어 「권한 600」이 없다. 「어디에도 보내지 않아요」 꼴은 쓰지 않는다."""
    s = serve(store=make(tmp_path))
    _, body = get(s)
    key, tg = squash_html(_before_first_input(_panel_of(body, "key"))), squash_html(_before_first_input(_panel_of(body, "telegram")))
    assert key == f"binance main 계정에 조회 권한만 켠 키를 넣어 주세요. 키는 {words}에만 저장하고 바이낸스 조회에만 써요. API 키"  # 끝은 첫 칸의 라벨
    assert tg == (f"토큰은 {words}에만 저장하고 봇 확인, 연결할 채팅 찾기, 알림 보내기에만 써요. "
                  "BotFather가 준 토큰을 넣어 주세요. 새 토큰을 저장하면 전에 이은 대화는 끊겨요. 텔레그램 봇 토큰")
    assert f"계정 binance main, 보관 {words}" in squash_html(body) and "권한 600" not in body and "어디에도 보내지 않" not in body
    assert copy_violations(body) == []
    status, _, saved = post(s, "key")
    assert status == 200 and f"권한 조회만, 보관 {words}." in squash_html(saved) and "권한 600" not in saved and copy_violations(saved) == []
    status, _, saved_tg = post(s, "telegram")
    assert status == 200 and f"보관 {words}." in squash_html(saved_tg) and "권한 600" not in saved_tg and copy_violations(saved_tg) == []


def squash_html(fragment):
    """HTML 조각의 보이는 글(태그 벗김, entity 풀림, 공백 하나로)."""
    return " ".join(unescape(re.sub(r"<[^>]+>", " ", fragment)).split())


def test_other_account_note_is_in_the_page_foot_not_the_panel(serve):
    """「다른 계정이나 테스트넷 키는 …」는 조회 키 패널에서 빠져 쪽 아래 사실 줄로 내려간다(마지막 패널 뒤). 한 번용 화면에는 지금처럼 없다."""
    s = serve()
    _, body = get(s)
    assert web.OTHER_ACCOUNT == "다른 계정이나 테스트넷 키는 AI에게 부탁하면 다른 포트에 한 번용 화면을 열어 줘요."  # 윤문한 글
    assert web.OTHER_ACCOUNT not in _panel_of(body, "key") and body.index(web.OTHER_ACCOUNT) > body.rindex("</section>")
    assert all(x in body[body.rindex("</section>"):] for x in web.CONNECT_FACTS)
    for kind in KIND_IDS:
        _, once = get(serve(root=s.root, once=dashboard.Once(kind, threading.Event())))
        assert web.OTHER_ACCOUNT not in once and all(x in once for x in web.CONNECT_FACTS), kind
        assert "에만 저장" in _before_first_input(_panel_of(once, kind)) or kind == "choose"


def _check_502(c):
    raise BinanceError(503, None, "Service Unavailable")


def _tg_502(c):
    raise TelegramError("텔레그램 응답 모양이 다릅니다(KeyError)")


FAIL_CASES = [
    ("rejected", "key", {"check_key": lambda c: Permission("rejected", "조회 말고 켜진 권한: enableWithdrawals", enabled=("enableWithdrawals",))}, None, 200),
    ("reading-off", "key", {"check_key": lambda c: check(Restrictions({**S1, "enableReading": False}))}, None, 200),
    ("502-key", "key", {"check_key": _check_502}, None, 502),
    ("502-telegram", "telegram", {"check_telegram": _tg_502}, None, 502),
    ("400-key", "key", {}, {"api_key": "short", "secret_key": SECRET}, 400),
]
"""키 · 텔레그램 실패 갈래: 거부 · 조회 꺼짐 · 502 키 · 502 텔레그램 + 키 모양 400. (이름, 패널, serve 인자, 보낼 칸, 상태). 고르기는 아래 따로."""


@pytest.mark.parametrize("label, kind, opts, fields, want", FAIL_CASES, ids=[x[0] for x in FAIL_CASES])
def test_fail_line_sits_between_the_panel_note_and_the_first_input(serve, label, kind, opts, fields, want):
    """키 · 텔레그램 패널의 실패 줄(`role="status"`)은 패널 설명 줄 뒤 · 첫 입력 칸 앞에 있다(버튼 아래가 아님: 1024×768에서는 autofocus된 첫 칸이 이미 보여 스크롤되지 않아
    버튼 아래 줄이 화면 밖이었다. 첫 칸이 잘못된 칸이라 첫 칸 위가 맞다). 그 칸의 `aria-describedby`가 이 줄의 id를 가리키고, 패널마다 줄은 하나다."""
    s = serve(**opts)
    status, _, body = post(s, kind, fields=fields)
    assert status == want and _fail_line(body, kind) != ""
    panel = _panel_of(body, kind)
    fail, first = panel.index('<p class="fail" role="status"'), re.search(r"<input\b", panel).start()
    assert panel.index("</h2>") < fail < first and panel.count('role="status"') == 1
    assert panel.index("</p>") < fail  # 설명 줄(첫 `<p class="sub">`)이 줄보다 앞
    tag = re.search(r"<input\b[^>]*aria-describedby[^>]*>", panel)[0]
    assert f'aria-describedby="fail-{kind}"' in tag and f'id="fail-{kind}"' in panel and copy_violations(body) == []


def test_empty_fail_line_is_one_hidden_status_per_panel(serve):
    """실패가 없을 때는 패널마다 빈 `role="status"` 하나(id는 그대로)가 첫 입력 칸 앞에 있고, 비면 자리를 차지하지 않는다(`.fail:empty{display:none}`, 새 색 0)."""
    _, body = get(serve())
    for kind in KIND_IDS:
        panel = _panel_of(body, kind)
        assert panel.count(f'<p class="fail" role="status" id="fail-{kind}"></p>') == 1 and panel.count('role="status"') == 1
        assert panel.index('role="status"') < re.search(r"<input\b", panel).start()
    css = web.css()
    assert ".fail:empty{display:none}" in css and "min-height" not in re.search(r"\.fail\{([^}]*)\}", css)[1]


# ── 디자인 검토: 고르기 패널의 실패 줄은 잘못된 칸 바로 위 ───────────────────────────────────────────

def _between_fail_and_field(panel):
    """고르기 패널에서 실패 줄(`<p class="fail"`) 시작부터 `aria-describedby`가 붙은 첫 칸 앞까지의 HTML과, 줄 · 칸의 자리."""
    fail = panel.index('<p class="fail" role="status"')
    field = re.search(r"<input\b[^>]*aria-describedby[^>]*>", panel)
    return panel[fail:field.start()], fail, field.start()


def _first_retro(root):
    """첫 회고(`retro.md`)가 있는 레포로 만든다(고르기에 실행 경고 칸이 그려짐)."""
    d = root / "strategies" / "s1"
    d.mkdir(parents=True)
    (d / "retro.md").write_text("# 실행 회고 · x\n", encoding="utf-8")


def test_choose_healthcheck_failure_line_sits_right_before_the_bad_field(serve):
    """고르기의 잘못된 칸(`healthcheck`)은 폼 깊숙이 있어 autofocus 스크롤이 일어난다. 실패 줄이 폼 맨 위에 있으면 위로 밀려 나가므로(320×568에서 화면 밖),
    줄은 그 칸의 `.field` 바로 앞이다: 줄 < `aria-describedby`가 붙은 첫 칸, 그 사이에 다른 `<input` · `<fieldset`이 없고, `id="fail-choose"`는 문서에 하나다."""
    s = serve()
    status, _, body = post(s, "choose", fields={"sensitivity": "보통", "quiet": "매일", "healthcheck": "http://x"})
    assert status == 400 and _fail_line(body, "choose") != "" and body.count('id="fail-choose"') == 1
    panel = _panel_of(body, "choose")
    between, fail, field = _between_fail_and_field(panel)
    assert fail < field and "<input" not in between and "<fieldset" not in between and between.count('role="status"') == 1
    assert panel[field:].startswith('<input class="input" id="healthcheck"') and 'aria-describedby="fail-choose"' in panel[field:field + 300]
    assert panel.index("<fieldset") < fail and panel.count("<fieldset") == 3  # 앞의 두 fieldset(민감도 · 조용한 날) 뒤, 실행 경고 fieldset 앞
    assert panel.index("실행 경고</legend>") > field and " autofocus" in panel[field:field + 300] and copy_violations(body) == []


def test_choose_execution_alert_failure_line_is_inside_its_fieldset(serve):
    """첫 회고가 있어 실행 경고 칸이 그려졌고 그 칸이 잘못된 칸이면, 줄은 그 fieldset 안 첫 자리(legend 다음, 보이지 않는 숨김 칸 뒤 · 체크 칸 앞)다.
    줄 < 체크 칸, 그 사이에 다른 `<input` · `<fieldset` 없음. `id="fail-choose"`는 하나."""
    s = serve()
    _first_retro(s.root)
    body = web.connect(dashboard.connect_view(s.root, s.connect), s.token, s.connect.csrf, "choose", False, "첫 회고를 본 뒤에 켤 수 있어요.", None, "execution_alerts").decode()
    assert body.count('id="fail-choose"') == 1
    panel = _panel_of(body, "choose")
    between, fail, field = _between_fail_and_field(panel)
    assert fail < field and "<input" not in between and "<fieldset" not in between
    assert panel[field:].startswith('<input type="checkbox" name="execution_alerts"') and " autofocus" in panel[field:field + 200]
    assert panel.rindex("<fieldset", 0, fail) > panel.index("조용한 날 알림</legend>") and "실행 경고</legend>" in panel[panel.rindex("<fieldset", 0, fail):fail]
    assert copy_violations(body) == []


@pytest.mark.parametrize("label, fields, bad", [
    ("unknown-choice", {"sensitivity": "아주민감", "quiet": "매일"}, None),
    ("execution-alert-not-drawn", None, "execution_alerts")], ids=["unknown-choice", "execution-alert-not-drawn"])
def test_choose_failure_without_a_field_to_point_at_stays_at_the_top(serve, label, fields, bad):
    """가리키는 칸이 없는 실패(모르는 고르기 값) · 가리켜도 그 칸이 그려지지 않은 실패(첫 회고가 없어 실행 경고 칸 없음)는 지금처럼 폼 맨 위에 둔다
    (첫 fieldset 앞). 가리키는 칸이 없으니 `aria-describedby`와 `autofocus`도 없고, 줄 id는 하나다."""
    s = serve()
    if fields is not None:
        status, _, body = post(s, "choose", fields=fields)
        assert status == 400
    else:
        body = web.connect(dashboard.connect_view(s.root, s.connect), s.token, s.connect.csrf, "choose", False, "첫 회고를 본 뒤에 켤 수 있어요.", None, bad).decode()
    assert body.count('id="fail-choose"') == 1
    panel = _panel_of(body, "choose")
    assert panel.index('<form class="form"') < panel.index('<p class="fail" role="status"') < panel.index("<fieldset")
    assert re.search(r'<form class="form"[^>]*><p class="fail" role="status" id="fail-choose">', panel) and " autofocus" not in panel and "aria-describedby" not in panel
    assert copy_violations(body) == []


def test_choose_without_failure_has_one_empty_status_at_the_top_of_the_form(serve):
    """실패가 없을 때는 고르기 폼 맨 위에 빈 `role="status"` 하나. 한 번용 고르기 화면도 같다. 키 · 텔레그램은 그대로(첫 칸 위)."""
    s = serve()
    for body in (get(s)[1], get(serve(root=s.root, once=dashboard.Once("choose", threading.Event())))[1]):
        panel = _panel_of(body, "choose")
        assert body.count('id="fail-choose"') == 1
        assert re.search(r'<form class="form"[^>]*><p class="fail" role="status" id="fail-choose"></p><fieldset', panel)


def test_rejected_names_in_words(serve):
    """켜진 권한은 바이낸스 필드 이름이 아니라 화면 말로(표에 있는 것은 이름, 순서는 표 순서)."""
    s = serve(check_key=lambda c: check(Restrictions({**S1, "enableWithdrawals": True, "enableSpotAndMarginTrading": True})))
    _, _, body = post(s, "key")
    assert "출금, 현물과 마진 거래" in _fail_line(body) and "enable" not in body and copy_violations(body) == []


@pytest.mark.parametrize("extra", [{"permitsUniversalTransfer": True}, {"enableFoo": True}])
def test_rejected_first_line_says_only_the_condition(serve, extra):
    """출금도 거래도 아닌 권한(계정 간 이체만, 표에 없는 필드만)만 켠 키도 첫 문장이 사실(「주문이나 출금」이라 단정하지 않음)."""
    s = serve(check_key=lambda c: check(Restrictions({**S1, **extra})))
    _, _, body = post(s, "key")
    assert _fail_line(body).split("<br>")[0] == dashboard.REJECTED_TEXT and "주문이나 출금" not in _fail_line(body)


def test_rejected_names_follow_table_order(serve):
    """켜진 권한은 표(위험 순) 순서로, 현물 + 마진 키에서 「마진」 둘이 서로 다른 항목으로 읽힌다."""
    s = serve(check_key=lambda c: check(Restrictions({**S1, **{n: True for n in dashboard.PERMISSION_WORDS}, "enableFoo": True})))
    _, _, body = post(s, "key")
    assert _fail_line(body).split("<br>")[1].startswith(f"켜진 권한: {', '.join(dashboard.PERMISSION_WORDS.values())}, 그 밖의 권한 1개.")
    s = serve(check_key=lambda c: check(Restrictions({**S1, "enableMargin": True, "enableSpotAndMarginTrading": True})))
    _, _, body = post(s, "key")
    assert _fail_line(body).split("<br>")[1].startswith("켜진 권한: 현물과 마진 거래, 마진 대출(상환과 이체 포함).")


def test_rejected_unknown_field_hidden(serve):
    """표에 없는 새 권한은 이름을 싣지 않고 개수로만(모르는 영문 필드가 화면에 나가지 않게)."""
    s = serve(check_key=lambda c: check(Restrictions({**S1, "enableFoo": True, "enableBar": True, "enableWithdrawals": True})))
    _, _, body = post(s, "key")
    assert "출금, 그 밖의 권한 2개" in _fail_line(body) and "enableFoo" not in body and "enableBar" not in body


def test_rejected_line_two_sentences(serve):
    """첫 줄은 「…저장하지 않았어요.」 한 문장, 켜진 권한과 다시 만드는 안내는 둘째 줄."""
    s = serve(check_key=lambda c: check(Restrictions({**S1, "enableFutures": True})))
    _, _, body = post(s, "key")
    first, second = _fail_line(body).split("<br>")
    assert first == dashboard.REJECTED_TEXT
    assert second == "켜진 권한: 선물. 바이낸스에서 조회만 켠 키를 새로 만들어 넣어 주세요."


def test_tg502_no_permission_word(serve):
    """텔레그램 확인 실패 줄에는 「권한」이 없고 봇 확인 실패라고 말한다."""
    s = serve(check_telegram=lambda c: (_ for _ in ()).throw(TelegramError("x")))
    status, _, body = post(s, "telegram")
    line = _fail_line(body, "telegram")
    assert status == 502 and "권한" not in line and line == f"{dashboard.TG_FAIL_TEXT} 잠시 뒤 다시 넣어 주세요."
    assert dashboard.TG_FAIL_TEXT == "봇을 확인하지 못해 저장하지 않았어요."


def test_telegram_panel_uses(serve):
    """텔레그램 패널은 토큰을 실제로 쓰는 세 곳(getMe · getUpdates · sendMessage)만 말한다."""
    assert "봇 확인, 연결할 채팅 찾기, 알림 보내기에만 써요" in get(serve())[1]

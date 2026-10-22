"""blackclip dashboard 서버 · 보안 최소선: 127.0.0.1 · Host · Origin · 접속 토큰 · 머리글 · 로그 없음 · CLI."""
import errno
import http.client
import json
import re
import socket
import threading

import pytest
from conftest import MemStore
from facts import write_facts

import blackclip.cli as cli
from blackclip import dashboard
from blackclip.cli import main
from blackclip.connect import BAD_REQUEST_TEXT, HEADERS, request_allowed


@pytest.fixture(autouse=True)
def _home(monkeypatch, tmp_path):
    monkeypatch.setenv("HOME", str(tmp_path / "home"))


@pytest.fixture
def start(tmp_path):
    """레포(.blackclip)를 받아 대시보드 서버를 띄우고 (server, port, token)을 돌려준다. 끝에 shutdown · server_close."""
    servers = []

    def _start(root=None):
        server = dashboard.make_server(root or write_facts(tmp_path), 0)
        threading.Thread(target=server.serve_forever, daemon=True).start()
        servers.append(server)
        return server, server.server_address[1], server.token

    yield _start
    for s in servers:
        s.shutdown()
        s.server_close()


def req(port, method, path, headers=None):
    """요청 하나 → (상태, 머리글, 본문). headers에 Host가 있으면 http.client가 따로 붙이지 않는다."""
    c = http.client.HTTPConnection("127.0.0.1", port, timeout=5)
    c.request(method, path, headers=headers or {})
    r = c.getresponse()
    out = r.status, dict(r.getheaders()), r.read().decode()
    c.close()
    return out


def raw(port, data: bytes):
    """날 바이트 요청 하나 → (상태, 머리글, 본문). 서버가 닫을 때까지 읽는다."""
    with socket.create_connection(("127.0.0.1", port), timeout=5) as s:
        s.sendall(data)
        s.shutdown(socket.SHUT_WR)  # 요청 끝을 알린다(3.12까지는 HTTP/0.9 요청에도 머리글 끝까지 읽어 기다림)
        got = b""
        while chunk := s.recv(65536):
            got += chunk
    head, _, body = got.partition(b"\r\n\r\n")
    lines = head.decode("latin-1").split("\r\n")
    return int(lines[0].split()[1]), dict(x.split(": ", 1) for x in lines[1:]), body.decode()


def has_headers(h: dict) -> bool:
    """connect.HEADERS 여섯 머리글이 모두 그 값으로 붙었는지."""
    return all(h.get(k) == v for k, v in HEADERS.items())


def test_binds_loopback_only(start):
    server, _, _ = start()
    assert server.server_address[0] == "127.0.0.1"


def test_token_changes_each_run(tmp_path):
    root = write_facts(tmp_path)
    a, b = dashboard.make_server(root, 0), dashboard.make_server(root, 0)
    try:
        assert a.token != b.token and len(a.token) >= 43 and len(b.token) >= 43  # token_urlsafe(32)
    finally:
        for s in (a, b):
            s.server_close()


def test_refuses_non_loopback_host(start):
    """완료 기준 「루프백이 아닌 Host」: 다른 Host는 토큰이 맞아도 403. Host 없음은 400(RFC 9112 §3.2 MUST)."""
    _, p, t = start()
    assert req(p, "GET", f"/?t={t}")[0] == 200
    assert req(p, "GET", f"/?t={t}", {"Host": f"localhost:{p}"})[0] == 200
    for host in (f"192.168.0.10:{p}", "evil.example", f"127.0.0.1:{p + 1}"):
        status, h, body = req(p, "GET", f"/?t={t}", {"Host": host})
        assert status == 403 and has_headers(h) and t not in body
    for version in ("HTTP/1.1", "HTTP/1.0"):
        status, h, body = raw(p, f"GET /?t={t} {version}\r\n\r\n".encode())
        assert status == 400 and has_headers(h) and t not in body, version
        assert BAD_REQUEST_TEXT in body and "·" not in body and "Bad Request" not in body  # 옛 test_connect_missing_or_duplicate_host_is_400
    for data in (b"GET / HTTP/1.1 x\r\n\r\n", b"GET / HTTP/x\r\n\r\n"):  # 깨진 요청 줄의 400도 같은 한 줄
        assert BAD_REQUEST_TEXT in raw(p, data)[2]


@pytest.mark.parametrize("path", ["/", "/export.csv", "/export.json", "/s/trend_10_50"])
def test_refuses_missing_token(start, path):
    """완료 기준 「토큰 없음」: 어느 주소든 403(라우트보다 먼저), HEAD도 403 · 본문 없음."""
    _, p, t = start()
    status, h, body = req(p, "GET", path)
    assert status == 403 and has_headers(h) and t not in body
    status, h, body = req(p, "HEAD", path)
    assert status == 403 and has_headers(h) and body == ""


def test_refuses_wrong_token(start):
    """완료 기준 「틀린 토큰」: 틀린 값 · 앞부분만 · 다른 칸 이름 · 비ASCII(퍼센트 인코딩) 모두 403(TypeError 아님)."""
    _, p, t = start()
    for q in ("t=wrong", f"t={t[:-1]}", f"x={t}", "t=%ED%95%9C%EA%B8%80", f"t={t}%00"):
        status, h, _ = req(p, "GET", f"/?{q}")
        assert status == 403 and has_headers(h), q


def test_refuses_absolute_form_target(start):
    """절대 주소 꼴 요청 대상(`GET http://evil.example/?t=…`)은 토큰 · Host가 맞아도 403."""
    _, p, t = start()
    # `//evil.example/…`은 표준 라이브러리가 먼저 `/evil.example/…`로 접어 같은 출처 경로가 된다(그래서 여기 없음, 404)
    for target in (f"http://evil.example/?t={t}", f"http://127.0.0.1:{p}/?t={t}", f"https://evil.example/?t={t}"):
        status, h, _ = raw(p, f"GET {target} HTTP/1.1\r\nHost: 127.0.0.1:{p}\r\n\r\n".encode())
        assert status == 403 and has_headers(h), target


def test_refuses_duplicate_host(start):
    """Host 머리글이 둘이면(첫 값이 맞아도) 400(RFC 9112 §3.2 「more than one Host header field line」 MUST)."""
    _, p, t = start()
    for hosts in ((f"127.0.0.1:{p}", "evil.example"), (f"127.0.0.1:{p}", f"127.0.0.1:{p}")):
        lines = "".join(f"Host: {x}\r\n" for x in hosts)
        status, h, body = raw(p, f"GET /?t={t} HTTP/1.1\r\n{lines}\r\n".encode())
        assert status == 400 and has_headers(h), hosts
        assert "요청 모양이 맞지 않아요. 브라우저 주소를 다시 열어 주세요." in body and "Bad Request" not in body
        assert raw(p, f"POST /?t={t} HTTP/1.1\r\n{lines}\r\n".encode())[0] == 400


def test_serve_closes_port_when_guidance_fails(tmp_path):
    """안내 줄을 내다 끊겨도(만든 바로 뒤부터 try) 포트를 닫는다."""
    seen = []

    def out(line):
        seen.append(line)
        raise KeyboardInterrupt

    assert dashboard.serve(write_facts(tmp_path), 0, False, out=out) == 0
    p = int(re.search(r":(\d+)/", seen[0]).group(1))
    with pytest.raises(OSError):
        socket.create_connection(("127.0.0.1", p), timeout=1).close()


def test_refuses_cross_origin(start):
    _, p, t = start()
    assert req(p, "GET", f"/?t={t}", {"Origin": "http://evil.example"})[0] == 403
    assert req(p, "GET", f"/?t={t}", {"Origin": "null"})[0] == 403
    assert req(p, "GET", f"/?t={t}", {"Origin": f"http://127.0.0.1:{p}"})[0] == 200
    assert req(p, "GET", f"/?t={t}", {"Origin": f"http://localhost:{p}"})[0] == 200


def test_unknown_path_without_token_is_403_with_token_is_404(start):
    _, p, t = start()
    assert req(p, "GET", "/nope")[0] == 403
    assert raw(p, f"POST /nope HTTP/1.1\r\nHost: 127.0.0.1:{p}\r\nContent-Length: 1\r\n\r\nx".encode())[0] == 403  # 토큰 없는 POST도 라우트를 알려 주지 않음
    status, h, body = req(p, "GET", f"/nope?t={t}")
    assert status == 404 and has_headers(h)
    assert req(p, "GET", f"/s/trend_10_50?t={t}")[0] == 200  # 가드레일 화면
    for path in ("/export.csv", "/export.json"):  # 내보내기
        assert req(p, "GET", f"{path}?t={t}")[0] == 200


def test_malformed_target_is_403_without_traceback(start, capfd):
    """옛 test_connect_malformed_target_is_403_not_crash: `GET · POST http://[`는 보안 검사가 먼저라 403 · 머리글 여섯 · 표준 오류 빈 출력."""
    _, p, _ = start()
    for method in ("GET", "POST"):
        status, h, _ = raw(p, f"{method} http://[ HTTP/1.1\r\nHost: 127.0.0.1:{p}\r\n\r\n".encode())
        assert status == 403 and has_headers(h), method
    assert capfd.readouterr().err == ""


def test_security_headers_on_every_response(start):
    """200 · 403 · 404 · 405 · TRACE(기본 처리기 501) · 깨진 요청 대상 응답 모두 HEADERS 여섯 머리글."""
    _, p, t = start()
    seen = {}
    for method, path in (("GET", f"/?t={t}"), ("GET", "/"), ("GET", f"/x?t={t}"), ("POST", f"/?t={t}")):
        status, h, _ = req(p, method, path)
        seen[status] = has_headers(h)
    status, h, _ = raw(p, f"TRACE /?t={t} HTTP/1.1\r\nHost: 127.0.0.1:{p}\r\n\r\n".encode())
    seen[status] = has_headers(h)
    status, h, _ = raw(p, f"GET http://[ HTTP/1.1\r\nHost: 127.0.0.1:{p}\r\n\r\n".encode())
    assert status == 403  # urlsplit의 ValueError → 거절
    assert has_headers(h)
    # 깨진 요청 줄 · 모르는 버전 · HTTP/0.9: 표준 라이브러리는 버전을 0.9로 둬 머리글을 빼는데 send_error가 되살린다
    # HTTP/0.9의 상태는 표준 라이브러리 판마다 다르다(3.13부터 403, 그 전은 머리글을 읽어 Host 없음 → 400). 지키는 것은 머리글이다
    for data, codes in ((b"GET / HTTP/1.1 x\r\n\r\n", {400}), (b"GET / HTTP/x\r\n\r\n", {400}),
                        (b"GET / HTTP/2.0\r\n\r\n", {505}), (b"GET /\r\n", {400, 403}), (b"GET /" + b"a" * 65532, {414})):
        status, h, _ = raw(p, data)
        assert status in codes, data
        assert has_headers(h), data
    assert seen == {200: True, 403: True, 404: True, 405: True, 501: True}


def test_431_carries_security_headers(start):
    """요청 머리글이 너무 크면 표준 라이브러리가 do_*를 거치지 않고 431을 낸다. 그 응답에도 HEADERS.

    트리거(CPython `http/client.py` `_read_headers`): 한 줄이 `_MAXLINE`(65536)바이트를 넘거나, 머리글 줄이 `_MAXHEADERS`(100)줄을 넘을 때.
    둘 다 서버가 더 읽을 바이트가 남지 않게 보낸다(남으면 닫을 때 연결이 끊겨 응답을 못 읽을 수 있음).
    """
    _, p, t = start()
    head = f"GET /?t={t} HTTP/1.1\r\nHost: 127.0.0.1:{p}\r\n".encode()
    long_line = b"X: " + b"a" * (http.client._MAXLINE + 1 - 3)  # 개행 없이 정확히 65537바이트째에서 거절
    many = b"".join(b"X-%d: 1\r\n" % i for i in range(http.client._MAXHEADERS))  # Host까지 101줄째에서 거절
    for data in (head + long_line, head + many):
        status, h, body = raw(p, data)
        assert status == 431 and has_headers(h), data[-30:]
        assert re.search(r"<title>([^<]+)</title><main><p>\1</p></main>", body) and "<title>431</title>" not in body  # 제목 = 본문 한 줄
        assert "Python" not in body and t not in body


def test_error_page_lang_follows_the_text(start):
    """WCAG 3.1.1: 오류 페이지의 `lang`은 그 쪽에 실제로 쓰인 글의 언어다. 우리 글(403 · 404 · 405 · 400)은 `ko`,
    표준 라이브러리가 내는 영어 글(414 · 431 · 501 · 505)은 `en`."""
    _, p, t = start()
    host = f"Host: 127.0.0.1:{p}\r\n"
    lang = lambda body: re.search(r'<html lang="(\w+)">', body)[1]
    ours = [(403, req(p, "GET", "/")), (404, req(p, "GET", f"/x?t={t}")), (405, req(p, "POST", f"/?t={t}")),
            (400, raw(p, f"GET /?t={t} HTTP/1.1\r\n\r\n".encode())),  # Host 없음: 우리 400
            (400, raw(p, b"GET / HTTP/1.1 x\r\n\r\n"))]  # 깨진 요청 줄: 표준 라이브러리가 부르지만 글은 우리 BAD_REQUEST_TEXT
    for code, (status, _, body) in ours:
        assert status == code and lang(body) == "ko", (code, body)
    assert BAD_REQUEST_TEXT in ours[-1][1][2]
    std = [(501, raw(p, f"TRACE /?t={t} HTTP/1.1\r\n{host}\r\n".encode())), (505, raw(p, b"GET / HTTP/2.0\r\n\r\n")),
           (414, raw(p, b"GET /" + b"a" * 65532)),
           (431, raw(p, f"GET /?t={t} HTTP/1.1\r\n{host}X: ".encode() + b"a" * (http.client._MAXLINE + 1 - 3)))]
    for code, (status, _, body) in std:
        assert status == code and lang(body) == "en", (code, body)
        assert re.search(r"<title>([^<]+)</title><main><p>\1</p></main>", body)  # 글 그대로, 제목 = 본문 한 줄


def test_server_header_has_no_python_version(start):
    """`Server` 머리글은 `server_version + " " + sys_version`(`BaseHTTPRequestHandler.version_string`)이고 파이썬 버전이 없다.

    `sys_version`이 빈 글이라 값 끝에 공백이 하나 남는다(표준 라이브러리 꼴 그대로, 받는 쪽은 앞뒤 공백을 무시).
    """
    server, p, t = start()
    cls = server.RequestHandlerClass
    assert cls.server_version + " " + cls.sys_version == "blackclip "
    for status, h in ((200, req(p, "GET", f"/?t={t}")[1]), (403, req(p, "GET", "/")[1]), (404, req(p, "GET", f"/nope?t={t}")[1]),
                      (405, req(p, "POST", f"/?t={t}")[1]),
                      (431, raw(p, f"GET / HTTP/1.1\r\nHost: 127.0.0.1:{p}\r\nX: ".encode() + b"a" * (http.client._MAXLINE + 1 - 3))[1])):
        assert h["Server"] == "blackclip " and "Python" not in h["Server"] and "BaseHTTP" not in h["Server"], status


@pytest.mark.parametrize("method", ["POST", "PUT", "DELETE", "PATCH", "OPTIONS"])
def test_post_is_405(start, method):
    """GET · HEAD 밖 정의한 메서드는 토큰이 맞아도 405 · Allow 머리글."""
    _, p, t = start()
    status, h, body = req(p, method, f"/?t={t}")
    assert status == 405 and has_headers(h) and h.get("Allow") == "GET, HEAD" and t not in body


def test_head_same_headers_without_body(start):
    _, p, t = start()
    s1, h1, b1 = req(p, "GET", f"/?t={t}")
    s2, h2, b2 = req(p, "HEAD", f"/?t={t}")
    assert (s1, s2) == (200, 200) and b2 == "" and h2["Content-Length"] == h1["Content-Length"] == str(len(b1.encode()))
    assert has_headers(h2)


def test_no_request_log(start, capfd):
    """요청 줄에 접속 토큰이 있어 서버는 아무것도 적지 않는다(오류 응답 포함)."""
    _, p, t = start()
    req(p, "GET", f"/?t={t}")
    req(p, "GET", "/?t=wrong")
    raw(p, f"TRACE /?t={t} HTTP/1.1\r\n\r\n".encode())
    out, err = capfd.readouterr()
    assert out == "" and err == ""


def test_handler_has_socket_timeout(start):
    server, _, _ = start()
    assert server.RequestHandlerClass.timeout == 30


def test_links_carry_token_and_stay_same_origin(start):
    _, p, t = start()
    status, _, body = req(p, "GET", f"/?t={t}")
    links = re.findall(r'href="([^"]*)"', body)
    assert status == 200 and links
    assert all(x.startswith("/") and not x.startswith("//") and x.endswith(f"?t={t}") for x in links)
    assert re.findall(r'="\s*(?:https?:|//)', body) == []


def test_repo_text_is_escaped_and_plain(tmp_path, start):
    """레포에서 온 대표 상태 글(<script> · 칩 흉내)은 escape되고, 칩 수는 엔진 판정 수(기록이 있는 전략 수)와 같다."""
    root = write_facts(tmp_path)
    for name, rep in (("trend_10_50", "<script>x</script>"), ("grid_v1", '정상 ▣ 경고 <span class="chip chip-warn">')):
        p = root / "strategies" / name / "daily" / "days.jsonl"
        recs = [json.loads(x) for x in p.read_text(encoding="utf-8").splitlines()]
        recs[-1]["rep"] = rep
        p.write_text("".join(json.dumps(r, ensure_ascii=False) + "\n" for r in recs), encoding="utf-8")
    _, port, t = start(root)
    status, _, body = req(port, "GET", f"/?t={t}")
    assert status == 200 and "<script" not in body and "▣" not in body
    assert body.count('class="chip ') == 8  # 기록이 있는 전략 4개 × (표 + 쌓은 목록)


def test_unreadable_strategy_is_row_not_error(tmp_path, start):
    """렌더 중 한 전략을 못 읽으면 그 줄만 「확인 불가」 + web.reason(경로 없음), 응답은 200."""
    root = write_facts(tmp_path)
    (root / "strategies" / "grid_v1" / "daily" / "days.jsonl").write_text("{broken\n", encoding="utf-8")
    _, port, t = start(root)
    status, _, body = req(port, "GET", f"/?t={t}")
    assert status == 200 and "매일 대조 기록 파일을 읽지 못했어요(링크이거나 모양이 달라요)" in body
    assert body.count("확인 불가") >= 1 and "trend_10_50" in body and str(tmp_path) not in body


def test_rows_follow_status_selection(tmp_path):
    """home_rows는 status와 같은 기준: 실전 줄은 _rank · 이름순, 확인 불가는 뒤, 끈 전략은 따로."""
    root = write_facts(tmp_path)
    (root / "strategies" / "grid_v1" / "daily" / "days.jsonl").write_text("{broken\n", encoding="utf-8")
    rows, off, day = dashboard.home_rows(root)
    assert [r["name"] for r in rows] == ["breakout_v2", "trend_10_50", "trend_ema_v7", "grid_v1"]
    assert rows[-1]["word"] == "확인 불가" and rows[-1]["live"] is True and rows[0]["word"] == "확인 불가"
    assert off == [] and day == "2026-10-14"
    assert set(rows[0]) == {"name", "live", "word", "reason", "actual", "bt", "gap", "trades", "day", "until", "spark", "spark_start",
                            "warn_days", "bt0", "live_day", "guardrail"}  # 뒤 셋은 내보내기가 쓰는 칸(홈은 읽지 않음)


def test_whole_page_workspace_error_is_200(tmp_path, start):
    """strategies/가 링크면 홈 전체가 「확인 불가」 한 장(200, 경로 없음)."""
    (tmp_path / ".git").mkdir()
    root = tmp_path / ".blackclip"
    root.mkdir()
    (tmp_path / "elsewhere").mkdir()
    (root / "strategies").symlink_to(tmp_path / "elsewhere")
    _, port, t = start(root)
    status, h, body = req(port, "GET", f"/?t={t}")
    assert status == 200 and has_headers(h) and "확인 불가" in body and str(tmp_path) not in body


def test_other_exception_is_500(start, monkeypatch):
    _, p, t = start()

    def boom(root):
        raise RuntimeError(str(root))

    monkeypatch.setattr(dashboard, "home_rows", boom)
    status, h, body = req(p, "GET", f"/?t={t}")
    assert status == 500 and has_headers(h) and "화면을 그리지 못했어요. 터미널에서 blackclip status로 확인해 주세요." in body
    assert t not in body and "Traceback" not in body


def test_request_allowed_value_error_is_false():
    """urlsplit이 ValueError를 내는 요청 대상은 예외가 아니라 False."""
    h = {"Host": "127.0.0.1:8765"}
    assert request_allowed(h, "http://[", 8765, "tok") is False
    assert request_allowed(h, "/?t=tok", 8765, "tok") is True
    assert request_allowed(h, "http://127.0.0.1:8765/?t=tok", 8765, "tok") is False


def test_dashboard_prints_loopback_url_with_token(tmp_path):
    """serve는 안내 줄 셋을 내고, 그 주소로 GET 200, stop 뒤에는 포트가 닫힌다."""
    root = write_facts(tmp_path)
    lines, stop, done = [], threading.Event(), []
    th = threading.Thread(target=lambda: done.append(dashboard.serve(root, 0, False, out=lines.append, stop=stop)))
    th.start()
    try:
        for _ in range(200):
            if len(lines) == 3:
                break
            threading.Event().wait(0.01)
        m = re.fullmatch(r"대시보드: http://127\.0\.0\.1:(\d+)/\?t=([A-Za-z0-9_-]{32,})", lines[0])
        assert m, lines
        p = int(m.group(1))
        assert lines[1] == f"화면 없는 서버면 노트북에서 ssh -L {p}:127.0.0.1:{p} <서버> 뒤 위 주소를 여세요."
        assert lines[2] == "끌 때는 Ctrl+C를 눌러 주세요."
        assert req(p, "GET", f"/?t={m.group(2)}")[0] == 200
    finally:
        stop.set()
        th.join(5)
    assert done == [0]
    with pytest.raises(OSError):
        socket.create_connection(("127.0.0.1", p), timeout=1).close()


def test_serve_ctrl_c_closes_and_returns_0(tmp_path):
    class Stop:
        def wait(self):
            raise KeyboardInterrupt

    lines = []
    assert dashboard.serve(write_facts(tmp_path), 0, False, out=lines.append, stop=Stop()) == 0
    p = int(re.search(r":(\d+)/", lines[0]).group(1))
    with pytest.raises(OSError):
        socket.create_connection(("127.0.0.1", p), timeout=1).close()


def test_dashboard_without_workspace_exits_2(tmp_path, monkeypatch, capsys):
    (tmp_path / ".git").mkdir()
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(cli, "serve_dashboard", lambda *a, **k: pytest.fail("서버를 띄우면 안 됨"))
    assert main(["dashboard", "--no-browser"]) == 2
    assert capsys.readouterr().err.strip() == (
        "blackclip dashboard: 이 폴더에서 .blackclip을 찾지 못했어요. 레포 안에서 blackclip scan을 먼저 돌려 주세요.")


def test_dashboard_refuses_linked_workspace(tmp_path, monkeypatch, capsys):
    (tmp_path / ".git").mkdir()
    (tmp_path / "real").mkdir()
    (tmp_path / ".blackclip").symlink_to(tmp_path / "real")
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(cli, "serve_dashboard", lambda *a, **k: pytest.fail("서버를 띄우면 안 됨"))
    assert main(["dashboard", "--no-browser"]) == 2
    assert "찾지 못했어요" in capsys.readouterr().err


def test_dashboard_does_not_create_workspace(tmp_path, monkeypatch):
    (tmp_path / ".git").mkdir()
    monkeypatch.chdir(tmp_path)
    assert main(["dashboard", "--no-browser"]) == 2
    assert not (tmp_path / ".blackclip").exists() and not (tmp_path / ".gitignore").exists()


@pytest.mark.parametrize("holder", ["dashboard", "socket"])
def test_dashboard_when_port_in_use_exits_2(tmp_path, monkeypatch, capsys, holder):
    """포트를 대시보드가 먼저 잡았든 다른 프로그램이 잡았든 같은 안내 한 줄 · 종료 2(판별 프로브 없음). 그 밖 OSError는 지금 글."""
    root = write_facts(tmp_path)
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(cli, "default_store", MemStore)  # 실제 키체인 0(상주 대시보드는 늘 연결 설정을 넘김)
    if holder == "dashboard":
        s = dashboard.make_server(root, 0)
        threading.Thread(target=s.serve_forever, daemon=True).start()
        release = lambda: (s.shutdown(), s.server_close())  # noqa: E731
    else:
        s = socket.socket()
        s.bind(("127.0.0.1", 0))
        s.listen()
        release = s.close
    try:
        p = s.server_address[1] if holder == "dashboard" else s.getsockname()[1]
        assert main(["dashboard", "--no-browser", "--port", str(p)]) == 2
        assert capsys.readouterr().err == (f"blackclip dashboard: {p} 포트가 이미 쓰이고 있어요. "
                                           "대시보드가 떠 있으면 그 창을 쓰고, 아니면 --port <다른 번호>로 띄워 주세요.\n")
    finally:
        release()

    def refuse(*a, **k):
        raise OSError(errno.EACCES, "Permission denied")
    monkeypatch.setattr(dashboard, "make_server", refuse)
    assert main(["dashboard", "--no-browser", "--port", "8800"]) == 2
    assert capsys.readouterr().err == "blackclip dashboard: 8800 포트로 화면을 열지 못했어요(Permission denied). --port로 다른 번호를 주세요.\n"


def test_dashboard_cli_passes_root_port_and_browser(tmp_path, monkeypatch):
    root = write_facts(tmp_path)
    monkeypatch.chdir(tmp_path)
    got = []
    monkeypatch.setattr(cli, "serve_dashboard", lambda *a, **k: got.append((a, k)) or 0)
    assert main(["dashboard"]) == 0 and main(["dashboard", "--no-browser", "--port", "9000"]) == 0
    assert got == [((root, 8765), {"open_browser": True}), ((root, 9000), {"open_browser": False})]
    assert main(["dashboard", "--port", "80"]) == 2


def test_dashboard_cli_passes_connect_and_nav_link_is_200(tmp_path, monkeypatch):
    """`blackclip dashboard`는 늘 연결 설정(main · 테스트넷 아님)을 넘긴다 → CLI가 만든 상주 서버의 홈 내비 「연결」 링크가 200.
    키 보관은 MemStore로 바꿔치기(실제 키체인 0)."""
    root = write_facts(tmp_path)
    monkeypatch.chdir(tmp_path)
    store = MemStore()
    monkeypatch.setattr(cli, "default_store", lambda: store)
    real, lines, stop, seen = dashboard.serve, [], threading.Event(), []

    def run(*a, connect=None, **k):
        seen.append(connect)
        return real(root, 0, False, out=lines.append, stop=stop, connect=connect)
    monkeypatch.setattr(dashboard, "serve", run)
    th = threading.Thread(target=lambda: main(["dashboard", "--no-browser"]), daemon=True)
    th.start()
    try:
        for _ in range(500):
            if lines:
                break
            threading.Event().wait(0.01)
        p, t = re.fullmatch(r"대시보드: http://127\.0\.0\.1:(\d+)/\?t=([A-Za-z0-9_-]+)", lines[0]).groups()
        home = req(int(p), "GET", f"/?t={t}")[2]
        link = re.search(r'href="(/connect\?t=[^"]+)"[^>]*>연결<', home).group(1)
        assert req(int(p), "GET", link)[0] == 200
    finally:
        stop.set()
        th.join(5)
    c = seen[0]
    assert (c.store, c.label, c.testnet) == (store, "main", False) and not th.is_alive()


def test_one_server_one_secure_handler(tmp_path):
    """서버 구현은 dashboard.make_server 하나. 상주 · 연결 설정 · 한 번용 어느 판이든 처리기는 바탕(connect.SecureHandler)
    하나를 상속하고 send_error · 로그 · 시간 제한을 따로 두지 않는다(옛 test_both_servers_share_one_secure_handler)."""
    from blackclip.connect import Connect, SecureHandler

    root = write_facts(tmp_path)
    conn = Connect(MemStore(), "main", False, "연결", lambda c: None, lambda c: None)
    servers = [dashboard.make_server(root, 0), dashboard.make_server(root, 0, connect=conn),
               dashboard.make_server(root, 0, connect=conn, once=dashboard.Once("key", threading.Event()))]
    try:
        for s in servers:
            h = s.RequestHandlerClass
            assert issubclass(h, SecureHandler) and h is not SecureHandler and h.timeout == 30
            assert not {"send_error", "log_message", "timeout", "server_version", "sys_version"} & set(vars(h))
    finally:
        for s in servers:
            s.server_close()

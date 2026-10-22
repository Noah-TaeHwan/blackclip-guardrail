"""blackclip fetch 흐름: 키 없음 · 권한 거부 · 받기 · 이어 받기 · 일부 실패 · 상태 요약."""
from datetime import timedelta

from conftest import CREDS, NOW, READ_ONLY, MemStore, maker, routes

from blackclip.fetch import EXIT_INPUT, EXIT_PARTIAL, EXIT_PERMISSION, run_fetch, status_lines
from blackclip.fillstore import read_coverage
from blackclip.retro.fills import read_executions
from blackclip.workspace import ensure, load_connections


def go(root, store, answer, made=None, **kw):
    """run_fetch를 기본값으로 부른다."""
    args = {"label": "main", "testnet": False, "markets": ["spot"], "symbols": ["BTCUSDT"], "since": None, "now": NOW} | kw
    return run_fetch(root, store, maker(answer, made), **args)


def test_no_key_exit_2(tmp_path):
    """키가 없으면 연결 화면을 가리키고 멈춘다."""
    code, lines = go(ensure(tmp_path), MemStore(), routes())
    assert code == EXIT_INPUT and lines == ["키가 없습니다(binance main) · blackclip connect --open"]


def test_rejected_key_writes_nothing(tmp_path):
    """주문 권한이 켜진 키면 한 건도 받지 않고 거부를 기록한다."""
    root = ensure(tmp_path)
    code, lines = go(root, MemStore({"binance:main": CREDS}), routes(perm={**READ_ONLY, "enableSpotAndMarginTrading": True}))
    assert code == EXIT_PERMISSION and "enableSpotAndMarginTrading" in lines[0]
    assert not (root / "fills").exists()
    assert load_connections(root)["binance"]["main"]["permission"]["status"] == "rejected"


def test_spot_fetch_writes_csv_and_coverage(tmp_path):
    """받은 체결을 회고가 읽는 CSV로 쌓고 받은 범위를 적는다. 처음은 30일 전 0시 UTC부터."""
    root = ensure(tmp_path)
    code, lines = go(root, MemStore({"binance:main": CREDS}), routes())
    d = root / "fills" / "binance-main" / "spot"
    assert code == 0 and lines[0] == "binance main · 권한 조회만(조회만 켜져 있음 · IP 제한 없음)"
    assert lines[1] == "spot BTCUSDT: 새로 1건 · 2026-09-15T00:00 ~ 2026-10-15T12:00 UTC 받음"
    assert [e.side for e in read_executions(d / "fills.csv")] == ["buy"]
    assert read_coverage(d)["BTCUSDT"] == {"from": "2026-09-15T00:00:00.000+00:00", "until": "2026-10-15T12:00:00.000+00:00"}


def test_rerun_continues_from_until(tmp_path):
    """다시 돌리면 받은 끝부터 이어 받고 같은 체결을 또 쌓지 않는다. 종목은 기억한다."""
    root, store, made = ensure(tmp_path), MemStore({"binance:main": CREDS}), []
    go(root, store, routes())
    code, lines = go(root, store, routes(), made, symbols=None, now=NOW + timedelta(hours=1))
    trade_calls = [q for c in made for p, q in c.calls if p == "/api/v3/myTrades"]
    assert code == 0 and trade_calls[0]["startTime"] == int(NOW.timestamp() * 1000)
    assert "새로 0건" in lines[1]
    assert len(read_executions(root / "fills" / "binance-main" / "spot" / "fills.csv")) == 1


def test_one_symbol_failure_keeps_others(tmp_path):
    """한 종목이 실패해도 다른 종목은 쌓고, 실패한 종목은 받은 범위를 늘리지 않고 확인 불가로 말한다."""
    root = ensure(tmp_path)
    code, lines = go(root, MemStore({"binance:main": CREDS}), routes(fail="BADXUSDT"), symbols=["BTCUSDT", "BADXUSDT"])
    cov = read_coverage(root / "fills" / "binance-main" / "spot")
    assert code == EXIT_PARTIAL and "BTCUSDT" in cov and "BADXUSDT" not in cov
    assert any(line.startswith("spot BADXUSDT: 받지 못함(바이낸스 응답 400 · 코드 -1121") and "확인 불가" in line for line in lines)


def test_value_error_marks_only_that_symbol(tmp_path):
    """한 종목에서 ValueError(JSON이 아닌 200 본문 등)가 나도 그 종목만 받지 못함이고 다른 종목은 쌓는다."""
    base = routes()

    def answer(path, q):
        if q.get("symbol") == "BADXUSDT":
            raise ValueError("bad json")
        return base(path, q)

    root = ensure(tmp_path)
    code, lines = go(root, MemStore({"binance:main": CREDS}), answer, symbols=["BTCUSDT", "BADXUSDT"])
    cov = read_coverage(root / "fills" / "binance-main" / "spot")
    assert code == EXIT_PARTIAL and "BTCUSDT" in cov and "BADXUSDT" not in cov
    assert any(line.startswith("spot BADXUSDT: 받지 못함(bad json)") for line in lines)


def test_futures_includes_funding_and_clip(tmp_path):
    """선물은 펀딩비도 받고, 3개월보다 앞은 확인 불가라고 말한다."""
    root = ensure(tmp_path)
    code, lines = go(root, MemStore({"binance:main": CREDS}), routes(trades=()), markets=["futures"], since=NOW - timedelta(days=120))
    d = root / "fills" / "binance-main" / "futures"
    assert code == 0 and (d / "funding.csv").exists() and (d / "fills.csv").exists()
    assert "확인 불가(선물 조회는 3개월까지)" in lines[1] and lines[2].startswith("futures 펀딩비: 새로 0건")


def test_missing_symbols_asks(tmp_path):
    """받을 종목을 준 적이 없으면 추측하지 않고 묻는다."""
    code, lines = go(ensure(tmp_path), MemStore({"binance:main": CREDS}), routes(), symbols=None)
    assert code == EXIT_INPUT and lines[1] == "spot: 받을 종목이 없습니다 · --symbols BTCUSDT,ETHUSDT처럼 주세요"


def test_testnet_permission_unknown(tmp_path):
    """테스트넷은 권한 조회를 부르지 않고 권한 확인 불가로 받는다."""
    made = []
    code, lines = go(ensure(tmp_path), MemStore({"binance:main-testnet": CREDS}), routes(), made, testnet=True)
    assert code == 0 and lines[0].startswith("binance main-testnet · 권한 확인 불가(테스트넷")
    assert all(p != "/sapi/v1/account/apiRestrictions" for c in made for p, _ in c.calls)


def test_status_lines_show_last4_not_secret(tmp_path):
    """상태 요약은 끝 4자리 · 권한 · 받은 범위만 말한다."""
    root, store = ensure(tmp_path), MemStore({"binance:main": CREDS})
    go(root, store, routes())
    text = "\n".join(status_lines(root, store))
    assert "끝 WXYZ" in text and "권한 조회만" in text and "spot BTCUSDT: 2026-09-15T00:00 ~ 2026-10-15T12:00 UTC 받음" in text
    assert CREDS.api_key not in text and CREDS.secret_key not in text


def test_status_without_connections(tmp_path):
    """연결한 적이 없으면 연결 화면을 가리킨다."""
    assert status_lines(ensure(tmp_path), MemStore()) == ["연결된 계정 없음 · blackclip connect --open"]

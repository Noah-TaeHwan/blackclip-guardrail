"""blackclip daily 묶음: 받기 → 대조 → 파일 → state.log → 텔레그램 → 헬스체크."""
import http.client
import json
import os
import re
import signal
from datetime import datetime, timedelta, timezone
from decimal import Decimal

import pytest
from conftest import (CREDS, READ_ONLY, TG_TOKEN, FakeTelegram, KlineClient, MemStore, make_prices, maker, routes,
                      write_link)
from test_check_report import _body

from blackclip.cli import main
from blackclip.daily.live import live_run
from blackclip.daily.messages import broken_message, broken_note
from blackclip.daily.run import run_daily
from blackclip.keystore import Credentials, account_name
from blackclip.notify import TELEGRAM_ACCOUNT, set_notify, write_live
from blackclip.telegram import Bot
from blackclip.workspace import ensure, load_connections, save_connections

UTC = timezone.utc
NOW = datetime(2025, 2, 3, 12, 0, tzinfo=UTC)
LIVE = datetime(2024, 12, 20, tzinfo=UTC)
END = datetime(2025, 2, 3, tzinfo=UTC)


def bt_executions(exec_="next_open", live: datetime = LIVE, windows: tuple[int, int] = (10, 30)):
    """예시 신호(windows 짧은 평균 > 긴 평균, 기본 10일선 > 30일선)를 바이낸스 봉(같은 가격)으로 돌린 실전 구간 체결(live는 실전 시작)."""
    p = make_prices()

    def sig(f):
        c = f["BTCUSDT"]["Close"]
        return {"BTCUSDT": (c.rolling(windows[0]).mean() > c.rolling(windows[1]).mean()).astype(float)}

    return live_run(sig, {"BTCUSDT": p}, exec_, 86400, live, END).executions


def trades_from(execs, capital=10000.0, shift=timedelta(minutes=5)):
    """백테스트 체결을 바이낸스 myTrades 응답 모양으로(기본 5분 뒤 · 같은 가격 · 수수료 0.1% USDT)."""
    out = []
    for i, e in enumerate(execs):
        qty = round(float(e.qty) * capital / float(e.price), 6)
        out.append({"symbol": "BTCUSDT", "id": 100 + i, "orderId": 900 + i, "price": str(e.price), "qty": str(qty),
                    "commission": f"{qty * float(e.price) * 0.001:.6f}", "commissionAsset": "USDT",
                    "time": int((e.time + shift).timestamp() * 1000), "isBuyer": e.side == "buy"})
    return out


def setup(tmp_path, monkeypatch, capsys, link_telegram=True, body=None, start: datetime = LIVE):
    """check · save(실전 시작 start, 기본 2024-12-20) · live(전날 낮에 잔고 USDT 10000 · BTC 0 → 대조는 start부터) · 텔레그램 대화 번호까지."""
    monkeypatch.chdir(tmp_path)
    write_link(tmp_path, body or _body())
    assert main(["save", "s1", "--start", f"{start:%Y-%m-%d}"]) == 0
    capsys.readouterr()
    root = tmp_path / ".blackclip"
    write_live(root, "s1", "main", "USDT", start - timedelta(hours=12), Decimal("10000"), {"BTCUSDT": Decimal(0)})
    if link_telegram:
        conns = load_connections(root)
        conns["telegram"] = {"chat_id": 4242, "bot": "@bc_test_bot"}
        save_connections(root, conns)
    store = MemStore({account_name("binance", "main"): CREDS, TELEGRAM_ACCOUNT: Credentials(TG_TOKEN, "")})
    return root, store


def _add_s2(tmp_path, root, name="s2"):
    """두 번째 전략 s2(같은 link · 같은 실전 시작 · 같은 계좌)를 저장하고 실전 시작 잔고까지 받아 둔다(test_one_strategy_failure_does_not_stop_others와 같은 방식)."""
    write_link(tmp_path, _body(), name)
    assert main(["save", name, "--start", "2024-12-20"]) == 0
    write_live(root, name, "main", "USDT", LIVE - timedelta(hours=12), Decimal(10000), {"BTCUSDT": Decimal(0)})


def run(root, store, trades, tg=None, answer=None, pings=None, now=NOW, klines=None):
    tg = tg or FakeTelegram()
    code, lines = run_daily(root, store, maker(answer or routes(trades=trades)),
                            lambda m: KlineClient(klines or {"BTCUSDT": make_prices()}, m), lambda t: Bot(t, tg), now,
                            ping_opener=(pings.append if pings is not None else None))
    return code, lines, tg


def test_daily_missed_order_end_to_end(tmp_path, monkeypatch, capsys):
    """마지막 백테스트 체결 하나를 빼면 실행 칸이 경고(놓친 주문으로 추정) · 파일 · state.log · 텔레그램 두 통."""
    root, store = setup(tmp_path, monkeypatch, capsys)
    execs = bt_executions()
    assert len(execs) >= 2
    code, lines, tg = run(root, store, trades_from(execs[:-1]))
    assert code == 0, lines
    md = (root / "strategies" / "s1" / "daily" / "2025-02-02.md").read_text(encoding="utf-8")
    assert "- 실행: 경고 · 실제 − BT(비용 0) " in md and "백테스트에만 있는 거래 1건(놓친 주문으로 추정" in md
    assert "\n기준 자본: 계좌 USDT 잔고 전체 + 출발 포지션(" in md  # live.json에 capital이 없으면 분모는 잔고 전체
    assert "- 전략: 판단 보류 · " in md
    log = [json.loads(x) for x in (root / "state.log").read_text().splitlines()]
    assert len(log) == 1 and log[0]["day"] == "2025-02-02" and log[0]["states"]["실행"] == "경고"
    sent = tg.sent()
    assert len(sent) == 2 and sent[1].startswith("매일 묶음 · 2025-02-02 UTC 마감 · 전략 1개")
    assert "실행: 첫 회고를 본 뒤 켬(blackclip notify --execution-alerts on) · 숫자는 daily 파일에" in sent[0]
    assert json.loads((root / "notified.json").read_text())["s1"]["실행"] == "경고"
    import facts
    from blackclip.daily.days import read_days
    recs = read_days(root, "s1")
    assert len(recs) == 1 and recs[0]["day"] == "2025-02-02" and recs[0]["states"]["실행"] == "경고"
    n = recs[0]["nums"]
    assert set(n) == set(facts.record("trend_10_50", 22)["nums"])  # 지어낸 기록과 엔진 줄의 칸이 같음
    assert n["missed_n"] == 1 and len(n["missed"]) == 1 and n["missed"][0]["side"] == "sell"
    assert f"실제 {n['actual']:+.1%}" in md and f"같은 기간 BT {n['bt']:+.1%}" in md
    assert recs[0]["head"] in md and recs[0]["lines"]["실행"] in md
    code2, _, tg2 = run(root, store, trades_from(execs[:-1]))
    assert code2 == 0 and len(tg2.sent()) == 1 and len((root / "state.log").read_text().splitlines()) == 1


def test_daily_close_fill_seconds_before_midnight_is_not_a_false_execution_warning(tmp_path, monkeypatch, capsys):
    """종가 체결 전략의 실제 체결이 BT 체결(봉 끝 0시)보다 5초 일러도 그날 실행이 정상이고, 다음 날도 정상이다."""
    root, store = setup(tmp_path, monkeypatch, capsys, body=_body().replace('EXEC = "next_open"', 'EXEC = "close"'))
    trades = trades_from(bt_executions("close"), shift=timedelta(seconds=-5))
    trades[-1]["qty"] = trades[0]["qty"]  # 산 만큼만 판다(체결 수량 환산이 가격 따라 달라 초과 매도 = 기록 불완전이 되지 않게)
    folder = root / "strategies" / "s1" / "daily"
    for now, day in ((datetime(2025, 1, 23, 12, 0, tzinfo=UTC), "2025-01-22"), (datetime(2025, 1, 24, 12, 0, tzinfo=UTC), "2025-01-23")):
        assert run(root, store, trades, now=now)[0] == 0
        md = (folder / f"{day}.md").read_text(encoding="utf-8")
        assert "- 실행: 정상 · 실제 − BT(비용 0) " in md and "실제에만 있는 거래" not in md, md


def test_daily_telegram_line_says_nothing_sent_when_no_state_changed(tmp_path, monkeypatch, capsys):
    """「바뀔때만」에 상태가 그대로인 날은 보낸 글이 없으니 「보냄」이 아니라 「바뀐 상태 없음 · 안 보냄」."""
    root, store = setup(tmp_path, monkeypatch, capsys)
    set_notify(root, quiet="바뀔때만")
    now = datetime(2025, 1, 23, 12, 0, tzinfo=UTC)  # 일요일이 아니라 매주 회고 글이 끼지 않는 날
    trades = trades_from(bt_executions())
    assert run(root, store, trades, now=now)[1][-1] == "텔레그램: 보냄"
    _, lines, tg = run(root, store, trades, now=now)
    assert lines[-1] == "텔레그램: 바뀐 상태 없음 · 안 보냄" and tg.sent() == []


def test_daily_capital_flag_sets_the_denominator(tmp_path, monkeypatch, capsys):
    """계좌 잔고 10000 중 전략 몫 1000: 기준 자본을 안 주면 실제 %가 잔고 전체 기준이라 작고, 1000을 주면 몇 배로 커지며 daily 파일에 출처가 적힘."""
    root, store = setup(tmp_path, monkeypatch, capsys)
    trades = trades_from(bt_executions()[:-1], capital=1000.0)  # 마지막 매도를 빼 현물 초과 매도(기록 불완전)를 피함
    md = root / "strategies" / "s1" / "daily" / "2025-02-02.md"

    def actual_pct():
        assert run(root, store, trades)[0] == 0
        return float(re.search(r"(?m)^실제 ([+-]\d+\.\d)%", md.read_text(encoding="utf-8")).group(1))

    small = actual_pct()
    assert "\n기준 자본: 계좌 USDT 잔고 전체 + 출발 포지션(" in md.read_text(encoding="utf-8")
    write_live(root, "s1", "main", "USDT", LIVE - timedelta(hours=12), Decimal("10000"), {"BTCUSDT": Decimal(0)}, Decimal("1000"))
    big = actual_pct()
    assert "\n기준 자본: 고객이 정한 금액\n" in md.read_text(encoding="utf-8")
    assert small < 0 and big < 0 and big / small > 5  # 같은 체결 · 분모만 1/10(구간 처음 손익이 분모에 더해져 정확히 10배는 아님)


def test_daily_fetch_failure_still_reports(tmp_path, monkeypatch, capsys):
    """체결 받기가 실패해도 파일은 남고, 봇 칸 확인 불가 · 종료 코드 4 · 묶음에 실패 줄."""
    root, store = setup(tmp_path, monkeypatch, capsys)

    def answer(path, q):
        if path == "/sapi/v1/account/apiRestrictions":
            return READ_ONLY
        raise OSError("network down")

    code, lines, tg = run(root, store, [], answer=answer)
    assert code == 4
    md = (root / "strategies" / "s1" / "daily" / "2025-02-02.md").read_text(encoding="utf-8")
    assert "- 봇: 확인 불가 · " in md
    assert "체결 받기 일부 실패(binance main spot)" in tg.sent()[-1]


def test_daily_telegram_failure_keeps_files(tmp_path, monkeypatch, capsys):
    """텔레그램이 안 되면 종료 코드 5, daily 파일 · state.log는 남고 notified.json은 안 씀."""
    root, store = setup(tmp_path, monkeypatch, capsys)
    code, lines, _ = run(root, store, trades_from(bt_executions()), tg=FakeTelegram(fail="sendMessage"))
    assert code == 5 and lines[-1].startswith("텔레그램: 못 보냄(텔레그램 sendMessage 실패(HTTP 400")
    assert (root / "strategies" / "s1" / "daily" / "2025-02-02.md").exists() and (root / "state.log").exists()
    assert not (root / "notified.json").exists()


def test_daily_outputs_never_contain_token(tmp_path, monkeypatch, capsys):
    """화면 줄 · daily 파일 · state.log · notified.json · connections.json에 토큰 · 키가 없다, 헬스체크는 한 번."""
    root, store = setup(tmp_path, monkeypatch, capsys)
    set_notify(root, healthcheck="https://hc-ping.com/abc")
    pings = []
    code, lines, _ = run(root, store, trades_from(bt_executions()), tg=FakeTelegram(fail="sendMessage"), pings=pings)
    texts = ["\n".join(lines)] + [p.read_text(encoding="utf-8") for p in root.rglob("*") if p.is_file() and p.suffix in {".md", ".log", ".json"}]
    for text in texts:
        assert TG_TOKEN not in text and CREDS.api_key not in text and CREDS.secret_key not in text
    assert pings == ["https://hc-ping.com/abc"]


def test_daily_without_guardrail_or_link_telegram(tmp_path, monkeypatch, capsys):
    """가드레일이 없으면 2, 대화 연결 전이면 안 보냈다는 줄."""
    (tmp_path / ".git").mkdir()
    root = ensure(tmp_path)
    assert run_daily(root, MemStore(), maker(routes()), lambda m: None, lambda t: None, NOW)[0] == 2
    root, store = setup(tmp_path, monkeypatch, capsys, link_telegram=False)
    code, lines, tg = run(root, store, trades_from(bt_executions()))
    assert code == 0 and lines[-1] == "텔레그램: 대화 연결 전이라 안 보냄 · blackclip connect --telegram" and tg.sent() == []


def test_one_strategy_failure_does_not_stop_others(tmp_path, monkeypatch, capsys):
    """첫 전략의 link.py가 없어져도 둘째 전략은 대조되고, 첫 전략은 확인 불가 파일 · 종료 코드 4."""
    root, store = setup(tmp_path, monkeypatch, capsys)
    write_link(tmp_path, _body(), "s2")
    assert main(["save", "s2", "--start", "2024-12-20"]) == 0
    write_live(root, "s2", "main", "USDT", LIVE - timedelta(hours=12), Decimal("10000"), {"BTCUSDT": Decimal(0)})
    (root / "strategies" / "s1" / "link.py").unlink()
    code, lines, _ = run(root, store, trades_from(bt_executions()))
    assert code == 4, lines
    assert "- 전략: 확인 불가(대조하지 못함 · FileNotFoundError)" in (root / "strategies" / "s1" / "daily" / "2025-02-02.md").read_text()
    assert (root / "strategies" / "s2" / "daily" / "2025-02-02.md").exists()
    assert any(x.startswith("s2: ") for x in lines)


def test_telegram_incomplete_read_still_pings(tmp_path, monkeypatch, capsys):
    """텔레그램 응답이 중간에 끊겨도 종료 코드 5 · 헬스체크는 감."""
    root, store = setup(tmp_path, monkeypatch, capsys)
    set_notify(root, healthcheck="https://hc-ping.com/abc")

    def cut(url, form):
        raise http.client.IncompleteRead(b"")

    pings = []
    code, lines = run_daily(root, store, maker(routes(trades=trades_from(bt_executions()))),
                            lambda m: KlineClient({"BTCUSDT": make_prices()}, m), lambda t: Bot(t, cut), NOW,
                            ping_opener=pings.append)
    assert code == 5 and pings == ["https://hc-ping.com/abc"] and lines[-1].startswith("텔레그램: 못 보냄(IncompleteRead")


def _edit_guardrail(root, edit):
    """저장된 가드레일(JSON) 최신 판을 고친다."""
    path = root / "strategies" / "s1" / "guardrail.v1.yaml"
    g = json.loads(path.read_text(encoding="utf-8"))
    edit(g)
    path.write_text(json.dumps(g, ensure_ascii=False), encoding="utf-8")


def test_daily_skips_bars_longer_than_a_day(tmp_path, monkeypatch, capsys):
    """봉이 1일보다 길면(주봉) 바이낸스 봉을 한 번도 안 받고 전략 칸에 까닭을 점검 불가로 적는다."""
    root, store = setup(tmp_path, monkeypatch, capsys)
    _edit_guardrail(root, lambda g: g["target"].update(bar_seconds=604800))
    klines = KlineClient({"BTCUSDT": make_prices()}, "spot")
    code, lines = run_daily(root, store, maker(routes(trades=[])), lambda m: klines, lambda t: Bot(t, FakeTelegram()), NOW)
    assert klines.calls == []
    md = (root / "strategies" / "s1" / "daily" / "2025-02-02.md").read_text(encoding="utf-8")
    assert "- 전략: 점검 불가(매일 대조는 1일 이하 봉만 · 점검(blackclip check)은 그대로)" in md
    assert code == 0, lines  # 영구 사유는 일시 장애(확인 불가)가 아니라 매일 종료 4가 나지 않는다


def test_daily_line_still_judged_when_signal_function_fails(tmp_path, monkeypatch, capsys):
    """신호 함수가 실패해도(비중 범위 밖) 봉 · 체결이 있으면 가드레일 선은 실제 손익으로 판정한다."""
    root, store = setup(tmp_path, monkeypatch, capsys)
    _edit_guardrail(root, lambda g: g["warning_line"].update(line=-0.01))  # 실제 손실이 이 선을 넘게
    write_link(tmp_path, _body(signal="c * 0 + 2.0"))  # 저장 뒤 신호 함수가 망가진 고객 코드
    code, lines, tg = run(root, store, trades_from(bt_executions()[:-1]))
    md = (root / "strategies" / "s1" / "daily" / "2025-02-02.md").read_text(encoding="utf-8")
    assert re.search(r"- 전략: 확인 불가\(신호 함수를 돌리지 못함\(\w+\)\)", md)
    sent = tg.sent()  # 대표 칸은 전략(확인 불가)이라도 첫 줄(알림 미리보기)과 묶음 줄에 경고선 경고가 보인다
    assert sent[0].splitlines()[0].endswith(" · 전략 · 경고선 경고") and sent[-1].splitlines()[1].count(" · 경고선 경고 · ") == 1
    assert re.search(r"- 가드레일 선: 경고 · 실제 -\d+\.\d%\(이번 구간 최저 -\d+\.\d%\) · 경고선 -1\.0%", md)


def test_daily_telegram_oserror_line_has_no_path(tmp_path, monkeypatch, capsys):
    """notified.json 저장이 OSError(원문에 절대 경로)로 실패해도 화면 줄에는 종류 이름만 남는다."""
    root, store = setup(tmp_path, monkeypatch, capsys)

    def boom(root_, notified):
        raise OSError("write failed: /Users/secret-customer/repo/.blackclip/tmp123")

    monkeypatch.setattr("blackclip.daily.run.save_notified", boom)
    code, lines, _ = run(root, store, trades_from(bt_executions()))
    assert code == 5 and lines[-1].startswith("텔레그램: 못 보냄(OSError) · ")
    assert "secret-customer" not in "\n".join(lines)


def test_daily_state_log_link_is_reported_and_the_run_goes_on(tmp_path, monkeypatch, capsys):
    """state.log 자리에 링크가 있으면 대상을 건드리지 않고 화면에 한 줄만 더하며, daily 파일 · 텔레그램은 이어진다."""
    root, store = setup(tmp_path, monkeypatch, capsys)
    target = tmp_path / "elsewhere"
    target.write_text("keep\n")
    (root / "state.log").symlink_to(target)
    code, lines, tg = run(root, store, trades_from(bt_executions()))
    assert target.read_text() == "keep\n" and tg.sent()
    assert (root / "strategies" / "s1" / "daily" / "2025-02-02.md").is_file()
    notes = [ln for ln in lines if "state.log를 쓰지 못함" in ln]
    assert len(notes) == 1 and notes[0].startswith("s1: state.log를 쓰지 못함(") and notes[0].endswith(" · daily 파일은 남음")


def test_daily_link_py_fifo_does_not_hang(tmp_path, monkeypatch, capsys):
    """link.py 자리가 FIFO여도 멈추지 않고 그 전략만 확인 불가(종료 코드 4)로 남고, 정상인 둘째 전략은 계속 돈다."""
    root, store = setup(tmp_path, monkeypatch, capsys)
    _add_s2(tmp_path, root)
    link = root / "strategies" / "s1" / "link.py"
    link.unlink()
    os.mkfifo(link)

    def boom(*_):
        pytest.fail("run_daily가 5초 넘게 멈춤(FIFO 읽기)")  # Failed는 BaseException이라 run_daily의 except OSError에 삼켜지지 않는다

    old = signal.signal(signal.SIGALRM, boom)
    signal.alarm(5)
    try:
        code, lines, _ = run(root, store, trades_from(bt_executions()))
    finally:
        signal.alarm(0)
        signal.signal(signal.SIGALRM, old)
    assert code == 4, lines
    assert "- 전략: 확인 불가(대조하지 못함 · WorkspaceError)" in (root / "strategies" / "s1" / "daily" / "2025-02-02.md").read_text()
    md2 = (root / "strategies" / "s2" / "daily" / "2025-02-02.md").read_text(encoding="utf-8")  # s1이 확인 불가인 동안에도 s2는 정상으로 이어진다
    assert "대조하지 못함" not in md2 and "WorkspaceError" not in md2
    row2 = next(x for x in lines if x.startswith("s2: "))
    assert "대조하지 못함" not in row2 and row2.endswith("저장 .blackclip/strategies/s2/daily/2025-02-02.md")


def test_daily_line_written_again_on_rerun(tmp_path, monkeypatch, capsys):
    """같은 날 두 번 돌리면 줄이 둘, 읽을 때는 하루 하나(마지막 줄)."""
    root, store = setup(tmp_path, monkeypatch, capsys)
    trades = trades_from(bt_executions())
    run(root, store, trades)
    run(root, store, trades)
    from blackclip.daily.days import DAYS, read_days
    assert len((root / "strategies" / "s1" / "daily" / DAYS).read_text().splitlines()) == 2
    assert len(read_days(root, "s1")) == 1


def test_daily_exception_day_lands_in_days_jsonl(tmp_path, monkeypatch, capsys):
    """_one이 예외로 끝나면 run_daily가 만든 대조 실패 Day(nums={})가 실제 경로로 days.jsonl에 남는다."""
    import blackclip.daily.run as run_mod
    from blackclip.daily.days import read_days

    def boom(*a, **k):
        raise KeyError("boom")

    root, store = setup(tmp_path, monkeypatch, capsys)
    monkeypatch.setattr(run_mod, "reconcile", boom)
    run(root, store, trades_from(bt_executions()))
    rec = read_days(root, "s1")[-1]
    assert rec["day"] == "2025-02-02" and rec["nums"] == {} and rec["rep"] == "확인 불가(대조하지 못함 · KeyError)"


def test_daily_actual_counts_each_partial_fill_at_its_own_time(tmp_path, monkeypatch, capsys):
    """지정가 매도 하나가 마감 전 · 후 두 번에 나눠 체결되면 마감 전 몫만 그날 손익에 든다.

    myTrades는 체결마다 한 줄(id · orderId · time, 공식 문서 「Account trade list」)이라 두 줄이 같은 orderId를 단다.
    주문으로 묶어 첫 체결 시각에 전량을 세면 안 되고, 같은 두 체결을 다른 주문 둘로 받았을 때와 실제 손익이 같아야 한다.
    """
    from test_trades import SPOT

    from blackclip.daily.days import read_days
    after_end = int(datetime(2025, 2, 3, 6, 0, tzinfo=UTC).timestamp() * 1000)  # 2025-02-02 마감(2/3 0시) 뒤 · 실행(12시) 전

    def shaped(t):  # 공식 예시 한 줄(SPOT)의 열을 다 채운 myTrades 줄 · quoteQty = 가격 × 수량
        return dict(SPOT, **t, quoteQty=f"{float(t['price']) * float(t['qty']):.8f}", isMaker=not t["isBuyer"])

    def actual(sub, same_order):
        d = tmp_path / sub
        d.mkdir()
        root, store = setup(d, monkeypatch, capsys)
        trades = trades_from(bt_executions())
        assert [t["isBuyer"] for t in trades] == [True, False]  # 시험 전제: 왕복 하나(이 가격표의 10/30 신호)
        last = dict(trades[-1], qty=trades[0]["qty"])  # 산 만큼만 판다(체결 수량 환산 차로 초과 매도 = 기록 불완전이 되지 않게)
        half = f"{float(last['qty']) / 2:.6f}"
        rest = dict(last, id=last["id"] + 1000, orderId=last["orderId"] if same_order else last["orderId"] + 1000, qty=half,
                    commission=f"{float(last['commission']) / 2:.6f}", time=after_end)
        trades[-1] = dict(last, qty=half, commission=rest["commission"])
        code, lines, _ = run(root, store, [shaped(t) for t in trades + [rest]])
        assert code == 0, lines
        return read_days(root, "s1")[-1]["nums"]["actual"]

    one, two = actual("one_order", True), actual("two_orders", False)
    assert two is not None and one == two


def split_last_sell(parts):
    """예시 왕복(매수 · 매도)의 매도를 같은 orderId의 체결 여럿으로 나눈 myTrades 줄(수량 비율 parts, 수수료는 수량 비례).

    산 만큼만 판다(체결 수량 환산 차로 초과 매도 = 기록 불완전이 되지 않게). parts가 ["1"]이면 나누지 않은 것과 같다.
    """
    from test_trades import SPOT
    trades = trades_from(bt_executions())
    last = dict(trades[-1], qty=trades[0]["qty"])
    q, fee, unit = Decimal(last["qty"]), Decimal(last["commission"]), Decimal("0.00000001")
    cut = [(q * Decimal(x)).quantize(unit) for x in parts[:-1]]
    cut.append(q - sum(cut, Decimal(0)))
    trades[-1:] = [dict(last, id=last["id"] + i * 1000, qty=format(c, "f"), commission=format((fee * c / q).quantize(unit), "f"))
                   for i, c in enumerate(cut)]
    return [dict(SPOT, **t, quoteQty=f"{float(t['price']) * float(t['qty']):.8f}", isMaker=not t["isBuyer"]) for t in trades]


SPLIT = ["0.995", "0.004", "0.001"]
"""청산 주문 하나의 체결 셋: 첫 체결 뒤 남은 몫(0.5%)이 평평 문턱(FLAT_SHARE 1%) 아래라 체결마다 세면 가짜 왕복이 생기는 모양."""


def test_daily_one_exit_order_in_many_fills_is_one_round_trip(tmp_path, monkeypatch, capsys):
    """청산 주문 하나가 체결 셋으로 와도 왕복은 하나다: 거래 수 · 거래 1건 수익 · 실제 손익이 한 체결로 받았을 때와 같다.

    체결마다 왕복을 세면 첫 체결로 평평해진 뒤 남은 몫의 매도가 새 진입으로 보여 왕복 둘이 되고 봇 빈도가 늘었다.
    왕복은 주문 묶음으로 센다(백테스트 거래 목록도 주문 단위).
    """
    from blackclip.daily.days import read_days

    def nums(sub, parts):
        d = tmp_path / sub
        d.mkdir()
        root, store = setup(d, monkeypatch, capsys)
        code, lines, _ = run(root, store, split_last_sell(parts))
        assert code == 0, lines
        n = read_days(root, "s1")[-1]["nums"]
        return n["trade_n"], n["trade_skip"], n["trade_rets"], n["actual"]

    one = nums("one_fill", ["1"])
    assert one[0] == 1 and nums("three_fills", SPLIT) == one


def test_daily_trade_returns_keep_fees_paid_in_another_asset(tmp_path, monkeypatch, capsys):
    """BNB로 낸 수수료도 거래 1건 수익에 든다: 같은 왕복의 수수료를 USDT로 · 같은 값의 BNB(BNBUSDT 종가 2로 고정)로 받으면
    거래 수 · 거래 수익 · 실제 손익이 같다. 왕복을 주문 묶음으로 다시 셀 때 종가를 안 넘기면 BNB 수수료가 환산되지 않아
    거래 수익에서만 빠졌다."""
    from blackclip.daily.days import read_days
    bnb = make_prices()
    bnb[["Open", "High", "Low", "Close"]] = 2.0
    klines = {"BTCUSDT": make_prices(), "BNBUSDT": bnb}

    def nums(sub, asset):
        d = tmp_path / sub
        d.mkdir()
        root, store = setup(d, monkeypatch, capsys)
        trades = [dict(t, commissionAsset="BNB", commission=format(Decimal(t["commission"]) / 2, "f")) if asset == "BNB" else t
                  for t in split_last_sell(["1"])]
        code, lines, _ = run(root, store, trades, klines=klines)
        assert code == 0, lines
        n = read_days(root, "s1")[-1]["nums"]
        return n["trade_n"], n["trade_rets"], n["actual"]

    usdt = nums("usdt", "USDT")
    assert usdt[0] == 1 and usdt[1][0] < 0 and nums("bnb", "BNB") == usdt


@pytest.mark.parametrize("live", [datetime(2024, 12, 20, tzinfo=UTC), datetime(2024, 12, 4, tzinfo=UTC)], ids=["all_history_45", "all_history_61"])
def test_daily_all_history_counts_first_period_missed_order(tmp_path, monkeypatch, capsys, live):
    """45일째(2구간) · 61일째(3구간) — 첫 구간의 왕복 하나(체결 둘)를 빼면 이번 구간 missed_n 0, nums.all.missed_n 2."""
    from blackclip.daily.days import read_days
    # 기본 10/30 신호는 이 가격표에서 왕복이 하나뿐이고 그 왕복이 구간 경계를 가로지른다(매수 12/23 · 매도 1/23) → 첫 구간 안에 왕복이 있는 8/10 신호로
    root, store = setup(tmp_path, monkeypatch, capsys, start=live,
                        body=_body(signal="(c.rolling(8).mean() > c.rolling(10).mean()).astype(float)"))
    execs = bt_executions(live=live, windows=(8, 10))
    i = next(k for k, e in enumerate(execs) if e.time >= live)
    assert execs[i + 1].time < live + timedelta(days=30)  # 시험 전제: 빼는 왕복이 첫 구간 안
    code, lines, _ = run(root, store, trades_from(execs[:i] + execs[i + 2:]))
    nums = read_days(root, "s1")[-1]["nums"]
    assert code == 0, lines
    assert nums["period_start"] > live.date().isoformat() and nums["missed_n"] == 0 and nums["all"]["missed_n"] == 2


def test_daily_days_jsonl_link_is_reported_and_the_run_goes_on(tmp_path, monkeypatch, capsys):
    """days.jsonl 자리에 링크가 있으면 대상을 건드리지 않고 화면에 한 줄만 더하며, daily 파일 · state.log · 텔레그램은 이어진다."""
    root, store = setup(tmp_path, monkeypatch, capsys)
    target = tmp_path / "elsewhere"
    target.write_text("keep\n")
    folder = root / "strategies" / "s1" / "daily"
    folder.mkdir(exist_ok=True)
    (folder / "days.jsonl").symlink_to(target)
    code, lines, tg = run(root, store, trades_from(bt_executions()))
    assert target.read_text() == "keep\n" and tg.sent() and (root / "state.log").is_file()
    assert (folder / "2025-02-02.md").is_file()
    notes = [ln for ln in lines if "기계 판독 기록(days.jsonl)을 쓰지 못함" in ln]
    assert len(notes) == 1 and notes[0].startswith("s1: ") and notes[0].endswith(" · daily 파일은 남음")


def test_daily_days_jsonl_value_error_does_not_stop_the_run(tmp_path, monkeypatch, capsys):
    """days.jsonl 줄을 만들다 ValueError(숫자 모양 등)가 나도 한 줄만 더하고 daily 파일 · 텔레그램은 이어진다."""
    import blackclip.daily.run as run_mod

    def boom(*a, **k):
        raise ValueError("nan in /Users/secret-customer/repo")

    root, store = setup(tmp_path, monkeypatch, capsys)
    monkeypatch.setattr(run_mod, "append_day", boom)
    code, lines, tg = run(root, store, trades_from(bt_executions()))
    assert code == 0 and tg.sent() and (root / "strategies" / "s1" / "daily" / "2025-02-02.md").is_file()
    notes = [ln for ln in lines if "기계 판독 기록(days.jsonl)을 쓰지 못함(ValueError)" in ln]
    assert len(notes) == 1 and "secret-customer" not in "\n".join(lines)


@pytest.mark.parametrize("how", ["folder_link", "plain_file"])
def test_daily_md_write_failure_is_isolated_to_that_strategy(tmp_path, monkeypatch, capsys, how):
    """s1의 daily/ 자리가 레포 밖 폴더 링크 · 일반 파일이면 s1만 「daily 파일을 쓰지 못함」 줄 · 종료 4,
    s2의 daily 파일 · state.log · 텔레그램은 이어지고 레포 밖에는 아무것도 안 생긴다. 못 쓴 전략의 요약 줄은 저장했다고 하지 않는다."""
    root, store = setup(tmp_path, monkeypatch, capsys)
    _add_s2(tmp_path, root)
    outside = tmp_path / "outside"
    outside.mkdir()
    daily = root / "strategies" / "s1" / "daily"
    if how == "folder_link":
        daily.symlink_to(outside, target_is_directory=True)
    else:
        daily.write_text("not a folder", encoding="utf-8")
    code, lines, tg = run(root, store, trades_from(bt_executions()[:-1]))
    assert code == 4, lines
    notes = [x for x in lines if x.startswith("s1: daily 파일을 쓰지 못함(") and x.endswith(")")]
    assert len(notes) == 1, lines
    assert list(outside.iterdir()) == [] and (how == "folder_link" or daily.read_text(encoding="utf-8") == "not a folder")
    row1 = next(x for x in lines if x.startswith("s1: ") and "daily 파일을 쓰지 못함" not in x and "days.jsonl" not in x)
    assert row1.endswith(" · daily 파일 못 씀") and "저장 .blackclip" not in row1
    row2 = next(x for x in lines if x.startswith("s2: "))
    assert row2.endswith("저장 .blackclip/strategies/s2/daily/2025-02-02.md")
    assert (root / "strategies" / "s2" / "daily" / "2025-02-02.md").is_file()
    assert [json.loads(x)["strategy"] for x in (root / "state.log").read_text().splitlines()] == ["s1", "s2"]  # 나머지 쓰기는 이어진다
    assert tg.sent() and "전략 2개" in tg.sent()[-1]
    days_note = next(x for x in lines if "기계 판독 기록(days.jsonl)을 쓰지 못함" in x)  # 같은 폴더라 이것도 못 쓰는데, daily 파일이 남는다고 말하지 않는다
    assert days_note.startswith("s1: ") and not days_note.endswith("daily 파일은 남음")


# --- 가드레일을 못 읽은 전략만 확인 불가, 나머지는 계속(종료 4) ---

BREAK_GUARDRAIL = {
    "shape": lambda p: p.write_text("{}", encoding="utf-8"),
    "deep_json": lambda p: p.write_text("[" * 200_000, encoding="utf-8"),
    "huge_number": lambda p: p.write_text('{"normal_range": ' + "9" * 5000 + "}", encoding="utf-8"),
}
SKIP_TAIL = ") · 이 전략만 건너뜀"


def _broken_row(lines, name):
    """name의 「가드레일을 읽지 못함」 화면 줄(없으면 시험 실패)."""
    return next(x for x in lines if x.startswith(f"{name}: 확인 불가(가드레일을 읽지 못함 · "))


@pytest.mark.parametrize("how", sorted(BREAK_GUARDRAIL))
def test_daily_broken_guardrail_skips_only_that_strategy(tmp_path, monkeypatch, capsys, how):
    """s1의 가드레일이 깨져도(모양 · 깊은 JSON · 거대한 수) s2는 daily 파일 · 줄 · 텔레그램까지 평소대로, s1은 화면 줄 하나 · 파일 없음 · 종료 4."""
    root, store = setup(tmp_path, monkeypatch, capsys)
    _add_s2(tmp_path, root)
    BREAK_GUARDRAIL[how](root / "strategies" / "s1" / "guardrail.v1.yaml")
    code, lines, tg = run(root, store, trades_from(bt_executions()[:-1]))  # 마지막 매도를 빼 기록 불완전 없이 s2가 실제 숫자로 판정됨
    assert code == 4, lines
    row = _broken_row(lines, "s1")
    assert row.endswith(SKIP_TAIL) and "\n" not in row and ".blackclip" in row  # 사유는 WorkspaceError 메시지 그대로(경로 포함)
    assert sum(x.startswith("s1: ") for x in lines) == 1  # s1에 대한 줄은 이것 하나(대조 · 기록 줄 없음)
    row2 = next(x for x in lines if x.startswith("s2: "))
    assert " · 실제 -" in row2 and " · 같은 기간 BT " in row2 and row2.endswith("저장 .blackclip/strategies/s2/daily/2025-02-02.md")
    assert (root / "strategies" / "s2" / "daily" / "2025-02-02.md").is_file()
    assert not (root / "strategies" / "s1" / "daily").exists() or sorted(p.name for p in (root / "strategies" / "s1" / "daily").iterdir()) == []
    assert [json.loads(x)["strategy"] for x in (root / "state.log").read_text().splitlines()] == ["s2"]
    sent = tg.sent()
    assert sent and "전략 1개" in sent[-1] and sent[-1].splitlines()[-1] == "가드레일을 읽지 못한 전략 s1 · 컴퓨터에서 blackclip daily 출력 확인"
    assert str(tmp_path) not in "\n".join(sent) and "guardrail" not in "\n".join(sent)  # 경로 · 원문을 텔레그램에 싣지 않는다


def test_daily_unreadable_guardrail_file_is_skipped_too(tmp_path, monkeypatch, capsys):
    """권한이 없어 열 수 없는 가드레일도 그 전략만 확인 불가(OSError가 WorkspaceError로 바뀌어 올라옴)."""
    if os.geteuid() == 0:
        pytest.skip("root는 권한을 무시함")
    root, store = setup(tmp_path, monkeypatch, capsys)
    _add_s2(tmp_path, root)
    (root / "strategies" / "s1" / "guardrail.v1.yaml").chmod(0)
    code, lines, _ = run(root, store, trades_from(bt_executions()))
    assert code == 4 and _broken_row(lines, "s1").endswith(SKIP_TAIL)
    assert (root / "strategies" / "s2" / "daily" / "2025-02-02.md").is_file()


def test_daily_guardrail_oserror_reason_is_the_type_name_only(tmp_path, monkeypatch, capsys):
    """가드레일 읽기가 OSError(원문에 경로)로 실패하면 사유는 종류 이름만(daily의 link.py · days.jsonl 처리와 같은 규칙)."""
    import blackclip.daily.run as run_mod
    real = run_mod.read_guardrail

    def flaky(root_, name):
        if name == "s1":
            raise OSError("EIO /Users/secret-customer/repo/.blackclip")
        return real(root_, name)

    root, store = setup(tmp_path, monkeypatch, capsys)
    _add_s2(tmp_path, root)
    monkeypatch.setattr(run_mod, "read_guardrail", flaky)
    code, lines, tg = run(root, store, trades_from(bt_executions()))
    assert code == 4 and _broken_row(lines, "s1") == "s1: 확인 불가(가드레일을 읽지 못함 · OSError" + SKIP_TAIL
    assert "secret-customer" not in "\n".join(lines) + "\n".join(tg.sent())
    assert (root / "strategies" / "s2" / "daily" / "2025-02-02.md").is_file()


def test_daily_broken_guardrail_reason_goes_through_show(tmp_path, monkeypatch, capsys):
    """사유에 줄바꿈 · ESC가 있어도 화면 줄 하나로(레포 글은 scan.show 경계를 지남)."""
    import blackclip.daily.run as run_mod
    from blackclip.workspace import WorkspaceError
    real = run_mod.read_guardrail

    def flaky(root_, name):
        if name == "s1":
            raise WorkspaceError("나쁜 글\n\x1b[31m빨강")
        return real(root_, name)

    root, store = setup(tmp_path, monkeypatch, capsys)
    _add_s2(tmp_path, root)
    monkeypatch.setattr(run_mod, "read_guardrail", flaky)
    _, lines, _ = run(root, store, trades_from(bt_executions()))
    assert _broken_row(lines, "s1") == "s1: 확인 불가(가드레일을 읽지 못함 · 나쁜 글\\u000a\\u001b[31m빨강" + SKIP_TAIL


def test_daily_every_guardrail_broken_is_exit_2_with_only_the_lines(tmp_path, monkeypatch, capsys):
    """읽을 수 있는 가드레일이 하나도 없고 깨진 전략만 있으면 화면 줄들만 · 종료 2(체결 받기 · 텔레그램 · 헬스체크 없음 · 「저장된 전략이 없습니다」 안내 아님)."""
    root, store = setup(tmp_path, monkeypatch, capsys)
    _add_s2(tmp_path, root)
    set_notify(root, healthcheck="https://hc-ping.com/abc")
    for n in ("s1", "s2"):
        BREAK_GUARDRAIL["shape"](root / "strategies" / n / "guardrail.v1.yaml")
    pings = []
    code, lines, tg = run(root, store, trades_from(bt_executions()), pings=pings)
    assert code == 2 and len(lines) == 2
    assert [x.split(":")[0] for x in lines] == ["s1", "s2"] and all(x.endswith(SKIP_TAIL) for x in lines)
    assert tg.sent() == [] and pings == [] and not (root / "state.log").exists() and not (root / "fills").exists()


def test_daily_no_saved_guardrail_keeps_the_old_hint(tmp_path, monkeypatch, capsys):
    """저장된 가드레일이 아예 없으면 지금 안내 그대로(깨진 전략이 없을 때)."""
    (tmp_path / ".git").mkdir()
    root = ensure(tmp_path)
    code, lines = run_daily(root, MemStore(), maker(routes()), lambda m: None, lambda t: None, NOW)
    assert code == 2 and lines == ["가드레일이 저장된 전략이 없습니다 · blackclip save <전략> 뒤 blackclip live <전략>"]


@pytest.mark.parametrize("kind", ["fifo", "link"])
def test_daily_guardrail_version_file_that_is_not_a_regular_file_is_not_seen(tmp_path, monkeypatch, capsys, kind):
    """링크 · FIFO인 가드레일 판 파일은 versions가 세지 않아 읽기까지 가지 않는다: 멈추지 않고 그 전략은 목록에 없다.
    확인 불가가 아니라 「없는 전략」이다 — 예전 계획의 「가드레일 링크 · FIFO → 종료 4」와 다른 현재 동작."""
    root, store = setup(tmp_path, monkeypatch, capsys)
    _add_s2(tmp_path, root)
    path = root / "strategies" / "s1" / "guardrail.v1.yaml"
    path.rename(tmp_path / "moved.yaml")
    if kind == "fifo":
        os.mkfifo(path)
    else:
        path.symlink_to(tmp_path / "moved.yaml")

    def boom(*_):
        pytest.fail("run_daily가 5초 넘게 멈춤(FIFO 읽기)")

    old = signal.signal(signal.SIGALRM, boom)
    signal.alarm(5)
    try:
        _, lines, _ = run(root, store, trades_from(bt_executions()))
    finally:
        signal.alarm(0)
        signal.signal(signal.SIGALRM, old)
    assert not any(x.startswith("s1: ") for x in lines)
    assert (root / "strategies" / "s2" / "daily" / "2025-02-02.md").is_file() and not (root / "strategies" / "s1" / "daily").exists()
    assert _mixed_lines(root, "s2") == _reasons(LOST_REASON)  # 판을 잃은 s1의 live.json 계좌 = s2는 혼자 계좌가 아니라 섞였을 수 있음


def test_daily_quiet_changes_only_has_no_note_line_but_still_exits_4(tmp_path, monkeypatch, capsys):
    """텔레그램 notes는 체결 받기 실패 notes와 같은 길(매일 묶음 끝)이라 「바뀔때만」이면 안 보내고, 화면 줄 · 종료 4는 그대로."""
    root, store = setup(tmp_path, monkeypatch, capsys)
    _add_s2(tmp_path, root)
    set_notify(root, quiet="바뀔때만")
    BREAK_GUARDRAIL["shape"](root / "strategies" / "s1" / "guardrail.v1.yaml")
    code, lines, tg = run(root, store, trades_from(bt_executions()))
    assert code == 4 and _broken_row(lines, "s1")
    assert not any("가드레일을 읽지 못한 전략" in x for x in tg.sent())


# --- 가드레일을 읽지 못한 전략의 live.json 계좌는 「섞였을 수 있음」(시장 · 종목을 몰라 계좌 단위) ---


OVERLAP_REASON = "같은 계좌에서 종목이 겹치는 다른 전략이 있어 이 전략 몫을 나눌 수 없음(계좌를 나누면 따로 봄)"
LOST_REASON = "같은 계좌에 가드레일을 읽지 못한 다른 전략이 있어 이 전략 몫을 나눌 수 없음(가드레일을 고치면 따로 봄)"


def _mixed_lines(root, name):
    """name의 daily 파일 실행 · 봇 칸 줄(「- 실행: …」 「- 봇: …」)."""
    md = (root / "strategies" / name / "daily" / "2025-02-02.md").read_text(encoding="utf-8")
    return [x for x in md.splitlines() if x.startswith(("- 실행: ", "- 봇: "))]


def _reasons(reason):
    """섞인 기록 사유가 실행 · 봇 칸에 그대로 저장된 줄 둘."""
    return [f"- 실행: 점검 불가(섞인 기록) · {reason}", f"- 봇: 점검 불가(섞인 기록) · {reason}"]


def _marked_mixed(root, name):
    """name의 daily 파일 실행 · 봇 칸이 「점검 불가(섞인 기록)」인지."""
    md = (root / "strategies" / name / "daily" / "2025-02-02.md").read_text(encoding="utf-8")
    return "- 실행: 점검 불가(섞인 기록)" in md and "- 봇: 점검 불가(섞인 기록)" in md


def test_daily_broken_guardrail_marks_the_same_account_strategy_as_mixed(tmp_path, monkeypatch, capsys):
    """s1 가드레일이 깨졌고 live.json은 읽혀 같은 계좌(main)면 정상인 s2의 실제 손익에 s1 체결이 섞였을 수 있어 s2 daily 파일 · 줄에 섞인 기록 표시가 따라간다."""
    root, store = setup(tmp_path, monkeypatch, capsys)
    _add_s2(tmp_path, root)
    BREAK_GUARDRAIL["shape"](root / "strategies" / "s1" / "guardrail.v1.yaml")
    code, lines, _ = run(root, store, trades_from(bt_executions()[:-1]))
    assert code == 4 and _mixed_lines(root, "s2") == _reasons(LOST_REASON)  # 종목은 모르니 「종목이 겹치는」이라 쓰지 않는다
    assert "섞인 기록" in next(x for x in lines if x.startswith("s2: ")) and "종목이 겹치는" not in (
        root / "strategies" / "s2" / "daily" / "2025-02-02.md").read_text(encoding="utf-8")


def test_daily_overlapping_symbols_keep_the_overlap_reason(tmp_path, monkeypatch, capsys):
    """깨진 가드레일이 없고 같은 계좌 · 같은 종목(BTCUSDT)에 정상 전략이 둘이면 둘 다 지금 문구(종목이 실제로 겹침) 그대로."""
    root, store = setup(tmp_path, monkeypatch, capsys)
    _add_s2(tmp_path, root)
    run(root, store, trades_from(bt_executions()[:-1]))
    assert _mixed_lines(root, "s1") == _mixed_lines(root, "s2") == _reasons(OVERLAP_REASON)


def test_daily_overlap_reason_wins_when_a_broken_guardrail_is_in_the_account_too(tmp_path, monkeypatch, capsys):
    """종목이 실제로 겹치는 s1 · s2에 같은 계좌의 깨진 s3까지 있으면 확인된 사실(겹침) 문구가 우선."""
    root, store = setup(tmp_path, monkeypatch, capsys)
    _add_s2(tmp_path, root)
    write_link(tmp_path, _body(), "s3")
    assert main(["save", "s3", "--start", "2024-12-20"]) == 0
    write_live(root, "s3", "main", "USDT", LIVE - timedelta(hours=12), Decimal(10000), {"BTCUSDT": Decimal(0)})
    BREAK_GUARDRAIL["shape"](root / "strategies" / "s3" / "guardrail.v1.yaml")
    code, _, _ = run(root, store, trades_from(bt_executions()[:-1]))
    assert code == 4 and _mixed_lines(root, "s1") == _mixed_lines(root, "s2") == _reasons(OVERLAP_REASON)


@pytest.mark.parametrize("other", ["다른 계좌", "live.json 없음", "live.json 깨짐"])
def test_daily_broken_guardrail_without_a_known_same_account_does_not_mark_mixed(tmp_path, monkeypatch, capsys, other):
    """깨진 전략의 계좌를 모르거나(live.json 없음 · 깨짐 → read_live가 None) 다른 계좌면 s2에 아무 표시도 하지 않는다."""
    root, store = setup(tmp_path, monkeypatch, capsys)
    _add_s2(tmp_path, root)
    BREAK_GUARDRAIL["shape"](root / "strategies" / "s1" / "guardrail.v1.yaml")
    live1 = root / "strategies" / "s1" / "live.json"
    if other == "다른 계좌":
        write_live(root, "s1", "sub1", "USDT", LIVE - timedelta(hours=12), Decimal(10000), {"BTCUSDT": Decimal(0)})
    elif other == "live.json 없음":
        live1.unlink()
    else:
        live1.write_text("{ broken", encoding="utf-8")
    code, lines, _ = run(root, store, trades_from(bt_executions()[:-1]))
    assert code == 4 and not _marked_mixed(root, "s2")
    assert "섞인 기록" not in next(x for x in lines if x.startswith("s2: "))


def test_daily_guardrail_lost_without_an_error_marks_the_same_account_strategy_as_mixed(tmp_path, monkeypatch, capsys):
    """가드레일 판을 지워 읽기 오류 없이 목록에서 빠진 s1도, live.json이 읽히면
    같은 계좌의 s2를 섞였을 수 있음으로 본다(판 파일 링크 · FIFO를 목록에서 빼면서 생긴 회귀를 같은 규칙으로 닫음)."""
    root, store = setup(tmp_path, monkeypatch, capsys)
    _add_s2(tmp_path, root)
    (root / "strategies" / "s1" / "guardrail.v1.yaml").unlink()
    code, lines, _ = run(root, store, trades_from(bt_executions()[:-1]))
    assert code == 0 and not any(x.startswith("s1: ") for x in lines)  # 종료 코드 · 목록에서 빠지는 것은 그대로
    assert _mixed_lines(root, "s2") == _reasons(LOST_REASON)


def test_daily_guardrail_behind_folder_link_does_not_mark_mixed(tmp_path, monkeypatch, capsys):
    """전략 폴더가 링크이면 read_live가 그 안의 live.json을 따라가 읽지 않아 계좌를 모르니 s2에 표시하지 않는다.
    옛 시험은 링크를 따라 읽어 s2를 섞였을 수 있음으로 봤다(folder_link) · 안전 읽기가 이 뜻을 바꿈."""
    root, store = setup(tmp_path, monkeypatch, capsys)
    _add_s2(tmp_path, root)
    folder = root / "strategies" / "s1"
    folder.rename(tmp_path / "s1_real")
    folder.symlink_to(tmp_path / "s1_real", target_is_directory=True)
    code, lines, _ = run(root, store, trades_from(bt_executions()[:-1]))
    assert code == 0 and not any(x.startswith("s1: ") for x in lines)  # 목록에서 빠지는 것은 그대로
    assert not _marked_mixed(root, "s2")


def test_daily_guardrail_deleted_without_a_live_json_does_not_mark_mixed(tmp_path, monkeypatch, capsys):
    """판을 잃은 s1의 live.json도 없으면 계좌를 몰라 s2에 아무 표시도 하지 않는다(영향 없음)."""
    root, store = setup(tmp_path, monkeypatch, capsys)
    _add_s2(tmp_path, root)
    (root / "strategies" / "s1" / "guardrail.v1.yaml").unlink()
    (root / "strategies" / "s1" / "live.json").unlink()
    code, _, _ = run(root, store, trades_from(bt_executions()[:-1]))
    assert code == 0 and not _marked_mixed(root, "s2")


def test_daily_bad_match_window_bars_skips_only_that_strategy(tmp_path, monkeypatch, capsys):
    """s1 가드레일의 `match_window_bars`가 리스트여도 TypeError로 전체가 멈추지 않고 s1만 확인 불가(종료 4),
    정상인 s2는 daily 파일 · 텔레그램까지 이어진다."""
    root, store = setup(tmp_path, monkeypatch, capsys)
    _add_s2(tmp_path, root)
    _edit_guardrail(root, lambda g: g.update(match_window_bars=[]))
    code, lines, tg = run(root, store, trades_from(bt_executions()[:-1]))
    assert code == 4, lines
    assert "가드레일 모양이 다릅니다(match_window_bars)" in _broken_row(lines, "s1")
    assert (root / "strategies" / "s2" / "daily" / "2025-02-02.md").is_file() and tg.sent()


def test_daily_skips_stopped_strategy_without_files_or_telegram(tmp_path, monkeypatch, capsys):
    """중지한 전략은 대조 루프 처음에서 한 줄만 남기고 건너뜀(종료 0) — md · days.jsonl · 텔레그램 없음. 기록 손상은 종료 4, 텔레그램은 손상 글 1통뿐."""
    root, store = setup(tmp_path, monkeypatch, capsys)
    assert main(["record", "s1", "--state", "중지"]) == 0
    code, lines, tg = run(root, store, trades_from(bt_executions()))
    assert code == 0 and any(x.startswith("s1: 중지(") and x.endswith(") · 대조 안 함") for x in lines), lines
    daily = root / "strategies" / "s1" / "daily"
    assert not list(daily.glob("*.md")) and not (daily / "days.jsonl").exists()
    assert not any("s1" in m for m in tg.sent())
    rec = root / "strategies" / "s1" / "record.md"  # 깨진 기록도 실제 daily 경로에서 멈춘 채
    rec.write_text("<<<<<<< HEAD\n" + rec.read_text() + "=======\n>>>>>>> origin/main\n")
    code, lines, tg = run(root, store, trades_from(bt_executions()))
    assert code == 4 and ("s1: 기록 손상 · 전략 기록(record.md)을 읽지 못함 · 대조 멈춤 · "
                          "git 충돌이면 풀고, 아니면 record.md를 지운 뒤 blackclip record로 다시") in lines, lines
    assert not list(daily.glob("*.md"))
    assert tg.sent() == [broken_message("s1")]  # s1 손상 글 1통, 대조 글(상태 바뀜 · 묶음) 0통


def test_daily_telegram_failure_line_drops_kept_files_when_no_md_written(tmp_path, monkeypatch, capsys):
    """이번 실행에서 daily md를 하나도 못 썼으면 텔레그램 실패 줄이 「daily 파일은 남음」을 말하지 않는다(썼으면 그대로)."""
    root, store = setup(tmp_path, monkeypatch, capsys)
    daily = root / "strategies" / "s1" / "daily"
    daily.write_text("not a folder", encoding="utf-8")  # daily/ 자리를 일반 파일로 막아 md를 못 쓰게
    code, lines, _ = run(root, store, trades_from(bt_executions()), tg=FakeTelegram(fail="sendMessage"))
    assert code == 5 and any("daily 파일을 쓰지 못함(" in x for x in lines), lines
    assert lines[-1].startswith("텔레그램: 못 보냄(") and "daily 파일은 남음" not in lines[-1]
    assert lines[-1].endswith(" · 바뀐 상태는 다음 실행에서 다시 보냄")


def test_daily_stopped_strategy_with_broken_guardrail_gets_one_stopped_line_and_no_exit_code(tmp_path, monkeypatch, capsys):
    """중지한 전략은 가드레일을 못 읽어도 끈 전략 줄 하나만 내고 그 때문에 종료 코드를 올리지 않는다(s2는 평소대로)."""
    root, store = setup(tmp_path, monkeypatch, capsys)
    _add_s2(tmp_path, root)
    assert main(["record", "s1", "--state", "중지"]) == 0
    (root / "strategies" / "s1" / "guardrail.v1.yaml").write_text("{}", encoding="utf-8")
    code, lines, _ = run(root, store, trades_from(bt_executions()[:-1]))
    assert code == 0, lines
    s1 = [x for x in lines if x.startswith("s1: ")]
    assert len(s1) == 1 and s1[0].startswith("s1: 중지(") and s1[0].endswith(") · 대조 안 함"), lines
    assert (root / "strategies" / "s2" / "daily" / "2025-02-02.md").is_file()


def _off_account_setup(tmp_path, monkeypatch, capsys, state):
    """s1(계좌 main)을 state로 끄고, 살아 있는 s2는 다른 계좌 sub에 둔다. 키는 sub만 있다(main 키 없음)."""
    root, store = setup(tmp_path, monkeypatch, capsys)
    _add_s2(tmp_path, root)
    write_live(root, "s2", "sub", "USDT", LIVE - timedelta(hours=12), Decimal(10000), {"BTCUSDT": Decimal(0)})
    store.items.pop(account_name("binance", "main"))
    store.items[account_name("binance", "sub")] = CREDS
    assert main(["record", "s1", "--state", state]) == 0
    return root, store


def test_daily_retired_only_account_is_not_fetched(tmp_path, monkeypatch, capsys):
    """은퇴 전략만 쓰는 계좌는 체결을 받지 않는다 — 키가 없어도 출력 줄 · 텔레그램 · 종료 코드에 그 계좌가 없다."""
    root, store = _off_account_setup(tmp_path, monkeypatch, capsys, "은퇴")
    asked = []
    orig = store.load
    store.load = lambda account: (asked.append(account), orig(account))[1]
    code, lines, tg = run(root, store, trades_from(bt_executions()[:-1]))
    assert code == 0, lines
    assert not any("binance main" in x for x in lines) and account_name("binance", "main") not in asked, (lines, asked)
    assert not any("main" in m for m in tg.sent()) and (root / "strategies" / "s2" / "daily" / "2025-02-02.md").is_file()


def test_daily_stopped_only_account_fetch_failure_is_output_only(tmp_path, monkeypatch, capsys):
    """중지 전략만 쓰는 계좌는 계속 받되 받기 실패는 출력 줄만 — 텔레그램 notes · 종료 코드(4)에 올리지 않는다."""
    root, store = _off_account_setup(tmp_path, monkeypatch, capsys, "중지")
    code, lines, tg = run(root, store, trades_from(bt_executions()[:-1]))
    assert any("binance main" in x for x in lines), lines  # 받기를 시도했고 출력에는 남는다
    assert code == 0, lines
    assert not any("main" in m or "체결 받기" in m for m in tg.sent()), tg.sent()


def test_daily_stopped_strategy_on_same_account_still_marks_the_live_one_mixed(tmp_path, monkeypatch, capsys):
    """s1을 중지해도 같은 계좌 · 같은 종목의 s2는 혼자 계좌로 판정되지 않고 섞인 기록(종목 겹침)으로 남는다."""
    root, store = setup(tmp_path, monkeypatch, capsys)
    _add_s2(tmp_path, root)
    assert main(["record", "s1", "--state", "중지"]) == 0
    code, lines, _ = run(root, store, trades_from(bt_executions()[:-1]))
    assert code == 0, lines
    assert _marked_mixed(root, "s2")


def _break_record(root, name):
    """name의 record.md를 git 충돌 표지로 깨 「기록 손상」으로 만든다."""
    (root / "strategies" / name / "record.md").write_text("<<<<<<< HEAD\n=======\n>>>>>>> origin/main\n", encoding="utf-8")


def _broken_sent(tg, name="s2"):
    """보낸 글 가운데 name의 손상 글(맨 윗줄로 가림)."""
    return [m for m in tg.sent() if m.startswith(f"기록 손상: {name}의")]


def _notified(root):
    return json.loads((root / "notified.json").read_text(encoding="utf-8")) if (root / "notified.json").exists() else {}


def test_broken_record_sends_once_then_quiet(tmp_path, monkeypatch, capsys):
    """실전 s1 + 손상 s2 — 첫 daily에 s2 손상 글 1통(s1의 평소 글도 함께), 둘째 날은 손상 글 0통. 종료 4. notified에 record:s2."""
    root, store = setup(tmp_path, monkeypatch, capsys)
    _add_s2(tmp_path, root)
    _break_record(root, "s2")
    trades = trades_from(bt_executions()[:-1])
    code, _, tg = run(root, store, trades)
    assert code == 4 and _broken_sent(tg) == [broken_message("s2")]
    assert any(m.startswith("매일 묶음") for m in tg.sent()) and any("s1" in m for m in tg.sent())
    assert _notified(root)["record:s2"] == {"기록": "기록 손상"}
    code, _, tg = run(root, store, trades)
    assert code == 4 and _broken_sent(tg) == []


def test_broken_only_strategy_still_sends(tmp_path, monkeypatch, capsys):
    """대조가 돈 전략이 0개(s1은 중지)이고 s2만 손상이어도 `quiet="매일"`에서 정확히 1통(손상 글). 「전략 0개」 묶음은 없다."""
    root, store = setup(tmp_path, monkeypatch, capsys)
    _add_s2(tmp_path, root)
    assert main(["record", "s1", "--state", "중지"]) == 0
    _break_record(root, "s2")
    set_notify(root, quiet="매일")
    code, lines, tg = run(root, store, trades_from(bt_executions()[:-1]))
    assert tg.sent() == [broken_message("s2")] and code == 4, lines
    assert lines[-1] == "텔레그램: 보냄"


def test_broken_and_unreadable_guardrail_still_sends(tmp_path, monkeypatch, capsys):
    """s2의 기록도 가드레일도 깨져 유효한 가드레일이 0개(이른 반환 길)여도 손상 글 1통. 종료 코드는 지금 값(4) 그대로."""
    root, store = setup(tmp_path, monkeypatch, capsys)
    _add_s2(tmp_path, root)
    (root / "strategies" / "s1" / "guardrail.v1.yaml").unlink()
    (root / "strategies" / "s2" / "guardrail.v1.yaml").write_text("{}", encoding="utf-8")
    _break_record(root, "s2")
    code, lines, tg = run(root, store, trades_from(bt_executions()))
    assert code == 4 and tg.sent() == [broken_message("s2")], lines
    assert lines[-1] == "텔레그램: 보냄" and any(x.startswith("s2: 기록 손상") for x in lines)
    code, _, tg = run(root, store, trades_from(bt_executions()))
    assert code == 4 and tg.sent() == []  # 둘째 날은 조용


def test_broken_record_notice_survives_quiet_days(tmp_path, monkeypatch, capsys):
    """`quiet="바뀔때만"`에서도 손상 글은 간다(상태 바뀜이라). 묶음은 안 간다."""
    root, store = setup(tmp_path, monkeypatch, capsys)
    _add_s2(tmp_path, root)
    _break_record(root, "s2")
    set_notify(root, quiet="바뀔때만")
    _, _, tg = run(root, store, trades_from(bt_executions()[:-1]))
    assert _broken_sent(tg) == [broken_message("s2")] and not any(m.startswith("매일 묶음") for m in tg.sent())


def test_broken_again_after_fix_sends_again(tmp_path, monkeypatch, capsys):
    """손상(1통) → 고친 날(바뀔때만 · 상태 변화 0 · 보낼 글 0통)에 notified의 record:s2가 지워져 저장 → 다시 깨지면 1통."""
    root, store = setup(tmp_path, monkeypatch, capsys)
    _add_s2(tmp_path, root)
    assert main(["record", "s2", "--state", "중지"]) == 0  # s2는 처음부터 끈 전략 → 대조 글은 s1만
    set_notify(root, quiet="바뀔때만")
    trades = trades_from(bt_executions()[:-1])
    now = datetime(2025, 1, 23, 12, 0, tzinfo=UTC)  # 일요일이 아니라 매주 회고 글이 끼지 않는 날
    _break_record(root, "s2")
    assert _broken_sent(run(root, store, trades, now=now)[2]) == [broken_message("s2")] and "record:s2" in _notified(root)
    (root / "strategies" / "s2" / "record.md").unlink()  # 고침: 지운 뒤 다시 적음
    assert main(["record", "s2", "--state", "중지"]) == 0
    code, lines, tg = run(root, store, trades, now=now)
    assert tg.sent() == [] and lines[-1] == "텔레그램: 바뀐 상태 없음 · 안 보냄"
    assert "record:s2" not in _notified(root)
    _break_record(root, "s2")
    assert _broken_sent(run(root, store, trades, now=now)[2]) == [broken_message("s2")]


def test_bundle_names_broken_strategy(tmp_path, monkeypatch, capsys):
    """둘째 날 묶음(quiet 매일)이 가면 끝에 `기록 손상 s2: 매일 대조가 멈춰 있어요` 한 줄, 손상 글은 0통."""
    root, store = setup(tmp_path, monkeypatch, capsys)
    _add_s2(tmp_path, root)
    _break_record(root, "s2")
    set_notify(root, quiet="매일")
    trades = trades_from(bt_executions()[:-1])
    run(root, store, trades)
    _, _, tg = run(root, store, trades)
    assert _broken_sent(tg) == [] and tg.sent()[-1].splitlines()[-1] == broken_note("s2") == "기록 손상 s2: 매일 대조가 멈춰 있어요"


def test_stopped_strategy_sends_no_broken_notice(tmp_path, monkeypatch, capsys):
    """중지 s3(기록이 멀쩡한 끈 전략)은 손상 글도 묶음 끝 줄도 없다."""
    root, store = setup(tmp_path, monkeypatch, capsys)
    _add_s2(tmp_path, root)
    assert main(["record", "s2", "--state", "중지"]) == 0
    _, _, tg = run(root, store, trades_from(bt_executions()[:-1]))
    assert not any("기록 손상" in m for m in tg.sent()) and "record:s2" not in _notified(root)


def test_broken_only_telegram_failure_exits_5(tmp_path, monkeypatch, capsys):
    """손상 s2뿐인데 텔레그램 토큰이 없으면 종료 5(손상 4보다 앞섬) · 「텔레그램: 못 보냄(…)」 줄 · 안 보냈으니 notified도 안 씀."""
    root, store = setup(tmp_path, monkeypatch, capsys)
    _add_s2(tmp_path, root)
    assert main(["record", "s1", "--state", "중지"]) == 0
    _break_record(root, "s2")
    store.items.pop(TELEGRAM_ACCOUNT)
    code, lines, tg = run(root, store, trades_from(bt_executions()[:-1]))
    assert code == 5 and tg.sent() == [] and "record:s2" not in _notified(root), lines
    assert lines[-1].startswith("텔레그램: 못 보냄(텔레그램 토큰이 없습니다")


def test_broken_before_chat_link_says_not_sent(tmp_path, monkeypatch, capsys):
    """대화 연결 전(chat_id 없음)이면 손상 글도 안 보내고 「대화 연결 전이라 안 보냄」 줄만 낸다. 종료 4."""
    root, store = setup(tmp_path, monkeypatch, capsys, link_telegram=False)
    _add_s2(tmp_path, root)
    assert main(["record", "s1", "--state", "중지"]) == 0
    _break_record(root, "s2")
    code, lines, tg = run(root, store, trades_from(bt_executions()[:-1]))
    assert code == 4 and tg.sent() == [] and "record:s2" not in _notified(root)
    assert lines[-1] == "텔레그램: 대화 연결 전이라 안 보냄 · blackclip connect --telegram"


def _fix_record(root, name="s2"):
    """깨진 record.md를 지우고 blackclip record로 다시 적는다(손상 글이 알려 준 고치는 길, 상태는 중지)."""
    (root / "strategies" / name / "record.md").unlink()
    assert main(["record", name, "--state", "중지"]) == 0


def test_broken_again_after_fix_when_nothing_reconciled(tmp_path, monkeypatch, capsys):
    """고친 날 대조가 돈 전략도 깨진 전략도 없어도(s1 중지) notified의 record:s2가 지워져, 다시 깨지면 1통."""
    root, store = setup(tmp_path, monkeypatch, capsys)
    _add_s2(tmp_path, root)
    assert main(["record", "s1", "--state", "중지"]) == 0
    _break_record(root, "s2")
    trades = trades_from(bt_executions()[:-1])
    assert _broken_sent(run(root, store, trades)[2]) == [broken_message("s2")] and "record:s2" in _notified(root)
    _fix_record(root)
    code, _, tg = run(root, store, trades)
    assert code == 0 and tg.sent() == [] and "record:s2" not in _notified(root)
    _break_record(root, "s2")
    assert _broken_sent(run(root, store, trades)[2]) == [broken_message("s2")]


def test_broken_again_after_fix_on_early_return_path(tmp_path, monkeypatch, capsys):
    """유효한 가드레일이 0개인 이른 반환 길에서도 고친 날 record:s2가 지워져 다시 깨지면 1통."""
    root, store = setup(tmp_path, monkeypatch, capsys)
    _add_s2(tmp_path, root)
    (root / "strategies" / "s1" / "guardrail.v1.yaml").unlink()
    (root / "strategies" / "s2" / "guardrail.v1.yaml").write_text("{}", encoding="utf-8")
    _break_record(root, "s2")
    trades = trades_from(bt_executions())
    assert _broken_sent(run(root, store, trades)[2]) == [broken_message("s2")]
    _fix_record(root)
    _, _, tg = run(root, store, trades)
    assert tg.sent() == [] and "record:s2" not in _notified(root)
    _break_record(root, "s2")
    assert _broken_sent(run(root, store, trades)[2]) == [broken_message("s2")]


@pytest.mark.parametrize("how", ["send_fails", "no_token"])
def test_broken_key_is_cleared_even_when_telegram_fails_on_the_fix_day(tmp_path, monkeypatch, capsys, how):
    """고친 날 텔레그램이 실패해도(보내기 실패 · 토큰 없음, 종료 5) record:s2는 지워져 다시 깨지면 1통."""
    root, store = setup(tmp_path, monkeypatch, capsys)
    _add_s2(tmp_path, root)
    _break_record(root, "s2")
    trades = trades_from(bt_executions()[:-1])
    assert _broken_sent(run(root, store, trades)[2]) == [broken_message("s2")]
    _fix_record(root)
    token = store.items.pop(TELEGRAM_ACCOUNT) if how == "no_token" else None
    code, lines, _ = run(root, store, trades, tg=FakeTelegram(fail="sendMessage") if how == "send_fails" else None)
    assert code == 5 and lines[-1].startswith("텔레그램: 못 보냄("), lines
    assert "record:s2" not in _notified(root)
    if token:
        store.items[TELEGRAM_ACCOUNT] = token
    _break_record(root, "s2")
    assert _broken_sent(run(root, store, trades)[2]) == [broken_message("s2")]


def test_broken_record_without_guardrail_shows_line_and_exits_4(tmp_path, monkeypatch, capsys):
    """가드레일 없이 record.md만 깨진 폴더도 화면에 기존 손상 줄이 나오고 종료 4. 유효한 가드레일이 0개여도 안내 줄에 밀리지 않는다."""
    root, store = setup(tmp_path, monkeypatch, capsys)
    _add_s2(tmp_path, root)
    assert main(["record", "s2", "--state", "중지"]) == 0
    (root / "strategies" / "s2" / "guardrail.v1.yaml").unlink()
    _break_record(root, "s2")
    head = "s2: 기록 손상 · 전략 기록(record.md)을 읽지 못함 · 대조 멈춤 · "
    code, lines, tg = run(root, store, trades_from(bt_executions()[:-1]))
    assert code == 4 and sum(x.startswith(head) for x in lines) == 1, lines
    assert _broken_sent(tg) == [broken_message("s2")]
    (root / "strategies" / "s1" / "guardrail.v1.yaml").unlink()  # 유효한 가드레일 0개(이른 반환 길)
    code, lines, _ = run(root, store, trades_from(bt_executions()[:-1]))
    assert code == 4 and sum(x.startswith(head) for x in lines) == 1, lines


def _early_return_with_broken_s2(tmp_path, monkeypatch, capsys):
    """유효한 가드레일 0개 이른 반환 길: s1은 가드레일을 못 읽고(`{}`) s2는 가드레일 없이 record.md만 git 충돌."""
    root, store = setup(tmp_path, monkeypatch, capsys)
    _add_s2(tmp_path, root)
    (root / "strategies" / "s1" / "guardrail.v1.yaml").write_text("{}", encoding="utf-8")
    (root / "strategies" / "s2" / "guardrail.v1.yaml").unlink()
    _break_record(root, "s2")
    return root, store


def test_early_return_keeps_exit_4_when_a_record_is_broken(tmp_path, monkeypatch, capsys):
    """가드레일이 읽히는 전략이 0개여도 기록 손상이 있으면 종료 4(2로 덮지 않음) · 손상 글 1통, 토큰이 없으면 5."""
    root, store = _early_return_with_broken_s2(tmp_path, monkeypatch, capsys)
    trades = trades_from(bt_executions())
    code, lines, tg = run(root, store, trades)
    assert code == 4 and tg.sent() == [broken_message("s2")], lines
    assert any(x.startswith("s1: ") for x in lines) and any(x.startswith("s2: 기록 손상") for x in lines)
    (root / "notified.json").unlink()
    store.items.pop(TELEGRAM_ACCOUNT)
    code, lines, tg = run(root, store, trades)
    assert code == 5 and tg.sent() == [] and lines[-1].startswith("텔레그램: 못 보냄(텔레그램 토큰이 없습니다"), lines


def test_early_return_without_broken_record_stays_exit_2(tmp_path, monkeypatch, capsys):
    """기록 손상이 없으면 가드레일을 못 읽은 이른 반환은 지금처럼 종료 2."""
    root, store = setup(tmp_path, monkeypatch, capsys)
    (root / "strategies" / "s1" / "guardrail.v1.yaml").write_text("{}", encoding="utf-8")
    assert run(root, store, trades_from(bt_executions()))[0] == 2


def test_early_return_screen_lines_survive_unreadable_connections(tmp_path, monkeypatch, capsys):
    """connections.json을 못 읽어도 이른 반환 길의 화면 줄(손상 줄)은 남고 「못 보냄(connections.json을 못 읽음)」 줄 + 종료 5."""
    root, store = _early_return_with_broken_s2(tmp_path, monkeypatch, capsys)
    (root / "connections.json").write_text("{깨짐", encoding="utf-8")
    code, lines, tg = run(root, store, trades_from(bt_executions()))
    assert code == 5 and tg.sent() == [], lines
    assert any(x.startswith("s2: 기록 손상") for x in lines)
    assert lines[-1].startswith("텔레그램: 못 보냄(connections.json을 못 읽음)"), lines


def test_normal_path_screen_lines_survive_unreadable_connections(tmp_path, monkeypatch, capsys):
    """보통 길도 설정을 못 읽으면 대조 줄은 남고 「못 보냄(connections.json을 못 읽음)」 줄 + 종료 5(헬스체크는 주소를 몰라 건너뜀)."""
    import blackclip.daily.run as daily_run
    from blackclip.workspace import WorkspaceError
    root, store = setup(tmp_path, monkeypatch, capsys)
    pings = []

    def boom(_root):
        raise WorkspaceError("connections.json: 깨짐")
    monkeypatch.setattr(daily_run, "settings", boom)
    code, lines, tg = run(root, store, trades_from(bt_executions()[:-1]), pings=pings)
    assert code == 5 and tg.sent() == [] and pings == [], lines
    assert any(x.startswith("s1: ") for x in lines) and lines[-1].startswith("텔레그램: 못 보냄(connections.json을 못 읽음)")


def test_nothing_to_send_skips_token_even_when_missing(tmp_path, monkeypatch, capsys):
    """이미 알린 손상 전략만 있고 보낼 글이 0통이면 토큰이 없어도 「바뀐 상태 없음 · 안 보냄」, 종료는 손상의 4(5 아님)."""
    root, store = setup(tmp_path, monkeypatch, capsys)
    _add_s2(tmp_path, root)
    assert main(["record", "s1", "--state", "중지"]) == 0
    _break_record(root, "s2")
    trades = trades_from(bt_executions()[:-1])
    assert _broken_sent(run(root, store, trades)[2]) == [broken_message("s2")]
    store.items.pop(TELEGRAM_ACCOUNT)
    code, lines, tg = run(root, store, trades)
    assert code == 4 and tg.sent() == [] and lines[-1] == "텔레그램: 바뀐 상태 없음 · 안 보냄", lines
    assert not any("못 보냄" in x for x in lines)


def test_two_broken_strategies_send_one_each_in_name_order(tmp_path, monkeypatch, capsys):
    """깨진 전략이 둘(s3 · s2)이면 손상 글은 이름순(s2, s3)으로 각 1통, 묶음 끝 줄도 같은 순서."""
    root, store = setup(tmp_path, monkeypatch, capsys)
    _add_s2(tmp_path, root)
    _add_s2(tmp_path, root, "s3")
    _break_record(root, "s3")
    _break_record(root, "s2")
    set_notify(root, quiet="매일")
    _, _, tg = run(root, store, trades_from(bt_executions()[:-1]))
    assert [m for m in tg.sent() if m.startswith("기록 손상:")] == [broken_message("s2"), broken_message("s3")]
    assert tg.sent()[-1].splitlines()[-2:] == [broken_note("s2"), broken_note("s3")]

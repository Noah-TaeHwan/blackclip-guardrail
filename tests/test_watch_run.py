"""blackclip watch 묶음: 처음 · 같은 시간 두 번 · 포지션 유예 · 출금 · 받기 틈 · 확인 불가 두 번 연속 · 깨진 상태 · 섞인 계좌."""
import json
from datetime import datetime, timedelta, timezone
from decimal import Decimal

import pytest

from conftest import READ_ONLY, FakeTelegram, KlineClient, make_prices, maker, routes, write_link
from test_check_report import _body
from test_daily_run import LIVE, SPLIT, bt_executions, setup, split_last_sell, trades_from

from blackclip.cli import main
from blackclip.notify import write_live
from blackclip.telegram import Bot
from blackclip.watch.run import run_watch
from blackclip.workspace import ensure

UTC = timezone.utc
AT = datetime(2025, 1, 23, 7, 0, tzinfo=UTC)  # BT 목표가 01/23 0시에 없음으로 바뀐 날(유예 6시간 뒤)
NOTE = "1시간 확인 기준 · 보정은 독립 정규 가정(미검증)"


def account(usdt="10000", btc="0", fail_trades=False):
    """잔고를 정하고(체결은 예시 BT 그대로) 체결 받기를 실패시킬 수 있는 응답."""
    inner = routes(trades=trades_from(bt_executions()))

    def answer(path, q):
        if path == "/api/v3/account":
            return {"balances": [{"asset": "USDT", "free": usdt, "locked": "0"}, {"asset": "BTC", "free": btc, "locked": "0"}]}
        if fail_trades and path == "/api/v3/myTrades":
            raise OSError("network down")
        return inner(path, q)
    return answer


def late_fill_account(fill_at, bought=None):
    """체결 하나(BTC 100개 · 가격 100 · 수수료 0)가 fill_at에 일어나는 계좌. 잔고는 bought()가 참이면 거래 뒤(현금 0 · BTC 100),
    아니면 거래 전(현금 10000). 기본은 체결 받기(myTrades)를 한 번이라도 한 뒤에 읽는 잔고만 거래 뒤 = 받기 끝 뒤에 거래가 낀 모양."""
    fill = {"symbol": "BTCUSDT", "id": 99999, "orderId": 99999, "price": "100", "qty": "100", "commission": "0",
            "commissionAsset": "USDT", "time": int(fill_at.timestamp() * 1000), "isBuyer": True}
    inner = routes(trades=trades_from(bt_executions()) + [fill])
    fetched = []
    bought = bought or (lambda: bool(fetched))

    def answer(path, q):
        if path == "/api/v3/myTrades":
            fetched.append(q["symbol"])
        if path == "/api/v3/account":
            done = bought()
            return {"balances": [{"asset": "USDT", "free": "0" if done else "10000", "locked": "0"},
                                 {"asset": "BTC", "free": "100" if done else "0", "locked": "0"}]}
        return inner(path, q)
    return answer


def watch(root, store, answer, now=AT, tg=None, price=None):
    tg = tg or FakeTelegram()
    code, lines = run_watch(root, store, maker(answer), lambda m: KlineClient({"BTCUSDT": make_prices()}, m, price=price),
                            lambda t: Bot(t, tg), now, paths=2000)
    return code, lines, tg


def test_watch_counts_one_exit_order_in_many_fills_as_one_round_trip(tmp_path, monkeypatch, capsys):
    """자주 보는 시계의 봇 빈도도 청산 주문 하나의 체결 셋을 왕복 하나로 센다(매일 대조와 같은 주문 묶음)."""
    import blackclip.watch.run as wr
    real, seen = wr.bot_cause, {}

    def exits_of(sub, parts):
        d = tmp_path / sub
        d.mkdir()
        root, store = setup(d, monkeypatch, capsys)
        inner = routes(trades=split_last_sell(parts))

        def answer(path, q):
            if path == "/api/v3/account":
                return {"balances": [{"asset": "USDT", "free": "10000", "locked": "0"}, {"asset": "BTC", "free": "0", "locked": "0"}]}
            return inner(path, q)

        monkeypatch.setattr(wr, "bot_cause", lambda *a, **k: (seen.__setitem__(sub, list(a[5])), real(*a, **k))[1])
        code, lines, _ = watch(root, store, answer)
        assert code == 0, lines
        return seen[sub]

    one = exits_of("one_fill", ["1"])
    assert len(one) == 1 and exits_of("three_fills", SPLIT) == one


def test_watch_without_link_says_connect_telegram(tmp_path, monkeypatch, capsys):
    """대화 연결 전이면 보낼 것이 있어도 안 보냈다는 줄, 다음 걸음은 대화 잇기만(토큰이 이미 있는 상태)."""
    root, store = setup(tmp_path, monkeypatch, capsys, link_telegram=False)
    code, lines, tg = watch(root, store, account())
    assert code == 0 and lines[-1] == "텔레그램: 대화 연결 전이라 안 보냄 · blackclip connect --telegram" and tg.sent() == []


def test_first_watch_captures_then_same_hour_sends_nothing(tmp_path, monkeypatch, capsys):
    """처음: 구간 기준 잡음 · 포지션 맞음(BT 없음 · 실제 없음) · 텔레그램 세 줄 한 통. 같은 시각 다시: 보낼 것 없음."""
    root, store = setup(tmp_path, monkeypatch, capsys)
    code, lines, tg = watch(root, store, account())
    assert code == 0, lines
    row = next(x for x in lines if x.startswith("s1: "))
    assert row.startswith("s1: 정상 · 가드레일 선 정상 · 구간 기준 잡음 · 구간 수익 +0.0%(이번 구간 최저 +0.0%) · 경고선 ")
    assert f" · {NOTE}) · 봇 " in row and row.endswith("포지션 맞음: BTCUSDT 없음")
    assert "체결 01/23 07:00 UTC까지 받음 · 마지막 체결 01/23 00:05 UTC · 최근 7일 1건" in row
    assert lines[-1] == "텔레그램: 보냄" and len(tg.sent()) == 1 and len(tg.sent()[0].splitlines()) == 3
    assert tg.sent()[0].splitlines()[0] == "정상 · 가드레일 선 · s1 · 자주 보는 시계"
    notified = json.loads((root / "notified.json").read_text())
    assert set(notified) == {"watch:s1"}  # 매일 대조 키(s1)와 따로
    code2, lines2, tg2 = watch(root, store, account())
    assert code2 == 0 and lines2[-1] == "텔레그램: 바뀐 상태 없음 · 안 보냄" and tg2.sent() == []
    state = json.loads((root / "strategies" / "s1" / "watch.json").read_text())
    assert set(state) == {"line", "window", "target", "unconfirmed"} and state["window"]["start"] == "2025-01-19T00:00:00+00:00"
    assert state["unconfirmed"] is False


def test_position_mismatch_warns_only_after_grace(tmp_path, monkeypatch, capsys):
    """실제로 BTC를 들고 있는데 BT 목표가 01/23 0시에 없음으로 바뀜: 05:59는 따라오는 중(정상), 06:00부터 봇 경고 · 텔레그램."""
    root, store = setup(tmp_path, monkeypatch, capsys)
    code, lines, _ = watch(root, store, account(btc="20"), now=AT - timedelta(hours=1, minutes=1))
    assert code == 0 and "포지션 따라오는 중: BTCUSDT BT 없음 · 실제 보유(목표 포지션이 01/23 00:00 UTC에 바뀜 · 유예 6시간)" in lines[-2]
    code, lines, tg = watch(root, store, account(btc="20"), now=AT - timedelta(hours=1))
    row = next(x for x in lines if x.startswith("s1: "))
    assert row.startswith("s1: 경고 · ") and row.endswith("포지션 다름: BTCUSDT BT 없음 · 실제 보유")
    assert tg.sent()[0].splitlines()[0] == "경고 · 봇 · s1 · 자주 보는 시계"


def test_withdrawal_is_not_a_loss(tmp_path, monkeypatch, capsys):
    """출금 5000(가격 그대로): 구간 수익 0 · 선 정상 · 「입출금 추정 · 손익에서 뺌(기준 대비 -50.0%)」, 경고 없음."""
    root, store = setup(tmp_path, monkeypatch, capsys)
    watch(root, store, account())
    code, lines, _ = watch(root, store, account(usdt="5000"), now=AT + timedelta(hours=1))
    row = next(x for x in lines if x.startswith("s1: "))
    assert code == 0 and row.startswith("s1: 정상 · 가드레일 선 정상 · 구간 수익 +0.0%(이번 구간 최저 +0.0%)")
    assert "입출금 추정 · 손익에서 뺌(기준 대비 -50.0%)" in row


def test_fetch_gap_is_unconfirmed_without_alarm(tmp_path, monkeypatch, capsys):
    """체결 받기 실패: 선 · 봇 확인 불가(봇은 「1시간째 못 받음」) · 종료 4, 구간 상태(지난 확인)는 그대로라 다음 확인에서 이어 셈.
    확인 불가 1회째라 텔레그램은 안 감, 다음 시간에 정상으로 돌아오면 notified가 그대로라 0통."""
    root, store = setup(tmp_path, monkeypatch, capsys)
    watch(root, store, account())
    path = root / "strategies" / "s1" / "watch.json"
    before = json.loads(path.read_text())["window"]
    code, lines, tg = watch(root, store, account(usdt="9000", fail_trades=True), now=AT + timedelta(hours=1))
    row = next(x for x in lines if x.startswith("s1: "))
    assert code == 4 and row.startswith("s1: 확인 불가 · 가드레일 선 확인 불가 · 체결을 다 받지 못해 이번 단계는 나누지 않음")
    assert "체결 내역을 01/23 07:00 UTC까지만 받음 · 1시간째 못 받음 · " in row
    assert lines[-1] == "텔레그램: 확인 불가 1회째 · 다음 확인도 그러면 보냄(s1)" and tg.sent() == []
    assert json.loads(path.read_text())["window"] == before
    code, lines, tg = watch(root, store, account(usdt="9000"), now=AT + timedelta(hours=2))
    assert code == 0 and lines[-1] == "텔레그램: 바뀐 상태 없음 · 안 보냄" and tg.sent() == []


def test_unconfirmed_sends_only_on_the_second_in_a_row(tmp_path, monkeypatch, capsys):
    """정상(보냄) → 확인 불가(1회째, 안 보냄) → 확인 불가(2회째, 한 통). 정상 → 경고는 바로 보냄(test_position_mismatch_warns_only_after_grace)."""
    root, store = setup(tmp_path, monkeypatch, capsys)
    assert len(watch(root, store, account())[2].sent()) == 1
    assert watch(root, store, account(fail_trades=True), now=AT + timedelta(hours=1))[2].sent() == []
    code, lines, tg = watch(root, store, account(fail_trades=True), now=AT + timedelta(hours=2))
    assert code == 4 and lines[-1] == "텔레그램: 보냄" and len(tg.sent()) == 1
    assert tg.sent()[0].splitlines()[0].startswith("확인 불가 · ")


def test_fresh_line_warning_is_not_held_back_by_a_first_unconfirmed_bot(tmp_path, monkeypatch, capsys):
    """선이 정상 → 경고로 바뀐 같은 시간에 봇이 처음 확인 불가(BT 목표를 못 냄)여도 그 한 통은 바로 가고, 선 경고가 글에 든다.
    두 번 연속을 기다리는 건 바뀐 칸이 모두 확인 불가일 때뿐이다: 선 경고가 한 시간 늦으면 1시간 시계의 뜻이 없다."""
    root, store = setup(tmp_path, monkeypatch, capsys)
    hold = account(btc="100", usdt="0")
    assert len(watch(root, store, hold, price={"BTCUSDT": 100.0}, now=AT - timedelta(hours=1, minutes=1))[2].sent()) == 1  # 정상 · 정상
    path = root / "strategies" / "s1" / "watch.json"
    state = json.loads(path.read_text())
    state["target"] = "깨진 캐시"  # BT 목표를 못 냄 = 봇 확인 불가
    path.write_text(json.dumps(state), encoding="utf-8")
    code, lines, tg = watch(root, store, hold, price={"BTCUSDT": 70.0}, now=AT - timedelta(hours=1))  # 가격 -30% = 선 경고
    row = next(x for x in lines if x.startswith("s1: "))
    assert code == 4 and row.startswith("s1: 확인 불가 · 가드레일 선 경고 · 구간 수익 -30.0%") and " · 봇 확인 불가 · " in row
    assert lines[-1] == "텔레그램: 보냄" and len(tg.sent()) == 1
    assert tg.sent()[0].splitlines()[1].startswith("가드레일 선: 경고 · 구간 수익 -30.0%")


def test_first_ever_unconfirmed_bot_with_a_normal_line_is_still_held(tmp_path, monkeypatch, capsys):
    """보낸 글이 아직 없을 때 선 정상 · 봇 처음 확인 불가면 기다린다: 보낸 적 없는 칸의 정상은 바뀜이 아니다(첫 확인의
    받기 한 번 실패로 확인 불가 글이 가지 않게)."""
    root, store = setup(tmp_path, monkeypatch, capsys)
    path = root / "strategies" / "s1" / "watch.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({"target": "깨진 캐시"}), encoding="utf-8")  # BT 목표를 못 냄 = 봇 확인 불가
    code, lines, tg = watch(root, store, account(btc="100", usdt="0"), price={"BTCUSDT": 100.0}, now=AT - timedelta(hours=1))
    assert code == 4 and " · 봇 확인 불가 · " in next(x for x in lines if x.startswith("s1: "))
    assert tg.sent() == [] and "1회째" in lines[-1]


def test_new_period_is_captured_when_the_check_crosses_midnight(tmp_path, monkeypatch, capsys):
    """구간의 끝은 확인한 날의 다음 0시다: 12/20 구간의 마지막 날 23:40에 잡은 기준이 01/19 00:40 확인에서 01/19 시작 구간으로 다시
    잡힌다(끝을 now로 보면 그 시각이 아직 옛 구간이라 12/20에 머문다). 한낮 확인에서는 두 식이 같아 가려내지 못한다."""
    root, store = setup(tmp_path, monkeypatch, capsys)
    path = root / "strategies" / "s1" / "watch.json"
    watch(root, store, account(), now=datetime(2025, 1, 18, 23, 40, tzinfo=UTC))
    assert json.loads(path.read_text())["window"]["start"] == "2024-12-20T00:00:00+00:00"
    _, lines, _ = watch(root, store, account(), now=datetime(2025, 1, 19, 0, 40, tzinfo=UTC))
    assert json.loads(path.read_text())["window"]["start"] == "2025-01-19T00:00:00+00:00"
    assert " · 구간 기준 잡음 · " in next(x for x in lines if x.startswith("s1: "))


def test_broken_state_file_rebuilds_and_says_so(tmp_path, monkeypatch, capsys):
    """깨진 watch.json: 이번 확인은 선 확인 불가(지난 손익을 잃었다고 말함), 파일은 새로 써서 다음 확인부터 셈.
    확인 불가 1회째여도 이 글은 바로 한 통 간다(깨진 파일은 한 번뿐이라 두 번 연속을 기다리면 영영 안 나감)."""
    root, store = setup(tmp_path, monkeypatch, capsys)
    watch(root, store, account())
    path = root / "strategies" / "s1" / "watch.json"
    path.write_text("{ broken", encoding="utf-8")
    code, lines, tg = watch(root, store, account(), now=AT + timedelta(hours=1))
    assert code == 4 and "가드레일 선 확인 불가 · 상태 파일을 읽지 못해 구간 기준을 새로 잡음" in lines[-2]
    assert lines[-1] == "텔레그램: 보냄" and len(tg.sent()) == 1
    assert tg.sent()[0].splitlines()[0].startswith("확인 불가 · ")
    assert "지난 손익 · 최저를 잃음" in tg.sent()[0]
    assert json.loads(path.read_text())["window"]["mark"]["at"] == (AT + timedelta(hours=1)).isoformat()
    code, lines, tg = watch(root, store, account(), now=AT + timedelta(hours=2))  # 다음 시간은 정상: 확인 불가 → 정상으로 바뀌어 한 통
    assert code == 0 and lines[-1] == "텔레그램: 보냄" and len(tg.sent()) == 1
    assert tg.sent()[0].splitlines()[0].startswith("정상 · ")


def test_corrupt_target_cache_breaks_only_the_position_check(tmp_path, monkeypatch, capsys):
    """watch.json의 "target" 칸이 dict가 아니면(AttributeError) 봇의 포지션 대조만 확인 불가, 가드레일 선은 그대로 셈."""
    root, store = setup(tmp_path, monkeypatch, capsys)
    watch(root, store, account())
    path = root / "strategies" / "s1" / "watch.json"
    state = json.loads(path.read_text())
    state["target"] = "깨진 캐시"
    path.write_text(json.dumps(state), encoding="utf-8")
    code, lines, _ = watch(root, store, account(), now=AT + timedelta(hours=1))
    row = next(x for x in lines if x.startswith("s1: "))
    assert code == 4 and row.startswith("s1: 확인 불가 · 가드레일 선 정상 · 구간 수익 +0.0%(이번 구간 최저 +0.0%)")
    assert " · 봇 확인 불가 · " in row and row.endswith(" · BT 목표를 못 냄(바이낸스 봉 · AttributeError)")
    assert "target" not in json.loads(path.read_text())  # 깨진 캐시는 버림: 다음 확인이 BT 목표를 다시 셈
    code, lines, _ = watch(root, store, account(), now=AT + timedelta(hours=2))
    row = next(x for x in lines if x.startswith("s1: "))
    assert code == 0 and row.startswith("s1: 정상 · ") and row.endswith("포지션 맞음: BTCUSDT 없음")
    assert set(json.loads(path.read_text())["target"]) == {"bar_end", "version", "weights", "changed"}


@pytest.mark.parametrize("where,value", [(("line",), "x"), (("line",), ["x"]), (("window",), {"start": "x"}),
                                         (("window", "mark"), "x"), (("window", "growth"), "x"), (("window", "low"), "x"),
                                         (("window", "base"), "x")])
def test_misshaped_state_is_told_once_and_heals_next_hour(tmp_path, monkeypatch, capsys, where, value):
    """watch.json의 line · window 칸 모양이나 window 안쪽 값(mark · growth · low · base)이 다르면(손으로 고침) 깨진 파일과 같이:
    선 확인 불가 한 통 · 새로 잡음 · 다음 시간 정상. 예외로 run_watch가 죽거나(line) 같은 오류로 영영 확인 불가(window, 연결 확인이라는
    엉뚱한 안내와 함께)가 되지 않는다."""
    root, store = setup(tmp_path, monkeypatch, capsys)
    watch(root, store, account())
    path = root / "strategies" / "s1" / "watch.json"
    state = json.loads(path.read_text())
    node = state
    for key in where[:-1]:
        node = node[key]
    node[where[-1]] = value
    path.write_text(json.dumps(state), encoding="utf-8")
    code, lines, tg = watch(root, store, account(), now=AT + timedelta(hours=1))
    assert code == 4 and "가드레일 선 확인 불가 · 상태 파일을 읽지 못해 구간 기준을 새로 잡음" in lines[-2]
    assert lines[-1] == "텔레그램: 보냄" and len(tg.sent()) == 1 and tg.sent()[0].startswith("확인 불가 · ")
    code, lines, _ = watch(root, store, account(), now=AT + timedelta(hours=2))
    assert code == 0 and "구간 수익 +0.0%" in lines[-2]


def test_balance_is_read_before_the_fills_are_fetched(tmp_path, monkeypatch, capsys):
    """받기 끝(now) 뒤에 일어난 체결이 잔고에 먼저 들면 다음 단계가 그 체결을 보유 손익과 체결 손익으로 두 번 센다(구간 수익 -5.9%
    · 가짜 입금 +300). 잔고 · 가격을 체결 받기보다 먼저 받으면 그 체결은 다음 단계에 한 번만 든다(가격 -3%면 구간 수익 -3.0%)."""
    root, store = setup(tmp_path, monkeypatch, capsys)
    t1, t2 = AT + timedelta(hours=1), AT + timedelta(hours=2)
    at100, at97 = {"BTCUSDT": 100.0}, {"BTCUSDT": 97.0}
    watch(root, store, account(), price=at100)
    watch(root, store, late_fill_account(t1 + timedelta(seconds=2)), now=t1, price=at100)
    _, lines, _ = watch(root, store, late_fill_account(t1 + timedelta(seconds=2), bought=lambda: True), now=t2, price=at97)
    row = next(x for x in lines if x.startswith("s1: "))
    assert " · 가드레일 선 정상 · 구간 수익 -3.0%(이번 구간 최저 -3.0%)" in row and "입출금 추정" not in row


def test_capital_and_mixed_accounts_are_skipped_and_told_once(tmp_path, monkeypatch, capsys):
    """같은 계좌에 --capital 없는 전략 둘: 둘 다 건너뛰고 섞인 계좌 알림 한 통, 다음 확인은 안 보냄. --capital 있는 전략은 터미널에만."""
    root, store = setup(tmp_path, monkeypatch, capsys)
    for n in ("s2", "s3"):
        write_link(tmp_path, _body(), n)
        assert main(["save", n, "--start", "2024-12-20"]) == 0
    write_live(root, "s2", "main", "USDT", LIVE - timedelta(hours=12), Decimal("10000"), {"BTCUSDT": Decimal(0)})
    write_live(root, "s3", "sub1", "USDT", LIVE - timedelta(hours=12), Decimal("10000"), {"BTCUSDT": Decimal(0)}, Decimal("500"))
    code, lines, tg = watch(root, store, account())
    assert code == 0 and not any(x.startswith(("s1: ", "s2: ")) for x in lines)
    assert lines[-2] == ("자주 보는 시계 건너뜀 3개: s1(같은 계좌(binance main spot)에 전략 2개 = 섞인 계좌 · 매일 대조만) · "
                         "s2(같은 계좌(binance main spot)에 전략 2개 = 섞인 계좌 · 매일 대조만) · "
                         "s3(기준 자본(--capital) 있음 = 섞인 계좌 · 매일 대조만)")
    assert len(tg.sent()) == 1 and tg.sent()[0].startswith("섞인 계좌라 1시간마다 보지 않고 매일 대조로만 봐요")
    assert watch(root, store, account())[2].sent() == []


def _two_strategies(tmp_path, monkeypatch, capsys):
    """s1(setup) · s2(같은 link · 같은 계좌) 저장 — test_capital_and_mixed_accounts_are_skipped_and_told_once와 같은 방식. 아직 둘 다 정상 가드레일."""
    root, store = setup(tmp_path, monkeypatch, capsys)
    write_link(tmp_path, _body(), "s2")
    assert main(["save", "s2", "--start", "2024-12-20"]) == 0
    write_live(root, "s2", "main", "USDT", LIVE - timedelta(hours=12), Decimal(10000), {"BTCUSDT": Decimal(0)})
    return root, store


def test_broken_guardrail_skips_only_that_strategy(tmp_path, monkeypatch, capsys):
    """s1의 가드레일이 깨져도(모양 · 깊은 JSON) 다른 계좌의 s2는 평소대로 판정되고 s1은 화면 줄 하나 · 상태 파일 없음 · 종료 4.
    (같은 계좌면 s2도 섞였을 수 있어 빠진다 — test_broken_guardrail_takes_the_same_account_strategy_out_of_the_watch)"""
    root, store = _two_strategies(tmp_path, monkeypatch, capsys)
    write_live(root, "s1", "sub1", "USDT", LIVE - timedelta(hours=12), Decimal(10000), {"BTCUSDT": Decimal(0)})  # s1은 다른 계좌
    (root / "strategies" / "s1" / "guardrail.v1.yaml").write_text("{}", encoding="utf-8")
    code, lines, tg = watch(root, store, account())
    assert code == 4, lines
    broken = next(x for x in lines if x.startswith("s1: "))
    assert broken.startswith("s1: 확인 불가(가드레일을 읽지 못함 · ") and broken.endswith(") · 이 전략만 건너뜀") and "가드레일 모양이 다릅니다(target)" in broken
    assert sum(x.startswith("s1: ") for x in lines) == 1
    row = next(x for x in lines if x.startswith("s2: "))
    assert row.startswith("s2: 정상 · 가드레일 선 정상 · ") and row.endswith("포지션 맞음: BTCUSDT 없음")
    assert lines[-1] == "텔레그램: 보냄" and len(tg.sent()) == 1 and tg.sent()[0].splitlines()[0] == "정상 · 가드레일 선 · s2 · 자주 보는 시계"
    assert (root / "strategies" / "s2" / "watch.json").is_file() and not (root / "strategies" / "s1" / "watch.json").exists()
    (root / "strategies" / "s1" / "guardrail.v1.yaml").write_text("[" * 200_000, encoding="utf-8")  # 깊은 JSON도 같은 길
    code, lines, _ = watch(root, store, account(), now=AT + timedelta(hours=1))
    assert code == 4 and "중첩이 너무 깊음" in next(x for x in lines if x.startswith("s1: ")) and any(x.startswith("s2: 정상 · ") for x in lines)


def test_every_guardrail_broken_is_exit_2_with_only_the_lines(tmp_path, monkeypatch, capsys):
    """읽을 수 있는 가드레일이 하나도 없고 깨진 전략만 있으면 화면 줄들만 · 종료 2(체결 받기 · 텔레그램 없음)."""
    root, store = _two_strategies(tmp_path, monkeypatch, capsys)
    for n in ("s1", "s2"):
        (root / "strategies" / n / "guardrail.v1.yaml").write_text("{}", encoding="utf-8")
    code, lines, tg = watch(root, store, account())
    assert code == 2 and [x.split(":")[0] for x in lines] == ["s1", "s2"] and all(x.endswith(") · 이 전략만 건너뜀") for x in lines)
    assert tg.sent() == [] and not (root / "fills").exists() and not (root / "strategies" / "s2" / "watch.json").exists()


LOST_REASON = "같은 계좌(binance main)에 가드레일을 읽지 못한 전략 = 섞였을 수 있음 · 매일 대조만"


def test_broken_guardrail_takes_the_same_account_strategy_out_of_the_watch(tmp_path, monkeypatch, capsys):
    """s1 가드레일이 깨졌고 live.json은 읽혀 같은 계좌(main)면 s2의 잔고에 s1 몫이 섞였을 수 있어 s2는 보지 않고 까닭을 적는다(종목 · 시장을 몰라 계좌 단위)."""
    root, store = _two_strategies(tmp_path, monkeypatch, capsys)
    (root / "strategies" / "s1" / "guardrail.v1.yaml").write_text("{}", encoding="utf-8")
    code, lines, tg = watch(root, store, account())
    assert code == 4 and not any(x.startswith("s2: ") for x in lines)
    assert f"자주 보는 시계 건너뜀 1개: s2({LOST_REASON})" in lines
    assert tg.sent() == [] and not (root / "strategies" / "s2" / "watch.json").exists()


def test_guardrail_file_link_takes_the_same_account_strategy_out_of_the_watch(tmp_path, monkeypatch, capsys):
    """s1 가드레일 판 파일이 링크라 읽기 오류 없이 목록에서 빠져도, live.json이 읽히면 같은 계좌의 s2는
    판 파일을 못 읽을 때와 같은 까닭으로 보지 않는다(s1 몫이 섞였을 수 있음). 종료 코드는 0(s1은 확인 불가가 아니라 목록에 없음)."""
    root, store = _two_strategies(tmp_path, monkeypatch, capsys)
    guard = root / "strategies" / "s1" / "guardrail.v1.yaml"
    guard.rename(tmp_path / "moved.yaml")
    guard.symlink_to(tmp_path / "moved.yaml")
    code, lines, tg = watch(root, store, account())
    assert code == 0 and not any(x.startswith(("s1: ", "s2: ")) for x in lines), lines
    assert f"자주 보는 시계 건너뜀 1개: s2({LOST_REASON})" in lines
    assert tg.sent() == [] and not (root / "strategies" / "s2" / "watch.json").exists()


@pytest.mark.parametrize("other", ["다른 계좌", "live.json 없음", "live.json 깨짐"])
def test_broken_guardrail_without_a_known_same_account_leaves_the_watch_alone(tmp_path, monkeypatch, capsys, other):
    """깨진 전략의 계좌를 모르거나(live.json 없음 · 깨짐) 다른 계좌면 s2는 평소대로 본다."""
    root, store = _two_strategies(tmp_path, monkeypatch, capsys)
    (root / "strategies" / "s1" / "guardrail.v1.yaml").write_text("{}", encoding="utf-8")
    live1 = root / "strategies" / "s1" / "live.json"
    if other == "다른 계좌":
        write_live(root, "s1", "sub1", "USDT", LIVE - timedelta(hours=12), Decimal(10000), {"BTCUSDT": Decimal(0)})
    elif other == "live.json 없음":
        live1.unlink()
    else:
        live1.write_text("{ broken", encoding="utf-8")
    code, lines, _ = watch(root, store, account())
    assert code == 4 and any(x.startswith("s2: 정상 · ") for x in lines)
    assert not any("가드레일을 읽지 못한 전략" in x for x in lines)


def test_no_guardrail_exit_2(tmp_path):
    """가드레일이 없으면 2."""
    (tmp_path / ".git").mkdir()
    root = ensure(tmp_path)
    assert run_watch(root, None, None, None, None, AT)[0] == 2


def test_stopped_strategy_is_not_watched_but_keeps_account_shared(tmp_path, monkeypatch, capsys):
    """같은 계좌의 s1 · s2 중 s1을 끄면 s1은 「감시 안 함」, s2는 여전히 섞인 계좌(혼자 계좌로 보지 않음)."""
    root, store = setup(tmp_path, monkeypatch, capsys)
    write_link(tmp_path, _body(), "s2")
    assert main(["save", "s2", "--start", "2024-12-20"]) == 0
    write_live(root, "s2", "main", "USDT", LIVE - timedelta(hours=12), Decimal("10000"), {"BTCUSDT": Decimal(0)})
    assert main(["record", "s1", "--state", "중지"]) == 0
    code, lines, _ = watch(root, store, account())
    assert code == 0 and not any(x.startswith(("s1: ", "s2: ")) for x in lines)
    assert "s1(중지(" in lines[-2] and ") · 감시 안 함)" in lines[-2]
    assert "s2(같은 계좌(binance main spot)에 전략 2개 = 섞인 계좌 · 매일 대조만)" in lines[-2]
    rec = root / "strategies" / "s1" / "record.md"  # 깨진 기록도 실제 watch 경로에서 감시 안 함
    rec.write_text("<<<<<<< HEAD\n" + rec.read_text() + "=======\n>>>>>>> origin/main\n")
    code, lines, _ = watch(root, store, account())
    assert code == 0 and "s1(기록 손상 · 감시 안 함)" in lines[-2]


def test_stopped_strategy_with_broken_guardrail_is_one_not_watched_entry_and_no_exit_code(tmp_path, monkeypatch, capsys):
    """중지한 전략은 가드레일을 못 읽어도 「감시 안 함」 하나만 내고 그 때문에 종료 코드를 올리지 않는다."""
    root, store = setup(tmp_path, monkeypatch, capsys)
    write_link(tmp_path, _body(), "s2")
    assert main(["save", "s2", "--start", "2024-12-20"]) == 0
    write_live(root, "s2", "main", "USDT", LIVE - timedelta(hours=12), Decimal("10000"), {"BTCUSDT": Decimal(0)})
    assert main(["record", "s1", "--state", "중지"]) == 0
    (root / "strategies" / "s1" / "guardrail.v1.yaml").write_text("{}", encoding="utf-8")
    code, lines, _ = watch(root, store, account())
    assert code == 0 and not any(x.startswith("s1: ") for x in lines), lines
    assert sum("s1(" in x for x in lines) == 1 and "s1(중지(" in lines[-2] and ") · 감시 안 함)" in lines[-2], lines

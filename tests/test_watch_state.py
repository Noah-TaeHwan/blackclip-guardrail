"""자주 보는 시계 대상 판정 · watch 상태 파일."""
from decimal import Decimal

from blackclip.watch.state import WINDOW_KEYS, pick, read_state, write_state


def g(market="spot", bar_s=86400):
    return {"target": {"market": market, "bar_seconds": bar_s, "symbols": ["BTCUSDT"]}}


def cfg(account="main", capital=None):
    return {"account": account, "capital": capital}


def test_pick_dedicated_mixed_and_skips():
    """--capital 없음 = 전략별 계좌 → 봄. --capital 있음 · live.json 없음 · 1일 초과 봉 → 뺌.
    같은 계좌 · 시장에 실전 전략이 둘 이상이면(--capital 있는 전략이 끼어도) --capital 없는 쪽도 섞인 계좌."""
    guards = {n: g() for n in "abcdhkm"} | {"e": g(bar_s=259200), "f": g(market="futures")}
    cfgs = {"a": cfg("sub1"), "b": cfg("main"), "c": cfg("main"), "d": cfg("sub2", Decimal("1000")), "e": cfg("sub3"),
            "f": cfg("main"), "h": None, "k": cfg("sub4"), "m": cfg("sub4", Decimal("500"))}
    watched, skipped, mixed = pick(guards, cfgs)
    assert watched == ["a", "f"]  # 같은 main이어도 선물 지갑은 따로
    assert mixed == {("main", "spot"): ["b", "c"], ("sub4", "spot"): ["k"]}
    assert dict(skipped) == {
        "b": "같은 계좌(binance main spot)에 전략 2개 = 섞인 계좌 · 매일 대조만",
        "c": "같은 계좌(binance main spot)에 전략 2개 = 섞인 계좌 · 매일 대조만",
        "d": "기준 자본(--capital) 있음 = 섞인 계좌 · 매일 대조만",
        "e": "봉 길이 1일 초과 · 점검(blackclip check)만",
        "h": "실전 시작 잔고 없음 · blackclip live h",
        "k": "같은 계좌(binance sub4 spot)에 전략 2개 = 섞인 계좌 · 매일 대조만",
        "m": "기준 자본(--capital) 있음 = 섞인 계좌 · 매일 대조만"}


def test_longer_bar_strategy_still_makes_the_account_mixed():
    """같은 main 현물에 일봉 전략과 3일봉 전략(둘 다 --capital 없음): 3일봉은 봉 길이로, 일봉은 섞인 계좌로 — 아무도 안 봄."""
    watched, skipped, mixed = pick({"daily": g(), "three": g(bar_s=259200)}, {"daily": cfg(), "three": cfg()})
    assert watched == [] and mixed == {("main", "spot"): ["daily"]}
    assert dict(skipped) == {"daily": "같은 계좌(binance main spot)에 전략 2개 = 섞인 계좌 · 매일 대조만",
                             "three": "봉 길이 1일 초과 · 점검(blackclip check)만"}


def test_state_round_trip_and_broken_file(tmp_path):
    """없으면 빈 상태, 쓰고 읽으면 그대로, 깨졌으면 빈 상태 + 깨짐 표시."""
    (tmp_path / "strategies" / "s1").mkdir(parents=True)
    assert read_state(tmp_path, "s1") == ({}, False)
    write_state(tmp_path, "s1", {"line": {"version": 1}})
    assert read_state(tmp_path, "s1") == ({"line": {"version": 1}}, False)
    (tmp_path / "strategies" / "s1" / "watch.json").write_text("{ broken", encoding="utf-8")
    assert read_state(tmp_path, "s1") == ({}, True)
    (tmp_path / "strategies" / "s1" / "watch.json").write_text("[1]", encoding="utf-8")
    assert read_state(tmp_path, "s1") == ({}, True)


def test_unreadable_or_misshaped_file_is_broken_and_only_a_missing_one_is_absent(tmp_path):
    """없음(폴더째 없어도) = 깨짐 아님. 있는데 못 읽음(폴더 · 잘못된 글자) · line / window 칸이 객체가 아님 · window에 키가 빠짐 = 깨짐.
    target 칸은 여기서 안 봄(BT 목표 캐시는 버리고 다시 셈하면 됨)."""
    assert read_state(tmp_path, "s1") == ({}, False)  # strategies/s1 폴더도 없음
    folder = tmp_path / "strategies" / "s1"
    folder.mkdir(parents=True)
    (folder / "watch.json").mkdir()
    assert read_state(tmp_path, "s1") == ({}, True)
    (folder / "watch.json").rmdir()
    (folder / "watch.json").write_bytes(b"\xff\xfe{")
    assert read_state(tmp_path, "s1") == ({}, True)
    window = dict.fromkeys(WINDOW_KEYS, "x")
    for doc in ({"line": "x"}, {"line": ["x"]}, {"window": "x"}, {"window": {"start": "x"}},
                {"window": {k: v for k, v in window.items() if k != "growth"}}):
        write_state(tmp_path, "s1", doc)
        assert read_state(tmp_path, "s1") == ({}, True), doc
    good = {"start": "2025-01-19T00:00:00+00:00", "base": "10000", "pnl": "0", "flows": "0", "growth": "1", "low": 0.0,
            "mark": {"at": "2025-01-23T07:00:00+00:00", "cash": "10000", "hold": {"BTCUSDT": "0"}, "price": {"BTCUSDT": "100"}}}
    for doc in ({"target": "x"}, {"window": good, "line": {}, "unconfirmed": True}):
        write_state(tmp_path, "s1", doc)
        assert read_state(tmp_path, "s1") == (doc, False), doc
    for key, bad in (("mark", "x"), ("mark", {"at": "x"}), ("growth", "x"), ("low", "x"), ("base", None), ("pnl", [1]), ("flows", "x")):
        write_state(tmp_path, "s1", {"window": {**good, key: bad}})  # 안쪽 값을 못 읽음 = 깨짐(window.advance가 읽다 영영 확인 불가가 되지 않게)
        assert read_state(tmp_path, "s1") == ({}, True), (key, bad)

"""check.c1 · check.source: 잘라 다시 돌리기 + 정적 검사(미래참조)."""
from conftest import make_prices, write_link

from blackclip.check.c1 import cut_points, c1
from blackclip.check.link import check_prices, check_weights, load_link, to_signals
from blackclip.check.source import source_files

HEAD = '''
import pandas as pd

EXEC = "next_open"
MARKET = "spot"


def load_prices():
    return {}

'''
CLEAN = HEAD + '''
def signals(prices):
    c = prices["BTCUSDT"]["Close"]
    return (c.rolling(10).mean() > c.rolling(30).mean()).astype(float)
'''
LEAK = CLEAN.replace("c.rolling(10).mean()", "c.rolling(10).mean().shift(-1)")
NONE = HEAD + '''
def signals(prices):
    return pd.Series(0.0, index=prices["BTCUSDT"].index)
'''
CRASH = CLEAN.replace("    c = prices", "    if len(prices[\"BTCUSDT\"]) < 400:\n        raise RuntimeError(\"짧음\")\n    c = prices")
HIDDEN = CLEAN + '''

def _unused(c):
    return c.shift(-1)
'''
REACH = HEAD + '''
def _lead(c):
    return c.shift(periods=-1) * 0


def signals(prices):
    c = prices["BTCUSDT"]["Close"]
    return (c.rolling(10).mean() > c.rolling(30).mean()).astype(float) + _lead(c).fillna(0)
'''
WEAK = CLEAN + '''
SCALE = pd.Series([1.0, 2.0]).mean()
'''


def _run(tmp_path, body):
    link = load_link(write_link(tmp_path, body), "s1")
    prices, _ = check_prices({"BTCUSDT": make_prices(400)})

    def at(cut):
        """Runner.signals(cut=…)를 흉내: 그 시각까지만 남긴 가격표로 돌린 비중."""
        sub = {s: p.loc[:cut] for s, p in prices.items()}
        return check_weights(link.module.signals(to_signals(sub, False)), sub)

    weights = check_weights(link.module.signals(to_signals(prices, False)), prices)
    return c1(link, prices, weights, source_files(link), at)


def test_c1_passes_clean(tmp_path):
    """미래 정보를 안 쓰면 이 범위에서 통과."""
    r = _run(tmp_path, CLEAN)
    assert r["state"] == "이 범위에서 통과" and "바뀐 신호 없음" in r["text"]


def test_c1_catches_shift_minus_one(tmp_path):
    """다음 봉 값을 당겨 쓰면 잘라 보기에서 신호가 바뀌어 의심."""
    r = _run(tmp_path, LEAK)
    assert r["state"] == "의심" and "이후 신호" in r["text"]
    assert ".blackclip/strategies/s1/link.py:" in r["text"]
    assert str(tmp_path) not in r["text"]


def test_c1_no_signals_judgement_impossible(tmp_path):
    """신호가 한 번도 안 나면 판단 불가(신호 0건) — 통과로 쓰면 거짓 안심."""
    assert _run(tmp_path, NONE)["state"] == "판단 불가(신호 0건)"


def test_c1_crash_on_truncated_is_not_pass(tmp_path):
    """잘린 데이터에서 신호 함수가 죽고 바뀐 신호가 없으면 점검 불가."""
    r = _run(tmp_path, CRASH)
    assert r["state"].startswith("점검 불가(잘린 데이터에서 오류 ") and "RuntimeError" in r["text"]


def test_c1_static_hint_on_reachable_code_suspect(tmp_path):
    """잘라 보기로는 안 걸려도 신호 함수가 부르는 코드에 확실한 패턴(키워드 인자 꼴 포함)이 있으면 의심."""
    r = _run(tmp_path, REACH)
    assert r["state"] == "의심" and "정적 검사" in r["text"] and "shift(-n)" in r["text"]


def test_c1_unreachable_code_not_flagged(tmp_path):
    """신호 함수가 부르지 않는 함수의 패턴은 보지 않는다(신호 함수에서 닿는 코드만)."""
    r = _run(tmp_path, HIDDEN)
    assert r["state"] == "이 범위에서 통과" and "정적 검사" not in r["text"]


def test_c1_weak_hint_note_only(tmp_path):
    """전체 기간 통계 후보는 메모만(rolling과 같은 줄은 뺌)."""
    r = _run(tmp_path, WEAK)
    assert r["state"] == "이 범위에서 통과" and "전체 기간 통계 후보 1곳" in r["text"]


def test_cut_points_capped():
    """바뀌는 봉이 많아도 많아야 80곳, 마지막 봉 포함, 오름차순."""
    pts = cut_points(10_000, list(range(0, 10_000, 3)))
    assert len(pts) <= 80 and pts == sorted(pts) and pts[-1] == 9_999


def test_source_files_skips_site_packages(tmp_path):
    """레포 안 모듈만 대상(pandas 같은 설치 패키지는 뺌)."""
    (tmp_path / "mystrat_c1.py").write_text("def f():\n    return 1\n", encoding="utf-8")
    link = load_link(write_link(tmp_path, "import mystrat_c1\n" + CLEAN), "s1")
    names = [p.name for p in source_files(link)]
    assert names == ["link.py", "mystrat_c1.py"]

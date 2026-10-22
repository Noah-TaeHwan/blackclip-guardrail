"""check.c2: 보수적 비용으로 다시 돌려 기준과 맞대기(거래비용)."""
import dataclasses

import numpy as np
import pandas as pd
from conftest import write_link

from blackclip.check.c2 import c2
from blackclip.check.link import load_link
from blackclip.check.source import source_files

STUB = '''
EXEC = "next_open"
MARKET = "spot"


def load_prices():
    return {}


def signals(prices):
    return None
'''


def _trend(n=400, step=0.001):
    """시가가 봉마다 step씩 오르는 가격표(다음 봉 시가 체결이면 들고 있는 봉마다 step만큼 번다)."""
    idx = pd.date_range("2024-01-01", periods=n, freq="D", tz="UTC")
    c = 100 * (1 + step) ** np.arange(n)
    o = np.concatenate([[100.0], c[:-1]])
    return {"BTCUSDT": pd.DataFrame({"Open": o, "High": c, "Low": o, "Close": c}, index=idx)}


def _c2(tmp_path, prices, w, body=STUB, cagr=None):
    link = load_link(write_link(tmp_path, body), "s1")
    if cagr is not None:
        link = dataclasses.replace(link, result_cagr=cagr)
    weights = {"BTCUSDT": pd.Series(w, index=prices["BTCUSDT"].index, dtype=float)}
    return c2(link, prices, weights, 86400, source_files(link))


def test_c2_keep_when_rarely_trading(tmp_path):
    """계속 들고 있으면 비용이 한 번뿐이라 유지."""
    r = _c2(tmp_path, _trend(), np.ones(400))
    assert r["state"] == "유지" and "보수적 비용 편도 30bp" in r["text"] and "비용 0으로 다시 돌린 값" in r["text"]


def test_c2_shrink(tmp_path):
    """20봉마다 한 봉 쉬면 비용이 수익을 크게 깎아 축소."""
    w = np.ones(400)
    w[::20] = 0.0
    assert _c2(tmp_path, _trend(), w)["state"] == "축소"


def test_c2_vanish(tmp_path):
    """봉마다 사고팔면 보수적 비용에서 0 아래로 내려가 소멸."""
    assert _c2(tmp_path, _trend(), np.tile([1.0, 0.0], 200))["state"] == "소멸"


def test_c2_base_nonpositive(tmp_path):
    """기준 수익이 0 이하면 해당 없음."""
    r = _c2(tmp_path, _trend(step=-0.001), np.ones(400))
    assert r["state"] == "해당 없음" and "0 이하" in r["text"]


def test_c2_uses_customer_result_when_given(tmp_path):
    """고객 보고값이 있으면 그것과 맞댄다."""
    r = _c2(tmp_path, _trend(), np.ones(400), cagr=0.9)
    assert "연 90.0%" in r["text"] and "기준: 고객 보고값" in r["text"] and r["state"] == "축소"


def test_c2_midprice_found(tmp_path):
    """고객 코드에 중간가 체결 가정이 보이면 발견으로 적는다."""
    body = STUB + '\n\ndef fill(df):\n    return (df["High"] + df["Low"]) / 2\n'
    r = _c2(tmp_path, _trend(), np.ones(400), body=body)
    assert "중간가 체결 가정 발견: .blackclip/strategies/s1/link.py:" in r["text"]


def test_c2_reported_far_from_rerun_says_check_reproduction(tmp_path):
    """보고값(90%)과 비용 0 재실행(약 44%)이 크게 다르면 재현부터 확인하라고 적고 비용 0 값도 같이 보인다."""
    r = _c2(tmp_path, _trend(), np.ones(400), cagr=0.9)
    assert "비용 0으로 다시 돌린 값 43.9%" in r["text"] and "보고값과 다시 돌린 값 차이 큼, 재현부터 확인" in r["text"]


def test_c2_reported_close_to_rerun_has_no_gap_note(tmp_path):
    """보고값이 비용 0 재실행과 가까우면 차이 큼 문구 없이 비용 0 값만 보인다(상태 논리는 그대로)."""
    r = _c2(tmp_path, _trend(), np.ones(400), cagr=0.42)
    assert "비용 0으로 다시 돌린 값 43.9%" in r["text"] and "차이 큼" not in r["text"] and r["state"] == "유지"


def test_c2_midprice_ignores_plain_mid_variable(tmp_path):
    """이진 탐색 · 볼린저의 변수 mid, pyramid_fill 같은 이름은 중간가 체결 가정이 아니다."""
    body = STUB + '''

def search(lo, hi):
    mid = (lo + hi) // 2
    left, mid, right = lo, mid, hi
    return left, mid, right


def bands(close):
    mid = close.rolling(20).mean()
    pyramid_fill = 1
    return mid, pyramid_fill
'''
    r = _c2(tmp_path, _trend(), np.ones(400), body=body)
    assert "발견" not in r["text"]


def test_c2_midprice_names_found(tmp_path):
    """mid_price · midfill 이름은 중간가 체결 가정으로 본다."""
    body = STUB + "\n\ndef fill(df):\n    mid_price = df['Close']\n    MidFill = df['Open']\n    return mid_price, MidFill\n"
    r = _c2(tmp_path, _trend(), np.ones(400), body=body)
    assert "mid 가격" in r["text"] and "mid 체결" in r["text"]


def test_c2_midprice_hits_deduped_and_capped(tmp_path):
    """맞는 줄이 다섯이면 세 곳만 적고 외 2곳(한 줄이 두 패턴에 걸려도 한 곳으로 센다)."""
    row = '    {} = (df["High"] + df["Low"]) / 2\n'
    body = STUB + "\n\ndef fill(df):\n" + "".join(row.format(n) for n in "abcd") + row.format("mid_price") + "    return a\n"
    r = _c2(tmp_path, _trend(), np.ones(400), body=body)
    assert r["text"].count(".blackclip/strategies/s1/link.py:") == 3 and r["text"].endswith(" 외 2곳")


def test_c2_clean_strategy_says_nothing_about_midprice(tmp_path):
    """찾은 게 없으면 중간가 이야기를 아예 안 쓴다(조용함은 증거가 아니라서 미발견도 적지 않는다)."""
    r = _c2(tmp_path, _trend(), np.ones(400))
    assert "발견" not in r["text"] and "중간가" not in r["text"]

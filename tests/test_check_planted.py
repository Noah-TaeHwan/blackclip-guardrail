"""심은 결함을 잡나: 우리 시험 전략 여덟 개를 한 레포에서 blackclip check로 돌린다.

| 전략 | 심은 것 | 기대 |
| clean | 없음(원래 거래 목록도 맞음) | 재현성 일치 · 미래참조 이 범위에서 통과 |
| leak | 다음 봉 이동평균(shift(-1)) | 미래참조 의심 |
| leak_roll | 다음 봉 종가를 np.roll(-1)로 당김(정적 패턴에 안 걸림) | 미래참조 의심(잘라 다시 돌린 신호 차이) |
| leak_stat | 전체 기간 평균 위에 있나(정적 패턴에 안 걸림) | 미래참조 의심(잘라 다시 돌린 신호 차이) |
| midfill | 중간가 체결 가정 함수 | 거래비용 중간가 체결 가정 발견 |
| other_params | 원래 거래 목록은 5/20, 연결은 10/30 | 재현성 불일치 → 나머지 점검 넷 · 추천 가드레일 점검 불가(재현 불일치) |
| leverage | 비중 1.5 | 전부 점검 불가(표현 불가) |
| portfolio | 3종목 모멘텀 1등만 | 미래참조 이 범위에서 통과 · 3종목 |
"""
from datetime import timedelta

import pytest
from conftest import write_link

from blackclip.check.backtest import simulate
from blackclip.check.link import check_prices, check_weights, load_link, to_signals
from blackclip.check.report import LABELS
from blackclip.cli import main

PRICES = '''
import numpy as np
import pandas as pd

EXEC = "next_open"
MARKET = "spot"


def _walk(seed):
    rng = np.random.default_rng(seed)
    c = 100 * np.exp(np.cumsum(rng.normal(0, 0.02, 500)))
    o = np.concatenate([[100.0], c[:-1]])
    idx = pd.date_range("2023-01-01", periods=500, freq="D", tz="UTC")
    return pd.DataFrame({"Open": o, "High": np.maximum(o, c) * 1.01, "Low": np.minimum(o, c) * 0.99, "Close": c},
                        index=idx)


def load_prices():
    return {"BTCUSDT": _walk(1)}

'''
SMA = '''
def signals(prices, fast=10, slow=30):
    c = prices["BTCUSDT"]["Close"]
    return (c.rolling(fast).mean() > c.rolling(slow).mean()).astype(float)
'''
STRATEGIES = {
    "clean": PRICES + SMA + '\nTRADES = "bt/clean.csv"\n',
    "leak": PRICES + SMA.replace("c.rolling(fast).mean()", "c.rolling(fast).mean().shift(-1)"),
    "leak_roll": PRICES + '''
def signals(prices):
    c = prices["BTCUSDT"]["Close"]
    nxt = pd.Series(np.roll(c.to_numpy(), -1), index=c.index)  # 다음 봉 종가(shift(-n) 글자가 없다)
    return (nxt > c).astype(float)
''',
    "leak_stat": PRICES + '''
def signals(prices):
    c = prices["BTCUSDT"]["Close"]
    return (c > c.mean()).astype(float)  # 전체 기간 평균: 자르면 평균이 달라져 신호가 바뀐다
''',
    "midfill": PRICES + SMA + '\n\ndef fill_price(bar):\n    return (bar["High"] + bar["Low"]) / 2\n',
    "other_params": PRICES + SMA + '\nTRADES = "bt/other.csv"\n',
    "leverage": PRICES + SMA.replace(".astype(float)", ".astype(float) * 1.5"),
    "portfolio": PRICES.replace('return {"BTCUSDT": _walk(1)}',
                                'return {"BTCUSDT": _walk(1), "ETHUSDT": _walk(2), "SOLUSDT": _walk(3)}') + '''
def signals(prices):
    mom = pd.DataFrame({s: p["Close"].pct_change(20) for s, p in prices.items()})
    best = mom.fillna(float("-inf")).idxmax(axis=1)  # 앞 20봉은 NaN이라 idxmax가 못 고름
    return pd.DataFrame({s: (best == s).astype(float) for s in prices}, index=mom.index).where(mom.notna().all(axis=1), 0.0)
''',
}


def _trades_csv(path, root, name, **kw):
    """연결 파일의 신호로 원래 거래 목록을 만든다(봉 안 +5분 시각, 고객 백테스트가 적은 것처럼)."""
    link = load_link(root, name)
    prices, naive = check_prices(link.module.load_prices())
    w = check_weights(link.module.signals(to_signals(prices, naive), **kw), prices)
    rows = ["time,symbol,side,qty,price,fee,order_id"]
    for i, e in enumerate(simulate(prices, w, link.exec_).executions):
        rows.append(f"{(e.time + timedelta(minutes=5)).isoformat()},{e.symbol},{e.side},{e.qty},{e.price},0,o{i}")
    path.write_text("\n".join(rows) + "\n", encoding="utf-8")


@pytest.fixture(scope="module")
def repo(tmp_path_factory):
    """여덟 전략이 든 레포. 원래 거래 목록 두 개는 같은 신호 함수로(other는 다른 파라미터로) 만든다."""
    top = tmp_path_factory.mktemp("planted")
    root = None
    for name, body in STRATEGIES.items():
        root = write_link(top, body, name)
    (top / "bt").mkdir()
    for name in ("clean", "other_params"):
        (top / "bt" / f"{name}.csv").write_text("time,symbol,side,qty,price,fee,order_id\n", encoding="utf-8")
    _trades_csv(top / "bt" / "clean.csv", root, "midfill")
    _trades_csv(top / "bt" / "other.csv", root, "midfill", fast=5, slow=20)
    return top


def _run(repo, monkeypatch, capsys, name):
    monkeypatch.chdir(repo)
    assert main(["check", name, "--no-binance"]) == 0
    return capsys.readouterr().out


def test_clean_passes(repo, monkeypatch, capsys):
    out = _run(repo, monkeypatch, capsys, "clean")
    assert "재현성  일치" in out and "미래참조  이 범위에서 통과" in out


def test_leak_caught(repo, monkeypatch, capsys):
    out = _run(repo, monkeypatch, capsys, "leak")
    assert "미래참조  의심" in out and "먼저 볼 항목: 미래참조" in out
    assert "데이터 끝을 잘라 다시 돌리면" in out and "정적 검사 후보 .blackclip/strategies/leak/link.py:" in out


@pytest.mark.parametrize("name", ["leak_roll", "leak_stat"])
def test_leak_caught_without_static_pattern(repo, monkeypatch, capsys, name):
    """정적 검사 패턴(shift(-n) 등)에 안 걸리는 미래참조는 데이터 끝을 잘라 다시 돌린 신호 차이로 잡는다."""
    out = _run(repo, monkeypatch, capsys, name)
    assert "미래참조  의심 · 데이터 끝을 잘라 다시 돌리면" in out and "이후 신호" in out
    assert "정적 검사 후보" not in out and "먼저 볼 항목: 미래참조" in out


def test_midfill_found(repo, monkeypatch, capsys):
    out = _run(repo, monkeypatch, capsys, "midfill")
    assert "중간가 체결 가정 발견: .blackclip/strategies/midfill/link.py:" in out


def test_other_params_mismatch(repo, monkeypatch, capsys):
    out = _run(repo, monkeypatch, capsys, "other_params")
    assert "재현성  불일치" in out and out.count("점검 불가(재현 불일치)") == 5


def test_leverage_not_expressible(repo, monkeypatch, capsys):
    out = _run(repo, monkeypatch, capsys, "leverage")
    assert out.count("점검 불가(표현 불가)") == len(LABELS)


def test_portfolio_runs(repo, monkeypatch, capsys):
    out = _run(repo, monkeypatch, capsys, "portfolio")
    assert "· 3종목 ·" in out and "미래참조  이 범위에서 통과" in out

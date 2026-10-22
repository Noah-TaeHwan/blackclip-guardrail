"""「두 번 돌려 같은 숫자」 + 시도 수 · 국면 · save 심은 결함 끝-끝 시험(우리 시험 전략 셋).

| 전략 | 심은 것 | 기대 |
| momentum | 3종목 · 900일 · 자기상관 수익(전날 오르면 산다) · TRIALS 1,000개 | 시도 수 하향(범위) · 국면 충분 · 정상 범위를 그림 |
| bull | 1종목 · 오르기만 하는 장 | 국면 편중(횡보 · 하락 0건) |
| sparse | 1종목 · 500일 · SMA 10/30 | 국면 거래 부족 · save는 정상 범위 그리지 않음 |
"""
import json
from datetime import date

import pytest
from conftest import write_link

from blackclip.check.save import run_save
from blackclip.cli import main

PRICES = '''
import numpy as np
import pandas as pd

EXEC = "next_open"
MARKET = "spot"


def _walk(seed, n=900, drift=0.0):
    rng = np.random.default_rng(seed)
    e = rng.normal(drift, 0.02, n)
    r = np.zeros(n)
    for i in range(n):
        r[i] = e[i] + (0.3 * r[i - 1] if i else 0.0)  # 자기상관 0.3: 전날 방향이 이어지는 장
    c = 100 * np.exp(np.cumsum(r))
    o = np.concatenate([[100.0], c[:-1]])
    idx = pd.date_range("2022-01-01", periods=n, freq="D", tz="UTC")
    return pd.DataFrame({"Open": o, "High": np.maximum(o, c) * 1.01, "Low": np.minimum(o, c) * 0.99, "Close": c},
                        index=idx)

'''
MOMENTUM = '''
def signals(prices):
    return pd.DataFrame({s: (p["Close"].pct_change() > 0).astype(float) / len(prices) for s, p in prices.items()})
'''
STRATEGIES = {
    "momentum": PRICES + '''
TRIALS = (1000, "격자 10x10x10")


def load_prices():
    return {"BTCUSDT": _walk(0), "ETHUSDT": _walk(1), "SOLUSDT": _walk(2)}
''' + MOMENTUM,
    "bull": PRICES + '''
def load_prices():
    return {"BTCUSDT": _walk(5, drift=0.004)}
''' + MOMENTUM,
    "sparse": PRICES + '''
def load_prices():
    return {"BTCUSDT": _walk(1, n=500)}


def signals(prices):
    c = prices["BTCUSDT"]["Close"]
    return (c.rolling(10).mean() > c.rolling(30).mean()).astype(float)
''',
}


@pytest.fixture(scope="module")
def repo(tmp_path_factory):
    """세 전략이 든 레포."""
    top = tmp_path_factory.mktemp("planted_s5")
    for name, body in STRATEGIES.items():
        write_link(top, body, name)
    return top


def _run(repo, monkeypatch, capsys, *argv):
    monkeypatch.chdir(repo)
    code = main(list(argv))
    out = capsys.readouterr()
    return code, out.out, out.err


def test_momentum_c3_lowered_c4_enough(repo, monkeypatch, capsys):
    code, out, _ = _run(repo, monkeypatch, capsys, "check", "momentum", "--no-binance")
    assert code == 0
    assert "시도 수  하향(범위) · 샤프 " in out and "(시도 1,000개: 고객이 밝힌 값 · 최솟값(미검증) · 격자 10x10x10)" in out
    assert "국면  충분 · 왕복 거래 541건 중 " in out
    assert "추천 가드레일  민감도 보통 · 앞으로 한 달 90% 정상 범위 " in out


def test_bull_regime_skew(repo, monkeypatch, capsys):
    _, out, _ = _run(repo, monkeypatch, capsys, "check", "bull", "--no-binance")
    assert "국면  국면 편중 · 횡보 · 하락 거래가 30건보다 적음" in out and "횡보 0 · 하락 0" in out


def test_sparse_too_few_and_no_range(repo, monkeypatch, capsys):
    _, out, _ = _run(repo, monkeypatch, capsys, "check", "sparse", "--no-binance")
    assert "국면  거래 부족 · " in out
    code, out, _ = _run(repo, monkeypatch, capsys, "save", "sparse")
    assert code == 0 and "정상 범위 그리지 않음(백테스트 왕복 거래 " in out


def test_momentum_save_draws_range(repo, monkeypatch, capsys):
    code, out, _ = _run(repo, monkeypatch, capsys, "save", "momentum", "--sensitivity", "민감")
    assert code == 0 and out.splitlines()[1].startswith("앞으로 한 달 90% 정상 범위 ")
    doc = json.loads((repo / ".blackclip" / "strategies" / "momentum" / "guardrail.v1.yaml").read_text(encoding="utf-8"))
    nr = doc["normal_range"]
    assert nr["low"] < 0 < nr["high"] and doc["warning_line"]["line"] < nr["low"]
    assert doc["checks"]["C3"] == "하향(범위)" and doc["checks"]["C4"] == "충분"
    assert doc["target"]["symbols"] == ["BTCUSDT", "ETHUSDT", "SOLUSDT"]


def test_two_runs_same_numbers(tmp_path, monkeypatch, capsys):
    """두 번 돌려 같은 숫자: 같은 연결 파일을 두 레포에서 저장하면 가드레일 파일이 바이트까지 같고,
    점검 화면도 두 번 같다(점검 시각 줄은 check.md에만 있음)."""
    files = []
    for side in ("a", "b"):
        top = tmp_path / side
        top.mkdir()
        root = write_link(top, STRATEGIES["momentum"], "momentum")
        path, _ = run_save(root, "momentum", "보통", date(2026, 9, 23), None, date(2026, 10, 15))
        files.append(path.read_bytes())
    assert files[0] == files[1]
    outs = [_run(tmp_path / "a", monkeypatch, capsys, "check", "momentum", "--no-binance")[1] for _ in range(2)]
    assert outs[0] == outs[1]

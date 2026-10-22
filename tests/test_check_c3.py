"""check.c3: 「N번 시도했다면」 표 · TRIALS."""
import math
from pathlib import Path

import numpy as np
import pandas as pd
import pytest
from conftest import write_link

from blackclip.check import c3 as c3_mod
from blackclip.check.c3 import c3, deflate, emax
from blackclip.check.link import LinkError, has_abs_path, load_link
from blackclip.check.runner import BAD_REPLY, Runner


def _returns(mu, n=2800, seed=0, freq="D"):
    idx = pd.date_range("2019-01-01", periods=n, freq=freq, tz="UTC")
    return pd.Series(np.random.default_rng(seed).normal(mu, 0.02, n), index=idx)


def test_emax_matches_stage0_table():
    """표값(scipy norm.ppf와 같은 값)."""
    assert [round(emax(k), 4) for k in (10, 100, 1920, 10_000)] == [1.5746, 2.5306, 3.4362, 3.8607]
    assert emax(1) == 0.0


def test_emax_huge_k_stable():
    """아주 큰 k에서도 죽지 않는다(1 - 1/k가 1.0으로 반올림되는 자리). 예: 1.42 · 2,800봉 · 10^17 → -1.67179."""
    assert emax(10**12) > emax(10**6) > 0
    assert round(1.42 - emax(10**17) * (365.25 / 2800) ** 0.5, 5) == -1.67179


def test_deflate_numbers_for_guardrail():
    """가드레일 파일에 남길 시도 수 숫자: 시도 수 · 출처 · 방법 · 아래쪽 · 위쪽(모름 = None) · 미검증."""
    d = deflate(_returns(0.002), 86400, (1920, "격자"))
    assert (d["trials"], d["source"], d["note"], d["method"], d["adjusted_high"], d["unverified"]) == (
        1920, "고객이 밝힌 값", "격자", "독립 가정 근사", None, True)
    assert d["adjusted_low"] < d["sharpe_cost0"] and [t for t, _ in d["table"]] == [10, 50, 100, 1000, 10000]
    none = deflate(_returns(0.002), 86400, None)
    assert (none["trials"], none["source"], none["adjusted_low"]) == (None, None, None) and none["zero_at_trials"] > 1
    assert deflate(_returns(-0.002), 86400, (10, "x"))["table"] is None


def _scale(d, n_trials=10_000):
    """deflate 결과에서 샤프 표준오차를 거꾸로 구한다(표값 = 샤프 - emax(N) * scale)."""
    return (d["sharpe_cost0"] - dict(d["table"])[n_trials]) / emax(n_trials)


@pytest.mark.parametrize("n, freq, bar_s, expect", [(2800, "D", 86400, 0.361), (26_280, "h", 3600, 0.578)])
def test_deflate_scale_by_bar_length(n, freq, bar_s, expect):
    """샤프 표준오차 = sqrt(365.25 * 86400 / 봉 길이 / 봉 수): 일봉 2,800개 약 0.361 · 시간봉 26,280개 약 0.578."""
    d = deflate(_returns(0.002, n=n, freq=freq), bar_s, None)
    assert round(_scale(d), 3) == expect == round(math.sqrt(365.25 * 86400 / bar_s / n), 3)


def test_deflate_table_decreasing_in_trials():
    """시도 수가 늘수록 깎인 샤프는 줄어든다(표 순서 그대로)."""
    values = [v for _, v in deflate(_returns(0.002), 86400, None)["table"]]
    assert values == sorted(values, reverse=True) and len(set(values)) == len(values)


def test_zero_at_trials_is_exact_crossing(monkeypatch):
    """0 이하가 되는 시도 수는 2의 거듭제곱으로 올려 잡지 않고 정수로 이분 탐색한다(샤프 1.33 · 일봉 2,800개)."""
    monkeypatch.setattr(c3_mod, "metrics", lambda returns, bar_s: {"cagr": 0.0, "sharpe": 1.33})
    k = deflate(_returns(0.002), 86400, None)["zero_at_trials"]
    scale = math.sqrt(365.25 / 2800)
    assert k == 4898 and k < 2 ** 13
    assert 1.33 - emax(k) * scale <= 0 < 1.33 - emax(k - 1) * scale
    assert f"샤프가 0 이하가 되는 시도 수 약 {k:,}개" in c3(_returns(0.002), 86400, None)["text"]


@pytest.mark.parametrize("sharpe, expect", [(1e-6, 2), (100.0, None)])
def test_zero_at_trials_edges(monkeypatch, sharpe, expect):
    """첫 거듭제곱(2)에서 닿으면 2, 2^40개로도 안 닿으면 None(상한 그대로)."""
    monkeypatch.setattr(c3_mod, "metrics", lambda returns, bar_s: {"cagr": 0.0, "sharpe": sharpe})
    assert deflate(_returns(0.002), 86400, None)["zero_at_trials"] == expect


def test_c3_unknown_shows_table():
    """시도 수가 없으면 모름 + 표(독립 가정 꼬리표, N을 추측하지 않음)."""
    r = c3(_returns(0.002), 86400, None)
    assert r["state"] == "모름"
    assert "시도 수 기록 없음(훅 기록 전)" in r["text"]
    assert "N번 시도했다면(독립 가정 · 과하게 깎일 수 있음): 10회면 " in r["text"]
    assert "10,000회면 " in r["text"] and "샤프가 0 이하가 되는 시도 수 약 " in r["text"]


def test_c3_trials_lowers_with_range():
    """TRIALS가 있으면 하향(범위): 아래쪽은 독립 가정, 위쪽은 모름, 고객이 밝힌 값 · 최솟값(미검증)."""
    r = c3(_returns(0.002), 86400, (1920, "격자 탐색"))
    assert r["state"] == "하향(범위)"
    assert "~ 모름 (시도 1,920개: 고객이 밝힌 값 · 최솟값(미검증) · 격자 탐색)" in r["text"]


def test_c3_nonpositive_sharpe_not_applicable():
    """비용 0 샤프가 0 이하면 해당 없음."""
    assert c3(_returns(-0.002), 86400, (10, "x"))["state"] == "해당 없음"


LINK = '''
import pandas as pd

EXEC = "next_open"
MARKET = "spot"


def load_prices():
    idx = pd.date_range("2024-01-01", periods=5, freq="D", tz="UTC")
    return {"BTCUSDT": pd.DataFrame({"Open": 1.0, "High": 1.0, "Low": 1.0, "Close": 1.0}, index=idx)}


def signals(prices):
    return pd.Series(0.0, index=prices["BTCUSDT"].index)
'''


def test_load_link_reads_trials(tmp_path):
    """TRIALS = (N, 근거)를 읽는다. 없으면 None."""
    assert load_link(write_link(tmp_path, LINK), "s1").trials is None
    link = load_link(write_link(tmp_path, LINK + '\nTRIALS = (1920, " 격자 10x12x16 ")\n'), "s1")
    assert link.trials == (1920, "격자 10x12x16")


@pytest.mark.parametrize("bad", ["1920", "(True, 'x')", "(0, 'x')", "(-5, 'x')", "(10.5, 'x')", "(10, '')",
                                 "(10,)", "('10', 'x')", "(10, 'x' * 121)", "(10**13, 'x')"])
def test_load_link_bad_trials_one_line(tmp_path, bad):
    """TRIALS 꼴이 틀리면 레포 기준 경로를 넣은 한 줄(절대 경로 없음)."""
    with pytest.raises(LinkError) as exc:
        load_link(write_link(tmp_path, LINK + f"\nTRIALS = {bad}\n"), "s1")
    assert str(exc.value).startswith(".blackclip/strategies/s1/link.py: TRIALS는 ")
    assert str(tmp_path) not in str(exc.value)


def test_runner_passes_trials(tmp_path):
    """고객 코드는 하위 프로세스에서 돌고, TRIALS는 엔진 쪽 Link에 다시 확인돼 온다."""
    with Runner(write_link(tmp_path, LINK + '\nTRIALS = (50, "x")\n'), "s1") as runner:
        assert runner.link.trials == (50, "x")


@pytest.mark.parametrize("note", ["/srv/alice/quant/grid.csv", "~/quant/grid.csv", "C:\\quant\\grid.csv",
                                  "근거 파일 /home/a/x.csv"])
def test_load_link_trials_note_absolute_path_rejected(tmp_path, note):
    """TRIALS 근거에 절대 경로가 있으면 받지 않는다(check.md는 커밋됨). 「10/30」 같은 말은 받는다."""
    with pytest.raises(LinkError, match="TRIALS 근거에 절대 경로가 있습니다"):
        load_link(write_link(tmp_path, LINK + f"\nTRIALS = (10, {note!r})\n"), "s1")
    assert load_link(write_link(tmp_path, LINK + '\nTRIALS = (10, "SMA 10/30 · 5~15 격자")\n'), "s1").trials[0] == 10


@pytest.mark.parametrize("text", ["/srv/alice/quant/grid.csv", "~/quant/grid.csv", "C:\\quant\\grid.csv", "D:/data/x.csv",
                                  "근거 파일 /home/a/x.csv", "file:///Users/alice/x.csv", "dir:/srv/a/b", "근거:/home/a/x",
                                  "\\\\server\\share\\x", "(/srv/a/b)", "path=/srv/a/b"])
def test_has_abs_path_flags_path_like_tokens(text):
    """절대 경로 꼴(폴더 둘 이상의 /…, ~/, file://, 드라이브, 서버 경로)은 잡는다."""
    assert has_abs_path(text)


@pytest.mark.parametrize("text", ["SMA 10/30 · 5~15 격자", "5 ~ 15 격자", "IS / OOS", "train / test", "PnL +3 / -2", "격자 /",
                                  "https://example.com/grid/x", "w/ 5/15 격자", "IS:OOS 70/30"])
def test_has_abs_path_lets_plain_slashes_through(text):
    """나눗셈 · 범위 · 구분 · 주소의 슬래시는 절대 경로가 아니다."""
    assert not has_abs_path(text)


def test_has_abs_path_home_guard(monkeypatch):
    """홈 폴더 글은 그대로 잡고, 홈 폴더를 못 정하거나 「/」면 그 검사만 건너뛴다(모든 글이 걸리지 않게)."""
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: Path("/alice")))
    assert has_abs_path("export in /alice")

    def _raise(cls):
        raise RuntimeError("no home")

    monkeypatch.setattr(Path, "home", classmethod(_raise))
    assert not has_abs_path("export in /alice")
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: Path("/")))
    assert not has_abs_path("IS / OOS 5/15")


@pytest.mark.parametrize("note", ["첫 줄\n둘째 줄", "a\x00b", "a\tb", "a\rb"])
def test_load_link_trials_note_control_chars_rejected(tmp_path, note):
    """TRIALS 근거에 줄바꿈 · 탭 · NUL이 있으면 한 줄 메시지로 받지 않는다(check.md 표가 깨짐). 앞뒤 공백 · 줄바꿈은 떼고 받는다."""
    with pytest.raises(LinkError, match="TRIALS 근거에 줄바꿈") as exc:
        load_link(write_link(tmp_path, LINK + f"\nTRIALS = (10, {note!r})\n"), "s1")
    assert "\n" not in str(exc.value)
    assert load_link(write_link(tmp_path, LINK + '\nTRIALS = (10, " 격자\\n")\n'), "s1").trials == (10, "격자")


@pytest.mark.parametrize("trials", [[0, "x"], [True, "x"], "1920", [10, "/srv/a/b"], [10, "a\nb"], [10]])
def test_runner_bad_trials_reply_is_bad_reply(tmp_path, trials):
    """하위 프로세스가 계약 밖 TRIALS를 돌려주면 다른 값처럼 BAD_REPLY 한 줄이다. 없는 키는 None."""
    with Runner(write_link(tmp_path, LINK), "s1") as runner:
        meta = {"exec": "next_open", "market": "spot", "trades": None, "result_cagr": None, "naive": False, "files": []}
        assert runner._link(meta, ".blackclip", "s1")[0].trials is None
        assert runner._link({**meta, "trials": [50, "x"]}, ".blackclip", "s1")[0].trials == (50, "x")
        with pytest.raises(LinkError) as exc:
            runner._link({**meta, "trials": trials}, ".blackclip", "s1")
        assert str(exc.value) == BAD_REPLY


@pytest.mark.parametrize("note", ["격자\u00a010", "\ufeff격자 10", "격자\u200b10", "격자 \U0001F468\u200d\U0001F4BB"])
def test_trials_invisible_or_format_char_named(tmp_path, note):
    """NBSP · BOM · ZWSP · ZWJ 이모지는 제어 문자가 아니라 「보이지 않는 공백 · 서식 문자」로 알린다."""
    with pytest.raises(LinkError, match="TRIALS 근거에 보이지 않는 공백 · 서식 문자가 있습니다"):
        load_link(write_link(tmp_path, LINK + f"\nTRIALS = (10, {note!r})\n"), "s1")

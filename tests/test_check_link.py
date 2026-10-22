"""check.link: 연결 파일 · 가격표 · 신호 확인."""
import subprocess

import numpy as np
import pandas as pd
import pytest
from conftest import make_prices, write_link

from blackclip.check.link import (ExpressError, LinkError, bar_seconds, check_prices, check_weights, load_link,
                                  to_signals)

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


def test_load_link_reads_contract(tmp_path):
    """EXEC · MARKET을 읽고, TRADES · RESULT_CAGR가 없으면 None."""
    link = load_link(write_link(tmp_path, LINK), "s1")
    assert (link.exec_, link.market, link.trades, link.result_cagr) == ("next_open", "spot", None, None)
    assert link.repo.resolve() == tmp_path.resolve()


def test_load_link_missing_file_says_where(tmp_path):
    """연결 파일이 없으면 레포 기준 자리를 알려 준다(절대 경로 없음)."""
    root = write_link(tmp_path, LINK)
    with pytest.raises(LinkError) as exc:
        load_link(root, "s2")
    assert ".blackclip/strategies/s2/link.py" in str(exc.value)
    assert str(tmp_path) not in str(exc.value)


def test_link_symlink_message(tmp_path):
    """link.py가 링크이면 따라가 읽지 않고 레포 기준 자리와 함께 한 줄(합쇼체 그대로)."""
    root = write_link(tmp_path, LINK)
    real = tmp_path / "real_link.py"
    (root / "strategies" / "s1" / "link.py").rename(real)
    (root / "strategies" / "s1" / "link.py").symlink_to(real)
    with pytest.raises(LinkError) as exc:
        load_link(root, "s1")
    assert str(exc.value) == ".blackclip/strategies/s1/link.py가 링크라 읽지 않습니다"


def test_bad_name_rejected(tmp_path):
    """폴더를 벗어나는 이름은 받지 않는다."""
    with pytest.raises(LinkError, match="전략 이름"):
        load_link(write_link(tmp_path, LINK), "../x")


def test_load_link_bad_exec(tmp_path):
    """EXEC가 모르는 값이면 한 줄."""
    with pytest.raises(LinkError, match="EXEC"):
        load_link(write_link(tmp_path, LINK.replace('"next_open"', '"open"')), "s1")


def test_load_link_import_error_one_line(tmp_path):
    """고객 코드가 불러오다 죽어도 traceback 대신 LinkError 한 줄."""
    with pytest.raises(LinkError, match="ZeroDivisionError"):
        load_link(write_link(tmp_path, LINK + "\nX = 1 / 0\n"), "s1")


def test_load_link_imports_repo_module_and_dataclass(tmp_path):
    """연결 파일이 고객 레포의 모듈을 불러오고, 안에서 dataclass를 써도 된다(sys.modules 등록)."""
    (tmp_path / "mystrat_s1.py").write_text("WEIGHT = 0.5\n", encoding="utf-8")
    body = "from dataclasses import dataclass\nfrom mystrat_s1 import WEIGHT\n" + LINK + "\n@dataclass\nclass P:\n    n: 'int' = 1\n"
    link = load_link(write_link(tmp_path, body), "s1")
    assert link.module.WEIGHT == 0.5


def test_load_link_trades_must_be_relative_inside_repo(tmp_path):
    """TRADES는 레포 안 상대 경로만(check.md가 커밋되므로)."""
    with pytest.raises(LinkError, match="TRADES"):
        load_link(write_link(tmp_path, LINK + '\nTRADES = "/etc/passwd"\n'), "s1")
    with pytest.raises(LinkError, match="TRADES"):
        load_link(write_link(tmp_path, LINK + '\nTRADES = "../outside.csv"\n'), "s1")
    (tmp_path / "bt").mkdir()
    (tmp_path / "bt" / "t.csv").write_text("time,symbol,side,qty,price,fee,order_id\n", encoding="utf-8")
    link = load_link(write_link(tmp_path, LINK + '\nTRADES = "bt/t.csv"\nRESULT_CAGR = 0.182\n'), "s1")
    assert (link.trades_label, link.result_cagr) == ("bt/t.csv", 0.182)


def test_gitignore_keeps_link_py(tmp_path):
    """link.py · check.md는 Git에 보이고 체결 파일은 안 보인다."""
    subprocess.run(["git", "init", "-q", str(tmp_path)], check=True)
    root = write_link(tmp_path, LINK)
    (root / "strategies" / "s1" / "check.md").write_text("x", encoding="utf-8")
    (root / "fills").mkdir()
    (root / "fills" / "a.csv").write_text("x", encoding="utf-8")
    out = subprocess.run(["git", "-C", str(tmp_path), "status", "--porcelain", "--untracked-files=all"],
                         capture_output=True, text=True, check=True).stdout
    assert ".blackclip/strategies/s1/link.py" in out
    assert ".blackclip/strategies/s1/check.md" in out
    assert "fills" not in out


def test_prices_naive_becomes_utc_flag():
    """시간대 없는 시각은 UTC로 셈하고 그렇다고 알린다."""
    prices, naive = check_prices({"BTCUSDT": make_prices(5, tz=None)})
    assert str(prices["BTCUSDT"].index.tz) == "UTC"
    assert naive is True


def test_to_signals_keeps_naive():
    """신호 함수에는 고객이 준 꼴(시간대 없음)으로, 복사본을 넘긴다."""
    prices, naive = check_prices({"BTCUSDT": make_prices(5, tz=None)})
    given = to_signals(prices, naive)
    assert given["BTCUSDT"].index.tz is None
    given["BTCUSDT"].iloc[0, 0] = -1.0
    assert prices["BTCUSDT"].iloc[0, 0] > 0


def test_prices_symbol_normalized_keeps_volume():
    """BTC/USDT 표기를 받고, Volume 열은 있으면 그대로 둔다."""
    prices, naive = check_prices({"BTC/USDT": make_prices(5)})
    assert list(prices) == ["BTCUSDT"]
    assert list(prices["BTCUSDT"].columns) == ["Open", "High", "Low", "Close", "Volume"]
    assert naive is False


def test_prices_lowercase_columns_rejected():
    """열 이름은 대문자 시작으로 정한다(바꿔 넘기면 고객 코드가 깨짐)."""
    with pytest.raises(LinkError, match="Open"):
        check_prices({"BTCUSDT": make_prices(5).rename(columns=str.lower)})


def test_prices_not_dict_or_empty_symbol():
    """dict가 아니거나 종목 이름이 비면 한 줄."""
    with pytest.raises(LinkError, match="load_prices"):
        check_prices(make_prices(5))
    with pytest.raises(LinkError, match="종목 이름"):
        check_prices({" ": make_prices(5)})


def test_prices_duplicate_time():
    """같은 시각이 두 번이면 그 시각을 알린다."""
    df = make_prices(5)
    df = pd.concat([df, df.iloc[[2]]]).sort_index()
    with pytest.raises(LinkError, match="같은 시각.*2024-01-03"):
        check_prices({"BTCUSDT": df})


def test_prices_backwards_time():
    """시각이 거꾸로 가면 한 줄."""
    df = make_prices(5).iloc[[0, 2, 1, 3, 4]]
    with pytest.raises(LinkError, match="거꾸로"):
        check_prices({"BTCUSDT": df})


def test_prices_nat_time():
    """시각이 비어(NaT) 있으면 비교(<)가 False라 지나치므로 따로 막는다."""
    df = make_prices(5)
    df.index = df.index.where(df.index != df.index[2])
    with pytest.raises(LinkError, match="BTCUSDT.*시각이 비어"):
        check_prices({"BTCUSDT": df})


def test_prices_zero_or_nan():
    """0 이하나 빈 가격이면 그 시각을 알린다."""
    df = make_prices(5)
    df.iloc[3, df.columns.get_loc("Close")] = np.nan
    with pytest.raises(LinkError, match="2024-01-04"):
        check_prices({"BTCUSDT": df})
    df = make_prices(5)
    df.iloc[1, df.columns.get_loc("Low")] = 0.0
    with pytest.raises(LinkError, match="0 이하"):
        check_prices({"BTCUSDT": df})


def test_bar_seconds_daily_and_hourly():
    """봉 길이는 시각 차이의 중앙값."""
    assert bar_seconds({"A": make_prices(10)}) == 86400
    assert bar_seconds({"A": make_prices(10, freq="h")}) == 3600


def _prices(*syms):
    return check_prices({s: make_prices(6, seed=i) for i, s in enumerate(syms)})[0]


def test_weights_series_single_symbol_and_nan_is_zero():
    """종목 하나면 Series를 받고, NaN은 0."""
    p = _prices("BTCUSDT")
    w = check_weights(pd.Series([np.nan, 1, 1, 0, -1, 0.5], index=p["BTCUSDT"].index), p)
    assert w["BTCUSDT"].tolist() == [0.0, 1.0, 1.0, 0.0, -1.0, 0.5]


def test_weights_series_multi_symbol_rejected():
    """종목이 여럿인데 Series면 한 줄."""
    p = _prices("BTCUSDT", "ETHUSDT")
    with pytest.raises(LinkError, match="DataFrame"):
        check_weights(pd.Series(0.0, index=p["BTCUSDT"].index), p)


def test_weights_unknown_symbol():
    """가격표에 없는 종목 열이면 한 줄."""
    p = _prices("BTCUSDT")
    with pytest.raises(LinkError, match="가격표에 없는 종목"):
        check_weights(pd.DataFrame({"XRPUSDT": 0.0}, index=p["BTCUSDT"].index), p)


def test_weights_out_of_range_express():
    """-1~1 밖이면 표현 불가."""
    p = _prices("BTCUSDT")
    with pytest.raises(ExpressError, match="-1~1"):
        check_weights(pd.Series(2.0, index=p["BTCUSDT"].index), p)


def test_weights_leverage_sum_express():
    """한 시점 절댓값 합이 1을 넘으면 표현 불가."""
    p = _prices("BTCUSDT", "ETHUSDT")
    idx = p["BTCUSDT"].index
    with pytest.raises(ExpressError, match="합이 1을 넘"):
        check_weights(pd.DataFrame({"BTCUSDT": 0.7, "ETHUSDT": -0.7}, index=idx), p)


def test_weights_no_matching_times():
    """신호 시각이 가격표와 하나도 안 맞으면 한 줄."""
    p = _prices("BTCUSDT")
    other = pd.date_range("2030-01-01", periods=6, freq="D", tz="UTC")
    with pytest.raises(LinkError, match="맞지 않"):
        check_weights(pd.Series(1.0, index=other), p)

"""체결 CSV 읽기: 시간대 · 부분 체결 · 종목 표기 · 잘못된 파일."""
from datetime import datetime, timezone
from decimal import Decimal

import pytest

from blackclip.retro.fills import FillsError, normalize_symbol, read_executions, read_funding

HEADER = "time,symbol,side,qty,price,fee,order_id,fee_asset\n"


def write(tmp_path, text, name="f.csv"):
    """임시 CSV를 쓰고 경로를 돌려준다."""
    p = tmp_path / name
    p.write_text(text, encoding="utf-8")
    return p


def test_partial_fills_grouped_by_order(tmp_path):
    """같은 order_id 세 줄은 체결 1건(수량 합 · 가중 평균가 · 수수료 합)이 된다."""
    p = write(tmp_path, HEADER
              + "2026-10-08T00:31:00+00:00,BTCUSDT,buy,0.05,61000,0.5,A1,USDT\n"
              + "2026-10-08T00:31:05+00:00,BTCUSDT,buy,0.05,61060,0.5,A1,USDT\n"
              + "2026-10-08T00:31:09+00:00,BTCUSDT,buy,0.02,61100,0.2,A1,USDT\n")
    [e] = read_executions(p)
    assert e.qty == Decimal("0.12")
    expected = (Decimal("0.05") * 61000 + Decimal("0.05") * 61060 + Decimal("0.02") * 61100) / Decimal("0.12")
    assert e.price == expected
    assert e.fee == Decimal("1.2")
    assert e.time == datetime(2026, 10, 8, 0, 31, tzinfo=timezone.utc)


def test_symbol_normalized():
    """BTC/USDT · btc-usdt · BTCUSDT · ccxt 선물 BTC/USDT:USDT 를 같은 종목으로 본다."""
    assert normalize_symbol("BTC/USDT") == normalize_symbol("btc-usdt") == normalize_symbol("BTC/USDT:USDT") == "BTCUSDT"


def test_naive_time_is_rejected(tmp_path):
    """시간대가 없는 시각은 추측하지 않고 행 번호와 함께 멈춘다."""
    p = write(tmp_path, HEADER + "2026-10-08 09:31:00,BTCUSDT,buy,0.12,61054,1.2,B1,USDT\n")
    with pytest.raises(FillsError, match="2행.*시간대"):
        read_executions(p)


def test_korean_side_words(tmp_path):
    """매수 · 매도 표기도 buy · sell로 읽는다."""
    p = write(tmp_path, HEADER + "2026-10-08T09:31:00+09:00,BTCUSDT,매수,0.12,61054,1.2,B1,USDT\n")
    assert read_executions(p)[0].side == "buy"


def test_missing_column_named(tmp_path):
    """필수 열이 없으면 빠진 열 이름을 알려 준다."""
    p = write(tmp_path, "time,symbol,side,qty,price\n2026-10-08T00:00:00+00:00,BTCUSDT,buy,1,1\n")
    with pytest.raises(FillsError, match="fee.*order_id"):
        read_executions(p)


def test_bad_number_names_row(tmp_path):
    """숫자가 아닌 값은 행 번호와 열 이름으로 알려 준다."""
    p = write(tmp_path, HEADER + "2026-10-08T00:00:00+00:00,BTCUSDT,buy,abc,61000,1,C1,USDT\n")
    with pytest.raises(FillsError, match="2행 qty"):
        read_executions(p)


def test_header_only_is_empty(tmp_path):
    """머리줄만 있으면 빈 목록이다(오류 아님)."""
    assert read_executions(write(tmp_path, HEADER)) == []


def test_read_funding(tmp_path):
    """펀딩비 CSV는 부호 그대로 읽는다(내면 -)."""
    p = write(tmp_path, "time,symbol,amount,asset\n2026-10-10T08:00:00+00:00,BTCUSDT,-1.25,USDT\n", "fund.csv")
    [f] = read_funding(p)
    assert f.amount == Decimal("-1.25") and f.symbol == "BTCUSDT"


def test_mixed_fee_assets_become_unknown(tmp_path):
    """부분 체결마다 수수료 자산이 다르면 자산을 모름("")으로 둔다(첫 줄 자산으로 추측하지 않는다)."""
    p = write(tmp_path, HEADER
              + "2026-10-08T00:31:00+00:00,BTCUSDT,buy,0.05,61000,0.3,A1,USDT\n"
              + "2026-10-08T00:31:05+00:00,BTCUSDT,buy,0.05,61060,0.0004,A1,BNB\n")
    assert read_executions(p)[0].fee_asset == ""


def test_duplicate_row_rejected(tmp_path):
    """이어 붙인 내보내기의 완전히 같은 줄은 두 행 번호와 함께 멈춘다."""
    line = "2026-10-08T00:31:00+00:00,BTCUSDT,buy,0.12,61000,1.2,A1,USDT\n"
    with pytest.raises(FillsError, match="3행.*2행"):
        read_executions(write(tmp_path, HEADER + line + line))


def test_trade_id_separates_equal_fills(tmp_path):
    """같은 주문의 같은 가격 · 수량 두 체결은 trade_id 열로 구분하면 중복이 아니다."""
    head = "time,symbol,side,qty,price,fee,order_id,fee_asset,trade_id\n"
    p = write(tmp_path, head
              + "2026-10-08T00:31:00+00:00,BTCUSDT,buy,0.06,61000,0.6,A1,USDT,t1\n"
              + "2026-10-08T00:31:00+00:00,BTCUSDT,buy,0.06,61000,0.6,A1,USDT,t2\n")
    assert read_executions(p)[0].qty == Decimal("0.12")


def test_same_order_id_other_symbol_is_other_order(tmp_path):
    """주문 번호는 종목 안에서만 유일하므로 (종목, 주문 번호)로 묶는다."""
    p = write(tmp_path, HEADER
              + "2026-10-08T00:31:00+00:00,BTCUSDT,buy,0.12,61000,1.2,7,USDT\n"
              + "2026-10-08T00:32:00+00:00,ETHUSDT,buy,1,2400,0.5,7,USDT\n")
    assert [e.symbol for e in read_executions(p)] == ["BTCUSDT", "ETHUSDT"]


def test_short_row_and_blank_line_row_number(tmp_path):
    """칸이 모자란 행은 오류이고, 빈 줄이 있어도 파일의 실제 줄 번호를 알려 준다."""
    p = write(tmp_path, HEADER + "\n" + "2026-10-08T00:31:00+00:00,BTCUSDT,buy\n")
    with pytest.raises(FillsError, match="3행.*칸 수"):
        read_executions(p)


def test_zero_price_rejected(tmp_path):
    """가격 0은 행 번호와 함께 멈춘다(체결가 차이를 나눌 수 없음)."""
    p = write(tmp_path, HEADER + "2026-10-08T00:31:00+00:00,BTCUSDT,buy,0.12,0,1.2,A1,USDT\n")
    with pytest.raises(FillsError, match="2행 price"):
        read_executions(p)


def test_blank_symbol_rejected(tmp_path):
    """종목이 비면 묶을 기준이 없으므로 행 번호와 함께 멈춘다(엉뚱한 종목끼리 평균내지 않는다)."""
    p = write(tmp_path, HEADER + "2026-10-08T00:31:00+00:00,,buy,0.12,61000,1.2,A1,USDT\n")
    with pytest.raises(FillsError, match="2행 symbol"):
        read_executions(p)


def test_funding_blank_symbol_and_asset_rejected(tmp_path):
    """펀딩비의 빈 종목 · 빈 자산도 행 번호와 함께 멈춘다."""
    p = write(tmp_path, "time,symbol,amount,asset\n2026-10-10T08:00:00+00:00,,-1.25,USDT\n", "fund.csv")
    with pytest.raises(FillsError, match="2행 symbol"):
        read_funding(p)
    p2 = write(tmp_path, "time,symbol,amount,asset\n2026-10-10T08:00:00+00:00,BTCUSDT,-1.25,\n", "fund2.csv")
    with pytest.raises(FillsError, match="2행 asset"):
        read_funding(p2)


def test_funding_duplicate_rejected(tmp_path):
    """기간을 나눠 받은 펀딩비 파일을 이어 붙여 같은 줄이 두 번 들어가면 두 행 번호와 함께 멈춘다."""
    line = "2026-10-10T08:00:00+00:00,BTCUSDT,-1.25,USDT\n"
    with pytest.raises(FillsError, match="3행.*2행"):
        read_funding(write(tmp_path, "time,symbol,amount,asset\n" + line + line, "fund.csv"))


def test_bad_side_names_its_row(tmp_path):
    """같은 주문 안에서 잘못된 방향이 있으면 그 행 번호를 알려 준다(첫 행이 아니다)."""
    p = write(tmp_path, HEADER
              + "2026-10-08T00:31:00+00:00,BTCUSDT,buy,0.05,61000,0.5,A1,USDT\n"
              + "2026-10-08T00:31:05+00:00,BTCUSDT,by,0.05,61060,0.5,A1,USDT\n")
    with pytest.raises(FillsError, match="3행 side"):
        read_executions(p)


def test_mixed_sides_name_both_rows(tmp_path):
    """한 주문에 매수와 매도가 섞이면 두 행 번호를 모두 알려 준다."""
    p = write(tmp_path, HEADER
              + "2026-10-08T00:31:00+00:00,BTCUSDT,buy,0.05,61000,0.5,A1,USDT\n"
              + "2026-10-08T00:31:05+00:00,BTCUSDT,sell,0.05,61060,0.5,A1,USDT\n")
    with pytest.raises(FillsError, match="2행과 3행"):
        read_executions(p)


def test_error_names_file(tmp_path):
    """값 오류 메시지는 파일 이름으로 시작하고, 그 뒤에 행 번호와 열 이름이 온다."""
    p = write(tmp_path, HEADER + "2026-10-08T00:00:00+00:00,BTCUSDT,buy,abc,61000,1,C1,USDT\n", "fills.csv")
    with pytest.raises(FillsError, match=r"fills\.csv 2행 qty"):
        read_executions(p)


def test_time_error_names_file(tmp_path):
    """시각 오류도 파일 이름과 「2행 time」을 함께 담는다."""
    p = write(tmp_path, HEADER + "2026-10-08 09:31:00,BTCUSDT,buy,0.12,61054,1.2,B1,USDT\n", "fills.csv")
    with pytest.raises(FillsError, match=r"fills\.csv 2행 time"):
        read_executions(p)


def test_dated_futures_symbol_kept_apart():
    """만기 있는 선물(BTC/USDT:USDT-241227)은 무기한(BTC/USDT:USDT)과 다른 종목이다."""
    assert normalize_symbol("BTC/USDT:USDT-241227") == "BTCUSDT241227"
    assert normalize_symbol("BTC/USDT:USDT") == "BTCUSDT"


def test_duplicate_header_rejected(tmp_path):
    """머리줄에 같은 열 이름이 두 번 있으면 조용히 뒤쪽 값을 쓰지 않고 멈춘다."""
    p = write(tmp_path, "time,symbol,side,qty,price,fee,order_id,qty\n"
              + "2026-10-08T00:31:00+00:00,BTCUSDT,buy,0.12,61000,1.2,A1,999\n")
    with pytest.raises(FillsError, match="같은 열 이름이 두 번.*qty"):
        read_executions(p)


def test_spot_and_perp_forms_in_one_file_are_refused(tmp_path):
    """같은 종목이 현물 표기(BTC/USDT)와 선물 표기(BTC/USDT:USDT)로 한 파일에 섞이면 두 시장이 합쳐지므로 멈춘다."""
    p = write(tmp_path, HEADER
              + "2026-09-02T00:01:00Z,BTC/USDT,buy,0.01,60010,0.6,1001,USDT\n"
              + "2026-09-02T00:01:05Z,BTC/USDT:USDT,buy,0.5,60020,12,1001,USDT\n")
    with pytest.raises(FillsError) as err:
        read_executions(p)
    assert str(err.value) == "f.csv 3행 symbol: 같은 종목이 현물 표기(BTC/USDT)와 선물 표기(BTC/USDT:USDT)로 섞여 있습니다. 현물과 선물은 파일을 나눠 주세요"


def test_perp_form_first_then_spot_form_is_refused_too(tmp_path):
    """선물 표기가 먼저 나와도 막고, 어느 행에서 만났는지는 두 번째 표기의 행이다."""
    p = write(tmp_path, HEADER
              + "2026-09-02T00:01:00Z,eth/usdt:usdt,buy,1,2500,1,1,USDT\n"
              + "2026-09-02T00:02:00Z,ETH/USDT,buy,1,2500,1,2,USDT\n")
    with pytest.raises(FillsError, match=r"3행 symbol: 같은 종목이 현물 표기\(ETH/USDT\)와 선물 표기\(eth/usdt:usdt\)"):
        read_executions(p)


def test_spot_and_perp_forms_in_funding_file_are_refused(tmp_path):
    """펀딩비 파일도 같은 검사를 한다."""
    p = write(tmp_path, "time,symbol,amount,asset\n2026-09-05T16:00:00Z,BTC/USDT,-0.5,USDT\n2026-09-05T17:00:00Z,BTC/USDT:USDT,-0.5,USDT\n")
    with pytest.raises(FillsError, match="3행 symbol: 같은 종목이 현물 표기"):
        read_funding(p)


def test_bare_and_separated_names_do_not_trigger_the_mix_check(tmp_path):
    """슬래시 없는 BTCUSDT · btc-usdt는 어느 시장인지 말해 주지 않으므로 선물 표기와 함께 있어도 막지 않는다. 종목이 다르면 섞임이 아니다."""
    p = write(tmp_path, HEADER
              + "2026-09-05T10:01:00Z,ETH/USDT:USDT,buy,1,2502,1,1,USDT\n"
              + "2026-09-20T03:00:00Z,ETHUSDT,sell,0.5,2550,1,2,USDT\n"
              + "2026-09-20T04:00:00Z,btc-usdt,sell,0.5,60000,1,3,USDT\n"
              + "2026-09-20T05:00:00Z,BTC/USDT,sell,0.5,60000,1,4,USDT\n"
              + "2026-09-20T06:00:00Z,SOL/USDT:USDT,sell,1,150,1,5,USDT\n")
    assert len(read_executions(p)) == 5

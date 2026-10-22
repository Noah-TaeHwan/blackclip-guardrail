"""키 권한: 조회만이면 통과, 그 밖에 켜진 권한이 하나라도 있으면 거부, 테스트넷은 확인 불가."""
from blackclip.binance.permissions import Permission, check

S1 = {"ipRestrict": False, "createTime": 1792000000000, "enableReading": True, "enableWithdrawals": False,
      "enableInternalTransfer": False, "enableMargin": False, "enableFutures": False, "permitsUniversalTransfer": False,
      "enableVanillaOptions": False, "enableFixApiTrade": False, "enableFixReadOnly": False,
      "enableSpotAndMarginTrading": False, "enablePortfolioMarginTrading": False}
"""실호출과 같은 모양(조회만 켠 키)."""


class Fake:
    """apiRestrictions 하나만 답하는 클라이언트."""

    def __init__(self, answer, testnet=False):
        self.answer, self.testnet, self.calls = answer, testnet, []

    def get(self, path, params=None, signed=True):
        self.calls.append(path)
        return self.answer


def test_read_only_key_passes():
    """실호출에서 받은 모양 그대로면 조회만이고 IP 제한이 없다고 덧붙인다."""
    assert check(Fake(S1)) == Permission("read_only", "조회만 켜져 있음 · IP 제한 없음")


def test_ip_restricted_key_passes_without_note():
    """IP 제한이 켜져 있으면 덧붙임이 없다."""
    assert check(Fake({**S1, "ipRestrict": True})) == Permission("read_only", "조회만 켜져 있음")


def test_trading_permission_rejected():
    """주문 권한이 켜져 있으면 그 이름을 대고 거부한다."""
    p = check(Fake({**S1, "enableSpotAndMarginTrading": True, "enableWithdrawals": True}))
    assert p.status == "rejected"
    assert p.detail == "조회 말고 켜진 권한: enableSpotAndMarginTrading, enableWithdrawals"


def test_unknown_true_field_rejected():
    """문서에 없던 새 권한이 켜져 있어도 거부한다(모르면 막는 쪽)."""
    assert check(Fake({**S1, "enableSomethingNew": True})).status == "rejected"


def test_reading_off_rejected():
    """조회 권한이 꺼진 키는 쓸 수 없다."""
    assert check(Fake({**S1, "enableReading": False})) == Permission("rejected", "조회 권한이 꺼져 있습니다")


def test_testnet_is_unknown_without_call():
    """테스트넷에는 권한 조회 API가 없어 부르지 않고 확인 불가로 둔다."""
    f = Fake(S1, testnet=True)
    assert check(f).status == "unknown" and f.calls == []


def test_rejected_carries_reason_fields():
    """거부 사유를 글과 별개로 필드에도 싣는다(웹이 글 비교 없이 가름): 조회 꺼짐은 reading_off, 켜진 권한은 enabled(이름순), 통과는 둘 다 비어 있다."""
    off = check(Fake({**S1, "enableReading": False}))
    on = check(Fake({**S1, "enableWithdrawals": True, "enableSpotAndMarginTrading": True}))
    ok = check(Fake(S1))
    assert (off.status, off.reading_off, off.enabled) == ("rejected", True, ())
    assert (on.status, on.reading_off, on.enabled) == ("rejected", False, ("enableSpotAndMarginTrading", "enableWithdrawals"))
    assert (ok.status, ok.reading_off, ok.enabled) == ("read_only", False, ())


def test_check_non_dict_raises():
    """응답이 dict이 아니면 추측하지 않고 조회 실패(BinanceError)."""
    import pytest
    from blackclip.binance.client import SHAPE_TEXT, BinanceError
    for bad in ([], "ok", None):
        with pytest.raises(BinanceError) as e:
            check(Fake(bad))
        assert str(e.value).endswith(SHAPE_TEXT)


def test_check_reading_not_bool_raises():
    """enableReading이 없거나 bool이 아니면 조회 실패. 「꺼짐」으로 단정하지 않는다."""
    import pytest
    from blackclip.binance.client import BinanceError
    missing = {k: v for k, v in S1.items() if k != "enableReading"}
    for bad in ({**S1, "enableReading": "true"}, {**S1, "enableReading": 1}, missing):
        with pytest.raises(BinanceError):
            check(Fake(bad))


def test_check_int_fields_pass():
    """실제 응답에 있는 정수 칸(createTime 등)은 모양 오류가 아니다(실측)."""
    assert check(Fake({**S1, "tradingAuthorityExpirationTime": 1793000000000})).status == "read_only"

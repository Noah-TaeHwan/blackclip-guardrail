"""조회 전용 키인지 확인한다(실호출: 조회만 켠 키는 enableReading만 true)."""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from blackclip.binance.client import SHAPE_TEXT, BinanceError

ALLOWED_TRUE = frozenset({"enableReading", "ipRestrict", "enableFixReadOnly"})
"""true여도 되는 필드. 이 밖의 bool 필드가 하나라도 true면 거부한다(새 권한 필드가 생겨도 막는 쪽)."""


@dataclass(frozen=True)
class Permission:
    """권한 확인 결과.

    Attributes:
        status: "read_only"(조회만) · "rejected"(거부) · "unknown"(권한 확인 불가).
        detail: 화면에 쓸 한 줄.
        enabled: 거부 사유가 「조회 말고 켜진 권한」일 때 그 이름들(이름순). 그 밖에는 빈 튜플.
        reading_off: 거부 사유가 「조회 권한이 꺼짐」이면 True. 웹이 `detail` 글을 비교하지 않고 사유를 가르려고 싣는다.
            두 필드는 `detail`이 이미 말한 사실이라 `==` 비교에는 넣지 않는다.
    """

    status: str
    detail: str
    enabled: tuple[str, ...] = field(default=(), compare=False)
    reading_off: bool = field(default=False, compare=False)


def check(client: Any) -> Permission:
    """GET /sapi/v1/account/apiRestrictions로 키 권한을 본다.

    Args:
        client: 현물 Client(또는 testnet · get을 가진 같은 모양).

    Returns:
        Permission.

    Raises:
        BinanceError: 응답이 dict이 아니거나 enableReading이 bool이 아닐 때(`SHAPE_TEXT`, 조회 실패).
    """
    if client.testnet:
        return Permission("unknown", "테스트넷은 권한 조회 API가 없어 확인 불가")
    d = client.get("/sapi/v1/account/apiRestrictions")
    if not isinstance(d, dict) or type(d.get("enableReading")) is not bool:  # 모르는 모양을 「꺼짐」으로 단정하지 않는다
        raise BinanceError(0, None, SHAPE_TEXT)
    if d["enableReading"] is not True:
        return Permission("rejected", "조회 권한이 꺼져 있습니다", reading_off=True)
    extra = sorted(k for k, v in d.items() if v is True and k not in ALLOWED_TRUE)
    if extra:
        return Permission("rejected", f"조회 말고 켜진 권한: {', '.join(extra)}", enabled=tuple(extra))
    return Permission("read_only", "조회만 켜져 있음" + ("" if d.get("ipRestrict") else " · IP 제한 없음"))

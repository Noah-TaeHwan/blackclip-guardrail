"""고르는 화면의 엔진 글: 세 칸 1년 헛경보 확률.

옛 연결 서버(지금은 없음)로 보던 고르기 화면 시험은 대시보드 연결 화면 시험(`test_dashboard_connect.py`)과
CLI 시험(`test_connect.py`)으로 옮겼다.
"""
from blackclip.notify import sensitivity_text


def test_sensitivity_text_matches_d83():
    """세 칸: 1년 약 46 · 20 · 5%(한 달 5 · 1.84 · 0.43%), 둔감은 미검증."""
    assert [sensitivity_text(k) for k in ("민감", "보통", "둔감")] == [
        "민감 · 1년 내내 보면 헛경보 약 46%(한 달 약 5%)", "보통 · 1년 내내 보면 헛경보 약 20%(한 달 약 1.84%)",
        "둔감 · 1년 내내 보면 헛경보 약 5%(한 달 약 0.43%) · 미검증"]

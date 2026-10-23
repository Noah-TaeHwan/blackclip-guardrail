"""패키지 버전과 명령 진입점 확인."""
import blackclip
from blackclip.cli import main


def test_version_is_0_2_0():
    """키 없는 감시판 버전 번호가 SemVer · PEP 440 규칙대로인지 본다."""
    assert blackclip.__version__ == "0.2.0"


def test_cli_version_flag(capsys):
    """blackclip --version 이 버전만 찍고 0으로 끝나는지 본다."""
    assert main(["--version"]) == 0
    assert capsys.readouterr().out.strip() == "blackclip 0.2.0"

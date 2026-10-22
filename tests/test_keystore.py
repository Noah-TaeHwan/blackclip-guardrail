"""키 보관: 키체인은 값이 명령 인자에 안 실리고, 파일은 권한 600만 읽는다. 어디에도 값이 출력되지 않는다."""
import os
import stat

import pytest

from blackclip.keystore import Credentials, FileStore, Keychain, KeystoreError, account_name

C = Credentials("A" * 60 + "WXYZ", "S" * 64)


class FakeSecurity:
    """macOS security 명령 흉내. 받은 인자와 표준 입력을 모두 남긴다."""

    def __init__(self):
        self.items, self.argvs, self.stdins = {}, [], []

    def __call__(self, argv, stdin):
        self.argvs.append(argv)
        self.stdins.append(stdin)
        if argv[:2] == ["security", "-i"]:
            parts = stdin.split()
            self.items[parts[parts.index("-a") + 1]] = parts[parts.index("-w") + 1]
            return 0, ""
        acct = argv[argv.index("-a") + 1]
        if argv[1] == "find-generic-password":
            return (0, self.items[acct] + "\n") if acct in self.items else (44, "")
        if argv[1] == "delete-generic-password":
            return (0, "") if self.items.pop(acct, None) else (44, "")
        raise AssertionError(argv)


def test_keychain_roundtrip_and_secret_not_in_argv():
    """저장한 키를 그대로 읽고, 키 값은 명령 인자(ps에 보임)에 한 번도 실리지 않는다."""
    fake = FakeSecurity()
    kc = Keychain(fake)
    kc.save("binance:main", C)
    assert kc.load("binance:main") == C
    assert kc.load("binance:other") is None
    joined = [" ".join(a) for a in fake.argvs]
    assert all(C.secret_key not in a and C.api_key not in a for a in joined)
    kc.delete("binance:main")
    assert kc.load("binance:main") is None


def test_keychain_save_is_verified():
    """저장 뒤 다시 읽어 같지 않으면 저장 실패로 멈춘다(security -i는 실패해도 0으로 끝날 수 있다)."""
    fake = FakeSecurity()
    kc = Keychain(lambda argv, stdin: (0, "") if argv[1] == "-i" else fake(argv, stdin))
    with pytest.raises(KeystoreError, match="저장하지 못했습니다"):
        kc.save("binance:main", C)


def test_keychain_garbage_is_named():
    """다른 형식으로 들어 있는 항목은 다시 넣으라고 말하고 멈춘다."""
    kc = Keychain(lambda argv, stdin: (0, "not base64!!\n"))
    with pytest.raises(KeystoreError, match="형식"):
        kc.load("binance:main")


def test_keychain_other_error_has_code_only():
    """security가 다른 이유로 실패하면 종료 코드만 말한다."""
    kc = Keychain(lambda argv, stdin: (51, ""))
    with pytest.raises(KeystoreError, match="종료 코드 51"):
        kc.load("binance:main")


def test_repr_hides_values():
    """repr에 키 값이 나오지 않고 끝 4자리만 꺼낼 수 있다."""
    assert C.secret_key not in repr(C) and C.api_key not in repr(C)
    assert C.last4() == "WXYZ"


def test_file_store_mode_and_roundtrip(tmp_path):
    """파일 보관은 0600으로 쓰고 그대로 읽는다."""
    fs = FileStore(tmp_path / "cfg" / "secrets.json")
    fs.save("binance:main", C)
    assert stat.S_IMODE(fs.path.stat().st_mode) == 0o600
    assert fs.load("binance:main") == C and fs.load("x") is None
    fs.delete("binance:main")
    assert fs.load("binance:main") is None


def test_file_store_refuses_wide_permissions(tmp_path):
    """다른 사용자가 읽을 수 있는 권한이면 읽지 않고 고치는 법을 말한다."""
    fs = FileStore(tmp_path / "secrets.json")
    fs.save("a", C)
    os.chmod(fs.path, 0o644)
    with pytest.raises(KeystoreError, match="chmod 600"):
        fs.load("a")


def test_account_name():
    """보관 이름은 거래소:계정."""
    assert account_name("binance", "main-testnet") == "binance:main-testnet"


def test_keychain_timeout_one_line(monkeypatch):
    """키체인이 답하지 않으면(잠김 · SSH 접속) 파일로 넘기지 않고 한 줄."""
    import subprocess

    import pytest

    import blackclip.keystore as ks

    def slow(*a, **k):
        raise subprocess.TimeoutExpired(cmd="security", timeout=15)

    monkeypatch.setattr(ks.subprocess, "run", slow)
    with pytest.raises(ks.KeystoreError, match="15초"):
        ks._run(["security", "find-generic-password"], None)

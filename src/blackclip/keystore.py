"""거래소 키 보관: macOS 키체인, 안 되면 권한 600 파일. 키 값은 어디에도 출력하지 않는다."""
from __future__ import annotations

import base64
import json
import os
import shutil
import stat
import subprocess
import sys
import time
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from dataclasses import dataclass, field
from pathlib import Path

from blackclip.workspace import write_atomic

SERVICE = "blackclip-guardrail"
"""키체인 항목의 서비스 이름."""

NOT_FOUND = 44
"""security 명령이 항목을 못 찾았을 때의 종료 코드."""

Runner = Callable[[list[str], "str | None"], tuple[int, str]]
"""명령 실행기: (인자 목록, 표준 입력) → (종료 코드, 표준 출력)."""


class KeystoreError(RuntimeError):
    """키를 저장하거나 읽지 못했을 때. 메시지에 키 값을 넣지 않는다."""


class KeystoreBusy(KeystoreError):
    """보관 파일 잠금을 기다려도 다른 blackclip이 쥐고 있을 때(아직 쓰기 0)."""


LOCK_WAIT_S = 5.0
"""보관 파일 잠금을 기다리는 상한(초). 대시보드 저장은 서버 Lock · 연결 잠금을 쥔 채 기다리므로 끝없이 기다리지 않는다."""


@dataclass(frozen=True)
class Credentials:
    """거래소 키 한 쌍. repr에 값이 나오지 않는다.

    Attributes:
        api_key: API 키.
        secret_key: 비밀 키.
    """

    api_key: str = field(repr=False)
    secret_key: str = field(repr=False)

    def last4(self) -> str:
        """화면에 보일 끝 4자리.

        Returns:
            API 키의 마지막 4글자.
        """
        return self.api_key[-4:]


def _encode(c: Credentials) -> str:
    """키 한 쌍을 공백 · 따옴표 없는 한 줄로 바꾼다(키체인 대화형 명령에 넣으려고, 암호화 아님).

    Args:
        c: 키 한 쌍.

    Returns:
        base64 문자열.
    """
    return base64.b64encode(json.dumps({"api_key": c.api_key, "secret_key": c.secret_key}).encode()).decode()


def _decode(text: str) -> Credentials:
    """_encode의 반대.

    Args:
        text: 저장된 문자열.

    Returns:
        Credentials.
    """
    try:
        d = json.loads(base64.b64decode(text.strip(), validate=True))
        return Credentials(d["api_key"], d["secret_key"])
    except (ValueError, KeyError, TypeError):
        raise KeystoreError("저장된 키 형식이 다릅니다. blackclip connect --open으로 다시 넣으세요") from None


def _run(argv: list[str], stdin: str | None) -> tuple[int, str]:
    """명령을 실행한다.

    Args:
        argv: 인자 목록.
        stdin: 표준 입력(없으면 None).

    Returns:
        (종료 코드, 표준 출력).

    Raises:
        KeystoreError: 15초 안에 답하지 않을 때(잠긴 키체인).
    """
    try:
        p = subprocess.run(argv, input=stdin, capture_output=True, text=True, timeout=15)
    except subprocess.TimeoutExpired:  # 잠긴 키체인(SSH 접속 등)은 묻는 창 없이 멈춘다
        raise KeystoreError("키체인이 15초 안에 답하지 않았습니다 · 잠겨 있으면 security unlock-keychain 뒤 다시") from None
    return p.returncode, p.stdout


class Keychain:
    """macOS 키체인(security 명령). 값은 명령 인자가 아니라 표준 입력으로 넘긴다(ps에 안 보이게)."""

    name = "macOS 키체인"

    def __init__(self, runner: Runner = _run) -> None:
        """Args:
            runner: 명령 실행기(시험에서 바꿔 끼움).
        """
        self._run = runner

    def save(self, account: str, creds: Credentials) -> None:
        """키를 저장하고 다시 읽어 같은지 확인한다.

        Args:
            account: 보관 이름(binance:main).
            creds: 키 한 쌍.
        """
        self._run(["security", "-i"], f"add-generic-password -U -s {SERVICE} -a {account} -w {_encode(creds)}\n")
        if self.load(account) != creds:
            raise KeystoreError(f"키체인에 저장하지 못했습니다({account})")

    def load(self, account: str) -> Credentials | None:
        """키를 읽는다.

        Args:
            account: 보관 이름.

        Returns:
            Credentials, 없으면 None.
        """
        code, out = self._run(["security", "find-generic-password", "-s", SERVICE, "-a", account, "-w"], None)
        if code == NOT_FOUND:
            return None
        if code != 0:
            raise KeystoreError(f"키체인에서 읽지 못했습니다({account}, security 종료 코드 {code})")
        return _decode(out)

    def delete(self, account: str) -> None:
        """키를 지운다. 없으면 그냥 지나간다.

        Args:
            account: 보관 이름.
        """
        code, _ = self._run(["security", "delete-generic-password", "-s", SERVICE, "-a", account], None)
        if code not in (0, NOT_FOUND):
            raise KeystoreError(f"키체인에서 지우지 못했습니다({account}, security 종료 코드 {code})")


class FileStore:
    """권한 600 파일 보관(키체인이 없는 환경). 화면에 이 보관을 쓴다고 표시한다."""

    name = "권한 600 파일"

    def __init__(self, path: Path) -> None:
        """Args:
            path: 보관 파일 경로.
        """
        self.path = path

    def _read(self) -> dict[str, str]:
        """보관 파일을 읽는다. 다른 사용자가 읽을 수 있는 권한이면 멈춘다.

        Returns:
            보관 이름 → 인코딩한 값.
        """
        if not self.path.exists():
            return {}
        mode = stat.S_IMODE(self.path.stat().st_mode)
        if mode & 0o077:
            raise KeystoreError(f"{self.path}: 다른 사용자도 읽을 수 있는 권한입니다({mode:o}). chmod 600 {self.path} 뒤 다시 하세요")
        try:
            return json.loads(self.path.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            raise KeystoreError(f"{self.path}: 읽을 수 없습니다") from None

    def _write(self, data: dict[str, str]) -> None:
        """보관 파일을 같은 폴더의 임시 파일(실행마다 다른 이름, mkstemp라 0600 · O_EXCL)에 쓴 뒤 바꿔치기한다. 실패하면 임시 파일을 지운다.

        고정 `.tmp` 이름은 쓰지 않는다: 동시에 쓰는 두 프로세스가 같은 임시 파일을 덮거나, 그 자리에 심은 링크를 따라 쓰게 된다.

        Args:
            data: 보관 이름 → 인코딩한 값.
        """
        write_atomic(self.path, json.dumps(data))  # mkstemp(0600) → fsync → os.replace, 실패하면 임시 파일을 지움

    @contextmanager
    def _locked(self) -> Iterator[None]:
        """`<보관 파일>.lock`을 쥔다(다른 프로세스의 읽기 → 고치기 → 쓰기와 겹치지 않게, 잠금 순서의 마지막 칸). 폴더는 0700.

        읽기(`load`)는 잠그지 않는다: 쓰기는 `os.replace` 원자 교체라 반쯤 쓴 파일을 읽지 않는다.
        `LOCK_NB`로 0.1초마다 다시 해 `LOCK_WAIT_S`까지만 기다린다(멈춘 다른 프로세스가 대시보드 저장을 붙잡지 않게).

        Raises:
            KeystoreBusy: 상한까지 못 얻음.
        """
        import fcntl  # shortcut: 윈도에는 fcntl이 없다, 윈도 고객이 생기면 msvcrt.locking으로(workspace.lock과 같음)

        self.path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        fd = os.open(f"{self.path}.lock", os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW, 0o600)
        try:
            end = time.monotonic() + LOCK_WAIT_S
            while True:  # 이 잠금 안에서는 다른 잠금을 잡지 않아 교착이 없다
                try:
                    fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                    break
                except BlockingIOError:
                    if time.monotonic() >= end:
                        raise KeystoreBusy(f"다른 blackclip이 키 보관을 쓰고 있습니다({self.path.name}) · 잠시 뒤 다시") from None
                    time.sleep(0.1)
            yield
        finally:
            os.close(fd)

    def save(self, account: str, creds: Credentials) -> None:
        """키를 저장한다(보관 파일 잠금 안에서 읽기 → 고치기 → 쓰기).

        Args:
            account: 보관 이름.
            creds: 키 한 쌍.
        """
        with self._locked():
            data = self._read()
            data[account] = _encode(creds)
            self._write(data)

    def load(self, account: str) -> Credentials | None:
        """키를 읽는다.

        Args:
            account: 보관 이름.

        Returns:
            Credentials, 없으면 None.
        """
        value = self._read().get(account)
        return None if value is None else _decode(value)

    def delete(self, account: str) -> None:
        """키를 지운다.

        Args:
            account: 보관 이름.
        """
        with self._locked():
            data = self._read()
            if data.pop(account, None) is not None:
                self._write(data)


Store = Keychain | FileStore
"""키 보관 하나."""


def config_dir() -> Path:
    """사용자 설정 폴더(XDG_CONFIG_HOME, 없으면 ~/.config)의 blackclip 폴더.

    Returns:
        경로.
    """
    return Path(os.environ.get("XDG_CONFIG_HOME") or Path.home() / ".config") / "blackclip"


def default_store() -> Store:
    """이 컴퓨터에서 쓸 키 보관을 고른다.

    Returns:
        macOS면 Keychain, 아니면 FileStore.
    """
    if sys.platform == "darwin" and shutil.which("security"):
        return Keychain()
    # shortcut: 리눅스 Secret Service(keyring)는 안 쓴다, 화면 없는 서버가 흔해서. 고객이 원하면 그때 붙인다
    return FileStore(config_dir() / "secrets.json")


def account_name(exchange: str, key: str) -> str:
    """키 보관 이름.

    Args:
        exchange: 거래소(binance).
        key: workspace.account_key 결과.

    Returns:
        "binance:main".
    """
    return f"{exchange}:{key}"

"""고객 레포 안 작업 폴더 .blackclip/ 을 찾고 만들고, 그 안 경로와 연결 상태 파일을 다룬다."""
from __future__ import annotations

import errno
import json
import math
import os
import re
import stat
import tempfile
import time
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from datetime import date
from pathlib import Path, PurePosixPath
from typing import Any

DIRNAME = ".blackclip"
"""작업 폴더 이름."""

GITIGNORE = (
    "# blackclip이 만든 파일. 전략 기록과 연결 코드만 커밋하고 나머지(체결 · 연결 상태 · 이력)는 Git에서 뺀다.\n"
    "*\n"
    "!.gitignore\n"
    "!strategies/\n"
    "!strategies/*/\n"
    "!strategies/*/check.md\n"
    "!strategies/*/guardrail.v*.yaml\n"
    "!strategies/*/record.md\n"
    "!strategies/*/link.py\n"
)
"""작업 폴더 자기 .gitignore(check.md · guardrail · record.md, 연결 코드 link.py만 커밋)."""

_GITIGNORE_HEAD = "# blackclip이 만든 파일."
"""우리가 쓴 .gitignore 머리줄의 앞부분(옛 판을 알아보는 데 씀)."""

_LABEL = re.compile(r"^[a-z0-9][a-z0-9_]{0,31}$")
"""계정 이름 모양. 하이픈은 테스트넷 꼬리(-testnet)에만 쓴다."""


class WorkspaceError(ValueError):
    """작업 폴더 · 연결 상태 파일 · 계정 이름을 쓸 수 없을 때."""


class LockBusy(WorkspaceError):
    """같은 이름 잠금을 다른 blackclip이 잡고 있을 때(자주 보는 시계는 이번 확인을 건너뜀)."""


def _chain(start: Path) -> list[Path]:
    """start와 그 위 폴더들을 가까운 것부터 돌려준다.

    Args:
        start: 시작 폴더.

    Returns:
        폴더 목록.
    """
    s = start.resolve()
    return [s, *s.parents]


def find_root(start: Path) -> Path | None:
    """start에서 위로 올라가며 .blackclip 폴더를 찾는다. Git 레포 꼭대기를 넘지 않는다.

    Args:
        start: 시작 폴더.

    Returns:
        찾은 .blackclip 경로, 없으면 None.
    """
    for d in _chain(start):
        if (d / DIRNAME).is_dir():
            return d / DIRNAME
        if (d / ".git").exists():
            return None
    return None


def ensure(start: Path) -> Path:
    """작업 폴더를 찾고, 없으면 Git 레포 꼭대기(레포가 아니면 start)에 만든다.

    자기 .gitignore가 없으면 쓰고, 우리 머리줄로 시작하는 옛 판이면 새 판으로 바꾼다(고객이 머리줄을 지운 파일은 그대로).
    .blackclip이 심볼릭 링크이거나 .gitignore가 링크 · 일반 파일 아님이면 쓰지 않고 거절한다(레포가 심은 링크를 따라
    레포 밖에 쓰거나 FIFO에서 멈추지 않게, 훅과 같은 원칙).

    Args:
        start: 시작 폴더(보통 현재 폴더).

    Returns:
        .blackclip 경로.

    Raises:
        WorkspaceError: .blackclip이 링크이거나 폴더가 아님, .gitignore가 링크이거나 일반 파일이 아님.
    """
    from blackclip.hook import _read_regular  # hook이 workspace를 부르므로 여기서만

    top = next((d for d in _chain(start) if (d / ".git").exists()), start.resolve())
    root = find_root(start) or top / DIRNAME
    if root.is_symlink() or (root.exists() and not root.is_dir()):
        raise WorkspaceError(f"{DIRNAME}가 심볼릭 링크이거나 폴더가 아니라 쓰지 않습니다 · 레포 안의 진짜 폴더여야 합니다")
    root.mkdir(parents=True, exist_ok=True)
    gi = root / ".gitignore"
    old = None
    if gi.is_symlink() or gi.exists():
        raw = _read_regular(gi, 1 << 16)
        if raw is None:
            raise WorkspaceError(f"{DIRNAME}/.gitignore가 심볼릭 링크이거나 일반 파일이 아니라 쓰지 않습니다")
        old = raw.decode("utf-8", "replace")
    if old is None or (old != GITIGNORE and old.startswith(_GITIGNORE_HEAD)):
        gi.write_text(GITIGNORE, encoding="utf-8")
    return root


def account_key(label: str, testnet: bool) -> str:
    """계정 이름을 확인하고 보관 · 폴더에 쓸 열쇠 이름을 만든다.

    Args:
        label: 고객이 고른 계정 이름(서브 계정마다 하나, 기본 main).
        testnet: 테스트넷 키면 True.

    Returns:
        "main" 또는 "main-testnet".
    """
    if not _LABEL.fullmatch(label):
        raise WorkspaceError(f"--account: 영문 소문자 · 숫자 · 밑줄 32자까지입니다({label})")
    return f"{label}-testnet" if testnet else label


def fills_dir(root: Path, exchange: str, key: str, market: str) -> Path:
    """체결을 쌓을 폴더를 만들어 돌려준다. 시장마다 따로 둔다(현물 · 선물 섞임 방지).

    Args:
        root: .blackclip 경로.
        exchange: 거래소 이름(binance).
        key: account_key 결과.
        market: "spot" 또는 "futures".

    Returns:
        예: .blackclip/fills/binance-main/spot
    """
    d = root / "fills" / f"{exchange}-{key}" / market
    d.mkdir(parents=True, exist_ok=True)
    return d


def write_atomic(path: Path, text: str) -> None:
    """같은 폴더의 임시 파일(실행마다 다른 이름)에 쓴 뒤 바꿔치기한다. 실패하면 임시 파일을 지운다.

    Args:
        path: 쓸 파일.
        text: 내용(UTF-8).
    """
    fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=f".{path.name}.", suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write(text)
            f.flush()
            os.fsync(f.fileno())  # 바꿔치기 전에 디스크에(가드레일은 daily · watch가 읽는 계약 파일)
        os.replace(tmp, path)
    except BaseException:
        Path(tmp).unlink(missing_ok=True)
        raise


def open_or_create(name: str, flags: int, d: int) -> int:
    """폴더 FD 기준으로 파일을 연다. 없으면 `O_CREAT | O_EXCL`로 만들고, 그사이 다른 쪽이 먼저 만들었으면(EEXIST) 다시 연다(권한 0600).

    `O_CREAT`만 주고 dir_fd 기준으로 동시에 열면 macOS 27.0.1(26A434) 커널은 없는 파일에 ENOENT를 낸다(8스레드 재현 300번 가운데
    285번). 이미 있는 파일 열기와 `O_EXCL` 생성은 0번(2,000번): `O_EXCL` 생성은 원자적이라 한 쪽만 만들고 나머지는 EEXIST를 받는다.
    링크 자리는 첫 열기의 `O_NOFOLLOW`가 ELOOP를 낸다(가리키는 파일이 없어도). 그사이 생긴 링크는 `O_EXCL`이 EEXIST, 다시 열기가 ELOOP.

    Args:
        name: 폴더 안 파일 이름.
        flags: 열기 플래그(`O_CREAT` · `O_EXCL`은 넣지 않는다).
        d: 폴더 FD.

    Returns:
        파일 FD.

    Raises:
        OSError: 열지 못함(ELOOP는 링크, 그 밖 권한 · 디스크).
    """
    try:
        return os.open(name, flags, dir_fd=d)
    except FileNotFoundError:
        pass
    try:
        return os.open(name, flags | os.O_CREAT | os.O_EXCL, 0o600, dir_fd=d)
    except FileExistsError:
        return os.open(name, flags, dir_fd=d)


@contextmanager
def lock(root: Path, name: str, wait_s: int = 0, sleep: Callable[[float], None] = time.sleep) -> Iterator[None]:
    """같은 작업 폴더에서 같은 일이 동시에 두 번 돌지 않게 잠근다(.blackclip/<name>.lock, 프로세스가 끝나면 풀림).

    잠금 파일도 공용 안전 쓰기와 같은 규칙으로 연다: 작업 폴더 FD 기준 · O_NOFOLLOW · O_NONBLOCK · 일반 파일만 · 비우지 않음.

    Args:
        root: .blackclip 경로.
        name: 일 이름(fetch · daily · record).
        wait_s: 잡혀 있으면 1초마다 다시 해 볼 횟수(매일 대조가 자주 보는 시계를 기다림). 0이면 바로 멈춘다.
        sleep: 기다리기(시험).

    Raises:
        LockBusy: 기다려도 잡혀 있을 때.
        WorkspaceError: 작업 폴더가 링크 · 잠금 파일이 링크 · FIFO · 장치일 때(따라가지 않고 멈춤).
        OSError: 잠금 파일을 만들 수 없을 때(읽기 전용 폴더 등, 링크가 아닌 까닭).
    """
    import fcntl  # shortcut: 윈도에는 fcntl이 없다, 윈도 고객이 생기면 msvcrt.locking으로

    where = root / f"{name}.lock"
    d = _dir_fd(root, ())
    try:
        fd = open_or_create(f"{name}.lock", os.O_RDWR | os.O_NOFOLLOW | os.O_NONBLOCK, d)
    except OSError as exc:
        if exc.errno != errno.ELOOP:  # 권한 · 디스크 문제는 그 OSError 그대로(경로 없는 strerror로 보이게, 링크라고 잘못 말하지 않음)
            raise
        raise WorkspaceError(f"{where}: 링크 · FIFO · 장치라 잠그지 않습니다({exc.strerror})") from None
    finally:
        os.close(d)
    try:
        if not stat.S_ISREG(os.fstat(fd).st_mode):
            raise WorkspaceError(f"{where}: 일반 파일이 아니라 잠그지 않습니다")
        for left in range(max(wait_s, 0), -1, -1):
            try:
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                break
            except BlockingIOError:
                if not left:
                    raise LockBusy(f"다른 blackclip {name}가 이 작업 폴더에서 돌고 있습니다 · 끝난 뒤 다시") from None
                sleep(1)
        yield
    finally:
        os.close(fd)


def load_connections(root: Path) -> dict:
    """연결 상태(키 값이 아닌 것: 권한 확인 결과 · 받을 종목)를 읽는다.

    Args:
        root: .blackclip 경로.

    Returns:
        연결 상태 dict. 파일이 없으면 빈 dict.

    Raises:
        WorkspaceError: 링크 · 일반 파일 아님 · 1MiB 넘음이거나, UTF-8이 아니거나 JSON이 깨졌거나 최상위가 객체가 아닐 때(파일 이름 포함).
    """
    try:
        data = read_json(root, "connections.json")  # 링크 · 장치 · FIFO는 안 따르고 1MiB 넘으면 앞부분 대신 거절
    except FileNotFoundError:
        return {}
    if not isinstance(data, dict):
        raise WorkspaceError(f"{root / 'connections.json'}: 최상위가 객체({{…}})가 아닙니다")
    return data


def section(conns: dict, *path: str) -> dict:
    """connections.json 안의 칸(예: telegram · defaults · binance → 계정 → permission)을 객체로 꺼낸다. 없는 칸은 빈 객체를 달아 줌.

    손으로 고치거나 레포가 심은 `[]` · null · 숫자는 `.get` · 쓰기에서 traceback이 되므로 한 줄로 멈춘다.

    Args:
        conns: load_connections 결과.
        path: 칸 이름들(위에서 아래로).

    Returns:
        마지막 칸의 dict(conns 안의 것 그대로, 고치면 conns가 바뀜).

    Raises:
        WorkspaceError: 길 위의 어느 칸이든 객체가 아닐 때(그 칸 이름을 말함).
    """
    value = conns
    for i, key in enumerate(path):
        value = value.setdefault(key, {})
        if not isinstance(value, dict):
            raise WorkspaceError(f"connections.json의 {'.'.join(path[:i + 1])} 칸이 객체가 아니라 멈췄어요(그 칸을 지우거나 {{}}로 고친 뒤 다시)")
    return value


def save_connections(root: Path, data: dict) -> None:
    """연결 상태를 쓴다.

    Args:
        root: .blackclip 경로.
        data: 연결 상태 dict(키 값을 넣지 않는다).
    """
    write_atomic(root / "connections.json", json.dumps(data, ensure_ascii=False, indent=2, sort_keys=True) + "\n")


def apply_connections(root: Path, fn: Callable[[dict], None]) -> dict:
    """연결 상태를 읽고 fn으로 고쳐 쓴다(잠그지 않음: `connections` 잠금을 이미 쥔 쪽만 부른다).

    Args:
        root: .blackclip 경로.
        fn: 읽은 dict를 제자리에서 고치는 함수. 예외를 내면 쓰지 않는다.

    Returns:
        쓴 연결 상태 dict.

    Raises:
        WorkspaceError: load_connections · section이 내는 것(파일 손상 · 칸이 객체 아님).
        OSError: 쓰지 못했을 때.
    """
    conns = load_connections(root)
    fn(conns)
    save_connections(root, conns)
    return conns


def update_connections(root: Path, fn: Callable[[dict], None], wait_s: int = 5) -> dict:
    """연결 상태 쓰기 하나를 `connections` 잠금 안에서 한다(쓰는 곳 다섯이 모두 이 길).

    잠금 순서는 서버 Lock → 이 잠금 → 키 보관 파일 잠금이다. 잠금 안(fn 안)에서 이 함수를 다시 부르지 않는다:
    flock은 열린 파일 설명마다라 같은 프로세스도 자기 자신을 기다려 `LockBusy`가 된다.

    Args:
        root: .blackclip 경로.
        fn: 읽은 dict를 제자리에서 고치는 함수.
        wait_s: 잡혀 있으면 1초마다 다시 해 볼 횟수.

    Returns:
        쓴 연결 상태 dict.

    Raises:
        LockBusy: wait_s를 기다려도 다른 blackclip이 쥐고 있을 때.
        WorkspaceError · OSError: apply_connections와 같음.
    """
    with lock(root, "connections", wait_s):
        return apply_connections(root, fn)


def ignore_intact(root: Path) -> bool:
    """`ensure()`의 사후 조건이 그대로인지: `.gitignore`가 링크 아닌 일반 파일이고, 우리 머리줄로 시작하면 지금 판과 바이트가 같다.

    우리 머리줄로 시작하지 않는 파일은 고객이 쓴 것이라 `ensure()`처럼 그대로 둔다(True). 대시보드 서버는 `ensure()`를 부르지 않으므로
    쓰기 전에 이것으로 막는다(머리줄만 남기고 `*`를 지운 파일 · 옛 판이면 체결 · 연결 상태가 Git에 들어갈 수 있음).

    Args:
        root: .blackclip 경로.

    Returns:
        써도 되면 True. 없음 · 링크 · 일반 파일 아님 · 머리줄은 우리 것인데 내용이 다름은 False.
    """
    try:
        raw = read_bytes(root, ".gitignore", 1 << 16)
    except (OSError, WorkspaceError):
        return False
    text = raw.decode("utf-8", "replace")
    return text == GITIGNORE or not text.startswith(_GITIGNORE_HEAD)


# ── 레포 파일 안전 읽기 · 쓰기 ──────────────────────────────
JSON_CAP = 1 << 20
_DIR_FLAGS = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW
_NOT_REGULAR = "심볼릭 링크이거나 일반 파일이 아니라 읽지 않습니다"


def _parts(rel: str | Path) -> tuple[tuple[str, ...], str]:
    """작업 폴더 안 상대 경로를 (폴더 이름들, 파일 이름)으로 가른다.

    Args:
        rel: .blackclip 기준 상대 경로.

    Returns:
        (폴더 이름들, 파일 이름).

    Raises:
        WorkspaceError: 절대 경로 · `..` · 빈 경로.
    """
    p = PurePosixPath(str(rel))
    if p.is_absolute() or ".." in p.parts or not p.parts:
        raise WorkspaceError(f"{rel}: 작업 폴더 안 상대 경로가 아닙니다")
    return p.parts[:-1], p.parts[-1]


def _dir_fd(root: Path, parts: tuple[str, ...], make: bool = False) -> int:
    """root(.blackclip)부터 parts 폴더까지 링크 아닌 폴더만 하나씩 열어 마지막 폴더 FD를 준다.

    Args:
        root: .blackclip 경로(자기 자신도 링크면 거절).
        parts: 그 아래 폴더 이름들.
        make: 없는 폴더를 만들지(0o755, 링크는 만들지 않음).

    Returns:
        열린 폴더 FD(부르는 쪽이 닫음).

    Raises:
        FileNotFoundError: 폴더가 없고 make가 아님.
        WorkspaceError: 작업 폴더 · 중간 폴더가 링크이거나 폴더가 아님.
    """
    try:
        fd = os.open(root, _DIR_FLAGS)
    except OSError as exc:
        raise WorkspaceError(f"{root}: 작업 폴더가 링크이거나 폴더가 아닙니다({exc.strerror})") from None
    for i, part in enumerate(parts):
        where = root.joinpath(*parts[: i + 1])
        try:
            if make:
                try:
                    os.mkdir(part, 0o755, dir_fd=fd)
                except FileExistsError:
                    pass
            nxt = os.open(part, _DIR_FLAGS, dir_fd=fd)
        except FileNotFoundError:
            os.close(fd)
            raise FileNotFoundError(str(where)) from None
        except OSError:
            os.close(fd)
            raise WorkspaceError(f"{where}: 상위 폴더가 링크이거나 폴더가 아니라 읽지도 쓰지도 않습니다") from None
        os.close(fd)
        fd = nxt
    return fd


def read_bytes(root: Path, rel: str | Path, cap: int) -> bytes:
    """작업 폴더 안 일반 파일을 cap 바이트까지 읽는다(넘으면 통째 거절, cap + 1 읽기).

    Args:
        root: .blackclip 경로.
        rel: 상대 경로.
        cap: 상한(바이트).

    Returns:
        내용.

    Raises:
        FileNotFoundError: 없음.
        WorkspaceError: 링크 · FIFO · 장치 · 못 읽음 · 상한 넘음.
    """
    parts, name = _parts(rel)
    d = _dir_fd(root, parts)
    try:
        try:
            fd = os.open(name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=d)
        except FileNotFoundError:
            raise FileNotFoundError(str(root / rel)) from None
        except OSError:
            raise WorkspaceError(f"{root / rel}: {_NOT_REGULAR}") from None
        try:
            if not stat.S_ISREG(os.fstat(fd).st_mode):
                raise WorkspaceError(f"{root / rel}: {_NOT_REGULAR}")
            data = b""
            while len(data) <= cap and (more := os.read(fd, cap + 1 - len(data))):
                data += more
        finally:
            os.close(fd)
    finally:
        os.close(d)
    if len(data) > cap:
        raise WorkspaceError(f"{root / rel}: {cap:,}바이트를 넘어 읽지 않습니다")
    return data


def _loads(where: Path, text: str) -> Any:
    """JSON 한 덩이를 읽는다(오류 문구는 load_connections와 같음).

    Args:
        where: 오류에 쓸 경로.
        text: 글.

    Returns:
        값.

    Raises:
        WorkspaceError: JSON 아님 · 숫자가 너무 김 · 중첩이 너무 깊음.
    """
    try:
        return json.loads(text)
    except json.JSONDecodeError as exc:
        raise WorkspaceError(f"{where}: JSON을 읽을 수 없습니다({exc.msg})") from None
    except ValueError:  # 4,300자리 넘는 정수(int 자릿수 상한)
        raise WorkspaceError(f"{where}: JSON을 읽을 수 없습니다(숫자가 너무 김)") from None
    except RecursionError:
        raise WorkspaceError(f"{where}: JSON을 읽을 수 없습니다(중첩이 너무 깊음)") from None


def read_json(root: Path, rel: str | Path, cap: int = JSON_CAP) -> Any:
    """작업 폴더 안 JSON 파일 하나를 안전하게 읽는다.

    Args:
        root: .blackclip 경로.
        rel: 상대 경로.
        cap: 상한(바이트).

    Returns:
        JSON 값(모양 검사는 부르는 쪽).

    Raises:
        FileNotFoundError: 없음.
        WorkspaceError: read_bytes · UTF-8 · JSON 문제.
    """
    raw = read_bytes(root, rel, cap)
    try:
        text = raw.decode("utf-8")
    except UnicodeDecodeError:
        raise WorkspaceError(f"{root / rel}: UTF-8이 아닙니다") from None
    return _loads(root / rel, text)


def read_jsonl(root: Path, rel: str | Path, cap: int) -> list[Any]:
    """JSON 줄 파일을 읽는다. 없으면 빈 목록, 깨진 줄(잘린 끝 줄 · 깊은 JSON · 긴 정수)은 그 줄만 건너뜀.

    Args:
        root: .blackclip 경로.
        rel: 상대 경로.
        cap: 상한(바이트).

    Returns:
        줄마다 값.

    Raises:
        WorkspaceError: 파일 자체 문제(링크 · FIFO · 상한 · 못 읽음).
    """
    try:
        raw = read_bytes(root, rel, cap)
    except FileNotFoundError:
        return []
    out = []
    for line in raw.decode("utf-8", "replace").splitlines():
        try:
            out.append(json.loads(line))
        except (ValueError, RecursionError):
            continue
    return out


def append_line(root: Path, rel: str | Path, text: str, make: bool = False) -> None:
    """작업 폴더 안 일반 파일 끝에 한 번에 덧붙인다(O_APPEND · O_NOFOLLOW · O_NONBLOCK · fstat).

    Args:
        root: .blackclip 경로.
        rel: 상대 경로.
        text: 덧붙일 글(줄 끝 포함).
        make: 없는 폴더를 만들지.

    Raises:
        WorkspaceError: 상위 폴더 링크 · 링크 · FIFO · 장치.
        OSError: 다 쓰지 못함 · 디스크 문제.
    """
    parts, name = _parts(rel)
    d = _dir_fd(root, parts, make)
    try:
        try:
            fd = open_or_create(name, os.O_WRONLY | os.O_APPEND | os.O_NOFOLLOW | os.O_NONBLOCK, d)
        except OSError as exc:
            raise WorkspaceError(f"{root / rel}: 링크 · FIFO · 장치라 덧붙이지 않습니다({exc.strerror})") from None
        try:
            if not stat.S_ISREG(os.fstat(fd).st_mode):
                raise WorkspaceError(f"{root / rel}: 일반 파일이 아니라 덧붙이지 않습니다")
            data = text.encode("utf-8")
            if os.write(fd, data) != len(data):
                raise OSError(f"{root / rel}: 다 쓰지 못함")
        finally:
            os.close(fd)
    finally:
        os.close(d)


def write_in(root: Path, rel: str | Path, text: str, make: bool = False) -> None:
    """작업 폴더 안에 임시 파일(0600)을 쓰고 fsync한 뒤 같은 폴더 FD 기준으로 바꿔치기한다(링크 자리는 링크 자체를 바꿈).

    Args:
        root: .blackclip 경로.
        rel: 상대 경로.
        text: 내용(UTF-8).
        make: 없는 폴더를 만들지.

    Raises:
        WorkspaceError: 상위 폴더 링크.
        OSError: 디스크 문제(임시 파일은 지움).
    """
    parts, name = _parts(rel)
    d = _dir_fd(root, parts, make)
    tmp = f".{name}.{os.getpid()}.{os.urandom(4).hex()}.tmp"
    try:
        fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600, dir_fd=d)
        try:
            data = text.encode("utf-8")
            if os.write(fd, data) != len(data):
                raise OSError(f"{root / rel}: 다 쓰지 못함")
            os.fsync(fd)  # 바꿔치기 전에 디스크에(write_atomic과 같음)
        finally:
            os.close(fd)
        os.replace(tmp, name, src_dir_fd=d, dst_dir_fd=d)
    except BaseException:
        try:
            os.unlink(tmp, dir_fd=d)
        except OSError:
            pass
        raise
    finally:
        os.close(d)


def exists_regular(root: Path, rel: str | Path) -> bool:
    """작업 폴더 안 rel이 링크 아닌 일반 파일인지(상위 폴더 링크 · 없음 · 오류는 False, 읽지 않음).

    Args:
        root: .blackclip 경로.
        rel: 상대 경로.

    Returns:
        일반 파일이면 True.
    """
    try:
        parts, name = _parts(rel)
        d = _dir_fd(root, parts)
    except (OSError, WorkspaceError):
        return False
    try:
        return stat.S_ISREG(os.stat(name, dir_fd=d, follow_symlinks=False).st_mode)
    except OSError:
        return False
    finally:
        os.close(d)


GUARD_FILE = re.compile(r"guardrail\.v(\d+)\.yaml")
"""가드레일 파일 이름(.gitignore가 커밋 대상으로 둠)."""

_ISO_DAY = re.compile(r"[0-9]{4}-[0-9]{2}-[0-9]{2}")
_HEX64 = re.compile(r"[0-9a-f]{64}")
_MISSING = object()


def strategy_names(root: Path) -> list[str]:
    """strategies/ 아래 이름(폴더 · 파일 · 링크 가리지 않고)을 이름순으로 돌려준다. 링크를 따라가지 않는다.

    Args:
        root: .blackclip 경로.

    Returns:
        이름 목록. strategies/가 없으면 빈 목록.

    Raises:
        WorkspaceError: strategies/가 링크이거나 폴더가 아님.
    """
    try:
        fd = _dir_fd(root, ("strategies",))
    except FileNotFoundError:
        return []
    try:
        with os.scandir(fd) as it:
            return sorted(e.name for e in it)
    finally:
        os.close(fd)


def versions(root: Path, name: str) -> list[int]:
    """전략 폴더에 있는 가드레일 판 번호. 폴더가 없거나 링크이면 빈 목록, 링크인 판 파일은 세지 않는다.

    Args:
        root: .blackclip 경로.
        name: 전략 이름.

    Returns:
        오름차순 번호.
    """
    try:
        fd = _dir_fd(root, ("strategies", name))
    except (FileNotFoundError, WorkspaceError):
        return []
    try:
        with os.scandir(fd) as it:
            return sorted(int(m[1]) for e in it if (m := GUARD_FILE.fullmatch(e.name)) and e.is_file(follow_symlinks=False))
    finally:
        os.close(fd)


def _num(x: Any) -> bool:
    """bool이 아닌 유한한 수인지(int는 절댓값 1e6 이하만 봐서 `math.isfinite(10**400)`의 OverflowError를 피함).

    Args:
        x: 값.

    Returns:
        그렇다면 True.
    """
    if isinstance(x, bool):
        return False
    if isinstance(x, int):
        return abs(x) <= 1e6
    return isinstance(x, float) and math.isfinite(x)


def _int(x: Any) -> bool:
    """bool이 아닌 정수인지.

    Args:
        x: 값.

    Returns:
        그렇다면 True.
    """
    return isinstance(x, int) and not isinstance(x, bool)


def _day(x: Any) -> date | None:
    """YYYY-MM-DD 글을 날짜로(다른 모양은 None).

    Args:
        x: 값.

    Returns:
        날짜, 아니면 None.
    """
    try:
        return date.fromisoformat(x) if isinstance(x, str) and _ISO_DAY.fullmatch(x) else None
    except ValueError:
        return None


def _nums(x: Any, size: int | None = None) -> bool:
    """숫자 목록인지(size를 주면 그 개수, 안 주면 1개 이상).

    Args:
        x: 값.
        size: 필요한 개수.

    Returns:
        그렇다면 True.
    """
    return isinstance(x, list) and (len(x) == size if size else bool(x)) and all(_num(v) for v in x)


def guardrail_problem(doc: Any) -> str | None:
    """가드레일 안쪽 모양을 소비자(daily · watch · status · chart · live · reconcile)가 읽는 칸만 검사한다.

    Args:
        doc: 가드레일 파일에서 읽은 값.

    Returns:
        처음 틀린 칸 이름(예: `target.symbols`), 모두 맞으면 None. 객체가 아니면 `target`.
    """
    if not isinstance(doc, dict):
        return "target"

    def at(path: str) -> Any:
        cur: Any = doc
        for key in path.split("."):
            if not isinstance(cur, dict) or key not in cur:
                return _MISSING
            cur = cur[key]
        return cur

    drawn, live = at("normal_range.drawn") is True, _day(at("live_start"))
    checks: list[tuple[str, Callable[[Any], bool]]] = [
        ("target", lambda v: isinstance(v, dict)),
        ("target.market", lambda v: v in ("spot", "futures")),
        ("target.symbols", lambda v: isinstance(v, list) and bool(v) and all(isinstance(s, str) for s in v)),
        ("target.bar_seconds", lambda v: _int(v) and v > 0),
        ("live_start", lambda v: live is not None),
        ("normal_range.drawn", lambda v: isinstance(v, bool)),
        ("normal_range.low", lambda v: not drawn or _num(v)),
        ("normal_range.high", lambda v: not drawn or _num(v)),
        ("normal_range.daily_low", lambda v: not drawn or _nums(v, 30)),
        ("normal_range.daily_high", lambda v: not drawn or _nums(v, 30)),
        ("warning_line.line", _num),
        ("warning_line.sensitivity", lambda v: isinstance(v, str)),
        ("judgment_hold.min_trades", lambda v: _int(v) and v >= 1),  # 0 · 음수면 chart E의 30건 선 자리가 없음
        ("trade_frequency.low", lambda v: v is None or _num(v)),
        ("trade_frequency.high", lambda v: v is None or _num(v)),
        ("response", lambda v: v is _MISSING or (isinstance(v, dict) and all(isinstance(k, str) and isinstance(x, str)
                                                                              for k, x in v.items()))),
        # 소비자(reconcile.strategy_cause · thesis_cause · line_cause, daily._one, watch.line · position)가 읽는 나머지 칸
        ("judgment_hold.hold_if_edge0_at_least", _num),
        ("judgment_hold.edge0_in_range", lambda v: v is None or _nums(v)),
        ("thesis.rule", lambda v: isinstance(v, str)),
        ("target.exec", lambda v: v in ("next_open", "close")),
        ("target.start", lambda v: (d := _day(v)) is not None and d <= live),
        ("target.link_sha256", lambda v: isinstance(v, str) and _HEX64.fullmatch(v) is not None),
        ("period.mean", _num),
        ("period.std", _num),
        ("version", lambda v: _int(v) and v >= 1),
        ("warning_line.lines", lambda v: isinstance(v, dict) and all(isinstance(k, str) and _num(x) for k, x in v.items())),
        # 선택 칸. daily가 int(g.get("match_window_bars", 1))로 읽고 save는 늘 1을 씀(없으면 소비자 기본 1)
        ("match_window_bars", lambda v: v is _MISSING or (_int(v) and v >= 1)),
        # chart E가 읽는 선택 칸(예전 가드레일에는 분위가 없음 — 없으면 통과, 있으면 숫자 또는 None)
        ("basis", lambda v: v is _MISSING or isinstance(v, dict)),
        *((f"basis.{k}", lambda v: v is _MISSING or v is None or _num(v)) for k in ("trade_mean", "trade_p10", "trade_p90")),
    ]
    return next((path for path, ok in checks if not ok(at(path))), None)


def read_guardrail(root: Path, name: str) -> tuple[int, dict] | None:
    """전략의 최신 가드레일 한 판을 안전하게 읽고 안쪽 모양까지 확인한다.

    Args:
        root: .blackclip 경로.
        name: 전략 이름.

    Returns:
        (판 번호, 내용). 판이 없거나 전략 폴더가 링크이면 None.

    Raises:
        WorkspaceError: 읽기 문제(링크 · FIFO · 상한 · JSON) 또는 `가드레일 모양이 다릅니다(<칸>)`.
    """
    have = versions(root, name)
    if not have:
        return None
    rel = f"strategies/{name}/guardrail.v{have[-1]}.yaml"
    doc = read_json(root, rel)
    if (why := guardrail_problem(doc)) is not None:
        raise WorkspaceError(f"{root / rel}: 가드레일 모양이 다릅니다({why})")
    return have[-1], doc

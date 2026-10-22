"""git 커밋에서 지난 시도 되살리기(최솟값: git 커밋만).

고객 레포를 읽기만 한다: `git log`를 고정 PATH(`hook.TRUSTED_PATH`)의 git으로, 레포 설정이 프로그램을 띄울 수 있는 값을
모두 무력화해 돌린다(공식 git(1) SECURITY 「it is not safe to run Git commands in a .git directory … when that .git
directory itself comes from an untrusted source」). 후보 파일마다 공식 `--follow`(「works only for a single file」)로
지금 HEAD 줄기의 이름 바뀜을 잇고(가지끼리 이름표를 섞지 않음), 만든 커밋(A)에서 멈춘다(같은 이름을 새로 만든 옛 파일은 안 셈).
다른 로컬 가지는 그 만든 커밋에서 이어진 커밋 중 지금 이름을 바꾼 것만 더한다(`--ancestry-path`).
결과는 map.json · setup 화면에만 쓰고 시도 수 점검 · 가드레일 파일에는 넣지 않는다.
"""
from __future__ import annotations

import os
import re
import subprocess
import threading
import time
from pathlib import Path

from blackclip.hook import TRUSTED_PATH, _git  # 고정 PATH에서만 찾는 git(같은 원칙을 한 곳에서)

TIMEOUT_S = 10.0
"""한 번의 훑기에서 git에 쓰는 시간 전체(초). 넘으면 거기까지만 세고 「시간 초과」(불완전)."""

MAX_FILES = 500
"""`--follow`로 세는 후보 파일 수 상한(파일마다 git 한두 번). 넘으면 앞쪽만 세고 불완전."""

MAX_OUT = 4 << 20
"""git 한 번의 출력 바이트 상한(4MiB, 커밋 약 10만 개). 넘으면 멈추고 「출력 상한」(불완전)."""

SAFE = ("--no-pager", "--no-replace-objects", "--no-lazy-fetch", "--no-optional-locks", "--literal-pathspecs",
        "-c", "core.fsmonitor=false", "-c", "log.showSignature=false", "-c", "log.showRoot=true", "-c", "log.mailmap=false")
"""git 앞 옵션: 레포 설정(local scope, 보호되지 않음)이 띄우는 프로그램 · 바꾸는 결과를 막는다.
`log.mailmap=false`는 레포가 `mailmap.file`을 FIFO · 장치로 두어 git이 열다 막히는 것을 막는다.
`--literal-pathspecs`(git(1))는 `s[1].py` · `s?.py` 같은 파일 이름을 glob · pathspec magic으로 읽지 않게."""

_TAIL = ("--full-history", "--no-merges", "--no-ext-diff", "--no-textconv", "--no-color")
"""두 명령 공통: 합친 커밋 뺌 · 외부 diff · textconv 끔."""

FOLLOW = ("log", "--follow", "--name-status", "-z", "--format=%x00%H", *_TAIL, "HEAD", "--")
"""HEAD 줄기에서 파일 하나의 이름 바뀜을 잇는 명령(뒤에 경로 하나)."""

OTHERS = ("log", "--ancestry-path", "--branches", "--format=%H", *_TAIL)
"""만든 커밋에서 이어진 로컬 가지 커밋(뒤에 `^<만든 커밋> -- 경로`)."""

_HASH = re.compile(r"[0-9a-f]{40}(?:[0-9a-f]{24})?")
"""커밋 해시(SHA-1 40자 · SHA-256 64자)."""

_STATUS = re.compile(r"[ACDMRTUXB][0-9]*")
"""`--name-status` 상태 글자(R · C는 뒤에 비슷한 정도 숫자, 경로 둘)."""


def env(top: Path | None = None) -> dict[str, str]:
    """git에 줄 환경: 물려받은 GIT_* 를 모두 빼고 전역 · 시스템 설정을 읽지 않게 하고, PATH는 고정 값.

    Args:
        top: 레포 꼭대기. 주면 git이 그 위 폴더의 레포로 올라가지 못하게 한다(GIT_CEILING_DIRECTORIES, git(1) —
            `.git`이 비었거나 깨졌을 때 부모 레포를 세지 않게).

    Returns:
        환경 변수 dict(GIT_EXTERNAL_DIFF · GIT_DIR · GIT_CONFIG_* 같은 물려받은 값은 없음).
    """
    base = {k: v for k, v in os.environ.items() if not k.startswith("GIT_")}
    out = {**base, "PATH": TRUSTED_PATH, "GIT_CONFIG_GLOBAL": "/dev/null", "GIT_CONFIG_NOSYSTEM": "1", "GIT_PAGER": "cat",
           "GIT_TERMINAL_PROMPT": "0", "GIT_OPTIONAL_LOCKS": "0", "LC_ALL": "C"}
    if top is not None:
        out["GIT_CEILING_DIRECTORIES"] = str(top.resolve().parent)
    return out


def parse(out: str) -> tuple[set[str], str | None]:
    """`git log --follow -z --name-status --format=%x00%H` 출력에서 커밋을 모으고 만든 커밋(A)에서 멈춘다.

    Args:
        out: git 출력(NUL로 나뉨, 새 커밋부터).

    Returns:
        (커밋 해시 집합, 만든 커밋 해시 또는 None — 보이는 기록 안에 A가 없으면 None).
    """
    commits: set[str] = set()
    cur = None
    toks, i = out.split("\0"), 0
    while i < len(toks):
        t = toks[i].lstrip("\n")
        if _HASH.fullmatch(t):
            cur = t
            commits.add(t)
        elif cur and _STATUS.fullmatch(t):
            if t == "A":
                return commits, cur
            i += 2 if t[0] in "RC" else 1  # 경로는 레포가 정하는 글자라 해시 · 상태로 읽지 않고 건너뜀
        i += 1
    return commits, None


def _run(git: str, top: Path, args: list[str], deadline: float) -> bytes | str:
    """git 한 번(읽기만, 출력 바이트 · 시간 상한).

    Args:
        git: git 절대 경로.
        top: 레포 꼭대기.
        args: SAFE 뒤 인자.
        deadline: time.monotonic() 기준 마감.

    Returns:
        stdout 바이트, 못 했으면 이유(「시간 초과」 · 「출력 상한」 · 「git 없음」 · 「git 실패(종료 N)」).
    """
    left = deadline - time.monotonic()
    if left <= 0:
        return "시간 초과"
    try:
        proc = subprocess.Popen([git, "-C", str(top), *SAFE, *args], cwd="/", env=env(top), stdin=subprocess.DEVNULL,
                                stdout=subprocess.PIPE, stderr=subprocess.DEVNULL)
    except OSError:
        return "git 없음"
    timer = threading.Timer(left, proc.kill)
    timer.start()
    try:
        data = proc.stdout.read(MAX_OUT + 1)
        if len(data) > MAX_OUT:
            proc.kill()
        code = proc.wait()
    finally:
        timer.cancel()
        proc.stdout.close()
    if len(data) > MAX_OUT:
        return "출력 상한"
    if code < 0:  # 마감 타이머가 죽임
        return "시간 초과"
    return data if code == 0 else f"git 실패(종료 {code})"


def history(top: Path, paths: list[str], timeout: float = TIMEOUT_S) -> tuple[dict[str, set[str]], str]:
    """후보 파일마다 그 파일을 바꾼 커밋을 모은다(읽기만, 최솟값).

    Args:
        top: 레포 꼭대기(.git이 있는 폴더).
        paths: 레포 꼭대기 기준 경로(scan 후보 · 연결 파일).
        timeout: 시간 제한 전체(초).

    Returns:
        (지금 경로 → 커밋 해시 집합, 상태). 상태는 「ok」, 아니면 이유 — 이때 dict에는 거기까지 센 파일만 있다(불완전).
    """
    if not (top / ".git").exists():
        return {}, "git 레포 아님"
    git = _git()
    if git is None:
        return {}, "git 없음"
    deadline, out = time.monotonic() + timeout, {}
    for i, p in enumerate(paths):
        if i >= MAX_FILES:
            return out, f"파일 수 상한({MAX_FILES}개까지만 셈)"
        got = _run(git, top, [*FOLLOW, p], deadline)
        if isinstance(got, str):
            return out, got
        commits, created = parse(got.decode("ascii", "replace"))
        if created:
            more = _run(git, top, [*OTHERS, f"^{created}", "--", p], deadline)
            if isinstance(more, str):
                return out, more
            commits |= set(_HASH.findall(more.decode("ascii", "replace")))
        out[p] = commits
    return out, "ok"


def count(touched: dict[str, set[str]], paths: list[str]) -> int:
    """경로들을 바꾼 서로 다른 커밋 수.

    Args:
        touched: history 결과 dict.
        paths: 레포 꼭대기 기준 경로.

    Returns:
        커밋 수(합집합).
    """
    return len(set().union(*(touched.get(p, set()) for p in paths)))

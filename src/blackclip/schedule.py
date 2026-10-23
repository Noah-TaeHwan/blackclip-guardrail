"""blackclip schedule: 매일 대조와 자주 보는 시계를 launchd 사용자 에이전트 둘로 건다(macOS). 다른 운영체제는 crontab 줄만 보인다.

근거: launchd.plist(5) 「Unlike cron which skips job invocations when the computer is asleep, launchd will start the job
the next time the computer wakes up. If multiple intervals transpire before the computer is woken, those events will be
coalesced into one event upon wake from sleep」. 그래서 둘 다 StartCalendarInterval이다(자주 보는 시계는 매시 M분):
잠든 동안은 돌지 않고 깨어나면 한 번 돈다. 로그는 .blackclip/daily.log · .blackclip/watch.log(Git 제외).
"""
from __future__ import annotations

import hashlib
import os
import plistlib
import shutil
import stat
import subprocess
import sys
from collections.abc import Callable
from pathlib import Path

Run = Callable[[list[str]], int]
"""명령 실행기: 인자 목록 → 종료 코드(시험에서 가짜)."""


NOT_LOADED = 3
"""launchctl bootout이 「없는 예약」에 내는 종료 코드(이 Mac 실측: 「Boot-out failed: 3: No such process」)."""

KINDS = (("daily", "매일 대조"), ("watch", "자주 보는 시계"))
"""(예약 종류, 화면 이름). 표지 · 지우기 순서."""


def label(repo: Path, kind: str = "daily") -> str:
    """레포마다 다른 launchd 표지.

    Args:
        repo: 레포 꼭대기(.blackclip의 부모).
        kind: "daily" · "watch" · "follow".

    Returns:
        "com.blackclip.<kind>.<sha256 앞 10자>".
    """
    return f"com.blackclip.{kind}." + hashlib.sha256(str(repo.resolve()).encode()).hexdigest()[:10]


def executable() -> str:
    """blackclip 실행 파일 절대 경로(가상환경 bin, 없으면 PATH).

    Returns:
        경로.

    Raises:
        FileNotFoundError: 못 찾을 때.
    """
    here = Path(sys.executable).parent / "blackclip"
    found = str(here) if here.exists() else shutil.which("blackclip")
    if not found:
        raise FileNotFoundError("blackclip 실행 파일을 찾지 못했습니다 · 엔진을 설치한 가상환경에서 다시")
    return found


ENGINE = "engine"
"""엔진 위치 파일 이름(사용자 설정 폴더 안). 훅 래퍼가 PATH 다음에 읽는다."""


def _mine(path: Path, folder: bool) -> bool:
    """링크가 아니고, 내 것이고, 남이 못 쓰는 자리인지(따르지 않고 봄).

    Args:
        path: 경로.
        folder: 폴더여야 하면 True(그룹 · 남 쓰기 없음), 파일이면 False(0600 — 그룹 · 남 권한 없음).

    Returns:
        그렇다면 True.
    """
    try:
        st = os.lstat(path)
    except OSError:
        return False
    kind = stat.S_ISDIR(st.st_mode) if folder else stat.S_ISREG(st.st_mode)
    return kind and st.st_uid == os.getuid() and not st.st_mode & (0o022 if folder else 0o077)


def remember_engine() -> Path | None:
    """지금 실행 중인 엔진 진입점(sys.argv[0])의 실제 경로를 사용자 설정 폴더(keystore.config_dir())의 engine 파일에 적는다.

    레포 가상환경에만 깐 고객도 훅이 엔진을 찾게 한다(레포 .venv의 blackclip을 실행하지 않는 대신). 옆 · PATH에서 다시 찾지 않는다(다른 설치본을
    적을 수 있어서). sys.argv[0]이 절대 경로이고, 해석한 실제 경로의 이름이 blackclip인 실행 가능한 일반 파일일 때만 적는다.
    설정 폴더는 링크가 아니고 내 것이며 그룹 · 남이 못 쓸 때만 쓴다(없으면 0700으로 만듦). engine 파일은 내용이 같고 내 것 ·
    0600 · 링크 아님일 때만 그대로 두고, 아니면 다시 쓴다(write_atomic, 0600). 무엇이 실패해도 예외를 내지 않는다.

    Returns:
        engine 파일 경로(이미 같은 값이면 쓰지 않고 그 경로), 적지 못했으면 None.
    """
    from blackclip.keystore import config_dir
    from blackclip.workspace import write_atomic

    try:
        arg = sys.argv[0] if sys.argv else ""
        exe = os.path.realpath(arg)
        if not (os.path.isabs(arg) and os.path.basename(exe) == "blackclip" and os.path.isfile(exe)
                and os.access(exe, os.X_OK)):
            return None
        d = config_dir()
        if not d.is_absolute():
            return None
        if not _mine(d, folder=True):
            if d.exists() or d.is_symlink():
                return None
            d.mkdir(mode=0o700, parents=True)
        path, text = d / ENGINE, exe + "\n"
        if _mine(path, folder=False) and path.read_text(encoding="utf-8") == text:
            return path
        write_atomic(path, text)  # 임시 파일(0600, 내 것) → 바꿔치기: 링크였어도 링크 자체를 바꿈
        return path
    except (OSError, ValueError, RuntimeError):  # RuntimeError: HOME도 passwd 항목도 없을 때 Path.home()
        return None


def plist(repo: Path, exe: str, hour: int, minute: int) -> bytes:
    """LaunchAgent plist 내용.

    Args:
        repo: 레포 꼭대기.
        exe: blackclip 절대 경로.
        hour: 시(로컬).
        minute: 분.

    Returns:
        XML plist 바이트.
    """
    log = str(repo / ".blackclip" / "daily.log")
    return plistlib.dumps({"Label": label(repo), "ProgramArguments": [exe, "daily"], "WorkingDirectory": str(repo),
                           "StartCalendarInterval": {"Hour": hour, "Minute": minute},
                           "StandardOutPath": log, "StandardErrorPath": log})


def watch_minute(minute: int) -> int:
    """자주 보는 시계가 도는 매시 분 = 매일 대조 분 + 30(60으로 나눈 나머지). 매일 대조와 가장 멀리 떨어진 분.

    Args:
        minute: 매일 대조 분.

    Returns:
        0~59.
    """
    return (minute + 30) % 60


def watch_plist(repo: Path, exe: str, minute: int) -> bytes:
    """자주 보는 시계 LaunchAgent plist 내용(매시 minute분).

    Args:
        repo: 레포 꼭대기.
        exe: blackclip 절대 경로.
        minute: 매시 몇 분(watch_minute 결과).

    Returns:
        XML plist 바이트.
    """
    log = str(repo / ".blackclip" / "watch.log")
    return plistlib.dumps({"Label": label(repo, "watch"), "ProgramArguments": [exe, "watch"], "WorkingDirectory": str(repo),
                           "StartCalendarInterval": {"Minute": minute}, "StandardOutPath": log, "StandardErrorPath": log})


FOLLOW_S = 300
"""키 없이 지켜보는 시계 간격(초). StartInterval도 잠든 동안 놓친 차례를 깨어날 때 한 번으로 합친다(launchd.plist(5))."""


def follow_plist(repo: Path, exe: str) -> bytes:
    """키 없이 지켜보는 시계 LaunchAgent plist 내용(5분마다).

    Args:
        repo: 레포 꼭대기.
        exe: blackclip 절대 경로.

    Returns:
        XML plist 바이트.
    """
    log = str(repo / ".blackclip" / "follow.log")
    return plistlib.dumps({"Label": label(repo, "follow"), "ProgramArguments": [exe, "follow"], "WorkingDirectory": str(repo),
                           "StartInterval": FOLLOW_S, "StandardOutPath": log, "StandardErrorPath": log})


def _run(argv: list[str]) -> int:
    """명령을 돌리고 종료 코드만.

    Args:
        argv: 인자.

    Returns:
        종료 코드.
    """
    return subprocess.run(argv, capture_output=True, timeout=30).returncode


def install(repo: Path, hour: int, minute: int, agents: Path | None = None, run: Run = _run,
            platform: str = sys.platform, exe: str | None = None, follow: bool = False, daily: bool = True) -> list[str]:
    """plist 둘(매일 대조 · 자주 보는 시계)을 쓰고 launchd에 올린다(이미 있으면 내렸다 다시). macOS가 아니면 crontab 줄만.

    follow면 키 없이 지켜보는 시계(5분마다)도 건다. 선(follow.json)이 있는 레포만이라 조회 키만 쓰는 레포는 그대로 둘이다.
    daily가 False면 둘은 걸지 않는다(선만 있고 저장한 가드레일이 없는 레포: 걸면 돌 때마다 「저장된 전략이 없습니다」로 끝남).
    전에 건 둘이 있으면 내리고 plist를 지운다(_drop). 내려갔는지 모르면 그 줄로 말하고 종료 코드는 그대로.

    Args:
        repo: 레포 꼭대기.
        hour: 매일 대조 시(로컬).
        minute: 매일 대조 분.
        agents: LaunchAgents 폴더(기본 ~/Library/LaunchAgents).
        run: 명령 실행기.
        platform: 운영체제(시험).
        exe: blackclip 경로(시험).
        follow: 키 없이 지켜보는 시계도 걸지.
        daily: 매일 대조 · 자주 보는 시계를 걸지.

    Returns:
        화면 줄.

    Raises:
        OSError: 파일을 못 쓰거나 launchctl bootstrap이 실패했을 때(어느 plist인지 이름을 넣음).
    """
    exe = exe or executable()
    wm = watch_minute(minute)
    kinds = (["daily", "watch"] if daily else []) + (["follow"] if follow else [])
    later = [] if daily else ["매일 대조 · 자주 보는 시계는 안 걸었어요 · 가드레일을 저장한 전략이 생기면 blackclip schedule을 다시"]
    if platform != "darwin":
        cron = {"daily": f"{minute} {hour} * * * cd {repo} && {exe} daily >> .blackclip/daily.log 2>&1",
                "watch": f"{wm} * * * * cd {repo} && {exe} watch >> .blackclip/watch.log 2>&1",
                "follow": f"*/5 * * * * cd {repo} && {exe} follow >> .blackclip/follow.log 2>&1"}
        what = {"watch": "자주 보는 시계", "follow": "키 없이 지켜보는 시계"}
        return ([f"이 운영체제는 자동으로 걸지 않아요 · crontab -e에 한 줄: {cron[kinds[0]]}"]
                + [f"{what[k]}도 걸려면 한 줄 더: {cron[k]}" for k in kinds[1:]] + later
                + ([] if daily else ["crontab에 이미 넣은 daily · watch 줄이 있으면 지워요"]))
    agents = agents or Path.home() / "Library" / "LaunchAgents"
    agents.mkdir(parents=True, exist_ok=True)
    domain = f"gui/{os.getuid()}"
    jobs = {"daily": plist(repo, exe, hour, minute), "watch": watch_plist(repo, exe, wm), "follow": follow_plist(repo, exe)}
    for kind in kinds:
        path = agents / f"{label(repo, kind)}.plist"
        run(["launchctl", "bootout", f"{domain}/{label(repo, kind)}"])  # 없으면 실패해도 된다
        path.write_bytes(jobs[kind])
        if run(["launchctl", "bootstrap", domain, str(path)]) != 0:
            raise OSError(f"launchctl bootstrap이 실패했습니다({path.name})")
    for kind, what in () if daily else KINDS:  # 전에 건 둘이 남으면 돌 때마다 「저장된 전략이 없습니다」로 끝남
        line = _drop(repo, agents, run, kind, what)[1]
        if line:
            later.append(line)
    said = {"daily": f"매일 {hour:02d}:{minute:02d}(이 컴퓨터 시간) blackclip daily를 걸었어요 · 잠들어 있었으면 깨어날 때 한 번 돎",
            "watch": f"매시 {wm:02d}분 blackclip watch를 걸었어요 · 전략별 계좌(--capital 없음)인 전략만 · "
                     "맥이 잠든 동안은 1시간 점검도 멈춤 · 깨어나면 한 번 돎",
            "follow": f"{FOLLOW_S // 60}분마다 blackclip follow를 걸었어요 · 키 없이 지켜보는 전략(선 follow.json이 있는 전략)만 · "
                      "맥이 잠든 동안은 멈춤 · 깨어나면 한 번 돎"}
    return ([said[k] for k in kinds] + later
            + ["로그: " + " · ".join(f".blackclip/{k}.log" for k in kinds) + " · 끄기: blackclip schedule --remove"])


def _drop(repo: Path, agents: Path, run: Run, kind: str, what: str) -> tuple[bool, str | None]:
    """예약 하나를 launchd에서 내리고 plist를 지운다. 말은 bootout 종료 코드와 파일 유무가 말해 주는 만큼만 한다.

    bootout 0 = 지움, 3(없는 예약) = launchd에 없음이 확인됨. 그 밖의 종료 코드는 내려갔는지 모르므로 파일을 남기고
    확인 불가로 말한다(다시 --remove하면 같은 길).

    Args:
        repo: 레포 꼭대기.
        agents: LaunchAgents 폴더.
        run: 명령 실행기.
        kind: 예약 종류(daily · watch · follow).
        what: 화면에 쓰는 이름.

    Returns:
        (확인됐는지, 화면 줄). 건 적 없음(종료 3 · 파일 없음)이면 줄은 None.
    """
    path = agents / f"{label(repo, kind)}.plist"
    target = f"gui/{os.getuid()}/{label(repo, kind)}"
    rc = run(["launchctl", "bootout", target])
    existed = path.exists()
    if rc not in (0, NOT_LOADED):
        return False, f"{what} 예약 확인 불가(종료 {rc}) · 확인: launchctl print {target}"
    if existed:
        path.unlink()
    if rc == 0:
        return True, f"{what} 예약을 지웠어요"
    if existed:
        return True, f"{what} 예약 파일을 지웠어요 · launchd에는 올라가 있지 않았어요(종료 {rc})"
    return True, None


def remove(repo: Path, agents: Path | None = None, run: Run = _run) -> tuple[bool, list[str]]:
    """launchd에서 예약을 모두 내리고 plist를 지운다(예약마다 _drop).

    Args:
        repo: 레포 꼭대기.
        agents: LaunchAgents 폴더.
        run: 명령 실행기.

    Returns:
        (모두 확인됐는지, 화면 줄(예약마다 한 줄)). False면 cli가 실패 종료 코드를 낸다.
    """
    agents = agents or Path.home() / "Library" / "LaunchAgents"
    ok, out = True, []
    for kind, what in KINDS + (("follow", "키 없이 지켜보는 시계"),):
        if kind == "follow" and not (agents / f"{label(repo, kind)}.plist").exists():  # 건 적 없는 레포(조회 키만)는 0.1.0과 같은 명령 · 두 줄
            continue
        done, line = _drop(repo, agents, run, kind, what)
        ok = ok and done
        out.append(line or f"걸린 {what} 예약이 없어요")
    return ok, out

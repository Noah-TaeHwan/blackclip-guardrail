"""blackclip setup: 첫 셋업이 어디까지 왔는지와 다음 할 일 하나를 코드가 판정한다.

레포 쪽은 읽기만 한다(작업 폴더 파일 · 받은 체결만, 네트워크 없음). 숫자 · 상태는 여기서, 설명은 AI가 한다.
체결 기록 유무 갈래는 이미 받은 `.blackclip/fills/`만 본다. 있으면 전략 연결 다음이 「첫 회고」(연결된 전략마다 retro.md가 근거).
전략 단계는 가장 앞선 전략 하나를 끝까지 민다. 끝남은 파일이 있음이 아니라 읽어서 맞는 모양일 때만.
작업 폴더 · 중간 폴더가 링크면 따르지 않고, 파일은 링크 아닌 일반 파일만 상한까지 읽는다(훅과 같은 원칙).
레포에서 온 전략 이름은 _NAME 모양만 받고, 명령에 넣는 경로는 셸 인용한다.
"""
from __future__ import annotations

import json
import math
import os
import re
import shlex
import stat
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

from blackclip.hook import _NAME, _read_regular
from blackclip.notify import RETRO, has_retro, read_live
from blackclip.sim.warning_line import SENSITIVITY
from blackclip.workspace import WorkspaceError, load_connections

STEPS = ("훑기", "조회 키", "체결 기록", "전략 연결", "점검", "가드레일", "실전 시작", "예약 · 알림")
"""셋업 단계(순서대로). 체결 기록이 있으면 「전략 연결」 다음에 RETRO_STEP이 끼어 아홉 단계."""

RETRO_STEP = "첫 회고"
"""체결 기록 있음 갈래에서만 있는 단계(「실전 전략만 먼저 연결하고 회고부터」, 이름은 「첫 회고」). 연결된 전략 중 하나라도 회고가 있으면 끝, 나머지는 알림 줄."""

FIRST_DAYS = 90
"""첫 회고가 보는 기간(일, 지난 석 달). 체결 기록 단계의 fetch --since와 retro --days가 같은 값."""

BOTFATHER = ("텔레그램 봇은 직접 만들어요. 텔레그램에서 @BotFather → /newbot → 봇 이름과 아이디를 정하고, 받은 토큰은 "
             "대화창에 붙이지 말고 blackclip connect --open --telegram 화면에 넣어 주세요")
"""텔레그램 봇 만들기 안내 한 줄(사용자마다 자기 봇)."""

LIVE_HOW = ("실전 시작: 이 전략만 쓰는 계좌(서브 계정)면 blackclip live <전략>(1시간마다 보는 시계까지) · "
            "한 계좌에 여러 전략이면 blackclip live <전략> --capital <이 전략 몫>(하루 한 번 대조만)")
"""blackclip live 쓰는 법(전략별 계좌 판정은 --capital 유무, 따로 묻지 않음)."""

FILLS_CAP = 64 << 20
"""fills.csv에서 줄을 세는 바이트 상한(64MiB). 넘으면 거기까지 센 수(최솟값)."""

_GUARD = re.compile(r"guardrail\.v([1-9][0-9]{0,5})\.yaml")
"""가드레일 판 파일 이름."""


def _dir(p: Path) -> bool:
    """링크 아닌 폴더인지.

    Args:
        p: 경로.

    Returns:
        그렇다면 True.
    """
    return p.is_dir() and not p.is_symlink()


def _regular(p: Path) -> bool:
    """링크 아닌 일반 파일인지(따르지 않고 봄).

    Args:
        p: 경로.

    Returns:
        그렇다면 True.
    """
    try:
        return stat.S_ISREG(os.lstat(p).st_mode)
    except OSError:
        return False


def _json(p: Path) -> dict | None:
    """링크 아닌 일반 파일의 JSON 객체(1MiB까지).

    Args:
        p: 파일.

    Returns:
        dict, 없음 · 링크 · 깨짐 · 객체 아님이면 None.
    """
    raw = _read_regular(p, 1 << 20)
    try:
        data = json.loads(raw.decode("utf-8")) if raw is not None else None
    except (ValueError, RecursionError):  # 깊게 중첩된 JSON도 못 읽은 것으로
        return None
    return data if isinstance(data, dict) else None


def _fills(root: Path) -> tuple[bool, int, str | None]:
    """받은 체결(blackclip fetch가 쓴 것)만 센다. 폴더 이름은 _NAME 모양, 링크는 안 따름.

    Args:
        root: .blackclip 경로.

    Returns:
        (한 번이라도 받았나, 체결 줄 수, 줄이 가장 많은 fills.csv의 레포 기준 경로).
    """
    fetched, rows, best, most = False, 0, None, 0
    base = root / "fills"
    for acct in sorted(base.iterdir()) if _dir(base) else []:
        for d in sorted(acct.iterdir()) if _NAME.fullmatch(acct.name) and _dir(acct) else []:
            if not (_NAME.fullmatch(d.name) and _dir(d)):
                continue
            fetched = fetched or bool(_json(d / "coverage.json"))
            raw = _read_regular(d / "fills.csv", FILLS_CAP)
            n = max(raw.count(b"\n") - 1, 0) if raw else 0
            rows += n
            if n > most:
                best, most = (d / "fills.csv").relative_to(root.parent).as_posix(), n
    return fetched, rows, best


def _check(folder: Path) -> str | None:
    """check.md 상태: blackclip check가 쓴 모양이면 「ok」(추천 가드레일 있음) · 「막힘」(점검 불가), 아니면 None.

    Args:
        folder: .blackclip/strategies/<전략>.

    Returns:
        「ok」 · 「막힘」 · None.
    """
    raw = _read_regular(folder / "check.md", 1 << 20)
    text = raw.decode("utf-8", "replace") if raw else ""
    if not text.startswith(f"# 점검 · {folder.name}\n") or "\n| 추천 가드레일 | " not in text:
        return None
    return "ok" if "\n| 추천 가드레일 | 민감도 " in text else "막힘"


def _num(x: object) -> bool:
    """유한한 숫자인지(bool · NaN · 무한 아님).

    Args:
        x: 값.

    Returns:
        그렇다면 True.
    """
    try:
        return isinstance(x, (int, float)) and not isinstance(x, bool) and math.isfinite(x)
    except OverflowError:  # 309자리를 넘는 정수는 float로 못 바꿈(레포가 심을 수 있음)
        return False


def _guard(folder: Path) -> bool:
    """최신 판 가드레일을 watch · daily가 쓸 수 있는지: format 1 · 전략 이름 · 판 번호, 그리고 watch가 읽는 칸
    (target.market · symbols · bar_seconds, warning_line.sensitivity · line · lines 모든 칸, period.mean · std,
    trade_frequency low · high)이 맞는 모양 · 유한한 숫자.

    Args:
        folder: .blackclip/strategies/<전략>.

    Returns:
        맞으면 True.
    """
    have = sorted(int(m[1]) for f in folder.iterdir() if (m := _GUARD.fullmatch(f.name)))
    g = _json(folder / f"guardrail.v{have[-1]}.yaml") if have else None
    if not (g and g.get("format") == 1 and g.get("strategy") == folder.name and g.get("version") == have[-1]):
        return False
    t, wl, per, tf = g.get("target"), g.get("warning_line"), g.get("period"), g.get("trade_frequency")
    return (isinstance(t, dict) and t.get("market") in ("spot", "futures") and isinstance(t.get("symbols"), list)
            and bool(t["symbols"]) and all(isinstance(x, str) for x in t["symbols"])
            and _num(t.get("bar_seconds")) and float(t["bar_seconds"]).is_integer() and t["bar_seconds"] > 0
            and isinstance(wl, dict) and isinstance(wl.get("sensitivity"), str) and wl["sensitivity"] in SENSITIVITY
            and _num(wl.get("line"))
            and isinstance(wl.get("lines"), dict) and all(_num(wl["lines"].get(k)) for k in SENSITIVITY)
            and all(_num(v) for v in wl["lines"].values())  # watch/line.py가 모든 칸을 돎
            and isinstance(per, dict) and _num(per.get("mean")) and _num(per.get("std"))
            and isinstance(tf, dict) and all(tf.get(k) is None or _num(tf[k]) for k in ("low", "high")))  # None = 청산 없는 주


def _strategy(root: Path, folder: Path) -> tuple[int, str | None]:
    """전략 하나가 간 단계 수(연결 1 · 점검 2 · 가드레일 3 · 실전 시작 4, 앞 단계가 안 맞으면 거기서 멈춤)와 check.md 상태.

    Args:
        root: .blackclip 경로.
        folder: .blackclip/strategies/<전략>.

    Returns:
        (0~4, _check 결과).
    """
    if not _regular(folder / "link.py"):
        return 0, None
    chk = _check(folder)
    steps = (chk == "ok", _guard(folder), read_live(root, folder.name) is not None)  # read_live: 링크 아닌 일반 파일 · 상한
    return 1 + next((i for i, ok in enumerate(steps) if not ok), len(steps)), chk


def judge(root: Path, agents: Path | None = None, now: datetime | None = None) -> dict:
    """셋업 단계 판정.

    Args:
        root: .blackclip 경로.
        agents: LaunchAgents 폴더(시험, 기본 ~/Library/LaunchAgents). macOS가 아니면 예약은 미확인.
        now: 지금(시험에서 시계를 넣음, 기본 UTC 현재). 체결 기록 단계의 --since(90일 전 0시 UTC)를 정함.

    Returns:
        {"steps": 단계 이름들, "done": [True · False · None(미확인)], "now": 지금 단계 이름 또는 None,
         "next": 다음 할 일 한 줄, "branch": "있음" · "없음" · None, "notes": [덧붙일 줄], "strategy": 민 전략 이름 또는 None,
         "alerts": 「예약 · 알림」 단계의 세 값 {"chosen": 민감도를 골랐나, "linked": 텔레그램 대화를 이었나,
         "scheduled": 예약 plist가 있나(macOS가 아니면 None)}(대시보드 연결 화면이 씀)}.
    """
    from blackclip.schedule import label  # plistlib만 부름(가벼움)

    notes: list[str] = []
    if root.is_symlink():  # 그 안 · 옆 어떤 경로도 읽지 않고 처음 상태
        return {"steps": list(STEPS), "done": [False] * len(STEPS), "now": STEPS[0], "branch": None, "strategy": None,
                "alerts": {"chosen": False, "linked": False, "scheduled": None},
                "notes": ["작업 폴더 .blackclip이 심볼릭 링크라 읽지 않음(레포 안의 진짜 폴더여야 함)"],
                "next": "레포의 .blackclip 심볼릭 링크를 치운 뒤 blackclip scan . (scan도 링크된 작업 폴더에는 쓰지 않음)"}
    try:
        conns = load_connections(root)
    except WorkspaceError as exc:
        conns = {}
        notes.append(f"연결 상태 파일을 못 읽음({exc})")
    keyed = any(isinstance(a, dict) and a.get("permission") for a in (conns.get("binance") or {}).values()) \
        if isinstance(conns.get("binance"), dict) else False
    fetched, rows, fills = _fills(root)
    branch = ("있음" if rows else "없음") if fetched else None
    sdir = root / "strategies"
    folders = sorted(p for p in sdir.iterdir() if _dir(p)) if _dir(sdir) else []
    odd = [p for p in folders if not _NAME.fullmatch(p.name)]
    if odd:
        notes.append(f"이름 모양이 다른 전략 폴더 {len(odd)}개는 안 봄(영문 · 숫자 · _ · - 64자까지)")
    levels = {p: _strategy(root, p) for p in folders if p not in odd}
    best = max(levels, key=lambda p: levels[p][0], default=None)  # 같으면 이름순 첫째(max는 처음 만난 최댓값)
    level, chk = levels[best] if best else (0, None)
    linked_bots = [p for p, (lv, _) in levels.items() if lv >= 1]  # 연결 파일(link.py)이 일반 파일인 전략
    no_retros = [p for p in linked_bots if not has_retro(p)] if branch == "있음" else []  # 회고 없는 연결 전략(이름순), 그 갈래에서만 읽음
    if len(no_retros) < len(linked_bots) and no_retros:  # 회고가 하나는 있음: 나머지는 실전 봇일 수도 연구용일 수도 있어 막지 않고 알림
        shown = ", ".join(p.name for p in no_retros[:5]) + (" 등" if len(no_retros) > 5 else "")
        notes.append(f"회고 없는 연결 전략 {len(no_retros)}개({shown}): 실전 봇이면 blackclip retro로 더하고, 연구용이면 그대로 둠")
    no_retro = no_retros[0] if no_retros else None
    linked = (conns.get("telegram") or {}).get("chat_id") is not None if isinstance(conns.get("telegram"), dict) else False
    chosen = isinstance(conns.get("defaults"), dict) and "sensitivity" in conns["defaults"]
    agents = agents or Path.home() / "Library" / "LaunchAgents"
    scheduled = (agents / f"{label(root.parent)}.plist").exists() if sys.platform == "darwin" else None
    alerts = (None if scheduled is None else scheduled) if chosen and linked else False
    steps: list[str] = list(STEPS)
    done: list[bool | None] = [_regular(root / "map.json"), keyed, fetched, level >= 1, level >= 2, level >= 3, level >= 4,
                               alerts]
    if branch == "있음":
        steps.insert(4, RETRO_STEP)
        done.insert(4, len(no_retros) < len(linked_bots))  # 연결된 전략 중 하나라도 회고가 있으면 끝
    current = next((s for s, d in zip(steps, done) if d is not True), None)
    focus = no_retro if current == RETRO_STEP and no_retro else best  # 민 전략: 첫 회고 단계에서는 회고 없는 전략
    name = focus.name if focus else "<전략>"
    since = (now or datetime.now(timezone.utc)).astimezone(timezone.utc).replace(hour=0, minute=0, second=0, microsecond=0) \
        - timedelta(days=FIRST_DAYS)
    nxt = {
        "훑기": "blackclip scan . (연 폴더만 실행 없이 훑음, 다른 폴더는 고객이 고를 때만)",
        "조회 키": "blackclip connect --open (바이낸스에서 「조회」만 켠 API 키를 이 컴퓨터 브라우저에서 넣기)",
        "체결 기록": (f"blackclip fetch --symbols <봇이 거래하는 종목>(예: BTCUSDT) --since {since.isoformat()} "
                  "(지난 석 달 체결을 받아 체결 기록이 있는지에 따라 다음 길을 정해요)"),
        "전략 연결": ("실전에서 돌리는 전략만 먼저 연결 파일 .blackclip/strategies/<이름>/link.py를 만든다(신호 함수 또는 TRADES 거래 목록)"
                  if branch == "있음" else
                  "map.json 전략 후보 중 하나로 연결 파일 .blackclip/strategies/<이름>/link.py를 만든다(신호 함수)"),
        RETRO_STEP: (f"blackclip retro --fills {shlex.quote(fills or '<fills.csv>')} --backtest <그 전략의 백테스트 거래 목록 CSV> "
                     f"--capital <실전 시작 잔고> --live-start <실전 시작 ISO 시각> --days {FIRST_DAYS} "
                     f"--out .blackclip/strategies/{name}/{RETRO} (체결 기록 {rows}건이라 첫 회고부터 해요)"),
        "점검": (f"check.md의 추천 가드레일이 점검 불가 · 연결 파일을 고친 뒤 blackclip check {name}" if chk == "막힘"
               else f"blackclip check {name}"),
        "가드레일": (f"blackclip save {name}" if chosen else
                 f"민감도를 먼저 고르기: blackclip connect --open --choose, 그다음 blackclip save {name}"),
        "실전 시작": LIVE_HOW.replace("<전략>", name),
        "예약 · 알림": ("blackclip connect --open --choose (민감도 · 조용한 날 · 실행 경고 고르기)" if not chosen else
                    BOTFATHER if not linked else
                    "이 운영체제에서는 예약을 확인하지 않음 · blackclip schedule이 보인 crontab 줄을 걸었는지 직접 확인"
                    if alerts is None else "blackclip schedule (매일 대조 · 자주 보는 시계 예약)"),
    }
    return {"steps": steps, "done": done, "now": current, "branch": branch, "notes": notes, "strategy": focus.name if focus else None,
            "alerts": {"chosen": chosen, "linked": linked, "scheduled": scheduled},
            "next": nxt[current] if current else "셋업 끝 · 매일 대조 결과는 .blackclip/strategies/<전략>/daily/"}


def render(j: dict) -> list[str]:
    """AI가 읽을 셋업 요약(색 없음).

    Args:
        j: judge 결과.

    Returns:
        화면 줄.
    """
    n = sum(d is True for d in j["done"])
    unknown = j["now"] and j["done"][j["steps"].index(j["now"])] is None
    head = f"셋업: {len(j['steps'])}단계 중 {n}단계 끝" + (f" · 지금 「{j['now']}」" + ("(미확인)" if unknown else "")
                                                       if j["now"] else "")
    marks = " · ".join(f"{s} {'끝' if d is True else '미확인' if d is None else '전'}" for s, d in zip(j["steps"], j["done"]))
    branch = {"있음": "체결 기록 있음(실전 전략 먼저 · 첫 회고부터)", "없음": "체결 기록 없음(훑기 → 점검 → 가드레일)",
              None: "체결 기록 모름(아직 안 받음)"}[j["branch"]]
    return [head, marks, f"갈래: {branch}", f"다음: {j['next']}", *j["notes"]]

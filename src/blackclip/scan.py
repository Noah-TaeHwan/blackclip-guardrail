"""blackclip scan: 연 폴더를 실행 없이 글로만 읽어 전략 지도 `.blackclip/map.json`을 쓴다.

고객 코드를 import · 실행하지 않는다: .py는 글, .ipynb는 JSON의 코드 칸 글만 본다. `.env*` 파일은 열지 않고
이름만 「키처럼 보이는 값이 든 파일」로 적는다. 그 밖 파일도 키처럼 보이는 값의 자리(경로)만 적고 값은 어디에도 안 적는다.
심볼릭 링크는 따르지 않고, 항목 수 · 읽는 파일 수 · 파일 크기에 상한을 둔다. 크기 상한에 잘린 파일 · 모양이 다른 노트북은
「미검사」로 적는다(키가 없다는 뜻이 아님). 이름이 UTF-8이 아닌 파일은 건너뛰고 수만 적는다.
화면에 내는 레포 글자(경로)는 제어 문자를 \\u 꼴로 바꾼다. git 커밋 수는 gitlog가 센다(최솟값).
"""
from __future__ import annotations

import json
import os
import re
import time
from pathlib import Path

from blackclip import gitlog
from blackclip.hook import _NAME, _read_regular  # 링크 안 따르는 일반 파일 읽기 · 전략 이름 모양(훅과 같은 규칙)
from blackclip.telegram import TOKEN
from blackclip.workspace import WorkspaceError, write_in

MAP = "map.json"
"""전략 지도 파일(.blackclip 안, Git 제외는 GITIGNORE `*`)."""

MAX_FILES = 20000
"""읽는 파일 수 상한. 넘으면 앞쪽만 보고 map.json에 「상한에 닿음」."""

MAX_WALK = 200000
"""훑는 항목 수 상한(os.scandir가 내놓은 폴더 · 파일 · 링크 모두, 집 폴더처럼 큰 폴더에서 끝나게)."""

MAX_BYTES = 1 << 20
"""파일 하나에서 읽는 바이트 상한(1MiB)."""

SKIP_DIRS = {"node_modules", "__pycache__", "site-packages", "venv", "env"}
"""들어가지 않는 폴더(점으로 시작하는 폴더 · .git · .blackclip · .venv도 들어가지 않음)."""

TEXT = (".py", ".ipynb", ".json", ".yaml", ".yml", ".toml", ".ini", ".cfg", ".txt", ".sh")
"""키처럼 보이는 값을 찾아보는 파일 꼴."""

FRAMEWORKS = (("freqtrade", "freqtrade"), ("jesse", "jesse"), ("vectorbt", "vectorbt"),
              ("backtesting", "backtesting.py"), ("backtrader", "backtrader"), ("pandas", "pandas"))
"""(import 이름, 화면 이름). 앞에 있는 것이 먼저: 파일 하나는 틀 하나로 센다(합이 전략 수)."""

_IMPORT = re.compile(r"^[ \t]*(?:from[ \t]+([\w.]+)[ \t]+import[ \t]+\(?([\w \t,]+)|import[ \t]+([\w., \t]+))", re.M)
_STRATEGY_WORD = re.compile(r"signal|strateg|backtest|sharpe|crossover|drawdown", re.I)
"""pandas만 쓰는 파일은 이 낱말이 있을 때만 전략 후보(데이터 정리 스크립트를 빼려고)."""

_ORDER = re.compile(r"\.(?:create_(?:market_|limit_)?(?:buy_|sell_)?order|order_(?:market|limit)(?:_buy|_sell)?"
                    r"|futures_create_order|new_order)\s*\(")
"""주문 함수 호출 흔적(ccxt create_order · create_market_buy_order, python-binance order_market_buy · futures_create_order,
binance-connector new_order)."""

_LIVE_CONFIG = re.compile(r'"dry_run"\s*:\s*false')
"""freqtrade 설정의 실전 표시."""

_SECRET = re.compile(r"(?i)(?:key|secret|token|passw|private)[\w-]{0,40}[\"']?\s{0,8}[:=]\s{0,8}[\"']?[A-Za-z0-9+/_\-]{20}")
"""키처럼 보이는 값: 이름에 key · secret · token · passw · private가 들고 = 또는 : 뒤에 20자 이상 값.

반복마다 길이를 묶어 1MiB 반복 글에서도 시작점마다 일정한 시간에 끝난다. 이름 부분이 40자를 넘으면 못 잡는다."""

_PATH_STR = re.compile(r"[\"']([^\"'\n]{1,300}\.(?:py|ipynb))[\"']")
"""연결 파일 안의 .py · .ipynb 경로 글자."""


ABS_PATH = re.compile(r"(?:^|[\s:(\[\"'=,])(?:/[^\s/]+/|~/|\\\\[^\s\\]+\\)|file://|(?<![A-Za-z0-9])[A-Za-z]:[\\/]",
                      re.IGNORECASE)
"""절대 경로 꼴의 토큰: /폴더/… · ~/ · file:// · 드라이브 글자 + 「:」 + 역슬래시나 「/」 · 역슬래시 둘로 시작하는 서버 경로.
앞이 글 처음 · 공백 · 「:」 · 여는 괄호 · 따옴표 · = · , 인 「/」가 곧바로 폴더 이름과 또 하나의 「/」로 이어질 때만 걸린다.
「10/30」 · 「5 ~ 15」 · 「IS / OOS」 · 「격자 /」 · 주소의 「://」는 안 걸린다.
shortcut: 폴더 하나짜리 「/tmp」는 못 잡는다(홈 폴더 글은 has_abs_path가 따로 봄), 놓치는 사례가 나오면 넓힌다."""


def has_abs_path(text: str) -> bool:
    """커밋되는 파일(check.md · 가드레일)에 남길 고객 글에 절대 경로나 홈 폴더가 들었는지 본다.

    Args:
        text: 고객이 쓴 글(TRIALS 근거 · save --reason).

    Returns:
        절대 경로처럼 보이면 True.
    """
    try:
        home = str(Path.home())
    except (RuntimeError, OSError, KeyError):  # 홈 폴더를 못 정하는 환경(서비스 계정 등)
        home = ""
    return bool(ABS_PATH.search(text)) or (home not in ("", ".", "/") and home in text)


def has_secret(text: str) -> bool:
    """커밋되는 파일에 남길 고객 글에 키처럼 보이는 값이 들었는지 본다(레포 파일 검사와 같은 `_SECRET` · 텔레그램 `TOKEN`, record --reason · --decision).

    Args:
        text: 고객이 쓴 글.

    Returns:
        이름에 key · secret · token 같은 낱말이 든 「이름 = 20자 이상 값」 꼴이나 텔레그램 봇 토큰 꼴이 있으면 True.
    """
    return bool(_SECRET.search(text) or TOKEN.search(text))


def show(text: str) -> str:
    """레포에서 온 글자를 화면 한 줄 데이터로: 줄바꿈 · 제어 · 서식 문자를 \\uXXXX 꼴로 바꾼다.

    Args:
        text: 경로 같은 레포 글자.

    Returns:
        인쇄 가능한 글자만 남긴 글.
    """
    return "".join(c if c.isprintable() else f"\\u{ord(c):04x}" for c in text)


def _source(path: Path, raw: bytes) -> str | None:
    """파일 글. .ipynb면 코드 칸 글만 이어 붙인다.

    Args:
        path: 파일.
        raw: 읽은 바이트.

    Returns:
        글. 노트북 JSON이 깨졌거나 cells · source 모양이 다르면 None(미검사).
    """
    text = raw.decode("utf-8", "replace")
    if path.suffix != ".ipynb":
        return text
    try:
        cells = json.loads(text).get("cells")
    except (ValueError, AttributeError, RecursionError):  # 깊이 중첩된 JSON도 미검사
        return None
    if not isinstance(cells, list):
        return None
    out = []
    for c in cells:
        if not isinstance(c, dict):
            return None
        src = c.get("source", "")
        if isinstance(src, list) and all(isinstance(x, str) for x in src):
            src = "".join(src)
        if not isinstance(src, str):
            return None
        if c.get("cell_type") == "code":
            out.append(src)
    return "\n".join(out)


def imports(text: str) -> set[str]:
    """import 문이 부르는 모듈 이름(점 이름 그대로, from X import a면 X와 X.a 둘 다).

    Args:
        text: 파이썬 글.

    Returns:
        모듈 이름 집합(상대 import `.x`는 뺌).
    """
    mods: set[str] = set()
    for frm, names, plain in _IMPORT.findall(text):
        if frm:
            mods.add(frm)
            mods.update(f"{frm}.{n.split()[0]}" for n in names.split(",") if n.split())
        else:
            mods.update(p.split()[0] for p in plain.split(",") if p.split())
    return {m for m in mods if not m.startswith(".")}


def framework(text: str) -> str | None:
    """이 글이 쓰는 백테스트 틀(FRAMEWORKS 앞 순서로 하나), 전략 후보가 아니면 None.

    Args:
        text: 파이썬 글.

    Returns:
        화면 이름 또는 None.
    """
    tops = {m.split(".")[0] for m in imports(text)}
    for mod, label in FRAMEWORKS:
        if mod in tops and (mod != "pandas" or _STRATEGY_WORD.search(text)):
            return label
    return None


def _walk(folder: Path):
    """폴더를 링크 없이 훑어 파일 경로를 내놓는다(폴더마다 이름순, 점 폴더 · SKIP_DIRS 안 들어감).

    `os.scandir` 항목(폴더 · 파일 · 링크 모두)을 하나 읽을 때마다 MAX_WALK에서 세고, 다 쓰면 그 폴더도 더 읽지 않는다.
    정렬은 상한 안에서 읽은 항목에만 한다(한 폴더에 파일이 아주 많아도 메모리 · 시간이 상한에 묶임).

    Args:
        folder: 연 폴더.

    Yields:
        파일 경로(링크 아님), 항목 상한에 닿으면 마지막으로 None 하나.
    """
    left, stack = MAX_WALK, [folder]
    while stack:
        entries = []
        try:
            with os.scandir(stack.pop()) as it:
                for e in it:
                    if left <= 0:
                        break
                    left -= 1
                    entries.append(e)
        except OSError:
            continue
        subdirs = []
        for e in sorted(entries, key=lambda e: e.name):
            if e.is_dir(follow_symlinks=False):
                if not e.name.startswith(".") and e.name not in SKIP_DIRS:
                    subdirs.append(Path(e.path))
            elif e.is_file(follow_symlinks=False):
                yield Path(e.path)
        if left <= 0:
            yield None
            return
        stack.extend(reversed(subdirs))  # 앞 폴더부터(os.walk 위에서 아래 순서와 같음)


QUANT_DEPTH = 3
"""looks_quant가 들어가는 폴더 깊이(연 폴더가 0)."""

QUANT_ENTRIES = 300
"""looks_quant가 scandir에서 꺼내는 항목 총수(폴더 · 파일 · 링크 모두, 열거 순서대로 센다)."""

QUANT_BYTES = 64 << 10
"""looks_quant가 파일 하나에서 읽는 바이트 상한."""

QUANT_DEADLINE_S = 0.5
"""looks_quant 전체 마감(초). 지나면 거짓(첫 셋업 줄이 SessionStart를 붙잡지 않게)."""


_CELLS = re.compile(r'"cells"\s*:\s*\[')
_GAP = re.compile(r"[ \t\r\n,]*")


def _head_code(raw: bytes) -> str | None:
    """64KiB에서 잘려 JSON이 깨진 노트북의 앞부분에서 온전한 코드 칸 글만 이어 붙인다.

    `"cells"` 목록 안의 칸을 `raw_decode`로 하나씩 풀다가 못 푸는 칸(잘린 칸)에서 멈추고, 그 앞까지의 코드 칸 source만 쓴다.

    Args:
        raw: 읽은 바이트(QUANT_BYTES에서 잘림).

    Returns:
        코드 칸 글. `"cells"`가 앞부분에 없거나 온전한 코드 칸이 없거나 너무 깊은 JSON이면 None(미검사).
    """
    text = raw.decode("utf-8", "replace")
    m = _CELLS.search(text)
    if m is None:
        return None
    dec, pos, out = json.JSONDecoder(), m.end(), []
    try:
        while (pos := _GAP.match(text, pos).end()) < len(text) and text[pos] == "{":
            cell, pos = dec.raw_decode(text, pos)
            src = cell.get("source", "")
            src = "".join(src) if isinstance(src, list) and all(isinstance(x, str) for x in src) else src
            if not isinstance(src, str):
                break
            if cell.get("cell_type") == "code":
                out.append(src)
    except ValueError:  # 잘린 칸: 그 앞의 온전한 칸만 쓴다
        pass
    except RecursionError:
        return None
    return "\n".join(out) if out else None


def looks_quant(folder: Path) -> bool:
    """연 폴더가 퀀트 폴더처럼 보이는지 얕게 본다(첫 셋업 줄을 낼지 정함).

    너비 우선으로 깊이 QUANT_DEPTH까지, 폴더마다 `os.scandir`에서 한 항목씩 꺼내며 남은 항목 예산(QUANT_ENTRIES 총합) ·
    마감(QUANT_DEADLINE_S)을 그 자리에서 보고 넘으면 그 폴더 열거를 멈춘다. 꺼낸 것만 이름순으로 정렬해 차례를 정한다.
    링크 폴더 · 링크 파일은 따르지 않고, SKIP_DIRS · 점 폴더 · 못 읽는 폴더는 건너뛴다. `.py`는 글 그대로, `.ipynb`는 코드 칸만,
    파일은 링크 · 일반 파일 아님을 거르며 QUANT_BYTES까지만 읽는다. 틀 import(framework) 또는 주문 호출 흔적(_ORDER)이 하나라도 있으면 참.
    집 폴더 자체와 그 조상 폴더는 훑지 않고 거짓(세션을 집에서 열 때 문서 · 데스크탑 scandir가 macOS 권한 창을 띄우지 않게).
    QUANT_BYTES에서 잘린 .ipynb는 앞부분의 온전한 코드 칸만 본다(_head_code).
    shortcut: 첫 코드 칸 하나가 QUANT_BYTES를 넘으면 놓친다, 그런 노트북이 나오면 한도를 올리거나 칸 안 source만 따로 읽는다.

    Args:
        folder: 연 폴더.

    Returns:
        퀀트처럼 보이면 True(한도 안에서 못 찾았거나 마감이 지나면 False).
    """
    try:
        here, home = folder.resolve(), Path.home().resolve()
        if here == home or here in home.parents:  # 집 폴더와 그 조상(/Users · /)도 훑지 않음: 깊이 2~3에서 집 안 Desktop · Documents를 열게 됨
            return False
    except (RuntimeError, KeyError, OSError):  # 집 폴더를 못 정하면 이 검사만 건너뜀
        pass
    deadline, left = time.monotonic() + QUANT_DEADLINE_S, QUANT_ENTRIES
    queue = [(folder, 0)]
    while queue:
        d, depth = queue.pop(0)
        if left <= 0 or time.monotonic() > deadline:
            return False
        entries = []
        try:
            with os.scandir(d) as it:
                for e in it:
                    entries.append(e)
                    left -= 1
                    if left <= 0 or time.monotonic() > deadline:
                        break
        except OSError:
            continue
        for e in sorted(entries, key=lambda e: e.name):
            try:
                if e.is_dir(follow_symlinks=False):
                    if depth < QUANT_DEPTH and not e.name.startswith(".") and e.name not in SKIP_DIRS:
                        queue.append((Path(e.path), depth + 1))
                    continue
                if not (e.name.endswith((".py", ".ipynb")) and e.is_file(follow_symlinks=False)):
                    continue
            except OSError:
                continue
            if time.monotonic() > deadline:
                return False
            raw = _read_regular(Path(e.path), QUANT_BYTES)
            text = None if raw is None else _source(Path(e.path), raw)
            if text is None and raw is not None and len(raw) >= QUANT_BYTES and e.name.endswith(".ipynb"):
                text = _head_code(raw)
            if text is not None and (framework(text) or _ORDER.search(text)):
                return True
    return False


def linked(root: Path) -> dict[str, list[str]]:
    """전략 연결 파일(link.py)이 import하거나 경로로 적은 레포 안 .py · .ipynb(정적으로, 실행 없음).

    Args:
        root: .blackclip 경로.

    Returns:
        전략 이름 → 레포 꼭대기 기준 경로 목록(이름순).
    """
    top, out = root.parent, {}
    sdir = root / "strategies"
    if not sdir.is_dir() or sdir.is_symlink():
        return out
    for d in sorted(sdir.iterdir()):
        raw = _read_regular(d / "link.py", MAX_BYTES) if _NAME.fullmatch(d.name) and not d.is_symlink() else None
        if raw is None:
            continue
        text = raw.decode("utf-8", "replace")
        cands = [f"{m.replace('.', '/')}{tail}" for m in imports(text) for tail in (".py", "/__init__.py")]
        cands += [s for s in _PATH_STR.findall(text) if not s.startswith(("/", "~")) and ".." not in s.split("/")]
        files = sorted({c for c in cands if (top / c).is_file() and not (top / c).is_symlink()
                        and root not in (top / c).resolve().parents})
        out[d.name] = files
    return out


def scan(folder: Path, root: Path, now: float | None = None) -> dict:
    """연 폴더를 훑어 전략 지도를 만들고 `.blackclip/map.json`에 쓴다.

    Args:
        folder: 연 폴더(이 안만 훑음).
        root: .blackclip 경로(ensure 결과, 부모가 레포 꼭대기).
        now: 시각(시험).

    Returns:
        map.json 내용(경로는 모두 레포 꼭대기 기준, 키 값 없음).

    Raises:
        WorkspaceError: 훑을 폴더 경로가 UTF-8 이름이 아님 · .blackclip이 링크.
    """
    top = root.parent
    folder = folder.resolve()
    where = folder.relative_to(top).as_posix() or "."
    try:
        where.encode("utf-8")
    except UnicodeEncodeError:  # 폴더 이름 바이트가 UTF-8이 아님(리눅스): map.json에 못 적어 훑지 않음
        raise WorkspaceError("훑을 폴더 경로가 UTF-8 이름이 아니라 훑지 않습니다(UTF-8 이름의 폴더에서 다시)") from None
    strategies, bots, secrets, unchecked, notebooks, seen, odd, capped = [], [], [], [], 0, 0, 0, False
    for p in _walk(folder):
        if p is None or seen >= MAX_FILES:
            capped = True
            break
        name = p.name
        if not (name.startswith(".env") or p.suffix in TEXT):
            continue
        rel = p.relative_to(top).as_posix()
        try:
            rel.encode("utf-8")
        except UnicodeEncodeError:  # 이름 바이트가 UTF-8이 아님(리눅스): map.json에 못 적어 건너뛰고 수만
            odd += 1
            continue
        seen += 1
        if name.startswith(".env"):  # 열지 않는다, 이름만
            secrets.append(rel)
            continue
        raw = _read_regular(p, MAX_BYTES + 1)
        if raw is None:
            continue
        if len(raw) > MAX_BYTES:  # 앞부분만 보고 「키 없음」으로 읽히지 않게 통째로 미검사
            unchecked.append({"path": rel, "why": "잘림 · 미검사(크기 상한 넘음)"})
            continue
        text = raw.decode("utf-8", "replace")
        if _SECRET.search(text) or TOKEN.search(text):
            secrets.append(rel)
        if p.suffix == ".json" and _LIVE_CONFIG.search(text):
            bots.append({"path": rel, "why": "freqtrade dry_run false"})
        if p.suffix not in (".py", ".ipynb"):
            continue
        notebooks += p.suffix == ".ipynb"
        src = _source(p, raw)
        if src is None:
            unchecked.append({"path": rel, "why": "노트북 모양이 다름 · 미검사"})
            continue
        if (fw := framework(src)) is not None:
            strategies.append({"path": rel, "framework": fw})
        if _ORDER.search(src):
            bots.append({"path": rel, "why": "주문 함수"})
    cands, link_files = [s["path"] for s in strategies], linked(root)
    touched, git_state = gitlog.history(top, list(dict.fromkeys(cands + [f for fs in link_files.values() for f in fs])))
    for s in strategies:
        s["commits"] = len(touched[s["path"]]) if s["path"] in touched else None
    links = {n: {"files": fs, "commits": gitlog.count(touched, fs) if fs and all(f in touched for f in fs) else None}
             for n, fs in link_files.items()}
    doc = {"v": 1, "scanned_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(now)),
           "folder": where, "files_read": seen, "capped": capped,
           "notebooks": notebooks, "strategies": strategies, "bots": bots, "secrets": secrets, "unchecked": unchecked,
           "odd_names": odd, "linked": links,
           "git": {"state": git_state, "commits": gitlog.count(touched, cands) if git_state == "ok" else None,
                   "counted": sum(p in touched for p in cands)}}
    write_in(root, MAP, json.dumps(doc, ensure_ascii=False, indent=2, sort_keys=True) + "\n")
    return doc


def render(doc: dict) -> list[str]:
    """AI가 읽을 요약(숫자만, 색 없음).

    Args:
        doc: scan 결과.

    Returns:
        화면 줄.
    """
    counts: dict[str, int] = {}
    for s in doc["strategies"]:
        counts[s["framework"]] = counts.get(s["framework"], 0) + 1
    by = " · ".join(f"{k} {v}" for k, v in sorted(counts.items(), key=lambda kv: (-kv[1], kv[0])))
    g = doc["git"]
    git = (f"git 커밋 {g['commits']}개(전략 후보 파일을 바꾼 것 · 이름 바뀜 이어 셈 · git 기록 밖은 몰라 최솟값)"
           if g["state"] == "ok" else f"git 커밋 모름({g['state']})" if not g["counted"] else
           f"git 커밋 불완전({g['state']} · 전략 후보 {g['counted']}/{len(doc['strategies'])}개만 셈)")
    lines = [f"훑기 · 폴더 {show(doc['folder'])} · 읽은 파일 {doc['files_read']}개"
             + ("(상한에 닿음 · 나머지는 안 봄)" if doc["capped"] else ""),
             f"전략 후보 {len(doc['strategies'])}개" + (f"({by})" if by else "")
             + f" · 실전 봇 후보 {len(doc['bots'])}개 · 노트북 {doc['notebooks']}개 · {git}"]
    def few(paths: list[str]) -> str:
        """경로 다섯 개까지(제어 문자 바꿈) + 나머지 수."""
        return ", ".join(show(x) for x in paths[:5]) + (f" 외 {len(paths) - 5}개" if len(paths) > 5 else "")

    if doc["secrets"]:
        lines.append(f"키처럼 보이는 값이 든 파일 {len(doc['secrets'])}개(값은 안 적음 · 이 파일들은 열어 읽지 않기): "
                     f"{few(doc['secrets'])}")
    if doc["unchecked"]:
        lines.append(f"미검사 파일 {len(doc['unchecked'])}개(크기 상한에 잘렸거나 노트북 모양이 다름 · 키가 없다는 뜻 아님 · "
                     f"열어 읽지 않기): {few([u['path'] for u in doc['unchecked']])}")
    if doc["odd_names"]:
        lines.append(f"이름이 UTF-8이 아닌 파일 {doc['odd_names']}개는 건너뜀")
    for n, v in sorted(doc["linked"].items()):
        lines.append(f"연결된 전략 {n}: " + (f"커밋 {v['commits']}개(연결 파일이 부르는 레포 파일 {len(v['files'])}개)"
                                          if v["commits"] is not None else
                                          "커밋 모름(연결 파일이 부르는 레포 .py · .ipynb를 못 찾음)" if not v["files"]
                                          else f"커밋 모름({g['state']})"))
    lines.append(f"저장: .blackclip/{MAP}(Git 제외) · 다음 할 일: blackclip setup")
    return lines

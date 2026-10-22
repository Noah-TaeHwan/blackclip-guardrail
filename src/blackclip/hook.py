"""Claude Code 훅 엔진(`blackclip hook <이벤트>`): 지켜보기만 한다.

백테스트로 보이는 Bash 명령의 시작 · 끝과 레포 안 .py · .ipynb 편집을 `.blackclip/runs/runs.jsonl`에 한 줄씩 적고,
세션 시작 때 상태 한 줄을 낸다. 명령을 고치지 않고 허락 · 거절도 내지 않는다. 예외는 삼키고 늘 종료 0,
SessionStart 밖에서는 stdout에 아무것도 쓰지 않는다(async 훅 stdout은 AI에게 감).
pandas · numpy · blackclip.check · daily · watch를 import하지 않는다(훅마다 파이썬이 새로 뜸).
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import shlex
import shutil
import signal
import stat
import subprocess
import sys
import time
from pathlib import Path

from blackclip.workspace import _chain, find_root

SCAN = 4096
"""백테스트 고르기는 명령 앞 이만큼 글자만 본다(거대 명령에서 훅이 늦어지지 않게)."""

LIMIT = 512
"""기록 한 줄 바이트 상한(POSIX PIPE_BUF 최소). O_APPEND 한 번 write가 섞이지 않게 이 안으로 자른다."""

RUNS = "runs/runs.jsonl"
"""기록 파일(.blackclip 안, Git 제외는 GITIGNORE `*`)."""

_PY = re.compile(r"\b(?:python3?|ipython)\b", re.I)
_SKIP = re.compile(r"\b(?:pytest|pip)\b", re.I)
_WORD = re.compile(r"[\s/_.-](?:back_?test|hyperopt|optimi[sz]e|grid_?search|sweep)", re.I)
_TOOL = re.compile(r"\bfreqtrade\s+(?:backtesting|hyperopt)|\bjesse\s+(?:backtest|optimize)|\bblackclip\s+run\s+--", re.I)

SENSITIVE = ("KEY", "TOKEN", "SECRET", "PASS", "PWD", "AUTH", "BEARER", "COOKIE", "CREDENTIAL", "JWT", "PRIVATE", "MNEMONIC")
"""토큰에 들어 있으면 값을 가리는 낱말. `-` · `_`를 뺀 대문자 토큰에서 찾는다(BEARER는 Bearer 뒤 값,
PRIVATE · MNEMONIC은 지갑 비밀값). SEED는 `--seed 1`(백테스트 난수)을 가리므로 넣지 않는다."""

_TG_TOKEN = re.compile(r"(?<![0-9])[0-9]{5,16}:[A-Za-z0-9_-]{30,80}")
"""텔레그램 봇 토큰 모양(telegram.TOKEN과 같은 꼴, 그 모듈을 안 불러 사본 · 시험이 대조). 이 제품이 다루는 비밀값이라 낱말이 없어도 가림."""

_USERINFO = re.compile(r"(?<=://)[^/@\s]*@")
_CUT = re.compile(r"[=:\s]")


def is_backtest(cmd: str) -> bool:
    """백테스트로 보이는 명령인가(처음 잰 규칙 v2와 같은 판정, 역추적 없이 · 앞 SCAN 글자만).

    규칙 v2: `;` `&` `|`로 나눈 한 토막 안에서 python · python3 · ipython 뒤에 pytest · pip가 없고,
    그 뒤에 구분자(공백 / _ . -) + 백테스트 낱말이 있음. 또는 freqtrade · jesse 백테스트 · `blackclip run --`.
    놓침 2/10 · 오탐 1/5(작업자가 만든 표본).

    Args:
        cmd: Bash 명령.

    Returns:
        맞으면 True.
    """
    head = cmd[:SCAN]
    if _TOOL.search(head):
        return True
    for seg in re.split(r"[;&|]", head):
        skip = max((m.start() for m in _SKIP.finditer(seg)), default=-1)
        py = next((m for m in _PY.finditer(seg) if m.end() > skip), None)  # pytest · pip보다 뒤에 끝나는 첫 python
        if py and _WORD.search(seg, py.end()):
            return True
    return False


def _sensitive(token: str) -> bool:
    """토큰에 SENSITIVE 낱말이 들었나(대소문자 · `-` · `_` 무시: `--api_key` · `X-Api-Key` · `apikey`).

    Args:
        token: 셸 토막 하나.

    Returns:
        들었으면 True.
    """
    flat = token.upper().replace("-", "").replace("_", "")
    return any(w in flat for w in SENSITIVE)


def mask(cmd: str) -> str | None:
    """명령을 셸처럼 나눠 키처럼 보이는 값을 `***`로 가린다(한 번 훑는 한 규칙).

    셸 기호(`;` `&&` `||` `|` …)에서 끊어 그 뒤는 새로 시작한다. 토막마다 `://사람:비번@`은 `://***@`로, 텔레그램 봇 토큰 꼴은 `***`로 바꾸고,
    토막에 민감어(SENSITIVE)가 들면 첫 `=` · `:` · 공백 중 먼저 오는 것 뒤를 가린다.
    - `=`가 먼저(`NAME=값` · `--옵션=값`): 그 뒤만 가림. 토막 앞은 남음(`API_KEY=x python3 a.py`의 명령은 남음).
      `--옵션=머리글:`처럼 `-`로 시작하는 토막의 값에 `:`가 있으면 값이 다음 토막으로 이어질 수 있어 아래처럼 이음.
    - 공백이 먼저(따옴표로 묶인 `"Bearer x"` · `"Authorization Bearer x"`): 공백 뒤만 가림(값이 같은 토막 안에 있음).
    - `:`가 먼저(머리글 `이름:값` · `{"token":"x"}`) 또는 `:` · `=` · 공백이 없음(`--auth` · `Bearer`): `:` 뒤를 가리고,
      다음 셸 기호까지 남은 토막 전부를 가림(`Authorization: Basic x`의 자격 증명이 다음 토막이라서).
    과잉 가림은 받아들인다(`--auth x --symbol BTC`는 `--symbol BTC`도 가려짐).

    Args:
        cmd: Bash 명령.

    Returns:
        가린 명령(토막을 공백으로 다시 이음, 공백 든 토막만 따옴표). 셸처럼 나누지 못하면(따옴표가 안 닫힘 등) None.
    """
    try:
        lex = shlex.shlex(cmd, posix=True, punctuation_chars=True)
        lex.whitespace_split, lex.commenters = True, ""
        tokens = list(lex)
    except ValueError:
        return None
    out, rest = [], False  # rest: 다음 셸 기호 전까지 남은 토막을 모두 가림
    for t in tokens:
        if t and all(c in lex.punctuation_chars for c in t):  # 셸 기호에서 끊고 새로 시작
            rest = False
        elif rest:
            t = "***"
        else:
            t = _TG_TOKEN.sub("***", _USERINFO.sub("***@", t))
            if _sensitive(t):
                m = _CUT.search(t)
                if m is None:
                    rest = True
                else:
                    i = m.start()
                    rest = t[i] == ":" or (t[0] == "-" and ":" in t[i:])
                    t = t[: i + 1] + (" ***" if t[i] == ":" else "***")
        out.append(shlex.quote(t) if any(c.isspace() for c in t) else t)
    return " ".join(out)


def fit(text: str, budget: int, tail: bool = False) -> str:
    """JSON escape 뒤 길이가 budget 바이트 안이 되게 앞(tail이면 뒤)을 남기고 「…」를 붙인다.

    Args:
        text: 글.
        budget: 바이트(escape 뒤, 「…」 6바이트 포함).
        tail: 뒤를 남기려면 True(경로).

    Returns:
        그대로이거나 잘린 글. budget이 6보다 작으면 「…」 하나.
    """
    if len(json.dumps(text)) - 2 <= budget:
        return text
    keep, used = [], 6
    for c in reversed(text) if tail else text:
        used += len(json.dumps(c)) - 2  # 한글 6바이트 · 따옴표 2바이트
        if used > budget:
            break
        keep.append(c)
    return "…" + "".join(reversed(keep)) if tail else "".join(keep) + "…"


def line(rec: dict) -> bytes | None:
    """기록 한 줄을 만든다(ASCII JSON). LIMIT를 넘으면 cmd 뒤를 잘라 맞춘다.

    Args:
        rec: 기록(cmd는 가린 것 또는 None).

    Returns:
        줄 바이트(끝 줄바꿈 포함). cmd를 잘라도 넘으면 None(경로가 아주 긴 편집 등, 적지 않음).
    """
    def dump(r: dict) -> bytes:
        return (json.dumps(r, ensure_ascii=True, separators=(",", ":")) + "\n").encode()

    b, cmd = dump(rec), rec.get("cmd")
    if len(b) > LIMIT and isinstance(cmd, str):
        b = dump({**rec, "cmd": fit(cmd, LIMIT - (len(b) - (len(json.dumps(cmd)) - 2)))})
    return b if len(b) <= LIMIT else None


def append(root: Path, rec: dict) -> bool:
    """`.blackclip/runs/runs.jsonl`에 한 줄을 O_APPEND로 write 한 번에 덧붙인다(잠금 없음).

    Args:
        root: .blackclip 경로(있는 것만, 새로 만들지 않음).
        rec: 기록.

    Returns:
        적었으면 True. 줄이 너무 길거나 write가 줄 길이만큼 못 썼으면 False(조용히 포기).

    Raises:
        OSError: runs/ · 파일을 못 열 때(`.blackclip`이 없음 · 권한 · 심볼릭 링크 · 디스크 가득). 부르는 쪽이 삼킨다.
    """
    b = line(rec)
    if b is None:
        return False
    path = root / RUNS
    path.parent.mkdir(exist_ok=True)
    fd = os.open(path, os.O_WRONLY | os.O_APPEND | os.O_CREAT | os.O_NOFOLLOW, 0o600)  # 파일 링크는 안 엶(폴더 링크는 _root가 막음)
    try:
        return os.write(fd, b) == len(b)
    finally:
        os.close(fd)


OPEN = "runs/open"
"""시작을 적은 명령의 표지 폴더(.blackclip 안, 파일 이름 = tool_use_id). 끝은 표지가 있을 때만 쓴다."""

BUDGET_S = 2.0
"""훅 한 번의 스스로 시간 제한(초). async 훅은 Claude Code가 timeout을 강제하지 않는다."""

EDIT_TOOLS = {"Edit": "file_path", "Write": "file_path", "NotebookEdit": "notebook_path"}
"""편집 도구 → 경로 칸(공식 문서 tools-reference · Agent SDK 입력 타입)."""

SUFFIXES = (".py", ".ipynb")
"""편집을 적는 파일 꼴."""

ORDER = ("확인 불가", "점검 불가", "경고", "판단 보류", "정상")
"""대표 상태 우선순위. daily.reconcile.ORDER와 같아야 한다(그 모듈은 pandas를 불러 여기서 못 씀, 시험이 대조)."""

NO_ROOT = "blackclip-guardrail 첫 셋업 전이에요. 아무 말이나 하면 먼저 여쭤볼게요"
"""작업 폴더가 없을 때 화면 한 줄(예전 「연결 전」 줄을 바꿈)."""

FIRST_CONTEXT = (
    "blackclip-guardrail(트레이딩 전략을 실돈 전에 점검하고 실전을 지켜보는 플러그인)이 이 세션에 켜져 있다. "
    "이 작업 폴더에는 .blackclip이 없다: 첫 셋업 전이고, 훅 기록도 아직 쌓이지 않는다. "
    "고객 화면에는 「" + NO_ROOT + "」 한 줄이 떠 있다. "
    "이 플러그인의 첫 셋업 흐름: 고객이 이 세션에서 처음 말하면 그 말이 무엇이든 먼저 첫 셋업을 할지 한 번 여쭙는다. "
    "고객이 「응」이라고 하면 Bash로 `blackclip scan .`을 돌린다(연 폴더만 실행 없이 훑음, 다른 폴더는 고객이 고를 때만). "
    "그다음 할 일은 `blackclip setup`이 알려 주고, 그 출력의 「다음:」 아래 줄까지 고객에게 읽어 준다. 순서와 연결 파일 계약은 `blackclip` 스킬에 있다. "
    "고객이 셋업을 원하지 않으면 다시 묻지 않고 원래 요청을 돕는다."
)
"""작업 폴더가 없을 때 AI 맥락(공식 문서: 지시문보다 사실 문장으로, 10,000자 안)."""

STALE_S = 86400
"""이보다 오래된 시작 표지는 끝이 오지 않은 것(권한 거절 · 중단 · cd로 레포 밖)으로 보고 SessionStart가 지운다."""

SWEEP_MAX = 1000
"""SessionStart 한 번에 훑는 표지 수 상한(2초 제한 안)."""

SHOWN = 5
"""SessionStart 한 줄에 이름을 보이는 전략 수(나머지는 「외 N개」)."""

READ_CAP = 64 << 20
"""SessionStart가 runs.jsonl에서 읽는 바이트 상한(한 줄 512바이트면 약 13만 줄). 넘는 뒤쪽은 안 세어 수는 여전히 최솟값."""

NOTIFIED_CAP = 1 << 20
"""notified.json 읽기 상한."""

_ID = re.compile(r"[A-Za-z0-9_-]{1,64}")

_NAME = re.compile(r"[A-Za-z0-9][A-Za-z0-9_-]{0,63}")
"""SessionStart 한 줄에 세고 보이는 전략 폴더 이름 모양. check/link.py NAME의 사본(그 모듈을 못 불러 둠, 시험이 대조)."""


class _Budget(BaseException):
    """BUDGET_S가 지났을 때(Exception이 아니라 중간의 except OSError 등에 먹히지 않음)."""


def _root(start: Path) -> Path | None:
    """훅이 쓰는 작업 폴더: find_root에 더해 .blackclip · runs · runs/open이 심볼릭 링크면 None.

    훅은 고객이 고르지 않은 레포에서도 저절로 돈다. 레포가 커밋해 둔 링크를 따라 레포 밖에 쓰거나
    장치 파일을 읽지 않게 막는다(레포가 심은 파일을 따르거나 실행하지 않는 원칙). 고객이 직접 부르는 다른 명령은 그대로.

    Args:
        start: 시작 폴더.

    Returns:
        .blackclip 경로, 없거나 링크면 None.
    """
    root = find_root(start)
    if root is None or any(p.is_symlink() for p in (root, root / "runs", root / OPEN)):
        return None
    return root


def _read_regular(path: Path, cap: int) -> bytes | None:
    """링크를 따르지 않고(O_NOFOLLOW) 막히지 않게(O_NONBLOCK) 열어 일반 파일일 때만 cap 바이트까지 읽는다.

    Args:
        path: 파일.
        cap: 최대 바이트.

    Returns:
        읽은 바이트, 없음 · 링크 · 일반 파일 아님 · 못 읽음이면 None.
    """
    try:
        fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    except OSError:
        return None
    try:
        if not stat.S_ISREG(os.fstat(fd).st_mode):
            return None
        chunks, left = [], cap
        while left > 0 and (b := os.read(fd, min(left, 1 << 20))):
            chunks.append(b)
            left -= len(b)
        return b"".join(chunks)
    except OSError:
        return None
    finally:
        os.close(fd)


DECLINED = "declined"
"""첫 셋업을 다시 묻지 않기로 한 레포의 표지 폴더 이름(사용자 설정 폴더 안)."""


def repo_top(start: Path) -> Path:
    """start에서 위로 올라가 `.git`이 있는 첫 폴더(레포 꼭대기), 없으면 start의 실제 경로.

    Args:
        start: 시작 폴더.

    Returns:
        실제 경로로 푼 레포 꼭대기.
    """
    chain = _chain(start)
    return next((d for d in chain if (d / ".git").exists()), chain[0])


def _mark(top: Path) -> Path:
    """레포 꼭대기의 거절 표지 경로: `설정 폴더/declined/<sha256(실경로)>`(hook · setup --decline이 같은 규칙을 쓰게 한 곳에).

    Args:
        top: 레포 꼭대기.

    Returns:
        표지 파일 경로(있는지는 안 봄).
    """
    from blackclip.keystore import config_dir

    return config_dir() / DECLINED / hashlib.sha256(str(top.resolve()).encode()).hexdigest()


def declined(top: Path) -> bool:
    """이 레포가 첫 셋업 거절 표지를 가졌는지: `설정 폴더/declined/<sha256(실경로)>`를 따르지 않고(lstat) 본다.

    표지는 일반 파일이고 내 것이어야 하고, 설정 폴더 · declined 폴더는 내 것이며 남이 못 써야 한다(schedule._mine).
    그 밖 · 오류는 거절이 아니다(레포가 심은 표지로 줄을 끌 수 없음, 훅은 늘 열림).

    Args:
        top: 레포 꼭대기.

    Returns:
        거절 표지가 믿을 만하게 있으면 True.
    """
    from blackclip.keystore import config_dir
    from blackclip.schedule import _mine

    try:
        d = config_dir()
        mark = os.lstat(_mark(top))
        return (_mine(d, folder=True) and _mine(d / DECLINED, folder=True)
                and stat.S_ISREG(mark.st_mode) and mark.st_uid == os.getuid())
    except (OSError, RuntimeError):  # RuntimeError: HOME도 passwd 항목도 없을 때 Path.home()
        return False


def _now() -> str:
    """지금 UTC 시각(초).

    Returns:
        예: 2026-10-16T09:00:00Z
    """
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


TRUSTED_PATH = "/usr/bin:/bin:/usr/sbin:/sbin:/opt/homebrew/bin:/usr/local/bin"
"""git을 찾고 git에 넘기는 PATH(고정, sudo의 secure_path 꼴). 사용자 PATH는 빈 칸 · . · 레포 안 칸 · git을 다시 찾는 래퍼
(mise · asdf 심 등)를 거쳐 연 레포가 심은 git에 닿을 수 있어 쓰지 않는다."""


def _git() -> str | None:
    """고정 PATH에서만 찾은 git.

    Returns:
        git 절대 경로, 없으면 None.
    """
    return shutil.which("git", path=TRUSTED_PATH)


def _git_head(cwd: str) -> str | None:
    """cwd의 git 짧은 해시(1초 timeout). git은 `/`에서 `-C`로 부르고 PATH도 고정 값만 넘긴다.

    Args:
        cwd: 폴더.

    Returns:
        짧은 해시(40자까지), 못 얻으면 None.
    """
    if (git := _git()) is None:
        return None
    try:
        r = subprocess.run([git, "-C", cwd, "rev-parse", "--short", "HEAD"], cwd="/", capture_output=True, text=True,
                           timeout=1, env={**os.environ, "PATH": TRUSTED_PATH})
    except (OSError, subprocess.SubprocessError):
        return None
    return (r.stdout.strip()[:40] or None) if r.returncode == 0 else None


def _rel(path: Path, top: Path) -> str:
    """레포 꼭대기 기준 상대 경로(꼭대기는 「.」).

    Args:
        path: 절대 경로.
        top: 레포 꼭대기(.blackclip의 부모).

    Returns:
        `/`로 나눈 상대 경로.
    """
    return path.relative_to(top).as_posix() or "."


def _tool_id(data: dict) -> str | None:
    """표지 파일 이름으로 쓸 수 있는 tool_use_id.

    Args:
        data: 훅 stdin JSON.

    Returns:
        영문 · 숫자 · _ · - 64자까지면 그 id, 아니면 None.
    """
    tid = data.get("tool_use_id")
    return tid if isinstance(tid, str) and _ID.fullmatch(tid) else None


def on_pre(data: dict) -> None:
    """PreToolUse(Bash): 규칙에 맞는 명령만 시작 기록 + 표지(id · 세션 · 가린 명령 · 레포 상대 cwd · git 짧은 해시).

    칸마다 길이를 잘라(세션 64 · cwd 뒤 120바이트 · id 64자) 시작 줄은 언제나 LIMIT 안이다.

    Args:
        data: 훅 stdin JSON.
    """
    cmd = (data.get("tool_input") or {}).get("command")
    tid = _tool_id(data)
    if data.get("tool_name") != "Bash" or not isinstance(cmd, str) or tid is None or not is_backtest(cmd):
        return
    cwd = Path(data.get("cwd") or ".").resolve()
    if (root := _root(cwd)) is None:
        return
    rec = {"v": 1, "ev": "start", "t": _now(), "id": tid, "session": fit(str(data.get("session_id")), 64),
           "cwd": fit(_rel(cwd, root.parent), 120, tail=True), "git": _git_head(str(cwd)), "cmd": mask(cmd)}
    if append(root, rec):
        (root / OPEN).mkdir(parents=True, exist_ok=True)
        (root / OPEN / tid).touch()


def exit_code(error: object) -> int | None:
    """PostToolUseFailure error 첫 줄 「Exit code N」의 N.

    Args:
        error: error 칸.

    Returns:
        종료 코드, 그 줄이 없으면 None(셸을 못 띄운 경우 등).
    """
    m = re.match(r"Exit code (-?\d+)", error) if isinstance(error, str) else None
    return int(m.group(1)) if m else None


def on_end(data: dict, failed: bool) -> None:
    """Bash 끝(PostToolUse · PostToolUseFailure 같은 처리): 시작 표지가 있을 때만 표지를 지우고 끝 기록.

    성공은 종료 코드 칸이 없어 code null(grep · diff 종료 1도 성공으로 옴). 백그라운드로 띄운 명령은
    끝이 아니라 띄운 순간이라 bg true(미검증).

    Args:
        data: 훅 stdin JSON.
        failed: PostToolUseFailure면 True.
    """
    tid = _tool_id(data)
    if data.get("tool_name") != "Bash" or tid is None or (root := _root(Path(data.get("cwd") or "."))) is None:
        return
    try:
        (root / OPEN / tid).unlink()  # 표지를 먼저 가져가 끝이 두 번 써지지 않게
    except FileNotFoundError:
        return
    resp = data.get("tool_response") if isinstance(data.get("tool_response"), dict) else {}
    rec = {"v": 1, "ev": "end", "t": _now(), "id": tid, "ok": not failed,
           "code": exit_code(data.get("error")) if failed else None,
           "interrupt": bool(data.get("is_interrupt") if failed else resp.get("interrupted")),
           "ms": data.get("duration_ms") if isinstance(data.get("duration_ms"), int) else None}
    if resp.get("backgroundTaskId"):
        rec["bg"] = True
    append(root, rec)


def on_edit(data: dict) -> None:
    """PostToolUse(Edit · Write · NotebookEdit): 레포 안 .py · .ipynb(.blackclip 밖)의 상대 경로 + 디스크 sha256.

    Args:
        data: 훅 stdin JSON.
    """
    field = EDIT_TOOLS.get(data.get("tool_name"))
    raw = (data.get("tool_input") or {}).get(field) if field else None
    if not isinstance(raw, str) or not raw.endswith(SUFFIXES):
        return
    path = (Path(data.get("cwd") or ".") / raw).resolve()
    root = _root(path.parent)
    if root is None or root in path.parents or not path.is_file():
        return
    append(root, {"v": 1, "ev": "edit", "t": _now(), "id": _tool_id(data), "session": fit(str(data.get("session_id")), 64),
                  "path": _rel(path, root.parent), "sha256": hashlib.sha256(path.read_bytes()).hexdigest()})


def session_line(cwd: Path) -> str:
    """SessionStart 한 줄: 전략 수 · 전략별 대표 상태(notified.json) · 훅이 본 백테스트 수.

    백테스트 수는 끝 줄 수다. 시작 줄은 권한 거절된 명령에도 남아(PreToolUse가 권한 확인보다 먼저, 실측)
    세면 부풀므로 실제로 돈 것만 센다. async 끝 줄이 세션 종료로 빠질 수 있어 최솟값.
    live.json · watch.json(금액 칸)은 읽지 않는다. 네트워크 없음. 파일은 링크를 따르지 않고 상한까지만 읽는다.

    Args:
        cwd: 세션 폴더.

    Returns:
        화면 한 줄.
    """
    root = _root(cwd)
    if root is None:
        return NO_ROOT
    sdir = root / "strategies"
    names = sorted(p.name for p in sdir.iterdir() if p.is_dir() and _NAME.fullmatch(p.name)) \
        if sdir.is_dir() and not sdir.is_symlink() else []
    raw = _read_regular(root / "notified.json", NOTIFIED_CAP)
    try:
        notified = json.loads(raw.decode("utf-8")) if raw is not None else {}
    except ValueError:
        notified = {}
    notified = notified if isinstance(notified, dict) else {}
    parts = [f"전략 {len(names)}개"]
    for n in names[:SHOWN]:
        seen = [s.split("(")[0] for k in (n, f"watch:{n}") if isinstance(notified.get(k), dict)
                for s in notified[k].values() if isinstance(s, str)]
        ranked = [s for s in ORDER if s in seen]
        parts.append(f"{n} {ranked[0] if ranked else '알림 전'}")
    if len(names) > SHOWN:
        parts.append(f"외 {len(names) - SHOWN}개")
    runs = (_read_regular(root / RUNS, READ_CAP) or b"").count(b'"ev":"end"')
    parts.append(f"훅이 본 백테스트 {runs}회(최솟값)")
    return "blackclip: " + " · ".join(parts)


def sweep(root: Path | None, now: float | None = None) -> int:
    """하루 지난 시작 표지(`runs/open/<id>`, 일반 파일 · id 모양만)를 지운다. 링크는 따르지 않는다(_root가 폴더 링크를 막음).

    Args:
        root: _root 결과(None이면 아무것도 안 함).
        now: 지금 시각(시험).

    Returns:
        지운 수.
    """
    if root is None:
        return 0
    cut, n = (time.time() if now is None else now) - STALE_S, 0
    try:
        with os.scandir(root / OPEN) as it:
            for i, e in enumerate(it):
                if i >= SWEEP_MAX:
                    break
                if _ID.fullmatch(e.name) and e.is_file(follow_symlinks=False) and e.stat(follow_symlinks=False).st_mtime < cut:
                    os.unlink(e.path)
                    n += 1
    except OSError:
        pass
    return n


def handle(event: str, data: dict) -> dict | None:
    """이벤트 하나를 처리한다.

    Args:
        event: SessionStart · PreToolUse · PostToolUse · PostToolUseFailure.
        data: 훅 stdin JSON(깨졌으면 빈 dict).

    Returns:
        SessionStart면 stdout에 쓸 JSON, 그 밖은 None.
    """
    if event == "SessionStart":
        cwd = Path(data.get("cwd") or os.getcwd())
        root = _root(cwd)
        if root is None:  # 사용자 범위 설치라 모든 폴더에서 뜨던 첫 셋업 줄을 퀀트 폴더 · 거절 안 한 레포로만
            from blackclip import scan  # scan이 hook을 불러서 여기서만(순환 import 방지)

            if declined(repo_top(cwd)) or not scan.looks_quant(cwd):
                return None
        sweep(root)
        text = session_line(cwd)
        ctx = text if text != NO_ROOT else FIRST_CONTEXT
        return {"systemMessage": text, "hookSpecificOutput": {"hookEventName": "SessionStart", "additionalContext": ctx}}
    if event == "PreToolUse":
        on_pre(data)
    elif event in ("PostToolUse", "PostToolUseFailure"):
        if data.get("tool_name") in EDIT_TOOLS:
            if event == "PostToolUse":  # 실패한 편집은 파일이 그대로라 적지 않음(hooks.json과 상관없이)
                on_edit(data)
        else:
            on_end(data, event == "PostToolUseFailure")
    return None


def _out_of_time(signum: int, frame: object) -> None:
    """SIGALRM 처리: 시간 제한 예외를 던진다.

    Args:
        signum: 신호 번호.
        frame: 실행 중이던 프레임.

    Raises:
        _Budget: 늘.
    """
    raise _Budget


def run_hook(event: str) -> int:
    """stdin JSON을 읽어 처리한다. 무엇이 깨져도, BUDGET_S가 지나도 종료 0(fail-open).

    Args:
        event: 훅 이벤트 이름.

    Returns:
        늘 0.
    """
    old = None
    try:
        try:
            old = signal.signal(signal.SIGALRM, _out_of_time)  # shortcut: 윈도에는 SIGALRM이 없다(그 OS에선 아무것도 안 하고 0), 윈도 고객이 생기면 스레드 타이머로
            signal.setitimer(signal.ITIMER_REAL, BUDGET_S)
            try:
                data = json.loads(sys.stdin.buffer.read() or b"{}")
            except ValueError:
                data = {}
            out = handle(event, data if isinstance(data, dict) else {})
            if out is not None:
                sys.stdout.buffer.write((json.dumps(out, ensure_ascii=False) + "\n").encode())
                sys.stdout.buffer.flush()
        finally:
            try:
                if old is not None:
                    signal.signal(signal.SIGALRM, signal.SIG_IGN)  # 해제하는 사이 알람이 와도 무시(그 뒤 앞 처리기로)
                signal.setitimer(signal.ITIMER_REAL, 0)  # 바깥 try 안: setitimer가 없어도 새지 않음
            finally:
                if old is not None:  # 앞 처리기로 되돌림(같은 프로세스의 시험 · 코드가 _Budget을 맞지 않게)
                    signal.signal(signal.SIGALRM, old)
    except (Exception, _Budget):  # noqa: BLE001 — 훅이 고장 나도 고객 명령은 돈다
        pass
    return 0

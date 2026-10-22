"""공개 스냅숏(`git archive`)에 나가는 글에 내부 표지가 없고, 표지를 지운 .py는 코드가 그대로다.

본다: .py의 주석과 docstring, tests 밖 .py의 문자열(통째로 점검 키 `KEY` 꼴인 문자열은 뺌), .py가 아닌 글 파일 전체(커밋 해시 포함,
워크플로의 공개 액션 SHA 고정 줄만 뺌).
건너뛴다: 식별자, tests의 문자열(시험 데이터 · 검사기의 패턴과 잡아야 할 예시), 점검 키, 바이너리 파일. 이것들을 바꾸면 코드가 바뀌어
아래 AST 대조가 깨진다. 건너뛴 수는 한 줄로 찍는다(실패 조건 아님): `uv run pytest tests/test_public_tree.py -rP`.

AST 대조는 기준 커밋을 줄 때만 돈다: `BLACKCLIP_AST_BASE=<기준 sha> uv run pytest tests/test_public_tree.py -rP`.
"""
import ast
import io
import os
import re
import subprocess
import tokenize
from collections import Counter
from pathlib import Path

import pytest
from test_customer_copy import _A, _Z, KEY, MARKERS

ROOT = Path(__file__).resolve().parent.parent

CHECKER = "tests/test_customer_copy.py"
"""표지 검사기 파일. 패턴 정의와 잡아야 할 예시는 문자열이라 건너뛰고(따로 셈), 주석 · docstring은 다른 파일처럼 본다."""

HASH = rf"(?<!#){_A}(?=[0-9a-f]*[a-f])(?=[0-9a-f]*[0-9])[0-9a-f]{{7,40}}{_Z}"
"""비공개 커밋 해시: 글자와 숫자가 섞인 7~40자 hex(색 `#…`은 뺌). .py 주석 · docstring과 .py가 아닌 글 파일에서 본다."""

PROSE = re.compile(rf"{_A}(?:M-?|L)[0-9]+{_Z}|(?<!noqa: )(?<![%#]){_A}[AB][0-9]+{_Z}|(?<![-%#0-9A-Fa-fux])[0-9]{{1,3}}[a-c]{_Z}"
                   rf"|{HASH}|고침 [0-9]+|{_A}V5[A-Z][A-Za-z]+|{_A}m[0-9]{{2}}{_Z}|[0-9]+판 [0-9]+-[0-9]+"
                   rf"|{_A}201(?: (?:토큰|값)|에 없는)")
""".py 주석 · docstring에서만 보는 표지: 설계 · 검수 번호 M · L(다른 파일 줄) · A · B · m과 두 자리, 묶음 번호, 비공개 커밋 해시, 고침 회차,
캔버스 이름(V5 뒤 영문), 계획서 판 · 항목 번호(숫자 + 판 + 숫자-숫자), 디자인 레포 번호(번호 뒤 토큰 · 값 · 「에 없는」, 레포 · DESIGN은 MARKERS).
화면 HTML의 SVG 경로 명령(`M` · `L` 뒤 좌표) · 색(`#B4`) · 퍼센트 인코딩(`%2e`) · ruff 코드(`noqa: A002`)와 겹쳐 MARKERS에는 넣지 않는다.
맨 `2-2` 꼴 항목 번호는 날짜(`09-14`)와 꼴이 같아 넣지 않는다."""

PROSE_MARKERS = re.compile(f"{MARKERS.pattern}|{PROSE.pattern}")
"""주석 · docstring에 거는 표지 전체."""

TEXT_MARKERS = re.compile(f"{MARKERS.pattern}|{HASH}")
""".py가 아닌 글 파일에 거는 표지."""

PINNED = re.compile(r"^[ \t]*(?:- )?uses: [\w.-]+/[\w./-]+@[0-9a-f]{40}(?:[ \t]+# v[0-9.]+)?[ \t]*$", re.MULTILINE)
"""워크플로에서 공개 외부 액션을 커밋 SHA로 고정한 줄(공급망 고정, 해시 표지의 유일한 예외)."""

_NOQA = re.compile(rf"noqa:[ \t]*[A-Z]+[0-9]+{_Z}(?:[ \t]*,[ \t]*[A-Z]+[0-9]+{_Z})*")
"""주석의 ruff 지시문 코드(여럿 · 붙여 씀도). 글이 아니라 떼고 본다. 코드마다 끝을 막아, 코드 꼴 뒤에 글자가 붙은 표지는 남긴다."""

_KEY_STR = re.compile(rf"[rRuU]?(['\"])(?:{KEY.pattern})\1")
"""통째로 점검 키인 문자열 토큰(따옴표 포함). bytes · f-string은 접두사가 달라 걸리지 않는다."""

_FSTRING = re.compile(r"[A-Za-z]*[fF][A-Za-z]*['\"]")
"""f-string 토큰의 머리(접두사와 따옴표). 파이썬 3.11은 f-string을 STRING 토큰 하나로 준다."""

_STRINGS = {tokenize.STRING, *(getattr(tokenize, n) for n in ("FSTRING_MIDDLE", "TSTRING_MIDDLE") if hasattr(tokenize, n))}
"""문자열 몸통을 담는 토큰 종류(파이썬 3.12부터 f-string은 조각으로 나뉨)."""


def _git(*args: str, stdin: bytes | None = None) -> bytes:
    """레포 꼭대기에서 git을 돌린다.

    Args:
        args: git 인자.
        stdin: 표준 입력으로 넘길 바이트.

    Returns:
        표준 출력 바이트.
    """
    return subprocess.run(["git", "-C", str(ROOT), *args], input=stdin, capture_output=True, check=True).stdout


def _exported() -> list[str]:
    """`git archive`에 들어갈 파일: 추적 중이고 `export-ignore`가 아닌 것.

    Returns:
        레포 기준 경로 목록.
    """
    files = [p for p in _git("ls-files", "-z").decode().split("\0") if p]
    dirs = sorted({str(d) for p in files for d in Path(p).parents if str(d) != "."})
    out = _git("check-attr", "-z", "--stdin", "export-ignore", stdin="\0".join(files + dirs).encode()).decode().split("\0")
    ignored = {out[i] for i in range(0, len(out) - 2, 3) if out[i + 2] == "set"}  # 경로 · 속성 · 값이 한 묶음
    # 폴더에 건 속성은 안 파일에 퍼지지 않지만 git archive는 그 폴더째 뺀다
    return [p for p in files if p not in ignored and not any(str(d) in ignored for d in Path(p).parents)]


def _alone(tree: ast.AST) -> list[tuple]:
    """홀로 선 문자열 식 문장(모듈 · 클래스 · 함수 · 속성 docstring)의 자리.

    Args:
        tree: 파싱한 모듈.

    Returns:
        (시작 줄, 시작 칸, 끝 줄, 끝 칸) 목록.
    """
    return [(n.lineno, n.col_offset, n.end_lineno, n.end_col_offset) for n in ast.walk(tree)
            if isinstance(n, ast.Expr) and isinstance(n.value, ast.Constant) and isinstance(n.value.value, str)]


def _hits(text: str, where: str, first: int, rx: re.Pattern = MARKERS) -> list[str]:
    """글 조각에서 표지가 있는 줄을 `경로:줄: [표지] 앞뒤 40자`로 모은다.

    Args:
        text: 검사할 글.
        where: 파일 경로.
        first: 글 첫 줄의 파일 안 줄 번호.
        rx: 거는 표지.

    Returns:
        걸린 줄 목록.
    """
    return [f"{where}:{first + i}: [{m.group(0)}] {line[max(0, m.start() - 40):m.end() + 40].strip()!r}"
            for i, line in enumerate(text.splitlines()) for m in [rx.search(line)] if m]


def _python(text: str, rel: str, skipped: Counter) -> list[str]:
    """.py 한 파일: 사람이 읽는 글(주석 · docstring · tests 밖 문자열)만 보고, 나머지는 갈래별로 센다.

    Args:
        text: 파일 원문.
        rel: 레포 기준 경로.
        skipped: 건너뛴 표지 수를 더할 곳.

    Returns:
        걸린 줄 목록.
    """
    alone, lines = _alone(ast.parse(text)), io.StringIO(text).readlines()
    hits: list[str] = []
    for tok in tokenize.generate_tokens(io.StringIO(text).readline):
        row, col = tok.start
        at = (row, len(lines[row - 1][:col].encode())) if row <= len(lines) else tok.start  # ast 칸은 UTF-8 바이트, tokenize 칸은 글자
        prose = tok.type == tokenize.COMMENT or (tok.type in _STRINGS and any((a, b) <= at < (c, d) for a, b, c, d in alone))
        body = _NOQA.sub("", tok.string) if tok.type == tokenize.COMMENT else tok.string
        if tok.type == tokenize.STRING and _FSTRING.match(tok.string):  # 3.11: 3.12+의 FSTRING_MIDDLE처럼 글 조각만 본다
            body = "".join(v.value for v in ast.parse(tok.string, mode="eval").body.values if isinstance(v, ast.Constant))
        found = len((PROSE_MARKERS if prose else MARKERS).findall(body))
        if not found:
            continue
        if prose:
            hits += _hits(body, rel, tok.start[0], PROSE_MARKERS)
        elif tok.type == tokenize.NAME:
            skipped["식별자"] += found
        elif tok.type not in _STRINGS:
            skipped["그 밖의 코드"] += found
        elif tok.type == tokenize.STRING and _KEY_STR.fullmatch(tok.string):  # 값을 풀지 않음: 3.11은 f-string도 STRING 토큰 하나
            skipped["점검 키"] += found
        elif rel == CHECKER:
            skipped["검사기 패턴 · 예시 문자열"] += found
        elif rel.startswith("tests/"):
            skipped["tests 문자열"] += found
        else:
            hits += _hits(body, rel, tok.start[0])
    return hits


def _text(text: str, rel: str) -> list[str]:
    """.py가 아닌 글 파일 한 개: 표지와 커밋 해시를 본다. `.github/workflows/*.yml`의 공개 액션 SHA 고정 줄만 뺀다.

    Args:
        text: 파일 원문.
        rel: 레포 기준 경로.

    Returns:
        걸린 줄 목록.
    """
    if os.path.dirname(rel) == ".github/workflows" and rel.endswith(".yml"):
        text = PINNED.sub("", text)  # 줄바꿈은 남겨 줄 번호를 지킨다
    return _hits(text, rel, 1, TEXT_MARKERS)


def test_public_tree_prose_has_no_internal_markers():
    """공개 스냅숏의 사람이 읽는 글에 내부 표지가 0이다. 건너뛴 것은 갈래별 수로 한 줄 찍는다."""
    hits: list[str] = []
    skipped: Counter = Counter()
    for rel in _exported():
        try:
            text = (ROOT / rel).read_text(encoding="utf-8")
        except UnicodeDecodeError:
            skipped["바이너리 파일"] += 1
            continue
        if rel.endswith(".py"):
            hits += _python(text, rel, skipped)
        else:
            hits += _text(text, rel)
    print("건너뜀(실패 조건 아님): " + " · ".join(f"{k} {v}" for k, v in sorted(skipped.items())))
    assert hits == [], f"{len(hits)}줄\n" + "\n".join(hits)



@pytest.mark.parametrize(("src", "rel"), [
    ('def 검수(): """Ruling 777"""\n', "tests/probe.py"),  # 한글 이름 뒤 docstring(같은 줄)도 docstring으로
    ('def 한글(): """C1"""\n', "src/probe.py"),  # docstring이 키 꼴이어도 글
    ('label = "의심"\nmessage = f"C1{label}"\n', "src/probe.py"),  # f-string 조각이 키 꼴이어도 키가 아님(완성 글은 조각과 다름)
])
def test_python_prose_is_read_wherever_it_sits(src, rel):
    """docstring 자리는 글자 칸으로 견주고(앞에 한글이 있어도), 점검 키 예외는 통째 문자열에만 건다."""
    assert len(_python(src, rel, Counter())) == 1


@pytest.mark.parametrize(("src", "n"), [
    ("x = 1  # noqa: A002, B003\n", 0), ("x = 1  # noqa:A002\n", 0),  # ruff 지시문 코드는 여럿 · 붙여 써도 글이 아님
    ("x = 1  # noqa: E501 (I4)\n", 1),  # 지시문 뒤 글은 그대로 봄
    ("x = 1  # noqa: E501, S14A\n", 1),  # 코드 끝을 막아 붙은 표지는 남김
    ('x = b"I1"\n', 1),  # bytes는 점검 키 판정에서 예외 없이 지나가 tests 밖 문자열로 걸림
    ("x = 'C2'\n", 0),  # 통째 점검 키는 건너뜀
    ("x = f\"{LABELS['C0']} 점검\"\n", 0), ('x = f"I1 {y}"\n', 1),  # f-string은 판과 상관없이 글 조각만: 안쪽 키는 건너뛰고 글은 봄
])
def test_python_reads_comments_around_ruff_codes_and_bytes(src, n):
    """주석에서 ruff 지시문 코드만 떼고, bytes 문자열도 점검 키 판정을 지나간다."""
    assert len(_python(src, "src/probe.py", Counter())) == n


def test_prose_markers_split_internal_numbers_from_product_text():
    """주석 · docstring 전용 표지: 설계 · 검수 번호 · 다른 파일 줄 · 묶음 번호 · 커밋 해시는 잡고, ruff 코드 · 퍼센트 인코딩 ·
    색 · SVG 경로 · 해시 정규식은 잡지 않는다."""
    for leak in ("(모르면 모름, M2)", "검수 M-2", "('http/server.py' L401)", "(120a)", "131b · 131c", "817694e부터",
                 "A3(WCAG 3.3.7)", "(반복 B1)", "(최종 고침 9)", "고침 1회차", "(캔버스 V5Retro)", "201 토큰", "201에 없는 값",
                 "(갈래 없음, m18)", "(4판 3-2)", "커밋 3d3c42e5aac5ba805825da76410c181273ba90b1"):
        assert PROSE.search(leak), leak
    for word in ("# noqa: A002", "%ED%95%9C%EA%B8%80", "'%2e%2e'", "#b4b4be80", r'r"[0-9a-f]{40}"',
                 "UTF-8", ".2f", "1h", "고침 없음", "V5 판", r"\u001b", r"\x1b", "#2010ff", "1201", "2,201건",
                 "# HTTP 201 Created", "HTTP 201 응답", "m3", "--window 15m", "(09-14)", "10-11 기록"):
        assert not PROSE.search(word), word


def test_text_files_skip_only_pinned_workflow_actions():
    """글 파일의 커밋 해시는 잡고, 워크플로에서 공개 액션을 SHA로 고정한 줄만 뺀다."""
    pin = "      - uses: actions/checkout@3d3c42e5aac5ba805825da76410c181273ba90b1  # v7.0.1\n"
    assert _text(pin, ".github/workflows/publish.yml") == []
    assert len(_text(pin, "README.md")) == 1  # 워크플로 밖
    assert len(_text("# 3d3c42e5aac5ba805825da76410c181273ba90b1부터\n", ".github/workflows/publish.yml")) == 1  # 고정 줄 꼴이 아님

def _code(src: str) -> str:
    """주석과 홀로 선 문자열 식 문장을 뺀 AST 덤프(줄 번호 없음). 그런 문장은 실행 결과를 바꾸지 않는다.

    Args:
        src: 파이썬 원문.

    Returns:
        `ast.dump` 글.
    """
    tree = ast.parse(src)
    for n in ast.walk(tree):
        for field in ("body", "orelse", "finalbody"):
            body = getattr(n, field, None)
            if isinstance(body, list):
                body[:] = [s for s in body if not (isinstance(s, ast.Expr) and isinstance(s.value, ast.Constant)
                                                   and isinstance(s.value.value, str))]
    return ast.dump(tree)


@pytest.mark.skipif(not os.environ.get("BLACKCLIP_AST_BASE"), reason="BLACKCLIP_AST_BASE=<기준 sha>일 때만 돎(PR 때 대조)")
def test_changed_python_keeps_its_code():
    """기준에서 갈라진 뒤 고친 .py마다, 주석과 docstring을 뺀 AST가 기준과 같다(동작 변경 0)."""
    base = _git("merge-base", os.environ["BLACKCLIP_AST_BASE"], "HEAD").decode().strip()
    changed = _git("diff", "--name-only", "--diff-filter=M", base, "HEAD", "--", "*.py").decode().split()
    differ = [p for p in changed if _code(_git("show", f"{base}:{p}").decode()) != _code(_git("show", f"HEAD:{p}").decode())]
    print(f"AST 대조: 기준 {base[:7]} · 고친 .py {len(changed)}개 · 코드가 다른 파일 {len(differ)}개")
    assert changed and differ == [], differ

"""고객이 보는 글(화면 · 터미널 · README · 스킬)에 내부 표지가 없다(공개 준비 수용 기준).

내부 표지: 판정 번호 · 설계 결정 번호(`D`와 숫자) · 점검 번호(`C`와 숫자 하나, 고객에게는 이름만) · 카드 번호 · 검수 이름 · 계획 번호(`R`과 숫자-숫자) · 스프린트 번호(`S`와 숫자). 정확한 꼴은 MARKERS.
src는 문자열 상수만 본다. 문장으로 홀로 선 문자열(모듈 · 함수 · 클래스 · 속성 docstring)은 개발자 글이라 뺀다.

만든 파일(공개 데모 HTML 등)도 같은 검사로 본다: `uv run python tests/test_customer_copy.py <파일> …`(표지가 있으면 종료 1).

알려진 한계: 트리 검사(`tests/test_public_tree.py`)는 docstring을 토큰마다 보므로, 붙여 쓴 문자열 조각으로 쪼갠 표지는 놓친다(지금 트리 0건).
"""
import ast
import re
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent

_A = r"(?<![A-Za-z0-9])"
_Z = r"(?![A-Za-z0-9])"
_REVIEW = r"I-?R?|R|X|N|P|FE|FI|FX|PFB|PFC|C-R|O-[IM]|[CDO]-"
"""검수 · 설계 번호의 머리. 뒤에 숫자가 붙어야 표지다."""

_STD = "".join(rf"(?<!{n} [0-9.]{{{k}}} )" for n in ("RFC", "PEP", "WCAG", "ISO") for k in range(1, 7))
"""외부 표준 이름 + 번호(6글자까지) 바로 뒤의 §는 표지가 아니다(파이썬 뒤보기는 길이가 정해져야 해서 길이마다 하나씩)."""

MARKERS = re.compile(rf"Ruling|{_A}D[0-9]+{_Z}|{_A}C[0-9]{_Z}|{_A}S[0-9]+[A-Za-z]?{_Z}|CMP-|이사회|(?<!MM[-/]){_A}DD{_Z}"
                     rf"|{_A}R[0-9]+-[0-9]+{_Z}|Paperclip|(?<!#){_A}(?:{_REVIEW})[0-9]+{_Z}"
                     rf"|{_A}Codex{_Z}|{_A}(?:Task|Focus|Minor|Important|Step) [0-9]|Santa|작업 [0-9]|설계서|최종 검수|교차 검수|[0-9]차 (?:I[0-9]|검수)"
                     rf"|docs-facts|201 (?:레포|DESIGN)|{_STD}§[0-9]")
"""내부 표지(판정 · 설계 · 점검 · 스프린트 · 계획 · 검수 번호, 내부 도구 · 문서 이름). `(?<!#)`는 색 값(`#FE12`)을, `RFC · PEP · WCAG · ISO 번호 §`는 외부 표준 절을 뺀다(`_STD`). 경계는 ASCII 기준이다(`\\b`는 점검 키 뒤에 한글 조사가 붙으면 경계를 못 봄). `(?<!MM[-/])`는 날짜 형식 표기(`YYYY-MM-DD` · `MM/DD`)의 일 자리를 뺀다."""

KEY = re.compile(r"C[0-4]")
"""점검 항목의 코드 안 키(첫 키 꼴 등). 글 전체가 이 꼴이면 화면 글이 아니라 키다."""


def _src_strings():
    """src의 (파일, 줄, 글): docstring처럼 문장으로 홀로 선 문자열과 맨 키는 뺀다."""
    for f in sorted((ROOT / "src" / "blackclip").rglob("*.py")):
        tree = ast.parse(f.read_text(encoding="utf-8"))
        alone = {id(n.value) for n in ast.walk(tree) if isinstance(n, ast.Expr) and isinstance(n.value, ast.Constant)}
        for n in ast.walk(tree):
            if isinstance(n, ast.Constant) and isinstance(n.value, str) and id(n) not in alone and not KEY.fullmatch(n.value):
                yield f.relative_to(ROOT), n.lineno, n.value


def marker_hits(text: str, where: object = "") -> list[str]:
    """글에서 내부 표지가 있는 줄을 모은다. 만든 HTML은 원문 그대로 넣는다(CSS 주석 · 속성도 소스 보기로 고객에게 보임).

    Args:
        text: 검사할 글(README · 터미널 출력 · 렌더한 HTML 등).
        where: 결과 줄 앞에 붙일 이름(파일 경로 등).

    Returns:
        `이름:줄: [표지] 표지 앞뒤 40자` 목록, 없으면 빈 목록.
    """
    return [f"{where}:{i}: [{m.group(0)}] {line[max(0, m.start() - 40):m.end() + 40].strip()!r}"
            for i, line in enumerate(text.splitlines(), 1) for m in [MARKERS.search(line)] if m]


def test_no_internal_markers():
    """고객 글에 내부 표지가 0개다. 실패하면 어디에 무엇이 남았는지 줄마다 보인다."""
    docs = (ROOT / "README.md", ROOT / "plugin" / "skills" / "blackclip" / "SKILL.md", ROOT / "CHANGELOG.md",
            ROOT / ".claude-plugin" / "marketplace.json", ROOT / "plugin" / ".claude-plugin" / "plugin.json",
            ROOT / "pyproject.toml")  # 공개 레포 루트 · 마켓 · PyPI에서 바로 보이는 글
    hits = [*(h for f, n, s in _src_strings() for h in marker_hits(s, f"{f}:{n}")),
            *(h for f in docs for h in marker_hits(f.read_text(encoding="utf-8"), f.relative_to(ROOT)))]
    assert hits == [], "\n".join(hits)


def test_store_descriptions_use_the_readme_words_and_say_macos():
    """마켓 · 플러그인 · 패키지 설명이 README 첫 문장의 말로 전략을 부르고 macOS 전용임을 밝힌다(예약이 launchd라 다른 운영체제에서는 안 돎)."""
    import json
    import tomllib
    term = "AI와 함께 만든 매매 전략"
    assert term in (ROOT / "README.md").read_text(encoding="utf-8").splitlines()[2]
    market = json.loads((ROOT / ".claude-plugin" / "marketplace.json").read_text(encoding="utf-8"))
    descs = [market["description"], *(p["description"] for p in market["plugins"]),
             json.loads((ROOT / "plugin" / ".claude-plugin" / "plugin.json").read_text(encoding="utf-8"))["description"],
             tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))["project"]["description"]]
    assert [(term in d, "macOS)" in d) for d in descs] == [(True, True)] * 4, descs


def test_marker_pattern_catches_known_leaks():
    """정규식이 실제로 새던 꼴을 잡고, 날짜 꼴 · 맨 키는 잡지 않는다(시험이 헛돌지 않게)."""
    for leak in ("텔레그램 봇은 직접 만들어요(Ruling 117)", "갈래(D81): 있음", "C0 재현 확인 전", "C2 거래비용", "R13-5",  # 한 예시에 갈래 하나
                 "C0가 불일치라", "점검 불가(C3를 셀 수 없음)", "## 대시보드 (S12)", "체결 받기 (S3)", "(I-R4)", "검수 I-1", "(I4)",
                 "R25", "최종 검수", "교차 검수", "S9a", "S14A", "(X8)", "(N13)", "(O-M12)", "C-R1", "FE3 부록", "PFB1", "Santa",
                 "Codex 7", "(Codex 재현)", "Codex Important", "Task 5", "Review Focus 3", "작업 4a", "설계서", "§9", "2차 검수",
                 "docs-facts", "201 DESIGN.md", "(§11 이름 그대로)", "v4 §6"):
        assert MARKERS.search(leak), leak
    assert not MARKERS.search("실전 시작일 YYYY-MM-DD") and KEY.fullmatch("C3")
    for word in ("UTF-8", "--window 1d", "RFC 9110 §15.5.6", "PEP 440 §3", "WCAG 2.2 §1.4.3", "ISO 8601 §4.1", "WCAG 3.3.7", "PEP 440", "BTC/USDT:USDT-241227", "#FE12A0",
                 "6단계 전략 연결", "smoke", "FSL-1.1-ALv2", "Claude Code 2.1.275", "SHA-256", "ISO 8601", "HTTP 400",
                 '<path d="M204 22L208 27H200Z"/>'):  # 끝 줄: 공개 데모 홈의 실제 SVG 조각(M · L 명령)
        assert not MARKERS.search(word), word  # 제품 말 · 외부 표준 · 색 값 · SVG 경로


def test_date_formats_are_not_markers():
    """날짜 형식 표기(`MM/DD` · `YYYY-MM-DD`)의 일 자리는 표지가 아니고, 홀로 선 두 글자 검토 표지는 그대로 잡는다."""
    for date in ("체결 MM/DD분까지 받음", "`MM/DD 은퇴 · 고객 결정 · 이유: …`", "- 전략(MM/DD 상태)", "실전 시작일 YYYY-MM-DD"):
        assert not MARKERS.search(date), date
    for leak in ("DD 고침 2", "(DD R3: 조회 키 패널에서 내렸다)", "DD D2: 어느 대응이"):
        assert MARKERS.findall(leak)[:1] == ["DD"], leak


def test_marker_hits_on_built_html():
    """만든 HTML(공개 데모 실측 꼴): CSS 주석 · 칸 글의 표지를 줄 번호와 함께 잡고, 색 · 날짜 · SVG 경로는 잡지 않는다."""
    page = ('<style>.cmd{color:#D0E1F2} /* 고정폭 예외(Ruling 215) */</style>\n'
            '<li class="one">C0 재현 확인 전</li><svg><path d="M0 0C10 20 30 40"/></svg>\n<p>2026-10-14 · 3d</p>')
    assert marker_hits(page, "demo/index.html") == [
        "demo/index.html:1: [Ruling] '<style>.cmd{color:#D0E1F2} /* 고정폭 예외(Ruling 215) */</style>'",
        "demo/index.html:2: [C0] '<li class=\"one\">C0 재현 확인 전</li><svg><path d=\"M0 0C10 20 30'"]
    long = "x" * 200 + "시도 수 보정(C3)은" + "y" * 200
    assert marker_hits(long, "f") == ["f:1: [C3] '" + "x" * 32 + "시도 수 보정(C3)은" + "y" * 38 + "'"]


if __name__ == "__main__":
    import sys
    found = [h for p in sys.argv[1:] for h in marker_hits(Path(p).read_text(encoding="utf-8"), p)]
    print("\n".join(found) or f"표지 0 · 파일 {len(sys.argv) - 1}개")
    sys.exit(1 if found else 0)

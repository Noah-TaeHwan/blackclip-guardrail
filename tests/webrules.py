"""웹 화면 글 검사 도우미: 여러 웹 화면 시험이 같이 쓴다.

`BANNED`는 test_copy_rules의 것을 그대로 가져오고(바꾸지 않음), 웹에서만 더 막는 낱말을 덧붙인다.
"""
import html as _html
import re

from test_copy_rules import BANNED, EMOJI

WEB_BANNED = BANNED + ("곧", "준비 중", "미정", "검증 안 됨")
"""웹 금지 낱말: 터미널 BANNED에 「곧」 · 「준비 중」 · 「미정」 · 「검증 안 됨」을 더한 것."""

_BMP_PICTO = """
00A9 00AE 203C 2049 2122 2139 2194-2199 21A9-21AA 231A-231B 2328 2388 23CF
23E9-23F3 23F8-23FA 24C2 25AA-25AB 25B6 25C0 25FB-25FE 2600-2605 2607-2612 2614-2685 2690-2705 2708-2712
2714 2716 271D 2721 2728 2733-2734 2744 2747 274C 274E 2753-2755 2757
2763-2767 2795-2797 27A1 27B0 27BF 2934-2935 2B05-2B07 2B1B-2B1C 2B50 2B55 3030 303D
3297 3299
"""
"""Unicode UTS #51 emoji-data.txt 16.0의 Extended_Pictographic 가운데 U+FFFF 이하(16진 코드 포인트, 범위는 `-`).
그대로 옮겨 둔 것이라 고치지 않는다(새 판이 나오면 그 파일에서 다시 옮김)."""

WEB_EMOJI = re.compile(EMOJI.pattern[:-1] + "".join("-".join(chr(int(c, 16)) for c in r.split("-")) for r in _BMP_PICTO.split()) + "]")
"""웹 이모지: 터미널 EMOJI(U+1F000~U+1FAFF · U+FE0F)에 BMP 안의 그림 글자(⚠ ✅ ❌ ☀ ⭐ © 등)를 더한 것. BMP 범위는 `_BMP_PICTO`다.
웹 칩 기호는 글자가 아니라 SVG라 판정 기호 글자(`paint.SYMBOL`)에 대한 예외는 두지 않는다."""

_ARROW = re.compile(r"→(?:\s|</\w+>)*</(?:a|button)>|</(?:a|button)>\s*→")
"""버튼 · 링크 글 뒤의 화살표 `→`: 링크 · 버튼 글 끝에 있거나 닫는 태그 바로 뒤에 있는 것."""

_STYLE = re.compile(r"<style\b.*?</style>", re.S)
_ID = re.compile(r"""<(\w+)\b[^>]*?(?<![\w-])class=(?:"(?:[^"]*\s)?id(?:\s[^"]*)?"|'(?:[^']*\s)?id(?:\s[^']*)?')[^>]*>.*?</\1>""", re.S)
"""`class` 낱말이 정확히 `id`인 요소(`web.ident`가 감싼 식별자: 키 · 토큰 끝 네 자리 등). 수량이 아니라 쉼표를 넣을 수 없어 숫자 규칙에서만 뺀다."""


def copy_hits(html: str) -> list[str]:
    """렌더한 HTML에서 화면 글 규칙을 어긴 낱말 · 기호를 모은다.

    `<style>` 블록은 보이는 글이 아니므로(예: CSS의 `width:100%`) 뺀다. 태그 속성(`aria-label` · `title`)은 그대로 검사한다.

    Args:
        html: 페이지 HTML 문자열.

    Returns:
        걸린 것(웹 금지 낱말 · 가운뎃점 `·` · 긴 대시 `—` · 이모지 `WEB_EMOJI`). 없으면 빈 목록.
    """
    body = _STYLE.sub("", html)
    return [w for w in WEB_BANNED if w in body] + [c for c in ("·", "—") if c in body] + WEB_EMOJI.findall(body)


_QUOTE = re.compile(r"""<(\w+)\b[^>]*?(?<![\w-])class=(?:"(?:[^"]*\s)?quote(?:\s[^"]*)?"|'(?:[^']*\s)?quote(?:\s[^']*)?')[^>]*>.*?</\1>""", re.S)
_TAG = re.compile(r"<[^>]*>")
_ATTR = re.compile(r"""(?<![\w-])(?:aria-label|aria-valuetext|alt|title)=(?:"([^"]*)"|'([^']*)')""")
_DATE = re.compile(r"\d{4}-\d{2}-\d{2}")
_MD = re.compile(r"(?<!\d)\d{1,2}/\d{1,2}(?!\d)")
_BIG = re.compile(r"(?<![\d,.])\d{4,}(?!\d|,|bp)")
_BT = re.compile(r"(?<![A-Za-z])BT(?![A-Za-z])")


def visible_text(html: str) -> str:
    """렌더한 HTML에서 사람이 읽는 글만: `<style>`과 `class="quote"` 요소(레포 고객이 쓴 인용)를 빼고, 태그를 벗기고(`<title>` 글은 남음),
    `aria-label` · `aria-valuetext` · `alt` · `title` 속성 값(큰따옴표 · 작은따옴표)을 덧붙인다(속성은 읽어 주는 글이라 같은 규칙).
    인용 제외는 `class` 낱말이 정확히 `quote`인 요소만이다(`no-quote` · `quote-x`는 제외하지 않음).

    Args:
        html: 페이지 HTML 문자열.

    Returns:
        entity를 푼 글(href의 접속 토큰 같은 다른 속성 값은 들지 않음).
    """
    body = _QUOTE.sub(" ", _STYLE.sub("", html))
    return _html.unescape(" ".join([_TAG.sub(" ", body), *(a or b for a, b in _ATTR.findall(body))]))


def copy_violations(html: str) -> list[str]:
    """보이는 글(`visible_text`)이 문구 규칙을 어긴 곳을 모은다. 홈과 가드레일 화면이 같이 쓴다.

    잡는 것: 웹 금지 낱말 · 가운뎃점 `·` · 긴 대시 `—` · 합쇼체 `니다` · 이모지(`WEB_EMOJI`, BMP 그림 글자 포함) · 링크 · 버튼 글 뒤 `→` · `M/D` 날짜 · 쉼표 없는 네 자리 이상 정수(날 `YYYY-MM-DD`와
    뒤에 `bp`가 붙은 수는 빼며, 연도는 날 안에서만 나온다) · 약어 `BT`(`BTCUSDT` 같은 영문 낱말 안은 아님).
    `class="id"` 요소(`web.ident`: 키 · 토큰 끝 네 자리 같은 식별자) 안 글은 숫자 규칙(`M/D` · 큰 수)에서만 빠지고 나머지 규칙은 그대로 받는다.

    Args:
        html: 페이지 HTML 문자열.

    Returns:
        걸린 것(규칙 이름과 걸린 글 조각). 없으면 빈 목록.
    """
    text = visible_text(html)
    undated = _DATE.sub(" ", visible_text(_ID.sub(" ", html)))  # 식별자(class="id")는 숫자 규칙(큰 수 · M/D)에서만 빠진다
    return ([w for w in WEB_BANNED if w in text] + [c for c in ("·", "—", "니다") if c in text] + WEB_EMOJI.findall(text)
            + ["링크 · 버튼 뒤 →"] * len(_ARROW.findall(_QUOTE.sub(" ", _STYLE.sub("", html))))
            + [f"M/D {m}" for m in _MD.findall(undated)] + [f"쉼표 없는 큰 수 {m}" for m in _BIG.findall(undated)]
            + ["BT"] * len(_BT.findall(text)))

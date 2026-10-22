"""대시보드 화면 틀: 디자인 토큰 · CSS · 판정 칩 · 글 도우미 · 페이지 틀 · 홈 · 가드레일 화면(차트 A · E · 원인 셋) · 회고 화면.

모두 순수 함수다: 파일을 읽지 않고, 서버도 모르고, 숫자와 상태는 호출한 쪽(서버)이 채워 넘긴다.
여기서 하는 일은 넘겨받은 값을 글 규칙(해요체 · 가운뎃점 없음 · 천 단위 쉼표 · 엔진과 같은 자릿수)대로 HTML로 옮기는 것뿐이다.
레포에서 온 글(전략 이름 · 이유 · 날)은 모두 `text`를 거쳐 escape한다. 색은 `:root` 한 곳에만 있고 나머지는 `var()` 클래스다.
"""
from __future__ import annotations

import html
import math
import posixpath
import re
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from datetime import date, timedelta
from typing import Any
from urllib.parse import quote

from blackclip.daily.days import TRADE_KEEP
from blackclip.daily.reconcile import RESPONSE_COL, triggered
from blackclip.hook import _NAME
from blackclip.keystore import FileStore, Keychain
from blackclip.record import BROKEN, C0_WORDS
from blackclip.screen.data import _utc
from blackclip.screen.status import plain
from blackclip.states import base, representative
from blackclip.workspace import WorkspaceError, _day

TOKENS: dict[str, str] = {
    "canvas": "#030304", "ink": "#F4F4F6", "muted": "#B4B4BE", "surface": "#111113", "surface-2": "#1B1B1F",
    "evidence": "#93A5FF", "evidence-wash": "rgb(147 165 255 / .16)", "evidence-line": "rgb(147 165 255 / .50)",
    "caution": "#F0B45C", "control-border": "rgb(255 255 255 / .40)", "hairline-2": "rgb(255 255 255 / .12)",
    "fill-soft": "rgb(255 255 255 / .06)", "focus": "rgb(255 255 255 / .82)", "selection": "rgb(147 165 255 / .32)",
}
"""색 토큰. 이름은 `--studio-color-{이름}`으로 나간다. 색 값을 바꿀 땐 여기만 바꾼다."""

FONT = '"Wanted Sans","Apple SD Gothic Neo",system-ui,sans-serif'
"""글꼴 스택(파일 반입 없음)."""

MONO = 'ui-monospace,"SF Mono",Menlo,Consolas,monospace'
"""명령 글자(`.cmd`)의 고정폭 글꼴 스택(파일 반입 없음)."""

SPACE: tuple[int, ...] = (4, 8, 12, 16, 24, 32, 44)
"""간격 `--studio-space-1`~`-7`(px)."""

RADIUS: dict[str, int] = {"chip": 6, "row": 12, "card": 16, "panel": 24, "pill": 999}
"""반경 `--studio-radius-{이름}`(px)."""

CAUSE: dict[str, str] = {"전략": "전략 쪽", "실행": "실행 문제로 추정", "봇": "봇과 체결 받기", "선": "경고선", "논지": "전제와 안 맞는 장"}
"""화면의 원인 이름. 키는 `states.TRACKED`와 같다. `states.TITLES`와 뜻은 같고 가운뎃점만 뺐다(터미널 TITLES는 그대로)."""

EMPTY_LIVE = "실전 중이고 매일 대조 기록이 아직 없어요"
"""매일 대조 기록이 없는 실전 전략의 상태 칸 글(live.json 있음)."""

EMPTY_PRE = "실전 시작 전이에요"
"""매일 대조 기록이 없고 live.json도 없는 전략의 상태 칸 글."""

EMPTY_ALL = "아직 실전 전략이 없어요."
"""실전 전략도 끈 전략도 없을 때의 한 줄."""

CMD_EMPTY = "blackclip save <전략> 뒤 blackclip live <전략>"
"""실전 전략이 없을 때 권하는 행동 하나(명령 글자)."""

DIFF_NOTE = "차이는 비용을 0으로 둔 같은 기간 백테스트와 비교해요. 같은 기간 백테스트 칸은 보수적 비용 기준이에요."
"""홈 표 아래 보조 글: 차이 칸과 같은 기간 백테스트 칸의 비용 기준이 다름을 알린다."""

SPARK_NOTE = "흐름은 전략마다 자기 최저와 최고로 그려 모양만 비교해요."
"""홈 표 아래 보조 글: 흐름 칸은 전략마다 눈금이 달라 크기가 아니라 모양만 본다."""

FOOT = "이 화면은 이 컴퓨터에서만 열려요. 엔진은 주문을 내거나 봇을 멈추지 않아요."
"""홈 맨 아래 보조 글(서버 화면)."""

STATIC_FOOT = "이 화면은 지어낸 전략으로 그렸어요. 엔진은 주문을 내거나 봇을 멈추지 않아요."
"""홈 바닥 글(공개 샘플 데모)."""

STATIC_BANNER = "샘플 데이터로 만든 읽기 전용 화면이에요. 실제 Guardrail은 내 컴퓨터에서 돌고, 거래소 키도 내 컴퓨터에만 둬요."
"""공개 샘플 데모의 모든 페이지 맨 위 띠 글(뒤에 「BlackClip 홈」 링크)."""

BRAND = "BlackClip Guardrail"
"""내비 · 탭 제목의 화면 이름(랜딩 blackclip.app과 같은 표기)."""

AFTER_NOTE = "끈 뒤 회고가 있어요"
"""끈 전략에 after.md가 있을 때의 메모."""

BROKEN_NOTE = "전략 기록(record.md)을 읽지 못해 매일 대조가 멈췄어요"
"""기록 손상 전략의 메모."""

EMPTY_DAYS = "매일 대조 기록이 아직 없어요. 첫 대조가 끝나면 이 화면에 그려져요."
"""매일 대조 기록이 없는 전략의 가드레일 · 회고 화면 안내 한 줄(숫자 · 차트 자리)."""

BROKEN_FULL = BROKEN_NOTE + ". git 충돌이면 풀고, 아니면 record.md를 지운 뒤 blackclip record로 다시 적어 주세요."
"""기록 손상 전략의 가드레일 화면 상태 줄."""

# .cmd 고정폭 예외: 명령 글자는 복사해 그대로 쳐야 해서(공개 CSS에 주석을 싣지 않게 여기 적음)
_RULES = """\
html, html body{margin:0;background:$c-canvas;color:$c-ink;font-family:var(--studio-font-sans);font-size:1rem;line-height:1.625;font-variant-numeric:tabular-nums;word-break:keep-all;overflow-wrap:anywhere;hyphens:none;-webkit-tap-highlight-color:transparent}
[class]{color:inherit;font-family:inherit}
::selection{background:$c-selection}
:focus-visible{outline:2px solid $c-focus;outline-offset:2px}
.top{border-bottom:1px solid $c-hairline-2}
.nav,.wrap{box-sizing:border-box;max-width:1280px;margin:0 auto;padding-left:max($s4,env(safe-area-inset-left,0px));padding-right:max($s4,env(safe-area-inset-right,0px))}
.nav{display:flex;flex-wrap:wrap;align-items:center;gap:$s2;padding-top:$s2;padding-bottom:$s2}
.wrap{padding-top:$s5;padding-bottom:$s6}
.stack{display:grid;gap:$s4}
.brand{margin-right:$s3;color:$c-ink;font-size:.875rem;line-height:1.25rem;font-weight:600}
.nav-link{display:inline-flex;align-items:center;justify-content:center;min-width:44px;min-height:44px;padding:0 $s3;border-radius:$r-pill;color:$c-ink;font-size:.875rem;line-height:1.25rem;font-weight:500;text-decoration:none}
.nav-link[aria-current="page"]{color:$c-ink;font-weight:600}
.panel{container-type:inline-size;display:grid;gap:$s3;min-width:0;padding:$s4;background:$c-surface;color:$c-ink;border:1px solid $c-hairline-2;border-radius:$r-card}
.head{display:flex;flex-wrap:wrap;align-items:baseline;justify-content:space-between;gap:$s1 $s4}
.display{margin:0;color:$c-ink;font-size:1.5rem;line-height:2rem;font-weight:500;letter-spacing:-0.02em}
@media (min-width:480px){.display{font-size:1.75rem;line-height:2.25rem}}
.h2{margin:0;color:$c-ink;font-size:1rem;line-height:1.625rem;font-weight:600}
.sub{margin:0;color:$c-muted;font-size:.8125rem;line-height:1.25rem}
.empty{margin:0;color:$c-ink;font-size:1rem;line-height:1.625rem}
.cmd{justify-self:start;padding:$s1 $s3;border-radius:$r-chip;background:$c-surface-2;color:$c-ink;font-family:var(--studio-font-mono);font-size:.8125rem;line-height:1.25rem}
.scroll{min-width:0;overflow-x:auto}
.tbl{width:100%;min-width:44rem;border-collapse:collapse}
.tbl-fit{min-width:0}
.cell{padding:$s2 $s3;border-bottom:1px solid $c-hairline-2;color:$c-ink;font-size:.8125rem;line-height:1.25rem;font-weight:400;text-align:left;vertical-align:middle}
.cell-head{background:$c-surface-2;color:$c-muted;font-weight:500;white-space:nowrap}
.cell-num{text-align:right;white-space:nowrap}
.cell-name,.item-name{color:$c-ink;font-size:.875rem;line-height:1.25rem;font-weight:500}
.cell-name{white-space:nowrap}
.row:hover .cell{background:$c-fill-soft}
.link{color:$c-ink;text-decoration:underline;text-decoration-color:$c-muted;text-decoration-thickness:1px;text-underline-offset:4px}
.link:hover{text-decoration-color:$c-ink}
.hit{display:inline-flex;align-items:center;justify-content:center;min-width:44px;min-height:44px}
.actions{display:flex;flex-wrap:wrap;gap:$s2}
.btn{display:inline-flex;align-items:center;justify-content:center;min-height:44px;padding:0 $s4;border-radius:$r-pill;color:$c-ink;font-size:.875rem;line-height:1.25rem;font-weight:500;text-decoration:none;white-space:nowrap}
.btn-fill{background:$c-ink;color:$c-canvas}
.btn-quiet{background:$c-fill-soft;box-shadow:inset 0 0 0 1px $c-control-border}
.chip{display:inline-flex;align-items:center;justify-content:center;gap:4px;box-sizing:border-box;min-width:3.75rem;height:1.25rem;padding:0 7px 0 6px;border-radius:$r-chip;background:$c-surface-2;box-shadow:inset 0 0 0 1px $c-control-border;color:$c-ink;font-size:.75rem;line-height:1.25rem;font-weight:500;white-space:nowrap;vertical-align:1px}
.chip-glyph{flex:none;width:.625rem;height:.625rem}
.chip-ok{color:$c-ink}
.chip-warn{color:$c-caution;box-shadow:inset 0 0 0 2px $c-caution}
.chip-mute{color:$c-muted}
.why{margin:$s1 0 0;color:$c-muted;font-size:.8125rem;line-height:1.25rem}
.why-warn{color:$c-caution}
.meter{position:relative;box-sizing:border-box;height:8px;overflow:hidden;border:1px solid $c-control-border;border-radius:$r-pill;background:$c-surface-2}
.meter-fill{position:absolute;inset:0;background:$c-ink;transform:scaleX(var(--fill,0));transform-origin:left center}
.quote{margin:0;padding:$s3 $s4;border-radius:$r-card;background:$c-surface-2;color:$c-ink;font-size:1rem;line-height:1.625rem;white-space:pre-wrap}
.keep{white-space:pre-wrap}
.list{display:grid;gap:$s3;margin:0;padding:0;list-style:none;color:$c-ink;font-size:.875rem;line-height:1.25rem}
.rows{display:grid;margin:0;padding:0;list-style:none}
.item{display:grid;gap:$s2;padding:$s3 0;border-bottom:1px solid $c-hairline-2;color:$c-ink;font-size:.8125rem;line-height:1.25rem}
.item:last-child{border-bottom:0}
.item .why{margin:0}
.item-head{display:flex;flex-wrap:wrap;align-items:center;justify-content:space-between;gap:$s1 $s3}
.item-tail{display:flex;flex-wrap:wrap;align-items:center;gap:$s1 $s4;color:$c-muted}
.pairs{display:grid;grid-template-columns:repeat(auto-fit,minmax(6rem,1fr));gap:$s2 $s3;margin:0}
.pairs dt{color:$c-muted}
.pairs dd{margin:0;color:$c-ink}
.nw{white-space:nowrap}
.legend{display:flex;flex-wrap:wrap;gap:$s2 $s4;margin:0;padding:0;list-style:none;color:$c-muted;font-size:.8125rem;line-height:1.25rem}
.legend-item{display:inline-flex;align-items:center;gap:$s2}
.sw{flex:none;width:28px;height:12px}
.g2{display:grid;grid-template-columns:minmax(0,1fr);gap:$s3}
@media (min-width:1024px){.g2{grid-template-columns:minmax(0,1.35fr) minmax(0,1fr)}.g2.eq{grid-template-columns:repeat(2,minmax(0,1fr))}.g2.by{grid-template-areas:"cn wn" "nt nt" "cd wd"}.g2.by>:nth-child(1){grid-area:cn}.g2.by>:nth-child(2){grid-area:nt}.g2.by>:nth-child(3){grid-area:cd}.g2.by>:nth-child(4){grid-area:wn}.g2.by>:nth-child(5){grid-area:wd}}
.chart{display:block;width:100%;height:auto;min-height:180px;overflow:visible}
.fig{display:grid;grid-template-columns:3.5rem minmax(0,1fr);column-gap:$s2;color:$c-muted;font-size:.8125rem;line-height:1.25rem;container-type:inline-size}
.yax{position:relative;min-width:0}
.tick{position:absolute;right:0;transform:translateY(-50%);color:$c-muted;white-space:nowrap}
.xax{grid-column:2;display:flex;justify-content:space-between;padding-top:$s1;color:$c-muted}
.e-dots-off{display:none;grid-column:2;margin:0;padding-top:$s1}
.spark{display:block}
.spark-bar{fill:$c-ink}
.spark-warn{fill:$c-caution}
.spark-gap{fill:$c-muted}
.s-band,.s-bt,.s-actual,.s-line,.s-mean,.s-run,.s-zero,.s-start,.s-count,.s-dot,.s-missed{vector-effect:non-scaling-stroke}
.s-band{fill:$c-evidence-wash;stroke:$c-evidence;stroke-width:1}
.s-bt{fill:none;stroke:$c-evidence;stroke-width:2;stroke-dasharray:6 4}
.s-actual{fill:none;stroke:$c-ink;stroke-width:2}
.s-line{fill:none;stroke:$c-caution;stroke-width:1.5;stroke-dasharray:2 3}
.s-mean{fill:none;stroke:$c-evidence;stroke-width:1.5;stroke-dasharray:6 4}
.s-run{fill:none;stroke:$c-ink;stroke-width:1.5}
.s-zero{fill:none;stroke:$c-control-border;stroke-width:1;stroke-dasharray:4 4}
.s-start{fill:none;stroke:$c-muted;stroke-width:1;stroke-dasharray:2 3}
.s-count{fill:none;stroke:$c-muted;stroke-width:1;stroke-dasharray:4 3}
.s-dot{fill:none;stroke:$c-ink;stroke-width:$dot-max;stroke-linecap:round}
@supports (width:1cqw){.s-dot{stroke-width:clamp($dot-minpx, calc((100cqw - 3.5rem - $s2) * 708 / 720 * $dot-ratio / var(--gaps, 1)), $dot-maxpx)}}
.s-missed{fill:none;stroke:$c-caution;stroke-width:10;stroke-linecap:round}
.lesson{margin:0;color:$c-ink;font-size:.875rem;line-height:1.25rem}
.g2.eq>.panel{align-content:start}
.fig>.legend{grid-column:1/-1;margin-top:$s4}
.s-slow-sw{fill:$c-muted;fill-opacity:.55}
.s-slow{fill:url(#b-hatch)}
.s-fast{fill:$c-caution;fill-opacity:.55}
.s-hatch{fill:none;stroke:$c-canvas;stroke-width:1.5}
@media (max-width:1023.98px){.wide:not(.cq){display:none}}
@media (min-width:1024px){.narrow:not(.cq){display:none}}
@container (width < 48rem){.cq.wide{display:none}}
@container (width >= 48rem){.cq.narrow{display:none}}
@container (width < 16rem){.wide:not(.cq){display:none}.narrow:not(.cq){display:grid}}
@container (width >= calc(12rem + 12px)){.span2{grid-column:span 2}}
@container (width < 9rem){.nw{white-space:normal}}
.form{display:grid;gap:$s3;margin:0}
.field{display:grid;gap:$s1;min-width:0}
.label{color:$c-muted;font-size:.8125rem;line-height:1.25rem}
.input{box-sizing:border-box;width:100%;height:44px;margin:0;padding:0 $s3;border:0;border-radius:$r-row;background:$c-surface-2;box-shadow:inset 0 0 0 1px $c-control-border;color:$c-ink;font-size:1rem;line-height:1.625rem}
.set{display:grid;gap:$s2;min-width:0;margin:0;padding:0;border:0}
.set>.h2{padding:0;margin-bottom:$s1}
.opt{display:flex;align-items:center;gap:$s3;box-sizing:border-box;min-height:44px;padding:$s2 $s3;border-radius:$r-row;box-shadow:inset 0 0 0 1px $c-control-border;color:$c-ink;font-size:.875rem;line-height:1.25rem}
.opt input{flex:none;width:1.125rem;height:1.125rem;margin:0;accent-color:$c-ink}
button.btn{justify-self:start;border:0;cursor:pointer}
.fail{margin:0;color:$c-ink;font-size:.8125rem;line-height:1.25rem;font-weight:500}
.fail:empty{display:none}
"""
"""`:root` 밖 CSS 규칙. `$c-이름` · `$s번호` · `$r-이름`은 `css()`가 `var(--studio-...)`로 바꾼다(색 값은 여기에 쓰지 않는다).
`$dot-min` · `$dot-max` · `$dot-ratio`는 차트 E 점 지름 상수(`DOT_MIN` · `DOT_MAX` · `DOT_RATIO`) 값으로 바뀐다. 지름 식은 `@supports (width:1cqw)` 안에 둔다:
식에 `var(--gaps, 1)`이 있으면 브라우저가 파싱 때 선언을 검사하지 않고(계산할 때야 검사) 받아들이므로, `cqw`를 모르는 브라우저는 앞 선언을 두어도 이 선언이
무효가 되며 점이 1px로 나오기 때문이다(브라우저 실측).
`.span2`(연결 상태의 체결 받은 범위 칸)는 열이 둘 이상일 때만 둘을 차지한다: 열이 하나일 때 `span 2`는 없는 열을 만들어 넘친다. 조건의 `12px`은 열 간격 `$s3`다
(컨테이너 조건에는 `var()`를 못 써서 글자 그대로). `.nw`(날짜 · 시각 덩이)는 줄바꿈이 덩이 사이에서만 일어나게 하고, 칸이 덩이(8.4rem)보다 좁은 `9rem` 미만의 아주 좁은 폭에서만
풀어 가로 넘침을 막는다. `.g2.by`(회고 구간 둘)는 문서 순서가 구간별(숫자 이번 → 기준 줄 · 비용 안내 → 차이 이번 → 숫자 실전 → 차이 실전)이라 1024 미만에서는 그냥 한 열로 쌓이고, 1024 이상에서만
격자 영역으로 위 줄 = 숫자 둘(왼쪽 이번, 오른쪽 실전), 가운데 줄 = 노트(두 열에 걸침), 아래 줄 = 차이 둘로 편다. 영역 지정은 자식 순서(`:nth-child`)로 한다. `.fail:empty`는 늘 있는 `role="status"` 실패 줄이 비었을 때 자리를 차지하지 않게 한다.
CSS 주석은 `.cmd` 하나뿐이라는 시험이 있어 이유는 여기에 적는다."""


def css() -> str:
    """`<style>` 한 블록에 넣을 CSS: `:root`(토큰 · 간격 · 반경 · `color-scheme: dark`) + 바탕 규칙 + 부품 클래스.

    바탕 규칙은 `html, html body`에 바탕 · 글자 · 글꼴을 함께 적고, `[class]`로 상속을 되살리며, 글자가 있는 부품은
    자기 클래스에 `color`를 직접 적는다. 반응형은 본문 1280 가운데, 1024 이상 `.g2` 2열, 표는 가로 스크롤 상자, 조작 44px.

    Returns:
        CSS 글(`:root`가 맨 앞이고, 색 값은 `:root` 안에만 있다).
    """
    root = ("color-scheme:dark;" + "".join(f"--studio-color-{k}:{v};" for k, v in TOKENS.items()) + f"--studio-font-sans:{FONT};--studio-font-mono:{MONO};"
            + "".join(f"--studio-space-{i}:{v}px;" for i, v in enumerate(SPACE, 1))
            + "".join(f"--studio-radius-{k}:{v}px;" for k, v in RADIUS.items()))
    dots = {"min": str(DOT_MIN), "max": str(DOT_MAX), "ratio": f"{DOT_RATIO:g}".removeprefix("0")}  # 비율은 앞 0 없는 꼴(.8)
    rules = re.sub(r"\$(?:c-([a-z0-9-]*[a-z0-9])|s(\d)|r-([a-z]+)|dot-(min|max|ratio))",
                   lambda m: (f"var(--studio-color-{m[1]})" if m[1] else f"var(--studio-space-{m[2]})" if m[2]
                              else f"var(--studio-radius-{m[3]})" if m[3] else dots[m[4]]),
                   _RULES)
    return f":root{{{root}}}\n{rules}"


def num(x: Any) -> float | None:
    """JSON에서 읽은 값 가운데 쓸 수 있는 숫자만 float으로.

    Args:
        x: 읽은 값.

    Returns:
        bool이 아닌 유한한 int · float이면 float, 아니면 None(문자열 · NaN · 무한대 · 너무 큰 정수 포함).
    """
    if isinstance(x, bool) or not isinstance(x, (int, float)):
        return None
    try:
        v = float(x)
    except OverflowError:
        return None
    return v if math.isfinite(v) else None


def obj(x: Any) -> dict:
    """JSON에서 읽은 값이 dict이면 그대로, 아니면 빈 dict(선택 객체의 모양 검사).

    Args:
        x: 읽은 값.

    Returns:
        dict 또는 `{}`.
    """
    return x if isinstance(x, dict) else {}


def text(s: object) -> str:
    """레포에서 온 글을 화면에 낼 수 있게: `plain`(그림 글자 평문화) → ` · `를 `, `로 → HTML escape.

    Args:
        s: 글(전략 이름 · 이유 · 날 등). str이 아니면 str()로 바꾼다.

    Returns:
        escape된 글(가운뎃점 낱말 구분이 없음).
    """
    return html.escape(plain(str(s)).replace(" · ", ", "))


def ident(s: object) -> str:
    """수량이 아니라 식별자인 값(키 · 토큰의 끝 네 자리 등)을 `<span class="id">`로 감싼다(보이는 글 · 모양은 그대로, 새 CSS 규칙 0).

    식별자는 숫자만으로 되어 있어도 쉼표를 넣을 수 없다. 문구 관문(`tests/webrules.copy_violations`)은 이 요소 안 글만 숫자 규칙에서 뺀다.

    Args:
        s: 식별자 값. str이 아니면 str()로 바꾼다.

    Returns:
        escape한 글을 감싼 HTML.
    """
    return f'<span class="id">{text(s)}</span>'


def reason(exc: BaseException, what: str) -> str:
    """읽지 못한 이유를 화면 · 내보내기용 한 줄로: 경로 · 합쇼체 · 가운뎃점이 든 `WorkspaceError` 메시지는 쓰지 않는다.

    Args:
        exc: 읽기에서 난 예외.
        what: 읽으려던 것(`매일 대조 기록` · `가드레일` · `전략 기록`(끈 전략의 record.md) · `전략 목록`(strategies/ 폴더 자체, 홈 전체가 막힐 때)).

    Returns:
        평문 한 줄(escape 전). 파일이 없으면 「파일이 없어요」, 링크이거나 모양이 틀리면(WorkspaceError) 그 안내, 그 밖에는 예외 종류 이름.
    """
    if isinstance(exc, FileNotFoundError):
        return f"{what} 파일이 없어요"
    return f"{what} 파일을 읽지 못했어요({'링크이거나 모양이 달라요' if isinstance(exc, WorkspaceError) else type(exc).__name__})"


def _signed(x: Any, digits: int, unit: str) -> str:
    """비율(0.01 = 1%)을 부호 붙은 퍼센트 글로. 반올림해 0이면 부호 없이, 1,000 이상은 쉼표(터미널 `_e_pct`와 같은 모양).

    Args:
        x: 비율. 숫자가 아니면 「모름」.
        digits: 소수 자릿수.
        unit: 뒤에 붙일 단위(`%` 또는 `%p`).

    Returns:
        예: `-4.6%` · `+1,000.00%` · `0.0%` · `모름`.
    """
    v = num(x)
    if v is None:
        return "모름"
    s = f"{v * 100:+,.{digits}f}"
    return (s[1:] if float(s.replace(",", "")) == 0 else s) + unit


def pct(x: Any, digits: int = 1) -> str:
    """손익 비율을 `status._pct`(한 자리) · `chart._e_pct`(두 자리)와 같은 자릿수 · 부호로.

    Args:
        x: 비율(0.01 = 1%).
        digits: 소수 자릿수(손익 1, 거래 단위 2).

    Returns:
        `-4.6%` · `0.0%`(반올림해 0) · `모름`.
    """
    return _signed(x, digits, "%")


def pp(x: Any) -> str:
    """비율 차이를 `states.pp`와 같은 한 자리 `%p`로(0은 부호 없이).

    Args:
        x: 비율 차이.

    Returns:
        `-2.7%p` · `0.0%p` · `모름`.
    """
    return _signed(x, 1, "%p")


def prob(x: Any) -> str:
    """확률을 정수 퍼센트로(엔진 `{:.0%}`). 99%를 넘으면 금지 낱말 `100%`가 나오지 않게 `99% 넘게`.

    Args:
        x: 0~1 확률.

    Returns:
        `79%` · `99% 넘게` · `모름`.
    """
    v = num(x)
    return "모름" if v is None else "99% 넘게" if v > 0.99 else f"{v:.0%}"


@dataclass(frozen=True)
class Static:
    """공개 샘플 데모(정적 파일)로 그리는 문맥. 접속 토큰 자리에 넘긴다.

    Attributes:
        here: 지금 페이지의 서버 경로(`/`, `/s/<이름>`, `/s/<이름>/retro`). 링크는 여기서 가는 상대 디렉터리 주소가 된다.
    """

    here: str


Ctx = str | Static
"""그리기 문맥: 접속 토큰(서버 화면) 또는 `Static`(공개 샘플 데모)."""


def _static_dir(path: str) -> str:
    """서버 경로를 정적 출력의 디렉터리(데모 루트 기준)로 바꾼다. 정적 데모에 있는 화면만 받는다.

    Args:
        path: `/`, `/s/<이름>`, `/s/<이름>/retro` 가운데 하나.

    Returns:
        `.`, `s/<이름>`, `s/<이름>/retro`.

    Raises:
        ValueError: 정적 데모에 없는 경로(연결 · 내보내기 등, 죽은 링크를 만들지 않게).
    """
    parts = path.split("/")[1:]
    if path == "/":
        return "."
    if parts[0] == "s" and len(parts) in (2, 3) and parts[1] not in ("", ".", "..") and parts[2:] in ([], ["retro"]):
        return "/".join(parts)
    raise ValueError(f"정적 데모에 없는 경로: {path!r}")


def static_path(path: str) -> str:
    """서버 경로 → 정적 출력의 파일 경로(데모 루트 기준).

    Args:
        path: `/`, `/s/<이름>`, `/s/<이름>/retro` 가운데 하나.

    Returns:
        예: `index.html`, `s/trend_10_50/index.html`, `s/trend_10_50/retro/index.html`.

    Raises:
        ValueError: 정적 데모에 없는 경로.
    """
    d = _static_dir(path)
    return "index.html" if d == "." else f"{d}/index.html"


def href(path: str, token: Ctx) -> str:
    """같은 출처 상대 주소에 접속 토큰을 붙인다. 결과는 속성 안에 그대로 넣어도 안전하다(quote를 거침).

    Args:
        path: `/`로 시작하는 경로(`//`로 시작하면 다른 출처라 안 됨).
        token: 접속 토큰, 또는 `Static`이면 토큰 없이 지금 페이지에서 가는 상대 디렉터리 주소(예: `../a/retro/`).

    Returns:
        예: `/s/trend_10_50?t=abc`.

    Raises:
        ValueError: path가 `/`로 시작하지 않거나 `//`로 시작함, 또는 `Static`인데 정적 데모에 없는 경로.
    """
    if not path.startswith("/") or path.startswith("//"):
        raise ValueError(f"같은 출처 경로가 아님: {path!r}")
    if isinstance(token, Static):
        return quote(posixpath.relpath(_static_dir(path), _static_dir(token.here)), safe="/") + "/"
    return f"{quote(path, safe='/')}?t={quote(token, safe='')}"


_GLYPH: dict[str, tuple[str, str]] = {
    "정상": ('viewBox="0 0 12 12" fill="none" stroke="currentColor" stroke-width="1.6" stroke-linecap="round" stroke-linejoin="round"',
           '<circle cx="6" cy="6" r="5"/><path d="M3.6 6.2l1.6 1.5 3.1-3.3"/>'),
    "경고": ('viewBox="0 0 10 10"',
           '<rect x="1" y="1" width="8" height="8" rx="1" fill="none" stroke="currentColor" stroke-width="2"/><circle cx="5" cy="5" r="1.5" fill="currentColor"/>'),
    "판단 보류": ('viewBox="0 0 10 10"',
              ('<rect x="0.5" y="0.5" width="9" height="9" rx="1" fill="none" stroke="currentColor" stroke-width="1"/>'
               '<rect x="0.5" y="5" width="9" height="4.5" fill="currentColor"/>')),
    "확인 불가": ('viewBox="0 0 10 10"',
              '<rect x="0.5" y="0.5" width="9" height="9" rx="1" fill="none" stroke="currentColor" stroke-width="1" stroke-dasharray="3 2"/>'),
    "점검 불가": ('viewBox="0 0 10 10"',
              ('<rect x="0.5" y="0.5" width="9" height="9" rx="1" fill="none" stroke="currentColor" stroke-width="1"/>'
               '<line x1="1" y1="9" x2="9" y2="1" stroke="currentColor" stroke-width="1"/>')),
}
"""판정 칩 기호 다섯(낱말 → (viewBox 등 svg 속성, 안쪽 도형)). 좌표는 디자인 시스템 그대로이고 색은 `currentColor`라 칩 클래스 색을 따른다.
다섯 모양이 서로 달라 라벨을 가려도 갈린다: 원 고리 + 체크(정상), 2px 사각 + 점(경고), 아래 반을 채운 사각(판단 보류), 파선 사각(확인 불가), 사선 사각(점검 불가)."""


def chip(word: str) -> str:
    """판정 칩: 10×10 SVG 기호(aria-hidden) + 낱말. 접근 이름은 낱말이다.

    기호는 글꼴 글자가 아니라 SVG라 기계마다 폭 · 모양 · 기준선이 같다. 모르는 낱말은 점검 불가 모양 + escape된 낱말.
    괄호 사유를 떼는 일은 부르는 쪽이 한다(`states.base`).

    Args:
        word: 대표 상태 낱말(정상 · 경고 · 판단 보류 · 확인 불가 · 점검 불가).

    Returns:
        `<span class="chip chip-...">` HTML. 경고만 2px caution 테두리 클래스(`chip-warn`).
    """
    cls = {"정상": "chip-ok", "경고": "chip-warn"}.get(word, "chip-mute")
    attrs, shapes = _GLYPH.get(word, _GLYPH["점검 불가"])
    return f'<span class="chip {cls}"><svg class="chip-glyph" {attrs} aria-hidden="true" focusable="false">{shapes}</svg>{text(word)}</span>'


_PITCH, _BAR_W, _BAR_H, _BASE, _H = 10, 8, 18, 20, 28
"""흐름 막대 한 칸의 간격 · 폭, 가장 높은 막대 높이, 막대 바닥 선 y, 그림 높이(px). 바닥 선 아래 6px은 경고 삼각형 자리."""


def spark_svg(cells: Sequence[dict], start: str | None = None, warn_days: int | None = None) -> str:
    """D 흐름 칸(`data.spark` 결과)을 막대 SVG 한 줄로. 색은 클래스(`spark-bar` · `spark-warn` · `spark-gap`)로만 칠한다.

    칸마다 막대 하나(높이는 눈금에 비례해 `(눈금 + 1) / 6`, 눈금 0도 보임), 값이 없는 칸은 흐린 점, 경고인 칸은 막대 아래 작은
    주의 삼각형(색만으로 가르지 않기).

    Args:
        cells: 칸마다 {"day", "value", "warn": bool, "level": 0~5 | None}.
        start: 그림이 덮는 첫 기록 날. None이면 첫 칸의 날(22일 이하는 같음, 넘으면 칸이 날을 묶어 첫 칸이 첫 날이 아님).
        warn_days: 그린 범위에서 경고인 기록(날) 수. None이면 경고 칸 수(22일 이하는 같음, 넘으면 칸의 경고는 그 날 하루뿐이라 모자람).

    Returns:
        `<svg role="img" aria-label=...>` HTML. 칸이 없으면 빈 글.
    """
    if not cells:
        return ""
    parts = []
    for i, c in enumerate(cells):
        x = i * _PITCH
        if c["level"] is None:
            parts.append(f'<circle class="spark-gap" cx="{x + _BAR_W // 2}" cy="{_BASE - 2}" r="1.5"/>')
        else:
            h = round(_BAR_H * (c["level"] + 1) / 6)
            parts.append(f'<rect class="spark-bar" x="{x}" y="{_BASE - h}" width="{_BAR_W}" height="{h}"/>')
        if c["warn"]:
            parts.append(f'<path class="spark-warn" d="M{x + _BAR_W // 2} {_BASE + 2}L{x + _BAR_W} {_H - 1}H{x}Z"/>')
    w = len(cells) * _PITCH - (_PITCH - _BAR_W)
    k = sum(1 for c in cells if c["warn"]) if warn_days is None else warn_days
    label = f"{text(cells[0]['day'] if start is None else start)}~{text(cells[-1]['day'])} 실전 시작부터 실제 손익 흐름이에요. 경고인 날은 {k}일이에요."
    return (f'<svg class="spark" role="img" aria-label="{label}" width="{w}" height="{_H}" viewBox="0 0 {w} {_H}">'
            + "".join(parts) + "</svg>")


HOME_NAV: tuple[tuple[str, str | None, bool], ...] = (("홈", "/", True), ("연결", "/connect", False))
"""홈 화면의 내비(`page`의 nav 모양): (글, 경로 또는 None(링크 아님), 지금 화면인지). `연결`은 언제나 끝 칸."""

CONNECT_NAV: tuple[tuple[str, str | None, bool], ...] = (("홈", "/", False), ("연결", "/connect", True))
"""상주 연결 화면의 내비. 한 번용 서버 화면은 내비가 없다(빈 nav)."""


def strategy_nav(name: str, here: str, guard: bool, retro: bool) -> tuple[tuple[str, str | None, bool], ...]:
    """전략 화면의 내비: `홈` → 전략 이름(링크 아님) → `가드레일`(그 화면이 있을 때만) → `회고`(있을 때만) → `연결`. 죽은 링크를 만들지 않는다.

    Args:
        name: 전략 이름(전략 이름 모양을 지난 것).
        here: 지금 화면(`guard` 또는 `retro`).
        guard: 가드레일 화면이 있는지.
        retro: 회고 화면이 있는지.

    Returns:
        `page`의 nav.
    """
    return (("홈", "/", False), (name, None, False), *((("가드레일", f"/s/{name}", here == "guard"),) if guard else ()),
            *((("회고", f"/s/{name}/retro", here == "retro"),) if retro else ()), ("연결", "/connect", False))


def page(title: str, body: str, token: Ctx, nav: Sequence[tuple[str, str | None, bool]]) -> bytes:
    """페이지 한 장: 다크 메타 · `<style>` 한 블록 · 내비 · 본문. `Static`이면 내비에서 「연결」을 빼고, 검색 제외 메타와
    맨 위 샘플 띠(`STATIC_BANNER` + BlackClip 홈 링크)를 넣는다.

    Args:
        title: 탭 제목(escape함).
        body: 본문 HTML(이미 escape한 것).
        token: 접속 토큰(내비 링크에 붙음) 또는 `Static`.
        nav: 내비 칸들. 칸마다 (글, 같은 출처 경로 또는 None, 지금 화면인지). 경로가 None이면 링크가 아닌 글자(전략 이름)이고, 지금 화면인 링크에만 `aria-current`가 붙는다.

    Returns:
        UTF-8 바이트(`<!doctype html>`로 시작).
    """
    def item(label: str, path: str | None, here: bool) -> str:
        if path is None:
            return f'<span class="nav-link">{text(label)}</span>'
        cur = ' aria-current="page"' if here else ""
        return f'<a class="nav-link" href="{href(path, token)}"{cur}>{text(label)}</a>'

    static = isinstance(token, Static)
    links = "".join(item(*x) for x in nav if not (static and x[1] == "/connect"))
    brand = f'<span class="brand">{BRAND}</span>'
    if static:  # 깊은 쪽을 공유받은 사람도 설치 줄이 있는 첫 장(데모 루트의 부모)으로
        up = posixpath.normpath(posixpath.join(posixpath.relpath(".", _static_dir(token.here)), ".."))
        brand = f'<a class="nav-link" href="{up}/">{brand}</a>'
    banner = (f'<div class="top"><p class="nav sub">{STATIC_BANNER} <a class="nav-link" href="https://blackclip.app/">BlackClip 홈</a></p></div>\n'
              if static else "")
    return ('<!doctype html>\n<html lang="ko">\n<head>\n<meta charset="utf-8">\n'
            '<meta name="viewport" content="width=device-width,initial-scale=1,viewport-fit=cover">\n'
            + ('<meta name="robots" content="noindex">\n' if static else "")
            + '<meta name="color-scheme" content="dark">\n'
            f'<meta name="theme-color" content="{TOKENS["canvas"]}">\n'
            f"<title>{text(title)} | {BRAND}</title>\n<style>{css()}</style>\n</head>\n<body>\n{banner}"
            f'<header class="top"><nav class="nav" aria-label="화면 이동">{brand}{links}</nav></header>\n'
            f'<main class="wrap stack">\n{body}\n</main>\n</body>\n</html>\n').encode()


_LIVE_HEADS = (("전략", ""), ("상태", ""), ("실전 시작부터 실제", " cell-num"), ("같은 기간 백테스트", " cell-num"),
               ("차이(비용 0)", " cell-num"), ("거래", " cell-num"), ("기준 날", " cell-num"), ("흐름", ""))
"""홈 실전 표 칸 머리(글, 덧붙일 클래스). 쌓은 목록의 라벨도 이 글이다. 차이는 비용 0 같은 기간 백테스트와의 차이이고 뜻은 `DIFF_NOTE`가 말한다."""

_OFF_HEADS = (("전략", ""), ("상태", ""), ("시작 날", " cell-num"), ("메모", ""))
"""홈 끈 전략 표 칸 머리."""


def _table(label: str, heads: Sequence[tuple[str, str]], rows: Sequence[str], cls: str = "") -> str:
    """가로 스크롤 상자 안의 표. 좁으면 상자째 숨기고(`wide`) 짝인 `_stack`이 대신 보인다.

    `min-width:44rem` 표(`cls`에 `tbl-fit` 없음)는 컨테이너 쌍(`cq`)이라 창이 아니라 둘러싼 `.panel` 폭 48rem 미만에서 숨는다.
    `tbl-fit` 표는 창 폭 1024 미만에서 숨고(예전 모양 그대로), 패널 내용 폭이 16rem(잰 최소 폭 15.91rem) 미만이면 목록으로 바꾼다.
    한계: 표 글이 길어 최소 폭이 16rem을 넘거나 루트 글자가 100%보다 작으면(잰 값은 루트 16px · 32px 둘) 가로 스크롤 상자가 남을 수 있다. 짝인 `_stack`에 같은 `fit`을 넘긴다.

    Args:
        label: 상자 이름(접근성).
        heads: (칸 머리 글, 덧붙일 클래스) 목록.
        rows: `<tr>` HTML 목록.
        cls: 표에 덧붙일 클래스(앞에 공백 포함, 예 `" tbl-fit"` = 좁은 칸에서 줄바꿈으로 맞춤).

    Returns:
        HTML.
    """
    th = "".join(f'<th scope="col" class="cell cell-head{x}">{h}</th>' for h, x in heads)
    pair = "" if "tbl-fit" in cls else " cq"
    return (f'<div class="scroll wide{pair}" role="region" aria-label="{label}" tabindex="0"><table class="tbl{cls}"><thead><tr>{th}</tr></thead>'
            f'<tbody>{"".join(rows)}</tbody></table></div>')


def _stack(label: str, rows: Sequence[tuple[str, str, Sequence[tuple[str, str]], str]], fit: bool = False) -> str:
    """좁을 때 표 대신 보이는 쌓은 목록: 줄마다 머리 → 이유 → 값 쌍(`dl`) → 꼬리 순서, 줄 사이는 hairline 줄(카드 아님).

    표 넷(실전 · 끈 · 원인 셋 · 기준)이 같이 쓴다. 표와 달리 칸을 가로로 놓지 않아 좁은 폭에서도 가로 스크롤이 없다.

    Args:
        label: 목록 이름(접근성).
        rows: 줄마다 (머리 HTML, 이유 HTML, [(라벨, 값 HTML)], 꼬리 HTML). 이유 · 값 쌍 · 꼬리는 비어 있으면 만들지 않는다.
        fit: 짝인 표가 `tbl-fit`이면 True(창 폭 1024 이상에서 숨김). 아니면 컨테이너 쌍(`cq`)이라 패널 폭 48rem 이상에서 숨는다.

    Returns:
        `<ul class="rows narrow">` HTML(컨테이너 쌍이면 `cq`가 더 붙음).
    """
    def item(head: str, why: str, pairs: Sequence[tuple[str, str]], tail: str) -> str:
        dl = "".join(f"<div><dt>{k}</dt><dd>{v}</dd></div>" for k, v in pairs)
        return (f'<li class="item"><div class="item-head">{head}</div>{why}' + (f'<dl class="pairs">{dl}</dl>' if dl else "")
                + (f'<div class="item-tail">{tail}</div>' if tail else "") + "</li>")

    return f'<ul class="rows narrow{"" if fit else " cq"}" aria-label="{label}">{"".join(item(*r) for r in rows)}</ul>'


def _name_link(name: str, token: Ctx, tail: str = "") -> str:
    """전략 이름에서 전략 화면으로 가는 링크(기본은 가드레일 화면, `tail`이 `/retro`면 회고 화면). 전략 이름 모양(`hook._NAME`)이 아니면 링크 없이 글만.

    Args:
        name: 전략 이름.
        token: 접속 토큰.
        tail: 경로 끝에 붙일 글(`/retro` 또는 빈 글).

    Returns:
        `<a>` HTML, 이름 모양이 아니면 escape된 글.
    """
    if not _NAME.fullmatch(name):
        return text(name)
    return f'<a class="link hit" href="{href("/s/" + name + tail, token)}">{text(name)}</a>'


def _live_row(r: dict, token: Ctx) -> tuple[str, tuple[str, str, list[tuple[str, str]], str]]:
    """실전 한 줄을 두 모양으로: 표의 `<tr>`와 쌓은 목록의 줄(`_stack` 항목). 같은 값 · 같은 글을 쓴다.

    기록이 없으면 표는 상태 칸 하나(상태부터 흐름까지 일곱 칸에 걸침), 목록은 머리 + 안내만 둔다(가짜 숫자 · 자리 행 없음).
    실제 값 아래에는 마지막 날 실제를 몰라 마지막으로 안 날을 caption 줄(`{날}까지`)로 둔다.

    Args:
        r: `home`의 rows 항목.
        token: 접속 토큰.

    Returns:
        (`<tr>` HTML, (머리, 이유, [(라벨, 값)], 꼬리) HTML 조각들).
    """
    name = _name_link(r["name"], token)
    th = f'<th scope="row" class="cell cell-name">{name}</th>'
    head = f'<span class="item-name">{name}</span>'
    if r["word"] is None:
        note = EMPTY_LIVE if r["live"] else EMPTY_PRE
        return f'<tr class="row">{th}<td class="cell" colspan="7">{note}</td></tr>', (head, f"<div>{note}</div>", [], "")
    why = f'<div class="why{" why-warn" if r["word"] == "경고" else ""}">{text(r["reason"])}</div>' if r["reason"] else ""
    actual = pct(r["actual"]) + (f'<div class="sub">{text(r["until"])}까지</div>' if r["until"] and num(r["actual"]) is not None else "")
    trades = "모름" if (n := num(r["trades"])) is None else f"{int(n):,}건"
    day = text(r["day"]) if r["day"] else "모름"
    nums = (actual, pct(r["bt"]), pp(r["gap"]), trades, day)
    spark = spark_svg(r["spark"], r["spark_start"], r["warn_days"])
    tr = (f'<tr class="row">{th}<td class="cell">{chip(r["word"])}{why}</td>'
          + "".join(f'<td class="cell cell-num">{v}</td>' for v in nums) + f'<td class="cell">{spark or "모름"}</td></tr>')
    labels = [h for h, _ in _LIVE_HEADS]  # 표 머리와 같은 글
    tail = f"<span>{labels[5]} {trades}</span><span>{labels[6]} {day}</span>" + (spark or f"<span>{labels[7]} 모름</span>")
    return tr, (head + chip(r["word"]), why, list(zip(labels[2:5], nums)), tail)


def _off_row(o: dict, token: Ctx) -> tuple[str, tuple[str, str, list[tuple[str, str]], str]]:
    """끈 전략 한 줄을 두 모양으로(표의 `<tr>`, 쌓은 목록의 줄).

    Args:
        o: `home`의 off 항목.
        token: 접속 토큰.

    Returns:
        (`<tr>` HTML, (머리, 이유, [(라벨, 값)], 꼬리) HTML 조각들).
    """
    name = _name_link(o["name"], token, "/retro")  # 끈 전략은 모두 회고 화면이 있다
    note = BROKEN_NOTE if o["state"] == BROKEN else AFTER_NOTE if o["after"] else ""
    since = text(o["since"]) if o["since"] else "모름"
    tr = (f'<tr class="row"><th scope="row" class="cell cell-name">{name}</th><td class="cell">{text(o["state"])}</td>'
          f'<td class="cell cell-num">{since}</td><td class="cell">{note}</td></tr>')
    return tr, (f'<span class="item-name">{name}</span><span>{text(o["state"])}</span>', f'<div class="why">{note}</div>' if note else "",
                [(_OFF_HEADS[2][0], since)], "")


def home(rows: list[dict], off: list[dict], day: str | None, token: Ctx) -> bytes:
    """홈 화면: 화면 제목(패널 밖) + 실전 전략 표 + 끈 전략 표(있을 때만) + 내보내기 링크 둘 + 바닥 글.

    넘겨받은 순서대로 그린다(줄 순서 `status._rank` · 이름은 서버가 정한다). 숫자는 날 값 그대로 받아 여기서 글로 옮긴다.
    값이 없으면 「모름」, 마지막 날에 실제 손익이 없으면 실제 값 아래에 caption 줄 `{until}까지`.
    표는 1024 이상에서, 쌓은 목록(`_stack`)은 1024 미만에서 보인다(둘 다 HTML에 있고 CSS가 고른다).

    Args:
        rows: 실전 전략 줄. 키(모두 필요):
            name: 전략 이름 str.
            live: bool. 실전 중이면 True(`live.json`이 있거나 기록이 있음), 가드레일만 저장했으면 False. 제목의 「실전 전략 N개」는 True만 센다.
            word: 칩 낱말 str(`states.base(rep)`로 괄호 사유를 뗀 것) 또는 None(기록이 없는 전략: 칩 없이 안내 글만).
            reason: 칩 밖 이유 한 줄 str 또는 None(정상이면 None). 괄호 사유 또는 `CAUSE` 이름 또는 `reason(...)`.
            actual: 실전 시작부터 실제 손익 비율 float | None (`data.d_values["pnl"]`).
            bt: 같은 기간 백테스트(보수적 비용) 비율 float | None (`status._whole(nums, "bt")`).
            gap: 실제 − 비용 0 같은 기간 백테스트 비율 차이 float | None (`data.d_values["gap"]`, `(날까지)` 줄은 그 날 기록의 차이).
            trades: 거래 수 int | None.
            day: 마지막 기록 날 "YYYY-MM-DD" | None.
            until: 손익을 마지막으로 안 날 "YYYY-MM-DD" | None (`data.d_values["until"]`, 마지막 날에 실제가 없을 때만).
            spark: `data.spark(recs, last)` 결과 list[dict] (기록이 없으면 `[]`).
            spark_start: 흐름이 덮는 첫 기록 날 "YYYY-MM-DD" | None (흐름 글의 시작 날, 22일을 넘으면 첫 칸의 날과 다름).
            warn_days: 흐름 범위에서 경고인 기록 수 int (흐름 글의 「경고인 날은 k일」, 칸 수가 아니라 기록 수).
            (`dashboard._row`는 내보내기가 쓰는 `bt0` · `live_day` · `guardrail`(읽은 가드레일)도 담지만 홈은 읽지 않는다.)
        off: 끈 전략 줄. 키: name str, state str(`중지` · `은퇴` · `기록 손상`), since "YYYY-MM-DD" | None(기록 손상이면 None),
            after bool(after.md가 있음), guard bool(가드레일 판이 있음, 이름 링크는 모두 회고 화면으로 가서 화면은 쓰지 않음).
        day: 전략 가운데 가장 늦은 기준 날 "YYYY-MM-DD" | None.
        token: 접속 토큰 또는 `Static`(내보내기 링크를 빼고 바닥 글이 `STATIC_FOOT`).

    Returns:
        UTF-8 HTML 바이트. 실전도 끈 전략도 없으면 글 한 줄 + 행동 하나만 낸다.
    """
    live = sum(1 for r in rows if r["live"])
    head = f'<div class="head"><h1 class="display">실전 전략 {live}개</h1>' + (f'<p class="sub">기준 {text(day)}</p>' if day else "") + "</div>"
    if not rows and not off:
        body = f'{head}<section class="panel"><p class="empty">{EMPTY_ALL}</p><code class="cmd">{html.escape(CMD_EMPTY)}</code></section>'
    else:
        body = f'{head}<section class="panel">'
        if rows:
            built = [_live_row(r, token) for r in rows]
            body += (_table("실전 전략 표", _LIVE_HEADS, [tr for tr, _ in built]) + _stack("실전 전략 목록", [it for _, it in built])
                     + f'<p class="sub">{DIFF_NOTE}</p><p class="sub">{SPARK_NOTE}</p>')
        else:  # 끈 전략만 있어도 빈 패널이 아니라 실전 표의 빈 글
            body += f'<p class="empty">{EMPTY_ALL}</p>'
        body += "</section>"
        if off:
            built = [_off_row(o, token) for o in off]
            body += (f'<section class="panel"><h2 class="h2">끈 전략 {len(off)}개</h2>'
                     + _table("끈 전략 표", _OFF_HEADS, [tr for tr, _ in built]) + _stack("끈 전략 목록", [it for _, it in built]) + "</section>")
        if not isinstance(token, Static):  # 내보내기는 서버가 있어야 해서 공개 샘플 데모에는 없다
            body += (f'<div class="actions"><a class="btn btn-fill" href="{href("/export.csv", token)}">CSV 받기</a>'
                     f'<a class="btn btn-quiet" href="{href("/export.json", token)}">JSON 받기</a></div>')
    foot = STATIC_FOOT if isinstance(token, Static) else FOOT
    return page("홈", body + f'\n<p class="sub">{foot}</p>', token, HOME_NAV)


_CW, _CH, _CPAD = 720, 240, 6
"""차트 viewBox 폭 · 높이와 좌우 여백(px). 좌표는 서버가 이 칸 안에서 계산하고 CSS가 화면 폭에 맞춘다.
이름이 `_PITCH · _H`(홈 흐름 막대)와 겹치지 않게 `_C`로 시작한다."""

DOT_MIN = 3
"""차트 E 점 지름의 하한(px). 점 사이 간격 × `DOT_RATIO`가 이보다 작아지는 폭에서는 점을 빼고 안내 한 줄만 보인다."""

DOT_MAX = 6
"""차트 E 점 지름의 상한(px). 간격이 넉넉할 때의 지름이고, 지름 식을 모르는 브라우저가 쓰는 지름이기도 하다."""

DOT_RATIO = 0.8
"""차트 E 점 지름 ÷ 점 사이 간격: 이웃 점과의 간격 가운데 이만큼만 점이 차지한다."""


def _batchim(s: str) -> bool:
    """글의 마지막 글자가 받침 있는 한글인지(조사 고르기). 한글이 아니면(`%` · `p` 등 읽을 때 모음으로 끝나는 글자) 받침 없음으로 본다.

    Args:
        s: 글.

    Returns:
        받침이 있으면 True.
    """
    c = s[-1:]
    return "가" <= c <= "힣" and (ord(c) - 0xAC00) % 28 != 0


def _be(s: str) -> str:
    """이름 뒤에 서술 어미를 붙인다: 받침 있으면 「이에요」, 없으면 「예요」(`안이에요` · `위예요` · `-2.7%p예요` · `모름이에요`)."""
    return s + ("이에요" if _batchim(s) else "예요")


def _ro(s: str) -> str:
    """이름 뒤에 방향 조사를 붙인다: 받침이 없거나 ㄹ 받침이면 「로」, 그 밖의 받침이면 「으로」(`경고로` · `정상으로`)."""
    return s + ("으로" if _batchim(s) and (ord(s[-1]) - 0xAC00) % 28 != 8 else "로")


def _ga(s: str) -> str:
    """이름 뒤에 주격 조사를 붙인다: 받침 있으면 「이」, 없으면 「가」(`-1.9%가` · `모름이`)."""
    return s + ("이" if _batchim(s) else "가")


def _iso(x: Any) -> str | None:
    """`YYYY-MM-DD` 꼴로 실제 있는 날인 글만 통과시킨다(선택 칸 `set_on`의 모양 검사).

    Args:
        x: 읽은 값.

    Returns:
        그 글, 아니면 None.
    """
    return x if _day(x) is not None else None


def _y_axis(vals: Sequence[float], unit: str = "%") -> tuple[Callable[[float], float], list[tuple[float, str]]]:
    """값 범위에 위아래 8% 여백을 둔 세로 좌표 함수와 눈금 라벨(HTML 축에 쓸 y좌표 · 글).

    Args:
        vals: 그릴 값들(0 포함). 모두 같으면 위아래 1%p를 더해 편다.
        unit: 눈금 글 단위(`%` 손익, `%p` 차이).

    Returns:
        (값 → y좌표(위가 0, 높이 `_CH`), [(눈금 y좌표, 눈금 글)]). 눈금은 1 · 2 · 5의 배수로 대여섯 개 안쪽이다.
    """
    lo, hi = min(vals), max(vals)
    if hi == lo:
        lo, hi = lo - 0.01, hi + 0.01
    bottom, top = lo - (hi - lo) * 0.08, hi + (hi - lo) * 0.08

    def y(v: float) -> float:
        return (top - v) / (top - bottom) * _CH

    raw = (top - bottom) / 4
    mag = 10 ** math.floor(math.log10(raw))
    step = next(m * mag for m in (1, 2, 5, 10) if m * mag >= raw)
    digits = max(0, -math.floor(math.log10(step * 100) + 1e-9))
    ticks = [(y(k * step), _signed(k * step, digits, unit)) for k in range(math.ceil(bottom / step - 1e-9), math.floor(top / step + 1e-9) + 1)]
    return y, ticks


def _pts(points: Sequence[tuple[float, float]]) -> str:
    """꼭짓점들을 `points` 속성 글로. 하루짜리 구간(점 하나)은 눈에 보이게 좌우로 1.5씩 늘린다.

    Args:
        points: (x, y) 목록.

    Returns:
        `x,y x,y ...` 글.
    """
    if len(points) == 1:
        (x, y), = points
        points = [(x - 1.5, y), (x + 1.5, y)]
    return " ".join(f"{x:.1f},{y:.1f}" for x, y in points)


def _fig(svg: str, ticks: Sequence[tuple[float, str]], left: str, right: str, fid: str = "", tail: str = "") -> str:
    """차트 SVG에 HTML 축 라벨(세로 눈금 · 가로 양끝)을 붙인다. SVG 안에는 `<text>`가 없다.

    `.fig`는 폭 컨테이너(`container-type:inline-size`)라, 차트 E의 점 지름 식(`cqw`)과 점 감추기 규칙(`@container`)이 이 폭을 읽는다.

    Args:
        svg: 차트 SVG.
        ticks: (눈금 y좌표, 글).
        left: 가로 축 왼쪽 끝 글(escape된 것).
        right: 가로 축 오른쪽 끝 글(escape된 것).
        fid: `.fig`의 id(그 차트 하나만 가리키는 `<style>` 규칙용, 기본 없음).
        tail: 가로 축 뒤에 `.fig` 안으로 덧붙일 HTML(컨테이너 안이어야 `@container`가 이 폭을 읽는다, 기본 없음).

    Returns:
        `<div class="fig">` HTML(축은 보조 기술에 숨김, 차트 설명은 SVG의 `aria-label`).
    """
    marks = "".join(f'<span class="tick" style="top:{y / _CH * 100:.2f}%">{t}</span>' for y, t in ticks)
    ident = f' id="{fid}"' if fid else ""
    return (f'<div class="fig"{ident}><div class="yax" aria-hidden="true">{marks}</div>{svg}'
            f'<div class="xax" aria-hidden="true"><span>{left}</span><span>{right}</span></div>{tail}</div>')


_SWATCH = {
    "band": '<rect class="s-band" x="1" y="1" width="26" height="10"/>',
    "bt": '<line class="s-bt" x1="1" y1="6" x2="27" y2="6"/>',
    "actual": '<line class="s-actual" x1="1" y1="6" x2="27" y2="6"/>',
    "line": '<line class="s-line" x1="1" y1="6" x2="27" y2="6"/>',
    "mean": '<line class="s-mean" x1="1" y1="6" x2="27" y2="6"/>',
    "run": '<line class="s-run" x1="1" y1="6" x2="27" y2="6"/>',
    "missed": '<path class="s-missed" d="M14 6h0"/>',
    "dot": '<path class="s-dot" d="M14 6h0"/>',
    "count": '<line class="s-count" x1="14" y1="0" x2="14" y2="12"/>',
    "slow": '<rect class="s-slow" x="1" y="1" width="26" height="10"/>',
    "fast": '<rect class="s-fast" x="1" y="1" width="26" height="10"/>',
}
"""범례 견본 모양(차트 선과 같은 `s-*` 클래스를 쓴 28×12 SVG 안쪽). `slow`는 차트 B 면과 같은 `s-slow`라 같은 문서 안 `#b-hatch` 무늬를 쓴다(흑백에서 `fast`와 갈리게)."""


def _legend(items: Sequence[tuple[str, str]]) -> str:
    """범례: 견본(차트 선과 같은 클래스) + 글. 견본 이름이 빈 글이면 글만.

    Args:
        items: (견본 이름(`_SWATCH` 키 또는 ""), 글(escape 전)).

    Returns:
        `<ul class="legend">` HTML. 견본은 장식이라 보조 기술에 숨긴다.
    """
    li = "".join('<li class="legend-item">'
                 + (f'<svg class="sw" viewBox="0 0 28 12" aria-hidden="true" focusable="false">{_SWATCH[k]}</svg>' if k else "")
                 + f"<span>{text(t)}</span></li>" for k, t in items)
    return f'<ul class="legend" aria-label="범례">{li}</ul>'


def _missed_dots(recs: list[dict], marks: dict) -> list[str]:
    """차트 A에 놓친 주문 점을 찍을 날: 놓친 주문이 있는 날 가운데 그날 실제 값이 있는 날(터미널 `_a_figure`와 같은 조건).

    Args:
        recs: 이 전략의 기록 전체.
        marks: `data.a_marks` 결과.

    Returns:
        날(`YYYY-MM-DD`) 목록, 오름차순. 범례의 「놓친 주문」은 이것이 비어 있지 않을 때만 나온다.
    """
    actual = {r["day"]: r["nums"].get("actual") for r in recs}
    return [d for d in sorted(marks["missed"]) if actual.get(d) is not None]


def a_svg(recs: list[dict], marks: dict) -> str:
    """차트 A(손익 3줄과 정상 범위)를 서버 좌표 SVG + HTML 축으로: 띠 · 0% 선 · 구간 경계 · 경고선 · 같은 기간 백테스트 · 실제 · 놓친 주문 점.

    x는 첫 날부터의 날 수에 비례하고(빈 날은 칸으로 남음), 선과 띠는 값이 없는 날과 새 구간이 시작되는 날(`marks["starts"]`)에서 끊어
    다시 시작한다(터미널 `chart_a`와 같은 규칙). 띠는 그날 lo · hi가 둘 다 있는 날의 묶음마다 `polygon` 하나다.
    색은 `s-*` 클래스로만 칠한다.

    Args:
        recs: 이 전략의 기록 전체(날짜순, 비어 있지 않음, 실제나 백테스트가 하나는 있음).
        marks: `data.a_marks(recs, g)` 결과.

    Returns:
        `<div class="fig">` HTML(`role="img"` SVG 하나 + 축).
    """
    first = date.fromisoformat(recs[0]["day"])
    n = (date.fromisoformat(recs[-1]["day"]) - first).days + 1
    by_day = {r["day"]: r["nums"] for r in recs}
    nums = [by_day.get((first + timedelta(days=i)).isoformat()) for i in range(n)]
    cuts = {(date.fromisoformat(d) - first).days for d in marks["starts"]}

    def val(i: int, key: str) -> float | None:
        return None if nums[i] is None else nums[i].get(key)

    def band_at(i: int) -> tuple[float, float] | None:
        lo, hi = val(i, "lo"), val(i, "hi")
        return None if lo is None or hi is None else (lo, hi)

    def runs(at: Callable[[int], Any]) -> list[list[tuple[int, Any]]]:
        out, cur = [], []
        for i in range(n):
            v = at(i)
            if cur and (v is None or i in cuts):
                out, cur = [*out, cur], []
            if v is not None:
                cur.append((i, v))
        return [*out, cur] if cur else out

    def x(i: int) -> float:
        return _CPAD + (_CW - 2 * _CPAD) * (i / (n - 1) if n > 1 else 0.5)

    vals = [0.0, *(v for i in range(n) for k in ("actual", "bt", "line") if (v := val(i, k)) is not None),
            *(v for i in range(n) if (b := band_at(i)) for v in b)]
    y, ticks = _y_axis(vals)
    parts = []
    for run in runs(band_at):
        if len(run) == 1:  # 하루짜리 띠도 보이게 좌우로 늘림
            (i, (lo, hi)), = run
            pts = [(x(i) - 1.5, y(hi)), (x(i) + 1.5, y(hi)), (x(i) + 1.5, y(lo)), (x(i) - 1.5, y(lo))]
        else:
            pts = [(x(i), y(hi)) for i, (_, hi) in run] + [(x(i), y(lo)) for i, (lo, _) in reversed(run)]
        parts.append(f'<polygon class="s-band" points="{_pts(pts)}"/>')
    parts.append(f'<line class="s-zero" x1="{_CPAD}" y1="{y(0):.1f}" x2="{_CW - _CPAD}" y2="{y(0):.1f}"/>')
    parts += [f'<line class="s-start" x1="{x(i):.1f}" y1="0" x2="{x(i):.1f}" y2="{_CH}"/>' for i in sorted(cuts)]
    for cls, key in (("s-line", "line"), ("s-bt", "bt"), ("s-actual", "actual")):
        parts += [f'<polyline class="{cls}" points="{_pts([(x(i), y(v)) for i, v in run])}"/>' for run in runs(lambda i, k=key: val(i, k))]
    for day in _missed_dots(recs, marks):  # 그날 실제 값 자리에 점
        i = (date.fromisoformat(day) - first).days
        parts.append(f'<path class="s-missed" d="M{x(i):.1f} {y(val(i, "actual")):.1f}h0"/>')
    last = recs[-1]["nums"]
    label = (f"실전 {text(recs[0]['day'])}부터 {text(recs[-1]['day'])}까지 실제 손익({pct(last.get('actual'))})과 "
             f"같은 기간 백테스트({pct(last.get('bt'))})를 그린 차트예요.")
    svg = f'<svg class="chart" role="img" aria-label="{label}" viewBox="0 0 {_CW} {_CH}" preserveAspectRatio="none">{"".join(parts)}</svg>'
    return _fig(svg, ticks, text(recs[0]["day"]), text(recs[-1]["day"]))


def _dots_off_width(gaps: int) -> int:
    """점 지름(간격 × `DOT_RATIO`)이 `DOT_MIN`px 아래가 되는 차트 SVG 폭(px)의 올림: 이 폭보다 좁으면 점을 뺀다.

    점은 SVG 폭의 `(_CW - 2 × _CPAD) / _CW`만큼에 간격 `gaps`개로 늘어서 간격 = 폭 × 그 비율 ÷ `gaps`다.

    Args:
        gaps: 점 사이 간격 수(점 수 - 1, 1 이상).

    Returns:
        올림한 SVG 폭(px).
    """
    return math.ceil(DOT_MIN / DOT_RATIO * gaps * _CW / (_CW - 2 * _CPAD))


def e_svg(e: dict, *, nth: int, legend: str = "") -> str:
    """차트 E(거래 단위)를 서버 좌표 SVG + HTML 축으로: 백테스트 80% 띠 · 0% 선 · 백테스트 평균 · 30건 선 · 실제 누적 평균 · 거래 1건 점.

    x는 거래 순서(첫 거래부터 마지막 거래까지 고르게)다. 80% 띠는 분위가 둘 다 있을 때만, 30건 선은 `line30`이 참일 때만 그린다.

    점 지름은 CSS가 점 사이 간격 × `DOT_RATIO`(`DOT_MIN`~`DOT_MAX`px)로 맞춘다: SVG에 간격 수 `--gaps`(= 점 수 - 1)를 실어 준다.
    지름이 `DOT_MIN` 아래가 되는 SVG 폭 N(`_dots_off_width`)보다 좁으면 점 대신 안내 한 줄만 보이게, 그 차트 하나만 가리키는 `<style>` 규칙을 바로 앞에 낸다.
    같은 규칙이 범례의 첫 줄(점 견본 「실제 거래 1건」)도 뺀다: 컨테이너 규칙은 컨테이너 안쪽만 고칠 수 있어 범례를 `.fig` 안(`tail`)에 둔다.
    점이 하나 이하면 간격이 없어 `--gaps` · 규칙 · 안내 줄이 모두 없다. 규칙 조건은 컨테이너(`.fig`) 폭이라 `.fig` 열 값(3.5rem)과 열 간격(`SPACE[1]`)을 더한다
    (`var()`는 컨테이너 조건에 쓸 수 없어 값을 그대로 적는다).

    Args:
        e: `data.e_series(recs, g)` 결과(`rets`가 비어 있지 않음).
        nth: 이 차트의 화면 안 순번(1부터, 키워드로 반드시 넘김). id `e{nth}`에 쓰이니 한 화면에 E가 둘 이상이면 부르는 쪽이 다르게 넘긴다(모듈에 카운터를 두지 않는다).
        legend: 범례 HTML(`_legend` 결과, 첫 줄이 점 견본). 안내 줄 뒤 `.fig` 안에 둔다. 기본 없음.

    Returns:
        점이 둘 이상이면 `<style>` 한 줄 + `<div class="fig" id="e{nth}">`, 아니면 `<div class="fig">` HTML(`role="img"` SVG 하나 + 축 + 범례).
    """
    rets, run, mean = e["rets"], e["run"], e["mean"]
    k = len(rets)
    band = e["p10"] is not None and e["p90"] is not None
    y, ticks = _y_axis([0.0, *rets, *run, *([e["p10"], e["p90"]] if band else []), *([] if mean is None else [mean])])

    def x(i: int) -> float:
        return _CPAD + (_CW - 2 * _CPAD) * (i / (k - 1) if k > 1 else 0.5)

    full = f'x1="{_CPAD}" x2="{_CW - _CPAD}"'
    parts = ([f'<rect class="s-band" x="{_CPAD}" y="{y(e["p90"]):.1f}" width="{_CW - 2 * _CPAD}" height="{y(e["p10"]) - y(e["p90"]):.1f}"/>'] if band else [])
    parts.append(f'<line class="s-zero" {full} y1="{y(0):.1f}" y2="{y(0):.1f}"/>')
    if mean is not None:
        parts.append(f'<line class="s-mean" {full} y1="{y(mean):.1f}" y2="{y(mean):.1f}"/>')
    if e["line30"]:
        parts.append(f'<line class="s-count" x1="{x(e["minimum"] - 1):.1f}" y1="0" x2="{x(e["minimum"] - 1):.1f}" y2="{_CH}"/>')
    parts.append(f'<polyline class="s-run" points="{_pts([(x(i), y(v)) for i, v in enumerate(run)])}"/>')
    parts += [f'<path class="s-dot" d="M{x(i):.1f} {y(v):.1f}h0"/>' for i, v in enumerate(rets)]
    label = f"거래 {k:,}건의 실제 수익과 누적 평균을 그린 차트예요."
    gaps = k - 1
    style = f' style="--gaps:{gaps}"' if gaps else ""
    svg = f'<svg class="chart" role="img" aria-label="{label}" viewBox="0 0 {_CW} {_CH}" preserveAspectRatio="none"{style}>{"".join(parts)}</svg>'
    if not gaps:
        return _fig(svg, ticks, "1번째 거래", f"{k:,}번째 거래", tail=legend)
    rule = (f"<style>@container (width < calc(3.5rem + {SPACE[1]}px + {_dots_off_width(gaps)}px)){{#e{nth} .s-dot{{display:none}} "
            f"#e{nth} .e-dots-off{{display:block}} #e{nth} .legend-item:first-child{{display:none}}}}</style>")
    off = '<p class="e-dots-off">점이 겹치지 않게 이 폭에서는 점은 빼고 누적 평균 선만 그렸어요.</p>'
    return rule + _fig(svg, ticks, "1번째 거래", f"{k:,}번째 거래", fid=f"e{nth}", tail=off + legend)


_Q = {"전략": "전략이 망가졌나", "실행": "실행에서 새나", "봇": "봇이 멈췄나"}
"""원인 셋 표의 원인 이름(물음 셋, 키는 `states.TRACKED`)."""


def _strategy_cause(word: str, n: dict) -> str:
    """「전략이 망가졌나」 근거: 같은 기간 백테스트가 정상 범위의 안 · 위 · 아래인지.

    안 · 위 · 아래는 상태와 상관없이 엔진이 저장한 `bt_pos`다: 엔진(`reconcile.strategy_cause`)은 범위 위도 「정상」을 내므로
    상태에서 위치를 거꾸로 추정하지 않는다(A 범례와 같은 낱말). 상태는 판단 보류의 엣지 문장을 덧붙일지에만 쓴다.

    Args:
        word: 이 칸의 상태 낱말(정상 · 경고 · 판단 보류).
        n: 마지막 기록의 nums.

    Returns:
        평문 한 문장(레포 글이 들 수 있어 내기 전에 `text`를 거침).
    """
    pos = n.get("bt_pos")
    if not pos:
        return "정상 범위를 그리지 않아 안팎을 말할 수 없어요."
    out = f"같은 기간 백테스트 {_ga(pct(n.get('bt')))} 정상 범위 {_be(str(pos))}."
    if word == "판단 보류" and n.get("edge0") is not None:
        p = prob(n["edge0"])  # 99%를 넘으면 `99% 넘게`라 「…넘게라」가 되지 않게 「99%를 넘어」로 잇는다
        out += f" 엣지가 없어도 이 범위에 들어올 확률이 {'99%를 넘어' if p.endswith('넘게') else p + '라'} 거래가 더 쌓여야 판단할 수 있어요."
    return out


def _execution_cause(n: dict, market: str | None) -> str:
    """「실행에서 새나」 근거: 실제와 비용 0 백테스트의 차이 + 놓친 주문 + 체결가 차이 + 펀딩비.

    펀딩비는 가드레일 `target.market`이 선물일 때만 말한다: 값이 있으면 그 값, None이면 받은 범위가 구간을 덮지 못했다는 뜻이다.
    현물이면 말하지 않는다(엔진은 현물에도 `funding` 키를 쓰고 값은 None이라 키 유무로는 가를 수 없다).

    Args:
        n: 마지막 기록의 nums.
        market: 가드레일 `target.market`(`spot` · `futures`). 모르면 None(펀딩비를 말하지 않음).

    Returns:
        평문 한 문장 이상.
    """
    out = f"실제와 비용 0 같은 기간 백테스트의 차이는 {_be(pp(n.get('gap')))}."
    if (missed := n.get("missed_n")):
        out += f" 백테스트에만 있는 거래 {missed:,}건이 놓친 주문으로 추정돼요."
    med, mx = num(n.get("slip_med_bp")), num(n.get("slip_max_bp"))
    if med is not None or mx is not None:
        bp = lambda v: "모름" if v is None else f"{v:,.1f}bp"  # noqa: E731
        out += f" 체결가 차이 중앙값 {bp(med)}, 최대 {_be(bp(mx))}."
    if market == "futures":
        out += f" 펀딩비는 {_be(pp(n['funding']))}." if num(n.get("funding")) is not None else " 펀딩비는 받은 범위가 구간을 덮지 못해 모름이에요."
    return out


def causes(rec: dict, market: str | None) -> list[tuple[str, str, str]]:
    """원인 셋 표의 세 줄: (원인 이름, 칩 낱말, 근거 한 문장). 근거는 칸 상태(`states[칸]`)로 고른 템플릿이고 엔진의 `lines`는 쓰지 않는다.

    칸이 「확인 불가」 · 「점검 불가」면 근거 대신 `판단하지 않았어요(괄호 사유, 없으면 기록이 없어요).`다.

    Args:
        rec: 마지막 기록 줄(`states` · `nums`).
        market: 가드레일 `target.market`(펀딩비 문장을 낼지 정함, `_execution_cause`).

    Returns:
        [(원인 이름, 칩 낱말(괄호 사유를 뗌), 근거 평문)] 전략 · 실행 · 봇 순서. 근거에는 레포 글이 들 수 있어 내기 전에 `text`를 거쳐야 한다.
    """
    out = []
    for k, q in _Q.items():
        state = rec["states"][k]
        word = base(state)
        if word in ("확인 불가", "점검 불가"):
            why = state.partition("(")[2].removesuffix(")")
            ev = f"판단하지 않았어요({why or '기록이 없어요'})."
        elif k == "전략":
            ev = _strategy_cause(word, rec["nums"])
        elif k == "실행":
            ev = _execution_cause(rec["nums"], market)
        else:
            n = rec["nums"]
            lo, hi = n.get("tf_low"), n.get("tf_high")
            ev = ("최근 7일 거래는 아직 비교하지 않았어요." if n.get("n7") is None else
                  f"최근 7일 거래는 {n['n7']:,}건이고 백테스트 기준은 " + (f"{lo:,}~{hi:,}건이에요." if lo is not None and hi is not None else "모름이에요."))
        out.append((q, word, ev))
    return out


def _a_legend(recs: list[dict], marks: dict) -> str:
    """차트 A 범례: 값은 마지막 기록의 nums를 터미널 `chart_a`와 같은 자리에서 읽고, 경고선은 `a_marks.line`이다.

    Args:
        recs: 이 전략의 기록 전체.
        marks: `data.a_marks(recs, g)` 결과.

    Returns:
        범례 HTML. 그리지 않은 것(정상 범위 · 한 달 범위 · 놓친 주문)은 뺀다.
    """
    n, band = recs[-1]["nums"], marks["band"]
    items: list[tuple[str, str]] = []
    if band:
        lo, hi, d = n.get("lo"), n.get("hi"), n.get("d")
        items.append(("band", f"오늘{'' if d is None else f'({d}일째)'} 정상 범위 " + ("모름" if lo is None or hi is None else f"{pct(lo)}~{pct(hi)}")))
        if n.get("month_lo") is not None and n.get("month_hi") is not None:
            items.append(("", f"30일 범위 {pct(n['month_lo'])}~{pct(n['month_hi'])}"))

    def inside(pos: Any) -> str:  # 띠를 그렸을 때만 안 · 위 · 아래
        return f" 범위 {pos}" if band and pos else ""

    items += [("bt", f"같은 기간 백테스트 {pct(n.get('bt'))}{inside(n.get('bt_pos'))}"), ("actual", f"실제 {pct(n.get('actual'))}{inside(n.get('pos'))}")]
    line, room = marks["line"], n.get("room")
    items.append(("line", "경고선 여유 모름" if room is None or line is None else
                  f"경고선 {pct(line)}까지 {room * 100:.1f}%p 남았어요" if room >= 0 else f"경고선을 {-room * 100:.1f}%p 넘었어요"))
    if dots := _missed_dots(recs, marks):  # 점이 하나도 안 찍히면 항목도 없다(터미널 `dots > 0`)
        items.append(("missed", f"놓친 주문 {n.get('missed_n') or len(dots):,}건"))
    return _legend(items)


def _panel(*parts: str) -> str:
    """패널 하나(`<section class="panel">`).

    Args:
        *parts: 안에 넣을 HTML 조각들.

    Returns:
        HTML.
    """
    return f'<section class="panel">{"".join(parts)}</section>'


def _a_panel(recs: list[dict], marks: dict) -> str:
    """「지금 위험한가」 패널: 차트 A + 범례 + (띠가 없으면) 안팎을 말할 수 없다는 글. 그릴 숫자가 없으면 그 한 줄.

    Args:
        recs: 이 전략의 기록 전체.
        marks: `data.a_marks` 결과.

    Returns:
        HTML.
    """
    head = '<h2 class="h2">지금 위험한가</h2><p class="sub">실제 손익과 같은 기간 백테스트, 경고선을 정상 범위와 함께 봐요.</p>'
    if not any(r["nums"].get(k) is not None for r in recs for k in ("actual", "bt")):  # 터미널과 같이 대조 실패 기록뿐
        return _panel(head, '<p class="empty">그릴 숫자가 없어요. 매일 대조를 확인하지 못했어요.</p>')
    why = marks["reason"]
    note = ("" if marks["band"] else
            f'<p class="sub">{text("정상 범위를 그리지 않아 안팎을 말할 수 없어요" + (f"({why})" if why else "") + ".")}</p>')
    return _panel(head, a_svg(recs, marks), _a_legend(recs, marks), note)


def _cause_panel(rec: dict, market: str | None) -> str:
    """「원인 셋」 패널: 원인 · 상태 칩 · 근거 한 문장 세 줄(1024 이상은 표, 미만은 쌓은 목록).

    Args:
        rec: 마지막 기록 줄.
        market: 가드레일 `target.market`.

    Returns:
        HTML.
    """
    got = causes(rec, market)
    rows = [f'<tr class="row"><th scope="row" class="cell cell-name">{q}</th><td class="cell">{chip(w)}</td><td class="cell">{text(ev)}</td></tr>'
            for q, w, ev in got]
    return _panel('<h2 class="h2">어디서 생긴 문제예요?</h2><p class="sub">전략이 망가졌는지, 실행에서 새는지, 봇이 멈췄는지 따로 봐요.</p>',
                  _table("원인 셋 표", (("원인", ""), ("상태", ""), ("근거", "")), rows, " tbl-fit"),
                  _stack("원인 셋 목록", [(f'<span class="item-name">{q}</span>{chip(w)}', f'<div class="why">{text(ev)}</div>', [], "") for q, w, ev in got], fit=True))


def _e_notes(e: dict, cap: int) -> list[str]:
    """차트 E 표본 고지 문장들: 기록 상한 · 화면 상한 · 빠진 거래 · 기록에 없는 청산 · 제외한 청산. 해당하는 것만.

    Args:
        e: `data.e_series` 결과.
        cap: 화면 상한(그릴 수 있는 거래 수).

    Returns:
        평문 문장 목록.
    """
    out, drawn = [], len(e["rets"])
    if e["lost"]:
        if e["capped"]:
            out.append(f"기록 상한(구간마다 {TRADE_KEEP:,}건)에 걸려 앞쪽 거래가 빠진 구간이 있어요: " + ", ".join(f"{d}부터" for d in e["capped"]) + ".")
        if e["kept"] > drawn:
            out.append(f"화면 상한 {cap:,}건을 넘어 앞쪽 {e['kept'] - drawn:,}건은 그리지 않았어요.")
        out.append(f"빠진 거래 {e['lost']:,}건이라 점과 평균은 그린 표본 {drawn:,}건 기준이에요.")
    if e["unseen"] > 0:
        out.append(f"매일 대조 기록에 없는 청산 {e['unseen']:,}건이 있어요.")
    if e["skip"] > 0:
        out.append(f"실전 전에 연 포지션의 청산 {e['skip']:,}건은 표본에서 뺐어요.")
    return out


def _e_panel(e: dict, cap: int, *, nth: int) -> str:
    """「거래 단위로 보기」 패널: 차트 E + 범례 + 옆 목록(거래 수 막대 · 평균 · 확률 · 거래 빈도 · 표본 고지). 표본이 없으면 그 한 줄.

    「그린 표본」은 `len(e["rets"])`다: 터미널 E의 「점 · 평균은 남은 {len(rets):,}건 기준」과 같고, 실제 평균(`run[-1]`)의
    분모이기도 하다. `e["kept"]`는 화면 상한으로 자르기 전 수라 상한에 걸리면 그리지 않은 거래까지 세게 된다.

    Args:
        e: `data.e_series` 결과.
        cap: 화면 상한.
        nth: 이 화면 안 차트 E의 순번(1부터, `e_svg`로 넘김).

    Returns:
        HTML.
    """
    head = '<h2 class="h2">거래 단위로 보기</h2><p class="sub">전략이 망가졌는지 날짜가 아니라 거래 수로 봐요.</p>'
    rets = e["rets"]
    if not rets:
        return _panel(head, '<p class="empty">거래 1건 수익 기록이 아직 없어요. 다음 매일 대조부터 세어요.</p>')
    n, minimum = e["n"], e["minimum"]
    band = e["p10"] is not None and e["p90"] is not None
    legend = _legend([("dot", "실제 거래 1건"), ("run", "실제 누적 평균"), *([("band", "백테스트 거래 가운데 80%")] if band else []),
                      *([("mean", "백테스트 평균")] if e["mean"] is not None else []), *([("count", f"{minimum:,}건 선")] if e["line30"] else [])])
    no_band = "" if band else '<p class="sub">백테스트 거래 가운데 80%는 다음 가드레일 저장부터 그려요</p>'
    enough = (f"거래 {n:,}건으로 판단 기준 {minimum:,}건을 넘었어요" if n >= minimum else f"거래 {n:,}건이 쌓였고 판단에는 {minimum:,}건이 필요해요")
    meter = f'<div class="meter" aria-hidden="true"><div class="meter-fill" style="--fill:{min(n / minimum, 1):.3f}"></div></div>'
    items = [f"<li>{enough}{meter}</li>",
             f'<li class="keep">거래 1건 평균 실제 {pct(e["run"][-1], 2)}  백테스트 {pct(e["mean"], 2)}</li>']
    if e["edge0"] is not None:
        items.append(f"<li>엣지가 없어도 이 범위에 들어올 확률 {prob(e['edge0'])}</li>")
    if e["n7"] is None:
        items.append("<li>최근 7일 거래는 아직 비교하지 않았어요.</li>")
    else:
        lo, hi = e["tf_low"], e["tf_high"]
        items.append(f"<li>최근 7일 거래 {e['n7']:,}건" + (f"(백테스트 기준 {lo:,}~{hi:,}건)" if lo is not None and hi is not None else "") + "</li>")
    items.append(f'<li>청산 {e["exits"]:,}건, 수익 표본 {n:,}건, 그린 표본 {len(rets):,}건이고 점과 실제 평균은 그린 표본 {len(rets):,}건으로 냈어요.</li>')
    items += [f"<li>{text(x)}</li>" for x in _e_notes(e, cap)]
    return _panel(head, f'<div class="g2"><div class="stack">{e_svg(e, nth=nth, legend=legend)}{no_band}</div><ul class="list">{"".join(items)}</ul></div>')


def _criteria_panel(g: dict, version: int) -> str:
    """「가드레일 v{N}」 패널: 실돈 넣기 전에 정한 기준 네 줄(정상 범위 · 경고선 · 판단 보류 · 시도 수 보정).

    선택 칸(`cost`)은 `obj` · `num`으로 모양을 검사해 어긋나면 그 줄의 그 부분만 뺀다.

    Args:
        g: 가드레일 내용(`read_guardrail`이 읽는 칸의 모양은 검사됨).
        version: 판 번호.

    Returns:
        HTML.
    """
    nr, wl, jh, cost = (obj(g.get(k)) for k in ("normal_range", "warning_line", "judgment_hold", "cost"))
    days = num(nr.get("days"))
    rng = (f"{'' if days is None else f'{int(days)}일 '}범위 {pct(nr.get('low'))}~{pct(nr.get('high'))}" if nr.get("drawn") is True else
           "그리지 않았어요" + (f"({nr['reason']})" if isinstance(nr.get("reason"), str) and nr["reason"] else ""))
    if (bp := num(cost.get("one_way_bp"))) is not None:
        rng += f", 보수적 비용 편도 {bp:g}bp 기준"
    minimum = num(jh.get("min_trades"))
    rows = [("정상 범위", rng), ("경고선", f"{pct(wl.get('line'))}, 민감도 {wl.get('sensitivity')}"),
            ("판단 보류", f"거래 {'모름' if minimum is None else f'{int(minimum):,}'}건 전이거나 엣지가 없어도 범위 안일 확률이 "
                      f"{prob(jh.get('hold_if_edge0_at_least'))} 이상이면 판단 보류예요."),
            ("시도 수 보정", "시도 수 보정은 정상 범위에 넣지 않고 판단 보류 옆에 숫자로만 적어요. 판단 보류 계산에는 아직 쓰지 않아요.")]
    body = [f'<tr class="row"><th scope="row" class="cell cell-name">{k}</th><td class="cell">{text(v)}</td></tr>' for k, v in rows]
    return _panel(f'<h2 class="h2">가드레일 v{version}(실돈 넣기 전에 정한 기준)</h2>', _table("가드레일 기준 표", (("기준", ""), ("정한 값", "")), body, " tbl-fit"),
                  _stack("가드레일 기준 목록", [(f'<span class="item-name">{k}</span>', f"<div>{text(v)}</div>", [], "") for k, v in rows], fit=True))


_RESPONSE_LABEL = {"execution_warning": "실행 경고", "warning_line": "경고선"}
"""대응 칸 이름 → 인용 위 라벨(엔진이 아는 둘). 모르는 칸은 칸 이름 그대로."""

RESPONSE_NOTE = "엔진은 주문을 내거나 봇을 멈추지 않아요. 멈출지는 직접 정해요."
"""대응 패널 맨 아래 보조 글."""


def _quote(v: str) -> str:
    """고객이 가드레일에 적은 대응 글 한 값을 인용 블록으로. 줄마다 `plain` 뒤 escape만 한다(문구 규칙을 고객 글에 적용하지 않으니 ` · `도 그대로).

    Args:
        v: 대응 글(여러 줄일 수 있음, 줄바꿈은 CSS `pre-wrap`으로 보임).

    Returns:
        `<blockquote class="quote">` HTML.
    """
    return f'<blockquote class="quote">{html.escape(chr(10).join(plain(x) for x in v.splitlines()))}</blockquote>'


def _head_quote(rec: dict) -> list[str]:
    """머리 패널에 둘 「적어 둔 대응」 라벨 + 인용: 지금 걸린 대응 하나. 걸린 것이 없으면 빈 목록.

    걸렸는지는 칩 낱말이 아니라 `reconcile.triggered`가 정한다(대표 칩이 확인 불가 · 점검 불가여도 걸린 대응은 보인다).
    대표 칸(`states.representative`)의 대응이 걸렸으면 그것, 아니면 걸린 것 가운데 첫째다. 고른 대응 글이 비어 있으면 내지 않는다.

    Args:
        rec: 마지막 기록 줄(`states` · `response`).

    Returns:
        [라벨 `<p>`, 인용 `<blockquote>`] 또는 [].
    """
    hit = triggered(rec["states"])
    if not hit:
        return []
    key = RESPONSE_COL.get(representative(rec["states"])[1])
    v = rec["response"].get(key if key in hit else hit[0], "")
    return ['<p class="sub">적어 둔 대응</p>', _quote(v)] if v.strip() else []


def _response_panel(rec: dict) -> str:
    """「대응」 패널: 가드레일에 적어 둔 대응(고객 글)을 값마다 인용 블록으로. 걸린 것을 먼저, 걸릴 수 있는데 안 걸린 것(`RESPONSE_COL` 값)은 「아직 안 걸림」 caption과 함께. 빈 값은 만들지 않는다.

    Args:
        rec: 마지막 기록 줄(`states` · `response`).

    Returns:
        HTML.
    """
    hit = triggered(rec["states"])
    got = sorted(((k, v) for k, v in rec["response"].items() if v.strip()), key=lambda kv: kv[0] not in hit)  # 안정 정렬: 같은 무리 안은 적은 순서
    body = ("".join(f'<p class="sub">{text(_RESPONSE_LABEL.get(k, k))}</p>' + ('<p class="sub">아직 안 걸림</p>' if k in RESPONSE_COL.values() and k not in hit else "") + _quote(v) for k, v in got)
            or '<p class="empty">가드레일에 적어 둔 대응이 없어요.</p>')
    return _panel("<h2 class=\"h2\">대응(가드레일에 적어 둔 것)</h2>", body, f'<p class="sub">{RESPONSE_NOTE}</p>')


def _guard_head(view: dict, token: Ctx) -> str:
    """가드레일 화면 머리: 화면 제목(전략 이름 `h1` + 칩, 패널 밖) + 머리 패널(이유 한 줄 · 걸린 대응 인용 · 보조 글 · 기록이 없을 때의 안내).

    끈 전략은 칩 대신 상태 줄이다. 머리 패널에 넣을 것이 하나도 없으면 패널을 만들지 않는다.

    Args:
        view: `guard`의 view.
        token: 접속 토큰(회고 화면 링크).

    Returns:
        HTML.
    """
    name, recs, g, off = view["name"], view["recs"], view["g"], view["off"]
    chip_html, notes = "", []
    if off:
        state, since = off
        msg = BROKEN_FULL if state == BROKEN else f"{state}한 전략이에요({since}부터). 매일 대조는 멈춰 있어요."
        notes.append(f'<p class="empty">{text(msg)}' + ("" if state == BROKEN else
                     f' 끈 뒤 회고는 <a class="link" href="{href("/s/" + name + "/retro", token)}">회고 화면</a>에서 볼 수 있어요.') + "</p>")
    if view["why"]:
        chip_html = "" if off else chip("확인 불가")
        notes.append(f'<p class="why">{text(view["why"])}</p>')
    elif recs and not off:
        chip_html = chip(view["word"])
        if view["reason"]:
            notes.append(f'<p class="why{" why-warn" if view["word"] == "경고" else ""}">{text(view["reason"])}</p>')
        notes += _head_quote(recs[-1])
    if g is not None:
        set_on = _iso(g.get("set_on"))
        last = recs[-1] if recs else None
        facts = [f"실전 시작 {_iso(g.get('live_start')) or '모름'}", *([f"실전 {last['live_day']:,}일째", f"거래 {last['trades']:,}건"] if last else []),
                 f"가드레일 v{view['version']}" + (f"({set_on} 설정)" if set_on else ""), *([f"기준 {last['day']}"] if last else [])]
        notes.append(f'<p class="sub keep">{text("  ".join(facts))}</p>')
    if g is not None and not recs and not view["why"]:
        notes.append(f'<p class="empty">{EMPTY_DAYS}</p>')
    return f'<div class="head"><h1 class="display">{text(name)}</h1>{chip_html}</div>' + (_panel(*notes) if notes else "")


def guard(view: dict, token: Ctx) -> bytes:
    """가드레일 화면: 머리 → 지금 위험한가(A) · 원인 셋 → 거래 단위로 보기(E) → 가드레일 기준 → 대응.

    기록이 없으면 머리 + 안내 + 기준 표만, 읽지 못한 것이 있으면 머리에 사유 한 줄(가드레일을 못 읽으면 그 줄뿐)이다.

    Args:
        view: `dashboard.guard_view`의 결과. 키: name 전략 이름, off (상태, 시작 날) | None(끈 전략), after after.md 유무, version 판 번호 | None,
            g 가드레일 내용 | None(못 읽음), recs 기록 목록(없거나 못 읽으면 []), why 못 읽은 사유 평문 | None, word 칩 낱말(기록 있는 실전 전략),
            reason 칩 밖 이유 한 줄 | None, marks `data.a_marks` 결과, series `data.e_series` 결과, cap 화면 상한(E 고지에 씀),
            retro 회고 화면이 있는지(내비 칸).
        token: 접속 토큰.

    Returns:
        UTF-8 HTML 바이트.
    """
    recs, g = view["recs"], view["g"]
    parts = [_guard_head(view, token)]
    if recs:
        market = obj(g.get("target")).get("market")
        parts += [f'<div class="g2">{_a_panel(recs, view["marks"])}{_cause_panel(recs[-1], market)}</div>', _e_panel(view["series"], view["cap"], nth=1)]  # E는 한 화면에 하나
    if g is not None:
        parts.append(_criteria_panel(g, view["version"]))
    if recs:
        parts.append(_response_panel(recs[-1]))
    return page(view["name"], "\n".join(parts), token, strategy_nav(view["name"], "guard", True, view["retro"]))


_SIDE = {"sell": "매도", "buy": "매수"}
"""체결 방향 → 화면 낱말(터미널 `_b_labels`와 같음). 모르는 방향은 레포 글 그대로(escape해서)."""


def _side(side: object) -> str:
    """체결 방향을 낱말로: sell · buy는 매도 · 매수, 그 밖은 escape된 원문.

    Args:
        side: 레포에서 온 방향 글.

    Returns:
        화면 글(escape 끝).
    """
    return _SIDE.get(str(side).lower()) or text(side)


def _commas(s: str) -> str:
    """글 속 `4자리 이상 정수 + 건 · 개 · 회 · 번 · 일`에 천 단위 쉼표를 넣는다(웹에서만). 연도 · 소수 · 시각은 단위가 없어 그대로다.

    Args:
        s: 글(저장된 lesson 등).

    Returns:
        쉼표를 넣은 글.
    """
    return re.sub(r"(?<![\d.,:])(\d{4,})(?=건|개|회|번|일)", lambda m: f"{int(m[1]):,}", s)


def _bp(med: Any, mx: Any) -> str | None:
    """체결가 차이 중앙값 · 최대(bp) 한 줄. 둘 다 모르면 None.

    Args:
        med: 중앙값(bp) 또는 None.
        mx: 최대(bp) 또는 None.

    Returns:
        `체결가 차이 중앙값 -5.3bp, 최대 -9.1bp`(하나만 모르면 그쪽은 모름).
    """
    a, b = num(med), num(mx)
    if a is None and b is None:
        return None
    show = lambda v: "모름" if v is None else f"{v:,.1f}bp"
    return f"체결가 차이 중앙값 {show(a)}, 최대 {show(b)}"


def _upto(day: str | None) -> str:
    """마지막 날에 값이 없어 옛 날 값을 보일 때 붙이는 `(YYYY-MM-DD까지)`(escape됨). 날이 없으면 빈 글."""
    return f"({text(day)}까지)" if day else ""


def _numbers_panel(p: dict) -> str:
    """회고 큰 숫자 셋 패널: 구간 제목 + 같은 기간 백테스트 · 실제 · 차이(비용 0). 값이 없으면 모름, 마지막 날 값이 아니면 `(날까지)` 줄.

    Args:
        p: `dashboard.retro_view`의 periods 항목(`title` · `head`: 키 → (값, 날 | None)).

    Returns:
        HTML.
    """
    def big(label: str, key: str, fmt: Callable[[Any], str]) -> str:
        value, day = p["head"][key]
        return f'<div><dt class="sub">{label}</dt><dd class="display">{fmt(value)}' + (f'<div class="sub">{_upto(day)}</div>' if day else "") + "</dd></div>"

    return _panel(f'<h2 class="h2">{text(p["title"])}</h2>', '<dl class="pairs">' + big("같은 기간 백테스트", "bt", pct) + big("실제", "actual", pct)
                  + big("차이(비용 0)", "gap", pp) + "</dl>")


def parts_panel(p: dict, pair: bool = False) -> str:
    """「차이가 어디서 났나」 패널: 체결가 차이 · 수수료 · (선물이면) 펀딩비 · 나머지 줄, 줄마다 값 글과 막대(길이 = |값| / 이 칸 줄들의 |값| 최대).

    막대는 꾸밈이라 보조 기술에 숨기고 채움은 모두 ink다(경고는 글이 한다). 나머지는 추정이라고 적고 원인 후보(백테스트에만 있는 거래 건수)를 말한다.
    놓친 주문 하나에 손익을 붙이지 않는다. 체결가 차이 중앙값 · 최대(bp)는 이번 구간 칸(`bp`가 있을 때)에만 체결가 차이 줄 아래에 두고, 실제에만 있는 체결은 실전 시작부터 칸(`whole`)에만 나머지 아래에 둔다.

    Args:
        p: `dashboard.retro_view`의 periods 항목(`title` · `whole` · `futures` · `parts`: 키 → (값, 날 | None) · `missed_n` · `extra` · `bp`).
        pair: 구간 칸이 둘이면 True. 제목 뒤에 아래 줄(`title`)이 쓰는 구간 이름을 괄호로 붙여 두 제목이 같지 않게 한다(WCAG 2.4.6).

    Returns:
        HTML.
    """
    rows = [("체결가 차이", "slip"), ("수수료", "fee"), *((("펀딩비", "funding"),) if p["futures"] else ()), ("나머지", "rest")]
    top = max((abs(v) for _, k in rows if (v := num(p["parts"][k][0])) is not None), default=0.0)
    items = []
    for label, key in rows:
        value, day = p["parts"][key]
        v = num(value)
        note = ""
        if key == "slip" and p["bp"] is not None and (line := _bp(p["bp"]["med"][0], p["bp"]["max"][0])):
            note = f'<div class="why">{line}{_upto(p["bp"]["med"][1] or p["bp"]["max"][1])}</div>'
        if key == "rest":
            n = num(p["missed_n"][0]) or 0
            note = f'<div class="why">(추정{f", 원인 후보: 백테스트에만 있는 거래 {int(n):,}건" if n else ""})</div>'
            if p["whole"] and (extra := num(p["extra"][0])) and extra > 0:  # nums.all.extra: 행은 없다
                note += f'<div class="why">실제에만 있는 체결 {int(extra):,}건(원인 모름)</div>'
        items.append(f'<li class="item"><div class="item-head"><span class="item-name">{label}</span><span>{pp(value)}{_upto(day)}</span></div>'
                     f'<div class="meter" aria-hidden="true"><div class="meter-fill" style="--fill:{0.0 if v is None or not top else abs(v) / top:.3f}"></div></div>{note}</li>')
    head = f'차이가 어디서 났나({text(p["title"].partition("(")[0])})' if pair else "차이가 어디서 났나"
    return _panel(f'<h2 class="h2">{head}</h2>', f'<p class="sub">{text(p["title"])}</p>', f'<ul class="rows">{"".join(items)}</ul>')


def _b_legend(lab: dict, swatch: bool) -> str:
    """차트 B 범례: 잰 것 · 나머지(추정) · (놓친 주문 후보가 있으면) 처음 후보 · 합계. 숫자는 `data.b_series`의 라벨 값(터미널 `_b_labels`와 같음).

    Args:
        lab: `b_series(recs)["labels"]`.
        swatch: 견본(막대 · 선 모양)을 붙일지(그림이 있을 때만).

    Returns:
        범례 HTML. 시각은 UTC `YYYY-MM-DD HH:MM`, 날은 `YYYY-MM-DD`다.
    """
    items = [("slow" if swatch else "", f"잰 것(체결가 차이, 수수료{', 펀딩비' if lab['funding'] else ''}) {pp(lab['slow'])}"),
             ("fast" if swatch else "", f"나머지(추정) {pp(lab['fast'])}")]
    if (m := lab["first_missed"]) is not None:
        items.append(("", f"처음 후보 {_utc(m['t']):%Y-%m-%d %H:%M} UTC {_side(m['side'])}"))  # b_series가 이미 바꿔 본 시각이라 실패하지 않는다
    items.append(("run" if swatch else "", f"합계{_upto(lab['upto'])} {pp(lab['gap'])}"))
    return _legend([(k, t) for k, t in items])


_B_EMPTY = {"none": "차이를 낸 날이 없어요(실행 칸을 확인하지 못했어요).", "zero": "차이가 0.0%p라 그릴 면적이 없어요.",
            "unknown": "두 기여를 모두 확인한 날이 없어 차이를 몰라요."}
"""그릴 면적이 없을 때의 한 줄(`b_series` empty 종류 → 글). `zero_upto`는 날이 들어가 `b_svg`가 짠다."""


def b_svg(series: dict) -> str:
    """차트 B(차이가 어디서 났나, 날마다): 날마다 0%p 위 · 아래로 쌓은 막대 두 겹 + 합계 선 + 0%p 선 + 구간 경계. 그릴 면적이 없으면 SVG 없이 해요체 한 줄.

    x는 첫 날부터의 날 수(빈 날은 칸으로 남음)이고 칸 폭 = 그림 폭 / 날 수라 점이 없어 겹침이 없다(점 지름 규칙은 B에 닿지 않음). 아래로는 잰 것(slow: 체결가 차이 + 수수료
    + 펀딩비, muted 알파 .55 + 사선 무늬)을 먼저, 나머지(fast: caution 알파 .55, 무늬 없음)를 그 밖으로 쌓는다. 두 면의 대비가 1.115라 무늬와 범례 낱말로 가른다.
    SVG에 `<text>`는 없고 축 글은 HTML이다.

    Args:
        series: `data.b_series(recs)` 결과.

    Returns:
        HTML(그림 + 범례, 또는 안내 한 줄).
    """
    empty, lab = series["empty"], series["labels"]
    if empty is not None:
        line = f"{text(empty['seen_day'])}까지 차이가 0.0%p라 그릴 면적이 없어요." if empty["kind"] == "zero_upto" else _B_EMPTY[empty["kind"]]
        return f'<p class="empty">{line}</p>' + ("" if empty["kind"] in ("none", "zero", "unknown") else _b_legend(lab, False))
    days = series["days"]
    first, last = date.fromisoformat(days[0]["day"]), date.fromisoformat(days[-1]["day"])
    n = (last - first).days + 1
    col = (_CW - 2 * _CPAD) / n
    bar = min(col * 0.8, 48.0)
    y, ticks = _y_axis([series["low"], series["high"], 0.0], "%p")
    bars, runs, cur, prev = [], [], [], 0
    cuts = []
    for d in days:
        i = (date.fromisoformat(d["day"]) - first).days
        cx = _CPAD + (i + 0.5) * col
        if d["start"]:
            cuts.append(f'<line class="s-start" x1="{_CPAD + i * col:.1f}" y1="0" x2="{_CPAD + i * col:.1f}" y2="{_CH}"/>')
        parts = [(r, d[r]) for r in ("slow", "fast") if d[r]]
        for sign, group in ((-1, [x for x in parts if x[1] < 0]), (1, [x for x in parts if x[1] > 0])):
            at = 0.0
            for role, v in group:
                a, b = y(at), y(at + v)
                bars.append(f'<rect class="s-{role}" x="{cx - bar / 2:.1f}" y="{min(a, b):.1f}" width="{bar:.1f}" height="{abs(b - a):.1f}"/>')
                at += v
        if d["gap"] is None or d["start"] or (cur and i != prev + 1):  # 값이 없는 날 · 새 구간 · 빈 날에서 합계 선을 끊는다
            runs, cur = ([*runs, cur] if cur else runs), []
        if d["gap"] is not None:
            cur.append((cx, y(d["gap"])))
            prev = i
    runs = [*runs, cur] if cur else runs
    pattern = ('<defs><pattern id="b-hatch" patternUnits="userSpaceOnUse" width="6" height="6" patternTransform="rotate(45)">'
               '<rect class="s-slow-sw" width="6" height="6"/><line class="s-hatch" x1="0" y1="0" x2="0" y2="6"/></pattern></defs>')
    shapes = (pattern + "".join(bars) + f'<line class="s-zero" x1="{_CPAD}" y1="{y(0):.1f}" x2="{_CW - _CPAD}" y2="{y(0):.1f}"/>' + "".join(cuts)
              + "".join(f'<polyline class="s-run" points="{_pts(r)}"/>' for r in runs))
    label = (f"{text(days[0]['day'])}부터 {text(days[-1]['day'])}까지 실제와 비용 0 같은 기간 백테스트의 차이가 어디서 났는지 날마다 쌓아 그린 차트예요.")
    svg = f'<svg class="chart" role="img" aria-label="{label}" viewBox="0 0 {_CW} {_CH}" preserveAspectRatio="none">{shapes}</svg>'
    return _fig(svg, ticks, text(days[0]["day"]), text(days[-1]["day"])) + _b_legend(lab, True)


def _b_panel(view: dict) -> str:
    """차트 B 패널. 값을 만들다 실패했으면(놓친 주문 시각 변환 등) 이 패널에만 사유 한 줄.

    Args:
        view: `retro_view` 결과.

    Returns:
        HTML.
    """
    head = '<h2 class="h2">차이가 쌓인 모습</h2><p class="sub">실제와 비용 0 같은 기간 백테스트의 차이를 날마다 잰 것과 나머지로 나눠 쌓아요.</p>'
    return _panel(head, f'<p class="why">{text(view["b_error"])}</p>' if view["b_error"] else b_svg(view["b"]))


def fills_panel(view: dict) -> str:
    """「체결 대조」 패널: ① 매일 대조가 저장한 요약(이번 구간 nums) ② 첫 회고 표가 어디 있는지. 건별 짝은 저장되지 않아 만들지 않는다.

    ① 놓친 주문 후보 행(시각 UTC · 종목 · 방향) + 기록 상한에 걸렸으면 안내 + 체결가 차이 중앙값 · 최대(bp) + 실제에만 있는 체결 + 고지 한 줄.
    ② 첫 회고(`retro.md`)가 일반 파일이면 경로 줄, 아니면 AI에게 부탁하라는 줄. retro.md는 파싱하지 않는다. 매일 대조 기록이 없으면 ①은 안내 한 줄.

    Args:
        view: `retro_view` 결과(`name` · `error` · `has_days` · `fills` · `first_retro`).

    Returns:
        HTML.
    """
    f, out = view["fills"], ['<h2 class="h2">체결 대조</h2>']
    if len(view["periods"]) > 1:  # 칸이 둘이면 이 요약은 이번 구간 값이다
        out.append(f'<p class="sub">{text(view["periods"][0]["title"])} 값이에요.</p>')
    if view["error"]:
        out.append(f'<p class="why">{text(view["error"])}</p>')
    elif not view["has_days"]:
        out.append(f'<p class="empty">{EMPTY_DAYS}</p>')
    elif f["error"]:
        out.append(f'<p class="why">{text(f["error"])}</p>')
    else:
        rows = f["rows"]
        if rows:
            memo = "백테스트에만 있는 거래(놓친 주문으로 추정)"
            heads = (("시각(UTC)", ""), ("종목", ""), ("방향", ""), ("뜻", ""))
            out.append(_table("놓친 주문 후보 표", heads, [f'<tr class="row"><td class="cell">{text(r["when"])} UTC</td><td class="cell">{text(r["symbol"])}</td>'
                                                    f'<td class="cell">{_side(r["side"])}</td><td class="cell"><span class="why-warn">{memo}</span></td></tr>' for r in rows])
                       + _stack("놓친 주문 후보 목록", [(f'<span class="item-name">{text(r["when"])} UTC</span>', f'<div class="why why-warn">{memo}</div>',
                                                      [("종목", text(r["symbol"])), ("방향", _side(r["side"]))], "") for r in rows]))
        count = num(f["missed_n"])
        if count is not None and int(count) > len(rows):
            out.append(f'<p class="sub">놓친 주문 후보 {int(count):,}건 가운데 {len(rows):,}건만 기록돼 있어요.</p>')
        if line := _bp(f["med"], f["max"]):
            out.append(f'<p class="sub">{line}{_upto(f["upto"])}</p>')
        if (extra := num(f["extra"])) and extra > 0:
            out.append(f'<p class="sub">실제에만 있는 체결 {int(extra):,}건(원인 모름)</p>')
        out.append('<p class="sub">매일 대조는 건별 체결 짝(가격과 수량)을 저장하지 않아요.</p>')
    out.append(f'<p class="sub">첫 회고 표(백테스트 거래와 실제 체결의 짝)는 .blackclip/strategies/{text(view["name"])}/retro.md에 있어요.</p>' if view["first_retro"]
               else '<p class="sub">건별 짝 표가 필요하면 AI에게 첫 회고를 부탁해 주세요.</p>')
    return _panel(*out)


def _kinds(runs: Sequence[dict]) -> str:
    """기록 없는 날 · 못 받은 기간 목록을 `YYYY-MM-DD부터 YYYY-MM-DD까지(n일)` 글로 이은다.

    Args:
        runs: [{"first", "last"}] (날 글).

    Returns:
        쉼표로 이은 글(escape 전).
    """
    return ", ".join(f"{r['first']}부터 {r['last']}까지({(date.fromisoformat(r['last']) - date.fromisoformat(r['first'])).days + 1:,}일)" for r in runs)


def _one_lines(v: dict) -> list[str]:
    """끈 뒤 칸 ① 실전 기간 전체의 줄들(HTML, escape 끝): `after_values`의 one · top.c0를 값에서 해요체로 다시 짠다. 엔진 설명 글(`one.strategy.line` · 사유 글)은 옮기지 않는다.

    줄: 숫자(또는 읽지 못한 사유) · 구간별 · 전략(칩 + 거래 수가 모자라 판단하지 않았으면 그 말) · 실행(잰 것 · 나머지 · 원인 후보) · 봇(못 받은 기간) · 재현성.
    날은 값에서 `YYYY-MM-DD`로, 시각은 UTC `YYYY-MM-DD HH:MM`으로 짠다(`after_values`가 이미 시각을 바꿔 봤으니 실패하지 않는다).

    Args:
        v: `after_values` 결과.

    Returns:
        줄 HTML 목록. 기록이 없거나 못 읽으면 사유 한 줄(과 재현성)만이다.
    """
    one, out = v["one"], []
    if one["status"] != "ok":
        out.append({"unread": "매일 대조 기록을 읽지 못해 숫자를 쓰지 않았어요.", "no_records": "매일 대조 기록이 없어 숫자를 쓰지 않았어요.",
                    "no_nums": "매일 대조에 숫자가 없어 숫자를 쓰지 않았어요."}[one["status"]])
    else:
        out.append(f"실제 {pct(one['actual'])}, 같은 기간 백테스트 {pct(one['bt'])}, 차이 {pp(one['gap'])}")
        out += [f"구간 {g['first']}부터 {g['last']}까지 실제 {pct(g['actual'])}, 같은 기간 백테스트 {pct(g['bt'])}, 차이 {pp(g['gap'])}" for g in one["segments"]]
    out = [text(x) for x in out]
    if (st := one["strategy"]) is not None:
        hold = f" 거래 {st['trades']:,}건이라 판단하지 않았어요(판단에 {st['min_trades']:,}건 필요)." if st["hold"] else ""
        out.append(f"전략 {chip(base(st['state']))}{text(hold)}")
        x = one["exec"]
        if x["kind"] == "unknown":
            out.append(f"실행 {chip(base(x['state']))}")
        elif x["kind"] == "no_nums":
            out.append("실행은 매일 대조에 숫자가 없어 근거가 없어요.")
        else:
            med = num(x["slip_med_bp"])
            line = f"체결에서 잰 값: 체결가 차이 {pp(x['slip'])}" + ("" if med is None else f"(중앙값 {med:,.1f}bp)") + f", 수수료 {pp(x['fee'])}"
            if x["funding"] is not None:
                line += f", 펀딩비 {pp(x['funding'])}"
            elif x["market"] not in (None, "spot"):
                line += ", 펀딩비 모름"
            n = x["missed_n"] or 0
            if not n:
                line += ". 원인 후보는 없어요."
            else:
                line += f". 나머지 {pp(x['rest'])}(추정, 원인 후보: 백테스트에만 있는 거래 {n:,}건"
                if (m := x["first_missed"]) is not None:
                    line += f", 처음 {_utc(m['t']):%Y-%m-%d %H:%M} UTC {m['symbol']} {_SIDE.get(str(m['side']).lower(), m['side'])}"
                line += ")"
            out.append(text(line))
        b = one["bot"]
        out.append(f"봇 {chip(base(b['state']))}" + text(f" 체결 내역을 못 받은 기간 {_kinds(b['lost'])}" if b["lost"] else " 체결 내역을 못 받은 날은 없어요."))
    if v["top"]["c0"] is not None:
        out.append(text(C0_WORDS.get(v["top"]["c0"], f"재현성 {v['top']['c0']}")))
    return out


def after_panel(view: dict, static: bool = False) -> str:
    """끈 뒤 회고 칸: 끈 전략(중지 · 은퇴)의 회고 화면 맨 위. 머리 문장 · 날 → 끈 이유 → 다음 점검에서 먼저 볼 것 → 실전 기간 전체 → 그 기간의 경고와 고객 결정.

    「그래서 뭐 해야 하나」가 첫 화면에 보이도록 짧은 쪽(끈 이유 · 다음 점검)을 앞에 두고, 기록이 늘수록 길어지는 실전 기간 전체와 타임라인을 뒤에 둔다. 글 · 숫자 · 빈 상태 갈래는 순서만 바뀌었고,
    「실전 N부터 M까지 거래」 줄은 실전 기간 전체의 머리 문장이라 그 앞에서 같이 내려가며, 「근거는 …」 · 미관측 · 봇 쪽 확인 줄은 다음 점검 쪽에 남고, after.md 안내는 맨 끝이다.
    기준 사건은 사건 목록만으로 정한다(`record.after_event`): 중지 → 은퇴면 중지 기준이고 은퇴는 한 줄 덧붙인다. after.md는 파싱하지 않고 일반 파일로 있다는 사실만 말한다.
    기록 손상이면 손상 문장뿐이다. 엔진의 설명 글(`one.strategy.line`, 사유 글 등)은 옮기지 않는다: 날은 값에서 `YYYY-MM-DD`로 다시 짜고, 레포 글은 모두 `text`/`_quote`를 거친다.
    ① 실전 기간 전체(`_one_lines`)는 값에서 다시 짜서 기록이 없거나 읽지 못하는 전략도 이 칸이 혼자 선다.

    Args:
        view: `retro_view` 결과(`name` · `after`: `broken` | `event` · `retire` · `values` · `error` · `lesson` · `file`, `has_days`).
        static: 공개 샘플 데모. 하루짜리 미관측은 「X 하루」로, 보는 사람 맥에 없는 after.md 경로 줄은 뺀다.

    Returns:
        HTML.
    """
    a, name = view["after"], view["name"]
    head = '<h2 class="h2">끈 뒤 회고</h2>'
    if a.get("broken"):
        return _panel(head, f'<p class="empty">{text(BROKEN_FULL)}</p>')
    ev, retire, v = a["event"], a["retire"], a["values"]
    out = [head, f'<p class="empty">{text(ev["state"] + "한 전략이에요(" + ev["day"] + "부터). 매일 대조는 멈춰 있어요.")}</p>']
    if retire is not None:
        out.append(f'<p class="sub">{text("은퇴 " + retire["day"] + ", 이유 " + (retire["reason"] or "이유 미기록"))}</p>')
    if a["error"]:
        out.append(f'<p class="why">{text(a["error"])}</p>')
    out.append('<h3 class="h2">끈 이유</h3>')
    out.append(_quote(v["reason"] if v is not None and v["reason"] else (ev["reason"] or "이유 미기록")))
    lesson = a["lesson"] if a["lesson"] is not None else (v["four"]["lesson"] if v is not None else None)
    if lesson is not None:
        out.append('<h3 class="h2">다음 점검에서 먼저 볼 것</h3>')
        out.append(f'<p class="lesson">{text(_commas(lesson))}</p>')
    if v is not None:
        t, w, four = v["top"], v["two"], v["four"]
        if four["basis"]:
            out.append(f'<p class="sub">근거는 {text(four["basis"]["first"])}부터 {text(four["basis"]["last"])}까지 일별 기록이에요.</p>')
        elif not view["has_days"]:
            out.append('<p class="sub">근거가 된 매일 대조 기록이 이 컴퓨터에 없어요.</p>')
        if (un := four["unseen"]) is not None:
            tail = "마지막 일별 기록 뒤부터 끈 날까지" if un["after_last"] else "실전 시작부터 끈 날까지"
            span = f"{un['first']} 하루" if static and un["first"] == un["last"] else f"{un['first']}부터 {un['last']}까지"
            out.append(f'<p class="sub">미관측 {text(span)}({tail})</p>')
        if four["bot_check"]:
            out.append('<p class="sub">봇 쪽 확인이 필요해요. 점검 항목은 아니에요. 백테스트에만 있는 거래가 왜 없었는지 봇 기록에서 확인해 주세요.</p>')
        trades = "거래 모름" if t["trades"] is None else f"거래 {t['trades']:,}건"
        out.append(f'<p class="sub keep">{text("실전 " + (t["start"] or "모름") + "부터 " + t["day"] + "까지  " + trades)}</p>')
        out.append('<h3 class="h2">실전 기간 전체</h3>')
        lis = "".join('<li class="one">' + x + "</li>" for x in _one_lines(v))
        out.append(f'<ul class="list" aria-label="실전 기간 전체">{lis}</ul>')
        out.append('<h3 class="h2">그 기간의 경고와 고객 결정</h3>')
        st = w["start"]
        out.append(f'<p class="sub">{text("실전 시작 " + st["day"] + ", 가드레일 v" + str(st["version"]) + ", 민감도 " + str(st["sensitivity"]))}</p>' if st
                   else '<p class="sub">가드레일을 읽지 못해 실전 시작일과 판을 몰라요.</p>')
        if w["log_why"]:
            out.append('<p class="sub">state.log를 읽지 못해 매일 대조 기록으로만 썼어요.</p>')
        items = []
        for m in w["timeline"]:
            if m["kind"] == "decision":
                line = f"{m['day']} {m['state']}" + (f", 고객 결정: {m['decision']}" if m["decision"] else "")
            else:
                line = f"{m['day']} {m['field']}: " + (f"{base(m['before'])}에서 " if m["before"] is not None else "") + _ro(base(m['after'])) + " 바뀌었어요"
            items.append(f'<li class="tl">{text(line)}</li>')
        out.append(f'<ul class="list" aria-label="경고와 고객 결정">{"".join(items)}</ul>')
        g = w["gaps"]
        out.append('<p class="sub">' + ("기록 없는 날은 매일 대조 기록을 읽지 못해 몰라요." if g["status"] == "unread" else
                                      "기록 없는 날은 실전 시작일을 몰라 알 수 없어요." if g["status"] == "no_start" else
                                      f"기록 없는 날: {_kinds(g['runs'])}" if g["runs"] else "기록 없는 날은 없어요.") + "</p>")
    if a["file"] and not static:
        out.append(f'<p class="sub">끈 뒤 회고 한 장은 .blackclip/strategies/{text(name)}/after.md에도 있어요.</p>')
    return _panel(*out)


def retro(view: dict, token: Ctx) -> bytes:
    """회고 화면: 제목 → (끈 전략) 끈 뒤 칸 → 큰 숫자 셋(구간 칸 하나 또는 둘) → 차이가 어디서 났나 → 차트 B → 체결 대조 → 바닥 글.

    구간 칸이 하나면 숫자 칸 → 기준 줄 · 비용 안내 → 차이 칸이다. 둘이면 구간별로 묶어 한 격자에 숫자(이번) → 기준 줄 · 비용 안내(감싸개 하나) → 차이(이번) → 숫자(실전) → 차이(실전) 순으로 쌓는다:
    좁은 폭에서 이번 구간의 「나머지」 줄이 첫 화면 안에 들고, 「차이(비용 0)」를 설명하는 비용 안내는 그 숫자 칸 바로 뒤에 있다(1024 이상은 `.g2.by`가 위 줄 = 숫자 둘, 가운데 줄 = 노트, 아래 줄 = 차이 둘로 편다).
    매일 대조 기록이 없으면 숫자 · B 자리에 안내 한 줄이고 체결 대조(첫 회고 표 위치)는 그대로 보인다. 못 읽은 것은 그 칸에만 사유 한 줄이다(화면은 200).

    Args:
        view: `dashboard.retro_view` 결과. 키: name 전략 이름, guard 가드레일 화면이 있는지(내비), off 끈 전략 {"state", "day"} | None, after 끈 뒤 칸 값 | None,
            error 일별 기록을 못 읽은 사유 | None, has_days 일별 기록이 있는지, last 마지막 기록 {"day", "live_day", "trades"} | None, periods 구간 칸 목록
            (이번 구간 · 실전 시작부터, 또는 실전 시작부터 하나), futures 선물인지, b `data.b_series` 결과 | None, b_error 사유 | None, fills 체결 대조 요약, first_retro.
        token: 접속 토큰.

    Returns:
        UTF-8 HTML 바이트.
    """
    name = view["name"]
    out = [f'<div class="head"><h1 class="display">{text(name)}</h1><p class="sub">회고</p></div>']
    if view["off"] is not None:
        out.append(after_panel(view, isinstance(token, Static)))
    if view["error"]:
        out.append(_panel(f'<p class="why">{text(view["error"])}</p>'))
    elif not view["has_days"]:
        out.append(_panel(f'<p class="empty">{EMPTY_DAYS}</p>'))
    else:
        ps, last = view["periods"], view["last"]
        asof = f"기준 {last['day']}  실전 {last['live_day']:,}일째  거래 {last['trades']:,}건"
        notes = [f'<p class="sub keep">{text(asof)}</p>', f'<p class="sub">{DIFF_NOTE}</p>']
        if len(ps) > 1:
            cur, *rest = ps
            grid = _numbers_panel(cur) + f'<div class="stack">{"".join(notes)}</div>' + parts_panel(cur, True) + "".join(_numbers_panel(p) + parts_panel(p, True) for p in rest)
            out.append(f'<div class="g2 eq by">{grid}</div>')
        else:
            out += [f'<div class="stack">{"".join(_numbers_panel(p) for p in ps)}</div>', *notes, f'<div class="stack">{"".join(parts_panel(p) for p in ps)}</div>']
        out.append(_b_panel(view))
    out += [fills_panel(view), f'<p class="sub">{RESPONSE_NOTE}</p>']
    return page(f"{name} 회고", "\n".join(out), token, strategy_nav(name, "retro", view["guard"], True))


# ── 연결 화면 ──────────────────────────────────────────────────────────────────

SETUP_WEB: dict[str, str] = {
    "훑기": "blackclip scan .",
    "조회 키": "아래 「조회 키」에서 넣어 주세요.",
    "점검": "blackclip check {name}",
    "가드레일": "blackclip save {name}",
    "고르기 전": "아래 「고르기」에서 골라 주세요.",
    "토큰 없음": "아래 「텔레그램」에 봇 토큰을 넣어 주세요.",
    "대화 전": "AI에게 텔레그램 대화 잇기를 부탁해 주세요.",
    "예약 없음": "AI에게 매일 대조 예약을 부탁해 주세요.",
    "예약 모름": "이 컴퓨터에서는 예약을 확인하지 못해요. AI에게 예약 확인을 부탁해 주세요.",
    "그 밖": "AI에게 다음 셋업을 부탁해 주세요.",
    "끝": "셋업을 다 했어요.",
}
"""연결 화면 첫 셋업 칸의 지금 단계 한 줄. `훑기` · `점검` · `가드레일`(고른 뒤)은 명령 글자(`.cmd`), 나머지는 해요체 한 줄.
엔진의 `next` · `notes` 글은 웹에 넣지 않는다(터미널 글은 그대로)."""

SETUP_BRANCH: dict[str | None, str] = {
    "있음": "체결 기록이 있어 실전 전략을 먼저 연결하고 첫 회고를 봐요.",
    "없음": "체결 기록이 없어 훑기, 점검, 가드레일 순서로 가요.",
    None: "체결 기록을 아직 받지 않아 어느 길로 갈지 몰라요.",
}
"""첫 셋업 갈래 한 줄."""

CONNECT_FACTS = ("키를 넣은 뒤에는 다시 보여 주지 않아요. 끝 4자리만 보여요.", "주문이나 출금 권한이 켜진 키는 받지 않아요.",
                 "엔진은 키를 AI에게 보내지 않아요.")
"""연결 화면 바닥의 사실 문구. 「AI가 키 파일을 직접 읽지 않게 권한 규칙」 문장은 plugin/에 그 규칙이 없어 넣지 않는다."""

OTHER_ACCOUNT = "다른 계정이나 테스트넷 키는 AI에게 부탁하면 다른 포트에 한 번용 화면을 열어 줘요."
"""상주 화면 쪽 아래 사실 줄의 마지막 글(조회 키 패널에서 내렸다). 한 번용 화면에는 없다."""

_STORE_WORDS: dict[str, str] = {Keychain.name: "이 맥의 키체인", FileStore.name: "이 컴퓨터에서 나만 읽을 수 있는 파일"}
"""키 보관 이름 → 화면 말. 키는 엔진 클래스의 `name` 속성이라 글 비교가 아니다."""


def store_words(name: str) -> str:
    """키 보관 이름을 사람 말로(화면 층에서만: 엔진 `Store.name`과 터미널 글은 그대로).

    Args:
        name: `Store.name`(`keystore.Keychain.name` · `keystore.FileStore.name` 또는 그 밖의 보관).

    Returns:
        키체인 = `이 맥의 키체인`, 파일 보관 = `이 컴퓨터에서 나만 읽을 수 있는 파일`, 그 밖은 엔진 이름 그대로.
    """
    return _STORE_WORDS.get(name, name)

SAVED_UNSURE = "저장했는지 확인하지 못했어요. AI에게 blackclip connect --status를 부탁해 주세요."
"""키 보관 저장이 예외를 냈을 때(이미 썼을 수 있음) 결과 한 줄."""

SAVED_PARTIAL = "키는 저장했어요. 연결 상태는 적지 못했어요. AI에게 blackclip connect --status를 부탁해 주세요."
"""키는 저장했고 연결 상태 기록이 실패했을 때 결과 한 줄."""

ONCE_CLOSE = "이 창을 닫고 AI에게 「넣었어」라고 말해 주세요."
"""한 번용 화면 저장 뒤 한 줄."""

TELEGRAM_NEXT = "대화 잇기는 AI에게 「넣었어」라고 말하면 터미널에서 이어져요."
"""텔레그램 토큰 저장 뒤 한 줄(대화 잇기는 터미널 한 길)."""

_MARKET = {"spot": "현물", "futures": "선물"}
"""시장 이름(화면 말)."""


def _odds(name: str) -> str:
    """민감도 한 칸의 1년 헛경보 확률 글(`notify.sensitivity_text`와 같은 수, 가운뎃점 없이).

    Args:
        name: `SENSITIVITY` 키.

    Returns:
        예: `1년 내내 보면 헛경보 약 20%`, 둔감은 끝에 `, 미검증`.
    """
    from blackclip.sim.warning_line import SENSITIVITY, year_false_alarm

    return f"1년 내내 보면 헛경보 약 {year_false_alarm(SENSITIVITY[name]) * 100:.0f}%" + (", 미검증" if name == "둔감" else "")


def _minute(iso: object) -> str | None:
    """ISO 시각 글을 `YYYY-MM-DD HH:MM`으로(앞 16자가 그 꼴일 때만).

    Args:
        iso: 엔진이 적은 시각 글.

    Returns:
        글 또는 None(꼴이 다름).
    """
    s = str(iso)[:16]
    return s.replace("T", " ") if re.fullmatch(r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}", s) else None


def _held(item: dict) -> str:
    """키 · 토큰 칸 값: `넣었어요`(끝 4자리) · `없어요` · `확인 불가`(사유).

    Args:
        item: {"held": bool | None, "last4": str | None, "why": str | None}.

    Returns:
        HTML.
    """
    if item["why"]:
        return f'확인 불가<div class="sub">{text(item["why"])}</div>'
    return f'넣었어요<div class="sub">끝 4자리 {ident(item["last4"])}</div>' if item["held"] else "없어요"


def _status_panel(view: dict) -> str:
    """① 연결 상태 패널: 조회 키 · 권한 · 체결 받은 범위 · 텔레그램 토큰 · 대화 · 민감도 · 조용한 날 · 실행 경고(값 줄, 못 읽으면 그 칸만 사유).

    Args:
        view: `dashboard.connect_view` 결과.

    Returns:
        HTML.
    """
    from blackclip.fetch import PERMISSION_TEXT

    p = view["permission"]
    if p["why"]:
        perm = f'확인 불가<div class="sub">{text(p["why"])}</div>'
    elif p["status"] in PERMISSION_TEXT:
        when = _minute(p["checked_at"])
        perm = PERMISSION_TEXT[p["status"]] + (f'<div class="sub">{when} UTC 확인</div>' if when else "")  # 적을 때 UTC로 쓴 시각(`note_permission`)
    else:
        perm = "없어요"
    cov = view["coverage"]
    spans = (f'확인 불가<div class="sub">{text(cov["why"])}</div>' if cov["why"] else
             "".join(f'<div>{_MARKET[m]} <span class="nw">{a}</span>~<wbr><span class="nw">{b}</span> UTC</div>' for m, a, b in cov["spans"]) or "없어요")
    c, linked = view["choose"], view["linked"]
    talk = (f'확인 불가<div class="sub">{text(view["choose_why"])}</div>' if view["choose_why"] else "이었어요" if linked else "없어요")
    pairs = [("조회 키", _held(view["key"])), ("권한", perm), ("체결 받은 범위", spans), ("텔레그램 토큰", _held(view["token"])),
             ("텔레그램 대화", talk)]
    if c is not None:
        pairs += [("민감도", f'{text(c["sensitivity"])}<div class="sub">{_odds(c["sensitivity"])}</div>'),
                  ("조용한 날", "매일 한 줄" if c["quiet"] == "매일" else "바뀔 때만"), ("실행 경고", "켜짐" if c["execution_alerts"] else "꺼짐")]
    dl = "".join(f'<div class="span2"><dt>{k}</dt><dd>{v}</dd></div>' if k == "체결 받은 범위" else f"<div><dt>{k}</dt><dd>{v}</dd></div>" for k, v in pairs)
    return _panel('<h2 class="h2">연결 상태</h2>', f'<p class="sub">계정 {text(view["account"])}, 보관 {text(store_words(view["store"]))}</p>',
                  f'<dl class="pairs">{dl}</dl>')


def setup_line(j: dict, token_held: bool) -> str:
    """첫 셋업 칸의 지금 단계 한 줄(`SETUP_WEB`): 명령 글자 셋 · 「예약 · 알림」 넷(+ 예약 모름) · 그 밖은 AI에게 부탁.

    Args:
        j: `stages.judge` 결과(`now` · `strategy` · `alerts`만 씀).
        token_held: 텔레그램 토큰이 보관에 있는지(「예약 · 알림」에서 대화 잇기 전 두 갈래).

    Returns:
        HTML 한 줄(`<code class="cmd">` 또는 `<p class="empty">`).
    """
    now, a = j["now"], j["alerts"]
    if now in ("훑기", "점검") or (now == "가드레일" and a["chosen"]):
        return f'<code class="cmd">{html.escape(SETUP_WEB[now].format(name=j["strategy"] or "<전략>"))}</code>'
    if now is None:
        key = "끝"
    elif now == "가드레일":
        key = "고르기 전"
    elif now == "예약 · 알림":
        key = ("고르기 전" if not a["chosen"] else ("대화 전" if token_held else "토큰 없음") if not a["linked"]
               else "예약 모름" if a["scheduled"] is None else "예약 없음")
    else:
        key = now if now == "조회 키" else "그 밖"
    return f'<p class="empty">{SETUP_WEB[key]}</p>'


def _setup_panel(view: dict, token: str) -> str:
    """② 첫 셋업 패널: 몇 단계까지 했는지 · 갈래 한 줄 · 지금 단계의 웹 한 줄 · 첫 회고가 있는 전략의 회고 화면 링크.

    Args:
        view: `dashboard.connect_view` 결과.
        token: 접속 토큰.

    Returns:
        HTML.
    """
    j = view["setup"]
    if j is None:
        return _panel('<h2 class="h2">첫 셋업</h2>', f'<p class="why">{text(view["setup_why"])}</p>')
    done = sum(d is True for d in j["done"])
    out = ['<h2 class="h2">첫 셋업</h2>', f'<p class="empty">셋업 {len(j["steps"])}단계 가운데 {done}단계까지 했어요.</p>',
           f'<p class="sub">{SETUP_BRANCH[j["branch"]]}</p>', setup_line(j, bool(view["token"]["held"]))]
    if view["retros"]:
        out.append('<p class="sub">첫 회고: ' + " ".join(_name_link(n, token, "/retro") for n in view["retros"]) + "</p>")
    return _panel(*out)


def _fail(note: str | None, kind: str) -> str:
    """패널 설명 줄 아래 · 첫 입력 칸 위의 실패 줄(늘 있는 `role="status"`, 비어 있으면 글 없고 자리도 없음 `.fail:empty`). id는 패널마다 `fail-<kind>`: 잘못된 칸이 `aria-describedby`로 가리킨다.

    Args:
        note: 실패 줄 또는 None. `\n`은 줄 나눔.
        kind: 패널(`key` · `telegram` · `choose`).

    Returns:
        HTML.
    """
    body = "<br>".join(text(line) for line in note.split("\n")) if note else ""  # 줄바꿈은 줄 나눔(거부 줄 두 줄)
    return f'<p class="fail" role="status" id="fail-{kind}">{body}</p>'


def _form(kind: str, token: str, csrf: str, fields: str, note: str | None, lead: bool = True) -> str:
    """POST 폼 하나: 실패 줄 + 칸들 + CSRF 숨김 칸 + 조용한 저장 버튼. 실패 줄이 첫 칸 앞이라 첫 칸에 포커스가 가 있어도(1024×768) 줄이 화면 안에 있다.
    첫 칸이 잘못된 칸이 아닌 폼(고르기)은 `lead=False`로 줄을 붙이지 않고, `fields` 안 잘못된 칸 바로 앞에 `_fail`을 직접 넣는다. 줄 id는 문서에 하나뿐이어야 한다.

    Args:
        kind: `key` · `telegram` · `choose`(주소 `/connect/<kind>`).
        token: 접속 토큰.
        csrf: 폼 CSRF 토큰.
        fields: 칸 HTML.
        note: 실패 줄 또는 None.
        lead: True면 실패 줄을 폼 맨 앞에 붙인다. False면 붙이지 않는다(`fields`가 직접 가지고 있다).

    Returns:
        HTML.
    """
    return (f'<form class="form" method="post" action="{href("/connect/" + kind, token)}" aria-labelledby="h-{kind}">{_fail(note, kind) if lead else ""}{fields}'
            f'<input type="hidden" name="csrf" value="{html.escape(csrf)}">'
            f'<div><button class="btn btn-quiet" type="submit">저장</button></div></form>')


CHECK_FAILED = "check"
"""`connect`의 `bad` 값: 권한 · getMe 확인이 안 된 실패(502). 입력이 틀린 게 아니라서 칸에 `aria-invalid`를 붙이지 않는다(실패 줄 연결 · 포커스는 그대로)."""


def _flag(kind: str, names: Sequence[str], name: str, invalid: bool = True) -> str:
    """잘못된 칸이면 `aria-invalid` + 실패 줄 연결, 첫 칸에는 `autofocus`도 붙는 속성 글(앞 공백 포함, 아니면 빈 글).

    실패 응답은 새 문서라 포커스가 맨 위에 있다. 잘못된 칸으로 포커스를 보내야 줄(칸 바로 위)이 칸과 이어져 읽힌다.

    Args:
        kind: 패널(실패 줄 id `fail-<kind>`).
        names: 잘못된 칸 이름들(문서 순서, 실패가 없으면 빈 칸).
        name: 이 칸 이름.
        invalid: False면 `aria-invalid`를 빼고 실패 줄 연결 · `autofocus`만 붙인다(확인 실패: 입력이 틀린 게 아님).

    Returns:
        속성 글.
    """
    if name not in names:
        return ""
    return (' aria-invalid="true"' if invalid else "") + f' aria-describedby="fail-{kind}"' + (" autofocus" if name == names[0] else "")


def _secret(name: str, label: str, kind: str = "password", *, required: bool = True, flag: str = "") -> str:
    """값을 다시 넣지 않는 입력 칸 하나(`value` 없음, 라벨은 위, 자리표시 없음).

    Args:
        name: 칸 이름(id와 같음).
        label: 라벨 글.
        kind: `password`(자동 채움 `new-password`) 또는 `text`(자동 채움 끔 · 맞춤법 끔).
        required: 꼭 넣는 칸이면 True(헬스체크는 비우면 그대로라 False).
        flag: `_flag` 결과(잘못된 칸이면 속성 글).

    Returns:
        HTML.
    """
    extra = 'type="password" autocomplete="new-password"' if kind == "password" else 'type="text" autocomplete="off" spellcheck="false"'
    return (f'<div class="field"><label class="label" for="{name}">{label}</label>'
            f'<input class="input" id="{name}" name="{name}" {extra}{" required" if required else ""}{flag}></div>')


def _key_panel(view: dict, token: str, csrf: str, note: str | None, once: bool, sent: dict | None = None, bad: str | None = None) -> str:
    """조회 키 패널: 설명 두 문장(조회 권한만 켠 키 · 키는 이 컴퓨터에만 저장) · API 키(글) · 시크릿(비밀) · 저장. 계정은 서버가 정한다. 실패하면 두 칸이 모두 비워지니 둘 다 잘못된 칸으로 가리킨다.

    Args:
        view: `dashboard.connect_view` 결과.
        token: 접속 토큰.
        csrf: 폼 CSRF 토큰.
        note: 이 패널의 실패 줄 또는 None.
        once: 쓰지 않음(패널 모양을 맞춤. 다른 계정 안내는 쪽 아래 사실 줄로 내렸다).
        sent: 쓰지 않음(패널 모양을 맞춤).
        bad: `CHECK_FAILED`면 칸에 `aria-invalid`를 붙이지 않는다(확인 실패). 그 밖은 쓰지 않음.

    Returns:
        HTML.
    """
    sub = (f'<p class="sub">{text(view["account"])} 계정에 조회 권한만 켠 키를 넣어 주세요. '
           f'키는 {text(store_words(view["store"]))}에만 저장하고 바이낸스 조회에만 써요.</p>')
    names = ("api_key", "secret_key") if note else ()
    inv = bad != CHECK_FAILED
    fields = _secret("api_key", "API 키", "text", flag=_flag("key", names, "api_key", inv)) + _secret("secret_key", "시크릿", flag=_flag("key", names, "secret_key", inv))
    return _panel('<h2 class="h2" id="h-key">조회 키</h2>', sub, _form("key", token, csrf, fields, note))


def _telegram_panel(view: dict, token: str, csrf: str, note: str | None, once: bool, sent: dict | None = None, bad: str | None = None) -> str:
    """텔레그램 패널: 설명(토큰은 이 컴퓨터에만 저장 · 알림에만 씀) · 봇 토큰(비밀) · 저장. 새 토큰을 저장하면 전에 이은 대화는 끊긴다.

    Args:
        view: `dashboard.connect_view` 결과(`store`만 씀).
        token: 접속 토큰.
        csrf: 폼 CSRF 토큰.
        note: 이 패널의 실패 줄 또는 None.
        once: 한 번용 화면이면 True(쓰지 않음).
        sent: 쓰지 않음(패널 모양을 맞춤).
        bad: `CHECK_FAILED`면 칸에 `aria-invalid`를 붙이지 않는다(확인 실패). 그 밖은 쓰지 않음.

    Returns:
        HTML.
    """
    sub = (f'<p class="sub">토큰은 {text(store_words(view["store"]))}에만 저장하고 봇 확인, 연결할 채팅 찾기, 알림 보내기에만 써요. '
           "BotFather가 준 토큰을 넣어 주세요. 새 토큰을 저장하면 전에 이은 대화는 끊겨요.</p>")
    field = _secret("bot_token", "텔레그램 봇 토큰", flag=_flag("telegram", ("bot_token",) if note else (), "bot_token", bad != CHECK_FAILED))
    return _panel('<h2 class="h2" id="h-telegram">텔레그램</h2>', sub, _form("telegram", token, csrf, field, note))


def _radio(name: str, value: str, label: str, sub: str, on: bool) -> str:
    """라디오형 고르기 상자 하나(네이티브 `input type=radio` + `label`, 44px 상자).

    Args:
        name: 칸 이름.
        value: 값.
        label: 글.
        sub: 보조 글(빈 글이면 없음).
        on: 지금 골라져 있으면 True.

    Returns:
        HTML.
    """
    tail = f'<span class="sub">{sub}</span>' if sub else ""
    return (f'<label class="opt"><input type="radio" name="{name}" value="{html.escape(value)}" required{" checked" if on else ""}>'
            f"<span>{text(label)} {tail}</span></label>")


def _choose_panel(view: dict, token: str, csrf: str, note: str | None, once: bool, sent: dict | None = None, bad: str | None = None) -> str:
    """고르기 패널: 경고선 민감도 셋 · 조용한 날 둘 · 헬스체크 주소(비밀, 비우면 그대로) · 실행 경고(첫 회고 뒤에만 칸). 지금 값이 골라져 있다.

    실패 응답이면 방금 제출한 값으로 골라 다시 그린다(WCAG 3.3.7, 허용 목록 밖 값은 저장된 값). 헬스체크는 쓰기 전용이라 비운다.
    실패 줄은 잘못된 칸 바로 앞에 둔다: 헬스체크면 그 칸 앞, 실행 경고면 그 fieldset 안(숨김 칸 뒤 · 체크 칸 앞). 잘못된 칸이 폼 깊숙이 있어 포커스 스크롤이 일어나므로
    폼 맨 위에 두면 줄이 화면 밖으로 밀린다. 가리키는 칸이 없거나(`bad`가 None) 그 칸이 그려지지 않았으면(첫 회고 전 실행 경고) 지금처럼 폼 맨 위, 실패가 없어도 빈 줄 하나가 맨 위다.

    Args:
        view: `dashboard.connect_view` 결과(`choose` · `choose_why`).
        token: 접속 토큰.
        csrf: 폼 CSRF 토큰.
        note: 이 패널의 실패 줄 또는 None.
        once: 한 번용 화면이면 True(쓰지 않음).
        sent: 실패한 POST가 제출한 값(`sensitivity` · `quiet` · `execution_alerts` · `ea_shown`) 또는 None.
        bad: 실패가 가리키는 칸 이름(`healthcheck` · `execution_alerts`) 또는 None(칸을 가리키지 않는 실패).

    Returns:
        HTML. 알림 설정을 읽지 못하면 폼 대신 사유 한 줄.
    """
    from blackclip.notify import QUIET
    from blackclip.sim.warning_line import SENSITIVITY

    title = '<h2 class="h2" id="h-choose">고르기</h2>'
    c = view["choose"]
    if c is None:
        return _panel(title, f'<p class="why">{text(view["choose_why"])}</p>', _fail(note, "choose"))
    sent = sent or {}
    sens_on = sent["sensitivity"] if sent.get("sensitivity") in SENSITIVITY else c["sensitivity"]
    quiet_on = sent["quiet"] if sent.get("quiet") in QUIET else c["quiet"]
    ea_on = sent.get("execution_alerts") == "on" if "ea_shown" in sent else c["execution_alerts"]  # `dashboard.form_value`와 같은 읽기
    names = (bad,) if note and bad else ()
    fail = _fail(note, "choose")
    at = names[0] if names and (names[0] == "healthcheck" or (names[0] == "execution_alerts" and c["first_retro"])) else None  # 줄이 가리킬 칸이 실제로 그려질 때만
    sens = "".join(_radio("sensitivity", k, k, _odds(k), k == sens_on) for k in SENSITIVITY)
    quiet = (_radio("quiet", QUIET[0], "매일 한 줄", "매일 묶음이 안 오면 엔진이 멈춘 거예요", quiet_on == QUIET[0])
             + _radio("quiet", QUIET[1], "바뀔 때만", "이때는 바깥 헬스체크 주소를 꼭 넣기를 권해요", quiet_on == QUIET[1]))
    hc = (fail if at == "healthcheck" else "") + _secret("healthcheck", f'헬스체크 주소(https, 비워 두면 그대로예요. 지금 {"있어요" if c["healthcheck"] else "없어요"})',
                                                         required=False, flag=_flag("choose", names, "healthcheck"))
    ea = ('<input type="hidden" name="ea_shown" value="1">' + (fail if at == "execution_alerts" else "") + '<label class="opt"><input type="checkbox" name="execution_alerts" value="on"'
          f'{" checked" if ea_on else ""}{_flag("choose", names, "execution_alerts")}><span>실행 경고 텔레그램 켜기 <span class="sub">놓친 주문과 비싸게 체결된 것을 알려요. 추정이 붙어요.</span></span></label>'
          if c["first_retro"] else '<p class="sub">실행 경고 텔레그램은 첫 회고를 본 뒤 켤 수 있어요.</p>')
    fields = (f'<fieldset class="set"><legend class="h2">경고선 민감도</legend><p class="sub">가드레일을 저장할 때 기본값이에요. 실행 쪽 경고는 이 칸과 상관없이 가요.</p>{sens}</fieldset>'
              f'<fieldset class="set"><legend class="h2">조용한 날 알림</legend>{quiet}</fieldset>{hc}'
              f'<fieldset class="set"><legend class="h2">실행 경고</legend>{ea}</fieldset>')
    return _panel(title, _form("choose", token, csrf, fields if at else fail + fields, note, lead=False))


_PANELS = {"key": _key_panel, "telegram": _telegram_panel, "choose": _choose_panel}
"""kind → 폼 패널 함수."""


def _facts(once: bool) -> str:
    """연결 화면 바닥 사실 문구(`CONNECT_FACTS`, 상주 화면은 끝에 `OTHER_ACCOUNT` 한 줄 더).

    Args:
        once: 한 번용 화면이면 True(다른 계정 안내를 뺌).

    Returns:
        HTML.
    """
    return "".join(f'<p class="sub">{x}</p>' for x in CONNECT_FACTS + (() if once else (OTHER_ACCOUNT,)))


def connect(view: dict, token: str, csrf: str, kind: str, once: bool, note: str | None = None, sent: dict | None = None,
            bad: str | None = None) -> bytes:
    """연결 화면. 상주: 제목 → ① 연결 상태 → ② 첫 셋업 → ③ 폼 패널 셋 → ④ 사실 문구(+ 다른 계정 안내), 내비 `홈` · `연결`.
    한 번용: 제목(`view["title"]`) → 연 kind 패널 하나 → 사실 문구, 내비 없음.

    키 · 시크릿 · 토큰 · 헬스체크 입력 칸에는 값을 넣지 않는다(쓰기 전용). 레포 · 엔진 글은 `text`를 거친다.

    Args:
        view: `dashboard.connect_view` 결과.
        token: 접속 토큰.
        csrf: 폼 CSRF 토큰.
        kind: 실패 줄을 붙일 패널(한 번용이면 보일 패널).
        once: 한 번용 서버 화면이면 True.
        note: kind 패널의 실패 줄(해요체) 또는 None.
        sent: 고르기 실패 뒤 다시 그릴 때 방금 제출한 값(`_choose_panel`이 읽음) 또는 None.
        bad: 고르기 실패가 가리키는 칸 이름 또는 None(`_choose_panel`이 읽음), 또는 `CHECK_FAILED`(키 · 텔레그램 패널이 읽음).

    Returns:
        UTF-8 HTML 바이트.
    """
    def form(k: str) -> str:
        return _PANELS[k](view, token, csrf, note if k == kind else None, once, sent, bad)

    if once:
        body = f'<div class="head"><h1 class="display">{text(view["title"])}</h1></div>{form(kind)}{_facts(True)}'
        return page(view["title"], body, token, ())
    body = (f'<div class="head"><h1 class="display">연결</h1></div>{_status_panel(view)}{_setup_panel(view, token)}'
            f'{form("key")}{form("telegram")}{form("choose")}{_facts(False)}')
    return page("연결", body, token, CONNECT_NAV)


def connect_saved(saved: Any, token: str, once: bool) -> bytes:
    """저장 결과 한 장(성공 · 부분 실패 · 저장했는지 모름). 끝 4자리 · 권한 · 보관 이름만 보인다(키 글자 0).

    Args:
        saved: `connect.Saved`.
        token: 접속 토큰.
        once: 한 번용 서버면 True(내비 없음 · 창 닫기 한 줄), 상주면 연결 화면으로 돌아가는 링크.

    Returns:
        UTF-8 HTML 바이트.
    """
    from blackclip.fetch import PERMISSION_TEXT

    if saved.state_error is not None:
        lines = [text(SAVED_UNSURE if saved.permission is None else SAVED_PARTIAL)]
    elif saved.kind == "choose":
        lines = [text("저장했어요.")]
    else:
        label = "봇 확인" if saved.kind == "telegram" else PERMISSION_TEXT.get(saved.permission.status, "확인 불가")
        lines = [f"{text('저장했어요. 끝 4자리')} {ident(saved.last4)}{text(f', 권한 {label}, 보관 {store_words(saved.store_name)}.')}"]
        if saved.kind == "telegram":
            lines.append(text(TELEGRAM_NEXT))
    if once and saved.state_error is None:
        lines.append(text(ONCE_CLOSE))
    parts = [f'<p class="empty">{x}</p>' for x in lines]
    if not once:
        parts.append(f'<a class="link hit" href="{href("/connect", token)}">연결 화면으로 돌아가기</a>')
    head = '<div class="head"><h1 class="display">연결</h1></div>'
    return page("연결", head + _panel(*parts), token, () if once else CONNECT_NAV)

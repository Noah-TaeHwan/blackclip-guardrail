"""자주 보는 시계 판정 글: 가드레일 선 · 봇(체결 유입 · 7일 거래 빈도 · 포지션 대조), 터미널 한 줄 · 텔레그램 세 줄."""
from __future__ import annotations

from decimal import Decimal

from blackclip.daily.reconcile import LABELS, base, representative, response_text
from blackclip.watch.line import LINE_NOTE
from blackclip.watch.window import CAPTURED, NOT_DIVIDED, window_return

WATCHED: tuple[str, ...] = ("선", "봇")
"""자주 보는 시계가 적고 바뀌면 알리는 칸."""

FLOW_SHOW = Decimal("0.001")
"""바깥 흐름 합이 기준 자산의 이 비율 이상이면 근거에 「입출금 추정」을 적는다(작은 반올림 차이는 안 보임, 손익에서는 늘 뺌)."""

BROKEN = "상태 파일을 읽지 못해 구간 기준을 새로 잡음(지난 손익 · 최저를 잃음) · 다음 확인부터 셈"
"""watch.json이 깨졌을 때 선 근거(이번 확인은 확인 불가)."""


def line_state(win: dict, note: str | None, broken: bool, line: float, sensitivity: str) -> tuple[str, str]:
    """가드레일 선: 이번 구간 최저 구간 수익이 1시간 확인 경고선 이하면 경고.

    Args:
        win: 이번 확인 뒤 "window" 칸(window.advance 결과).
        note: advance가 준 말(CAPTURED · NOT_DIVIDED · None).
        broken: 상태 파일이 깨져 다시 잡았는지.
        line: 고른 민감도의 1시간 확인 경고선.
        sensitivity: 민감도 이름.

    Returns:
        (상태, 근거).
    """
    if broken:
        return "확인 불가", BROKEN
    if note == NOT_DIVIDED:
        return "확인 불가", NOT_DIVIDED
    r = window_return(win)
    if r is None:
        return "확인 불가", "지금 자산이 0 이하라 구간 수익을 셀 수 없음(입금되면 다음 확인부터)"
    low = float(win["low"])
    text = f"구간 수익 {r:+.1%}(이번 구간 최저 {low:+.1%}) · 경고선 {line:.1%}(민감도 {sensitivity} · {LINE_NOTE})"
    if note == CAPTURED:
        text = f"{CAPTURED} · {text}"
    b, flows = Decimal(win["base"]), Decimal(win["flows"])
    if b > 0 and abs(flows) >= FLOW_SHOW * b:
        text += f" · 입출금 추정 · 손익에서 뺌(기준 대비 {float(flows / b):+.1%})"
    return ("경고" if low <= line else "정상"), text


def bot_state(cause: tuple[str, str], position: tuple[str, str]) -> tuple[str, str]:
    """봇: 체결 유입 · 7일 빈도(daily.reconcile.bot_cause)와 포지션 대조를 합친다. 체결을 못 받았으면 그것만.

    Args:
        cause: bot_cause 결과.
        position: position.compare 결과(BT 목표를 못 냈으면 (「확인 불가」, 까닭)).

    Returns:
        (상태, 근거). 상태는 둘 중 우선순위가 앞선 것.
    """
    if base(cause[0]) == "확인 불가":
        return cause
    return representative({"빈도": cause[0], "포지션": position[0]})[0], f"{cause[1]} · {position[1]}"


def terminal_line(name: str, states: dict[str, str], lines: dict[str, str]) -> str:
    """터미널에 본 전략마다 한 줄.

    Args:
        name: 전략 이름.
        states: 칸 → 상태.
        lines: 칸 → 근거.

    Returns:
        한 줄.
    """
    rep, _ = representative(states)
    return f"{name}: {rep} · 가드레일 선 {states['선']} · {lines['선']} · 봇 {states['봇']} · {lines['봇']}"


def message(name: str, states: dict[str, str], lines: dict[str, str], response: dict[str, str]) -> str:
    """상태가 바뀐 전략 하나의 텔레그램 세 줄(결론 · 근거 · 대응).

    Args:
        name: 전략 이름.
        states: 칸 → 상태.
        lines: 칸 → 근거.
        response: 가드레일 대응(warning_line).

    Returns:
        글.
    """
    rep, key = representative(states)
    second = f"가드레일 선: {states['선']} · {lines['선']}"
    if base(states["선"]) == "경고":
        second += f" · 가드레일 대응(내가 정해 둔 것): {response_text(response.get('warning_line'))}"
    return "\n".join([f"{rep} · {LABELS[key]} · {name} · 자주 보는 시계", second, f"봇: {states['봇']} · {lines['봇']}"])


def mixed_message(mixed: dict[tuple[str, str], list[str]]) -> str:
    """섞인 계좌로 본 전략들을 처음 한 번 알리는 세 줄.

    Args:
        mixed: (계좌, 시장) → 전략들(새로 섞인 것만).

    Returns:
        글.
    """
    where = ", ".join(f"binance {a} {m}({', '.join(n)})" for (a, m), n in sorted(mixed.items()))
    return "\n".join(["섞인 계좌라 1시간마다 보지 않고 매일 대조로만 봐요", f"다른 전략과 계좌를 같이 써요: {where}",
                      "전략마다 계좌(서브 계정)를 나누면 1시간마다 봐요"])


MIXED_STATES = {"선": "점검 불가(섞인 계좌)", "봇": "점검 불가(섞인 계좌)"}
"""섞인 계좌로 본 전략의 notified 기록(한 번만 알리게)."""


def plan_watch(results: list[tuple[str, dict[str, str], dict[str, str], dict[str, str]]],
               mixed: dict[tuple[str, str], list[str]], notified: dict[str, dict[str, str]]) -> list[tuple[str, dict[str, dict[str, str]]]]:
    """보낼 글과, 그 글이 가면 notified에 적을 것(`watch:<전략>` 키, 매일 대조 키와 따로).

    Args:
        results: [(전략, 상태, 근거, 대응)].
        mixed: pick의 섞인 계좌.
        notified: messages.load_notified 결과.

    Returns:
        [(글, {키: 상태})]. 상태가 바뀐 전략만, 섞인 계좌는 처음 한 번 한 통.
    """
    out = [(message(n, st, ln, resp), {f"watch:{n}": st}) for n, st, ln, resp in results
           if notified.get(f"watch:{n}") != st]
    new = {k: [n for n in v if notified.get(f"watch:{n}") != MIXED_STATES] for k, v in mixed.items()}
    new = {k: v for k, v in new.items() if v}
    if new:
        out.append((mixed_message(new), {f"watch:{n}": dict(MIXED_STATES) for v in new.values() for n in v}))
    return out

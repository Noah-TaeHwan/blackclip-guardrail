"""blackclip status [전략]: 매일 대조 기록(days.jsonl)과 가드레일을 늘 평문으로 요약한다.

맨 앞 두 줄이 결론 · 손익이고, 숫자와 상태는 daily가 저장한 것을 그대로 옮긴다(여기서 판정 · 계산을 새로 하지 않음).
ESC와 그림 글자를 내지 않고, 레포에서 온 글은 `scan.show`를 거친다. pandas를 부르지 않는다(reconcile을 import하지 않음).
"""
from __future__ import annotations

from pathlib import Path

from blackclip.daily.days import current_period, read_days
from blackclip.hook import _NAME
from blackclip.record import BROKEN, stopped
from blackclip.scan import show
from blackclip.states import LABELS, ORDER, TRACKED, base, pp
from blackclip.workspace import WorkspaceError, exists_regular, read_guardrail, strategy_names, versions

FORBIDDEN: frozenset[str] = frozenset("\x1b▁▂▃▄▅▆▇█━┃┣┫●◆░✓▲◐◌⊘▣⬓⬚⧄╌┄┈│┤├└┬┼─▕▏▔◂")
"""status 출력에 들어가면 안 되는 글자(ESC와 chart의 그림 글자). 시험이 출력 전체를 이 집합으로 가른다."""

NO_GUARDRAIL = "가드레일을 저장한 전략 없음 · blackclip check <전략> → blackclip save <전략>"
"""가드레일이 있는 전략이 하나도 없을 때의 한 줄."""

PARTIAL = 4
"""전체 보기에서 가드레일 · days.jsonl을 읽지 못한 전략(확인 불가)이 있을 때의 종료 코드(daily · watch의 EXIT_PARTIAL과 같은 값, 그쪽은 pandas를 불러와 여기서 다시 정함)."""


def plain(text: str) -> str:
    """출력 경계: 레포 글을 `scan.show`로 거른 뒤 FORBIDDEN 글자(인쇄 가능한 그림 글자 포함)를 평문 `\\uXXXX`로 바꾼다.

    `scan.show`는 인쇄 못 하는 글자만 바꾸므로 레포 글의 「█」 같은 그림 글자는 지나간다. 공용 `show`의 계약은 넓히지 않고 status만 막는다.

    Args:
        text: 출력할 한 줄(레포 글이 섞여 있을 수 있음).

    Returns:
        FORBIDDEN 글자가 없는 글(「█」는 「\\u2588」 글자 그대로).
    """
    return "".join(f"\\u{ord(c):04x}" if c in FORBIDDEN else c for c in show(text))


def strategies(root: Path) -> list[str]:
    """가드레일 판이 있는 전략 이름(전략 이름 모양 · 링크 아닌 폴더만), 이름순.

    Args:
        root: .blackclip 경로.

    Returns:
        이름 목록.

    Raises:
        WorkspaceError: strategies/가 링크이거나 폴더가 아님.
    """
    return [n for n in strategy_names(root) if _NAME.fullmatch(n) and versions(root, n)]


def _why(exc: Exception) -> str:
    """전체 보기에서 한 전략을 읽지 못한 사유: WorkspaceError는 그 메시지, OSError는 종류 이름(daily의 link.py · days.jsonl 처리와 같은 규칙).

    Args:
        exc: read_guardrail · read_days가 낸 WorkspaceError 또는 OSError.

    Returns:
        사유 글(레포 글이 들 수 있어 plain을 거쳐 내야 함).
    """
    return str(exc) if isinstance(exc, WorkspaceError) else type(exc).__name__


def _md(day: str, pad: bool = False) -> str:
    """ISO 날짜(검사를 지난 YYYY-MM-DD)를 월/일로.

    Args:
        day: 「2026-10-14」.
        pad: True면 0을 채운 「10/04」, 아니면 「10/4」.

    Returns:
        「10/14」.
    """
    return f"{day[5:7]}/{day[8:10]}" if pad else f"{int(day[5:7])}/{int(day[8:10])}"


def _pct(x: float | None) -> str:
    """비율을 부호 있는 퍼센트로, 없으면 「모름」.

    Args:
        x: 비율(0.01 = 1%).

    Returns:
        「-4.6%」 또는 「모름」.
    """
    return "모름" if x is None else f"{x:+.1%}"


def _pp(x: float | None) -> str:
    """비율 차이를 %p로, 없으면 「모름」.

    Args:
        x: 비율 차이.

    Returns:
        「-2.7%p」 또는 「모름」.
    """
    return "모름" if x is None else pp(x)


def _one_line(v: object) -> str:
    """가드레일 대응 글을 한 줄로(reconcile.response_text와 같은 규칙, 그쪽은 pandas 모듈이라 따로 둠).

    Args:
        v: 대응 칸의 값.

    Returns:
        공백 하나로 이은 글, 비면 「적어 둔 것 없음」.
    """
    return " ".join(str(v or "").split()) or "적어 둔 것 없음"


def _whole(nums: dict, key: str) -> float | None:
    """실전 시작부터의 값(nums.all, 없으면 구간 값).

    Args:
        nums: 줄의 nums.
        key: 숫자 칸 이름.

    Returns:
        값, 없으면 None.
    """
    return (nums["all"] if isinstance(nums.get("all"), dict) else nums).get(key)


def _head(name: str, rec: dict) -> str:
    """결론 줄 앞머리: 이름 · 대표 상태(경고면 원인 이름).

    Args:
        name: 전략 이름.
        rec: 마지막 줄.

    Returns:
        「trend_10_50 · 경고 · 실행 문제로 추정」.
    """
    return f"{name} · {rec['rep']}" + (f" · {rec['title']}" if base(rec["rep"]) == "경고" else "")


def _detail(name: str, ver: int, g: dict, recs: list[dict]) -> list[str]:
    """전략 하나의 화면: 결론 · 손익 두 줄, 실전 시작부터, 원인, 거래 빈도, 대응, 근거 기간, 그림 안내.

    Args:
        name: 전략 이름.
        ver: 가드레일 판 번호.
        g: 가드레일 내용(read_guardrail이 모양을 검사함).
        recs: 기록이 있는 read_days 결과.

    Returns:
        줄 목록(레포 글은 아직 show 전).
    """
    last = recs[-1]
    n, st, ln, resp = last["nums"], last["states"], last["lines"], last["response"]
    out = [f"{_head(name, last)} · {_md(last['day'], True)} 기준 · 실전 {last['live_day']}일째 · 거래 {last['trades']}건", last["head"]]
    whole = n.get("all")
    if isinstance(whole, dict) and whole.get("start") and whole["start"] != n.get("period_start"):  # 구간이 둘 이상
        out.append(f"실전 시작부터({_md(whole['start'])} ~ {_md(last['day'])}): 실제 {_pct(whole.get('actual'))} · "
                   f"같은 기간 BT {_pct(whole.get('bt'))} · 실제 − BT(비용 0) {_pp(whole.get('gap'))}")
    out += [f"- {LABELS[k]}: {st[k]} · {ln[k]}" for k in TRACKED if base(st[k]) != "해당 없음"]
    if n.get("n7") is not None:
        lo, hi = n.get("tf_low"), n.get("tf_high")
        out.append(f"거래 빈도: 최근 7일 {n['n7']}건 · 백테스트 기준 " + (f"{lo}~{hi}건(청산 시각)" if lo is not None and hi is not None else "없음"))
    out.append(f"가드레일 대응(내가 정해 둔 것): 실행 경고 → {_one_line(resp.get('execution_warning'))} · "
               f"경고선 → {_one_line(resp.get('warning_line'))}")
    start = next((p for r in reversed(current_period(recs)) if (p := r["nums"].get("period_start"))), None)
    nr, wl = g["normal_range"], g["warning_line"]
    out.append("근거: " + " · ".join([
        f"이번 구간 {start} ~ {last['day']} UTC(30일씩 끊음)" if start else "이번 구간 모름(숫자가 있는 기록 없음)",
        f"한 달 정상 범위 {nr['low']:+.1%} ~ {nr['high']:+.1%}(90% · 보수적 비용 기준)" if nr["drawn"]
        else f"정상 범위 그리지 않음({nr.get('reason') or '사유 없음'})",
        f"경고선 {wl['line']:+.1%}(민감도 {wl['sensitivity']})", f"가드레일 v{ver}"]))
    out.append(f"그림: ! blackclip chart {name}")
    return out


def _empty(root: Path, name: str, ver: int, g: dict) -> str:
    """기록이 없는 전략의 한 줄: live.json 유무로 「실전 중」과 「실전 시작 전」을 가른다.

    Args:
        root: .blackclip 경로.
        name: 전략 이름.
        ver: 가드레일 판 번호.
        g: 가드레일 내용.

    Returns:
        한 줄.
    """
    if exists_regular(root, f"strategies/{name}/live.json"):
        return (f"{name} · 실전 중 · 매일 대조 기록 아직 없음(첫 blackclip daily 뒤에 보임) · 가드레일 v{ver} · "
                f"실전 시작일 {g['live_start']}")
    return f"{name} · 실전 시작 전 · 가드레일 v{ver} · 점검 결과는 blackclip check {name}"


def _row(name: str, recs: list[dict], latest: str) -> str:
    """전체 화면의 전략 한 줄: 대표 상태 · 실전 시작부터의 실제 · 차이 · 거래, 다른 날이면 끝에 그 날짜.

    실제가 마지막 날에 없으면(받지 못한 날 · 대조 실패) 이번 구간에서 값이 있는 마지막 날 값에 「(M/D까지)」를 붙이고(`data.seen`), 차이는 모름으로 둔다.

    Args:
        name: 전략 이름.
        recs: 기록이 있는 read_days 결과.
        latest: 모든 전략 중 가장 늦은 마지막 날(ISO).

    Returns:
        한 줄.
    """
    from blackclip.screen.data import seen  # data.py가 이 모듈의 _whole · plain을 불러서 맨 위에서 부르면 순환(record._days와 같은 방식)

    last = recs[-1]
    value, upto = seen(recs, "actual", True)
    actual = "모름" if value is None else _pct(value) + ("" if upto is None else f"({_md(upto)}까지)")
    return (f"{_head(name, last)} · 실제 {actual} · 실제−BT(비용 0) {_pp(_whole(last['nums'], 'gap'))} · 거래 {last['trades']}건"
            + ("" if last["day"] == latest else f" · {_md(last['day'], True)} 기준"))


def _rank(recs: list[dict]) -> int:
    """전체 화면의 줄 순서: 급한 상태(ORDER 앞쪽)가 먼저, 기록이 없는 전략은 맨 뒤.

    Args:
        recs: read_days 결과.

    Returns:
        ORDER 안의 자리(대표 상태 이름이 ORDER에 없거나 기록이 없으면 맨 뒤).
    """
    b = base(recs[-1]["rep"]) if recs else ""
    return ORDER.index(b) if b in ORDER else len(ORDER)


def _off_line(root: Path, name: str, state: str, day: str) -> str:
    """끈 전략의 한 줄: 「이름 · 중지 · 날」 + 끈 뒤 회고(after.md)가 있으면 끝에 알림, 기록을 못 읽으면 대조 멈춤 안내.

    Args:
        root: .blackclip 경로.
        name: 전략 이름.
        state: stopped가 돌려준 상태(중지 · 은퇴 · 기록 손상).
        day: 그 상태가 시작된 날(손상이면 "").

    Returns:
        한 줄(레포 글 없음 · 이름은 전략 이름 모양 검사를 지남).
    """
    if state == BROKEN:
        return f"{name} · {BROKEN} · 전략 기록(record.md)을 읽지 못함 · 대조 멈춤"
    return f"{name} · {state} · {day}" + (" · 끈 뒤 회고 있음" if exists_regular(root, f"strategies/{name}/after.md") else "")


def status_lines(root: Path, name: str | None) -> tuple[int, list[str]]:
    """status 화면 줄들. 이름을 주면 그 전략의 자세한 화면, 안 주면 가드레일이 있는 전략마다 한 줄.

    전체 보기에서 가드레일 · days.jsonl을 읽지 못한 전략은 그 전략만 「확인 불가」 행으로 정상 행들 뒤에 두고 나머지는 평소대로 낸다
    (종료 코드 4). 끈 전략(중지 · 은퇴 · 기록 손상, record.md)은 이 모든 행 · 「실전 N개」 셈에서 빠지고 맨 끝에 한 줄씩 낸다
    (기록 손상이면 종료 코드 4, 중지 · 은퇴는 올리지 않음). 이름을 준 보기는 그대로 오류를 올리고, 끈 전략을 이름으로 주면 끈 전략 줄 하나만 낸다(기록 손상이면 종료 코드 4).

    Args:
        root: .blackclip 경로.
        name: 전략 이름 또는 None.

    Returns:
        (종료 코드 0 또는 4, 줄 목록). 모든 줄이 plain을 거침(scan.show + 그림 글자 평문).

    Raises:
        WorkspaceError: 이름 모양이 다르거나 없는 전략 · strategies/가 링크 · 이름을 준 보기의 파일 문제(링크 · FIFO · 상한 · 깨진 JSON ·
            틀린 모양 · 읽을 줄이 하나도 없는 days.jsonl).
        OSError: 이름을 준 보기에서 읽지 못함.
    """
    names = strategies(root)
    stop = stopped(root)  # 가드레일 판 파일이 없는 끈 전략도 들어 있음
    off: dict[str, tuple[str, str]] = {}
    if name is None:  # 끈 전략(중지 · 은퇴 · 기록 손상)은 정상 행 · 확인 불가 행 · 「실전 N개」에서 빠지고 맨 끝에 한 줄씩
        off = stop
        names = [n for n in names if n not in off]
    if name is not None:
        if not _NAME.fullmatch(name):
            raise WorkspaceError(f"전략 이름 모양이 다릅니다({show(name)})")
        if (got := stop.get(name)) is not None:  # 끈 전략은 전체 보기의 끈 전략 줄 하나만 · 숫자 화면 없음
            return (PARTIAL if got[0] == BROKEN else 0), [plain(_off_line(root, name, *got))]
        if name not in names:
            raise WorkspaceError(f"전략이 없습니다({show(name)}) · blackclip status로 목록")
        names = [name]
    elif not names and not off:
        return 0, [NO_GUARDRAIL]
    rows, bad = [], []
    for n in names:
        try:
            got, recs = read_guardrail(root, n), read_days(root, n)
        except (WorkspaceError, OSError) as exc:
            if name is not None:
                raise
            bad.append((n, _why(exc)))
            continue
        if got is None:  # 목록을 본 뒤 폴더가 사라진 경우
            raise WorkspaceError(f"전략이 없습니다({show(n)}) · blackclip status로 목록")
        rows.append((n, got[0], got[1], recs))
    if name is not None:
        _, ver, g, recs = rows[0]
        return 0, [plain(x) for x in (_detail(name, ver, g, recs) if recs else [_empty(root, name, ver, g)])]
    latest = max((r[-1]["day"] for _, _, _, r in rows if r), default=None)
    # 확인 불가 전략은 기록 유무를 알 수 없어 live.json이 있을 때만 센다
    live = (sum(1 for n, _, _, r in rows if r or exists_regular(root, f"strategies/{n}/live.json"))
            + sum(1 for n, _ in bad if exists_regular(root, f"strategies/{n}/live.json")))
    no_record = "읽을 수 있는 매일 대조 기록 없음" if bad else "매일 대조 기록 아직 없음"  # 못 읽은 전략이 있으면 「아직 없음」이라 단정하지 않음
    if not names:  # 전략이 모두 꺼짐: 「기록 아직 없음」이라 단정하지 않고 끈 전략 수만
        head = f"실전 0개 · 끈 전략 {len(off)}개 · 그림은 ! blackclip chart"
    else:
        head = (f"실전 {live}개 · {_md(latest, True)} 기준(매일 대조 기록) · 숫자는 실전 시작부터" if latest
                else f"실전 {live}개 · {no_record}") + " · 그림은 ! blackclip chart" + (f" · 끈 전략 {len(off)}개" if off else "")
    rows.sort(key=lambda x: (_rank(x[3]), x[0]))
    return (PARTIAL if bad or any(v[0] == BROKEN for v in off.values()) else 0), [
        plain(x) for x in [head, *(_row(n, r, latest) if r else _empty(root, n, v, g) for n, v, g, r in rows),
                           *(f"{n} · 확인 불가({why})" for n, why in bad), *(_off_line(root, n, *v) for n, v in sorted(off.items()))]]

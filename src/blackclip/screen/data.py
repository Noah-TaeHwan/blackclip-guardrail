"""화면이 갈릴 수 있는 규칙의 순수 함수: 터미널 chart와 웹 대시보드가 같은 숫자를 보이도록 한 곳에 둔다.

엔진이 days.jsonl에 계산해 둔 그날 값(nums.actual · bt · line · lo · hi 등)은 두 화면이 그대로 읽는다. 여기 있는 것은 그 값을 보고
화면이 정하는 규칙뿐이다: D 스파크라인 칸 · 눈금 · 「까지」 날, A 띠를 그리는지 · 놓친 주문 날 · 마지막 경고선 · 새 구간 날,
E 누적 평균 · 빠진 거래 · 기록에 없는 청산 · 30건 선, B 날마다의 두 기여 · 세로 범위 · 그릴 면적이 없는 경우 · 라벨 값, 칸마다 「마지막으로 아는 날」 규칙.
글자 · 색 · 폭은 여기에 없다(그리기는 chart.py가 한다).
파일을 읽지 않고 터미널 폭도 모른다. 레포에서 온 글은 가공하지 않고 돌려주니, 화면에 내기 전에 각자 이스케이프한다.
"""
from __future__ import annotations

from collections.abc import Iterable, Sequence
from datetime import UTC, date, datetime, timedelta

from blackclip.daily.days import current_period
from blackclip.screen.status import _whole, plain
from blackclip.states import base
from blackclip.workspace import WorkspaceError

SPARK_CELLS = 22
"""D 스파크라인의 최대 칸 수(날이 더 많으면 묶는다)."""

E_CAP = 5000
"""E가 한 화면에 올리는 거래 1건 수익의 최대 개수(실전 시작부터 이어 붙인 것 가운데 가장 최근)."""


def _utc(t: str) -> datetime:
    """ISO 시각을 UTC로(시간대가 없으면 UTC로 봄).

    Args:
        t: days.jsonl이 모양을 검사한 ISO 시각.

    Returns:
        UTC 시각.

    Raises:
        WorkspaceError: 모양 검사는 통과했지만 UTC로 바꿀 수 없는 시각(0001-01-01T00:00:00+01:00 같은 끝값).
    """
    try:
        dt = datetime.fromisoformat(t)
        return dt.astimezone(UTC) if dt.tzinfo else dt.replace(tzinfo=UTC)
    except (OverflowError, ValueError) as exc:
        raise WorkspaceError(f"days.jsonl의 놓친 주문 시각을 UTC로 바꾸지 못했습니다({plain(t[:40])})") from exc


def spark(recs: list[dict], last: date) -> list[dict]:
    """D 스파크라인 칸들: 첫 기록 날부터 last까지 날마다 한 칸(22일을 넘으면 22칸으로 묶어 칸마다 묶음의 마지막 날).

    값 = 실전 시작부터의 실제 손익. 기록이 없거나 값이 없는 날은 value · level이 None이다. 눈금은 이 줄에 그려진 값의 최소 · 최대로
    0~5를 매기고(모두 같으면 2) 줄마다 다르다. warn은 값이 그려진 칸 가운데 그날 대표 상태가 경고인 칸만 참이다.

    Args:
        recs: 이 전략의 기록 전체(날짜순, 비어 있지 않음).
        last: 그릴 마지막 날. 전체 전략 가운데 가장 늦은 날을 넘겨, 일찍 끝난 전략도 같은 끝까지 빈 칸으로 잇는다.

    Returns:
        칸마다 {"day": "YYYY-MM-DD", "value": float | None, "warn": bool, "level": int | None}.
    """
    first = date.fromisoformat(recs[0]["day"])
    n = (last - first).days + 1
    m = min(n, SPARK_CELLS)
    by_day = {r["day"]: r for r in recs}
    days = [first + timedelta(days=(c + 1) * n // m - 1) for c in range(m)]
    rows = [by_day.get(d.isoformat()) for d in days]
    vals = [None if r is None else _whole(r["nums"], "actual") for r in rows]
    known = [v for v in vals if v is not None]
    lo, hi = (min(known), max(known)) if known else (0.0, 0.0)
    return [{"day": d.isoformat(), "value": v, "warn": r is not None and v is not None and base(r["rep"]) == "경고",
             "level": None if v is None else round((v - lo) / (hi - lo) * 5) if hi > lo else 2}
            for d, r, v in zip(days, rows, vals)]


def last_day(days: Iterable[list[dict]]) -> str | None:
    """D 스파크라인의 공통 끝 날(`spark`의 last): 기록이 있는 전략 가운데 마지막 기록 날이 가장 늦은 날.

    터미널 `chart_d`와 웹 홈이 같은 규칙을 쓰게 여기 둔다. 넘기는 기록 집합도 같아야 한다: 끈 전략을 뺀 전략 가운데 days.jsonl을 읽은 것
    (가드레일을 읽었는지와 상관없음).

    Args:
        days: 전략마다 기록 전체(날짜순). 빈 목록은 건너뛴다.

    Returns:
        "YYYY-MM-DD" 또는 None(기록이 있는 전략이 없음).
    """
    return max((r[-1]["day"] for r in days if r), default=None)


def d_values(recs: list[dict]) -> dict:
    """D 한 줄의 숫자: 손익 · 실제-BT 차이 · 마지막으로 손익을 안 날.

    손익은 실제 손익을 아는 가장 최근 날의 값이다. 마지막 날이 그 날이면 그 날의 차이를 주고, 아니면 차이 대신 그 날(until)을 준다.

    Args:
        recs: 이 전략의 기록 전체(날짜순, 비어 있지 않음).

    Returns:
        {"pnl": 실전 시작부터 실제 손익 | None(한 번도 모름), "gap": 마지막 날의 실제-BT 차이 | None(마지막 날 손익을 모르거나 차이를 모름),
        "until": 손익을 마지막으로 안 날 "YYYY-MM-DD" | None(마지막 날 손익을 알거나 한 번도 모름)}.
    """
    seen = next((r for r in reversed(recs) if _whole(r["nums"], "actual") is not None), None)
    end = recs[-1]
    return {"pnl": None if seen is None else _whole(seen["nums"], "actual"),
            "gap": _whole(end["nums"], "gap") if seen is end else None,
            "until": seen["day"] if seen is not None and seen is not end else None}


def period_cuts(items: Sequence[dict | None]) -> list[int]:
    """새 구간이 처음 보이는 항목의 번호: period_start가 앞 구간과 달라진 첫 항목(첫 구간 시작은 아님).

    기록이 없는 항목(None)과 대조 실패한 항목(period_start 없음)은 앞 구간을 이어받아 경계로 세지 않는다.
    기록(날 단위)과 그림 칸(칸 단위)이 같은 규칙을 쓰도록 한 곳에 둔다.

    Args:
        items: 기록 또는 None의 날짜순 목록(기록 전체, 또는 그림 칸마다의 그날 기록).

    Returns:
        경계 항목의 번호(0부터), 오름차순.
    """
    cuts, cur = [], None
    for i, r in enumerate(items):
        if r is not None and (start := r["nums"].get("period_start")):
            if cur is not None and start != cur:
                cuts.append(i)
            cur = start
    return cuts


def a_marks(recs: list[dict], g: dict) -> dict:
    """A 범위 차트의 표시 규칙: 띠를 그리는지 · 놓친 주문 날 · 마지막 경고선 · 새 구간이 시작된 날.

    띠는 가드레일의 drawn이 아니라 기록에 그날 lo · hi가 둘 다 있는 날이 하나라도 있는지로 정한다(터미널과 같은 규칙).

    Args:
        recs: 이 전략의 기록 전체(날짜순, 비어 있지 않음).
        g: 가드레일 내용(read_guardrail이 모양을 검사함, normal_range만 봄).

    Returns:
        {"band": 띠를 그리는지, "reason": 띠가 없을 때 가드레일이 적은 사유(레포 글, 이스케이프 전) | None,
        "missed": 놓친 주문이 있는 UTC 날 집합, "line": None이 아닌 마지막 경고선 | None,
        "starts": 앞 구간과 period_start가 달라진 첫 날들(period_cuts 규칙: 대조 실패 날은 앞 구간을 이어받아 건너뜀, 첫 구간 시작은 아님)}.

    Raises:
        WorkspaceError: 모양 검사는 통과했지만 UTC로 바꿀 수 없는 놓친 주문 시각.
    """
    nums = [r["nums"] for r in recs]
    band = any(n.get("lo") is not None and n.get("hi") is not None for n in nums)
    reason = g["normal_range"].get("reason")
    lines = [v for n in nums if (v := n.get("line")) is not None]
    return {"band": band, "reason": str(reason) if reason and not band else None,
            "missed": {_utc(m["t"]).date().isoformat() for n in nums for m in n.get("missed") or []},
            "line": lines[-1] if lines else None, "starts": [recs[i]["day"] for i in period_cuts(recs)]}


def trade_sample(recs: list[dict], cap: int) -> dict:
    """E의 표본: 30일 구간마다 trade_rets가 None이 아닌 마지막 줄의 거래 1건 수익을 시작 순서로 이어 붙인다.

    Args:
        recs: 이 전략의 기록 전체(날짜순). period_start가 없는 대조 실패 줄은 어느 구간에도 안 든다.
        cap: 이어 붙인 것 가운데 가장 최근 몇 개만 남길지.

    Returns:
        {"rets": 남긴 수익 목록, "kept": 이어 붙인 수(cap으로 자르기 전), "exits": 마지막 구간 마지막 줄의 trades(실전 시작부터 청산 수),
        "n": 구간들의 trade_n 합(표본 전체 수), "skip": trade_skip 합, "capped": trade_n이 기록된 수보다 많은 구간의 시작 날짜}.
        옛 줄에 trade_n이 없으면 기록된 수로 본다.
    """
    last: dict[str, dict] = {}
    seen: dict[str, dict] = {}  # 구간마다 trade_rets가 None이 아닌 마지막 줄(체결을 못 읽은 날 줄이 표본을 지우지 않게)
    for r in recs:
        if start := r["nums"].get("period_start"):
            last[start] = r
            if r["nums"].get("trade_rets") is not None:
                seen[start] = r
    rets: list[float] = []
    n = skip = 0
    capped: list[str] = []
    for start in sorted(last):
        nums = seen.get(start, last[start])["nums"]
        got = nums.get("trade_rets") or []
        count = len(got) if nums.get("trade_n") is None else nums["trade_n"]
        rets += got
        n += count
        skip += nums.get("trade_skip") or 0
        if count > len(got):
            capped.append(start)
    return {"rets": rets[-cap:], "kept": len(rets), "exits": last[max(last)]["trades"] if last else 0, "n": n, "skip": skip,
            "capped": capped}


def e_series(recs: list[dict], g: dict, cap: int = E_CAP) -> dict:
    """E 거래 단위 차트의 숫자: 표본(trade_sample) + 누적 평균 · 백테스트 기준 · 빠진 거래 · 기록에 없는 청산 · 30건 선.

    Args:
        recs: 이 전략의 기록 전체(날짜순, 비어 있지 않음).
        g: 가드레일 내용(read_guardrail이 모양을 검사함, basis · judgment_hold만 봄).
        cap: 한 화면에 올리는 거래 수의 상한(화면이 정함, 기본 E_CAP).

    Returns:
        trade_sample 결과(rets · kept · exits · n · skip · capped)에 더해
        {"run": i번째 거래까지의 실제 평균(rets와 같은 길이), "p10" · "p90": 백테스트 거래 가운데 80% 띠의 하단 · 상단 | None,
        "mean": 백테스트 거래 1건 평균 | None(실제 평균은 run[-1]), "minimum": 판단에 필요한 거래 수,
        "lost": cap이나 기록 상한으로 빠진 거래 수(0 이상), "unseen": 매일 대조 기록에 없는 청산 수(0 이상),
        "line30": minimum건 선을 그릴 수 있는지(빠진 거래 · 기록에 없는 청산이 없고 거래가 minimum건 이상일 때만),
        "edge0": 엣지가 없어도 범위 안일 확률 | None, "n7": 최근 7일 거래 수 | None, "tf_low" · "tf_high": 백테스트 기준 7일 거래 수 | None}.
    """
    smp = trade_sample(recs, cap)
    rets, n = smp["rets"], smp["n"]
    basis, minimum, nums = g.get("basis") or {}, g["judgment_hold"]["min_trades"], recs[-1]["nums"]
    lost = max(n - len(rets), 0)
    unseen = max(smp["exits"] - n - smp["skip"], 0)  # 구간 마지막 날 daily가 안 돌았거나 체결을 못 읽어 표본에 못 든 청산(저장된 수끼리 뺄셈)
    line30 = bool(rets) and lost == 0 and unseen == 0 and len(rets) >= minimum  # 빠진 거래 · 기록에 없는 청산이 있으면 표본의 30번째 점이 실제 30번째 거래가 아님
    return {**smp, "run": [sum(rets[:i + 1]) / (i + 1) for i in range(len(rets))],  # i번째까지 평균
            "p10": basis.get("trade_p10"), "p90": basis.get("trade_p90"), "mean": basis.get("trade_mean"), "minimum": minimum,
            "lost": lost, "unseen": unseen, "line30": line30, **{k: nums.get(k) for k in ("edge0", "n7", "tf_low", "tf_high")}}


def _split(n: dict) -> tuple[float | None, float | None]:
    """하루의 두 기여: 조금씩 = 수수료 + 체결가 차이 + 펀딩비, 한 번에 = 나머지.

    수수료와 체결가 차이는 둘 다 알아야 조금씩을 말한다(하나라도 모르면 None — 아는 쪽만 합쳐 「잰 것」이라 하지 않음).
    펀딩비는 선택이라 None이면 0.

    Args:
        n: 줄의 nums.

    Returns:
        (slow, fast). 모르면 None.
    """
    fee, slip = n.get("fee"), n.get("slip")
    slow = None if fee is None or slip is None else fee + slip + (n.get("funding") or 0.0)
    return slow, n.get("rest")


def seen(recs: list[dict], key: str, whole: bool) -> tuple[float | None, str | None]:
    """칸 하나의 「마지막으로 아는 날」 규칙: 이번 구간에서 그 칸에 값이 있는 가장 최근 날의 값과, 마지막 기록이 아닐 때 그 날.

    터미널 `status` 전체 보기의 「실제 -4.6%(10/11까지)」와 웹 회고 칸이 같은 규칙을 쓰게 여기 둔다. 마지막 날에 값이 없을 때(받지 못한 날 · 대조 실패)
    마지막 날 값을 모른다고 하지 않고 옛 값을 보이되 그 날을 붙인다. 이번 구간(`current_period`) 밖의 앞 구간 값은 보지 않는다.

    Args:
        recs: 이 전략의 기록 전체(날짜순, 비어 있지 않음).
        key: nums 칸 이름(예: "actual" · "gap" · "slip").
        whole: True면 실전 시작부터 값(nums.all의 칸, `_whole` 규칙), False면 이번 구간 값(nums의 칸).

    Returns:
        (값, 날). 마지막 기록에 값이 있으면 (값, None), 이전 날 값이면 (값, "YYYY-MM-DD"), 이번 구간에 값이 하나도 없으면 (None, None).
    """
    def pick(r: dict) -> float | None:
        """칸 값: whole이면 실전 시작부터 값(`_whole`), 아니면 이번 구간 값. 없으면 None."""
        return _whole(r["nums"], key) if whole else r["nums"].get(key)

    hit = next((r for r in reversed(current_period(recs)) if pick(r) is not None), None)
    return (None, None) if hit is None else (pick(hit), None if hit is recs[-1] else hit["day"])


def b_series(recs: list[dict]) -> dict:
    """B 차이 차트의 값: 날마다의 두 기여 · 세로 범위 · 그릴 면적이 없는 경우 · 라벨 값. 터미널 `chart_b`와 웹 B 그림이 같이 읽는다.

    조금씩 = 수수료 + 체결가 차이(+ 펀딩비), 한 번에 = 나머지. 세로 범위는 gap까지 넣어 아래(low)를 잡고 위(high)는 날마다 양수 기여의 합만 본다.
    면적을 그릴 수 있는지 여부와 라벨 숫자는 값만 돌려주고 글은 만들지 않는다(터미널 글은 chart.py, 웹 글은 web.py).

    Args:
        recs: 이 전략의 기록 전체(날짜순, 비어 있지 않음).

    Returns:
        {"days": 날마다 {"day": ISO, "slow": 조금씩 | None(수수료나 체결가 차이를 모름), "fast": 나머지 | None, "gap": 합계 | None,
        "start": 앞 구간과 period_start가 달라진 첫 날인지(period_cuts 규칙)},
        "low": 세로 범위 아래(0 이하) | None(empty.kind가 none), "high": 위(0 이상) | None(empty.kind가 none),
        "empty": 그릴 면적이 없으면 {"kind": "none"(차이를 낸 날 없음) | "zero"(마지막 날 두 기여를 알고 모두 0) |
        "zero_upto"(마지막 날은 일부를 모르고 두 기여를 모두 확인한 마지막 날 seen_day까지 0) | "unknown"(두 기여를 모두 확인한 날 없음),
        "seen_day": zero_upto일 때 ISO, 나머지 None}, 아니면 None,
        "labels": {"slow" · "fast": 마지막 기록의 조금씩 · 나머지(None = 모름), "gap": 합계(마지막 날 gap을 모르면 gap을 아는 가장 최근 날의 값 | None),
        "upto": 그 값이 마지막 날이 아닐 때 그 날 ISO(마지막 날 값이면 None), "funding": 마지막 기록에 펀딩비가 있는지,
        "missed_n": 마지막 기록의 놓친 주문 건수(missed_n, 없으면 목록 길이), "first_missed": 건수가 있고 목록이 있을 때 시각이 가장 이른 것
        {"t", "symbol", "side", ...}(레포 글 그대로 — 화면에서 이스케이프) | None(없을 때, 그리고 라벨을 쓰지 않는 empty.kind none · zero일 때)}}.

    Raises:
        WorkspaceError: 라벨을 쓰는 경로(empty가 None · zero_upto · unknown)에서 마지막 기록의 놓친 주문 시각을 UTC로 바꿀 수 없음.
            none · zero는 라벨을 쓰지 않아 시각을 바꾸지 않으므로 오류가 없다.
    """
    cuts = set(period_cuts(recs))
    split = [_split(r["nums"]) for r in recs]
    days = [{"day": r["day"], "slow": s, "fast": f, "gap": r["nums"].get("gap"), "start": i in cuts}
            for i, (r, (s, f)) in enumerate(zip(recs, split))]
    n = recs[-1]["nums"]
    missed = n.get("missed") or []
    count = n.get("missed_n") if n.get("missed_n") is not None else len(missed)
    gap, upto = n.get("gap"), None
    if gap is None and (hit := next((r for r in reversed(recs) if r["nums"].get("gap") is not None), None)):
        gap, upto = hit["nums"]["gap"], hit["day"]  # 마지막 날을 모르면 마지막으로 확인된 날의 합계
    labels = {"slow": split[-1][0], "fast": split[-1][1], "gap": gap, "upto": upto, "funding": n.get("funding") is not None,
              "missed_n": count, "first_missed": None}
    if all(s is None and f is None for s, f in split):
        return {"days": days, "low": None, "high": None, "empty": {"kind": "none", "seen_day": None}, "labels": labels}
    gaps = [v for r in recs if (v := r["nums"].get("gap")) is not None]
    low = min([0.0, *gaps, *(sum(min(v, 0.0) for v in d if v is not None) for d in split)])
    high = max([0.0, *(sum(max(v, 0.0) for v in d if v is not None) for d in split)])
    empty = None
    if high == low:  # 0은 두 기여를 모두 아는 날만 말한다
        hit = next((r for r, d in zip(reversed(recs), reversed(split)) if None not in d), None)
        if None not in split[-1]:
            empty = {"kind": "zero", "seen_day": None}
        else:
            empty = {"kind": "unknown", "seen_day": None} if hit is None else {"kind": "zero_upto", "seen_day": hit["day"]}
    if empty is None or empty["kind"] != "zero":  # 라벨을 쓰는 경로에서만 시각을 바꾼다(none은 위에서 돌려보냄, zero는 한 줄뿐)
        labels["first_missed"] = min(missed, key=lambda x: _utc(x["t"])) if count and missed else None
    return {"days": days, "low": low, "high": high, "empty": empty, "labels": labels}

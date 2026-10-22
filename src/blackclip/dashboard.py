"""blackclip dashboard: 이 컴퓨터에서만 여는 화면 서버.

127.0.0.1에만 열고, 요청마다 Host · Origin · 접속 토큰(`?t=`, 실행마다 새로)을 라우트보다 먼저 확인한다. 기록은 요청마다
`workspace`의 폴더 FD 함수로 다시 읽는다(캐시 없음). 쓰는 곳은 연결 화면 POST(`/connect/key` · `telegram` · `choose`) 셋뿐이고,
폼 CSRF · 서버 Lock · 연결 상태 잠금 안에서만 쓴다. 그 밖 화면은 읽기만 한다.
"""
from __future__ import annotations

import csv
import io
import json
import re
import secrets
import threading
import webbrowser
from collections.abc import Callable
from dataclasses import dataclass
from datetime import date, datetime, timezone
from http.server import ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

from blackclip.binance.client import BinanceError
from blackclip.binance.permissions import Permission
from blackclip.connect import HEALTHCHECK_SHAPE_TEXT, KINDS, MAX_BODY, Connect, SecureHandler, Saved, form_value
from blackclip.daily.days import read_days
from blackclip.hook import _NAME
from blackclip.keystore import Credentials, KeystoreBusy, KeystoreError, account_name
from blackclip.notify import RETRO_HEAD, TELEGRAM_ACCOUNT, change_notify, first_retro, has_retro
from blackclip.record import (
    BROKEN,
    RecordError,
    _is_folder,
    after_event,
    after_values,
    read_record,
    stopped,
)
from blackclip.screen import web
from blackclip.screen.data import (
    E_CAP,
    _utc,
    a_marks,
    b_series,
    d_values,
    e_series,
    last_day,
    seen,
    spark,
)
from blackclip.screen.status import _rank, _whole, strategies
from blackclip.states import TITLES, base
from blackclip.workspace import (
    LockBusy,
    WorkspaceError,
    account_key,
    apply_connections,
    exists_regular,
    ignore_intact,
    load_connections,
    lock,
    read_bytes,
    read_guardrail,
    section,
    strategy_names,
    versions,
)

FORBIDDEN_TEXT = "터미널에 나온 주소로 다시 열어 주세요."
"""403(Host · Origin · 접속 토큰 검사 실패) 본문 한 줄."""

NOT_FOUND_TEXT = "없는 주소예요."
"""404 본문 한 줄."""

METHOD_TEXT = "이 주소는 이 요청 방법을 받지 않아요."
"""405(셋 밖 POST와 PUT · DELETE · PATCH · OPTIONS) 본문 한 줄. 연결 주소는 POST를 받으니 화면이 읽기만 한다고 말하지 않는다."""

FAILED_TEXT = "화면을 그리지 못했어요. 터미널에서 blackclip status로 확인해 주세요."
"""500(렌더 중 읽기 오류가 아닌 예외) 본문 한 줄."""

FIELD_MEANINGS: dict[str, str] = {
    "strategy": "전략 이름이에요. 전략 목록을 읽지 못하면 비어 있어요.",
    "state": "끈 전략이면 끈 상태(중지, 은퇴, 기록 손상), 아니면 마지막 기록의 판정이에요. 읽지 못하면 확인 불가, 기록이 없으면 비어 있어요.",
    "reason": "판정의 이유 한 줄이에요. 읽지 못했을 때는 읽지 못한 까닭이에요.",
    "day": "마지막 기록 날(YYYY-MM-DD)이에요. live_day와 trades는 이 날 기준이에요.",
    "until": "actual, bt, bt0, gap이 가리키는 기록 날(YYYY-MM-DD)이에요. 마지막 기록 날에 실제를 알면 day와 같고, 모르면 실제를 안 마지막 날이에요. 숫자를 낼 기록이 없으면 비어 있어요.",
    "live_day": "마지막 기록 날(day)이 실전 며칠째인지예요.",
    "trades": "마지막 기록 날(day)까지 실전 시작부터의 거래 수예요.",
    "actual": "until 날 기준 실전 시작부터 실제 손익 비율이에요(0.01은 1%).",
    "bt": "until 날 기준 실전 시작부터 같은 기간 백테스트 비율이에요. 보수적 비용 기준이에요.",
    "bt0": "until 날 기준 실전 시작부터 같은 기간 백테스트 비율이에요. 비용을 0으로 둔 값이에요.",
    "gap": "until 날 기준 실제 − 비용 0 같은 기간 백테스트의 차이 비율이에요(actual − bt0). 엔진이 저장한 값을 그대로 옮겨요.",
    "guardrail_version": "읽은 가드레일의 판 번호예요.",
    "set_on": "가드레일을 정한 날이에요.",
    "live_start": "가드레일에 적은 실전 시작 날이에요.",
    "cost_one_way_bp": "가드레일을 만들 때 쓴 보수적 비용이에요(편도, bp).",
    "normal_low": "한 달 정상 범위의 아래쪽 끝 비율이에요. 범위를 그리지 않았으면 비어 있어요.",
    "normal_high": "한 달 정상 범위의 위쪽 끝 비율이에요. 범위를 그리지 않았으면 비어 있어요.",
    "warning_line": "경고선 비율이에요.",
    "c3_sharpe_cost0": "시도 수 보정에 쓴 비용 0 백테스트의 샤프예요.",
    "c3_adjusted_low": "비용 0 샤프를 시도 수로 깎은 값이에요. 시도 수를 몰라 깎을 수 없으면 비어 있어요.",
    "c3_trials": "직접 밝힌 시도 수예요.",
    "c3_unverified": "참이면 시도 수 보정은 독립 가정의 근사라 검증하지 않은 값이에요.",
}
"""내보내기 칸 이름 → 한 줄 뜻(JSON `fields`). 순서가 곧 칸 순서다. 문구는 해요체 또는 명사구, 가운뎃점 없음."""

EXPORT_FIELDS: tuple[str, ...] = tuple(FIELD_MEANINGS)
"""내보내기 칸 순서(CSV 머리 줄 · JSON 줄의 키 순서)."""

FORMULA_LEAD = ("=", "+", "-", "@", "\t", "\r", "\n", "＝", "＋", "－", "＠")
"""스프레드시트가 수식으로 읽을 수 있는 글 시작 글자(OWASP CSV Injection: = + - @ 탭 CR LF와 전각 ＝ ＋ － ＠)."""

EXPORT_TYPES = {"/export.csv": ("csv", "text/csv"), "/export.json": ("json", "application/json")}
"""내보내기 주소 → (확장자, 내용 종류)."""

_ISO_DAY = re.compile(r"\d{4}-\d{2}-\d{2}", re.ASCII)
"""파일 이름 · day에 쓸 수 있는 날 꼴."""


def _row(name: str, live: bool, word: str | None, reason: str | None, day: str | None, guardrail: tuple[int, dict] | None = None) -> dict:
    """`web.home`의 실전 줄 하나. 숫자 칸 · 흐름은 비어 있고(화면에는 「모름」), 기록이 있는 줄은 `_cells`가 채운다.

    Args:
        name: 전략 이름.
        live: 실전 중이면 True.
        word: 칩 낱말(괄호 사유를 뗀 것) 또는 None(기록 없음).
        reason: 칩 밖 이유 한 줄 또는 None.
        day: 마지막 기록 날 또는 None.
        guardrail: 이 줄을 만들며 읽은 가드레일 (판 번호, 내용) 또는 None(못 읽음). 내보내기가 다시 읽지 않고 쓴다.

    Returns:
        `web.home` rows 항목(키 열세 개)에 내보내기가 쓰는 `bt0` · `live_day` · `guardrail`(홈은 읽지 않음)을 더한 열여섯 개.
    """
    return {"name": name, "live": live, "word": word, "reason": reason, "actual": None, "bt": None, "bt0": None, "gap": None,
            "trades": None, "live_day": None, "day": day, "until": None, "spark": [], "spark_start": None, "warn_days": 0,
            "guardrail": guardrail}


def _reason(rec: dict) -> str | None:
    """칩 밖 이유 한 줄: 대표 상태의 괄호 사유, 없으면 대표 칸의 웹 원인 이름, 정상이면 없음.

    대표 칸은 `rec["title"]`을 `states.TITLES`에서 거꾸로 찾아 정한다. TITLES에 없는 글(손으로 고친 기록)이면 이유를 내지 않는다.

    Args:
        rec: 마지막 기록 줄.

    Returns:
        이유 글(레포 글이 들 수 있어 `web.text`를 거쳐 내야 함) 또는 None.
    """
    if base(rec["rep"]) == "정상":
        return None
    why = rec["rep"].partition("(")[2].removesuffix(")")
    col = next((k for k, v in TITLES.items() if v == rec["title"]), None)
    return why or web.CAUSE.get(col)


def _cells(row: dict, recs: list[dict], last: date) -> dict:
    """기록이 있는 줄의 숫자 칸 · 흐름을 채운다: 터미널 D와 같은 `data.d_values` · `data.spark`.

    실제 · 차이 · 「까지」 날은 `d_values`, 흐름은 `spark(recs, last)`(`last` = 실전 전략 전체의 가장 늦은 날, `chart_d`와 같음).
    같은 기간 백테스트(보수적 비용)는 `status._whole(nums, "bt")`이고, 차이는 엔진이 저장한 `actual − bt0`(비용 0, `_whole(nums, "gap")`)을
    그대로 옮긴다(비용 0 백테스트 `bt0`도 같은 기록에서 읽어 내보내기가 쓴다). 실제를 마지막 날 몰라 `(날까지)`를 붙이는 줄은 둘 다 실제를 안
    그 날의 기록에서 읽는다(다른 기간의 숫자를 나란히 두지 않음). 터미널 D는 그 줄의 차이 칸이 「M/D까지」 자리라 비지만 웹은
    칸이 따로라 값을 보인다.
    흐름 글(`spark_svg`)에는 첫 기록 날(`spark_start`)과 경고인 기록 수(`warn_days`)를 따로 넘긴다: 22일을 넘으면 칸이 날을 묶어
    칸 날 · 칸 경고 수로는 문장이 틀리기 때문이다. `last`는 모든 기록 날 이상이라 기록 전체가 그린 범위 안이다.

    Args:
        row: `_row`가 만든 줄.
        recs: 이 전략의 기록 전체(날짜순, 비어 있지 않음).
        last: 모든 실전 전략 가운데 가장 늦은 기록 날.

    Returns:
        숫자 칸 · 이유 · 흐름을 채운 줄(`row`).
    """
    v = d_values(recs)
    seen = next((r for r in recs if r["day"] == v["until"]), recs[-1])
    return row | {"reason": _reason(recs[-1]), "actual": v["pnl"], "bt": _whole(seen["nums"], "bt"), "bt0": _whole(seen["nums"], "bt0"),
                  "gap": v["gap"] if v["until"] is None else _whole(seen["nums"], "gap"), "trades": recs[-1]["trades"],
                  "live_day": recs[-1]["live_day"], "until": v["until"], "spark": spark(recs, last), "spark_start": recs[0]["day"],
                  "warn_days": sum(1 for r in recs if base(r["rep"]) == "경고")}


def home_rows(root: Path) -> tuple[list[dict], list[dict], str | None]:
    """홈 화면 줄들을 `status_lines` 전체 보기와 같은 기준으로 고른다.

    가드레일 판이 있는 전략(`status.strategies`) 가운데 끈 전략(`record.stopped`)을 뺀 것이 실전 줄이고, `status._rank` · 이름순이다.
    기록이 있는 줄은 `_cells`가 숫자 칸 · 이유 · 흐름을 채운다(기록 없는 줄은 칩 없이 안내 글만이라 비어 있음).
    가드레일 · days.jsonl을 읽지 못한 전략은 그 전략만 「확인 불가」 줄로 정상 줄들 뒤에 이름순으로 둔다(응답은 200).
    흐름의 공통 끝 날(`data.last_day`)은 days.jsonl을 읽은 실전 전략 모두에서 구한다(가드레일을 못 읽은 전략 포함, 터미널 D와 같은 집합).
    읽지 못한 줄의 모양: `word="확인 불가"`, `reason=web.reason(exc, "가드레일" 또는 "매일 대조 기록")`(경로 없는 한 줄),
    `live`는 `live.json`이 있을 때만 True(기록 유무를 몰라서, status 셈과 같음), 숫자 칸 · `day` · `until`은 None, `spark`는 `[]`.

    Args:
        root: .blackclip 경로.

    Returns:
        (실전 줄, 끈 전략 줄, 가장 늦은 기준 날 또는 None). 줄 키는 `web.home` 문서와 같다.

    Raises:
        WorkspaceError: strategies/가 링크이거나 폴더가 아님(부르는 쪽이 화면 한 장의 「확인 불가」로 바꿈).
    """
    off = stopped(root)
    good, bad, read = [], [], []
    for n in strategies(root):
        if n in off:
            continue
        recs, got, why = None, None, None
        try:  # 기록은 가드레일과 따로 읽는다: 가드레일을 못 읽은 전략의 기록도 공통 끝 날에 들어간다(터미널 chart 전체 보기와 같은 집합)
            read.append(recs := read_days(root, n))
        except (WorkspaceError, OSError) as exc:
            why = web.reason(exc, "매일 대조 기록")
        try:
            if (got := read_guardrail(root, n)) is None:  # 목록을 본 뒤 폴더가 사라짐
                continue
        except (WorkspaceError, OSError) as exc:  # 둘 다 못 읽으면 가드레일 사유를 보인다
            why = web.reason(exc, "가드레일")
        if why is not None:  # 가드레일은 읽혔는데 days.jsonl만 못 읽었으면 got은 남아 있다
            bad.append(_row(n, exists_regular(root, f"strategies/{n}/live.json"), "확인 불가", why, None, got))
            continue
        good.append((n, recs, got))
    good.sort(key=lambda x: (_rank(x[1]), x[0]))
    latest = last_day(read)  # 터미널 chart_d의 last와 같은 규칙 · 같은 기록 집합
    rows = [_cells(_row(n, True, base(r[-1]["rep"]), None, r[-1]["day"], got), r, date.fromisoformat(latest)) if r
            else _row(n, exists_regular(root, f"strategies/{n}/live.json"), None, None, None, got) for n, r, got in good] + bad
    out = [{"name": n, "state": st, "since": None if st == BROKEN else day,
            "after": exists_regular(root, f"strategies/{n}/after.md"), "guard": bool(versions(root, n))}
           for n, (st, day) in sorted(off.items())]
    return rows, out, latest


def guard_view(root: Path, name: str) -> dict | None:
    """가드레일 화면 하나(`/s/<이름>`)에 필요한 값을 읽어 모은다. 못 읽은 것은 예외 대신 `why` 한 줄로 담는다(응답은 200).

    이름이 전략 이름 모양(`hook._NAME`)이 아니거나, 가드레일 판이 있는 실전 전략도 끈 전략(`record.stopped`)도 아니면 None(404). 끈 전략은
    기록 손상까지 포함하고, 끈 전략이면 가드레일 판이 없어도 화면이 있다(상태 줄 + 사유). 요청마다 다시 읽는다.

    Args:
        root: .blackclip 경로.
        name: 주소에서 나온 이름(퍼센트 인코딩을 풀지 않은 그대로).

    Returns:
        `web.guard`가 받는 view(키: name · off · after · version · g · recs · why · word · reason · marks · series · cap · retro(회고 화면이 있는지, 내비 칸)) 또는 None.
    """
    if not _NAME.fullmatch(name):
        return None
    try:
        off = stopped(root).get(name)
        if off is None and name not in strategies(root):
            return None
    except (WorkspaceError, OSError):  # strategies/를 못 읽으면 어떤 이름도 있다고 말할 수 없음
        return None
    view = {"name": name, "off": off, "after": exists_regular(root, f"strategies/{name}/after.md"), "version": None, "g": None, "recs": [],
            "why": None, "word": None, "reason": None, "marks": None, "series": None, "cap": E_CAP, "retro": retro_ok(root, name)}
    try:
        got = read_guardrail(root, name)
    except (WorkspaceError, OSError) as exc:
        return view | {"why": web.reason(exc, "가드레일")}
    if got is None:
        return None if off is None else view | {"why": web.reason(FileNotFoundError(), "가드레일")}
    view["version"], view["g"] = got
    try:
        recs = read_days(root, name)
        if recs:
            view |= {"recs": recs, "marks": a_marks(recs, got[1]), "series": e_series(recs, got[1], E_CAP), "word": base(recs[-1]["rep"]),
                     "reason": _reason(recs[-1])}
    except (WorkspaceError, OSError) as exc:  # 놓친 주문 시각을 날로 못 바꾸는 경우도 이 길
        view |= {"recs": [], "why": web.reason(exc, "매일 대조 기록")}
    return view


def retro_ok(root: Path, name: str) -> bool:
    """회고 화면(`/s/<이름>/retro`)이 있는 전략인지: 전략 이름 모양이고 링크 아닌 폴더이며, 가드레일 판 · record.md · 첫 회고(`notify.has_retro`) 가운데 하나라도 있거나 끈 전략이다.

    끈 전략(기록 손상 포함)은 record.md가 읽히지 않아도 화면이 있다(홈 끈 표의 이름 링크가 죽지 않게). 요청마다 다시 본다.

    Args:
        root: .blackclip 경로.
        name: 주소에서 나온 이름(퍼센트 인코딩을 풀지 않은 그대로).

    Returns:
        있으면 True. strategies/를 읽지 못해도 False(링크를 따르지 않음).
    """
    if not _NAME.fullmatch(name):
        return False
    try:
        folder = root / "strategies" / name
        return (name in strategy_names(root) and _is_folder(folder)
                and bool(versions(root, name) or exists_regular(root, f"strategies/{name}/record.md") or has_retro(folder) or name in stopped(root)))
    except (WorkspaceError, OSError):
        return False


def _after_view(root: Path, name: str) -> dict:
    """끈 뒤 칸의 값: 기준 사건은 사건 목록만으로 정한다(`record.after_event`). 읽지 못하면 그 사유를 값에 담는다(화면은 200).

    Args:
        root: .blackclip 경로.
        name: 끈 전략 이름(`stopped`에 있는 이름).

    Returns:
        `{"broken": True}`(record.md를 읽지 못함) 또는 {"event": 기준 사건, "retire": 덧붙일 은퇴 사건 | None, "values": `after_values` 결과 | None, "error": `web.reason` 한 줄 | None,
        "lesson": 끌 때 저장한 lesson | None, "file": after.md가 일반 파일로 있는지}.
    """
    try:
        got = read_record(root, name)
    except RecordError:
        return {"broken": True}
    if got is None or not got[0]["events"]:
        return {"broken": True}
    doc = got[0]
    event, retire = after_event(doc)
    base_doc = doc if retire is None else {**doc, "events": doc["events"][:-1]}  # 은퇴는 덧붙일 한 줄이라 ②에 넣지 않는다
    values = error = None
    try:
        values = after_values(root, name, base_doc, date.fromisoformat(event["day"]))
    except (WorkspaceError, OSError, OverflowError, ValueError) as exc:  # 놓친 주문 시각을 UTC로 못 바꾸는 경우(OverflowError)도 이 길
        error = web.reason(exc, "끈 뒤 회고")
    return {"event": event, "retire": retire, "values": values, "error": error, "lesson": doc["lesson"],
            "file": exists_regular(root, f"strategies/{name}/after.md")}


def _period(recs: list[dict], futures: bool, whole: bool, title: str) -> dict:
    """회고 구간 칸 하나: 칸마다 `data.seen`으로 이번 구간에서 값이 있는 마지막 날 값과, 마지막 기록이 아닐 때 그 날을 모은다.

    Args:
        recs: 이 전략의 기록 전체(날짜순, 비어 있지 않음).
        futures: 가드레일 시장이 선물인지(펀딩비 줄을 낼지).
        whole: True면 실전 시작부터 값(`nums.all`), False면 이번 구간 값(`nums`).
        title: 칸 제목(날은 값에서 짠 `YYYY-MM-DD`).

    Returns:
        {"title", "whole", "futures", "head": {"actual" · "bt" · "gap": (값, 날 | None)}, "parts": {"slip" · "fee" · "funding" · "rest": (값, 날 | None)}, "missed_n": (값, 날 | None),
        "extra": (값, 날 | None)(실전 시작부터 칸이면 nums.all.extra),
        "bp": 이번 구간 칸이면 {"med" · "max": (값, 날 | None)}, 실전 시작부터 칸이면 None(저장된 값이 구간 값뿐)}.
    """
    def s(key: str) -> tuple[float | None, str | None]:
        return seen(recs, key, whole)

    return {"title": title, "whole": whole, "futures": futures, "head": {k: s(k) for k in ("actual", "bt", "gap")},
            "parts": {k: s(k) for k in ("slip", "fee", "funding", "rest")}, "missed_n": s("missed_n"), "extra": s("extra"),
            "bp": None if whole else {"med": s("slip_med_bp"), "max": s("slip_max_bp")}}


def _periods(recs: list[dict], futures: bool) -> list[dict]:
    """회고 구간 칸들: 칸 둘은 마지막 기록에 `nums.all.start`가 있고 `nums.period_start`와 다를 때만(`status` 전체 보기와 같은 조건), 아니면 `실전 시작부터` 하나.

    Args:
        recs: 이 전략의 기록 전체(날짜순, 비어 있지 않음).
        futures: 선물인지.

    Returns:
        칸 목록(둘이면 [이번 구간, 실전 시작부터]).
    """
    n = recs[-1]["nums"]
    whole = n.get("all") if isinstance(n.get("all"), dict) else {}
    start = whole.get("start") or next((r["nums"]["all"]["start"] for r in reversed(recs) if isinstance(r["nums"].get("all"), dict) and r["nums"]["all"].get("start")), None) \
        or n.get("period_start") or recs[0]["day"]
    out = [_period(recs, futures, True, f"실전 시작부터({start}부터)")]
    if whole.get("start") and whole["start"] != n.get("period_start"):
        out.insert(0, _period(recs, futures, False, f"이번 구간({n.get('period_start') or '모름'}부터)"))
    return out


def _fills(recs: list[dict]) -> dict:
    """체결 대조 요약: 이번 구간 nums에서 놓친 주문 후보 행(시각 UTC 오름차순) · 건수 · 실제에만 있는 체결 · 체결가 차이 중앙값 · 최대(bp). 건별 짝은 저장되지 않아 없다.

    Args:
        recs: 이 전략의 기록 전체(날짜순, 비어 있지 않음).

    Returns:
        {"rows": [{"when": "YYYY-MM-DD HH:MM"(UTC), "symbol", "side"}], "missed_n", "extra", "med", "max", "upto": bp를 마지막으로 안 날 | None, "error": None}.
        놓친 주문 시각을 UTC로 못 바꾸면 {"error": `web.reason` 한 줄}.
    """
    missed, count, extra = (seen(recs, k, False)[0] for k in ("missed", "missed_n", "extra"))
    (med, med_day), (mx, mx_day) = seen(recs, "slip_med_bp", False), seen(recs, "slip_max_bp", False)
    try:
        rows = sorted(((_utc(m["t"]), m) for m in missed or []), key=lambda x: x[0])
    except WorkspaceError as exc:
        return {"error": web.reason(exc, "매일 대조 기록")}
    return {"rows": [{"when": f"{t:%Y-%m-%d %H:%M}", "symbol": m["symbol"], "side": m["side"]} for t, m in rows],
            "missed_n": count if count is not None else len(missed or []), "extra": extra, "med": med, "max": mx, "upto": med_day or mx_day, "error": None}


def _first_retro(root: Path, name: str) -> bool:
    """첫 회고 표(retro.md)가 있는지: retro.md 머리줄을 직접 읽어 `notify.RETRO_HEAD`로 시작하는 일반 파일일 때만(`has_retro`는 daily/*.md만 있어도 참이라 쓰지 않는다).

    Args:
        root: .blackclip 경로.
        name: 전략 이름.

    Returns:
        머리가 맞는 retro.md가 있으면 True. 링크 · 없음 · 머리가 다름 · 읽지 못함은 False.
    """
    try:  # shortcut: workspace에 앞부분만 읽는 함수가 없어 4MiB 넘는 retro.md는 「없음」으로 본다, 앞부분 읽기가 생기면 바꾼다
        return read_bytes(root, f"strategies/{name}/retro.md", 4 << 20).startswith(RETRO_HEAD.encode())
    except (WorkspaceError, OSError):
        return False


def retro_view(root: Path, name: str) -> dict | None:
    """회고 화면 하나(`/s/<이름>/retro`)에 필요한 값을 읽어 모은다. 못 읽은 것은 예외 대신 그 칸의 사유 한 줄로 담는다(응답은 200).

    `retro_ok`가 아니면 None(404). 끈 전략이면 끈 뒤 칸 값(`_after_view`), 매일 대조 기록이 있으면 구간 칸 · 차트 B · 체결 대조 요약을 모은다. 요청마다 다시 읽는다.

    Args:
        root: .blackclip 경로.
        name: 주소에서 나온 이름(퍼센트 인코딩을 풀지 않은 그대로).

    Returns:
        `web.retro`가 받는 view(키는 그 함수 설명) 또는 None.
    """
    if not retro_ok(root, name):
        return None
    try:
        off = stopped(root).get(name)
        guard = bool(versions(root, name))
        first = _first_retro(root, name)
    except (WorkspaceError, OSError):  # 목록을 본 뒤 strategies/가 바뀐 경우
        return None
    try:
        got = read_guardrail(root, name)
    except (WorkspaceError, OSError):  # 가드레일을 못 읽어도 회고는 그린다(선물 여부만 모름)
        got = None
    view = {"name": name, "guard": off is not None or guard, "off": None if off is None else {"state": off[0], "day": off[1]},
            "after": None if off is None else _after_view(root, name), "first_retro": first,
            "futures": got is not None and web.obj(web.obj(got[1]).get("target")).get("market") == "futures", "has_days": False, "last": None,
            "periods": [], "b": None, "b_error": None, "fills": None, "error": None}
    try:
        recs = read_days(root, name)
    except (WorkspaceError, OSError) as exc:
        return view | {"error": web.reason(exc, "매일 대조 기록")}
    if not recs:
        return view
    try:
        b, b_error = b_series(recs), None
    except (WorkspaceError, OSError, OverflowError, ValueError) as exc:  # 놓친 주문 시각을 UTC로 못 바꾸는 경우도 이 길
        b, b_error = None, web.reason(exc, "매일 대조 기록")
    return view | {"has_days": True, "last": {k: recs[-1][k] for k in ("day", "live_day", "trades")}, "periods": _periods(recs, view["futures"]), "b": b, "b_error": b_error,
                   "fills": _fills(recs)}


def home_page(root: Path, token: str) -> bytes:
    """홈 화면. strategies/ 자체를 못 읽으면 화면 한 장의 「확인 불가」(200)로 바꾼다.

    Args:
        root: .blackclip 경로.
        token: 접속 토큰.

    Returns:
        UTF-8 HTML 바이트.
    """
    try:
        return web.home(*home_rows(root), token)
    except (WorkspaceError, OSError) as exc:
        return web.page("홈", f'<section class="panel"><p class="empty">확인 불가. {web.text(web.reason(exc, "전략 목록"))}</p></section>', token, web.HOME_NAV)


def _guard_cells(version: int, g: dict) -> dict:
    """내보내기 줄의 가드레일 칸. 선택 객체는 `web.obj`, 숫자는 `web.num`, 글은 날 꼴일 때만, 불리언은 불리언일 때만 읽는다.

    정상 범위의 위 · 아래는 그린 범위(`drawn`이 True)일 때만 낸다(화면과 같음). 시도 수는 0 이상의 정수일 때만 낸다.

    Args:
        version: 판 번호.
        g: 가드레일 내용(`read_guardrail`이 필수 칸의 모양을 검사함).

    Returns:
        가드레일 칸 열 개(`guardrail_version` 부터 `c3_unverified`까지). 모양이 틀린 칸은 None.
    """
    cost, nr, wl, c3 = (web.obj(g.get(k)) for k in ("cost", "normal_range", "warning_line", "c3"))
    drawn, trials, unverified = nr.get("drawn") is True, web.num(c3.get("trials")), c3.get("unverified")
    return {"guardrail_version": version, "set_on": web._iso(g.get("set_on")), "live_start": web._iso(g.get("live_start")),
            "cost_one_way_bp": web.num(cost.get("one_way_bp")),
            "normal_low": web.num(nr.get("low")) if drawn else None, "normal_high": web.num(nr.get("high")) if drawn else None,
            "warning_line": web.num(wl.get("line")), "c3_sharpe_cost0": web.num(c3.get("sharpe_cost0")),
            "c3_adjusted_low": web.num(c3.get("adjusted_low")),
            "c3_trials": int(trials) if trials is not None and trials.is_integer() and trials >= 0 else None,
            "c3_unverified": unverified if isinstance(unverified, bool) else None}


def export_rows(root: Path) -> list[dict]:
    """내보내기 줄들: 실전 · 끈 전략 모두 한 줄씩, 이름순, 키는 `EXPORT_FIELDS` 순서.

    실전 줄의 상태 · 이유 · 날 · 거래 · 실제 · 백테스트 · 차이 · 가드레일은 `home_rows`의 같은 줄에서 옮긴다(홈과 내보내기가 어긋나지 않게,
    실제 · 백테스트 · 비용 0 · 차이는 모두 「까지」 날 기록). 가드레일도 홈 줄이 읽은 것을 그대로 쓰므로 전략마다 요청 하나에 한 번만
    읽는다(두 번 읽으면 그 사이 파일이 바뀔 때 상태와 이유가 어긋남). 숫자는 `web.num`을 거친 비율 숫자 그대로다. `until`은 그 숫자들이 가리키는
    기록 날이다: 홈 줄의 `until`(마지막 날에 실제를 몰라 「(날까지)」를 붙이는 줄)이 없으면 마지막 기록 날이고, 기록이 없으면 None.
    끈 전략은 홈의 끈 표처럼 숫자 칸이 비고, 상태가 끈 상태(기록 손상이면 이유에 `web.BROKEN_NOTE`)다. 가드레일 칸은 가드레일을 읽을 수 있을 때만
    채운다. 못 읽은 사유는 `web.reason`이고(예외 글에는 경로가 든다), 끈 전략은 상태를 끈 상태로 두고 사유를 이유에 적는다. 읽지 못한 실전 전략은
    상태 `확인 불가`다. strategies/ 자체를 못 읽으면 이름을 알 수 없어, 전략 칸이 빈 `확인 불가` 한 줄을 낸다(200, 사유 `전략 목록`).

    Args:
        root: .blackclip 경로.

    Returns:
        칸 이름 → 값 dict의 목록. 값이 없으면 None(CSV 빈칸 · JSON null).
    """
    blank = dict.fromkeys(EXPORT_FIELDS)
    try:
        live, off, _ = home_rows(root)
    except (WorkspaceError, OSError) as exc:
        return [blank | {"state": "확인 불가", "reason": web.reason(exc, "전략 목록")}]
    rows = [blank | {"strategy": r["name"], "state": r["word"], "reason": r["reason"], "day": r["day"], "until": r["until"] or r["day"],
                     "live_day": r["live_day"], "trades": r["trades"], **{k: web.num(r[k]) for k in ("actual", "bt", "bt0", "gap")},
                     **(_guard_cells(*r["guardrail"]) if r["guardrail"] else {})} for r in live]
    for o in off:  # 끈 전략은 홈 줄이 없어 여기서 한 번 읽는다
        row = blank | {"strategy": o["name"], "state": o["state"], "reason": web.BROKEN_NOTE if o["state"] == BROKEN else None}
        try:
            if (got := read_guardrail(root, o["name"])) is not None:
                row |= _guard_cells(*got)
        except (WorkspaceError, OSError) as exc:
            row["reason"] = row["reason"] or web.reason(exc, "가드레일")
        rows.append(row)
    return sorted(rows, key=lambda r: r["strategy"])


def export_day(rows: list[dict]) -> str | None:
    """내보내기 파일의 기준 날(JSON `day` · 파일 이름): 줄들의 `day` 가운데 가장 늦은 날.

    `\\d{4}-\\d{2}-\\d{2}` 꼴(ASCII 숫자)의 글만 쓴다. 레포 글이 머리글(`Content-Disposition`)에 줄바꿈 · 따옴표를 심지 못하게.

    Args:
        rows: `export_rows` 결과.

    Returns:
        "YYYY-MM-DD" 또는 None(기록이 하나도 없음).
    """
    return max((d for r in rows if isinstance(d := r["day"], str) and _ISO_DAY.fullmatch(d)), default=None)


def export_csv(rows: list[dict]) -> bytes:
    """내보내기 CSV: UTF-8 BOM(엑셀의 한글) + 머리 줄 + 전략마다 한 줄. `csv` 모듈이 쉼표 · 따옴표 · 줄바꿈을 따옴표로 감싼다.

    글 칸이 `FORMULA_LEAD`로 시작하면 앞에 `'`를 붙여 스프레드시트가 수식으로 읽지 못하게 한다(OWASP CSV Injection). 숫자 · 불리언 · None은
    글이 아니라 그대로 두므로 음수도 숫자로 남고, None은 빈칸이다. UTF-8로 쓸 수 없는 낱 서로게이트는 `?`로 바꾼다(`replace`).

    Args:
        rows: `export_rows` 결과.

    Returns:
        UTF-8 바이트(BOM으로 시작, 줄 끝 CRLF).
    """
    buf = io.StringIO(newline="")
    out = csv.writer(buf)
    out.writerow(EXPORT_FIELDS)
    out.writerows([("'" + v if isinstance(v, str) and v.startswith(FORMULA_LEAD) else v) for v in (r[k] for k in EXPORT_FIELDS)] for r in rows)
    return buf.getvalue().encode("utf-8-sig", "replace")


def export_json(rows: list[dict], day: str | None) -> bytes:
    """내보내기 JSON: `{"format", "version", "day", "fields", "strategies"}`. 한글은 그대로(`ensure_ascii=False`), NaN은 내지 않는다.

    Args:
        rows: `export_rows` 결과.
        day: 기준 날(`export_day`) 또는 None.

    Returns:
        UTF-8 바이트(BOM 없음: JSON은 BOM을 금지함).
    """
    doc = {"format": "blackclip-guardrail-export", "version": 1, "day": day, "fields": FIELD_MEANINGS, "strategies": rows}
    return (json.dumps(doc, ensure_ascii=False, allow_nan=False, indent=2) + "\n").encode("utf-8", "replace")


def export_response(root: Path, path: str) -> tuple[bytes, dict[str, str]]:
    """내보내기 응답 하나: 본문과 덮어쓸 머리글(`Content-Type` · `Content-Disposition`).

    파일 이름은 `blackclip-guardrail-{day}.csv|json`, 기준 날이 없으면 `blackclip-guardrail.csv|json`이다. 레포 글은 들어가지 않는다.

    Args:
        root: .blackclip 경로.
        path: `/export.csv` 또는 `/export.json`.

    Returns:
        (본문, 머리글 두 개).
    """
    ext, mime = EXPORT_TYPES[path]
    rows = export_rows(root)
    day = export_day(rows)
    body = export_csv(rows) if ext == "csv" else export_json(rows, day)
    return body, {"Content-Type": f"{mime}; charset=utf-8",
                  "Content-Disposition": f'attachment; filename="blackclip-guardrail{f"-{day}" if day else ""}.{ext}"'}


@dataclass
class Once:
    """한 번용 연결 서버(`blackclip connect --open`)의 상태.

    Attributes:
        kind: 연 패널(`key` · `telegram` · `choose`). 다른 kind POST는 403.
        event: 저장(부분 실패 포함)을 마치고 응답을 쓴 뒤(못 써도) 켜진다.
        result: 저장 결과(`connect.Saved`), 쓰기가 일어나기 전에는 None.
        done: 쓰기가 일어났거나 `serve`가 한 번용을 닫았으면 True(그 뒤 POST는 CSRF와 무관하게 403).
    """

    kind: str
    event: threading.Event
    result: Saved | None = None
    done: bool = False


KEYSTORE_WHY = "키 보관을 읽지 못했어요"
"""연결 화면: 키 보관 읽기가 실패한 칸의 사유(보관 오류 글에는 경로가 들어 있어 쓰지 않는다)."""


def connect_view(root: Path, connect: Connect) -> dict:
    """연결 화면(`/connect`)에 필요한 값을 요청마다 다시 읽어 모은다. 못 읽은 칸은 예외 대신 그 칸의 사유 한 줄(경로 없음).

    기존 판정 · 원천만 부른다: 연결 상태 `load_connections` · 키 보관 `load` · `fillstore.read_coverage` · `notify.settings` ·
    `chosen_sensitivity` · `first_retro` · `has_retro` · `stages.judge`.

    Args:
        root: .blackclip 경로.
        connect: 연결 설정(계정 · 키 보관).

    Returns:
        `web.connect`가 받는 view: title · account · store · key · token({"held", "last4", "why"}) · permission({"status", "checked_at", "why"}) ·
        coverage({"spans": [(시장, 처음, 끝)], "why"}) · linked · choose({"sensitivity", "quiet", "execution_alerts", "healthcheck", "first_retro"} | None) ·
        choose_why · setup(`stages.judge` 결과 | None) · setup_why · retros(첫 회고가 있는 전략 이름).
    """
    from blackclip.fillstore import read_coverage
    from blackclip.notify import chosen_sensitivity, settings
    from blackclip.stages import judge

    key = account_key(connect.label, connect.testnet)

    def held(account: str) -> dict:
        try:
            creds = connect.store.load(account)
        except (KeystoreError, OSError):
            return {"held": None, "last4": None, "why": KEYSTORE_WHY}
        return {"held": creds is not None, "last4": creds.last4() if creds else None, "why": None}

    permission = {"status": None, "checked_at": None, "why": None}
    try:
        p = section(load_connections(root), "binance", key, "permission")
        permission.update(status=p.get("status") if isinstance(p.get("status"), str) else None, checked_at=p.get("checked_at"))
    except (WorkspaceError, OSError) as exc:
        permission["why"] = web.reason(exc, "연결 상태")
    coverage: dict = {"spans": [], "why": None}
    try:
        for market in ("spot", "futures"):
            cov = read_coverage(root / "fills" / f"binance-{key}" / market)
            starts = [m for c in cov.values() if (m := web._minute(c["from"]))]
            ends = [m for c in cov.values() if (m := web._minute(c["until"]))]
            if starts and ends:
                coverage["spans"].append((market, min(starts), max(ends)))
    except (WorkspaceError, OSError, ValueError) as exc:
        coverage = {"spans": [], "why": web.reason(exc, "받은 범위")}
    try:
        s = settings(root)
        choose, choose_why = {"sensitivity": chosen_sensitivity(root), "quiet": s["quiet"], "execution_alerts": bool(s["execution_alerts"]),
                              "healthcheck": bool(s["healthcheck"]), "first_retro": first_retro(root)}, None
        linked = s["chat_id"] is not None
    except (WorkspaceError, OSError) as exc:
        choose, choose_why, linked = None, web.reason(exc, "연결 상태"), None
    try:
        setup, setup_why = judge(root), None
    except (WorkspaceError, OSError) as exc:
        setup, setup_why = None, web.reason(exc, "셋업 상태")
    try:
        retros = [n for n in strategy_names(root) if _NAME.fullmatch(n) and _is_folder(root / "strategies" / n) and has_retro(root / "strategies" / n)]
    except (WorkspaceError, OSError):
        retros = []
    return {"title": connect.title, "account": f"binance {connect.label}" + (" 테스트넷" if connect.testnet else ""), "store": connect.store.name,
            "key": held(account_name("binance", key)), "token": held(TELEGRAM_ACCOUNT), "permission": permission, "coverage": coverage,
            "linked": linked, "choose": choose, "choose_why": choose_why, "setup": setup, "setup_why": setup_why, "retros": retros}


def connect_page(root: Path, connect: Connect, once: Once | None, token: str, kind: str = "key", note: str | None = None,
                 sent: dict[str, str] | None = None, bad: str | None = None) -> bytes:
    """연결 화면 한 장(상주 · 한 번용).

    Args:
        root: .blackclip 경로.
        connect: 연결 설정.
        once: 한 번용이면 그 상태(보일 패널은 `once.kind`), 상주면 None.
        token: 접속 토큰.
        kind: 실패 줄을 붙일 패널(상주).
        note: 실패 줄 또는 None.
        sent: 고르기 실패 뒤 다시 그릴 때 방금 제출한 값(`sensitivity` · `quiet` · `execution_alerts` · `ea_shown`만). 저장된 값은 바꾸지 않는다.
        bad: 고르기 실패가 가리키는 칸 이름(`healthcheck` · `execution_alerts`) 또는 None.

    Returns:
        UTF-8 HTML 바이트.
    """
    return web.connect(connect_view(root, connect), token, connect.csrf, once.kind if once else kind, once is not None, note, sent, bad)


CSRF_TEXT = "화면이 바뀌었어요. 연결 화면을 다시 열어 주세요."
"""403(폼 CSRF가 지금 값과 다름: 옛 폼 · 다른 탭 · 다시 보내기) 본문 한 줄."""

ONCE_DONE_TEXT = "이미 저장했어요. 이 창을 닫아 주세요."
"""한 번용 서버에서 저장한 뒤 온 POST(CSRF와 무관하게 403) 본문 한 줄."""

ONCE_CLOSED_TEXT = "시간이 지나 이 창을 닫았어요. 저장하지 않았어요. 연결 화면을 다시 열어 주세요."
"""한 번용 서버가 저장 없이 닫힌 뒤(기다림 끝) Lock을 얻은 POST(403 · 쓰기 0) 본문 한 줄."""

ONCE_KIND_TEXT = "이 창에서는 {}만 넣어요."
"""한 번용 서버에 다른 패널 POST가 왔을 때 403 본문 한 줄(`{}`에 연 패널 이름 `connect.KINDS`)."""

TOO_BIG_TEXT = "보낸 글의 길이가 맞지 않아요. 연결 화면을 다시 열어 주세요."
"""413(본문 길이 0 이하 · 4096바이트 넘음) 본문 한 줄."""

GITIGNORE_TEXT = ("작업 폴더의 .gitignore가 blackclip이 만든 판과 달라 저장하지 않았어요. "
                  "AI에게 blackclip connect --status를 부탁하면 다시 써요.")
"""409(`workspace.ignore_intact` 거짓: 쓰면 키 · 연결 상태가 Git에 들어갈 수 있음) 본문 한 줄."""

BUSY_TEXT = "다른 blackclip 작업이 연결 상태를 쓰고 있어요. 잠시 뒤 다시 저장해 주세요."
"""503(연결 상태 잠금을 5초 안에 못 얻음, 쓰기 0) 본문 한 줄."""

STATE_FAIL_TEXT = "연결 상태를 적지 못해 저장하지 않았어요. AI에게 blackclip connect --status를 부탁해 주세요."
"""500(키 보관보다 먼저 하는 연결 상태 쓰기가 실패, 쓰기 0) 본문 한 줄."""

CHECK_FAIL_STEM = "권한을 확인하지 못해 저장하지 않았어요"
"""502(권한 · getMe 확인이 예외) 실패 줄 앞 문장의 마침표 앞까지. 바이낸스가 응답을 줬으면 이 뒤에 `(바이낸스 응답 번호)`가 붙는다(오류 코드는 식별자라 웹 숫자 규칙을 못 지켜 터미널에만 남김)."""

CHECK_FAIL_TEXT = CHECK_FAIL_STEM + "."
"""502 실패 줄 앞 문장. 응답 번호가 없으면 뒤에 `CHECK_RETRY_TEXT`. 엔진 사유 글(합쇼체 · 터미널용)은 웹에 싣지 않는다."""

CHECK_RETRY_TEXT = "잠시 뒤 다시 넣어 주세요."
"""502에서 바이낸스 응답 번호가 없을 때(다시 부르기를 다 씀 · 응답 모양 오류 · 텔레그램 · 그 밖 예외) 앞 문장 뒤에 붙는 한 문장."""

TG_FAIL_TEXT = "봇을 확인하지 못해 저장하지 않았어요."
"""텔레그램 502(getMe 확인이 예외) 실패 줄 앞 문장. 뒤에 `CHECK_RETRY_TEXT`. 텔레그램 토큰에는 「권한」이 없어 키와 글을 나눈다."""

REJECTED_TEXT = "조회 말고 다른 권한도 켜진 키라 저장하지 않았어요."
"""권한 확인에서 거부된 키 가운데 조회 말고 켜진 권한이 있는 경우(`Permission.enabled`)의 실패 줄 첫 줄. 둘째 줄에 켜진 권한 이름 · 다시 만드는 안내."""

PERMISSION_WORDS: dict[str, str] = {
    "enableWithdrawals": "출금", "enableSpotAndMarginTrading": "현물과 마진 거래", "enableFutures": "선물",
    "enableVanillaOptions": "옵션", "enablePortfolioMarginTrading": "포트폴리오 마진 거래", "enableFixApiTrade": "FIX API 거래",
    "enableMargin": "마진 대출(상환과 이체 포함)", "enableInternalTransfer": "내부 이체", "permitsUniversalTransfer": "계정 간 이체"}
"""바이낸스 apiRestrictions 필드 → 화면 이름, 화면에 나오는 순서(위험 순: 출금 → 거래 → 대출 → 이체). 화면 글은 ` · `를 `, `로 바꾸므로(`web.text`) 이름 안에 가운뎃점을 쓰지 않는다. 표에 없는 필드는 이름을 싣지 않고 개수로만 쓴다(모르는 영문이 화면에 나가지 않게)."""


def permission_words(enabled: tuple[str, ...]) -> str:
    """켜진 권한 필드를 화면 말로 잇는다.

    Args:
        enabled: `Permission.enabled`.

    Returns:
        「출금, 현물과 마진 거래」 꼴(`PERMISSION_WORDS` 순서). 표에 없는 것은 끝에 「그 밖의 권한 N개」.
    """
    known = [w for n, w in PERMISSION_WORDS.items() if n in enabled]
    rest = len(enabled) - len(known)
    return ", ".join(known + ([f"그 밖의 권한 {rest}개"] if rest else []))

READING_OFF_TEXT = "조회 권한이 꺼진 키라 저장하지 않았어요. 바이낸스 API 관리에서 조회 권한을 켜 주세요."
"""권한 확인에서 조회 권한이 꺼진 키(`Permission.reading_off`)의 실패 줄 한 줄."""

EA_TEXT = "실행 경고는 첫 회고를 본 뒤에 켤 수 있어요."
"""첫 회고 전에 실행 경고를 켜려 할 때 고르기 패널 실패 줄(400)."""

POST_PATHS = {f"/connect/{k}": k for k in KINDS}
"""POST가 되는 경로 셋 → 패널(그 밖 POST는 405)."""

LINK_FIELDS = ("chat_id", "bot", "linked_at")
"""텔레그램 대화 연결 칸(`notify.link_telegram`이 적음). 새 토큰을 넣으면 보관보다 먼저 지운다."""


def _why(exc: BaseException) -> str:
    """쓰기를 시작한 뒤 난 예외의 사유(`Saved.state_error`, 터미널용, 늘 빈 글 아님).

    Args:
        exc: 예외.

    Returns:
        보관 · 작업 폴더 · 파일 오류는 그 글(비었으면 종류 이름), 그 밖 종류는 이름만(글에 무엇이 들지 몰라서).
    """
    return (str(exc) if isinstance(exc, (KeystoreError, WorkspaceError, OSError)) else "") or type(exc).__name__


def _save_key(root: Path, connect: Connect, creds: Credentials, perm: Permission, wrote: Callable[[], None]) -> Saved:
    """조회 키를 보관하고 같은 연결 잠금 안에서 권한 확인 결과를 적는다(`connections` 잠금을 쥔 쪽만 부름).

    Args:
        root: .blackclip 경로.
        connect: 연결 설정.
        creds: 검증한 키 한 쌍.
        perm: 권한 확인 결과(거부 아님).
        wrote: 쓰기 단계에 들어가는 순간(보관을 부르기 직전) 부른다.

    Returns:
        Saved. 보관이 예외면 permission None(썼는지 모름), 연결 상태 기록이 실패하면 permission은 그대로 · state_error에 사유.
        보관을 부른 뒤에는 예외 종류를 가리지 않는다(쓰기가 일어났을 수 있으면 CSRF · 한 번용을 바꿔야 해서).

    Raises:
        KeystoreBusy: 보관 잠금을 기다려도 못 얻음(쓰기 0).
    """
    from blackclip.fetch import note_permission

    out = Saved("key", creds.last4(), connect.store.name, perm)
    wrote()
    try:
        connect.store.save(account_name("binance", account_key(connect.label, connect.testnet)), creds)
    except KeystoreBusy:
        raise
    except Exception as exc:  # noqa: BLE001 - 키체인은 쓴 뒤 다시 읽어 검증하므로 어떤 예외든 이미 썼을 수 있다
        return Saved("key", out.last4, out.store_name, None, _why(exc))
    try:
        apply_connections(root, lambda c: note_permission(c, connect.label, connect.testnet, perm, datetime.now(timezone.utc)))
    except Exception as exc:  # noqa: BLE001 - 키는 이미 보관했으니 종류와 상관없이 「부분 실패」
        out.state_error = _why(exc)
    return out


def _save_telegram(root: Path, connect: Connect, creds: Credentials, perm: Permission, wrote: Callable[[], None]) -> Saved | None:
    """대화 연결 칸을 먼저 지우고 봇 토큰을 보관한다(`connections` 잠금을 쥔 쪽만 부름).

    지운 뒤 보관이 실패해도 「대화만 끊긴」 안전 쪽이다: 옛 대화가 새 봇 토큰과 짝지어 남지 않는다.

    Args:
        root: .blackclip 경로.
        connect: 연결 설정.
        creds: Credentials(토큰, "").
        perm: getMe 확인 결과.
        wrote: 지우기를 쓴 순간(apply_connections가 돌아온 뒤) 부른다. 그 전 실패는 쓰기 0이 확실하다(쓰기는 os.replace 한 번).

    Returns:
        Saved(보관이 예외면 permission None), 지우기가 실패해 아무것도 안 썼으면 None.
    """
    def clear(c: dict) -> None:
        t = section(c, "telegram")
        for k in LINK_FIELDS:
            t.pop(k, None)

    try:
        apply_connections(root, clear)
    except (WorkspaceError, OSError):
        return None
    wrote()
    try:
        connect.store.save(TELEGRAM_ACCOUNT, creds)
    except Exception as exc:  # noqa: BLE001 - 지우기가 이미 썼고 보관도 썼을 수 있다(잠금 상한 포함): 종류와 상관없이 「확인 못함」
        return Saved("telegram", creds.last4(), connect.store.name, None, _why(exc))
    return Saved("telegram", creds.last4(), connect.store.name, perm)


def connect_post(root: Path, connect: Connect, once: Once | None, token: str, kind: str, form: dict[str, str]) -> tuple[int, bytes | str]:
    """연결 화면 POST 하나를 처리한다. 서버 Lock 안에서만 부른다(POST는 한 번에 하나, CSRF 비교 · 바꿈이 같은 Lock 안).

    순서: 한 번용 완료 403 → CSRF 403 → `.gitignore` 409 → 값 검증 400 → 확인(권한 · getMe) 502 · 거부 → 저장(연결 잠금 → 키 보관).
    쓰기가 일어났거나 일어났을 수 있으면 CSRF를 새 값으로 바꾸고, 한 번용이면 `once.result` · `once.done`을 넣는다.

    Args:
        root: .blackclip 경로.
        connect: 연결 설정(`csrf`를 바꿈).
        once: 한 번용 상태 또는 None.
        token: 접속 토큰.
        kind: `key` · `telegram` · `choose`.
        form: 본문을 푼 값(칸마다 첫 값).

    Returns:
        (HTTP 상태, 페이지 바이트) 또는 (상태, 거절 한 줄 str — `send_error`로 냄).
    """
    if once is not None and once.done:  # 저장한 뒤 · 저장 없이 시간이 지나 닫힌 뒤를 결과 유무로 가름
        return 403, ONCE_DONE_TEXT if once.result is not None else ONCE_CLOSED_TEXT
    if not secrets.compare_digest(form.get("csrf", "").encode(), connect.csrf.encode()):  # 바이트 비교: 비ASCII면 str 비교가 TypeError
        return 403, CSRF_TEXT
    if not ignore_intact(root):
        return 409, GITIGNORE_TEXT

    def again(status: int, note: str, bad: str | None = None) -> tuple[int, bytes]:
        # 고른 값은 다시 싣고(WCAG 3.3.7) 비밀 칸(헬스체크 포함)은 싣지 않는다: 허용한 네 칸만 골라 넘긴다
        sent = {k: form[k] for k in ("sensitivity", "quiet", "execution_alerts", "ea_shown") if k in form} if kind == "choose" else None
        return status, connect_page(root, connect, once, token, kind, note, sent, bad)

    value = form_value(kind, form)
    if isinstance(value, str):
        return again(400, value, "healthcheck" if value == HEALTHCHECK_SHAPE_TEXT else None)
    if kind == "choose" and value["execution_alerts"] and not first_retro(root):
        return again(400, EA_TEXT, "execution_alerts")
    if kind != "choose":
        try:
            perm = (connect.check_key if kind == "key" else connect.check_telegram)(value)
        except (BinanceError, OSError, ValueError) as exc:  # TelegramError는 ValueError의 하위
            # 엔진 사유 글(합쇼체 · 터미널용)은 싣지 않는다: 바이낸스가 준 응답은 상태 번호만(오류 코드는 네 자리 식별자라 웹 숫자 규칙에 걸림), 그 밖은 다시 넣어 달라는 한 문장
            if kind == "telegram":
                note = f"{TG_FAIL_TEXT} {CHECK_RETRY_TEXT}"
            elif isinstance(exc, BinanceError) and exc.status > 0:
                note = f"{CHECK_FAIL_STEM}(바이낸스 응답 {exc.status})."
            else:
                note = f"{CHECK_FAIL_TEXT} {CHECK_RETRY_TEXT}"
            return again(502, note, web.CHECK_FAILED)  # 입력이 틀린 게 아니라 확인이 안 된 것: 칸에 aria-invalid를 붙이지 않음
        if perm.status == "rejected":  # 사유는 글이 아니라 Permission 필드로 가른다(`detail`은 싣지 않음)
            if perm.reading_off:
                return again(200, READING_OFF_TEXT)
            return again(200, f"{REJECTED_TEXT}\n켜진 권한: {permission_words(perm.enabled)}. 바이낸스에서 조회만 켠 키를 새로 만들어 넣어 주세요.")
    writing = []  # 쓰기 단계에 들어가면 값 하나: 그 뒤 예외는 잠금을 빠져나오는 것까지 종류와 상관없이 「확인 못함」

    def wrote() -> None:
        writing.append(True)

    try:
        if kind == "choose":
            # 안에서 update_connections(연결 잠금): 바꾸기를 마치면 wrote, 터미널용 다시 읽기는 부르지 않는다
            change_notify(root, **value, on_change=wrote)
            saved = Saved("choose", None, None, None)
        else:
            with lock(root, "connections", 5):  # 잠금 순서: 서버 Lock → 연결 상태 → 키 보관 파일
                saved = (_save_key if kind == "key" else _save_telegram)(root, connect, value, perm, wrote)
    except (LockBusy, KeystoreBusy):  # 연결 잠금 · 키 보관 잠금을 못 얻음(키 보관은 _save_key만 올림): 쓰기 0
        return 503, BUSY_TEXT
    except Exception as exc:
        if not writing:  # 쓰기 전: 잠금 파일을 열지 못함(링크 · 권한) · 깨진 연결 상태 · 칸이 객체 아님
            if kind == "choose" and isinstance(exc, WorkspaceError):
                return again(400, web.reason(exc, "연결 상태"))
            if isinstance(exc, (WorkspaceError, OSError)):
                return 500, STATE_FAIL_TEXT
            raise
        saved = Saved(kind, None if kind == "choose" else value.last4(), None if kind == "choose" else connect.store.name, None, _why(exc))
    if saved is None:  # telegram 지우기가 실패: 쓰기 0
        return 500, STATE_FAIL_TEXT
    connect.csrf = secrets.token_urlsafe(32)
    if once is not None:
        once.result, once.done = saved, True
    return 500 if saved.state_error is not None else 200, web.connect_saved(saved, token, once is not None)


def make_server(root: Path, port: int, token: str | None = None, *, connect: Connect | None = None,
                once: Once | None = None) -> ThreadingHTTPServer:
    """127.0.0.1에만 묶인 대시보드 서버를 만든다(띄우지는 않음). 서버 구현은 이것 하나다.

    Args:
        root: .blackclip 경로.
        port: 포트(0이면 아무 빈 포트).
        token: 접속 토큰. None이면 `secrets.token_urlsafe(32)`로 새로 만든다.
        connect: 연결 화면 설정. None이면 `/connect` · `POST /connect/*`는 404.
        once: 한 번용 서버면 그 상태(GET `/connect`와 그 kind POST만 받고, 저장 뒤 `event`가 켜짐). 상주면 None.

    Returns:
        ThreadingHTTPServer(`server.token`에 접속 토큰, `server.post_lock`에 POST를 하나씩 하는 Lock).

    Raises:
        OSError: 포트를 열지 못함(쓰이는 중 등).
    """
    token = token or secrets.token_urlsafe(32)

    class Handler(SecureHandler):
        """대시보드 요청 처리(바탕: `SecureHandler`): 검사(400 · 403) → 메서드(405) → 라우트(404) → 렌더(200 · 500) · 연결 화면 저장(POST)."""

        def _checked(self) -> bool:
            """보안 검사를 라우트보다 먼저: Host 없음 · 둘이면 400, 그 밖 실패는 403을 내고 False.

            Returns:
                통과면 True.
            """
            code = self._refusal(token)
            if code is not None:
                self.send_error(code, explain=FORBIDDEN_TEXT if code == 403 else None)
            return code is None

        def do_GET(self) -> None:
            """홈 · 가드레일 · 회고 화면과 내보내기를 낸다. 렌더 중 읽기 오류는 화면 안 「확인 불가」(200), 그 밖 예외는 500 한 장."""
            if not self._checked():
                return None
            path = urlsplit(self.path).path  # 퍼센트 인코딩을 풀지 않는다: `%2e%2e`는 이름 모양에 안 맞아 404
            extra = None
            if once is not None and path != "/connect":  # 한 번용 서버는 연결 화면 하나만(내비 있는 화면을 내지 않음)
                return self.send_error(404, explain=NOT_FOUND_TEXT)
            try:
                if path == "/":
                    body = home_page(root, token)
                elif path == "/connect":
                    if connect is None:
                        return self.send_error(404, explain=NOT_FOUND_TEXT)
                    body = connect_page(root, connect, once, token)
                elif path.startswith("/s/") and path.endswith("/retro") and (rview := retro_view(root, path[3:-6])) is not None:
                    body = web.retro(rview, token)
                elif path.startswith("/s/") and (view := guard_view(root, path[3:])) is not None:
                    body = web.guard(view, token)
                elif path in EXPORT_TYPES:
                    body, extra = export_response(root, path)
                else:
                    return self.send_error(404, explain=NOT_FOUND_TEXT)
            except Exception:  # noqa: BLE001 - 사유는 화면에 내지 않음(경로 · 레포 글이 들 수 있음)
                return self.send_error(500, explain=FAILED_TEXT)
            self._send(200, body, extra)

        do_HEAD = do_GET

        def do_POST(self) -> None:
            """연결 화면 저장: 검사(400 · 403) → 경로(셋 밖 405 · 연결 설정 없음 404 · 한 번용 다른 패널 403) → 길이(413) → 서버 Lock 안
            `connect_post` → 응답(못 써도 한 번용이면 `once.event`를 켬)."""
            if not self._checked():
                return None
            kind = POST_PATHS.get(urlsplit(self.path).path)
            if kind is None:
                return self._not_allowed()
            if connect is None:
                return self.send_error(404, explain=NOT_FOUND_TEXT)
            if once is not None and kind != once.kind:
                return self.send_error(403, explain=ONCE_KIND_TEXT.format(KINDS[once.kind]))
            try:
                n = int(self.headers.get("Content-Length", "0"))
            except ValueError:
                n = -1
            if not 0 < n <= MAX_BODY:
                return self.send_error(413, explain=TOO_BIG_TEXT)
            form = {k: v[0] for k, v in parse_qs(self.rfile.read(n).decode("utf-8", "replace")).items()}
            try:
                with self.server.post_lock:
                    status, out = connect_post(root, connect, once, token, kind, form)
            except Exception:  # noqa: BLE001 - 사유는 화면에 내지 않음(경로 · 레포 글이 들 수 있음)
                status, out = 500, FAILED_TEXT
            try:
                if isinstance(out, str):
                    self.send_error(status, explain=out)
                else:
                    self._send(status, out)
            except ConnectionError:  # 응답 전에 브라우저가 끊음(BrokenPipe · Reset): 저장 결과는 once.result에 있어 CLI는 그것으로 끝남
                pass
            finally:
                if once is not None and once.done:
                    once.event.set()

        def _not_allowed(self) -> None:
            """405. `Allow`는 그 경로가 지금 받는 방법이다(RFC 9110 §15.5.6): 연결 POST 경로 셋은 GET이 404라 POST뿐, 그 밖은 GET, HEAD."""
            posts = connect is not None and urlsplit(self.path).path in POST_PATHS
            self.send_error(405, explain=METHOD_TEXT, allow="POST" if posts else "GET, HEAD")

        def _refuse(self) -> None:
            """GET · HEAD · POST 밖 메서드: 검사에 지면 400 · 403, 아니면 405."""
            if self._checked():
                self._not_allowed()

        do_PUT = do_DELETE = do_PATCH = do_OPTIONS = _refuse

    server = ThreadingHTTPServer(("127.0.0.1", port), Handler)
    server.token = token
    server.post_lock = threading.Lock()  # POST는 한 번에 하나(CSRF 비교 → 저장 → CSRF 바꿈이 한 덩어리), GET은 잠그지 않음
    return server


def serve(root: Path, port: int, open_browser: bool, out: Callable[[str], None] = lambda s: print(s, flush=True),
          stop: threading.Event | None = None, *, connect: Connect | None = None, once: Once | None = None, path: str = "/",
          wait_s: float = 900) -> int | Saved | None:
    """대시보드를 띄운다. 상주면 Ctrl+C(또는 stop)까지, 한 번용(`once`)이면 저장되거나 `wait_s`가 지날 때까지 돈다.

    Args:
        root: .blackclip 경로.
        port: 포트(0이면 아무 빈 포트).
        open_browser: 이 컴퓨터 브라우저로 열지.
        out: 안내 줄을 내보낼 곳. 기본은 줄마다 flush하는 print(파이프 stdout에서도 주소가 바로 나가게).
        stop: 켜지면 끝냄(상주, 시험용). None이면 Ctrl+C까지.
        connect: 연결 화면 설정(`make_server`에 넘김).
        once: 한 번용 상태. 있으면 `once.event`를 `wait_s`만큼 기다린다.
        path: 안내 주소의 경로(쿼리를 붙일 수 있음, 접속 토큰은 경로 바로 뒤 `?t=`로 붙임).
        wait_s: 한 번용 기다림(초, 기본 15분).

    Returns:
        상주는 0. 한 번용은 `once.result`(저장 결과, 시간이 지나면 None) — 서버 POST Lock을 잡고 닫음 표시를 한 뒤에 읽는다
        (처리 중이던 저장이 Lock 안에서 끝난 뒤의 값, 그 뒤 POST는 403).

    Raises:
        OSError: 포트를 열지 못함(쓰이는 중이면 errno EADDRINUSE).
        KeyboardInterrupt: 한 번용에서 Ctrl+C(시간 지남과 섞지 않음, 포트는 닫음).
    """
    server = make_server(root, port, connect=connect, once=once)
    p = server.server_address[1]
    base, _, query = path.partition("?")
    url = f"http://127.0.0.1:{p}{base}?t={server.token}" + (f"&{query}" if query else "")
    try:  # 만든 바로 뒤부터: 안내 줄 · 브라우저 여는 중의 Ctrl+C · 예외도 포트를 닫고 끝남
        threading.Thread(target=server.serve_forever, daemon=True).start()
        out(f"{'대시보드' if once is None else '연결 화면'}: {url}")
        out(f"화면 없는 서버면 노트북에서 ssh -L {p}:127.0.0.1:{p} <서버> 뒤 위 주소를 여세요.")
        out("끌 때는 Ctrl+C를 눌러 주세요." if once is None else f"{int(wait_s // 60)}분 뒤 닫힙니다.")
        if open_browser:
            webbrowser.open(url)
        if once is None:
            (stop or threading.Event()).wait()
        else:
            once.event.wait(wait_s)
    except KeyboardInterrupt:
        if once is not None:
            raise
    finally:
        server.shutdown()
        if once is not None:
            # 요청 스레드는 daemon이라(ThreadingHTTPServer.daemon_threads) server_close가 기다리지 않는다. 저장은 POST Lock 안에서
            # 끝나므로 Lock을 잡아 처리 중인 저장을 기다리고, 닫음 표시(done)로 그 뒤 온 POST는 쓰기 없이 403이 되게 한다.
            with server.post_lock:
                once.done = True
        server.server_close()
    return 0 if once is None else once.result

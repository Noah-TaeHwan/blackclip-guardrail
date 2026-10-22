"""알림 설정 · 텔레그램 대화 잇기 · 실전 시작 잔고 파일 · 헬스체크.

설정은 `.blackclip/connections.json`의 "telegram" 칸(Git 제외), 실전 시작 잔고 · 포지션 · 기준 자본(선택)은 `strategies/<전략>/live.json`(Git 제외).
토큰은 키 보관에만 있고 여기 파일에는 쓰지 않는다.
"""
from __future__ import annotations

import json
import re
import secrets
import time
import urllib.request
from collections.abc import Callable
from datetime import datetime, timedelta, timezone
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any

from blackclip.keystore import KeystoreError, account_name
from blackclip.sim.warning_line import DEFAULT_SENSITIVITY, SENSITIVITY, year_false_alarm
from blackclip.telegram import Bot, TelegramError, bind_chat, skip_backlog
from blackclip.workspace import (WorkspaceError, account_key, load_connections, read_bytes, section, update_connections,
                                 versions, write_in)

TELEGRAM_ACCOUNT = account_name("telegram", "main")
"""텔레그램 토큰의 키 보관 이름."""

QUIET = ("매일", "바뀔때만")
"""조용한 날 선택: 매일 한 줄 / 바뀔 때만."""

DEFAULTS: dict[str, Any] = {"quiet": "매일", "healthcheck": None, "execution_alerts": False}
"""알림 설정 기본값(기본 「매일 한 줄」, 실행 경고는 첫 회고 뒤 켬)."""

NO_HEALTHCHECK = "바뀔 때만 · 헬스체크 주소 없음: 엔진(이 컴퓨터)이 멈춰도 알 길이 없음 · blackclip notify --healthcheck <주소>를 권함"
"""「바뀔 때만」인데 헬스체크 주소가 없을 때 한 줄."""

LINK_WAIT_S = 900
"""대화 잇기에서 숫자를 기다리는 초(15분)."""

_HC = re.compile(r"https://[^\s]{4,300}")
"""헬스체크 주소 모양(https만)."""

CAPITAL_TEXT = "--capital: 거래 통화 기준 0보다 큰 숫자로 주세요(예: 10000)"
"""기준 자본 입력 오류 한 줄. 입력한 금액은 문구에 넣지 않는다."""

LINKED_TEXT = "BlackClip Guardrail 연결을 확인했어요. 상태가 바뀔 때와 매일 묶음(고른 경우)이 이 대화로 와요."
"""대화를 잇고 처음 보내는 확인 글."""

CAPITAL_DROPPED_TEXT = ("기준 자본(--capital)을 주지 않아 지난번 값을 지웠습니다. "
                        "이제 계좌 잔고 전체 + 출발 포지션 기준으로 %를 셉니다(유지하려면 --capital을 다시 주세요).")
"""live를 --capital 없이 다시 돌려 지난 기준 자본이 지워질 때 화면에 띄우는 한 줄. 금액 값은 넣지 않는다."""


def _capital_ok(value: Decimal) -> bool:
    """기준 자본으로 쓸 수 있는 값인가(유한하고 0보다 큼).

    Args:
        value: 금액.

    Returns:
        쓸 수 있으면 True.
    """
    return value.is_finite() and value > 0


def parse_capital(text: str) -> Decimal:
    """`blackclip live --capital` 값을 거래 통화 기준 양수로 읽는다.

    Args:
        text: 입력.

    Returns:
        기준 자본.

    Raises:
        WorkspaceError: 숫자가 아니거나 0 이하 · 무한대 · NaN일 때(금액 값은 문구에 안 넣음).
    """
    try:
        value = Decimal(text.strip())
    except InvalidOperation:
        raise WorkspaceError(CAPITAL_TEXT) from None
    if not _capital_ok(value):
        raise WorkspaceError(CAPITAL_TEXT)
    return value


def sensitivity_text(name: str) -> str:
    """민감도 한 칸의 뜻(고객 말: 1년 내내 보면 헛경보가 한 번 이상 날 확률, 둔감은 늘 「미검증」).

    Args:
        name: SENSITIVITY 키.

    Returns:
        예: 「보통 · 1년 내내 보면 헛경보 약 20%(한 달 약 1.84%)」, 둔감은 끝에 「 · 미검증」.
    """
    m = SENSITIVITY[name]
    return (f"{name} · 1년 내내 보면 헛경보 약 {year_false_alarm(m) * 100:.0f}%(한 달 약 {m * 100:.3g}%)"
            + (" · 미검증" if name == "둔감" else ""))


def chosen_sensitivity(root: Path) -> str:
    """blackclip save가 --sensitivity 없이 돌 때 쓸 민감도: 고르는 화면에서 고른 값, 없거나 모르는 값이면 보통.

    Args:
        root: .blackclip 경로.

    Returns:
        SENSITIVITY 키.
    """
    try:
        value = section(load_connections(root), "defaults").get("sensitivity")
    except WorkspaceError:
        value = None
    return value if isinstance(value, str) and value in SENSITIVITY else DEFAULT_SENSITIVITY  # [] · {}는 dict 비교에서 TypeError


RETRO = "retro.md"
"""첫 회고를 남기는 자리: .blackclip/strategies/<전략>/retro.md(blackclip retro --out, Git 제외)."""

RETRO_HEAD = "# 실행 회고 · "
"""blackclip retro 결과 첫 줄의 앞부분(손으로 만든 빈 파일을 회고로 치지 않게)."""


def has_retro(folder: Path) -> bool:
    """전략 하나에 첫 회고가 있는지: retro.md(머리줄이 회고) 또는 blackclip daily가 만든 daily/*.md. 링크 · 일반 파일 아님은 치지 않는다.

    Args:
        folder: .blackclip/strategies/<전략>(링크 아닌 폴더, 이름 거르기는 부르는 쪽).

    Returns:
        있으면 True.
    """
    from blackclip.hook import _read_regular

    if (_read_regular(folder / RETRO, len(RETRO_HEAD.encode())) or b"").decode("utf-8", "replace") == RETRO_HEAD:
        return True
    daily = folder / "daily"
    return daily.is_dir() and not daily.is_symlink() and any(
        f.suffix == ".md" and f.is_file() and not f.is_symlink() for f in daily.iterdir())


def first_retro(root: Path) -> bool:
    """첫 회고를 봤는지: 어느 전략이든 has_retro가 참이면(실행 경고를 켜는 열쇠, 하나면 됨).

    실행 경고를 켤 수 있는지(고르는 화면)가 쓴다. 셋업의 「첫 회고」 단계도 같은 기준(연결된 전략 중 하나라도).

    Args:
        root: .blackclip 경로.

    Returns:
        봤으면 True.
    """
    from blackclip.hook import _NAME

    sdir = root / "strategies"
    if root.is_symlink() or sdir.is_symlink() or not sdir.is_dir():
        return False
    return any(_NAME.fullmatch(d.name) and d.is_dir() and not d.is_symlink() and has_retro(d) for d in sdir.iterdir())


def settings(root: Path) -> dict[str, Any]:
    """알림 설정을 읽는다(없는 칸은 기본값).

    Args:
        root: .blackclip 경로.

    Returns:
        quiet · healthcheck · execution_alerts · chat_id · bot.
    """
    t = section(load_connections(root), "telegram")
    return {**DEFAULTS, **{k: t[k] for k in DEFAULTS if k in t}, "chat_id": t.get("chat_id"), "bot": t.get("bot")}


def change_notify(root: Path, *, quiet: str | None = None, healthcheck: str | None = None, clear_healthcheck: bool = False,
                  execution_alerts: bool | None = None, sensitivity: str | None = None,
                  on_change: Callable[[], None] | None = None) -> None:
    """알림 설정을 바꾼다(검증 → 연결 잠금 안 갱신, 쓰기는 잠금 안 마지막 한 번). 대시보드 저장이 이것만 부르고, 잠금을 빠져나오다
    난 예외처럼 쓴 뒤일 수 있는 것은 `on_change`로 가른다.

    Args:
        root: .blackclip 경로.
        quiet: "매일" 또는 "바뀔때만"(None이면 그대로).
        healthcheck: https 주소(None이면 그대로).
        clear_healthcheck: 헬스체크 주소를 지운다.
        execution_alerts: 실행 경고 텔레그램 켜기 · 끄기(None이면 그대로).
        sensitivity: save 기본 민감도(None이면 그대로, connections.json "defaults" 칸).
        on_change: 잠금 안에서 바꾸기를 마친 순간(쓰기 직전) 부른다(대시보드가 「쓰기 단계」를 아는 데 씀). None이면 부르지 않음.

    Raises:
        WorkspaceError: 값이 틀렸거나, 첫 회고(first_retro) 전에 실행 경고를 켜려 할 때,
            connections.json의 telegram 칸(민감도를 저장하면 defaults 칸도)이 객체가 아닐 때. LockBusy(하위)도.
        OSError: 쓰지 못했을 때.
    """
    if sensitivity is not None and sensitivity not in SENSITIVITY:
        raise WorkspaceError(f"민감도: {' · '.join(SENSITIVITY)} 중 하나입니다")
    if quiet is not None and quiet not in QUIET:
        raise WorkspaceError(f"--quiet: {' · '.join(QUIET)} 중 하나입니다")
    if healthcheck is not None and not _HC.fullmatch(healthcheck):
        raise WorkspaceError("--healthcheck: https://로 시작하는 주소 하나만(공백 없이 300자까지)")
    if execution_alerts and not first_retro(root):
        raise WorkspaceError("--execution-alerts on: 첫 회고(blackclip retro --out .blackclip/strategies/<전략>/retro.md "
                             "또는 blackclip daily가 만든 daily 파일)를 본 뒤에 켤 수 있어요")
    def change(conns: dict) -> None:
        t = section(conns, "telegram")
        if quiet is not None:
            t["quiet"] = quiet
        if healthcheck is not None:
            t["healthcheck"] = healthcheck
        if clear_healthcheck:
            t.pop("healthcheck", None)
        if execution_alerts is not None:
            t["execution_alerts"] = execution_alerts
        if sensitivity is not None:
            section(conns, "defaults")["sensitivity"] = sensitivity
        if on_change is not None:
            on_change()

    update_connections(root, change)


def set_notify(root: Path, *, quiet: str | None = None, healthcheck: str | None = None, clear_healthcheck: bool = False,
               execution_alerts: bool | None = None, sensitivity: str | None = None) -> list[str]:
    """알림 설정을 바꾸고(`change_notify`) 결과 줄을 돌려준다(터미널 `blackclip notify`).

    Args:
        root: .blackclip 경로.
        quiet · healthcheck · clear_healthcheck · execution_alerts · sensitivity: `change_notify`와 같음.

    Returns:
        화면 줄(바꾼 뒤 다시 읽은 `status_lines`).

    Raises:
        WorkspaceError · OSError: `change_notify`와 같고, 다시 읽기(`status_lines`)가 실패할 때도.
    """
    change_notify(root, quiet=quiet, healthcheck=healthcheck, clear_healthcheck=clear_healthcheck,
                  execution_alerts=execution_alerts, sensitivity=sensitivity)
    return status_lines(root)


def status_lines(root: Path, store: Any | None = None) -> list[str]:
    """알림 상태 줄(AI가 읽는 출력, 색 없음, 토큰 · 주소 값 없음).

    Args:
        root: .blackclip 경로.
        store: 키 보관(주면 토큰 끝 4자리를 보이고, 「대화 연결 전」 다음 걸음을 토큰 유무로 가른다. 없으면 토큰 유무를 모르는 것으로 본다).

    Returns:
        화면 줄.
    """
    s = settings(root)
    held, has_token = "", False
    if store is not None:
        try:
            creds = store.load(TELEGRAM_ACCOUNT)
            has_token = bool(creds)
            held = f" · 토큰 {'있음(' + store.name + ' · 끝 ' + creds.last4() + ')' if creds else '없음'}"
        except KeystoreError as exc:
            held = f" · 토큰 읽기 실패({exc})"
    step = "connect --telegram" if has_token else "connect --open --telegram"  # 토큰이 있다고 확인될 때만 대화 잇기만, 없거나 모르면 토큰 넣는 화면부터
    linked = f"대화 연결됨({s['bot'] or '봇'})" if s["chat_id"] is not None else f"대화 연결 전 · blackclip {step}"
    lines = [f"텔레그램: {linked}{held}",
             f"조용한 날: {'매일 한 줄' if s['quiet'] == '매일' else '바뀔 때만'} · 상태가 바뀌면 따로 · 매주 회고 한 줄",
             f"헬스체크: {'주소 있음' if s['healthcheck'] else '없음'}",
             "실행 경고 텔레그램: " + ("켜짐" if s["execution_alerts"] else "꺼짐(첫 회고를 본 뒤 blackclip notify --execution-alerts on)"),
             f"기본 민감도: {sensitivity_text(chosen_sensitivity(root))} · blackclip save가 --sensitivity 없을 때"]
    if s["quiet"] == "바뀔때만" and not s["healthcheck"]:
        lines.append(NO_HEALTHCHECK)
    return lines


class _TokenChanged(Exception):
    """대화를 잇는 도중 보관된 텔레그램 토큰이 바뀌었거나 지워졌을 때(연결 상태를 쓰지 않고 멈추는 신호)."""


def link_telegram(root: Path, store: Any, make_bot: Callable[[str], Bot], out: Callable[[str], None], now: datetime,
                  wait_s: float = LINK_WAIT_S, code: str | None = None,
                  clock: Callable[[], float] | None = None) -> int:
    """저장된 토큰으로 대화를 잇는다: 1회용 숫자를 띄우고, 고객이 봇에게 보내면 대화 번호를 적고 확인 글을 보낸다.

    Args:
        root: .blackclip 경로.
        store: 키 보관.
        make_bot: 토큰 → Bot(시험에서 가짜 transport).
        out: 안내 줄을 내보낼 곳.
        now: 지금(UTC).
        wait_s: 기다릴 초.
        code: 1회용 숫자(시험에서 고정, 없으면 6자리 난수).
        clock: 단조 시계(시험).

    Returns:
        종료 코드(0 이음, 1 시간 지남, 2 토큰 없음 · 텔레그램 오류 · 잇는 도중 보관 토큰이 바뀜).
    """
    creds = store.load(TELEGRAM_ACCOUNT)
    if creds is None:
        out("텔레그램 토큰이 없습니다 · blackclip connect --open --telegram")
        return 2
    try:
        bot = make_bot(creds.api_key)
        name = "@" + str(bot.get_me().get("username", "봇"))
        code = code or f"{secrets.randbelow(10**6):06d}"
        start = skip_backlog(bot)  # 숫자를 띄우기 전에 밀린 글을 잊게 함: 그 앞 글로 잇지 않음(맥 시계와 무관)
        out(f"텔레그램에서 {name}에게 이 숫자를 보내 주세요: {code} ({int(wait_s // 60)}분 안)")
        chat = bind_chat(bot, code, wait_s, clock or time.monotonic, start)
        if chat is None:
            out("시간 안에 숫자가 오지 않아 대화를 잇지 못했어요 · blackclip connect --telegram으로 다시")
            return 1
        def bind(conns: dict) -> None:
            # 잠금 안에서 보관 토큰을 다시 읽는다(잠금 없는 load, 순서 연결 → 보관): 연결 화면 POST는 같은 잠금 안에서 대화 지우기 → 보관 차례
            held = store.load(TELEGRAM_ACCOUNT)
            if held is None or not secrets.compare_digest(held.api_key.encode(), creds.api_key.encode()):
                raise _TokenChanged
            section(conns, "telegram").update(chat_id=chat, bot=name, linked_at=now.isoformat(timespec="seconds"))

        try:
            update_connections(root, bind)
        except _TokenChanged:
            out("텔레그램 토큰이 바뀌어 대화를 잇지 않았어요 · blackclip connect --telegram으로 다시")
            return 2
        bot.send(chat, LINKED_TEXT)
    except TelegramError as exc:
        out(f"텔레그램: {exc}")
        return 2
    out(f"연결됨 · 텔레그램 {name} · 확인 글을 보냈어요")
    return 0


def snapshot(client: Any, market: str, symbols: list[str], quote: str) -> tuple[Decimal, dict[str, Decimal]]:
    """조회 키로 지금 잔고 · 포지션을 받는다(서명 GET만 · 주문 경로 없음).

    현물 `GET /api/v3/account`(가중치 20)의 free + locked. 선물 `GET /fapi/v2/positionRisk`(가중치 5)의 positionAmt를
    종목마다 더하고(양방향 모드 LONG · SHORT 합), `GET /fapi/v2/balance`(가중치 5)의 지갑 잔고에서 Σ 포지션 × 진입가를 뺀
    값을 현금으로 둔다(그래야 현물과 같은 식 「현금 + 포지션 × 종가」가 지갑 + 미실현 손익이 된다).

    Args:
        client: 그 시장의 서명 클라이언트(get).
        market: "spot" 또는 "futures".
        symbols: 가드레일 종목.
        quote: 거래 통화.

    Returns:
        (현금, 종목 → 포지션 수량).

    Raises:
        WorkspaceError: 종목이 거래 통화로 끝나지 않을 때.
        BinanceError · OSError · ValueError · KeyError · TypeError · InvalidOperation: 받기 실패 · 모양이 다른 응답.
    """
    odd = [s for s in symbols if not s.endswith(quote)]
    if odd:
        raise WorkspaceError(f"거래 통화 {quote}로 끝나지 않는 종목이 있어 잔고를 셀 수 없음({', '.join(odd[:3])})")
    if market == "spot":
        bal = {b["asset"]: Decimal(b["free"]) + Decimal(b["locked"])
               for b in client.get("/api/v3/account", {"omitZeroBalances": "true"})["balances"]}
        return bal.get(quote, Decimal(0)), {s: bal.get(s[: -len(quote)], Decimal(0)) for s in symbols}
    pos = {s: Decimal(0) for s in symbols}
    entry = Decimal(0)
    for row in client.get("/fapi/v2/positionRisk"):
        if row["symbol"] in pos:
            amt = Decimal(row["positionAmt"])
            pos[row["symbol"]] += amt
            entry += amt * Decimal(row["entryPrice"])
    wallet = sum((Decimal(b["balance"]) for b in client.get("/fapi/v2/balance") if b["asset"] == quote), Decimal(0))
    return wallet - entry, pos


def write_live(root: Path, name: str, account: str, quote: str, taken_at: datetime, cash: Decimal,
               positions: dict[str, Decimal], capital: Decimal | None = None) -> Path:
    """실전 시작 잔고 · 포지션을 적는다(Git 제외 파일). 값 매기기는 그날 UTC 종가가 난 뒤 daily가 한다.

    출발 시각 = 받은 날(실전 시작일)의 UTC 마감(다음 날 0시). 다시 적으면 그날부터 새로 센다(기준 자본도 다시 줘야 함).

    Args:
        root: .blackclip 경로.
        name: 전략 이름.
        account: 계좌 이름(fetch --account와 같음).
        quote: 거래 통화.
        taken_at: 잔고를 받은 시각(UTC).
        cash: snapshot의 현금.
        positions: snapshot의 종목 → 수량.
        capital: 기준 자본(실제 손익 %의 분모, 거래 통화). 주면 "capital"로 적고, 없으면 daily가
            현금 + 출발 포지션 평가를 분모로 쓴다. 잔고 · 포지션은 이 값과 상관없이 그대로 적는다.

    Returns:
        쓴 파일.

    Raises:
        WorkspaceError: 거래 통화 이름이 틀렸거나 가드레일이 없거나 기준 자본이 0 이하일 때.
    """
    if not versions(root, name):
        raise WorkspaceError(f"{name}: 가드레일이 없습니다 · blackclip save {name} 뒤에")
    quote = quote.strip().upper()
    if not re.fullmatch(r"[A-Z0-9]{2,10}", quote):
        raise WorkspaceError(f"--quote: 거래 통화 이름으로(예: USDT)({quote})")
    if capital is not None and not _capital_ok(capital):
        raise WorkspaceError(CAPITAL_TEXT)
    t0 = taken_at.astimezone(timezone.utc)
    start = t0.replace(hour=0, minute=0, second=0, microsecond=0) + timedelta(days=1)
    doc = {"account": account_key(account, False), "quote": quote, "taken_at": t0.isoformat(timespec="seconds"),
           "start": start.isoformat(), "cash": str(cash), "positions": {s: str(q) for s, q in positions.items()}}
    if capital is not None:
        doc["capital"] = str(capital)
    rel = f"strategies/{name}/live.json"
    write_in(root, rel, json.dumps(doc, ensure_ascii=False, indent=2, sort_keys=True) + "\n")
    return root / rel


def read_live(root: Path, name: str) -> dict[str, Any] | None:
    """실전 시작 잔고 파일을 읽는다.

    Args:
        root: .blackclip 경로.
        name: 전략 이름.

    Returns:
        {"account", "quote", "taken_at", "start"(datetime), "cash"(Decimal), "positions"({종목: Decimal}),
        "capital"(Decimal 또는 None: `blackclip live --capital`로 정한 기준 자본)},
        없거나 깨졌으면(손으로 고쳐 capital이 0 이하가 된 것 포함) None(daily는 「점검 불가(실전 시작 잔고 모름)」).
    """
    try:  # 상위 폴더 링크 · 링크 · 일반 파일 아님 · 1MiB 넘음은 읽지 않고 모름(read_bytes가 WorkspaceError)
        d = json.loads(read_bytes(root, f"strategies/{name}/live.json", 1 << 20).decode("utf-8"))
        capital = Decimal(d["capital"]) if d.get("capital") is not None else None
        if capital is not None and not _capital_ok(capital):
            return None
        return {"account": str(d["account"]), "quote": str(d["quote"]),
                "taken_at": datetime.fromisoformat(d["taken_at"]), "start": datetime.fromisoformat(d["start"]),
                "cash": Decimal(d["cash"]), "positions": {str(s): Decimal(q) for s, q in d["positions"].items()},
                "capital": capital}
    except (OSError, ValueError, KeyError, TypeError, AttributeError, InvalidOperation, RecursionError):  # 깊은 중첩 JSON도 모름
        return None


def ping(url: str | None, opener: Callable[[str], Any] | None = None) -> str | None:
    """헬스체크 주소에 본문 없는 GET 한 번.

    Args:
        url: https 주소(없으면 아무것도 안 함).
        opener: 주소 → 응답(시험에서 가짜). 기본은 urllib 10초.

    Returns:
        실패하면 한 줄(주소 값 없음), 아니면 None.
    """
    if not url:
        return None
    try:
        if opener is not None:
            opener(url)
        else:
            with urllib.request.urlopen(urllib.request.Request(url, method="GET"), timeout=10):
                pass
    except (OSError, ValueError) as exc:
        return f"헬스체크 신호를 못 보냄({type(exc).__name__})"
    return None

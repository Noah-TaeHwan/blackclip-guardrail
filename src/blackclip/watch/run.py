"""blackclip watch: 전략별 계좌인 전략을 1시간마다 — 체결 받기 → 잔고 · 지금 가격 → 구간 수익 · 1시간 선 → 봇 세 신호 →
watch.json → 상태가 바뀌면 텔레그램.

전략 하나가 실패해도 다른 전략은 돈다. 상태 파일은 텔레그램을 보내기 전에 쓴다. 매일 대조 숫자와 섞지 않는다.
확인 불가는 두 번 연속일 때만 텔레그램으로 보낸다(받기 한 번 실패로 두 통이 가지 않게).
단, 마지막으로 보낸 글에서 바뀐 칸 가운데 확인 불가가 아닌 것이 있으면(선 경고 등) 1회째여도 바로 보낸다.
또 상태 파일이 깨진 확인의 확인 불가는 1회째여도 바로 보낸다(깨진 파일은 한 번뿐이라 기다리면 영영 안 나감).
"""
from __future__ import annotations

import http.client
from collections.abc import Callable
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path
from typing import Any

from blackclip.binance.client import BinanceError
from blackclip.binance.klines import PATH
from blackclip.check.link import ExpressError, LinkError
from blackclip.check.runner import SignalError
from blackclip.daily.actual import actual_pnl
from blackclip.daily.messages import load_notified, save_notified
from blackclip.daily.reconcile import base, bot_cause, period, representative
from blackclip.daily.run import EXIT_INPUT, EXIT_PARTIAL, EXIT_TELEGRAM, _guards, _lost_accounts, _skipped_line
from blackclip.fetch import ClientFactory, run_fetch
from blackclip.fillstore import FUNDING_KEY, coverage_span, read_coverage
from blackclip.keystore import KeystoreError, account_name
from blackclip.notify import TELEGRAM_ACCOUNT, read_live, settings, snapshot
from blackclip.record import stop_label, stopped
from blackclip.retro.fills import normalize_symbol, read_executions, read_funding
from blackclip.telegram import Bot, TelegramError
from blackclip.watch.judge import WATCHED, bot_state, line_state, plan_watch, terminal_line
from blackclip.watch.line import hourly_lines
from blackclip.watch.position import bt_target, compare, held
from blackclip.watch.state import pick, read_state, write_state
from blackclip.watch.window import Mark, advance
from blackclip.workspace import WorkspaceError, lock

UTC = timezone.utc

STRATEGY_ERRORS = (OSError, ValueError, KeyError, TypeError, ArithmeticError, IndexError, BinanceError, KeystoreError,
                   http.client.HTTPException)
"""전략 하나를 보다 나는 오류: 그 전략만 확인 불가로 두고 다른 전략은 돈다(daily와 같음)."""


def now_prices(client: Any, symbols: list[str]) -> dict[str, Decimal]:
    """지금 가격: 종목마다 최근 1분 봉 하나(진행 중 봉)의 종가. 한 번의 확인은 이 출처 하나만 쓴다.

    Args:
        client: 공개 봉 클라이언트(market · get).
        symbols: 종목.

    Returns:
        종목 → 가격.

    Raises:
        BinanceError · OSError · IndexError · TypeError · ArithmeticError: 받기 실패 · 모양이 다른 응답.
    """
    return {s: Decimal(str(client.get(PATH[client.market], {"symbol": s, "interval": "1m", "limit": 1}, signed=False)[-1][4]))
            for s in symbols}


def _mark(store: Any, make_client: ClientFactory, kline_client: Callable[[str], Any], g: dict, cfg: dict,
          now: datetime) -> Mark:
    """잔고 · 지금 가격을 받아 이번 확인의 Mark로 만든다. 체결 받기보다 먼저 불러야 한다.

    잔고에 이미 든 체결은 받기 끝(now)보다 앞에 있어야 한다. 받기 뒤에 잔고를 읽으면 그 사이 체결이 잔고에는 있고 이번 단계
    체결에는 없어, 다음 단계가 같은 체결을 보유 손익과 체결 손익으로 두 번 센다.

    Args:
        store: 키 보관.
        make_client: (시장, 테스트넷, 키) → 서명 클라이언트.
        kline_client: 시장 → 공개 봉 클라이언트.
        g: 가드레일(최신 판).
        cfg: read_live 결과(--capital 없음).
        now: 지금(UTC, 초 단위).

    Returns:
        Mark(now, 현금, 보유, 지금 가격).

    Raises:
        WorkspaceError: 키가 없을 때.
        STRATEGY_ERRORS: 받기 실패 · 모양이 다른 응답.
    """
    t = g["target"]
    market, symbols = t["market"], list(t["symbols"])
    creds = store.load(account_name("binance", cfg["account"]))
    if creds is None:
        raise WorkspaceError(f"키가 없습니다(binance {cfg['account']}) · blackclip connect --open")
    cash, hold = snapshot(make_client(market, False, creds), market, symbols, cfg["quote"])
    return Mark(now, cash, hold, now_prices(kline_client(market), symbols))


def _one(root: Path, name: str, g: dict, cfg: dict, mark: Mark | Exception, kline_client: Callable[[str], Any],
         now: datetime, paths: int | None) -> tuple[dict[str, str], dict[str, str]]:
    """전략 하나를 한 번 본다.

    Args:
        root: .blackclip 경로.
        name: 전략 이름.
        g: 가드레일(최신 판).
        cfg: read_live 결과(--capital 없음).
        mark: 체결 받기 전에 받은 잔고 · 가격(_mark), 받지 못했으면 그 오류(그대로 다시 낸다).
        kline_client: 시장 → 공개 봉 클라이언트.
        now: 지금(UTC, 초 단위).
        paths: 1시간 선 흉내 경로 수(시험).

    Returns:
        (칸 → 상태, 칸 → 근거).

    Raises:
        STRATEGY_ERRORS: mark가 오류일 때 그 오류 · 받아 둔 체결 파일 · 상태 파일을 읽다 난 오류.
    """
    if isinstance(mark, Exception):
        raise mark
    t = g["target"]
    market, symbols, bar_s, quote = t["market"], list(t["symbols"]), int(t["bar_seconds"]), cfg["quote"]
    doc, broken = read_state(root, name)
    if broken:  # 직전을 모르니 직전도 확인 불가였던 것으로 둔다: 이번 확인 불가는 두 번 연속을 기다리지 않고 바로 간다
        doc["unconfirmed"] = True
    lines_by, doc["line"] = hourly_lines(g, doc.get("line"), paths)
    sens = g["warning_line"]["sensitivity"]
    d = root / "fills" / f"binance-{cfg['account']}" / market
    raw = read_coverage(d)
    cov = {normalize_symbol(k): v for k, v in raw.items() if not k.startswith("_")}
    missing = [s for s in symbols if s not in cov]
    span = coverage_span(cov, symbols)
    cov_from, cov_until = span if span else (None, None)
    fills, orders = [], []
    if (d / "fills.csv").exists():  # 단계 손익은 체결 하나하나(확인 경계를 넘는 부분 체결), 왕복 거래 세기는 매일 대조와 같은 주문 묶음
        fills = [e for e in read_executions(d / "fills.csv", per_fill=True) if e.symbol in symbols]
        orders = [e for e in read_executions(d / "fills.csv") if e.symbol in symbols]
    funding = [f for f in read_funding(d / "funding.csv") if f.symbol in symbols] if (d / "funding.csv").exists() else []
    prev_at = datetime.fromisoformat(doc["window"]["mark"]["at"]) if doc.get("window") else now
    fc = raw.get(FUNDING_KEY)
    covered = (span is not None and cov_from <= prev_at and cov_until >= now
               and (market != "futures" or (fc is not None and datetime.fromisoformat(fc["from"]) <= prev_at
                                            and datetime.fromisoformat(fc["until"]) >= now)))
    day_end = now.replace(hour=0, minute=0, second=0) + timedelta(days=1)  # period는 끝이 0시라고 본다: 오늘 구간
    pstart, _ = period(cfg["start"], day_end)
    doc["window"], note = advance(doc.get("window"), pstart, mark, (fills, funding) if covered else None, quote)
    st_line = line_state(doc["window"], note, broken, lines_by[sens], sens)
    last = max((e.time for e in fills), default=None)
    got = (f"체결 {cov_until:%m/%d %H:%M} UTC까지 받음 · "
           + (f"마지막 체결 {last.astimezone(UTC):%m/%d %H:%M} UTC" if last else "받은 체결 없음")) if cov_until else None
    exits = actual_pnl(orders, [], {}, cfg, [], now, market == "spot").exits  # 매일 대조와 같은 왕복 거래 세기
    cause = bot_cause(cov_from, cov_until, missing, now, cfg["taken_at"], exits, g["trade_frequency"],
                      (now - cfg["start"]).days, got)
    try:
        weights, changed, doc["target"] = bt_target(root, name, g, cfg["start"], kline_client, now, doc.get("target"))
        pos = compare(weights, changed, held(mark), now, bar_s, market == "futures")
    except (LinkError, ExpressError, SignalError) as exc:  # 원문에 고객 stderr · 경로가 들 수 있어 종류만
        pos = ("확인 불가", f"BT 목표를 못 냄(신호 함수 · {type(exc).__name__})")
    except (BinanceError, OSError, ValueError, KeyError, TypeError, AttributeError,
            http.client.HTTPException) as exc:  # TypeError · AttributeError: 캐시 칸이 손에 깨져 모양이 다를 때도 포지션 대조만 확인 불가
        doc.pop("target", None)  # 깨진 캐시를 남기면 매 시간 같은 오류: 버리면 다음 확인이 BT 목표를 다시 셈
        pos = ("확인 불가", f"BT 목표를 못 냄(바이낸스 봉 · {type(exc).__name__})")
    st_bot = bot_state(cause, pos)
    write_state(root, name, doc)
    return {"선": st_line[0], "봇": st_bot[0]}, {"선": st_line[1], "봇": st_bot[1]}


def _skip_line(skipped: list[tuple[str, str]]) -> str:
    """자주 보는 시계에서 건너뛴 전략들의 요약 줄.

    Args:
        skipped: (전략 이름, 까닭) 목록(비지 않음).

    Returns:
        한 줄.
    """
    return f"자주 보는 시계 건너뜀 {len(skipped)}개: " + " · ".join(f"{n}({w})" for n, w in skipped)


def run_watch(root: Path, store: Any, make_client: ClientFactory, kline_client: Callable[[str], Any],
              make_bot: Callable[[str], Bot], now: datetime, paths: int | None = None) -> tuple[int, list[str]]:
    """자주 보는 시계를 한 번 돌린다.

    Args:
        root: .blackclip 경로.
        store: 키 보관(바이낸스 조회 키 · 텔레그램 토큰).
        make_client: (시장, 테스트넷, 키) → 서명 클라이언트.
        kline_client: 시장 → 공개 봉 클라이언트.
        make_bot: 토큰 → Bot.
        now: 지금(UTC).
        paths: 1시간 선 흉내 경로 수(시험, 없으면 20,000).

    Returns:
        (종료 코드 0 · 2 · 4 · 5(daily와 같은 규칙), 화면 줄). 가드레일을 읽지 못한 전략은 그 전략만 확인 불가 줄로 두고(종료 4)
        나머지는 평소대로 돈다.
    """
    guards, broken = _guards(root)
    off = {n: v for n, v in stopped(root).items() if n in guards or n in broken}
    bad = {n: why for n, why in broken.items() if n not in off}  # 끈 전략은 가드레일을 못 읽어도 「감시 안 함」 하나만, 종료 코드도 올리지 않음
    code = EXIT_PARTIAL if bad else 0
    lines = [_skipped_line(n, why) for n, why in bad.items()]
    if not guards:  # 깨진 전략뿐이면 그 줄들만, 저장된 전략이 아예 없으면 안내(daily와 같음)
        if off:
            lines.append(_skip_line([(n, f"{stop_label(*v)} · 감시 안 함") for n, v in sorted(off.items())]))
        return (EXIT_INPUT if bad or not lines else code), lines or ["가드레일이 저장된 전략이 없습니다 · blackclip save <전략> 뒤 blackclip live <전략>"]
    now = now.astimezone(UTC).replace(microsecond=0)  # 받은 범위 끝(ms)과 같은 시각으로 비교
    cfgs = {n: read_live(root, n) for n in guards}
    watched, skipped, mixed = pick(guards, cfgs, _lost_accounts(root, guards))  # 끈 전략도 넣어 남은 전략의 섞인 계좌 판정에 쓴다
    watched = [n for n in watched if n not in off]
    skipped = [(n, w) for n, w in skipped if n not in off] + [(n, f"{stop_label(*v)} · 감시 안 함") for n, v in sorted(off.items())]
    mixed = {k: left for k, v in mixed.items() if (left := [n for n in v if n not in off])}
    ready = [n for n in watched if now >= cfgs[n]["start"]]
    skipped += [(n, f"실전 시작 전({cfgs[n]['start']:%Y-%m-%d} UTC부터)") for n in watched if n not in ready]
    results = []
    groups: dict[tuple[str, str], list[str]] = {}
    for n in ready:
        groups.setdefault((cfgs[n]["account"], guards[n]["target"]["market"]), []).append(n)
    marks: dict[str, Mark | Exception] = {}
    for n in ready:  # 잔고 · 지금 가격은 체결 받기보다 먼저(전략마다 한 번씩 모두): 순서가 바뀌면 받기 뒤 체결을 두 번 센다
        try:
            marks[n] = _mark(store, make_client, kline_client, guards[n], cfgs[n], now)
        except STRATEGY_ERRORS as exc:
            marks[n] = exc
    with lock(root, "fetch"):
        for (acct, market), names in sorted(groups.items()):
            try:
                c, got = run_fetch(root, store, make_client, label=acct, testnet=False, markets=[market],
                                   symbols=sorted({s for n in names for s in guards[n]["target"]["symbols"]}),
                                   since=min(cfgs[n]["taken_at"] for n in names), now=now)
            except (KeystoreError, WorkspaceError, BinanceError, OSError, ValueError) as exc:
                c, got = EXIT_PARTIAL, [f"binance {acct} {market}: 받지 못함({type(exc).__name__}) · 키 · 연결 확인"]
            lines += got
            if c:
                code = max(code, EXIT_PARTIAL)
    for n in ready:
        try:
            states, why = _one(root, n, guards[n], cfgs[n], marks[n], kline_client, now, paths)
        except STRATEGY_ERRORS as exc:  # 전략 하나의 실패를 결과로
            states = {k: f"확인 불가(확인하지 못함 · {type(exc).__name__})" for k in WATCHED}
            why = {k: "blackclip watch 출력 · 연결 확인" for k in WATCHED}
        if any(base(s) == "확인 불가" for s in states.values()):
            code = max(code, EXIT_PARTIAL)
        results.append((n, states, why, dict(guards[n].get("response") or {})))
        lines.append(terminal_line(n, states, why))
    if skipped:
        lines.append(_skip_line(skipped))
    notified = load_notified(root)
    first = []  # 확인 불가 1회째: 보내지 않고 notified도 그대로, 직전 확인이 확인 불가였는지는 watch.json "unconfirmed"
    for n, states, _, _ in results:
        doc, broken = read_state(root, n)
        unconfirmed = base(representative(states)[0]) == "확인 불가"
        sent = notified.get(f"watch:{n}") or {}
        # 마지막으로 보낸 글과 다른 칸: 모두 확인 불가일 때만 기다림(선 경고 등이 같이 바뀌면 바로). 보낸 적 없는 칸의 정상은 바뀜이 아님
        changed = [v for k, v in states.items() if sent.get(k) != v and not (k not in sent and base(v) == "정상")]
        if changed and all(base(v) == "확인 불가" for v in changed) and not broken and not doc.get("unconfirmed"):
            first.append(n)
        if not broken:  # 못 읽는 파일은 직전을 모르므로 바로 보내고(_one이 새로 쓴 파일은 "unconfirmed"가 이미 True), 덮어쓰지 않아 다음 _one이 「지난 손익을 잃음」을 말하게 둔다
            write_state(root, n, {**doc, "unconfirmed": unconfirmed})
    msgs = plan_watch([r for r in results if r[0] not in first], mixed, notified)
    s = settings(root)
    if first:
        lines.append(f"텔레그램: 확인 불가 1회째 · 다음 확인도 그러면 보냄({', '.join(first)})")
    if not msgs:
        if not first:
            lines.append("텔레그램: 바뀐 상태 없음 · 안 보냄")
    elif s["chat_id"] is None:
        lines.append("텔레그램: 대화 연결 전이라 안 보냄 · blackclip connect --telegram")
    else:
        try:
            creds = store.load(TELEGRAM_ACCOUNT)
            if creds is None:
                raise TelegramError("텔레그램 토큰이 없습니다 · blackclip connect --open --telegram")
            bot = make_bot(creds.api_key)
            for text, keys in msgs:
                bot.send(s["chat_id"], text)
                notified = load_notified(root)  # 보낸 것만 적는다, 매일 대조 키는 그대로
                notified.update(keys)
                save_notified(root, notified)
            lines.append("텔레그램: 보냄")
        except (TelegramError, KeystoreError, OSError, http.client.HTTPException) as exc:
            code = max(code, EXIT_TELEGRAM)
            lines.append(f"텔레그램: 못 보냄({exc if isinstance(exc, TelegramError) else type(exc).__name__}) · "
                         "상태 파일은 남음 · 바뀐 상태는 다음 확인에서 다시 보냄")
    return code, lines

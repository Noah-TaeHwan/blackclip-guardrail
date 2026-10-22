"""blackclip daily: 받기 → 같은 기간 백테스트 → 실제 손익 → 판정 → daily 파일 · state.log → 텔레그램 → 헬스체크.

전략 하나가 실패해도 다른 전략은 돈다. 파일은 텔레그램을 보내기 전에 쓴다.
"""
from __future__ import annotations

import hashlib
import http.client
import json
from collections.abc import Callable
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from functools import partial
from pathlib import Path
from typing import Any

import pandas as pd

from blackclip.binance.client import BinanceError
from blackclip.check.link import ExpressError, LinkError
from blackclip.check.runner import Runner, SignalError
from blackclip.daily.actual import actual_pnl
from blackclip.daily.days import append_day
from blackclip.daily.live import binance_frames, live_run
from blackclip.daily.messages import load_notified, plan_messages, save_notified
from blackclip.daily.reconcile import (MIXED_LOST, MIXED_OVERLAP, TRACKED, Day, base, capital_basis, period, reconcile,
                                       render_md, representative, sideways_days)
from blackclip.fetch import ClientFactory, run_fetch
from blackclip.fillstore import FUNDING_KEY, coverage_span, read_coverage
from blackclip.hook import _NAME
from blackclip.keystore import KeystoreError
from blackclip.notify import TELEGRAM_ACCOUNT, ping, read_live, settings
from blackclip.record import BROKEN, stop_label, stopped
from blackclip.retro.fills import FillsError, normalize_symbol, read_executions, read_funding
from blackclip.retro.report import build_retro
from blackclip.scan import show
from blackclip.states import states_ok
from blackclip.telegram import Bot, TelegramError
from blackclip.workspace import (WorkspaceError, append_line, lock, read_bytes, read_guardrail, read_jsonl,
                                 strategy_names, write_in)

EXIT_INPUT, EXIT_PARTIAL, EXIT_TELEGRAM = 2, 4, 5
"""종료 코드: 돌릴 수 없음 · 일부 확인 불가 · 텔레그램 못 보냄."""

UTC = timezone.utc


def _guards(root: Path) -> tuple[dict[str, dict], dict[str, str]]:
    """가드레일을 저장한 전략의 최신 판을 읽는다(링크를 따르지 않고 안쪽 모양까지 확인).

    한 전략의 가드레일을 읽지 못하면 그 전략만 사유와 함께 따로 담고 나머지는 계속 읽는다(link.py 읽기 실패와 같은 규칙).

    Args:
        root: .blackclip 경로.

    Returns:
        (전략 이름 → 가드레일 내용, 읽지 못한 전략 이름 → 사유). 사유는 WorkspaceError이면 그 메시지, OSError이면 종류 이름.

    Raises:
        WorkspaceError: strategies/가 링크이거나 폴더가 아닐 때(목록을 못 만드는 경우, 한 줄 · 종료 2로 받음).
    """
    out, broken = {}, {}
    for name in strategy_names(root):
        if not _NAME.fullmatch(name):
            continue
        try:
            got = read_guardrail(root, name)
        except (WorkspaceError, OSError) as exc:
            broken[name] = str(exc) if isinstance(exc, WorkspaceError) else type(exc).__name__
            continue
        if got is not None:
            out[name] = got[1]
    return out, broken


def _lost_accounts(root: Path, guards: dict[str, dict]) -> frozenset[str]:
    """가드레일을 잃은 전략의 live.json이 읽히면 그 계좌 이름들.

    「잃은 전략」은 strategies/에 이름 모양대로 있는데 읽은 가드레일(guards)에 없는 전략이다: 못 읽은 전략(_guards의 broken)뿐 아니라 판 파일이
    링크 · FIFO이거나 판을 지웠거나 전략 폴더가 링크라 읽기 오류 없이 목록에서 빠진 전략도 포함한다. live.json은 가드레일이 있어야 생기므로
    live.json이 읽히면 실전 전략이 가드레일만 잃은 것이다. 그 전략의 시장 · 종목은 가드레일 안이라 몰라서 같은 계좌의 다른 전략은
    잔고 · 체결이 섞였을 수 있다고 계좌 단위로 본다. live.json이 없거나 깨졌거나 링크 · FIFO면(read_live가 None) 계좌를 모르니 아무것도 하지 않는다.
    read_live는 예외를 내지 않는다.

    Args:
        root: .blackclip 경로.
        guards: _guards가 돌려준 읽은 전략 이름 → 가드레일.

    Returns:
        계좌 이름 집합.
    """
    return frozenset(cfg["account"] for n in strategy_names(root) if _NAME.fullmatch(n) and n not in guards
                     and (cfg := read_live(root, n)))


def _skipped_line(name: str, why: str) -> str:
    """가드레일을 읽지 못해 건너뛴 전략의 화면 줄(daily · watch 공용, 사유는 레포 글이 들 수 있어 scan.show를 거침).

    Args:
        name: 전략 이름.
        why: _guards가 돌려준 사유.

    Returns:
        한 줄.
    """
    return f"{name}: 확인 불가(가드레일을 읽지 못함 · {show(why)}) · 이 전략만 건너뜀"


def _stopped_line(name: str, state: str, day: str) -> str:
    """끈 전략(중지 · 은퇴 · 기록 손상)의 daily 화면 줄. 기록 손상이면 고치는 길까지.

    Args:
        name: 전략 이름.
        state: record.stopped가 돌려준 상태.
        day: 그 상태가 시작된 날(손상이면 "").

    Returns:
        한 줄.
    """
    if state == BROKEN:
        return (f"{name}: {BROKEN} · 전략 기록(record.md)을 읽지 못함 · 대조 멈춤 · "
                "git 충돌이면 풀고, 아니면 record.md를 지운 뒤 blackclip record로 다시")
    return f"{name}: {stop_label(state, day)} · 대조 안 함"


def _day0(text: str) -> datetime:
    """YYYY-MM-DD → 그날 0시 UTC.

    Args:
        text: 날짜.

    Returns:
        시각.
    """
    return datetime.fromisoformat(text).replace(tzinfo=UTC)


def _state_line(rec: Any) -> bool:
    """state.log 줄이 상태 줄 모양인지: 객체이고 states가 TRACKED 다섯 칸 모두 상태 글(days.jsonl과 같은 규칙 states_ok).

    논지 칸만 「해당 없음」도 받는다(논지가 없을 때 thesis_cause가 내는 정상 값, 대표 상태에서 빠짐).

    Args:
        rec: 줄 하나를 읽은 값.

    Returns:
        상태 줄이면 True.
    """
    return isinstance(rec, dict) and states_ok(rec.get("states"))


def _log_state(root: Path, day: Day) -> None:
    """상태가 바뀐 날만 .blackclip/state.log에 JSON 한 줄. 같은 전략의 마지막 상태 줄과 비교한다.

    Args:
        root: .blackclip 경로.
        day: 하루 결과.

    Raises:
        WorkspaceError: state.log가 링크 · FIFO · 장치이거나 16MiB를 넘을 때.
        OSError: 쓰지 못함.
    """
    last = None
    for rec in read_jsonl(root, "state.log", 16 << 20):
        if _state_line(rec) and rec.get("strategy") == day.strategy:
            last = rec["states"]
    if last != day.states:
        append_line(root, "state.log", json.dumps({"day": day.day.isoformat(), "strategy": day.strategy, "states": day.states},
                                                  ensure_ascii=False, sort_keys=True) + "\n")


def _one(root: Path, name: str, g: dict, cfg: dict | None, kline_client: Callable[[str], Any], end: datetime,
         mixed: str | None) -> Day:
    """전략 하나의 하루를 판정한다.

    Args:
        root: .blackclip 경로.
        name: 전략 이름.
        g: 가드레일.
        cfg: read_live 결과(없으면 None).
        kline_client: 시장 → 공개 봉 클라이언트.
        end: 기간 끝(오늘 0시 UTC).
        mixed: 섞인 기록이면 그 사유 글(reconcile.MIXED_OVERLAP · MIXED_LOST), 아니면 None.

    Returns:
        Day.
    """
    t = g["target"]
    bar_s, market, symbols = int(t["bar_seconds"]), t["market"], list(t["symbols"])
    live_start = cfg["start"] if cfg else _day0(g["live_start"])  # live.json이 있으면 그 출발이 실전 시작
    pstart, _ = period(live_start, end)
    # 링크 · FIFO는 거절(WorkspaceError → 이 전략만 확인 불가), 상한 1MiB는 scan과 같음
    link_changed = hashlib.sha256(read_bytes(root, f"strategies/{name}/link.py", 1 << 20)).hexdigest() != t["link_sha256"]
    live, why, side = None, None, None
    frames: dict = {}  # 받기가 실패해도 아래 실제 손익 쪽이 안 깨지게
    absent: list = []
    if bar_s > 86400:  # 매일 대조는 1일 이하 봉만
        why = "매일 대조는 1일 이하 봉만 · 점검(blackclip check)은 그대로"
    else:
        try:
            frames, absent = binance_frames(kline_client(market), symbols, bar_s, _day0(t["start"]), end)
            if absent:
                why = f"바이낸스 {market}에 봉 없음({', '.join(absent[:3])})"
            else:
                with Runner(root, name) as runner:
                    live = live_run(lambda f: runner.signals(frames=f), frames, t["exec"], bar_s, live_start, end)
                side = sideways_days(frames, bar_s)
        except (LinkError, ExpressError, SignalError) as exc:  # 원문에 고객 stderr · 경로가 들 수 있어 종류만
            why = f"신호 함수를 돌리지 못함({type(exc).__name__})"
        except (BinanceError, OSError, ValueError, KeyError, http.client.HTTPException) as exc:
            why = f"바이낸스 봉을 못 받음({type(exc).__name__})"
    actual = retro = retro_all = cov_until = cov_from = None
    missing: list[str] = []
    funding_ok = market != "futures"
    fund_total = Decimal(0)
    if cfg:
        d = root / "fills" / f"binance-{cfg['account']}" / market
        try:
            raw = read_coverage(d)
            cov = {normalize_symbol(k): v for k, v in raw.items() if not k.startswith("_")}
            missing = [s for s in symbols if s not in cov]
            span = coverage_span(cov, symbols)
            cov_from, cov_until = span if span else (None, None)
            fills, orders = [], []
            if (d / "fills.csv").exists():  # 손익은 체결 하나하나(마감을 넘는 부분 체결을 제 시각에), 왕복 세기 · BT 맞춰 보기는 주문 묶음
                fills = [e for e in read_executions(d / "fills.csv", per_fill=True) if e.symbol in symbols]
                orders = [e for e in read_executions(d / "fills.csv") if e.symbol in symbols]
            funding = ([f for f in read_funding(d / "funding.csv") if f.symbol in symbols]
                       if (d / "funding.csv").exists() else [])
            fc = raw.get(FUNDING_KEY)
            if market == "futures" and fc:
                funding_ok = datetime.fromisoformat(fc["from"]) <= pstart and datetime.fromisoformat(fc["until"]) >= end
        except (FillsError, OSError, ValueError, KeyError, TypeError) as exc:
            missing, cov_from, cov_until = [f"체결 파일을 읽을 수 없음({type(exc).__name__})"], None, None
            fills, orders, funding = [], [], []
        fund_total = sum((f.amount for f in funding if pstart <= f.time < end and f.asset == cfg["quote"]), Decimal(0))
        ends = [x.to_pydatetime() for x in pd.date_range(live_start, end, freq="D")]
        # 봉 없는 종목이 있으면 일부 종가로 손익을 내지 않는다(틀린 「기록 불완전」이 됨) · 신호 함수가 실패해도 봉이 있으면 실제 손익은 낸다
        closes = {s: f["Close"] for s, f in frames.items()} if frames and not absent else {}
        fee_syms = sorted({e.fee_asset + cfg["quote"] for e in fills if e.fee and e.fee_asset
                           and e.fee_asset != cfg["quote"] and e.symbol != e.fee_asset + cfg["quote"]})
        if closes and fee_syms:  # 다른 자산 수수료 환산용 현물 일봉(추정)
            try:
                got, _ = binance_frames(kline_client("spot"), fee_syms, 86400, cfg["taken_at"] - timedelta(days=1), end)
                closes.update({s: f["Close"] for s, f in got.items()})
            except (BinanceError, OSError, ValueError, http.client.HTTPException):
                pass  # 환산할 종가가 없으면 actual_pnl이 점검 불가 사유를 낸다
        rest = (funding, closes, cfg, ends if closes else [], end, market == "spot")
        actual = actual_pnl(fills, *rest)
        # 왕복은 주문 묶음으로 센다: 체결마다 세면 청산 주문 하나의 남은 몫이 새 진입으로 보여 가짜 왕복이 됨(백테스트 거래도 주문 단위).
        # 같은 종가를 넘겨야 다른 자산(BNB 등) 수수료가 거래 1건 수익에 듦
        trips = actual_pnl(orders, *rest)
        actual = replace(actual, exits=trips.exits, trips=trips.trips)
        if live:
            retro_of = partial(build_retro, live.executions, orders, funding, actual.capital or Decimal(1), end,
                               window=timedelta(seconds=bar_s * int(g.get("match_window_bars", 1))), quote=cfg["quote"],
                               fills_until=cov_until, fills_until_source="blackclip fetch", fills_from=cov_from)
            retro = retro_of(live_start=pstart, days=30)
            if pstart > live_start:  # 31일째부터: 실전 시작부터의 놓친 주문 · 잰 것을 nums.all에(첫 구간이면 구간 값 복사)
                # 기간 인자를 명시해야 한다(기본 30일이면 [끝 − 30일, 끝)로 잘려 앞 구간의 놓친 주문이 0건이 됨)
                retro_all = retro_of(live_start=live_start, days=(end - live_start).days + 1)
    return reconcile(name, g, end=end, live_start=live_start, live=live, actual=actual, retro=retro, cov_from=cov_from,
                     cov_until=cov_until, missing=missing, funding_ok=funding_ok, funding_total=fund_total, mixed=mixed,
                     link_changed=link_changed, side_days=side, need_from=cfg["taken_at"] if cfg else None, why=why,
                     permanent=bar_s > 86400, retro_all=retro_all)


def _telegram(root: Path, store: Any, make_bot: Callable[[str], Bot], days: list[Day], notes: list[str],
              broken: list[str], wrote: bool) -> tuple[int, list[str]]:
    """보낼 글(상태 바뀜 · 기록 손상 · 묶음)을 보내고 notified를 글마다 갱신한다. 보통 길과 유효한 가드레일 0개인 이른 반환 길이 함께 쓴다.

    알림 설정(connections.json)은 여기서 직접 읽는다. 못 읽으면 보낼 글이 있는 날은 못 보냄으로 다루고 화면 줄은 호출부가 그대로 둔다.

    Args:
        root: .blackclip 경로.
        store: 키 보관(텔레그램 토큰).
        make_bot: 토큰 → Bot.
        days: 오늘 대조가 돈 전략마다 결과(비어도 됨).
        notes: 묶음 끝에 붙일 줄.
        broken: 기록이 깨진 전략 이름(이름순).
        wrote: 이번 실행에서 daily md를 하나라도 썼는지.

    Returns:
        (올릴 종료 코드, 화면 줄). 보낼 것이 없으면 (0, []), 못 보내면 (5, 「못 보냄」 줄), 보낼 글이 0통이면 토큰을 보지 않고 (0, 「바뀐 상태 없음」 줄).
        고쳐진 전략의 `record:` 키는 chat_id가 있으면 늘 지운다.
    """
    tail = (" · daily 파일은 남음" if wrote else "") + " · 바뀐 상태는 다음 실행에서 다시 보냄"
    try:
        s = settings(root)
    except (WorkspaceError, OSError):  # 손으로 고친 connections.json이 깨졌거나 링크 · 읽기 실패(settings → load_connections · section)
        return (EXIT_TELEGRAM, ["텔레그램: 못 보냄(connections.json을 못 읽음)" + tail]) if days or broken else (0, [])
    if s["chat_id"] is None:  # 연결 전이라 보낸 적이 없으니 notified도 건드리지 않음
        return (0, ["텔레그램: 대화 연결 전이라 안 보냄 · blackclip connect --telegram"]) if days or broken else (0, [])
    try:
        notified = load_notified(root)
        msgs, stale = plan_messages(days, notified, s, notes, broken)
        if stale:  # 고쳐진 전략의 손상 키는 토큰 · 보내기 · 보낼 글 수와 상관없이 먼저 지워 다시 깨지면 다시 알림
            for k in stale:
                del notified[k]
            save_notified(root, notified)
        if not (days or broken):
            return 0, []
        if not msgs:  # 보낼 글이 없으면 토큰도 봇도 보지 않음(이미 알린 손상만 있는 날 토큰이 없어도 5가 아님)
            return 0, ["텔레그램: 바뀐 상태 없음 · 안 보냄"]
        creds = store.load(TELEGRAM_ACCOUNT)
        if creds is None:
            raise TelegramError("텔레그램 토큰이 없습니다 · blackclip connect --open --telegram")
        bot = make_bot(creds.api_key)
        for text, sent in msgs:
            bot.send(s["chat_id"], text)
            notified.update(sent)
            save_notified(root, notified)
        return 0, ["텔레그램: 보냄"]
    except (TelegramError, KeystoreError, OSError, http.client.HTTPException) as exc:  # 헬스체크까지 가게
        return EXIT_TELEGRAM, [f"텔레그램: 못 보냄({exc if isinstance(exc, TelegramError) else type(exc).__name__})" + tail]


def run_daily(root: Path, store: Any, make_client: ClientFactory, kline_client: Callable[[str], Any],
              make_bot: Callable[[str], Bot], now: datetime,
              ping_opener: Callable[[str], Any] | None = None) -> tuple[int, list[str]]:
    """하루 대조를 돌린다.

    Args:
        root: .blackclip 경로.
        store: 키 보관(바이낸스 조회 키 · 텔레그램 토큰).
        make_client: (시장, 테스트넷, 키) → 서명 클라이언트(체결 받기).
        kline_client: 시장 → 공개 봉 클라이언트.
        make_bot: 토큰 → Bot.
        now: 지금(UTC).
        ping_opener: 헬스체크 요청(시험에서 가짜).

    Returns:
        (종료 코드, 화면 줄). 가드레일을 읽지 못한 전략은 그 전략만 확인 불가 줄로 두고 나머지는 평소대로 돌며 종료 4,
        읽을 수 있는 가드레일이 하나도 없으면 그 줄들만 내고 종료 2(기록이 깨진 전략이 있으면 4).
        daily 파일을 쓰지 못한 전략도 그 전략만 줄을 내고 종료 4. 텔레그램을 못 보내면 5가 앞선다.
    """
    guards, broken = _guards(root)
    off = stopped(root)  # 끈 전략은 위 계좌 묶음 · 섞인 기록 판정에는 남고 대조 루프에서만 빠진다 · md · days.jsonl · 텔레그램 없음
    bad = {n: why for n, why in broken.items() if n not in off}  # 끈 전략은 가드레일을 못 읽어도 끈 전략 줄 하나만, 종료 코드도 올리지 않음
    code = EXIT_PARTIAL if bad else 0
    lines = [_skipped_line(n, why) for n, why in bad.items()]
    for n in broken:
        if n in off:
            lines.append(_stopped_line(n, *off[n]))
            code = max(code, EXIT_PARTIAL if off[n][0] == BROKEN else 0)  # 기록 손상 자체는 4
    for n, (state, _) in off.items():
        if state == BROKEN and n not in guards and n not in broken:  # 가드레일 없이 기록만 깨진 폴더도 화면에(텔레그램과 같게)
            lines.append(_stopped_line(n, state, ""))
            code = EXIT_PARTIAL
    notes = [f"가드레일을 읽지 못한 전략 {n} · 컴퓨터에서 blackclip daily 출력 확인" for n in bad]  # 경로 · 원문은 텔레그램에 싣지 않음
    rec_broken = sorted(n for n, (state, _) in off.items() if state == BROKEN)  # 기록이 깨진 전략, 이름순
    if not guards:  # 깨진 전략뿐이면 그 줄들만(체결 받기 · 헬스체크 없음, 텔레그램은 기록 손상 글만), 저장된 전략이 아예 없으면 안내
        if bad or not lines:  # 기록 손상이 있으면 max(2, 4) = 4를 지킴
            code = EXIT_PARTIAL if rec_broken else EXIT_INPUT
        if rec_broken or any(k.startswith("record:") for k in load_notified(root)):  # 고쳐진 전략의 키 지우기도 이 길을 거침
            add, extra = _telegram(root, store, make_bot, [], notes, rec_broken, False)
            code, lines = max(code, add), lines + extra
        return code, lines or ["가드레일이 저장된 전략이 없습니다 · blackclip save <전략> 뒤 blackclip live <전략>"]
    end = now.astimezone(UTC).replace(hour=0, minute=0, second=0, microsecond=0)
    cfgs = {n: read_live(root, n) for n in guards}
    groups: dict[tuple[str, str], set[str]] = {}
    users: dict[tuple[str, str], list[str]] = {}  # 체결 받기 묶음(계좌 · 시장)을 쓰는 전략들(끈 전략 판정용)
    owners: dict[tuple[str, str, str], list[str]] = {}
    for n, g in guards.items():
        if cfgs[n]:
            key = (cfgs[n]["account"], g["target"]["market"])
            groups.setdefault(key, set()).update(g["target"]["symbols"])
            users.setdefault(key, []).append(n)
            for s in g["target"]["symbols"]:
                owners.setdefault((*key, s), []).append(n)
    lost = _lost_accounts(root, guards)  # 가드레일을 잃은 전략과 같은 계좌의 정상 전략도 섞인 기록으로 본다
    mixed = {n: MIXED_LOST for n in guards if cfgs[n] and cfgs[n]["account"] in lost}
    mixed.update({n: MIXED_OVERLAP for names in owners.values() if len(names) > 1 for n in names})  # 종목이 실제로 겹치면 확인된 사실이 우선
    with lock(root, "fetch"):
        for (acct, market), syms in sorted(groups.items()):
            kinds = {off[n][0] if n in off else "실전" for n in users[(acct, market)]}
            if kinds == {"은퇴"}:  # 은퇴 전략만 쓰는 계좌는 받지 않는다 · 중지만이면 다시 켤 때 체결을 잃지 않게 받되 실패는 출력만
                continue
            quiet = "실전" not in kinds
            since = min(cfgs[n]["taken_at"] for n in guards if cfgs[n] and cfgs[n]["account"] == acct)
            try:
                c, got = run_fetch(root, store, make_client, label=acct, testnet=False, markets=[market],
                                   symbols=sorted(syms), since=since, now=now)
            except (KeystoreError, WorkspaceError, BinanceError, OSError, ValueError) as exc:
                c, got = EXIT_PARTIAL, [f"binance {acct} {market}: 받지 못함({type(exc).__name__}) · 키 · 연결 확인"]
            lines += got
            if c and not quiet:
                code = max(code, EXIT_PARTIAL)
                notes.append(f"체결 받기 일부 실패(binance {acct} {market}) · 컴퓨터에서 blackclip daily 출력 확인")
    days: list[Day] = []
    wrote = False  # 이번 실행에서 daily md를 하나라도 썼는지(텔레그램 실패 줄이 「파일은 남음」을 말할 수 있는지)
    for n, g in guards.items():
        if n in off:
            lines.append(_stopped_line(n, *off[n]))
            code = max(code, EXIT_PARTIAL if off[n][0] == BROKEN else 0)
            continue
        start = cfgs[n]["start"] if cfgs[n] else _day0(g["live_start"])
        if end <= start:
            lines.append(f"{n}: 실전 시작 전({start:%Y-%m-%d} UTC부터 대조) · 대조 안 함")
            continue
        try:
            day = _one(root, n, g, cfgs[n], kline_client, end, mixed.get(n))
        except (OSError, ValueError, KeyError, TypeError, ArithmeticError) as exc:  # 전략 하나의 실패를 결과로
            why = f"확인 불가(대조하지 못함 · {type(exc).__name__})"
            day = Day(n, (end - timedelta(days=1)).date(), (end - start).days, 0, "실제 모름 · 같은 기간 BT 모름",
                      {k: why for k in TRACKED}, {k: "blackclip daily 출력 · 연결 파일 확인" for k in TRACKED},
                      dict(g.get("response") or {}), None)
        kept = " · daily 파일은 남음"
        try:
            write_in(root, f"strategies/{n}/daily/{day.day.isoformat()}.md",
                     render_md(day, int(g.get("match_window_bars", 1)), capital_basis(cfgs[n])), make=True)
            saved, wrote = f"저장 .blackclip/strategies/{n}/daily/{day.day.isoformat()}.md", True
        except (OSError, ValueError) as exc:  # 상위 폴더 링크 · 폴더가 아님(WorkspaceError) · 디스크: 이웃 쓰기와 같은 규칙으로 이 전략만
            lines.append(f"{n}: daily 파일을 쓰지 못함({exc if isinstance(exc, WorkspaceError) else type(exc).__name__})")
            code, kept, saved = max(code, EXIT_PARTIAL), "", "daily 파일 못 씀"  # 주 산출물이 없다 · 아래 이웃 줄이 남는다고 말하지 않게
        try:
            append_day(root, day)
        except (OSError, ValueError) as exc:  # 링크 · FIFO(WorkspaceError) · 디스크 · 숫자 모양까지: days.jsonl 하나 때문에 다른 전략 · 텔레그램이 멈추지 않게
            what = exc if isinstance(exc, WorkspaceError) else type(exc).__name__
            lines.append(f"{n}: 기계 판독 기록(days.jsonl)을 쓰지 못함({what}){kept}")
        try:
            _log_state(root, day)
        except (WorkspaceError, OSError) as exc:  # state.log 하나 때문에 다른 전략 · 텔레그램까지 멈추지 않게
            lines.append(f"{n}: state.log를 쓰지 못함({exc if isinstance(exc, WorkspaceError) else type(exc).__name__}){kept}")
        days.append(day)
        rep, _ = representative(day.states)
        if base(rep) == "확인 불가":
            code = max(code, EXIT_PARTIAL)
        lines.append(f"{n}: {rep} · {day.head} · {saved}")
    add, extra = _telegram(root, store, make_bot, days, notes, rec_broken, wrote)
    code, lines = max(code, add), lines + extra
    try:
        hc = settings(root)["healthcheck"]
    except (WorkspaceError, OSError):  # 못 읽으면 주소를 몰라 건너뜀(위 텔레그램 줄이 이미 말함)
        hc = None
    failed = ping(hc, ping_opener)
    if failed:
        lines.append(failed)
    return code, lines

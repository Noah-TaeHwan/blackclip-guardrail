"""blackclip 명령의 입구. 하위 명령을 여기에 붙인다."""
from __future__ import annotations

import argparse
import errno
import os
import re
import subprocess
import sys
import threading
from collections.abc import Callable
from datetime import date, datetime, timezone
from decimal import Decimal, InvalidOperation
from pathlib import Path

from blackclip import __version__
from blackclip.binance.client import BinanceError, Client
from blackclip.binance.permissions import Permission, check
from blackclip.connect import Connect
from blackclip.fetch import PERMISSION_TEXT, run_fetch, status_lines
from blackclip.fillstore import coverage_span, read_coverage
from blackclip.keystore import Credentials, KeystoreError, account_name, default_store
from blackclip.notify import (CAPITAL_DROPPED_TEXT, QUIET, chosen_sensitivity, link_telegram, parse_capital,
                              read_live, set_notify, snapshot, status_lines as notify_lines, write_live)
from blackclip.retro.fills import FillsError, normalize_symbol, read_executions, read_funding
from blackclip.retro.match import parse_window
from blackclip.retro.report import build_retro, next_day_utc, render
from blackclip.telegram import Bot, TelegramError
from blackclip.sim.warning_line import DEFAULT_SENSITIVITY, SENSITIVITY
from blackclip.record import STATES, RecordError, after_line, lessons, write_record
from blackclip.workspace import (LockBusy, WorkspaceError, account_key, ensure, find_root, lock, read_guardrail,
                                 strategy_names, write_atomic, write_in)


def _port(text: str) -> int:
    """--port 값을 확인한다.

    Args:
        text: 입력.

    Returns:
        포트 번호(1024~65535).
    """
    try:
        n = int(text)
    except ValueError:
        raise argparse.ArgumentTypeError("1024~65535 사이 숫자로 주세요") from None
    if not 1024 <= n <= 65535:
        raise argparse.ArgumentTypeError("1024~65535 사이 숫자로 주세요")
    return n


def _day(text: str) -> date:
    """--start 날짜를 읽는다.

    Args:
        text: YYYY-MM-DD.

    Returns:
        날짜.

    Raises:
        argparse.ArgumentTypeError: 꼴이 다를 때.
    """
    try:
        return date.fromisoformat(text)
    except ValueError:
        raise argparse.ArgumentTypeError(f"날짜는 YYYY-MM-DD입니다({text})") from None


def _clock(text: str) -> tuple[int, int]:
    """--at 시각을 읽는다.

    Args:
        text: HH:MM.

    Returns:
        (시, 분).

    Raises:
        argparse.ArgumentTypeError: 꼴이 다를 때.
    """
    m = re.fullmatch(r"([01]?\d|2[0-3]):([0-5]\d)", text)
    if not m:
        raise argparse.ArgumentTypeError(f"시각은 HH:MM입니다({text})")
    return int(m.group(1)), int(m.group(2))


def build_parser() -> argparse.ArgumentParser:
    """blackclip 명령의 인자 해석기를 만든다.

    Returns:
        retro · fetch · connect · check · save 하위 명령이 붙은 ArgumentParser.
    """
    parser = argparse.ArgumentParser(prog="blackclip")
    parser.add_argument("--version", action="version", version=f"blackclip {__version__}")
    sub = parser.add_subparsers(dest="command")
    retro = sub.add_parser("retro", help="체결 CSV와 백테스트 거래 목록으로 실행 회고 한 장")
    retro.add_argument("--fills", required=True, type=Path, help="실제 체결 CSV")
    retro.add_argument("--backtest", required=True, type=Path, help="백테스트 거래 목록 CSV")
    retro.add_argument("--funding", type=Path, help="선물 펀딩비 CSV(없으면 그 줄만 점검 불가)")
    retro.add_argument("--capital", required=True, help="자본(거래 통화, 실전 시작일 잔고)")
    retro.add_argument("--live-start", required=True, help="실전 시작 ISO 시각(이전 체결 · 백테스트 거래는 보지 않음)")
    retro.add_argument("--quote", default="USDT", help="거래 통화(기본 USDT). 금액은 이 자산으로만 더한다")
    retro.add_argument("--days", type=int, default=30, help="기간 날수(기본 30)")
    retro.add_argument("--window", default="2h", help="매칭 창(기본 2h)")
    retro.add_argument("--end", help="기간 끝 ISO 시각(기본: 실제 · 백테스트 체결 중 마지막 체결 다음 날 0시 UTC, 체결이 없으면 지금). 체결 파일이 어디까지 담겼는지는 뜻하지 않는다")
    retro.add_argument("--fills-until", help="체결 파일에 이 ISO 시각 전의 체결이 모두 담겼다는 말(시간대 필요, 생략 가능). 안 주면 blackclip fetch가 쓴 fills.csv는 받은 범위로 정하고, 그 밖 파일은 마지막 체결 뒤 백테스트 거래를 구분 불가로 둔다")
    retro.add_argument("--out", type=Path, help="결과 파일(없으면 화면에)")
    fetch = sub.add_parser("fetch", help="조회 키로 바이낸스 현물 · 선물 체결과 펀딩비를 받아 .blackclip/fills/에 쌓기")
    fetch.add_argument("--account", default="main", help="계정 이름(서브 계정마다 하나, 기본 main)")
    fetch.add_argument("--testnet", action="store_true", help="바이낸스 테스트넷 키로 받기")
    fetch.add_argument("--market", choices=["spot", "futures", "all"], default="all", help="받을 시장(기본 둘 다)")
    fetch.add_argument("--symbols", help="쉼표로 나눈 종목(예: BTCUSDT,ETHUSDT). 한 번 주면 계정 · 시장마다 기억한다")
    fetch.add_argument("--since", help="처음 받을 때 시작 ISO 시각(기본 30일 전 0시 UTC). 이미 받은 종목은 받은 끝부터 이어 받는다")
    connect = sub.add_parser("connect", help="조회 키 연결 화면 열기 · 연결 상태")
    mode = connect.add_mutually_exclusive_group()  # 셋 다 없으면 _run_connect가 한 줄로 거절(--telegram만이면 대화 잇기)
    mode.add_argument("--open", action="store_true", help="이 컴퓨터 브라우저에서 키를 넣는 화면(127.0.0.1에만 열림)")
    mode.add_argument("--status", action="store_true", help="연결 상태 요약")
    connect.add_argument("--account", default="main", help="계정 이름(서브 계정마다 하나, 기본 main)")
    connect.add_argument("--testnet", action="store_true", help="바이낸스 테스트넷 키")
    connect.add_argument("--port", type=_port, default=8765, help="화면 포트(기본 8765)")
    connect.add_argument("--no-browser", action="store_true", help="브라우저를 열지 않고 주소만 띄움(화면 없는 서버, SSH 포트 포워딩)")
    connect.add_argument("--telegram", action="store_true",
                         help="텔레그램: --open과 함께면 봇 토큰을 넣는 화면, --open 없이면 저장된 토큰으로 대화 잇기")
    connect.add_argument("--choose", action="store_true",
                         help="민감도 · 조용한 날 · 실행 경고를 고르는 화면(--open과 함께, 저장은 blackclip notify와 같은 자리)")
    chk = sub.add_parser("check", help="실전 투입 전 점검: 재현성 · 미래참조 · 거래비용 · 바이낸스 데이터 차이")
    chk.add_argument("strategy", help="전략 이름(.blackclip/strategies/<이름>/link.py)")
    chk.add_argument("--no-binance", action="store_true", help="바이낸스 공개 봉과 비교하지 않음(인터넷 없이)")
    sv = sub.add_parser("save", help="가드레일 저장: 점검을 다시 돌려 .blackclip/strategies/<전략>/guardrail.vN.yaml에")
    sv.add_argument("strategy", help="전략 이름(.blackclip/strategies/<이름>/link.py)")
    sv.add_argument("--start", type=_day, help="실전 시작일 YYYY-MM-DD(기본 오늘 UTC, 이미 실전 중이면 지난 날짜)")
    sv.add_argument("--sensitivity", choices=list(SENSITIVITY),
                    help="경고선 민감도: 전략이 멀쩡해도 1년 내내 보면 헛경보가 한 번 이상 날 확률 "
                         "민감 약 46%% · 보통 약 20%% · 둔감 약 5%%(한 달 기준 5%% · 1.84%% · 0.43%%). "
                         f"기본은 고르는 화면(blackclip connect --open --choose)에서 고른 값, 없으면 {DEFAULT_SENSITIVITY}")
    sv.add_argument("--reason", help="바꾸는 이유(이미 판이 있으면 필수, 다음 판으로 남김)")
    lv = sub.add_parser("live", help="실전 시작: 조회 키로 지금 잔고 · 포지션을 받아 적기(.blackclip/strategies/<전략>/live.json, Git 제외)")
    lv.add_argument("strategy", help="전략 이름")
    lv.add_argument("--account", default="main", help="체결을 받는 계좌 이름(fetch --account와 같음, 기본 main)")
    lv.add_argument("--quote", default="USDT", help="거래 통화(기본 USDT)")
    lv.add_argument("--capital", help="기준 자본(거래 통화, 0보다 큰 숫자): 주면 실제 손익 비율의 분모로 씀(live.json에만 적음). "
                                      "안 주면 계좌 잔고 전체 + 출발 포지션. 잔고 · 포지션은 어느 쪽이든 받음")
    nt = sub.add_parser("notify", help="알림 설정 보기 · 바꾸기(조용한 날 · 헬스체크 · 실행 경고)")
    nt.add_argument("--quiet", choices=list(QUIET), help="조용한 날: 매일(매일 한 줄, 기본) · 바뀔때만")
    hc = nt.add_mutually_exclusive_group()
    hc.add_argument("--healthcheck", help="바깥 헬스체크 https 주소(매일 대조가 끝나면 내용 없는 신호)")
    hc.add_argument("--no-healthcheck", action="store_true", help="헬스체크 주소 지우기")
    nt.add_argument("--execution-alerts", choices=["on", "off"], help="실행 경고 텔레그램(첫 회고를 본 뒤 on)")
    sub.add_parser("daily", help="매일 대조: 체결 받기 · 같은 기간 백테스트 · 원인 셋 · daily 파일 · 텔레그램(예약 작업이 부름)")
    sub.add_parser("watch", help="자주 보는 시계: 전략별 계좌(--capital 없이 live한 전략)의 가드레일 선 · 봇 멈춤 · 거래 빈도를 "
                                 "1시간마다(예약 작업이 부름)")
    ev = sub.add_parser("events", help="키 없는 감시: 봇 로그를 어댑터로 이벤트로 바꿔 쌓고, 무엇이 들어오는지 보기(이벤트 확인)")
    ev.add_argument("strategy", help="전략 이름(.blackclip/strategies/<전략>/adapter.py가 있는 폴더)")
    fl = sub.add_parser("follow", help="키 없는 감시: 전략 없이 = 지켜보는 시계 한 번(예약 작업이 5분마다 부름), "
                                       "전략과 --quiet-minutes = 「봇이 안 보여요」 선 승인")
    fl.add_argument("strategy", nargs="?", help="선을 적을 전략 이름")
    fl.add_argument("--quiet-minutes", type=int, help="이벤트가 이 분 넘게 없으면 「봇이 안 보여요」(5~10080)")
    scn = sub.add_parser("scan", help="첫 셋업: 연 폴더를 실행 없이 훑어 전략 지도 .blackclip/map.json 만들기(전략 후보 · 실전 봇 · "
                                      "노트북 · 키처럼 보이는 값이 든 파일 · git 커밋 수)")
    scn.add_argument("folder", nargs="?", default=Path("."), type=Path, help="훑을 폴더(기본 지금 폴더, 그 안만 봄)")
    st = sub.add_parser("status", help="매일 대조 기록 요약(늘 평문): 결론 · 손익 두 줄 먼저, 전략을 안 주면 전략마다 한 줄")
    st.add_argument("strategy", nargs="?", help="전략 이름(안 주면 가드레일이 있는 전략 전체)")
    ch = sub.add_parser("chart", help="매일 대조 기록 그림(색 · 그림 글자, NO_COLOR면 색 없음): 전략을 안 주면 스파크라인 목록, 주면 그 전략의 그림")
    ch.add_argument("strategy", nargs="?", help="전략 이름(안 주면 가드레일이 있는 전략 전체)")
    ch.add_argument("--trades", action="store_true", help="거래 단위 그림(E): 실전 시작부터 거래 1건 수익 · 백테스트 가운데 80%% · 실제 평균(전략 이름과 함께)")
    rc = sub.add_parser("record", help="전략 상태 기록: 끄기(중지 · 은퇴) · 다시 켜기(실전) · 이유와 결정을 .blackclip/strategies/<전략>/record.md에")
    rc.add_argument("strategy", help="전략 이름(.blackclip/strategies/ 안 폴더)")
    rc.add_argument("--state", required=True, choices=list(STATES), help="바꿀 상태: 실전 · 중지 · 은퇴(은퇴는 되돌리지 않음)")
    rc.add_argument("--reason", help="바꾼 이유(한 줄 · 500자 이하 · 절대 경로 없음 · git에 올라감)")
    rc.add_argument("--decision", help="내가 정한 것(한 줄 · 500자 이하 · 절대 경로 없음 · git에 올라감)")
    su = sub.add_parser("setup", help="첫 셋업: 지금 어느 단계인지와 다음 할 일 하나(작업 폴더를 읽기만 함)")
    su.add_argument("--decline", action="store_true", help="이 레포에서는 첫 셋업 줄을 다시 묻지 않음(사용자 설정 폴더에 표지, 레포에는 안 씀)")
    db = sub.add_parser("dashboard", help="보기와 연결 화면: 이 컴퓨터 브라우저로 전략 상태 보기(127.0.0.1, 실행마다 새 접속 토큰, Ctrl+C로 끔)")
    db.add_argument("--port", type=_port, default=8765, help="화면 포트(기본 8765)")
    db.add_argument("--no-browser", action="store_true", help="브라우저를 열지 않고 주소만 띄움(화면 없는 서버, SSH 포트 포워딩)")
    sc = sub.add_parser("schedule", help="매일 대조 · 자주 보는 시계를 예약 작업으로 걸기(macOS launchd) · 지우기")
    when = sc.add_mutually_exclusive_group()
    when.add_argument("--at", type=_clock, default=(9, 10), help="이 컴퓨터 시간 HH:MM(기본 09:10 = 한국 기준 일봉 마감 10분 뒤)")
    when.add_argument("--remove", action="store_true", help="예약 지우기")
    return parser


def _aware(name: str, text: str) -> datetime:
    """명령 인자의 ISO 시각을 읽는다. 시간대가 없으면 멈춘다.

    Args:
        name: 인자 이름(오류 문구용).
        text: ISO 시각 문자열.

    Returns:
        시간대가 있는 datetime.
    """
    try:
        t = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError as exc:
        raise FillsError(f"{name}: 시각을 읽을 수 없습니다({text})") from exc
    if t.tzinfo is None:
        raise FillsError(f"{name}: 시간대가 없습니다({text}). 예: 2026-09-23T00:00:00+00:00")
    return t


def _end_of(args: argparse.Namespace, fill_times: list[datetime]) -> datetime:
    """회고 기간의 끝 시각을 정한다. 기본은 실제 · 백테스트 두 파일 중 마지막 체결 다음 날 0시 UTC다(멈춘 봇의 뒤 백테스트 거래도 기간에 남기려고).

    Args:
        args: 해석한 인자(--end).
        fill_times: 실제 체결 파일과 백테스트 파일의 체결 시각 전부. 비었으면 지금을 마지막 체결로 본다.

    Returns:
        시간대가 있는 기간 끝(제외).
    """
    if args.end:
        return _aware("--end", args.end)
    return next_day_utc(max(fill_times, default=datetime.now(timezone.utc)))


def _read(arg: str, reader: Callable[[Path], list], path: Path) -> list:
    """입력 CSV를 읽고, UTF-8이 아니면 어느 인자인지 적어 멈춘다.

    Args:
        arg: 인자 이름(--fills 등, 오류 문구용).
        reader: read_executions 또는 read_funding.
        path: 읽을 파일.

    Returns:
        reader가 돌려준 목록.
    """
    try:
        return reader(path)
    except UnicodeDecodeError:
        raise FillsError(f"--{arg}: 파일이 UTF-8이 아닙니다. 엑셀이면 「CSV UTF-8」로 다시 저장하세요") from None


def _run_retro(args: argparse.Namespace) -> int:
    """retro 하위 명령을 실행한다.

    Args:
        args: 해석한 인자.

    Returns:
        종료 코드(0 성공, 2 입력 오류).
    """
    try:
        inputs = {p.resolve() for p in (args.fills, args.backtest, args.funding) if p}
        if args.out and args.out.resolve() in inputs:
            raise FillsError("--out: 입력 파일과 같은 경로입니다")
        try:
            capital = Decimal(args.capital)
        except InvalidOperation:
            raise FillsError(f"--capital: 숫자가 아닙니다({args.capital})") from None
        if not capital.is_finite() or capital <= 0:
            raise FillsError("--capital: 0보다 큰 숫자여야 합니다")
        if args.days <= 0:
            raise FillsError("--days: 1 이상이어야 합니다")
        quote = args.quote.strip().upper()
        if not quote:
            raise FillsError("--quote: 비어 있습니다")
        live_start = _aware("--live-start", args.live_start)
        fills_until = _aware("--fills-until", args.fills_until) if args.fills_until else None
        backtest = _read("backtest", read_executions, args.backtest)
        actual = _read("fills", read_executions, args.fills)
        funding = _read("funding", read_funding, args.funding) if args.funding else None
        window = parse_window(args.window)
        last_fill = max((e.time for e in actual), default=None)
        if fills_until and last_fill and fills_until < last_fill:
            raise FillsError(f"--fills-until: 마지막 실제 체결({last_fill.astimezone(timezone.utc).isoformat()})보다 앞입니다")
        end = _end_of(args, [e.time for e in actual + backtest])
        if end <= live_start:
            if args.end:
                raise FillsError("--end: 실전 시작보다 뒤여야 합니다")
            raise FillsError("--end(주지 않으면 실제 · 백테스트 체결 중 마지막 체결 다음 날 0시 UTC, 체결이 없으면 지금): 실전 시작보다 뒤여야 합니다")
        source, fills_from = "--fills-until", None
        # 받은 범위는 blackclip fetch가 쓴 fills.csv에만 쓴다(옆에 둔 다른 CSV는 일부만 담았을 수 있어서).
        # --fills-until을 줬으면 읽지 않는다(깨진 coverage.json에 막히지 않게).
        if fills_until is None and args.fills.name == "fills.csv":
            try:
                # 종목 이름은 회고처럼 정규화해 맞댄다(만기 선물 BTCUSDT_261225 → BTCUSDT261225).
                cov = {normalize_symbol(k): v for k, v in read_coverage(args.fills.parent).items() if not k.startswith("_")}
                if cov:
                    syms = sorted({e.symbol for e in backtest})
                    span = coverage_span(cov, syms)
                    if span is None:
                        missing = ", ".join(s for s in syms if s not in cov) or "(백테스트 종목 없음)"
                        print(f"blackclip retro: blackclip fetch 기록에 {missing}가 없어 체결 파일 끝을 정하지 않음", file=sys.stderr)
                    elif None in (span[0].tzinfo, span[1].tzinfo):
                        raise ValueError("시각에 시간대가 없습니다")
                    elif span[0] >= span[1]:  # 한 종목은 일찍 멈추고 다른 종목은 늦게 받기 시작한 경우
                        print("blackclip retro: blackclip fetch 기록의 종목별 받은 범위가 겹치지 않아 체결 파일 범위를 정하지 않음", file=sys.stderr)
                    else:
                        # 시작이 기간보다 늦어도 버리지 않는다: 받기 전 거래는 build_retro가 범위 밖으로 센다
                        fills_from, fills_until, source = span[0], span[1], "blackclip fetch"
            except (ValueError, KeyError, TypeError, AttributeError) as exc:
                raise FillsError(f"coverage.json: 읽을 수 없습니다({exc})") from exc
        page = render(build_retro(backtest, actual, funding, capital, end, live_start, args.days, window, quote,
                                  fills_until=fills_until, fills_until_source=source, fills_from=fills_from))
        if args.out:
            write_atomic(args.out, page)  # 레포가 심은 링크를 따라가 레포 밖 파일을 덮지 않고 링크 자리를 바꿔치기함
        else:
            sys.stdout.write(page)
    except (FillsError, ValueError, OSError, ArithmeticError) as exc:
        print(f"blackclip retro: {exc}", file=sys.stderr)
        return 2
    return 0


_SYMBOL = re.compile(r"^[^\s,/:]{2,40}$")
"""바이낸스 종목 모양: 거래소 원문 그대로(BTCUSDT · 만기 선물 BTCUSDT_261225 · 한자 종목). 공백 · 쉼표 · 슬래시 · 콜론만 막는다."""


def _make_client(market: str, testnet: bool, creds: Credentials) -> Client:
    """실제 바이낸스 클라이언트를 만든다(시험에서 바꿔 끼움).

    Args:
        market: "spot" 또는 "futures".
        testnet: 테스트넷이면 True.
        creds: 키.

    Returns:
        Client.
    """
    return Client(market, testnet, creds)


def _run_fetch(args: argparse.Namespace) -> int:
    """fetch 하위 명령을 실행한다.

    Args:
        args: 해석한 인자.

    Returns:
        종료 코드(0 성공, 2 입력, 3 권한 거부, 4 일부 받지 못함).
    """
    try:
        symbols = None
        if args.symbols:
            symbols = [s.strip().upper() for s in args.symbols.split(",") if s.strip()]
            bad = [s for s in symbols if not _SYMBOL.match(s)]
            if bad or not symbols:
                raise FillsError(f"--symbols: 바이낸스 종목 이름으로 주세요(예: BTCUSDT){': ' + ', '.join(bad) if bad else ''}")
        since = _aware("--since", args.since) if args.since else None
        if since and since > datetime.now(timezone.utc):
            raise FillsError("--since: 지금보다 뒤입니다")
        markets = ["spot", "futures"] if args.market == "all" else [args.market]
        root = ensure(Path.cwd())
        with lock(root, "fetch"):
            code, lines = run_fetch(root, default_store(), _make_client, label=args.account, testnet=args.testnet,
                                    markets=markets, symbols=symbols, since=since, now=datetime.now(timezone.utc))
    # BinanceError: 권한 조회가 401 등(34) · OSError: 네트워크 끊김 · 시간 초과(39) · ValueError: 권한 응답이 JSON이 아님 · 깨진 coverage.json(JSON · 시각 형식)(34)
    except (FillsError, WorkspaceError, KeystoreError, BinanceError, OSError, ValueError) as exc:
        print(f"blackclip fetch: {exc}", file=sys.stderr)
        return 2
    print("\n".join(lines))
    return code


CONNECT_WAIT_S = 900
"""`blackclip connect --open`이 한 번용 연결 화면을 열어 두는 초(15분). 시험이 줄여 끼운다."""

CONNECT_TITLES = {"telegram": "텔레그램 봇 연결", "choose": "알림 고르기"}
"""한 번용 연결 화면 제목(웹 글: 가운뎃점 없음). 조회 키는 `binance {계정} 연결`(테스트넷이면 계정 뒤 「테스트넷」)."""


def _shape_checked(fn: Callable[[Credentials], Permission], error: Callable[[str], Exception]) -> Callable[[Credentials], Permission]:
    """권한 · getMe 확인 함수를 감싼다: 응답 모양이 다를 때 나는 AttributeError · KeyError · TypeError · OverflowError를 엔진 자신의
    오류 종류(`error`)로 바꾼다.

    바이낸스가 200에 객체 아닌 JSON(목록 · 숫자)을 주면 `permissions.check`의 `d.get`이 AttributeError, 시각 맞춤
    (`Client._sync_time`)의 `["serverTime"]`이 KeyError · TypeError, `int(1e999)`가 OverflowError를 낸다. 연결 화면은 쓰기 전 확인 예외를
    BinanceError · OSError · ValueError로만 받아 502 · 쓰기 0으로 답하므로(dashboard.connect_post),
    그 밖 종류가 500 「화면을 그리지 못했어요」로 새지 않게 502 갈래의 예외 종류로 맞춘다.

    Args:
        fn: 키 한 쌍 → 확인 결과.
        error: 사유 글 → 낼 예외(키는 BinanceError, 텔레그램은 TelegramError).

    Returns:
        같은 일을 하되 응답 모양 오류를 `error(「응답 모양이 다릅니다(종류)」)`로 내는 함수.
    """
    def run(creds: Credentials) -> Permission:
        try:
            return fn(creds)
        except (AttributeError, KeyError, TypeError, OverflowError) as exc:
            raise error(f"응답 모양이 다릅니다({type(exc).__name__})") from None
    return run


def _connect_setup(store, label: str, testnet: bool, title: str) -> Connect:  # noqa: ANN001
    """대시보드 서버에 넘길 연결 화면 설정(상주 대시보드 · `connect --open` 한 번용이 같이 씀).

    Args:
        store: 키 보관.
        label: 계정 이름(`--account`, 상주는 main).
        testnet: 테스트넷 키면 True(상주는 False).
        title: 한 번용 화면 제목.

    Returns:
        Connect(바이낸스 권한 조회 · 텔레그램 getMe 확인, 응답 모양 오류는 BinanceError · TelegramError).
    """
    return Connect(store=store, label=label, testnet=testnet, title=title,
                   check_key=_shape_checked(lambda c: check(_make_client("spot", testnet, c)), lambda why: BinanceError(0, None, why)),
                   check_telegram=_shape_checked(_telegram_check, lambda why: TelegramError(f"텔레그램 {why}")))


def _run_connect(args: argparse.Namespace) -> int:
    """connect 하위 명령: `--status` 상태(먼저) · `--telegram`만 대화 잇기 · `--open` 한 번용 연결 화면.

    `--open`은 대시보드 서버(`dashboard.serve`)를 한 번용으로 띄워 연 패널 하나만 받고, 저장되면 결과 줄로 끝난다. 권한은 서버가
    저장과 같은 연결 잠금 안에서 적는다. 텔레그램은 토큰 저장까지만(대화 잇기는 `blackclip connect --telegram`).

    Args:
        args: 해석한 인자.

    Returns:
        종료 코드(0 저장 · 상태 · 이음, 1 시간 지남, 2 입력 · 포트 · 보관 오류 · 저장 뒤 실패).
    """
    try:
        if args.choose and (not args.open or args.telegram):
            raise WorkspaceError("--choose는 --open과 함께, --telegram 없이 씁니다(지금 값은 blackclip notify)")
        if not (args.open or args.status or args.telegram):
            raise WorkspaceError("--open(화면) · --status(상태) · --telegram(대화 잇기) 가운데 하나를 함께 씁니다")
        root, store = ensure(Path.cwd()), default_store()
        if args.status:
            print("\n".join(status_lines(root, store)))
            return 0
        if not args.open:
            return link_telegram(root, store, _telegram_bot, lambda s: print(s, flush=True), datetime.now(timezone.utc))
        kind = "choose" if args.choose else "telegram" if args.telegram else "key"
        key = account_key(args.account, args.testnet)
        title = CONNECT_TITLES.get(kind, f"binance {args.account}{' 테스트넷' if args.testnet else ''} 연결")
        connect = _connect_setup(store, args.account, args.testnet, title)
    # OSError: 상태 경로의 파일 오류(39) · ValueError · KeyError: 깨진 coverage.json · 모르는 permission.status
    # TypeError: permission 안 status가 목록 · checked_at이 숫자처럼 모양이 틀린 값
    except (WorkspaceError, KeystoreError, OSError, ValueError, KeyError, TypeError) as exc:
        print(f"blackclip connect: {exc}", file=sys.stderr)
        return 2
    from blackclip import dashboard  # 늦게: 훅 경로가 cli를 불러도 대시보드 · 화면 모듈을 부르지 않게

    opened: list[str] = []

    def out(line: str) -> None:
        opened.append(line)  # 첫 안내 줄 = 포트를 연 뒤: 그 뒤 OSError(파이프가 닫힘 등)는 포트 글이 아님
        print(line, flush=True)

    try:
        saved = dashboard.serve(root, args.port, not args.no_browser, out, connect=connect, once=dashboard.Once(kind, threading.Event()),
                                path=f"/connect?kind={kind}", wait_s=CONNECT_WAIT_S)
    except OSError as exc:
        if opened:
            print(f"blackclip connect: 연결 화면을 띄운 뒤 멈췄습니다({exc.strerror or exc})", file=sys.stderr)
        elif exc.errno == errno.EADDRINUSE:  # 판별 프로브 없음: 같은 레포 대시보드인지 응답으로 가를 수 없어 두 길만 안내
            print(f"blackclip connect: {args.port} 포트가 이미 쓰이고 있습니다. 같은 레포에서 띄운 대시보드면 그 화면의 「연결」에서 "
                  "넣습니다(main 계정). 다른 계정 · 테스트넷이거나 다른 프로그램이면 --port <다른 번호>를 붙여 다시 실행합니다.", file=sys.stderr)
        else:
            print(f"blackclip connect: {args.port} 포트로 화면을 열지 못했습니다({exc.strerror or exc}) · --port로 다른 번호", file=sys.stderr)
        return 2
    if saved is None:
        print(f"시간이 지나 {'고르는' if kind == 'choose' else '연결'} 화면을 닫았어요(저장 안 함)")
        return 1
    if saved.state_error is not None:
        if saved.permission is None:  # 보관 · 연결 상태 쓰기를 시작한 뒤 예외: 저장했는지 모름
            print(f"blackclip connect: 저장했는지 확인하지 못했습니다({saved.state_error}) · blackclip connect --status로 확인", file=sys.stderr)
        else:
            print(f"blackclip connect: 키는 저장했지만 연결 상태를 적지 못했습니다({saved.state_error})", file=sys.stderr)
        return 2
    if kind == "telegram":  # 저장 뒤 끝남: 숫자 줄을 AI가 바로 전할 수 있게 잇기는 따로 띄우는 명령으로
        print(f"토큰 저장됨 · 텔레그램 {saved.permission.detail}\n대화 잇기: blackclip connect --telegram")
        return 0
    if kind == "choose":
        try:
            print("\n".join(["골랐어요", *notify_lines(root)]))
        except (WorkspaceError, OSError) as exc:  # 저장은 됐고 다시 읽기만 실패
            print(f"blackclip connect: {exc}", file=sys.stderr)
            return 2
        return 0
    print(f"연결됨 · binance {key} · 권한 {PERMISSION_TEXT[saved.permission.status]}({saved.permission.detail}) · 이제 blackclip fetch --symbols BTCUSDT처럼")
    return 0


def _telegram_bot(token: str) -> Bot:
    """실제 텔레그램 봇을 만든다(시험에서 바꿔 끼움).

    Args:
        token: 봇 토큰.

    Returns:
        Bot.
    """
    return Bot(token)


def _telegram_check(creds: Credentials) -> Permission:
    """연결 화면이 저장 전에 토큰을 확인한다(getMe).

    Args:
        creds: Credentials(토큰, "").

    Returns:
        Permission("read_only", "봇 @이름").
    """
    me = _telegram_bot(creds.api_key).get_me()
    return Permission("read_only", f"봇 @{me.get('username', '?')}")


def _run_live(args: argparse.Namespace) -> int:
    """live 하위 명령: 실전 시작 잔고 · 포지션을 조회 키로 받아 적는다(--capital).

    Args:
        args: 해석한 인자.

    Returns:
        종료 코드(0 적음, 2 입력 오류 · 키 없음 · 권한 거부 · 받기 실패).
    """
    from decimal import InvalidOperation

    from blackclip.check.link import NAME

    try:
        if not NAME.fullmatch(args.strategy):
            raise WorkspaceError(f"전략 이름 모양이 다릅니다({args.strategy})")
        capital = parse_capital(args.capital) if args.capital is not None else None  # 받기 전에 거른다
        root = ensure(Path.cwd())
        got = read_guardrail(root, args.strategy)
        if got is None:
            raise WorkspaceError(f"{args.strategy}: 가드레일이 없습니다 · blackclip save {args.strategy} 뒤에")
        _, g = got
        target = g["target"]
        store = default_store()
        creds = store.load(account_name("binance", account_key(args.account, False)))
        if creds is None:
            raise WorkspaceError(f"키가 없습니다(binance {args.account}) · blackclip connect --open")
        perm = check(_make_client("spot", False, creds))
        if perm.status == "rejected":
            raise WorkspaceError(f"받지 않았습니다: {perm.detail}. 조회만 켠 키로 다시 연결하세요(blackclip connect --open)")
        now = datetime.now(timezone.utc)
        cash, pos = snapshot(_make_client(target["market"], False, creds), target["market"], list(target["symbols"]),
                             args.quote.strip().upper())
        old = read_live(root, args.strategy) if capital is None else None
        dropped = old is not None and old["capital"] is not None  # 조용히 분모가 바뀌지 않게
        path = write_live(root, args.strategy, args.account, args.quote, now, cash, pos, capital)
    except (WorkspaceError, KeystoreError, OSError, KeyError, ValueError) as exc:
        print(f"blackclip live: {exc}", file=sys.stderr)
        return 2
    except (BinanceError, TypeError, InvalidOperation) as exc:  # 응답 원문은 안 보임
        print(f"blackclip live: 잔고 · 포지션을 못 받음({type(exc).__name__}) · 키 · 연결 확인 뒤 다시", file=sys.stderr)
        return 2
    held = [f"{s} {q}" for s, q in pos.items() if q]
    print(f"실전 시작 잔고 받음 · {args.strategy} · 계좌 {args.account} · 포지션 {', '.join(held) if held else '없음'} · "
          f"{now:%Y-%m-%d} UTC 종가로 평가해 출발(대조는 다음 날부터) · {path.relative_to(root.parent).as_posix()}(Git 제외)")
    if dropped:
        print(CAPITAL_DROPPED_TEXT)
    return 0


def _run_notify(args: argparse.Namespace) -> int:
    """notify 하위 명령: 알림 설정을 바꾸거나(인자가 있으면) 보인다.

    Args:
        args: 해석한 인자.

    Returns:
        종료 코드(0, 2 입력 오류).
    """
    try:
        root, store = ensure(Path.cwd()), default_store()
        if args.quiet or args.healthcheck or args.no_healthcheck or args.execution_alerts:
            set_notify(root, quiet=args.quiet, healthcheck=args.healthcheck, clear_healthcheck=args.no_healthcheck,
                       execution_alerts=None if args.execution_alerts is None else args.execution_alerts == "on")
        print("\n".join(notify_lines(root, store)))
    except (WorkspaceError, KeystoreError, OSError) as exc:
        print(f"blackclip notify: {exc}", file=sys.stderr)
        return 2
    return 0


def _run_daily(args: argparse.Namespace) -> int:
    """daily 하위 명령: 하루 대조. pandas는 여기서만 불러온다.

    Args:
        args: 해석한 인자.

    Returns:
        종료 코드(0 · 2 · 4 · 5).
    """
    from blackclip.daily.run import run_daily

    try:
        root = ensure(Path.cwd())
        with lock(root, "daily", wait_s=600):  # 자주 보는 시계가 돌고 있으면 끝날 때까지 기다린다(그날 대조를 잃지 않게)
            code, lines = run_daily(root, default_store(), _make_client, lambda m: _make_client(m, False, None), _telegram_bot,
                                    datetime.now(timezone.utc))
    except (WorkspaceError, KeystoreError, OSError, ValueError) as exc:
        print(f"blackclip daily: {exc}", file=sys.stderr)
        return 2
    print("\n".join(lines))
    return code


def _run_watch(args: argparse.Namespace) -> int:
    """watch 하위 명령: 자주 보는 시계 한 번. pandas는 여기서만 불러온다.

    매일 대조와 같은 잠금(daily)을 잡는다: 둘이 같이 돌면 notified.json · 체결 받기가 겹친다. 잡혀 있으면 이번 확인은
    건너뛴다(다음 시간에 다시, 종료 0).

    Args:
        args: 해석한 인자.

    Returns:
        종료 코드(0 · 2 · 4 · 5, daily와 같은 규칙).
    """
    from blackclip.watch.run import run_watch

    try:
        root = ensure(Path.cwd())
        with lock(root, "daily"):
            code, lines = run_watch(root, default_store(), _make_client, lambda m: _make_client(m, False, None),
                                    _telegram_bot, datetime.now(timezone.utc))
    except LockBusy as exc:
        print(f"blackclip watch: 이번 확인은 건너뜀({exc}) · 다음 시간에 다시")
        return 0
    except (WorkspaceError, KeystoreError, OSError, ValueError) as exc:
        print(f"blackclip watch: {exc}", file=sys.stderr)
        return 2
    print("\n".join(lines))
    return code


def _run_events(args: argparse.Namespace) -> int:
    """events 하위 명령: 이벤트 확인(봇 로그의 새 줄을 어댑터로 바꿔 쌓고 요약).

    Args:
        args: 해석한 인자.

    Returns:
        종료 코드(0, 2 어댑터 · 봇 로그 · 작업 폴더 문제).
    """
    from blackclip.events import EventsError, ingest, summary

    now = datetime.now(timezone.utc).replace(microsecond=0)
    try:
        root = ensure(Path.cwd())
        with lock(root, "follow"):
            got = ingest(root, args.strategy, now)
    except LockBusy as exc:
        print(f"blackclip events: 지금은 건너뜀({exc})")
        return 0
    except (EventsError, WorkspaceError, OSError) as exc:
        print(f"blackclip events: {exc}", file=sys.stderr)
        return 2
    print("\n".join(summary(args.strategy, got, now)))
    return 0


def _run_follow(args: argparse.Namespace) -> int:
    """follow 하위 명령: 전략과 --quiet-minutes면 선 승인, 아무것도 없으면 지켜보는 시계 한 번(예약 작업이 부름).

    Args:
        args: 해석한 인자.

    Returns:
        종료 코드(0 · 2 · 4 · 5, daily와 같은 규칙).
    """
    from blackclip.events import EventsError
    from blackclip.follow import approve, run_follow

    if (args.strategy is None) != (args.quiet_minutes is None):
        print("blackclip follow: 선을 적으려면 전략과 --quiet-minutes를 함께, 시계를 돌리려면 둘 다 빼고", file=sys.stderr)
        return 2
    now = datetime.now(timezone.utc)
    try:
        root = ensure(Path.cwd())
        with lock(root, "follow"):
            if args.strategy is not None:
                code, lines = 0, approve(root, args.strategy, args.quiet_minutes, now)
            else:
                code, lines = run_follow(root, default_store(), _telegram_bot, now)
    except LockBusy as exc:
        print(f"blackclip follow: 이번 확인은 건너뜀({exc}) · 다음 확인에서 다시")
        return 0
    except (EventsError, WorkspaceError, KeystoreError, OSError) as exc:
        print(f"blackclip follow: {exc}", file=sys.stderr)
        return 2
    print("\n".join(lines))
    return code


def _run_schedule(args: argparse.Namespace) -> int:
    """schedule 하위 명령: launchd에 매일 대조 · 자주 보는 시계(선이 있으면 키 없이 지켜보는 시계도) 걸기 · 지우기.

    Args:
        args: 해석한 인자.

    Returns:
        종료 코드(0, 2 실패 · 지웠는지 확인 못 한 예약이 있음).
    """
    from blackclip.follow import followed
    from blackclip.schedule import install, remove

    try:
        root = ensure(Path.cwd())
        repo = root.parent
        if args.remove:  # 전략 폴더를 읽지 않음: 못 읽어도 예약은 지움
            ok, lines = remove(repo)
        else:
            watching = bool(followed(root))
            guarded = any(p.is_file() for p in (root / "strategies").glob("*/guardrail.v*.yaml"))  # daily · watch가 볼 것
            ok, lines = True, install(repo, *args.at, follow=watching, daily=guarded or not watching)
    except (WorkspaceError, OSError, subprocess.TimeoutExpired) as exc:
        print(f"blackclip schedule: {exc}", file=sys.stderr)
        return 2
    print("\n".join(lines))
    return 0 if ok else 2


def _run_scan(args: argparse.Namespace) -> int:
    """scan 하위 명령: 연 폴더를 훑어 map.json을 쓰고 요약을 보인다(고객 코드는 실행하지 않음).

    Args:
        args: 해석한 인자.

    Returns:
        종료 코드(0, 2 폴더 아님 · 쓰기 실패).
    """
    from blackclip.scan import render, scan

    if not args.folder.is_dir():
        print(f"blackclip scan: 폴더가 아닙니다({args.folder})", file=sys.stderr)
        return 2
    if os.path.abspath(args.folder) != str(args.folder.resolve()):  # 경로 중간에 링크가 있으면 두 길이 갈림(레포 밖을 훑지 않게)
        print(f"blackclip scan: 폴더 경로에 심볼릭 링크가 있어 훑지 않습니다({args.folder}) · 레포 안의 진짜 폴더를 주세요",
              file=sys.stderr)
        return 2
    try:
        doc = scan(args.folder, ensure(args.folder))
    except (WorkspaceError, OSError) as exc:
        print(f"blackclip scan: 훑지 못했습니다({type(exc).__name__}: {exc})", file=sys.stderr)
        return 2
    print("\n".join(render(doc)))
    return 0


def _run_setup(args: argparse.Namespace) -> int:
    """setup 하위 명령: 셋업 단계와 다음 할 일 하나(레포에는 아무것도 만들지 않음). --decline이면 거절 표지만 적는다.

    Args:
        args: 해석한 인자(decline: 이 레포의 첫 셋업 줄 끄기).

    Returns:
        종료 코드(0, 2 읽기 실패).
    """
    if args.decline:
        return _run_decline()
    from blackclip.stages import judge, render

    root = find_root(Path.cwd()) or Path.cwd().resolve() / ".blackclip"  # 없으면 없는 자리를 읽어 「훑기」부터
    try:
        lines = render(judge(root))
    except OSError as exc:  # 읽기 권한 없는 폴더(git은 권한을 옮기지 못해 이 컴퓨터에서만 생김)
        print(f"blackclip setup: 작업 폴더를 읽지 못했습니다({type(exc).__name__}: {exc})", file=sys.stderr)
        return 2
    print("\n".join(lines))
    return 0


def _run_decline() -> int:
    """setup --decline: 이 레포(꼭대기)의 첫 셋업 거절 표지를 사용자 설정 폴더에 적는다(레포에는 아무것도 쓰지 않음).

    Returns:
        종료 코드(0, 2 설정 폴더가 내 것이 아님 · 쓰지 못함).
    """
    from blackclip.hook import DECLINED, _mark, repo_top
    from blackclip.keystore import config_dir
    from blackclip.schedule import _mine
    from blackclip.workspace import write_atomic

    try:
        d = config_dir()
        for p in (d, d / DECLINED):
            if not _mine(p, folder=True):
                if p.exists() or p.is_symlink():
                    print("blackclip setup: 사용자 설정 폴더가 내 것이 아니거나 남이 쓸 수 있어 적지 않았어요", file=sys.stderr)
                    return 2
                p.mkdir(mode=0o700, parents=True)
        write_atomic(_mark(repo_top(Path.cwd())), "")
    except (OSError, RuntimeError) as exc:
        print(f"blackclip setup: 표지를 적지 못했어요({type(exc).__name__}: {exc})", file=sys.stderr)
        return 2
    print("이 레포에서는 첫 셋업을 다시 묻지 않아요 · 하려면 「blackclip 셋업」이라고 말하면 됨(blackclip scan .)")
    return 0


def _run_status(args: argparse.Namespace) -> int:
    """status 하위 명령: days.jsonl · 가드레일을 읽어 평문 요약을 보인다(읽기만, pandas 안 부름).

    Args:
        args: 해석한 인자.

    Returns:
        종료 코드(0, 2 작업 폴더 없음 · 없는 전략 · 파일 문제, 4 전략을 안 준 보기에서 가드레일 · days.jsonl을 읽지 못한 전략이 있음).
    """
    from blackclip.screen.status import plain, status_lines

    root = find_root(Path.cwd())
    if root is None or root.is_symlink():
        print("blackclip status: 작업 폴더 .blackclip이 없습니다 · 첫 셋업은 blackclip scan .", file=sys.stderr)
        return 2
    try:
        code, lines = status_lines(root, args.strategy)
    except (WorkspaceError, OSError) as exc:
        print(f"blackclip status: {plain(str(exc))}", file=sys.stderr)
        return 2
    print("\n".join(lines))
    return code


def _run_chart(args: argparse.Namespace) -> int:
    """chart 하위 명령: days.jsonl을 그림으로 보인다(읽기만, pandas 안 부름). 색은 NO_COLOR면 끄고 폭은 터미널을 따른다.

    Args:
        args: 해석한 인자.

    Returns:
        종료 코드(0, 2 작업 폴더 없음 · 없는 전략 · 파일 문제, 4 전략을 안 준 보기에서 days.jsonl을 읽지 못한 전략 또는 기록 손상 전략이 있음).
    """
    from blackclip.screen.chart import chart_lines
    from blackclip.screen.paint import color_on, width
    from blackclip.screen.status import plain

    if args.trades and args.strategy is None:
        print("blackclip chart: --trades는 전략 이름과 함께", file=sys.stderr)
        return 2
    root = find_root(Path.cwd())
    if root is None or root.is_symlink():
        print("blackclip chart: 작업 폴더 .blackclip이 없습니다 · 첫 셋업은 blackclip scan .", file=sys.stderr)
        return 2
    try:
        code, lines = chart_lines(root, args.strategy, color_on(), width(), args.trades)
    except (WorkspaceError, OSError) as exc:
        print(f"blackclip chart: {plain(str(exc))}", file=sys.stderr)
        return 2
    sys.stdout.write("\n".join(lines) + "\n")
    return code


def serve_dashboard(root: Path, port: int, open_browser: bool) -> int:
    """`dashboard.serve`를 부른다. 모듈 위에서 import하면 훅 경로가 daily를 불러와 여기서 늦게 부른다(시험이 이 이름을 바꿔 끼움).

    연결 설정을 늘 넘긴다(main 계정 · 테스트넷 아님, 키 보관은 `default_store()`): 홈 내비 「연결」이 404가 아니게.

    Args:
        root: .blackclip 경로.
        port: 포트.
        open_browser: 이 컴퓨터 브라우저로 열지.

    Returns:
        종료 코드 0.

    Raises:
        OSError: 포트를 열지 못함.
    """
    from blackclip import dashboard

    return dashboard.serve(root, port, open_browser, connect=_connect_setup(default_store(), "main", False, "연결"))


def _run_dashboard(args: argparse.Namespace) -> int:
    """dashboard 하위 명령: 보기와 연결 화면 서버를 Ctrl+C까지 띄운다. 작업 폴더를 만들지 않는다.

    Args:
        args: 해석한 인자.

    Returns:
        종료 코드(0 Ctrl+C로 끔, 2 작업 폴더 없음 · 링크 · 포트를 열지 못함).
    """
    root = find_root(Path.cwd())
    if root is None or root.is_symlink():
        print("blackclip dashboard: 이 폴더에서 .blackclip을 찾지 못했어요. 레포 안에서 blackclip scan을 먼저 돌려 주세요.", file=sys.stderr)
        return 2
    try:
        return serve_dashboard(root, args.port, open_browser=not args.no_browser)
    except OSError as exc:
        if exc.errno == errno.EADDRINUSE:  # 판별 프로브 없음: 떠 있는 것이 이 레포 대시보드인지 응답으로 가를 수 없음
            print(f"blackclip dashboard: {args.port} 포트가 이미 쓰이고 있어요. 대시보드가 떠 있으면 그 창을 쓰고, "
                  "아니면 --port <다른 번호>로 띄워 주세요.", file=sys.stderr)
        else:
            print(f"blackclip dashboard: {args.port} 포트로 화면을 열지 못했어요({exc.strerror or exc}). "
                  "--port로 다른 번호를 주세요.", file=sys.stderr)
        return 2


def _run_record(args: argparse.Namespace) -> int:
    """record 하위 명령: 전략 상태를 record.md · state.log에 기록하고 멈추는 것을 말해 준다(pandas 안 부름).

    Args:
        args: 해석한 인자.

    Returns:
        종료 코드(0 기록함 · 바뀐 것 없음 · 이어서 마침, 2 작업 폴더 · 전략 없음 · 글 · 기록 문제 · 쓰기 단계 실패).
    """
    from blackclip.scan import show

    root = find_root(Path.cwd())
    if root is None or root.is_symlink():
        print("blackclip record: 작업 폴더 .blackclip이 없습니다 · 첫 셋업은 blackclip scan .", file=sys.stderr)
        return 2
    name, today = args.strategy, date.today()  # 오늘 = 이 컴퓨터의 날짜(고객이 보는 달력)
    try:
        if name not in strategy_names(root):
            raise RecordError(f"전략 폴더가 없습니다({show(name)}) · .blackclip/strategies/ 아래 이름")
        prev, items = write_record(root, name, args.state, args.reason, args.decision, today)
    except RecordError as exc:
        print(f"blackclip record: {show(str(exc))}", file=sys.stderr)
        return 2
    except (WorkspaceError, OSError) as exc:  # 쓰기 단계 실패: 같은 명령을 다시 하면 빠진 단계만 마침
        why = str(exc) if isinstance(exc, WorkspaceError) else exc.strerror or type(exc).__name__
        print(f"blackclip record: {show(why)} · 같은 명령을 다시 하면 이어서 마칩니다", file=sys.stderr)
        return 2
    state = args.state
    if items:
        print(f"이어서 마침 · {name} · {state} · {' · '.join(items)}")
        return 0
    if prev == "같음":
        print(f"바뀐 것 없음 · {name} · {state}")
        return 0
    off = "이 전략의 매일 대조 · 실시간 감시 · 텔레그램을"
    out = [f"{name} · {prev} → {state} · {today.isoformat()}"]
    if state != "실전":
        out.append(f"{off} 멈춤(다른 전략은 그대로)")
        if state == "은퇴":
            out.append("은퇴는 되돌리지 않음 · 다시 쓰려면 새 이름")
        out += [f"끈 뒤 회고 한 장: .blackclip/strategies/{name}/after.md", f"다음 점검에서 먼저 볼 것: {after_line(root, name, today)}"]
    elif prev != "실전":
        out.append(f"{off} 다시 켬(다음 예약부터) · 끈 뒤 회고는 남음")
    out.append(f"기록: .blackclip/strategies/{name}/record.md(git에 올라감 · 공개 레포면 이유 글도 보임)")
    print("\n".join(out))
    return 0


def _run_check(args: argparse.Namespace) -> int:
    """check 하위 명령을 실행한다. pandas는 여기서만 불러온다(다른 명령이 느려지지 않게).

    Args:
        args: 해석한 인자.

    Returns:
        종료 코드(0 점검이 돎 · 상태와 무관, 2 못 돌림).
    """
    from blackclip.check.link import LinkError
    from blackclip.check.report import render, render_md, run_check

    try:
        root = ensure(Path.cwd())
        result = run_check(root, args.strategy, None if args.no_binance else (lambda m: _make_client(m, False, None)))
    except (LinkError, WorkspaceError, OSError) as exc:
        print(f"blackclip check: {exc}", file=sys.stderr)
        return 2
    if result.detail:  # 원문은 터미널에만(check.md는 커밋됨)
        print(f"blackclip check: {result.detail}", file=sys.stderr)
    print(render(result))  # 쓰기가 실패해도 결과는 보이게 먼저 찍는다
    for line in lessons(root, args.strategy):  # 끈 전략의 ④(터미널에만, check.md에는 안 씀)
        print(line)
    out = root / "strategies" / args.strategy / "check.md"
    try:
        write_in(root, f"strategies/{args.strategy}/check.md", render_md(result, datetime.now(timezone.utc)))
    except WorkspaceError as exc:  # 상위 폴더가 링크(WorkspaceError는 ValueError라 아래 OSError에 안 잡힘)
        print(f"blackclip check: check.md를 쓰지 못했습니다({exc})", file=sys.stderr)
        return 2
    except OSError as exc:
        print(f"blackclip check: check.md를 쓰지 못했습니다({exc.strerror or type(exc).__name__})", file=sys.stderr)
        return 2
    print(f"저장: {out.relative_to(root.parent).as_posix()}")
    return 0


def _run_save(args: argparse.Namespace) -> int:
    """save 하위 명령을 실행한다. pandas는 여기서만 불러온다.

    Args:
        args: 해석한 인자.

    Returns:
        종료 코드(0 저장함, 2 저장 안 함 · 못 돌림).
    """
    from blackclip.check.link import LinkError
    from blackclip.check.save import SaveError, render_save, run_save

    today = datetime.now(timezone.utc).date()
    try:
        root = ensure(Path.cwd())
        path, doc = run_save(root, args.strategy, args.sensitivity or chosen_sensitivity(root), args.start or today,
                             args.reason, today)
    except (LinkError, SaveError, WorkspaceError) as exc:
        print(f"blackclip save: {exc}", file=sys.stderr)
        return 2
    except OSError as exc:  # 메시지에 이 컴퓨터 경로가 들어 있어 설명만(_run_check 관례)
        print(f"blackclip save: 파일을 읽거나 쓰지 못했습니다({exc.strerror or type(exc).__name__})", file=sys.stderr)
        return 2
    print(render_save(doc, path.relative_to(root.parent).as_posix()))
    return 0


def main(argv: list[str] | None = None) -> int:
    """명령을 실행하고 종료 코드를 돌려준다.

    Args:
        argv: 명령 인자. None이면 sys.argv를 쓴다.

    Returns:
        종료 코드(0 정상, 2 입력 오류).
    """
    argv = sys.argv[1:] if argv is None else argv
    if argv[:1] == ["hook"]:  # 숨은 하위 명령(훅 래퍼만 부름): argparse 종료 2는 PreToolUse에서 명령을 막아 거치지 않음
        from blackclip.hook import run_hook

        return run_hook(argv[1] if len(argv) > 1 else "")
    parser = build_parser()
    try:
        args = parser.parse_args(argv)
    except SystemExit as exc:  # --version · --help · 인자 오류는 argparse가 SystemExit로 끝낸다
        return int(exc.code or 0)
    if args.command in ("scan", "setup", "connect"):  # 훅 래퍼가 PATH에 없는 엔진을 찾는 자리, 실패해도 그대로
        from blackclip.schedule import remember_engine

        remember_engine()
    if args.command == "retro":
        return _run_retro(args)
    if args.command == "fetch":
        return _run_fetch(args)
    if args.command == "connect":
        return _run_connect(args)
    if args.command == "check":
        return _run_check(args)
    if args.command == "save":
        return _run_save(args)
    if args.command == "live":
        return _run_live(args)
    if args.command == "notify":
        return _run_notify(args)
    if args.command == "daily":
        return _run_daily(args)
    if args.command == "watch":
        return _run_watch(args)
    if args.command == "events":
        return _run_events(args)
    if args.command == "follow":
        return _run_follow(args)
    if args.command == "schedule":
        return _run_schedule(args)
    if args.command == "scan":
        return _run_scan(args)
    if args.command == "setup":
        return _run_setup(args)
    if args.command == "status":
        return _run_status(args)
    if args.command == "chart":
        return _run_chart(args)
    if args.command == "record":
        return _run_record(args)
    if args.command == "dashboard":
        return _run_dashboard(args)
    parser.print_help()
    return 0

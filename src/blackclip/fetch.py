"""blackclip fetch · connect --status: 조회 키로 체결을 받아 쌓고, 연결 상태를 요약한다."""
from __future__ import annotations

from collections.abc import Callable
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

from blackclip.binance.client import BinanceError
from blackclip.binance.permissions import Permission, check
from blackclip.binance.trades import COLUMNS, FUNDING_COLUMNS, fetch_funding, fetch_trades, iso
from blackclip.fillstore import FUNDING_KEY, merge_csv, read_coverage, update_coverage
from blackclip.keystore import Credentials, KeystoreError, account_name
from blackclip.workspace import account_key, fills_dir, load_connections, section, update_connections

PERMISSION_TEXT = {"read_only": "조회만", "unknown": "확인 불가", "rejected": "거부"}
"""권한 상태 → 화면 말."""

EXIT_INPUT, EXIT_PERMISSION, EXIT_PARTIAL = 2, 3, 4
"""종료 코드: 입력 문제 · 권한 거부 · 일부 받지 못함."""

ClientFactory = Callable[[str, bool, Credentials], Any]
"""(시장, 테스트넷 여부, 키) → 클라이언트."""


def _ms(t: datetime) -> int:
    """datetime → 유닉스 ms.

    Args:
        t: 시간대가 있는 시각.

    Returns:
        ms.
    """
    return int(t.timestamp() * 1000)


def default_since(now: datetime) -> datetime:
    """처음 받을 때 기본 시작: 30일 전 0시 UTC(회고 기본 기간과 같게).

    Args:
        now: 지금(UTC).

    Returns:
        시작 시각.
    """
    return now.replace(hour=0, minute=0, second=0, microsecond=0) - timedelta(days=30)


def _start(cov: dict[str, str] | None, since: datetime | None, now: datetime) -> int:
    """받기 시작 시각을 정한다.

    받은 적이 있으면 받은 끝부터 이어 받는다. --since가 받은 처음보다 앞이면 --since부터 다시 받는다(중복은 쌓을 때 빠진다).
    처음이면 --since, 없으면 30일 전 0시 UTC.

    Args:
        cov: 그 종목의 받은 범위(없으면 None).
        since: --since.
        now: 지금.

    Returns:
        시작 ms.
    """
    if cov and not (since and since < datetime.fromisoformat(cov["from"])):
        return _ms(datetime.fromisoformat(cov["until"]))
    return _ms(since or default_since(now))


def note_permission(conns: dict, label: str, testnet: bool, perm: Permission, now: datetime) -> None:
    """연결 상태 dict에 권한 확인 결과를 적는다(제자리 고침, 키 값은 적지 않는다).

    Args:
        conns: load_connections 결과.
        label: 계정 이름.
        testnet: 테스트넷이면 True.
        perm: 확인 결과.
        now: 확인 시각.

    Raises:
        WorkspaceError: 계정 이름이 틀렸거나 binance · 계정 칸이 객체가 아닐 때.
    """
    acc = section(conns, "binance", account_key(label, testnet))
    acc["permission"] = {"status": perm.status, "detail": perm.detail, "checked_at": now.isoformat(timespec="seconds")}


def record_permission(root: Path, label: str, testnet: bool, perm: Permission, now: datetime) -> None:
    """권한 확인 결과를 연결 상태에 적는다(`connections` 잠금 안).

    Args:
        root: .blackclip 경로.
        label: 계정 이름.
        testnet: 테스트넷이면 True.
        perm: 확인 결과.
        now: 확인 시각.
    """
    update_connections(root, lambda c: note_permission(c, label, testnet, perm, now))


def run_fetch(root: Path, store: Any, make_client: ClientFactory, *, label: str, testnet: bool, markets: list[str],
              symbols: list[str] | None, since: datetime | None, now: datetime) -> tuple[int, list[str]]:
    """키를 읽고 권한을 본 뒤 시장 · 종목마다 체결(선물이면 펀딩비도)을 받아 쌓는다.

    Args:
        root: .blackclip 경로.
        store: 키 보관.
        make_client: 클라이언트 공장.
        label: 계정 이름.
        testnet: 테스트넷이면 True.
        markets: ["spot"] · ["futures"] · 둘 다.
        symbols: 받을 종목. None이면 전에 쓴 종목.
        since: 처음 받을 시작(또는 다시 받을 시작).
        now: 지금(UTC). 받는 끝이 된다.

    Returns:
        (종료 코드, 화면 줄).
    """
    key = account_key(label, testnet)
    creds = store.load(account_name("binance", key))
    if creds is None:
        return EXIT_INPUT, [f"키가 없습니다(binance {key}) · blackclip connect --open" + (" --testnet" if testnet else "")]
    perm = check(make_client("spot", testnet, creds))
    record_permission(root, label, testnet, perm, now)
    if perm.status == "rejected":
        return EXIT_PERMISSION, [f"받지 않았습니다: {perm.detail}. 조회만 켠 키로 다시 연결하세요(blackclip connect --open)"]
    lines = [f"binance {key} · 권한 {PERMISSION_TEXT[perm.status]}({perm.detail})"]
    saved = section(load_connections(root), "binance", key, "symbols")  # 읽기만: 받을 종목 정하기(저장은 끝의 잠금 안 갱신)
    used: dict[str, list[str]] = {}
    code, end = 0, _ms(now)
    for market in markets:
        syms = symbols or saved.get(market, [])
        if not syms:
            lines.append(f"{market}: 받을 종목이 없습니다 · --symbols BTCUSDT,ETHUSDT처럼 주세요")
            code = max(code, EXIT_INPUT)
            continue
        used[market] = syms
        d = fills_dir(root, "binance", key, market)
        cov = read_coverage(d)
        client = make_client(market, testnet, creds)
        for name in syms + ([FUNDING_KEY] if market == "futures" else []):
            shown = "펀딩비" if name == FUNDING_KEY else name
            start = _start(cov.get(name), since, now)
            if start >= end:
                lines.append(f"{market} {shown}: 받을 기간 없음(받은 끝 {iso(start)[:16]}이 지금보다 뒤)")
                continue
            try:
                if name == FUNDING_KEY:
                    got = fetch_funding(client, start, end)
                    added = merge_csv(d / "funding.csv", FUNDING_COLUMNS, got.rows, ("tran_id",))
                else:
                    got = fetch_trades(client, name, start, end)
                    added = merge_csv(d / "fills.csv", COLUMNS, got.rows, ("symbol", "trade_id"))
            except (BinanceError, OSError, ValueError) as exc:  # ValueError: 200인데 JSON이 아닌 본문 · 이상한 Retry-After
                lines.append(f"{market} {shown}: 받지 못함({exc}) · {iso(start)[:16]} 뒤는 확인 불가")
                code = EXIT_PARTIAL
                continue
            update_coverage(d, name, iso(got.start_ms), iso(got.end_ms))
            line = f"{market} {shown}: 새로 {added}건 · {iso(got.start_ms)[:16]} ~ {iso(got.end_ms)[:16]} UTC 받음"
            if got.lost:
                line += f" · {iso(got.lost[0])[:10]} ~ {iso(got.lost[1])[:10]}는 확인 불가(선물 조회는 3개월까지)"
            lines.append(line)
    update_connections(root, lambda c: section(c, "binance", key, "symbols").update(used))
    return code, lines


def status_lines(root: Path, store: Any) -> list[str]:
    """연결 상태 요약(AI가 읽는 출력, 색 없음).

    Args:
        root: .blackclip 경로.
        store: 키 보관.

    Returns:
        화면 줄.
    """
    all_conns = load_connections(root)
    accounts = section(all_conns, "binance")
    if not accounts:
        return ["연결된 계정 없음 · blackclip connect --open"]
    out = []
    for key in sorted(accounts):
        try:
            creds = store.load(account_name("binance", key))
            held = f"키 있음({store.name} · 끝 {creds.last4()})" if creds else "키 없음"
        except KeystoreError as exc:
            held = f"키 읽기 실패({exc})"
        p = section(all_conns, "binance", key, "permission") or None  # 계정 · permission 칸이 객체인지도 봄, 없으면 빈 객체 → None
        perm = f"권한 {PERMISSION_TEXT[p['status']]}({p['checked_at'][:16]} 확인)" if p else "권한 확인 안 함"
        out.append(f"binance {key}: {held} · {perm}")
        for market in ("spot", "futures"):
            for name, c in sorted(read_coverage(root / "fills" / f"binance-{key}" / market).items()):
                shown = "펀딩비" if name == FUNDING_KEY else name
                out.append(f"  {market} {shown}: {c['from'][:16]} ~ {c['until'][:16]} UTC 받음")
    return out

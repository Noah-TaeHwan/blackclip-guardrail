"""전략 연결 파일(.blackclip/strategies/<전략>/link.py)을 읽고 가격표 · 신호가 계약대로인지 확인한다.

연결 파일 계약(고객 AI가 만든다):
    EXEC = "next_open" | "close"       신호 봉 다음 봉 시가 / 신호 봉 종가에 체결
    MARKET = "spot" | "futures"        바이낸스 시장(데이터 차이)
    TRADES = "backtests/x_trades.csv"  (선택) 원래 백테스트 거래 목록, 레포 기준 경로, 체결 CSV와 같은 열
    RESULT_CAGR = 0.182                (선택) 고객 백테스트가 보고한 연 수익률
    TRIALS = (1920, "격자 탐색 10x12x16")  (선택) 평가한 설정 수(최솟값)와 근거, 시도 수에 「고객이 밝힌 값」으로 적음
    def load_prices() -> dict[str, DataFrame]   종목 → OHLC(DatetimeIndex)
    def signals(prices) -> DataFrame | Series   열 = 종목, 값 = 목표 비중 -1~1(한 시점 절댓값 합 ≤ 1)
"""
from __future__ import annotations

import importlib.util
import math
import re
import sys
import unicodedata
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import numpy as np
import pandas as pd

from blackclip.retro.fills import normalize_symbol
from blackclip.scan import has_abs_path  # 절대 경로 검사는 pandas 없는 scan에 한 벌(record도 씀)
from blackclip.workspace import exists_regular

NAME = re.compile(r"[A-Za-z0-9][A-Za-z0-9_-]{0,63}")
"""전략 이름 모양(폴더 이름이 되므로 점 · 슬래시 없음)."""

EXECS: tuple[str, ...] = ("next_open", "close")
"""체결 규칙."""

MARKETS: tuple[str, ...] = ("spot", "futures")
"""바이낸스 시장."""

COLUMNS: tuple[str, ...] = ("Open", "High", "Low", "Close")
"""가격표 필수 열(이름 그대로). Volume은 있으면 그대로 둔다."""

INTERVALS: dict[int, str] = {60: "1m", 180: "3m", 300: "5m", 900: "15m", 1800: "30m", 3600: "1h", 7200: "2h",
                             14400: "4h", 21600: "6h", 28800: "8h", 43200: "12h", 86400: "1d", 259200: "3d",
                             604800: "1w"}
"""봉 길이(초) → 바이낸스 봉 단위."""

_EPS = 1e-9
"""비중 비교 여유."""

MAX_TRIALS = 10**12
"""TRIALS 설정 수 상한(시도 수 점검의 「샤프가 0 이하가 되는 시도 수」를 2^40까지 찾고, 더 큰 수는 계산 범위 밖)."""



class LinkError(ValueError):
    """연결 파일 · 가격표 · 신호 함수를 쓸 수 없을 때(점검을 못 돌림, 종료 코드 2)."""


class ExpressError(ValueError):
    """신호가 목표 비중(-1~1, 합 1 이하)으로 표현되지 않을 때(점검 불가 · 표현 불가, 종료 코드 0)."""


@dataclass(frozen=True)
class Link:
    """읽은 연결 파일.

    Attributes:
        name: 전략 이름.
        path: link.py 경로.
        repo: 레포 꼭대기(.blackclip의 부모).
        module: 불러온 연결 모듈.
        exec_: 체결 규칙.
        market: 바이낸스 시장.
        trades: 원래 거래 목록 경로(없으면 None).
        trades_label: 화면에 보일 레포 기준 경로.
        result_cagr: 고객 보고 연 수익률(없으면 None).
        trials: 고객이 밝힌 (평가한 설정 수, 근거)(없으면 None).
    """

    name: str
    path: Path
    repo: Path
    module: Any
    exec_: str
    market: str
    trades: Path | None
    trades_label: str | None
    result_cagr: float | None
    trials: tuple[int, str] | None = None


def check_trials(raw: Any) -> tuple[int, str] | None:
    """TRIALS 값을 확인한다(엔진 쪽에서도 하위 프로세스 응답을 다시 확인하려고 따로 둔다).

    Args:
        raw: 연결 파일의 TRIALS(또는 하위 프로세스가 돌려준 목록).

    Returns:
        (설정 수, 근거) 또는 None.

    Raises:
        LinkError: (1 ~ 10^12 정수, 비지 않은 글 120자까지) 꼴이 아니거나 근거에 절대 경로가 있을 때.
    """
    if raw is None:
        return None
    ok = (isinstance(raw, (tuple, list)) and len(raw) == 2 and isinstance(raw[0], int)
          and not isinstance(raw[0], bool) and 1 <= raw[0] <= MAX_TRIALS and isinstance(raw[1], str)
          and 0 < len(raw[1].strip()) <= 120)
    if not ok:
        raise LinkError('TRIALS는 (평가한 설정 수, "근거") 꼴입니다(설정 수는 1 이상 10^12 이하 정수, 근거는 120자까지)')
    odd = [ch for ch in raw[1].strip() if not ch.isprintable()]
    if any(unicodedata.category(ch) == "Cc" for ch in odd):
        raise LinkError("TRIALS 근거에 줄바꿈 · 탭 같은 제어 문자가 있습니다 · 한 줄 글로 적어 주세요")
    if odd:  # NBSP · ZWSP · BOM · ZWJ 같은 것은 제어 문자가 아니라 눈에 안 보이는 글자
        raise LinkError("TRIALS 근거에 보이지 않는 공백 · 서식 문자가 있습니다 · 보통 공백과 글자로 적어 주세요")
    if has_abs_path(raw[1]):
        raise LinkError("TRIALS 근거에 절대 경로가 있습니다 · 커밋되는 check.md에 남으므로 레포 기준 상대 경로나 말로 적어 주세요")
    return raw[0], raw[1].strip()


def load_link(root: Path, name: str) -> Link:
    """연결 파일을 불러와 계약을 확인한다.

    Args:
        root: .blackclip 경로.
        name: 전략 이름.

    Returns:
        Link.

    Raises:
        LinkError: 이름 · 파일 · 속성이 계약과 다를 때(레포 기준 경로를 넣은 한 줄).
    """
    if not NAME.fullmatch(name):
        raise LinkError(f"전략 이름: 영문 · 숫자 · 밑줄 · 하이픈 64자까지입니다({name})")
    repo = root.parent
    path = root / "strategies" / name / "link.py"
    rel = path.relative_to(repo).as_posix()
    if not exists_regular(root, f"strategies/{name}/link.py"):  # 상위 폴더 링크 · 링크는 일반 파일이 아님
        if path.is_symlink():
            raise LinkError(f"{rel}가 링크라 읽지 않습니다")
        raise LinkError(f"{rel}가 없습니다 · 고객 AI가 이 자리에 연결 코드(load_prices · signals · EXEC · MARKET)를 만듭니다")
    if str(repo) not in sys.path:
        sys.path.insert(0, str(repo))  # 연결 코드가 고객 레포의 모듈을 불러올 수 있게
    mod_name = f"blackclip_link_{name.replace('-', '_')}"
    spec = importlib.util.spec_from_file_location(mod_name, path)
    module = importlib.util.module_from_spec(spec)
    sys.modules[mod_name] = module  # dataclass 등이 sys.modules에서 자기 모듈을 찾는다
    try:
        spec.loader.exec_module(module)
    except Exception as exc:  # 고객 코드라 무엇이든 날 수 있다
        raise LinkError(f"{rel}: 불러오지 못했습니다({type(exc).__name__}: {str(exc)[:120]})") from None
    for attr in ("load_prices", "signals"):
        if not callable(getattr(module, attr, None)):
            raise LinkError(f"{rel}: {attr} 함수가 없습니다")
    exec_ = getattr(module, "EXEC", None)
    if exec_ not in EXECS:
        raise LinkError(f"{rel}: EXEC는 {' · '.join(EXECS)} 중 하나입니다({exec_!r})")
    market = getattr(module, "MARKET", None)
    if market not in MARKETS:
        raise LinkError(f"{rel}: MARKET은 {' · '.join(MARKETS)} 중 하나입니다({market!r})")
    label = getattr(module, "TRADES", None)
    trades = None
    if label is not None:
        if not isinstance(label, str) or Path(label).is_absolute():
            raise LinkError(f"{rel}: TRADES는 레포 꼭대기 기준 상대 경로 문자열입니다")
        trades = (repo / label).resolve()
        if repo.resolve() not in trades.parents:
            raise LinkError(f"{rel}: TRADES가 레포 밖을 가리킵니다({label})")
        if not trades.is_file():
            raise LinkError(f"{rel}: TRADES 파일이 없습니다({label})")
    cagr = getattr(module, "RESULT_CAGR", None)
    if cagr is not None and (isinstance(cagr, bool) or not isinstance(cagr, (int, float)) or not math.isfinite(cagr)):
        raise LinkError(f"{rel}: RESULT_CAGR는 소수입니다(연 18.2%면 0.182)")
    try:
        trials = check_trials(getattr(module, "TRIALS", None))
    except LinkError as exc:
        raise LinkError(f"{rel}: {exc}") from None
    return Link(name, path, repo, module, exec_, market, trades, label, None if cagr is None else float(cagr), trials)


def _utc(index: pd.DatetimeIndex) -> tuple[pd.DatetimeIndex, bool]:
    """시각을 UTC로 바꾼다.

    Args:
        index: 시각 인덱스.

    Returns:
        (UTC 인덱스, 시간대가 없었으면 True).
    """
    if index.tz is None:
        return index.tz_localize("UTC"), True
    return index.tz_convert("UTC"), False


def _symbol(raw: Any, where: str) -> str:
    """종목 이름을 정규화한다.

    Args:
        raw: 원래 이름.
        where: 오류 문구 앞머리.

    Returns:
        정규화한 종목.
    """
    sym = normalize_symbol(str(raw))
    if not sym:
        raise LinkError(f"{where}: 종목 이름이 비었습니다({raw!r})")
    return sym


def check_prices(raw: Any) -> tuple[dict[str, pd.DataFrame], bool]:
    """load_prices() 결과를 확인해 종목 → OHLC 표(float, UTC 시각 오름차순)로 돌려준다.

    Args:
        raw: load_prices()가 돌려준 것.

    Returns:
        (종목(정규화) → DataFrame[Open, High, Low, Close(, Volume)], 시간대가 없었으면 True).

    Raises:
        LinkError: 모양 · 시각 · 가격이 계약과 다를 때(종목과 첫 시각을 넣은 한 줄).
    """
    if not isinstance(raw, dict) or not raw:
        raise LinkError("load_prices(): {종목: 가격표 DataFrame} dict를 돌려줘야 합니다")
    out: dict[str, pd.DataFrame] = {}
    naive = False
    for key, df in raw.items():
        sym = _symbol(key, "load_prices()")
        if sym in out:
            raise LinkError(f"load_prices(): {key}가 다른 종목과 같은 이름({sym})이 됩니다")
        if not isinstance(df, pd.DataFrame):
            raise LinkError(f"load_prices() {key}: pandas DataFrame이 아닙니다")
        missing = [c for c in COLUMNS if c not in df.columns]
        if missing:
            raise LinkError(f"load_prices() {key}: 열이 없습니다({', '.join(missing)}) · 열 이름은 Open · High · Low · Close(대문자 시작)")
        if not isinstance(df.index, pd.DatetimeIndex):
            raise LinkError(f"load_prices() {key}: 시각 인덱스(DatetimeIndex)가 아닙니다")
        t = df[list(COLUMNS) + (["Volume"] if "Volume" in df.columns else [])].copy()
        t.index, was_naive = _utc(t.index)
        naive = naive or was_naive
        if t.index.isna().any():  # NaT는 아래 중복 · 역순 비교(<)가 False라 그냥 지나간다
            raise LinkError(f"load_prices() {key}: 시각이 비어 있는 행이 있습니다(NaT)")
        if len(t) < 2:
            raise LinkError(f"load_prices() {key}: 봉이 2개보다 적습니다")
        dup = t.index[t.index.duplicated()]
        if len(dup):
            raise LinkError(f"load_prices() {key}: 같은 시각이 두 번 있습니다({dup[0]:%Y-%m-%d %H:%M})")
        back = t.index[1:][t.index[1:] < t.index[:-1]]
        if len(back):
            raise LinkError(f"load_prices() {key}: 시각이 거꾸로 갑니다({back[0]:%Y-%m-%d %H:%M})")
        try:
            t = t.astype(float)
        except (TypeError, ValueError):
            raise LinkError(f"load_prices() {key}: 가격이 숫자가 아닙니다") from None
        v = t[list(COLUMNS)].to_numpy()
        bad = ~(np.isfinite(v).all(axis=1) & (v > 0).all(axis=1))
        if bad.any():
            raise LinkError(f"load_prices() {key}: 가격이 비었거나 0 이하입니다({t.index[bad][0]:%Y-%m-%d %H:%M})")
        out[sym] = t
    return out, naive


def to_signals(prices: dict[str, pd.DataFrame], naive: bool) -> dict[str, pd.DataFrame]:
    """신호 함수에 넘길 가격표 복사본(고객이 준 꼴: 시간대가 없었으면 시간대를 뗀다).

    Args:
        prices: 종목 → 가격표(UTC).
        naive: check_prices가 돌려준 시간대 없음 표시.

    Returns:
        종목 → 복사본(고객 코드가 고쳐도 엔진 쪽 가격표는 그대로).
    """
    return {s: (p.tz_localize(None) if naive else p).copy() for s, p in prices.items()}


def check_weights(raw: Any, prices: dict[str, pd.DataFrame]) -> dict[str, pd.Series]:
    """signals() 결과를 확인해 종목 → 그 종목 가격표 시각에 맞춘 목표 비중으로 돌려준다(NaN은 0).

    Args:
        raw: signals()가 돌려준 것.
        prices: check_prices 결과.

    Returns:
        종목 → 비중 Series(가격표 시각, float).

    Raises:
        LinkError: 모양이 계약과 다를 때.
        ExpressError: 비중이 -1~1 밖 · 무한대 · 한 시점 절댓값 합 > 1일 때.
    """
    if isinstance(raw, pd.Series):
        if len(prices) != 1:
            raise LinkError("signals(): 종목이 여럿이면 DataFrame(열 = 종목)으로 돌려줘야 합니다")
        raw = raw.to_frame(next(iter(prices)))
    if not isinstance(raw, pd.DataFrame):
        raise LinkError("signals(): pandas DataFrame(열 = 종목, 값 = 목표 비중) 또는 Series가 아닙니다")
    if not isinstance(raw.index, pd.DatetimeIndex):
        raise LinkError("signals(): 시각 인덱스(DatetimeIndex)가 아닙니다")
    if raw.columns.has_duplicates:
        raise LinkError("signals(): 같은 종목 열이 두 번 있습니다")
    frame = raw.copy()
    frame.index = _utc(frame.index)[0]
    if frame.index.has_duplicates:
        raise LinkError("signals(): 같은 시각이 두 번 있습니다")
    out: dict[str, pd.Series] = {}
    for col in frame.columns:
        sym = _symbol(col, "signals()")
        if sym not in prices:
            raise LinkError(f"signals(): 가격표에 없는 종목입니다({col})")
        if sym in out:
            raise LinkError(f"signals(): {col}가 다른 열과 같은 종목({sym})이 됩니다")
        try:
            s = frame[col].astype(float).reindex(prices[sym].index)
        except (TypeError, ValueError):
            raise LinkError(f"signals() {col}: 숫자가 아닙니다") from None
        if s.isna().all() and frame[col].notna().any():
            raise LinkError(f"signals() {col}: 신호 시각이 가격표 시각과 하나도 맞지 않습니다")
        s = s.fillna(0.0)
        v = s.to_numpy()
        if not np.isfinite(v).all():
            raise ExpressError(f"{col}: 비중에 무한대가 있습니다")
        over = np.abs(v) > 1 + _EPS
        if over.any():
            raise ExpressError(f"{col}: 목표 비중이 -1~1 밖입니다({s.index[over][0]:%Y-%m-%d %H:%M})")
        out[sym] = s
    if not out:
        raise LinkError("signals(): 종목 열이 없습니다")
    total = pd.concat([s.abs() for s in out.values()], axis=1).fillna(0.0).sum(axis=1)
    lev = total > 1 + _EPS
    if lev.any():
        raise ExpressError(f"한 시점 목표 비중 절댓값 합이 1을 넘습니다({total.index[lev][0]:%Y-%m-%d %H:%M}, 레버리지)")
    return out


def bar_seconds(prices: dict[str, pd.DataFrame]) -> int:
    """봉 길이(초) = 첫 종목 시각 차이의 중앙값.

    Args:
        prices: check_prices 결과.

    Returns:
        초.
    """
    idx = next(iter(prices.values())).index
    return int((idx[1:] - idx[:-1]).median().total_seconds())

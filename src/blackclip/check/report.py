"""blackclip check: 연결 파일 → 다시 돌리기 → 점검 다섯(재현성 · 미래참조 · 거래비용 · 시도 수 · 국면) · 데이터 차이 · 추천 가드레일 → 화면 · check.md.

AI가 Bash로 돌리는 출력이라 색 · 표 없이 결론 → 근거 순서의 숫자 요약만 낸다.
"""
from __future__ import annotations

import hashlib
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any

from blackclip import __version__
from blackclip.check.backtest import simulate
from blackclip.check.c0 import c0
from blackclip.check.c1 import c1
from blackclip.check.c2 import CONSERVATIVE_BP, c2
from blackclip.check.c3 import c3, deflate
from blackclip.check.c4 import c4, regime_stats
from blackclip.check.datadiff import data_diff
from blackclip.check.guardrail import GuardrailError, build, prices_sha256, summary
from blackclip.check.link import INTERVALS, ExpressError, LinkError, bar_seconds
from blackclip.check.runner import ISOLATION_NOTE, Runner, SignalError
from blackclip.check.trades import round_trips
from blackclip.sim.warning_line import DEFAULT_SENSITIVITY
from blackclip.workspace import read_bytes

LABELS: dict[str, str] = {"C0": "재현성", "C1": "미래참조", "C2": "거래비용", "C3": "시도 수",
                          "C4": "국면", "DATA": "데이터 차이", "GUARD": "추천 가드레일"}
"""항목 키 → 화면 이름(이 순서로 찍는다). 고객 글에는 키를 쓰지 않고 이름만 쓴다."""

NAIVE_NOTE = "가격표에 시간대가 없어 UTC로 셈함(신호 함수에는 원래 꼴로 넘김)"
"""시간대 없는 가격표 메모."""

FUNDING_NOTE = "선물 펀딩비는 거래비용에 넣지 않았어요(이 버전)"
"""선물 시장 메모(거래비용은 수수료 · 스프레드 · 슬리피지만 셈)."""

DEAD_TEXT = "잘라 다시 돌리던 중 고객 코드를 돌리던 하위 프로세스가 끝남(시간 초과 또는 종료, 원문은 터미널에만)"
"""미래참조 점검 도중 하위 프로세스가 끝났을 때 근거."""

DEAD_AFTER = " · (이후 하위 프로세스가 끝남)"
"""미래참조가 이미 의심이던 뒤 하위 프로세스가 끝났을 때 근거 끝에 붙는 문구."""

EXPRESS_TEXT = "목표 비중 -1~1, 한 시점 합 1 이하로 나타낼 수 없는 신호"
"""표현 불가 근거(check.md에는 고정 문구만, 원문은 터미널에)."""


@dataclass
class CheckResult:
    """점검 한 번의 결과.

    Attributes:
        name: 전략 이름.
        header: 가격표 기간 · 종목 수 · 봉 · 다시 돌린 체결 수.
        items: 항목 키(LABELS) → {"state", "text"}.
        notes: 메모 줄.
        trades_label: 원래 거래 목록(레포 기준 경로) 또는 None.
        exec_: 체결 규칙.
        market: 바이낸스 시장.
        detail: 터미널(표준 오류)에만 보일 원문(check.md에는 넣지 않음) 또는 None.
        guardrail: 가드레일 숫자(build 결과 + target), 만들 수 없었으면 None(save가 씀).
        c1_dead: 미래참조 잘라 보기 도중 고객 코드 하위 프로세스가 끝났는지(save가 저장을 막음).
    """

    name: str
    header: str
    items: dict[str, dict[str, str]]
    notes: list[str]
    trades_label: str | None
    exec_: str
    market: str
    detail: str | None = None
    guardrail: dict | None = None
    c1_dead: bool = False


def run_check(root: Path, name: str, client_factory: Callable[[str], Any] | None) -> CheckResult:
    """점검을 돌린다. 고객 코드는 하위 프로세스에서만 돈다. 못 돌리는 문제는 LinkError로, 점검 결과는 상태로.

    Args:
        root: .blackclip 경로.
        name: 전략 이름.
        client_factory: 시장 → 공개 봉 클라이언트. None이면 데이터 차이를 건너뜀(--no-binance).

    Returns:
        CheckResult.

    Raises:
        LinkError: 연결 파일 · 가격표 · 신호 함수 · 원래 거래 목록을 쓸 수 없을 때.
        WorkspaceError: 해시를 셀 때 link.py의 상위 폴더가 링크이거나 1MiB를 넘을 때.
    """
    with Runner(root, name) as runner:
        link, prices = runner.link, runner.prices
        bar_s = bar_seconds(prices)
        notes = ([NAIVE_NOTE] if runner.naive else []) + ([FUNDING_NOTE] if link.market == "futures" else [])
        notes.append(ISOLATION_NOTE)
        start = min(p.index[0] for p in prices.values())
        end = max(p.index[-1] for p in prices.values())
        header = (f"가격표 {start:%Y-%m-%d} ~ {end:%Y-%m-%d} · {len(prices)}종목 · "
                  f"{INTERVALS.get(bar_s, f'{bar_s:,}초')} 봉")
        result = CheckResult(name, header, {}, notes, link.trades_label, link.exec_, link.market)
        dead: list[LinkError] = []

        def alive(cut: Any) -> dict[str, Any]:
            """runner.signals를 부르고, 하위 프로세스가 죽거나 시간 초과로 닫혔으면 그 오류를 남긴다.

            한 번 닫히면 남은 자를 곳은 닫힌 Runner를 부르지 않고 같은 오류를 던진다.

            Args:
                cut: 이 시각까지만 남긴 가격표로(Runner.signals(cut=…)).

            Returns:
                종목 → 목표 비중.
            """
            if dead:
                raise dead[0]
            try:
                return runner.signals(cut=cut)
            except LinkError as exc:
                if runner.proc.returncode is not None:
                    dead.append(exc)
                raise

        try:
            weights = runner.signals()
        except ExpressError as exc:
            result.items = {k: {"state": "점검 불가(표현 불가)", "text": EXPRESS_TEXT} for k in LABELS}
            result.detail = f"표현 불가: {exc}"
            return result
        except SignalError as exc:
            raise LinkError(f"signals(): 실패했습니다({exc})") from None
        run = simulate(prices, weights, link.exec_)
        result.header += f" · 다시 돌린 체결 {len(run.executions):,}건"
        result.items["C0"] = c0(link, prices, run, bar_s)
        if result.items["C0"]["state"] == "불일치":
            blocked = {"state": "점검 불가(재현 불일치)", "text": "재현성이 불일치라 같은 전략을 돌렸는지 알 수 없어요"}
            for key in ("C1", "C2", "C3", "C4", "GUARD"):
                result.items[key] = dict(blocked)
        else:
            result.items["C1"] = c1(link, prices, weights, runner.files, alive)
            if dead:  # 원문(stderr 꼬리)은 터미널에만, check.md에는 고정 문구
                if result.items["C1"]["state"] == "의심":  # 죽기 전에 이미 잡은 의심과 근거는 남김
                    result.items["C1"]["text"] += DEAD_AFTER
                else:
                    result.items["C1"] = {"state": "점검 불가(하위 프로세스가 끝남)", "text": DEAD_TEXT}
                result.detail = f"미래참조 잘라 보기 중: {dead[0]}"
            result.c1_dead = bool(dead)
            try:
                result.items["C2"] = c2(link, prices, weights, bar_s, runner.files)
            except OverflowError:  # 아주 짧은 기간에 큰 수익이면 연환산이 float를 넘는다(backtest.metrics)
                result.items["C2"] = {"state": "점검 불가(연 수익률을 셀 수 없음)",
                                      "text": f"수익률 {len(run.returns):,}봉 · 기간이 너무 짧아 연환산이 넘침"}
            try:
                result.items["C3"] = c3(run.returns, bar_s, link.trials)
                deflated = deflate(run.returns, bar_s, link.trials)
            except (IndexError, OverflowError):  # 수익률이 0~1봉이거나 기간이 너무 짧으면 샤프 · 표준오차를 못 셈
                deflated = None
                result.items["C3"] = {"state": "점검 불가(샤프를 셀 수 없음)",
                                      "text": f"비용 0 수익률 {len(run.returns):,}봉 · 기간이 너무 짧음"}
            trades = round_trips(prices, weights, link.exec_, CONSERVATIVE_BP)
            stats = regime_stats(trades, prices, bar_s)
            result.items["C4"] = c4(stats)
            if deflated is None:
                result.items["GUARD"] = {"state": "점검 불가(시도 수를 셀 수 없음)", "text": "blackclip save가 저장하지 않음"}
            else:
                try:
                    g = build(simulate(prices, weights, link.exec_, CONSERVATIVE_BP).returns, trades, stats, bar_s,
                              deflated)
                except GuardrailError as exc:
                    result.items["GUARD"] = {"state": f"점검 불가({exc})", "text": "blackclip save가 저장하지 않음"}
                else:
                    g["target"] = {"market": link.market, "exec": link.exec_, "symbols": sorted(prices),
                                   "bar_seconds": bar_s, "start": f"{start:%Y-%m-%d}", "end": f"{end:%Y-%m-%d}",
                                   "link_sha256": hashlib.sha256(read_bytes(root, f"strategies/{name}/link.py", 1 << 20)).hexdigest(),
                                   "prices_sha256": prices_sha256(prices)}
                    result.guardrail = g
                    result.items["GUARD"] = {"state": f"민감도 {DEFAULT_SENSITIVITY}",
                                             "text": " · ".join(summary(g, DEFAULT_SENSITIVITY))
                                             + (" · (저장 안 됨: 미래참조 점검 중 하위 프로세스가 끝남)" if dead else "")}
        if client_factory is None:
            result.items["DATA"] = {"state": "건너뜀", "text": "--no-binance"}
        elif dead:
            result.items["DATA"] = {"state": "확인 불가", "text": "하위 프로세스가 끝나 바이낸스 봉으로 신호를 다시 돌릴 수 없음"}
        else:
            result.items["DATA"] = data_diff(link, prices, run, bar_s, client_factory(link.market),
                                             lambda frames: runner.signals(frames=frames))
    result.items = {k: result.items[k] for k in LABELS}
    return result


def first_look(items: dict[str, dict[str, str]]) -> str | None:
    """먼저 볼 항목(확인 순서: 재현성 불일치 → 미래참조 의심 → 거래비용 소멸 · 축소 → 시도 수 하향 → 국면 편중 · 부족 → 점검 불가(추천 가드레일 포함)).

    시도 수 「모름」은 시도 기록 전이면 늘 나와서 먼저 볼 항목으로 고르지 않는다.

    Args:
        items: 항목 결과.

    Returns:
        화면 이름 또는 None(볼 항목 없음).
    """
    for key, states in (("C0", ("불일치",)), ("C1", ("의심",)), ("C2", ("소멸", "축소")), ("C3", ("하향(범위)",)),
                        ("C4", ("국면 편중", "거래 부족"))):
        if items[key]["state"] in states:
            return LABELS[key]
    for key in ("C0", "C1", "C2", "C3", "C4", "GUARD"):
        if items[key]["state"].startswith("점검 불가"):
            return LABELS[key]
    return None


def render(result: CheckResult) -> str:
    """터미널 화면(AI가 읽는 요약, 색 없음).

    Args:
        result: 점검 결과.

    Returns:
        여러 줄 문자열.
    """
    first = first_look(result.items)
    lines = [f"점검 · {result.name} · {result.header}", f"먼저 볼 항목: {first}" if first else "먼저 볼 항목 없음"]
    lines += [f"{LABELS[k]}  {v['state']} · {v['text']}" for k, v in result.items.items()]
    lines += [f"메모: {n}" for n in result.notes]
    return "\n".join(lines)


def render_md(result: CheckResult, now: datetime) -> str:
    """check.md 내용(커밋됨: 절대 경로 없음, 표 칸의 |는 이스케이프).

    Args:
        result: 점검 결과.
        now: 점검 시각(UTC).

    Returns:
        마크다운.
    """
    first = first_look(result.items)

    def cell(s: str) -> str:
        """표 칸 하나.

        Args:
            s: 내용.

        Returns:
            홈 폴더 경로를 ~로 바꾸고 |를 이스케이프한 내용.
        """
        return s.replace(str(Path.home()), "~").replace("|", "\\|")

    out = [f"# 점검 · {result.name}", "",
           f"- 점검 시각: {now:%Y-%m-%d %H:%M} UTC · blackclip {__version__}",
           f"- {result.header} · 체결 규칙 {result.exec_} · 시장 {result.market}",
           f"- 원래 거래 목록: {result.trades_label or '없음'}",
           f"- 먼저 볼 항목: {first or '없음'}", "",
           "| 점검 | 상태 | 근거 |", "| --- | --- | --- |"]
    out += [f"| {LABELS[k]} | {cell(v['state'])} | {cell(v['text'])} |" for k, v in result.items.items()]
    out.append("")
    out += [f"- 메모: {n}" for n in result.notes]
    return "\n".join(out) + "\n"

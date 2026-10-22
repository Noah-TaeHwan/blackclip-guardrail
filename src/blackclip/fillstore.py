"""받은 체결 · 펀딩비를 .blackclip/fills/ 아래 CSV에 쌓고, 종목마다 어디까지 받았는지(coverage.json) 적는다."""
from __future__ import annotations

import csv
import io
import json
from datetime import datetime
from pathlib import Path

from blackclip.workspace import write_atomic

COVERAGE = "coverage.json"
"""받은 범위 파일 이름. {종목: {"from": ISO, "until": ISO}}, until은 제외."""

FUNDING_KEY = "_funding"
"""받은 범위에서 펀딩비를 가리키는 이름(종목 이름과 안 겹치게 밑줄로 시작)."""


def merge_csv(path: Path, columns: tuple[str, ...], rows: list[dict[str, str]], key: tuple[str, ...]) -> int:
    """기존 CSV에 새 줄을 더한다. key가 같은 줄은 한 번만 남기고 시각 순으로 다시 쓴다.

    Args:
        path: CSV 경로(없으면 만든다).
        columns: 열 순서.
        rows: 더할 줄.
        key: 중복을 가르는 열.

    Returns:
        새로 더한 줄 수.

    Raises:
        ValueError: 기존 파일의 열이 다르거나 csv 모듈이 읽지 못할 때(파일 쓰기 전에 멈춤).
    """
    existing: list[dict[str, str]] = []
    if path.exists():
        try:
            with open(path, newline="", encoding="utf-8") as f:
                reader = csv.DictReader(f)
                if tuple(reader.fieldnames or ()) != tuple(columns):
                    raise ValueError(f"{path}: 열이 다릅니다(예상 {','.join(columns)})")
                existing = list(reader)
        except csv.Error as exc:  # ValueError가 아니라 호출자가 못 잡음(칸이 너무 김 등), 경로 없이 종류만
            raise ValueError(f"{path.name}: CSV를 읽을 수 없습니다({type(exc).__name__})") from None
    seen = {tuple(r[c] for c in key) for r in existing}
    added = []
    for r in rows:
        k = tuple(r[c] for c in key)
        if k not in seen:
            seen.add(k)
            added.append(r)
    if not added and path.exists():
        return 0
    buf = io.StringIO()
    w = csv.DictWriter(buf, fieldnames=list(columns), lineterminator="\n")
    w.writeheader()
    w.writerows(sorted(existing + added, key=lambda r: tuple(r[c] for c in ("time", *key))))
    write_atomic(path, buf.getvalue())
    return len(added)


def read_coverage(d: Path) -> dict[str, dict[str, str]]:
    """받은 범위를 읽는다.

    Args:
        d: 시장 폴더(.blackclip/fills/binance-main/spot).

    Returns:
        {이름: {"from", "until"}}. 파일이 없으면 빈 dict.

    Raises:
        ValueError: JSON이 깨졌거나 최상위가 객체가 아닐 때, 항목 모양이 다를 때(파일 이름 포함).
    """
    p = d / COVERAGE
    data = json.loads(p.read_text(encoding="utf-8")) if p.exists() else {}
    if not isinstance(data, dict):
        raise ValueError(f"{p}: 최상위가 객체({{…}})가 아닙니다")
    for name, span in data.items():
        if not (isinstance(span, dict) and isinstance(span.get("from"), str) and isinstance(span.get("until"), str)):
            raise ValueError(f"{p}: {name} 항목 모양이 다릅니다(from · until 시각 문자열)")
    return data


def update_coverage(d: Path, name: str, start_iso: str, end_iso: str) -> None:
    """받은 범위를 적는다. 새로 받은 시작이 옛 범위 안이면 이어 붙이고, 아니면(빈틈) 새 범위로 바꾼다.

    Args:
        d: 시장 폴더.
        name: 종목 또는 FUNDING_KEY.
        start_iso: 이번에 받은 시작(포함).
        end_iso: 이번에 받은 끝(제외).
    """
    cov = read_coverage(d)
    old = cov.get(name)
    if old and old["from"] <= start_iso <= old["until"]:
        cov[name] = {"from": old["from"], "until": max(old["until"], end_iso)}
    else:
        cov[name] = {"from": start_iso, "until": end_iso}
    write_atomic(d / COVERAGE, json.dumps(cov, indent=2, sort_keys=True) + "\n")


def coverage_span(cov: dict[str, dict[str, str]], symbols: list[str]) -> tuple[datetime, datetime] | None:
    """종목 모두를 빈틈없이 받은 범위.

    Args:
        cov: read_coverage 결과.
        symbols: 확인할 종목.

    Returns:
        (가장 늦은 처음, 가장 이른 끝). 종목이 없거나 하나라도 범위에 없으면 None.
    """
    if not symbols or any(s not in cov for s in symbols):
        return None
    return (max(datetime.fromisoformat(cov[s]["from"]) for s in symbols),
            min(datetime.fromisoformat(cov[s]["until"]) for s in symbols))

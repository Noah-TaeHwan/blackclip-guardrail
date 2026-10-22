"""공개 샘플 데모(guardrail.blackclip.app/demo/)를 그릴 지어낸 레포. 같은 인자로 다시 돌리면 같은 바이트가 나온다(날짜 · 숫자 모두 고정값).

쓰는 법(레포 폴더에서): `uv run python demo/sample.py <만들 폴더> [--scratch <폴더>]`

- `<만들 폴더>`: 없거나 비어 있거나 이 스크립트가 만든 폴더(`.blackclip-demo` 표지가 있음)일 때만 쓴다. 그 밖의 폴더는 지우지 않고 멈춘다.
- `--scratch`: 가짜 `security`(부르면 `security.log`에 한 줄 쓰고 종료 1)를 둘 폴더. 기본은 `<만들 폴더>`의 부모. 만드는 동안만 PATH 맨 앞에 둔다
  (끝나고 `<scratch>/security.log`가 0줄이어야 한다).

시연 흐름: 홈 → trend_10_50(실전 · 실행 경고, 10/07부터 새 구간) → grid_v1(중지 → 은퇴, 끈 뒤 회고). 상태 기록은 CLI(`date.today()`) 대신
`record.write_record`에 날을 고정해 부른다. 개발용 스크립트라 패키지에 넣지 않고 `tests/facts.py`를 그대로 쓴다.
"""
import argparse
import json
import os
import shutil
import stat
import sys
from collections.abc import Callable
from datetime import date
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO / "tests"))

MARK = ".blackclip-demo"
"""이 스크립트가 만든 폴더의 표지 파일 이름(다시 돌릴 때 지워도 되는 폴더인지 가름)."""

SPLIT_FROM = 15
"""trend_10_50 구간 둘: 실전 이 날째(2026-10-07)부터 새 구간(period_start)."""

STOPS: tuple[tuple[str, str, str, str | None, date], ...] = (
    ("trend_ema_v7", "중지", "거래가 너무 적어요", None, date(2026, 10, 12)),
    ("grid_v1", "중지", "전략이 안 맞아요", "다음 달에 다시 봐요", date(2026, 10, 10)),
    ("grid_v1", "은퇴", "다시 쓰지 않기로 했어요", "접어요", date(2026, 10, 13)),
)
"""(전략, 상태, 이유, 결정, 날). 모두 기준일(실전 전략의 마지막 기록 2026-10-14)보다 앞이다(끈 전략 「시작 날」이 기준일 뒤면 안 됨)."""


def fake_security(scratch: Path) -> tuple[Path, str]:
    """부르면 로그에 한 줄 쓰고 종료 1인 가짜 `security`를 만들어 PATH 맨 앞에 둔다.

    Args:
        scratch: 가짜 명령 · 로그를 둘 폴더.

    Returns:
        (로그 파일 경로, 되돌릴 옛 PATH).
    """
    fakebin = scratch / "fakebin"
    fakebin.mkdir(parents=True, exist_ok=True)
    log = scratch / "security.log"
    log.touch()
    sec = fakebin / "security"
    sec.write_text(f'#!/bin/sh\necho "called $*" >> "{log}"\nexit 1\n', encoding="utf-8")
    sec.chmod(sec.stat().st_mode | stat.S_IXUSR)
    old = os.environ.get("PATH", "")
    os.environ["PATH"] = f"{fakebin}{os.pathsep}{old}"
    return log, old


def clean(repo: Path) -> None:
    """만들 폴더를 비운다. 이 스크립트가 만든 폴더(표지 있음)이거나 비어 있을 때만 지운다.

    Args:
        repo: 만들 폴더.

    Raises:
        SystemExit: 표지 없는 비어 있지 않은 폴더(아무것도 지우지 않음).
    """
    if repo.exists():
        if any(repo.iterdir()) and not (repo / MARK).exists():
            raise SystemExit(f"{repo}: 비어 있지 않고 {MARK} 표지가 없어 지우지 않았어요")
        shutil.rmtree(repo)
    repo.mkdir(parents=True)
    (repo / MARK).write_text("demo/sample.py가 만든 지어낸 레포\n", encoding="utf-8")


def rewrite_days(path: Path, fn: Callable[[dict], dict | None]) -> None:
    """days.jsonl을 읽어 기록마다 fn으로 고쳐 다시 쓴다(fn이 None이면 그 기록을 뺀다).

    Args:
        path: days.jsonl 경로.
        fn: 기록 dict 하나를 받아 고친 dict 또는 None을 돌려주는 함수.
    """
    recs = [fn(json.loads(x)) for x in path.read_text(encoding="utf-8").splitlines()]
    path.write_text("".join(json.dumps(r, ensure_ascii=False) + "\n" for r in recs if r is not None), encoding="utf-8")


def split_period(rec: dict) -> dict:
    """10/07째부터의 기록을 「이번 구간 = 10/07부터」 값으로 바꾼다(전체 값 `nums.all`은 그대로).

    d = 구간 일수, 실제 +1.0%p(앞 구간 손익을 덜어냄), 수수료 · 체결가 차이 · 나머지 · 차이는 절반, 같은 기간 백테스트 = 실제 − 차이.

    Args:
        rec: days.jsonl 기록 하나.

    Returns:
        고친 기록(10/07 앞 날은 그대로).
    """
    n = rec["nums"]
    if rec["live_day"] < SPLIT_FROM or n.get("actual") is None:
        return rec
    n["period_start"], n["d"] = "2026-10-07", rec["live_day"] - SPLIT_FROM + 1
    n["actual"] = round(n["actual"] + 0.01, 6)
    for k in ("fee", "slip", "rest", "gap"):
        n[k] = round(n[k] * 0.5, 6)
    n["bt"] = n["bt0"] = round(n["actual"] - n["gap"], 6)
    return rec


def build_sample(repo: Path, scratch: Path) -> Path:
    """지어낸 레포를 처음부터 만든다. 만드는 동안만 가짜 `security`를 PATH 맨 앞에 둔다.

    Args:
        repo: 만들 폴더.
        scratch: 가짜 security · 로그를 둘 폴더.

    Returns:
        `.blackclip` 경로.

    Raises:
        SystemExit: 만들 폴더가 이 스크립트가 만든 폴더가 아님.
    """
    from facts import write_facts

    from blackclip.record import write_record

    clean(repo)
    _, old = fake_security(scratch)
    try:
        root = write_facts(repo)
        s = root / "strategies"
        rewrite_days(s / "trend_10_50" / "daily" / "days.jsonl", split_period)
        for name, state, reason, decision, day in STOPS:
            if state == "중지":  # 끈 날부터는 매일 대조가 멈춘다(끈 뒤 날의 기록이 없게)
                cut = day.isoformat()
                rewrite_days(s / name / "daily" / "days.jsonl", lambda r, cut=cut: r if r["day"] < cut else None)
            write_record(root, name, state, reason, decision, day)
    finally:
        os.environ["PATH"] = old
    return root


def main() -> None:
    """인자를 읽어 레포를 만들고 `.blackclip` 경로를 찍는다."""
    p = argparse.ArgumentParser(description="공개 샘플 데모의 지어낸 레포(재현 가능)")
    p.add_argument("repo", type=Path, help="만들 폴더")
    p.add_argument("--scratch", type=Path, help="가짜 security 폴더(기본: 만들 폴더의 부모)")
    a = p.parse_args()
    repo = a.repo.resolve()
    print(build_sample(repo, (a.scratch or repo.parent).resolve()))


if __name__ == "__main__":
    main()

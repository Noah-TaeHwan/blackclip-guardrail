"""점검 하위 프로세스: 고객의 연결 코드 · 신호 함수는 여기서만 돈다.

엔진 프로세스와는 JSON 한 줄 요청 · 응답과, 임시 폴더의 npz(피클 없음) 파일로만 주고받는다.
고객 코드가 print해도 응답 통로가 섞이지 않게 처음에 표준 출력을 표준 오류로 돌린다.
"""
from __future__ import annotations

import json
import os
import sys
from pathlib import Path
from typing import Any

import numpy as np
import pandas as pd

from blackclip.check.link import ExpressError, LinkError, check_prices, check_weights, load_link, to_signals
from blackclip.check.source import source_files


def save_frames(path: Path, frames: dict[str, pd.DataFrame]) -> None:
    """종목 → 표를 npz 하나로 쓴다(시각 · 열 이름 · 숫자만, 피클 없음).

    Args:
        path: 쓸 파일.
        frames: 종목 → DataFrame(UTC 시각 인덱스, 숫자 열).
    """
    arrays: dict[str, np.ndarray] = {"syms": np.array(list(frames), dtype=str)}
    for i, df in enumerate(frames.values()):
        arrays[f"t{i}"] = df.index.tz_convert(None).as_unit("ns").to_numpy()
        arrays[f"c{i}"] = np.array([str(c) for c in df.columns], dtype=str)
        arrays[f"v{i}"] = df.to_numpy(dtype=float)
    with open(path, "wb") as f:
        np.savez(f, **arrays)


def load_frames(path: Path) -> dict[str, pd.DataFrame]:
    """save_frames로 쓴 파일을 읽는다(allow_pickle=False).

    Args:
        path: 읽을 파일.

    Returns:
        종목 → DataFrame(UTC 시각 인덱스).
    """
    with np.load(path, allow_pickle=False) as z:
        return {s: pd.DataFrame(z[f"v{i}"], index=pd.DatetimeIndex(z[f"t{i}"]).tz_localize("UTC"),
                                columns=z[f"c{i}"].tolist())
                for i, s in enumerate(z["syms"].tolist())}


def cut_frames(frames: dict[str, pd.DataFrame], cut: pd.Timestamp | None) -> dict[str, pd.DataFrame]:
    """cut 시각까지만 남긴 표(봉이 하나도 안 남는 종목은 뺀다). 엔진도 worker 응답을 확인할 때 같은 모양을 쓴다.

    Args:
        frames: 종목 → 표(UTC).
        cut: 남길 마지막 시각(포함). None이면 그대로.

    Returns:
        종목 → 잘린 표.
    """
    if cut is None:
        return frames
    return {s: p.loc[:cut] for s, p in frames.items() if len(p.loc[:cut])}


def _handle(req: dict[str, Any], repo: Path, name: str, tmp: Path, state: dict[str, Any]) -> dict[str, Any]:
    """요청 하나를 처리한다.

    Args:
        req: {"op": "load"} 또는 {"op": "signals", "cut": ISO 시각(선택), "frames": npz 이름(선택)}.
        repo: 레포 꼭대기.
        name: 전략 이름.
        tmp: 주고받는 임시 폴더.
        state: 요청 사이에 남기는 것(연결 모듈 · 가격표).

    Returns:
        응답 dict.
    """
    if req["op"] == "load":
        link = load_link(repo / ".blackclip", name)
        try:
            raw = link.module.load_prices()
        except Exception as exc:  # 고객 코드라 무엇이든 날 수 있다
            raise LinkError(f"load_prices(): 실패했습니다({type(exc).__name__}: {str(exc)[:120]})") from None
        prices, naive = check_prices(raw)
        state.update(link=link, prices=prices, naive=naive)
        save_frames(tmp / "prices.npz", prices)
        base = repo.resolve()
        return {"exec": link.exec_, "market": link.market, "trades": link.trades_label, "result_cagr": link.result_cagr,
                "trials": link.trials,
                "naive": naive, "files": [p.relative_to(base).as_posix() for p in source_files(link)],
                "prices": "prices.npz"}
    link, naive = state["link"], state["naive"]
    frames = load_frames(tmp / req["frames"]) if req.get("frames") else state["prices"]
    frames = cut_frames(frames, pd.Timestamp(req["cut"]) if req.get("cut") else None)
    weights = check_weights(link.module.signals(to_signals(frames, naive)), frames)
    save_frames(tmp / "weights.npz", {s: w.to_frame("w") for s, w in weights.items()})
    return {"weights": "weights.npz"}


def main(argv: list[str]) -> int:
    """요청을 한 줄씩 받아 처리한다. 인자: 레포 꼭대기 · 전략 이름 · 임시 폴더.

    Args:
        argv: 명령 인자.

    Returns:
        종료 코드.
    """
    reply = os.fdopen(os.dup(1), "w", encoding="utf-8")
    os.dup2(2, 1)  # 고객 코드의 print는 표준 오류로
    sys.stdout = sys.stderr
    repo, name, tmp = Path(argv[0]), argv[1], Path(argv[2])
    state: dict[str, Any] = {}
    for line in sys.stdin:
        try:
            resp = _handle(json.loads(line), repo, name, tmp, state)
        except LinkError as exc:
            resp = {"error": str(exc), "kind": "link"}
        except ExpressError as exc:
            resp = {"error": str(exc), "kind": "express"}
        except Exception as exc:  # 고객 코드라 무엇이든 날 수 있다
            resp = {"error": f"{type(exc).__name__}: {str(exc)[:120]}", "kind": "crash", "type": type(exc).__name__}
        reply.write(json.dumps(resp, ensure_ascii=False) + "\n")
        reply.flush()
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))

"""엔진 쪽: 고객 코드를 하위 프로세스(환경 변수 비움)에서 돌리고 숫자만 받는다.

하위 프로세스는 같은 사용자 권한으로 돈다(환경 변수 상속만 막고, 파일 · 키체인 접근과 네트워크는 막지 못한다 · 그 점은 화면 메모로 알린다).
"""
from __future__ import annotations

import json
import os
import queue
import shutil
import signal
import subprocess
import sys
import tempfile
import threading
import zipfile
from pathlib import Path
from typing import Any

import numpy as np
import pandas as pd

from blackclip.check.link import EXECS, MARKETS, ExpressError, Link, LinkError, check_trials, check_weights
from blackclip.check.worker import cut_frames, load_frames, save_frames

ENV: dict[str, str] = {"PYTHONIOENCODING": "utf-8", "PYTHONUTF8": "1", "PYTHONDONTWRITEBYTECODE": "1"}
"""하위 프로세스에 넘기는 환경 변수 전부(키 · 토큰이 든 엔진 환경을 넘기지 않는다)."""

TIMEOUT = 600.0
"""요청 하나를 기다리는 초."""

ISOLATION_NOTE = "고객 코드: 하위 프로세스 · 환경 변수 상속 차단 · 같은 사용자 권한(파일 · 키체인은 막지 못함) · 네트워크 차단 안 함(이 버전)"
"""「안 되면 안 됐다고 표시」 메모(막은 것과 못 막은 것을 그대로 적는다)."""

BAD_REPLY = "고객 코드를 돌리던 하위 프로세스의 응답이 계약과 다릅니다"
"""하위 프로세스 응답을 읽지 못했거나 값이 계약 밖일 때의 한 줄."""


class SignalError(RuntimeError):
    """고객 신호 함수가 죽음.

    Attributes:
        type_name: 고객 쪽 예외 이름(check.md에는 이것만 적는다).
    """

    def __init__(self, message: str, type_name: str) -> None:
        """Args:
            message: 예외 이름과 메시지.
            type_name: 예외 이름.
        """
        super().__init__(message)
        self.type_name = type_name


class Runner:
    """전략 하나의 하위 프로세스. with 문으로 쓴다.

    Attributes:
        link: 연결 파일 정보(module은 None, 고객 모듈은 하위 프로세스에만 있다).
        files: 정적 검사할 레포 안 파일.
        prices: 종목 → 가격표(UTC).
        naive: 고객 가격표에 시간대가 없었으면 True.
    """

    def __init__(self, root: Path, name: str, timeout: float = TIMEOUT) -> None:
        """하위 프로세스를 띄우고 연결 파일 · 가격표를 읽힌다.

        Args:
            root: .blackclip 경로.
            name: 전략 이름.
            timeout: 요청 하나를 기다리는 초.

        Raises:
            LinkError: 연결 파일 · 가격표가 계약과 다르거나 하위 프로세스가 죽었을 때.
        """
        repo = root.parent.resolve()
        self.repo = repo
        self.timeout = timeout
        self.tmp = Path(tempfile.mkdtemp(prefix="blackclip-check-")).resolve()
        self._err = open(self.tmp / "stderr.txt", "w", encoding="utf-8")
        self._lines: queue.Queue[str | None] = queue.Queue()
        # -P: cwd(=고객 레포)를 sys.path 앞에 넣지 않는다(레포 꼭대기의 random.py 등이 표준 모듈을 가리지 않게)
        # start_new_session: 고객 코드가 띄운 손자 프로세스까지 프로세스 그룹으로 한꺼번에 끝내려고
        self.proc = subprocess.Popen([sys.executable, "-P", "-m", "blackclip.check.worker", str(repo), name, str(self.tmp)],
                                     cwd=repo, env=ENV, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                                     stderr=self._err, text=True, encoding="utf-8", start_new_session=True)
        threading.Thread(target=self._pump, daemon=True).start()
        try:
            meta = self._call({"op": "load"})
            self.link, self.files, self.naive = self._link(meta, root.name, name)
            self.prices = self._frames(meta.get("prices"))
        except BaseException:
            self.close()
            raise

    def _link(self, meta: dict[str, Any], root_name: str, name: str) -> tuple[Link, list[Path], bool]:
        """load 응답을 확인해 Link · 정적 검사 파일 · naive로 만든다(경로는 레포 안인지 확인).

        Args:
            meta: load 응답.
            root_name: .blackclip 폴더 이름.
            name: 전략 이름.

        Returns:
            (Link, 레포 안 파일 경로 목록, naive).

        Raises:
            LinkError: 값이 없거나 계약 밖이거나 경로가 레포 밖일 때.
        """
        try:
            exec_, market, label, cagr = meta["exec"], meta["market"], meta["trades"], meta["result_cagr"]
            naive = meta["naive"]
            trials = check_trials(meta.get("trials"))  # LinkError는 ValueError라 아래에서 BAD_REPLY가 된다
            files = [self._inside(self.repo, f) for f in meta["files"]] if isinstance(meta["files"], list) else None
            ok = (files is not None and exec_ in EXECS and market in MARKETS and isinstance(naive, bool)
                  and (cagr is None or isinstance(cagr, float)) and (label is None or isinstance(label, str)))
        except (KeyError, TypeError, ValueError):
            ok = False
        if not ok:
            raise LinkError(BAD_REPLY)
        trades = None if label is None else self._inside(self.repo, label)
        link = Link(name, self.repo / root_name / "strategies" / name / "link.py", self.repo, None, exec_, market,
                    trades, label, cagr, trials)
        return link, files, naive

    def _inside(self, base: Path, rel: object) -> Path:
        """응답의 상대 경로를 base 안의 풀어 쓴 경로로 바꾼다.

        Args:
            base: 풀어 쓴 기준 폴더(레포 또는 임시 폴더).
            rel: 하위 프로세스가 보낸 경로 문자열.

        Returns:
            base 안의 절대 경로.

        Raises:
            LinkError: 문자열이 아니거나 base 밖(.. · 절대 경로 · 심볼릭 링크)을 가리킬 때.
        """
        path = (base / rel).resolve() if isinstance(rel, str) else None
        if path is None or base not in path.parents:
            raise LinkError(BAD_REPLY)
        return path

    def _frames(self, name: object) -> dict[str, pd.DataFrame]:
        """임시 폴더의 npz 하나를 읽는다.

        Args:
            name: 하위 프로세스가 알려 준 파일 이름.

        Returns:
            종목 → 표.

        Raises:
            LinkError: 임시 폴더 밖이거나 npz 형식이 아닐 때.
        """
        path = self._inside(self.tmp, name)
        try:
            return load_frames(path)
        except (OSError, KeyError, TypeError, ValueError, zipfile.BadZipFile):
            raise LinkError(BAD_REPLY) from None

    def _pump(self) -> None:
        """하위 프로세스 응답 줄을 큐로 옮긴다(끝나면 None)."""
        for line in self.proc.stdout:
            self._lines.put(line)
        self._lines.put(None)

    def _call(self, req: dict[str, Any]) -> dict[str, Any]:
        """요청 하나를 보내고 응답을 기다린다.

        Args:
            req: 요청.

        Returns:
            응답.

        Raises:
            LinkError · ExpressError · SignalError: 하위 프로세스가 보낸 오류 종류대로.
        """
        try:
            self.proc.stdin.write(json.dumps(req) + "\n")
            self.proc.stdin.flush()
            line = self._lines.get(timeout=self.timeout)
        except BrokenPipeError:
            line = None
        except queue.Empty:
            self.close()
            raise LinkError(f"고객 코드가 {self.timeout:.0f}초 안에 끝나지 않았습니다") from None
        if line is None:
            self._reap()
            raise LinkError("고객 코드를 돌리던 하위 프로세스가 끝났습니다(고객 코드가 프로세스를 끝냈거나 죽음)" + self._stderr_tail())
        resp = kind = message = type_name = None
        try:
            resp = json.loads(line)
            kind = resp["kind"] if "error" in resp else None
            if kind is not None:
                message, type_name = str(resp["error"]), str(resp.get("type", "Exception"))
        except (ValueError, KeyError, TypeError):
            resp = None
        if not isinstance(resp, dict) or kind not in (None, "link", "express", "crash"):
            raise LinkError(BAD_REPLY)
        if kind == "link":
            raise LinkError(message)
        if kind == "express":
            raise ExpressError(message)
        if kind == "crash":
            raise SignalError(message, type_name)
        return resp

    def _stderr_tail(self) -> str:
        """하위 프로세스가 마지막에 쓴 표준 오류 몇 줄(엔진 쪽 경로는 가림).

        Returns:
            " · 마지막 출력: ..." 꼴 문자열. 읽을 게 없으면 빈 문자열.
        """
        try:
            text = (self.tmp / "stderr.txt").read_text(encoding="utf-8", errors="replace")
        except OSError:
            return ""
        for raw, mark in ((str(self.tmp), "<임시>"), (str(self.repo), "."), (str(Path.home()), "~")):
            if len(raw) > 1:
                text = text.replace(raw, mark)  # 줄을 자르기 전에 가린다(절대 경로가 잘려 남지 않게)
        lines = [ln.strip()[:200] for ln in text.splitlines() if ln.strip()][-5:]
        return f" · 마지막 출력: {' | '.join(lines)}" if lines else ""

    def signals(self, cut: pd.Timestamp | None = None,
                frames: dict[str, pd.DataFrame] | None = None) -> dict[str, pd.Series]:
        """신호 함수를 돌려 확인한 목표 비중을 받는다.

        Args:
            cut: 이 시각까지만 남긴 가격표로(미래참조 잘라 보기).
            frames: 고객 가격표 대신 이 표로(데이터 차이의 바이낸스 봉).

        Returns:
            종목 → 목표 비중(가격표 시각).

        Raises:
            ExpressError: 응답 비중이 NaN · 무한대이거나 -1~1 밖이거나 합이 1을 넘을 때(worker가 확인한 값이어도 다시 확인).
            LinkError: 응답에 모르는 종목이 있거나 시각이 가격표와 안 맞을 때.
        """
        req: dict[str, Any] = {"op": "signals"}
        if cut is not None:
            req["cut"] = cut.isoformat()
        if frames is not None:
            save_frames(self.tmp / "frames.npz", frames)
            req["frames"] = "frames.npz"
        resp = self._call(req)
        try:
            got = {s: df["w"] for s, df in self._frames(resp["weights"]).items()}
        except KeyError:
            raise LinkError(BAD_REPLY) from None
        # 고객 코드가 worker와 같은 프로세스라 응답 파일을 바꿀 수 있다: 엔진도 같은 규칙(check_weights)으로 다시 확인한다
        base = self.prices if frames is None else frames
        if not set(got) <= set(base):
            raise LinkError(BAD_REPLY)  # 위조한 종목 이름은 문구에 싣지 않는다
        # worker는 NaN을 0으로 채운 실수만 보내므로 숫자가 아니거나(글자 배열 등) NaN이면 위조
        if any(w.dtype.kind not in "biuf" or not np.isfinite(w.to_numpy()).all() for w in got.values()):
            raise ExpressError("비중에 숫자가 아닌 값 · NaN · 무한대가 있습니다")
        return check_weights(pd.DataFrame(got), cut_frames(base, cut))

    def _reap(self) -> None:
        """하위 프로세스와 그 손자들(프로세스 그룹)을 끝내고 거둔다(여러 번 불러도 됨)."""
        if self.proc.returncode is None:  # 아직 안 거뒀으면 pid가 그대로라 그룹 번호도 안전하다
            try:
                os.killpg(self.proc.pid, signal.SIGKILL)
            except (ProcessLookupError, PermissionError):
                self.proc.kill()
        self.proc.wait()

    def close(self) -> None:
        """하위 프로세스(와 손자)를 끝내고 임시 폴더를 지운다(여러 번 불러도 됨)."""
        self._reap()
        self._err.close()
        shutil.rmtree(self.tmp, ignore_errors=True)

    def __enter__(self) -> Runner:
        """with 문 시작.

        Returns:
            자기 자신.
        """
        return self

    def __exit__(self, *exc: object) -> None:
        """with 문 끝에서 닫는다.

        Args:
            exc: 예외 정보(쓰지 않음).
        """
        self.close()

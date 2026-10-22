"""시험 도우미: 메모리 키 보관 · 가짜 바이낸스 클라이언트(fetch · connect 시험이 같이 씀)."""
import os
import subprocess
import textwrap
from datetime import datetime, timedelta, timezone

import numpy as np
import pandas as pd
import pytest

from blackclip.binance.client import BinanceError
from blackclip.keystore import Credentials
from blackclip.workspace import ensure


@pytest.fixture(autouse=True)
def _config_home(monkeypatch, tmp_path_factory):
    """모든 시험의 사용자 설정 폴더를 임시 자리로(scan · setup · connect가 engine 파일을 적어도 ~/.config에 안 닿게)."""
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path_factory.getbasetemp() / "xdg"))


NOW = datetime(2026, 10, 15, 12, 0, tzinfo=timezone.utc)
CREDS = Credentials("A" * 60 + "WXYZ", "S" * 64)
READ_ONLY = {"ipRestrict": False, "enableReading": True, "enableWithdrawals": False,
             "enableSpotAndMarginTrading": False, "enableFutures": False}
TRADE = {"symbol": "BTCUSDT", "id": 7, "orderId": 70, "price": "60000.00", "qty": "0.001", "commission": "0.00000100",
         "commissionAsset": "BTC", "time": int((NOW - timedelta(days=1)).timestamp() * 1000), "isBuyer": True}


class MemStore:
    """메모리 키 보관."""

    name = "메모리"

    def __init__(self, items=None):
        self.items = dict(items or {})

    def save(self, account, creds):
        self.items[account] = creds

    def load(self, account):
        return self.items.get(account)

    def delete(self, account):
        self.items.pop(account, None)


class FakeClient:
    """경로 · 쿼리로 답하는 클라이언트."""

    def __init__(self, market, testnet, answer):
        self.market, self.testnet, self.answer, self.calls = market, testnet, answer, []

    def get(self, path, params=None, signed=True):
        self.calls.append((path, dict(params or {})))
        return self.answer(path, params or {})


def routes(trades=(TRADE,), perm=READ_ONLY, fail=None):
    """권한 · 체결 · 펀딩비 응답을 고르는 함수를 만든다."""
    def answer(path, q):
        if path == "/sapi/v1/account/apiRestrictions":
            return perm
        if path == "/fapi/v1/income":
            return []
        if path == "/api/v3/account":
            return {"balances": [{"asset": "USDT", "free": "10000", "locked": "0"}]}
        if path == "/fapi/v2/positionRisk":
            return []
        if path == "/fapi/v2/balance":
            return [{"asset": "USDT", "balance": "10000"}]
        if q.get("symbol") == fail:
            raise BinanceError(400, -1121, "Invalid symbol.")
        return [t for t in trades if t["symbol"] == q["symbol"] and q["startTime"] <= t["time"] <= q["endTime"]]
    return answer


def maker(answer, made=None):
    """가짜 클라이언트 공장. made에 만든 클라이언트를 모은다."""
    def make(market, testnet, creds):
        c = FakeClient(market, testnet, answer)
        if made is not None:
            made.append(c)
        return c
    return make


def make_prices(n=400, seed=0, start="2024-01-01", freq="D", tz="UTC"):
    """시험용 가격표(로그 정규 무작위 걸음, OHLC). tz=None이면 시간대 없는 시각.

    Args:
        n: 봉 수.
        seed: 난수 씨앗.
        start: 첫 시각.
        freq: 봉 길이(pandas 표기, 예: "D" · "h").
        tz: 시간대(None이면 없음).

    Returns:
        열 Open · High · Low · Close · Volume인 DataFrame.
    """
    rng = np.random.default_rng(seed)
    c = 100 * np.exp(np.cumsum(rng.normal(0, 0.02, n)))
    o = np.concatenate([[100.0], c[:-1]])
    idx = pd.date_range(start, periods=n, freq=freq, tz=tz)
    return pd.DataFrame({"Open": o, "High": np.maximum(o, c) * 1.01, "Low": np.minimum(o, c) * 0.99,
                         "Close": c, "Volume": 1.0}, index=idx)


def write_link(tmp_path, body, name="s1"):
    """tmp_path를 Git 레포로 보고 .blackclip/strategies/<name>/link.py를 쓴다.

    Args:
        tmp_path: 레포 꼭대기.
        body: link.py 내용(들여쓰기는 dedent).
        name: 전략 이름.

    Returns:
        .blackclip 경로.
    """
    (tmp_path / ".git").mkdir(exist_ok=True)
    root = ensure(tmp_path)
    d = root / "strategies" / name
    d.mkdir(parents=True, exist_ok=True)
    (d / "link.py").write_text(textwrap.dedent(body), encoding="utf-8")
    return root


def swap_for_link(path, outside):
    """path(폴더)를 outside를 가리키는 심볼릭 링크로 바꾼다. 진짜 폴더는 옆(`<이름>.real`)으로 옮긴다.

    점검 · 판 번호 확인을 지난 뒤 레포가 상위 폴더에 링크를 심은 상황을 흉내 낸다.

    Args:
        path: 링크로 바꿀 폴더(없으면 안 됨).
        outside: 링크가 가리킬 레포 밖 폴더.
    """
    path.rename(path.with_name(path.name + ".real"))
    path.symlink_to(outside, target_is_directory=True)


class KlineClient:
    """가격표를 바이낸스 공개 봉 응답처럼 돌려주는 가짜 클라이언트(서명 없는 요청만)."""

    def __init__(self, frames, market="spot", fail=None, price=None):
        self.market, self.frames, self.fail, self.calls, self.price = market, frames, fail, [], dict(price or {})

    def get(self, path, params=None, signed=True):
        assert signed is False
        self.calls.append((path, dict(params)))
        if self.fail:
            raise self.fail
        if params["symbol"] not in self.frames:
            raise BinanceError(400, -1121, "Invalid symbol.")
        df = self.frames[params["symbol"]]
        if "startTime" not in params:  # 지금 가격: 최근 1분 봉 하나(자주 보는 시계), 종가는 price가 있으면 그 값
            t, r = df.index[-1], df.iloc[-1]
            close = self.price.get(params["symbol"], float(r["Close"]))
            return [[int(t.timestamp() * 1000), str(r["Open"]), str(r["High"]), str(r["Low"]), str(close), "1.0"]]
        rows = []
        for t, r in zip(df.index, df.itertuples()):
            ms = int(t.timestamp() * 1000)
            if params["startTime"] <= ms <= params["endTime"]:
                rows.append([ms, str(r.Open), str(r.High), str(r.Low), str(r.Close), "1.0"])
        return rows[: params["limit"]]


FORGE = """

def _forge():
    import sys
    from pathlib import Path
    m = sys.modules["__main__"]
    orig = m.save_frames

    def save(path, frames):
        if path.name == "weights.npz":  # 고객 코드가 응답 파일을 제 값으로 다시 쓴다(argv[3] = 임시 폴더)
            path, frames = Path(sys.argv[3]) / "weights.npz", __FORGED__
        orig(path, frames)

    m.save_frames = save
"""
"""고객 코드가 하위 프로세스 안에서 weights.npz를 위조하는 연결 코드 조각(__FORGED__ 자리에 FORGED 식, frames = worker가 쓰려던 표)."""

FORGED = {
    "nan": '{s: f.assign(w=float("nan")) for s, f in frames.items()}',
    "over_one": "{s: f.assign(w=3.0) for s, f in frames.items()}",
    "unknown_symbol": '{"ZZZUSDT": f for f in frames.values()}',
    "foreign_index": "{s: f.set_axis(f.index + pd.Timedelta(days=4000)) for s, f in frames.items()}",
}
"""위조 방식 이름 → 위조 표를 만드는 식(frames = worker가 쓰려던 종목 → 표, 가격표 시각 인덱스)."""


TG_TOKEN = "123456789:" + "T" * 31 + "Zq9x"
"""시험용 봇 토큰(지어낸 값, 모양만 맞음)."""


class FakeTelegram:
    """가짜 텔레그램 transport: 부른 메서드 · 폼을 모으고 정해 둔 답을 돌려준다.

    Args:
        updates: getUpdates가 돌려줄 목록(연결 시작 뒤 온 글).
        fail: 이 메서드면 ok False(400)로 답한다.
        flood: sendMessage 첫 번째를 429(retry_after 이 값)로 답한다.
        backlog: 연결 시작 전에 이미 밀려 있던 글(update_id는 updates보다 작게).
    """

    def __init__(self, updates=(), fail=None, flood=None, backlog=()):
        self.updates, self.fail, self.flood, self.calls = list(updates), fail, flood, []
        self.backlog = list(backlog)

    def __call__(self, url, form):
        method = url.rsplit("/", 1)[1]
        self.calls.append((url, method, dict(form)))
        if method == self.fail:
            return 400, {"ok": False, "error_code": 400, "description": f"Bad Request: chat not found {url}"}
        if method == "sendMessage" and self.flood:
            wait, self.flood = self.flood, None
            return 429, {"ok": False, "description": "Too Many Requests", "parameters": {"retry_after": wait}}
        if method == "getMe":
            return 200, {"ok": True, "result": {"id": 123456789, "is_bot": True, "username": "bc_test_bot"}}
        if method == "getUpdates":
            since = int(form.get("offset", 0))
            if since < 0:  # 텔레그램: 음수 offset은 큐 끝에서 -offset개만 주고 그 앞은 잊음(시작 시점 큐 = backlog)
                self.backlog = self.backlog[since:]
                return 200, {"ok": True, "result": list(self.backlog)}
            return 200, {"ok": True, "result": [u for u in self.backlog + self.updates if u["update_id"] >= since]}
        return 200, {"ok": True, "result": {"message_id": len(self.calls)}}

    def sent(self):
        """보낸 글 목록."""
        return [f["text"] for _, m, f in self.calls if m == "sendMessage"]


def private_update(text, chat_id=4242, chat_type="private", update_id=1):
    """getUpdates 한 칸."""
    return {"update_id": update_id, "message": {"message_id": 1, "text": text, "chat": {"id": chat_id, "type": chat_type}}}


GIT_ENV = {**{k: v for k, v in os.environ.items() if not k.startswith("GIT_")}, "GIT_CONFIG_GLOBAL": "/dev/null",
           "GIT_CONFIG_NOSYSTEM": "1", "GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@t", "GIT_COMMITTER_NAME": "t",
           "GIT_COMMITTER_EMAIL": "t@t"}
"""시험 레포를 만드는 git 환경(이 컴퓨터 전역 설정 · 서명과 상관없이)."""


def git(repo, *args):
    """시험 레포에서 git 한 번(/usr/bin/git, 실패하면 시험 실패)."""
    subprocess.run(["/usr/bin/git", *args], cwd=repo, env=GIT_ENV, check=True, capture_output=True)


def commit(repo, msg, files):
    """파일을 쓰고(값이 None이면 지움) 모두 커밋한다.

    Args:
        repo: 레포 꼭대기.
        msg: 커밋 글.
        files: 레포 기준 경로 → 내용(None이면 지움).
    """
    for name, text in files.items():
        p = repo / name
        if text is None:
            p.unlink()
        else:
            p.parent.mkdir(parents=True, exist_ok=True)
            p.write_text(text)
    git(repo, "add", "-A")
    git(repo, "commit", "-q", "-m", msg)

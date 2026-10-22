"""check.runner · check.worker: 고객 코드는 하위 프로세스에서, 환경 변수 없이."""
import json
import os
import signal
import time

import pandas as pd
import pytest
from conftest import FORGE, FORGED, write_link

import blackclip.check.runner as runner_mod
from blackclip.check.link import ExpressError, LinkError
from blackclip.check.runner import Runner, SignalError

BODY = '''
import json
import os

import numpy as np
import pandas as pd

EXEC = "next_open"
MARKET = "spot"
PROBE = {"pid": os.getpid(), "env": sorted(os.environ)}


def load_prices():
    rng = np.random.default_rng(0)
    c = 100 * np.exp(np.cumsum(rng.normal(0, 0.02, 200)))
    idx = pd.date_range("2024-01-01", periods=200, freq="D", tz=__TZ__)
    return {"BTCUSDT": pd.DataFrame({"Open": c, "High": c, "Low": c, "Close": c}, index=idx)}


def signals(prices):
    print("고객 코드가 찍은 줄")
    c = prices["BTCUSDT"]["Close"]
    __EXTRA__
    return (c.rolling(5).mean() > c.rolling(20).mean()).astype(float)


def probe():
    return PROBE
'''


def _body(tz='"UTC"', extra="pass"):
    return BODY.replace("__TZ__", tz).replace("__EXTRA__", extra)


def test_runner_loads_and_signals(tmp_path):
    """가격표 · 연결 정보를 받고, 신호를 확인한 비중으로 받는다(고객 print가 통로를 깨지 않음)."""
    with Runner(write_link(tmp_path, _body()), "s1") as r:
        assert list(r.prices) == ["BTCUSDT"] and str(r.prices["BTCUSDT"].index.tz) == "UTC"
        assert (r.link.exec_, r.link.market, r.link.module, r.naive) == ("next_open", "spot", None, False)
        assert [p.name for p in r.files] == ["link.py"]
        w = r.signals()["BTCUSDT"]
        c = r.prices["BTCUSDT"]["Close"]
        assert w.tolist() == (c.rolling(5).mean() > c.rolling(20).mean()).astype(float).tolist()
        assert len(r.signals(cut=r.prices["BTCUSDT"].index[99])["BTCUSDT"]) == 100


def test_runner_child_has_no_engine_env_and_own_pid(tmp_path, monkeypatch):
    """고객 코드는 다른 프로세스에서 돌고, 엔진의 환경 변수를 물려받지 않는다(상속만 확인, 파일 · 키체인은 못 막음)."""
    monkeypatch.setenv("BLACKCLIP_PROBE_SECRET", "x")
    body = _body(extra='open("probe.json", "w").write(json.dumps(PROBE))')
    with Runner(write_link(tmp_path, body), "s1") as r:
        r.signals()
    probe = json.loads((tmp_path / "probe.json").read_text(encoding="utf-8"))
    assert probe["pid"] != os.getpid()
    assert "BLACKCLIP_PROBE_SECRET" not in probe["env"] and "HOME" not in probe["env"]


def test_runner_passes_naive_frames(tmp_path):
    """시간대 없는 가격표면 신호 함수에는 시간대 없이(바이낸스 봉을 넘길 때도)."""
    body = _body(tz="None", extra="assert c.index.tz is None")
    with Runner(write_link(tmp_path, body), "s1") as r:
        assert r.naive is True
        r.signals()
        r.signals(frames=r.prices)


def test_runner_error_kinds(tmp_path):
    """고객 함수가 죽으면 SignalError(예외 이름), 비중이 계약 밖이면 ExpressError, 연결 파일이 틀리면 LinkError."""
    with Runner(write_link(tmp_path, _body(extra="raise ValueError('짧음') if len(c) < 200 else None")), "s1") as r:
        with pytest.raises(SignalError) as exc:
            r.signals(cut=r.prices["BTCUSDT"].index[50])
        assert exc.value.type_name == "ValueError"
    with Runner(write_link(tmp_path, _body().replace(".astype(float)", ".astype(float) * 3")), "s1") as r:
        with pytest.raises(ExpressError):
            r.signals()
    with pytest.raises(LinkError, match="EXEC"):
        Runner(write_link(tmp_path, _body().replace('"next_open"', '"open"')), "s1")


def test_runner_cleans_up_on_load_error(tmp_path, monkeypatch):
    """불러오다 실패해도 하위 프로세스와 임시 폴더를 남기지 않는다."""
    made = []
    real = runner_mod.tempfile.mkdtemp
    monkeypatch.setattr(runner_mod.tempfile, "mkdtemp", lambda **k: made.append(real(**k)) or made[-1])
    with pytest.raises(LinkError):
        Runner(write_link(tmp_path, _body(tz="'bad/zone'")), "s1")
    assert made and not os.path.exists(made[0])


def test_runner_timeout(tmp_path):
    """신호 함수가 너무 오래 걸리면 LinkError 한 줄(하위 프로세스는 끝냄)."""
    body = _body(extra="import time; time.sleep(30)")
    with Runner(write_link(tmp_path, body), "s1", timeout=2) as r:
        with pytest.raises(LinkError, match="2초 안에"):
            r.signals()
        assert r.proc.poll() is not None


def test_runner_frames_roundtrip(tmp_path):
    """다른 표를 넘기면 그 표로 돌린다(데이터 차이)."""
    with Runner(write_link(tmp_path, _body()), "s1") as r:
        other = {"BTCUSDT": r.prices["BTCUSDT"].iloc[:60]}
        w = r.signals(frames=other)["BTCUSDT"]
        assert isinstance(w, pd.Series) and len(w) == 60


def test_runner_timeout_kills_grandchildren(tmp_path):
    """시간 초과 때 고객 코드가 띄운 손자 프로세스도 끝낸다(프로세스 그룹)."""
    extra = ('import subprocess, time; p = subprocess.Popen(["/bin/sleep", "120"]); '
             'open("sleep.pid", "w").write(str(p.pid)); time.sleep(30)')
    pid = None
    try:
        with Runner(write_link(tmp_path, _body(extra=extra)), "s1", timeout=2) as r:
            with pytest.raises(LinkError, match="2초 안에"):
                r.signals()
        pid = int((tmp_path / "sleep.pid").read_text(encoding="utf-8"))
        for _ in range(40):
            try:
                os.kill(pid, 0)
            except ProcessLookupError:
                break
            time.sleep(0.1)
        else:
            pytest.fail("손자 프로세스가 남아 있다")
    finally:
        if pid:
            try:
                os.kill(pid, signal.SIGKILL)
            except ProcessLookupError:
                pass


@pytest.mark.parametrize("payload", [b"garbage\n", b'{"error": "x"}\n', b"[1]\n"])
def test_runner_bad_reply_is_link_error(tmp_path, payload):
    """응답 통로(고객 코드가 fd 3으로 끼워 넣을 수 있음)가 깨져도 LinkError 한 줄이다."""
    with Runner(write_link(tmp_path, _body(extra=f"import os; os.write(3, {payload!r})")), "s1") as r:
        with pytest.raises(LinkError, match="응답") as exc:
            r.signals()
    assert str(tmp_path) not in str(exc.value)


@pytest.mark.parametrize("field,value", [("files", ["../../x"]), ("prices", "../../x.npz"), ("trades", "../../x")])
def test_runner_rejects_forged_paths(tmp_path, field, value):
    """응답의 경로가 레포 · 임시 폴더 밖을 가리키면 LinkError(고객 코드가 응답을 위조해도)."""
    reply = {"exec": "close", "market": "spot", "trades": None, "result_cagr": None, "naive": False,
             "files": ["x"], "prices": "prices.npz", field: value}
    body = _body() + f"\nos.write(3, {(json.dumps(reply) + chr(10)).encode()!r})\n"
    with pytest.raises(LinkError, match="응답") as exc:
        Runner(write_link(tmp_path, body), "s1")
    assert "x.npz" not in str(exc.value) and str(tmp_path) not in str(exc.value)


def test_runner_ignores_repo_top_module_names(tmp_path):
    """레포 꼭대기의 random.py · numbers.py · pandas.py가 하위 프로세스의 표준 · 외부 모듈을 가리지 않는다."""
    root = write_link(tmp_path, _body())
    for name in ("random", "numbers", "pandas"):
        (tmp_path / f"{name}.py").write_text('raise ImportError("가림")\n', encoding="utf-8")
    with Runner(root, "s1") as r:
        assert list(r.prices) == ["BTCUSDT"] and len(r.signals()["BTCUSDT"]) == 200


def test_runner_dead_worker_reports_scrubbed_stderr_tail(tmp_path, monkeypatch):
    """고객 코드가 프로세스를 끝내면 마지막 출력을 붙이고, 홈 폴더는 ~ · 레포는 . 으로 가린다."""
    monkeypatch.setenv("HOME", str(tmp_path.resolve().parent))
    body = _body().replace("    rng = np.random.default_rng(0)",
                           '    print("위치", os.path.dirname(os.getcwd()), os.getcwd())\n    raise SystemExit("고객이 끝냄")')
    with pytest.raises(LinkError, match="끝났습니다") as exc:
        Runner(write_link(tmp_path, body), "s1")
    msg = str(exc.value)
    assert "고객이 끝냄" in msg and "위치 ~ ." in msg
    assert str(tmp_path.resolve().parent) not in msg


def test_runner_link_fields_roundtrip(tmp_path):
    """TRADES · RESULT_CAGR가 있어도 Link로 그대로 돌아오고, 경로는 풀어 쓴 레포 기준이다."""
    (tmp_path / "data").mkdir()
    (tmp_path / "data" / "t.csv").write_text("x\n", encoding="utf-8")
    body = _body() + '\nTRADES = "data/t.csv"\nRESULT_CAGR = 0.182\n'
    with Runner(write_link(tmp_path, body), "s1") as r:
        assert (r.link.trades_label, r.link.result_cagr) == ("data/t.csv", 0.182)
        assert r.link.trades == (tmp_path / "data" / "t.csv").resolve() and r.link.repo == tmp_path.resolve()
        assert r.files == [tmp_path.resolve() / ".blackclip" / "strategies" / "s1" / "link.py"]


def test_runner_repo_helper_import_still_works(tmp_path):
    """-P로 바꿔도 연결 코드가 레포 꼭대기의 도우미 모듈을 불러올 수 있고, 정적 검사 대상에도 든다."""
    (tmp_path / "helper.py").write_text("def half(x):\n    return x * 0.5\n", encoding="utf-8")
    body = _body().replace("import numpy as np", "import numpy as np\nimport helper").replace(
        ".astype(float)", ".astype(float).pipe(helper.half)")
    with Runner(write_link(tmp_path, body), "s1") as r:
        assert set(r.signals()["BTCUSDT"].unique()) <= {0.0, 0.5}
        assert sorted(p.name for p in r.files) == ["helper.py", "link.py"]


EXPECT = {"nan": (ExpressError, "NaN"), "over_one": (ExpressError, "-1~1 밖"),
          "unknown_symbol": (LinkError, "응답이 계약과 다릅니다"), "foreign_index": (LinkError, "시각")}
"""위조 방식 이름(conftest.FORGED) → (엔진이 내야 할 오류, 오류 문구에 든 말)."""


@pytest.mark.parametrize("kind", list(EXPECT))
def test_runner_revalidates_weights_npz(tmp_path, kind):
    """고객 코드가 같은 프로세스에서 weights.npz를 위조해도(NaN · 3.0 · 모르는 종목 · 시각 어긋남)
    엔진이 worker와 같은 규칙으로 다시 확인해 ExpressError · LinkError로 멈춘다(KeyError · nan 숫자로 새지 않음)."""
    err, word = EXPECT[kind]
    body = _body(extra="_forge()") + FORGE.replace("__FORGED__", FORGED[kind])
    with Runner(write_link(tmp_path, body), "s1") as r:
        with pytest.raises(err, match=word) as exc:
            r.signals()
    assert type(exc.value) is err and str(tmp_path) not in str(exc.value)

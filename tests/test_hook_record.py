"""훅 기록 쓰기 · 가리기 · 규칙 v2(역추적 없이)."""
import json
import re
import subprocess
import sys
import time
from pathlib import Path

import pytest

from blackclip.hook import LIMIT, append, fit, is_backtest, line, mask

SRC = str(Path(__file__).resolve().parents[1] / "src")

V2 = re.compile(
    r"\b(?:python3?|ipython)\b(?![^;&|]*\b(?:pytest|pip)\b)[^;&|]*?(?:[\s/_.-])"
    r"(?:back_?test\w*|hyperopt|optimi[sz]e|grid_?search|sweep)"
    r"|\bfreqtrade\s+(?:backtesting|hyperopt)|\bjesse\s+(?:backtest|optimize)|\bblackclip\s+run\s+--",
    re.I,
)
"""처음 잴 때 쓴 규칙 v2 원문. is_backtest가 같은 판정인지 대조용."""

SAMPLES = [
    "python3 backtest.py", "python backtest.py", "python3 backtest.py --symbol BTCUSDT", "python3 grid_search.py",
    "python3 optimize.py --trials 10", "python3 sweep.py", "python3 -m backtester.run", "uv run python run_backtest.py --fast",
    "uv run --no-project python run_backtest.py", "python3 strategies/sma_backtest.py", "python sma_backtest.py",
    "python3 -m strategies.sweep --grid", "freqtrade backtesting --strategy X", "jesse optimize", "blackclip run -- python3 bt.py",
    "python3 report.py --out sweep_report.md", "ipython hyperopt_run.py", "cd s && python3 backtest.py", "python3 -m pytest -q test_backtest.py",
    "python3 -m pip --version", "pip install backtesting", "ls backtest", "cat backtest.py", "python3 bt.py", "python3 run.py",
    "python3 plot_equity.py", 'python3 -c "print(1+1)"', "git log --oneline -3 -- backtest.py", "grep -n ok backtest.py",
    'echo "backtest done"', "python3 -m pip install x; python3 backtest.py", "python3 backtest.py | tee out; pytest",
    "python -m pip list && python sweep.py", "pythonx backtest.py", "python3 x.py pip backtest",
]


@pytest.mark.parametrize("cmd", SAMPLES)
def test_is_backtest_matches_rule_v2(cmd):
    """처음 잰 명령 · 경계 사례에서 규칙 v2 원문 정규식과 같은 판정(놓침 · 오탐 숫자를 그대로 물려받음)."""
    assert is_backtest(cmd) is bool(V2.search(cmd))


def test_is_backtest_reads_only_the_head_and_is_fast():
    """42,000바이트 명령도 바로 끝남. 앞 4,096자 밖의 백테스트 낱말은 안 봄."""
    t = time.process_time()  # CPU 시간: load가 높아도 덜 흔들림(시험 위생)
    assert is_backtest("python " * 6000) is False
    assert is_backtest("python3 backtest.py " + "python " * 6000) is True
    assert is_backtest("python " * 6000 + "backtest.py") is False
    assert time.process_time() - t < 0.5


@pytest.mark.parametrize("cmd, want", [
    ("TOKEN=DEMO python3 backtest.py", "TOKEN=*** python3 backtest.py"),
    ("KEY=DEMO SECRET=DEMO PASS=DEMO PWD=DEMO AUTH=DEMO python3 backtest.py",
     "KEY=*** SECRET=*** PASS=*** PWD=*** AUTH=*** python3 backtest.py"),
    ('env "API_KEY=DEMO VALUE" python3 backtest.py', "env API_KEY=*** python3 backtest.py"),
    ('python3 backtest.py --token="a\\"DEMO"', "python3 backtest.py --token=***"),
    ('python3 backtest.py --header "Authorization: Bearer DEMO"', "python3 backtest.py --header 'Authorization: ***'"),
])
def test_mask_known_cases(cmd, want):
    """다섯 명령: 값 DEMO가 어디에도 남지 않음."""
    assert mask(cmd) == want


def test_mask_more_shapes():
    """옵션 뒤 값 · = 값 · 소문자 이름 · Bearer 단독 · 셸 기호 · 가릴 것 없음 · 따옴표가 안 닫히면 None."""
    assert mask("API_KEY=abc123 python3 backtest.py --token xyz --symbol BTCUSDT") == \
        "API_KEY=*** python3 backtest.py --token *** *** ***"
    assert mask("my_github_token=g python3 sweep.py --password=hunter2 --api-key K --secret S") == \
        "my_github_token=*** python3 sweep.py --password=*** --api-key *** *** ***"
    assert mask("curl -H Authorization: Bearer XYZ && python3 backtest.py") == \
        "curl -H 'Authorization: ***' *** *** && python3 backtest.py"
    assert mask("PERIOD=20 python3 backtest.py --symbol BTC #1") == "PERIOD=20 python3 backtest.py --symbol BTC #1"
    assert mask("X=1;DB_PASS=p python3 backtest.py") == "X=1 ; DB_PASS=*** python3 backtest.py"
    assert mask('python3 backtest.py "unclosed') is None


@pytest.mark.parametrize("cmd, want", [
    ("curl -H Authorization: Basic DEMO && python3 backtest.py", "curl -H 'Authorization: ***' *** *** && python3 backtest.py"),
    ("curl -H Authorization: token DEMO ; python3 backtest.py", "curl -H 'Authorization: ***' *** *** ; python3 backtest.py"),
    ("curl -H Authorization:Basic DEMO", "curl -H 'Authorization: ***' ***"),
    ("curl -H Authorization:Bearer DEMO", "curl -H 'Authorization: ***' ***"),
    ("curl --header=Authorization: Basic DEMO", "curl --header=*** *** ***"),
    ("curl --header=Authorization: Bearer DEMO", "curl --header=*** *** ***"),
    ("python3 backtest.py --auth Bearer DEMO", "python3 backtest.py --auth *** ***"),
    ("python3 backtest.py --auth-header Bearer DEMO", "python3 backtest.py --auth-header *** ***"),
    ("python3 backtest.py --api_key DEMO --symbol BTC", "python3 backtest.py --api_key *** *** ***"),
    ("python3 backtest.py --apikey=DEMO", "python3 backtest.py --apikey=***"),
    ("python3 backtest.py --access-token DEMO", "python3 backtest.py --access-token ***"),
    ('python3 backtest.py -H "X-Api-Key: DEMO" --symbol BTC', "python3 backtest.py -H 'X-Api-Key: ***' *** ***"),
    ("docker run --env=API_KEY=DEMO img python3 backtest.py", "docker run --env=*** img python3 backtest.py"),
    ("curl https://u:DEMO@host/x", "curl https://***@host/x"),
    ("""curl -d '{"token":"DEMO"}' && python3 backtest.py""", """curl -d '{"token": ***' && python3 backtest.py"""),
    ("key.name=DEMO python3 backtest.py", "key.name=*** python3 backtest.py"),
    ('python3 backtest.py --header "Bearer DEMO"', "python3 backtest.py --header 'Bearer ***'"),
    ('curl -H "Authorization Bearer DEMO"', "curl -H 'Authorization ***'"),
    ('curl -H "Cookie: sid=DEMO"', "curl -H 'Cookie: ***'"),
    ("python3 backtest.py --credentials DEMO", "python3 backtest.py --credentials ***"),
    ("python3 backtest.py --jwt DEMO", "python3 backtest.py --jwt ***"),
])
def test_mask_by_shape(cmd, want):
    """한 규칙으로 가림. 어떤 모양이든 DEMO가 남지 않음."""
    got = mask(cmd)
    assert got == want and "DEMO" not in got


def test_mask_keeps_what_is_not_a_secret():
    """`NAME=값`은 값만 가려 명령이 남고, 셸 기호 앞과 뒤(새로 시작)는 남고, 민감어가 없는 토막은 그대로."""
    assert mask("API_KEY=DEMO python3 backtest.py") == "API_KEY=*** python3 backtest.py"
    assert mask("x && python3 backtest.py --auth DEMO") == "x && python3 backtest.py --auth ***"
    assert mask("python3 backtest.py --auth DEMO && python3 sweep.py --symbol BTC") == \
        "python3 backtest.py --auth *** && python3 sweep.py --symbol BTC"
    assert mask("docker run --env=PERIOD=20 img python3 backtest.py") == "docker run --env=PERIOD=20 img python3 backtest.py"


def test_mask_is_linear_on_huge_tokens():
    """40KB 명령 · `=` · `:` · `://`가 아주 많은 토막도 한 번 훑어 0.1초 안(`=`마다 다시 잘라 제곱이던 것)."""
    cmds = ["python3 backtest.py " + "a=b " * 10000, "python3 backtest.py --x " + "=" * 42000,
            "python3 backtest.py --api" + "=" * 42000, "python3 backtest.py token" + ":" * 42000, "x " + "://a" * 10000]
    for c in cmds:
        t = time.process_time()
        assert mask(c) is not None
        assert time.process_time() - t < 0.1


def test_fit_and_line_stay_within_limit():
    """한 줄은 LIMIT 바이트 안: 긴 cmd는 뒤를 잘라 「…」(한글 escape 6바이트로 셈), cmd 없이 넘으면 None."""
    rec = {"v": 1, "ev": "start", "id": "toolu_x", "cmd": "python3 backtest.py " + "--p 한글값 " * 200}
    b = line(rec)
    assert len(b) <= LIMIT and b.endswith(b"\n") and b.count(b"\n") == 1
    got = json.loads(b)
    assert got["cmd"].startswith("python3 backtest.py --p ") and got["cmd"].endswith("…")
    assert line({"v": 1, "ev": "start", "cmd": None}) == b'{"v":1,"ev":"start","cmd":null}\n'
    assert line({"v": 1, "ev": "edit", "path": "a/" * 300 + "x.py"}) is None
    assert fit("a" * 200 + "/" + "b" * 200, 120, tail=True) == "…" + "b" * 114
    assert fit("short", 120) == "short" and fit("한글", 3) == "…"


def test_append_creates_runs_with_owner_only_mode(tmp_path):
    """runs/는 만들어도 되고 파일은 0600(Git 제외 · 소유자만)."""
    root = tmp_path / ".blackclip"
    root.mkdir()
    assert append(root, {"v": 1, "ev": "start", "cmd": "python3 backtest.py"})
    p = root / "runs" / "runs.jsonl"
    assert (p.stat().st_mode & 0o777) == 0o600
    assert [json.loads(x)["ev"] for x in p.read_text().splitlines()] == ["start"]


def test_concurrent_append_100_of_100(tmp_path):
    """훅 프로세스 20개가 동시에 5줄씩(한 줄 500바이트 가까이) → 100줄 모두 온전히 읽힘."""
    root = tmp_path / ".blackclip"
    root.mkdir()
    code = ("import sys; sys.path.insert(0, sys.argv[1]); from pathlib import Path; from blackclip.hook import append\n"
            "for i in range(5): assert append(Path(sys.argv[2]), {'v': 1, 'ev': 'start', 'id': sys.argv[3] + '-' + str(i),"
            " 'cmd': 'python3 backtest.py ' + 'x' * 600})")
    procs = [subprocess.Popen([sys.executable, "-c", code, SRC, str(root), f"p{n}"]) for n in range(20)]
    assert [p.wait(timeout=60) for p in procs] == [0] * 20
    rows = (root / "runs" / "runs.jsonl").read_bytes().splitlines()
    assert len(rows) == 100
    recs = [json.loads(r) for r in rows]
    assert len({r["id"] for r in recs}) == 100 and all(len(r) + 1 <= LIMIT for r in rows)


def test_append_never_creates_blackclip(tmp_path):
    """`.blackclip`이 없으면 만들지 않음: runs/만 만들 수 있고 root는 부르는 쪽 몫."""
    root = tmp_path / ".blackclip"
    with pytest.raises(FileNotFoundError):
        append(root, {"v": 1, "ev": "start", "cmd": "python3 backtest.py"})
    assert not root.exists()


def test_append_refuses_symlink(tmp_path):
    """runs.jsonl이 심볼릭 링크면 따라가 레포 밖에 쓰지 않음(O_NOFOLLOW)."""
    root = tmp_path / ".blackclip"
    (root / "runs").mkdir(parents=True)
    outside = tmp_path / "outside.txt"
    (root / "runs" / "runs.jsonl").symlink_to(outside)
    with pytest.raises(OSError):
        append(root, {"v": 1, "ev": "start", "cmd": "python3 backtest.py"})
    assert not outside.exists()


def test_append_gives_up_quietly_on_short_write(tmp_path, monkeypatch):
    """write가 줄 길이보다 적게 쓰면 예외 없이 False."""
    root = tmp_path / ".blackclip"
    root.mkdir()
    monkeypatch.setattr("blackclip.hook.os.write", lambda fd, b: len(b) - 1)
    assert append(root, {"v": 1, "ev": "start", "cmd": "python3 backtest.py"}) is False


def test_mask_wallet_words_and_telegram_token_shape():
    """PRIVATE · MNEMONIC 낱말과 낱말 없는 텔레그램 봇 토큰 꼴은 가리고, `--seed 1`은 남긴다."""
    from blackclip import hook, telegram
    assert mask('MNEMONIC="w1 w2 w3" python3 backtest.py') == "MNEMONIC=*** python3 backtest.py"
    assert mask("PRIVATE=DEMO python3 backtest.py") == "PRIVATE=*** python3 backtest.py"
    tg = "1234567:" + "A" * 35
    assert mask(f"TELEGRAM_BOT={tg} python3 backtest.py") == "TELEGRAM_BOT=*** python3 backtest.py"
    assert mask(f"python3 backtest.py {tg}") == "python3 backtest.py ***"
    assert mask("python3 backtest.py --seed 1 --start 2024-01-01T10:30") == \
        "python3 backtest.py --seed 1 --start 2024-01-01T10:30"
    assert hook._TG_TOKEN.pattern.endswith(telegram.TOKEN.pattern)

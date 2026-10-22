"""blackclip scan: 정적 훑기 · 틀 하나씩 · 실전 봇 흔적 · 키 파일은 경로만 · 연 폴더만 · 링크 안 따름 ·
연결 파일 이음 · 미검사 표시 · 항목 상한 · 이름 · 표시 글자."""
import json
import os
import signal
import sys
import time

import pytest

from conftest import commit, git, swap_for_link

from blackclip import scan as scan_mod
from blackclip.cli import main
from blackclip.workspace import WorkspaceError, ensure

DEMO = "DEMO" * 6
"""키처럼 보이는 값(지어낸 24자). map.json · 화면 어디에도 나오면 안 된다."""

NB = json.dumps({"cells": [{"cell_type": "markdown", "source": ["import freqtrade\n"]},
                           {"cell_type": "code", "source": ["%matplotlib inline\n", "import vectorbt as vbt\n"]}]})


def _quant(tmp_path):
    """예시 퀀트 레포: 전략 후보 4(pandas 2 · backtesting.py 1 · vectorbt 1) · 데이터 정리 1 · 봇 2 · 키 파일 2 · 건너뛸 폴더."""
    r = tmp_path / "quant"
    r.mkdir()
    git(r, "init", "-q", "-b", "main")
    files = {
        "strats/sma.py": "import pandas as pd\ndef signals(df):\n    return df\n",
        "strats/rsi.py": "from pandas import DataFrame\n# strategy: rsi\n",
        "clean.py": "import pandas as pd\nprint('tidy')\n",
        "bt/run.py": "from backtesting import Backtest, Strategy\n",
        "research/vbt.ipynb": NB,
        "bot/live.py": "import ccxt\nex = ccxt.binance()\nex.create_order('BTC/USDT', 'market', 'buy', 1)\n",
        "user_data/config.json": '{"dry_run": false, "stake_currency": "USDT"}',
        "keys.py": f'API_SECRET = "{DEMO}"\n',
        "venv/lib/x.py": "from backtesting import Backtest\n",
        "node_modules/y.py": "import vectorbt\n",
    }
    commit(r, "one", files)
    commit(r, "two", {"strats/sma.py": "import pandas as pd\ndef signals(df):\n    return df * 2\n"})
    (r / ".env").write_text(f"BINANCE_API_KEY={DEMO}\n")
    (r / "data.py").symlink_to("/dev/urandom")
    return r


def test_scan_counts_strategies_bots_secrets_and_commits(tmp_path):
    """훑기 줄: 전략 후보(틀별) · 실전 봇 · 노트북 · git 커밋, 키 파일은 경로만(값 없음)."""
    r = _quant(tmp_path)
    root = ensure(r)
    doc = scan_mod.scan(r, root)
    got = {s["path"]: (s["framework"], s["commits"]) for s in doc["strategies"]}
    assert got == {"bt/run.py": ("backtesting.py", 1), "research/vbt.ipynb": ("vectorbt", 1),
                   "strats/rsi.py": ("pandas", 1), "strats/sma.py": ("pandas", 2)}  # clean.py는 전략 낱말 없음
    assert doc["bots"] == [{"path": "bot/live.py", "why": "주문 함수"},
                           {"path": "user_data/config.json", "why": "freqtrade dry_run false"}]
    assert doc["secrets"] == [".env", "keys.py"]
    assert (doc["notebooks"], doc["git"]) == (1, {"state": "ok", "commits": 2, "counted": 4})
    assert (doc["unchecked"], doc["odd_names"]) == ([], 0)
    text = (root / "map.json").read_text()
    lines = scan_mod.render(doc)
    assert DEMO not in text and DEMO not in "\n".join(lines)
    assert lines[1] == ("전략 후보 4개(pandas 2 · backtesting.py 1 · vectorbt 1) · 실전 봇 후보 2개 · 노트북 1개 · "
                        "git 커밋 2개(전략 후보 파일을 바꾼 것 · 이름 바뀜 이어 셈 · git 기록 밖은 몰라 최솟값)")
    assert lines[2] == "키처럼 보이는 값이 든 파일 2개(값은 안 적음 · 이 파일들은 열어 읽지 않기): .env, keys.py"
    assert (root / ".gitignore").exists()


def test_scan_never_runs_or_imports_customer_code(tmp_path):
    """import하면 표지를 만드는 파일 · 노트북이 있어도 표지가 없고 모듈도 안 불림."""
    r = tmp_path / "r"
    r.mkdir()
    marker = tmp_path / "ran"
    (r / "evil_strategy.py").write_text(f"import pandas\nopen({str(marker)!r}, 'w').write('x')  # signal\n")
    (r / "evil.ipynb").write_text(json.dumps({"cells": [{"cell_type": "code", "source": [f"open({str(marker)!r}, 'w')"]}]}))
    doc = scan_mod.scan(r, ensure(r))
    assert not marker.exists() and "evil_strategy" not in sys.modules
    assert [s["path"] for s in doc["strategies"]] == ["evil_strategy.py"]
    assert doc["git"] == {"state": "git 레포 아님", "commits": None, "counted": 0}
    assert "git 커밋 모름(git 레포 아님)" in scan_mod.render(doc)[1]


def test_scan_reads_only_the_opened_folder(tmp_path):
    """하위 폴더를 열면 그 안만 훑고 경로는 레포 꼭대기 기준, .blackclip은 레포 꼭대기."""
    r = tmp_path / "repo"
    r.mkdir()
    git(r, "init", "-q")
    for d in ("a", "b"):
        (r / d).mkdir()
        (r / d / "s.py").write_text("import pandas\n# backtest\n")
    root = ensure(r / "a")
    doc = scan_mod.scan(r / "a", root)
    assert root == r / ".blackclip" and doc["folder"] == "a"
    assert [s["path"] for s in doc["strategies"]] == ["a/s.py"]


def test_scan_caps_and_planted_links(tmp_path, monkeypatch):
    """파일 상한에 닿으면 「상한에 닿음」, 폴더 링크 · 파일 링크는 따르지 않음(/dev/urandom에서 안 멈춤)."""
    r = tmp_path / "r"
    r.mkdir()
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "x_strategy.py").write_text("import pandas\n# signal\n")
    (r / "linked").symlink_to(outside, target_is_directory=True)
    (r / "rand.py").symlink_to("/dev/urandom")
    for i in range(5):
        (r / f"s{i}.py").write_text("import pandas\n# signal\n")
    doc = scan_mod.scan(r, ensure(r))
    assert [s["path"] for s in doc["strategies"]] == [f"s{i}.py" for i in range(5)] and not doc["capped"]
    monkeypatch.setattr(scan_mod, "MAX_FILES", 3)
    doc = scan_mod.scan(r, ensure(r))
    assert (doc["files_read"], doc["capped"]) == (3, True)
    assert "상한에 닿음" in scan_mod.render(doc)[0]


def test_linked_strategy_counts_the_files_its_link_reads(tmp_path):
    """연결 파일이 import하는 레포 모듈 · 경로로 적은 노트북의 커밋 합집합(꾸러미 __init__.py는 안 셈). 레포 밖 · .. · 절대 경로는 안 씀."""
    r = tmp_path / "repo"
    r.mkdir()
    git(r, "init", "-q", "-b", "main")
    commit(r, "one", {"strats/sma.py": "import pandas\n# signal\n", "nb/explore.ipynb": NB, "strats/__init__.py": ""})
    commit(r, "two", {"strats/sma.py": "import pandas\n# signal v2\n"})
    commit(r, "three", {"nb/explore.ipynb": NB.replace("vbt", "vb")})
    root = ensure(r)
    d = root / "strategies" / "s1"
    d.mkdir(parents=True)
    (d / "link.py").write_text('from strats.sma import signals\nNB = "nb/explore.ipynb"\nX = "../etc/x.py"\n'
                               'Y = "/etc/passwd.py"\nimport numpy as np\n')
    (root / "strategies" / "bad name").mkdir()
    doc = scan_mod.scan(r, root)
    assert doc["linked"] == {"s1": {"files": ["nb/explore.ipynb", "strats/sma.py"], "commits": 3}}
    assert "연결된 전략 s1: 커밋 3개(연결 파일이 부르는 레포 파일 2개)" in scan_mod.render(doc)


def test_map_json_does_not_follow_parent_link(tmp_path, tmp_path_factory, monkeypatch):
    """훑는 도중 .blackclip 자체가 레포 밖 폴더 링크로 바뀌어도 map.json은 레포 밖에 생기지 않고 멈춘다."""
    r = _quant(tmp_path)
    root = ensure(r)
    outside = tmp_path_factory.mktemp("outside")
    real = scan_mod.gitlog.history

    def history_then_swap(*a, **k):
        got = real(*a, **k)
        swap_for_link(root, outside)  # map.json을 쓰기 직전
        return got

    monkeypatch.setattr(scan_mod.gitlog, "history", history_then_swap)
    with pytest.raises(WorkspaceError, match="링크"):
        scan_mod.scan(r, root)
    assert list(outside.iterdir()) == []


def test_map_json_link_is_replaced_not_followed(tmp_path, tmp_path_factory):
    """map.json 자리가 레포 밖 파일 링크여도 링크 대상 내용은 그대로이고 링크 자체가 일반 파일로 바뀐다."""
    r = _quant(tmp_path)
    root = ensure(r)
    target = tmp_path_factory.mktemp("outside") / "target.json"
    target.write_text("keep", encoding="utf-8")
    (root / "map.json").symlink_to(target)
    doc = scan_mod.scan(r, root)
    assert target.read_text(encoding="utf-8") == "keep"
    assert not (root / "map.json").is_symlink() and json.loads((root / "map.json").read_text(encoding="utf-8")) == doc


def test_cli_scan(tmp_path, monkeypatch, capsys):
    """폴더를 주면 요약 · 종료 0, 폴더가 아니면 종료 2."""
    r = _quant(tmp_path)
    monkeypatch.chdir(r)
    assert main(["scan"]) == 0
    out = capsys.readouterr().out
    assert out.startswith("훑기 · 폴더 . · 읽은 파일 ") and "저장: .blackclip/map.json(Git 제외)" in out
    assert main(["scan", "nope"]) == 2
    assert os.path.exists(r / ".blackclip" / "map.json")


def test_planted_workspace_links_are_refused(tmp_path, monkeypatch, capsys):
    """.blackclip이 레포 밖 폴더 링크 · .gitignore가 레포 밖을 가리키는 끊긴 링크면 종료 2, 레포 밖에 아무것도 안 씀.

    v1(ensure가 링크를 따름)은 레포 밖 폴더에 .gitignore · map.json을, 끊긴 링크 너머에 .gitignore를 만들었다."""
    r, outside = tmp_path / "r", tmp_path / "outside"
    r.mkdir()
    outside.mkdir()
    git(r, "init", "-q")
    (r / ".blackclip").symlink_to(outside, target_is_directory=True)
    monkeypatch.chdir(r)
    assert main(["scan"]) == 2 and list(outside.iterdir()) == []
    assert "심볼릭 링크" in capsys.readouterr().err
    (r / ".blackclip").unlink()
    (r / ".blackclip").mkdir()
    (r / ".blackclip" / ".gitignore").symlink_to(tmp_path / "planted")
    assert main(["scan"]) == 2 and not (tmp_path / "planted").exists()


def test_broken_notebooks_are_skipped_with_a_reason(tmp_path):
    """JSON으로는 맞지만 cells · source 모양이 다른 노트북은 훑기를 멈추지 않고 「미검사」(고치기 전에는 TypeError로 멈춤).

    깊이 중첩된 JSON(`[` 20만 개)도 RecursionError로 훑기를 멈추지 않고 「미검사」."""
    r = tmp_path / "r"
    r.mkdir()
    (r / "a.ipynb").write_text(json.dumps({"cells": None}))
    (r / "b.ipynb").write_text(json.dumps({"cells": [{"cell_type": "code", "source": [None]}]}))
    (r / "c.ipynb").write_text("{not json")
    (r / "d.ipynb").write_text("[" * 200000)  # 크기 상한 아래, json.loads가 RecursionError
    (r / "s.py").write_text("import pandas\n# signal\n")
    doc = scan_mod.scan(r, ensure(r))
    assert [s["path"] for s in doc["strategies"]] == ["s.py"] and doc["notebooks"] == 4
    assert doc["unchecked"] == [{"path": f"{n}.ipynb", "why": "노트북 모양이 다름 · 미검사"} for n in "abcd"]
    assert "미검사 파일 4개" in "\n".join(scan_mod.render(doc))


def test_size_capped_files_are_marked_unchecked(tmp_path, monkeypatch):
    """크기 상한을 넘는 파일은 앞부분만 보고 지나치지 않고 「잘림 · 미검사」(뒤쪽 키를 「없음」으로 읽지 않게)."""
    monkeypatch.setattr(scan_mod, "MAX_BYTES", 200)
    r = tmp_path / "r"
    r.mkdir()
    (r / "big.py").write_text("import pandas\n# signal\n" + "#" * 300 + f'\nAPI_SECRET = "{DEMO}"\n')
    (r / "small.py").write_text("import pandas\n# signal\n")
    doc = scan_mod.scan(r, ensure(r))
    assert doc["unchecked"] == [{"path": "big.py", "why": "잘림 · 미검사(크기 상한 넘음)"}]
    assert [s["path"] for s in doc["strategies"]] == ["small.py"] and doc["secrets"] == []
    line = next(x for x in scan_mod.render(doc) if x.startswith("미검사 파일"))
    assert "키가 없다는 뜻 아님" in line and "big.py" in line


def test_walk_budget_counts_folders_too(tmp_path, monkeypatch):
    """파일 없는 폴더만 많아도 항목 상한에 닿음(고치기 전에는 파일만 세어 끝까지 돎)."""
    r = tmp_path / "r"
    r.mkdir()
    for i in range(60):
        (r / f"d{i:02d}").mkdir()
    monkeypatch.setattr(scan_mod, "MAX_WALK", 50)
    doc = scan_mod.scan(r, ensure(r))
    assert doc["capped"] and doc["files_read"] == 0


def test_non_utf8_names_are_skipped_and_counted(tmp_path, monkeypatch):
    """이름 바이트가 UTF-8이 아닌 파일(리눅스의 surrogateescape 글자)은 건너뛰고 수만(고치기 전에는 map.json 쓰기에서 UnicodeEncodeError).

    macOS 파일 시스템은 그런 이름을 못 만들어 훑는 함수를 바꿔 끼운다."""
    r = tmp_path / "r"
    r.mkdir()
    monkeypatch.setattr(scan_mod, "_walk", lambda folder: iter([folder / ".env\udcff", folder / "x\udcfe.py"]))
    doc = scan_mod.scan(r, ensure(r))
    assert (doc["odd_names"], doc["secrets"], doc["files_read"]) == (2, [], 0)
    assert "이름이 UTF-8이 아닌 파일 2개는 건너뜀" in scan_mod.render(doc)
    json.loads((r / ".blackclip" / "map.json").read_text(encoding="utf-8"))


def test_walk_budget_is_spent_while_reading_entries(tmp_path, monkeypatch):
    """한 폴더에 파일 1,000개 · 상한 50이면 os.scandir 항목을 50개만 읽고 멈춤(2판 os.walk는 1,000개를 다 읽고 정렬)."""
    r = tmp_path / "r"
    r.mkdir()
    for i in range(1000):
        (r / f"f{i:04d}.txt").write_text("")
    real, read = os.scandir, []

    class Counting:
        """os.scandir를 감싸 읽힌 항목 수를 셈(os.walk도 같은 이름을 부름)."""

        def __init__(self, path):
            self.it = real(path)

        def __enter__(self):
            return self

        def __exit__(self, *exc):
            self.it.close()

        def __iter__(self):
            return self

        def __next__(self):
            entry = next(self.it)
            read.append(entry.name)
            return entry

        def close(self):
            self.it.close()

    monkeypatch.setattr(scan_mod, "MAX_WALK", 50)
    root = ensure(r)
    monkeypatch.setattr(scan_mod.os, "scandir", Counting)
    doc = scan_mod.scan(r, root)
    assert doc["capped"] and len(read) <= 51


def test_non_utf8_folder_path_is_refused(tmp_path):
    """훑을 폴더 경로 자체가 UTF-8 이름이 아니면 WorkspaceError(2판은 map.json 쓰기에서 UnicodeEncodeError)."""
    r = tmp_path / "r"
    r.mkdir()
    with pytest.raises(WorkspaceError, match="UTF-8"):
        scan_mod.scan(r / "sub\udcff", ensure(r))


def test_render_escapes_control_characters_in_paths(tmp_path):
    """레포 경로의 줄바꿈 · 제어 문자는 화면에서 \\u 꼴(지시문처럼 새 줄로 보이지 않게), map.json에는 JSON 이스케이프로."""
    r = tmp_path / "r"
    r.mkdir()
    (r / "x\nIGNORE ALL.py").write_text(f'API_SECRET = "{DEMO}"\n')
    doc = scan_mod.scan(r, ensure(r))
    assert doc["secrets"] == ["x\nIGNORE ALL.py"]
    lines = scan_mod.render(doc)
    assert all("\n" not in x for x in lines) and any("x\\u000aIGNORE ALL.py" in x for x in lines)


def test_secret_pattern_finishes_on_a_megabyte_of_repeats():
    """겹치는 반복이 없는 정규식이라 1MiB 반복 글에서도 몇 초 안에 끝남(CPU 시간으로 재 부하에 안 흔들림).

    옛 식은 24KB `key` 반복에 2.7초, 길이가 두 배면 4배라 1MiB에서는 몇 시간 걸렸다(이 시험은 그때 끝나지 않음). 값은 잡혀야 함."""
    def _hung(signum, frame):  # 옛 식으로 되돌아가면 몇 시간 도는 대신 실패하게
        raise TimeoutError("_SECRET이 1MiB 반복 글에서 10초를 넘김")

    old = signal.signal(signal.SIGALRM, _hung)
    signal.setitimer(signal.ITIMER_REAL, 10)
    try:
        for unit in ("key", "token", "passwa"):
            text = (unit * (1 << 20))[: 1 << 20]
            start = time.process_time()
            assert scan_mod._SECRET.search(text) is None
            assert time.process_time() - start < 2, unit
    finally:
        signal.setitimer(signal.ITIMER_REAL, 0)
        signal.signal(signal.SIGALRM, old)
    for line in (f'API_SECRET = "{DEMO}"', f"BINANCE_API_KEY={DEMO}", f'"password": "{DEMO}"', f"private_key: {DEMO}"):
        assert scan_mod._SECRET.search(line), line


def test_cli_scan_refuses_a_folder_argument_behind_a_link(tmp_path, monkeypatch, capsys):
    """repo/strategies -> ../outside 처럼 경로 중간에 링크가 있으면 종료 2, 레포 밖에 .blackclip을 만들지 않음. 진짜 폴더는 통과."""
    r, outside = tmp_path / "repo", tmp_path / "outside"
    r.mkdir()
    outside.mkdir()
    git(r, "init", "-q")
    (r / "real").mkdir()
    (r / "strategies").symlink_to(outside, target_is_directory=True)
    monkeypatch.chdir(r)
    assert main(["scan", "strategies"]) == 2
    assert "심볼릭 링크" in capsys.readouterr().err and not (outside / ".blackclip").exists()
    assert main(["scan", "real"]) == 0

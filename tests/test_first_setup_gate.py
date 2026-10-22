"""첫 셋업 줄 거르기 · 거절 표지: 퀀트처럼 보이는 폴더에서만, 거절한 레포에서는 안 냄."""
import hashlib
import json
import os
import time
from pathlib import Path

import pytest

import blackclip.hook as hook
import blackclip.scan as scan
from blackclip.cli import main
from blackclip.keystore import config_dir
from blackclip.scan import looks_quant


@pytest.fixture(autouse=True)
def _home(monkeypatch, tmp_path):
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "xdg"))


def _repo(tmp_path, files):
    r = tmp_path / "repo"
    (r / ".git").mkdir(parents=True)
    for rel, text in files.items():
        (r / rel).parent.mkdir(parents=True, exist_ok=True)
        (r / rel).write_text(text)
    return r


QUANT = {"strategies/trend.py": "import pandas as pd\n\ndef signal(df):\n    return df\n"}
WEBSITE = {"index.html": "<html></html>", "src/app.js": "export {}", "package.json": "{}",
           "server/app.py": "from flask import Flask\napp = Flask(__name__)\n"}


def _start(cwd):
    return hook.handle("SessionStart", {"cwd": str(cwd), "source": "startup"})


def test_website_folder_gets_no_first_setup_line(tmp_path):
    r = _repo(tmp_path, WEBSITE)
    assert looks_quant(r) is False and _start(r) is None


@pytest.mark.parametrize("files", [QUANT, {"bots/live.py": "import ccxt\nex.create_order('BTC/USDT', 'market', 'buy', 1)\n"},
                                   {"nb/a.ipynb": json.dumps({"cells": [{"cell_type": "code", "source": ["import vectorbt as vbt\n"]}]})}])
def test_quant_folder_gets_the_line(tmp_path, files):
    r = _repo(tmp_path, files)
    out = _start(r)
    assert looks_quant(r) and out["systemMessage"] == hook.NO_ROOT


def test_quant_check_is_bounded(tmp_path):
    """링크 폴더 · 링크 파일 · FIFO · 64KB 뒤의 import · 300 항목 뒤 · 깊이 4는 안 봄, 멈추지 않음."""
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "s.py").write_text(QUANT["strategies/trend.py"])
    r = _repo(tmp_path, {"README.md": "x"})
    (r / "linked").symlink_to(outside, target_is_directory=True)
    (r / "s.py").symlink_to(outside / "s.py")
    os.mkfifo(r / "pipe.py")
    (r / "big.py").write_text("#" * (64 << 10) + "\nimport backtrader\n")
    (r / "a" / "b" / "c" / "d").mkdir(parents=True)
    (r / "a" / "b" / "c" / "d" / "deep.py").write_text("import freqtrade\n")
    t0 = time.monotonic()  # 파일 준비가 아니라 판정 함수만 잰다
    assert looks_quant(r) is False
    assert time.monotonic() - t0 < 1.5


class _It:
    """scandir 대신: 주어진 순서로 항목을 내주고 몇 개 꺼냈는지 센다(with · for 둘 다)."""

    def __init__(self, items):
        self.items, self.pulled = iter(items), 0

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def __iter__(self):
        return self

    def __next__(self):
        item = next(self.items)
        self.pulled += 1
        return item

    def close(self):
        pass


@pytest.mark.parametrize("quant_first, want", [(True, True), (False, False)])
def test_quant_budget_follows_enumeration_order(tmp_path, monkeypatch, quant_first, want):
    """300 항목 예산은 이름순이 아니라 열거 순서대로 — 퀀트 파일이 1번째로 나오면 참, .git + 300개 뒤면 거짓.
    이름(a_…)은 이름순이면 언제나 맨 앞이라, 이 시험이 「꺼낸 것만 정렬」 정책을 묶는다."""
    r = _repo(tmp_path, {f"f{i:03d}.py": "x = 1\n" for i in range(300)})
    (r / "a_quant.py").write_text("import backtrader\n")
    real = os.scandir

    def ordered(path):
        with real(path) as it:
            entries = list(it)
        q = [e for e in entries if e.name == "a_quant.py"]
        rest = sorted((e for e in entries if e.name != "a_quant.py"), key=lambda e: e.name)
        return _It(q + rest if quant_first else rest + q)

    monkeypatch.setattr(scan.os, "scandir", ordered)
    t0 = time.monotonic()
    assert looks_quant(r) is want
    assert time.monotonic() - t0 < 1.5


def test_quant_check_stops_enumerating_at_budget(tmp_path, monkeypatch):
    """한 폴더에 항목이 10만 개여도 scandir에서 예산만큼만 꺼내고 멈춘다(전부 꺼낸 뒤 정렬하지 않음)."""

    class Entry:
        def __init__(self, i):
            self.name = f"f{i:06d}.txt"
            self.path = str(tmp_path / self.name)

        def is_dir(self, follow_symlinks=True):
            return False

        def is_file(self, follow_symlinks=True):
            return True

        def is_symlink(self):
            return False

    made = []

    def many(path):
        made.append(_It(Entry(i) for i in range(100_000)))
        return made[-1]

    monkeypatch.setattr(scan.os, "scandir", many)
    assert looks_quant(tmp_path / "home" / "proj") is False  # 집 아래 폴더(집의 조상은 훑지 않음)
    assert made and made[0].pulled <= scan.QUANT_ENTRIES + 1


def test_decline_marker_silences_this_repo_only(tmp_path, monkeypatch, capsys):
    r = _repo(tmp_path, QUANT)
    other = tmp_path / "other"
    (other / ".git").mkdir(parents=True)
    (other / "s.py").write_text(QUANT["strategies/trend.py"])
    monkeypatch.chdir(r / "strategies")
    assert main(["setup", "--decline"]) == 0
    assert capsys.readouterr().out.startswith("이 레포에서는 첫 셋업을 다시 묻지 않아요")
    mark = config_dir() / "declined" / hashlib.sha256(str(r.resolve()).encode()).hexdigest()
    assert mark.is_file() and oct(mark.stat().st_mode & 0o777) == "0o600"
    assert _start(r) is None and _start(r / "strategies") is None and _start(other)["systemMessage"] == hook.NO_ROOT
    assert not (r / ".blackclip").exists()  # 레포를 더럽히지 않음


def test_decline_marker_must_be_a_plain_file_of_mine(tmp_path):
    r = _repo(tmp_path, QUANT)
    d = config_dir() / "declined"
    d.mkdir(parents=True, mode=0o700)
    os.chmod(config_dir(), 0o700)
    name = hashlib.sha256(str(r.resolve()).encode()).hexdigest()
    target = tmp_path / "t"
    target.write_text("")
    (d / name).symlink_to(target)
    assert _start(r)["systemMessage"] == hook.NO_ROOT
    (d / name).unlink()
    os.mkfifo(d / name)
    assert _start(r)["systemMessage"] == hook.NO_ROOT
    (d / name).unlink()
    (d / name).write_text("")
    os.chmod(d, 0o777)
    assert _start(r)["systemMessage"] == hook.NO_ROOT  # 남이 쓸 수 있는 폴더의 표지는 안 믿음


def test_truncated_notebook_still_shows_its_whole_code_cells(tmp_path):
    """그림 출력을 저장한 노트북(64KiB 넘음, 끝이 잘려 JSON이 깨짐)도 앞쪽 온전한 코드 칸으로 판정한다."""
    cells = [{"cell_type": "code", "metadata": {}, "outputs": [], "source": ["import vectorbt as vbt\n"]},
             {"cell_type": "code", "metadata": {}, "outputs": [{"data": {"image/png": "A" * 70_000}}], "source": ["plt.show()\n"]}]
    r = _repo(tmp_path, {"nb/big.ipynb": json.dumps({"cells": cells, "nbformat": 4}, indent=1, sort_keys=True)})
    assert (r / "nb" / "big.ipynb").stat().st_size > scan.QUANT_BYTES and looks_quant(r) is True
    # "cells"가 앞 64KiB 안에 없으면(앞에 큰 metadata) 건너뜀, 첫 칸이 온전히 안 들어와도 건너뜀
    big_meta = {"metadata": {"x": "M" * 70_000}, "cells": cells[:1], "nbformat": 4}
    cut = {"cells": [{"cell_type": "code", "metadata": {}, "outputs": [], "source": ["#" * 70_000 + "\nimport vectorbt\n"]}]}
    r2 = tmp_path / "repo2"
    (r2 / ".git").mkdir(parents=True)
    (r2 / "a.ipynb").write_text(json.dumps(big_meta))  # 삽입 순서 그대로: metadata가 cells 앞
    (r2 / "b.ipynb").write_text(json.dumps(cut))
    assert looks_quant(r2) is False


def test_home_folder_itself_is_never_scanned(tmp_path):
    """집 폴더를 열면 문서 · 데스크탑을 열지 않으려고 바로 거짓. 집 아래 폴더는 그대로 본다."""
    home = tmp_path / "home"
    (home / "proj").mkdir(parents=True)
    (home / "s.py").write_text(QUANT["strategies/trend.py"])
    (home / "proj" / "s.py").write_text(QUANT["strategies/trend.py"])
    assert looks_quant(home) is False and looks_quant(home / "proj") is True


def test_ancestor_of_home_is_never_scanned(tmp_path):
    """집 폴더의 조상(임시 HOME의 부모 · 루트)을 열어도 깊이 2~3에서 집 안 Desktop · Documents를 열지 않게 바로 거짓."""
    (tmp_path / "home" / "Desktop").mkdir(parents=True)
    (tmp_path / "home" / "Desktop" / "s.py").write_text(QUANT["strategies/trend.py"])
    assert looks_quant(tmp_path / "home" / "Desktop") is True  # 집 아래는 그대로 봄
    assert looks_quant(tmp_path) is False and looks_quant(tmp_path.parent) is False and looks_quant(Path("/")) is False


def test_decline_refuses_an_unsafe_config_folder(tmp_path, monkeypatch, capsys):
    """설정 폴더를 남이 쓸 수 있으면 표지를 안 적고 종료 2 + 안내 한 줄."""
    r = _repo(tmp_path, QUANT)
    config_dir().mkdir(parents=True)
    os.chmod(config_dir(), 0o777)
    monkeypatch.chdir(r)
    assert main(["setup", "--decline"]) == 2
    cap = capsys.readouterr()
    assert cap.err == "blackclip setup: 사용자 설정 폴더가 내 것이 아니거나 남이 쓸 수 있어 적지 않았어요\n" and cap.out == ""
    assert not (config_dir() / "declined").exists()

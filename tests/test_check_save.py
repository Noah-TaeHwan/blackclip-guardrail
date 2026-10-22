"""blackclip save: 가드레일 파일 · 판 번호 · 막을 때."""
import json
import re
import os
from datetime import date, datetime, timezone

import pytest
from conftest import swap_for_link, write_link
from test_check_report import CLEAN, _body

import blackclip.check.report as report
from blackclip.check.guardrail import summary
from blackclip.cli import main
from blackclip.workspace import guardrail_problem, read_guardrail, versions


def _save(tmp_path, monkeypatch, capsys, *args, body=None):
    monkeypatch.chdir(tmp_path)
    if body is not None:
        write_link(tmp_path, body)
    code = main(["save", "s1", *args])
    out = capsys.readouterr()
    return code, out.out, out.err


def _root(tmp_path):
    return tmp_path / ".blackclip"


def _folder(tmp_path):
    return _root(tmp_path) / "strategies" / "s1"


def test_save_writes_v1(tmp_path, monkeypatch, capsys):
    """v1 · 기본 민감도 보통 · 실전 시작일 오늘(UTC) · JSON 내용 · 절대 경로 없음."""
    code, out, _ = _save(tmp_path, monkeypatch, capsys, body=_body())
    assert code == 0
    lines = out.splitlines()
    today = datetime.now(timezone.utc).date().isoformat()
    assert lines[0] == f"가드레일 v1 저장 · s1 · 실전 시작일 {today} · 민감도 보통"
    assert "보수적 비용 기준 · 시도 수 보정은 범위에 넣지 않고 판단 보류 옆에 숫자로 적어요(판단 보류 계산에는 아직 안 써요)" in lines
    assert lines[-1] == "저장: .blackclip/strategies/s1/guardrail.v1.yaml"
    check_line = next(x for x in lines if x.startswith("점검: "))  # 저장 출력도 점검 키가 아니라 이름
    assert check_line.startswith("점검: 재현성 ") and not re.search(r"(?<![A-Za-z0-9])C[0-9](?![A-Za-z0-9])", out)
    text = (_folder(tmp_path) / "guardrail.v1.yaml").read_text(encoding="utf-8")
    doc = json.loads(text)
    assert (doc["version"], doc["strategy"], doc["live_start"], doc["reason"], doc["previous"]) == (1, "s1", today, None, None)
    assert doc["warning_line"]["sensitivity"] == "보통" and doc["warning_line"]["line"] == doc["warning_line"]["lines"]["보통"]
    assert doc["response"] == {"execution_warning": "", "warning_line": ""} and doc["match_window_bars"] == 1
    assert doc["checks"]["C0"] == "재현 확인 전" and set(doc["checks"]) == {"C0", "C1", "C2", "C3", "C4"}
    assert doc["target"]["symbols"] == ["BTCUSDT"] and len(doc["target"]["link_sha256"]) == 64
    assert doc["c3"]["method"] == "독립 가정 근사" and doc["c3"]["trials"] is None and doc["format"] == 1
    assert str(tmp_path) not in text and "/Users/" not in text and "/private/" not in text
    assert guardrail_problem(doc) is None  # 저장기가 쓴 실제 가드레일은 읽기 쪽 안쪽 모양 검사를 통과한다


def test_save_start_must_not_precede_backtest_start(tmp_path, monkeypatch, capsys):
    """읽는 쪽이 target.start ≤ live_start를 요구하므로, 백테스트 시작(2024-01-01)보다 하루 이른 --start는 쓰기 전에 한 줄 · 코드 2,
    같은 날이면 저장되고 read_guardrail이 받는다."""
    code, out, err = _save(tmp_path, monkeypatch, capsys, "--start", "2023-12-31", body=_body())
    assert code == 2 and out == ""
    assert err.splitlines() == ["blackclip save: 가드레일을 저장하지 않음 · 실전 시작일이 백테스트 시작(2024-01-01)보다 이릅니다 · "
                                "--start를 그날 이후로 다시"]
    assert versions(_root(tmp_path), "s1") == [] and not list(_folder(tmp_path).glob("guardrail.v*"))
    code, _, _ = _save(tmp_path, monkeypatch, capsys, "--start", "2024-01-01")
    assert code == 0
    got = read_guardrail(tmp_path / ".blackclip", "s1")
    assert got is not None and got[0] == 1 and got[1]["live_start"] == got[1]["target"]["start"] == "2024-01-01"


def test_save_existing_needs_reason_and_never_overwrites(tmp_path, monkeypatch, capsys):
    """v1이 있으면 이유 없이는 코드 2(v1 그대로), 이유가 있으면 v2 · 이전 판 · 이유."""
    _save(tmp_path, monkeypatch, capsys, body=_body())
    v1 = (_folder(tmp_path) / "guardrail.v1.yaml").read_bytes()
    code, _, err = _save(tmp_path, monkeypatch, capsys)
    assert code == 2 and err.startswith("blackclip save: guardrail.v1.yaml이 이미 있음 · 덮어쓰지 않음")
    code, out, _ = _save(tmp_path, monkeypatch, capsys, "--reason", " 민감으로 바꿈 ", "--sensitivity", "민감",
                         "--start", "2026-09-23")
    assert code == 0 and "이유: 민감으로 바꿈" in out
    doc = json.loads((_folder(tmp_path) / "guardrail.v2.yaml").read_text(encoding="utf-8"))
    assert (doc["version"], doc["previous"], doc["reason"], doc["live_start"]) == (2, "v1", "민감으로 바꿈", "2026-09-23")
    assert doc["warning_line"]["sensitivity"] == "민감"
    assert (_folder(tmp_path) / "guardrail.v1.yaml").read_bytes() == v1


@pytest.mark.parametrize("when,args", [("after_check", ("--reason", "바꿈")), ("after_check", ()), ("before_write", ("--reason", "바꿈"))],
                         ids=["after_check_reason", "after_check_plain", "before_write"])
def test_guardrail_save_does_not_follow_parent_link(tmp_path, tmp_path_factory, monkeypatch, capsys, when, args):
    """점검 뒤(after_check) · 파일을 쓰기 직전(before_write)에 strategies/s1이 레포 밖 폴더 링크가 되어도 레포 밖에
    가드레일 판이 생기지 않고 「링크」 한 줄 · 종료 2. before_write는 판 번호 · 자리 확인을 지난 뒤라 쓰는 자리만 가르고,
    after_check_plain은 이유가 없을 때 레포 밖 v1을 판으로 세지 않는지(판 번호를 읽는 자리)를 가른다."""
    import blackclip.check.save as save_mod

    outside = tmp_path_factory.mktemp("outside")
    (outside / "guardrail.v1.yaml").write_text("{}", encoding="utf-8")  # 경로 glob이 보면 v2로 이어 쓰는 자리
    target = "run_check" if when == "after_check" else "document"
    real = getattr(save_mod, target)

    def then_swap(*a, **k):
        got = real(*a, **k)
        swap_for_link(_folder(tmp_path), outside)
        return got

    monkeypatch.setattr(save_mod, target, then_swap)
    code, out, err = _save(tmp_path, monkeypatch, capsys, *args, body=_body())
    assert code == 2 and out == "" and err.startswith("blackclip save: ") and "링크" in err
    assert len(err.strip().splitlines()) == 1
    assert sorted(p.name for p in outside.iterdir()) == ["guardrail.v1.yaml"]  # 새 판도 임시 파일도 없음
    assert (outside / "guardrail.v1.yaml").read_text(encoding="utf-8") == "{}"


def test_save_refuses_non_regular_version_slot(tmp_path, monkeypatch, capsys):
    """새 판 번호 자리에 링크 같은 일반 파일이 아닌 것이 있으면 저장을 거절하고 링크 · 링크 대상을 그대로 둔다."""
    code, _, _ = _save(tmp_path, monkeypatch, capsys, body=_body())
    assert code == 0
    target = tmp_path / "elsewhere.txt"
    target.write_text("keep", encoding="utf-8")
    slot = _folder(tmp_path) / "guardrail.v2.yaml"
    slot.symlink_to(target)
    code, out, err = _save(tmp_path, monkeypatch, capsys, "--reason", "바꿈")
    assert code == 2 and out == ""
    assert err == "blackclip save: guardrail.v2.yaml 자리에 일반 파일이 아닌 것이 있어 저장하지 않습니다\n"
    assert slot.is_symlink() and os.readlink(slot) == str(target) and target.read_text(encoding="utf-8") == "keep"
    assert not (_folder(tmp_path) / "guardrail.v3.yaml").exists()


def test_save_refuses_c0_mismatch(tmp_path, monkeypatch, capsys):
    """재현성 불일치면 저장하지 않고 코드 2와 한 줄(같은 전략인지 모름)."""
    (tmp_path / "bt").mkdir()
    (tmp_path / "bt" / "t.csv").write_text(
        "time,symbol,side,qty,price,fee,order_id\n"
        "2024-06-01T00:00:00+00:00,BTCUSDT,buy,1,100,0,a\n"
        "2024-06-20T00:00:00+00:00,BTCUSDT,sell,1,100,0,b\n", encoding="utf-8")
    code, _, err = _save(tmp_path, monkeypatch, capsys, body=_body(extra='\nTRADES = "bt/t.csv"\n'))
    assert code == 2 and "가드레일을 저장하지 않음 · 재현성 불일치" in err
    assert versions(_root(tmp_path), "s1") == []


def test_save_refuses_not_expressible_and_no_trades(tmp_path, monkeypatch, capsys):
    """표현 불가 · 거래 0건이면 저장하지 않는다."""
    code, _, err = _save(tmp_path, monkeypatch, capsys, body=_body(signal="c * 0 + 2.0"))
    assert code == 2 and "재현성 점검 불가(표현 불가)" in err
    code, _, err = _save(tmp_path, monkeypatch, capsys, body=_body(signal="c * 0.0"))
    assert code == 2 and "추천 가드레일 점검 불가(가드레일을 만들 수 없음: 왕복 거래 0건)" in err
    assert versions(_root(tmp_path), "s1") == []


def test_save_refuses_link_over_1mib(tmp_path, monkeypatch, capsys):
    """CHANGELOG 한계: save도 check와 같은 해시 읽기(`check/report.py`, 1MiB 상한)를 지나 1MiB 넘는 link.py는 저장하지 않고 종료 2."""
    code, _, err = _save(tmp_path, monkeypatch, capsys, body=_body() + "#" * (1 << 20) + "\n")
    assert code == 2 and "바이트를 넘어 읽지 않습니다" in err and versions(_root(tmp_path), "s1") == []


def test_save_bad_start_and_sensitivity(tmp_path, monkeypatch, capsys):
    """날짜 꼴 · 없는 민감도는 인자 오류(코드 2), 파일 없음."""
    monkeypatch.chdir(tmp_path)
    write_link(tmp_path, _body())
    assert main(["save", "s1", "--start", "2026/09/23"]) == 2
    assert main(["save", "s1", "--sensitivity", "아주민감"]) == 2
    assert "YYYY-MM-DD" in capsys.readouterr().err and versions(_root(tmp_path), "s1") == []


def test_save_help_shows_sensitivity_meaning(capsys):
    """도움말에 세 칸의 뜻(1년 · 한 달 기준)이 고객 말로 나온다(% 글자가 argparse에서 깨지지 않음)."""
    assert main(["save", "--help"]) == 0
    out = capsys.readouterr().out
    assert ("민감 약 46% · 보통 약 20% · 둔감 약 5%(한 달 기준 5% · 1.84% · 0.43%). "
            "기본은 고르는 화면(blackclip connect --open --choose)에서 고른 값, 없으면 보통") in " ".join(out.split())


def test_save_default_is_the_chosen_sensitivity(tmp_path, monkeypatch, capsys):
    """--sensitivity가 없으면 고르는 화면에서 고른 값(connections.json defaults), 주면 그 값."""
    (tmp_path / ".git").mkdir()
    root = tmp_path / ".blackclip"
    root.mkdir()
    (root / "connections.json").write_text(json.dumps({"defaults": {"sensitivity": "민감"}}, ensure_ascii=False))
    code, out, _ = _save(tmp_path, monkeypatch, capsys, body=_body())
    assert code == 0 and out.splitlines()[0].endswith("민감도 민감")
    code, out, _ = _save(tmp_path, monkeypatch, capsys, "--reason", "둔감으로", "--sensitivity", "둔감")
    assert code == 0 and out.splitlines()[0].endswith("민감도 둔감")


def test_check_and_save_same_numbers(tmp_path, monkeypatch, capsys):
    """check의 추천 가드레일 줄과 save가 쓴 파일이 같은 숫자다(같은 씨앗)."""
    monkeypatch.chdir(tmp_path)
    write_link(tmp_path, _body(signal=CLEAN))
    assert main(["check", "s1", "--no-binance"]) == 0
    guard = next(line for line in capsys.readouterr().out.splitlines() if line.startswith("추천 가드레일  "))
    assert main(["save", "s1"]) == 0
    doc = json.loads((_folder(tmp_path) / "guardrail.v1.yaml").read_text(encoding="utf-8"))
    assert guard == "추천 가드레일  민감도 보통 · " + " · ".join(summary(doc, "보통"))
    assert date.fromisoformat(doc["set_on"]) == datetime.now(timezone.utc).date()


def test_save_reason_with_absolute_path_refused(tmp_path, monkeypatch, capsys):
    """--reason에 절대 경로가 있으면 저장하지 않는다(가드레일은 커밋됨)."""
    _save(tmp_path, monkeypatch, capsys, body=_body())
    code, _, err = _save(tmp_path, monkeypatch, capsys, "--reason", "/srv/alice/quant/config.py 비용 변경")
    assert code == 2 and "--reason에 절대 경로가 있음" in err and versions(_root(tmp_path), "s1") == [1]


def test_save_weekly_bars_refused_check_still_runs(tmp_path, monkeypatch, capsys):
    """주봉이면 check는 시도 수 · 국면까지 돌고(코드 0) save는 봉 길이를 말하며 코드 2."""
    body = _body().replace('periods=400, freq="D"', 'periods=400, freq="W"')
    monkeypatch.chdir(tmp_path)
    write_link(tmp_path, body)
    assert main(["check", "s1", "--no-binance"]) == 0
    out = capsys.readouterr().out
    assert "국면  " in out and "시도 수  " in out and "추천 가드레일  점검 불가(가드레일을 만들 수 없어요: 봉 길이 1w" in out
    code, _, err = _save(tmp_path, monkeypatch, capsys)
    assert code == 2 and "봉 길이 1w(이 버전은 하루 이하 봉만" in err and versions(_root(tmp_path), "s1") == []


RUIN = '''
import numpy as np
import pandas as pd

EXEC = "close"
MARKET = "spot"


def load_prices():
    idx = pd.date_range("2024-01-01", periods=400, freq="D", tz="UTC")
    c = np.where(np.arange(400) % 50 == 1, 400.0, 100.0)
    return {"BTCUSDT": pd.DataFrame({"Open": c, "High": c, "Low": c, "Close": c}, index=idx)}


def signals(prices):
    i = np.arange(len(prices["BTCUSDT"]))
    return pd.Series(np.where(i % 2 == 0, -1.0, 0.0), index=prices["BTCUSDT"].index)
'''


def test_save_ruined_backtest_refused(tmp_path, monkeypatch, capsys):
    """백테스트 자산이 0 이하로 떨어지는 전략(가격 4배 날 숏)은 저장하지 않는다."""
    code, _, err = _save(tmp_path, monkeypatch, capsys, body=RUIN)
    assert code == 2 and "가드레일을 만들 수 없음: 백테스트 자산이 0 이하로 떨어짐" in err
    assert versions(_root(tmp_path), "s1") == []


NO_LOSS = '''
import numpy as np
import pandas as pd

EXEC = "close"
MARKET = "spot"


def load_prices():
    idx = pd.date_range("2024-01-01", periods=400, freq="D", tz="UTC")
    c = 100 * 1.006 ** np.arange(400)
    return {"BTCUSDT": pd.DataFrame({"Open": c, "High": c, "Low": c, "Close": c}, index=idx)}


def signals(prices):
    i = np.arange(len(prices["BTCUSDT"]))
    return pd.Series((i % 40 != 0).astype(float), index=prices["BTCUSDT"].index)
'''


def test_save_no_loss_backtest_refused(tmp_path, monkeypatch, capsys):
    """손실 구간이 없는 백테스트는 경고선이 0%라 저장하지 않고 추천 가드레일 줄의 까닭을 보인다."""
    code, _, err = _save(tmp_path, monkeypatch, capsys, body=NO_LOSS)
    assert code == 2 and len(err.strip().splitlines()) == 1
    assert "추천 가드레일 점검 불가(가드레일을 만들 수 없음: 백테스트에 손실 구간이 거의 없어 경고선이 0%)" in err
    assert versions(_root(tmp_path), "s1") == []


@pytest.mark.parametrize("leak", [False, True], ids=["unchecked", "suspect"])
def test_save_refuses_when_worker_died_in_c1(tmp_path, monkeypatch, capsys, leak):
    """미래참조 점검 도중 고객 코드 하위 프로세스가 끝나면(점검 불가 · 의심 뒤 끝남 모두) run_check가 가드레일을 남겨도
    저장하지 않고 고정 문구 한 줄, 원문은 안 보임."""
    class Quick(report.Runner):
        def __init__(self, *a, **k):
            super().__init__(*a, **k)
            self.timeout = 3.0

    monkeypatch.setattr(report, "Runner", Quick)
    die = "print('TAILMARK') or __import__('os')._exit(3)"
    if leak:  # shift(-1)로 새서 미래참조가 먼저 의심을 잡은 뒤 끝남
        extra = f"\n_CALLS = [0]\n\n\ndef _tick():\n    _CALLS[0] += 1\n    if _CALLS[0] > 40:\n        {die}\n"
        body = _body(signal="_tick() or " + CLEAN.replace("c.rolling(10).mean()", "c.rolling(10).mean().shift(-1)"),
                     extra=extra)
    else:
        body = _body(signal=f"({die} if len(c) < 400 else None) or " + CLEAN)
    code, out, err = _save(tmp_path, monkeypatch, capsys, body=body)
    assert code == 2 and out == "" and len(err.strip().splitlines()) == 1
    assert "점검이 끝까지 돌지 않아 가드레일을 만들 수 없어요" in err and "TAILMARK" not in err
    assert versions(_root(tmp_path), "s1") == []


@pytest.mark.parametrize("reason", ["   ", "\t\n"])
def test_save_blank_reason_is_no_reason(tmp_path, monkeypatch, capsys, reason):
    """공백뿐인 --reason은 이유 없음과 같다: v1이면 reason null, v1이 있으면 이유 없음으로 거절."""
    code, _, _ = _save(tmp_path, monkeypatch, capsys, "--reason", reason, body=_body())
    assert code == 0 and json.loads((_folder(tmp_path) / "guardrail.v1.yaml").read_text(encoding="utf-8"))["reason"] is None
    code, _, err = _save(tmp_path, monkeypatch, capsys, "--reason", reason)
    assert code == 2 and "이미 있음" in err and versions(_root(tmp_path), "s1") == [1]


@pytest.mark.parametrize("reason", ["민감으로\n바꿈", "민감\x00", "민감\u200b바꿈"])
def test_save_reason_with_line_break_or_control_refused(tmp_path, monkeypatch, capsys, reason):
    """--reason에 줄바꿈 · 제어 · 서식 문자가 있으면 고정 문구로 종료 2(입력 글을 되풀이하지 않음)."""
    code, _, err = _save(tmp_path, monkeypatch, capsys, "--reason", reason, body=_body())
    assert code == 2 and err == "blackclip save: --reason에 줄바꿈 · 제어 문자 · 보이지 않는 공백 · 서식 문자가 있음 · 한 줄 글로 적어 주세요\n"
    assert versions(_root(tmp_path), "s1") == []


def test_save_nan_in_document_refused(tmp_path, monkeypatch, capsys):
    """가드레일 숫자에 NaN이 섞이면 JSON 밖 글자(NaN)를 쓰지 않고 고정 문구 · 종료 2."""
    real = report.build
    monkeypatch.setattr(report, "build", lambda *a, **k: {**real(*a, **k), "period": {"mean": float("nan")}})
    code, _, err = _save(tmp_path, monkeypatch, capsys, body=_body())
    assert code == 2 and err == "blackclip save: 가드레일을 저장하지 않음 · 숫자가 아닌 값(NaN · 무한대)이 있음\n"
    assert versions(_root(tmp_path), "s1") == []


def test_save_write_error_shows_reason_only(tmp_path, monkeypatch, capsys):
    """가드레일 파일을 못 쓰면 OSError 설명(strerror)만, 이 컴퓨터 경로는 안 보임."""
    import blackclip.check.save as save_mod

    def deny(*a, **k):
        raise PermissionError(13, "Permission denied", str(a[0]))  # 파일 이름 칸에 이 컴퓨터 경로(root)

    monkeypatch.setattr(save_mod, "write_in", deny)
    code, _, err = _save(tmp_path, monkeypatch, capsys, body=_body())
    assert code == 2 and err == "blackclip save: 파일을 읽거나 쓰지 못했습니다(Permission denied)\n"
    assert str(tmp_path) not in err



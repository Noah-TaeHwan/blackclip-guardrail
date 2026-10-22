"""blackclip check: 명령 · 화면 · check.md."""
import re
import subprocess
import sys

import pandas as pd
import pytest

import blackclip.check.report as report
import blackclip.cli as cli
from conftest import FORGE, FORGED, KlineClient, swap_for_link, write_link

from blackclip.check.backtest import Run
from blackclip.check.link import check_prices, load_link
from blackclip.check.report import LABELS, first_look
from blackclip.check.runner import ISOLATION_NOTE
from blackclip.cli import main

BODY = '''
import numpy as np
import pandas as pd

EXEC = "next_open"
MARKET = "spot"


def load_prices():
    rng = np.random.default_rng(0)
    c = 100 * np.exp(np.cumsum(rng.normal(0, 0.02, 400)))
    o = np.concatenate([[100.0], c[:-1]])
    idx = pd.date_range("2024-01-01", periods=400, freq="D", tz=__TZ__)
    return {"BTCUSDT": pd.DataFrame({"Open": o, "High": np.maximum(o, c) * 1.01, "Low": np.minimum(o, c) * 0.99,
                                     "Close": c}, index=idx)}


def signals(prices):
    c = prices["BTCUSDT"]["Close"]
    return __SIGNAL__
'''
CLEAN = "(c.rolling(10).mean() > c.rolling(30).mean()).astype(float)"


def _body(signal=CLEAN, tz='"UTC"', extra=""):
    return BODY.replace("__SIGNAL__", signal).replace("__TZ__", tz) + extra


def _check(tmp_path, monkeypatch, capsys, body, *args):
    monkeypatch.chdir(tmp_path)
    write_link(tmp_path, body)
    code = main(["check", "s1", *args])
    out = capsys.readouterr()
    return code, out.out, out.err


def test_check_cli_writes_check_md(tmp_path, monkeypatch, capsys):
    """돌면 코드 0, 결론 → 근거 순서로 찍고 check.md를 남긴다(절대 경로 없음)."""
    code, out, _ = _check(tmp_path, monkeypatch, capsys, _body(), "--no-binance")
    assert code == 0
    lines = out.splitlines()
    assert re.fullmatch(r"점검 · s1 · 가격표 2024-01-01 ~ 2025-02-03 · 1종목 · 1d 봉 · 다시 돌린 체결 \d+건", lines[0])
    assert lines[1] == "먼저 볼 항목: 국면"
    assert "재현성  재현 확인 전" in out and "미래참조  이 범위에서 통과" in out
    assert "거래비용  " in out and "데이터 차이  건너뜀 · --no-binance" in out
    assert "시도 수  해당 없음 · 비용 0 샤프 " in out and "국면  거래 부족 · " in out
    assert "추천 가드레일  민감도 보통 · 정상 범위 그리지 않음(백테스트 왕복 거래 7건" in out
    assert f"메모: {ISOLATION_NOTE}" in out  # 격리 메모 문구(상수 그대로)
    assert lines[-1] == "저장: .blackclip/strategies/s1/check.md"
    md = (tmp_path / ".blackclip" / "strategies" / "s1" / "check.md").read_text(encoding="utf-8")
    assert md.startswith("# 점검 · s1\n") and "| 미래참조 | 이 범위에서 통과 |" in md
    assert str(tmp_path) not in md and "/Users/" not in md and "/private/" not in md


def test_check_missing_link_exit_2(tmp_path, monkeypatch, capsys):
    """연결 파일이 없으면 코드 2와 자리 한 줄."""
    monkeypatch.chdir(tmp_path)
    write_link(tmp_path, _body())
    assert main(["check", "nope", "--no-binance"]) == 2
    err = capsys.readouterr().err
    assert err.startswith("blackclip check: ") and ".blackclip/strategies/nope/link.py" in err


def test_check_load_prices_crash_exit_2(tmp_path, monkeypatch, capsys):
    """load_prices가 죽으면 코드 2와 한 줄(traceback 없음)."""
    body = _body().replace("    rng = np.random", "    raise RuntimeError('파일 없음')\n    rng = np.random")
    code, _, err = _check(tmp_path, monkeypatch, capsys, body, "--no-binance")
    assert code == 2 and "load_prices(): 실패했습니다(RuntimeError" in err and "Traceback" not in err


def test_check_express_error_exit_0(tmp_path, monkeypatch, capsys):
    """비중이 -1~1 밖이면 점검 불가(표현 불가)로 끝까지 가고 코드 0."""
    code, out, err = _check(tmp_path, monkeypatch, capsys, _body(signal="c * 0 + 2.0"), "--no-binance")
    assert code == 0 and out.count("점검 불가(표현 불가)") == len(LABELS)
    assert "먼저 볼 항목: 재현성" in out
    md = (tmp_path / ".blackclip" / "strategies" / "s1" / "check.md").read_text(encoding="utf-8")
    assert "BTCUSDT: 목표 비중이 -1~1 밖입니다" in err and "-1~1 밖입니다" not in md + out  # 원문은 터미널에만


def test_check_c0_mismatch_blocks_c1_c2(tmp_path, monkeypatch, capsys):
    """원래 거래 목록과 안 맞으면 미래참조 · 거래비용은 점검 불가(재현 불일치)."""
    (tmp_path / "bt").mkdir()
    (tmp_path / "bt" / "t.csv").write_text(
        "time,symbol,side,qty,price,fee,order_id\n"
        "2024-06-01T00:00:00+00:00,BTCUSDT,buy,1,100,0,a\n"
        "2024-06-20T00:00:00+00:00,BTCUSDT,sell,1,100,0,b\n", encoding="utf-8")
    code, out, _ = _check(tmp_path, monkeypatch, capsys, _body(extra='\nTRADES = "bt/t.csv"\n'), "--no-binance")
    assert code == 0 and "재현성  불일치" in out
    assert out.count("점검 불가(재현 불일치)") == len(LABELS) - 2 and "먼저 볼 항목: 재현성" in out  # 재현성 · 데이터 차이 빼고


def test_check_first_look_c1(tmp_path, monkeypatch, capsys):
    """미래참조가 의심이면 미래참조부터(확인 순서)."""
    leak = CLEAN.replace("c.rolling(10).mean()", "c.rolling(10).mean().shift(-1)")
    _, out, _ = _check(tmp_path, monkeypatch, capsys, _body(signal=leak), "--no-binance")
    assert "먼저 볼 항목: 미래참조" in out


def test_check_naive_note_and_naive_signals(tmp_path, monkeypatch, capsys):
    """시간대 없는 가격표면 메모를 남기고, 신호 함수는 어디서나 시간대 없는 가격표를 받는다."""
    sig = CLEAN + " * (1 if c.index.tz is None else 5)"
    _, out, _ = _check(tmp_path, monkeypatch, capsys, _body(signal=sig, tz="None"), "--no-binance")
    assert "메모: 가격표에 시간대가 없어 UTC로 셈함(신호 함수에는 원래 꼴로 넘김)" in out
    assert "미래참조  이 범위에서 통과" in out


def test_check_deterministic(tmp_path, monkeypatch, capsys):
    """두 번 돌려도 화면과 check.md가 같다(점검 시각 줄 빼고)."""
    _, out1, _ = _check(tmp_path, monkeypatch, capsys, _body(), "--no-binance")
    md_path = tmp_path / ".blackclip" / "strategies" / "s1" / "check.md"
    md1 = [line for line in md_path.read_text(encoding="utf-8").splitlines() if not line.startswith("- 점검 시각")]
    assert main(["check", "s1", "--no-binance"]) == 0
    out2 = capsys.readouterr().out
    md2 = [line for line in md_path.read_text(encoding="utf-8").splitlines() if not line.startswith("- 점검 시각")]
    assert out1 == out2 and md1 == md2


def test_check_with_binance_client(tmp_path, monkeypatch, capsys):
    """--no-binance가 없으면 시장에 맞는 클라이언트로 데이터 차이를 잰다."""
    monkeypatch.chdir(tmp_path)
    root = write_link(tmp_path, _body())
    frames, _ = check_prices(load_link(root, "s1").module.load_prices())
    seen = []

    def fake(market, testnet, creds):
        seen.append((market, testnet, creds))
        return KlineClient(frames, market=market)

    monkeypatch.setattr(cli, "_make_client", fake)
    assert main(["check", "s1"]) == 0
    out = capsys.readouterr().out
    assert seen == [("spot", False, None)]
    assert "데이터 차이  잰 것 · 바이낸스 spot 1d 봉과 겹치는 2024-01-01 ~ 2025-02-03: 종가 차이 중앙값 0.0bp" in out


def test_version_does_not_import_pandas():
    """blackclip --version · fetch · connect는 pandas · numpy · blackclip.check를 불러오지 않는다(check에서만)."""
    code = "import sys, blackclip.cli; print([m for m in ('pandas', 'numpy', 'blackclip.check') if m in sys.modules])"
    out = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True, check=True).stdout
    assert out.strip() == "[]"


def test_check_md_has_no_local_paths_from_errors(tmp_path, monkeypatch, capsys):
    """오류 문구에 이 컴퓨터 경로가 들어 있어도 check.md에는 남기지 않는다(커밋됨)."""
    monkeypatch.chdir(tmp_path)
    write_link(tmp_path, _body())
    monkeypatch.setattr(cli, "_make_client",
                        lambda m, t, c: KlineClient({}, market=m, fail=OSError(f"cannot use {tmp_path}/x.sock")))
    assert main(["check", "s1"]) == 0
    md = (tmp_path / ".blackclip" / "strategies" / "s1" / "check.md").read_text(encoding="utf-8")
    assert "데이터 차이 | 확인 불가 | 바이낸스 봉을 못 받음(OSError)" in md and str(tmp_path) not in md


def test_check_futures_funding_note(tmp_path, monkeypatch, capsys):
    """선물이면 거래비용에 펀딩비를 넣지 않았다는 메모를 화면과 check.md에 남긴다."""
    code, out, _ = _check(tmp_path, monkeypatch, capsys, _body().replace('"spot"', '"futures"'), "--no-binance")
    md = (tmp_path / ".blackclip" / "strategies" / "s1" / "check.md").read_text(encoding="utf-8")
    assert code == 0 and f"메모: {report.FUNDING_NOTE}" in out and f"- 메모: {report.FUNDING_NOTE}" in md


@pytest.mark.parametrize("how, mark", [
    ("print('TAILMARK') or __import__('os')._exit(3)", "TAILMARK"),  # 죽음: stderr 꼬리가 LinkError에 붙음
    ("__import__('time').sleep(60)", "끝나지 않았습니다"),  # 멈춤: 시간 초과
], ids=["dies", "hangs"])
def test_check_runner_dies_mid_c1_keeps_results(tmp_path, monkeypatch, capsys, how, mark):
    """잘라 보기 중 하위 프로세스가 끝나면 닫힌 Runner를 다시 부르지 않고 미래참조 점검 불가 · 데이터 차이 확인 불가,
    재현성 · 거래비용은 그대로, 원문은 터미널에만, check.md를 쓰고 코드 0."""
    late: list[bool] = []

    class Quick(report.Runner):
        def __init__(self, *a, **k):
            super().__init__(*a, **k)
            self.timeout = 3.0  # load는 기본 시간으로(pandas 불러오기), 그 뒤 요청만 짧게

        def signals(self, *a, **k):
            late.append(self.proc.returncode is not None)
            return super().signals(*a, **k)

    seen = []
    monkeypatch.setattr(report, "Runner", Quick)
    monkeypatch.setattr(cli, "_make_client", lambda m, t, c: seen.append(m) or KlineClient({}, market=m))
    sig = f"({how} if len(c) < 400 else None) or " + CLEAN
    code, out, err = _check(tmp_path, monkeypatch, capsys, _body(signal=sig))
    md = (tmp_path / ".blackclip" / "strategies" / "s1" / "check.md").read_text(encoding="utf-8")
    assert code == 0 and not any(late) and seen == []
    assert "미래참조  점검 불가(하위 프로세스가 끝남)" in out and "| 미래참조 | 점검 불가(하위 프로세스가 끝남) |" in md
    assert "재현성  재현 확인 전" in out and "거래비용  " in out and "| 거래비용 | " in md
    assert "데이터 차이  확인 불가" in out
    assert mark in err and mark not in md and mark not in out
    guard = next(line for line in out.splitlines() if line.startswith("추천 가드레일"))
    assert guard.endswith(" · (저장 안 됨: 미래참조 점검 중 하위 프로세스가 끝남)")  # 정상 추천처럼 보이지 않게


def test_check_trades_csv_error_exit_2(tmp_path, monkeypatch, capsys):
    """TRADES가 csv 모듈이 못 읽는 파일(칸 > 131072자)이면 traceback 대신 코드 2 · 한 줄 · check.md 없음."""
    (tmp_path / "bt").mkdir()
    (tmp_path / "bt" / "t.csv").write_text(
        "time,symbol,side,qty,price,fee,order_id\n2024-06-01T00:00:00+00:00,BTCUSDT,buy,1,100,0," + "x" * 200_000 + "\n",
        encoding="utf-8")
    code, out, err = _check(tmp_path, monkeypatch, capsys, _body(extra='\nTRADES = "bt/t.csv"\n'), "--no-binance")
    assert code == 2 and out == "" and "Traceback" not in err and len(err.strip().splitlines()) == 1
    assert "t.csv: CSV를 읽을 수 없습니다(Error)" in err and str(tmp_path) not in err
    assert not (tmp_path / ".blackclip" / "strategies" / "s1" / "check.md").exists()


def test_check_md_write_failure_still_shows_result(tmp_path, monkeypatch, capsys):
    """check.md를 못 써도 화면 결과는 먼저 찍고, 쓰기 실패는 한 줄과 코드 2."""
    def boom(*a, **k):
        raise PermissionError(13, "Permission denied")

    monkeypatch.setattr(cli, "write_in", boom)
    code, out, err = _check(tmp_path, monkeypatch, capsys, _body(), "--no-binance")
    assert code == 2 and "미래참조  이 범위에서 통과" in out and "저장:" not in out
    assert err.startswith("blackclip check: check.md를 쓰지 못했습니다") and len(err.strip().splitlines()) == 1


def test_check_md_does_not_follow_parent_link(tmp_path, tmp_path_factory, monkeypatch, capsys):
    """점검이 끝난 뒤 strategies/s1이 레포 밖 폴더 링크로 바뀌어도 check.md는 레포 밖에 생기지 않는다(한 줄 · 종료 2)."""
    outside = tmp_path_factory.mktemp("outside")
    real = report.run_check

    def run_then_swap(root, name, client_factory):
        result = real(root, name, client_factory)
        swap_for_link(root / "strategies" / name, outside)  # 쓰기 직전에 링크를 심음
        return result

    monkeypatch.setattr(report, "run_check", run_then_swap)
    code, _, err = _check(tmp_path, monkeypatch, capsys, _body(), "--no-binance")
    assert code == 2 and err.startswith("blackclip check: check.md를 쓰지 못했습니다(") and "링크" in err
    assert len(err.strip().splitlines()) == 1
    assert list(outside.iterdir()) == []


def test_link_hash_does_not_follow_parent_link(tmp_path, tmp_path_factory, monkeypatch, capsys):
    """해시 단계에서 strategies/s1이 레포 밖 폴더 링크가 되면 레포 밖 link.py를 읽지 않고 한 줄 · 종료 2."""
    outside = tmp_path_factory.mktemp("outside")
    (outside / "link.py").write_text(_body(), encoding="utf-8")  # 읽히면 점검이 끝까지 돌아 코드 0이 된다
    real = report.build

    def build_then_swap(*a, **k):
        got = real(*a, **k)
        swap_for_link(tmp_path / ".blackclip" / "strategies" / "s1", outside)  # link_sha256을 셈하기 직전
        return got

    monkeypatch.setattr(report, "build", build_then_swap)
    code, out, err = _check(tmp_path, monkeypatch, capsys, _body(), "--no-binance")
    assert code == 2 and out == "" and err.startswith("blackclip check: ") and "링크" in err
    assert len(err.strip().splitlines()) == 1


@pytest.mark.parametrize("how", ["__import__('os')._exit(3)", "__import__('time').sleep(60)"], ids=["dies", "hangs"])
def test_check_runner_dies_after_c1_suspect_keeps_suspect(tmp_path, monkeypatch, capsys, how):
    """shift(-1) 새는 신호라 미래참조가 이미 의심인데 그 뒤 하위 프로세스가 끝나면 의심과 근거를 지우지 않고
    「(이후 하위 프로세스가 끝남)」만 붙인다. check.md를 쓰고 코드 0."""
    class Quick(report.Runner):
        def __init__(self, *a, **k):
            super().__init__(*a, **k)
            self.timeout = 3.0

    monkeypatch.setattr(report, "Runner", Quick)
    extra = f"\n_CALLS = [0]\n\n\ndef _tick():\n    _CALLS[0] += 1\n    if _CALLS[0] > 40:\n        {how}\n"
    leak = "_tick() or " + CLEAN.replace("c.rolling(10).mean()", "c.rolling(10).mean().shift(-1)")
    code, out, _ = _check(tmp_path, monkeypatch, capsys, _body(signal=leak, extra=extra), "--no-binance")
    md = (tmp_path / ".blackclip" / "strategies" / "s1" / "check.md").read_text(encoding="utf-8")
    assert code == 0
    c1_line = next(line for line in out.splitlines() if line.startswith("미래참조"))
    assert c1_line.startswith("미래참조  의심 · 데이터 끝을 잘라 다시 돌리면")
    assert "정적 검사 후보" in c1_line and "shift(-n)" in c1_line and c1_line.endswith("(이후 하위 프로세스가 끝남)")
    assert "점검 불가(하위 프로세스가 끝남)" not in out + md
    assert "먼저 볼 항목: 미래참조" in out and "| 미래참조 | 의심 | " in md and "(이후 하위 프로세스가 끝남) |" in md


def test_check_md_binance_5xx_html_stays_one_clean_row(tmp_path, monkeypatch, capsys):
    """바이낸스가 줄바꿈 · IP가 든 HTML 502를 줘도 check.md 데이터 차이 줄은 상태 번호만 담은 한 줄이다."""
    from blackclip.binance.client import Client

    html = b"<html>\n<body>client 203.0.113.7 | cf-ray 1234</body></html>"
    monkeypatch.chdir(tmp_path)
    write_link(tmp_path, _body())
    monkeypatch.setattr(cli, "_make_client", lambda m, t, c: Client(m, t, transport=lambda u, h: (502, {}, html)))
    assert main(["check", "s1"]) == 0
    out = capsys.readouterr().out
    md = (tmp_path / ".blackclip" / "strategies" / "s1" / "check.md").read_text(encoding="utf-8")
    assert "| 데이터 차이 | 확인 불가 | 바이낸스 봉을 못 받음(바이낸스 응답 502) |\n" in md
    assert "데이터 차이  확인 불가 · 바이낸스 봉을 못 받음(바이낸스 응답 502)" in out
    assert "<html" not in md + out and "203.0.113.7" not in md + out


@pytest.mark.parametrize("kind", ["nan", "over_one"])
def test_check_forged_weights_reads_as_express_error(tmp_path, monkeypatch, capsys, kind):
    """고객 코드가 weights.npz를 NaN · 3.0으로 위조해도 traceback 없이 worker 쪽 위반과 같은 점검 불가(표현 불가) · 코드 0."""
    body = _body(signal="_forge() or " + CLEAN, extra=FORGE.replace("__FORGED__", FORGED[kind]))
    code, out, err = _check(tmp_path, monkeypatch, capsys, body, "--no-binance")
    assert code == 0 and out.count("점검 불가(표현 불가)") == len(LABELS) and "Traceback" not in err
    assert "nan%" not in out and "표현 불가:" in err


FORGE_TEXT = """

def _forge():
    import sys
    from pathlib import Path
    m = sys.modules["__main__"]
    orig = m.save_frames

    def save(path, frames):
        if path.name != "weights.npz":
            return orig(path, frames)
        out = Path(sys.argv[3]) / "weights.npz"
        orig(out, frames)  # 정상으로 쓴 뒤 비중 칸만 글자 배열로 바꿔 다시 쓴다
        with np.load(out) as z:
            arrays = {k: z[k] for k in z.files}
        arrays.update({k: np.full(v.shape, "x") for k, v in arrays.items() if k.startswith("v")})
        np.savez(out, **arrays)

    m.save_frames = save
"""


def test_check_forged_text_weights_reads_as_express_error(tmp_path, monkeypatch, capsys):
    """weights.npz 비중 칸을 글자 배열로 위조해도 isfinite TypeError traceback 없이 점검 불가(표현 불가) · 코드 0."""
    code, out, err = _check(tmp_path, monkeypatch, capsys, _body(signal="_forge() or " + CLEAN, extra=FORGE_TEXT),
                            "--no-binance")
    assert code == 0 and out.count("점검 불가(표현 불가)") == len(LABELS) and "Traceback" not in err


def test_check_forged_weights_unknown_symbol_exit_2(tmp_path, monkeypatch, capsys):
    """weights.npz에 가격표에 없는 종목을 끼워 넣어도 KeyError traceback 대신 코드 2 · 한 줄."""
    body = _body(signal="_forge() or " + CLEAN, extra=FORGE.replace("__FORGED__", FORGED["unknown_symbol"]))
    code, out, err = _check(tmp_path, monkeypatch, capsys, body, "--no-binance")
    assert code == 2 and out == "" and "Traceback" not in err and len(err.strip().splitlines()) == 1
    assert "응답이 계약과 다릅니다" in err and "ZZZ" not in err


def test_first_look_order_c3_c4():
    """거래비용 다음 시도 수 하향 → 국면 편중 · 부족 순서, 시도 수 모름은 고르지 않는다."""
    items = {k: {"state": "유지", "text": ""} for k in LABELS}
    items["C3"]["state"], items["C4"]["state"] = "하향(범위)", "국면 편중"
    assert first_look(items) == "시도 수"
    items["C2"]["state"] = "축소"
    assert first_look(items) == "거래비용"
    items["C2"]["state"], items["C3"]["state"] = "유지", "모름"
    assert first_look(items) == "국면"
    items["C4"]["state"] = "충분"
    assert first_look(items) is None
    items["GUARD"]["state"] = "점검 불가(가드레일을 만들 수 없음: 봉 길이 1w)"  # 가드레일만 점검 불가여도 고른다
    assert first_look(items) == "추천 가드레일"


def test_check_flat_signal_guardrail_blocked(tmp_path, monkeypatch, capsys):
    """신호가 한 번도 안 나면 국면 거래 부족 · 추천 가드레일 점검 불가, 그래도 코드 0."""
    code, out, _ = _check(tmp_path, monkeypatch, capsys, _body(signal="c * 0.0"), "--no-binance")
    assert code == 0 and "국면  거래 부족 · 국면을 나눈 거래 0건 < 30건" in out
    assert "추천 가드레일  점검 불가(가드레일을 만들 수 없음: 왕복 거래 0건) · blackclip save가 저장하지 않음" in out


def test_check_md_c3_lowered_keeps_trials_basis(tmp_path, monkeypatch, capsys):
    """샤프가 양수이고 TRIALS가 있으면 check.md 시도 수 칸이 하향(범위)이고 고객이 밝힌 근거 글이 들어간다."""
    leak = "(c.shift(-1) > c).astype(float)"  # 다음 봉을 보는 신호라 비용 0 샤프가 확실히 양수
    _check(tmp_path, monkeypatch, capsys, _body(signal=leak, extra='\nTRIALS = (200, "격자 4x50")\n'), "--no-binance")
    md = (tmp_path / ".blackclip" / "strategies" / "s1" / "check.md").read_text(encoding="utf-8")
    row = next(line for line in md.splitlines() if line.startswith("| 시도 수 |"))
    assert row.startswith("| 시도 수 | 하향(범위) |") and "시도 200개: 고객이 밝힌 값 · 최솟값(미검증) · 격자 4x50)" in row


def test_run_check_guardrail_target(tmp_path, monkeypatch):
    """가드레일 target은 시장 · 체결 규칙 · 종목 · 봉 · 기간 · 연결 파일 해시 · 가격표 해시."""
    import hashlib

    monkeypatch.chdir(tmp_path)
    root = write_link(tmp_path, _body())
    path = root / "strategies" / "s1" / "link.py"
    result = report.run_check(root, "s1", None)
    t = result.guardrail["target"]
    assert {k: t[k] for k in ("market", "exec", "symbols", "bar_seconds", "start", "end")} == {
        "market": "spot", "exec": "next_open", "symbols": ["BTCUSDT"], "bar_seconds": 86400,
        "start": "2024-01-01", "end": "2025-02-03"}
    assert t["link_sha256"] == hashlib.sha256(path.read_bytes()).hexdigest()
    assert len(t["prices_sha256"]) == 64 and int(t["prices_sha256"], 16) >= 0


def test_check_md_has_c3_c4_guard_rows(tmp_path, monkeypatch, capsys):
    """check.md 표에도 시도 수 · 국면 · 추천 가드레일 줄이 있고 「아직 없음」 줄은 없다."""
    _check(tmp_path, monkeypatch, capsys, _body(extra='\nTRIALS = (200, "격자 4x50")\n'), "--no-binance")
    md = (tmp_path / ".blackclip" / "strategies" / "s1" / "check.md").read_text(encoding="utf-8")
    assert "| 시도 수 | 해당 없음 |" in md and "| 국면 | 거래 부족 |" in md and "| 추천 가드레일 | 민감도 보통 |" in md
    assert "아직 없음" not in md


@pytest.mark.parametrize("bars", [0, 1])
def test_check_short_returns_c3_guard_blocked(tmp_path, monkeypatch, capsys, bars):
    """비용 0 수익률이 0~1봉이면 c3 · deflate가 IndexError · OverflowError를 던지는데, 죽지 않고
    시도 수 · 추천 가드레일을 점검 불가로 두고 나머지는 그대로 코드 0."""
    real = report.simulate

    def short(prices, weights, exec_, cost_bp=0.0):
        run = real(prices, weights, exec_, cost_bp)
        if cost_bp:
            return run
        return Run(pd.Series([0.01] * bars, index=run.returns.index[:bars], dtype=float), run.executions)

    monkeypatch.setattr(report, "simulate", short)
    code, out, err = _check(tmp_path, monkeypatch, capsys, _body(), "--no-binance")
    assert code == 0 and "Traceback" not in err
    assert f"시도 수  점검 불가(샤프를 셀 수 없음) · 비용 0 수익률 {bars}봉 · 기간이 너무 짧음" in out
    assert "추천 가드레일  점검 불가(시도 수를 셀 수 없음) · blackclip save가 저장하지 않음" in out
    assert "국면  " in out and "미래참조  이 범위에서 통과" in out


MINUTE_BODY = BODY.replace('periods=400, freq="D"', 'periods=400, freq="min"').replace(
    "rng.normal(0, 0.02, 400)", "rng.normal(0.01, 0.002, 400)")


def test_check_short_minute_bars_big_gain_no_traceback(tmp_path, monkeypatch, capsys):
    """1분 봉 400개(약 6.7시간)에 큰 수익이면 연환산 수익률이 float를 넘는다(backtest.metrics). traceback 없이
    거래비용 · 시도 수 · 추천 가드레일을 점검 불가로 두고 코드 0."""
    body = MINUTE_BODY.replace("__SIGNAL__", "c * 0 + 1.0").replace("__TZ__", '"UTC"')
    code, out, err = _check(tmp_path, monkeypatch, capsys, body, "--no-binance")
    assert code == 0 and "Traceback" not in err
    assert "거래비용  점검 불가(연 수익률을 셀 수 없음) · " in out
    assert "시도 수  점검 불가(샤프를 셀 수 없음) · " in out
    assert "추천 가드레일  점검 불가(" in out


def test_check_md_binance_string_code_not_copied(tmp_path, monkeypatch, capsys):
    """바이낸스가 아닌 JSON 오류가 문자열 code(줄바꿈 · | · IP)를 줘도 check.md 칸에는 상태 번호만."""
    from blackclip.binance.client import Client

    bad = b'{"code": "x\\n| 203.0.113.7", "msg": "no"}'
    monkeypatch.chdir(tmp_path)
    write_link(tmp_path, _body())
    monkeypatch.setattr(cli, "_make_client", lambda m, t, c: Client(m, t, transport=lambda u, h: (400, {}, bad)))
    assert main(["check", "s1"]) == 0
    out = capsys.readouterr().out
    md = (tmp_path / ".blackclip" / "strategies" / "s1" / "check.md").read_text(encoding="utf-8")
    assert "| 데이터 차이 | 확인 불가 | 바이낸스 봉을 못 받음(바이낸스 응답 400) |\n" in md
    assert "203.0.113.7" not in md + out

"""1시간 확인 경고선: 저장 선 + min(0, 흉내 24회 − 1회) · 캐시."""
from blackclip.sim.warning_line import calibrate_lines
from blackclip.watch.line import hourly_lines


def guard(version=1, mean=0.0005, std=0.02):
    return {"version": version, "period": {"seconds": 86400, "mean": mean, "std": std},
            "warning_line": {"lines": {"민감": -0.15, "보통": -0.2, "둔감": -0.25}, "sensitivity": "보통", "line": -0.2}}


def test_hourly_line_adds_the_m4_gap_to_each_saved_line():
    """세 칸 모두 저장 선 + min(0, 24회 − 1회): 1시간 선은 저장 선보다 위로 가지 않는다(경로가 적으면 차이가 잡음이라 0에서 자름)."""
    lines, cache = hourly_lines(guard(), None, paths=2000)
    many, once = calibrate_lines(0.0005, 0.02, 24, paths=2000), calibrate_lines(0.0005, 0.02, 1, paths=2000)
    saved = guard()["warning_line"]["lines"]
    assert lines == {k: round(v + min(0.0, many[k] - once[k]), 6) for k, v in saved.items()}
    assert all(lines[k] <= saved[k] for k in lines)
    assert cache == {"version": 1, "mean": 0.0005, "std": 0.02, "lines": lines}


def test_hourly_line_cache_reused_until_guardrail_changes(monkeypatch):
    """판 · mean · std가 같으면 다시 흉내 내지 않고, 하나라도 바뀌면 다시 셈."""
    lines, cache = hourly_lines(guard(), None, paths=2000)
    import blackclip.watch.line as wl

    monkeypatch.setattr(wl, "calibrate_lines", lambda *a, **k: (_ for _ in ()).throw(AssertionError("다시 셈")))
    assert hourly_lines(guard(), cache)[0] == lines
    monkeypatch.undo()
    assert hourly_lines(guard(version=2), cache, paths=2000)[1]["version"] == 2
    assert hourly_lines(guard(std=0.03), cache, paths=2000)[1]["std"] == 0.03

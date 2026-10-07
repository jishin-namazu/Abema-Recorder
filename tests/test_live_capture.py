from pathlib import Path

from abema_recorder.live.capture import CapturePlan, PIPE_OPTIONS_ENV


def test_capture_plan_keeps_segments_for_timeline_safe_merger(monkeypatch):
    monkeypatch.setenv(PIPE_OPTIONS_ENV, "stale-pipe-output.ts")
    plan = CapturePlan(
        "http://127.0.0.1:18081/index.m3u8",
        Path("archive/run.out"),
        "run",
        Path("N_m3u8DL-RE"),
    )
    argv = plan.argv()
    assert "--skip-merge" in argv
    assert "--live-real-time-merge" not in argv
    assert "--live-pipe-mux" not in argv
    assert argv[argv.index("--live-keep-segments") + 1] == "True"
    assert argv[argv.index("--live-wait-time") + 1] == "2"
    assert argv[argv.index("--download-retry-count") + 1] == "10"
    assert PIPE_OPTIONS_ENV not in plan.environment()


def test_burst_output_cannot_bypass_timeline_safe_merger():
    plan = CapturePlan(
        "http://127.0.0.1:18081/index.m3u8",
        Path("archive/run.out"),
        "run",
        Path("N_m3u8DL-RE"),
        paced_output=False,
    )

    assert "--live-real-time-merge" not in plan.argv()
    assert "--skip-merge" in plan.argv()
    assert "--live-pipe-mux" not in plan.argv()

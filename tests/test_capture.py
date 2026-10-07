"""Capture argv shape for the static skip-merge flow and the live flow."""

from __future__ import annotations

from pathlib import Path

from abema_recorder import capture
from abema_recorder.keys import ContentKey, KeyRing
from abema_recorder.toolchain import Decryptor, Toolchain

KEY = ContentKey("e290c6873b074410a408cdd3763151a1", "c4dcef6a235c704c3b8f77224b9d547d")
TOOLS = Toolchain(downloader=None, muxer=None, decryptor=None, decryptor_kind=Decryptor.SHAKA)


def _plan(**overrides) -> capture.CapturePlan:
    values = dict(
        url="https://example/index.mpd",
        keys=KeyRing([KEY]),
        out_dir=Path("run.out"),
        run_name="run",
        tools=TOOLS,
        live=False,
        keep_shards=True,
    )
    values.update(overrides)
    return capture.CapturePlan(**values)


def test_static_with_shards_skips_downloader_merge() -> None:
    argv = _plan().argv()
    assert "--skip-merge" in argv
    assert argv[argv.index("--check-segments-count") + 1] == "False"
    assert "--live-pipe-mux" not in argv


def test_static_without_shards_lets_downloader_mux() -> None:
    argv = _plan(keep_shards=False).argv()
    assert "--skip-merge" not in argv


def test_live_pipeline_unchanged() -> None:
    argv = _plan(live=True).argv()
    assert "--live-pipe-mux" in argv
    assert argv[argv.index("--live-keep-segments") + 1] == "True"
    assert "--skip-merge" not in argv


def test_quality_replaces_auto_select() -> None:
    argv = _plan(quality="720p").argv()
    assert "--auto-select" not in argv
    assert argv[argv.index("-sv") + 1] == "res=1280x720"


def test_no_quality_keeps_auto_select() -> None:
    assert "--auto-select" in _plan().argv()


def test_record_limit_only_on_live() -> None:
    argv = _plan(live=True, record_limit="01:30:00").argv()
    assert argv[argv.index("--live-record-limit") + 1] == "01:30:00"
    assert "--live-record-limit" not in _plan(record_limit="01:30:00").argv()


def test_static_hls_is_accepted_and_inert() -> None:
    plan = _plan(hls=True)
    assert capture.PIPE_OPTIONS_ENV not in plan.environment()

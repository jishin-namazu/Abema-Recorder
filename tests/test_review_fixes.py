"""Regression tests for the PR review findings."""

from __future__ import annotations

import json
import socket
from pathlib import Path

import pytest

from abema_recorder import records, salvage
from abema_recorder.cli import build_parser
from abema_recorder.errors import ConfigError, SalvageError, SourceError
from abema_recorder.live import resolver
from abema_recorder.live.proxy import HLSProxy


def test_resolver_wraps_streamlink_errors() -> None:
    # example.com matches no Streamlink plugin; the failure must be a SourceError,
    # not a bare NoPluginError traceback.
    with pytest.raises(SourceError):
        resolver.resolve("https://example.com/", "1080p")


def test_proxy_busy_port_raises_config_error() -> None:
    blocker = socket.socket()
    blocker.bind(("127.0.0.1", 0))
    blocker.listen(1)
    port = blocker.getsockname()[1]
    try:
        with pytest.raises(ConfigError):
            HLSProxy(
                "https://abema.tv/now-on-air/x",
                "1080p",
                "127.0.0.1",
                port,
                media_url="https://example.com/playlist.m3u8",
            )
    finally:
        blocker.close()


def test_legacy_hls_record_is_not_treated_as_dash(tmp_path) -> None:
    out_dir = tmp_path / "abema_20260101_000000.out"
    out_dir.mkdir()
    legacy = {
        "source_url": "https://abema.tv/now-on-air/x",
        "media_url": "https://example.com/playlist.m3u8",
        "quality": "1080p",
        "proxy_url": "http://127.0.0.1:18081/index.m3u8",
        "run_name": "abema_20260101_000000",
    }
    (out_dir / "run.json").write_text(json.dumps(legacy), encoding="utf-8")
    assert records.record_engine(legacy) == "hls"
    with pytest.raises(SalvageError):
        salvage.rebuild(out_dir, tmp_path / "out.mkv")


def test_subcommands_accept_settings() -> None:
    parser = build_parser()
    for argv in (["probe", "--settings", "x.env"], ["rebuild", "--settings", "x.env", "t"], ["backfill", "--settings", "x.env", "t"]):
        parser.parse_args(argv)  # must not exit with "unrecognized arguments"

"""URL routing to the two engines, and the local-playlist default rules."""

from __future__ import annotations

from pathlib import Path

import pytest

from abema_recorder import hls, mpd, runbook, source
from abema_recorder.errors import SourceError
from abema_recorder.settings import Settings

FIXTURE = Path(__file__).parent / "fixtures" / "index.mpd"


def test_mpd_url_routes_to_dash() -> None:
    route = source.classify("https://vod-playout-abematv.akamaized.net/tokyo/v6/vod-dash/s/out/index.mpd")
    assert route.engine == source.DASH
    assert route.media_url is None


def test_dash_path_without_mpd_suffix_routes_to_dash() -> None:
    route = source.classify("https://example.akamaized.net/vod-dash/s/out/v1/index")
    assert route.engine == source.DASH


def test_channel_pages_route_to_hls_via_streamlink() -> None:
    for url in (
        "https://abema.tv/now-on-air/abema-special",
        "https://abema.tv/channels/abema-news",
        "https://abema.tv/video/episode/90-1234",
    ):
        route = source.classify(url)
        assert route.engine == source.HLS
        assert route.media_url is None


def test_bare_m3u8_is_preresolved() -> None:
    url = "https://linear-abematv.akamaized.net/channel/abema-special/1080/playlist.m3u8"
    route = source.classify(url)
    assert route.engine == source.HLS
    assert route.media_url == url


def test_unknown_url_is_auto() -> None:
    assert source.classify("https://example.com/watch?v=1").engine == source.AUTO


def test_live_dash_defaults_hls_on() -> None:
    assert runbook.resolve_hls_address(Settings(), live=True) == "127.0.0.1:8080"


def test_static_dash_ignores_hls() -> None:
    assert runbook.resolve_hls_address(Settings(hls_address="127.0.0.1:9000"), live=False) == ""


def test_explicit_hls_address_wins() -> None:
    settings = Settings(hls_address="0.0.0.0:9000")
    assert runbook.resolve_hls_address(settings, live=True) == "0.0.0.0:9000"


def test_burst_output_suppresses_the_default() -> None:
    assert runbook.resolve_hls_address(Settings(paced_output=False), live=True) == ""


def test_bare_port_normalizes_to_loopback() -> None:
    endpoint = hls.Endpoint.parse("8080")
    assert endpoint.host == "127.0.0.1"
    assert endpoint.port == 8080


def test_pick_tracks_matches_requested_quality() -> None:
    playlist = mpd.parse(FIXTURE.read_text(encoding="utf-8"), "https://example/index.mpd")
    tracks = mpd.pick_tracks(playlist, "720p")
    video = next(t for t in tracks if t.kind == mpd.VIDEO)
    assert video.resolution == "1280x720"


def test_pick_tracks_rejects_unknown_quality() -> None:
    playlist = mpd.parse(FIXTURE.read_text(encoding="utf-8"), "https://example/index.mpd")
    with pytest.raises(SourceError):
        mpd.pick_tracks(playlist, "999p")


def test_pick_tracks_rejects_resolution_not_in_manifest() -> None:
    playlist = mpd.parse(FIXTURE.read_text(encoding="utf-8"), "https://example/index.mpd")
    with pytest.raises(SourceError):
        mpd.pick_tracks(playlist, "2160p")


def test_engine_flag_overrides_routing() -> None:
    url = "https://abema.tv/now-on-air/abema-news"
    assert runbook.route_for(Settings(url=url, engine="dash")).engine == source.DASH
    assert runbook.route_for(Settings(url="https://example.com/x.mpd", engine="hls")).engine == source.HLS
    assert runbook.route_for(Settings(url=url)).engine == source.HLS

"""MPD parser tests against a captured ABEMA timeshift manifest."""

from __future__ import annotations

from pathlib import Path

import pytest

from abema_recorder import mpd
from abema_recorder.errors import SourceError

FIXTURE = Path(__file__).parent / "fixtures" / "index.mpd"
MPD_URL = "https://vod-playout-abematv.akamaized.net/example/index.mpd"

EXPECTED_KID = "e290c6873b074410a408cdd3763151a1"
EXPECTED_PSSH = "AAAANHBzc2gAAAAA7e+LqXnWSs6jyCfc1R0h7QAAABQIARIQ4pDGhzsHRBCkCM3TdjFRoQ=="


@pytest.fixture(scope="module")
def playlist() -> mpd.Playlist:
    return mpd.parse(FIXTURE.read_text(encoding="utf-8"), MPD_URL)


def test_widevine_pssh_collected(playlist: mpd.Playlist) -> None:
    assert EXPECTED_PSSH in playlist.protection.payloads


def test_kid_advertised(playlist: mpd.Playlist) -> None:
    assert EXPECTED_KID in playlist.protection.advertised


def test_playready_recorded_but_not_advertised(playlist: mpd.Playlist) -> None:
    # The PlayReady WRM header names the same content KID in little-endian
    # GUID order; it lands in the diagnosis-only set, never in coverage.
    assert EXPECTED_KID in playlist.protection.foreign_kids
    assert playlist.protection.payloads  # a PlayReady PSSH is not a Widevine payload
    assert all("7e+LqXnWSs6jyCfc1R0h7Q" in p for p in playlist.protection.payloads)


def test_renditions_found(playlist: mpd.Playlist) -> None:
    videos = playlist.video_renditions
    audios = playlist.audio_renditions
    assert len(videos) == 5
    assert len(audios) == 5
    best = max(videos, key=lambda r: r.bandwidth)
    assert best.bandwidth == 8_000_000
    assert best.resolution == "1920x1080"
    assert all(a.language == "eng" for a in audios)


def test_static_mpd_is_not_live(playlist: mpd.Playlist) -> None:
    assert playlist.live is False


def test_segment_duration_from_timeline(playlist: mpd.Playlist) -> None:
    # The video SegmentTemplate has no @duration; the timeline says
    # 240240 ticks at timescale 60000 -> 4.004 s per segment, 3630 of them.
    assert playlist.target_duration == pytest.approx(4.004, abs=0.001)
    assert playlist.segment_count == 3630


def test_pick_tracks_takes_best_video_and_one_audio(playlist: mpd.Playlist) -> None:
    tracks = mpd.pick_tracks(playlist)
    kinds = {t.kind for t in tracks}
    assert kinds == {mpd.VIDEO, mpd.AUDIO}
    assert max(t.bandwidth for t in tracks if t.kind == mpd.VIDEO) == 8_000_000


def test_resolve_needed_is_selected_tracks_kids(monkeypatch: pytest.MonkeyPatch) -> None:
    parsed = mpd.parse(FIXTURE.read_text(encoding="utf-8"), MPD_URL)
    monkeypatch.setattr(mpd, "fetch", lambda url, *, timeout=0: parsed)
    source = mpd.resolve(MPD_URL)
    assert source.protection.must_cover == {EXPECTED_KID}
    assert source.live is False
    assert source.segment_ms == 4004


def test_rejects_non_mpd() -> None:
    with pytest.raises(SourceError):
        mpd.parse("<html><body>login</body></html>", MPD_URL)
    with pytest.raises(SourceError):
        mpd.parse("not xml at all", MPD_URL)

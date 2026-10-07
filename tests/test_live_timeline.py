import json
import os
import shutil
import subprocess
from pathlib import Path

import pytest

from abema_recorder.live.timeline import PlaybackBuffer, TimelineMerger


FFMPEG = shutil.which("ffmpeg")
FFPROBE = shutil.which("ffprobe")


def packet_dts(path: Path) -> dict[int, list[float]]:
    result = subprocess.run(
        [
            str(FFPROBE),
            "-v",
            "error",
            "-show_entries",
            "packet=stream_index,dts_time",
            "-of",
            "json",
            str(path),
        ],
        check=True,
        capture_output=True,
        text=True,
    )
    streams: dict[int, list[float]] = {}
    for packet in json.loads(result.stdout)["packets"]:
        if "dts_time" in packet:
            streams.setdefault(packet["stream_index"], []).append(float(packet["dts_time"]))
    return streams


def make_segment(path: Path, offset: float, sample_rate: int = 48000) -> None:
    subprocess.run(
        [
            str(FFMPEG),
            "-y",
            "-hide_banner",
            "-loglevel",
            "error",
            "-f",
            "lavfi",
            "-i",
            "testsrc2=size=160x90:rate=25",
            "-f",
            "lavfi",
            "-i",
            f"sine=frequency=1000:sample_rate={sample_rate}",
            "-t",
            "0.5",
            "-c:v",
            "mpeg2video",
            "-c:a",
            "mp2",
            "-output_ts_offset",
            str(offset),
            "-f",
            "mpegts",
            str(path),
        ],
        check=True,
    )


def audio_stream(path: Path) -> dict[str, str]:
    result = subprocess.run(
        [
            str(FFPROBE),
            "-v",
            "error",
            "-select_streams",
            "a:0",
            "-show_entries",
            "stream=codec_name,sample_rate,channels",
            "-of",
            "json",
            str(path),
        ],
        check=True,
        capture_output=True,
        text=True,
    )
    return json.loads(result.stdout)["streams"][0]


class FakeClock:
    def __init__(self) -> None:
        self.now = 0.0

    def __call__(self) -> float:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += seconds


def test_playback_prebuffers_and_paces_a_segment_burst():
    work = Path(__file__).parent / "fixtures" / f"playback-{os.getpid()}"
    work.mkdir(exist_ok=True)
    clock = FakeClock()
    try:
        playback = PlaybackBuffer(
            max_segments=30,
            prebuffer_segments=10,
            initial_segments=3,
            clock=clock,
        )
        for sequence in range(100, 109):
            path = work / f"{sequence}.ts"
            path.write_bytes(b"segment")
            playback.register(sequence, path, 4.0)
        assert "/playback/" not in playback.playlist("http://127.0.0.1:18081")

        path = work / "109.ts"
        path.write_bytes(b"segment")
        playback.register(109, path, 4.0)

        initial = playback.playlist("http://127.0.0.1:18081")
        assert "/playback/100.ts" in initial
        assert "/playback/102.ts" in initial
        assert "/playback/103.ts" not in initial

        clock.advance(3.99)
        assert "/playback/103.ts" not in playback.playlist("http://127.0.0.1:18081")
        clock.advance(0.01)
        assert "/playback/103.ts" in playback.playlist("http://127.0.0.1:18081")

        # A ten-segment burst is published at media-rate cadence.
        clock.advance(20.0)
        after_24_seconds = playback.playlist("http://127.0.0.1:18081")
        assert "/playback/108.ts" in after_24_seconds
        assert "/playback/109.ts" not in after_24_seconds
        clock.advance(4.0)
        assert "/playback/109.ts" in playback.playlist("http://127.0.0.1:18081")
    finally:
        shutil.rmtree(work, ignore_errors=True)


@pytest.mark.skipif(not FFMPEG or not FFPROBE, reason="ffmpeg and ffprobe are required")
def test_merger_normalizes_main_and_ad_audio_to_48khz_aac():
    work = Path(__file__).parent / "fixtures" / f"audio-{os.getpid()}"
    work.mkdir(exist_ok=True)
    before = work / "200.ts"
    ad = work / "201.ts"
    merged = work / "merged.ts"
    try:
        make_segment(before, 1000, sample_rate=48000)
        make_segment(ad, 10, sample_rate=44100)
        assert audio_stream(before)["sample_rate"] == "48000"
        assert audio_stream(ad)["sample_rate"] == "44100"

        playback = PlaybackBuffer(
            max_segments=6,
            prebuffer_segments=2,
            initial_segments=2,
        )
        merger = TimelineMerger(merged, Path(FFMPEG), Path(FFPROBE), work, playback=playback)
        merger.append_segment(before, 200)
        merger.append_segment(ad, 201)

        for sequence in (200, 201):
            stream = audio_stream(playback.path(sequence))
            assert stream["codec_name"] == "aac"
            assert stream["sample_rate"] == "48000"
            assert stream["channels"] == 2

        merged_audio = audio_stream(merged)
        assert merged_audio["codec_name"] == "aac"
        assert merged_audio["sample_rate"] == "48000"
        decoded = subprocess.run(
            [str(FFMPEG), "-v", "warning", "-i", str(merged), "-f", "null", "-"],
            check=True,
            capture_output=True,
            text=True,
        )
        assert "error" not in decoded.stderr.lower()

        for timestamps in packet_dts(merged).values():
            assert all(current >= previous for previous, current in zip(timestamps, timestamps[1:]))
    finally:
        shutil.rmtree(work, ignore_errors=True)


@pytest.mark.skipif(not FFMPEG or not FFPROBE, reason="ffmpeg and ffprobe are required")
def test_merger_rebases_timestamp_reset_between_main_and_ad():
    work = Path(__file__).parent / "fixtures" / f"timeline-{os.getpid()}"
    work.mkdir(exist_ok=True)
    before = work / "100.ts"
    ad = work / "101.ts"
    merged = work / "merged.ts"
    try:
        make_segment(before, 1000)
        make_segment(ad, 10)
        before_dts = packet_dts(before)[0]
        ad_dts = packet_dts(ad)[0]
        assert ad_dts[0] < before_dts[-1]  # reproduces ABEMA's ad timestamp reset

        playback = PlaybackBuffer(max_segments=6, prebuffer_segments=2, initial_segments=2)
        merger = TimelineMerger(merged, Path(FFMPEG), Path(FFPROBE), work, playback=playback)
        merger.append_segment(before, 100)
        merger.append_segment(ad, 101)

        for timestamps in packet_dts(merged).values():
            assert all(current >= previous for previous, current in zip(timestamps, timestamps[1:]))

        playlist = playback.playlist("http://127.0.0.1:18081")
        assert "#EXT-X-MEDIA-SEQUENCE:100" in playlist
        assert "/playback/100.ts" in playlist
        assert "/playback/101.ts" in playlist
        assert "#EXT-X-DISCONTINUITY" not in playlist
        assert playback.path(100).is_file()
        assert playback.path(101).is_file()

        local_playlist = playlist.replace(
            "http://127.0.0.1:18081/playback/100.ts", ".timeline-normalized/100.ts"
        ).replace(
            "http://127.0.0.1:18081/playback/101.ts", ".timeline-normalized/101.ts"
        )
        playlist_path = work / "index.m3u8"
        playlist_path.write_text(local_playlist + "#EXT-X-ENDLIST\n", encoding="utf-8")
        checked = subprocess.run(
            [str(FFPROBE), "-v", "warning", "-show_packets", "-of", "json", str(playlist_path)],
            check=True,
            capture_output=True,
            text=True,
        )
        assert "Packet corrupt" not in checked.stderr
    finally:
        shutil.rmtree(work, ignore_errors=True)

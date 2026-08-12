"""Build a growing MPEG-TS file with a continuous timestamp timeline."""

from __future__ import annotations

import json
import logging
import math
import shutil
import subprocess
import threading
import time
from collections import OrderedDict, deque
from dataclasses import dataclass
from pathlib import Path
from typing import Callable


LOG = logging.getLogger("abema-recorder.timeline")
MPEG_TS_TICK = 1 / 90_000


@dataclass(frozen=True, slots=True)
class PlaybackSegment:
    sequence: int
    path: Path
    duration: float


class PlaybackBuffer:
    """Thread-safe, paced HLS window of normalized and decrypted segments.

    N_m3u8DL-RE can deliver ABEMA advertisement segments in bursts separated by
    20-25 second pauses.  Publishing a burst immediately moves the HLS live edge
    forward and leaves a player with no reserve for the following pause.  Keep a
    private queue and expose it at media time instead, after an initial buffer
    has accumulated.
    """

    def __init__(
        self,
        max_segments: int = 30,
        *,
        prebuffer_segments: int = 10,
        initial_segments: int = 3,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        if max_segments < 1:
            raise ValueError("max_segments must be positive")
        if prebuffer_segments < 1:
            raise ValueError("prebuffer_segments must be positive")
        if not 1 <= initial_segments <= prebuffer_segments:
            raise ValueError("initial_segments must be between 1 and prebuffer_segments")
        self.max_segments = max_segments
        self.prebuffer_segments = prebuffer_segments
        self.initial_segments = initial_segments
        self._clock = clock
        self._segments: OrderedDict[int, PlaybackSegment] = OrderedDict()
        self._pending: deque[PlaybackSegment] = deque()
        self._known: dict[int, PlaybackSegment] = {}
        self._started = False
        self._next_release_at: float | None = None
        self._lock = threading.RLock()

    def register(self, sequence: int, path: Path, duration: float) -> None:
        with self._lock:
            if sequence in self._known:
                old = self._known[sequence]
                if old.path != path:
                    path.unlink(missing_ok=True)
                return

            now = self._clock()
            # Advance the publication clock even when no player is currently
            # polling.  This bounds retained normalized files and preserves a
            # steady reserve for a player which connects later.
            self._promote(now)
            segment = PlaybackSegment(sequence, path, max(duration, MPEG_TS_TICK))
            pending_was_empty = not self._pending
            self._pending.append(segment)
            self._known[sequence] = segment

            if not self._started and len(self._pending) >= self.prebuffer_segments:
                for _ in range(self.initial_segments):
                    self._publish(self._pending.popleft())
                self._started = True
                self._next_release_at = now + self._segments[next(reversed(self._segments))].duration
                buffered = sum(item.duration for item in self._pending)
                LOG.info(
                    "playback ready: published=%d reserve=%d (%.1fs)",
                    len(self._segments),
                    len(self._pending),
                    buffered,
                )
                return

            # If the reserve was completely exhausted, the next segment must
            # resume playback immediately.  Reset the cadence so a later burst
            # refills the queue instead of being drained all at once.
            if (
                self._started
                and pending_was_empty
                and self._next_release_at is not None
                and self._next_release_at <= now
            ):
                resumed = self._pending.popleft()
                self._publish(resumed)
                self._next_release_at = now + resumed.duration
                LOG.warning("playback reserve exhausted; resumed at sequence=%d", sequence)

    def _publish(self, segment: PlaybackSegment) -> None:
        self._segments[segment.sequence] = segment
        while len(self._segments) > self.max_segments:
            sequence, expired = self._segments.popitem(last=False)
            self._known.pop(sequence, None)
            expired.path.unlink(missing_ok=True)

    def _promote(self, now: float | None = None) -> None:
        if not self._started or self._next_release_at is None:
            return
        if now is None:
            now = self._clock()
        while self._pending and self._next_release_at <= now:
            segment = self._pending.popleft()
            self._publish(segment)
            self._next_release_at += segment.duration

    def path(self, sequence: int) -> Path:
        with self._lock:
            return self._segments[sequence].path

    def playlist(self, local_base: str) -> str:
        with self._lock:
            self._promote()
            segments = list(self._segments.values())
        target = max(1, math.ceil(max((segment.duration for segment in segments), default=6.0)))
        media_sequence = segments[0].sequence if segments else 0
        lines = [
            "#EXTM3U",
            "#EXT-X-VERSION:3",
            f"#EXT-X-TARGETDURATION:{target}",
            f"#EXT-X-MEDIA-SEQUENCE:{media_sequence}",
            "#EXT-X-INDEPENDENT-SEGMENTS",
        ]
        for segment in segments:
            lines += [
                f"#EXTINF:{segment.duration:.6f},",
                f"{local_base.rstrip('/')}/playback/{segment.sequence}.ts",
            ]
        return "\n".join(lines) + "\n"


class TimelineMerger:
    """Remux individual TS segments before appending them.

    ABEMA's advertisements frequently reset PTS/DTS to a small value.  Binary
    concatenation therefore produces a file whose clock jumps backwards.  A
    short FFmpeg process per segment normalizes stream/PID layout and shifts all
    timestamps onto the continuously growing output timeline.
    """

    def __init__(
        self,
        output_path: Path,
        ffmpeg: Path,
        ffprobe: Path,
        work_dir: Path,
        *,
        delete_sources: bool = False,
        playback: PlaybackBuffer | None = None,
    ) -> None:
        self.output_path = output_path
        self.ffmpeg = ffmpeg
        self.ffprobe = ffprobe
        self.work_dir = work_dir
        self.delete_sources = delete_sources
        self.playback = playback
        self.next_offset = 0.0
        self.last_sequence: int | None = None
        self._continuity: dict[int, int] = {}
        self.normalized_dir = work_dir / ".timeline-normalized"
        self.normalized_dir.mkdir(parents=True, exist_ok=True)
        self.output_path.unlink(missing_ok=True)

    def _packet_end(self, path: Path) -> float:
        result = subprocess.run(
            [
                str(self.ffprobe),
                "-v",
                "error",
                "-show_entries",
                "packet=dts_time,duration_time",
                "-of",
                "json",
                str(path),
            ],
            check=True,
            capture_output=True,
            text=True,
        )
        ends: list[float] = []
        for packet in json.loads(result.stdout).get("packets", []):
            if "dts_time" not in packet:
                continue
            ends.append(float(packet["dts_time"]) + float(packet.get("duration_time", 0)))
        if not ends:
            raise RuntimeError(f"no DTS timestamps found in {path}")
        return max(ends)

    def _rewrite_continuity_counters(self, path: Path) -> None:
        """Continue MPEG-TS packet counters across independently remuxed files."""

        data = bytearray(path.read_bytes())
        if len(data) % 188:
            raise RuntimeError(f"{path} is not made of 188-byte MPEG-TS packets")
        for offset in range(0, len(data), 188):
            if data[offset] != 0x47:
                raise RuntimeError(f"MPEG-TS sync byte missing at {path}:{offset}")
            pid = ((data[offset + 1] & 0x1F) << 8) | data[offset + 2]
            adaptation_control = (data[offset + 3] >> 4) & 0x03
            if adaptation_control in (1, 3):  # packet carries payload
                continuity = self._continuity.get(pid, data[offset + 3] & 0x0F)
                data[offset + 3] = (data[offset + 3] & 0xF0) | continuity
                self._continuity[pid] = (continuity + 1) & 0x0F
            elif adaptation_control == 2 and pid in self._continuity:
                data[offset + 3] = (data[offset + 3] & 0xF0) | ((self._continuity[pid] - 1) & 0x0F)
        path.write_bytes(data)

    def append_segment(self, source: Path, sequence: int) -> None:
        normalized = self.normalized_dir / f"{sequence}.ts"
        normalized.unlink(missing_ok=True)
        registered = False
        command = [
            str(self.ffmpeg),
            "-y",
            "-hide_banner",
            "-loglevel",
            "error",
            "-i",
            str(source),
            "-map",
            "0:v:0",
            "-map",
            "0:a:0?",
            "-c:v",
            "copy",
            # ABEMA main content is normally 48 kHz while advertisements can
            # be 44.1 kHz.  A single HLS rendition must keep stable audio
            # parameters, so normalize every segment to stereo 48 kHz AAC.
            "-c:a",
            "aac",
            "-ar",
            "48000",
            "-ac",
            "2",
            "-b:a",
            "192k",
            "-muxdelay",
            "0",
            "-muxpreload",
            "0",
            "-mpegts_flags",
            "+resend_headers",
            "-output_ts_offset",
            f"{self.next_offset:.9f}",
            "-f",
            "mpegts",
            str(normalized),
        ]
        try:
            subprocess.run(command, check=True)
            self._rewrite_continuity_counters(normalized)
            start_offset = self.next_offset
            next_offset = self._packet_end(normalized) + MPEG_TS_TICK
            self.output_path.parent.mkdir(parents=True, exist_ok=True)
            with self.output_path.open("ab") as destination, normalized.open("rb") as data:
                shutil.copyfileobj(data, destination, length=1024 * 1024)
                destination.flush()
            self.next_offset = next_offset
            self.last_sequence = sequence
            if self.playback is not None:
                self.playback.register(sequence, normalized, next_offset - start_offset)
                registered = True
            LOG.info("timeline merged sequence=%d next=%.3fs", sequence, self.next_offset)
            if self.delete_sources:
                source.unlink(missing_ok=True)
        finally:
            if not registered:
                normalized.unlink(missing_ok=True)

    @staticmethod
    def _segments(root: Path) -> dict[int, Path]:
        found: dict[int, Path] = {}
        if not root.exists():
            return found
        for path in root.rglob("*.ts"):
            try:
                sequence = int(path.stem)
            except ValueError:
                continue
            found[sequence] = path
        return found

    def follow(
        self,
        segment_root: Path,
        process: subprocess.Popen,
        *,
        poll_interval: float = 0.25,
        initial_settle: float = 1.0,
        gap_timeout: float = 12.0,
        exit_grace: float = 3.0,
    ) -> int:
        """Append completed segments in sequence order until downloader exits."""

        sizes: dict[int, tuple[int, int]] = {}
        first_seen: float | None = None
        missing_since: float | None = None
        exited_at: float | None = None
        failures: dict[int, int] = {}
        next_sequence: int | None = None

        while True:
            now = time.monotonic()
            paths = self._segments(segment_root)
            stable: set[int] = set()
            for sequence, path in paths.items():
                size = path.stat().st_size
                previous_size, observations = sizes.get(sequence, (-1, 0))
                observations = observations + 1 if size == previous_size and size > 0 else 1
                sizes[sequence] = (size, observations)
                if observations >= 2:
                    stable.add(sequence)

            if paths and first_seen is None:
                first_seen = now
            if next_sequence is None and stable and first_seen is not None and now - first_seen >= initial_settle:
                next_sequence = min(stable)

            if next_sequence is not None:
                if next_sequence in stable:
                    try:
                        self.append_segment(paths[next_sequence], next_sequence)
                    except (OSError, ValueError, subprocess.SubprocessError, json.JSONDecodeError) as exc:
                        failures[next_sequence] = failures.get(next_sequence, 0) + 1
                        if failures[next_sequence] < 3:
                            LOG.warning(
                                "segment %d normalization failed (%d/3): %s",
                                next_sequence,
                                failures[next_sequence],
                                exc,
                            )
                            time.sleep(1)
                            continue
                        LOG.error("skipping unmergeable segment %d: %s", next_sequence, exc)
                    next_sequence += 1
                    missing_since = None
                    continue

                higher = [sequence for sequence in stable if sequence > next_sequence]
                if higher:
                    if missing_since is None:
                        missing_since = now
                    wait_limit = 0.5 if exited_at is not None else gap_timeout
                    if now - missing_since >= wait_limit:
                        LOG.warning("missing segment %d; continuing at %d", next_sequence, min(higher))
                        next_sequence = min(higher)
                        missing_since = None
                        continue
                else:
                    missing_since = None

            returncode = process.poll()
            if returncode is not None and exited_at is None:
                exited_at = now
            if exited_at is not None and now - exited_at >= exit_grace:
                remaining = [] if next_sequence is None else [sequence for sequence in stable if sequence >= next_sequence]
                if not remaining:
                    if self.playback is None:
                        self.normalized_dir.rmdir()
                    return returncode or 0

            time.sleep(poll_interval)

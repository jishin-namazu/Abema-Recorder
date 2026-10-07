"""Rebuilding a playable file from the shards that survived.

Reports per track: what was written, what never finished, what is missing
from the middle, and how much media each track holds.

On a live capture the muxed output is produced through an ffmpeg pipe and is
only as complete as the shutdown was, while each shard is a file that was
either finished or not; a run stopped with SIGTERM leaves a damaged tail on
the muxed file and a directory of intact shards beside it.

Container duration is taken from the longest track: an interrupted run that
left 6 video shards and 14 audio ones produces a file that reports the audio
length, with video covering a fraction of it.
"""

from __future__ import annotations

import subprocess
import threading
from dataclasses import dataclass, field
from pathlib import Path

from . import records, shards
from .errors import SalvageError
from .shards import AUDIO, VIDEO, TrackShards
from .toolchain import Toolchain

REBUILD_ORDER = (VIDEO, AUDIO)


@dataclass(frozen=True, slots=True)
class ShardLocation:
    root: Path
    record: dict | None
    stale: bool
    decrypting: bool | None
    segment_ms: int


def locate(target: Path) -> ShardLocation:
    """Resolve a shard root from either an output directory or a shard directory.

    When a run record is present its recorded paths are tried in order; the
    first that exists wins.
    """
    record = records.read_record(target)
    if record is None:
        return ShardLocation(target, None, stale=False, decrypting=None, segment_ms=0)

    if records.record_engine(record) == "hls":
        # An HLS run keeps .ts segments, not decryptable shards.
        return ShardLocation(target, record, stale=False, decrypting=None, segment_ms=0)

    # A recorded decryptor means the run decrypted, so a plain shard is
    # ciphertext.
    decrypting = True if record.get("decryptor") else None
    segment_ms = record.get("segment_ms") or 0

    candidates: list[Path] = []
    absolute = record.get("shard_root")
    if absolute:
        candidates.append(Path(absolute))
    relative = record.get("shard_root_relative")
    if relative:
        candidates.append((target / relative).resolve())
    session = record.get("session_shard_root")
    if session:
        candidates.append(Path(session))
    run_name = record.get("run_name")
    if run_name:
        candidates.append(shards.shard_root(str(run_name)))

    for candidate in candidates:
        if candidate.is_dir():
            return ShardLocation(
                candidate, record, stale=False, decrypting=decrypting, segment_ms=int(segment_ms)
            )

    if candidates:
        listed = "\n  ".join(str(c) for c in candidates)
        raise SalvageError(
            f"{records.RECORD_NAME} points at shard directories that no longer exist:\n  {listed}",
            remedy="Pass the shard directory directly if it was moved.",
        )
    return ShardLocation(target, record, stale=True, decrypting=decrypting, segment_ms=int(segment_ms))


@dataclass(slots=True)
class TrackOutcome:
    kind: str
    written: int
    missing: int
    unfinished: int
    covered: float
    span: float

    @property
    def intact(self) -> bool:
        return not self.missing and not self.unfinished

    def notes(self) -> list[str]:
        lines = []
        if self.unfinished:
            lines.append(f"{self.kind}: {self.unfinished} shard(s) never finished downloading — omitted")
        if self.missing:
            lines.append(f"{self.kind}: {self.missing} shard(s) missing from the middle of the sequence")
        shortfall = self.span - self.covered
        if shortfall > 1.0:
            lines.append(f"{self.kind}: holds {self.covered:.0f}s of a {self.span:.0f}s span")
        return lines


@dataclass(slots=True)
class Rebuilt:
    destination: Path
    seconds: float
    tracks: list[TrackOutcome] = field(default_factory=list)

    @property
    def intact(self) -> bool:
        return all(track.intact for track in self.tracks)

    def shortest_track(self) -> float:
        """Media every track covers."""
        if not self.tracks:
            return 0.0
        return min(track.covered for track in self.tracks)

    def notes(self) -> list[str]:
        lines = []
        for track in self.tracks:
            lines.extend(track.notes())
        if len(self.tracks) > 1:
            longest = max(t.covered for t in self.tracks)
            shortest = min(t.covered for t in self.tracks)
            if longest - shortest > max(2.0, longest * 0.02):
                lines.append(
                    f"tracks disagree by {longest - shortest:.0f}s — the container will report the longer one"
                )
        return lines


def _concatenate(
    track: TrackShards,
    destination: Path,
    progress=None,
    offset: int = 0,
    total: int = 0,
) -> int:
    """Byte-concatenate a track's shards, init segment first."""
    written = 0
    done = offset
    with destination.open("wb") as out:
        if track.init is not None:
            try:
                data = track.init.read_bytes()
            except OSError as exc:
                raise SalvageError(
                    f"the init segment for {track.kind} is unreadable: {exc}",
                    remedy="Without it this track cannot be decoded. Nothing can be done for it.",
                ) from exc
            out.write(data)
            done += len(data)
        for shard in track.shards:
            try:
                data = shard.read_bytes()
            except OSError:
                # One unreadable shard does not cost the whole rebuild.
                continue
            out.write(data)
            written += 1
            done += len(data)
            if progress is not None:
                progress(track.kind, done, total)
    return written


class _SizeWatch:
    """Reports destination size growth on a thread while ffmpeg writes it."""

    def __init__(self, path: Path, expected: int, progress) -> None:
        self._path = path
        self._expected = expected
        self._progress = progress
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None

    def start(self) -> None:
        self._thread = threading.Thread(target=self._loop, name="mux-progress", daemon=True)
        self._thread.start()

    def _loop(self) -> None:
        while not self._stop.wait(0.5):
            try:
                done = self._path.stat().st_size
            except OSError:
                done = 0
            self._progress("mux", min(done, self._expected), self._expected)

    def stop(self) -> None:
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout=2.0)
        self._progress("mux", self._expected, self._expected)


def _duration(tools: Toolchain, path: Path) -> float:
    prober = tools.prober
    if prober is None:
        return 0.0
    try:
        finished = subprocess.run(
            [
                str(prober),
                "-v",
                "error",
                "-show_entries",
                "format=duration",
                "-of",
                "default=nw=1:nk=1",
                str(path),
            ],
            capture_output=True,
            text=True,
            check=False,
            timeout=60,
        )
        return float(finished.stdout.strip())
    except (OSError, ValueError, subprocess.SubprocessError):
        return 0.0


def rebuild(
    target: Path,
    destination: Path | None = None,
    *,
    tools: Toolchain | None = None,
    progress=None,
) -> Rebuilt:
    tools = tools or Toolchain.discover()
    if tools.muxer is None:
        raise SalvageError(
            "ffmpeg is required to remux the rebuilt tracks",
            remedy="Run this inside the image, which ships ffmpeg.",
        )

    location = locate(target)
    if location.record and records.record_engine(location.record) == "hls":
        raise SalvageError(
            f"{target} was recorded by the HLS engine; the playable file is the .ts in the output directory",
            remedy="rebuild applies to DASH runs, which keep decrypted shards.",
        )
    tracks = shards.discover_tracks(location.root, decrypting=location.decrypting)
    if not tracks:
        raise SalvageError(
            f"no shards found under {location.root}",
            remedy="A run started with shards discarded leaves nothing to rebuild from.",
        )

    hint = location.segment_ms or None
    usable = [t for t in tracks if t.kind in REBUILD_ORDER]
    if not usable:
        raise SalvageError(f"no audio or video tracks under {location.root}")
    usable.sort(key=lambda t: REBUILD_ORDER.index(t.kind))

    destination = destination or location.root.with_name(f"{location.root.name}-rebuilt.mkv")
    destination.parent.mkdir(parents=True, exist_ok=True)
    scratch = destination.parent / f"_{destination.stem}_parts"
    scratch.mkdir(parents=True, exist_ok=True)

    total_bytes = 0
    for track in usable:
        for shard in track.shards:
            try:
                total_bytes += shard.stat().st_size
            except OSError:
                pass
        if track.init is not None:
            try:
                total_bytes += track.init.stat().st_size
            except OSError:
                pass

    outcomes: list[TrackOutcome] = []
    try:
        parts: list[Path] = []
        offset = 0
        for track in usable:
            part = scratch / f"{track.kind}.mp4"
            written = _concatenate(track, part, progress, offset, total_bytes)
            if not written:
                continue
            try:
                offset += part.stat().st_size
            except OSError:
                pass
            parts.append(part)
            outcomes.append(
                TrackOutcome(
                    kind=track.kind,
                    written=written,
                    missing=len(track.gaps(hint)),
                    unfinished=len(track.unfinished),
                    covered=written * track.duration_seconds(hint),
                    span=track.span_seconds(hint),
                )
            )
        if not parts:
            raise SalvageError(f"no readable shards under {location.root}")

        argv = [str(tools.muxer), "-hide_banner", "-loglevel", "error", "-y"]
        for part in parts:
            argv += ["-i", str(part)]
        for index in range(len(parts)):
            # Mapped explicitly; ffmpeg's default picks one stream per type.
            argv += ["-map", str(index)]
        argv += ["-c", "copy", str(destination)]

        watch = None
        if progress is not None:
            expected = 0
            for part in parts:
                try:
                    expected += part.stat().st_size
                except OSError:
                    pass
            watch = _SizeWatch(destination, expected, progress)
            watch.start()
        try:
            finished = subprocess.run(argv, capture_output=True, text=True, check=False)
        finally:
            if watch is not None:
                watch.stop()
        if finished.returncode != 0:
            raise SalvageError(
                f"ffmpeg failed to remux the rebuilt tracks: {finished.stderr.strip()[:300]}"
            )
    finally:
        for leftover in scratch.glob("*"):
            leftover.unlink(missing_ok=True)
        scratch.rmdir()

    return Rebuilt(destination=destination, seconds=_duration(tools, destination), tracks=outcomes)

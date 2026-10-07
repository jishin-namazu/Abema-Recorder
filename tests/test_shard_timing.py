"""TrackShards timing math for ordinal (sequence-numbered) shard stems."""

from __future__ import annotations

from pathlib import Path

from abema_recorder import shards


def _track(tmp_path: Path, count: int) -> shards.TrackShards:
    directory = tmp_path / "0_1_avc1"
    directory.mkdir(parents=True)
    (directory / "_init.mp4").write_bytes(b"INIT")
    for index in range(count):
        (directory / f"{index:04d}_dec.m4s").write_bytes(b"x" * 64)
    return shards.read_track(directory, decrypting=True)


def test_ordinal_stems_use_hint_duration(tmp_path) -> None:
    track = _track(tmp_path, 100)
    assert track.duration_seconds(4000) == 4.0
    assert track.covered_seconds(4000) == 400.0
    assert track.span_seconds(4000) == 400.0


def test_ordinal_stems_without_hint_fall_back(tmp_path) -> None:
    track = _track(tmp_path, 100)
    # No manifest hint: the floor value applies.
    assert track.duration_seconds(None) >= 0.5


def test_ms_stems_ignore_hint(tmp_path) -> None:
    directory = tmp_path / "0_1_avc1"
    directory.mkdir(parents=True)
    (directory / "_init.mp4").write_bytes(b"INIT")
    for index in range(10):
        (directory / f"{index * 4000}_dec.m4s").write_bytes(b"x" * 64)
    track = shards.read_track(directory, decrypting=True)
    assert track.duration_seconds(6000) == 4.0  # measured 4000 wins over the 6000 hint
    assert track.span_seconds(6000) == 40.0


def test_gap_settle_tolerates_out_of_order() -> None:
    ledger = shards.GapLedger(settle=30)
    # A burst 15 positions wide with holes inside the settle window: quiet.
    stamps = [s for s in range(16) if s not in (3, 7)]
    assert ledger.unreported("video", stamps) == []
    # The same holes persist once the sequence runs 30 past them: reported.
    stamps = [s for s in range(60) if s not in (3, 7)]
    assert ledger.unreported("video", stamps) == [3, 7]


def test_gap_settle_default_stays_narrow() -> None:
    ledger = shards.GapLedger()
    stamps = [s for s in range(10) if s != 5]
    assert ledger.unreported("video", stamps) == [5]

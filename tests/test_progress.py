"""Progress reporting during rebuild: concatenation callback and console bar."""

from __future__ import annotations

from pathlib import Path

from abema_recorder import console, salvage, shards


def _track(tmp_path: Path, sizes=(64, 128, 256)) -> shards.TrackShards:
    directory = tmp_path / "0_1_avc1"
    directory.mkdir(parents=True)
    (directory / "_init.mp4").write_bytes(b"I" * 32)
    for index, size in enumerate(sizes):
        (directory / f"{index:04d}_dec.m4s").write_bytes(b"x" * size)
    return shards.read_track(directory, decrypting=True)


def test_concatenate_reports_cumulative_bytes(tmp_path) -> None:
    track = _track(tmp_path)
    calls = []
    total = 32 + 64 + 128 + 256
    written = salvage._concatenate(
        track, tmp_path / "out.mp4", lambda kind, done, tot: calls.append((kind, done, tot)), 0, total
    )
    assert written == 3
    assert calls[-1] == (track.kind, total, total)
    dones = [done for _, done, _ in calls]
    assert dones == sorted(dones)


def test_console_progress_bar(capsys) -> None:
    console.progress("video", 50 * 1_048_576, 100 * 1_048_576)
    console.progress("video", 100 * 1_048_576, 100 * 1_048_576)
    console.progress_done()
    out = capsys.readouterr().out
    assert "video" in out
    assert "50.0%" in out
    assert "100.0%" in out
    assert out.endswith("\n")

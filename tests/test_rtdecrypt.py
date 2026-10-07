"""Real-time shard decryption: the worker and the watcher's decrypt branch."""

from __future__ import annotations

import subprocess
from pathlib import Path

from abema_recorder import rtdecrypt, shards
from abema_recorder.keys import ContentKey, KeyRing

KEY = ContentKey("e290c6873b074410a408cdd3763151a1", "c4dcef6a235c704c3b8f77224b9d547d")


def _make_track(root: Path, name: str = "0_1_avc1") -> Path:
    track = root / name
    track.mkdir(parents=True)
    (track / "_init.mp4").write_bytes(b"INIT")
    (track / "0000.m4s").write_bytes(b"SHARD0")
    (track / "0001.m4s").write_bytes(b"SHARD1")
    return track


def test_decryptor_runs_shaka_and_places_output(tmp_path, monkeypatch) -> None:
    track = _make_track(tmp_path)
    seen = {}

    def fake_run(argv, **kwargs):
        seen["argv"] = argv
        combined = Path(argv[3].split("=", 1)[1].split(",")[0])
        assert combined.read_bytes() == b"INITSHARD0"
        decrypted = Path(argv[3].rsplit("output=", 1)[1])
        decrypted.write_bytes(b"PLAIN")
        return subprocess.CompletedProcess(argv, 0)

    monkeypatch.setattr(subprocess, "run", fake_run)
    done = []
    worker = rtdecrypt.ShardDecryptor(Path("/bin/shaka"), KeyRing([KEY]), workers=1)
    worker.start(lambda path, kind: done.append((path, kind)))
    worker.submit(track / "_init.mp4", track / "0000.m4s", tmp_path / "out" / "0000_dec.m4s", "video")
    worker.stop()

    assert (tmp_path / "out" / "0000_dec.m4s").read_bytes() == b"PLAIN"
    assert done == [(tmp_path / "out" / "0000_dec.m4s", "video")]
    argv = seen["argv"]
    assert argv[1:3] == ["--quiet", "--enable_raw_key_decryption"]
    assert argv[5] == f"key_id={KEY.kid}:key={KEY.key}"


def test_decryptor_failure_warns_and_continues(tmp_path, monkeypatch) -> None:
    track = _make_track(tmp_path)
    monkeypatch.setattr(
        subprocess, "run", lambda *a, **k: subprocess.CompletedProcess(a, 1)
    )
    warnings = []
    worker = rtdecrypt.ShardDecryptor(Path("/bin/shaka"), KeyRing([KEY]), workers=1, warn=warnings.append)
    worker.start(lambda path, kind: None)
    worker.submit(track / "_init.mp4", track / "0000.m4s", tmp_path / "out" / "0000_dec.m4s", "video")
    worker.submit(track / "_init.mp4", track / "0001.m4s", tmp_path / "out" / "0001_dec.m4s", "video")
    worker.stop()
    assert len(warnings) == 2


def test_decryptor_delete_source_removes_ciphertext(tmp_path, monkeypatch) -> None:
    track = _make_track(tmp_path)

    def fake_run(argv, **kwargs):
        decrypted = Path(argv[3].rsplit("output=", 1)[1])
        decrypted.write_bytes(b"PLAIN")
        return subprocess.CompletedProcess(argv, 0)

    monkeypatch.setattr(subprocess, "run", fake_run)
    worker = rtdecrypt.ShardDecryptor(
        Path("/bin/shaka"), KeyRing([KEY]), workers=1, delete_source=True
    )
    worker.start(lambda path, kind: None)
    worker.submit(track / "_init.mp4", track / "0000.m4s", tmp_path / "out" / "0000_dec.m4s", "video")
    worker.stop()
    assert (tmp_path / "out" / "0000_dec.m4s").read_bytes() == b"PLAIN"
    assert not (track / "0000.m4s").exists()
    assert (track / "0001.m4s").exists()  # untouched: never submitted


class _FakeDecryptor:
    def __init__(self) -> None:
        self.jobs = []

    def submit(self, init, shard, destination, kind) -> None:
        self.jobs.append((init, shard, destination, kind))


def test_watcher_submits_ciphertext_and_adopts_existing(tmp_path) -> None:
    session = tmp_path / "session"
    mirror = tmp_path / "mirror"
    _make_track(session)
    _make_track(mirror)  # same layout; 0001 already decrypted from an earlier session
    (mirror / "0_1_avc1" / "0001_dec.m4s").write_bytes(b"PLAIN1")

    decryptor = _FakeDecryptor()
    watcher = shards.ShardWatcher(session, mirror_root=mirror, decryptor=decryptor)
    watcher.sweep()

    assert [(job[1].name, job[2].name) for job in decryptor.jobs] == [("0000.m4s", "0000_dec.m4s")]
    # 0001 was adopted, not submitted, and counts toward the tally.
    assert watcher.tallies[watcher.tallies.keys().__iter__().__next__()].count >= 1

    watcher.sweep()
    assert len(decryptor.jobs) == 1  # nothing is submitted twice


def test_watcher_without_init_waits(tmp_path) -> None:
    session = tmp_path / "session"
    track = session / "0_1_avc1"
    track.mkdir(parents=True)
    (track / "0000.m4s").write_bytes(b"SHARD0")

    decryptor = _FakeDecryptor()
    watcher = shards.ShardWatcher(session, mirror_root=tmp_path / "mirror", decryptor=decryptor)
    watcher.sweep()
    assert decryptor.jobs == []

    (track / "_init.mp4").write_bytes(b"INIT")
    watcher.sweep()
    assert [job[1].name for job in decryptor.jobs] == ["0000.m4s"]

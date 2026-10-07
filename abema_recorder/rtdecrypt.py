"""Per-shard decryption while a static capture runs.

The downloader's static-DASH pipeline keeps ciphertext on disk until its
final mux. This worker decrypts each shard as the watcher reports it — the
same shaka-packager invocation as the backfill path — and places
``<stem>_dec.m4s`` in the shared shard directory.
"""

from __future__ import annotations

import queue
import shutil
import subprocess
import tempfile
import threading
from pathlib import Path

from .keys import KeyRing


class ShardDecryptor:
    """Decrypts shards on a small pool of worker threads.

    Jobs are ``(init, shard, destination, kind)``. Completion is reported
    through the callback handed to ``start``, on the worker thread that did
    the work; the callback must tolerate concurrency.
    """

    def __init__(
        self,
        binary: Path,
        keys: KeyRing,
        *,
        workers: int = 2,
        warn=None,
        delete_source: bool = False,
    ) -> None:
        self._binary = str(binary)
        self._keys = ",".join(f"key_id={key.kid}:key={key.key}" for key in keys)
        self._warn = warn
        self._workers = workers
        # ``delete_source`` removes each ciphertext shard once its decrypted
        # copy exists. Requires the downloader to run with --skip-merge.
        self.delete_source = delete_source
        self._jobs: queue.Queue[tuple[Path, Path, Path, str] | None] = queue.Queue(maxsize=128)
        self._threads: list[threading.Thread] = []
        self._on_done = None

    def start(self, on_done) -> None:
        if self._threads:
            return
        self._on_done = on_done
        for index in range(self._workers):
            thread = threading.Thread(
                target=self._work, name=f"shard-decrypt-{index}", daemon=True
            )
            thread.start()
            self._threads.append(thread)

    def submit(self, init: Path, shard: Path, destination: Path, kind: str) -> None:
        self._jobs.put((init, shard, destination, kind))

    def stop(self) -> None:
        """Drain every queued job, then join the workers."""
        for _ in self._threads:
            self._jobs.put(None)
        for thread in self._threads:
            thread.join()
        self._threads.clear()

    # -- workers -----------------------------------------------------------

    def _work(self) -> None:
        while True:
            job = self._jobs.get()
            if job is None:
                return
            init, shard, destination, kind = job
            try:
                self._decrypt_one(init, shard, destination)
            except Exception as exc:
                if self._warn:
                    self._warn(f"could not decrypt {kind} shard {shard.name}: {exc}")
                continue
            if self.delete_source and destination.exists():
                try:
                    shard.unlink()
                except OSError:
                    pass
            if self._on_done is not None:
                self._on_done(destination, kind)

    def _decrypt_one(self, init: Path, shard: Path, destination: Path) -> None:
        destination.parent.mkdir(parents=True, exist_ok=True)
        if destination.exists():
            return
        with tempfile.TemporaryDirectory(prefix="abm-dec-") as scratch:
            scratch_dir = Path(scratch)
            combined = scratch_dir / "combined.mp4"
            decrypted = scratch_dir / "decrypted.m4s"
            with combined.open("wb") as out:
                with init.open("rb") as source:
                    shutil.copyfileobj(source, out)
                with shard.open("rb") as source:
                    shutil.copyfileobj(source, out)
            finished = subprocess.run(
                [
                    self._binary,
                    "--quiet",
                    "--enable_raw_key_decryption",
                    f"input={combined},stream=0,output={decrypted}",
                    "--keys",
                    self._keys,
                ],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                timeout=120,
                check=False,
            )
            if finished.returncode != 0 or not decrypted.is_file() or not decrypted.stat().st_size:
                raise OSError(f"shaka-packager exited {finished.returncode}")
            staging = destination.with_name(f".{destination.name}.dec.tmp")
            shutil.copyfile(decrypted, staging)
        if destination.exists():
            staging.unlink(missing_ok=True)
            return
        staging.replace(destination)

"""Key coverage: the gate before capture, and the watch during it.

The gate asks once, "do these keys cover the tracks about to be captured?"
The watch asks repeatedly, "has that changed?": a live broadcast can rotate
its keys mid-stream, and everything captured after that point will not
decrypt.

The watch has three outcomes, kept distinct: coverage held, a rotation
detected, or coverage could never be checked.

For a static (timeshift) MPD a rotation cannot happen — the document does
not change — so the watch re-reads the same manifest each interval.
"""

from __future__ import annotations

import threading
from dataclasses import dataclass
from pathlib import Path

from . import mpd
from .errors import CoverageGap
from .keys import KeyRing
from .protection import Protection


@dataclass(frozen=True, slots=True)
class GateReport:
    needed: frozenset[str]
    held: frozenset[str]
    missing: frozenset[str]
    spare: frozenset[str]

    @property
    def covered(self) -> bool:
        return not self.missing


def inspect(keys: KeyRing, protection: Protection) -> GateReport:
    needed = protection.must_cover
    return GateReport(
        needed=needed,
        held=keys.kids,
        missing=keys.gap(needed),
        spare=protection.spare,
    )


def enforce(keys: KeyRing, protection: Protection, *, strict: bool = True) -> GateReport:
    """Refuse to capture a stream the keys do not cover.

    ``strict=False`` records the gap and proceeds; those tracks will not
    decrypt.
    """
    report = inspect(keys, protection)
    if report.covered or not strict:
        return report
    if not report.needed:
        return report  # nothing to compare against; warn, do not block
    raise CoverageGap(
        f"the keys do not cover this stream — missing KID(s): {', '.join(sorted(report.missing))}",
        remedy=(
            "Keys on hand cover: "
            + (", ".join(sorted(report.held)) or "(none)")
            + ". Re-acquire the license; these belong to a different stream or session."
        ),
    )


class RotationWatch:
    """Re-checks coverage on a timer while capture runs.

    Runs on a background thread beside a live recording. A transient fetch
    failure is counted as neither a rotation nor a success; a run that never
    managed a single check says so.
    """

    QUIET_FAILURES = 3

    def __init__(
        self,
        url: str,
        keys: KeyRing,
        *,
        interval: float = 240.0,
        shard_root: Path | None = None,
        accepted: frozenset[str] = frozenset(),
        echo=None,
    ) -> None:
        self.url = url
        self.keys = keys
        self.interval = interval
        self.shard_root = shard_root
        self.accepted = accepted
        self.echo = echo

        self.checks = 0
        self.rotations = 0
        self.failures = 0
        self.total_failures = 0
        # Pre-seeded with gaps the operator already accepted; the first
        # heartbeat does not re-report them.
        self._announced: set[str] = set(accepted)
        self._blind = False
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None

    # -- lifecycle -------------------------------------------------------

    def start(self) -> None:
        if self._thread is not None:
            return
        self._thread = threading.Thread(target=self._loop, name="rotation-watch", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout=3.0)
            self._thread = None

    def _loop(self) -> None:
        while not self._stop.wait(self.interval):
            try:
                self._heartbeat()
            except Exception:
                # check_once already absorbs fetch failures; this covers the
                # reporting around it.
                continue

    # -- checking --------------------------------------------------------

    def _selection(self) -> tuple[mpd.Rendition, ...] | None:
        if self.shard_root is None:
            return None
        reported = mpd.read_reported_selection(self.shard_root)
        if not reported:
            return None
        try:
            source = mpd.fetch(self.url)
        except Exception:
            return None
        return mpd.align_selection(source, reported)

    def check_once(self) -> frozenset[str] | None:
        """KIDs the stream now needs and we lack. ``None`` means could not check."""
        try:
            source = mpd.resolve(self.url, selection=self._selection())
        except Exception:
            self.failures += 1
            self.total_failures += 1
            if self.failures >= self.QUIET_FAILURES and not self._blind:
                self._blind = True
                self._say(
                    f"coverage has not been verifiable for {self.failures} attempts — "
                    "a key rotation would go unnoticed"
                )
            return None
        self.failures = 0
        self._blind = False
        self.checks += 1
        return self.keys.gap(source.protection.must_cover)

    def _heartbeat(self) -> None:
        missing = self.check_once()
        if missing is None:
            return
        fresh = sorted(missing - self._announced)
        if not fresh:
            return
        self._announced.update(fresh)
        self.rotations += 1
        self._say(
            f"the stream now needs KID(s) with no key: {', '.join(fresh)}\n"
            "The stream rotated its keys. Everything from here will not decrypt — "
            "stop, re-acquire the license, and start a new run."
        )

    def _say(self, message: str) -> None:
        if self.echo:
            self.echo(message)

    # -- summary ---------------------------------------------------------

    def summary(self) -> str:
        if self.rotations:
            return (
                f"key coverage: {self.rotations} rotation(s) detected — "
                "part of this recording will not decrypt"
            )
        if not self.checks:
            if not self.total_failures:
                # The run ended before the first interval elapsed; nothing
                # failed.
                return (
                    "key coverage: not re-checked — the run was shorter than one "
                    f"{self.interval:.0f}s interval"
                )
            return (
                f"key coverage: never re-checked ({self.total_failures} failed attempt(s)) — "
                "a rotation would not have been noticed"
            )
        note = f"key coverage: held across {self.checks} re-check(s)"
        if self.accepted:
            note += f", not counting {len(self.accepted)} KID(s) knowingly left uncovered"
        if self.total_failures:
            note += f" ({self.total_failures} attempt(s) failed)"
        return note

"""abema_recorder — a container-first archiver for ABEMA streams.

Two engines share one CLI. The DASH engine archives Widevine-protected DASH:
a timeshift/replay event serves a static MPD, a real-time live event a dynamic
one. The HLS engine records ABEMA channel pages and bare media playlists
through Streamlink and a local proxy; see live/.

The DASH half splits along one line: what the stream *is* (mpd, protection,
keys, coverage) and what is *done* about it (capture, salvage). Key
acquisition means replaying a license request against a local CDM; the token
is an input to this tool.
"""

from __future__ import annotations

__version__ = "1.0.0"


def main(argv: list[str] | None = None) -> int:
    from .cli import main as _main

    return _main(argv)


__all__ = ["__version__", "main"]

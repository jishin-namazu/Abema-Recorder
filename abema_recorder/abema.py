"""Resolve ABEMA URLs through Streamlink's maintained ABEMA plugin."""

from __future__ import annotations

from dataclasses import dataclass

from streamlink import Streamlink


@dataclass(slots=True)
class ResolvedABEMA:
    session: Streamlink
    media_url: str
    quality: str
    available: tuple[str, ...]


def resolve(source_url: str, quality: str) -> ResolvedABEMA:
    session = Streamlink()
    streams = session.streams(source_url)
    stream = streams.get(quality)
    if stream is None:
        available = ", ".join(sorted(streams))
        raise RuntimeError(f"quality {quality!r} is unavailable; available: {available}")
    media_url = getattr(stream, "url", "")
    if not media_url:
        raise RuntimeError("resolved ABEMA stream has no HLS media URL")
    return ResolvedABEMA(session, media_url, quality, tuple(sorted(streams)))


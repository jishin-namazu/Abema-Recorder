"""Resolve ABEMA URLs through Streamlink's maintained ABEMA plugin."""

from __future__ import annotations

from dataclasses import dataclass

from streamlink import Streamlink

from ..errors import SourceError

DEFAULT_QUALITY = "1080p"


@dataclass(slots=True)
class ResolvedABEMA:
    session: Streamlink
    media_url: str
    quality: str
    available: tuple[str, ...]


def resolve(source_url: str, quality: str, media_url: str | None = None) -> ResolvedABEMA:
    session = Streamlink()
    if media_url:
        # A bare .m3u8 --url is the media playlist itself; no resolution step.
        return ResolvedABEMA(session, media_url, quality, ())
    try:
        streams = session.streams(source_url)
    except Exception as exc:
        raise SourceError(f"could not resolve {source_url}: {exc}") from exc
    stream = streams.get(quality)
    if stream is None:
        available = ", ".join(sorted(streams))
        raise SourceError(f"quality {quality!r} is unavailable; available: {available}")
    resolved_url = getattr(stream, "url", "")
    if not resolved_url:
        raise SourceError("resolved ABEMA stream has no HLS media URL")
    return ResolvedABEMA(session, resolved_url, quality, tuple(sorted(streams)))


def has_stream(url: str) -> bool:
    try:
        return bool(Streamlink().streams(url))
    except Exception:
        return False

"""Route a stream URL to the DASH engine or the HLS engine.

``*.mpd`` and DASH paths go to the Widevine/DASH engine. ABEMA channel and
now-on-air pages go to the HLS engine, which resolves them through Streamlink.
A bare ``*.m3u8`` is itself the HLS media playlist; it is used directly and no
resolution step runs. Anything else is probed with Streamlink first and falls
back to DASH.
"""

from __future__ import annotations

from dataclasses import dataclass
from urllib.parse import urlsplit

DASH = "dash"
HLS = "hls"
AUTO = "auto"


@dataclass(frozen=True, slots=True)
class Route:
    engine: str
    # Pre-resolved HLS media URL, set when --url is itself the playlist.
    media_url: str | None = None


def classify(url: str) -> Route:
    value = url.strip()
    parts = urlsplit(value)
    path = parts.path.lower()
    host = (parts.hostname or "").lower()
    if path.endswith(".mpd") or "dash" in path:
        return Route(DASH)
    if path.endswith(".m3u8"):
        return Route(HLS, media_url=value)
    if host == "abema.tv" or host.endswith(".abema.tv"):
        return Route(HLS)
    return Route(AUTO)

"""Pure HLS playlist rewriting.

ABEMA's media playlist mixes encrypted ``/tslive/`` segments with clear
``/tsad/`` segments and marks timeline/encoding boundaries using
``#EXT-X-DISCONTINUITY``.  Playback playlists retain those tags so players can
reset their decoders and clocks; an optional clean endpoint can hide them.
"""

from __future__ import annotations

import base64
import re
from dataclasses import dataclass
from pathlib import PurePosixPath
from urllib.parse import urljoin, urlsplit


URI_ATTRIBUTE_RE = re.compile(r'URI="([^"]+)"')


@dataclass(frozen=True, slots=True)
class Segment:
    sequence: int
    url: str
    mode: str
    discontinuity: bool


@dataclass(frozen=True, slots=True)
class RewrittenPlaylist:
    text: str
    segments: tuple[Segment, ...]


def encode_url(url: str) -> str:
    return base64.urlsafe_b64encode(url.encode("utf-8")).rstrip(b"=").decode("ascii")


def decode_url(token: str) -> str:
    token += "=" * (-len(token) % 4)
    return base64.urlsafe_b64decode(token.encode("ascii")).decode("utf-8")


def resource_name(url: str) -> str:
    parts = urlsplit(url)
    if parts.scheme == "abematv-license":
        return "key.bin"
    name = PurePosixPath(parts.path).name or "resource.bin"
    return re.sub(r"[^A-Za-z0-9._-]", "_", name)


def resource_url(local_base: str, remote_url: str) -> str:
    return f"{local_base.rstrip('/')}/resource/{encode_url(remote_url)}/{resource_name(remote_url)}"


def segment_mode(url: str) -> str:
    if "/tsad/" in url:
        return "tsad"
    if "/tslive/" in url:
        return "tslive"
    return "other"


def rewrite_playlist(
    text: str,
    playlist_url: str,
    local_base: str,
    *,
    strip_discontinuity: bool = False,
) -> RewrittenPlaylist:
    output: list[str] = []
    segments: list[Segment] = []
    sequence = 0
    pending_discontinuity = False

    for raw_line in text.splitlines():
        line = raw_line.strip()
        if line.startswith("#EXT-X-MEDIA-SEQUENCE:"):
            try:
                sequence = int(line.split(":", 1)[1])
            except ValueError:
                pass
            output.append(raw_line)
            continue

        if line == "#EXT-X-DISCONTINUITY":
            pending_discontinuity = True
            if not strip_discontinuity:
                output.append(raw_line)
            continue

        if line.startswith("#EXT-X-DISCONTINUITY-SEQUENCE:"):
            if not strip_discontinuity:
                output.append(raw_line)
            continue

        if line.startswith("#"):
            def replace_uri(match: re.Match[str]) -> str:
                remote = urljoin(playlist_url, match.group(1))
                return f'URI="{resource_url(local_base, remote)}"'

            output.append(URI_ATTRIBUTE_RE.sub(replace_uri, raw_line))
            continue

        if not line:
            output.append(raw_line)
            continue

        remote = urljoin(playlist_url, line)
        output.append(resource_url(local_base, remote))
        segments.append(Segment(sequence, remote, segment_mode(remote), pending_discontinuity))
        sequence += 1
        pending_discontinuity = False

    return RewrittenPlaylist("\n".join(output) + "\n", tuple(segments))

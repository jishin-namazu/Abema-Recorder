"""DASH MPD parsing, track selection, and resolution.

One fetch produces every track and every ContentProtection element; the
shapes exposed (Rendition, Playlist, Source, resolve) are manifest-agnostic.
Namespaces are matched by local name (``{*}Element``); ``cenc`` is declared
inline on the elements that use it and its prefix varies between generators.
"""

from __future__ import annotations

import base64
import json
import re
from dataclasses import dataclass
from pathlib import Path
from xml.etree import ElementTree

from . import netio, protection
from .errors import SourceError
from .protection import Protection

VIDEO = "video"
AUDIO = "audio"
SUBTITLE = "subtitle"

# Matched as a substring of schemeIdUri: servers spell the UUID with and
# without a urn prefix.
_WIDEVINE_MARK = "edef8ba9"
_PLAYREADY_MARK = "9a04f079"
_MP4PROTECTION_MARK = "mp4protection"

_PRO_KID = re.compile(r"<KID>([^<]+)</KID>")

_MIME_KINDS = {"video": VIDEO, "audio": AUDIO, "text": SUBTITLE, "subtitle": SUBTITLE}

# Streamlink-style quality labels mapped to the rendition resolution passed to
# the downloader's -sv selector.
QUALITY_RESOLUTIONS = {
    "2160p": "3840x2160",
    "1080p": "1920x1080",
    "720p": "1280x720",
    "480p": "854x480",
    "360p": "640x360",
}


def _local(attribute: str) -> str:
    """The local name of a possibly namespaced attribute or tag."""
    return attribute.rsplit("}", 1)[-1]


def _find(element: ElementTree.Element, name: str) -> ElementTree.Element | None:
    return element.find(f"{{*}}{name}")


def _findall(element: ElementTree.Element, name: str) -> list[ElementTree.Element]:
    return element.findall(f"{{*}}{name}")


@dataclass(frozen=True, slots=True)
class Rendition:
    """One selectable track in a manifest."""

    url: str
    kind: str = VIDEO
    bandwidth: int = 0
    resolution: str = ""
    group_id: str = ""
    name: str = ""
    language: str = ""
    default: bool = False
    # KIDs of the AdaptationSet this representation belongs to; DASH declares
    # protection per AdaptationSet.
    kids: frozenset[str] = frozenset()

    def describe(self) -> str:
        if self.kind == VIDEO:
            detail = self.resolution or f"{self.bandwidth // 1000} kbps"
            return f"video {detail}"
        label = self.language or self.name or self.group_id or "-"
        return f"{self.kind} {label}"


@dataclass(frozen=True, slots=True)
class Playlist:
    url: str
    is_master: bool
    live: bool
    segment_count: int
    renditions: tuple[Rendition, ...]
    protection: Protection
    target_duration: float = 0.0
    media_sequence: int = 0

    @property
    def audio_renditions(self) -> tuple[Rendition, ...]:
        return tuple(r for r in self.renditions if r.kind == AUDIO)

    @property
    def video_renditions(self) -> tuple[Rendition, ...]:
        return tuple(r for r in self.renditions if r.kind == VIDEO)


def _playready_kids(element: ElementTree.Element) -> list[str]:
    """KIDs out of a PlayReady ContentProtection element, for diagnosis only.

    The ``mspr:pro`` payload is base64 of a UTF-16LE XML header whose ``<KID>``
    is base64 of the 16 KID bytes in little-endian GUID order; the first three
    groups are byte-reversed before hex-encoding. PlayReady keys never take
    part in coverage.
    """
    pro = _find(element, "pro")
    if pro is None or not pro.text:
        return []
    try:
        xml = base64.b64decode("".join(pro.text.split())).decode("utf-16-le", "replace")
    except Exception:
        return []
    kids = []
    for match in _PRO_KID.findall(xml):
        try:
            raw = base64.b64decode("".join(match.split()))
        except Exception:
            continue
        if len(raw) != 16:
            continue
        kid = raw[0:4][::-1] + raw[4:6][::-1] + raw[6:8][::-1] + raw[8:]
        kids.append(kid.hex())
    return kids


def _content_protection(element: ElementTree.Element) -> tuple[list[str], set[str], set[str]]:
    """(payloads, kids, foreign kids) of one parent element.

    Reads the ContentProtection children of an AdaptationSet or Representation.
    ``kids`` are the Widevine-relevant ones: everything the Widevine PSSH names,
    plus the ``cenc:default_KID`` from the mp4protection element, the content
    KID the license must answer to.
    """
    payloads: list[str] = []
    kids: set[str] = set()
    foreign: set[str] = set()
    for guard in _findall(element, "ContentProtection"):
        scheme = (guard.get("schemeIdUri") or "").lower()
        if _MP4PROTECTION_MARK in scheme:
            for attribute, value in guard.attrib.items():
                if _local(attribute) == "default_KID" and value:
                    kids.update(k.lower() for k in re.split(r"[,\s]+", value) if k.strip())
        elif _WIDEVINE_MARK in scheme:
            pssh = _find(guard, "pssh")
            if pssh is None or not pssh.text or not pssh.text.strip():
                continue
            payload = "".join(pssh.text.split())
            read = protection.read_kids_quietly(payload)
            if not read:
                continue
            if payload not in payloads:
                payloads.append(payload)
            kids.update(read)
        elif _PLAYREADY_MARK in scheme:
            foreign.update(_playready_kids(guard))
    normalised = set()
    for kid in kids:
        try:
            normalised.add(protection_normalise(kid))
        except ValueError:
            continue
    return payloads, normalised, foreign


def protection_normalise(kid: str) -> str:
    """Bare lowercase hex for a KID read from an attribute (dashes allowed)."""
    stripped = kid.replace("-", "").strip().lower()
    if not re.fullmatch(r"[0-9a-f]{32}", stripped):
        raise ValueError(f"not a 128-bit hex KID: {kid!r}")
    return stripped


def _segment_geometry(
    adaptation_set: ElementTree.Element,
) -> tuple[float, int, int]:
    """(segment seconds, segment count, start number) for one AdaptationSet.

    Reads the SegmentTemplate — at the AdaptationSet, else on its first
    Representation. The duration comes from ``@duration/@timescale`` when
    present, else from the SegmentTimeline's average ``@d``; ABEMA's VOD MPDs
    use the timeline form. (0, 0, 0) when nothing can be derived.
    """
    template = _find(adaptation_set, "SegmentTemplate")
    if template is None:
        for representation in _findall(adaptation_set, "Representation"):
            template = _find(representation, "SegmentTemplate")
            if template is not None:
                break
    if template is None:
        return 0.0, 0, 0

    timescale = int(template.get("timescale") or 1)
    start = int(template.get("startNumber") or 1)

    duration = template.get("duration")
    if duration and timescale:
        return int(duration) / timescale, 0, start

    timeline = _find(template, "SegmentTimeline")
    if timeline is None:
        return 0.0, 0, start
    total_ticks = 0
    total_segments = 0
    for s in _findall(timeline, "S"):
        d = int(s.get("d") or 0)
        repeat = int(s.get("r") or 0)
        if repeat < 0:
            # r="-1" runs to the end of the Period; the count is unknowable
            # from the timeline, so no geometry is reported.
            return 0.0, 0, start
        total_ticks += d * (repeat + 1)
        total_segments += repeat + 1
    if not total_segments or not timescale:
        return 0.0, 0, start
    return total_ticks / total_segments / timescale, total_segments, start


def _kind_of(adaptation_set: ElementTree.Element) -> str:
    declared = (adaptation_set.get("contentType") or "").lower()
    if declared in _MIME_KINDS:
        return _MIME_KINDS[declared]
    mime = (adaptation_set.get("mimeType") or "").split("/", 1)[0].lower()
    if mime in _MIME_KINDS:
        return _MIME_KINDS[mime]
    for representation in _findall(adaptation_set, "Representation"):
        mime = (representation.get("mimeType") or "").split("/", 1)[0].lower()
        if mime in _MIME_KINDS:
            return _MIME_KINDS[mime]
    return VIDEO


def parse(text: str, url: str = "") -> Playlist:
    """Parse MPD text. Pure: never fetches anything."""
    where = f" from {url}" if url else ""
    try:
        root = ElementTree.fromstring(text)
    except ElementTree.ParseError as exc:
        raise SourceError(
            f"the response{where} is not a DASH manifest ({exc})",
            remedy="Check the URL — a login page or an API error page also returns 200.",
        ) from exc
    if _local(root.tag) != "MPD":
        raise SourceError(
            f"the response{where} is not a DASH manifest",
            remedy="Check the URL — a login page or an API error page also returns 200.",
        )

    live = (root.get("type") or "static").lower() == "dynamic"

    renditions: list[Rendition] = []
    payloads: list[str] = []
    advertised: set[str] = set()
    foreign: set[str] = set()
    target_duration = 0.0
    segment_count = 0
    media_sequence = 0

    for period in _findall(root, "Period"):
        for adaptation_set in _findall(period, "AdaptationSet"):
            kind = _kind_of(adaptation_set)
            language = adaptation_set.get("lang") or ""
            set_payloads, set_kids, set_foreign = _content_protection(adaptation_set)
            for payload in set_payloads:
                if payload not in payloads:
                    payloads.append(payload)
            advertised.update(set_kids)
            foreign.update(set_foreign)

            seconds, count, start = _segment_geometry(adaptation_set)
            if seconds and kind == VIDEO and not target_duration:
                target_duration, segment_count, media_sequence = seconds, count, start

            for representation in _findall(adaptation_set, "Representation"):
                rep_payloads, rep_kids, rep_foreign = _content_protection(representation)
                for payload in rep_payloads:
                    if payload not in payloads:
                        payloads.append(payload)
                advertised.update(rep_kids)
                foreign.update(rep_foreign)
                kids = set_kids | rep_kids

                width, height = representation.get("width"), representation.get("height")
                resolution = f"{width}x{height}" if width and height else ""
                try:
                    bandwidth = int(representation.get("bandwidth") or 0)
                except ValueError:
                    bandwidth = 0
                renditions.append(
                    Rendition(
                        url=url,
                        kind=kind,
                        bandwidth=bandwidth,
                        resolution=resolution,
                        group_id=adaptation_set.get("id") or "",
                        name=representation.get("id") or "",
                        language=language,
                        kids=frozenset(kids),
                    )
                )

    if not target_duration:
        # No video geometry — an audio-only manifest still gets a duration hint.
        for period in _findall(root, "Period"):
            for adaptation_set in _findall(period, "AdaptationSet"):
                seconds, count, start = _segment_geometry(adaptation_set)
                if seconds:
                    target_duration, segment_count, media_sequence = seconds, count, start
                    break
            if target_duration:
                break

    return Playlist(
        url=url,
        is_master=bool(renditions),
        live=live,
        segment_count=segment_count,
        renditions=tuple(renditions),
        protection=Protection(
            payloads=tuple(payloads),
            advertised=frozenset(advertised),
            foreign_kids=frozenset(foreign),
        ),
        target_duration=target_duration,
        media_sequence=media_sequence,
    )


def fetch(url: str, *, timeout: float = netio.DEFAULT_TIMEOUT) -> Playlist:
    try:
        reply = netio.get(url, timeout=timeout)
    except netio.TransportError as exc:
        raise SourceError(
            f"could not reach {url}: {exc}",
            remedy="Check connectivity, and HTTPS_PROXY if you are behind one.",
        ) from exc
    if not reply.ok:
        raise SourceError(
            f"{url} returned HTTP {reply.status}",
            remedy="A manifest URL can expire. Re-copy it from the player.",
        )
    return parse(reply.text, url)


def pick_tracks(playlist: Playlist, quality: str = "") -> tuple[Rendition, ...]:
    """Predict which renditions the downloader's selection will take.

    Highest-bandwidth video plus one audio track, or the video rendition
    matching ``quality`` when one is requested. Subtitles are excluded:
    they are unencrypted and contribute no KID.
    """
    chosen: list[Rendition] = []
    videos = playlist.video_renditions
    audios = playlist.audio_renditions
    if videos:
        if quality:
            wanted = QUALITY_RESOLUTIONS.get(quality)
            if wanted is None:
                raise SourceError(
                    f"unknown quality: {quality}",
                    remedy="Use one of: " + ", ".join(QUALITY_RESOLUTIONS),
                )
            matched = [r for r in videos if r.resolution == wanted]
            if not matched:
                available = ", ".join(sorted({r.resolution for r in videos if r.resolution}))
                raise SourceError(
                    f"no {wanted} video rendition in this manifest",
                    remedy=f"Available resolutions: {available or '(none declared)'}.",
                )
            chosen.append(max(matched, key=lambda r: r.bandwidth))
        else:
            chosen.append(max(videos, key=lambda r: (r.bandwidth, r.resolution)))
    if audios:
        chosen.append(next((a for a in audios if a.default), audios[0]))
    return tuple(chosen)


SELECTION_FILE = "meta_selected.json"

_AUDIO_FIELDS = (("group_id", "GroupId"), ("name", "Name"), ("language", "Language"))
_VIDEO_FIELDS = (("bandwidth", "Bandwidth"), ("resolution", "Resolution"))
_UNPROTECTED_MEDIA = frozenset({"SUBTITLES", "CLOSED-CAPTIONS"})


def read_reported_selection(shard_root: Path) -> list[dict] | None:
    """The downloader's own record of what it chose, if it has written one.

    Written with a BOM, which ``json.loads`` rejects, hence ``utf-8-sig``.
    """
    path = shard_root / SELECTION_FILE
    try:
        document = json.loads(path.read_text(encoding="utf-8-sig"))
    except (OSError, ValueError):
        return None
    return document if isinstance(document, list) else None


def _match_score(rendition: Rendition, entry: dict) -> int | None:
    media_type = str(entry.get("MediaType") or "").upper()
    if media_type in _UNPROTECTED_MEDIA:
        return None
    if media_type == "AUDIO":
        if rendition.kind != AUDIO:
            return None
        fields = _AUDIO_FIELDS
    elif media_type in ("", "VIDEO"):
        if rendition.kind != VIDEO:
            return None
        fields = _VIDEO_FIELDS
    else:
        return None

    score = 0
    for attribute, reported_key in fields:
        reported = entry.get(reported_key)
        mine = getattr(rendition, attribute)
        if reported in (None, "", 0) or mine in ("", 0):
            continue  # absent on either side is no evidence, not a mismatch
        if str(reported) != str(mine):
            return None
        score += 1
    return score or None


def align_selection(playlist: Playlist, reported: list[dict] | None) -> tuple[Rendition, ...] | None:
    """Map the downloader's reported tracks back onto manifest renditions.

    All or nothing: one unmatched entry discards the whole mapping and the
    caller falls back to prediction.
    """
    if not reported:
        return None
    matched: list[Rendition] = []
    for entry in reported:
        if str(entry.get("MediaType") or "").upper() in _UNPROTECTED_MEDIA:
            continue
        scored = [
            (score, r)
            for r in playlist.renditions
            if (score := _match_score(r, entry)) is not None
        ]
        if not scored:
            return None
        matched.append(max(scored, key=lambda pair: pair[0])[1])
    return tuple(matched) or None


@dataclass(frozen=True, slots=True)
class Source:
    """A resolved stream: the manifest plus the protection picture behind it."""

    playlist: Playlist
    protection: Protection
    tracks: tuple[Rendition, ...]

    @property
    def url(self) -> str:
        return self.playlist.url

    @property
    def live(self) -> bool:
        return self.playlist.live

    @property
    def segment_ms(self) -> int:
        """Declared segment duration in milliseconds, 0 when unknown."""
        return round(self.playlist.target_duration * 1000)


def resolve(
    url: str,
    *,
    selection: tuple[Rendition, ...] | None = None,
    quality: str = "",
    timeout: float = netio.DEFAULT_TIMEOUT,
) -> Source:
    """Fetch an MPD and compute the KIDs the predicted selection needs.

    ``needed`` is the union of the KIDs attached to the tracks the downloader
    is expected to pick.
    """
    root = fetch(url, timeout=timeout)
    tracks = selection or pick_tracks(root, quality)

    needed: set[str] = set()
    for track in tracks:
        needed |= track.kids
    if not tracks:
        needed |= root.protection.advertised

    resolved = Protection(
        payloads=root.protection.payloads,
        advertised=root.protection.advertised,
        needed=frozenset(needed),
        foreign_kids=root.protection.foreign_kids,
    )
    return Source(playlist=root, protection=resolved, tracks=tracks)

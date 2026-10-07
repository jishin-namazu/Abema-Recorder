"""Reading KIDs out of Widevine PSSH payloads.

Two container shapes reach this module:

* a full ``pssh`` box, as embedded in a manifest's ``<cenc:pssh>`` element
* a bare Widevine CENC header — the protobuf on its own, with no box around
  it, which is how several tools print PSSH and how some manifests inline it

A payload can name several KIDs (one stream routinely uses a different KID
per track), so every function here returns a list.
"""

from __future__ import annotations

import base64
import struct
from dataclasses import dataclass, field
from typing import Iterator

from .errors import ProtectionError

WIDEVINE_SYSTEM_ID = bytes.fromhex("edef8ba979d64acea3c827dcd51d21ed")

_BOX_TYPE = slice(4, 8)
_VERSION = 8
_SYSTEM_ID = slice(12, 28)
_HEADER_END = 28

_KEY_ID_FIELD = 2
_LENGTH_DELIMITED = 2
_KID_BYTES = 16


def decode_base64(text: str) -> bytes:
    """Decode base64 strictly, tolerating stray whitespace and lost padding.

    ``validate=True``: without it, base64 silently drops characters outside
    the alphabet and any text at all "decodes" into rubbish.
    """
    packed = "".join(text.split())
    packed += "=" * (-len(packed) % 4)
    try:
        return base64.b64decode(packed, validate=True)
    except Exception as exc:  # binascii.Error and friends
        raise ProtectionError(f"PSSH is not valid base64: {exc}") from exc


def _varint(buf: bytes, at: int) -> tuple[int, int]:
    value = 0
    shift = 0
    while True:
        if at >= len(buf):
            raise ProtectionError("protobuf ended mid-varint")
        byte = buf[at]
        at += 1
        value |= (byte & 0x7F) << shift
        if not byte & 0x80:
            return value, at
        shift += 7
        if shift > 63:
            raise ProtectionError("varint wider than 64 bits")


def _walk(buf: bytes) -> Iterator[tuple[int, int, bytes | int]]:
    """Yield ``(field, wire_type, value)`` for a protobuf message.

    Hand-rolled: the only field of interest is a repeated bytes field.
    """
    at = 0
    while at < len(buf):
        tag, at = _varint(buf, at)
        field_no, wire = tag >> 3, tag & 0x07
        if wire == 0:
            value, at = _varint(buf, at)
            yield field_no, wire, value
        elif wire == _LENGTH_DELIMITED:
            size, at = _varint(buf, at)
            end = at + size
            if end > len(buf):
                raise ProtectionError("protobuf field runs past the payload")
            yield field_no, wire, buf[at:end]
            at = end
        elif wire in (1, 5):
            width = 8 if wire == 1 else 4
            if at + width > len(buf):
                raise ProtectionError("protobuf fixed field is truncated")
            yield field_no, wire, buf[at : at + width]
            at += width
        else:
            raise ProtectionError(f"unsupported protobuf wire type {wire}")


def _kids_in_header(payload: bytes) -> list[str]:
    """Every ``key_id`` in a Widevine CENC header, in declaration order."""
    found = []
    for field_no, wire, value in _walk(payload):
        if field_no != _KEY_ID_FIELD or wire != _LENGTH_DELIMITED:
            continue
        assert isinstance(value, bytes)
        if len(value) == _KID_BYTES:
            found.append(value.hex())
    return found


def _looks_like_bare_header(raw: bytes) -> bool:
    # A Widevine header opens with either algorithm (field 1) or key_id
    # (field 2); both are the first byte of the tag, 0x08 and 0x12.
    return raw[:1] in (b"\x08", b"\x12")


def read_kids(pssh_b64: str) -> list[str]:
    """All KIDs named by a base64 PSSH payload, deduplicated, order kept."""
    raw = decode_base64(pssh_b64)
    if _looks_like_bare_header(raw):
        kids = _kids_in_header(raw)
        if not kids:
            raise ProtectionError("Widevine header carried no key IDs")
        return kids

    if len(raw) < _HEADER_END or raw[_BOX_TYPE] != b"pssh":
        raise ProtectionError("neither a pssh box nor a bare Widevine header")
    system_id = raw[_SYSTEM_ID]
    if system_id != WIDEVINE_SYSTEM_ID:
        raise ProtectionError(
            f"pssh box belongs to DRM system {system_id.hex()}, not Widevine"
        )

    kids: list[str] = []
    at = _HEADER_END
    if raw[_VERSION] >= 1:
        if at + 4 > len(raw):
            raise ProtectionError("pssh box ends before its KID count")
        (count,) = struct.unpack_from(">I", raw, at)
        at += 4
        if at + count * _KID_BYTES > len(raw):
            raise ProtectionError("pssh box ends inside its KID list")
        for _ in range(count):
            kids.append(raw[at : at + _KID_BYTES].hex())
            at += _KID_BYTES

    if at + 4 <= len(raw):
        (size,) = struct.unpack_from(">I", raw, at)
        at += 4
        for kid in _kids_in_header(raw[at : at + size]):
            if kid not in kids:
                kids.append(kid)

    if not kids:
        raise ProtectionError("pssh box parsed but named no key IDs")
    return kids


def read_kids_quietly(pssh_b64: str) -> list[str]:
    """``read_kids`` returning ``[]`` on malformed input.

    For sweeping a manifest, where one malformed element among several good
    ones is noise.
    """
    try:
        return read_kids(pssh_b64)
    except ProtectionError:
        return []


@dataclass(frozen=True, slots=True)
class Protection:
    """What a manifest says about encryption.

    ``advertised`` is every KID the manifest mentions. ``needed`` is the
    subset belonging to tracks that will actually be captured: a manifest may
    declare a KID for a low-bitrate variant that automatic track selection
    never picks. The gate asks about ``needed`` and reports the difference as
    informational.

    ``foreign_kids`` are KIDs belonging to DRM systems this tool does not
    license — PlayReady in an ABEMA MPD. They never take part in coverage: no
    Widevine key answers to them. Recorded for diagnosis only.
    """

    payloads: tuple[str, ...] = ()
    advertised: frozenset[str] = frozenset()
    needed: frozenset[str] = frozenset()
    foreign_kids: frozenset[str] = field(default=frozenset())

    @property
    def encrypted(self) -> bool:
        return bool(self.payloads)

    @property
    def must_cover(self) -> frozenset[str]:
        """KIDs the gate insists on.

        Falls back to everything advertised when the selection is unknown.
        """
        return self.needed or self.advertised

    @property
    def spare(self) -> frozenset[str]:
        """Advertised but not needed."""
        return self.advertised - self.must_cover

    def merge(self, other: "Protection") -> "Protection":
        payloads = list(self.payloads)
        for payload in other.payloads:
            if payload not in payloads:
                payloads.append(payload)
        return Protection(
            payloads=tuple(payloads),
            advertised=self.advertised | other.advertised,
            needed=self.needed | other.needed,
            foreign_kids=self.foreign_kids | other.foreign_kids,
        )

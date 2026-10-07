"""Content keys and the coverage question.

A key is thirty-two lowercase hex characters of KID and the same again of
key. The shape is enforced at construction: every consumer downstream — the
downloader's ``--key`` argument, the coverage comparison, the artefact file —
assumes it, and a KID that arrives as a dashed UUID from one source and as
bare hex from another compares unequal while naming the same track.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Iterable, Iterator

_HEX32 = re.compile(r"\A[0-9a-fA-F]{32}\Z")
_PAIR = re.compile(r"([0-9a-fA-F]{32})\s*:\s*([0-9a-fA-F]{32})")


def normalise_kid(text: str) -> str:
    """Reduce any spelling of a KID to bare lowercase hex."""
    stripped = text.strip().replace("-", "").replace(" ", "")
    if not _HEX32.match(stripped):
        raise ValueError(f"not a 128-bit hex KID: {text!r}")
    return stripped.lower()


@dataclass(frozen=True, slots=True)
class ContentKey:
    kid: str
    key: str

    def __post_init__(self) -> None:
        object.__setattr__(self, "kid", normalise_kid(self.kid))
        object.__setattr__(self, "key", normalise_kid(self.key))

    def __str__(self) -> str:
        return f"{self.kid}:{self.key}"

    @classmethod
    def parse(cls, text: str) -> "ContentKey":
        match = _PAIR.search(text)
        if not match:
            raise ValueError(f"expected KID:KEY, got {text!r}")
        return cls(match.group(1), match.group(2))


class KeyRing:
    """An immutable, KID-keyed collection of content keys."""

    __slots__ = ("_by_kid",)

    def __init__(self, keys: Iterable[ContentKey] = ()) -> None:
        collected: dict[str, ContentKey] = {}
        for key in keys:
            collected.setdefault(key.kid, key)
        self._by_kid = collected

    # -- construction ----------------------------------------------------

    @classmethod
    def scrape(cls, text: str) -> "KeyRing":
        """Pull every KID:KEY pair out of arbitrary text.

        Scans for the shape; no particular separator is demanded.
        """
        return cls(ContentKey(kid, key) for kid, key in _PAIR.findall(text))

    def with_keys(self, keys: Iterable[ContentKey]) -> "KeyRing":
        """Union, keeping the key already held when a KID repeats."""
        return KeyRing(list(self) + list(keys))

    # -- inspection ------------------------------------------------------

    @property
    def kids(self) -> frozenset[str]:
        return frozenset(self._by_kid)

    def __iter__(self) -> Iterator[ContentKey]:
        return iter(sorted(self._by_kid.values(), key=lambda k: k.kid))

    def __len__(self) -> int:
        return len(self._by_kid)

    def __bool__(self) -> bool:
        return bool(self._by_kid)

    def __eq__(self, other: object) -> bool:
        if not isinstance(other, KeyRing):
            return NotImplemented
        return self._by_kid == other._by_kid

    def __repr__(self) -> str:
        return f"KeyRing({sorted(self._by_kid)})"

    def __str__(self) -> str:
        return ",".join(str(key) for key in self)

    def gap(self, needed: Iterable[str]) -> frozenset[str]:
        """KIDs that are needed and not held."""
        return frozenset(normalise_kid(kid) for kid in needed) - self.kids

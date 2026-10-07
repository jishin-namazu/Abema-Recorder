"""Turning a license endpoint plus a PSSH into content keys.

The container replays the license exchange: it is handed a license URL (or
the token that addresses one) and performs the exchange itself, using a local
CDM to build the challenge. A license is bound to the CDM that issued the
challenge, so the browser's own challenge is not forwarded; the token in the
URL is the credential.

ABEMA's endpoint takes the credentials in the query string: an opaque ``t``
token and an ES256 JWT ``pt`` scoped to the PPV event. Whether the server also
inspects cookies or Origin is unknown. Any refused request — bad token, bad
challenge, missing headers — gets an HTTP 500 body of ``{"code":"LS001",...}``.
On that failure, capture the full request headers from the browser and pass
them with --headers-file.
"""

from __future__ import annotations

import base64
import json
import re
from dataclasses import dataclass, field
from pathlib import Path

from . import netio
from .errors import LicenseError, ToolError
from .keys import ContentKey, KeyRing

# A bare token addresses this endpoint.
ABEMA_LICENSE = "https://license.p-c3-e.abema-tv.com/playout/widevine"

# The challenge is a raw Widevine protobuf and nothing else.
CHALLENGE_HEADERS = {"Content-Type": "application/octet-stream"}

_HEADER_LINE = re.compile(r"\A(?P<name>[^:\s][^:]*):\s*(?P<value>.*)\Z")
_QUERY_PAIR = re.compile(r"(?:\A|&)(?:t|pt)=")

# Headers a client must compute from the body it sends, plus HTTP/2
# pseudo-headers.
_DROPPED_HEADERS = frozenset(
    {
        "host",
        "content-length",
        "connection",
        "content-encoding",
        "transfer-encoding",
        "accept-encoding",
    }
)


@dataclass(frozen=True, slots=True)
class LicenseTarget:
    """Where to send a challenge, and with what headers."""

    url: str
    headers: dict[str, str] = field(default_factory=lambda: dict(CHALLENGE_HEADERS))

    @classmethod
    def from_token(cls, token: str) -> "LicenseTarget":
        """Build the license URL from the pasted token.

        A full http(s) URL is used as-is. A query string carrying
        ``t=``/``pt=`` (with or without a leading ``?``) is appended to the
        ABEMA endpoint. Anything else is treated as the bare ``pt`` JWT.
        """
        token = token.strip()
        if token.startswith(("http://", "https://")):
            return cls(token)
        query = token.lstrip("?")
        if _QUERY_PAIR.search(query):
            return cls(f"{ABEMA_LICENSE}?{query}")
        return cls(f"{ABEMA_LICENSE}?pt={token}")

    @classmethod
    def from_url(cls, url: str, headers: dict[str, str] | None = None) -> "LicenseTarget":
        url = url.strip()
        if not url.startswith(("http://", "https://")):
            raise LicenseError(
                f"license URL must be absolute: {url!r}",
                remedy="Copy the full request URL, scheme included.",
            )
        return cls(url, sanitise_headers(headers) if headers else dict(CHALLENGE_HEADERS))

    def redacted(self) -> str:
        """The URL with the credentials elided, for printing and logging."""
        return re.sub(r"([?&](?:token|jwt|auth|t|pt)=)[^&]+", r"\1<redacted>", self.url)


def sanitise_headers(headers: dict[str, str]) -> dict[str, str]:
    kept = {}
    for name, value in headers.items():
        lowered = name.strip().lower()
        if not lowered or lowered.startswith(":") or lowered in _DROPPED_HEADERS:
            continue
        kept[name.strip()] = value.strip()
    return kept or dict(CHALLENGE_HEADERS)


def read_header_file(path: Path) -> dict[str, str]:
    """Read ``Name: value`` lines, one header per line.

    Values are taken verbatim after the first colon, so a value containing a
    colon (a URL, a timestamp) survives intact.
    """
    try:
        text = path.read_text(encoding="utf-8")
    except OSError as exc:
        raise LicenseError(f"cannot read headers file {path}: {exc}") from exc

    headers: dict[str, str] = {}
    for line in text.splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        matched = _HEADER_LINE.match(line)
        if not matched:
            continue
        value = matched.group("value").strip()
        if len(value) >= 2 and value[0] == value[-1] == '"':
            value = value[1:-1]
        headers[matched.group("name").strip()] = value

    if not headers:
        raise LicenseError(
            f"no headers could be read from {path}",
            remedy="Each line must look like: Content-Type: application/octet-stream",
        )
    return sanitise_headers(headers)


def _load_cdm(cdm_path: Path):
    """Import pywidevine and open the device file.

    The import is not wrapped in a broad except: when protobuf or construct
    is missing, the raised error names the absent module.
    """
    try:
        from pywidevine import Cdm, Device, PSSH  # type: ignore
    except ImportError as exc:
        missing = getattr(exc, "name", None) or "pywidevine"
        raise ToolError(
            f"local CDM unavailable — cannot import {missing}",
            remedy=f"pip install {missing}  (or rebuild the image with the cdm extra)",
        ) from exc

    if not cdm_path.is_file():
        raise ToolError(
            f"no CDM device file at {cdm_path}",
            remedy="Mount one at /config/device.wvd, or point ABM_CDM elsewhere.",
        )
    try:
        device = Device.load(str(cdm_path))
    except Exception as exc:
        raise ToolError(
            f"could not load the CDM at {cdm_path}: {exc}",
            remedy="The file is present but unreadable — re-copy it.",
        ) from exc
    return Cdm, Device, PSSH, device


def _payload_from_reply(reply: netio.Reply) -> bytes:
    """Extract the license bytes.

    Raw protobuf is the norm. A JSON object carrying a base64 string under
    ``license`` or ``response`` is unwrapped; anything else is passed through
    as-is.
    """
    body = reply.body
    if not body[:1] in (b"{", b"["):
        return body
    try:
        document = json.loads(body)
    except ValueError:
        return body
    if isinstance(document, dict):
        for field_name in ("license", "response", "payload"):
            wrapped = document.get(field_name)
            if isinstance(wrapped, str):
                try:
                    return base64.b64decode(wrapped, validate=True)
                except Exception:
                    continue
    return body


def _explain_http_failure(status: int, body: str) -> str:
    head = body[:200]
    if status == 404 and "<!doctype html" in head.lower():
        return "That URL reached a web server, not a license endpoint. Check the address."
    if status in (401, 403):
        return "The token was refused. It is tied to one playback session and expires — capture a fresh one."
    if status == 400:
        return "The challenge was rejected. The PSSH and the token likely belong to different assets."
    if "LS001" in head or status >= 500:
        return (
            "ABEMA answers every refusal — bad token, bad challenge, missing headers — "
            "with LS001. If the token is fresh, copy the full license request headers "
            "from the browser's developer tools into a headers file and retry with "
            "--license-url plus --headers-file."
        )
    return "Confirm the license URL and token belong to the stream being captured."


def exchange(
    pssh_b64: str,
    target: LicenseTarget,
    cdm_path: Path,
    *,
    timeout: float = 60.0,
) -> tuple[ContentKey, ...]:
    """One challenge/response round trip. Returns the CONTENT keys it yielded."""
    Cdm, _Device, PSSH, device = _load_cdm(cdm_path)
    cdm = Cdm.from_device(device)
    session = cdm.open()
    try:
        try:
            challenge = cdm.get_license_challenge(session, PSSH(pssh_b64))
        except Exception as exc:
            raise LicenseError(
                f"the CDM refused to build a challenge for this PSSH: {exc}",
                remedy="The PSSH may be malformed. Check pssh.txt in the run directory.",
            ) from exc

        try:
            reply = netio.post(target.url, challenge, headers=target.headers, timeout=timeout)
        except netio.TransportError as exc:
            raise LicenseError(
                f"could not reach the license server: {exc}",
                remedy="Check connectivity and HTTPS_PROXY.",
            ) from exc

        if not reply.ok:
            snippet = reply.text[:200].replace("\n", " ")
            raise LicenseError(
                f"license server returned HTTP {reply.status}: {snippet}",
                remedy=_explain_http_failure(reply.status, reply.text),
            )

        try:
            cdm.parse_license(session, _payload_from_reply(reply))
        except Exception as exc:
            raise LicenseError(
                f"the license could not be parsed: {exc}",
                remedy="The response was not a Widevine license — the endpoint may be wrong.",
            ) from exc

        found = tuple(
            ContentKey(entry.kid.hex, entry.key.hex())
            for entry in cdm.get_keys(session)
            if str(entry.type).upper().endswith("CONTENT")
        )
        if not found:
            raise LicenseError(
                "the license parsed but carried no content keys",
                remedy="The token is probably scoped to a different asset.",
            )
        return found
    finally:
        cdm.close(session)


@dataclass(frozen=True, slots=True)
class Acquisition:
    keys: KeyRing
    requests_made: int
    failures: tuple[str, ...] = ()


def acquire(
    payloads: tuple[str, ...],
    needed: frozenset[str],
    target: LicenseTarget,
    cdm_path: Path,
    *,
    timeout: float = 60.0,
) -> Acquisition:
    """Collect keys across every PSSH in the manifest.

    The payloads are one per track, so the results are unioned; the loop
    stops once coverage is satisfied.
    """
    if not payloads:
        raise LicenseError(
            "the manifest declared no Widevine PSSH, so no key can be requested",
            remedy="If the stream is unencrypted, no keys are needed at all.",
        )

    ring = KeyRing()
    failures: list[str] = []
    made = 0
    for payload in payloads:
        try:
            ring = ring.with_keys(exchange(payload, target, cdm_path, timeout=timeout))
        except LicenseError as exc:
            failures.append(exc.message)
            continue
        made += 1
        if needed and not ring.gap(needed):
            break

    if not ring:
        detail = "; ".join(failures) or "no keys were returned"
        raise LicenseError(
            f"no content keys could be obtained: {detail}",
            remedy="Confirm the token is current and matches this event.",
        )
    return Acquisition(keys=ring, requests_made=made, failures=tuple(failures))

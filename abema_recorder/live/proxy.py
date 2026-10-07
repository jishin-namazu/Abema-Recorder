"""Loopback HLS proxy for ABEMA playlists, AES keys and media segments."""

from __future__ import annotations

import json
import logging
import mimetypes
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlsplit

from . import playlists, resolver
from .timeline import PlaybackBuffer


LOG = logging.getLogger("abema-recorder.proxy")
ALLOWED_HTTP_HOST_SUFFIXES = (".abema.io", ".abema.tv", ".akamaized.net")


def allowed_upstream(url: str) -> bool:
    parts = urlsplit(url)
    if parts.scheme == "abematv-license":
        return bool(parts.netloc or parts.path)
    if parts.scheme not in ("http", "https") or not parts.hostname:
        return False
    host = parts.hostname.lower()
    return any(host == suffix[1:] or host.endswith(suffix) for suffix in ALLOWED_HTTP_HOST_SUFFIXES)


class ProxyState:
    def __init__(
        self,
        source_url: str,
        quality: str,
        port: int,
        public_host: str = "127.0.0.1",
        *,
        media_url: str | None = None,
        normalized_playback: bool = False,
    ):
        self.source_url = source_url
        self.quality = quality
        self.port = port
        self.public_host = public_host
        self.media_url_override = media_url
        self.normalized_playback = normalized_playback
        self.playback = PlaybackBuffer()
        self.resolved: resolver.ResolvedABEMA | None = None
        self.resolve_lock = threading.RLock()
        self.key_lock = threading.Lock()
        self.key_cache: dict[str, bytes] = {}
        self.seen_segments: set[int] = set()
        self.last_mode: str | None = None
        self.last_playlist_ok = 0.0
        self.resolve()

    @property
    def local_base(self) -> str:
        return f"http://{self.public_host}:{self.port}"

    @property
    def playlist_url(self) -> str:
        return f"{self.local_base}/index.m3u8"

    @property
    def source_playlist_url(self) -> str:
        return f"{self.local_base}/source.m3u8"

    @property
    def media_url(self) -> str:
        assert self.resolved is not None
        return self.resolved.media_url

    def resolve(self) -> None:
        with self.resolve_lock:
            self.resolved = resolver.resolve(self.source_url, self.quality, media_url=self.media_url_override)
            self.key_cache.clear()
            LOG.info("resolved %s -> %s", self.quality, self.resolved.media_url)

    def _request(self, url: str, headers: dict[str, str] | None = None):
        if not allowed_upstream(url):
            raise ValueError(f"blocked upstream URL: {url}")
        assert self.resolved is not None
        return self.resolved.session.http.get(url, headers=headers or {}, timeout=30)

    def fetch_playlist(self, *, strip_discontinuity: bool = False) -> bytes:
        error: Exception | None = None
        for attempt in range(2):
            try:
                response = self._request(self.media_url)
                response.raise_for_status()
                rewritten = playlists.rewrite_playlist(
                    response.text,
                    self.media_url,
                    self.local_base,
                    strip_discontinuity=strip_discontinuity,
                )
                self._observe(rewritten.segments)
                self.last_playlist_ok = time.time()
                return rewritten.text.encode("utf-8")
            except Exception as exc:
                error = exc
                if attempt == 0:
                    LOG.warning("playlist request failed; refreshing ABEMA session: %s", exc)
                    self.resolve()
        assert error is not None
        raise error

    def _observe(self, segments: tuple[playlists.Segment, ...]) -> None:
        for segment in segments:
            if segment.sequence in self.seen_segments:
                continue
            if self.last_mode is None:
                LOG.info("initial mode=%s sequence=%d", segment.mode, segment.sequence)
            elif segment.mode != self.last_mode:
                LOG.info(
                    "transition %s -> %s sequence=%d discontinuity=%s",
                    self.last_mode,
                    segment.mode,
                    segment.sequence,
                    segment.discontinuity,
                )
            elif segment.discontinuity:
                LOG.info("discontinuity within %s sequence=%d", segment.mode, segment.sequence)
            self.last_mode = segment.mode
            self.seen_segments.add(segment.sequence)
        if len(self.seen_segments) > 2000 and segments:
            floor = segments[-1].sequence - 1000
            self.seen_segments = {value for value in self.seen_segments if value >= floor}

    def fetch_resource(self, url: str, request_headers: dict[str, str]) -> tuple[int, dict[str, str], bytes]:
        if not allowed_upstream(url):
            raise ValueError(f"blocked upstream URL: {url}")

        if urlsplit(url).scheme == "abematv-license":
            with self.key_lock:
                cached = self.key_cache.get(url)
                if cached is not None:
                    return 200, {"Content-Type": "application/octet-stream"}, cached
                last_error: Exception | None = None
                for attempt in range(2):
                    try:
                        response = self._request(url)
                        response.raise_for_status()
                        data = response.content
                        if len(data) != 16:
                            raise RuntimeError(f"ABEMA key has {len(data)} bytes, expected 16")
                        self.key_cache[url] = data
                        LOG.info("fetched AES-128 key")
                        return 200, {"Content-Type": "application/octet-stream"}, data
                    except Exception as exc:
                        last_error = exc
                        if attempt == 0:
                            LOG.warning("key request failed; refreshing ABEMA session: %s", exc)
                            self.resolve()
                assert last_error is not None
                raise last_error

        forwarded = {
            name: request_headers[name]
            for name in ("Range", "If-None-Match", "If-Modified-Since")
            if name in request_headers
        }
        response = self._request(url, forwarded)
        if response.status_code in (401, 403):
            self.resolve()
            response = self._request(url, forwarded)
        data = response.content
        headers: dict[str, str] = {}
        for name in ("Content-Type", "Content-Range", "Accept-Ranges", "ETag", "Last-Modified", "Cache-Control"):
            value = response.headers.get(name)
            if value:
                headers[name] = value
        headers.setdefault(
            "Content-Type",
            mimetypes.guess_type(urlsplit(url).path)[0] or "application/octet-stream",
        )
        return response.status_code, headers, data


def make_handler(state: ProxyState):
    class Handler(BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"
        server_version = "ABEMA-Recorder/1.0"

        def log_message(self, fmt: str, *args) -> None:
            LOG.debug("%s - %s", self.client_address[0], fmt % args)

        def payload(self, status: int, content_type: str, data: bytes, headers=None, head=False):
            self.send_response(status)
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(len(data)))
            self.send_header("Access-Control-Allow-Origin", "*")
            self.send_header("Connection", "close")
            for name, value in (headers or {}).items():
                if name.lower() not in ("content-type", "content-length", "connection", "content-encoding"):
                    self.send_header(name, value)
            self.end_headers()
            if not head:
                try:
                    self.wfile.write(data)
                except (BrokenPipeError, ConnectionResetError):
                    pass

        def route(self, head=False):
            try:
                if self.path in ("/", "/index.m3u8") and state.normalized_playback:
                    self.payload(
                        200,
                        "application/vnd.apple.mpegurl",
                        state.playback.playlist(state.local_base).encode("utf-8"),
                        {"Cache-Control": "no-store, no-cache, must-revalidate"},
                        head,
                    )
                    return
                if self.path in ("/", "/index.m3u8", "/source.m3u8", "/clean.m3u8"):
                    self.payload(
                        200,
                        "application/vnd.apple.mpegurl",
                        state.fetch_playlist(strip_discontinuity=self.path == "/clean.m3u8"),
                        {"Cache-Control": "no-store, no-cache, must-revalidate"},
                        head,
                    )
                    return
                if self.path.startswith("/playback/") and self.path.endswith(".ts"):
                    try:
                        sequence = int(self.path.rsplit("/", 1)[1].removesuffix(".ts"))
                        path = state.playback.path(sequence)
                    except (KeyError, ValueError):
                        self.send_error(404)
                        return
                    self.payload(
                        200,
                        "video/mp2t",
                        path.read_bytes(),
                        {"Cache-Control": "public, max-age=120"},
                        head,
                    )
                    return
                if self.path == "/health":
                    body = json.dumps(
                        {
                            "ok": True,
                            "source": state.source_url,
                            "quality": state.quality,
                            "media_url": state.media_url,
                            "last_playlist_ok": state.last_playlist_ok,
                        }
                    ).encode()
                    self.payload(200, "application/json", body, head=head)
                    return
                if self.path.startswith("/resource/"):
                    token = self.path.split("/resource/", 1)[1].split("/", 1)[0]
                    remote = playlists.decode_url(token)
                    status, headers, body = state.fetch_resource(remote, dict(self.headers.items()))
                    self.payload(status, headers["Content-Type"], body, headers, head)
                    return
                self.send_error(404)
            except Exception as exc:
                LOG.exception("proxy request failed: %s", exc)
                self.payload(502, "text/plain; charset=utf-8", f"proxy error: {exc}\n".encode(), head=head)

        def do_GET(self):
            self.route(False)

        def do_HEAD(self):
            self.route(True)

    return Handler


class HLSProxy:
    def __init__(
        self,
        source_url: str,
        quality: str,
        host: str,
        port: int,
        *,
        media_url: str | None = None,
        normalized_playback: bool = False,
    ):
        self.state = ProxyState(
            source_url,
            quality,
            port,
            media_url=media_url,
            normalized_playback=normalized_playback,
        )
        self.server = ThreadingHTTPServer((host, port), make_handler(self.state))
        self.thread: threading.Thread | None = None

    @property
    def playlist_url(self) -> str:
        return self.state.playlist_url

    def start(self) -> None:
        if self.thread is not None:
            return
        self.thread = threading.Thread(target=self.server.serve_forever, name="abema-hls-proxy", daemon=True)
        self.thread.start()
        LOG.info("local playlist %s", self.playlist_url)

    def stop(self) -> None:
        if self.thread is None:
            return
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=5)
        self.thread = None

    def serve_forever(self) -> None:
        LOG.info("local playlist %s", self.playlist_url)
        try:
            self.server.serve_forever(poll_interval=0.5)
        finally:
            self.server.server_close()

    def __enter__(self):
        self.start()
        return self

    def __exit__(self, *_):
        self.stop()

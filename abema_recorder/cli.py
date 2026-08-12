"""Command line interface."""

from __future__ import annotations

import argparse
import logging
import os
import shutil
import subprocess
import sys
import urllib.request
from datetime import datetime
from pathlib import Path

from . import capture
from .proxy import HLSProxy


DEFAULT_URL = os.environ.get("ABEMA_URL", "https://abema.tv/now-on-air/luckyfes")
DEFAULT_QUALITY = os.environ.get("ABEMA_QUALITY", "1080p")
DEFAULT_HOST = os.environ.get("ABEMA_PROXY_HOST", "0.0.0.0")
DEFAULT_PORT = int(os.environ.get("ABEMA_PROXY_PORT", "18081"))


def common(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--url", default=DEFAULT_URL)
    parser.add_argument("--quality", default=DEFAULT_QUALITY)
    parser.add_argument("--host", default=DEFAULT_HOST)
    parser.add_argument("--port", type=int, default=DEFAULT_PORT)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="abema-recorder")
    parser.add_argument("--verbose", action="store_true")
    commands = parser.add_subparsers(dest="command", required=True)

    proxy = commands.add_parser("proxy", help="serve local HLS for OBS/VLC/PotPlayer")
    common(proxy)

    for name in ("capture", "plan"):
        command = commands.add_parser(name, help="record through N_m3u8DL-RE" if name == "capture" else "print capture command")
        common(command)
        command.add_argument("--out", type=Path)
        command.add_argument("--discard-segments", action="store_true")
        command.add_argument("--burst-output", action="store_true")
        command.add_argument("--verbose-downloader", action="store_true")
        command.add_argument("--record-limit", default="", metavar="HH:MM:SS")

    probe = commands.add_parser("probe", help="verify ABEMA auth, key proxy and decoded media")
    common(probe)
    return parser


def output_dir(value: Path | None) -> Path:
    if value is not None:
        return value
    root = Path("/archive") if Path("/archive").is_dir() else Path("archive")
    return root / f"abema_{datetime.now():%Y%m%d_%H%M%S}.out"


def run_probe(args: argparse.Namespace) -> int:
    with HLSProxy(args.url, args.quality, args.host, args.port) as proxy:
        with urllib.request.urlopen(proxy.playlist_url, timeout=30) as response:
            playlist = response.read().decode("utf-8")
        print(f"playlist: {proxy.playlist_url}")
        print(f"media:    {proxy.state.media_url}")
        print(f"ads:      {'yes' if '/tsad/' in playlist else 'not in current window'}")
        print(f"discontinuities: {playlist.count('#EXT-X-DISCONTINUITY')}")

        ffprobe = shutil.which("ffprobe")
        if not ffprobe:
            print("ffprobe: not found", file=sys.stderr)
            return 1
        command = [
            ffprobe,
            "-v", "error",
            "-read_intervals", "%+8",
            "-show_entries", "stream=codec_name,codec_type,width,height,sample_rate",
            "-of", "json",
            proxy.playlist_url,
        ]
        finished = subprocess.run(command, text=True, capture_output=True, timeout=60, check=False)
        if finished.stdout:
            print(finished.stdout)
        if finished.returncode != 0:
            print(finished.stderr, file=sys.stderr)
        return finished.returncode


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s [%(levelname)s] %(message)s",
    )
    try:
        if args.command == "proxy":
            HLSProxy(args.url, args.quality, args.host, args.port).serve_forever()
            return 0
        if args.command == "probe":
            return run_probe(args)
        if args.command in ("capture", "plan"):
            return capture.run(
                args.url,
                args.quality,
                args.host,
                args.port,
                output_dir(args.out),
                keep_segments=not args.discard_segments,
                paced_output=not args.burst_output,
                quiet_downloader=not args.verbose_downloader,
                record_limit=args.record_limit,
                dry_run=args.command == "plan",
            )
        raise AssertionError(args.command)
    except KeyboardInterrupt:
        return 130
    except Exception as exc:
        logging.error("%s", exc)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())

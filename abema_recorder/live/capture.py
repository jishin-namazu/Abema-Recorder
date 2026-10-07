"""N_m3u8DL-RE live capture through the local ABEMA HLS proxy."""

from __future__ import annotations

import os
import shutil
import signal
import subprocess
from dataclasses import dataclass
from pathlib import Path

from .. import console, records
from ..errors import ToolError
from .proxy import HLSProxy
from .timeline import TimelineMerger


PIPE_OPTIONS_ENV = "RE_LIVE_PIPE_OPTIONS"


def executable(name: str) -> Path | None:
    value = shutil.which(name)
    return Path(value) if value else None


@dataclass(frozen=True, slots=True)
class CapturePlan:
    proxy_url: str
    output_dir: Path
    run_name: str
    downloader: Path
    ffmpeg: Path | None = None
    ffprobe: Path | None = None
    keep_segments: bool = True
    paced_output: bool = True
    quiet_downloader: bool = True
    record_limit: str = ""

    @property
    def muxed_output(self) -> Path:
        return self.output_dir / f"{self.run_name}.ts"

    @property
    def segment_dir(self) -> Path:
        return self.output_dir.parent / self.run_name

    def argv(self) -> list[str]:
        args = [
            str(self.downloader),
            self.proxy_url,
            "--live-keep-segments",
            "True",
            "--auto-select",
            "--live-wait-time",
            "2",
            "--download-retry-count",
            "10",
            "--save-dir",
            str(self.output_dir),
            "--save-name",
            self.run_name,
            "--log-file-path",
            str(self.output_dir / "downloader.log"),
            "--disable-update-check",
        ]
        # Download and decryption stay independent from merging.  The recorder's
        # TimelineMerger normalizes every completed segment before appending it.
        args.append("--skip-merge")
        if self.record_limit:
            args += ["--live-record-limit", self.record_limit]
        if self.quiet_downloader:
            args += ["--log-level", "OFF"]
        return args

    def environment(self) -> dict[str, str]:
        env = dict(os.environ)
        # Ignore stale values from versions which used --live-pipe-mux.
        env.pop(PIPE_OPTIONS_ENV, None)
        return env

    def display(self) -> str:
        return subprocess.list2cmdline(self.argv())


def make_plan(
    proxy_url: str,
    output_dir: Path,
    *,
    keep_segments: bool,
    paced_output: bool,
    quiet_downloader: bool,
    record_limit: str,
) -> CapturePlan:
    downloader = executable("N_m3u8DL-RE")
    if downloader is None:
        raise ToolError(
            "N_m3u8DL-RE was not found on PATH",
            remedy="Run probe to check the toolchain; the image installs it on PATH.",
        )
    ffmpeg = executable("ffmpeg")
    if ffmpeg is None:
        raise ToolError(
            "ffmpeg was not found on PATH",
            remedy="Run probe to check the toolchain; the image installs it on PATH.",
        )
    ffprobe = executable("ffprobe")
    if ffprobe is None:
        raise ToolError(
            "ffprobe was not found on PATH",
            remedy="Run probe to check the toolchain; the image installs it on PATH.",
        )
    run_name = output_dir.name.removesuffix(".out") or records.default_run_name()
    return CapturePlan(
        proxy_url,
        output_dir,
        run_name,
        downloader,
        ffmpeg=ffmpeg,
        ffprobe=ffprobe,
        keep_segments=keep_segments,
        paced_output=paced_output,
        quiet_downloader=quiet_downloader,
        record_limit=record_limit,
    )


def run(
    source_url: str,
    quality: str,
    host: str,
    port: int,
    output_dir: Path,
    *,
    media_url: str | None = None,
    keep_segments: bool,
    paced_output: bool,
    quiet_downloader: bool,
    record_limit: str,
    dry_run: bool = False,
) -> int:
    proxy_url = f"http://127.0.0.1:{port}/source.m3u8"
    plan = make_plan(
        proxy_url,
        output_dir,
        keep_segments=keep_segments,
        paced_output=paced_output,
        quiet_downloader=quiet_downloader,
        record_limit=record_limit,
    )
    if dry_run:
        console.say(plan.display())
        return 0

    output_dir.mkdir(parents=True, exist_ok=True)
    with HLSProxy(source_url, quality, host, port, media_url=media_url, normalized_playback=True) as proxy:
        records.RunRecord(
            url=source_url,
            run_name=plan.run_name,
            output_dir=plan.output_dir,
            shard_root=plan.segment_dir,
            session_shard_root=plan.segment_dir,
            shards_kept=keep_segments,
            decryptor="",
            engine="hls",
            quality=quality,
            media_url=proxy.state.media_url,
            proxy_url=proxy.playlist_url,
            source_proxy_url=proxy.state.source_playlist_url,
            extra={"command": plan.argv(), "paced_output": paced_output},
        ).write()
        console.say(f"HLS for playback: {proxy.playlist_url}")
        console.say(f"recording:        {plan.muxed_output}")
        assert plan.ffmpeg is not None and plan.ffprobe is not None
        merger = TimelineMerger(
            plan.muxed_output,
            plan.ffmpeg,
            plan.ffprobe,
            plan.output_dir,
            delete_sources=not plan.keep_segments,
            playback=proxy.state.playback,
        )
        process = subprocess.Popen(plan.argv(), env=plan.environment())
        try:
            return merger.follow(plan.segment_dir, process)
        except KeyboardInterrupt:
            if process.poll() is None:
                process.send_signal(signal.SIGINT)
                try:
                    process.wait(timeout=10)
                except subprocess.TimeoutExpired:
                    process.terminate()
            return 130
        except Exception:
            if process.poll() is None:
                process.terminate()
            raise

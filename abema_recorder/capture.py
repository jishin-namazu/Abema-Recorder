"""Building and running the downloader command.

The recording itself is N_m3u8DL-RE's job: it fetches the MPD, downloads the
segments, hands each shard to the decryptor and muxes the result. This module
constructs that invocation. The child shares the console (no pipes), so Ctrl+C
reaches it directly and it runs its own drain-and-finalise path.

Under ``--live-pipe-mux`` the downloader feeds named pipes to ffmpeg; setting
``RE_LIVE_PIPE_OPTIONS`` adds ``-re`` to that ffmpeg invocation — paced output
at playback rate. The flag applies only to a live (dynamic MPD) capture. With
HLS serving configured for a live capture, the same ffmpeg process uses its
tee muxer to keep the archive while maintaining a short rolling live playlist.
"""

from __future__ import annotations

import os
import subprocess
from dataclasses import dataclass
from pathlib import Path

from .errors import CaptureError, ConfigError
from .keys import KeyRing
from .mpd import QUALITY_RESOLUTIONS
from .toolchain import Toolchain

LOG_NAME = "downloader.log"

# Read by the downloader; its presence makes the muxing ffmpeg run with -re,
# and its value is where that ffmpeg writes.
PIPE_OPTIONS_ENV = "RE_LIVE_PIPE_OPTIONS"

_UNSAFE_TEE_CHARACTERS = frozenset("\r\n\"'|")


def _tee_path(path: Path) -> str:
    value = path.as_posix()
    if any(character in _UNSAFE_TEE_CHARACTERS for character in value):
        raise ConfigError(
            "the output path contains a character ffmpeg's tee output cannot quote safely",
            remedy="Choose an --out path without quotes, line breaks, or |.",
        )
    return value


@dataclass(frozen=True, slots=True)
class CapturePlan:
    url: str
    keys: KeyRing
    out_dir: Path
    run_name: str
    tools: Toolchain
    live: bool = True
    keep_shards: bool = True
    quiet: bool = True
    paced: bool = True
    hls: bool = False
    quality: str = ""
    record_limit: str = ""

    def __post_init__(self) -> None:
        if not self.hls:
            return
        if not self.paced:
            raise ConfigError(
                "--hls cannot be combined with --burst-output",
                remedy="Remove --burst-output so the playlist advances in real time.",
            )
        _tee_path(self.muxed_output)
        _tee_path(self.hls_playlist)

    @property
    def muxed_output(self) -> Path:
        """Where the muxed .ts lands.

        Paced output requires handing ffmpeg an explicit destination.
        """
        return self.out_dir / f"{self.run_name}.ts"

    @property
    def hls_directory(self) -> Path:
        return self.out_dir / "hls"

    @property
    def hls_playlist(self) -> Path:
        return self.hls_directory / "live.m3u8"

    def environment(self) -> dict[str, str]:
        """The child's environment.

        Setting the pipe options turns on ``-re``. The value is a plain path,
        which the downloader treats as a destination; a value starting with
        ``-`` is spliced in as raw ffmpeg arguments.
        """
        env = dict(os.environ)
        if self.live and self.paced:
            env[PIPE_OPTIONS_ENV] = self.pipe_options
        return env

    @property
    def pipe_options(self) -> str:
        """Destination passed to the downloader's live ffmpeg process."""
        if not self.hls:
            return str(self.muxed_output)
        archive = f"[f=mpegts:onfail=abort]{_tee_path(self.muxed_output)}"
        flags = "delete_segments+omit_endlist+independent_segments+temp_file"
        live = (
            "[f=hls:onfail=ignore:hls_time=2:hls_list_size=6:"
            f"hls_delete_threshold=2:hls_allow_cache=0:hls_flags={flags}]"
            f"{_tee_path(self.hls_playlist)}"
        )
        return f'-f tee -shortest "{archive}|{live}"'

    def argv(self) -> list[str]:
        downloader = str(self.tools.downloader) if self.tools.downloader else "N_m3u8DL-RE"
        argv = [downloader, self.url]

        for key in self.keys:
            argv += ["--key", str(key)]

        if self.live:
            argv += ["--live-pipe-mux"]
            # The flag takes a capitalised boolean, not a lowercase one.
            argv += ["--live-keep-segments", "True" if self.keep_shards else "False"]
            if self.record_limit:
                argv += ["--live-record-limit", self.record_limit]
        elif self.keys and self.keep_shards:
            # Static capture with shard keeping: the downloader only fetches.
            # Each shard is decrypted tool-side as it lands and the final file
            # is merged from the decrypted shards afterwards.
            argv += ["--skip-merge"]
            argv += ["--check-segments-count", "False"]

        if self.quality:
            resolution = QUALITY_RESOLUTIONS.get(self.quality)
            if resolution is None:
                raise ConfigError(
                    f"unknown quality: {self.quality}",
                    remedy="Use one of: " + ", ".join(QUALITY_RESOLUTIONS),
                )
            argv += ["-sv", f"res={resolution}"]
        else:
            argv += ["--auto-select"]
        argv += ["--save-dir", str(self.out_dir)]
        argv += ["--save-name", self.run_name]
        argv += ["--decryption-engine", self.tools.decryptor_kind.value]

        if self.tools.decryptor:
            # shaka is published as packager-linux-x64; the downloader never
            # looks for that name, so the path is passed explicitly.
            argv += ["--decryption-binary-path", str(self.tools.decryptor)]

        argv += ["--log-file-path", str(self.out_dir / LOG_NAME)]
        if self.quiet:
            # The downloader logs the manifest's PlayReady PSSH at ERROR level,
            # repeating with every live manifest refresh. The log file still
            # records everything.
            argv += ["--log-level", "OFF"]
        return argv

    def display(self) -> str:
        parts = []
        for argument in self.argv():
            if any(ch in argument for ch in ' "\\'):
                parts.append('"' + argument.replace('"', '\\"') + '"')
            else:
                parts.append(argument)
        shown = " ".join(parts)
        if self.live and self.paced:
            # Part of the command; a copied command line needs it to behave
            # the same.
            options = self.pipe_options.replace('"', '\\"')
            shown = f'{PIPE_OPTIONS_ENV}="{options}" {shown}'
        return shown


def run(plan: CapturePlan) -> int:
    """Run the downloader to completion. Returns its exit status."""
    plan.tools.verify()
    plan.out_dir.mkdir(parents=True, exist_ok=True)
    if plan.hls:
        plan.hls_directory.mkdir(parents=True, exist_ok=True)
    try:
        finished = subprocess.run(plan.argv(), env=plan.environment(), check=False)
    except FileNotFoundError as exc:
        raise CaptureError(
            f"could not start the downloader: {exc}",
            remedy="Rebuild the image — N_m3u8DL-RE should be on PATH.",
        ) from exc
    except KeyboardInterrupt:
        return 130

    if finished.returncode not in (0, 130):
        raise CaptureError(
            f"the downloader exited with status {finished.returncode}",
            remedy=f"See {plan.out_dir / LOG_NAME} for what it reported.",
        )
    return finished.returncode

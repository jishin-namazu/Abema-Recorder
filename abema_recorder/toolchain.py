"""Locating and proving the external binaries.

The image installs these on PATH, so discovery is short. Every binary is
executed once before a run commits to it, and the version string that probe
produces is kept.
"""

from __future__ import annotations

import os
import shutil
import subprocess
from dataclasses import dataclass
from enum import Enum
from pathlib import Path

from .errors import ToolError

DOWNLOADER = "N_m3u8DL-RE"
MUXER = "ffmpeg"
PROBER = "ffprobe"


class Decryptor(str, Enum):
    """Which binary performs decryption.

    SHAKA is the default: it handles both ``cenc`` and ``cbcs`` and decrypts
    while data is still arriving.
    """

    SHAKA = "SHAKA_PACKAGER"
    BENTO = "MP4DECRYPT"

    def __str__(self) -> str:  # argparse renders choices with str()
        return self.value

    @property
    def binary(self) -> str:
        return "shaka-packager" if self is Decryptor.SHAKA else "mp4decrypt"

    @property
    def candidates(self) -> tuple[str, ...]:
        if self is Decryptor.SHAKA:
            # Published as packager-linux-x64.
            return ("shaka-packager", "packager", "packager-linux-x64")
        return ("mp4decrypt",)


_VERSION_FLAG = {MUXER: ["-version"], PROBER: ["-version"]}
_DEFAULT_VERSION_FLAG = ["--version"]


@dataclass(frozen=True, slots=True)
class Probe:
    runnable: bool
    detail: str


def probe(path: Path, *, timeout: float = 15.0) -> Probe:
    """Run a binary once and report whether the OS would execute it.

    A non-zero exit is expected — mp4decrypt has no ``--version`` and answers
    with usage text and exit 1; what is tested is whether the kernel refuses
    the image at all.
    """
    flag = _VERSION_FLAG.get(path.stem.lower(), _DEFAULT_VERSION_FLAG)
    try:
        finished = subprocess.run(
            [str(path), *flag],
            capture_output=True,
            text=True,
            timeout=timeout,
            check=False,
        )
    except OSError as exc:
        return Probe(False, str(exc))
    except subprocess.SubprocessError as exc:
        return Probe(False, f"did not respond: {exc}")

    if finished.returncode < 0:
        # A truncated ELF presents this way: the loader kills it with a signal.
        return Probe(False, f"killed by signal {-finished.returncode}")

    output = f"{finished.stdout}\n{finished.stderr}"
    line = next((ln.strip() for ln in output.splitlines() if ln.strip()), "runs")
    return Probe(True, line[:70])


def _from_env(variable: str) -> Path | None:
    value = os.environ.get(variable, "").strip()
    if value and Path(value).exists():
        return Path(value)
    return None


def find(*names: str, env: str | None = None) -> Path | None:
    if env:
        override = _from_env(env)
        if override:
            return override
    for name in names:
        found = shutil.which(name)
        if found:
            return Path(found)
    return None


@dataclass(frozen=True, slots=True)
class Toolchain:
    downloader: Path | None
    muxer: Path | None
    decryptor: Path | None
    decryptor_kind: Decryptor = Decryptor.SHAKA

    @classmethod
    def discover(cls, decryptor: Decryptor = Decryptor.SHAKA) -> "Toolchain":
        env = "ABM_SHAKA" if decryptor is Decryptor.SHAKA else "ABM_MP4DECRYPT"
        return cls(
            downloader=find(DOWNLOADER, env="ABM_DOWNLOADER"),
            muxer=find(MUXER, env="ABM_FFMPEG"),
            decryptor=find(*decryptor.candidates, env=env),
            decryptor_kind=decryptor,
        )

    @property
    def prober(self) -> Path | None:
        """ffprobe, looked for beside ffmpeg before falling back to PATH."""
        if self.muxer:
            sibling = self.muxer.with_name(self.muxer.name.replace(MUXER, PROBER))
            if sibling.exists():
                return sibling
        return find(PROBER)

    def verify(self) -> dict[str, str]:
        """Prove each required binary runs. Returns name -> version detail."""
        required = (
            (DOWNLOADER, self.downloader),
            (MUXER, self.muxer),
            (self.decryptor_kind.binary, self.decryptor),
        )
        missing = [name for name, path in required if path is None]
        if missing:
            raise ToolError(
                f"missing required binaries: {', '.join(missing)}",
                remedy="Rebuild the image — it installs all three on PATH.",
            )

        versions: dict[str, str] = {}
        broken: list[str] = []
        for name, path in required:
            assert path is not None
            result = probe(path)
            if result.runnable:
                versions[name] = result.detail
            else:
                broken.append(f"{name} ({result.detail})")
        if broken:
            raise ToolError(
                f"present but not runnable: {', '.join(broken)}",
                remedy="Usually a truncated download or a wrong architecture. Rebuild the image.",
            )
        return versions

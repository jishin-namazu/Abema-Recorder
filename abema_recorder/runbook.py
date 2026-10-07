"""Stage ordering for a run.

A DASH source follows a fixed sequence and each stage refuses to start unless
the one before it produced what it needs:

1. resolve the stream — one MPD fetch yields every track
2. obtain keys — from the command line, or by replaying a license request
3. gate on coverage against the tracks that will actually be captured
4. record what this run is, so it can be salvaged later
5. capture, with the shard watcher and the rotation watch running alongside

An HLS source routes to the live subpackage instead; see source.py.
"""

from __future__ import annotations

import json
import subprocess
from dataclasses import dataclass
from pathlib import Path

from . import capture, console, coverage, hls, licensing, mpd, records, rtdecrypt, salvage, shared, shards
from . import settings as cfg
from . import source as routing
from .errors import ArcError, ConfigError, LicenseError
from .keys import KeyRing
from .mpd import Source
from .records import default_run_name
from .settings import Settings
from .toolchain import Toolchain


def default_out_dir(root: Path | None = None) -> Path:
    """Artefacts go in the working directory, beside the shards.

    The downloader puts shards under the working directory named after the
    run, and the mount point is that same directory; an extra level here
    produces archive/archive/run_… on the host while the shards sit at
    archive/run_….
    """
    name = default_run_name()
    return (root / name) if root is not None else Path(f"{name}.out")


@dataclass(slots=True)
class Prepared:
    source: Source
    keys: KeyRing
    report: coverage.GateReport
    out_dir: Path
    run_name: str
    shard_root: Path
    session_shard_root: Path
    tools: Toolchain
    tool_versions: dict[str, str]
    hls_address: str = ""


def resolve_hls_address(settings: Settings, live: bool) -> str:
    """The address a run serves its rolling playlist on; empty means no serving.

    A live (dynamic MPD) run serves by default; a static run never serves.
    """
    if not live:
        return ""
    if settings.hls_address:
        return settings.hls_address
    if settings.paced_output:
        return cfg.DEFAULT_HLS_ADDRESS
    return ""


def route_for(settings: Settings) -> routing.Route:
    if not settings.url:
        raise ConfigError(
            "no stream URL was given",
            remedy="Pass --url, or set url= in the settings file.",
        )
    if settings.engine in (routing.DASH, routing.HLS):
        return routing.Route(settings.engine)
    route = routing.classify(settings.url)
    if route.engine == routing.AUTO:
        from .live import resolver

        engine = routing.HLS if resolver.has_stream(settings.url) else routing.DASH
        route = routing.Route(engine)
    return route


def _license_target(settings: Settings) -> licensing.LicenseTarget:
    if settings.license_url:
        headers = (
            licensing.read_header_file(settings.headers_file)
            if settings.headers_file is not None
            else None
        )
        return licensing.LicenseTarget.from_url(settings.license_url, headers)
    return licensing.LicenseTarget.from_token(settings.license_token)


def gather_keys(settings: Settings, source: Source) -> KeyRing:
    """Literal keys first, then a license replay.

    Literal keys are the offline path and skip the CDM entirely.
    """
    if settings.literal_keys:
        ring = KeyRing.scrape(",".join(settings.literal_keys))
        if not ring:
            raise ConfigError(
                "no KID:KEY pair could be read from the keys given",
                remedy="Each key looks like 32 hex characters, a colon, then 32 more.",
            )
        return ring

    if not settings.has_license_route:
        raise ConfigError(
            "no keys were given and no license route was configured",
            remedy=(
                "Either pass --key KID:KEY, or pass --token/--license-url so the "
                "license request can be replayed here."
            ),
        )

    target = _license_target(settings)
    console.detail(f"license endpoint {target.redacted()}")
    acquired = licensing.acquire(
        source.protection.payloads,
        source.protection.must_cover,
        target,
        settings.cdm_path,
    )
    console.detail(
        f"{len(acquired.keys)} key(s) from {acquired.requests_made} "
        f"of {len(source.protection.payloads)} PSSH payload(s)"
    )
    for failure in acquired.failures:
        console.warn(f"license attempt failed: {failure}")
    return acquired.keys


def prepare(settings: Settings) -> Prepared:
    if not settings.url:
        raise ConfigError(
            "no stream URL was given",
            remedy="Pass --url, or set url= in the settings file.",
        )

    console.stage("resolving the stream")
    source = mpd.resolve(settings.url, quality=settings.quality)
    console.detail(f"DASH manifest, {'live' if source.live else 'static (timeshift)'}")
    if not source.tracks:
        console.warn("no selectable tracks were found — the download may come out empty")
    for track in source.tracks:
        console.detail(f"track {track.describe()}")
    if source.protection.foreign_kids:
        console.detail(
            f"{len(source.protection.foreign_kids)} non-Widevine (PlayReady) key id(s) ignored"
        )
    if not source.protection.encrypted:
        console.detail("no Widevine protection declared")

    console.stage("obtaining keys")
    keys = gather_keys(settings, source)

    console.stage("checking key coverage")
    report = coverage.inspect(keys, source.protection)
    if report.spare:
        console.detail(f"not captured, no key needed: {', '.join(sorted(report.spare))}")
    if report.missing and not settings.strict_coverage:
        console.warn(
            f"proceeding without keys for: {', '.join(sorted(report.missing))} — "
            "those tracks will not decrypt"
        )
    coverage.enforce(keys, source.protection, strict=settings.strict_coverage)
    if report.covered and report.needed:
        console.good(f"all {len(report.needed)} stream KID(s) covered")

    out_dir = settings.out_dir or default_out_dir()
    # The shards are named from the run, and the artefact directory carries a
    # suffix so the two are siblings.
    run_name = out_dir.name.removesuffix(".out")

    hls_address = resolve_hls_address(settings, source.live)
    if settings.hls_address and not source.live:
        console.detail("--hls ignored — a static (timeshift) MPD produces no rolling playlist")
    elif hls_address and not settings.hls_address:
        console.detail(f"hls {hls_address}  (default for a live stream)")
    if hls_address:
        hls.Endpoint.parse(hls_address)

    console.stage("verifying the toolchain")
    tools = Toolchain.discover(settings.decryptor)
    versions = tools.verify()
    console.table(sorted(versions.items()))

    session_shard_root = shards.shard_root(run_name)
    shared_shard_root = (
        shared.canonical_shard_root(source.url, session_shard_root)
        if settings.keep_shards
        else session_shard_root
    )

    return Prepared(
        source=source,
        keys=keys,
        report=report,
        out_dir=out_dir,
        run_name=run_name,
        shard_root=shared_shard_root,
        session_shard_root=session_shard_root,
        tools=tools,
        tool_versions=versions,
        hls_address=hls_address,
    )


def write_artefacts(prepared: Prepared, settings: Settings) -> None:
    artefacts = records.Artefacts(prepared.out_dir)
    source = prepared.source
    artefacts.write_json(
        records.MANIFEST_NAME,
        {
            "url": source.url,
            "is_master": source.playlist.is_master,
            "live": source.live,
            "segment_count": source.playlist.segment_count,
            "media_sequence": source.playlist.media_sequence,
            "target_duration": source.playlist.target_duration,
            "tracks": [
                {
                    "url": t.url,
                    "kind": t.kind,
                    "bandwidth": t.bandwidth,
                    "resolution": t.resolution,
                    "language": t.language,
                }
                for t in source.tracks
            ],
            "pssh": list(source.protection.payloads),
            "advertised_kids": sorted(source.protection.advertised),
            "needed_kids": sorted(source.protection.must_cover),
            "foreign_kids": sorted(source.protection.foreign_kids),
        },
    )
    if source.protection.payloads:
        artefacts.write(records.PSSH_NAME, "\n".join(source.protection.payloads))
    artefacts.write(records.KEYS_NAME, str(prepared.keys))
    if settings.has_license_route:
        artefacts.write(records.LICENSE_NAME, _license_target(settings).redacted())

    records.RunRecord(
        url=source.url,
        run_name=prepared.run_name,
        output_dir=prepared.out_dir,
        shard_root=prepared.shard_root,
        session_shard_root=prepared.session_shard_root,
        shards_kept=settings.keep_shards,
        decryptor=settings.decryptor.value,
        tool_versions=prepared.tool_versions,
        key_kids=sorted(prepared.keys.kids),
        stream_kids=sorted(source.protection.must_cover),
        segment_ms=source.segment_ms,
        quality=settings.quality,
    ).write()


def build_plan(prepared: Prepared, settings: Settings) -> capture.CapturePlan:
    return capture.CapturePlan(
        url=prepared.source.url,
        keys=prepared.keys,
        out_dir=prepared.out_dir,
        run_name=prepared.run_name,
        tools=prepared.tools,
        live=prepared.source.live,
        keep_shards=settings.keep_shards,
        quiet=settings.quiet_downloader,
        paced=settings.paced_output,
        hls=bool(prepared.hls_address),
        quality=settings.quality,
        record_limit=settings.record_limit,
    )


def _execute_hls(settings: Settings, route: routing.Route, *, dry_run: bool) -> int:
    from .live import capture as live_capture
    from .live import resolver

    endpoint = hls.Endpoint.parse(settings.hls_address or cfg.DEFAULT_HLS_ADDRESS)
    quality = settings.quality or resolver.DEFAULT_QUALITY
    out_dir = settings.out_dir or default_out_dir()
    return live_capture.run(
        settings.url,
        quality,
        endpoint.host,
        endpoint.port,
        out_dir,
        media_url=route.media_url,
        keep_segments=settings.keep_shards,
        paced_output=settings.paced_output,
        quiet_downloader=settings.quiet_downloader,
        record_limit=settings.record_limit,
        dry_run=dry_run,
    )


def execute(settings: Settings) -> int:
    route = route_for(settings)
    if route.engine == routing.HLS:
        return _execute_hls(settings, route, dry_run=False)
    prepared = prepare(settings)
    prepared.out_dir.mkdir(parents=True, exist_ok=True)
    write_artefacts(prepared, settings)
    plan = build_plan(prepared, settings)

    decryptor = None
    if (
        settings.keep_shards
        and not prepared.source.live
        and prepared.keys
        and prepared.tools.decryptor
    ):
        # Static: shards are decrypted tool-side as they land. Live: the
        # downloader's pipe-mux flow decrypts inline, no worker is needed.
        decryptor = rtdecrypt.ShardDecryptor(
            prepared.tools.decryptor, prepared.keys, warn=console.warn, delete_source=True
        )

    watcher = None
    if settings.keep_shards:
        watcher = shards.ShardWatcher(
            prepared.session_shard_root,
            log_dir=prepared.out_dir if settings.shard_log else None,
            mirror_root=prepared.shard_root,
            echo=console.say if settings.shard_log and settings.shard_echo else None,
            hint_ms=prepared.source.segment_ms or None,
            decrypting=True,
            decryptor=decryptor,
            gap_settle=3 if prepared.source.live else 30,
        )

    guard = coverage.RotationWatch(
        prepared.source.url,
        prepared.keys,
        interval=settings.guard_interval,
        shard_root=prepared.session_shard_root,
        accepted=prepared.report.missing,
        echo=console.warn,
    )

    console.stage("capturing")
    console.detail(f"output   {prepared.out_dir}")
    console.detail(f"shards   {prepared.shard_root}  (shared by this playback URL)")
    if prepared.session_shard_root != prepared.shard_root:
        console.detail(f"session  {prepared.session_shard_root}")
    if plan.live and plan.paced:
        console.detail(f"file     {plan.muxed_output}  (playable while recording)")
    hls_server = None
    if prepared.hls_address:
        endpoint = hls.Endpoint.parse(prepared.hls_address)
        hls_server = hls.HlsServer(plan.hls_directory, endpoint)
        console.detail(f"hls      {hls_server.url}")

    try:
        if decryptor is not None and watcher is not None:
            decryptor.start(watcher.note)
        if watcher is not None:
            watcher.start()
        guard.start()
        if hls_server is not None:
            hls_server.start()
        status = capture.run(plan)
    finally:
        if hls_server is not None:
            hls_server.stop()
        guard.stop()
        if watcher is not None:
            watcher.stop()
        if decryptor is not None:
            decryptor.stop()

    if decryptor is not None and status == 0:
        console.stage("merging")
        result = salvage.rebuild(
            prepared.out_dir,
            prepared.out_dir / f"{prepared.run_name}.mkv",
            progress=console.progress,
        )
        console.progress_done()
        size_mb = result.destination.stat().st_size / 1_048_576
        console.say(
            f"{result.destination}  ({size_mb:.1f} MB, {result.seconds:.1f}s container / "
            f"{result.shortest_track():.0f}s on every track)"
        )
        for note in result.notes():
            console.warn(note)

    console.stage("finished")
    if watcher is not None and watcher.summary():
        console.detail(watcher.summary())
    console.detail(guard.summary())
    console.detail(f"artefacts in {prepared.out_dir}")
    if settings.keep_shards:
        console.detail(
            f"rebuild with: docker compose run --rm recorder rebuild {prepared.out_dir}"
        )
    return status


def describe_plan(settings: Settings) -> int:
    """Everything execute() would do, without running the downloader."""
    route = route_for(settings)
    if route.engine == routing.HLS:
        console.stage("planned command")
        return _execute_hls(settings, route, dry_run=True)
    prepared = prepare(settings)
    plan = build_plan(prepared, settings)
    console.stage("planned command")
    console.say(plan.display())
    if not prepared.source.live and prepared.keys and settings.keep_shards:
        console.detail(
            "shards are decrypted as they land; the capture finishes by merging them with "
            f"rebuild into {prepared.out_dir / (prepared.run_name + '.mkv')}"
        )
    console.stage("planned run record")
    console.say(
        json.dumps(
            records.RunRecord(
                url=prepared.source.url,
                run_name=prepared.run_name,
                output_dir=prepared.out_dir,
                shard_root=prepared.shard_root,
                session_shard_root=prepared.session_shard_root,
                shards_kept=settings.keep_shards,
                decryptor=settings.decryptor.value,
                tool_versions=prepared.tool_versions,
                key_kids=sorted(prepared.keys.kids),
                stream_kids=sorted(prepared.source.protection.must_cover),
                segment_ms=prepared.source.segment_ms,
                quality=settings.quality,
            ).as_document(),
            indent=2,
        )
    )
    return 0


def show_keys(settings: Settings) -> int:
    route = route_for(settings)
    if route.engine == routing.HLS:
        raise ConfigError(
            "keys applies to DASH streams; an HLS source is AES-128 encrypted and the proxy handles its keys",
            remedy="Point keys at an .mpd URL.",
        )
    console.stage("resolving the stream")
    source = mpd.resolve(settings.url, quality=settings.quality)
    console.stage("obtaining keys")
    keys = gather_keys(settings, source)
    report = coverage.inspect(keys, source.protection)
    console.stage("keys")
    console.say(str(keys))
    console.stage("coverage")
    console.table(
        [
            ("needed", ", ".join(sorted(report.needed)) or "(none declared)"),
            ("held", ", ".join(sorted(report.held))),
            ("missing", ", ".join(sorted(report.missing)) or "(none)"),
        ]
    )
    if settings.out_dir is not None:
        records.Artefacts(settings.out_dir).write(records.KEYS_NAME, str(keys))
        console.detail(f"written to {settings.out_dir / records.KEYS_NAME}")
    return 0 if report.covered else 2


def probe_environment(settings: Settings) -> int:
    """Run every binary and load the CDM a run depends on."""
    from . import toolchain as tools_mod

    console.stage("environment")
    ok = True
    tools = Toolchain.discover(settings.decryptor)
    for label, path in (
        (tools_mod.DOWNLOADER, tools.downloader),
        (tools_mod.MUXER, tools.muxer),
        (settings.decryptor.binary, tools.decryptor),
        (tools_mod.PROBER, tools.prober),
    ):
        if path is None:
            console.fail(f"{label}: not found")
            ok = False
            continue
        result = tools_mod.probe(path)
        if result.runnable:
            console.good(f"{label}: {result.detail}")
        else:
            console.fail(f"{label}: present but not runnable — {result.detail}")
            ok = False

    console.stage("local CDM")
    if not settings.cdm_path.exists():
        console.warn(f"no device file at {settings.cdm_path} — license replay will not work")
    else:
        try:
            from pywidevine import Cdm, Device  # type: ignore

            device = Device.load(str(settings.cdm_path))
            Cdm.from_device(device)
            console.good(
                f"{settings.cdm_path.name}: {device.type.name} L{device.security_level}"
            )
        except ImportError as exc:
            console.warn(f"pywidevine unavailable — cannot import {getattr(exc, 'name', 'pywidevine')}")
        except Exception as exc:
            console.fail(f"the CDM at {settings.cdm_path} could not be loaded: {exc}")
            ok = False

    console.stage("streamlink")
    try:
        import streamlink

        console.good(f"streamlink: {streamlink.__version__}")
    except ImportError:
        console.fail("streamlink: not installed — HLS sources will not resolve")
        ok = False

    if settings.url and routing.classify(settings.url).engine != routing.DASH:
        ok = _probe_hls(settings, tools.prober) and ok

    if ok:
        console.stage("ready")
    return 0 if ok else 1


def _probe_hls(settings: Settings, prober: Path | None) -> bool:
    """Network checks for a configured HLS source: playlist, AES key, decode."""
    import re
    import urllib.request

    from .live import resolver
    from .live.proxy import HLSProxy

    route = routing.classify(settings.url)
    endpoint = hls.Endpoint.parse(settings.hls_address or cfg.DEFAULT_HLS_ADDRESS)
    quality = settings.quality or resolver.DEFAULT_QUALITY
    console.stage("HLS source")
    try:
        with HLSProxy(settings.url, quality, endpoint.host, endpoint.port, media_url=route.media_url) as proxy:
            with urllib.request.urlopen(proxy.playlist_url, timeout=30) as response:
                playlist = response.read().decode("utf-8")
            console.good(f"playlist: {proxy.state.media_url}")
            console.detail(
                f"ads {'in current window' if '/tsad/' in playlist else 'not in current window'}; "
                f"discontinuities {playlist.count('#EXT-X-DISCONTINUITY')}"
            )

            key_match = re.search(r'URI="([^"]*key\.bin)"', playlist)
            if key_match:
                with urllib.request.urlopen(key_match.group(1), timeout=30) as response:
                    key = response.read()
                if len(key) == 16:
                    console.good("AES-128 key: 16 bytes through the proxy")
                else:
                    console.fail(f"AES-128 key has {len(key)} bytes, expected 16")
                    return False

            if prober is None:
                console.fail("ffprobe: not found — cannot decode-check the playlist")
                return False
            finished = subprocess.run(
                [
                    str(prober),
                    "-v", "error",
                    "-read_intervals", "%+8",
                    "-show_entries", "stream=codec_name,codec_type,width,height,sample_rate",
                    "-of", "json",
                    proxy.playlist_url,
                ],
                text=True,
                capture_output=True,
                timeout=60,
                check=False,
            )
            if finished.returncode != 0:
                console.fail(f"decode check failed: {finished.stderr.strip()[:200]}")
                return False
            console.good("decode check: playlist decodes")
            return True
    except Exception as exc:
        console.fail(f"HLS source: {exc}")
        return False


def serve_proxy(settings: Settings) -> int:
    route = route_for(settings)
    if route.engine != routing.HLS:
        raise ConfigError(
            "proxy serves HLS sources",
            remedy="Point --url at an ABEMA channel page or a media playlist (.m3u8).",
        )
    from .live import resolver
    from .live.proxy import HLSProxy

    endpoint = hls.Endpoint.parse(settings.hls_address or cfg.DEFAULT_HLS_ADDRESS)
    quality = settings.quality or resolver.DEFAULT_QUALITY
    proxy = HLSProxy(settings.url, quality, endpoint.host, endpoint.port, media_url=route.media_url)
    console.say(f"playlist: {proxy.playlist_url}")
    try:
        proxy.serve_forever()
    except KeyboardInterrupt:
        return 130
    return 0


def report_error(error: ArcError) -> int:
    console.fail(error.message, error.remedy)
    return error.exit_code


__all__ = [
    "ArcError",
    "LicenseError",
    "Prepared",
    "default_out_dir",
    "describe_plan",
    "execute",
    "prepare",
    "probe_environment",
    "report_error",
    "resolve_hls_address",
    "route_for",
    "serve_proxy",
    "show_keys",
]

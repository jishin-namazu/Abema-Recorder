"""Discovering and naming CDN shards for backfill."""

from __future__ import annotations

import base64
import json
import struct
from pathlib import Path

import pytest

from abema_recorder import backfill, netio, toolchain


def box(kind: bytes, payload: bytes) -> bytes:
    return struct.pack(">I4s", len(payload) + 8, kind) + payload


def full_box(kind: bytes, version: int, payload: bytes) -> bytes:
    return box(kind, bytes([version, 0, 0, 0]) + payload)


def test_an_indexed_segment_url_round_trips_its_query():
    indexed = backfill.IndexedUrl.parse(
        "https://cdn.example/live/index_video_7_0_2307.mp4?token=x"
    )
    assert indexed.index == 2307
    assert indexed.with_index(143).endswith("index_video_7_0_143.mp4?token=x")


def test_a_url_without_a_numeric_tail_is_refused():
    with pytest.raises(ValueError, match="numeric segment index"):
        backfill.IndexedUrl.parse("https://cdn.example/live/segment.mp4")


def test_contiguous_cdn_bounds_are_found_with_few_probes():
    calls = []

    def exists(index: int) -> bool:
        calls.append(index)
        return 143 <= index <= 912

    assert backfill.discover_bounds(500, exists) == (143, 912)
    assert len(calls) < 50


def test_the_seed_must_still_exist():
    with pytest.raises(ValueError, match="seed segment"):
        backfill.discover_bounds(500, lambda _: False)


@pytest.mark.parametrize(
    ("text", "expected"),
    [("0", 0), ("512K", 512 * 1024), ("3M", 3 * 1024**2), ("1.5m", int(1.5 * 1024**2))],
)
def test_rate_limits_are_human_readable(text, expected):
    assert backfill.parse_rate_limit(text) == expected


def test_bad_rate_limits_are_refused():
    with pytest.raises(ValueError, match="rate limit"):
        backfill.parse_rate_limit("quickly")


def test_local_timestamps_map_back_to_cdn_indexes_despite_aac_jitter():
    anchor_index = 297912785
    anchor_ms = 1787476708010
    assert backfill.index_for_timestamp(1787476600000, anchor_index, anchor_ms, 6000) == 297912767
    assert backfill.index_for_timestamp(1787476702016, anchor_index, anchor_ms, 6000) == 297912784


def test_tfdt_version_zero_is_read_through_moof_and_traf():
    tfdt = full_box(b"tfdt", 0, struct.pack(">I", 123456))
    fragment = box(b"moof", box(b"traf", tfdt))
    assert backfill.read_tfdt(fragment) == 123456


def test_tfdt_version_one_keeps_a_64_bit_decode_time():
    tfdt = full_box(b"tfdt", 1, struct.pack(">Q", 2**40 + 7))
    assert backfill.read_tfdt(box(b"moof", box(b"traf", tfdt))) == 2**40 + 7


def test_mdhd_timescale_is_read_from_a_version_zero_init():
    mdhd = full_box(b"mdhd", 0, struct.pack(">IIII", 1, 2, 48_000, 99))
    init = box(b"moov", box(b"trak", box(b"mdia", mdhd)))
    assert backfill.read_timescale(init) == 48_000


def test_decode_time_calibrates_the_exact_program_timestamp():
    stamp = backfill.timestamp_from_decode_time(
        anchor_timestamp_ms=1_000_005,
        anchor_decode_time=48_000,
        candidate_decode_time=48_000 + 288_512,
        timescale=48_000,
    )
    assert stamp == 1_006_015


def test_missing_but_not_recovered_is_not_a_complete_result():
    track = backfill.TrackResult("video", 10, 20, 20, 10, 1, 0)
    assert not backfill.BackfillResult(False, (track,)).ok


def test_plans_prefers_a_segment_present_on_disk_and_fallback_updates_anchor_timestamp(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = tmp_path / "run_1"
    out_dir = tmp_path / "run_1.out"
    video_dir = root / "0_video"
    video_dir.mkdir(parents=True)
    out_dir.mkdir(parents=True)

    mdhd = full_box(b"mdhd", 0, struct.pack(">IIII", 1, 2, 1000, 99))
    hdlr = full_box(b"hdlr", 0, b"\x00\x00\x00\x00vide" + b"\x00" * 12)
    (video_dir / "_init.mp4").write_bytes(box(b"moov", box(b"trak", box(b"mdia", mdhd + hdlr))))

    # Segment 100 (1970-01-01T00:00:10Z -> 10_000 ms) is absent locally.
    # Segment 102 (1970-01-01T00:00:22Z -> 22_000 ms) is present locally.
    frag_102 = box(b"moof", box(b"traf", full_box(b"tfdt", 0, struct.pack(">I", 22_000))))
    (video_dir / "22000_dec.m4s").write_bytes(frag_102)

    kid = "0" * 32
    key = "1" * 32
    wv_header_b64 = base64.b64encode(b"\x12\x10" + bytes.fromhex(kid)).decode("ascii")
    (out_dir / "run.json").write_text(
        json.dumps(
            {
                "engine": "dash",
                "run_name": "run_1",
                "shard_root": str(root),
                "segment_ms": 6000,
            }
        ),
        encoding="utf-8",
    )
    (out_dir / "keys.txt").write_text(f"{kid}:{key}\n", encoding="utf-8")

    selected = [
        {
            "MediaType": "VIDEO",
            "Playlist": {
                "MediaInit": {"EncryptInfo": {"Key": wv_header_b64}},
                "MediaParts": [
                    {
                        "MediaSegments": [
                            {
                                "Index": 100,
                                "DateTime": "1970-01-01T00:00:10.000Z",
                                "Url": "https://cdn.example/index_video_7_0_100.mp4",
                            },
                            {
                                "Index": 101,
                                "DateTime": "1970-01-01T00:00:16.000Z",
                                "Url": "https://cdn.example/index_video_7_0_101.mp4",
                            },
                        ]
                    }
                ],
            },
        }
    ]
    (root / "meta_selected.json").write_text(json.dumps(selected), encoding="utf-8")

    monkeypatch.setattr(backfill, "find", lambda *a, **k: tmp_path / "shaka")
    monkeypatch.setattr(backfill, "probe", lambda p: toolchain.Probe(True, "ok"))
    monkeypatch.setattr(
        netio,
        "head",
        lambda url, timeout=15: netio.Reply(200 if any(f"_{i}.mp4" in url for i in (100, 101, 102)) else 404, b""),
    )

    def fake_download(url: str, destination: Path, rate_limit: int) -> None:
        idx = int(url.rsplit("_", 1)[-1].removesuffix(".mp4"))
        tfdt_ms = 10_000 + (idx - 100) * 6000
        destination.write_bytes(box(b"moof", box(b"traf", full_box(b"tfdt", 0, struct.pack(">I", tfdt_ms)))))

    decrypted_destinations: list[Path] = []

    def fake_decrypt(plan, encrypted: Path, destination: Path, binary: Path) -> None:
        decrypted_destinations.append(destination)
        destination.write_bytes(encrypted.read_bytes())

    monkeypatch.setattr(backfill, "_download", fake_download)
    monkeypatch.setattr(backfill, "_decrypt", fake_decrypt)

    result = backfill.run(out_dir, include_live_tail=True)
    assert result.ok
    assert sorted(p.name for p in decrypted_destinations) == ["10000_dec.m4s", "16000_dec.m4s"]

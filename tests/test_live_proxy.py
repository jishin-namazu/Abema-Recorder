from abema_recorder.live.playlists import decode_url, rewrite_playlist


REMOTE = "https://linear-abematv.akamaized.net/channel/luckyfes/1080/playlist.m3u8"


def test_rewrite_keeps_ads_keys_and_discontinuity_for_playback():
    source = """#EXTM3U
#EXT-X-MEDIA-SEQUENCE:100
#EXT-X-DISCONTINUITY-SEQUENCE:25
#EXT-X-KEY:METHOD=AES-128,URI="abematv-license://ticket",IV=0x01
#EXTINF:4.0,
/tslive/channel/a.ts
#EXT-X-DISCONTINUITY
#EXT-X-KEY:METHOD=NONE
#EXTINF:5.0,
/tsad/luckyfes/ad.ts
#EXT-X-DISCONTINUITY
#EXT-X-KEY:METHOD=AES-128,URI="abematv-license://ticket2",IV=0x02
#EXTINF:4.0,
/tslive/channel/b.ts
"""
    result = rewrite_playlist(source, REMOTE, "http://127.0.0.1:18081")

    assert result.text.count("#EXT-X-DISCONTINUITY\n") == 2
    assert "#EXT-X-DISCONTINUITY-SEQUENCE:25" in result.text
    assert "/tsad/" not in result.text  # hidden behind the local URL, not filtered
    assert [s.mode for s in result.segments] == ["tslive", "tsad", "tslive"]
    assert [s.discontinuity for s in result.segments] == [False, True, True]
    assert [s.sequence for s in result.segments] == [100, 101, 102]
    assert result.text.count(".ts") == 3
    assert "key.bin" in result.text


def test_rewrite_can_hide_discontinuity_for_clean_endpoint():
    source = """#EXTM3U
#EXT-X-DISCONTINUITY-SEQUENCE:25
#EXTINF:4.0,
/tslive/channel/a.ts
#EXT-X-DISCONTINUITY
#EXTINF:5.0,
/tsad/luckyfes/ad.ts
"""

    result = rewrite_playlist(
        source,
        REMOTE,
        "http://127.0.0.1:18081",
        strip_discontinuity=True,
    )

    assert "#EXT-X-DISCONTINUITY" not in result.text
    assert [segment.discontinuity for segment in result.segments] == [False, True]


def test_resource_token_round_trip():
    url = "abematv-license://abc123"
    result = rewrite_playlist(
        f'#EXTM3U\n#EXT-X-KEY:METHOD=AES-128,URI="{url}"\n',
        REMOTE,
        "http://127.0.0.1:18081",
    )
    token = result.text.split("/resource/", 1)[1].split("/", 1)[0]
    assert decode_url(token) == url

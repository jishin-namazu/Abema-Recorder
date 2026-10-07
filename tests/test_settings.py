"""Settings file alias parsing."""

from __future__ import annotations

from abema_recorder import settings as cfg


def test_aliases_map_to_canonical_variables() -> None:
    values = cfg.parse_settings_file(
        """
        # comment
        mpd=https://example.com/index.mpd
        token=t%3Daaa%26pt%3Dbbb
        license=https://license.example/wv?pt=ccc
        key=0123456789abcdef0123456789abcdef:0123456789abcdef0123456789abcdef
        headers=headers.txt
        """
    )
    assert values[cfg.ENV_URL] == "https://example.com/index.mpd"
    assert values[cfg.ENV_TOKEN] == "t%3Daaa%26pt%3Dbbb"
    assert values[cfg.ENV_LICENSE_URL] == "https://license.example/wv?pt=ccc"
    assert values[cfg.ENV_KEYS].count(":") == 1
    assert values[cfg.ENV_HEADERS] == "headers.txt"


def test_url_and_pt_aliases() -> None:
    values = cfg.parse_settings_file("url=https://example.com/a.mpd\npt=eyJhbGciOiJFUzI1NiJ9.x.y\n")
    assert values[cfg.ENV_URL] == "https://example.com/a.mpd"
    assert values[cfg.ENV_TOKEN] == "eyJhbGciOiJFUzI1NiJ9.x.y"


def test_one_layer_of_matching_quotes_stripped() -> None:
    values = cfg.parse_settings_file('token="abc=def"\n')
    assert values[cfg.ENV_TOKEN] == "abc=def"


def test_jwt_verbatim_unquoted() -> None:
    raw = "eyJhbGciOiJFUzI1NiIsInR5cCI6IkpXVCJ9.eyJzdWIiOiIxIn0.SflKxwRJSMeKKF2QT4fwpMeJf36POk6yJV_adQssw5c"
    values = cfg.parse_settings_file(f"token={raw}\n")
    assert values[cfg.ENV_TOKEN] == raw


def test_unknown_names_pass_through() -> None:
    values = cfg.parse_settings_file("SOMETHING_ELSE=1\n")
    assert values == {"SOMETHING_ELSE": "1"}


def test_export_prefix_and_blank_lines_tolerated() -> None:
    values = cfg.parse_settings_file("\nexport mpd=https://example.com/x.mpd\n\n")
    assert values[cfg.ENV_URL] == "https://example.com/x.mpd"


def test_quality_alias() -> None:
    values = cfg.parse_settings_file("quality=720p\n")
    assert values[cfg.ENV_QUALITY] == "720p"


def test_legacy_abema_aliases() -> None:
    values = cfg.parse_settings_file(
        "ABEMA_URL=https://abema.tv/now-on-air/x\nABEMA_QUALITY=1080p\nABEMA_PROXY_PORT=18081\n"
    )
    assert values[cfg.ENV_URL] == "https://abema.tv/now-on-air/x"
    assert values[cfg.ENV_QUALITY] == "1080p"
    assert values[cfg.ENV_HLS] == "18081"


def test_legacy_environment_fallback(monkeypatch) -> None:
    monkeypatch.delenv(cfg.ENV_URL, raising=False)
    monkeypatch.setenv("ABEMA_URL", "https://abema.tv/now-on-air/x")
    assert cfg.env(cfg.ENV_URL) == "https://abema.tv/now-on-air/x"
    monkeypatch.setenv(cfg.ENV_URL, "https://example.com/a.mpd")
    assert cfg.env(cfg.ENV_URL) == "https://example.com/a.mpd"


def test_empty_env_value_does_not_veto_settings_file(tmp_path, monkeypatch) -> None:
    target = tmp_path / "test.env"
    target.write_text("url=https://example.com/a.mpd\n", encoding="utf-8")
    monkeypatch.setenv("ABM_URL", "")
    path, applied = cfg.load_settings_file(target)
    assert applied[cfg.ENV_URL] == "https://example.com/a.mpd"
    assert cfg.env(cfg.ENV_URL) == "https://example.com/a.mpd"


def test_real_env_value_still_wins_over_settings_file(tmp_path, monkeypatch) -> None:
    target = tmp_path / "test.env"
    target.write_text("url=https://example.com/a.mpd\n", encoding="utf-8")
    monkeypatch.setenv("ABM_URL", "https://env.example/x.mpd")
    cfg.load_settings_file(target)
    assert cfg.env(cfg.ENV_URL) == "https://env.example/x.mpd"

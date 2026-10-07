"""License target construction from the three --token spellings."""

from __future__ import annotations

from abema_recorder.licensing import ABEMA_LICENSE, LicenseTarget, sanitise_headers


def test_full_url_used_as_is() -> None:
    url = f"{ABEMA_LICENSE}?t=opaque&pt=jwt"
    assert LicenseTarget.from_token(url).url == url
    assert LicenseTarget.from_token("https://other.example/wv?pt=x").url == "https://other.example/wv?pt=x"


def test_query_string_appended_to_endpoint() -> None:
    target = LicenseTarget.from_token("t=opaque&pt=jwt")
    assert target.url == f"{ABEMA_LICENSE}?t=opaque&pt=jwt"


def test_leading_question_mark_tolerated() -> None:
    target = LicenseTarget.from_token("?t=opaque&pt=jwt")
    assert target.url == f"{ABEMA_LICENSE}?t=opaque&pt=jwt"


def test_stray_cmcd_parameter_kept() -> None:
    target = LicenseTarget.from_token("t=opaque&pt=jwt&CMCD=br%3D4000")
    assert target.url == f"{ABEMA_LICENSE}?t=opaque&pt=jwt&CMCD=br%3D4000"


def test_pt_only_query_recognised() -> None:
    target = LicenseTarget.from_token("pt=jwt")
    assert target.url == f"{ABEMA_LICENSE}?pt=jwt"


def test_bare_jwt_becomes_pt_parameter() -> None:
    jwt = "eyJhbGciOiJFUzI1NiJ9.eyJzdWIiOiIxIn0.signature"
    target = LicenseTarget.from_token(jwt)
    assert target.url == f"{ABEMA_LICENSE}?pt={jwt}"


def test_redaction_hides_credentials() -> None:
    target = LicenseTarget.from_token(f"{ABEMA_LICENSE}?t=opaque&pt=jwt")
    shown = target.redacted()
    assert "opaque" not in shown
    assert "jwt" not in shown
    assert "t=<redacted>" in shown and "pt=<redacted>" in shown


def test_sanitise_drops_hop_by_hop_headers() -> None:
    kept = sanitise_headers(
        {
            "Host": "license.p-c3-e.abema-tv.com",
            "Content-Length": "1234",
            ":authority": "x",
            "Origin": "https://abema.tv",
            "Cookie": "a=b",
        }
    )
    assert kept == {"Origin": "https://abema.tv", "Cookie": "a=b"}

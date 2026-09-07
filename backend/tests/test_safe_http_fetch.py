"""D17 SSRF-safe fetch contract.

No real external network. Resolver and transport are injected.
"""
from __future__ import annotations

import os
import socket
import sys

_HERE = os.path.dirname(os.path.abspath(__file__))
_BACKEND = os.path.abspath(os.path.join(_HERE, ".."))
_REPO = os.path.abspath(os.path.join(_BACKEND, ".."))
for _p in (_REPO, _BACKEND, os.path.join(_REPO, "database")):
    if _p not in sys.path:
        sys.path.insert(0, _p)

from services.safe_http_fetch import (  # noqa: E402
    SafeFetchResponse,
    fetch_url,
    is_blocked_ip,
    redact_url_for_log,
    validate_destination,
)


def _resolver(ip: str):
    def _inner(hostname: str, port: int):
        return [(socket.AF_INET, ip)]
    return _inner


def _mixed_resolver(hostname: str, port: int):
    return [(socket.AF_INET, "8.8.8.8"), (socket.AF_INET, "127.0.0.1")]


def test_e_literal_private_loopback_link_local_metadata_blocked() -> None:
    for url, klass in (
        ("http://127.0.0.1/", "ip_blocked"),
        ("http://10.0.0.5/x", "ip_blocked"),
        ("http://192.168.1.8/x", "ip_blocked"),
        ("http://169.254.169.254/latest/meta-data", "ip_blocked"),
        ("http://[::1]/", "ip_blocked"),
        ("http://[fd00:ec2::254]/", "ip_blocked"),
        ("http://localhost/admin", "host_blocked"),
        ("file:///etc/passwd", "scheme_blocked"),
        ("https://user:pass@example.test/", "credentials_blocked"),
    ):
        req, err = validate_destination(url, resolver=_resolver("1.1.1.1"))
        assert req is None, url
        assert err in {klass, "ip_blocked", "host_blocked", "scheme_blocked", "credentials_blocked"}, (url, err)


def test_e_blocked_ip_helper() -> None:
    assert is_blocked_ip("127.0.0.1") is True
    assert is_blocked_ip("10.1.2.3") is True
    assert is_blocked_ip("169.254.169.254") is True
    assert is_blocked_ip("::1") is True
    assert is_blocked_ip("8.8.8.8") is False


def test_f_redirect_to_private_ip_blocked() -> None:
    calls = []

    def transport(request):
        calls.append(request.url)
        if "example.test" in request.hostname:
            return SafeFetchResponse(
                status=302,
                headers={"location": "http://127.0.0.1/secret"},
            )
        raise AssertionError("private hop must not be fetched")

    result = fetch_url(
        "https://example.test/share",
        resolver=_resolver("8.8.8.8"),
        transport=transport,
    )
    assert result.ok is False
    assert result.error_class == "ip_blocked"
    assert len(calls) == 1


def test_g_mixed_public_private_dns_blocked() -> None:
    req, err = validate_destination(
        "https://rebinding.test/x",
        resolver=_mixed_resolver,
    )
    assert req is None
    assert err == "ip_blocked"


def test_h_redirect_loop_timeout_oversized_content_type() -> None:
    def loop_transport(request):
        return SafeFetchResponse(status=302, headers={"location": "/loop"})

    looped = fetch_url(
        "https://example.test/loop",
        resolver=_resolver("8.8.8.8"),
        transport=loop_transport,
    )
    assert looped.ok is False
    assert looped.error_class in {"redirect_loop", "too_many_redirects"}

    timed = fetch_url(
        "https://example.test/slow",
        resolver=_resolver("8.8.8.8"),
        transport=lambda request: SafeFetchResponse(status=0, error_class="timeout"),
    )
    assert timed.ok is False
    assert timed.error_class == "timeout"

    huge = fetch_url(
        "https://example.test/big",
        resolver=_resolver("8.8.8.8"),
        transport=lambda request: SafeFetchResponse(status=0, error_class="oversized"),
    )
    assert huge.ok is False
    assert huge.error_class == "oversized"

    pdf = fetch_url(
        "https://example.test/file.pdf",
        resolver=_resolver("8.8.8.8"),
        transport=lambda request: SafeFetchResponse(
            status=200,
            headers={"content-type": "application/pdf"},
            body=b"%PDF-1.4",
        ),
    )
    assert pdf.ok is False
    assert pdf.error_class == "content_type_blocked"


def test_query_string_redacted_in_logs() -> None:
    redacted = redact_url_for_log("https://example.test/p?token=super-secret&x=1")
    assert "super-secret" not in redacted
    assert "token=" not in redacted
    assert "example.test" in redacted

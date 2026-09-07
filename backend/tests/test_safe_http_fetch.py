"""D17 SSRF-safe fetch contract.

No real external network. Validation is separate from transport.
``default_transport`` is exercised against a local TCP fixture.
"""
from __future__ import annotations

import os
import socket
import ssl
import sys
import threading
import time
from typing import Callable

_HERE = os.path.dirname(os.path.abspath(__file__))
_BACKEND = os.path.abspath(os.path.join(_HERE, ".."))
_REPO = os.path.abspath(os.path.join(_BACKEND, ".."))
for _p in (_REPO, _BACKEND, os.path.join(_REPO, "database")):
    if _p not in sys.path:
        sys.path.insert(0, _p)

import asyncio
from unittest.mock import patch

from services.safe_http_fetch import (  # noqa: E402
    MAX_RESPONSE_BYTES,
    SafeFetchRequest,
    SafeFetchResponse,
    async_default_transport,
    default_transport,
    fetch_url,
    hostname_is_ip_trick,
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


class _LocalHttpServer:
    def __init__(self, handler: Callable[[socket.socket], None]) -> None:
        self._handler = handler
        self._sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self._sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self._sock.bind(("127.0.0.1", 0))
        self._sock.listen(1)
        self.port = int(self._sock.getsockname()[1])
        self.closed = threading.Event()
        self._thread = threading.Thread(target=self._run, daemon=True)
        self._thread.start()

    def _run(self) -> None:
        try:
            self._sock.settimeout(5)
            conn, _addr = self._sock.accept()
            with conn:
                self._handler(conn)
        except OSError:
            pass
        finally:
            self.closed.set()
            try:
                self._sock.close()
            except OSError:
                pass

    def close(self) -> None:
        try:
            self._sock.close()
        except OSError:
            pass
        self._thread.join(timeout=2)


def _request(port: int, **kwargs: object) -> SafeFetchRequest:
    deadline = float(kwargs.pop("deadline", time.monotonic() + 8.0) or 0.0)
    return SafeFetchRequest(
        url=f"http://example.test:{port}/",
        hostname=str(kwargs.get("hostname") or "example.test"),
        ip="127.0.0.1",
        port=int(port),
        scheme=str(kwargs.get("scheme") or "http"),
        path=str(kwargs.get("path") or "/"),
        timeout_connect=3.0,
        timeout_read=8.0,
        deadline=deadline,
    )


def test_e_literal_private_loopback_link_local_metadata_blocked() -> None:
    for url, klass in (
        ("http://127.0.0.1/", "ip_blocked"),
        ("http://10.0.0.5/x", "ip_blocked"),
        ("http://192.168.1.8/x", "ip_blocked"),
        ("http://169.254.169.254/latest/meta-data", "ip_blocked"),
        ("http://[::1]/", "ip_blocked"),
        ("http://[fd00:ec2::254]/", "ip_blocked"),
        ("http://[::ffff:127.0.0.1]/", "ip_blocked"),
        ("http://[::ffff:169.254.169.254]/", "ip_blocked"),
        ("http://localhost/admin", "host_blocked"),
        ("http://localhost./admin", "host_blocked"),
        ("file:///etc/passwd", "scheme_blocked"),
        ("https://user:pass@example.test/", "credentials_blocked"),
        ("https://example.test:22/x", "invalid_port"),
        ("http://example.test:8080/x", "invalid_port"),
        ("https://example.test:80/x", "invalid_port"),
        ("http://2130706433/", "ip_blocked"),
        ("http://0177.0.0.1/", "ip_blocked"),
        ("http://127.1/", "ip_blocked"),
        ("http://0x7f.0.0.1/", "ip_blocked"),
    ):
        req, err = validate_destination(url, resolver=_resolver("8.8.8.8"))
        assert req is None, url
        assert err in {
            klass,
            "ip_blocked",
            "host_blocked",
            "scheme_blocked",
            "credentials_blocked",
            "invalid_port",
        }, (url, err)


def test_e_blocked_ip_helper() -> None:
    assert is_blocked_ip("127.0.0.1") is True
    assert is_blocked_ip("10.1.2.3") is True
    assert is_blocked_ip("169.254.169.254") is True
    assert is_blocked_ip("::1") is True
    assert is_blocked_ip("::ffff:127.0.0.1") is True
    assert is_blocked_ip("8.8.8.8") is False
    assert hostname_is_ip_trick("2130706433") is True
    assert hostname_is_ip_trick("0177.0.0.1") is True
    assert hostname_is_ip_trick("0x7f.0.0.1") is True
    assert hostname_is_ip_trick("127.1") is True
    assert hostname_is_ip_trick("::ffff:8.8.8.8") is True
    assert hostname_is_ip_trick("example.test") is False


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

    empty_ct = fetch_url(
        "https://example.test/bin",
        resolver=_resolver("8.8.8.8"),
        transport=lambda request: SafeFetchResponse(
            status=200,
            headers={},
            body=b"\x00\x01VIDEO",
        ),
    )
    assert empty_ct.ok is False
    assert empty_ct.error_class == "content_type_blocked"


def test_query_string_redacted_in_logs() -> None:
    redacted = redact_url_for_log("https://example.test/p?token=super-secret&x=1#frag")
    assert "super-secret" not in redacted
    assert "token=" not in redacted
    assert "frag" not in redacted
    assert "example.test" in redacted


def test_default_transport_exact_max_bytes() -> None:
    body = b"a" * MAX_RESPONSE_BYTES
    payload = (
        b"HTTP/1.1 200 OK\r\nContent-Type: text/html\r\nConnection: close\r\n\r\n" + body
    )

    def handler(conn: socket.socket) -> None:
        conn.recv(1024)
        conn.sendall(payload)

    server = _LocalHttpServer(handler)
    try:
        resp = default_transport(_request(server.port, deadline=time.monotonic() + 3))
        assert resp.error_class == ""
        assert len(resp.body) == MAX_RESPONSE_BYTES
    finally:
        server.close()


def test_default_transport_max_bytes_plus_one_closes() -> None:
    body = b"a" * (MAX_RESPONSE_BYTES + 1)
    payload = (
        b"HTTP/1.1 200 OK\r\nContent-Type: text/html\r\nConnection: close\r\n\r\n" + body
    )

    def handler(conn: socket.socket) -> None:
        conn.recv(1024)
        try:
            conn.sendall(payload)
        except OSError:
            pass

    server = _LocalHttpServer(handler)
    try:
        resp = default_transport(_request(server.port, deadline=time.monotonic() + 3))
        assert resp.error_class == ""
        assert len(resp.body) == MAX_RESPONSE_BYTES
        assert resp.body_truncated is True
    finally:
        server.close()


def test_default_transport_slow_stream_uses_total_deadline() -> None:
    started = time.monotonic()

    def handler(conn: socket.socket) -> None:
        conn.recv(1024)
        conn.sendall(b"HTTP/1.1 200 OK\r\nContent-Type: text/html\r\n\r\n")
        for _ in range(20):
            time.sleep(0.25)
            try:
                conn.sendall(b"x")
            except OSError:
                return

    server = _LocalHttpServer(handler)
    try:
        resp = default_transport(_request(server.port, deadline=started + 0.6))
        elapsed = time.monotonic() - started
        assert resp.error_class == "timeout"
        assert elapsed < 2.5
        assert server.closed.wait(2.0)
    finally:
        server.close()


def test_default_transport_connects_to_request_ip_not_hostname() -> None:
    seen: dict[str, object] = {}

    def handler(conn: socket.socket) -> None:
        peer = conn.getpeername()
        seen["peer"] = peer
        data = conn.recv(2048)
        seen["raw"] = data
        conn.sendall(
            b"HTTP/1.1 200 OK\r\nContent-Type: text/html\r\n\r\n<html></html>"
        )

    server = _LocalHttpServer(handler)
    try:
        req = _request(server.port, hostname="fixture.example.test")
        resp = default_transport(req, deadline=time.monotonic() + 3)
        assert resp.status == 200
        raw = seen.get("raw") or b""
        assert b"Host: fixture.example.test" in raw
        assert b"Accept-Encoding: identity" in raw
        assert b"Cookie:" not in raw
        assert b"Authorization:" not in raw
    finally:
        server.close()


def test_default_transport_tls_hostname_mismatch() -> None:
    from cryptography import x509  # noqa: PLC0415
    from cryptography.hazmat.primitives import hashes, serialization  # noqa: PLC0415
    from cryptography.hazmat.primitives.asymmetric import rsa  # noqa: PLC0415
    from cryptography.x509.oid import NameOID  # noqa: PLC0415
    import datetime  # noqa: PLC0415

    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "other.test")])
    cert = (
        x509.CertificateBuilder()
        .subject_name(name)
        .issuer_name(name)
        .public_key(key.public_key())
        .serial_number(1)
        .not_valid_before(datetime.datetime.utcnow() - datetime.timedelta(days=1))
        .not_valid_after(datetime.datetime.utcnow() + datetime.timedelta(days=1))
        .sign(key, hashes.SHA256())
    )
    cert_pem = cert.public_bytes(serialization.Encoding.PEM)
    key_pem = key.private_bytes(
        serialization.Encoding.PEM,
        serialization.PrivateFormat.TraditionalOpenSSL,
        serialization.NoEncryption(),
    )
    server_ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    import tempfile  # noqa: PLC0415

    with tempfile.NamedTemporaryFile(delete=False) as cf, tempfile.NamedTemporaryFile(
        delete=False
    ) as kf:
        cf.write(cert_pem)
        kf.write(key_pem)
        cert_path, key_path = cf.name, kf.name
    server_ctx.load_cert_chain(cert_path, key_path)

    raw_sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    raw_sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    raw_sock.bind(("127.0.0.1", 0))
    raw_sock.listen(1)
    port = int(raw_sock.getsockname()[1])

    def serve() -> None:
        try:
            raw_sock.settimeout(3)
            conn, _addr = raw_sock.accept()
            tls = server_ctx.wrap_socket(conn, server_side=True)
            tls.close()
        except Exception:  # noqa: silent-ok — local TLS fixture teardown
            pass
        finally:
            raw_sock.close()

    threading.Thread(target=serve, daemon=True).start()
    client_ctx = ssl.create_default_context()
    client_ctx.check_hostname = True
    client_ctx.verify_mode = ssl.CERT_REQUIRED
    client_ctx.load_verify_locations(cadata=cert_pem.decode("ascii"))
    req = SafeFetchRequest(
        url=f"https://example.test:{port}/",
        hostname="example.test",
        ip="127.0.0.1",
        port=port,
        scheme="https",
        path="/",
        timeout_connect=3.0,
        timeout_read=3.0,
        deadline=time.monotonic() + 3,
    )
    resp = default_transport(req, deadline=time.monotonic() + 3, ssl_context=client_ctx)
    assert resp.error_class == "tls_error"
    os.unlink(cert_path)
    os.unlink(key_path)


def test_explicit_80_and_443_allowed_other_ports_blocked() -> None:
    req, err = validate_destination(
        "http://example.test:80/x", resolver=_resolver("8.8.8.8")
    )
    assert err == ""
    assert req is not None and req.port == 80
    req, err = validate_destination(
        "https://example.test:443/x", resolver=_resolver("8.8.8.8")
    )
    assert err == ""
    assert req is not None and req.port == 443


def test_trailing_dot_and_idna_canonicalized_before_dns() -> None:
    seen: dict[str, str] = {}

    def resolver(hostname: str, port: int):
        seen["host"] = hostname
        return [(socket.AF_INET, "8.8.8.8")]

    req, err = validate_destination("https://Example.TEST./page", resolver=resolver)
    assert err == ""
    assert req is not None
    assert seen["host"] == "example.test"
    req, err = validate_destination(
        "https://bücher.example.test./p", resolver=resolver
    )
    assert err == ""
    assert seen["host"] == "xn--bcher-kva.example.test"


def test_xml_content_type_blocked() -> None:
    xml = fetch_url(
        "https://example.test/feed.xml",
        resolver=_resolver("8.8.8.8"),
        transport=lambda request: SafeFetchResponse(
            status=200,
            headers={"content-type": "application/xml"},
            body=b"<rss></rss>",
        ),
    )
    assert xml.ok is False
    assert xml.error_class == "content_type_blocked"


def test_private_destination_does_not_call_transport() -> None:
    calls: list[object] = []

    def transport(request):
        calls.append(request)
        raise AssertionError("private destination must not be fetched")

    result = fetch_url(
        "https://evil.test/secret",
        resolver=_resolver("127.0.0.1"),
        transport=transport,
    )
    assert result.ok is False
    assert result.error_class == "ip_blocked"
    assert calls == []


def test_dns_timeout_is_deadline_bounded() -> None:
    def hang(*_a, **_k):
        time.sleep(8)
        raise AssertionError("getaddrinfo must not be awaited past deadline")

    started = time.monotonic()
    with patch("services.safe_http_fetch.socket.getaddrinfo", side_effect=hang):
        result = fetch_url(
            "https://example.test/slow-dns",
            deadline=started + 0.35,
            transport=lambda request: (_ for _ in ()).throw(
                AssertionError("transport must not run after dns timeout")
            ),
        )
    elapsed = time.monotonic() - started
    assert result.error_class == "timeout"
    assert elapsed < 1.5


def test_default_transport_slow_headers_uses_total_deadline() -> None:
    started = time.monotonic()

    def handler(conn: socket.socket) -> None:
        conn.recv(1024)
        conn.sendall(b"HTTP/1.1 200 OK\r\n")
        time.sleep(2)
        try:
            conn.sendall(b"Content-Type: text/html\r\n\r\n<body></body>")
        except OSError:
            return

    server = _LocalHttpServer(handler)
    try:
        resp = default_transport(_request(server.port, deadline=started + 0.4))
        elapsed = time.monotonic() - started
        assert resp.error_class == "timeout"
        assert elapsed < 2.0
    finally:
        server.close()


def test_default_transport_connect_timeout() -> None:
    blocker = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    blocker.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    blocker.bind(("127.0.0.1", 0))
    blocker.listen(1)
    port = int(blocker.getsockname()[1])
    filler = None
    try:
        filler = socket.create_connection(("127.0.0.1", port), timeout=1)
        started = time.monotonic()
        resp = default_transport(_request(port, deadline=started + 0.4))
        elapsed = time.monotonic() - started
        assert resp.error_class in {"timeout", "network_error"}
        assert elapsed < 2.5
    finally:
        if filler is not None:
            filler.close()
        blocker.close()


def test_tls_handshake_timeout_closes_socket() -> None:
    raw_sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    raw_sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    raw_sock.bind(("127.0.0.1", 0))
    raw_sock.listen(1)
    port = int(raw_sock.getsockname()[1])

    def serve() -> None:
        try:
            raw_sock.settimeout(4)
            conn, _addr = raw_sock.accept()
            time.sleep(3)
            conn.close()
        except Exception:  # noqa: silent-ok — local TLS fixture teardown
            pass
        finally:
            raw_sock.close()

    threading.Thread(target=serve, daemon=True).start()
    client_ctx = ssl.create_default_context()
    client_ctx.check_hostname = False
    client_ctx.verify_mode = ssl.CERT_NONE
    started = time.monotonic()
    req = SafeFetchRequest(
        url=f"https://example.test:{port}/",
        hostname="example.test",
        ip="127.0.0.1",
        port=port,
        scheme="https",
        path="/",
        timeout_connect=3.0,
        timeout_read=3.0,
        deadline=started + 0.45,
    )
    resp = default_transport(req, deadline=started + 0.45, ssl_context=client_ctx)
    elapsed = time.monotonic() - started
    assert resp.error_class in {"timeout", "tls_error", "network_error"}
    assert elapsed < 2.0


def test_async_default_transport_slow_body_deadline() -> None:
    started = time.monotonic()

    def handler(conn: socket.socket) -> None:
        conn.recv(1024)
        conn.sendall(b"HTTP/1.1 200 OK\r\nContent-Type: text/html\r\n\r\n")
        for _ in range(20):
            time.sleep(0.25)
            try:
                conn.sendall(b"x")
            except OSError:
                return

    server = _LocalHttpServer(handler)
    try:
        async def _go() -> SafeFetchResponse:
            return await async_default_transport(
                _request(server.port, deadline=started + 0.6)
            )

        resp = asyncio.run(_go())
        elapsed = time.monotonic() - started
        assert resp.error_class == "timeout"
        assert elapsed < 2.5
    finally:
        server.close()


def _local_transport(server_port: int):
    def _transport(request: SafeFetchRequest) -> SafeFetchResponse:
        local_req = SafeFetchRequest(
            url=request.url,
            hostname=request.hostname,
            ip="127.0.0.1",
            port=server_port,
            scheme="http",
            path=request.path or "/",
            timeout_connect=request.timeout_connect,
            timeout_read=request.timeout_read,
            deadline=request.deadline,
        )
        return default_transport(local_req, deadline=request.deadline or time.monotonic() + 8.0)

    return _transport


def _html_with_og(
    *,
    title: str = "OG Title",
    description: str = "OG Description",
    site_name: str = "Example",
    tail: bytes = b"",
) -> bytes:
    head = (
        "<!doctype html><html><head>"
        f'<meta property="og:title" content="{title}">'
        f'<meta property="og:description" content="{description}">'
        f'<meta property="og:site_name" content="{site_name}">'
        "</head><body>"
    ).encode("utf-8")
    return head + tail


def test_a_large_html_og_metadata_prefix_enrichment_via_fetch() -> None:
    html = _html_with_og(tail=b"z" * (MAX_RESPONSE_BYTES + 50_000))

    def handler(conn: socket.socket) -> None:
        conn.recv(1024)
        try:
            conn.sendall(
                b"HTTP/1.1 200 OK\r\nContent-Type: text/html\r\nConnection: close\r\n\r\n" + html
            )
        except OSError:
            pass

    server = _LocalHttpServer(handler)
    try:
        result = fetch_url(
            "http://example.test/",
            resolver=_resolver("8.8.8.8"),
            transport=_local_transport(server.port),
        )
        assert result.ok is True
        assert result.body_truncated is True
        assert len(result.body) <= MAX_RESPONSE_BYTES
        assert b"og:title" in result.body
        assert len(result.body) < len(html)
    finally:
        server.close()


def test_b_metadata_split_across_chunks() -> None:
    part1 = (
        b"HTTP/1.1 200 OK\r\nContent-Type: text/html\r\nConnection: close\r\n\r\n"
        b"<!doctype html><html><head><meta property=\"og:title\" content=\"Chunk"
    )
    part2 = b"ed Title\"></head><body>" + (b"x" * 120_000)

    def handler(conn: socket.socket) -> None:
        conn.recv(1024)
        conn.sendall(part1)
        time.sleep(0.05)
        conn.sendall(part2)

    server = _LocalHttpServer(handler)
    try:
        resp = default_transport(_request(server.port, deadline=time.monotonic() + 3))
        assert resp.error_class == ""
        assert b"Chunked Title" in resp.body
        assert resp.body_truncated is True
    finally:
        server.close()


def test_c_early_head_close_stops_before_huge_body() -> None:
    huge_tail = b"y" * 400_000
    html = _html_with_og(tail=huge_tail)
    received_after_head = {"count": 0}

    def handler(conn: socket.socket) -> None:
        conn.recv(1024)
        head_only = (
            b"HTTP/1.1 200 OK\r\nContent-Type: text/html\r\nConnection: close\r\n\r\n"
            + html[: html.find(b"</head>") + len(b"</head>")]
        )
        conn.sendall(head_only)
        try:
            while conn.recv(8192):
                received_after_head["count"] += 1
        except OSError:
            pass

    server = _LocalHttpServer(handler)
    try:
        resp = default_transport(_request(server.port, deadline=time.monotonic() + 3))
        assert resp.error_class == ""
        assert b"og:title" in resp.body
        assert len(resp.body) < len(html)
        assert received_after_head["count"] == 0
    finally:
        server.close()


def test_d_large_html_without_metadata_stays_unavailable() -> None:
    from services.url_context import enrich_current_turn_urls, reset_url_context_cache  # noqa: PLC0415

    junk = b"<html><head></head><body>" + (b"j" * (MAX_RESPONSE_BYTES + 10_000)) + b"</body></html>"

    def handler(conn: socket.socket) -> None:
        conn.recv(1024)
        conn.sendall(
            b"HTTP/1.1 200 OK\r\nContent-Type: text/html\r\nConnection: close\r\n\r\n" + junk
        )

    server = _LocalHttpServer(handler)
    try:
        url = "http://example.test/"
        result = fetch_url(
            url,
            resolver=_resolver("8.8.8.8"),
            transport=_local_transport(server.port),
        )
        assert result.ok is True
        assert result.body_truncated is True
        reset_url_context_cache()

        async def _go():
            rows = await enrich_current_turn_urls(
                message=url,
                tenant_id=33,
                fetch=lambda _u: result,
            )
            return rows[0]

        ctx = asyncio.run(_go())
        assert ctx.extraction_status == "unavailable"
        assert ctx.error_class == "oversized"
    finally:
        server.close()


def test_g_oversized_json_still_fails_closed() -> None:
    body = b"{" + (b'"x":' + b'"' + b"a" * MAX_RESPONSE_BYTES + b'"') + b"}"
    result = fetch_url(
        "https://example.test/big.json",
        resolver=_resolver("8.8.8.8"),
        transport=lambda request: SafeFetchResponse(
            status=200,
            headers={"content-type": "application/json"},
            body=body,
        ),
    )
    assert result.ok is False
    assert result.error_class == "oversized"


def test_l_stored_body_never_exceeds_max_via_transport() -> None:
    html = _html_with_og(tail=b"z" * (MAX_RESPONSE_BYTES + 80_000))

    def handler(conn: socket.socket) -> None:
        conn.recv(1024)
        conn.sendall(
            b"HTTP/1.1 200 OK\r\nContent-Type: text/html\r\nConnection: close\r\n\r\n" + html
        )

    server = _LocalHttpServer(handler)
    try:
        resp = default_transport(_request(server.port, deadline=time.monotonic() + 3))
        assert resp.error_class == ""
        assert len(resp.body) <= MAX_RESPONSE_BYTES
    finally:
        server.close()


def test_n_content_length_larger_than_max_with_early_metadata_succeeds() -> None:
    html = _html_with_og(tail=b"q" * 500_000)

    def handler(conn: socket.socket) -> None:
        conn.recv(1024)
        conn.sendall(
            b"HTTP/1.1 200 OK\r\n"
            b"Content-Type: text/html\r\n"
            b"Content-Length: 9999999\r\n"
            b"Connection: close\r\n\r\n" + html
        )

    server = _LocalHttpServer(handler)
    try:
        result = fetch_url(
            "http://example.test/",
            resolver=_resolver("8.8.8.8"),
            transport=_local_transport(server.port),
        )
        assert result.ok is True
        assert b"og:title" in result.body
        assert len(result.body) < len(html)
    finally:
        server.close()


def _encode_chunked(chunks: list[bytes], trailers: bytes = b"") -> bytes:
    out = bytearray()
    for chunk in chunks:
        out.extend(f"{len(chunk):x}\r\n".encode("ascii"))
        out.extend(chunk)
        out.extend(b"\r\n")
    out.extend(b"0\r\n")
    if trailers:
        out.extend(trailers)
        if not trailers.endswith(b"\r\n"):
            out.extend(b"\r\n")
    out.extend(b"\r\n")
    return bytes(out)


def _split_chunks(data: bytes, piece: int = 65_536) -> list[bytes]:
    if not data:
        return []
    return [data[i : i + piece] for i in range(0, len(data), piece)]


def _chunked_response_headers(
    content_type: str = "text/html",
    *,
    extra: bytes = b"",
) -> bytes:
    return (
        b"HTTP/1.1 200 OK\r\n"
        + f"Content-Type: {content_type}\r\n".encode("ascii")
        + b"Transfer-Encoding: chunked\r\n"
        + extra
        + b"\r\n"
    )


def test_chunked_decoder_single_large_chunk_wire() -> None:
    from services.safe_http_fetch import _BodyAccumulator, _ChunkedDecoder  # noqa: PLC0415

    chunk = b"a" * 65_536
    wire = _encode_chunked([chunk])
    dec = _ChunkedDecoder()
    out1, err1 = dec.feed(wire)
    assert err1 == ""
    assert len(out1) == 65_536

    head = _html_with_og(tail=b"")
    tail = b"z" * (MAX_RESPONSE_BYTES + 80_000)
    acc = _BodyAccumulator(content_type="text/html", framing="chunked", content_length=0)
    err = acc.feed_wire(_encode_chunked([head] + _split_chunks(tail)))
    assert err == ""
    assert acc.stop_early is True
    assert b"og:title" in acc.body


def test_chunked_html_large_early_og_real_transport() -> None:
    head = _html_with_og(tail=b"")
    tail = b"z" * (MAX_RESPONSE_BYTES + 80_000)
    body = _encode_chunked([head] + _split_chunks(tail))

    def handler(conn: socket.socket) -> None:
        conn.recv(1024)
        conn.sendall(_chunked_response_headers() + body)

    server = _LocalHttpServer(handler)
    try:

        async def _go() -> SafeFetchResponse:
            return await async_default_transport(
                _request(server.port, deadline=time.monotonic() + 5)
            )

        resp = asyncio.run(_go())
        assert resp.error_class == ""
        assert b"og:title" in resp.body
        assert b"0\r\n" not in resp.body
        assert len(resp.body) <= MAX_RESPONSE_BYTES
        assert resp.body_truncated is True
    finally:
        server.close()


def test_chunked_og_split_across_reads_and_chunk_boundaries() -> None:
    part_a = b'<!doctype html><html><head><meta property="og:title" content="Split'
    part_b = b' Across Chunks">'
    part_c = b"</head><body>" + (b"x" * 120_000)
    wire = _encode_chunked([part_a, part_b, part_c])

    def handler(conn: socket.socket) -> None:
        conn.recv(1024)
        conn.sendall(_chunked_response_headers())
        cursor = 0
        while cursor < len(wire):
            end = min(len(wire), cursor + 17)
            conn.sendall(wire[cursor:end])
            cursor = end
            time.sleep(0.01)

    server = _LocalHttpServer(handler)
    try:
        resp = default_transport(_request(server.port, deadline=time.monotonic() + 5))
        assert resp.error_class == ""
        assert b"Split Across Chunks" in resp.body
        assert resp.body_truncated is True
    finally:
        server.close()


def test_chunked_terminal_zero_chunk_with_trailers() -> None:
    html = _html_with_og()
    wire = _encode_chunked([html], trailers=b"X-Test: ok\r\n")

    def handler(conn: socket.socket) -> None:
        conn.recv(1024)
        conn.sendall(_chunked_response_headers() + wire)

    server = _LocalHttpServer(handler)
    try:
        resp = default_transport(_request(server.port, deadline=time.monotonic() + 3))
        assert resp.error_class == ""
        assert b"OG Title" in resp.body
        assert b"X-Test" not in resp.body
    finally:
        server.close()


def test_chunked_malformed_chunk_size_fail_closed() -> None:
    wire = b"ZZ\r\npayload\r\n0\r\n\r\n"

    def handler(conn: socket.socket) -> None:
        conn.recv(1024)
        conn.sendall(_chunked_response_headers() + wire)

    server = _LocalHttpServer(handler)
    try:
        resp = default_transport(_request(server.port, deadline=time.monotonic() + 3))
        assert resp.error_class == "invalid_chunk_framing"
    finally:
        server.close()


def test_chunked_missing_chunk_crlf_fail_closed() -> None:
    wire = b"5\r\nhello0\r\n\r\n"

    def handler(conn: socket.socket) -> None:
        conn.recv(1024)
        conn.sendall(_chunked_response_headers() + wire)

    server = _LocalHttpServer(handler)
    try:
        resp = default_transport(_request(server.port, deadline=time.monotonic() + 3))
        assert resp.error_class == "invalid_chunk_framing"
    finally:
        server.close()


def test_chunked_premature_eof_inside_chunk_fail_closed() -> None:
    wire = b"a\r\n"

    def handler(conn: socket.socket) -> None:
        conn.recv(1024)
        conn.sendall(_chunked_response_headers() + wire)

    server = _LocalHttpServer(handler)
    try:
        resp = default_transport(_request(server.port, deadline=time.monotonic() + 3))
        assert resp.error_class == "invalid_chunk_framing"
    finally:
        server.close()


def test_chunked_extension_ignored_and_invalid_extension_size_rejected() -> None:
    ok_wire = _encode_chunked([b"<html><head></head><body>ok</body></html>"])
    bad_wire = b"g\r\nxxxxx\r\n0\r\n\r\n"

    def ok_handler(conn: socket.socket) -> None:
        conn.recv(1024)
        conn.sendall(_chunked_response_headers(content_type="text/html") + ok_wire)

    def bad_handler(conn: socket.socket) -> None:
        conn.recv(1024)
        conn.sendall(_chunked_response_headers(content_type="text/html") + bad_wire)

    ok_server = _LocalHttpServer(ok_handler)
    bad_server = _LocalHttpServer(bad_handler)
    try:
        ok_resp = default_transport(_request(ok_server.port, deadline=time.monotonic() + 3))
        bad_resp = default_transport(_request(bad_server.port, deadline=time.monotonic() + 3))
        assert ok_resp.error_class == ""
        assert bad_resp.error_class == "invalid_chunk_framing"
    finally:
        ok_server.close()
        bad_server.close()


def test_chunked_oversized_json_fail_closed_without_max_plus_one() -> None:
    big = b"x" * (MAX_RESPONSE_BYTES + 4096)
    wire = _encode_chunked([big[:120_000], big[120_000:240_000], big[240_000:]])

    def handler(conn: socket.socket) -> None:
        conn.recv(1024)
        conn.sendall(_chunked_response_headers(content_type="application/json") + wire)

    server = _LocalHttpServer(handler)
    try:
        result = fetch_url(
            "http://example.test/big.json",
            resolver=_resolver("8.8.8.8"),
            transport=_local_transport(server.port),
        )
        assert result.ok is False
        assert result.error_class == "oversized"
        assert len(result.body) <= MAX_RESPONSE_BYTES
    finally:
        server.close()


def test_framing_ambiguous_content_length_and_chunked_fail_closed() -> None:
    def handler(conn: socket.socket) -> None:
        conn.recv(1024)
        conn.sendall(
            b"HTTP/1.1 200 OK\r\n"
            b"Content-Type: text/html\r\n"
            b"Content-Length: 10\r\n"
            b"Transfer-Encoding: chunked\r\n\r\n"
            b"0\r\n\r\n"
        )

    server = _LocalHttpServer(handler)
    try:
        resp = default_transport(_request(server.port, deadline=time.monotonic() + 3))
        assert resp.error_class == "framing_ambiguous"
    finally:
        server.close()


def test_unsupported_transfer_encoding_fail_closed() -> None:
    def handler(conn: socket.socket) -> None:
        conn.recv(1024)
        conn.sendall(
            b"HTTP/1.1 200 OK\r\n"
            b"Content-Type: text/html\r\n"
            b"Transfer-Encoding: gzip\r\n\r\n"
            b"payload"
        )

    server = _LocalHttpServer(handler)
    try:
        resp = default_transport(_request(server.port, deadline=time.monotonic() + 3))
        assert resp.error_class == "framing_unsupported"
    finally:
        server.close()


def test_chunked_early_head_stops_before_huge_tail_and_closes_socket() -> None:
    head = _html_with_og(tail=b"")
    tail = b"t" * 400_000
    received_after_head = {"count": 0}

    def handler(conn: socket.socket) -> None:
        conn.recv(1024)
        conn.sendall(_chunked_response_headers() + _encode_chunked([head]))
        try:
            conn.sendall(_encode_chunked([tail]))
            while conn.recv(8192):
                received_after_head["count"] += 1
        except OSError:
            pass

    server = _LocalHttpServer(handler)
    try:
        resp = default_transport(_request(server.port, deadline=time.monotonic() + 5))
        assert resp.error_class == ""
        assert b"og:title" in resp.body
        assert len(resp.body) < len(head + tail)
        assert received_after_head["count"] == 0
    finally:
        server.close()


def test_fake_head_in_comment_and_script_does_not_short_circuit() -> None:
    html = (
        b"<!doctype html><html><head><!-- </head> -->"
        b"<script>var x='</head>';</script>"
        b'<meta property="og:title" content="Real OG">'
        b"</head><body>" + (b"u" * 120_000) + b"</body></html>"
    )
    wire = _encode_chunked([html[:200], html[200:400], html[400:]])

    def handler(conn: socket.socket) -> None:
        conn.recv(1024)
        conn.sendall(_chunked_response_headers() + wire)

    server = _LocalHttpServer(handler)
    try:
        resp = default_transport(_request(server.port, deadline=time.monotonic() + 5))
        assert resp.error_class == ""
        assert b"Real OG" in resp.body
    finally:
        server.close()


def test_exact_memory_cap_content_length_html() -> None:
    html = _html_with_og(tail=b"m" * (MAX_RESPONSE_BYTES + 50_000))

    def handler(conn: socket.socket) -> None:
        conn.recv(1024)
        conn.sendall(
            b"HTTP/1.1 200 OK\r\nContent-Type: text/html\r\n"
            b"Content-Length: 999999\r\n\r\n" + html
        )

    server = _LocalHttpServer(handler)
    try:
        resp = default_transport(_request(server.port, deadline=time.monotonic() + 5))
        assert resp.error_class == ""
        assert len(resp.body) <= MAX_RESPONSE_BYTES
    finally:
        server.close()


def test_exact_memory_cap_chunked_html() -> None:
    html = _html_with_og(tail=b"n" * (MAX_RESPONSE_BYTES + 50_000))
    wire = _encode_chunked(_split_chunks(html, 90_000))

    def handler(conn: socket.socket) -> None:
        conn.recv(1024)
        conn.sendall(_chunked_response_headers() + wire)

    server = _LocalHttpServer(handler)
    try:
        resp = default_transport(_request(server.port, deadline=time.monotonic() + 5))
        assert resp.error_class == ""
        assert len(resp.body) <= MAX_RESPONSE_BYTES
    finally:
        server.close()


def test_exact_memory_cap_connection_close_html() -> None:
    html = _html_with_og(tail=b"o" * (MAX_RESPONSE_BYTES + 50_000))

    def handler(conn: socket.socket) -> None:
        conn.recv(1024)
        conn.sendall(
            b"HTTP/1.1 200 OK\r\nContent-Type: text/html\r\nConnection: close\r\n\r\n" + html
        )

    server = _LocalHttpServer(handler)
    try:
        resp = default_transport(_request(server.port, deadline=time.monotonic() + 5))
        assert resp.error_class == ""
        assert len(resp.body) <= MAX_RESPONSE_BYTES
    finally:
        server.close()


def test_exact_memory_cap_json_non_html() -> None:
    payload = b"{" + b'"k":"' + (b"j" * (MAX_RESPONSE_BYTES + 2048)) + b'"}'

    def handler(conn: socket.socket) -> None:
        conn.recv(1024)
        conn.sendall(
            b"HTTP/1.1 200 OK\r\nContent-Type: application/json\r\nConnection: close\r\n\r\n"
            + payload
        )

    server = _LocalHttpServer(handler)
    try:
        result = fetch_url(
            "http://example.test/big.json",
            resolver=_resolver("8.8.8.8"),
            transport=_local_transport(server.port),
        )
        assert result.ok is False
        assert result.error_class == "oversized"
        assert len(result.body) <= MAX_RESPONSE_BYTES
    finally:
        server.close()


def test_chunked_enrich_current_turn_urls_real_transport() -> None:
    from services.url_context import enrich_current_turn_urls, reset_url_context_cache  # noqa: PLC0415
    from services.safe_http_fetch import fetch_url_async  # noqa: PLC0415

    head = _html_with_og(title="Chunked OG", tail=b"")
    wire = _encode_chunked([head] + _split_chunks(b"q" * (MAX_RESPONSE_BYTES + 40_000)))

    def handler(conn: socket.socket) -> None:
        conn.recv(1024)
        conn.sendall(_chunked_response_headers() + wire)

    server = _LocalHttpServer(handler)
    try:
        reset_url_context_cache()
        url = "http://example.test/chunked"

        async def _fetch(u: str):
            return await fetch_url_async(
                u,
                resolver=_resolver("8.8.8.8"),
                transport=_local_transport(server.port),
            )

        async def _go():
            rows = await enrich_current_turn_urls(
                message=url,
                tenant_id=33,
                fetch=_fetch,
            )
            return rows[0]

        ctx = asyncio.run(_go())
        assert ctx.extraction_status == "ok"
        assert ctx.page_title == "Chunked OG"
        assert ctx.fetch_body_truncated is True
    finally:
        server.close()

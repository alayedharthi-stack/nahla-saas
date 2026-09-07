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
        assert resp.error_class == "oversized"
        assert resp.body == b""
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

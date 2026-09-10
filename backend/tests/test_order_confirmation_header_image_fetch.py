"""SSRF-safe header image fetch for order_confirmation Meta submit."""

from __future__ import annotations



import asyncio

import socket

import sys

import threading

import time

from pathlib import Path

from typing import Callable

from unittest.mock import AsyncMock, MagicMock, patch



import pytest



REPO_ROOT = Path(__file__).resolve().parents[2]

for _p in (REPO_ROOT, REPO_ROOT / "backend", REPO_ROOT / "database"):

    if str(_p) not in sys.path:

        sys.path.insert(0, str(_p))



from core.commerce_lifecycle.order_confirmation_header_image_fetch import (  # noqa: E402

    HeaderImageFetchError,

    MAX_HEADER_IMAGE_BYTES,

    fetch_header_image_bytes_secure,

)

from services.safe_http_fetch import (  # noqa: E402

    SafeFetchRequest,

    SafeFetchResponse,

    fetch_https_image_bytes_async,

)





def _run(coro):

    return asyncio.run(coro)





def _resolver(ip: str):

    def _inner(hostname: str, port: int):

        return [(socket.AF_INET, ip)]



    return _inner





def _jpeg_response(body: bytes | None = None) -> SafeFetchResponse:

    payload = body if body is not None else (b"\xff\xd8\xff\xe0" + b"img")

    return SafeFetchResponse(

        status=200,

        headers={"content-type": "image/jpeg"},

        body=payload,

    )





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





def _local_transport(server_port: int):

    def _transport(request: SafeFetchRequest) -> SafeFetchResponse:

        from services.safe_http_fetch import default_transport  # noqa: PLC0415



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

        return default_transport(

            local_req,

            deadline=request.deadline or time.monotonic() + 8.0,

            max_body_bytes=MAX_HEADER_IMAGE_BYTES,

            reject_content_length_over_limit=True,

        )



    return _transport





class TestSecureHeaderImageFetch:

    def test_blocks_internal_https_url(self):

        with pytest.raises(HeaderImageFetchError) as exc:

            _run(fetch_header_image_bytes_secure("https://127.0.0.1/secret.jpg"))

        assert exc.value.error_code in {"host_blocked", "ip_blocked", "destination_blocked"}



    def test_blocks_railway_internal_host(self):

        with pytest.raises(HeaderImageFetchError) as exc:

            _run(

                fetch_header_image_bytes_secure(

                    "https://postgres-ancu.railway.internal/header.jpg"

                )

            )

        assert exc.value.error_code == "host_blocked"



    def test_blocks_redirect_to_internal(self):

        public = "https://cdn.example.test/a.jpg"



        def transport(request: SafeFetchRequest) -> SafeFetchResponse:

            if request.hostname == "cdn.example.test":

                return SafeFetchResponse(

                    status=302,

                    headers={"location": "http://169.254.169.254/latest/meta-data"},

                )

            raise AssertionError("private hop must not be fetched")



        with pytest.raises(HeaderImageFetchError) as exc:

            _run(

                fetch_header_image_bytes_secure(

                    public,

                    transport=transport,

                    resolver=_resolver("8.8.8.8"),

                )

            )

        assert exc.value.error_code in {"ip_blocked", "scheme_blocked", "host_blocked"}



    def test_blocks_content_length_over_limit_before_body_read(self):

        huge_len = MAX_HEADER_IMAGE_BYTES + 1



        def handler(conn: socket.socket) -> None:

            conn.recv(1024)

            conn.sendall(

                (

                    "HTTP/1.1 200 OK\r\n"

                    f"Content-Type: image/jpeg\r\n"

                    f"Content-Length: {huge_len}\r\n"

                    "Connection: close\r\n\r\n"

                ).encode("ascii")

            )



        server = _LocalHttpServer(handler)

        try:

            with pytest.raises(HeaderImageFetchError) as exc:

                _run(

                    fetch_header_image_bytes_secure(

                        "https://example.test/huge.jpg",

                        transport=_local_transport(server.port),

                        resolver=_resolver("8.8.8.8"),

                    )

                )

            assert exc.value.error_code == "body_too_large"

        finally:

            server.close()



    def test_blocks_streaming_body_over_limit(self):

        jpeg_prefix = b"\xff\xd8\xff"

        chunk = b"x" * 65536



        def handler(conn: socket.socket) -> None:

            conn.recv(1024)

            conn.sendall(

                b"HTTP/1.1 200 OK\r\n"

                b"Content-Type: image/jpeg\r\n"

                b"Transfer-Encoding: chunked\r\n"

                b"Connection: close\r\n\r\n"

            )

            sent = 0

            target = MAX_HEADER_IMAGE_BYTES + len(jpeg_prefix) + 1

            while sent < target:

                piece = jpeg_prefix if sent == 0 else chunk

                piece = piece[: min(len(piece), target - sent)]

                wire = f"{len(piece):x}\r\n".encode("ascii") + piece + b"\r\n"

                conn.sendall(wire)

                sent += len(piece)

            conn.sendall(b"0\r\n\r\n")



        server = _LocalHttpServer(handler)

        try:

            with pytest.raises(HeaderImageFetchError) as exc:

                _run(

                    fetch_header_image_bytes_secure(

                        "https://example.test/stream-huge.jpg",

                        transport=_local_transport(server.port),

                        resolver=_resolver("8.8.8.8"),

                    )

                )

            assert exc.value.error_code == "body_too_large"

        finally:

            server.close()



    def test_blocks_html_response(self):

        def transport(_request: SafeFetchRequest) -> SafeFetchResponse:

            return SafeFetchResponse(

                status=200,

                headers={"content-type": "text/html"},

                body=b"<html><body>nope</body></html>",

            )



        with pytest.raises(HeaderImageFetchError) as exc:

            _run(

                fetch_header_image_bytes_secure(

                    "https://example.test/fake.jpg",

                    transport=transport,

                    resolver=_resolver("8.8.8.8"),

                )

            )

        assert exc.value.error_code == "html_response_blocked"



    def test_accepts_valid_jpeg(self):

        jpeg = b"\xff\xd8\xff\xe0" + b"img"



        def transport(request: SafeFetchRequest) -> SafeFetchResponse:

            assert request.ip == "8.8.8.8"

            return _jpeg_response(jpeg)



        body, mime = _run(

            fetch_header_image_bytes_secure(

                "https://example.test/ok.jpg",

                transport=transport,

                resolver=_resolver("8.8.8.8"),

            )

        )

        assert mime == "image/jpeg"

        assert body == jpeg



    def test_transport_connects_to_validated_ip_not_hostname(self):

        seen_ips: list[str] = []



        def transport(request: SafeFetchRequest) -> SafeFetchResponse:

            seen_ips.append(request.ip)

            assert request.hostname == "example.test"

            return _jpeg_response()



        _run(

            fetch_https_image_bytes_async(

                "https://example.test/pinned.jpg",

                transport=transport,

                resolver=_resolver("8.8.8.8"),

            )

        )

        assert seen_ips == ["8.8.8.8"]



    def test_dns_rebinding_does_not_change_connect_ip(self):

        expected_ip = "8.8.8.8"

        dns_calls = 0

        connect_args: list[tuple] = []



        def changing_dns(hostname, port, *args, **kwargs):

            nonlocal dns_calls

            dns_calls += 1

            ip = expected_ip if dns_calls == 1 else "198.51.100.99"

            return [(socket.AF_INET, socket.SOCK_STREAM, 6, "", (ip, port))]



        async def tracking_transport(request: SafeFetchRequest) -> SafeFetchResponse:

            connect_args.append((request.ip, request.port, request.hostname))

            return _jpeg_response()



        with patch("socket.getaddrinfo", side_effect=changing_dns):

            _run(

                fetch_https_image_bytes_async(

                    "https://example.test/rebind.jpg",

                    transport=tracking_transport,

                )

            )



        assert dns_calls == 1

        assert connect_args == [(expected_ip, 443, "example.test")]



    def test_default_transport_uses_pinned_ip_not_hostname(self):

        expected_ip = "8.8.8.8"

        dns_calls = 0

        reader = AsyncMock()

        reader.read = AsyncMock(return_value=b"")

        writer = AsyncMock()

        writer.write = MagicMock()

        writer.drain = AsyncMock()

        open_connection = AsyncMock(return_value=(reader, writer))



        def changing_dns(hostname, port, *args, **kwargs):

            nonlocal dns_calls

            dns_calls += 1

            ip = expected_ip if dns_calls == 1 else "198.51.100.99"

            return [(socket.AF_INET, socket.SOCK_STREAM, 6, "", (ip, port))]



        async def _go() -> None:

            with patch("socket.getaddrinfo", side_effect=changing_dns), patch(

                "asyncio.open_connection", open_connection

            ):

                result = await fetch_https_image_bytes_async("https://example.test/tls.jpg")

            open_connection.assert_awaited()

            call_kwargs = open_connection.await_args.kwargs

            assert call_kwargs.get("host") == expected_ip

            assert call_kwargs.get("server_hostname") == "example.test"

            assert dns_calls == 1

            assert result.ok is False



        _run(_go())



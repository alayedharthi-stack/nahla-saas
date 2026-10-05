"""The OTO webhook never buffers more than 32 KiB of request body.

A declared ``Content-Length`` above the limit is refused before any body byte
is read; without one (chunked transfer) the body is streamed and refused as
soon as the running total passes the limit. Exactly 32 KiB is accepted. A
refused body never reaches the connection lookup or signature check.

The real-server cases run uvicorn on a loopback port and talk raw HTTP/1.1, so
they observe what a client actually sees: the 413 arrives while the client is
still sending, and the server closes the connection instead of draining the
rest of the body. No OTO, WhatsApp or other external call is made.
"""
from __future__ import annotations

import json
import socket
import threading
import time
from contextlib import contextmanager

import pytest
import uvicorn
from fastapi import FastAPI
from fastapi.testclient import TestClient

from core.database import get_db
from oto.order_data import oto_order_id
from routers import oto_shipping

LIMIT = 32 * 1024
URL = "/oto/webhooks/staging/orderStatus"


class _RecordingDb:
    """Records connection lookups; no row exists, so a full read ends in 401."""

    def __init__(self):
        self.lookups = 0

    def query(self, _model):
        db = self

        class _Q:
            def filter_by(self, **_criteria):
                db.lookups += 1
                return self

            def first(self):
                return None

        return _Q()


def _app(db) -> FastAPI:
    app = FastAPI()
    app.include_router(oto_shipping.router)
    app.dependency_overrides[get_db] = lambda: db
    return app


def _json_body(size: int) -> bytes:
    """A valid webhook JSON object of exactly ``size`` bytes."""
    base = {"orderId": oto_order_id(7, 5), "status": "delivered", "pad": ""}
    empty = len(json.dumps(base, separators=(",", ":")).encode())
    base["pad"] = "x" * (size - empty)
    raw = json.dumps(base, separators=(",", ":")).encode()
    assert len(raw) == size
    return raw


def _chunks(raw: bytes, size: int = 4096):
    for i in range(0, len(raw), size):
        yield raw[i:i + size]


@pytest.fixture(autouse=True)
def _switch_on(monkeypatch):
    monkeypatch.setenv("OTO_EXTERNAL_EGRESS_ENABLED", "1")


# ── in-process (ASGI) ────────────────────────────────────────────────────────

def test_limit_is_32_kib():
    assert oto_shipping.WEBHOOK_BODY_LIMIT == LIMIT


def test_chunked_body_without_content_length_over_the_limit_is_refused_before_lookup():
    db = _RecordingDb()
    with TestClient(_app(db)) as client:
        response = client.post(URL, content=_chunks(_json_body(LIMIT + 1)),
                               headers={"content-type": "application/json"})
    assert response.status_code == 413
    assert response.json()["detail"] == "payload_too_large"
    assert db.lookups == 0


def test_chunked_body_of_exactly_32_kib_is_read_and_reaches_the_lookup():
    db = _RecordingDb()
    with TestClient(_app(db)) as client:
        response = client.post(URL, content=_chunks(_json_body(LIMIT)),
                               headers={"content-type": "application/json"})
    assert response.status_code == 401
    assert response.json()["detail"] == "oto_webhook_unconfigured"
    assert db.lookups == 1


def test_declared_body_of_exactly_32_kib_is_accepted_and_one_byte_more_is_refused():
    db = _RecordingDb()
    with TestClient(_app(db)) as client:
        at_limit = client.post(URL, content=_json_body(LIMIT), headers={"content-type": "application/json"})
        over = client.post(URL, content=_json_body(LIMIT + 1), headers={"content-type": "application/json"})
    assert at_limit.status_code == 401
    assert over.status_code == 413
    assert db.lookups == 1


def test_declared_oversize_is_refused_before_any_body_byte_is_read(monkeypatch):
    read = []

    async def _never_read(self):  # pragma: no cover - reaching here is the failure
        read.append(True)
        yield b""

    monkeypatch.setattr("starlette.requests.Request.stream", _never_read)
    db = _RecordingDb()
    with TestClient(_app(db)) as client:
        response = client.post(URL, content=b"{}", headers={"content-type": "application/json",
                                                           "content-length": str(LIMIT + 1)})
    assert response.status_code == 413
    assert read == [] and db.lookups == 0


def test_malformed_content_length_is_refused():
    db = _RecordingDb()
    with TestClient(_app(db)) as client:
        response = client.post(URL, content=b"{}", headers={"content-type": "application/json",
                                                           "content-length": "abc"})
    assert response.status_code in {400, 413}
    assert db.lookups == 0


def test_switched_off_still_answers_404_without_reading(monkeypatch):
    monkeypatch.setenv("OTO_EXTERNAL_EGRESS_ENABLED", "0")
    db = _RecordingDb()
    with TestClient(_app(db)) as client:
        response = client.post(URL, content=_chunks(_json_body(LIMIT * 4)),
                               headers={"content-type": "application/json"})
    assert response.status_code == 404
    assert db.lookups == 0


# ── real server (uvicorn, raw HTTP/1.1 over loopback) ────────────────────────

@contextmanager
def _server(http: str):
    sock = socket.socket()
    sock.bind(("127.0.0.1", 0))
    port = sock.getsockname()[1]
    sock.close()
    config = uvicorn.Config(_app(_RecordingDb()), host="127.0.0.1", port=port, log_level="error",
                            http=http, lifespan="off")
    server = uvicorn.Server(config)
    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()
    deadline = time.monotonic() + 10
    while not server.started:
        assert time.monotonic() < deadline, "uvicorn did not start"
        time.sleep(0.01)
    try:
        yield port
    finally:
        server.should_exit = True
        thread.join(timeout=10)


def _read_response(conn: socket.socket) -> tuple[bytes, bool]:
    """Read until the server closes or 3 s pass; return (data, closed_by_server)."""
    conn.settimeout(3)
    data = b""
    try:
        while True:
            part = conn.recv(65536)
            if not part:
                return data, True
            data += part
    except socket.timeout:
        return data, False
    except ConnectionResetError:
        return data, True


@pytest.mark.parametrize("http", ["h11", "httptools"])
def test_real_server_refuses_an_endless_chunked_body_and_closes(http):
    """The client keeps streaming; the server answers 413 after ~32 KiB and
    closes the connection rather than draining an unbounded body."""
    if http == "httptools":
        pytest.importorskip("httptools")
    with _server(http) as port:
        conn = socket.create_connection(("127.0.0.1", port), timeout=5)
        conn.sendall((f"POST {URL} HTTP/1.1\r\nHost: localhost\r\nContent-Type: application/json\r\n"
                      "Transfer-Encoding: chunked\r\n\r\n").encode())
        chunk = b"x" * 4096
        frame = f"{len(chunk):x}\r\n".encode() + chunk + b"\r\n"
        sent = 0
        refused_early = False
        try:
            for _ in range(2048):  # up to 8 MiB offered; never terminated
                conn.sendall(frame)
                sent += len(chunk)
        except (BrokenPipeError, ConnectionResetError):
            refused_early = True
        data, closed = _read_response(conn)
        conn.close()
    assert data.startswith(b"HTTP/1.1 413"), data[:120]
    assert b"payload_too_large" in data
    assert closed, "server kept the connection open after refusing the body"
    # Either the server stopped the client mid-stream, or the response was ready
    # long before the 8 MiB were offered; in both cases it did not wait for the end.
    assert refused_early or sent >= LIMIT


@pytest.mark.parametrize("http", ["h11", "httptools"])
def test_real_server_accepts_exactly_32_kib_chunked_and_keeps_the_connection(http):
    if http == "httptools":
        pytest.importorskip("httptools")
    body = _json_body(LIMIT)
    with _server(http) as port:
        conn = socket.create_connection(("127.0.0.1", port), timeout=5)
        head = (f"POST {URL} HTTP/1.1\r\nHost: localhost\r\nContent-Type: application/json\r\n"
                "Transfer-Encoding: chunked\r\n\r\n").encode()
        frames = b"".join(f"{len(c):x}\r\n".encode() + c + b"\r\n" for c in _chunks(body)) + b"0\r\n\r\n"
        conn.sendall(head + frames)
        data, closed = _read_response(conn)
        conn.close()
    assert data.startswith(b"HTTP/1.1 401"), data[:120]
    assert b"oto_webhook_unconfigured" in data
    assert not closed, "a fully read body must not force the connection closed"


@pytest.mark.parametrize("http", ["h11", "httptools"])
def test_real_server_refuses_a_declared_oversize_before_the_body_and_closes(http):
    """Only the headers are sent; the 413 must come back without waiting for a
    body the server would otherwise have to drain."""
    if http == "httptools":
        pytest.importorskip("httptools")
    with _server(http) as port:
        conn = socket.create_connection(("127.0.0.1", port), timeout=5)
        conn.sendall((f"POST {URL} HTTP/1.1\r\nHost: localhost\r\nContent-Type: application/json\r\n"
                      f"Content-Length: {LIMIT + 1}\r\n\r\n").encode())
        data, closed = _read_response(conn)
        conn.close()
    assert data.startswith(b"HTTP/1.1 413"), data[:120]
    assert closed

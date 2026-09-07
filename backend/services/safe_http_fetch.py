"""SSRF-safe HTTP GET for URL metadata enrichment.

This is the D17 security boundary. Do not reuse ``expand_maps_url``.
Never logs full query strings. Never sends cookies or credentials.
Production fetch is asyncio-native: DNS, connect, TLS, and recv all
honor one end-to-end deadline. Sync ``fetch_url`` exists for tests
outside a running loop.
"""
from __future__ import annotations

import asyncio
import atexit
import ipaddress
import logging
import re
import socket
import ssl
import time
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from typing import (
    Any,
    Callable,
    Dict,
    FrozenSet,
    Iterable,
    List,
    Optional,
    Sequence,
    Tuple,
)
from urllib.parse import urljoin, urlparse, urlunparse

logger = logging.getLogger("nahla.url_context.safe_http")

ALLOWED_SCHEMES: FrozenSet[str] = frozenset({"http", "https"})
MAX_REDIRECTS = 3
MAX_RESPONSE_BYTES = 262_144  # 256 KiB; body never exceeds this by one byte
MAX_HEADER_BYTES = 32_768
CONNECT_TIMEOUT_S = 3.0
TOTAL_TIMEOUT_S = 8.0
USER_AGENT = "NahlaURLContext/1.0"
FETCH_CONCURRENCY = 8
ALLOWED_CONTENT_TYPES: FrozenSet[str] = frozenset(
    {
        "text/html",
        "application/xhtml+xml",
        "application/json",
    }
)
HTML_CONTENT_TYPES: FrozenSet[str] = frozenset(
    {
        "text/html",
        "application/xhtml+xml",
    }
)
_WIRE_RECV_BYTES = 8192
MAX_CHUNK_SIZE_LINE_BYTES = 4096
MAX_TRAILER_BYTES = 8192
CLOUD_METADATA_HOSTS: FrozenSet[str] = frozenset(
    {
        "metadata.google.internal",
        "metadata.google.com",
        "metadata.internal",
        "instance-data",
    }
)
CLOUD_METADATA_IPS: FrozenSet[str] = frozenset(
    {
        "169.254.169.254",
        "fd00:ec2::254",
    }
)

_DECIMAL_HOST_RE = re.compile(r"^\d+$")
_HEX_HOST_RE = re.compile(r"0x", re.IGNORECASE)
_DOTTED_NUMERIC_RE = re.compile(r"^[\d.]+$")

Resolver = Callable[[str, int], Sequence[Tuple[socket.AddressFamily, str]]]
Transport = Callable[["SafeFetchRequest"], "SafeFetchResponse"]
AsyncTransport = Callable[["SafeFetchRequest"], Any]

_FETCH_SEMA_HOLDER: List[Any] = []
_DNS_SEMA_HOLDER: List[Any] = []
_IO_EXECUTOR: Optional[ThreadPoolExecutor] = None


@dataclass(frozen=True)
class SafeFetchRequest:
    url: str
    hostname: str
    ip: str
    port: int
    scheme: str
    path: str
    timeout_connect: float
    timeout_read: float
    deadline: float = 0.0


@dataclass
class SafeFetchResponse:
    status: int
    headers: Dict[str, str] = field(default_factory=dict)
    body: bytes = b""
    error_class: str = ""
    body_truncated: bool = False


@dataclass
class SafeHttpResult:
    ok: bool
    url: str = ""
    final_url: str = ""
    status: int = 0
    content_type: str = ""
    body: bytes = b""
    error_class: str = ""
    hops: int = 0
    body_truncated: bool = False


def redact_url_for_log(url: str) -> str:
    """Host + path only. Query and fragment are never logged."""
    raw = str(url or "").strip()
    if not raw:
        return ""
    try:
        parsed = urlparse(raw)
    except Exception:
        return "<unparseable>"
    host = parsed.hostname or ""
    path = parsed.path or "/"
    return f"{parsed.scheme}://{host}{path}"


def remaining_deadline(deadline: float, *, now: Optional[float] = None) -> float:
    left = float(deadline) - (now if now is not None else time.monotonic())
    return left


def _require_remaining(deadline: float) -> float:
    left = remaining_deadline(deadline)
    if left <= 0:
        raise TimeoutError("url_context deadline exceeded")
    return left


def _idna_hostname(host: str) -> str:
    raw = str(host or "").strip().rstrip(".").lower()
    if not raw:
        return ""
    try:
        return raw.encode("idna").decode("ascii")
    except Exception:
        return raw


def _normalize_content_type(value: str) -> str:
    return str(value or "").split(";", 1)[0].strip().lower()


def _is_blocked_hostname(host: str) -> bool:
    name = _idna_hostname(host)
    if not name:
        return True
    if name in CLOUD_METADATA_HOSTS:
        return True
    if name == "localhost" or name.endswith(".localhost") or name.endswith(".local"):
        return True
    if name.endswith(".internal") or name.endswith(".corp"):
        return True
    return False


def hostname_is_ip_trick(host: str) -> bool:
    """Reject decimal/octal/hex/short/mapped IP forms before DNS."""
    name = _idna_hostname(host)
    if not name:
        return True
    raw = name.strip("[]")
    try:
        ip = ipaddress.ip_address(raw)
        if isinstance(ip, ipaddress.IPv6Address):
            if ip.ipv4_mapped is not None or ip.sixtofour is not None:
                return True
            try:
                if ip.teredo is not None:
                    return True
            except Exception:  # noqa: silent-ok — optional Teredo unwrap
                return True
        return False
    except ValueError:
        pass
    if _DECIMAL_HOST_RE.match(name):
        return True
    if _HEX_HOST_RE.search(name):
        return True
    if _DOTTED_NUMERIC_RE.match(name):
        parts = name.split(".")
        if not parts or len(parts) > 4:
            return True
        if not all(p.isdigit() for p in parts):
            return True
        if len(parts) != 4:
            return True
        if any(len(p) > 1 and p.startswith("0") for p in parts):
            return True
        if any(int(p) > 255 for p in parts):
            return True
    return False


def canonicalize_ip(ip: ipaddress.IPv4Address | ipaddress.IPv6Address) -> ipaddress.IPv4Address | ipaddress.IPv6Address:
    if isinstance(ip, ipaddress.IPv6Address):
        if ip.ipv4_mapped is not None:
            return ipaddress.ip_address(ip.ipv4_mapped)
        if ip.sixtofour is not None:
            return ipaddress.ip_address(ip.sixtofour)
        try:
            if ip.teredo is not None:
                return ipaddress.ip_address(ip.teredo[1])
        except Exception:  # noqa: silent-ok — optional Teredo unwrap; original IP is still blocked below
            pass
    return ip


def is_blocked_ip(ip_text: str) -> bool:
    try:
        ip = canonicalize_ip(ipaddress.ip_address(str(ip_text).strip()))
    except Exception:
        return True
    if str(ip) in CLOUD_METADATA_IPS:
        return True
    if ip.is_private or ip.is_loopback or ip.is_link_local:
        return True
    if ip.is_multicast or ip.is_reserved or ip.is_unspecified:
        return True
    if getattr(ip, "is_site_local", False):
        return True
    return False


def default_resolver(hostname: str, port: int) -> List[Tuple[socket.AddressFamily, str]]:
    infos = socket.getaddrinfo(hostname, port, type=socket.SOCK_STREAM)
    out: List[Tuple[socket.AddressFamily, str]] = []
    seen = set()
    for family, _type, _proto, _canon, sockaddr in infos:
        ip = sockaddr[0]
        key = (family, ip)
        if key in seen:
            continue
        seen.add(key)
        out.append((family, ip))
    return out


def _scheme_port(scheme: str, explicit: Optional[int]) -> Tuple[Optional[int], str]:
    if scheme == "http":
        if explicit is None:
            return 80, ""
        if explicit != 80:
            return None, "invalid_port"
        return 80, ""
    if scheme == "https":
        if explicit is None:
            return 443, ""
        if explicit != 443:
            return None, "invalid_port"
        return 443, ""
    return None, "scheme_blocked"


def _parse_and_validate_url(url: str) -> Tuple[Optional[Any], str]:
    raw = str(url or "").strip()
    if not raw or len(raw) > 2048:
        return None, "invalid_url"
    if "\r" in raw or "\n" in raw:
        return None, "invalid_url"
    if raw.startswith("//"):
        raw = "https:" + raw
    try:
        parsed = urlparse(raw)
    except Exception:
        return None, "invalid_url"
    scheme = str(parsed.scheme or "").lower()
    if scheme not in ALLOWED_SCHEMES:
        return None, "scheme_blocked"
    if parsed.username or parsed.password:
        return None, "credentials_blocked"
    host = _idna_hostname(parsed.hostname or "")
    if not host:
        return None, "invalid_host"
    if _is_blocked_hostname(host):
        return None, "host_blocked"
    if hostname_is_ip_trick(host):
        return None, "ip_blocked"
    try:
        literal = ipaddress.ip_address(host.strip("[]"))
    except ValueError:
        literal = None
    if literal is not None and is_blocked_ip(str(literal)):
        return None, "ip_blocked"
    _port, port_err = _scheme_port(scheme, parsed.port)
    if port_err:
        return None, port_err
    return parsed, ""


def validate_destination(
    url: str,
    *,
    resolver: Resolver = default_resolver,
    deadline: Optional[float] = None,
) -> Tuple[Optional[SafeFetchRequest], str]:
    parsed, err = _parse_and_validate_url(url)
    if parsed is None:
        return None, err
    if deadline is not None:
        try:
            _require_remaining(deadline)
        except TimeoutError:
            return None, "timeout"
    host = _idna_hostname(parsed.hostname or "")
    scheme = parsed.scheme.lower()
    port, port_err = _scheme_port(scheme, parsed.port)
    if port_err or port is None:
        return None, port_err or "invalid_port"
    try:
        records = list(resolver(host, port) or [])
    except Exception:
        return None, "dns_error"
    if not records:
        return None, "dns_error"
    ips = [ip for _family, ip in records]
    if any(is_blocked_ip(ip) for ip in ips):
        return None, "ip_blocked"
    chosen_ip = ips[0]
    path = parsed.path or "/"
    if parsed.query:
        path = f"{path}?{parsed.query}"
    if "\r" in path or "\n" in path or "\r" in host or "\n" in host:
        return None, "invalid_url"
    req = SafeFetchRequest(
        url=urlunparse(
            (
                scheme,
                parsed.netloc.split("@")[-1],
                parsed.path or "/",
                "",
                parsed.query,
                "",
            )
        ),
        hostname=host,
        ip=chosen_ip,
        port=int(port),
        scheme=scheme,
        path=path,
        timeout_connect=CONNECT_TIMEOUT_S,
        timeout_read=TOTAL_TIMEOUT_S,
        deadline=float(deadline or 0.0),
    )
    return req, ""


def _header_map(raw_headers: Iterable[Tuple[str, str]]) -> Dict[str, str]:
    out: Dict[str, str] = {}
    for key, value in raw_headers:
        out[str(key or "").lower()] = str(value or "")
    return out


def _split_http_message(raw: bytes) -> Tuple[Dict[str, str], int, bytes, str]:
    header_blob, sep, body = raw.partition(b"\r\n\r\n")
    if not sep:
        return {}, 0, b"", "invalid_response"
    headers, status, err = _parse_status_headers(header_blob)
    if err:
        return {}, 0, b"", err
    return headers, status, body, ""


def _parse_status_headers(header_blob: bytes) -> Tuple[Dict[str, str], int, str]:
    lines = header_blob.split(b"\r\n")
    if not lines:
        return {}, 0, "invalid_response"
    status_line = lines[0].decode("latin-1", "replace")
    parts = status_line.split(" ", 2)
    status = int(parts[1]) if len(parts) > 1 and parts[1].isdigit() else 0
    headers: Dict[str, str] = {}
    for line in lines[1:]:
        if b":" not in line:
            continue
        name, value = line.split(b":", 1)
        headers[name.decode("latin-1", "replace").strip().lower()] = (
            value.decode("latin-1", "replace").strip()
        )
    return headers, status, ""


def _is_html_content_type(content_type: str) -> bool:
    return _normalize_content_type(content_type) in HTML_CONTENT_TYPES


def _html_head_complete(body: bytes) -> bool:
    """True when </head> closes the head outside comments and script/style blocks."""
    i = 0
    n = len(body)
    state = "normal"
    while i < n:
        if state == "normal":
            if i + 4 <= n and body[i : i + 4] == b"<!--":
                state = "comment"
                i += 4
                continue
            if i + 7 <= n and body[i : i + 7].lower() == b"<script":
                state = "script"
                i += 7
                continue
            if i + 6 <= n and body[i : i + 6].lower() == b"<style":
                state = "style"
                i += 6
                continue
            if i + 6 <= n and body[i : i + 6].lower() == b"</head":
                j = i + 6
                while j < n and body[j : j + 1] in (b" ", b"\t", b"\n", b"\r"):
                    j += 1
                if j < n and body[j : j + 1] == b">":
                    return True
            i += 1
            continue
        if state == "comment":
            end = body.find(b"-->", i)
            if end < 0:
                return False
            i = end + 3
            state = "normal"
            continue
        if state == "script":
            end = body.lower().find(b"</script>", i)
            if end < 0:
                return False
            i = end + len(b"</script>")
            state = "normal"
            continue
        end = body.lower().find(b"</style>", i)
        if end < 0:
            return False
        i = end + len(b"</style>")
        state = "normal"
    return False


def _append_bounded(body: bytes, piece: bytes, limit: int) -> Tuple[bytes, bool]:
    if not piece:
        return body, False
    room = limit - len(body)
    if room <= 0:
        return body[:limit], True
    if len(piece) <= room:
        return body + piece, False
    return body + piece[:room], True


def _resolve_body_framing(headers: Dict[str, str]) -> Tuple[str, int, str]:
    """Return (mode, content_length, error_class)."""
    cl_raw = str(headers.get("content-length") or "").strip()
    te_raw = str(headers.get("transfer-encoding") or "").strip()
    has_cl = bool(cl_raw)
    has_te = bool(te_raw)
    if has_cl and has_te:
        return "", 0, "framing_ambiguous"
    if has_te:
        parts = [part.strip().lower() for part in te_raw.split(",") if part.strip()]
        if not parts:
            return "", 0, "framing_unsupported"
        if len(parts) == 1 and parts[0] == "identity":
            has_te = False
        elif len(parts) == 1 and parts[0] == "chunked":
            return "chunked", 0, ""
        else:
            return "", 0, "framing_unsupported"
    if has_cl:
        try:
            content_length = int(cl_raw, 10)
        except ValueError:
            return "", 0, "invalid_response"
        if content_length < 0:
            return "", 0, "invalid_response"
        return "content_length", content_length, ""
    return "connection_close", 0, ""


class _ChunkedDecoder:
    """RFC 7230 chunked decoder. Semicolon chunk extensions are ignored."""

    def __init__(self) -> None:
        self._pending = b""
        self._phase = "size"
        self._chunk_left = 0
        self._trailers = b""
        self.complete = False
        self.error = ""

    def _fail(self, err: str) -> str:
        self.error = err
        self.complete = True
        return err

    def feed(self, wire: bytes) -> Tuple[bytes, str]:
        if self.complete:
            return b"", self.error
        if wire:
            self._pending += wire
        out = bytearray()
        while not self.complete and not self.error:
            if self._phase == "size":
                idx = self._pending.find(b"\r\n")
                if idx < 0:
                    if len(self._pending) > MAX_CHUNK_SIZE_LINE_BYTES:
                        return bytes(out), self._fail("invalid_chunk_framing")
                    break
                if idx > MAX_CHUNK_SIZE_LINE_BYTES:
                    return bytes(out), self._fail("invalid_chunk_framing")
                line = self._pending[:idx]
                self._pending = self._pending[idx + 2 :]
                semi = line.find(b";")
                size_part = (line[:semi] if semi >= 0 else line).strip()
                if not size_part:
                    return bytes(out), self._fail("invalid_chunk_framing")
                try:
                    chunk_size = int(size_part.decode("ascii"), 16)
                except (ValueError, UnicodeDecodeError):
                    return bytes(out), self._fail("invalid_chunk_framing")
                if chunk_size < 0 or chunk_size > (MAX_RESPONSE_BYTES * 4):
                    return bytes(out), self._fail("invalid_chunk_framing")
                if chunk_size == 0:
                    self._phase = "trailers"
                    continue
                self._chunk_left = chunk_size
                self._phase = "data"
                continue
            if self._phase == "data":
                if not self._pending:
                    break
                take = min(self._chunk_left, len(self._pending))
                out.extend(self._pending[:take])
                self._pending = self._pending[take:]
                self._chunk_left -= take
                if self._chunk_left:
                    break
                self._phase = "crlf"
                continue
            if self._phase == "crlf":
                if len(self._pending) < 2:
                    break
                if self._pending[:2] != b"\r\n":
                    return bytes(out), self._fail("invalid_chunk_framing")
                self._pending = self._pending[2:]
                self._phase = "size"
                if out:
                    return bytes(out), self.error
                continue
            if len(self._trailers) + len(self._pending) > MAX_TRAILER_BYTES:
                return bytes(out), self._fail("invalid_chunk_framing")
            self._trailers += self._pending
            self._pending = b""
            if b"\r\n\r\n" in self._trailers:
                self.complete = True
            break
        return bytes(out), self.error

    def eof(self) -> str:
        if self.complete:
            return ""
        return self._fail("invalid_chunk_framing")


class _BodyAccumulator:
    def __init__(self, *, content_type: str, framing: str, content_length: int) -> None:
        self.is_html = _is_html_content_type(content_type)
        self.framing = framing
        self.cl_remaining = content_length
        self.body = b""
        self.truncated = False
        self.oversized = False
        self.done = False
        self.stop_early = False
        self.chunked = _ChunkedDecoder() if framing == "chunked" else None

    def _mark_html_stop(self) -> None:
        self.truncated = True
        self.stop_early = True

    def add_decoded(self, piece: bytes) -> str:
        if not piece or self.done or self.stop_early:
            return ""
        self.body, hit_limit = _append_bounded(self.body, piece, MAX_RESPONSE_BYTES)
        if self.is_html:
            if len(self.body) >= MAX_RESPONSE_BYTES:
                self._mark_html_stop()
            elif b"<" in piece or b"<" in self.body:
                if _html_head_complete(self.body):
                    self._mark_html_stop()
            return ""
        if hit_limit:
            self.oversized = True
            self.done = True
            return "oversized"
        return ""

    def feed_wire(self, wire: bytes) -> str:
        if self.done or self.stop_early or not wire:
            return ""
        if self.framing == "chunked":
            assert self.chunked is not None
            pending = wire
            while pending or self.chunked._pending:
                if self.stop_early or self.oversized:
                    return ""
                decoded, err = self.chunked.feed(pending)
                pending = b""
                if err:
                    self.done = True
                    return err
                if decoded:
                    err = self.add_decoded(decoded)
                    if err:
                        return err
                    if self.stop_early or self.oversized:
                        return ""
                if self.chunked.complete:
                    self.done = True
                    return ""
                if not decoded:
                    break
            return ""
        if self.framing == "content_length":
            if self.cl_remaining <= 0:
                self.done = True
                return ""
            take = min(len(wire), self.cl_remaining)
            err = self.add_decoded(wire[:take])
            self.cl_remaining -= take
            if self.cl_remaining <= 0:
                self.done = True
            return err
        return self.add_decoded(wire)

    def eof(self) -> str:
        if self.framing == "chunked":
            assert self.chunked is not None
            if self.stop_early:
                return ""
            return self.chunked.eof()
        if self.framing == "content_length":
            if self.cl_remaining > 0 and not self.stop_early:
                return "invalid_response"
            self.done = True
            return ""
        self.done = True
        return ""


def _recv_sync(sock: socket.socket, hard_deadline: float) -> Tuple[bytes, str]:
    try:
        sock.settimeout(_require_remaining(hard_deadline))
        piece = sock.recv(_WIRE_RECV_BYTES)
    except (socket.timeout, TimeoutError, ssl.SSLWantReadError):
        return b"", "timeout"
    except OSError:
        return b"", "network_error"
    return piece, ""


async def _recv_async(reader: Any, hard_deadline: float) -> Tuple[bytes, str]:
    left = _require_remaining(hard_deadline)
    try:
        piece = await asyncio.wait_for(reader.read(_WIRE_RECV_BYTES), timeout=left)
    except (asyncio.TimeoutError, TimeoutError):
        return b"", "timeout"
    return piece, ""


def _read_response_body_sync(
    sock: socket.socket,
    hard_deadline: float,
    content_type: str,
    framing: str,
    content_length: int,
    initial_wire: bytes,
) -> Tuple[bytes, bool, str]:
    acc = _BodyAccumulator(
        content_type=content_type,
        framing=framing,
        content_length=content_length,
    )
    wire_buf = initial_wire
    while True:
        if wire_buf:
            err = acc.feed_wire(wire_buf)
            wire_buf = b""
            if err:
                return b"", False, err
        if acc.stop_early or acc.done or acc.oversized:
            break
        piece, recv_err = _recv_sync(sock, hard_deadline)
        if recv_err:
            return acc.body, acc.truncated, recv_err
        if not piece:
            eof_err = acc.eof()
            if eof_err:
                return b"", False, eof_err
            break
        wire_buf = piece
    if acc.oversized:
        return b"", False, "oversized"
    return acc.body, acc.truncated, ""


async def _read_response_body_async(
    reader: Any,
    hard_deadline: float,
    content_type: str,
    framing: str,
    content_length: int,
    initial_wire: bytes,
) -> Tuple[bytes, bool, str]:
    acc = _BodyAccumulator(
        content_type=content_type,
        framing=framing,
        content_length=content_length,
    )
    wire_buf = initial_wire
    while True:
        if wire_buf:
            err = acc.feed_wire(wire_buf)
            wire_buf = b""
            if err:
                return b"", False, err
        if acc.stop_early or acc.done or acc.oversized:
            break
        piece, recv_err = await _recv_async(reader, hard_deadline)
        if recv_err:
            return acc.body, acc.truncated, recv_err
        if not piece:
            eof_err = acc.eof()
            if eof_err:
                return b"", False, eof_err
            break
        wire_buf = piece
    if acc.oversized:
        return b"", False, "oversized"
    return acc.body, acc.truncated, ""


def _read_http_response_sync(
    sock: socket.socket,
    hard_deadline: float,
) -> SafeFetchResponse:
    header_buf = b""
    while b"\r\n\r\n" not in header_buf:
        sock.settimeout(_require_remaining(hard_deadline))
        try:
            piece = sock.recv(8192)
        except (socket.timeout, TimeoutError, ssl.SSLWantReadError):
            return SafeFetchResponse(status=0, error_class="timeout")
        except OSError:
            return SafeFetchResponse(status=0, error_class="network_error")
        if not piece:
            return SafeFetchResponse(status=0, error_class="invalid_response")
        header_buf += piece
        if len(header_buf) > MAX_HEADER_BYTES:
            return SafeFetchResponse(status=0, error_class="oversized")
    header_blob, _sep, initial_body = header_buf.partition(b"\r\n\r\n")
    headers, status, err = _parse_status_headers(header_blob)
    if err:
        return SafeFetchResponse(status=0, error_class=err)
    content_type = headers.get("content-type") or ""
    redirect = status in {301, 302, 303, 307, 308}
    if redirect:
        return SafeFetchResponse(status=status, headers=headers)
    framing, content_length, framing_err = _resolve_body_framing(headers)
    if framing_err:
        return SafeFetchResponse(status=status, headers=headers, error_class=framing_err)
    body, truncated, body_err = _read_response_body_sync(
        sock,
        hard_deadline,
        content_type,
        framing,
        content_length,
        initial_body,
    )
    if body_err:
        return SafeFetchResponse(status=status, headers=headers, error_class=body_err)
    return SafeFetchResponse(
        status=status,
        headers=headers,
        body=body,
        body_truncated=truncated,
    )


async def _read_http_response_async(
    reader: Any,
    hard_deadline: float,
) -> SafeFetchResponse:
    header_buf = b""
    while b"\r\n\r\n" not in header_buf:
        left = _require_remaining(hard_deadline)
        try:
            piece = await asyncio.wait_for(reader.read(8192), timeout=left)
        except (asyncio.TimeoutError, TimeoutError):
            return SafeFetchResponse(status=0, error_class="timeout")
        if not piece:
            return SafeFetchResponse(status=0, error_class="invalid_response")
        header_buf += piece
        if len(header_buf) > MAX_HEADER_BYTES:
            return SafeFetchResponse(status=0, error_class="oversized")
    header_blob, _sep, initial_body = header_buf.partition(b"\r\n\r\n")
    headers, status, err = _parse_status_headers(header_blob)
    if err:
        return SafeFetchResponse(status=0, error_class=err)
    content_type = headers.get("content-type") or ""
    redirect = status in {301, 302, 303, 307, 308}
    if redirect:
        return SafeFetchResponse(status=status, headers=headers)
    framing, content_length, framing_err = _resolve_body_framing(headers)
    if framing_err:
        return SafeFetchResponse(status=status, headers=headers, error_class=framing_err)
    body, truncated, body_err = await _read_response_body_async(
        reader,
        hard_deadline,
        content_type,
        framing,
        content_length,
        initial_body,
    )
    if body_err:
        return SafeFetchResponse(status=status, headers=headers, error_class=body_err)
    return SafeFetchResponse(
        status=status,
        headers=headers,
        body=body,
        body_truncated=truncated,
    )


def default_transport(
    request: SafeFetchRequest,
    *,
    deadline: Optional[float] = None,
    ssl_context: Optional[ssl.SSLContext] = None,
) -> SafeFetchResponse:
    """Connect to the pinned IP with TLS SNI for the original hostname.

    Used by transport tests against a local fixture. Production uses
    ``async_default_transport``. Does not honor HTTP(S)_PROXY.
    """
    sock: Optional[socket.socket] = None
    hard_deadline = deadline or request.deadline or (time.monotonic() + TOTAL_TIMEOUT_S)
    try:
        left = min(request.timeout_connect, _require_remaining(hard_deadline))
        sock = socket.create_connection((request.ip, request.port), timeout=left)
        sock.settimeout(_require_remaining(hard_deadline))
        if request.scheme == "https":
            ctx = ssl_context or ssl.create_default_context()
            sock = ctx.wrap_socket(sock, server_hostname=request.hostname)
        payload = (
            f"GET {request.path} HTTP/1.1\r\n"
            f"Host: {request.hostname}\r\n"
            f"User-Agent: {USER_AGENT}\r\n"
            f"Accept: text/html,application/xhtml+xml,application/json;q=0.9\r\n"
            f"Accept-Encoding: identity\r\n"
            f"Connection: close\r\n"
            f"\r\n"
        ).encode("ascii")
        sock.sendall(payload)
        return _read_http_response_sync(sock, hard_deadline)
    except (socket.timeout, TimeoutError, ssl.SSLWantReadError):
        return SafeFetchResponse(status=0, error_class="timeout")
    except ssl.SSLError:
        return SafeFetchResponse(status=0, error_class="tls_error")
    except OSError:
        return SafeFetchResponse(status=0, error_class="network_error")
    finally:
        if sock is not None:
            try:
                sock.close()
            except Exception:  # noqa: silent-ok — socket close is best-effort cleanup
                pass


async def _close_writer(writer: Any) -> None:
    if writer is None:
        return
    try:
        writer.close()
        await asyncio.wait_for(writer.wait_closed(), timeout=0.5)
    except Exception:  # noqa: silent-ok — socket close is best-effort cleanup
        try:
            writer.close()
        except Exception:  # noqa: silent-ok — already closing
            pass


async def async_default_transport(
    request: SafeFetchRequest,
    *,
    deadline: Optional[float] = None,
    ssl_context: Optional[ssl.SSLContext] = None,
) -> SafeFetchResponse:
    """Async pinned-IP GET. Cancellation/timeout closes the writer."""
    hard_deadline = deadline or request.deadline or (time.monotonic() + TOTAL_TIMEOUT_S)
    writer = None
    open_task: Optional[asyncio.Task[Any]] = None
    try:
        left = min(request.timeout_connect, _require_remaining(hard_deadline))
        ssl_ctx = None
        server_hostname = None
        if request.scheme == "https":
            ssl_ctx = ssl_context or ssl.create_default_context()
            server_hostname = request.hostname

        async def _open() -> Tuple[Any, Any]:
            return await asyncio.open_connection(
                host=request.ip,
                port=request.port,
                ssl=ssl_ctx,
                server_hostname=server_hostname,
                ssl_handshake_timeout=left if ssl_ctx is not None else None,
            )

        open_task = asyncio.create_task(_open())
        try:
            reader, writer = await asyncio.wait_for(asyncio.shield(open_task), timeout=left)
        except (asyncio.TimeoutError, TimeoutError):
            open_task.cancel()
            try:
                opened = await open_task
            except Exception:
                opened = None
            if opened is not None:
                await _close_writer(opened[1])
            return SafeFetchResponse(status=0, error_class="timeout")
        payload = (
            f"GET {request.path} HTTP/1.1\r\n"
            f"Host: {request.hostname}\r\n"
            f"User-Agent: {USER_AGENT}\r\n"
            f"Accept: text/html,application/xhtml+xml,application/json;q=0.9\r\n"
            f"Accept-Encoding: identity\r\n"
            f"Connection: close\r\n"
            f"\r\n"
        ).encode("ascii")
        writer.write(payload)
        await asyncio.wait_for(writer.drain(), timeout=_require_remaining(hard_deadline))
        return await _read_http_response_async(reader, hard_deadline)
    except asyncio.CancelledError:
        raise
    except (asyncio.TimeoutError, TimeoutError):
        return SafeFetchResponse(status=0, error_class="timeout")
    except ssl.SSLError:
        return SafeFetchResponse(status=0, error_class="tls_error")
    except OSError:
        return SafeFetchResponse(status=0, error_class="network_error")
    finally:
        await _close_writer(writer)


def _loop_semaphore(holder: List[Any], size: int) -> asyncio.Semaphore:
    loop = asyncio.get_running_loop()
    if not holder or holder[0] is not loop:
        holder[:] = [loop, asyncio.Semaphore(size)]
    return holder[1]


def _fetch_semaphore() -> asyncio.Semaphore:
    return _loop_semaphore(_FETCH_SEMA_HOLDER, FETCH_CONCURRENCY)


def _dns_semaphore() -> asyncio.Semaphore:
    return _loop_semaphore(_DNS_SEMA_HOLDER, FETCH_CONCURRENCY)


def _io_executor() -> ThreadPoolExecutor:
    global _IO_EXECUTOR
    if _IO_EXECUTOR is None:
        _IO_EXECUTOR = ThreadPoolExecutor(
            max_workers=FETCH_CONCURRENCY,
            thread_name_prefix="urlctx-io",
        )
    return _IO_EXECUTOR


def _shutdown_io_executor() -> None:
    global _IO_EXECUTOR
    pool = _IO_EXECUTOR
    _IO_EXECUTOR = None
    if pool is not None:
        pool.shutdown(wait=False, cancel_futures=True)


atexit.register(_shutdown_io_executor)


def _records_from_addrinfo(
    infos: Sequence[Any],
) -> List[Tuple[socket.AddressFamily, str]]:
    out: List[Tuple[socket.AddressFamily, str]] = []
    seen = set()
    for family, _type, _proto, _canon, sockaddr in infos:
        ip = sockaddr[0]
        key = (family, ip)
        if key in seen:
            continue
        seen.add(key)
        out.append((family, ip))
    return out


async def _async_resolve(
    hostname: str,
    port: int,
    *,
    deadline: float,
    resolver: Optional[Resolver],
) -> Tuple[List[Tuple[socket.AddressFamily, str]], str]:
    try:
        left = _require_remaining(deadline)
    except TimeoutError:
        return [], "timeout"
    if resolver is not None and resolver is not default_resolver:
        try:
            records = list(resolver(hostname, port) or [])
        except Exception:
            return [], "dns_error"
        return records, "" if records else "dns_error"
    loop = asyncio.get_running_loop()
    dns_slot = _dns_semaphore()
    try:
        await asyncio.wait_for(dns_slot.acquire(), timeout=left)
    except (asyncio.TimeoutError, TimeoutError):
        return [], "timeout"
    released = False

    def _release_dns_slot() -> None:
        nonlocal released
        if released:
            return
        released = True
        try:
            dns_slot.release()
        except Exception:  # noqa: silent-ok — slot release is best-effort
            pass

    try:
        fut = loop.run_in_executor(
            _io_executor(),
            socket.getaddrinfo,
            hostname,
            port,
            0,
            socket.SOCK_STREAM,
        )
        fut.add_done_callback(
            lambda _f: loop.call_soon_threadsafe(_release_dns_slot)
        )
        try:
            infos = await asyncio.wait_for(
                asyncio.shield(fut),
                timeout=_require_remaining(deadline),
            )
        except (asyncio.TimeoutError, TimeoutError):
            return [], "timeout"
    except TimeoutError:
        _release_dns_slot()
        return [], "timeout"
    except OSError:
        return [], "dns_error"
    except Exception:
        if not released:
            _release_dns_slot()
        return [], "dns_error"
    out = _records_from_addrinfo(infos)
    return out, "" if out else "dns_error"


async def _validate_async(
    url: str,
    *,
    resolver: Optional[Resolver],
    deadline: float,
) -> Tuple[Optional[SafeFetchRequest], str]:
    parsed, err = _parse_and_validate_url(url)
    if parsed is None:
        return None, err
    try:
        _require_remaining(deadline)
    except TimeoutError:
        return None, "timeout"
    host = _idna_hostname(parsed.hostname or "")
    scheme = parsed.scheme.lower()
    port, port_err = _scheme_port(scheme, parsed.port)
    if port_err or port is None:
        return None, port_err or "invalid_port"
    records, dns_err = await _async_resolve(
        host, port, deadline=deadline, resolver=resolver
    )
    if dns_err:
        return None, dns_err
    ips = [ip for _family, ip in records]
    if any(is_blocked_ip(ip) for ip in ips):
        return None, "ip_blocked"
    chosen_ip = ips[0]
    path = parsed.path or "/"
    if parsed.query:
        path = f"{path}?{parsed.query}"
    if "\r" in path or "\n" in path:
        return None, "invalid_url"
    left = remaining_deadline(deadline)
    req = SafeFetchRequest(
        url=urlunparse(
            (
                scheme,
                parsed.netloc.split("@")[-1],
                parsed.path or "/",
                "",
                parsed.query,
                "",
            )
        ),
        hostname=host,
        ip=chosen_ip,
        port=int(port),
        scheme=scheme,
        path=path,
        timeout_connect=min(CONNECT_TIMEOUT_S, max(0.05, left)),
        timeout_read=max(0.05, left),
        deadline=deadline,
    )
    return req, ""


async def _call_transport(
    transport: Any,
    request: SafeFetchRequest,
    *,
    deadline: float,
) -> SafeFetchResponse:
    if transport is None or transport is default_transport:
        return await async_default_transport(request, deadline=deadline)
    result = transport(request)
    if asyncio.iscoroutine(result):
        return await asyncio.wait_for(result, timeout=_require_remaining(deadline))
    return result


async def fetch_url_async(
    url: str,
    *,
    resolver: Optional[Resolver] = None,
    transport: Any = None,
    max_redirects: int = MAX_REDIRECTS,
    deadline: Optional[float] = None,
) -> SafeHttpResult:
    """Fetch ``url`` with SSRF controls. Fail closed. No cookies. Async."""
    started = time.monotonic()
    if deadline is None:
        deadline = started + TOTAL_TIMEOUT_S
    current = str(url or "").strip()
    seen: List[str] = []
    hops = 0
    last_error = "unknown"
    sem = _fetch_semaphore()
    async with sem:
        while hops <= max_redirects:
            try:
                _require_remaining(deadline)
            except TimeoutError:
                return SafeHttpResult(ok=False, url=url, error_class="timeout", hops=hops)
            req, err = await _validate_async(
                current, resolver=resolver, deadline=deadline
            )
            if req is None:
                logger.info(
                    "[url_context.fetch] blocked url=%s class=%s",
                    redact_url_for_log(current),
                    err,
                )
                return SafeHttpResult(
                    ok=False, url=url, final_url=current, error_class=err, hops=hops
                )
            marker = f"{req.scheme}://{req.hostname}{req.path}"
            if marker in seen:
                return SafeHttpResult(
                    ok=False, url=url, final_url=current, error_class="redirect_loop", hops=hops
                )
            seen.append(marker)
            try:
                resp = await _call_transport(transport, req, deadline=deadline)
            except TimeoutError:
                return SafeHttpResult(
                    ok=False, url=url, final_url=current, error_class="timeout", hops=hops
                )
            except Exception:
                return SafeHttpResult(
                    ok=False, url=url, final_url=current, error_class="transport_error", hops=hops
                )
            if resp.error_class:
                return SafeHttpResult(
                    ok=False,
                    url=url,
                    final_url=current,
                    status=resp.status,
                    error_class=resp.error_class,
                    hops=hops,
                )
            if resp.status in {301, 302, 303, 307, 308}:
                location = (resp.headers or {}).get("location") or ""
                if not location.strip():
                    return SafeHttpResult(
                        ok=False,
                        url=url,
                        final_url=current,
                        status=resp.status,
                        error_class="redirect_missing",
                        hops=hops,
                    )
                nxt = urljoin(current, location.strip())
                hops += 1
                if hops > max_redirects:
                    return SafeHttpResult(
                        ok=False,
                        url=url,
                        final_url=current,
                        error_class="too_many_redirects",
                        hops=hops,
                    )
                current = nxt
                last_error = "redirect"
                continue
            if resp.status < 200 or resp.status >= 300:
                return SafeHttpResult(
                    ok=False,
                    url=url,
                    final_url=current,
                    status=resp.status,
                    error_class="http_error",
                    hops=hops,
                )
            content_type = _normalize_content_type(
                (resp.headers or {}).get("content-type") or ""
            )
            if not content_type or content_type not in ALLOWED_CONTENT_TYPES:
                return SafeHttpResult(
                    ok=False,
                    url=url,
                    final_url=current,
                    status=resp.status,
                    content_type=content_type,
                    error_class="content_type_blocked",
                    hops=hops,
                )
            body = resp.body or b""
            if len(body) > MAX_RESPONSE_BYTES:
                return SafeHttpResult(
                    ok=False,
                    url=url,
                    final_url=current,
                    status=resp.status,
                    content_type=content_type,
                    error_class="oversized",
                    hops=hops,
                )
            return SafeHttpResult(
                ok=True,
                url=url,
                final_url=current,
                status=resp.status,
                content_type=content_type,
                body=body,
                hops=hops,
                body_truncated=bool(resp.body_truncated),
            )
    return SafeHttpResult(
        ok=False, url=url, error_class=last_error or "too_many_redirects", hops=hops
    )


def fetch_url(
    url: str,
    *,
    resolver: Resolver = default_resolver,
    transport: Transport = default_transport,
    max_redirects: int = MAX_REDIRECTS,
    deadline: Optional[float] = None,
) -> SafeHttpResult:
    """Sync wrapper for tests outside a running event loop."""
    try:
        asyncio.get_running_loop()
    except RuntimeError:
        return asyncio.run(
            fetch_url_async(
                url,
                resolver=resolver,
                transport=transport,
                max_redirects=max_redirects,
                deadline=deadline,
            )
        )
    raise RuntimeError("fetch_url_async required inside a running event loop")

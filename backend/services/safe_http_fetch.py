"""SSRF-safe HTTP GET for URL metadata enrichment.

This is the D17 security boundary. Do not reuse ``expand_maps_url``.
Never logs full query strings. Never sends cookies or credentials.
"""
from __future__ import annotations

import ipaddress
import logging
import socket
import ssl
import time
from dataclasses import dataclass, field
from typing import Callable, Dict, FrozenSet, Iterable, List, Optional, Sequence, Tuple
from urllib.parse import urljoin, urlparse, urlunparse

logger = logging.getLogger("nahla.url_context.safe_http")

ALLOWED_SCHEMES: FrozenSet[str] = frozenset({"http", "https"})
MAX_REDIRECTS = 3
MAX_RESPONSE_BYTES = 262_144  # 256 KiB, enforced while streaming
CONNECT_TIMEOUT_S = 3.0
READ_TIMEOUT_S = 4.0
TOTAL_TIMEOUT_S = 8.0
USER_AGENT = "NahlaURLContext/1.0"
ALLOWED_CONTENT_TYPES: FrozenSet[str] = frozenset(
    {
        "text/html",
        "application/xhtml+xml",
        "application/json",
        "text/plain",
        "application/xml",
        "text/xml",
    }
)
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

Resolver = Callable[[str, int], Sequence[Tuple[socket.AddressFamily, str]]]
Transport = Callable[["SafeFetchRequest"], "SafeFetchResponse"]


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


@dataclass
class SafeFetchResponse:
    status: int
    headers: Dict[str, str] = field(default_factory=dict)
    body: bytes = b""
    error_class: str = ""


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


def _idna_hostname(host: str) -> str:
    raw = str(host or "").strip().rstrip(".").lower()
    if not raw:
        return ""
    try:
        return raw.encode("idna").decode("ascii")
    except Exception:
        return raw


def _normalize_content_type(value: str) -> str:
    raw = str(value or "").split(";", 1)[0].strip().lower()
    return raw


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


def _parse_and_validate_url(url: str) -> Tuple[Optional[urlparse], str]:
    raw = str(url or "").strip()
    if not raw or len(raw) > 2048:
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
    try:
        literal = ipaddress.ip_address(host)
    except ValueError:
        literal = None
    if literal is not None and is_blocked_ip(str(literal)):
        return None, "ip_blocked"
    return parsed, ""


def validate_destination(
    url: str,
    *,
    resolver: Resolver = default_resolver,
) -> Tuple[Optional[SafeFetchRequest], str]:
    parsed, err = _parse_and_validate_url(url)
    if parsed is None:
        return None, err
    host = _idna_hostname(parsed.hostname or "")
    scheme = parsed.scheme.lower()
    port = parsed.port or (443 if scheme == "https" else 80)
    if port <= 0 or port > 65535:
        return None, "invalid_port"
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
        timeout_read=READ_TIMEOUT_S,
    )
    return req, ""


def _header_map(raw_headers: Iterable[Tuple[str, str]]) -> Dict[str, str]:
    out: Dict[str, str] = {}
    for key, value in raw_headers:
        out[str(key or "").lower()] = str(value or "")
    return out


def default_transport(request: SafeFetchRequest) -> SafeFetchResponse:
    """Connect to the pinned IP with TLS SNI for the original hostname."""
    sock: Optional[socket.socket] = None
    try:
        sock = socket.create_connection(
            (request.ip, request.port),
            timeout=request.timeout_connect,
        )
        sock.settimeout(request.timeout_read)
        if request.scheme == "https":
            ctx = ssl.create_default_context()
            sock = ctx.wrap_socket(sock, server_hostname=request.hostname)
        payload = (
            f"GET {request.path} HTTP/1.1\r\n"
            f"Host: {request.hostname}\r\n"
            f"User-Agent: {USER_AGENT}\r\n"
            f"Accept: text/html,application/xhtml+xml,application/json;q=0.9,*/*;q=0.1\r\n"
            f"Accept-Encoding: identity\r\n"
            f"Connection: close\r\n"
            f"\r\n"
        ).encode("ascii")
        sock.sendall(payload)
        chunks: List[bytes] = []
        total = 0
        while True:
            piece = sock.recv(8192)
            if not piece:
                break
            total += len(piece)
            if total > MAX_RESPONSE_BYTES + 8192:
                return SafeFetchResponse(status=0, error_class="oversized")
            chunks.append(piece)
            if total > MAX_RESPONSE_BYTES + 4096:
                break
        raw = b"".join(chunks)
        header_blob, sep, body = raw.partition(b"\r\n\r\n")
        if not sep:
            return SafeFetchResponse(status=0, error_class="invalid_response")
        lines = header_blob.split(b"\r\n")
        if not lines:
            return SafeFetchResponse(status=0, error_class="invalid_response")
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
        if len(body) > MAX_RESPONSE_BYTES:
            return SafeFetchResponse(
                status=status,
                headers=headers,
                error_class="oversized",
            )
        return SafeFetchResponse(status=status, headers=headers, body=body)
    except socket.timeout:
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


def fetch_url(
    url: str,
    *,
    resolver: Resolver = default_resolver,
    transport: Transport = default_transport,
    max_redirects: int = MAX_REDIRECTS,
    deadline: Optional[float] = None,
) -> SafeHttpResult:
    """Fetch ``url`` with SSRF controls. Fail closed. No cookies."""
    started = time.monotonic()
    if deadline is None:
        deadline = started + TOTAL_TIMEOUT_S
    current = str(url or "").strip()
    seen: List[str] = []
    hops = 0
    last_error = "unknown"
    while hops <= max_redirects:
        if time.monotonic() > deadline:
            return SafeHttpResult(ok=False, url=url, error_class="timeout", hops=hops)
        req, err = validate_destination(current, resolver=resolver)
        if req is None:
            logger.info(
                "[url_context.fetch] blocked url=%s class=%s",
                redact_url_for_log(current),
                err,
            )
            return SafeHttpResult(ok=False, url=url, final_url=current, error_class=err, hops=hops)
        marker = f"{req.scheme}://{req.hostname}{req.path}"
        if marker in seen:
            return SafeHttpResult(ok=False, url=url, final_url=current, error_class="redirect_loop", hops=hops)
        seen.append(marker)
        remaining = max(0.2, deadline - time.monotonic())
        pinned = SafeFetchRequest(
            url=req.url,
            hostname=req.hostname,
            ip=req.ip,
            port=req.port,
            scheme=req.scheme,
            path=req.path,
            timeout_connect=min(req.timeout_connect, remaining),
            timeout_read=min(req.timeout_read, remaining),
        )
        try:
            resp = transport(pinned)
        except Exception:
            return SafeHttpResult(ok=False, url=url, final_url=current, error_class="transport_error", hops=hops)
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
        content_type = _normalize_content_type((resp.headers or {}).get("content-type") or "")
        if content_type and content_type not in ALLOWED_CONTENT_TYPES:
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
            content_type=content_type or "text/html",
            body=body,
            hops=hops,
        )
    return SafeHttpResult(ok=False, url=url, error_class=last_error or "too_many_redirects", hops=hops)

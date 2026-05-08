"""
core/fast_path_middleware.py
────────────────────────────
Pure ASGI middleware that short-circuits ultra-light liveness routes
BEFORE they enter the FastAPI/Starlette ``BaseHTTPMiddleware`` chain.

Why this exists
───────────────
``app.middleware("http")(fn)`` registrations all become
``BaseHTTPMiddleware`` layers under the hood. ``BaseHTTPMiddleware``
has well-known interactions with ``asyncio.CancelledError`` and
``anyio.EndOfStream`` (both of which fire on a client disconnect or a
half-closed TCP stream): the inner ASGI task can finish without
sending any response event, which surfaces in Starlette as

    RuntimeError: No response returned.

When a request burst (e.g. 360dialog flushing a backlog of webhooks
after a deploy) saturates the ``BaseHTTPMiddleware`` chain, even
``GET /alive`` and ``GET /healthz`` start failing intermittently
because they share the same chain.

This module installs a *pure ASGI* middleware as the OUTERMOST layer
inside the ``CORSMiddleware`` (i.e. CORS still runs first to add the
allow-origin header, then this layer runs). When the request path
matches a registered fast-path route, we synthesize the response
ourselves with ``send`` events and never delegate to ``self.app`` —
so the entire ``BaseHTTPMiddleware`` chain (and every blocking
import, DB session, JWT decode, rate-limit lookup, etc.) is bypassed.

Routes served here
──────────────────
* ``GET /alive``       — process liveness, no DB, no JWT, no I/O
* ``GET /healthz``     — alias of /alive for upstream health checks
* ``GET /auth/ping``   — connectivity probe used by the dashboard
                         login page; needs the same fast-path
                         guarantee or login spinners stick when the
                         worker is congested

Anything else falls through to ``self.app`` unchanged.

The handler is deliberately allocation-light: ``orjson`` is preferred
when available (FastAPI ships it), the response body is built from a
single dict, and ``time.time()`` is the only syscall on the hot path.
"""
from __future__ import annotations

import asyncio
import logging
import os
import time
from typing import Awaitable, Callable, Dict, Iterable, Optional, Tuple

logger = logging.getLogger("nahla-backend.fast_path")

# Some reverse proxies / HTTP/2 edge stacks deliver ``receive()`` late or
# never for GET + empty-body probes. An unbounded ``await receive()`` then
# freezes the connection from the browser's perspective (0 bytes) even
# though POST webhooks on other paths work fine. Cap each ``receive()`` wait.
_FASTPATH_RECV_TIMEOUT = float(os.environ.get("NAHLA_FASTPATH_RECEIVE_TIMEOUT", "3.0"))


# Default routes served from the fast path. Keep this list short and
# obvious — anything that needs DB / JWT / business logic does NOT
# belong here.
DEFAULT_FAST_PATHS: Tuple[str, ...] = (
    "/",  # probes / misconfigured health checks hitting the bare host
    "/alive",
    "/healthz",
    "/auth/ping",
)


def _orjson_dumps(payload: Dict) -> bytes:
    """JSON-encode without an extra dependency: orjson if installed,
    otherwise the stdlib. Returns bytes ready for the ASGI
    ``http.response.body`` event."""
    try:
        import orjson  # type: ignore  # noqa: PLC0415
        return orjson.dumps(payload)
    except Exception:  # pragma: no cover — orjson should be available
        import json  # noqa: PLC0415
        return json.dumps(payload, separators=(",", ":")).encode("utf-8")


class FastPathMiddleware:
    """Pure ASGI app that wraps another ASGI app.

    On match it sends a synthesized 200 JSON response and returns,
    skipping every layer below it. CORS allow-origin headers are still
    added by ``CORSMiddleware`` because ``CORSMiddleware`` is the
    OUTERMOST middleware (added last via ``app.add_middleware``).
    """

    def __init__(
        self,
        app: Callable[..., Awaitable[None]],
        *,
        fast_paths: Optional[Iterable[str]] = None,
    ) -> None:
        self.app = app
        # frozenset for constant-time membership checks
        self._fast_paths = frozenset(fast_paths or DEFAULT_FAST_PATHS)
        # Cached bytes for known origins of the GET responses. The
        # body is rebuilt per-request because ``ts`` must be live.

    async def __call__(self, scope, receive, send) -> None:
        # Lifespan / websocket: pass through unchanged. We only
        # intercept HTTP requests and only when the method+path
        # matches a fast-path route.
        if scope.get("type") != "http":
            await self.app(scope, receive, send)
            return

        method = scope.get("method", "")
        path   = scope.get("path", "")
        if method not in ("GET", "HEAD") or path not in self._fast_paths:
            await self.app(scope, receive, send)
            return

        # ── Synthesize the response ─────────────────────────────────────
        # Best-effort drain of the ASGI request stream. MUST NOT block
        # indefinitely — see module-level comment on _FASTPATH_RECV_TIMEOUT.
        async def _recv_once():
            return await asyncio.wait_for(receive(), timeout=_FASTPATH_RECV_TIMEOUT)

        try:
            msg = await _recv_once()
            while msg.get("more_body", False):
                msg = await _recv_once()
        except asyncio.TimeoutError:
            logger.warning(
                "[FastPath] receive timeout (%.1fs) path=%s — sending response anyway",
                _FASTPATH_RECV_TIMEOUT,
                path,
            )
        except Exception as exc:  # noqa: BLE001
            logger.warning("[FastPath] receive() failed on %s: %s", path, exc)

        payload = {"ok": True, "ts": time.time(), "service": "fast-path"}
        body = _orjson_dumps(payload)
        is_head = method == "HEAD"
        # RFC 9110: HEAD uses same headers as GET but no message body.
        hdrs = [
            (b"content-type", b"application/json"),
            (b"cache-control", b"no-store"),
            (b"x-fast-path", b"1"),
            (b"content-length", str(len(body)).encode("ascii")),
        ]
        try:
            await send({"type": "http.response.start", "status": 200, "headers": hdrs})
            await send({
                "type":      "http.response.body",
                "body":      b"" if is_head else body,
                "more_body": False,
            })
        except Exception as exc:  # noqa: BLE001
            # Client disconnect after we started sending: log and
            # swallow. Re-raising would defeat the entire purpose of
            # this middleware.
            logger.warning(
                "[FastPath] send failed on %s (client likely gone): %s",
                path, exc,
            )

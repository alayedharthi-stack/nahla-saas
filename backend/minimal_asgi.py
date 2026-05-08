"""
backend/minimal_asgi.py
───────────────────────
Bare Starlette app with zero Nahla imports — used only when Railway sets
``NAHLA_MINIMAL_ASGI=1`` (see ``start.sh``).

If GET requests never hit ``backend.main`` but POST does, swapping to this
module proves whether Python/uvicorn/socket binding accepts GET at all.
"""
from __future__ import annotations

import os

from starlette.applications import Starlette
from starlette.responses import PlainTextResponse
from starlette.routing import Route

print("[BOOT/minimal_asgi] PORT=", os.getenv("PORT"), flush=True)


async def _probe(request):
    port = os.environ.get("PORT", "?")
    return PlainTextResponse(
        f"minimal_ok port={port} path={request.url.path}\n",
        media_type="text/plain",
    )


# Inner Starlette graph — wrapped so browser GET shows up as RAW_SCOPE in logs
# even though this module does not import backend.main (no FastAPI wrapper).
_STARLETTE_APPLICATION = Starlette(
    debug=False,
    routes=[
        Route("/", _probe),
        Route("/alive", _probe),
        Route("/healthz", _probe),
        Route("/auth/ping", _probe),
    ],
)


async def app(scope, receive, send):  # noqa: A001 — uvicorn entrypoint name
    if scope.get("type") == "http":
        print(
            "RAW_SCOPE",
            scope.get("type"),
            scope.get("method"),
            scope.get("path"),
            flush=True,
        )
    await _STARLETTE_APPLICATION(scope, receive, send)

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


app = Starlette(
    debug=False,
    routes=[
        Route("/", _probe),
        Route("/alive", _probe),
        Route("/healthz", _probe),
        Route("/auth/ping", _probe),
    ],
)

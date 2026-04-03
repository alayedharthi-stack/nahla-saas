"""
routers/widget.py
──────────────────
Serves the Nahla storefront JavaScript snippet.

Routes
  GET /snippet.js
"""
from __future__ import annotations

import os

from fastapi import APIRouter
from fastapi.responses import Response

router = APIRouter(tags=["Widget"])


@router.get("/snippet.js")
async def serve_snippet():
    """
    Serve the Nahla storefront tracking snippet.
    Merchants add one <script> tag pointing here; the script handles all event
    tracking (page view, product view, add to cart, cart abandon, checkout).
    """
    snippet_path = os.path.join(os.path.dirname(__file__), "..", "snippet.js")
    try:
        with open(snippet_path, "r", encoding="utf-8") as f:
            js = f.read()
    except FileNotFoundError:
        js = "/* Nahla snippet not found */"
    return Response(
        content=js,
        media_type="application/javascript",
        headers={"Cache-Control": "public, max-age=300"},
    )

"""
routers/inbound_media.py
────────────────────────
HTTP surface for serving inbound WhatsApp media that the normalizer
persisted via ``services.inbound_media_storage``.

Why a separate router (not piggy-backing on intelligence_libraries):

* The AI Media Library is *merchant-uploaded*, mutable, listed in a
  CRUD UI, and tied to ``AIMediaItem``. Lifecycle: merchant deletes
  → file disappears.
* Inbound media is *customer-uploaded*, immutable, system-owned,
  retained for diagnostic/playback purposes only. It has no DB row;
  the filesystem IS the source of truth (content-addressed by sha256).

Conflating these two would mean every voice-note check-in would
clutter the merchant's media library page. Keeping them separate
preserves the existing UX and stays migration-free.

Security:
  * The URL embeds the tenant_id so a sha256 leak doesn't grant
    cross-tenant access (a different tenant's URL won't resolve).
  * We require the caller's JWT to belong to the same tenant — the
    URL is NOT a public/signed link; the dashboard fetches it with
    the same Authorization header it uses everywhere else.
  * sha256 is validated to be hex-only before being used in a path.
"""
from __future__ import annotations

import logging
import mimetypes
import re
from pathlib import Path

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import FileResponse
from sqlalchemy.orm import Session

from core.database import get_db
from core.tenant import resolve_tenant_id
from services.inbound_media_storage import resolve_storage_path

router = APIRouter()
_logger = logging.getLogger("nahla-backend")

# Validate the URL slug ``<sha256>.<ext>`` shape strictly. We accept
# the same extensions ``_MIME_TO_EXT`` produces, plus a permissive
# ``.bin`` for legacy rows.
_SLUG_RE = re.compile(
    r"^(?P<sha>[0-9a-f]{64})(?P<ext>\.[a-z0-9]{2,5})$"
)


@router.get("/media/inbound/{tenant_id:int}/{slug}")
async def stream_inbound_media(
    tenant_id: int,
    slug: str,
    request: Request,
    db: Session = Depends(get_db),
):
    """Stream a previously-persisted inbound media file.

    Returns ``404`` when the file is missing (e.g. expired storage,
    misconfigured volume mount) so the dashboard can render a graceful
    "التسجيل غير متاح" placeholder instead of a broken icon.

    Returns ``403`` when the JWT belongs to a different tenant — this
    is the cross-tenant boundary; we don't leak the file's existence
    by returning 404 in that case.
    """
    caller_tenant = resolve_tenant_id(request)
    if int(caller_tenant) != int(tenant_id):
        # Match the cross-tenant posture used elsewhere in the
        # codebase: 403, not 404, so monitoring can flag unauthorised
        # access attempts distinctly from missing files.
        raise HTTPException(status_code=403, detail="cross_tenant_denied")

    m = _SLUG_RE.match(slug)
    if not m:
        raise HTTPException(status_code=404, detail="invalid_slug")

    sha = m.group("sha")
    ext = m.group("ext")

    path = resolve_storage_path(
        tenant_id=tenant_id, sha256=sha, ext=ext,
    )
    if path is None or not Path(path).exists():
        raise HTTPException(status_code=404, detail="not_found")

    media_type = (
        mimetypes.guess_type(path.name)[0]
        or "application/octet-stream"
    )
    return FileResponse(
        path=str(path),
        media_type=media_type,
        filename=path.name,
        # Allow the dashboard to cache the file aggressively — it is
        # content-addressed so the URL is immutable forever.
        headers={"Cache-Control": "public, max-age=31536000, immutable"},
    )

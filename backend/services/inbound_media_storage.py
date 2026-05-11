"""
services/inbound_media_storage.py
─────────────────────────────────
Persist inbound WhatsApp media (voice notes, audio files, images) to
durable storage so we can:

  * Surface the original recording / image in the conversation drawer
    even after Meta's CDN URL expires (Meta media URLs expire ~5 minutes
    after issue — relying on ``media_id`` alone is a guaranteed broken
    image / audio player on the second page-load).
  * Replay failed transcriptions / vision describes without re-hitting
    the merchant's Meta token bucket.
  * Power ``GET /conversations/{id}/media-debug`` with stable URLs and
    checksums so support can prove what arrived vs. what was processed.
  * Diff cross-tenant deduplication (the same brand asset re-uploaded
    by a customer ends up at the same sha256, served from one row).

Design choices, in order of importance:

1. **Content addressed** — files live at
   ``<root>/<tenant_id>/<YYYYMM>/<sha256>.<ext>``. Two uploads of the
   same bytes share storage automatically (idempotency for free).
2. **Tenant-scoped** — the ``tenant_id`` prefix in the path is a hard
   boundary: a 360dialog leak that revealed a sha256 wouldn't let any
   *other* tenant fetch it because the URL also includes their id.
3. **Stateless** — no database table. Metadata travels alongside the
   message via ``MessageEvent.extra_metadata.normalized_inbound`` so
   schema migrations stay zero in this commit.
4. **No outbound coupling** — we deliberately do NOT reuse
   ``intelligence_libraries._ensure_upload_dir`` because the outbound
   AI Media Library has different lifecycle (merchant-owned, deletable,
   listed in a CRUD page). Inbound media is system-owned: the customer
   uploaded it, the system stores it for diagnostics, the merchant
   sees a read-only player.

Local-disk only on purpose: a real deploy will mount a volume at
``NAHLA_INBOUND_MEDIA_DIR``. We never write to /tmp because /tmp gets
swept on every Railway redeploy, which would silently break the
conversation drawer history for past voice notes.
"""
from __future__ import annotations

import hashlib
import logging
import mimetypes
import os
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

logger = logging.getLogger("nahla.inbound_media")

# ── Storage root ────────────────────────────────────────────────────
# Production: mount a persistent volume on ``/data/inbound-media``.
# Dev: defaults to ``<repo>/uploads/inbound-media`` so tests + the
# Vite dev server can find it without extra config.
_REPO_ROOT = Path(__file__).resolve().parents[2]
_STORAGE_ROOT = Path(
    os.environ.get("NAHLA_INBOUND_MEDIA_DIR")
    or (_REPO_ROOT / "uploads" / "inbound-media")
).resolve()

# Mime type → file suffix. We keep this explicit (rather than relying
# on ``mimetypes.guess_extension``) because we want stable, vetted
# extensions in URLs — ``mimetypes`` returns ``.jpe`` for ``image/jpeg``
# on some platforms which then breaks the conversation player.
_MIME_TO_EXT = {
    # Voice / audio
    "audio/ogg":        ".ogg",
    "audio/ogg; codecs=opus": ".ogg",
    "audio/oga":        ".ogg",
    "audio/opus":       ".ogg",
    "audio/mpeg":       ".mp3",
    "audio/mp3":        ".mp3",
    "audio/wav":        ".wav",
    "audio/x-wav":      ".wav",
    "audio/aac":        ".aac",
    "audio/mp4":        ".m4a",
    "audio/m4a":        ".m4a",
    "audio/webm":       ".webm",
    # Image
    "image/jpeg":       ".jpg",
    "image/jpg":        ".jpg",
    "image/png":        ".png",
    "image/webp":       ".webp",
    "image/gif":        ".gif",
}


@dataclass(frozen=True)
class StoredInboundMedia:
    """Result of a successful ``save_inbound_media`` call.

    ``storage_url`` is what we persist on the message row and what the
    dashboard fetches via the inbound-media router. It is INTENTIONALLY
    relative (no host) so that local/staging/prod all share the same
    JSONB payload and the API base resolves at render time.
    """
    sha256: str
    byte_size: int
    mime_type: str
    ext: str
    storage_path: str
    storage_url: str
    tenant_id: int
    kind: str           # "audio" | "image"
    dedup: bool         # True when the same sha256 already existed on disk


def _resolve_ext(mime_type: str, fallback: str = ".bin") -> str:
    mime = (mime_type or "").lower().strip()
    # Strip ``; codecs=...`` parameters so ``audio/ogg; codecs=opus``
    # matches the catalogue.
    primary = mime.split(";", 1)[0].strip()
    return (
        _MIME_TO_EXT.get(mime)
        or _MIME_TO_EXT.get(primary)
        or (mimetypes.guess_extension(primary) if primary else None)
        or fallback
    )


def _tenant_root(tenant_id: int) -> Path:
    """Return the per-tenant storage directory, creating it if needed.

    We bucket files into ``YYYYMM`` subdirectories so a tenant with
    100k voice notes doesn't degrade filesystem performance on a
    flat directory.
    """
    yyyymm = datetime.now(timezone.utc).strftime("%Y%m")
    target = _STORAGE_ROOT / str(int(tenant_id)) / yyyymm
    target.mkdir(parents=True, exist_ok=True)
    return target


def save_inbound_media(
    *,
    tenant_id: int,
    file_bytes: bytes,
    mime_type: str,
    kind: str,
    media_id: Optional[str] = None,
) -> StoredInboundMedia:
    """Persist a downloaded WhatsApp media blob and return the metadata
    callers stamp onto ``MessageEvent.extra_metadata``.

    ``kind`` must be ``"audio"`` or ``"image"``. Callers are expected
    to validate this before calling — we don't want a future "document"
    type to silently start writing PDFs to inbound-media storage
    without an explicit code path.

    Raises ``ValueError`` for empty payloads. Never raises on
    filesystem errors — instead we re-raise the underlying ``OSError``
    so the caller can decide whether to fall back to in-memory only.
    """
    if not file_bytes:
        raise ValueError("save_inbound_media: empty payload")
    if kind not in {"audio", "image"}:
        raise ValueError(
            f"save_inbound_media: kind must be 'audio' or 'image' "
            f"(got {kind!r})"
        )

    sha256 = hashlib.sha256(file_bytes).hexdigest()
    ext = _resolve_ext(mime_type)
    target_dir = _tenant_root(tenant_id)
    target_path = target_dir / f"{sha256}{ext}"

    dedup = target_path.exists()
    if not dedup:
        # Write to a temp sibling and rename atomically so a crashed
        # worker can't leave a half-written file behind that would
        # then fail to play / decode.
        tmp_path = target_path.with_suffix(target_path.suffix + ".tmp")
        with tmp_path.open("wb") as f:
            f.write(file_bytes)
        os.replace(tmp_path, target_path)

    # The URL is rooted under ``/media/inbound/`` (see
    # ``routers/inbound_media.py``). Keep it relative — the dashboard
    # joins it with the API base at render time.
    storage_url = f"/media/inbound/{int(tenant_id)}/{sha256}{ext}"

    logger.info(
        "[InboundMedia] saved tenant=%s kind=%s sha256=%s bytes=%d "
        "ext=%s dedup=%s media_id=%s",
        tenant_id, kind, sha256[:12], len(file_bytes), ext, dedup,
        media_id or "—",
    )

    return StoredInboundMedia(
        sha256=sha256,
        byte_size=len(file_bytes),
        mime_type=(mime_type or "").strip() or "application/octet-stream",
        ext=ext,
        storage_path=str(target_path),
        storage_url=storage_url,
        tenant_id=int(tenant_id),
        kind=kind,
        dedup=dedup,
    )


def resolve_storage_path(
    *,
    tenant_id: int,
    sha256: str,
    ext: Optional[str] = None,
) -> Optional[Path]:
    """Locate a previously stored file for the inbound-media router.

    Tries the exact ``sha256<ext>`` first; if ``ext`` is unknown
    (legacy rows that didn't persist the extension) we fall back to
    globbing ``sha256.*`` across the tenant's monthly buckets.
    Returns ``None`` if the file isn't found — callers should respond
    with 404.
    """
    if not sha256:
        return None
    safe_sha = "".join(c for c in sha256 if c.isalnum())
    if safe_sha != sha256 or len(safe_sha) < 32:
        # Defense against path traversal: only allow hex-shaped sha256.
        return None
    tenant_dir = _STORAGE_ROOT / str(int(tenant_id))
    if not tenant_dir.exists():
        return None
    # Fast path — exact ext known.
    if ext:
        safe_ext = ext if ext.startswith(".") else f".{ext}"
        for monthly in sorted(tenant_dir.iterdir(), reverse=True):
            if not monthly.is_dir():
                continue
            candidate = monthly / f"{safe_sha}{safe_ext}"
            if candidate.exists():
                return candidate
    # Slow path — glob the tenant's monthly dirs.
    for monthly in sorted(tenant_dir.iterdir(), reverse=True):
        if not monthly.is_dir():
            continue
        matches = list(monthly.glob(f"{safe_sha}.*"))
        if matches:
            return matches[0]
    return None


def storage_root() -> Path:
    """Exposed for tests / debug endpoints that need to introspect the
    on-disk layout. NEVER use this for serving — go through
    ``resolve_storage_path`` so the tenant boundary is enforced."""
    return _STORAGE_ROOT

"""
routers/intelligence_libraries.py
──────────────────────────────────
Two independent merchant-curated libraries used by the AI brain:

* **Manual coupons** (``/intelligence/manual-coupons``) — coupon codes the
  brain may cite verbatim when the customer asks for a discount, without
  inventing or relying on the automatic coupon engine / Salla integration.

* **AI media library** (``/intelligence/ai-media``) — images, videos, PDFs,
  documents and audio the brain may attach to its WhatsApp reply (e.g.
  bank-transfer barcode, product photos, shipping policy graphic).

Both libraries are tenant-scoped, fully CRUD-able from the merchant
dashboard ("نحلة الذكية" tabs), and consumed by the brain through
``store_knowledge.build_merchant_context`` plus the WhatsApp media-send
helpers in ``core.whatsapp_media_send``.

Routes:
    GET    /intelligence/manual-coupons
    POST   /intelligence/manual-coupons
    PATCH  /intelligence/manual-coupons/{coupon_id}
    POST   /intelligence/manual-coupons/{coupon_id}/toggle
    DELETE /intelligence/manual-coupons/{coupon_id}

    GET    /intelligence/ai-media
    POST   /intelligence/ai-media                    (URL form)
    POST   /intelligence/ai-media/upload             (multipart)
    PATCH  /intelligence/ai-media/{media_id}
    POST   /intelligence/ai-media/{media_id}/toggle
    DELETE /intelligence/ai-media/{media_id}
    GET    /intelligence/ai-media/file/{media_id}    (signed-public stream)
"""
from __future__ import annotations

import logging
import mimetypes
import os
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional

from fastapi import (
    APIRouter,
    Depends,
    File,
    Form,
    HTTPException,
    Query,
    Request,
    UploadFile,
)
from fastapi.responses import FileResponse
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session

from core.database import get_db
from core.tenant import get_or_create_tenant, resolve_tenant_id
from models import AIMediaItem, ManualCoupon

router = APIRouter()
_logger = logging.getLogger("nahla-backend")


# ── Storage configuration ────────────────────────────────────────────────────
#
# Files uploaded via the multipart endpoint are stored on the local disk
# under ``<repo>/uploads/ai-media/<tenant_id>/<uuid>.<ext>`` and served
# back via the streaming endpoint below. The merchant can also paste an
# already-public URL, in which case nothing is stored locally.

_REPO_ROOT = Path(__file__).resolve().parents[2]
_UPLOAD_ROOT = Path(
    os.environ.get("NAHLA_AI_MEDIA_UPLOAD_DIR")
    or (_REPO_ROOT / "uploads" / "ai-media")
).resolve()


_ALLOWED_MEDIA_TYPES = {"image", "video", "pdf", "document", "audio"}

# Generous-but-bounded upload limit. WhatsApp Cloud API caps:
# image=5MB, video=16MB, document=100MB, audio=16MB. We pick the
# document ceiling so the merchant can upload a long catalog PDF.
_MAX_UPLOAD_BYTES = 100 * 1024 * 1024  # 100 MB


def _ensure_upload_dir(tenant_id: int) -> Path:
    target = _UPLOAD_ROOT / str(int(tenant_id))
    target.mkdir(parents=True, exist_ok=True)
    return target


def _public_file_url(request: Request, media_id: int) -> str:
    """Build the absolute public URL for a stored media row.

    Uses ``NAHLA_PUBLIC_BASE_URL`` if set, otherwise derives from the
    incoming request. WhatsApp Cloud requires HTTPS-accessible URLs.
    """
    base = (os.environ.get("NAHLA_PUBLIC_BASE_URL") or "").rstrip("/")
    if not base:
        base = str(request.base_url).rstrip("/")
    return f"{base}/intelligence/ai-media/file/{int(media_id)}"


# ── Pydantic schemas ─────────────────────────────────────────────────────────


class ManualCouponIn(BaseModel):
    code: str = Field(..., min_length=1, max_length=64)
    title: Optional[str] = Field(None, max_length=255)
    description: Optional[str] = None
    discount_text: Optional[str] = Field(None, max_length=255)
    usage_context: Optional[str] = None
    is_active: bool = True
    priority: int = Field(100, ge=0, le=10000)
    starts_at: Optional[datetime] = None
    expires_at: Optional[datetime] = None


class ManualCouponPatch(BaseModel):
    code: Optional[str] = Field(None, min_length=1, max_length=64)
    title: Optional[str] = Field(None, max_length=255)
    description: Optional[str] = None
    discount_text: Optional[str] = Field(None, max_length=255)
    usage_context: Optional[str] = None
    is_active: Optional[bool] = None
    priority: Optional[int] = Field(None, ge=0, le=10000)
    starts_at: Optional[datetime] = None
    expires_at: Optional[datetime] = None


class AIMediaIn(BaseModel):
    title: str = Field(..., min_length=1, max_length=255)
    description: Optional[str] = None
    media_type: str = Field("image", max_length=32)
    file_url: str = Field(..., min_length=1)
    thumbnail_url: Optional[str] = None
    usage_context: Optional[str] = None
    tags: List[str] = Field(default_factory=list)
    is_active: bool = True
    priority: int = Field(100, ge=0, le=10000)


class AIMediaPatch(BaseModel):
    title: Optional[str] = Field(None, max_length=255)
    description: Optional[str] = None
    media_type: Optional[str] = Field(None, max_length=32)
    file_url: Optional[str] = None
    thumbnail_url: Optional[str] = None
    usage_context: Optional[str] = None
    tags: Optional[List[str]] = None
    is_active: Optional[bool] = None
    priority: Optional[int] = Field(None, ge=0, le=10000)


# ── Serializers ──────────────────────────────────────────────────────────────


def _serialize_coupon(c: ManualCoupon) -> Dict[str, Any]:
    return {
        "id": int(c.id),
        "tenant_id": int(c.tenant_id),
        "code": c.code,
        "title": c.title,
        "description": c.description,
        "discount_text": c.discount_text,
        "usage_context": c.usage_context,
        "is_active": bool(c.is_active),
        "priority": int(c.priority or 0),
        "starts_at": c.starts_at.isoformat() if c.starts_at else None,
        "expires_at": c.expires_at.isoformat() if c.expires_at else None,
        "created_at": c.created_at.isoformat() if c.created_at else None,
        "updated_at": c.updated_at.isoformat() if c.updated_at else None,
    }


def _serialize_media(m: AIMediaItem, request: Optional[Request] = None) -> Dict[str, Any]:
    # If the file was uploaded locally, surface the absolute streaming URL
    # instead of the stored relative path so the dashboard can preview it.
    file_url = m.file_url
    if request is not None and m.storage_kind == "local":
        file_url = _public_file_url(request, int(m.id))
    return {
        "id": int(m.id),
        "tenant_id": int(m.tenant_id),
        "title": m.title,
        "description": m.description,
        "media_type": m.media_type,
        "file_url": file_url,
        "thumbnail_url": m.thumbnail_url,
        "usage_context": m.usage_context,
        "tags": list(m.tags or []),
        "is_active": bool(m.is_active),
        "priority": int(m.priority or 0),
        "storage_kind": m.storage_kind,
        "mime_type": m.mime_type,
        "file_size_bytes": int(m.file_size_bytes) if m.file_size_bytes is not None else None,
        "created_at": m.created_at.isoformat() if m.created_at else None,
        "updated_at": m.updated_at.isoformat() if m.updated_at else None,
    }


def _norm_media_type(raw: Optional[str]) -> str:
    t = (raw or "image").strip().lower()
    if t == "pdf":
        # Treat pdf as document at the WhatsApp-send layer but keep the
        # explicit label in the library so the UI can show a dedicated icon.
        return "pdf"
    if t not in _ALLOWED_MEDIA_TYPES:
        raise HTTPException(status_code=400, detail=f"invalid_media_type:{t}")
    return t


def _norm_tags(raw: Any) -> List[str]:
    if raw is None:
        return []
    if isinstance(raw, str):
        return [t.strip() for t in raw.split(",") if t.strip()]
    if isinstance(raw, list):
        return [str(t).strip() for t in raw if str(t).strip()]
    return []


# ── Manual coupon endpoints ──────────────────────────────────────────────────


@router.get("/intelligence/manual-coupons")
async def list_manual_coupons(
    request: Request,
    db: Session = Depends(get_db),
    only_active: bool = Query(False),
):
    tenant_id = resolve_tenant_id(request)
    get_or_create_tenant(db, tenant_id)

    q = db.query(ManualCoupon).filter(ManualCoupon.tenant_id == tenant_id)
    if only_active:
        q = q.filter(ManualCoupon.is_active.is_(True))
    rows = q.order_by(ManualCoupon.is_active.desc(), ManualCoupon.priority.asc(), ManualCoupon.id.desc()).all()
    return {"items": [_serialize_coupon(c) for c in rows]}


@router.post("/intelligence/manual-coupons", status_code=201)
async def create_manual_coupon(
    payload: ManualCouponIn,
    request: Request,
    db: Session = Depends(get_db),
):
    tenant_id = resolve_tenant_id(request)
    get_or_create_tenant(db, tenant_id)

    code = payload.code.strip()
    if not code:
        raise HTTPException(status_code=400, detail="code_required")

    existing = (
        db.query(ManualCoupon)
        .filter(ManualCoupon.tenant_id == tenant_id, ManualCoupon.code == code)
        .first()
    )
    if existing:
        raise HTTPException(status_code=409, detail="duplicate_code")

    row = ManualCoupon(
        tenant_id=tenant_id,
        code=code,
        title=payload.title,
        description=payload.description,
        discount_text=payload.discount_text,
        usage_context=payload.usage_context,
        is_active=bool(payload.is_active),
        priority=int(payload.priority),
        starts_at=payload.starts_at,
        expires_at=payload.expires_at,
    )
    db.add(row)
    db.commit()
    db.refresh(row)
    _logger.info("[ManualCoupon.create] tenant=%s id=%s code=%s", tenant_id, row.id, code)
    return _serialize_coupon(row)


@router.patch("/intelligence/manual-coupons/{coupon_id}")
async def update_manual_coupon(
    coupon_id: int,
    payload: ManualCouponPatch,
    request: Request,
    db: Session = Depends(get_db),
):
    tenant_id = resolve_tenant_id(request)
    row = (
        db.query(ManualCoupon)
        .filter(ManualCoupon.id == coupon_id, ManualCoupon.tenant_id == tenant_id)
        .first()
    )
    if not row:
        raise HTTPException(status_code=404, detail="not_found")

    data = payload.model_dump(exclude_unset=True)
    if "code" in data:
        new_code = (data["code"] or "").strip()
        if not new_code:
            raise HTTPException(status_code=400, detail="code_required")
        if new_code != row.code:
            dup = (
                db.query(ManualCoupon)
                .filter(
                    ManualCoupon.tenant_id == tenant_id,
                    ManualCoupon.code == new_code,
                    ManualCoupon.id != row.id,
                )
                .first()
            )
            if dup:
                raise HTTPException(status_code=409, detail="duplicate_code")
        data["code"] = new_code

    for key, value in data.items():
        setattr(row, key, value)
    db.commit()
    db.refresh(row)
    _logger.info("[ManualCoupon.update] tenant=%s id=%s fields=%s", tenant_id, row.id, list(data.keys()))
    return _serialize_coupon(row)


@router.post("/intelligence/manual-coupons/{coupon_id}/toggle")
async def toggle_manual_coupon(
    coupon_id: int,
    request: Request,
    db: Session = Depends(get_db),
):
    tenant_id = resolve_tenant_id(request)
    row = (
        db.query(ManualCoupon)
        .filter(ManualCoupon.id == coupon_id, ManualCoupon.tenant_id == tenant_id)
        .first()
    )
    if not row:
        raise HTTPException(status_code=404, detail="not_found")
    row.is_active = not bool(row.is_active)
    db.commit()
    db.refresh(row)
    _logger.info(
        "[ManualCoupon.toggle] tenant=%s id=%s is_active=%s",
        tenant_id, row.id, row.is_active,
    )
    return _serialize_coupon(row)


@router.delete("/intelligence/manual-coupons/{coupon_id}")
async def delete_manual_coupon(
    coupon_id: int,
    request: Request,
    db: Session = Depends(get_db),
):
    tenant_id = resolve_tenant_id(request)
    row = (
        db.query(ManualCoupon)
        .filter(ManualCoupon.id == coupon_id, ManualCoupon.tenant_id == tenant_id)
        .first()
    )
    if not row:
        raise HTTPException(status_code=404, detail="not_found")
    db.delete(row)
    db.commit()
    _logger.info("[ManualCoupon.delete] tenant=%s id=%s", tenant_id, coupon_id)
    return {"deleted": True, "id": int(coupon_id)}


# ── AI media library endpoints ───────────────────────────────────────────────


@router.get("/intelligence/ai-media")
async def list_ai_media(
    request: Request,
    db: Session = Depends(get_db),
    only_active: bool = Query(False),
    media_type: Optional[str] = Query(None),
):
    tenant_id = resolve_tenant_id(request)
    get_or_create_tenant(db, tenant_id)

    q = db.query(AIMediaItem).filter(AIMediaItem.tenant_id == tenant_id)
    if only_active:
        q = q.filter(AIMediaItem.is_active.is_(True))
    if media_type:
        q = q.filter(AIMediaItem.media_type == media_type.strip().lower())
    rows = q.order_by(AIMediaItem.is_active.desc(), AIMediaItem.priority.asc(), AIMediaItem.id.desc()).all()
    return {"items": [_serialize_media(r, request) for r in rows]}


@router.post("/intelligence/ai-media", status_code=201)
async def create_ai_media(
    payload: AIMediaIn,
    request: Request,
    db: Session = Depends(get_db),
):
    """Create a media row pointing at an externally-hosted URL."""
    tenant_id = resolve_tenant_id(request)
    get_or_create_tenant(db, tenant_id)

    url = (payload.file_url or "").strip()
    if not url:
        raise HTTPException(status_code=400, detail="file_url_required")
    if not (url.startswith("http://") or url.startswith("https://")):
        raise HTTPException(status_code=400, detail="file_url_must_be_http_https")

    row = AIMediaItem(
        tenant_id=tenant_id,
        title=payload.title.strip(),
        description=payload.description,
        media_type=_norm_media_type(payload.media_type),
        file_url=url,
        thumbnail_url=(payload.thumbnail_url or "").strip() or None,
        usage_context=payload.usage_context,
        tags=_norm_tags(payload.tags),
        is_active=bool(payload.is_active),
        priority=int(payload.priority),
        storage_kind="external",
    )
    db.add(row)
    db.commit()
    db.refresh(row)
    _logger.info(
        "[AIMedia.create] tenant=%s id=%s type=%s storage=external",
        tenant_id, row.id, row.media_type,
    )
    return _serialize_media(row, request)


@router.post("/intelligence/ai-media/upload", status_code=201)
async def upload_ai_media(
    request: Request,
    file: UploadFile = File(...),
    title: str = Form(...),
    description: Optional[str] = Form(None),
    media_type: str = Form("image"),
    usage_context: Optional[str] = Form(None),
    tags: Optional[str] = Form(None),
    priority: int = Form(100),
    is_active: bool = Form(True),
    db: Session = Depends(get_db),
):
    """Upload a binary file (multipart) to local storage and register it.

    The merchant pastes an already-hosted URL via :func:`create_ai_media`
    when they have one; this endpoint covers the common case where the
    merchant just wants to drag a file out of their phone.
    """
    tenant_id = resolve_tenant_id(request)
    get_or_create_tenant(db, tenant_id)

    norm_type = _norm_media_type(media_type)

    try:
        content = await file.read()
    except Exception as exc:
        raise HTTPException(status_code=400, detail=f"upload_read_failed:{exc}")
    if not content:
        raise HTTPException(status_code=400, detail="empty_file")
    if len(content) > _MAX_UPLOAD_BYTES:
        raise HTTPException(status_code=413, detail="file_too_large")

    original_name = file.filename or "media"
    suffix = Path(original_name).suffix.lower() or ""
    if not suffix:
        guessed = mimetypes.guess_extension(file.content_type or "") or ""
        suffix = guessed or ""
    fname = f"{uuid.uuid4().hex}{suffix}"
    target_dir = _ensure_upload_dir(tenant_id)
    storage_path = target_dir / fname
    try:
        storage_path.write_bytes(content)
    except OSError as exc:
        raise HTTPException(status_code=500, detail=f"upload_write_failed:{exc}")

    row = AIMediaItem(
        tenant_id=tenant_id,
        title=(title or "").strip() or original_name,
        description=description,
        media_type=norm_type,
        file_url="",  # filled in after we know the row id (below)
        thumbnail_url=None,
        usage_context=usage_context,
        tags=_norm_tags(tags),
        is_active=bool(is_active),
        priority=int(priority),
        storage_kind="local",
        storage_path=str(storage_path),
        mime_type=file.content_type or mimetypes.guess_type(original_name)[0],
        file_size_bytes=len(content),
    )
    db.add(row)
    db.flush()
    row.file_url = _public_file_url(request, int(row.id))
    db.commit()
    db.refresh(row)
    _logger.info(
        "[AIMedia.upload] tenant=%s id=%s type=%s bytes=%d path=%s",
        tenant_id, row.id, norm_type, len(content), storage_path.name,
    )
    return _serialize_media(row, request)


@router.patch("/intelligence/ai-media/{media_id}")
async def update_ai_media(
    media_id: int,
    payload: AIMediaPatch,
    request: Request,
    db: Session = Depends(get_db),
):
    tenant_id = resolve_tenant_id(request)
    row = (
        db.query(AIMediaItem)
        .filter(AIMediaItem.id == media_id, AIMediaItem.tenant_id == tenant_id)
        .first()
    )
    if not row:
        raise HTTPException(status_code=404, detail="not_found")

    data = payload.model_dump(exclude_unset=True)
    if "media_type" in data and data["media_type"]:
        data["media_type"] = _norm_media_type(data["media_type"])
    if "tags" in data:
        data["tags"] = _norm_tags(data["tags"])
    if "file_url" in data and data["file_url"]:
        url = data["file_url"].strip()
        if not (url.startswith("http://") or url.startswith("https://")):
            raise HTTPException(status_code=400, detail="file_url_must_be_http_https")
        data["file_url"] = url
        # If the merchant overrides the URL of a locally-uploaded row,
        # treat it as external to stop streaming the on-disk file.
        if row.storage_kind == "local":
            data["storage_kind"] = "external"

    for key, value in data.items():
        setattr(row, key, value)
    db.commit()
    db.refresh(row)
    _logger.info("[AIMedia.update] tenant=%s id=%s fields=%s", tenant_id, row.id, list(data.keys()))
    return _serialize_media(row, request)


@router.post("/intelligence/ai-media/{media_id}/toggle")
async def toggle_ai_media(
    media_id: int,
    request: Request,
    db: Session = Depends(get_db),
):
    tenant_id = resolve_tenant_id(request)
    row = (
        db.query(AIMediaItem)
        .filter(AIMediaItem.id == media_id, AIMediaItem.tenant_id == tenant_id)
        .first()
    )
    if not row:
        raise HTTPException(status_code=404, detail="not_found")
    row.is_active = not bool(row.is_active)
    db.commit()
    db.refresh(row)
    _logger.info(
        "[AIMedia.toggle] tenant=%s id=%s is_active=%s",
        tenant_id, row.id, row.is_active,
    )
    return _serialize_media(row, request)


@router.delete("/intelligence/ai-media/{media_id}")
async def delete_ai_media(
    media_id: int,
    request: Request,
    db: Session = Depends(get_db),
):
    tenant_id = resolve_tenant_id(request)
    row = (
        db.query(AIMediaItem)
        .filter(AIMediaItem.id == media_id, AIMediaItem.tenant_id == tenant_id)
        .first()
    )
    if not row:
        raise HTTPException(status_code=404, detail="not_found")

    if row.storage_kind == "local" and row.storage_path:
        # Best-effort cleanup; never block deletion on a missing/locked file.
        try:
            p = Path(row.storage_path)
            if p.exists() and p.is_file():
                p.unlink()
        except OSError as exc:
            _logger.warning("[AIMedia.delete] failed to remove file id=%s err=%s", media_id, exc)

    db.delete(row)
    db.commit()
    _logger.info("[AIMedia.delete] tenant=%s id=%s", tenant_id, media_id)
    return {"deleted": True, "id": int(media_id)}


# Streaming endpoint — left tenant-agnostic on purpose so WhatsApp Cloud
# (which is anonymous) can fetch the asset; access is gated by the
# unguessable integer id and the row's ``is_active`` flag is intentionally
# ignored so the brain can still send media that's been disabled in the
# library mid-conversation.
@router.get("/intelligence/ai-media/file/{media_id}")
async def stream_ai_media(media_id: int, db: Session = Depends(get_db)):
    row = db.query(AIMediaItem).filter(AIMediaItem.id == media_id).first()
    if not row or row.storage_kind != "local" or not row.storage_path:
        raise HTTPException(status_code=404, detail="not_found")
    p = Path(row.storage_path)
    if not p.exists() or not p.is_file():
        raise HTTPException(status_code=404, detail="file_missing")
    media_type = row.mime_type or mimetypes.guess_type(p.name)[0] or "application/octet-stream"
    return FileResponse(
        path=str(p),
        media_type=media_type,
        filename=p.name,
    )

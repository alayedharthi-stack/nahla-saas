"""
routers/knowledge.py
────────────────────
Smart Store Knowledge Hub — Phase 1 endpoints.

Exposes CRUD over the new ``merchant_knowledge_sections`` +
``merchant_knowledge_media`` tables (see migration 0067) and a
one-shot import endpoint that lifts the legacy
``ai_settings.manual_knowledge_base`` blob into structured sections
on first use of the redesigned dashboard page.

Routes:
    GET    /knowledge/section-kinds
    GET    /knowledge/sections
    POST   /knowledge/sections
    PATCH  /knowledge/sections/{section_id}
    POST   /knowledge/sections/{section_id}/toggle
    DELETE /knowledge/sections/{section_id}

    POST   /knowledge/sections/{section_id}/media
    DELETE /knowledge/sections/{section_id}/media/{link_id}

    POST   /knowledge/sections/migrate-from-legacy
    GET    /knowledge/legacy-knowledge-base

Phase 2 (drafts + GPT classifier) and Phase 3 (product linking) extend
this router additively — no breaking changes.
"""
from __future__ import annotations

import logging
import re
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from pydantic import BaseModel, Field
import sqlalchemy as sa
from sqlalchemy.orm import Session

from core.database import get_db
from core.tenant import (
    get_or_create_settings,
    get_or_create_tenant,
    merge_ai_defaults,
    resolve_tenant_id,
)
from models import (
    AIMediaItem,
    MerchantKnowledgeDraft,
    MerchantKnowledgeMedia,
    MerchantKnowledgeSection,
    MerchantKnowledgeSectionProduct,
    Product,
)
from services.knowledge_section_kinds import (
    ALLOWED_LINK_ROLES,
    GROUP_LABELS_AR,
    all_kinds,
    get_kind,
    group_for,
    is_valid_kind,
    is_valid_link_role,
)

router = APIRouter()
_logger = logging.getLogger("nahla-backend")


# ── Pydantic schemas ────────────────────────────────────────────────────────


class SectionIn(BaseModel):
    kind: str = Field(..., min_length=1, max_length=64)
    title: Optional[str] = Field(None, max_length=255)
    body: str = Field("", max_length=8000)
    metadata_json: Optional[Dict[str, Any]] = None
    priority: int = Field(100, ge=0, le=10000)
    is_active: bool = True


class SectionPatch(BaseModel):
    kind: Optional[str] = Field(None, min_length=1, max_length=64)
    title: Optional[str] = Field(None, max_length=255)
    body: Optional[str] = Field(None, max_length=8000)
    metadata_json: Optional[Dict[str, Any]] = None
    priority: Optional[int] = Field(None, ge=0, le=10000)
    is_active: Optional[bool] = None


class MediaLinkIn(BaseModel):
    media_id: int = Field(..., gt=0)
    link_role: str = Field("primary", max_length=32)


# ── Internal helpers (auto-link + serializers) ──────────────────────────────


def _maybe_autolink_payment_media_key(
    db: Session,
    tenant_id: int,
    media: AIMediaItem,
    section: MerchantKnowledgeSection,
    link_role: str,
    *,
    strict_role: bool = True,
) -> Optional[str]:
    """Best-effort: bind ``media.media_key`` to the canonical
    payment registry slug when the merchant just linked a
    barcode-role asset into a payment section.

    See ``services/payment_media_autolink.py`` for the full
    rationale. We keep the call site defensive so a future change
    to the registry / autolink module never crashes a legitimate
    "attach image" click.

    ``strict_role`` mirrors the inferrer's flag:
      * ``True`` (default) — the live ``link_media`` path. We only
        bind on ``link_role='barcode'`` to avoid mistaking a
        tutorial video for a payment QR.
      * ``False`` — the *backfill* path
        (``/knowledge/media/backfill-payment-keys``). The merchant
        already hand-linked this asset; we just need to fill in the
        ``media_key`` column their old link pre-dates. Other guards
        (section_kind, single-bank match) still apply.

    Returns the inferred key (or ``None`` when no change was made).
    The caller MUST still commit / flush the surrounding
    transaction — this helper only mutates the SQLAlchemy
    instance and stages the write.
    """
    # Skip when the merchant (or a prior auto-link) already pinned
    # a key. Never overwrite an explicit choice — applies to both
    # the live path AND backfill.
    if (getattr(media, "media_key", None) or "").strip():
        return None

    try:
        from services.payment_media_autolink import (  # noqa: PLC0415
            detect_payment_media_key,
        )
    except Exception as exc:  # pragma: no cover — import-time defense
        _logger.warning(
            "[KB.media.autolink] tenant=%s import_failed err=%s",
            tenant_id, exc,
        )
        return None

    try:
        inferred = detect_payment_media_key(
            section_kind=section.kind or "",
            section_title=section.title or "",
            section_body=section.body or "",
            media_title=getattr(media, "title", "") or "",
            link_role=link_role,
            strict_role=strict_role,
        )
    except Exception as exc:  # pragma: no cover — defensive
        _logger.warning(
            "[KB.media.autolink] tenant=%s detect_failed err=%s",
            tenant_id, exc,
        )
        return None

    if not inferred:
        return None

    media.media_key = inferred
    db.add(media)
    _logger.info(
        "[KB.media.autolink] tenant=%s media=%s section=%s "
        "section_kind=%s role=%s strict_role=%s → media_key=%s",
        tenant_id, media.id, section.id,
        section.kind, link_role, strict_role, inferred,
    )
    return inferred


def _serialize_media_link(link: MerchantKnowledgeMedia) -> Dict[str, Any]:
    media = link.media
    return {
        "id": int(link.id),
        "section_id": int(link.section_id),
        "media_id": int(link.media_id),
        "link_role": link.link_role,
        "created_at": link.created_at.isoformat() if link.created_at else None,
        # Surface the bare minimum from the linked AIMediaItem so the
        # dashboard can render a thumbnail strip without a second
        # round-trip. Guarded for the (very rare) case of a dangling
        # link — the FK has ON DELETE CASCADE so this should never
        # trigger in practice.
        "media": (
            {
                "id": int(media.id),
                "title": media.title,
                "media_type": media.media_type,
                "file_url": media.file_url,
                "thumbnail_url": media.thumbnail_url,
                "media_key": media.media_key,
                "is_active": bool(media.is_active),
            }
            if media is not None
            else None
        ),
    }


def _serialize_product_link(link: "MerchantKnowledgeSectionProduct") -> Dict[str, Any]:
    # Use ``getattr`` so we don't trigger an extra SELECT when the
    # relationship wasn't preloaded — None is fine, the dashboard
    # shows the product_id as a placeholder.
    product = None
    p = getattr(link, "product", None)
    if p is not None:
        product = {
            "id": int(p.id),
            "title": p.title,
            "external_id": p.external_id,
            "sku": p.sku,
            "in_stock": bool(p.in_stock),
        }
    return {
        "id": int(link.id),
        "product_id": int(link.product_id),
        "source": link.source,
        "confidence": float(link.confidence) if link.confidence is not None else None,
        "created_at": link.created_at.isoformat() if link.created_at else None,
        "product": product,
    }


def _serialize_section(section: MerchantKnowledgeSection) -> Dict[str, Any]:
    media_links = sorted(
        section.media_links or [],
        key=lambda lk: (0 if lk.link_role == "primary" else 1, lk.id),
    )
    product_links = sorted(
        getattr(section, "product_links", None) or [],
        key=lambda lk: lk.id,
    )
    return {
        "id": int(section.id),
        "tenant_id": int(section.tenant_id),
        "kind": section.kind,
        "group": group_for(section.kind),
        "title": section.title,
        "body": section.body or "",
        "metadata_json": section.metadata_json,
        "priority": int(section.priority or 0),
        "is_active": bool(section.is_active),
        "source": section.source,
        "ai_status": section.ai_status,
        "classification_confidence": section.classification_confidence,
        "conflicts_json": section.conflicts_json,
        "created_at": section.created_at.isoformat() if section.created_at else None,
        "updated_at": section.updated_at.isoformat() if section.updated_at else None,
        "media_links": [_serialize_media_link(lk) for lk in media_links],
        "product_links": [_serialize_product_link(lk) for lk in product_links],
    }


# ── Section-kinds registry ──────────────────────────────────────────────────


@router.get("/knowledge/section-kinds")
async def list_section_kinds() -> Dict[str, Any]:
    """Return the canonical registry the dashboard uses to render groups
    and the dropdown of "kind" choices when the merchant adds a section.

    Pure read-only — no tenant scoping needed (the registry is global).
    """
    return {
        "groups": [
            {"id": gid, "label_ar": GROUP_LABELS_AR[gid]}
            for gid in sorted(GROUP_LABELS_AR)
        ],
        "kinds": [
            {
                "kind": sk.kind,
                "group": sk.group,
                "label_ar": sk.label_ar,
                "placeholder_ar": sk.placeholder_ar,
                "is_product_bound": sk.is_product_bound,
            }
            for sk in all_kinds()
        ],
        "link_roles": list(ALLOWED_LINK_ROLES),
    }


# ── Section CRUD ────────────────────────────────────────────────────────────


@router.get("/knowledge/sections")
async def list_sections(
    request: Request,
    db: Session = Depends(get_db),
    only_active: bool = Query(False),
    kind: Optional[str] = Query(None),
    group: Optional[int] = Query(None, ge=1, le=6),
):
    tenant_id = resolve_tenant_id(request)
    get_or_create_tenant(db, tenant_id)

    q = db.query(MerchantKnowledgeSection).filter(
        MerchantKnowledgeSection.tenant_id == tenant_id,
    )
    if only_active:
        q = q.filter(MerchantKnowledgeSection.is_active.is_(True))
    if kind:
        q = q.filter(MerchantKnowledgeSection.kind == kind.strip().lower())
    rows = q.order_by(
        MerchantKnowledgeSection.is_active.desc(),
        MerchantKnowledgeSection.priority.asc(),
        MerchantKnowledgeSection.updated_at.desc(),
    ).all()

    if group is not None:
        rows = [r for r in rows if group_for(r.kind) == group]

    return {"items": [_serialize_section(r) for r in rows]}


@router.post("/knowledge/sections", status_code=201)
async def create_section(
    payload: SectionIn,
    request: Request,
    db: Session = Depends(get_db),
):
    tenant_id = resolve_tenant_id(request)
    get_or_create_tenant(db, tenant_id)

    kind = (payload.kind or "").strip().lower()
    if not is_valid_kind(kind):
        raise HTTPException(status_code=400, detail=f"invalid_kind:{kind}")

    row = MerchantKnowledgeSection(
        tenant_id=tenant_id,
        kind=kind,
        title=(payload.title or None),
        body=(payload.body or "").strip(),
        metadata_json=payload.metadata_json,
        priority=int(payload.priority),
        is_active=bool(payload.is_active),
        source="manual",
        ai_status="approved",
    )
    db.add(row)
    db.commit()
    db.refresh(row)
    _logger.info(
        "[KB.section.create] tenant=%s id=%s kind=%s",
        tenant_id, row.id, kind,
    )
    return _serialize_section(row)


@router.patch("/knowledge/sections/{section_id}")
async def update_section(
    section_id: int,
    payload: SectionPatch,
    request: Request,
    db: Session = Depends(get_db),
):
    tenant_id = resolve_tenant_id(request)
    row = (
        db.query(MerchantKnowledgeSection)
        .filter(
            MerchantKnowledgeSection.id == section_id,
            MerchantKnowledgeSection.tenant_id == tenant_id,
        )
        .first()
    )
    if not row:
        raise HTTPException(status_code=404, detail="not_found")

    data = payload.model_dump(exclude_unset=True)
    if "kind" in data and data["kind"]:
        kind = data["kind"].strip().lower()
        if not is_valid_kind(kind):
            raise HTTPException(status_code=400, detail=f"invalid_kind:{kind}")
        data["kind"] = kind
    if "body" in data and data["body"] is not None:
        data["body"] = (data["body"] or "").strip()
    if "title" in data and data["title"] is not None:
        data["title"] = (data["title"] or "").strip() or None

    for key, value in data.items():
        setattr(row, key, value)
    db.commit()
    db.refresh(row)
    _logger.info(
        "[KB.section.update] tenant=%s id=%s fields=%s",
        tenant_id, row.id, list(data.keys()),
    )
    return _serialize_section(row)


@router.post("/knowledge/sections/{section_id}/toggle")
async def toggle_section(
    section_id: int,
    request: Request,
    db: Session = Depends(get_db),
):
    tenant_id = resolve_tenant_id(request)
    row = (
        db.query(MerchantKnowledgeSection)
        .filter(
            MerchantKnowledgeSection.id == section_id,
            MerchantKnowledgeSection.tenant_id == tenant_id,
        )
        .first()
    )
    if not row:
        raise HTTPException(status_code=404, detail="not_found")
    row.is_active = not bool(row.is_active)
    db.commit()
    db.refresh(row)
    _logger.info(
        "[KB.section.toggle] tenant=%s id=%s is_active=%s",
        tenant_id, row.id, row.is_active,
    )
    return _serialize_section(row)


@router.delete("/knowledge/sections/{section_id}")
async def delete_section(
    section_id: int,
    request: Request,
    db: Session = Depends(get_db),
):
    tenant_id = resolve_tenant_id(request)
    row = (
        db.query(MerchantKnowledgeSection)
        .filter(
            MerchantKnowledgeSection.id == section_id,
            MerchantKnowledgeSection.tenant_id == tenant_id,
        )
        .first()
    )
    if not row:
        raise HTTPException(status_code=404, detail="not_found")
    db.delete(row)
    db.commit()
    _logger.info("[KB.section.delete] tenant=%s id=%s", tenant_id, section_id)
    return {"deleted": True, "id": int(section_id)}


# ── Media linking ───────────────────────────────────────────────────────────


@router.post("/knowledge/sections/{section_id}/media", status_code=201)
async def link_media(
    section_id: int,
    payload: MediaLinkIn,
    request: Request,
    db: Session = Depends(get_db),
):
    tenant_id = resolve_tenant_id(request)

    section = (
        db.query(MerchantKnowledgeSection)
        .filter(
            MerchantKnowledgeSection.id == section_id,
            MerchantKnowledgeSection.tenant_id == tenant_id,
        )
        .first()
    )
    if not section:
        raise HTTPException(status_code=404, detail="section_not_found")

    media = (
        db.query(AIMediaItem)
        .filter(
            AIMediaItem.id == payload.media_id,
            AIMediaItem.tenant_id == tenant_id,
        )
        .first()
    )
    if not media:
        raise HTTPException(status_code=404, detail="media_not_found")

    role = (payload.link_role or "primary").strip().lower()
    if not is_valid_link_role(role):
        raise HTTPException(status_code=400, detail=f"invalid_link_role:{role}")

    # Idempotent: unique (section_id, media_id, link_role) — return the
    # existing link if the merchant clicks "attach" twice.
    existing = (
        db.query(MerchantKnowledgeMedia)
        .filter(
            MerchantKnowledgeMedia.section_id == section.id,
            MerchantKnowledgeMedia.media_id == media.id,
            MerchantKnowledgeMedia.link_role == role,
        )
        .first()
    )
    if existing:
        return _serialize_media_link(existing)

    link = MerchantKnowledgeMedia(
        section_id=section.id,
        media_id=media.id,
        link_role=role,
    )
    db.add(link)
    # Auto-bind ``AIMediaItem.media_key`` BEFORE the commit so the
    # whole link operation (link row + media_key update) lands in a
    # single transaction. If the auto-link doesn't fire (wrong kind,
    # wrong role, ambiguous text) the call is a no-op and the
    # commit just persists the link row.
    _maybe_autolink_payment_media_key(db, tenant_id, media, section, role)
    db.commit()
    db.refresh(link)
    _logger.info(
        "[KB.media.link] tenant=%s section=%s media=%s role=%s",
        tenant_id, section.id, media.id, role,
    )
    return _serialize_media_link(link)


@router.delete("/knowledge/sections/{section_id}/media/{link_id}")
async def unlink_media(
    section_id: int,
    link_id: int,
    request: Request,
    db: Session = Depends(get_db),
):
    tenant_id = resolve_tenant_id(request)

    link = (
        db.query(MerchantKnowledgeMedia)
        .join(
            MerchantKnowledgeSection,
            MerchantKnowledgeSection.id == MerchantKnowledgeMedia.section_id,
        )
        .filter(
            MerchantKnowledgeMedia.id == link_id,
            MerchantKnowledgeMedia.section_id == section_id,
            MerchantKnowledgeSection.tenant_id == tenant_id,
        )
        .first()
    )
    if not link:
        raise HTTPException(status_code=404, detail="link_not_found")

    db.delete(link)
    db.commit()
    _logger.info(
        "[KB.media.unlink] tenant=%s section=%s link=%s",
        tenant_id, section_id, link_id,
    )
    return {"deleted": True, "id": int(link_id)}


# ── Backfill: heal legacy media links with NULL media_key ──────────────────
#
# Why this endpoint exists
# ────────────────────────
# The auto-link helper (``_maybe_autolink_payment_media_key``) runs at
# *link-creation* time. Merchants who linked their payment QR BEFORE
# the May 22 2026 deploy (commit c9e65218) have ``media_key=NULL`` on
# the underlying ``AIMediaItem`` row, which leaves the runtime safety
# net unable to resolve ``[MEDIA_KEY:payment_rajhi_barcode]`` → no QR
# attached.
#
# The Tenant 33 incident on May 22 surfaced exactly this:
#   * media id=1 "باركود التحويل البنكي الراجحي" (media_key=NULL)
#   * linked into a giant "all banks" payment section with
#     link_role='primary' (pre-deploy default).
#   * Auto-link's live path (strict_role=True, single-bank section)
#     refuses to bind on its own — by design, to avoid mis-binding.
#
# This endpoint lets the merchant (or platform admin) heal those
# rows in one shot, tenant-scoped, idempotent, never-overwrite. The
# call is safe to retry: the helper short-circuits on
# already-bound rows.
@router.post("/knowledge/media/backfill-payment-keys")
async def backfill_payment_media_keys(
    request: Request,
    db: Session = Depends(get_db),
) -> Dict[str, Any]:
    """Re-run the payment-media inferrer against the current tenant's
    existing links and fill in ``AIMediaItem.media_key`` where it was
    left NULL.

    Returns
    -------
    JSON with the breakdown so the dashboard can show a small
    "healed N links" toast:

        {
          "tenant_id": 33,
          "scanned": 4,        # links touched at all
          "already_set": 2,    # had media_key — left untouched
          "healed": 1,         # NULL → inferred key
          "ambiguous": 1,      # couldn't infer (multi-bank / no match)
          "details": [         # per-row reasoning (debug-friendly)
            {"link_id": 1, "media_id": 1, "section_id": 76,
             "old_key": null, "new_key": "payment_rajhi_barcode",
             "reason": "healed"},
            ...
          ]
        }

    Semantics
    ─────────
    * Tenant-scoped — only operates on rows whose section.tenant_id
      matches the caller. There is NO platform-admin override; if
      another merchant has the same issue, they call the same
      endpoint themselves.
    * Strict-role bypass — the inferrer is called with
      ``strict_role=False`` so links created with the legacy
      default ``link_role='primary'`` qualify. Other guards
      (section_kind, single-bank match in media_title) still
      apply, so we cannot mis-bind a tutorial video to a payment
      slug.
    * Idempotent — re-running is a no-op (every row is now
      ``already_set``).
    * Never overwrites — if the merchant manually pinned a key (or
      an earlier autolink already did), we leave it alone even
      when the inferrer would have picked a different bank.
    """
    tenant_id = resolve_tenant_id(request)

    # We need all payment-section links for this tenant, with the
    # joined section + media rows loaded so the helper can sniff
    # the bank pattern without firing N+1 selects. The orderby
    # keeps the response deterministic for the dashboard / tests.
    links = (
        db.query(MerchantKnowledgeMedia)
        .join(
            MerchantKnowledgeSection,
            MerchantKnowledgeSection.id == MerchantKnowledgeMedia.section_id,
        )
        .join(
            AIMediaItem,
            AIMediaItem.id == MerchantKnowledgeMedia.media_id,
        )
        .filter(
            MerchantKnowledgeSection.tenant_id == tenant_id,
            # Section must be a payment kind — the inferrer would
            # bail otherwise. Filtering early saves a per-row
            # detect call on unrelated sections.
            MerchantKnowledgeSection.kind.in_(("payment_method", "bank_transfer")),
        )
        .order_by(MerchantKnowledgeMedia.id.asc())
        .all()
    )

    scanned = 0
    already_set = 0
    healed = 0
    ambiguous = 0
    details: List[Dict[str, Any]] = []

    for link in links:
        scanned += 1
        media = link.media
        section = link.section
        if media is None or section is None:
            # Dangling link (FK cascade should prevent this — but
            # be defensive so a corrupt row doesn't 500 the
            # entire backfill).
            ambiguous += 1
            details.append({
                "link_id": int(link.id),
                "media_id": getattr(link, "media_id", None),
                "section_id": getattr(link, "section_id", None),
                "old_key": None,
                "new_key": None,
                "reason": "dangling_link",
            })
            continue

        old_key = (media.media_key or "").strip() or None
        if old_key:
            already_set += 1
            details.append({
                "link_id": int(link.id),
                "media_id": int(media.id),
                "section_id": int(section.id),
                "old_key": old_key,
                "new_key": old_key,
                "reason": "already_set",
            })
            continue

        new_key = _maybe_autolink_payment_media_key(
            db, tenant_id, media, section, link.link_role or "",
            strict_role=False,
        )

        if new_key:
            healed += 1
            reason = "healed"
        else:
            ambiguous += 1
            reason = "ambiguous"

        details.append({
            "link_id": int(link.id),
            "media_id": int(media.id),
            "section_id": int(section.id),
            "old_key": None,
            "new_key": new_key,
            "reason": reason,
        })

    if healed > 0:
        db.commit()
    else:
        # Nothing changed → no need to spend a commit; rollback
        # any pending no-op staged writes.
        db.rollback()

    _logger.info(
        "[KB.media.backfill] tenant=%s scanned=%s already=%s "
        "healed=%s ambiguous=%s",
        tenant_id, scanned, already_set, healed, ambiguous,
    )

    return {
        "tenant_id": int(tenant_id),
        "scanned": scanned,
        "already_set": already_set,
        "healed": healed,
        "ambiguous": ambiguous,
        "details": details,
    }


# ── Legacy import ───────────────────────────────────────────────────────────
#
# Heuristic split of the legacy ``manual_knowledge_base`` text blob into
# structured sections. We don't try to be smart — the Phase 2 GPT
# classifier handles the messy cases. This endpoint just does a "best
# effort" pass on common Arabic headings so the merchant doesn't have
# to copy-paste their old text into the new editor.
#
# Strategy:
#   1. Split the blob on Arabic heading lines (``# الشحن`` / ``الشحن:`` /
#      ``— الدفع —``).
#   2. Map each heading to a canonical ``kind`` via a keyword table.
#   3. If we find at least one mappable heading, create one section per
#      block. Otherwise we create a single ``custom`` section with the
#      whole text — the merchant can split it later from the UI.
#   4. We never delete the legacy field by default (``clear_legacy=false``).
#      The dashboard surfaces a separate "حذف النص القديم" button after
#      the merchant has confirmed the imported sections look right.

# A heading-like line is one that EITHER:
#   * starts with markdown ``#`` (one or more),
#   * is wrapped/prefixed by Arabic title markers (``—``, ``=``, ``*``),
#   * OR is short (≤ 40 chars after trimming) and ends with ``:``.
# Plain sentences that happen to mention a keyword (e.g. ``الشحن
# المجاني للطلبات فوق 200 ريال``) are NOT headings and must remain
# part of the previous section's body. We enforce that by requiring an
# explicit marker — never by keyword presence alone.
_HEADING_PREFIX_RE = re.compile(r"^[\s]*(#+|[-*•—–=]{1,3})\s+(.{1,80})$")
_HEADING_COLON_RE = re.compile(r"^[\s]*(.{1,40}):\s*$")

# Keyword → kind. The first match wins, so order from most specific
# to least specific. All comparisons are done on the lowercased,
# diacritics-stripped heading.
_HEADING_KEYWORDS: List[tuple[str, str]] = [
    ("التحويل البنك", "bank_transfer"),
    ("تحويل بنك",     "bank_transfer"),
    ("الدفع عند",     "cod"),
    ("كاش",           "cod"),
    ("طرق الدفع",     "payment_method"),
    ("الدفع",         "payment_method"),
    ("الاسترجاع",     "return_policy"),
    ("الاستبدال",     "return_policy"),
    ("الإرجاع",       "return_policy"),
    ("الإرجاع",       "return_policy"),
    ("الضمان",        "warranty"),
    ("الشحن المبرد",  "cold_shipping"),
    ("التبريد",       "cold_shipping"),
    ("شركات الشحن",   "shipping_carrier"),
    ("شركة الشحن",    "shipping_carrier"),
    ("مدة التوصيل",   "shipping_zones"),
    ("المناطق",       "shipping_zones"),
    ("الشحن",         "shipping_zones"),
    ("الصيف",         "summer_note"),
    ("اللهجة",        "dialect"),
    ("أسلوب",         "reply_style"),
    ("الرد",          "reply_style"),
    ("أوقات",         "working_hours"),
    ("الدوام",        "working_hours"),
    ("الفروع",        "branches"),
    ("الفرع",         "branches"),
    ("القصة",         "store_story"),
    ("نبذة",          "store_story"),
    ("طريقة الاستخدام", "product_usage"),
    ("الاستخدام",     "product_usage"),
    ("وصفة",          "product_recipe"),
    ("وصفات",         "product_recipe"),
    ("التخزين",       "product_storage"),
    ("الحفظ",         "product_storage"),
    ("الفوائد",       "product_benefit"),
    ("الفرق",         "product_compare"),
    ("الفروقات",      "product_compare"),
    ("سؤال",          "faq"),
    ("الأسئلة",       "faq"),
    ("FAQ",           "faq"),
]


def _normalize_ar(text: str) -> str:
    """Lowercase + strip Arabic diacritics for keyword matching."""
    if not text:
        return ""
    # Strip common Arabic diacritics (tashkeel) and tatweel.
    text = re.sub(r"[\u0610-\u061A\u064B-\u065F\u0670\u06D6-\u06ED\u0640]", "", text)
    return text.strip().lower()


def _classify_heading(heading: str) -> Optional[str]:
    h = _normalize_ar(heading)
    if not h:
        return None
    for kw, kind in _HEADING_KEYWORDS:
        if _normalize_ar(kw) in h:
            return kind
    return None


def _split_legacy_text(text: str) -> List[Dict[str, str]]:
    """Best-effort split on heading-like lines. Returns ``[{"kind", "title", "body"}, …]``.

    A "heading" is any short non-empty line (≤ 80 chars after trimming
    list markers) that maps to a known kind via ``_classify_heading``.
    Everything between two headings is the body of the first heading.
    Text before the first heading goes into a single ``custom`` section
    so it isn't lost.
    """
    if not text or not text.strip():
        return []

    blocks: List[Dict[str, str]] = []
    current: Optional[Dict[str, str]] = None
    leading: List[str] = []

    for raw_line in text.splitlines():
        line = raw_line.rstrip()
        if not line.strip():
            if current is not None:
                current["body"] += "\n"
            else:
                leading.append("")
            continue

        # Try to interpret this line as a heading. We REQUIRE an
        # explicit marker — keyword presence alone is not enough,
        # otherwise sentences like "الشحن المجاني للطلبات فوق 200
        # ريال" would split themselves out of the shipping body.
        stripped = line.strip()
        candidate: Optional[str] = None
        m_prefix = _HEADING_PREFIX_RE.match(stripped)
        m_colon = _HEADING_COLON_RE.match(stripped)
        if m_prefix:
            candidate = m_prefix.group(2).strip().rstrip(":")
        elif m_colon and ":" not in m_colon.group(1):
            candidate = m_colon.group(1).strip()

        kind = _classify_heading(candidate) if candidate else None

        if kind is not None:
            if current is not None:
                blocks.append(current)
            current = {"kind": kind, "title": candidate, "body": ""}
        else:
            if current is None:
                leading.append(line)
            else:
                current["body"] += line + "\n"

    if current is not None:
        blocks.append(current)

    leading_text = "\n".join(leading).strip()
    if leading_text:
        blocks.insert(0, {"kind": "custom", "title": "ملاحظات عامة", "body": leading_text})

    # Trim trailing whitespace on each body and drop empty bodies.
    cleaned: List[Dict[str, str]] = []
    for b in blocks:
        body = (b.get("body") or "").strip()
        if not body and not (b.get("title") or "").strip():
            continue
        cleaned.append({"kind": b["kind"], "title": (b.get("title") or "").strip()[:255], "body": body})
    return cleaned


class MigrateRequest(BaseModel):
    clear_legacy: bool = False
    dry_run: bool = False


@router.get("/knowledge/legacy-knowledge-base")
async def get_legacy_knowledge_base(
    request: Request,
    db: Session = Depends(get_db),
):
    """Return the current legacy ``manual_knowledge_base`` text + a
    preview of how the splitter would chunk it. Used by the dashboard
    to render the "Detected legacy text" banner before the merchant
    confirms the import.
    """
    tenant_id = resolve_tenant_id(request)
    settings = get_or_create_settings(db, tenant_id)
    db.commit()
    ai = merge_ai_defaults(settings.ai_settings)
    text = (ai.get("manual_knowledge_base") or "").strip()
    preview = _split_legacy_text(text) if text else []
    return {
        "text": text,
        "char_count": len(text),
        "preview": preview,
    }


@router.post("/knowledge/sections/migrate-from-legacy")
async def migrate_from_legacy(
    payload: MigrateRequest,
    request: Request,
    db: Session = Depends(get_db),
):
    """Lift ``ai_settings.manual_knowledge_base`` into structured rows.

    Idempotent-ish: re-running creates a fresh batch of sections with a
    new timestamp. The merchant is expected to delete duplicates from
    the UI if they re-run by accident — we deliberately don't try to
    deduplicate on body text because edits between runs are common.

    With ``dry_run=true`` we return the proposed split without writing
    anything, so the dashboard can show a confirmation step.

    With ``clear_legacy=true`` we move the text to
    ``ai_settings._kb_backup_v1`` and zero out the canonical field, so
    the prompt overlay starts using the structured rows on the next
    Claude turn.
    """
    tenant_id = resolve_tenant_id(request)
    settings = get_or_create_settings(db, tenant_id)
    ai = merge_ai_defaults(settings.ai_settings or {})
    text = (ai.get("manual_knowledge_base") or "").strip()
    if not text:
        return {"created": 0, "blocks": [], "cleared_legacy": False}

    blocks = _split_legacy_text(text)
    if not blocks:
        # Should not happen — the splitter always returns at least one
        # ``custom`` block when ``text`` is non-empty — but defend
        # against the edge case anyway.
        blocks = [{"kind": "custom", "title": "ملاحظات عامة", "body": text}]

    if payload.dry_run:
        return {"created": 0, "blocks": blocks, "cleared_legacy": False, "dry_run": True}

    created: List[Dict[str, Any]] = []
    base_priority = 200  # imported rows sort below user-edited rows
    for idx, b in enumerate(blocks):
        kind = b["kind"] if is_valid_kind(b["kind"]) else "custom"
        row = MerchantKnowledgeSection(
            tenant_id=tenant_id,
            kind=kind,
            title=(b.get("title") or None) or get_kind(kind).label_ar,
            body=b["body"],
            metadata_json={"imported_from": "manual_knowledge_base"},
            priority=base_priority + idx,
            is_active=True,
            source="imported",
            ai_status="approved",
        )
        db.add(row)
        db.flush()
        created.append({"id": int(row.id), "kind": kind})

    cleared_legacy = False
    if payload.clear_legacy:
        # Stash the text on the tenant's ai_settings so support can
        # recover it later if needed. We deliberately namespace the key
        # with a leading underscore so it never feeds the prompt overlay.
        merged = dict(ai)
        merged["_kb_backup_v1"] = text
        merged["manual_knowledge_base"] = ""
        settings.ai_settings = merged
        settings.updated_at = datetime.now(timezone.utc)
        cleared_legacy = True

    db.commit()
    _logger.info(
        "[KB.migrate] tenant=%s created=%d cleared_legacy=%s",
        tenant_id, len(created), cleared_legacy,
    )
    return {
        "created": len(created),
        "blocks": created,
        "cleared_legacy": cleared_legacy,
    }


# ── Phase 2 — AI classifier ("تنسيق ودمج بالذكاء") ─────────────────────────
#
# The merchant types a free-form note in the Quick-Updates field +
# (optionally) attaches media. We ask GPT to classify the text into
# structured ops against the existing sections, surface conflicts
# against the connected e-commerce platform (Salla / Zid / Shopify),
# and stash the proposal in ``merchant_knowledge_drafts`` for review.
# The merchant approves per-op from the dashboard preview drawer; we
# apply the approved subset to ``merchant_knowledge_sections`` and
# (where applicable) ``merchant_knowledge_media``.

from modules.ai.knowledge.classifier import (  # noqa: E402
    AttachedMedia,
    ExistingSection,
    PlatformSignal,
    _looks_like_platform_field_claim,
    classify_quick_update,
)
from modules.ai.knowledge.product_matcher import (  # noqa: E402
    CatalogProductForMatch,
    match_products,
)
from modules.ai.knowledge.repair_advisor import (  # noqa: E402
    analyze_sections,
    summarize,
)


class FormatRequest(BaseModel):
    raw_text: str = Field(..., min_length=1, max_length=8000)
    attached_media_ids: List[int] = Field(default_factory=list, max_length=20)


class DecideRequest(BaseModel):
    """Approve or reject the draft, optionally restricted to a subset
    of op_ids.  An empty ``op_ids`` list means "all proposed ops"."""

    op_ids: Optional[List[str]] = None


def _serialize_draft(draft: MerchantKnowledgeDraft) -> Dict[str, Any]:
    return {
        "id": int(draft.id),
        "tenant_id": int(draft.tenant_id),
        "raw_text": draft.raw_text,
        "attached_media_ids": list(draft.attached_media_ids or []),
        "status": draft.status,
        "proposal": draft.proposal_json or {"proposed_ops": [], "conflicts": []},
        "conflicts": draft.conflicts_json or [],
        "created_at": draft.created_at.isoformat() if draft.created_at else None,
        "decided_at": draft.decided_at.isoformat() if draft.decided_at else None,
        "applied_op_ids": list(draft.applied_op_ids or []),
    }


def _existing_sections_for_prompt(
    db: Session, tenant_id: int,
) -> List[ExistingSection]:
    rows = (
        db.query(MerchantKnowledgeSection)
        .filter(
            MerchantKnowledgeSection.tenant_id == tenant_id,
            MerchantKnowledgeSection.is_active.is_(True),
        )
        .order_by(MerchantKnowledgeSection.priority.asc())
        .all()
    )
    return [
        ExistingSection(
            id=int(r.id),
            kind=r.kind,
            title=r.title,
            body_preview=(r.body or "")[:240],
        )
        for r in rows
    ]


def _platform_signal_for_tenant(db: Session, tenant_id: int) -> PlatformSignal:
    """Read the store_settings JSONB to decide which platform is connected.

    Mirrors the precedence-banner logic in the dashboard so the
    classifier prompt sees the same world the merchant does.
    """
    try:
        settings = get_or_create_settings(db, tenant_id)
        store = dict(settings.store_settings or {})
    except Exception:
        store = {}

    platform = str(store.get("platform_type") or "").strip().lower()
    if platform == "salla" and store.get("salla_access_token"):
        return PlatformSignal(
            connected=True,
            platform="salla",
            warning=(
                "متجر التاجر مربوط بسلة. السعر، التوفر، اسم المنتج، الرابط، "
                "والصورة الأساسية تأتي من سلة وهي المصدر الرسمي. لا تقترح "
                "create/update لهذه الحقول؛ أنتج تعارضاً (conflict) بدلاً منها."
            ),
        )
    if platform == "zid" and store.get("zid_client_id"):
        return PlatformSignal(
            connected=True,
            platform="zid",
            warning=(
                "متجر التاجر مربوط بـ زد. السعر والمخزون يأتيان من زد — لا "
                "تتجاوز هذه الحقول من قاعدة المعرفة."
            ),
        )
    if platform == "shopify" and store.get("shopify_access_token"):
        return PlatformSignal(
            connected=True,
            platform="shopify",
            warning=(
                "Shopify is connected — prices, stock, product names, and "
                "primary URLs come from Shopify. Do not propose overrides "
                "for those fields; flag them as conflicts instead."
            ),
        )
    return PlatformSignal(
        connected=False, platform=None,
        warning="منصة التجارة غير متصلة — جميع المعلومات هنا هي المصدر.",
    )


@router.post("/knowledge/quick-update/format", status_code=201)
async def format_quick_update(
    payload: FormatRequest,
    request: Request,
    db: Session = Depends(get_db),
):
    """Run the GPT classifier on a free-form note and stash the proposal.

    Returns the saved draft (status=``pending``) so the dashboard can
    render the preview drawer. The merchant then calls
    ``/knowledge/drafts/{id}/approve`` or ``/reject`` with an optional
    list of ``op_ids``.
    """
    tenant_id = resolve_tenant_id(request)
    get_or_create_tenant(db, tenant_id)

    raw_text = (payload.raw_text or "").strip()
    if not raw_text:
        raise HTTPException(status_code=400, detail="raw_text_required")

    # Validate + resolve attached media — never trust client-supplied
    # ids (the FK guard happens later too, but failing fast gives a
    # cleaner error message).
    media_rows: List[AIMediaItem] = []
    if payload.attached_media_ids:
        media_rows = (
            db.query(AIMediaItem)
            .filter(
                AIMediaItem.id.in_(list({int(mid) for mid in payload.attached_media_ids})),
                AIMediaItem.tenant_id == tenant_id,
            )
            .all()
        )
        if len(media_rows) != len({int(mid) for mid in payload.attached_media_ids}):
            raise HTTPException(status_code=400, detail="invalid_media_ids")

    attached = [
        AttachedMedia(
            id=int(m.id),
            title=m.title or f"media#{m.id}",
            media_type=m.media_type,
            media_key=m.media_key,
        )
        for m in media_rows
    ]

    existing = _existing_sections_for_prompt(db, tenant_id)
    signal = _platform_signal_for_tenant(db, tenant_id)
    available = [sk.kind for sk in all_kinds()]

    proposal = classify_quick_update(
        raw_text=raw_text,
        attached_media=attached,
        existing_sections=existing,
        platform_signal=signal,
        available_kinds=available,
        tenant_id=tenant_id,
    )

    # ── Phase 3.2 — fuzzy product matching ─────────────────────────────────
    # For every create/update/merge op the classifier produced, scan its
    # body for likely product mentions against the tenant's catalog and
    # append ``link_product`` ops with ``source=ai_fuzzy_match``. The
    # merchant can dismiss them in the preview drawer (they default to
    # selected — opt-out, not opt-in — so the common case of "yes the
    # match is right" is zero-clicks).
    try:
        product_rows = (
            db.query(Product.id, Product.title, Product.sku, Product.external_id)
            .filter(Product.tenant_id == tenant_id)
            .limit(2000)  # safety cap — most merchants have ≪ this
            .all()
        )
        catalog = [
            CatalogProductForMatch(
                id=int(r.id), title=r.title or "",
                sku=r.sku, external_id=r.external_id,
            )
            for r in product_rows
        ]
    except Exception as exc:  # noqa: BLE001
        _logger.warning("[KB.fuzzy] catalog load failed tenant=%s: %s", tenant_id, exc)
        catalog = []

    if catalog:
        existing_ops = list(proposal.get("proposed_ops") or [])
        next_idx = len(existing_ops) + 1
        for op in existing_ops:
            if (op.get("op") or "").lower() not in ("create", "update", "merge"):
                continue
            scan_text = " ".join([op.get("title") or "", op.get("body") or ""])
            matches = match_products(scan_text, catalog, limit=3, min_confidence=0.5)
            for m in matches:
                proposal["proposed_ops"].append({
                    "op_id": f"op-{next_idx}",
                    "op": "link_product",
                    "kind": op.get("kind") or "custom",
                    "title": f"اربط منتج: {m.title}",
                    "body": "",
                    "metadata": {
                        "auto_match_source_op": op.get("op_id") or "",
                        "matched_tokens": list(m.matched_tokens),
                    },
                    "target_section_id": op.get("target_section_id"),
                    # link_product reuses link_role to nothing; classifier
                    # contract uses media_id for the foreign id.
                    "link_role": None,
                    "media_id": None,
                    "product_id": m.product_id,
                    "confidence": m.confidence,
                    "rationale": (
                        f"اقتراح ربط بمنتج '{m.title}' بثقة "
                        f"{round(m.confidence * 100)}% بناءً على تطابق "
                        f"الكلمات: {', '.join(m.matched_tokens) or '—'}"
                    ),
                })
                next_idx += 1

    draft = MerchantKnowledgeDraft(
        tenant_id=tenant_id,
        raw_text=raw_text,
        attached_media_ids=[m.id for m in attached],
        status="failed" if proposal.get("fallback_used") and proposal.get("fallback_reason") == "call_error" else "pending",
        proposal_json={
            "proposed_ops": proposal.get("proposed_ops", []),
            "confidence": proposal.get("confidence", 0.0),
            "model": proposal.get("model"),
            "fallback_used": bool(proposal.get("fallback_used")),
            "fallback_reason": proposal.get("fallback_reason"),
        },
        conflicts_json=proposal.get("conflicts", []),
    )
    db.add(draft)
    db.commit()
    db.refresh(draft)
    _logger.info(
        "[KB.classify] tenant=%s draft=%s ops=%d conflicts=%d fallback=%s",
        tenant_id, draft.id,
        len(proposal.get("proposed_ops") or []),
        len(proposal.get("conflicts") or []),
        proposal.get("fallback_used"),
    )
    return _serialize_draft(draft)


@router.get("/knowledge/drafts")
async def list_drafts(
    request: Request,
    db: Session = Depends(get_db),
    status: Optional[str] = Query(None),
):
    tenant_id = resolve_tenant_id(request)
    q = db.query(MerchantKnowledgeDraft).filter(
        MerchantKnowledgeDraft.tenant_id == tenant_id,
    )
    if status:
        q = q.filter(MerchantKnowledgeDraft.status == status.strip().lower())
    rows = q.order_by(MerchantKnowledgeDraft.created_at.desc()).limit(50).all()
    return {"items": [_serialize_draft(r) for r in rows]}


def _apply_op_to_db(
    db: Session,
    tenant_id: int,
    op: Dict[str, Any],
) -> Optional[int]:
    """Apply a single approved op against the live sections table.

    Returns the affected section id (created or updated). ``link_media``
    ops return the parent section id so the dashboard can refresh the
    correct card. Raises ``HTTPException`` only for invariant
    violations; normal "skipped" cases (e.g. target_section_id
    references another tenant) return ``None`` and the apply loop
    continues.
    """
    kind = (op.get("kind") or "custom").strip().lower()
    if not is_valid_kind(kind):
        kind = "custom"
    op_type = (op.get("op") or "create").strip().lower()

    if op_type == "link_product":
        product_id = _safe_pos_int(op.get("product_id"))
        target_id = _safe_pos_int(op.get("target_section_id"))
        if product_id is None or target_id is None:
            return None
        section = (
            db.query(MerchantKnowledgeSection)
            .filter(
                MerchantKnowledgeSection.id == target_id,
                MerchantKnowledgeSection.tenant_id == tenant_id,
            )
            .first()
        )
        if not section:
            return None
        product = (
            db.query(Product)
            .filter(Product.id == product_id, Product.tenant_id == tenant_id)
            .first()
        )
        if not product:
            return None
        existing = (
            db.query(MerchantKnowledgeSectionProduct)
            .filter(
                MerchantKnowledgeSectionProduct.section_id == section.id,
                MerchantKnowledgeSectionProduct.product_id == product.id,
            )
            .first()
        )
        if existing:
            return section.id
        conf_raw = op.get("confidence")
        try:
            conf = float(conf_raw) if conf_raw is not None else None
        except Exception:
            conf = None
        link = MerchantKnowledgeSectionProduct(
            section_id=section.id, product_id=product.id,
            source="ai_fuzzy_match", confidence=conf,
        )
        db.add(link)
        db.flush()
        return section.id

    if op_type == "link_media":
        media_id = _safe_pos_int(op.get("media_id"))
        target_id = _safe_pos_int(op.get("target_section_id"))
        if media_id is None or target_id is None:
            return None
        section = (
            db.query(MerchantKnowledgeSection)
            .filter(
                MerchantKnowledgeSection.id == target_id,
                MerchantKnowledgeSection.tenant_id == tenant_id,
            )
            .first()
        )
        if not section:
            return None
        media = (
            db.query(AIMediaItem)
            .filter(
                AIMediaItem.id == media_id,
                AIMediaItem.tenant_id == tenant_id,
            )
            .first()
        )
        if not media:
            return None
        role = (op.get("link_role") or "primary").strip().lower()
        if not is_valid_link_role(role):
            role = "primary"
        existing = (
            db.query(MerchantKnowledgeMedia)
            .filter(
                MerchantKnowledgeMedia.section_id == section.id,
                MerchantKnowledgeMedia.media_id == media.id,
                MerchantKnowledgeMedia.link_role == role,
            )
            .first()
        )
        if existing:
            # Even on idempotent re-application of an existing link,
            # try to auto-bind the canonical media_key — older
            # links pre-date this auto-bind so we should still fix
            # them up when a draft is re-approved.
            _maybe_autolink_payment_media_key(
                db, tenant_id, media, section, role,
            )
            return section.id
        link = MerchantKnowledgeMedia(
            section_id=section.id, media_id=media.id, link_role=role,
        )
        db.add(link)
        # Mirror the manual ``link_media`` endpoint behavior: when the
        # approved AI proposal creates a new link, give it the same
        # canonical-key autobind so the runtime safety net can find
        # the asset without the LLM needing to emit the marker.
        _maybe_autolink_payment_media_key(
            db, tenant_id, media, section, role,
        )
        db.flush()
        return section.id

    # create / update / merge — fall through to section write.
    title = (op.get("title") or "").strip() or None
    body = (op.get("body") or "").strip()
    metadata = op.get("metadata") if isinstance(op.get("metadata"), dict) else {}
    confidence = None
    target_id = _safe_pos_int(op.get("target_section_id"))

    if op_type in ("update", "merge") and target_id is not None:
        section = (
            db.query(MerchantKnowledgeSection)
            .filter(
                MerchantKnowledgeSection.id == target_id,
                MerchantKnowledgeSection.tenant_id == tenant_id,
            )
            .first()
        )
        if section is None:
            return None
        if op_type == "merge":
            # Append body separated by a newline; keep title if absent.
            base = (section.body or "").rstrip()
            section.body = (base + ("\n\n" if base else "") + body).strip() if body else base
            if title and not section.title:
                section.title = title
        else:
            section.body = body or section.body
            if title is not None:
                section.title = title
        merged_meta = dict(section.metadata_json or {})
        merged_meta.update(metadata or {})
        merged_meta["last_classifier_op"] = op_type
        section.metadata_json = merged_meta
        section.ai_status = "approved"
        section.source = "ai_classified"
        if confidence is not None:
            section.classification_confidence = confidence
        db.flush()
        return section.id

    # Default: create new
    section = MerchantKnowledgeSection(
        tenant_id=tenant_id,
        kind=kind,
        title=title,
        body=body,
        metadata_json=dict(metadata or {}, **{"ai_op_id": op.get("op_id") or ""}),
        priority=100,
        is_active=True,
        source="ai_classified",
        ai_status="approved",
    )
    db.add(section)
    db.flush()
    return section.id


def _safe_pos_int(val: Any) -> Optional[int]:
    if val in (None, "", "null"):
        return None
    try:
        n = int(val)
        return n if n > 0 else None
    except Exception:
        return None


@router.post("/knowledge/drafts/{draft_id}/approve")
async def approve_draft(
    draft_id: int,
    payload: DecideRequest,
    request: Request,
    db: Session = Depends(get_db),
):
    tenant_id = resolve_tenant_id(request)
    draft = (
        db.query(MerchantKnowledgeDraft)
        .filter(
            MerchantKnowledgeDraft.id == draft_id,
            MerchantKnowledgeDraft.tenant_id == tenant_id,
        )
        .first()
    )
    if not draft:
        raise HTTPException(status_code=404, detail="not_found")
    if draft.status == "approved":
        return _serialize_draft(draft)
    if draft.status == "rejected":
        raise HTTPException(status_code=409, detail="already_rejected")

    proposal = draft.proposal_json or {}
    ops = list(proposal.get("proposed_ops") or [])
    if not ops:
        raise HTTPException(status_code=400, detail="no_ops_to_apply")

    if payload.op_ids:
        wanted = set(payload.op_ids)
        ops_to_apply = [op for op in ops if op.get("op_id") in wanted]
    else:
        ops_to_apply = ops

    # ── Server-side conflict guard (Phase 4 stabilization) ──────────────
    # The dashboard hides the checkbox for ops blocked by a hard
    # platform conflict (price / stock / name / url) — but the server
    # MUST NOT trust that filter alone. A buggy / stale client (and our
    # own previous client logic missed the case where both
    # ``op.target_section_id`` and ``conflict.with_section_id`` are
    # ``null`` — e.g. a fresh ``create`` op flagged by a tenant-wide
    # platform_price conflict) could let the merchant accidentally
    # write a manual price that contradicts the connected storefront.
    # We re-derive the block set here using the same rules the client
    # *should* apply, then drop any op that matches.
    conflicts = list(draft.conflicts_json or [])
    hard_conflict_kinds = {
        "platform_price", "platform_stock",
        "platform_name", "platform_url",
    }
    blocked_op_ids: set = set()
    body_field_claims = {
        op.get("op_id"): _looks_like_platform_field_claim(op.get("body") or "")
        for op in ops
    }
    for c in conflicts:
        if not isinstance(c, dict):
            continue
        c_kind = str(c.get("kind") or "").strip().lower()
        if c_kind not in hard_conflict_kinds:
            continue
        c_target = c.get("with_section_id")
        for op in ops:
            op_type = (op.get("op") or "").lower()
            # link_media / link_product are always safe — they don't
            # carry price/stock claims.
            if op_type in ("link_media", "link_product"):
                continue
            op_target = op.get("target_section_id")
            # Pin the op to the conflict when:
            #   (a) both reference the same explicit section_id, OR
            #   (b) the conflict is tenant-wide (with_section_id=None)
            #       AND the op's body looks like a price/stock claim.
            same_target = (
                c_target is not None
                and op_target is not None
                and int(c_target) == int(op_target)
            )
            tenantwide_claim = (
                c_target is None and body_field_claims.get(op.get("op_id"))
            )
            if same_target or tenantwide_claim:
                blocked_op_ids.add(op.get("op_id") or "")

    if blocked_op_ids:
        before = len(ops_to_apply)
        ops_to_apply = [
            op for op in ops_to_apply
            if (op.get("op_id") or "") not in blocked_op_ids
        ]
        dropped = before - len(ops_to_apply)
        if dropped:
            _logger.warning(
                "[KB.draft.approve] tenant=%s draft=%s server-guard dropped %d "
                "ops due to platform conflicts: %s",
                tenant_id, draft.id, dropped, sorted(blocked_op_ids),
            )

    # Apply link_media / link_product ops AFTER create/update/merge so
    # target_section_id references inside a single draft can resolve to
    # a freshly-created row (we patch them up via the
    # op_id_to_section_id map below — and also resolve numeric
    # target_section_ids by their source op id for auto-generated
    # product links).
    link_op_kinds = ("link_media", "link_product")
    primary_ops = [
        op for op in ops_to_apply
        if (op.get("op") or "").lower() not in link_op_kinds
    ]
    link_ops = [
        op for op in ops_to_apply
        if (op.get("op") or "").lower() in link_op_kinds
    ]

    applied_ids: List[str] = []
    op_id_to_section: Dict[str, int] = {}
    for op in primary_ops:
        section_id = _apply_op_to_db(db, tenant_id, op)
        if section_id is not None:
            op_id_to_section[op.get("op_id") or ""] = section_id
            applied_ids.append(op.get("op_id") or "")

    for op in link_ops:
        # Allow target_section_id to reference an op_id from the same
        # draft (e.g. classifier picked a fresh section as the target,
        # OR the auto-product-matcher hung a link off a sibling op).
        target = op.get("target_section_id")
        if isinstance(target, str) and target.startswith("op-"):
            op["target_section_id"] = op_id_to_section.get(target)
        # Auto-product-match ops also carry the source op_id in metadata
        # — use it when the original target_section_id was None (the
        # source op was a brand-new create that didn't exist yet).
        if op.get("target_section_id") is None:
            md = op.get("metadata") or {}
            source_op_id = str(md.get("auto_match_source_op") or "")
            if source_op_id and source_op_id in op_id_to_section:
                op["target_section_id"] = op_id_to_section[source_op_id]
        section_id = _apply_op_to_db(db, tenant_id, op)
        if section_id is not None:
            applied_ids.append(op.get("op_id") or "")

    draft.status = "approved"
    draft.decided_at = datetime.now(timezone.utc)
    draft.applied_op_ids = applied_ids
    db.commit()
    db.refresh(draft)
    _logger.info(
        "[KB.draft.approve] tenant=%s draft=%s applied=%d",
        tenant_id, draft.id, len(applied_ids),
    )
    return _serialize_draft(draft)


@router.post("/knowledge/drafts/{draft_id}/reject")
async def reject_draft(
    draft_id: int,
    request: Request,
    db: Session = Depends(get_db),
):
    tenant_id = resolve_tenant_id(request)
    draft = (
        db.query(MerchantKnowledgeDraft)
        .filter(
            MerchantKnowledgeDraft.id == draft_id,
            MerchantKnowledgeDraft.tenant_id == tenant_id,
        )
        .first()
    )
    if not draft:
        raise HTTPException(status_code=404, detail="not_found")
    if draft.status == "approved":
        raise HTTPException(status_code=409, detail="already_approved")
    draft.status = "rejected"
    draft.decided_at = datetime.now(timezone.utc)
    db.commit()
    db.refresh(draft)
    _logger.info("[KB.draft.reject] tenant=%s draft=%s", tenant_id, draft.id)
    return _serialize_draft(draft)


# ── Phase 3 — Section ⇄ Product links ──────────────────────────────────────
#
# A section can be scoped to one or more catalog products. The runtime
# overlay only injects scoped sections when the conversation is about
# the linked product (matched downstream by the resolver layer). Global
# sections (no product links) stay as-is.


class ProductLinkIn(BaseModel):
    product_id: int = Field(..., gt=0)
    source: Optional[str] = Field("manual", max_length=32)
    confidence: Optional[float] = Field(None, ge=0, le=1)


def _section_for_tenant_or_404(
    db: Session, tenant_id: int, section_id: int,
) -> MerchantKnowledgeSection:
    section = (
        db.query(MerchantKnowledgeSection)
        .filter(
            MerchantKnowledgeSection.id == section_id,
            MerchantKnowledgeSection.tenant_id == tenant_id,
        )
        .first()
    )
    if not section:
        raise HTTPException(status_code=404, detail="section_not_found")
    return section


@router.get("/knowledge/sections/{section_id}/products")
async def list_product_links(
    section_id: int,
    request: Request,
    db: Session = Depends(get_db),
):
    tenant_id = resolve_tenant_id(request)
    section = _section_for_tenant_or_404(db, tenant_id, section_id)
    links = (
        db.query(MerchantKnowledgeSectionProduct)
        .filter(MerchantKnowledgeSectionProduct.section_id == section.id)
        .order_by(MerchantKnowledgeSectionProduct.id.asc())
        .all()
    )
    return {"items": [_serialize_product_link(lk) for lk in links]}


@router.post("/knowledge/sections/{section_id}/products", status_code=201)
async def link_product(
    section_id: int,
    payload: ProductLinkIn,
    request: Request,
    db: Session = Depends(get_db),
):
    tenant_id = resolve_tenant_id(request)
    section = _section_for_tenant_or_404(db, tenant_id, section_id)

    product = (
        db.query(Product)
        .filter(Product.id == payload.product_id, Product.tenant_id == tenant_id)
        .first()
    )
    if not product:
        raise HTTPException(status_code=400, detail="product_not_found")

    existing = (
        db.query(MerchantKnowledgeSectionProduct)
        .filter(
            MerchantKnowledgeSectionProduct.section_id == section.id,
            MerchantKnowledgeSectionProduct.product_id == product.id,
        )
        .first()
    )
    if existing:
        # Idempotent — return the existing row instead of 409 so the
        # dashboard can call this freely.
        return _serialize_product_link(existing)

    source = (payload.source or "manual").strip().lower()
    if source not in ("manual", "ai_fuzzy_match", "imported"):
        source = "manual"

    link = MerchantKnowledgeSectionProduct(
        section_id=section.id,
        product_id=product.id,
        source=source,
        confidence=payload.confidence,
    )
    db.add(link)
    db.commit()
    db.refresh(link)
    _logger.info(
        "[KB.product.link] tenant=%s section=%s product=%s source=%s",
        tenant_id, section.id, product.id, source,
    )
    return _serialize_product_link(link)


@router.delete("/knowledge/sections/{section_id}/products/{link_id}")
async def unlink_product(
    section_id: int,
    link_id: int,
    request: Request,
    db: Session = Depends(get_db),
):
    tenant_id = resolve_tenant_id(request)
    section = _section_for_tenant_or_404(db, tenant_id, section_id)
    link = (
        db.query(MerchantKnowledgeSectionProduct)
        .filter(
            MerchantKnowledgeSectionProduct.id == link_id,
            MerchantKnowledgeSectionProduct.section_id == section.id,
        )
        .first()
    )
    if not link:
        raise HTTPException(status_code=404, detail="link_not_found")
    db.delete(link)
    db.commit()
    return {"status": "deleted"}


@router.get("/knowledge/products/search")
async def search_products(
    request: Request,
    db: Session = Depends(get_db),
    q: str = Query("", max_length=120),
    limit: int = Query(20, ge=1, le=50),
):
    """Lightweight product autocomplete for the Knowledge Hub UI.

    Restricted to tenant-owned products. Matches against title / SKU
    / external_id (case-insensitive substring). Returns the bare
    minimum the section card dropdown needs.
    """
    tenant_id = resolve_tenant_id(request)
    query = db.query(Product).filter(Product.tenant_id == tenant_id)
    needle = (q or "").strip()
    if needle:
        pat = f"%{needle.lower()}%"
        query = query.filter(
            sa.or_(
                sa.func.lower(Product.title).like(pat),
                sa.func.lower(Product.sku).like(pat),
                sa.func.lower(Product.external_id).like(pat),
            )
        )
    rows = query.order_by(Product.title.asc()).limit(limit).all()
    return {
        "items": [
            {
                "id": int(p.id),
                "title": p.title,
                "external_id": p.external_id,
                "sku": p.sku,
                "in_stock": bool(p.in_stock),
            }
            for p in rows
        ]
    }


# ── KB-2 Repair Advisor (preview-only) ──────────────────────────────────────


@router.get("/knowledge/repair/preview")
async def repair_preview(
    request: Request,
    db: Session = Depends(get_db),
):
    """KB-2 — Suggest repairs to the tenant's knowledge sections.

    Read-only endpoint that scans the tenant's
    ``merchant_knowledge_sections`` rows and returns a list of
    suggestions in three categories:

      * **move**           — behavioral content sitting in a commerce kind
      * **duplicate**      — two rows with overlapping bodies + same kind
      * **contamination**  — a row mixing behavioral text and commerce facts

    The endpoint NEVER mutates state. The merchant reviews the list,
    approves what makes sense, and applies the moves via the existing
    section edit/move routes — there's no "auto-fix" button by design.

    Response shape::

        {
          "suggestions": [
            {
              "kind": "move",
              "severity": "warn",
              "section_ids": [42],
              "title_preview": "...",
              "body_preview": "...",
              "current_kind": "store_info",
              "suggested_kind": "forbidden_phrases",
              "reason_ar": "..."
            }, ...
          ],
          "summary": {"total": 3, "move": 1, "duplicate": 1, "contamination": 1, ...}
        }
    """
    tenant_id = resolve_tenant_id(request)
    get_or_create_tenant(db, tenant_id)

    rows = (
        db.query(MerchantKnowledgeSection)
        .filter(
            MerchantKnowledgeSection.tenant_id == tenant_id,
            MerchantKnowledgeSection.is_active.is_(True),
        )
        .order_by(MerchantKnowledgeSection.id.asc())
        .all()
    )

    suggestions = analyze_sections(rows)
    payload = [s.to_dict() for s in suggestions]
    summary = summarize(suggestions)

    _logger.info(
        "[KB.repair] tenant=%s scanned=%d suggestions=%d "
        "moves=%d duplicates=%d contamination=%d",
        tenant_id, len(rows), summary["total"],
        summary["move"], summary["duplicate"], summary["contamination"],
    )

    return {"suggestions": payload, "summary": summary}

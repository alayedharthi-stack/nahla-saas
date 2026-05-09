"""
core/ai_libraries.py
─────────────────────
Read-side helpers for the merchant-curated *Manual Coupons* and *AI Media
Library* tables.

These helpers are used in two places:

* :func:`core.store_knowledge.build_merchant_context` injects the active
  rows into the brain prompt so the LLM knows which coupon codes it may
  cite verbatim and which media files it may attach.

* :func:`extract_media_markers` parses the LLM reply for ``[MEDIA:<id>]``
  tokens, strips them out of the text, and returns the matching media
  rows so the WhatsApp webhook can dispatch image / video / document
  messages alongside the cleaned text reply.

Everything here is intentionally *read-only* and tenant-scoped; mutation
goes through ``routers/intelligence_libraries.py``.
"""
from __future__ import annotations

import logging
import re
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional, Tuple

from sqlalchemy.orm import Session

logger = logging.getLogger("nahla-backend")


# Hard ceilings keep the prompt size predictable even for merchants who
# upload a hundred coupons / media items.
_MAX_COUPONS_IN_CONTEXT = 12
_MAX_MEDIA_IN_CONTEXT = 20

# Marker shape: [MEDIA:<id>] — id is the integer DB primary key. We
# accept optional whitespace and an optional title hint after a pipe
# (``[MEDIA:42|product photo]``) so the LLM can be explicit; the title
# hint is ignored by the parser, only the numeric id is honoured.
_MEDIA_MARKER_RE = re.compile(r"\[MEDIA:\s*(\d+)(?:\s*\|[^\]]*)?\]", re.IGNORECASE)


def _is_currently_active(
    is_active: bool,
    starts_at: Optional[datetime],
    expires_at: Optional[datetime],
    *,
    now: Optional[datetime] = None,
) -> bool:
    if not is_active:
        return False
    moment = now or datetime.now(timezone.utc)
    if starts_at and starts_at > moment:
        return False
    if expires_at and expires_at < moment:
        return False
    return True


def list_active_manual_coupons(db: Session, tenant_id: int) -> List[Dict[str, Any]]:
    """Return active, in-window manual coupons for the brain to cite.

    Order: ``priority ASC`` (lower = more important) then by ``id ASC``.
    The brain MUST NOT invent coupons, so anything that fails the
    is-active / expiry check is filtered out here, not in the prompt.
    """
    from models import ManualCoupon  # noqa: PLC0415 — avoid circular import

    rows = (
        db.query(ManualCoupon)
        .filter(ManualCoupon.tenant_id == tenant_id, ManualCoupon.is_active.is_(True))
        .order_by(ManualCoupon.priority.asc(), ManualCoupon.id.asc())
        .limit(_MAX_COUPONS_IN_CONTEXT * 2)  # over-fetch then filter window
        .all()
    )
    now = datetime.now(timezone.utc)
    out: List[Dict[str, Any]] = []
    for r in rows:
        if not _is_currently_active(bool(r.is_active), r.starts_at, r.expires_at, now=now):
            continue
        out.append({
            "id": int(r.id),
            "code": r.code,
            "title": r.title or "",
            "description": r.description or "",
            "discount_text": r.discount_text or "",
            "usage_context": r.usage_context or "",
            "priority": int(r.priority or 0),
            "expires_at": r.expires_at.isoformat() if r.expires_at else None,
        })
        if len(out) >= _MAX_COUPONS_IN_CONTEXT:
            break
    return out


def list_active_ai_media(db: Session, tenant_id: int) -> List[Dict[str, Any]]:
    """Return active media-library items the brain may attach to a reply."""
    from models import AIMediaItem  # noqa: PLC0415 — avoid circular import

    rows = (
        db.query(AIMediaItem)
        .filter(AIMediaItem.tenant_id == tenant_id, AIMediaItem.is_active.is_(True))
        .order_by(AIMediaItem.priority.asc(), AIMediaItem.id.asc())
        .limit(_MAX_MEDIA_IN_CONTEXT)
        .all()
    )
    out: List[Dict[str, Any]] = []
    for r in rows:
        out.append({
            "id": int(r.id),
            "title": r.title,
            "description": r.description or "",
            "media_type": r.media_type,
            "usage_context": r.usage_context or "",
            "tags": list(r.tags or []),
            "priority": int(r.priority or 0),
        })
    return out


def extract_media_markers(
    db: Session,
    tenant_id: int,
    reply_text: str,
    *,
    max_attachments: int = 2,
) -> Tuple[str, List[Dict[str, Any]]]:
    """Strip ``[MEDIA:<id>]`` tokens from ``reply_text`` and resolve them.

    Returns ``(cleaned_text, attachments)`` where each attachment is a
    dict carrying enough metadata for the WhatsApp webhook to dispatch
    the file: ``id``, ``media_type``, ``file_url``, ``title``, optional
    ``mime_type`` and ``storage_kind``.

    Behaviour notes:

    * Attachments are deduped (a token referenced twice ships the file
      once) and capped at ``max_attachments`` so the brain can't accidentally
      flood the customer with media.
    * Media rows whose ``is_active`` flag is **false** OR which belong to
      a different tenant are silently dropped — the dashboard could have
      disabled them mid-conversation, and we never trust an id from the
      LLM blindly.
    * The cleaned text has surrounding whitespace and stranded blank
      lines collapsed so the customer doesn't see odd gaps where the
      marker used to be.
    """
    text = reply_text or ""
    if not text or "[MEDIA:" not in text.upper():
        return text, []

    from models import AIMediaItem  # noqa: PLC0415 — avoid circular import

    matches = list(_MEDIA_MARKER_RE.finditer(text))
    if not matches:
        return text, []

    seen_ids: List[int] = []
    for m in matches:
        try:
            mid = int(m.group(1))
        except (TypeError, ValueError):
            continue
        if mid not in seen_ids:
            seen_ids.append(mid)
        if len(seen_ids) >= max_attachments:
            break

    attachments: List[Dict[str, Any]] = []
    if seen_ids:
        rows = (
            db.query(AIMediaItem)
            .filter(
                AIMediaItem.tenant_id == tenant_id,
                AIMediaItem.id.in_(seen_ids),
                AIMediaItem.is_active.is_(True),
            )
            .all()
        )
        # Preserve the order in which the LLM cited them.
        rows_by_id = {int(r.id): r for r in rows}
        for mid in seen_ids:
            r = rows_by_id.get(mid)
            if r is None:
                logger.info(
                    "[AIMedia.attach] dropped marker id=%s tenant=%s reason=not_found_or_disabled",
                    mid, tenant_id,
                )
                continue
            attachments.append({
                "id": int(r.id),
                "title": r.title,
                "media_type": r.media_type,
                "file_url": r.file_url,
                "mime_type": r.mime_type,
                "storage_kind": r.storage_kind,
            })

    cleaned = _MEDIA_MARKER_RE.sub("", text)
    cleaned = re.sub(r"[ \t]+\n", "\n", cleaned)
    cleaned = re.sub(r"\n{3,}", "\n\n", cleaned).strip()
    return cleaned, attachments

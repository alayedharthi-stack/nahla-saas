"""
services/visual_product_dispatch.py
───────────────────────────────────
Pre-send visual product card enforcement for marker-only / empty-text turns.

Extracted from the WhatsApp webhook so card dispatch can run before the
wire layer without requiring a non-empty text reply first.
"""
from __future__ import annotations

import logging
from typing import Any, Dict, List, Optional, Tuple

logger = logging.getLogger("nahla.visual_product_dispatch")


def maybe_enforce_visual_product_card(
    *,
    db: Any,
    tenant_id: int,
    inbound_message: str,
    reply_text: str,
    brain_action: str,
    brain_state: Optional[dict],
    product_attachments: List[Dict[str, Any]],
    media_attachments: List[Dict[str, Any]],
    product_escalation_blocked: bool,
    fulfillment_discovery_blocked: bool,
    allow_product_cards: bool,
    dispatch_guard_reason: str,
    catalog_card_limit: int,
    customer_id: Optional[int] = None,
) -> Tuple[List[Dict[str, Any]], bool]:
    """Queue one product card when inbound is a visual request without rich content."""
    attachments = list(product_attachments or [])
    if product_escalation_blocked:
        _skip = (
            "fulfillment_lock"
            if fulfillment_discovery_blocked
            else (
                dispatch_guard_reason
                if not allow_product_cards
                else "non_commerce_block"
            )
        )
        logger.info(
            "[VISUAL_PRODUCT_ENFORCEMENT] tenant=%s SKIP reason=%s inbound=%r",
            tenant_id,
            _skip,
            (inbound_message or "")[:80],
        )
        return attachments, False

    try:
        from modules.observability import (  # noqa: PLC0415
            customer_wants_product_or_image as _wants_visual,
            has_visual_marker as _has_marker,
            pick_best_candidate_title as _pick_candidate,
        )
        from services.product_resolver import (  # noqa: PLC0415
            format_product_card_caption as _vp_caption,
            resolve_by_query as _resolve_query,
        )
    except Exception as exc:  # noqa: BLE001
        logger.debug(
            "[VISUAL_PRODUCT_ENFORCEMENT] tenant=%s import failed: %s",
            tenant_id,
            exc,
        )
        return attachments, False

    if not _wants_visual(
        inbound_text=inbound_message or "",
        brain_action=brain_action or "",
    ):
        return attachments, False

    if _has_marker(reply_text or "") or attachments or any(
        str(a.get("media_type") or "").lower().startswith("image")
        for a in (media_attachments or [])
    ):
        return attachments, False

    if len(attachments) >= int(catalog_card_limit or 1):
        logger.info(
            "[VISUAL_PRODUCT_ENFORCEMENT] tenant=%s SKIP reason=catalog_card_limit "
            "limit=%d count=%d inbound=%r",
            tenant_id,
            catalog_card_limit,
            len(attachments),
            (inbound_message or "")[:80],
        )
        return attachments, False

    bs = dict(brain_state or {})
    candidate_title, candidate_source = _pick_candidate(bs, inbound_message or "")
    logger.info(
        "[VISUAL_PRODUCT_ENFORCEMENT] tenant=%s TRIGGER inbound=%r brain_action=%s "
        "candidate=%r source=%s",
        tenant_id,
        (inbound_message or "")[:80],
        brain_action or "?",
        (candidate_title or "")[:80],
        candidate_source,
    )
    if not candidate_title:
        logger.warning(
            "[VISUAL_PRODUCT_ENFORCEMENT] tenant=%s FALLBACK_TEXT_ONLY "
            "reason=no_candidate inbound=%r brain_action=%s",
            tenant_id,
            (inbound_message or "")[:80],
            brain_action or "?",
        )
        return attachments, False

    try:
        resolved = _resolve_query(
            db,
            tenant_id,
            candidate_title,
            customer_id=customer_id,
        )
    except Exception as exc:  # noqa: BLE001
        logger.warning(
            "[VISUAL_PRODUCT_ENFORCEMENT] tenant=%s RESOLVER_FAILED candidate=%r err=%s",
            tenant_id,
            candidate_title[:80],
            exc,
        )
        return attachments, False

    if not resolved:
        logger.warning(
            "[VISUAL_PRODUCT_ENFORCEMENT] tenant=%s FALLBACK_TEXT_ONLY "
            "reason=resolver_no_match candidate=%r source=%s",
            tenant_id,
            candidate_title[:80],
            candidate_source,
        )
        return attachments, False

    card: Dict[str, Any] = {
        "kind": "product_card",
        "id": resolved.id,
        "title": resolved.title,
        "media_type": "image",
        "file_url": resolved.image_url or "",
        "caption": _vp_caption(resolved, include_description=False),
        "product_url": resolved.product_url,
        "price": resolved.price,
        "in_stock": resolved.in_stock,
        "external_id": resolved.external_id,
        "confidence": resolved.confidence,
        "_enforced": True,
        "dispatch_source": "visual",
        "candidate_origin": candidate_source,
    }
    attachments.append(card)
    logger.info(
        "[VISUAL_PRODUCT_ENFORCEMENT] tenant=%s ENFORCED product_id=%s title=%r "
        "image=%s url=%s source=%s",
        tenant_id,
        resolved.id,
        resolved.title,
        bool(resolved.image_url),
        bool(resolved.product_url),
        candidate_source,
    )
    return attachments, True

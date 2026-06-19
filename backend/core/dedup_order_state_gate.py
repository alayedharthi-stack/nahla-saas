"""
core/dedup_order_state_gate.py
──────────────────────────────
Dedup OrderStateRelevanceGate — P1-C-1.

When the chat near-duplicate guard replaces the brain reply, we must not
resurrect stale order/payment templates on price, product, or visual
pivots. Operational truth only when the inbound turn is relevant.
"""
from __future__ import annotations

import logging
import re
from typing import Any, Dict, Optional, Tuple

logger = logging.getLogger("nahla.dedup_order_state_gate")

_TRACK_ORDER_INBOUND_RE = re.compile(
    r"(?:رقم\s*الطلب|وين\s*(?:ال)?طلب|اين\s*(?:ال)?طلب|حالة\s*الطلب|"
    r"track\s*order|order\s*(?:id|number|#))",
    re.UNICODE | re.IGNORECASE,
)

_NEUTRAL_SHORT_REPLIES = frozenset({
    "تمام", "طيب", "اوك", "ok", "okay", "yes", "نعم", "ايه", "ايوه", "ايوا",
    "شكرا", "شكراً", "thanks", "thank", "حلو", "زين", "ماشي", "حاضر",
    "الب", "يلا", "هلا", "مرحبا", "سلام", "hi", "hello",
})

_VISUAL_PIVOT_RE = re.compile(
    r"(?:صور(?:ه|ة)?|الصور(?:ه|ة)?|صورة|الصورة|شكل(?:ه|ها)?|"
    r"image|photo|picture)",
    re.UNICODE | re.IGNORECASE,
)


def _normalize(text: str) -> str:
    try:
        from modules.ai.brain.state.state_relevance import (  # noqa: PLC0415
            _normalize as _sr_norm,
        )

        return _sr_norm(text)
    except Exception:  # noqa: BLE001
        return (text or "").strip().lower()


def inbound_is_visual_pivot(message: str) -> bool:
    raw = (message or "").strip()
    if not raw:
        return False
    try:
        from modules.ai.brain.commerce.product_visual import (  # noqa: PLC0415
            is_product_visual_request,
        )

        if is_product_visual_request(raw):
            return True
    except Exception:  # noqa: BLE001  # noqa: silent-ok — optional visual import for dedup gate
        pass
    return bool(_VISUAL_PIVOT_RE.search(raw))


def inbound_is_track_order_question(message: str) -> bool:
    return bool(_TRACK_ORDER_INBOUND_RE.search(message or ""))


def inbound_is_short_product_inquiry(message: str) -> bool:
    """Single-token or short product-name turns (e.g. «طلح»)."""
    norm = _normalize(message or "").strip()
    if not norm or len(norm) > 48:
        return False
    try:
        from modules.ai.brain.state.state_relevance import (  # noqa: PLC0415
            has_fulfillment_semantics,
            has_payment_semantics,
            has_price_variant_commerce_semantics,
            _COMMERCE_PRODUCT_RE,
        )
    except Exception:  # noqa: BLE001  # noqa: silent-ok — state relevance import optional at webhook boundary
        return False

    if has_payment_semantics(norm) or has_fulfillment_semantics(norm):
        return False
    if inbound_is_track_order_question(norm):
        return False

    tokens = [t for t in norm.split() if t]
    if not tokens or len(tokens) > 5:
        return False

    if has_price_variant_commerce_semantics(norm):
        return True
    if _COMMERCE_PRODUCT_RE.search(norm):
        return True
    if len(tokens) == 1 and len(tokens[0]) >= 2:
        if tokens[0] in _NEUTRAL_SHORT_REPLIES:
            return False
        return True
    return False


def inbound_pivots_away_from_order_state(
    message: str,
    *,
    inbound_metadata: Optional[Dict[str, Any]] = None,
    normalized_type: Optional[str] = None,
) -> bool:
    """True when inbound is a price/product/visual turn — not order/payment resume."""
    _ = inbound_metadata, normalized_type
    msg = message or ""
    if not msg.strip():
        return False
    if inbound_is_track_order_question(msg):
        return False
    if inbound_is_visual_pivot(msg):
        return True
    try:
        from modules.ai.brain.state.state_relevance import (  # noqa: PLC0415
            has_price_variant_commerce_semantics,
        )

        if has_price_variant_commerce_semantics(msg):
            return True
    except Exception:  # noqa: BLE001  # noqa: silent-ok — price semantics import optional at webhook boundary
        pass
    if inbound_is_short_product_inquiry(msg):
        return True
    return False


def should_suppress_dedup_order_templates(
    *,
    message: str,
    summary: Dict[str, Any],
    inbound_metadata: Optional[Dict[str, Any]] = None,
    normalized_type: Optional[str] = None,
) -> Tuple[bool, str]:
    """Return (suppress, reason) for order/payment dedup template resurrection."""
    try:
        from modules.ai.brain.state.product_correction import (  # noqa: PLC0415
            detect_product_correction,
        )
        from modules.ai.brain.state.product_information_topic import (  # noqa: PLC0415
            detect_product_information_topic_shift,
        )

        if detect_product_correction(message or ""):
            return True, "product_correction"
        if detect_product_information_topic_shift(message or ""):
            return True, "product_information_topic"
    except Exception:  # noqa: BLE001  # noqa: silent-ok — optional topic-shift imports at webhook boundary
        pass

    if not inbound_pivots_away_from_order_state(
        message,
        inbound_metadata=inbound_metadata,
        normalized_type=normalized_type,
    ):
        return False, ""

    try:
        from modules.ai.brain.state.state_relevance import (  # noqa: PLC0415
            should_block_workflow_resume,
            validate_state_relevance_from_summary,
        )

        verdict = validate_state_relevance_from_summary(
            message=message or "",
            summary=summary,
        )
        if summary.get("payment_receipt_received"):
            return True, "payment_receipt_received_commerce_pivot"
        if summary.get("awaiting_payment_receipt") and verdict.detected_topic_shift:
            return True, "awaiting_payment_commerce_pivot"
        if summary.get("selected_product") and should_block_workflow_resume(
            "stale_product_focus", verdict,
        ):
            return True, "stale_product_focus_commerce_pivot"
        if summary.get("selected_product") and inbound_pivots_away_from_order_state(
            message,
            inbound_metadata=inbound_metadata,
            normalized_type=normalized_type,
        ):
            return True, "active_product_commerce_pivot"
    except Exception:  # noqa: BLE001  # noqa: silent-ok — relevance validation fallback returns safe suppress
        return True, "commerce_pivot_fallback"

    return True, "commerce_pivot"


def log_dedup_state_mismatch(
    *,
    tenant_id: Any = None,
    phone_tail: str = "",
    reason: str,
    inbound_preview: str = "",
    blocked_template: str = "",
    payment_receipt_received: bool = False,
) -> None:
    try:
        logger.info(
            "[DEDUP_STATE_MISMATCH] tenant=%s phone=*%s reason=%s "
            "blocked_template=%s payment_receipt_received=%s inbound=%r",
            tenant_id,
            phone_tail,
            reason or "-",
            blocked_template or "-",
            str(bool(payment_receipt_received)).lower(),
            (inbound_preview or "")[:80],
        )
    except Exception:  # noqa: BLE001  # noqa: silent-ok — dedup telemetry must never block fallback
        pass


__all__ = [
    "inbound_is_short_product_inquiry",
    "inbound_is_track_order_question",
    "inbound_is_visual_pivot",
    "inbound_pivots_away_from_order_state",
    "log_dedup_state_mismatch",
    "should_suppress_dedup_order_templates",
]

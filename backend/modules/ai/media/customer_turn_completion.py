"""
Customer-origin turn completion contract.

Every genuine customer conversational/commercial turn must have a defined
owner and completion class. Observability only — does not compose replies.
"""
from __future__ import annotations

import logging
from typing import Any, Dict, Optional, Tuple

logger = logging.getLogger("nahla.customer_turn_completion")

CATALOG_FRAME_MARKER = "[طلب كتالوج من العميل]"

COMPLETION_BRAIN = "brain_natural_reply"
COMPLETION_STRUCTURED_VISIBLE = "structured_visible_action"
COMPLETION_STRUCTURED_AND_CONTINUATION = "structured_action_and_natural_continuation"
COMPLETION_HUMAN = "human_owned"
COMPLETION_PROTOCOL = "protocol_only"
COMPLETION_ORPHAN = "orphan_customer_turn"

# Bounded sibling sweep of customer-origin early-return paths.
# Classification is documentary + test-pinned; only the catalog_order
# persist-only silence is repaired in this change.
AUDITED_CUSTOMER_ORIGIN_EARLY_RETURNS = (
    {
        "path": "whatsapp_webhook.empty_text_no_fallback.catalog_order",
        "input_type": "catalog_order",
        "before": COMPLETION_ORPHAN,
        "after": COMPLETION_STRUCTURED_AND_CONTINUATION,
        "repaired": True,
        "note": "native catalog selection is a customer commerce turn, not protocol silence",
    },
    {
        "path": "whatsapp_webhook.empty_text_no_fallback.media_no_caption",
        "input_type": "media",
        "class": COMPLETION_PROTOCOL,
        "repaired": False,
        "note": "uncaptioned audio/image/video/document remains persist-only inbox visibility",
    },
    {
        "path": "whatsapp_webhook.unsupported_type.sticker_reaction",
        "input_type": "protocol",
        "class": COMPLETION_PROTOCOL,
        "repaired": False,
        "note": "sticker/reaction/unknown types stay persist-only",
    },
    {
        "path": "whatsapp_webhook.media_fallback_reply",
        "input_type": "media",
        "class": COMPLETION_STRUCTURED_VISIBLE,
        "repaired": False,
        "note": "fallback_reply_ar is a customer-visible structured action",
    },
    {
        "path": "whatsapp_webhook.receipt_payment_short_circuit",
        "input_type": "payment",
        "class": COMPLETION_STRUCTURED_VISIBLE,
        "repaired": False,
        "note": "payment/receipt owners already send a customer-visible result",
    },
    {
        "path": "whatsapp_webhook.location_action",
        "input_type": "location",
        "class": COMPLETION_STRUCTURED_AND_CONTINUATION,
        "repaired": False,
        "note": "location continues through existing location/Brain owners when text/coords exist",
    },
    {
        "path": "whatsapp_webhook.human_priority_takeover",
        "input_type": "handoff",
        "class": COMPLETION_HUMAN,
        "repaired": False,
        "note": "genuine human takeover remains a valid completion class",
    },
    {
        "path": "whatsapp_webhook.ai_paused_skip",
        "input_type": "handoff",
        "class": COMPLETION_HUMAN,
        "repaired": False,
        "note": "pause/skip is human-owned; inbound is stored",
    },
    {
        "path": "whatsapp_webhook.pending_action_transition",
        "input_type": "structured_purchase",
        "class": COMPLETION_STRUCTURED_AND_CONTINUATION,
        "repaired": False,
        "note": "pending-action transitions remain owned by existing commerce/Brain paths",
    },
)


def _meta(inbound_metadata: Optional[Dict[str, Any]]) -> Dict[str, Any]:
    return dict(inbound_metadata or {})


def is_structured_catalog_order_inbound(
    inbound_metadata: Optional[Dict[str, Any]] = None,
    message: str = "",
) -> bool:
    """True when this inbound is a native WhatsApp catalog_order commerce turn."""
    meta = _meta(inbound_metadata)
    text = str(message or "")
    try:
        from modules.ai.order_flow_v2.triggers import (  # noqa: PLC0415
            is_catalog_order_inbound,
        )

        if is_catalog_order_inbound(meta, text):
            return True
    except Exception:  # noqa: BLE001  # noqa: silent-ok — completion probe must not block inbound
        pass
    source = str(
        meta.get("source_type")
        or meta.get("inbound_source")
        or ""
    ).strip().lower()
    if source == "catalog_order":
        return True
    if CATALOG_FRAME_MARKER in text:
        return True
    return False


def catalog_order_must_not_orphan(
    inbound_metadata: Optional[Dict[str, Any]] = None,
    message: str = "",
) -> bool:
    """Catalog selection is never a valid protocol-only silent return."""
    return is_structured_catalog_order_inbound(inbound_metadata, message)


def customer_authored_catalog_order_text(
    inbound_metadata: Optional[Dict[str, Any]] = None,
    message: str = "",
) -> str:
    """Customer note for a catalog_order, never the operational frame marker."""
    meta = _meta(inbound_metadata)
    note = str(meta.get("customer_note") or "").strip()
    if note:
        return note
    text = str(message or "").strip()
    if not text or CATALOG_FRAME_MARKER in text:
        return ""
    return text


def should_continue_structured_catalog_order(
    inbound_metadata: Optional[Dict[str, Any]] = None,
    message: str = "",
) -> bool:
    """True when empty customer text must still continue as catalog_order."""
    return catalog_order_must_not_orphan(inbound_metadata, message)


def maybe_restore_catalog_order_semantic_text(
    *,
    semantic_text: str,
    original_brain_text: str,
    inbound_metadata: Optional[Dict[str, Any]] = None,
) -> Tuple[str, Dict[str, Any]]:
    """Continue catalog_order from structured metadata, not synthetic customer language."""
    current = str(semantic_text or "").strip()
    original = str(original_brain_text or "").strip()
    meta = _meta(inbound_metadata)
    authored = customer_authored_catalog_order_text(meta, current or original)
    if not catalog_order_must_not_orphan(meta, current or original):
        return current, {}
    logger.info(
        "[CUSTOMER_TURN_COMPLETION] catalog_order_structured_continue "
        "input_type=catalog_order completion_class=%s authored_len=%d",
        COMPLETION_STRUCTURED_AND_CONTINUATION,
        len(authored),
    )
    return authored, _completion_trace(
        input_type="catalog_order",
        semantic_owner="brain",
        structured_action_owner="wa_native_catalog_order",
        completion_class=COMPLETION_STRUCTURED_AND_CONTINUATION,
        state_persisted=True,
        brain_called=True,
        compose_called=True,
        suppression_reason=None,
        extra={
            "catalog_order_structured_event": True,
            "catalog_order_empty_text_continued": not bool(authored),
            "synthetic_customer_phrase": False,
        },
    )


def classify_empty_text_early_return(
    *,
    inbound_metadata: Optional[Dict[str, Any]] = None,
    normalized_type: str = "",
    has_fallback_reply: bool = False,
    human_owned: bool = False,
    message: str = "",
) -> str:
    """Classify persist-only empty-text returns; catalog_order is not protocol-only."""
    if human_owned:
        return COMPLETION_HUMAN
    if has_fallback_reply:
        return COMPLETION_STRUCTURED_VISIBLE
    if catalog_order_must_not_orphan(inbound_metadata, message):
        return COMPLETION_ORPHAN
    ntype = str(normalized_type or "").strip().lower()
    if ntype in {"sticker", "reaction", "unknown", "contacts", "unsupported"}:
        return COMPLETION_PROTOCOL
    if ntype in {"audio", "image", "video", "document"}:
        return COMPLETION_PROTOCOL
    return COMPLETION_PROTOCOL


def _completion_trace(
    *,
    input_type: str,
    semantic_owner: str,
    structured_action_owner: str,
    completion_class: str,
    state_persisted: bool = False,
    brain_called: bool = False,
    compose_called: bool = False,
    outbound_created: Optional[bool] = None,
    provider_called: Optional[bool] = None,
    customer_visible: Optional[bool] = None,
    suppression_reason: Optional[str] = None,
    extra: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    payload = {
        "customer_turn_completion": {
            "input_type": input_type,
            "semantic_owner": semantic_owner,
            "structured_action_owner": structured_action_owner,
            "state_persisted": state_persisted,
            "brain_called": brain_called,
            "compose_called": compose_called,
            "outbound_created": outbound_created,
            "provider_called": provider_called,
            "customer_visible": customer_visible,
            "completion_class": completion_class,
            "suppression_reason": suppression_reason,
        }
    }
    if extra:
        payload.update(extra)
    return payload


__all__ = [
    "AUDITED_CUSTOMER_ORIGIN_EARLY_RETURNS",
    "CATALOG_FRAME_MARKER",
    "COMPLETION_BRAIN",
    "COMPLETION_HUMAN",
    "COMPLETION_ORPHAN",
    "COMPLETION_PROTOCOL",
    "COMPLETION_STRUCTURED_AND_CONTINUATION",
    "COMPLETION_STRUCTURED_VISIBLE",
    "catalog_order_must_not_orphan",
    "classify_empty_text_early_return",
    "customer_authored_catalog_order_text",
    "is_structured_catalog_order_inbound",
    "maybe_restore_catalog_order_semantic_text",
    "should_continue_structured_catalog_order",
]

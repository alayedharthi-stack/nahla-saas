"""
general_media_reply_guard.py
────────────────────────────
Platform-wide ownership for general inbound images without a customer caption.

Machine-derived vision/OCR must not hijack identity/collaboration routes; when the
customer sends media with no authored ask, the system routes a safe LLM brief
instead of commerce escalation or inappropriate refusal.
"""
from __future__ import annotations

import logging
from typing import Any, Optional

logger = logging.getLogger("nahla.brain.general_media_reply_guard")

TOPIC_IMAGE_ACK_OR_CLARIFY = "image_ack_or_clarify"


def compose_image_ack_or_clarify_goal() -> str:
    return (
        "image_ack_or_clarify — The customer sent an image without a clear written "
        "request in their caption. Acknowledge that you received the image in natural "
        "Saudi Arabic. Do not claim unsupported facts about image contents. Do not "
        "identify people or brands unless the customer explicitly asked. Do not refuse "
        "or say you cannot help unless policy requires it. Ask naturally how you can "
        "help with the image or what they would like to know. Do not route to catalog, "
        "staff contact, or checkout unless the customer asks. Keep tone warm and brief."
    )


def _inbound_metadata(ctx: Any) -> dict:
    profile = getattr(ctx, "profile", None) or {}
    if not isinstance(profile, dict):
        return {}
    meta = profile.get("inbound_metadata")
    return meta if isinstance(meta, dict) else {}


def _commerce_already_blocked(ctx: Any, message: str) -> bool:
    if bool(getattr(ctx, "block_commerce_escalation", False)):
        return True
    intent = getattr(ctx, "intent", None)
    slots = getattr(intent, "slots", None) or {}
    if isinstance(slots, dict) and slots.get("block_commerce_escalation"):
        return True
    try:
        from modules.ai.brain.intent.non_commerce_classifier import (  # noqa: PLC0415
            resolve_commerce_block,
        )

        blocked = resolve_commerce_block(
            message or "",
            inbound_metadata=_inbound_metadata(ctx),
        )
        return bool(blocked and blocked.block_commerce)
    except Exception:  # noqa: BLE001
        return False


def _has_operational_caption_signal(caption: str) -> bool:
    text = (caption or "").strip()
    if not text:
        return False
    try:
        from modules.ai.brain.commerce.link_intent import (  # noqa: PLC0415
            LinkIntentType,
            resolve_inbound_link_intent,
        )

        if resolve_inbound_link_intent(text) != LinkIntentType.UNKNOWN_LINK:
            return True
    except Exception:  # noqa: BLE001
        pass
    try:
        from modules.ai.brain.commerce.staff_contact_evidence import (  # noqa: PLC0415
            classify_staff_contact_request,
        )

        if classify_staff_contact_request(text).kind != "none":
            return True
    except Exception:  # noqa: BLE001
        pass
    try:
        from modules.ai.brain.commerce.product_visual import (  # noqa: PLC0415
            is_product_visual_request,
        )

        if is_product_visual_request(text):
            return True
    except Exception:  # noqa: BLE001
        pass
    try:
        from modules.ai.brain.commerce.entity_extraction_guard import (  # noqa: PLC0415
            has_explicit_purchase_intent,
        )

        if has_explicit_purchase_intent(text):
            return True
    except Exception:  # noqa: BLE001
        pass
    return False


def try_general_media_ack_decision(ctx: Any, *, route: str = "") -> Optional[Any]:
    from modules.ai.brain.commerce.link_intent_media_source_guard import (  # noqa: PLC0415
        link_intent_message,
    )
    from modules.ai.brain.commerce.staff_contact_media_source_guard import (  # noqa: PLC0415
        is_media_framed_inbound_message,
    )
    from modules.ai.brain.decision.actions import ACTION_LLM_REPLY  # noqa: PLC0415
    from modules.ai.brain.types import Decision  # noqa: PLC0415

    full_msg = (getattr(ctx, "message", None) or "").strip()
    if not full_msg or not is_media_framed_inbound_message(full_msg):
        return None

    caption = link_intent_message(full_msg).strip()
    if caption:
        if _has_operational_caption_signal(caption):
            return None
        return None

    if _commerce_already_blocked(ctx, full_msg):
        return None

    logger.info(
        "[GENERAL_MEDIA_REPLY] tenant=%s route=%s topic=%s preview=%r",
        getattr(ctx, "tenant_id", None),
        route or "-",
        TOPIC_IMAGE_ACK_OR_CLARIFY,
        full_msg[:80],
    )
    return Decision(
        action=ACTION_LLM_REPLY,
        args={
            "topic": TOPIC_IMAGE_ACK_OR_CLARIFY,
            "block_commerce_escalation": True,
            "response_goal": compose_image_ack_or_clarify_goal(),
        },
        reason="general media inbound without customer caption — safe ack/clarify",
        confidence=0.91,
    )


__all__ = [
    "TOPIC_IMAGE_ACK_OR_CLARIFY",
    "compose_image_ack_or_clarify_goal",
    "try_general_media_ack_decision",
]

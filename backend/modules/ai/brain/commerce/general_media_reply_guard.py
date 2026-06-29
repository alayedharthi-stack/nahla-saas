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
import re
from typing import Any, Dict, List, Optional

logger = logging.getLogger("nahla.brain.general_media_reply_guard")

TOPIC_IMAGE_ACK_OR_CLARIFY = "image_ack_or_clarify"

_HANDLE_RE = re.compile(r"@[\w\u0600-\u06FF_.]{2,64}", re.UNICODE)
_HASHTAG_RE = re.compile(r"#[\w\u0600-\u06FF_.]{2,64}", re.UNICODE)
_URL_RE = re.compile(r"https?://\S+|www\.\S+", re.IGNORECASE)

_SOCIAL_SCENE_RE = re.compile(
    r"(?:"
    r"منشور|منصة\s*اجتماع|social\s*media|tiktok|instagram|snapchat|"
    r"screenshot|لقطة\s*شاشة|reels?|story|stories"
    r")",
    re.IGNORECASE | re.UNICODE,
)
_PRODUCT_SCENE_RE = re.compile(
    r"(?:"
    r"منتج|عبوة|product|packaging|بطاقة\s*سعر|price\s*tag|"
    r"رف|shelf|catalog\s*card"
    r")",
    re.IGNORECASE | re.UNICODE,
)
_PEOPLE_COUNT_RES = (
    re.compile(r"(?:person|people|individual|figure)s?\s*(?:visible|shown|appear)?", re.I),
    re.compile(r"(\d+)\s*(?:people|persons|individuals|figures)", re.I),
    re.compile(r"(?:يظهر|تظهر|يوجد|توجد)\s*(?:شخص|شخصان|شخصين|أشخاص|اشخاص)", re.UNICODE),
    re.compile(r"(?:person|people|شخص|أشخاص|اشخاص)", re.UNICODE | re.I),
)
_OBJECT_RES = (
    re.compile(r"(?:cup|mug|glass|bottle|كوب|كأس|زجاجة|علبة)", re.UNICODE | re.I),
    re.compile(r"(?:phone|mobile|هاتف|جوال|smartphone)", re.UNICODE | re.I),
    re.compile(r"(?:food|meal|dish|طعام|وجبة|طبق)", re.UNICODE | re.I),
    re.compile(r"(?:text|caption|overlay|comment|نص|تعليق|تفاعل|likes?|hearts?)", re.UNICODE | re.I),
)

_PAYMENT_IMAGE_KINDS = frozenset({
    "payment_receipt",
    "payment_pre_review",
    "payment_pending_evidence",
})


def compose_image_ack_or_clarify_goal(
    *,
    safe_image_facts: Optional[Dict[str, Any]] = None,
) -> str:
    lines = [
        "image_ack_or_clarify — The customer sent an image without a clear written "
        "request in their caption. Acknowledge that you received the image in natural "
        "Saudi Arabic.",
        "Use ONLY the SAFE_IMAGE_FACTS provided in known_facts when present — describe "
        "the image generally and safely so the customer sees you understood it.",
        "When SAFE_IMAGE_FACTS are present, do NOT ask the customer to describe what is "
        "in the image — you already have supported safe visual facts.",
        "After a brief safe description from facts, ask what they want help with "
        "regarding the image (e.g. وش تبغاني أوضح لك فيها؟).",
        "Use Saudi dialect (وش/إيش), never Iraqi/Gulf-non-Saudi markers like شنو/عايز/إزاي/بتاع.",
        "Do NOT reference prior conversation topics, catalog items, or availability subjects "
        "unless the customer caption explicitly mentions them.",
        "Do NOT identify people, name individuals, or treat @handles / #hashtags as "
        "staff/contact targets.",
        "Do NOT infer intent, identity, or brand ownership beyond SAFE_IMAGE_FACTS.",
        "Do NOT refuse or say you cannot help unless policy requires it.",
        "Ask naturally how you can help with the image or what they would like to know.",
        "Do NOT route to catalog, staff contact, or checkout unless the customer asks.",
        "Keep tone warm and brief.",
    ]
    facts = dict(safe_image_facts or {})
    if facts:
        scene = str(facts.get("scene_type") or "").strip()
        if scene:
            lines.append(f"Scene type: {scene}")
        elements = facts.get("visible_elements") or []
        if isinstance(elements, list) and elements:
            lines.append("Visible elements: " + "; ".join(str(e) for e in elements[:8]))
        text_summary = str(facts.get("visible_text_summary") or "").strip()
        if text_summary:
            lines.append(f"Visible text (sanitized): {text_summary}")
        safety = facts.get("safety_notes") or []
        if isinstance(safety, list) and safety:
            lines.append("Safety notes: " + "; ".join(str(n) for n in safety[:6]))
    return " | ".join(lines)


def _inbound_metadata(ctx: Any) -> dict:
    profile = getattr(ctx, "profile", None) or {}
    if not isinstance(profile, dict):
        return {}
    meta = profile.get("inbound_metadata")
    return meta if isinstance(meta, dict) else {}


def _vision_body_from_message(message: str) -> str:
    from modules.ai.brain.commerce.product_visual import strip_bot_media_framing  # noqa: PLC0415

    return strip_bot_media_framing(message or "").strip()


def _sanitize_visible_text(raw: str, *, max_len: int = 160) -> str:
    text = (raw or "").strip()
    if not text:
        return ""
    text = _HANDLE_RE.sub("", text)
    text = _HASHTAG_RE.sub("", text)
    text = _URL_RE.sub("", text)
    text = re.sub(r"\s+", " ", text).strip(" ,.;:-")
    if len(text) > max_len:
        text = text[: max_len - 1].rstrip() + "…"
    return text


def _detect_scene_type(blob: str, *, meta: dict) -> str:
    category = str(meta.get("media_semantic_category") or "").strip().lower()
    if category in {"social", "non_commerce", "religious", "sticker"}:
        return "social_media_screenshot"
    if category in {"product", "catalog"}:
        return "product_or_catalog_image"
    if _SOCIAL_SCENE_RE.search(blob):
        return "social_media_screenshot"
    if _PRODUCT_SCENE_RE.search(blob):
        return "product_or_catalog_image"
    if str(meta.get("image_kind") or "") == "map_screenshot":
        return "map_screenshot"
    return "general_photo"


def _extract_visible_elements(blob: str) -> List[str]:
    elements: List[str] = []
    lower = (blob or "").lower()

    for patterns, label in (
        (_PEOPLE_COUNT_RES, "people_visible"),
        (_OBJECT_RES[:1], "cup_or_drink_visible"),
        (_OBJECT_RES[1:2], "phone_visible"),
        (_OBJECT_RES[2:3], "food_visible"),
        (_OBJECT_RES[3:4], "on_screen_text_or_interactions"),
    ):
        for pat in patterns:
            if pat.search(blob) or pat.search(lower):
                if label not in elements:
                    elements.append(label)
                break

    if _SOCIAL_SCENE_RE.search(blob):
        if "social_platform_ui" not in elements:
            elements.append("social_platform_ui")
    if _PRODUCT_SCENE_RE.search(blob):
        if "product_packaging" not in elements:
            elements.append("product_packaging")

    return elements[:8]


def _humanize_elements(elements: List[str]) -> List[str]:
    mapping = {
        "people_visible": "people appear in the image",
        "cup_or_drink_visible": "a cup or drink is visible",
        "phone_visible": "a phone is visible",
        "food_visible": "food or a meal is visible",
        "on_screen_text_or_interactions": "on-screen text or interaction markers",
        "social_platform_ui": "social-platform interface elements",
        "product_packaging": "product packaging or shelf display",
    }
    return [mapping.get(e, e) for e in elements if e in mapping]


def build_safe_general_image_facts(
    *,
    message: str = "",
    inbound_metadata: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    """Build deterministic safe visual facts for general image compose."""
    meta = dict(inbound_metadata or {})
    image_kind = str(meta.get("image_kind") or "").strip()
    if image_kind in _PAYMENT_IMAGE_KINDS:
        return {}

    vision = str(meta.get("vision_text") or "").strip()
    body = vision or _vision_body_from_message(message)
    if not body:
        return {}

    scene_type = _detect_scene_type(body, meta=meta)
    raw_elements = _extract_visible_elements(body)
    visible_elements = _humanize_elements(raw_elements)

    visible_text_summary = _sanitize_visible_text(body)
    safety_notes = [
        "Do not identify people or name individuals.",
        "Do not treat handles or hashtags as contact targets.",
        "Do not claim operational facts beyond visible elements.",
    ]

    facts: Dict[str, Any] = {
        "scene_type": scene_type,
        "visible_elements": visible_elements,
        "safety_notes": safety_notes,
    }
    if visible_text_summary:
        facts["visible_text_summary"] = visible_text_summary
    return facts


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
    except Exception:  # noqa: BLE001  # noqa: silent-ok — optional non-commerce block probe
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
    except Exception:  # noqa: BLE001  # noqa: silent-ok — optional link intent probe
        pass
    try:
        from modules.ai.brain.commerce.staff_contact_evidence import (  # noqa: PLC0415
            classify_staff_contact_request,
        )

        if classify_staff_contact_request(text).kind != "none":
            return True
    except Exception:  # noqa: BLE001  # noqa: silent-ok — optional staff contact probe
        pass
    try:
        from modules.ai.brain.commerce.product_visual import (  # noqa: PLC0415
            is_product_visual_request,
        )

        if is_product_visual_request(text):
            return True
    except Exception:  # noqa: BLE001  # noqa: silent-ok — optional product visual probe
        pass
    try:
        from modules.ai.brain.commerce.entity_extraction_guard import (  # noqa: PLC0415
            has_explicit_purchase_intent,
        )

        if has_explicit_purchase_intent(text):
            return True
    except Exception:  # noqa: BLE001  # noqa: silent-ok — optional purchase intent probe
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

    meta = _inbound_metadata(ctx)
    image_kind = str(meta.get("image_kind") or "").strip()
    if image_kind in _PAYMENT_IMAGE_KINDS:
        return None

    safe_image_facts = build_safe_general_image_facts(
        message=full_msg,
        inbound_metadata=meta,
    )

    args: Dict[str, Any] = {
        "topic": TOPIC_IMAGE_ACK_OR_CLARIFY,
        "block_commerce_escalation": True,
        "response_goal": compose_image_ack_or_clarify_goal(
            safe_image_facts=safe_image_facts or None,
        ),
    }
    if safe_image_facts:
        args["safe_image_facts"] = safe_image_facts

    logger.info(
        "[GENERAL_MEDIA_REPLY] tenant=%s route=%s topic=%s preview=%r "
        "safe_facts=%s",
        getattr(ctx, "tenant_id", None),
        route or "-",
        TOPIC_IMAGE_ACK_OR_CLARIFY,
        full_msg[:80],
        bool(safe_image_facts),
    )
    return Decision(
        action=ACTION_LLM_REPLY,
        args=args,
        reason="general media inbound without customer caption — safe ack/clarify",
        confidence=0.91,
    )


__all__ = [
    "TOPIC_IMAGE_ACK_OR_CLARIFY",
    "build_safe_general_image_facts",
    "compose_image_ack_or_clarify_goal",
    "try_general_media_ack_decision",
]

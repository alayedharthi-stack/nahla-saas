"""
brain/commerce/product_media.py
─────────────────────────────────
Typed product-media turn detection and LLM response_goal (P1-E).

Operational routing/facts may be deterministic; reply wording stays LLM-owned.
"""
from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass
from typing import Any, Mapping, Optional, Sequence

from modules.ai.brain.intent.non_commerce_classifier import (
    has_product_commerce_signal,
    inbound_has_occasion_signal,
)
from modules.ai.brain.types import (
    INTENT_ASK_PRICE,
    INTENT_ASK_PRODUCT,
    INTENT_ASK_SHIPPING,
    INTENT_PAY_NOW,
    INTENT_PRODUCT_VISUAL_REQUEST,
    INTENT_SOCIAL,
    INTENT_START_ORDER,
    INTENT_TRACK_ORDER,
)

PRODUCT_MEDIA_TOPIC = "product_media"

_PRODUCT_MEDIA_TOPIC_HINTS: frozenset[str] = frozenset({
    "نحل_أو_عسل",
    "منتج_أو_شراء",
})

_SOCIAL_ONLY_TOPIC_HINTS: frozenset[str] = frozenset({
    "دعاء_أو_تهنئة",
})

_MEDIA_ORIGIN_MARKERS: tuple[str, ...] = (
    "[فيديو من العميل]",
    "[وصف الصورة",
    "[وصف الفيديو",
    "[وصف الصورة المرسلة]",
)

_EXPLICIT_COMMERCE_INTENTS: frozenset[str] = frozenset({
    INTENT_ASK_PRICE,
    INTENT_ASK_PRODUCT,
    INTENT_START_ORDER,
    INTENT_PAY_NOW,
    INTENT_ASK_SHIPPING,
    INTENT_TRACK_ORDER,
    INTENT_PRODUCT_VISUAL_REQUEST,
})

_NORM_RE = re.compile(r"[\u064B-\u065F\u0670\u0640]")
_WS_RE = re.compile(r"\s+")


def _norm(text: str) -> str:
    if not text:
        return ""
    t = unicodedata.normalize("NFKC", str(text)).strip().lower()
    t = _NORM_RE.sub("", t)
    t = (
        t.replace("أ", "ا").replace("إ", "ا").replace("آ", "ا")
        .replace("ى", "ي").replace("ة", "ه")
    )
    return _WS_RE.sub(" ", t).strip()


def has_active_order_evidence(commerce_bundle: Optional[Mapping[str, Any]]) -> bool:
    """True when structured state proves an open/post-order thread."""
    if not commerce_bundle:
        return False
    try:
        from core.active_order_context import structured_indicates_post_order  # noqa: PLC0415

        return bool(structured_indicates_post_order(dict(commerce_bundle)))
    except Exception:  # noqa: BLE001
        return False


def _topic_hints_from_meta(meta: Mapping[str, Any]) -> list[str]:
    raw = meta.get("topic_hints")
    if not isinstance(raw, list):
        return []
    return [str(x) for x in raw if x]


def _has_product_media_topic_hints(hints: Sequence[str]) -> bool:
    return any(h in _PRODUCT_MEDIA_TOPIC_HINTS for h in hints)


def _is_social_only_media(hints: Sequence[str], text: str) -> bool:
    if not hints:
        return False
    if _has_product_media_topic_hints(hints):
        return False
    if any(h in _SOCIAL_ONLY_TOPIC_HINTS for h in hints):
        return True
    if inbound_has_occasion_signal(text):
        return True
    return False


def _is_customer_media_origin(text: str, meta: Mapping[str, Any]) -> bool:
    if any(m in (text or "") for m in _MEDIA_ORIGIN_MARKERS):
        return True
    source = str(meta.get("source_type") or meta.get("normalized_type") or "").lower()
    return source in {"video", "image", "document"}


def _vision_evidence(meta: Mapping[str, Any], text: str) -> tuple[bool, str]:
    vision = str(meta.get("frame_vision_text") or "").strip()
    if meta.get("frame_vision_status") == "ok" and vision:
        return True, vision
    for line in (text or "").splitlines():
        if line.startswith("النص الظاهر/الوصف من الفيديو:"):
            body = line.split(":", 1)[-1].strip()
            if body:
                return True, body
        if line.startswith("[وصف الصورة") or line.startswith("[وصف الفيديو"):
            body = line.split("]", 1)[-1].strip() if "]" in line else line
            if len(body) >= 12:
                return True, body
    return False, ""


@dataclass(frozen=True)
class ProductMediaVerdict:
    matched: bool
    has_vision_evidence: bool = False
    has_hint_only: bool = False
    vision_preview: str = ""
    reason: str = ""


def detect_product_media_turn(
    text: str,
    *,
    inbound_metadata: Optional[Mapping[str, Any]] = None,
    intent_name: Optional[str] = None,
    commerce_blocked: bool = False,
) -> ProductMediaVerdict:
    """Return whether this turn should use the typed product-media LLM goal."""
    if commerce_blocked:
        return ProductMediaVerdict(matched=False, reason="commerce_blocked")

    name = (intent_name or "").strip().lower()
    if name in _EXPLICIT_COMMERCE_INTENTS:
        return ProductMediaVerdict(matched=False, reason="explicit_commerce_intent")
    if name == INTENT_SOCIAL:
        return ProductMediaVerdict(matched=False, reason="social_intent")

    meta = inbound_metadata if isinstance(inbound_metadata, dict) else {}
    hints = _topic_hints_from_meta(meta)
    raw = (text or "").strip()
    if not raw and not hints:
        return ProductMediaVerdict(matched=False, reason="empty")

    if _is_social_only_media(hints, raw):
        return ProductMediaVerdict(matched=False, reason="social_only_media")

    try:
        from ..state.product_information_topic import (  # noqa: PLC0415
            detect_product_information_topic_shift,
        )

        if detect_product_information_topic_shift(raw):
            return ProductMediaVerdict(
                matched=False,
                reason="product_attribute_or_usage_question",
            )
    except Exception:  # noqa: BLE001  # noqa: silent-ok — optional product info topic import
        pass

    has_media_origin = _is_customer_media_origin(raw, meta)
    has_product_signal = has_product_commerce_signal(raw, topic_hints=hints)
    has_pm_hints = _has_product_media_topic_hints(hints)

    if not (has_media_origin or has_pm_hints or has_product_signal):
        return ProductMediaVerdict(matched=False, reason="no_product_media_signal")

    has_vision, vision_preview = _vision_evidence(meta, raw)
    has_hint_only = bool(has_pm_hints or has_product_signal) and not has_vision

    return ProductMediaVerdict(
        matched=True,
        has_vision_evidence=has_vision,
        has_hint_only=has_hint_only,
        vision_preview=vision_preview[:240],
        reason="product_media_signal",
    )


def compose_product_media_response_goal(
    *,
    has_vision_evidence: bool,
    has_hint_only: bool,
    active_order_evidence: bool,
    vision_preview: str = "",
) -> str:
    """Strict LLM goal — tokens/directives only, never canned customer copy."""
    lines = [
        "product_media — Generate a short natural Saudi Arabic WhatsApp reply. "
        "The customer shared product/process media or product information "
        "(video, image, or descriptive text) — not a price/buy/track request. "
        "Respond like a warm merchant assistant on WhatsApp: acknowledge what "
        "is visible or stated, then offer ONE useful next step such as: "
        "drafting a short video caption, publish bullet points, turning details "
        "into product description, or organizing harvest/source/date facts. "
        "Persona composes wording — do NOT use support-desk hedging or CS templates.",
    ]
    if has_vision_evidence:
        lines.append(
            "vision_evidence=true — you MAY describe what the frame/description "
            "shows; do NOT claim you watched the full video beyond the provided "
            "description."
        )
        if vision_preview:
            lines.append(f"vision_preview: «{vision_preview[:180]}»")
    elif has_hint_only:
        lines.append(
            "vision_evidence=false — hints/caption only; phrase with appropriate "
            "confidence (avoid claiming you saw the clip); ask what they want "
            "to do with the media (caption, publish points, product info)."
        )
    else:
        lines.append(
            "Use available caption/text signals; if unclear, ask one open "
            "question about how they want to use the content."
        )

    lines.extend([
        "Do NOT open with or repeat «يبدو أنك تعرض» across turns — vary "
        "acknowledgment naturally.",
        "Do NOT end with standalone «شكرًا على المعلومات» without a useful offer.",
        "Do NOT say «لم أتمكن/لم أستطيع/لا أستطيع … مشاهدة/رؤية/شوف … الفيديو» "
        "when any vision text, caption, or product hint exists.",
        "Do NOT pitch checkout or [PRODUCT:…] unless the customer asks to buy.",
        "Do NOT use rigid FAQ/service closers.",
    ])

    if active_order_evidence:
        lines.append(
            "active_order_evidence=true — order/shipment mention allowed ONLY if "
            "directly relevant to the customer's media or stated question."
        )
    else:
        lines.append(
            "active_order_evidence=false — do NOT mention طلب/شحنة/تتبع or "
            "«حول طلبك أو الشحنة»."
        )

    return " | ".join(lines)


def build_product_media_decision_args(
    verdict: ProductMediaVerdict,
    *,
    commerce_bundle: Optional[Mapping[str, Any]] = None,
) -> dict[str, Any]:
    active = has_active_order_evidence(commerce_bundle)
    goal = compose_product_media_response_goal(
        has_vision_evidence=verdict.has_vision_evidence,
        has_hint_only=verdict.has_hint_only,
        active_order_evidence=active,
        vision_preview=verdict.vision_preview,
    )
    return {
        "topic": PRODUCT_MEDIA_TOPIC,
        "product_media": True,
        "response_goal": goal,
        "has_vision_evidence": verdict.has_vision_evidence,
        "active_order_evidence": active,
    }


__all__ = [
    "PRODUCT_MEDIA_TOPIC",
    "ProductMediaVerdict",
    "build_product_media_decision_args",
    "compose_product_media_response_goal",
    "detect_product_media_turn",
    "has_active_order_evidence",
]

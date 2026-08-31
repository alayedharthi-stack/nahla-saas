"""
Pre-brain routing guard for inbound media.

Location / arrival policies must inspect the customer's *current*
message (caption or plain text), never brain-facing extraction from
PDF OCR, vision, or transcripts.

Brain semantic routing is a separate owner. For inbound images, a
successful Vision pass is trusted structured evidence and must reach
Brain. Caption-only stripping stays on resolve_pre_brain_customer_message.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Dict, Optional, Sequence

from modules.ai.brain.commerce.staff_contact_media_source_guard import (
    is_media_framed_inbound_message,
)
from modules.ai.brain.commerce.product_visual import customer_authored_caption

_MEDIA_SOURCE_TYPES = frozenset({"document", "image", "audio", "video", "sticker"})
_AUDIO_INBOUND_TYPES = frozenset({"audio", "voice", "ptt"})
_IMAGE_INBOUND_TYPES = frozenset({"image"})
_TRUSTED_VISION_STATUS = "ok"


def _metadata_dict(inbound_metadata: Optional[Dict[str, Any]]) -> Dict[str, Any]:
    return dict(inbound_metadata or {})


def _inbound_type(
    meta: Dict[str, Any],
    *,
    inbound_normalized_type: Optional[str] = None,
) -> str:
    return str(
        inbound_normalized_type
        or meta.get("normalized_type")
        or meta.get("type")
        or ""
    ).strip().lower()


def _metadata_layers(meta: Dict[str, Any]) -> list[Dict[str, Any]]:
    nested = meta.get("normalized_inbound")
    sources: list[Dict[str, Any]] = [meta]
    if isinstance(nested, dict):
        sources.insert(0, nested)
    return sources


def _trusted_transcript_from_metadata(meta: Dict[str, Any]) -> str:
    for source in _metadata_layers(meta):
        for key in ("transcript_text", "transcript"):
            val = str(source.get(key) or "").strip()
            if val:
                return val
    return ""


def trusted_vision_text_from_metadata(
    inbound_metadata: Optional[Dict[str, Any]] = None,
) -> str:
    """Return trusted image Vision text from structured normalized metadata.

    Trusted only when ``vision_status`` is the successful structured
    pass and ``vision_text`` is non-empty. Does not parse customer
    wording. Empty when Vision failed, skipped, or was never run.
    """
    for source in _metadata_layers(_metadata_dict(inbound_metadata)):
        status = str(source.get("vision_status") or "").strip().lower()
        text = str(source.get("vision_text") or "").strip()
        if status == _TRUSTED_VISION_STATUS and text:
            return text
    return ""


def resolve_semantic_customer_message(
    *,
    brain_text: str,
    inbound_metadata: Optional[Dict[str, Any]] = None,
    inbound_normalized_type: Optional[str] = None,
) -> str:
    """Resolve the routing/brain semantic message for the current inbound turn."""
    meta = _metadata_dict(inbound_metadata)
    text = (brain_text or "").strip()
    inbound_kind = _inbound_type(
        meta, inbound_normalized_type=inbound_normalized_type,
    )
    try:
        from modules.ai.media.customer_turn_completion import (  # noqa: PLC0415
            is_structured_catalog_order_inbound,
        )

        # Native catalog_order is a structured commerce event. Keep any
        # real customer note; do not treat the operational frame marker
        # as customer-authored language.
        if is_structured_catalog_order_inbound(meta, text):
            from modules.ai.media.customer_turn_completion import (  # noqa: PLC0415
                customer_authored_catalog_order_text,
            )

            return customer_authored_catalog_order_text(meta, text)
    except Exception:  # noqa: BLE001  # noqa: silent-ok — catalog preserve must not block routing
        pass

    # Image Brain path: a successful Vision pass is trusted structured
    # evidence. Do not strip it to customer-caption-only, or an image
    # without a caption becomes empty and never reaches Brain.
    # Location/arrival still use resolve_pre_brain_customer_message
    # (caption-only). Document/PDF and video are unchanged here.
    if inbound_kind in _IMAGE_INBOUND_TYPES:
        vision = trusted_vision_text_from_metadata(meta)
        if vision:
            if text:
                return text
            return vision

    if is_media_framed_inbound_message(text):
        text = customer_authored_caption(text)

    transcript = _trusted_transcript_from_metadata(meta)
    if inbound_kind in _AUDIO_INBOUND_TYPES:
        if text:
            return text
        return transcript
    return text


def is_audio_without_trusted_transcript(
    inbound_metadata: Optional[Dict[str, Any]] = None,
    *,
    semantic_message: str = "",
    inbound_normalized_type: Optional[str] = None,
) -> bool:
    """True when inbound is audio but no trusted transcript is available."""
    meta = _metadata_dict(inbound_metadata)
    if _inbound_type(meta, inbound_normalized_type=inbound_normalized_type) not in _AUDIO_INBOUND_TYPES:
        return False
    if str(semantic_message or "").strip():
        return False
    return not _trusted_transcript_from_metadata(meta)


def should_route_unclear_audio_to_existing_order_support(
    *,
    inbound_metadata: Optional[Dict[str, Any]] = None,
    semantic_message: str = "",
    inbound_normalized_type: Optional[str] = None,
    history: Optional[Sequence[Any]] = None,
    brain_state: Optional[Dict[str, Any]] = None,
) -> bool:
    """True when unclear audio should own the turn as existing-order support."""
    if not is_audio_without_trusted_transcript(
        inbound_metadata,
        semantic_message=semantic_message,
        inbound_normalized_type=inbound_normalized_type,
    ):
        return False
    try:
        from modules.ai.brain.commerce.order_tracking_intent_guard import (  # noqa: PLC0415
            has_order_reference_support_context,
        )

        return has_order_reference_support_context(
            state=brain_state,
            history=list(history or []),
        )
    except Exception:  # noqa: BLE001  # noqa: silent-ok — order-support probe must not block routing
        return False


@dataclass(frozen=True)
class InboundSemanticRouting:
    semantic_text: str
    route_unclear_audio_order_support: bool


def resolve_inbound_semantic_routing(
    *,
    brain_text: str,
    inbound_metadata: Optional[Dict[str, Any]] = None,
    inbound_normalized_type: Optional[str] = None,
    history: Optional[Sequence[Any]] = None,
    brain_state: Optional[Dict[str, Any]] = None,
) -> InboundSemanticRouting:
    """Resolve semantic text and whether unclear audio should own order support."""
    semantic = resolve_semantic_customer_message(
        brain_text=brain_text,
        inbound_metadata=inbound_metadata,
        inbound_normalized_type=inbound_normalized_type,
    )
    route_support = False
    if not semantic.strip():
        route_support = should_route_unclear_audio_to_existing_order_support(
            inbound_metadata=inbound_metadata,
            semantic_message=semantic,
            inbound_normalized_type=inbound_normalized_type,
            history=history,
            brain_state=brain_state,
        )
    return InboundSemanticRouting(
        semantic_text=semantic,
        route_unclear_audio_order_support=route_support,
    )


def resolve_pre_brain_customer_message(
    *,
    brain_text: str,
    inbound_metadata: Optional[Dict[str, Any]] = None,
) -> str:
    """Return the customer-visible text for location/arrival pre-brain guards."""
    text = (brain_text or "").strip()
    if is_media_framed_inbound_message(text):
        return customer_authored_caption(text)
    ni = _metadata_dict(inbound_metadata)
    src = str(ni.get("source_type") or "").lower()
    if src in _MEDIA_SOURCE_TYPES:
        caption = str(ni.get("caption") or "").strip()
        return caption
    return text


def should_skip_contact_routing_for_media(
    inbound_metadata: Optional[Dict[str, Any]] = None,
) -> bool:
    """True when inbound is media without a customer caption."""
    ni = dict(inbound_metadata or {})
    src = str(ni.get("source_type") or "").lower()
    if src not in _MEDIA_SOURCE_TYPES:
        return False
    return not str(ni.get("caption") or "").strip()

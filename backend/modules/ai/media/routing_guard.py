"""
Pre-brain routing guard for inbound media.

Location / arrival policies must inspect the customer's *current*
message (caption or plain text), never brain-facing extraction from
PDF OCR, vision, or transcripts.
"""
from __future__ import annotations

from typing import Any, Dict, Optional

from modules.ai.brain.commerce.staff_contact_media_source_guard import (
    is_media_framed_inbound_message,
)
from modules.ai.brain.commerce.product_visual import customer_authored_caption

_MEDIA_SOURCE_TYPES = frozenset({"document", "image", "audio", "video", "sticker"})
_AUDIO_INBOUND_TYPES = frozenset({"audio", "voice", "ptt"})


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


def _trusted_transcript_from_metadata(meta: Dict[str, Any]) -> str:
    nested = meta.get("normalized_inbound")
    sources: list[Dict[str, Any]] = [meta]
    if isinstance(nested, dict):
        sources.insert(0, nested)
    for source in sources:
        for key in ("transcript_text", "transcript"):
            val = str(source.get(key) or "").strip()
            if val:
                return val
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
    if is_media_framed_inbound_message(text):
        text = customer_authored_caption(text)

    transcript = _trusted_transcript_from_metadata(meta)
    if _inbound_type(meta, inbound_normalized_type=inbound_normalized_type) in _AUDIO_INBOUND_TYPES:
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

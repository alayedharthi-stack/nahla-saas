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


def resolve_pre_brain_customer_message(
    *,
    brain_text: str,
    inbound_metadata: Optional[Dict[str, Any]] = None,
) -> str:
    """Return the customer-visible text for location/arrival pre-brain guards."""
    text = (brain_text or "").strip()
    if is_media_framed_inbound_message(text):
        return customer_authored_caption(text)
    ni = dict(inbound_metadata or {})
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

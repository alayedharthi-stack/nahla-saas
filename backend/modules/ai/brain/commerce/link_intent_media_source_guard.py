"""
link_intent_media_source_guard.py
─────────────────────────────────
Platform-wide guard: OCR / vision text must not activate storefront,
physical-location, or other operational link routes unless the customer
explicitly asked in their own caption — not in machine-derived media text.
"""
from __future__ import annotations

from modules.ai.brain.commerce.product_visual import customer_authored_caption
from modules.ai.brain.commerce.staff_contact_media_source_guard import (
    is_media_framed_inbound_message,
)


def link_intent_message(message: str) -> str:
    """
    Text eligible for link-intent detection (storefront, maps, product URL).

    For media-framed inbounds, only the customer-authored caption counts —
    never vision/OCR bodies appended by the normalizer.
    """
    raw = (message or "").strip()
    if not raw:
        return ""
    if is_media_framed_inbound_message(raw):
        return customer_authored_caption(raw)
    return raw


__all__ = [
    "is_media_framed_inbound_message",
    "link_intent_message",
]

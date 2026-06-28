"""
staff_contact_media_source_guard.py
───────────────────────────────────
Platform-wide guard: OCR / vision / social-media caption text must not
become staff/contact targets unless the customer explicitly asks in their
own message (caption), not in machine-derived media description.
"""
from __future__ import annotations

from modules.ai.brain.commerce.product_visual import customer_authored_caption


def is_media_framed_inbound_message(message: str) -> bool:
    """True when normalizer added vision/OCR/video framing to the inbound body."""
    raw = (message or "").strip()
    if not raw:
        return False
    return customer_authored_caption(raw) != raw


def staff_contact_intent_message(message: str) -> str:
    """
    Text eligible for staff/contact intent detection and name extraction.

    For media-framed inbounds, only the customer-authored caption counts —
    never Teddy&Abuk-style handles from screenshots or vision OCR.
    """
    raw = (message or "").strip()
    if not raw:
        return ""
    if is_media_framed_inbound_message(raw):
        return customer_authored_caption(raw)
    return raw


__all__ = [
    "is_media_framed_inbound_message",
    "staff_contact_intent_message",
]

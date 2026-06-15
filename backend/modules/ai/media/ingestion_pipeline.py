"""
Unified media ingestion pipeline entry point.

Delegates to ``normalizer.normalize_whatsapp_inbound`` — the single
platform-wide path for inbound WhatsApp media (PDF, image, audio, video).
"""
from .display_guard import (
    apply_media_display_outbound_guard,
    looks_like_media_extraction_dump,
)
from .normalizer import (
    MediaNormalizationResult,
    inbound_persist_body,
    normalize_whatsapp_inbound,
)

__all__ = [
    "MediaNormalizationResult",
    "apply_media_display_outbound_guard",
    "inbound_persist_body",
    "looks_like_media_extraction_dump",
    "normalize_whatsapp_inbound",
]

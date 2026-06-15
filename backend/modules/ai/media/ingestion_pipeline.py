"""
Unified media ingestion pipeline entry point.

Delegates to ``normalizer.normalize_whatsapp_inbound`` — the single
platform-wide path for inbound WhatsApp media (PDF, image, audio, video).
"""
from .display_guard import (
    apply_media_display_outbound_guard,
    looks_like_media_extraction_dump,
)
from .document_display import (
    DOCUMENT_CARD_FALLBACK_AR,
    is_readable_document_summary,
    safe_document_summary_for_display,
)
from .normalizer import (
    MediaNormalizationResult,
    inbound_persist_body,
    normalize_whatsapp_inbound,
)
from .payment_evidence_hints import (
    attach_payment_evidence_hints,
    extract_payment_evidence_hints,
    safe_payment_hints_for_display,
)
from .routing_guard import (
    resolve_pre_brain_customer_message,
    should_skip_contact_routing_for_media,
)

__all__ = [
    "DOCUMENT_CARD_FALLBACK_AR",
    "MediaNormalizationResult",
    "apply_media_display_outbound_guard",
    "attach_payment_evidence_hints",
    "extract_payment_evidence_hints",
    "inbound_persist_body",
    "is_readable_document_summary",
    "looks_like_media_extraction_dump",
    "normalize_whatsapp_inbound",
    "resolve_pre_brain_customer_message",
    "safe_document_summary_for_display",
    "safe_payment_hints_for_display",
    "should_skip_contact_routing_for_media",
]

"""
modules/observability
─────────────────────
Pure helpers used to instrument the runtime so silent UX regressions
become loud, structured logs.

The first inhabitant is :mod:`delivery_mode` — a small audit-and-classify
layer that turns "did the customer actually receive useful content?"
into an explicit log line emitted at the end of every WhatsApp turn.
Adding a helper here is preferred over inlining the logic in
``whatsapp_webhook.py`` because (a) it keeps the long-tail dispatch
function from growing further, and (b) the helpers are pure functions
so they can be exercised with cheap unit tests instead of full
integration scaffolding.
"""
from __future__ import annotations

from .delivery_mode import (
    DELIVERY_MODE_CATALOG,
    DELIVERY_MODE_CTA_ONLY,
    DELIVERY_MODE_FAILED,
    DELIVERY_MODE_IMAGE_CTA,
    DELIVERY_MODE_MEDIA_ONLY,
    DELIVERY_MODE_TEXT_ONLY,
    DeliveryAudit,
    compute_final_delivery_mode,
    customer_wants_product_or_image,
    new_delivery_audit,
)
from .visual_enforcement import (
    SOURCE_FOCUS,
    SOURCE_INBOUND_TEXT,
    SOURCE_LAST_RECOMMENDED,
    SOURCE_LAST_SEARCH,
    SOURCE_NONE,
    has_visual_marker,
    pick_best_candidate_title,
)

__all__ = [
    "DELIVERY_MODE_CATALOG",
    "DELIVERY_MODE_CTA_ONLY",
    "DELIVERY_MODE_FAILED",
    "DELIVERY_MODE_IMAGE_CTA",
    "DELIVERY_MODE_MEDIA_ONLY",
    "DELIVERY_MODE_TEXT_ONLY",
    "DeliveryAudit",
    "compute_final_delivery_mode",
    "customer_wants_product_or_image",
    "new_delivery_audit",
    # visual-product enforcement pure helpers
    "SOURCE_FOCUS",
    "SOURCE_INBOUND_TEXT",
    "SOURCE_LAST_RECOMMENDED",
    "SOURCE_LAST_SEARCH",
    "SOURCE_NONE",
    "has_visual_marker",
    "pick_best_candidate_title",
]

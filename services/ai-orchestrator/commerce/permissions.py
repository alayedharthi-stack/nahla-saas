"""
services/ai-orchestrator/commerce/permissions.py
────────────────────────────────────────────────
Backward-compatible shim for the legacy orchestrator service.

Canonical source of truth:
  backend/modules/ai/commerce/permissions.py

Any legacy imports from services/ai-orchestrator continue to work, but the
actual permission catalog now comes from the canonical AI module.
"""

from modules.ai.commerce.permissions import (  # noqa: F401
    APPLY_COUPON,
    AUTO_GENERATE_COUPON,
    BULK_DELETE,
    CANCEL_ORDER,
    CANCEL_PAID_ORDER,
    CREATE_CHECKOUT_LINK,
    CREATE_DRAFT_ORDER,
    CommercePermissionSet,
    DELETE_COUPON,
    DELETE_CUSTOMER,
    DELETE_ORDER,
    DELETE_PRODUCT,
    HARDCODED_FORBIDDEN,
    HARD_DELETE_DRAFT_ORDER,
    MODIFY_PAID_ORDER,
    PROPOSE_ORDER,
    SEND_MESSAGE,
    SEND_PAYMENT_LINK,
    SUGGEST_BUNDLE,
    SUGGEST_COUPON,
    SUGGEST_PRODUCT,
)

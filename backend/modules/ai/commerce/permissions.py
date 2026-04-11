"""
Commerce Permission Definitions
────────────────────────────────
Defines every action the AI orchestrator can propose, and maps each one to
the permission flag that controls whether this store can execute it.

Architecture rule:
  - Configurable permissions: stored in DB (CommercePermissions model).
    These can be toggled per tenant via the admin panel.
  - Hardcoded-forbidden actions: never permitted regardless of DB state.
    These are not stored in DB — no column, no override path.

This module is the single source of truth for action → permission mapping.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Dict, FrozenSet, Optional

# ── Action type constants ─────────────────────────────────────────────────────

# Configurable (can be toggled per tenant)
CREATE_DRAFT_ORDER       = "create_draft_order"
PROPOSE_ORDER            = "propose_order"         # alias for propose_order from Claude
CREATE_CHECKOUT_LINK     = "create_checkout_link"
SEND_PAYMENT_LINK        = "send_payment_link"
APPLY_COUPON             = "apply_coupon"
SUGGEST_COUPON           = "suggest_coupon"        # alias used by Claude tool
AUTO_GENERATE_COUPON     = "auto_generate_coupon"
CANCEL_ORDER             = "cancel_order"

# Always allowed (no permission flag needed)
SUGGEST_PRODUCT          = "suggest_product"
SUGGEST_BUNDLE           = "suggest_bundle"
SEND_MESSAGE             = "send_message"

# Hardcoded-forbidden (cannot be unlocked by any config)
DELETE_ORDER             = "delete_order"
DELETE_CUSTOMER          = "delete_customer"
DELETE_PRODUCT           = "delete_product"
DELETE_COUPON            = "delete_coupon"
CANCEL_PAID_ORDER        = "cancel_paid_order"
HARD_DELETE_DRAFT_ORDER  = "hard_delete_draft_order"
MODIFY_PAID_ORDER        = "modify_paid_order"
BULK_DELETE              = "bulk_delete"

# ── Permission flag names ─────────────────────────────────────────────────────

_ACTION_TO_PERMISSION: Dict[str, Optional[str]] = {
    # Always allowed — no permission check
    SUGGEST_PRODUCT:         None,
    SUGGEST_BUNDLE:          None,
    SEND_MESSAGE:            None,

    # Require permission flag
    CREATE_DRAFT_ORDER:      "can_create_orders",
    PROPOSE_ORDER:           "can_create_orders",
    CREATE_CHECKOUT_LINK:    "can_create_checkout_links",
    SEND_PAYMENT_LINK:       "can_send_payment_links",
    APPLY_COUPON:            "can_apply_coupons",
    SUGGEST_COUPON:          "can_apply_coupons",
    AUTO_GENERATE_COUPON:    "can_auto_generate_coupons",
    CANCEL_ORDER:            "can_cancel_orders",
}

# These are never permitted — the guard rejects them before any DB check
HARDCODED_FORBIDDEN: FrozenSet[str] = frozenset({
    DELETE_ORDER,
    DELETE_CUSTOMER,
    DELETE_PRODUCT,
    DELETE_COUPON,
    CANCEL_PAID_ORDER,
    HARD_DELETE_DRAFT_ORDER,
    MODIFY_PAID_ORDER,
    BULK_DELETE,
})


# ── Permission set (loaded per tenant) ───────────────────────────────────────

@dataclass
class CommercePermissionSet:
    """Tenant-specific permission snapshot. Loaded once per request."""
    tenant_id: int

    # Defaults mirror the DB model's server_default values
    can_create_orders: bool           = True
    can_create_checkout_links: bool   = True
    can_send_payment_links: bool      = True
    can_apply_coupons: bool           = True
    can_auto_generate_coupons: bool   = True
    can_cancel_orders: bool           = False  # opt-in only

    def is_permitted(self, action_type: str) -> bool:
        """Return True if this action is allowed for this tenant."""
        if action_type in HARDCODED_FORBIDDEN:
            return False
        flag = _ACTION_TO_PERMISSION.get(action_type)
        if flag is None:
            # No flag required → always allowed
            return True
        return bool(getattr(self, flag, False))

    def denial_reason(self, action_type: str) -> str:
        if action_type in HARDCODED_FORBIDDEN:
            return f"'{action_type}' is a forbidden destructive operation and cannot be enabled"
        flag = _ACTION_TO_PERMISSION.get(action_type)
        if flag is None:
            return ""
        if not getattr(self, flag, False):
            return f"store permission '{flag}' is disabled for this tenant"
        return ""

    def to_dict(self) -> Dict:
        return {
            "can_create_orders":           self.can_create_orders,
            "can_create_checkout_links":   self.can_create_checkout_links,
            "can_send_payment_links":      self.can_send_payment_links,
            "can_apply_coupons":           self.can_apply_coupons,
            "can_auto_generate_coupons":   self.can_auto_generate_coupons,
            "can_cancel_orders":           self.can_cancel_orders,
        }

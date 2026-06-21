"""
turn/legacy_owner.py
────────────────────
Map legacy DecisionEngine action → implied turn owner for shadow comparison.
"""
from __future__ import annotations

from typing import Any

from ..decision.actions import (
    ACTION_CLARIFY,
    ACTION_FAQ_REPLY,
    ACTION_GREET,
    ACTION_HANDOFF,
    ACTION_LLM_REPLY,
    ACTION_ORDER_CONTEXT_UPDATE,
    ACTION_OUT_OF_SCOPE,
    ACTION_PLATFORM_REPLY,
    ACTION_PROPOSE_DRAFT_ORDER,
    ACTION_SEARCH_PRODUCTS,
    ACTION_SEND_PAYMENT_LINK,
    ACTION_SOCIAL_REPLY,
    ACTION_SUGGEST_COUPON,
    ACTION_TRACK_ORDER,
)
from .contract import (
    OWNER_CHECKOUT,
    OWNER_DISCOVERY,
    OWNER_ORDERING,
    OWNER_PAYMENT,
    OWNER_PERSONA_SOCIAL,
    OWNER_POST_PURCHASE,
    OWNER_STAFF_ESCALATION,
    OWNER_SUPPORT,
    OWNER_TRACKING,
)

_ACTION_OWNER_MAP = {
    ACTION_GREET: OWNER_PERSONA_SOCIAL,
    ACTION_SOCIAL_REPLY: OWNER_PERSONA_SOCIAL,
    ACTION_PLATFORM_REPLY: OWNER_PERSONA_SOCIAL,
    ACTION_OUT_OF_SCOPE: OWNER_PERSONA_SOCIAL,
    ACTION_CLARIFY: OWNER_PERSONA_SOCIAL,
    ACTION_FAQ_REPLY: OWNER_DISCOVERY,
    ACTION_SEARCH_PRODUCTS: OWNER_DISCOVERY,
    ACTION_SUGGEST_COUPON: OWNER_DISCOVERY,
    ACTION_PROPOSE_DRAFT_ORDER: OWNER_ORDERING,
    ACTION_SEND_PAYMENT_LINK: OWNER_CHECKOUT,
    ACTION_ORDER_CONTEXT_UPDATE: OWNER_CHECKOUT,
    ACTION_TRACK_ORDER: OWNER_TRACKING,
    ACTION_HANDOFF: OWNER_STAFF_ESCALATION,
}


def _llm_reply_owner(decision: Any) -> str:
    args = dict(getattr(decision, "args", None) or {})
    topic = str(args.get("topic") or args.get("response_goal") or "").lower()
    if "complaint" in topic or "refund" in topic or "support" in topic:
        return OWNER_SUPPORT
    if "payment" in topic or "receipt" in topic:
        return OWNER_PAYMENT
    if "track" in topic or "shipment" in topic or "delivery" in topic:
        return OWNER_TRACKING
    if "post_purchase" in topic or "delivered" in topic:
        return OWNER_POST_PURCHASE
    if "order" in topic or "checkout" in topic or "draft" in topic:
        return OWNER_CHECKOUT
    if "product" in topic or "catalog" in topic or "discovery" in topic or "price" in topic:
        return OWNER_DISCOVERY
    if "social" in topic or "greet" in topic or "persona" in topic:
        return OWNER_PERSONA_SOCIAL
    return OWNER_PERSONA_SOCIAL


def legacy_owner_from_decision(decision: Any) -> str:
    """Infer which turn owner the legacy pipeline effectively selected."""
    action = str(getattr(decision, "action", "") or "")
    if action == ACTION_LLM_REPLY:
        return _llm_reply_owner(decision)
    return _ACTION_OWNER_MAP.get(action, OWNER_PERSONA_SOCIAL)


def owners_compatible(proposed: str, legacy: str) -> bool:
    """True when shadow owner and legacy owner are close enough."""
    if proposed == legacy:
        return True
    # Checkout ↔ ordering are often interchangeable in legacy routing.
    if {proposed, legacy} <= {OWNER_CHECKOUT, OWNER_ORDERING}:
        return True
    # Post-purchase gratitude ↔ persona/social overlap.
    if {proposed, legacy} <= {OWNER_POST_PURCHASE, OWNER_PERSONA_SOCIAL}:
        return True
    # Support ↔ post_purchase for complaints after delivery.
    if {proposed, legacy} <= {OWNER_SUPPORT, OWNER_POST_PURCHASE}:
        return True
    return False


__all__ = ["legacy_owner_from_decision", "owners_compatible"]

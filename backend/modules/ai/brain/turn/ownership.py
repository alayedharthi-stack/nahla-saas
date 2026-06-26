"""
turn/ownership.py
─────────────────
Central Conversation Turn Ownership — platform-wide routing authority.

Resolves who owns the current turn and which fallbacks are forbidden.
Discovery, product_discovery_gate, order_context_gate, and
catalog_browse_turn_policy are **consumers** of this layer — they do not
define ownership rules themselves.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Any, FrozenSet, Optional, Set

from ..decision.actions import (
    ACTION_CATALOG_NAVIGATE,
    ACTION_NARROW,
    ACTION_SEARCH_PRODUCTS,
)
from ..types import BrainContext
from .contract import (
    OWNER_CHECKOUT,
    OWNER_DISCOVERY,
    OWNER_HEALTH_ADVISORY,
    OWNER_ORDERING,
    OWNER_PAYMENT,
    OWNER_PERSONA_SOCIAL,
    OWNER_POST_PURCHASE,
    OWNER_STAFF_ESCALATION,
    OWNER_SUPPORT,
    OWNER_TRACKING,
    TurnArbitration,
)

logger = logging.getLogger("nahla.brain.turn_ownership")

FALLBACK_PRODUCT_DISCOVERY = "product_discovery"
FALLBACK_TOP_PRODUCTS = "top_products"
FALLBACK_CATALOG_BROWSE = "catalog_browse"
FALLBACK_STALE_CHECKOUT_SUSPEND = "stale_checkout_suspend"

_DISCOVERY_FALLBACKS = frozenset({
    FALLBACK_PRODUCT_DISCOVERY,
    FALLBACK_TOP_PRODUCTS,
    FALLBACK_CATALOG_BROWSE,
})

_BROWSE_ACTIONS = frozenset({
    ACTION_SEARCH_PRODUCTS,
    ACTION_CATALOG_NAVIGATE,
    ACTION_NARROW,
})

_OWNER_DEFAULT_FORBIDDEN: dict[str, FrozenSet[str]] = {
    OWNER_CHECKOUT: _DISCOVERY_FALLBACKS | {FALLBACK_STALE_CHECKOUT_SUSPEND},
    OWNER_ORDERING: _DISCOVERY_FALLBACKS | {FALLBACK_STALE_CHECKOUT_SUSPEND},
    OWNER_PAYMENT: _DISCOVERY_FALLBACKS,
    OWNER_TRACKING: _DISCOVERY_FALLBACKS,
    OWNER_POST_PURCHASE: _DISCOVERY_FALLBACKS,
    OWNER_SUPPORT: _DISCOVERY_FALLBACKS,
    OWNER_STAFF_ESCALATION: _DISCOVERY_FALLBACKS,
    OWNER_HEALTH_ADVISORY: _DISCOVERY_FALLBACKS,
    OWNER_PERSONA_SOCIAL: frozenset(),
    OWNER_DISCOVERY: frozenset(),
}

_CONTRACT_BROWSE_TOKENS = frozenset({
    "do_not_browse",
    "do_not_push_product_list",
    "do_not_search_products",
    "do_not_show_top_products",
})

_COMMERCE_STATE_TO_OWNER = {
    "whatsapp_quick_order": OWNER_CHECKOUT,
    "browse_with_purchase_intent": OWNER_DISCOVERY,
    "browse": OWNER_DISCOVERY,
    "support": OWNER_SUPPORT,
    "post_purchase_tracking": OWNER_TRACKING,
    "price_objection": OWNER_CHECKOUT,
    "online_store_redirect": OWNER_ORDERING,
    "showroom_visit": OWNER_ORDERING,
    "purchase_channel_selection": OWNER_ORDERING,
}


@dataclass(frozen=True)
class ConversationTurnOwnership:
    """Single turn-owner projection for routing fallbacks."""

    turn_owner: str
    reason: str
    forbidden_fallbacks: FrozenSet[str] = field(default_factory=frozenset)
    explicit_browse_intent: bool = False
    confidence: float = 0.0

    def forbids(self, kind: str) -> bool:
        if self.explicit_browse_intent and kind in _DISCOVERY_FALLBACKS:
            return False
        return kind in self.forbidden_fallbacks

    def to_dict(self) -> dict[str, Any]:
        return {
            "turn_owner": self.turn_owner,
            "reason": self.reason,
            "forbidden_fallbacks": sorted(self.forbidden_fallbacks),
            "explicit_browse_intent": self.explicit_browse_intent,
            "confidence": round(float(self.confidence), 3),
        }


def has_explicit_catalog_browse_intent(
    ctx: BrainContext,
    *,
    message: Optional[str] = None,
    intent_name: Optional[str] = None,
) -> bool:
    """
    True only for explicit catalog browse signals — never via weak discovery entry.
    """
    msg = message if message is not None else (ctx.message or "")
    intent = intent_name if intent_name is not None else str(
        getattr(getattr(ctx, "intent", None), "name", "") or ""
    )
    try:
        from ..catalog.catalog_browse_turn_policy import is_catalog_browse_message  # noqa: PLC0415

        if is_catalog_browse_message(msg, intent_name=intent):
            return True
    except Exception:  # noqa: BLE001
        logger.exception("[TURN_OWNERSHIP] explicit_browse_message_probe_failed")

    try:
        from ..product_discovery_gate import _has_prior_browse_context  # noqa: PLC0415
        from ..discovery.entry import _is_show_more_request  # noqa: PLC0415

        if _is_show_more_request(msg) and _has_prior_browse_context(ctx):
            return True
    except Exception:  # noqa: BLE001
        logger.exception("[TURN_OWNERSHIP] show_more_browse_probe_failed")

    return False


def _forbidden_from_contract(ctx: BrainContext) -> Set[str]:
    out: Set[str] = set()
    contract = getattr(ctx, "commerce_turn_contract", None)
    if contract is None:
        return out
    forbidden = set(getattr(contract, "forbidden_actions", None) or [])
    if forbidden & _CONTRACT_BROWSE_TOKENS:
        out.update(_DISCOVERY_FALLBACKS)
    state = str(getattr(contract, "commerce_state", "") or "")
    if state and state not in {"browse", "browse_with_purchase_intent"}:
        out.update(_DISCOVERY_FALLBACKS)
    return out


def _owner_from_contract(ctx: BrainContext) -> Optional[tuple[str, str]]:
    contract = getattr(ctx, "commerce_turn_contract", None)
    if contract is None:
        return None
    state = str(getattr(contract, "commerce_state", "") or "")
    owner = _COMMERCE_STATE_TO_OWNER.get(state)
    if owner:
        goal = str(getattr(contract, "next_goal", "") or "")
        return owner, f"commerce_contract:{state}:{goal or 'active'}"
    known = dict(getattr(contract, "known_facts", None) or {})
    if known.get("catalog_order_current_turn") or known.get("active_catalog_checkout"):
        return OWNER_CHECKOUT, "commerce_contract:active_catalog_checkout"
    return None


def _owner_from_arbitration(ctx: BrainContext) -> Optional[tuple[str, str, float]]:
    arbitration: Optional[TurnArbitration] = getattr(ctx, "turn_arbitration_shadow", None)
    if arbitration is None:
        return None
    return (
        str(arbitration.turn_owner or ""),
        str(arbitration.reason or "turn_arbiter"),
        float(getattr(arbitration, "confidence", 0.0) or 0.0),
    )


def _owner_from_understanding(ctx: BrainContext) -> Optional[tuple[str, str]]:
    understanding = getattr(ctx, "turn_understanding_shadow", None)
    if understanding is None:
        return None
    intent = str(getattr(understanding, "current_intent", "") or "")
    mapping = {
        "checkout_continuation": OWNER_CHECKOUT,
        "payment_action": OWNER_PAYMENT,
        "track_order": OWNER_TRACKING,
        "reach_staff": OWNER_STAFF_ESCALATION,
        "health_advisory": OWNER_HEALTH_ADVISORY,
        "complaint_refund": OWNER_SUPPORT,
        "product_inquiry": OWNER_DISCOVERY,
        "start_order": OWNER_ORDERING,
    }
    owner = mapping.get(intent)
    if owner:
        return owner, f"turn_understanding:{intent}"
    return None


def resolve_conversation_turn_ownership(ctx: BrainContext) -> ConversationTurnOwnership:
    """Resolve turn owner and forbidden fallbacks for the current inbound turn."""
    explicit_browse = has_explicit_catalog_browse_intent(ctx)

    arb = _owner_from_arbitration(ctx)
    if arb is not None:
        turn_owner, reason, confidence = arb
    else:
        contract_owner = _owner_from_contract(ctx)
        if contract_owner is not None:
            turn_owner, reason = contract_owner
            confidence = 0.85
        else:
            understanding_owner = _owner_from_understanding(ctx)
            if understanding_owner is not None:
                turn_owner, reason = understanding_owner
                confidence = 0.8
            else:
                turn_owner = OWNER_PERSONA_SOCIAL
                reason = "default_no_owner_signal"
                confidence = 0.5

    forbidden: Set[str] = set(_OWNER_DEFAULT_FORBIDDEN.get(turn_owner, frozenset()))
    forbidden.update(_forbidden_from_contract(ctx))

    if explicit_browse and turn_owner not in {OWNER_DISCOVERY}:
        turn_owner = OWNER_DISCOVERY
        reason = f"explicit_browse_override:{reason}"
        forbidden -= set(_DISCOVERY_FALLBACKS)

    ownership = ConversationTurnOwnership(
        turn_owner=turn_owner,
        reason=reason,
        forbidden_fallbacks=frozenset(forbidden),
        explicit_browse_intent=explicit_browse,
        confidence=confidence,
    )
    logger.info(
        "[TURN_OWNERSHIP] tenant=%s owner=%s reason=%s explicit_browse=%s "
        "forbidden=%s preview=%r",
        getattr(ctx, "tenant_id", None),
        ownership.turn_owner,
        ownership.reason,
        ownership.explicit_browse_intent,
        sorted(ownership.forbidden_fallbacks),
        (ctx.message or "")[:80],
    )
    return ownership


def attach_conversation_turn_ownership(
    ctx: BrainContext,
    ownership: ConversationTurnOwnership,
) -> None:
    ctx.conversation_turn_ownership = ownership  # type: ignore[attr-defined]
    profile = getattr(ctx, "profile", None)
    if isinstance(profile, dict):
        profile["conversation_turn_ownership"] = ownership.to_dict()


def get_conversation_turn_ownership(ctx: BrainContext) -> Optional[ConversationTurnOwnership]:
    ownership = getattr(ctx, "conversation_turn_ownership", None)
    if isinstance(ownership, ConversationTurnOwnership):
        return ownership
    return None


def ownership_forbids_fallback(ctx: BrainContext, kind: str) -> Optional[str]:
    """
    Return suppression reason when the central ownership layer forbids a fallback.

    Consumers (discovery, gates) call this — they do not re-derive rules.
    """
    ownership = get_conversation_turn_ownership(ctx)
    if ownership is None:
        ownership = resolve_conversation_turn_ownership(ctx)
        attach_conversation_turn_ownership(ctx, ownership)
    if ownership.forbids(kind):
        return f"conversation_ownership:{ownership.turn_owner}:{kind}"
    return None


def ownership_forbids_action(ctx: BrainContext, action: str) -> Optional[str]:
    """Map decision actions to fallback kinds for ownership checks."""
    act = str(action or "").strip()
    if act in _BROWSE_ACTIONS:
        return ownership_forbids_fallback(ctx, FALLBACK_PRODUCT_DISCOVERY)
    return None


__all__ = [
    "ConversationTurnOwnership",
    "FALLBACK_CATALOG_BROWSE",
    "FALLBACK_PRODUCT_DISCOVERY",
    "FALLBACK_STALE_CHECKOUT_SUSPEND",
    "FALLBACK_TOP_PRODUCTS",
    "attach_conversation_turn_ownership",
    "get_conversation_turn_ownership",
    "has_explicit_catalog_browse_intent",
    "ownership_forbids_action",
    "ownership_forbids_fallback",
    "resolve_conversation_turn_ownership",
]

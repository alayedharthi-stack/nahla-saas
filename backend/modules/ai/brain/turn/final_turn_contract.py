"""
turn/final_turn_contract.py
────────────────────────────
Phase 3.1 — Final Turn Contract (shadow).

Merges post-decide ``Decision``, ``ActionResult``, and pre-decide commerce
artifacts into one contract that downstream compose/postprocess can be
audited against. Does not mutate decisions or replies.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Set

from ..decision.actions import (
    ACTION_CATALOG_NAVIGATE,
    ACTION_LLM_REPLY,
    ACTION_NARROW,
    ACTION_SEARCH_PRODUCTS,
)
from ..types import ActionResult, BrainContext, Decision

logger = logging.getLogger("nahla.brain.final_turn_contract")

_CATALOG_BRAIN_ACTIONS = frozenset({
    ACTION_SEARCH_PRODUCTS,
    ACTION_CATALOG_NAVIGATE,
    ACTION_NARROW,
})

_SHIPPING_PURPOSES = frozenset({
    "shipping_post_order",
    "shipping",
    "track_order",
    "order_tracking",
})

_PRODUCT_FORBIDDEN_TOKENS = frozenset({
    "do_not_ask_product",
    "do_not_ask_product_yet",
    "do_not_browse",
    "do_not_search_products",
    "do_not_push_product_list",
})

_VARIANT_FORBIDDEN_TOKENS = frozenset({
    "do_not_ask_quantity",
    "do_not_append_quantity_prompt",
})

_NAME_FIELD_TOKENS = frozenset({
    "customer_first_name",
    "customer_last_name",
    "name",
    "full_name",
    "customer_name",
})


@dataclass
class FinalTurnContract:
    """Post-decide response/action ownership contract — facts only, no reply text."""

    response_purpose: str
    turn_owner: str
    decision_action: str
    decision_topic: str
    allowed_actions: List[str] = field(default_factory=list)
    forbidden_actions: List[str] = field(default_factory=list)
    required_action: Optional[str] = None
    pending_action: Optional[str] = None
    next_required_field: Optional[str] = None
    allowed_question_types: List[str] = field(default_factory=list)
    forbidden_question_types: List[str] = field(default_factory=list)
    factual_claims_allowed: List[str] = field(default_factory=list)
    promises_allowed: List[str] = field(default_factory=list)
    promises_forbidden: List[str] = field(default_factory=list)
    trusted_product_label: Optional[str] = None
    known_facts: Dict[str, Any] = field(default_factory=dict)
    inbound_text: str = ""
    browse_allowed: bool = False

    def to_dict(self) -> Dict[str, Any]:
        return {
            "response_purpose": self.response_purpose,
            "turn_owner": self.turn_owner,
            "decision_action": self.decision_action,
            "decision_topic": self.decision_topic,
            "allowed_actions": list(self.allowed_actions),
            "forbidden_actions": list(self.forbidden_actions),
            "required_action": self.required_action,
            "pending_action": self.pending_action,
            "next_required_field": self.next_required_field,
            "allowed_question_types": list(self.allowed_question_types),
            "forbidden_question_types": list(self.forbidden_question_types),
            "factual_claims_allowed": list(self.factual_claims_allowed),
            "promises_allowed": list(self.promises_allowed),
            "promises_forbidden": list(self.promises_forbidden),
            "trusted_product_label": self.trusted_product_label,
            "known_facts": dict(self.known_facts),
            "inbound_text": self.inbound_text,
            "browse_allowed": self.browse_allowed,
        }


def attach_final_turn_contract(ctx: BrainContext, contract: FinalTurnContract) -> None:
    ctx.final_turn_contract = contract  # type: ignore[attr-defined]
    profile = getattr(ctx, "profile", None)
    if isinstance(profile, dict):
        profile["final_turn_contract"] = contract.to_dict()


def get_final_turn_contract(ctx: BrainContext) -> Optional[FinalTurnContract]:
    contract = getattr(ctx, "final_turn_contract", None)
    if isinstance(contract, FinalTurnContract):
        return contract
    return None


def _merge_known_facts(ctx: BrainContext, commerce: Any) -> Dict[str, Any]:
    facts: Dict[str, Any] = {}
    if commerce is not None:
        facts.update(dict(getattr(commerce, "known_facts", None) or {}))
    hint = getattr(ctx, "merchant_operational_policy_hint", None)
    if hint is not None:
        try:
            from ..policy.contracts import hint_to_log_dict  # noqa: PLC0415

            facts["merchant_operational_policy_shadow"] = hint_to_log_dict(hint)
        except Exception:  # noqa: BLE001
            logger.exception(
                "[FINAL_TURN_CONTRACT] merchant_operational_policy_hint_failed",
            )
    phone = str(getattr(ctx, "customer_phone", "") or "").strip()
    if phone:
        facts.setdefault("phone_known", True)
        facts.setdefault("customer_phone", phone)
    profile = getattr(ctx, "profile", None)
    if isinstance(profile, dict):
        for key in ("customer_name", "name", "customer_first_name"):
            val = str(profile.get(key) or "").strip()
            if val:
                facts.setdefault("customer_name", val)
                facts["customer_name_known"] = True
                break
    return facts


def _trusted_product_label(ctx: BrainContext, result: ActionResult) -> Optional[str]:
    data = dict(getattr(result, "data", None) or {})
    for src in (
        data.get("focus_product"),
        data.get("selected_product"),
    ):
        if isinstance(src, dict):
            title = str(src.get("title") or src.get("name") or "").strip()
            if title:
                return title
    state = getattr(ctx, "state", None)
    if state is not None:
        focus = getattr(state, "current_product_focus", None) or {}
        if isinstance(focus, dict):
            title = str(focus.get("title") or focus.get("name") or "").strip()
            if title:
                return title
    products = data.get("products") or data.get("pending_candidates") or []
    if isinstance(products, list) and len(products) == 1:
        row = products[0] or {}
        if isinstance(row, dict):
            title = str(row.get("title") or row.get("name") or "").strip()
            if title:
                return title
    return None


def _resolve_turn_owner(ctx: BrainContext) -> str:
    ownership = getattr(ctx, "conversation_turn_ownership", None)
    if ownership is not None:
        return str(getattr(ownership, "turn_owner", "") or "")
    arbitration = getattr(ctx, "turn_arbitration_shadow", None)
    if arbitration is not None:
        return str(getattr(arbitration, "turn_owner", "") or "")
    return ""


def _browse_allowed(
    ctx: BrainContext,
    *,
    decision: Decision,
    forbidden_actions: Set[str],
    response_purpose: str,
) -> bool:
    if response_purpose in _SHIPPING_PURPOSES or response_purpose == "identity_collaboration":
        return False
    ownership = getattr(ctx, "conversation_turn_ownership", None)
    if ownership is not None and bool(getattr(ownership, "explicit_browse_intent", False)):
        return True
    action = str(getattr(decision, "action", "") or "")
    if action in _CATALOG_BRAIN_ACTIONS:
        return True
    browse_blocked = bool(
        forbidden_actions
        & {"do_not_browse", "do_not_search_products", "do_not_push_product_list"}
    )
    return not browse_blocked and action == ACTION_LLM_REPLY


def _derive_forbidden_question_types(
    *,
    known_facts: Dict[str, Any],
    forbidden_actions: Set[str],
    response_purpose: str,
    inbound_text: str,
) -> Set[str]:
    forbidden: Set[str] = set()

    name_known = bool(
        known_facts.get("customer_name_known")
        or known_facts.get("customer_name")
        or (
            known_facts.get("customer_first_name")
            and known_facts.get("customer_last_name")
        )
    )
    if name_known:
        forbidden.add("name")

    if known_facts.get("phone_known") or known_facts.get("customer_phone"):
        forbidden.add("phone")

    if forbidden_actions & _PRODUCT_FORBIDDEN_TOKENS:
        forbidden.update({"product", "browse", "catalog_promise"})

    if forbidden_actions & _VARIANT_FORBIDDEN_TOKENS:
        forbidden.update({"variant", "quantity"})

    if response_purpose in _SHIPPING_PURPOSES:
        forbidden.update({
            "product",
            "variant",
            "catalog_promise",
            "browse",
            "availability",
        })

    if response_purpose == "identity_collaboration":
        forbidden.update({"product", "variant", "catalog_promise", "browse"})

    try:
        from modules.ai.brain.commerce.product_label_hygiene import (  # noqa: PLC0415
            is_negative_logistics_or_contact_context,
        )

        if is_negative_logistics_or_contact_context(inbound_text):
            forbidden.update({
                "product",
                "variant",
                "catalog_promise",
                "availability",
            })
    except Exception:  # noqa: BLE001  # noqa: silent-ok — optional product label hygiene import
        pass

    return forbidden


def _derive_promises_forbidden(
    *,
    forbidden_question_types: Set[str],
    browse_allowed: bool,
    decision_action: str,
) -> Set[str]:
    out: Set[str] = set()
    if "catalog_promise" in forbidden_question_types:
        out.add("catalog_promise")
    if "availability" in forbidden_question_types:
        out.add("product_availability")
    if not browse_allowed and decision_action == ACTION_LLM_REPLY:
        out.add("catalog_promise")
    return out


def build_final_turn_contract(
    ctx: BrainContext,
    decision: Decision,
    result: ActionResult,
) -> FinalTurnContract:
    """Build final contract after execute, before compose."""
    commerce = getattr(ctx, "commerce_turn_contract", None)
    args = dict(getattr(decision, "args", None) or {})
    inbound = str(getattr(ctx, "raw_message", None) or ctx.message or "")

    decision_action = str(getattr(decision, "action", "") or "")
    decision_topic = str(args.get("topic") or "")
    response_purpose = (
        decision_topic
        or str(args.get("response_goal") or "")[:80]
        or str(getattr(commerce, "next_goal", "") or "")
    )

    allowed: List[str] = list(getattr(commerce, "allowed_actions", None) or [])
    forbidden_list: List[str] = list(getattr(commerce, "forbidden_actions", None) or [])
    forbidden_set = set(forbidden_list)

    known_facts = _merge_known_facts(ctx, commerce)
    missing = list(getattr(commerce, "missing_fields", None) or [])
    next_required = missing[0] if missing else None

    browse_allowed = _browse_allowed(
        ctx,
        decision=decision,
        forbidden_actions=forbidden_set,
        response_purpose=response_purpose,
    )
    if browse_allowed and "browse" not in allowed:
        allowed = list(allowed) + ["browse"]

    forbidden_q = _derive_forbidden_question_types(
        known_facts=known_facts,
        forbidden_actions=forbidden_set,
        response_purpose=response_purpose,
        inbound_text=inbound,
    )
    promises_forbidden = _derive_promises_forbidden(
        forbidden_question_types=forbidden_q,
        browse_allowed=browse_allowed,
        decision_action=decision_action,
    )

    allowed_q: Set[str] = set()
    if browse_allowed:
        allowed_q.add("browse")
    if next_required and next_required not in _NAME_FIELD_TOKENS:
        allowed_q.add(next_required)
    elif next_required in _NAME_FIELD_TOKENS and "name" not in forbidden_q:
        allowed_q.add("name")

    required_action = getattr(commerce, "action_to_execute", None)
    pending_action = required_action if required_action and required_action != decision_action else None

    contract = FinalTurnContract(
        response_purpose=response_purpose,
        turn_owner=_resolve_turn_owner(ctx),
        decision_action=decision_action,
        decision_topic=decision_topic,
        allowed_actions=allowed,
        forbidden_actions=forbidden_list,
        required_action=str(required_action) if required_action else None,
        pending_action=str(pending_action) if pending_action else None,
        next_required_field=next_required,
        allowed_question_types=sorted(allowed_q),
        forbidden_question_types=sorted(forbidden_q),
        promises_forbidden=sorted(promises_forbidden),
        trusted_product_label=_trusted_product_label(ctx, result),
        known_facts=known_facts,
        inbound_text=inbound,
        browse_allowed=browse_allowed,
    )

    logger.info(
        "[FINAL_TURN_CONTRACT] tenant=%s purpose=%s action=%s topic=%s "
        "owner=%s browse_allowed=%s forbidden_q=%s trusted_label=%r preview=%r",
        getattr(ctx, "tenant_id", None),
        contract.response_purpose,
        contract.decision_action,
        contract.decision_topic,
        contract.turn_owner,
        contract.browse_allowed,
        contract.forbidden_question_types,
        contract.trusted_product_label,
        inbound[:80],
    )
    return contract


__all__ = [
    "FinalTurnContract",
    "attach_final_turn_contract",
    "build_final_turn_contract",
    "get_final_turn_contract",
]

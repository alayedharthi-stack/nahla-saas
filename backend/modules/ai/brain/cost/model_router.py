"""
model_router.py
───────────────
PR1 — limited compose enforcement for commerce cheap-first routing.

Only ``brain.compose._llm_compose`` is enforced when
``NAHLA_MODEL_ROUTER_ENABLED=true``. Global provider_chain defaults are
unchanged; per-request overrides travel via ``prompt_overrides.__model_router``.
"""
from __future__ import annotations

import os
from dataclasses import dataclass
from typing import Any, Dict, Optional, Sequence, Tuple

from modules.ai.brain.cost.model_router_audit import (
    TIER_CHEAP,
    TIER_NONE,
    TIER_PREMIUM,
    TIER_STANDARD,
    _CHEAP_INTENTS,
    _STANDARD_INTENTS,
    _env_tier_default,
    is_premium_model_allowed,
)
from modules.ai.brain.decision.actions import ACTION_HANDOFF
from modules.ai.brain.intent_priority.types import (
    GOAL_PRICE_INQUIRY,
    GOAL_PRODUCT_AVAILABILITY,
    GOAL_STAFF_CONTACT,
)
from modules.ai.brain.types import (
    INTENT_EMPLOYEE_NOT_RESPONDING,
    INTENT_TALK_HUMAN,
    INTENT_TRACK_ORDER,
)
from modules.ai.orchestrator.customer_chat_models import (
    customer_chat_provider,
    openai_only_provider_chain,
    resolve_default_customer_chat_model,
    resolve_premium_customer_chat_model,
    resolve_standard_customer_chat_model,
)

_ROUTER_FLAG = "NAHLA_MODEL_ROUTER_ENABLED"

_COMPOSE_CHEAP_INTENTS = _CHEAP_INTENTS | frozenset({
    "product_availability",
    "product_reference",
    "pick_list_item",
    "evaluate_price",
    "start_order",
})

_HARD_STANDARD_REASONS = frozenset({
    "standard_intent",
    "handoff_or_escalation_intent",
    "order_payment_dispute",
    "handoff_action",
    "human_priority",
    "complaint_or_dispute",
    "staff_contact_goal",
})

_COMPOSE_CHEAP_GOALS = frozenset({
    GOAL_PRODUCT_AVAILABILITY,
    "product_reference",
})

_STANDARD_AMBIGUITY_CLASSES = frozenset({
    "missing_intent",
    "missing_objective",
})

_DISPUTE_RESPONSE_GOAL_MARKERS = (
    "complaint_recovery",
    "payment_dispute",
    "order_dispute",
)

_ROUTINE_DAILY_COMMERCE_INTENTS = _COMPOSE_CHEAP_INTENTS | frozenset({
    "browse",
    "discover_products",
    "need_based_product_advice",
    "pick_list_item",
    "evaluate_price",
    "start_order",
})

_ROUTINE_DAILY_COMMERCE_GOALS = _COMPOSE_CHEAP_GOALS | frozenset({
    GOAL_PRODUCT_AVAILABILITY,
    GOAL_PRICE_INQUIRY,
    "product_reference",
})

# Policy reasons that must not upgrade routine daily commerce to STANDARD.
_SOFT_POLICY_REASONS = frozenset({
    "service_availability_not_handoff",
    "non_commerce_clamp",
})

_AVAILABILITY_FOCUS_MARKERS = (
    "product_availability",
    "product_price_clarify",
    "availability",
)


@dataclass(frozen=True)
class ComposeModelRoute:
    enforced: bool
    tier: str
    provider: str
    model: str
    reason: str
    provider_hint: str
    provider_chain_override: Optional[Tuple[str, ...]] = None
    block_anthropic_fallback: bool = False

    def to_audit_dict(self) -> Dict[str, Any]:
        return {
            "model_tier": self.tier,
            "router_reason": self.reason,
            "behavior_change": self.enforced,
            "enforce_provider": self.provider,
            "enforce_model": self.model,
        }

    def to_prompt_override(self) -> Dict[str, Any]:
        payload: Dict[str, Any] = {
            "tier": self.tier,
            "provider": self.provider,
            "model": self.model,
            "reason": self.reason,
        }
        if self.provider_chain_override:
            payload["provider_chain_override"] = list(self.provider_chain_override)
        if self.block_anthropic_fallback:
            payload["block_anthropic_fallback"] = True
        return payload


def is_model_router_enabled() -> bool:
    return os.getenv(_ROUTER_FLAG, "false").strip().lower() in {"1", "true", "yes", "on"}


def _cheap_chain_override() -> Tuple[str, ...]:
    """Cheap tier — OpenAI-only, no Anthropic/Gemini."""
    return _cheap_chain_no_anthropic()


def _cheap_chain_no_anthropic() -> Tuple[str, ...]:
    """Routine daily commerce — OpenAI-compatible only."""
    return openai_only_provider_chain()


def _standard_chain_override() -> Tuple[str, ...]:
    return openai_only_provider_chain()


def _disabled_route() -> ComposeModelRoute:
    return ComposeModelRoute(
        enforced=False,
        tier=TIER_CHEAP,
        provider=customer_chat_provider(),
        model=resolve_default_customer_chat_model(),
        reason="router_disabled_openai_default",
        provider_hint=customer_chat_provider(),
        provider_chain_override=openai_only_provider_chain(),
        block_anthropic_fallback=True,
    )


def _compose_is_cheap_intent(
    intent_name: str,
    *,
    primary_customer_goal: str = "",
) -> bool:
    intent = str(intent_name or "").strip().lower()
    goal = str(primary_customer_goal or "").strip().lower()
    return intent in _COMPOSE_CHEAP_INTENTS or goal in _COMPOSE_CHEAP_GOALS


def is_routine_daily_commerce_compose(
    *,
    intent_name: str = "",
    primary_customer_goal: str = "",
) -> bool:
    """Routine catalog/availability/price turns that must stay on CHEAP."""
    intent = str(intent_name or "").strip().lower()
    goal = str(primary_customer_goal or "").strip().lower()
    return (
        intent in _ROUTINE_DAILY_COMMERCE_INTENTS
        or goal in _ROUTINE_DAILY_COMMERCE_GOALS
    )


def _is_availability_priority_focus(intent_focus: str) -> bool:
    focus = str(intent_focus or "").strip().lower()
    if not focus:
        return False
    return any(marker in focus for marker in _AVAILABILITY_FOCUS_MARKERS)


def detect_compose_standard_signals(
    *,
    intent_name: str = "",
    decision_action: Optional[str] = None,
    human_priority: bool = False,
    reply_state: Any = None,
    result_data: Optional[Dict[str, Any]] = None,
) -> Tuple[bool, str]:
    """Return (needs_standard, reason) for compose-only standard-tier routing."""
    intent = str(intent_name or "").strip().lower()
    action = str(decision_action or "").strip()
    data = dict(result_data or {})

    rs = reply_state
    primary_goal = str(getattr(rs, "primary_customer_goal", "") or "").strip().lower()
    routine_commerce = is_routine_daily_commerce_compose(
        intent_name=intent,
        primary_customer_goal=primary_goal,
    )

    if intent in _STANDARD_INTENTS:
        return True, "standard_intent"
    if intent in {INTENT_TALK_HUMAN, INTENT_EMPLOYEE_NOT_RESPONDING}:
        return True, "handoff_or_escalation_intent"
    if intent == INTENT_TRACK_ORDER and (
        data.get("dispute") or data.get("payment_dispute")
    ):
        return True, "order_payment_dispute"
    if action == ACTION_HANDOFF:
        return True, "handoff_action"
    if human_priority:
        return True, "human_priority"

    policy_from_result = str(data.get("policy_reason") or "").strip()
    if (
        str(data.get("type") or "") == "llm_fallback"
        and policy_from_result
        and not (routine_commerce and policy_from_result in _SOFT_POLICY_REASONS)
    ):
        return True, "tool_failure_recovery"

    if rs is not None:
        policy_reason = str(getattr(rs, "policy_reason", "") or "").strip()
        if policy_reason and not (
            routine_commerce and policy_reason in _SOFT_POLICY_REASONS
        ):
            return True, "policy_sensitive"

        response_goal = str(getattr(rs, "response_goal", "") or "").lower()
        if any(marker in response_goal for marker in _DISPUTE_RESPONSE_GOAL_MARKERS):
            return True, "complaint_or_dispute"

        if primary_goal == GOAL_STAFF_CONTACT:
            return True, "staff_contact_goal"

        ambiguity = str(getattr(rs, "ambiguity_class", "") or "").strip().lower()
        if ambiguity in _STANDARD_AMBIGUITY_CLASSES and not routine_commerce:
            return True, "high_ambiguity"

        intent_focus = str(getattr(rs, "intent_priority_focus", "") or "").lower()
        if (
            "conflict" in intent_focus
            and not _is_availability_priority_focus(intent_focus)
            and not routine_commerce
        ):
            return True, "multi_turn_conflict"

    return False, ""


def resolve_compose_model_route(
    *,
    intent_name: str = "",
    social_category: str = "",
    decision_action: Optional[str] = None,
    human_priority: bool = False,
    reply_state: Any = None,
    result_data: Optional[Dict[str, Any]] = None,
) -> ComposeModelRoute:
    """
    Resolve provider/model for ``brain.compose._llm_compose`` only.

    When the router flag is off, returns a non-enforced anthropic route so
    legacy behavior is unchanged.
    """
    if not is_model_router_enabled():
        return _disabled_route()

    if is_premium_model_allowed() and str(intent_name or "").strip().lower() == "premium_explicit":
        premium = _env_tier_default(TIER_PREMIUM)
        return ComposeModelRoute(
            enforced=True,
            tier=TIER_PREMIUM,
            provider=str(premium.suggested_provider or customer_chat_provider()),
            model=str(premium.suggested_model or resolve_premium_customer_chat_model()),
            reason=premium.reason,
            provider_hint=customer_chat_provider(),
            provider_chain_override=_standard_chain_override(),
        )

    needs_standard, std_reason = detect_compose_standard_signals(
        intent_name=intent_name,
        decision_action=decision_action,
        human_priority=human_priority,
        reply_state=reply_state,
        result_data=result_data,
    )
    primary_goal = str(getattr(reply_state, "primary_customer_goal", "") or "")
    routine = is_routine_daily_commerce_compose(
        intent_name=intent_name,
        primary_customer_goal=primary_goal,
    )
    if (
        needs_standard
        and routine
        and std_reason not in _HARD_STANDARD_REASONS
    ):
        needs_standard = False
        std_reason = ""

    if needs_standard:
        standard = _env_tier_default(TIER_STANDARD)
        return ComposeModelRoute(
            enforced=True,
            tier=TIER_STANDARD,
            provider=str(standard.suggested_provider or customer_chat_provider()),
            model=str(standard.suggested_model or resolve_standard_customer_chat_model()),
            reason=std_reason or standard.reason,
            provider_hint=customer_chat_provider(),
            provider_chain_override=_standard_chain_override(),
        )

    if _compose_is_cheap_intent(intent_name, primary_customer_goal=primary_goal):
        cheap = _env_tier_default(TIER_CHEAP)
        return ComposeModelRoute(
            enforced=True,
            tier=TIER_CHEAP,
            provider=str(cheap.suggested_provider or customer_chat_provider()),
            model=str(cheap.suggested_model or resolve_default_customer_chat_model()),
            reason="commerce_cheap_first",
            provider_hint=customer_chat_provider(),
            provider_chain_override=_cheap_chain_no_anthropic(),
            block_anthropic_fallback=True,
        )

    # Router enabled but no cheap/standard match — stay on cheap default (Luna).
    # Explicit escalation intents still take STANDARD above. Never premium here.
    cheap = _env_tier_default(TIER_CHEAP)
    return ComposeModelRoute(
        enforced=True,
        tier=TIER_CHEAP,
        provider=str(cheap.suggested_provider or customer_chat_provider()),
        model=str(cheap.suggested_model or resolve_default_customer_chat_model()),
        reason="default_cheap_when_enabled",
        provider_hint=customer_chat_provider(),
        provider_chain_override=_cheap_chain_no_anthropic(),
        block_anthropic_fallback=True,
    )


def compose_route_skips_llm(
    *,
    intent_name: str = "",
    social_category: str = "",
) -> bool:
    """True when compose should not reach LLM at all (persona/no-LLM paths)."""
    from modules.ai.brain.cost.model_router_audit import suggest_model_tier  # noqa: PLC0415

    suggestion = suggest_model_tier(
        call_site="brain.compose._llm_compose",
        intent_name=intent_name,
        social_category=social_category,
    )
    return suggestion.tier == TIER_NONE


def should_block_anthropic_compose_result(
    *,
    route: ComposeModelRoute,
    provider_used: str,
) -> bool:
    """Block Anthropic replies for cheap / routine-commerce compose routes."""
    if not route.enforced:
        return False
    if str(provider_used or "").strip().lower() != "anthropic":
        return False
    if route.tier == TIER_CHEAP or route.block_anthropic_fallback:
        return True
    return False

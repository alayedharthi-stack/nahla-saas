"""
model_router_audit.py
─────────────────────
PR0 — read-only model router scaffold.

Emits ``[MODEL_ROUTER_AUDIT]`` with a *suggested* tier only.
Does NOT change provider_chain, model selection, commerce, or persona.
"""
from __future__ import annotations

import json
import logging
import os
from dataclasses import dataclass
from decimal import Decimal
from typing import Any, Dict, Optional

_log = logging.getLogger("nahla.ai.brain.cost.model_router")

TIER_NONE = "none"
TIER_TINY = "tiny"
TIER_CHEAP = "cheap"
TIER_STANDARD = "standard"
TIER_PREMIUM = "premium"

_AUDIT_FLAG = "NAHLA_MODEL_ROUTER_AUDIT_ENABLED"
_PREMIUM_FLAG = "ALLOW_PREMIUM_MODEL"

from modules.ai.orchestrator.customer_chat_models import (
    MODEL_LUNA,
    customer_chat_provider,
)

# Reference OpenAI gpt-5.6-luna USD/1M (provisional pricing v2 audit baseline).
_LUNA_REFERENCE_INPUT = Decimal("0.015")
_LUNA_REFERENCE_OUTPUT = Decimal("0.060")

_TINY_CALL_SITES = frozenset({
    "brain.intent.slot_extractor",
    "brain.memory.updater._summarise",
})

_NONE_INTENTS = frozenset({
    "greeting",
    "social",
    "thanks",
    "farewell",
    "gratitude",
})

_NONE_SOCIAL_CATEGORIES = frozenset({
    "thanks",
    "blessing",
    "strong_praise",
    "general_courtesy",
    "morning_greeting",
    "celebration",
    "informational_only",
    "social_forward",
    "basmala",
    "prophet_invocation",
    "compliment",
    "emotional_personal",
    "eid_greeting",
    "dua",
    "condolence",
    "religious_media",
})

_CHEAP_INTENTS = frozenset({
    "ask_product",
    "browse",
    "discover_products",
    "solution_seeking_commerce",
    "need_based_product_advice",
    "ask_price",
    "evaluate_price",
    "start_order",
})

_STANDARD_INTENTS = frozenset({
    "talk_to_human",
    "complaint",
    "escalation",
    "support",
})


@dataclass(frozen=True)
class ModelTierSuggestion:
    tier: str
    reason: str
    suggested_provider: Optional[str] = None
    suggested_model: Optional[str] = None

    def to_dict(self) -> Dict[str, Any]:
        return {
            "tier": self.tier,
            "reason": self.reason,
            "suggested_provider": self.suggested_provider,
            "suggested_model": self.suggested_model,
        }


def is_model_router_audit_enabled() -> bool:
    return os.getenv(_AUDIT_FLAG, "false").strip().lower() in {"1", "true", "yes", "on"}


def is_premium_model_allowed() -> bool:
    return os.getenv(_PREMIUM_FLAG, "false").strip().lower() in {"1", "true", "yes", "on"}


def audit_luna_pricing_v2() -> Dict[str, Any]:
    """Verify ledger pricing v2 entry for openai_compatible/gpt-5.6-luna."""
    from modules.ai.orchestrator.ai_usage_pricing import (  # noqa: PLC0415
        PRICING_VERSION,
        lookup_model_pricing_v2,
    )

    pricing = lookup_model_pricing_v2("openai_compatible", MODEL_LUNA)
    input_ok = pricing.input_per_1m == _LUNA_REFERENCE_INPUT
    output_ok = pricing.output_per_1m == _LUNA_REFERENCE_OUTPUT
    return {
        "provider": customer_chat_provider(),
        "model": MODEL_LUNA,
        "pricing_version": PRICING_VERSION,
        "input_per_1m_usd": str(pricing.input_per_1m),
        "output_per_1m_usd": str(pricing.output_per_1m),
        "reference_input_per_1m_usd": str(_LUNA_REFERENCE_INPUT),
        "reference_output_per_1m_usd": str(_LUNA_REFERENCE_OUTPUT),
        "input_matches_reference": input_ok,
        "output_matches_reference": output_ok,
        "pricing_ok": input_ok and output_ok,
    }


def audit_gpt4o_mini_pricing_v2() -> Dict[str, Any]:
    """Backward-compatible alias — Luna is the production cheap/default model."""
    return audit_luna_pricing_v2()


def _env_tier_default(tier: str) -> ModelTierSuggestion:
    """Map tier label to env-backed provider/model *suggestions* (not enforced)."""
    if tier == TIER_TINY:
        return ModelTierSuggestion(
            tier=tier,
            reason="policy_tiny_call_site",
            suggested_provider=os.getenv("NAHLA_MODEL_TINY_PROVIDER", customer_chat_provider()),
            suggested_model=os.getenv("NAHLA_MODEL_TINY", MODEL_LUNA),
        )
    if tier == TIER_CHEAP:
        return ModelTierSuggestion(
            tier=tier,
            reason="policy_cheap_intent",
            suggested_provider=os.getenv("NAHLA_MODEL_CHEAP_PROVIDER", customer_chat_provider()),
            suggested_model=os.getenv("NAHLA_MODEL_CHEAP", MODEL_LUNA),
        )
    if tier == TIER_STANDARD:
        return ModelTierSuggestion(
            tier=tier,
            reason="policy_standard_intent",
            suggested_provider=os.getenv("NAHLA_MODEL_STANDARD_PROVIDER", customer_chat_provider()),
            suggested_model=os.getenv("NAHLA_MODEL_STANDARD", "gpt-5.6-terra"),
        )
    if tier == TIER_PREMIUM:
        return ModelTierSuggestion(
            tier=tier,
            reason="policy_premium_explicit_only",
            suggested_provider=customer_chat_provider(),
            suggested_model=os.getenv("NAHLA_MODEL_PREMIUM", "gpt-5.6-sol"),
        )
    return ModelTierSuggestion(tier=TIER_NONE, reason="policy_no_llm")


def suggest_model_tier(
    *,
    call_site: str,
    intent_name: Optional[str] = None,
    social_category: Optional[str] = None,
) -> ModelTierSuggestion:
    """
    Suggest a model tier for audit/logging only — no runtime enforcement.
    """
    site = (call_site or "").strip()
    intent = str(intent_name or "").strip().lower()
    social = str(social_category or "").strip().lower()

    if site in _TINY_CALL_SITES:
        return _env_tier_default(TIER_TINY)

    if intent in _NONE_INTENTS or social in _NONE_SOCIAL_CATEGORIES:
        return ModelTierSuggestion(tier=TIER_NONE, reason="policy_routine_social_or_greeting")

    if intent in _STANDARD_INTENTS:
        return _env_tier_default(TIER_STANDARD)

    if intent in _CHEAP_INTENTS:
        return _env_tier_default(TIER_CHEAP)

    if is_premium_model_allowed() and intent == "premium_explicit":
        return _env_tier_default(TIER_PREMIUM)

    # Unknown / default customer compose — cheap (Luna). Never premium by default.
    return _env_tier_default(TIER_CHEAP)


def emit_model_router_audit(**fields: Any) -> None:
    """Emit one ``[MODEL_ROUTER_AUDIT]`` line; never raises."""
    if not is_model_router_audit_enabled():
        return
    try:
        payload = {k: v for k, v in fields.items() if v is not None}
        payload.setdefault("mode", "audit_only")
        payload.setdefault("behavior_change", False)
        if payload.get("tier") in {TIER_TINY, TIER_CHEAP}:
            payload["luna_pricing_check"] = audit_luna_pricing_v2()
        _log.info("[MODEL_ROUTER_AUDIT] %s", json.dumps(payload, ensure_ascii=False))
    except Exception as exc:  # noqa: BLE001 — audit must never break replies
        _log.warning(
            "[MODEL_ROUTER_AUDIT_ERROR] err=%s",
            type(exc).__name__,
        )


def maybe_audit_model_router(
    *,
    call_site: str,
    intent_name: Optional[str] = None,
    social_category: Optional[str] = None,
    tenant_id: Optional[int] = None,
    conversation_id: Optional[int] = None,
    turn_id: Optional[int] = None,
    extra: Optional[Dict[str, Any]] = None,
) -> ModelTierSuggestion:
    """
    Compute tier suggestion and optionally emit audit log.
    Always safe to call — returns suggestion without side effects when disabled.
    """
    suggestion = suggest_model_tier(
        call_site=call_site,
        intent_name=intent_name,
        social_category=social_category,
    )
    emit_model_router_audit(
        call_site=call_site,
        intent=intent_name,
        social_category=social_category,
        tenant_id=tenant_id,
        conversation_id=conversation_id,
        turn_id=turn_id,
        **suggestion.to_dict(),
        **(extra or {}),
    )
    return suggestion

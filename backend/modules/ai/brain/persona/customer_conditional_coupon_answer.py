"""Fact-bound persona compose for conditional-coupon min-orders answers."""
from __future__ import annotations

from typing import Any, Optional

from .facts_bundle import (
    PERSONA_SURFACE_CUSTOMER_CONDITIONAL_COUPON_ANSWER,
    PersonaComposeResult,
    PersonaConstraints,
    PersonaFactsBundle,
)
from .integration import build_persona_compose_event_metadata
from modules.ai.brain.truth_surface.flags import (
    is_customer_conditional_coupon_compose_enabled,
)


def build_customer_conditional_coupon_answer_facts_bundle(
    *,
    inbound_text: str,
    tenant_id: int = 0,
    customer_phone: str = "",
    customer_conditional_coupon_facts: Optional[dict[str, Any]] = None,
    merchant_persona: Optional[dict[str, Any]] = None,
) -> PersonaFactsBundle:
    from .fact_bound_composer import detect_language  # noqa: PLC0415

    inbound = str(inbound_text or "").strip()
    language = detect_language(inbound)
    facts = dict(customer_conditional_coupon_facts or {})
    forbidden_claims: list[str] = [
        "coupon_code_disclosure",
        "coupon_issued_claim",
        "coupon_applied_claim",
        "checkout_pressure",
    ]
    if not facts.get("allow_min_orders_condition_claim"):
        forbidden_claims.append("final_min_orders_eligibility_claim")

    verified_facts: dict[str, Any] = {
        "surface": PERSONA_SURFACE_CUSTOMER_CONDITIONAL_COUPON_ANSWER,
        "inbound_text": inbound,
        "identity_status": str(facts.get("identity_status") or ""),
        "min_orders_condition_state": str(facts.get("min_orders_condition_state") or ""),
        "conditional_coupon_evaluation_state": str(
            facts.get("conditional_coupon_evaluation_state") or ""
        ),
        "order_history_completeness": str(facts.get("order_history_completeness") or ""),
        "completed_orders_count": facts.get("completed_orders_count"),
        "min_orders_for_eligibility": facts.get("min_orders_for_eligibility"),
        "orders_shortfall": facts.get("orders_shortfall"),
        "allow_min_orders_condition_claim": bool(
            facts.get("allow_min_orders_condition_claim")
        ),
        "closed_reason_code": facts.get("closed_reason_code"),
        "facts_snapshot_id": str(facts.get("facts_snapshot_id") or ""),
        "allow_checkout_pressure": False,
        "allow_slot_prompts": False,
    }
    return PersonaFactsBundle(
        surface=PERSONA_SURFACE_CUSTOMER_CONDITIONAL_COUPON_ANSWER,
        inbound_text=inbound,
        language=language,
        dialect="saudi_arabic" if language == "ar" else None,
        verified_facts=verified_facts,
        customer_context={},
        merchant_persona=dict(merchant_persona or {}),
        constraints=PersonaConstraints(
            max_chars=380,
            max_emojis=1,
            forbidden_claims=tuple(forbidden_claims),
        ),
        tenant_id=int(tenant_id or 0),
        customer_phone=str(customer_phone or ""),
    )


def build_customer_conditional_coupon_answer_event_metadata(
    result: PersonaComposeResult,
    *,
    tenant_id: int,
    compose_facts: dict[str, Any],
) -> dict[str, Any]:
    meta = build_persona_compose_event_metadata(
        result,
        tenant_id=int(tenant_id),
        allowlist_result="trusted_context_customer_conditional_coupon_compose",
    )
    meta["chosen_path"] = "customer_conditional_coupon_compose"
    meta["compose_source"] = str(result.source or "")
    meta["response_mode"] = "customer_conditional_coupon_answer"
    meta["llm_candidate_present"] = result.source == "persona_llm"
    meta["final_text_transformed"] = False
    meta["final_transform_reasons"] = []
    meta["customer_conditional_coupon_compose_active"] = True
    meta["facts_snapshot_id"] = str(compose_facts.get("facts_snapshot_id") or "")
    if result.fallback_reason:
        meta["fallback_reason"] = result.fallback_reason
        meta["fallback_action_type"] = "customer_conditional_coupon_answer"
    return meta


async def try_compose_customer_conditional_coupon_answer(
    *,
    tenant_id: int,
    customer_phone: str,
    inbound_text: str,
    customer_conditional_coupon_facts: dict[str, Any],
    ai_settings: Optional[dict[str, Any]] = None,
) -> tuple[Optional[str], Optional[PersonaComposeResult], Optional[dict[str, Any]]]:
    """Compose conditional-coupon min-orders answer when consumption gate is active."""
    if not is_customer_conditional_coupon_compose_enabled():
        return None, None, None
    if not customer_conditional_coupon_facts:
        return None, None, None

    from .fact_bound_composer import FactBoundPersonaComposer  # noqa: PLC0415

    settings = dict(ai_settings or {})
    bundle = build_customer_conditional_coupon_answer_facts_bundle(
        inbound_text=inbound_text,
        tenant_id=tenant_id,
        customer_phone=customer_phone,
        customer_conditional_coupon_facts=customer_conditional_coupon_facts,
        merchant_persona=settings,
    )
    composer = FactBoundPersonaComposer(enforce_gate=False)
    result = await composer.compose(bundle)
    event_meta = build_customer_conditional_coupon_answer_event_metadata(
        result,
        tenant_id=int(tenant_id),
        compose_facts=customer_conditional_coupon_facts,
    )

    if result.source == "persona_llm" and result.guard_passed and (result.text or "").strip():
        return result.text.strip(), result, event_meta

    return None, None, None


__all__ = [
    "build_customer_conditional_coupon_answer_event_metadata",
    "build_customer_conditional_coupon_answer_facts_bundle",
    "try_compose_customer_conditional_coupon_answer",
]

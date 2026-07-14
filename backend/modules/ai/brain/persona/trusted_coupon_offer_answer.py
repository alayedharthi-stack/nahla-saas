"""Fact-bound persona compose for trusted coupon/offer availability answers."""
from __future__ import annotations

from typing import Any, Optional

from .facts_bundle import (
    PERSONA_SURFACE_TRUSTED_COUPON_OFFER_ANSWER,
    PersonaComposeResult,
    PersonaConstraints,
    PersonaFactsBundle,
)
from .integration import build_persona_compose_event_metadata
from modules.ai.brain.truth_surface.coupon_offer_compose_projection import (
    AVAILABILITY_ACTIVE_OR_ELIGIBLE,
    AVAILABILITY_NONE_VERIFIED,
    AVAILABILITY_PRESENT_BUT_NOT_ELIGIBLE,
    AVAILABILITY_REQUIRES_CONTEXT,
)
from modules.ai.brain.truth_surface.flags import (
    is_trusted_context_coupon_offer_compose_enabled,
)


def build_trusted_coupon_offer_answer_facts_bundle(
    *,
    inbound_text: str,
    tenant_id: int = 0,
    customer_phone: str = "",
    trusted_coupon_offer_facts: Optional[dict[str, Any]] = None,
    merchant_persona: Optional[dict[str, Any]] = None,
) -> PersonaFactsBundle:
    from .fact_bound_composer import detect_language  # noqa: PLC0415

    inbound = str(inbound_text or "").strip()
    language = detect_language(inbound)
    facts = dict(trusted_coupon_offer_facts or {})
    forbidden_claims: list[str] = [
        "coupon_code_disclosure",
        "coupon_applied_claim",
        "checkout_pressure",
    ]
    if not facts.get("allow_code_mention"):
        forbidden_claims.append("mention_coupon_code")
    if not facts.get("allow_final_eligibility_claim"):
        forbidden_claims.append("final_eligibility_claim")

    verified_facts: dict[str, Any] = {
        "surface": PERSONA_SURFACE_TRUSTED_COUPON_OFFER_ANSWER,
        "inbound_text": inbound,
        "question_kind": str(facts.get("question_kind") or "").strip(),
        "coupon_availability": str(facts.get("coupon_availability") or ""),
        "promotion_availability": str(facts.get("promotion_availability") or ""),
        "verified_eligible_coupon_count": int(facts.get("verified_eligible_coupon_count") or 0),
        "verified_eligible_promotion_count": int(
            facts.get("verified_eligible_promotion_count") or 0
        ),
        "coupon_record_count": int(facts.get("coupon_record_count") or 0),
        "promotion_record_count": int(facts.get("promotion_record_count") or 0),
        "unavailability_reason_codes": list(facts.get("unavailability_reason_codes") or []),
        "allow_code_mention": bool(facts.get("allow_code_mention")),
        "allow_final_eligibility_claim": bool(facts.get("allow_final_eligibility_claim")),
        "facts_snapshot_id": str(facts.get("facts_snapshot_id") or ""),
        "allow_checkout_pressure": False,
        "allow_slot_prompts": False,
    }
    return PersonaFactsBundle(
        surface=PERSONA_SURFACE_TRUSTED_COUPON_OFFER_ANSWER,
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


def build_trusted_coupon_offer_answer_event_metadata(
    result: PersonaComposeResult,
    *,
    tenant_id: int,
    compose_facts: dict[str, Any],
) -> dict[str, Any]:
    meta = build_persona_compose_event_metadata(
        result,
        tenant_id=int(tenant_id),
        allowlist_result="trusted_context_coupon_offer_compose",
    )
    meta["chosen_path"] = "trusted_coupon_offer_compose"
    meta["compose_source"] = str(result.source or "")
    meta["response_mode"] = "trusted_coupon_offer_answer"
    meta["llm_candidate_present"] = result.source == "persona_llm"
    meta["final_text_transformed"] = False
    meta["final_transform_reasons"] = []
    meta["trusted_coupon_offer_compose_active"] = True
    meta["question_kind"] = str(compose_facts.get("question_kind") or "")
    meta["facts_snapshot_id"] = str(compose_facts.get("facts_snapshot_id") or "")
    if result.fallback_reason:
        meta["fallback_reason"] = result.fallback_reason
        meta["fallback_action_type"] = "trusted_coupon_offer_answer"
    return meta


def trusted_coupon_offer_emergency_fallback(bundle: PersonaFactsBundle) -> str:
    """One short factual line — trusted facts only."""
    facts = bundle.verified_facts or {}
    question_kind = str(facts.get("question_kind") or "combined")
    coupon_av = str(facts.get("coupon_availability") or "")
    promo_av = str(facts.get("promotion_availability") or "")

    def _coupon_line() -> str:
        if coupon_av == AVAILABILITY_ACTIVE_OR_ELIGIBLE:
            return "في كوبونات خصم متاحة حسب بيانات المتجر المؤكدة."
        if coupon_av == AVAILABILITY_PRESENT_BUT_NOT_ELIGIBLE:
            return "في سجلات كوبون لكن ما نقدر نأكد أهليتها لهذا الطلب حالياً."
        if coupon_av == AVAILABILITY_REQUIRES_CONTEXT:
            return "أهلية الكوبون تحتاج تفاصيل إضافية عن الطلب قبل التأكيد."
        return "ما عندنا بيانات مؤكدة عن كوبونات خصم حالياً."

    def _offer_line() -> str:
        if promo_av == AVAILABILITY_ACTIVE_OR_ELIGIBLE:
            return "في عروض متاحة حسب بيانات المتجر المؤكدة."
        if promo_av == AVAILABILITY_PRESENT_BUT_NOT_ELIGIBLE:
            return "في سجلات عروض لكن ما نقدر نأكد أهليتها لهذا الطلب حالياً."
        if promo_av == AVAILABILITY_REQUIRES_CONTEXT:
            return "أهلية العروض تحتاج تفاصيل إضافية عن الطلب قبل التأكيد."
        return "ما عندنا بيانات مؤكدة عن عروض حالياً."

    if question_kind == "coupon":
        return _coupon_line()
    if question_kind == "offer":
        return _offer_line()
    if coupon_av == AVAILABILITY_ACTIVE_OR_ELIGIBLE or promo_av == AVAILABILITY_ACTIVE_OR_ELIGIBLE:
        return "في عروض أو كوبونات متاحة حسب بيانات المتجر المؤكدة."
    if coupon_av == AVAILABILITY_NONE_VERIFIED and promo_av == AVAILABILITY_NONE_VERIFIED:
        return "ما عندنا بيانات مؤكدة عن عروض أو كوبونات حالياً."
    return "حالة العروض والكوبونات قيد التحقق من بيانات المتجر."


def trusted_coupon_offer_compose_failure_response(
    *,
    tenant_id: int,
    customer_phone: str,
    inbound_text: str,
    trusted_coupon_offer_facts: dict[str, Any],
    ai_settings: Optional[dict[str, Any]] = None,
    fallback_reason: str,
    llm_candidate_present: bool = False,
) -> tuple[str, dict[str, Any]]:
    """Emergency fallback after genuine trusted coupon/offer compose failure."""
    from .fact_bound_composer import canonical_facts_hash  # noqa: PLC0415
    from .facts_bundle import PersonaComposeResult  # noqa: PLC0415

    bundle = build_trusted_coupon_offer_answer_facts_bundle(
        inbound_text=inbound_text,
        tenant_id=tenant_id,
        customer_phone=customer_phone,
        trusted_coupon_offer_facts=trusted_coupon_offer_facts,
        merchant_persona=dict(ai_settings or {}),
    )
    fallback_text = trusted_coupon_offer_emergency_fallback(bundle)
    fallback = PersonaComposeResult(
        text=fallback_text,
        source="fallback_deterministic",
        surface=PERSONA_SURFACE_TRUSTED_COUPON_OFFER_ANSWER,
        facts_hash=canonical_facts_hash(bundle.verified_facts),
        guard_passed=True,
        fallback_reason=str(fallback_reason or "compose_failed"),
        language=bundle.language,
        dialect=bundle.dialect,
        emoji_count=0,
        latency_ms=0,
        model="",
    )
    event_meta = build_trusted_coupon_offer_answer_event_metadata(
        fallback,
        tenant_id=int(tenant_id),
        compose_facts=trusted_coupon_offer_facts,
    )
    event_meta["compose_source"] = "fallback_deterministic"
    event_meta["llm_candidate_present"] = bool(llm_candidate_present)
    event_meta["fallback_reason"] = str(fallback_reason or "compose_failed")
    event_meta["fallback_action_type"] = "trusted_coupon_offer_answer"
    event_meta["chosen_path"] = "trusted_coupon_offer_compose"
    return fallback_text.strip(), event_meta


async def try_compose_trusted_coupon_offer_answer(
    *,
    tenant_id: int,
    customer_phone: str,
    inbound_text: str,
    trusted_coupon_offer_facts: dict[str, Any],
    ai_settings: Optional[dict[str, Any]] = None,
) -> tuple[Optional[str], Optional[PersonaComposeResult], Optional[dict[str, Any]]]:
    """Compose coupon/offer availability answer when consumption gate is active."""
    if not is_trusted_context_coupon_offer_compose_enabled():
        return None, None, None
    if not trusted_coupon_offer_facts:
        return None, None, None

    from .fact_bound_composer import FactBoundPersonaComposer  # noqa: PLC0415

    settings = dict(ai_settings or {})
    bundle = build_trusted_coupon_offer_answer_facts_bundle(
        inbound_text=inbound_text,
        tenant_id=tenant_id,
        customer_phone=customer_phone,
        trusted_coupon_offer_facts=trusted_coupon_offer_facts,
        merchant_persona=settings,
    )
    composer = FactBoundPersonaComposer(enforce_gate=False)
    result = await composer.compose(bundle)
    event_meta = build_trusted_coupon_offer_answer_event_metadata(
        result,
        tenant_id=int(tenant_id),
        compose_facts=trusted_coupon_offer_facts,
    )

    if result.source == "persona_llm" and result.guard_passed and (result.text or "").strip():
        return result.text.strip(), result, event_meta

    fallback_text = trusted_coupon_offer_emergency_fallback(bundle)
    from .fact_bound_composer import canonical_facts_hash  # noqa: PLC0415

    fallback = PersonaComposeResult(
        text=fallback_text,
        source="fallback_deterministic",
        surface=PERSONA_SURFACE_TRUSTED_COUPON_OFFER_ANSWER,
        facts_hash=canonical_facts_hash(bundle.verified_facts),
        guard_passed=True,
        fallback_reason=result.fallback_reason or result.guard_failed_reason or "compose_failed",
        language=bundle.language,
        dialect=bundle.dialect,
        emoji_count=0,
        latency_ms=result.latency_ms,
        model=result.model,
    )
    event_meta = build_trusted_coupon_offer_answer_event_metadata(
        fallback,
        tenant_id=int(tenant_id),
        compose_facts=trusted_coupon_offer_facts,
    )
    event_meta["compose_source"] = "fallback_deterministic"
    event_meta["llm_candidate_present"] = bool((result.text or "").strip())
    return fallback_text.strip(), fallback, event_meta


__all__ = [
    "build_trusted_coupon_offer_answer_event_metadata",
    "build_trusted_coupon_offer_answer_facts_bundle",
    "trusted_coupon_offer_compose_failure_response",
    "try_compose_trusted_coupon_offer_answer",
    "trusted_coupon_offer_emergency_fallback",
]

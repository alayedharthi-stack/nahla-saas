"""Fact-bound persona compose for general offer discovery (namespaced bundles)."""
from __future__ import annotations

from typing import Any, Optional

from .facts_bundle import (
    PERSONA_SURFACE_GENERAL_OFFER_DISCOVERY_ANSWER,
    PersonaComposeResult,
    PersonaConstraints,
    PersonaFactsBundle,
)
from .integration import build_persona_compose_event_metadata
from modules.ai.brain.truth_surface.flags import is_general_offer_discovery_compose_enabled


def build_general_offer_discovery_facts_bundle(
    *,
    inbound_text: str,
    tenant_id: int = 0,
    customer_phone: str = "",
    general_offer_discovery_facts: Optional[dict[str, Any]] = None,
    merchant_persona: Optional[dict[str, Any]] = None,
) -> PersonaFactsBundle:
    from .fact_bound_composer import detect_language  # noqa: PLC0415

    inbound = str(inbound_text or "").strip()
    language = detect_language(inbound)
    facts = dict(general_offer_discovery_facts or {})
    product_bundle = dict(facts.get("product_sale_offer_facts") or {}) if facts.get("product_sale_offer_facts") else {}
    coupon_bundle = dict(facts.get("trusted_coupon_offer_facts") or {}) if facts.get("trusted_coupon_offer_facts") else {}

    verified_facts: dict[str, Any] = {
        "surface": PERSONA_SURFACE_GENERAL_OFFER_DISCOVERY_ANSWER,
        "inbound_text": inbound,
        "question_route": str(facts.get("question_route") or "general_offer_discovery"),
        "product_sale_offer_facts": product_bundle or None,
        "trusted_coupon_offer_facts": coupon_bundle or None,
        "forbidden_claims": list(facts.get("forbidden_claims") or []),
        "facts_snapshot_id": str(facts.get("facts_snapshot_id") or ""),
        "allow_checkout_pressure": False,
        "allow_slot_prompts": False,
    }
    return PersonaFactsBundle(
        surface=PERSONA_SURFACE_GENERAL_OFFER_DISCOVERY_ANSWER,
        inbound_text=inbound,
        language=language,
        dialect="saudi_arabic" if language == "ar" else None,
        verified_facts=verified_facts,
        customer_context={},
        merchant_persona=dict(merchant_persona or {}),
        constraints=PersonaConstraints(
            max_chars=420,
            max_emojis=1,
            forbidden_claims=(
                "invent_coupon_eligibility_from_catalog_sale",
                "invent_catalog_sale_from_promotion_eligibility",
                "mention_coupon_code",
                "coupon_applied_claim",
                "checkout_pressure",
            ),
        ),
        tenant_id=int(tenant_id or 0),
        customer_phone=str(customer_phone or ""),
    )


def build_general_offer_discovery_event_metadata(
    result: PersonaComposeResult,
    *,
    tenant_id: int,
    compose_facts: dict[str, Any],
) -> dict[str, Any]:
    meta = build_persona_compose_event_metadata(
        result,
        tenant_id=int(tenant_id),
        allowlist_result="trusted_context_general_offer_discovery_compose",
    )
    meta["chosen_path"] = "general_offer_discovery_compose"
    meta["compose_source"] = str(result.source or "")
    meta["response_mode"] = "general_offer_discovery_answer"
    meta["llm_candidate_present"] = result.source == "persona_llm"
    meta["final_text_transformed"] = False
    meta["final_transform_reasons"] = []
    meta["general_offer_discovery_compose_active"] = True
    meta["facts_snapshot_id"] = str(compose_facts.get("facts_snapshot_id") or "")
    if result.fallback_reason:
        meta["fallback_reason"] = result.fallback_reason
        meta["fallback_action_type"] = "general_offer_discovery_answer"
    return meta


def general_offer_discovery_emergency_fallback(bundle: PersonaFactsBundle) -> str:
    facts = bundle.verified_facts or {}
    product = dict(facts.get("product_sale_offer_facts") or {})
    coupon = dict(facts.get("trusted_coupon_offer_facts") or {})
    product_av = str(product.get("product_sale_availability") or "")
    if product_av == "active_sale_present":
        return "في منتجات بأسعار مخفّضة حسب بيانات الكتالوج المؤكدة."
    if coupon:
        return "في بيانات عروض أو كوبونات حسب ما هو متاح من مصادر المتجر المؤكدة."
    return "ما عندنا بيانات مؤكدة كافية عن العروض حالياً."


async def try_compose_general_offer_discovery_answer(
    *,
    tenant_id: int,
    customer_phone: str,
    inbound_text: str,
    general_offer_discovery_facts: dict[str, Any],
    ai_settings: Optional[dict[str, Any]] = None,
) -> tuple[Optional[str], Optional[PersonaComposeResult], Optional[dict[str, Any]]]:
    if not is_general_offer_discovery_compose_enabled():
        return None, None, None
    if not general_offer_discovery_facts:
        return None, None, None

    from .fact_bound_composer import FactBoundPersonaComposer  # noqa: PLC0415

    bundle = build_general_offer_discovery_facts_bundle(
        inbound_text=inbound_text,
        tenant_id=tenant_id,
        customer_phone=customer_phone,
        general_offer_discovery_facts=general_offer_discovery_facts,
        merchant_persona=dict(ai_settings or {}),
    )
    composer = FactBoundPersonaComposer(enforce_gate=False)
    result = await composer.compose(bundle)
    if not (result.text or "").strip():
        return None, None, None
    event_meta = build_general_offer_discovery_event_metadata(
        result,
        tenant_id=int(tenant_id),
        compose_facts=general_offer_discovery_facts,
    )
    return (result.text or "").strip(), result, event_meta


__all__ = [
    "build_general_offer_discovery_event_metadata",
    "build_general_offer_discovery_facts_bundle",
    "general_offer_discovery_emergency_fallback",
    "try_compose_general_offer_discovery_answer",
]

"""Fact-bound persona compose for product-scoped catalog sale offers."""
from __future__ import annotations

from typing import Any, Optional

from .facts_bundle import (
    PERSONA_SURFACE_PRODUCT_SALE_OFFER_ANSWER,
    PersonaComposeResult,
    PersonaConstraints,
    PersonaFactsBundle,
)
from .integration import build_persona_compose_event_metadata
from modules.ai.brain.truth_surface.flags import is_product_sale_offer_compose_enabled


def build_product_sale_offer_facts_bundle(
    *,
    inbound_text: str,
    tenant_id: int = 0,
    customer_phone: str = "",
    product_sale_offer_facts: Optional[dict[str, Any]] = None,
    merchant_persona: Optional[dict[str, Any]] = None,
) -> PersonaFactsBundle:
    from .fact_bound_composer import detect_language  # noqa: PLC0415

    inbound = str(inbound_text or "").strip()
    language = detect_language(inbound)
    facts = dict(product_sale_offer_facts or {})
    verified_facts: dict[str, Any] = {
        "surface": PERSONA_SURFACE_PRODUCT_SALE_OFFER_ANSWER,
        "inbound_text": inbound,
        "question_kind": str(facts.get("question_kind") or "product_scoped"),
        "product_sale_availability": str(facts.get("product_sale_availability") or ""),
        "verified_on_sale_product_count": int(facts.get("verified_on_sale_product_count") or 0),
        "target_product": dict(facts.get("target_product") or {}) or None,
        "allow_price_mention": bool(facts.get("allow_price_mention")),
        "facts_snapshot_id": str(facts.get("facts_snapshot_id") or ""),
        "allow_checkout_pressure": False,
        "allow_slot_prompts": False,
    }
    return PersonaFactsBundle(
        surface=PERSONA_SURFACE_PRODUCT_SALE_OFFER_ANSWER,
        inbound_text=inbound,
        language=language,
        dialect="saudi_arabic" if language == "ar" else None,
        verified_facts=verified_facts,
        customer_context={},
        merchant_persona=dict(merchant_persona or {}),
        constraints=PersonaConstraints(
            max_chars=360,
            max_emojis=1,
            forbidden_claims=("checkout_pressure", "coupon_applied_claim", "mention_coupon_code"),
        ),
        tenant_id=int(tenant_id or 0),
        customer_phone=str(customer_phone or ""),
    )


def build_product_sale_offer_event_metadata(
    result: PersonaComposeResult,
    *,
    tenant_id: int,
    compose_facts: dict[str, Any],
) -> dict[str, Any]:
    meta = build_persona_compose_event_metadata(
        result,
        tenant_id=int(tenant_id),
        allowlist_result="trusted_context_product_sale_offer_compose",
    )
    meta["chosen_path"] = "product_sale_offer_compose"
    meta["compose_source"] = str(result.source or "")
    meta["response_mode"] = "product_sale_offer_answer"
    meta["llm_candidate_present"] = result.source == "persona_llm"
    meta["final_text_transformed"] = False
    meta["final_transform_reasons"] = []
    meta["product_sale_offer_compose_active"] = True
    meta["facts_snapshot_id"] = str(compose_facts.get("facts_snapshot_id") or "")
    if result.fallback_reason:
        meta["fallback_reason"] = result.fallback_reason
        meta["fallback_action_type"] = "product_sale_offer_answer"
    return meta


def product_sale_offer_emergency_fallback(bundle: PersonaFactsBundle) -> str:
    facts = bundle.verified_facts or {}
    availability = str(facts.get("product_sale_availability") or "")
    if availability == "active_sale_present":
        return "المنتج الحالي عليه سعر مخفّض حسب بيانات الكتالوج المؤكدة."
    if availability == "none_verified":
        return "ما في سعر مخفّض مؤكد للمنتج الحالي حسب الكتالوج."
    if availability == "requires_product_context":
        return "يلزم تحديد المنتج أولاً قبل تأكيد وجود عرض سعر."
    return "ما عندنا بيانات مؤكدة كافية عن عرض السعر حالياً."


async def try_compose_product_sale_offer_answer(
    *,
    tenant_id: int,
    customer_phone: str,
    inbound_text: str,
    product_sale_offer_facts: dict[str, Any],
    ai_settings: Optional[dict[str, Any]] = None,
) -> tuple[Optional[str], Optional[PersonaComposeResult], Optional[dict[str, Any]]]:
    if not is_product_sale_offer_compose_enabled():
        return None, None, None
    if not product_sale_offer_facts:
        return None, None, None

    from .fact_bound_composer import FactBoundPersonaComposer  # noqa: PLC0415

    bundle = build_product_sale_offer_facts_bundle(
        inbound_text=inbound_text,
        tenant_id=tenant_id,
        customer_phone=customer_phone,
        product_sale_offer_facts=product_sale_offer_facts,
        merchant_persona=dict(ai_settings or {}),
    )
    composer = FactBoundPersonaComposer(enforce_gate=False)
    result = await composer.compose(bundle)
    if not (result.text or "").strip():
        return None, None, None
    event_meta = build_product_sale_offer_event_metadata(
        result,
        tenant_id=int(tenant_id),
        compose_facts=product_sale_offer_facts,
    )
    return (result.text or "").strip(), result, event_meta


__all__ = [
    "build_product_sale_offer_event_metadata",
    "build_product_sale_offer_facts_bundle",
    "product_sale_offer_emergency_fallback",
    "try_compose_product_sale_offer_answer",
]

"""Fact-bound persona compose for CE4 product knowledge answers."""
from __future__ import annotations

from typing import Any, Optional

from .facts_bundle import (
    PERSONA_SURFACE_KB_PRODUCT_ANSWER,
    PersonaComposeResult,
    PersonaConstraints,
    PersonaFactsBundle,
)
from .integration import (
    build_persona_compose_event_metadata,
)
from modules.ai.brain.commerce.product_knowledge_or_comparison import (
    TOPIC_PRODUCT_KNOWLEDGE_FACTS,
)

MISSING_KB_CLARIFICATION_AR = (
    "ما عندي تفاصيل مؤكدة عن هذا المنتج في قاعدة المعرفة حاليًا."
)


def _kb_sections_from_allowed(allowed_facts: dict[str, Any]) -> list[dict[str, Any]]:
    raw = allowed_facts.get("kb_sections") if isinstance(allowed_facts, dict) else None
    if not isinstance(raw, list):
        return []
    return [dict(s) for s in raw if isinstance(s, dict)]


def build_kb_product_answer_facts_bundle(
    *,
    inbound_text: str,
    tenant_id: int = 0,
    customer_phone: str = "",
    question_kind: str = "",
    allowed_facts: Optional[dict[str, Any]] = None,
    missing_facts: Optional[list[str]] = None,
    subject_product: Optional[dict[str, Any]] = None,
    merchant_persona: Optional[dict[str, Any]] = None,
) -> PersonaFactsBundle:
    from .fact_bound_composer import detect_language  # noqa: PLC0415

    inbound = str(inbound_text or "").strip()
    language = detect_language(inbound)
    allowed = dict(allowed_facts or {})
    subject = dict(subject_product or {})
    kb_sections = _kb_sections_from_allowed(allowed)
    kb_text = " ".join(
        f"{s.get('title', '')} {s.get('body', '')}".strip()
        for s in kb_sections
    ).strip()
    subject_title = str(
        allowed.get("product_title")
        or subject.get("title")
        or subject.get("title_hint_from_message")
        or ""
    ).strip()
    catalog_price = allowed.get("catalog_price")
    availability = allowed.get("catalog_availability") or allowed.get("availability")
    verified_facts: dict[str, Any] = {
        "surface": PERSONA_SURFACE_KB_PRODUCT_ANSWER,
        "inbound_text": inbound,
        "question_kind": str(question_kind or "").strip(),
        "subject_title": subject_title,
        "kb_sections": kb_sections,
        "kb_section_ids": [
            s.get("section_id") for s in kb_sections if s.get("section_id") is not None
        ],
        "kb_text": kb_text[:2000],
        "missing_facts": list(missing_facts or []),
        "catalog_product_id": allowed.get("product_id"),
        "allow_price_mention": catalog_price is not None,
        "catalog_price": catalog_price,
        "allow_availability_mention": availability is not None,
        "availability": availability,
        "allow_checkout_pressure": False,
        "allow_slot_prompts": False,
        "allow_medical_claims": str(question_kind or "").strip() == "health",
        "has_kb_sections": bool(kb_sections),
    }
    if catalog_price is not None:
        verified_facts["price_source"] = "catalog"
    if availability is not None:
        verified_facts["availability_source"] = str(
            allowed.get("availability_source") or "catalog"
        )
    return PersonaFactsBundle(
        surface=PERSONA_SURFACE_KB_PRODUCT_ANSWER,
        inbound_text=inbound,
        language=language,
        dialect="saudi_arabic" if language == "ar" else None,
        verified_facts=verified_facts,
        customer_context={},
        merchant_persona=dict(merchant_persona or {}),
        constraints=PersonaConstraints(max_chars=420, max_emojis=2),
        tenant_id=int(tenant_id or 0),
        customer_phone=str(customer_phone or ""),
    )


def build_kb_product_answer_event_metadata(
    result: PersonaComposeResult,
    *,
    tenant_id: int,
    allowlist_result: str,
    decision_args: dict[str, Any],
) -> dict[str, Any]:
    """Outbound metadata for KB product knowledge persona compose."""
    allowed = dict(decision_args.get("allowed_facts") or {})
    kb_sections = _kb_sections_from_allowed(allowed)
    kb_ids = [
        s.get("section_id") for s in kb_sections if s.get("section_id") is not None
    ]
    meta = build_persona_compose_event_metadata(
        result,
        tenant_id=int(tenant_id),
        allowlist_result=str(allowlist_result or ""),
    )
    meta.update({
        "compose_source": result.source,
        "response_mode": "grounded_persona_compose",
        "llm_candidate_present": result.source == "persona_llm",
        "final_text_transformed": False,
        "final_transform_reasons": [],
        "final_customer_text_source": result.source,
    })
    if result.source == "fallback_deterministic":
        meta.update({
            "fallback_reason": str(
                result.fallback_reason or "compose_unavailable"
            ),
            "fallback_action_type": "kb_product_answer",
        })
    meta["knowledge_source"] = (
        "tenant_knowledge_base" if kb_sections else "missing_kb"
    )
    meta["kb_section_ids"] = kb_ids
    meta["question_kind"] = str(decision_args.get("question_kind") or "").strip()
    product_id = allowed.get("product_id")
    if product_id is not None:
        meta["catalog_product_id"] = product_id
    if allowed.get("catalog_price") is not None:
        meta["price_source"] = "catalog"
    if allowed.get("catalog_availability") is not None or allowed.get("availability") is not None:
        meta["availability_source"] = str(
            allowed.get("availability_source") or "catalog"
        )
    return meta


def _kb_compose_emergency_fallback(
    bundle: PersonaFactsBundle,
    *,
    reason: str,
) -> PersonaComposeResult:
    from .fact_bound_composer import canonical_facts_hash  # noqa: PLC0415
    from .fallback_catalog import deterministic_fallback  # noqa: PLC0415

    resolved_reason = str(reason or "compose_unavailable")
    text = deterministic_fallback(bundle, reason=resolved_reason)
    return PersonaComposeResult(
        text=text,
        source="fallback_deterministic",
        surface=PERSONA_SURFACE_KB_PRODUCT_ANSWER,
        facts_hash=canonical_facts_hash(bundle.verified_facts),
        guard_passed=False,
        fallback_reason=resolved_reason,
        language=bundle.language,
        dialect=bundle.dialect,
        emoji_count=0,
        latency_ms=0,
        model=None,
    )


async def try_compose_kb_product_answer(
    *,
    tenant_id: int,
    customer_phone: str,
    inbound_text: str,
    decision_args: dict[str, Any],
    ai_settings: Optional[dict[str, Any]] = None,
) -> tuple[Optional[str], Optional[PersonaComposeResult], Optional[dict[str, Any]]]:
    """Compose KB-grounded product knowledge when test-mode gate passes."""
    from .fact_bound_composer import FactBoundPersonaComposer  # noqa: PLC0415

    settings = dict(ai_settings or {})
    topic = str(decision_args.get("topic") or "").strip()
    if topic != TOPIC_PRODUCT_KNOWLEDGE_FACTS:
        return None, None, None

    bundle = build_kb_product_answer_facts_bundle(
        inbound_text=inbound_text,
        tenant_id=tenant_id,
        customer_phone=customer_phone,
        question_kind=str(decision_args.get("question_kind") or ""),
        allowed_facts=dict(decision_args.get("allowed_facts") or {}),
        missing_facts=list(decision_args.get("missing_facts") or []),
        subject_product=dict(decision_args.get("subject_product") or {}),
        merchant_persona=settings,
    )
    from .flags import persona_composer_allowlist_result  # noqa: PLC0415

    allowlist_result = persona_composer_allowlist_result(
        tenant_id=int(tenant_id),
        customer_phone=str(customer_phone or ""),
        ai_settings=settings,
    )

    composer = FactBoundPersonaComposer(enforce_gate=False)
    try:
        result = await composer.compose(bundle)
    except Exception as exc:  # noqa: BLE001
        result = _kb_compose_emergency_fallback(
            bundle,
            reason=f"compose_exception:{type(exc).__name__}",
        )
    event_meta = build_kb_product_answer_event_metadata(
        result,
        tenant_id=int(tenant_id),
        allowlist_result=allowlist_result,
        decision_args=decision_args,
    )
    text = (result.text or "").strip()
    if (
        result.source == "persona_llm"
        and result.guard_passed
        and text
    ):
        return text, result, event_meta

    fallback = _kb_compose_emergency_fallback(
        bundle,
        reason=(
            result.fallback_reason
            or result.guard_failed_reason
            or "compose_empty"
        ),
    )
    event_meta = build_kb_product_answer_event_metadata(
        fallback,
        tenant_id=int(tenant_id),
        allowlist_result=allowlist_result,
        decision_args=decision_args,
    )
    return fallback.text.strip(), fallback, event_meta

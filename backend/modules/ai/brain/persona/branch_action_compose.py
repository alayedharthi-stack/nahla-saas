"""
Platform-wide lightweight compose for structured branch actions (location, arrival, contact).

Routing and WhatsApp payloads remain deterministic; only the surrounding customer
wording is LLM-composed. Not gated by tenant-33 persona allowlists.
"""
from __future__ import annotations

import logging
import os
import re
from dataclasses import dataclass, field
from typing import Any, Dict, Mapping, Optional

from .fact_bound_composer import (
    FactBoundPersonaComposer,
    canonical_facts_hash,
    detect_language,
    resolve_persona_compose_model_route,
)
from .facts_bundle import (
    PERSONA_SURFACE_BRANCH_ACTION,
    PersonaComposeResult,
    PersonaConstraints,
    PersonaFactsBundle,
)

logger = logging.getLogger("nahla.brain.persona.branch_action_compose")

ACTION_KIND_LOCATION = "location"
ACTION_KIND_ARRIVAL_SOFT = "arrival_soft"
ACTION_KIND_BRANCH_CONTACT = "branch_contact"

_FLAG_FALSY = frozenset({"0", "false", "no", "off"})
_URL_RE = re.compile(r"https?://\S+", re.IGNORECASE)
_PHONE_RE = re.compile(r"(?:\+?966|05)\d{7,9}")


def branch_action_compose_enabled() -> bool:
    raw = os.getenv("BRANCH_ACTION_COMPOSE_ENABLED", "1").strip().lower()
    return raw not in _FLAG_FALSY


@dataclass(frozen=True)
class BranchComposeFacts:
    action_kind: str
    customer_message: str = ""
    merchant_name: str = ""
    branch_name: str = ""
    location_already_sent: bool = False
    maps_cta_available: bool = False
    contact_card_available: bool = False
    contact_name: str = ""
    contact_role: str = ""
    maps_configured: bool = True
    needs_pickup_preference_choice: bool = False
    location_instructions: str = ""

    def to_verified_facts(self) -> Dict[str, Any]:
        return {
            "action_kind": self.action_kind,
            "merchant_name": self.merchant_name,
            "branch_name": self.branch_name,
            "location_already_sent": self.location_already_sent,
            "maps_cta_available": self.maps_cta_available,
            "contact_card_available": self.contact_card_available,
            "contact_name": self.contact_name,
            "contact_role": self.contact_role,
            "maps_configured": self.maps_configured,
            "needs_pickup_preference_choice": self.needs_pickup_preference_choice,
            "location_instructions": self.location_instructions,
            "customer_message": self.customer_message,
        }


@dataclass(frozen=True)
class BranchActionComposeOutcome:
    text: str
    compose_source: str
    fallback_reason: str = ""
    structured_action: str = ""
    persona_result: Optional[PersonaComposeResult] = None
    model: Optional[str] = None

    def to_metadata(self) -> Dict[str, Any]:
        meta: Dict[str, Any] = {
            "structured_action": self.structured_action,
            "compose_source": self.compose_source,
        }
        if self.fallback_reason:
            meta["fallback_reason"] = self.fallback_reason
        if self.persona_result is not None:
            meta["persona_compose"] = {
                "surface": self.persona_result.surface,
                "source": self.persona_result.source,
                "guard_passed": self.persona_result.guard_passed,
                "facts_hash": self.persona_result.facts_hash,
                "latency_ms": self.persona_result.latency_ms,
            }
        if self.model:
            meta["compose_model"] = self.model
        return meta


def build_branch_action_facts_bundle(
    facts: BranchComposeFacts,
    *,
    tenant_id: int = 0,
    customer_phone: str = "",
    merchant_persona: Optional[Mapping[str, Any]] = None,
) -> PersonaFactsBundle:
    inbound = str(facts.customer_message or "").strip()
    language = detect_language(inbound)
    return PersonaFactsBundle(
        surface=PERSONA_SURFACE_BRANCH_ACTION,
        inbound_text=inbound,
        language=language,
        dialect="saudi_arabic" if language == "ar" else None,
        verified_facts=facts.to_verified_facts(),
        customer_context={"conversation_language": language},
        merchant_persona=dict(merchant_persona or {}),
        constraints=PersonaConstraints(max_chars=160, max_emojis=1),
        tenant_id=int(tenant_id or 0),
        customer_phone=str(customer_phone or ""),
    )


def minimal_emergency_fallback(facts: BranchComposeFacts, *, reason: str = "") -> str:
    """One short factual line — emergency only, not the primary path."""
    if not facts.maps_configured:
        return "الموقع غير مهيّأ حالياً."
    if facts.action_kind == ACTION_KIND_LOCATION and facts.branch_name:
        return f"📍 {facts.branch_name}"
    if facts.action_kind == ACTION_KIND_ARRIVAL_SOFT:
        return "👋"
    if facts.action_kind == ACTION_KIND_BRANCH_CONTACT:
        if facts.contact_name:
            return facts.contact_name
        if facts.branch_name:
            return facts.branch_name
    return "…"


def guard_branch_action_body(text: str, facts: BranchComposeFacts) -> str:
    """Strip URLs/phones from composed body when structured delivery carries them."""
    body = (text or "").strip()
    if not body:
        return body
    if facts.maps_cta_available:
        body = _URL_RE.sub("", body).strip()
    if facts.contact_card_available:
        body = _PHONE_RE.sub("", body).strip()
    body = re.sub(r"\n{3,}", "\n\n", body).strip()
    return body


def _build_compose_prompts(bundle: PersonaFactsBundle) -> tuple[str, str]:
    facts = bundle.verified_facts or {}
    lang = str(bundle.language or "ar").lower()
    if lang.startswith("en"):
        system = (
            "You are a warm Saudi merchant assistant on WhatsApp. "
            "Write ONE short natural message (max 2 lines). "
            "Use only supplied facts. Do not invent names, URLs, phones, hours, or availability."
        )
    else:
        system = (
            "أنت مساعد تاجر سعودي ودود على واتساب. "
            "اكتب رسالة قصيرة طبيعية (سطر أو سطرين). "
            "استخدم الحقائق المعطاة فقط. ممنوع اختراع أسماء أو روابط أو أرقام أو ساعات أو تواجد موظفين."
        )
    lines = [
        f"action_kind: {facts.get('action_kind')}",
        f"customer_message: {facts.get('customer_message')}",
        f"merchant_name: {facts.get('merchant_name')}",
        f"branch_name: {facts.get('branch_name')}",
        f"location_already_sent: {facts.get('location_already_sent')}",
        f"maps_cta_available: {facts.get('maps_cta_available')}",
        f"contact_card_available: {facts.get('contact_card_available')}",
        f"contact_name: {facts.get('contact_name')}",
        f"contact_role: {facts.get('contact_role')}",
    ]
    if facts.get("location_instructions"):
        lines.append(f"location_instructions: {facts.get('location_instructions')}")
    if facts.get("needs_pickup_preference_choice"):
        lines.append("needs_pickup_preference_choice: true")
    lines.append(
        "rules: do not include raw maps URLs or phone numbers when maps_cta_available "
        "or contact_card_available is true; do not say you sent a button/card; "
        "do not claim staff answered or is present; arrival_soft must differ from "
        "a first location reply when location_already_sent is true"
    )
    return system, "\n".join(lines)


def load_merchant_display_name(db: Any, tenant_id: int) -> str:
    try:
        from database.models import StoreKnowledgeSnapshot  # noqa: PLC0415
        from core.store_display import clean_store_name  # noqa: PLC0415

        snap = (
            db.query(StoreKnowledgeSnapshot)
            .filter(StoreKnowledgeSnapshot.tenant_id == int(tenant_id or 0))
            .first()
        )
        if snap and snap.store_profile:
            return clean_store_name(str(snap.store_profile.get("name") or ""))
    except Exception:  # noqa: silent-ok — store name is optional compose context
        pass
    return ""


async def compose_branch_trigger_body(
    db: Any,
    *,
    tenant_id: int,
    customer_phone: str,
    compose_facts: BranchComposeFacts,
    merchant_persona: Optional[Mapping[str, Any]] = None,
    llm_callable: Any = None,
) -> BranchActionComposeOutcome:
    facts = BranchComposeFacts(
        action_kind=compose_facts.action_kind,
        customer_message=compose_facts.customer_message,
        merchant_name=compose_facts.merchant_name or load_merchant_display_name(db, tenant_id),
        branch_name=compose_facts.branch_name,
        location_already_sent=compose_facts.location_already_sent,
        maps_cta_available=compose_facts.maps_cta_available,
        contact_card_available=compose_facts.contact_card_available,
        contact_name=compose_facts.contact_name,
        contact_role=compose_facts.contact_role,
        maps_configured=compose_facts.maps_configured,
        needs_pickup_preference_choice=compose_facts.needs_pickup_preference_choice,
        location_instructions=compose_facts.location_instructions,
    )
    return await try_compose_branch_action(
        facts,
        tenant_id=tenant_id,
        customer_phone=customer_phone,
        merchant_persona=merchant_persona,
        llm_callable=llm_callable,
    )


async def try_compose_branch_action(
    facts: BranchComposeFacts,
    *,
    tenant_id: int = 0,
    customer_phone: str = "",
    merchant_persona: Optional[Mapping[str, Any]] = None,
    llm_callable: Any = None,
) -> BranchActionComposeOutcome:
    structured_action = str(facts.action_kind or "").strip()
    if not branch_action_compose_enabled():
        fb = minimal_emergency_fallback(facts, reason="compose_disabled")
        return BranchActionComposeOutcome(
            text=fb,
            compose_source="fallback_deterministic",
            fallback_reason="compose_disabled",
            structured_action=structured_action,
        )

    bundle = build_branch_action_facts_bundle(
        facts,
        tenant_id=tenant_id,
        customer_phone=customer_phone,
        merchant_persona=merchant_persona,
    )

    async def _default_llm(b: PersonaFactsBundle) -> str:
        route = resolve_persona_compose_model_route(b)
        system, user = _build_compose_prompts(b)
        import asyncio  # noqa: PLC0415

        def _sync() -> str:
            provider_key = str(route.provider or "").strip().lower()
            if provider_key == "openai_compatible":
                from modules.ai.orchestrator.providers.openai_compatible_provider import (  # noqa: PLC0415
                    OpenAICompatibleProvider,
                )

                provider = OpenAICompatibleProvider()
                if not provider.is_configured():
                    return ""
                result = provider.call(
                    user,
                    system,
                    audit_context={
                        "reason": "brain.persona.branch_action_compose",
                        "surface": PERSONA_SURFACE_BRANCH_ACTION,
                        "tenant_id": b.tenant_id,
                        "model_override": route.model,
                    },
                )
                if isinstance(result, dict):
                    return str(result.get("reply_text") or "").strip()
                return str(result or "").strip()
            return ""

        return str(await asyncio.wait_for(asyncio.to_thread(_sync), timeout=12.0) or "").strip()

    callable_fn = llm_callable or _default_llm
    composer = FactBoundPersonaComposer(enforce_gate=False, llm_callable=callable_fn)
    try:
        result = await composer.compose(bundle)
    except Exception as exc:  # noqa: BLE001
        logger.warning(
            "[BRANCH_ACTION_COMPOSE] compose_failed tenant=%s action=%s err=%s",
            tenant_id,
            structured_action,
            exc,
        )
        fb = minimal_emergency_fallback(facts, reason=type(exc).__name__)
        return BranchActionComposeOutcome(
            text=fb,
            compose_source="fallback_deterministic",
            fallback_reason=type(exc).__name__,
            structured_action=structured_action,
        )

    raw = guard_branch_action_body(result.text, facts)
    if raw and result.source == "persona_llm" and result.guard_passed:
        return BranchActionComposeOutcome(
            text=raw,
            compose_source="persona_llm",
            structured_action=structured_action,
            persona_result=result,
            model=result.model,
        )

    reason = result.fallback_reason or result.guard_failed_reason or "compose_empty"
    fb = minimal_emergency_fallback(facts, reason=reason)
    guarded_fb = guard_branch_action_body(fb, facts) or fb
    return BranchActionComposeOutcome(
        text=guarded_fb,
        compose_source="fallback_deterministic",
        fallback_reason=reason,
        structured_action=structured_action,
        persona_result=result,
        model=result.model,
    )


def plain_text_location_fallback_body(
    composed_body: str,
    maps_url: str,
    *,
    use_cta: bool,
) -> str:
    """Append trusted maps URL only when CTA is unavailable."""
    body = (composed_body or "").strip()
    url = (maps_url or "").strip()
    if use_cta or not url:
        return body
    if url in body:
        return body
    sep = "\n" if body else ""
    return f"{body}{sep}{url}".strip()


__all__ = [
    "ACTION_KIND_ARRIVAL_SOFT",
    "ACTION_KIND_BRANCH_CONTACT",
    "ACTION_KIND_LOCATION",
    "BranchActionComposeOutcome",
    "BranchComposeFacts",
    "branch_action_compose_enabled",
    "build_branch_action_facts_bundle",
    "guard_branch_action_body",
    "compose_branch_trigger_body",
    "load_merchant_display_name",
    "minimal_emergency_fallback",
    "plain_text_location_fallback_body",
    "try_compose_branch_action",
]

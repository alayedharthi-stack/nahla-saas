"""Fact-bound persona compose for payment media intro text only."""
from __future__ import annotations

from typing import Any, Optional

from .fact_bound_composer import FactBoundPersonaComposer, detect_language
from .facts_bundle import (
    PERSONA_SURFACE_PAYMENT_MEDIA_INTRO,
    PersonaComposeResult,
    PersonaConstraints,
    PersonaFactsBundle,
)
from .integration import (
    build_persona_compose_event_metadata,
    should_enforce_persona_compose_for_surface,
)
from modules.ai.brain.decision.payment_barcode_routing import payment_barcode_intro_text


def infer_payment_media_kind(media_key: str) -> str:
    key = str(media_key or "").strip().lower()
    if "qr" in key or "كيو" in key:
        return "qr"
    if key.endswith("_barcode") or "barcode" in key or "باركود" in key:
        return "barcode"
    if "receipt" in key or "إيصال" in key:
        return "receipt_request"
    return "payment_image"


def build_payment_media_intro_facts_bundle(
    *,
    inbound_text: str,
    tenant_id: int = 0,
    customer_phone: str = "",
    media_key: str = "",
    media_url_present: bool = False,
    payment_method: str = "bank_transfer",
    payment_status: str = "pending",
    order_id: Optional[str] = None,
    amount: Optional[str] = None,
    merchant_persona: Optional[dict[str, Any]] = None,
) -> PersonaFactsBundle:
    inbound = str(inbound_text or "").strip()
    language = detect_language(inbound)
    media_kind = infer_payment_media_kind(media_key)
    verified_facts = {
        "surface": PERSONA_SURFACE_PAYMENT_MEDIA_INTRO,
        "inbound_text": inbound,
        "media_key": str(media_key or "").strip(),
        "media_kind": media_kind,
        "media_url_present": bool(media_url_present),
        "payment_method": str(payment_method or "").strip() or "bank_transfer",
        "payment_status": str(payment_status or "").strip() or "pending",
        "order_id": str(order_id or "").strip(),
        "amount": str(amount or "").strip(),
        "allow_checkout_pressure": False,
        "allow_slot_prompts": False,
        "allow_paid_claim": str(payment_status or "").strip().lower() == "confirmed",
        "allow_receipt_request": str(payment_status or "").strip().lower()
        not in {"confirmed", "paid"},
    }
    return PersonaFactsBundle(
        surface=PERSONA_SURFACE_PAYMENT_MEDIA_INTRO,
        inbound_text=inbound,
        language=language,
        dialect="saudi_arabic" if language == "ar" else None,
        verified_facts=verified_facts,
        customer_context={},
        merchant_persona=dict(merchant_persona or {}),
        constraints=PersonaConstraints(max_chars=200, max_emojis=2),
        tenant_id=int(tenant_id or 0),
        customer_phone=str(customer_phone or ""),
    )


async def try_compose_payment_media_intro(
    *,
    tenant_id: int,
    customer_phone: str,
    inbound_text: str,
    media_key: str,
    media_url_present: bool,
    payment_method: str = "bank_transfer",
    payment_status: str = "pending",
    order_id: Optional[str] = None,
    amount: Optional[str] = None,
    ai_settings: Optional[dict[str, Any]] = None,
) -> tuple[str, Optional[PersonaComposeResult], Optional[dict[str, Any]]]:
    """Compose payment-media intro when test-mode gate passes; else legacy intro."""
    settings = dict(ai_settings or {})
    if not should_enforce_persona_compose_for_surface(
        tenant_id=tenant_id,
        customer_phone=customer_phone,
        surface=PERSONA_SURFACE_PAYMENT_MEDIA_INTRO,
        ai_settings=settings,
    ):
        return payment_barcode_intro_text(media_key), None, None

    bundle = build_payment_media_intro_facts_bundle(
        inbound_text=inbound_text,
        tenant_id=tenant_id,
        customer_phone=customer_phone,
        media_key=media_key,
        media_url_present=media_url_present,
        payment_method=payment_method,
        payment_status=payment_status,
        order_id=order_id,
        amount=amount,
        merchant_persona=settings,
    )
    composer = FactBoundPersonaComposer(enforce_gate=False)
    result = await composer.compose(bundle)
    from .flags import persona_composer_allowlist_result  # noqa: PLC0415

    event_meta = build_persona_compose_event_metadata(
        result,
        tenant_id=int(tenant_id),
        allowlist_result=persona_composer_allowlist_result(
            tenant_id=int(tenant_id),
            customer_phone=str(customer_phone or ""),
            ai_settings=settings,
        ),
    )
    return result.text.strip() or payment_barcode_intro_text(media_key), result, event_meta

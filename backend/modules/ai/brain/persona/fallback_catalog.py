"""Deterministic fallbacks for FactBoundPersonaComposer (safety net only)."""
from __future__ import annotations

from typing import Optional

from ..compose import templates as T
from ..types import BrainContext
from .facts_bundle import PersonaFactsBundle


def deterministic_fallback(
    bundle: PersonaFactsBundle,
    *,
    ctx: Optional[BrainContext] = None,
    reason: str = "",
) -> str:
    """Short safe reply when LLM compose or guards fail."""
    surface = str(bundle.surface or "").strip()
    inbound = str(bundle.inbound_text or "").strip()

    if surface == "thanks":
        return _thanks_fallback(inbound)
    if surface == "dua":
        return _dua_fallback(inbound)
    if surface == "social_checkin":
        return _checkin_fallback(ctx, inbound)
    if surface == "payment_media_intro":
        return _payment_media_intro_fallback(bundle)
    return _greeting_fallback(ctx, inbound)


def _greeting_fallback(ctx: Optional[BrainContext], inbound: str) -> str:
    mirrored = (T.social_mirror_fallback_reply(inbound) or "").strip()
    if mirrored:
        return mirrored
    if ctx is not None:
        from ..compose.persona_template_engine import pick_persona_greeting  # noqa: PLC0415

        return pick_persona_greeting(ctx, re_greet=False)
    return "ياهلا ومرحبا! 😊"


def _checkin_fallback(ctx: Optional[BrainContext], inbound: str) -> str:
    if ctx is not None:
        from ..compose.persona_template_engine import (  # noqa: PLC0415
            PERSONA_SOCIAL_WARM_BY_CATEGORY,
            pick_persona_variant,
        )

        pool = PERSONA_SOCIAL_WARM_BY_CATEGORY.get("wellbeing_check")
        if pool:
            return pick_persona_variant(pool, ctx)
    return "بخير الله يسعدك، وش تحتاج؟"


def _thanks_fallback(inbound: str) -> str:
    if "الله" in inbound:
        return "الله يعافيك 🤍"
    return "العفو، حياك الله 😊"


def _dua_fallback(inbound: str) -> str:
    if "الله" in inbound:
        return "الله يعافيك ويبارك فيك 🤍"
    return "أبشر، حياك الله 😊"


def _payment_media_intro_fallback(bundle: PersonaFactsBundle) -> str:
    facts = bundle.verified_facts or {}
    if not facts.get("media_url_present"):
        return "حالياً بيانات الدفع غير متوفرة، تواصل مع فريق المتجر 🌷"
    if not facts.get("allow_receipt_request"):
        return "تم استلام بيانات الدفع، وفريق المتجر يراجع الطلب 🌷"
    media_kind = str(facts.get("media_kind") or "barcode")
    if media_kind == "qr":
        return "تفضل رمز الدفع، وبعد التحويل أرسل الإيصال 🧾"
    return "تفضل باركود التحويل، وبعد التحويل أرسل الإيصال 🧾"

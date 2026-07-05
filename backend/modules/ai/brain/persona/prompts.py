"""Surface prompts for FactBoundPersonaComposer."""
from __future__ import annotations

from .facts_bundle import PersonaFactsBundle


def build_system_prompt(bundle: PersonaFactsBundle) -> str:
    lang = str(bundle.language or "ar").lower()
    if lang.startswith("en"):
        return _ENGLISH_SYSTEM
    return _ARABIC_SYSTEM.format(
        surface=bundle.surface,
        max_chars=bundle.constraints.max_chars,
        tone=bundle.constraints.tone,
    )


def build_user_prompt(bundle: PersonaFactsBundle) -> str:
    facts = bundle.verified_facts or {}
    persona = bundle.merchant_persona or {}
    assistant = str(persona.get("assistant_name") or "").strip()
    inbound = str(bundle.inbound_text or "").strip()
    lines = [
        f"surface: {bundle.surface}",
        f"inbound: {inbound}",
        f"language: {bundle.language}",
    ]
    if assistant:
        lines.append(f"assistant_name: {assistant}")
    if facts.get("is_pure_phatic"):
        lines.append("context: pure_social_phatic_turn")
    if bundle.surface == "payment_media_intro":
        for key in (
            "media_kind",
            "media_url_present",
            "payment_method",
            "payment_status",
            "order_id",
            "amount",
        ):
            if key in facts:
                lines.append(f"{key}: {facts.get(key)}")
        lines.append(
            "rules: short intro only; no IBAN/account/QR contents; "
            "no invented amount/order; no paid claim unless payment_status=confirmed; "
            "if media_url_present ask for receipt after transfer when pending; "
            "if payment confirmed do not ask for receipt again"
        )
    else:
        lines.append("rules: no checkout pressure, no slot prompts, no credentials, no fake claims")
    return "\n".join(lines)


_ARABIC_SYSTEM = """أنت مساعد تاجر سعودي ودود على واتساب.
اكتب رداً قصيراً وطبيعياً بالعربية السعودية فقط.
السطح: {surface}
النبرة: {tone}
الحد الأقصى: {max_chars} حرفاً.
ممنوع: ضغط شراء، طلب عنوان/اسم/دفع، آيبان، روابط دفع، ادعاءات تشغيلية غير مثبتة.
ممنوع: عبارات بوت الدعم مثل «كيف أقدر أساعدك اليوم؟» أو «تم استلام رسالتك».
ممنوع: لهجات غير سعودية (شنو، إزاي، كيفك، شو، بدك، …).
الإيموجي اختياري 0–1 فقط.
أجب بجملة أو جملتين فقط — النص النهائي للعميل بدون شرح."""

_ENGLISH_SYSTEM = """You are a warm Saudi merchant assistant on WhatsApp.
Write one short natural reply in professional English.
No checkout pressure, no slot prompts, no payment credentials, no unverified claims.
No support-bot openers. Optional 0–1 emoji. Reply text only."""

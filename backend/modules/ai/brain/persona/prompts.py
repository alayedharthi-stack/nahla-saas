"""Surface prompts for FactBoundPersonaComposer."""
from __future__ import annotations

from typing import Any

from .facts_bundle import PersonaFactsBundle


def _resolve_catalog_visible_price(product: dict[str, Any]) -> Any:
    from modules.ai.brain.postprocess.product_claim_grounding_evidence import (  # noqa: PLC0415
        parse_price_amount,
    )

    for key in ("price", "sale_price", "regular_price"):
        value = product.get(key)
        if value is None:
            continue
        if parse_price_amount(value) is not None:
            return value
    return None


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
    elif bundle.surface == "kb_product_answer":
        lines.append(f"question_kind: {facts.get('question_kind') or ''}")
        if facts.get("subject_title"):
            lines.append(f"subject_product: {facts.get('subject_title')}")
        for section in facts.get("kb_sections") or []:
            if not isinstance(section, dict):
                continue
            title = str(section.get("title") or "").strip()
            body = str(section.get("body") or "").strip()
            if title or body:
                lines.append(f"kb_section: {title} — {body}")
        if facts.get("allow_price_mention") and facts.get("catalog_price") is not None:
            lines.append(f"catalog_price: {facts.get('catalog_price')}")
        if facts.get("allow_availability_mention") and facts.get("availability") is not None:
            lines.append(f"availability: {facts.get('availability')}")
        lines.append(
            "rules: answer only from kb_sections and verified catalog facts; "
            "no invented benefits, price, availability, medical cure claims, "
            "checkout pressure, name/address/payment/quantity asks; "
            "no unsupported الأفضل/الأصلي/مضمون unless present in kb_sections"
        )
    elif bundle.surface == "catalog_product_answer":
        lines.append(f"question_kind: {facts.get('question_kind') or ''}")
        if facts.get("category_scope"):
            lines.append(f"category_scope: {facts.get('category_scope')}")
        if facts.get("catalog_search_query"):
            lines.append(f"catalog_search_query: {facts.get('catalog_search_query')}")
        lines.append(f"search_result_count: {facts.get('search_result_count')}")
        for product in facts.get("catalog_products") or []:
            if not isinstance(product, dict):
                continue
            title = str(product.get("title") or "").strip()
            if not title:
                continue
            parts = [f"product: {title}"]
            if product.get("category"):
                parts.append(f"category={product.get('category')}")
            if facts.get("allow_price_mention"):
                catalog_price = _resolve_catalog_visible_price(product)
                if catalog_price is not None:
                    parts.append(f"price={catalog_price} ريال")
            if facts.get("allow_availability_mention") and "available" in product:
                parts.append(f"available={product.get('available')}")
            lines.append(" | ".join(parts))
        lines.append(
            "rules: use only supplied catalog products; brief Saudi merchant tone; "
            "mention prices only when listed; mention availability only when available flag is set; "
            "no invented products/prices/availability/discounts; no الأفضل/superiority claims; "
            "no checkout/name/address/payment/quantity prompts; no category drift outside scope"
        )
    elif bundle.surface == "trusted_coupon_offer_answer":
        for key in (
            "question_kind",
            "coupon_availability",
            "promotion_availability",
            "verified_eligible_coupon_count",
            "verified_eligible_promotion_count",
            "coupon_record_count",
            "promotion_record_count",
            "allow_final_eligibility_claim",
        ):
            if key in facts:
                lines.append(f"{key}: {facts.get(key)}")
        reason_codes = facts.get("unavailability_reason_codes") or []
        if reason_codes:
            lines.append(f"unavailability_reason_codes: {', '.join(str(c) for c in reason_codes)}")
        lines.append(
            "rules: answer only the coupon/offer availability question from verified facts; "
            "never mention coupon codes; never claim a coupon was applied; "
            "do not claim final eligibility unless allow_final_eligibility_claim is true; "
            "no checkout pressure or order prompts; brief Saudi merchant tone"
        )
    elif bundle.surface == "general_offer_discovery_answer":
        for key in ("question_route", "forbidden_claims", "facts_snapshot_id"):
            if key in facts:
                lines.append(f"{key}: {facts.get(key)}")
        for bundle_key in ("product_sale_offer_facts", "trusted_coupon_offer_facts"):
            nested = facts.get(bundle_key)
            if isinstance(nested, dict) and nested:
                lines.append(f"{bundle_key}: present")
                if bundle_key == "product_sale_offer_facts":
                    if nested.get("sample_products"):
                        lines.append(f"sample_products: {nested.get('sample_products')}")
                    lines.append(
                        f"product_sale_availability: {nested.get('product_sale_availability')}"
                    )
                if bundle_key == "trusted_coupon_offer_facts":
                    lines.append(
                        f"promotion_availability: {nested.get('promotion_availability')}; "
                        f"coupon_availability: {nested.get('coupon_availability')}"
                    )
        lines.append(
            "rules: answer general offer discovery from namespaced verified facts only; "
            "catalog product sale facts and coupon/promotion facts are independent; "
            "never infer coupon eligibility from catalog sale or vice versa; "
            "no deterministic merging prose; brief Saudi merchant tone; no checkout pressure"
        )
    elif bundle.surface == "product_sale_offer_answer":
        for key in (
            "question_kind",
            "product_sale_availability",
            "verified_on_sale_product_count",
            "target_product",
            "allow_price_mention",
        ):
            if key in facts and facts.get(key) is not None:
                lines.append(f"{key}: {facts.get(key)}")
        lines.append(
            "rules: answer product-scoped sale question from catalog verified facts only; "
            "do not mention coupons or promotions; no checkout pressure; brief Saudi merchant tone"
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

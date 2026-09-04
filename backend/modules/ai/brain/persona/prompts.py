"""Surface prompts for FactBoundPersonaComposer."""
from __future__ import annotations

from typing import Any

from .facts_bundle import PERSONA_SURFACE_CUSTOMER_CONDITIONAL_COUPON_ANSWER, PersonaFactsBundle


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
    if bundle.surface == PERSONA_SURFACE_CUSTOMER_CONDITIONAL_COUPON_ANSWER:
        if lang.startswith("en"):
            return _CUSTOMER_CONDITIONAL_COUPON_ANSWER_ENGLISH_SYSTEM
        return _CUSTOMER_CONDITIONAL_COUPON_ANSWER_ARABIC_SYSTEM
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
        if facts.get("has_kb_sections"):
            for section in facts.get("kb_sections") or []:
                if not isinstance(section, dict):
                    continue
                title = str(section.get("title") or "").strip()
                body = str(section.get("body") or "").strip()
                if title or body:
                    lines.append(f"kb_section: {title} — {body}")
        else:
            lines.append("kb_status: no_confirmed_kb_sections")
            missing = facts.get("missing_facts") or []
            if missing:
                lines.append(f"missing_facts: {', '.join(str(m) for m in missing)}")
        if facts.get("allow_price_mention") and facts.get("catalog_price") is not None:
            lines.append(f"catalog_price: {facts.get('catalog_price')}")
        if facts.get("allow_availability_mention") and facts.get("availability") is not None:
            lines.append(f"availability: {facts.get('availability')}")
        if facts.get("has_kb_sections"):
            lines.append(
                "rules: answer only from kb_sections and verified catalog facts; "
                "no invented benefits, price, availability, medical cure claims, "
                "checkout pressure, name/address/payment/quantity asks; "
                "no unsupported الأفضل/الأصلي/مضمون unless present in kb_sections"
            )
        else:
            lines.append(
                "rules: honestly explain there are no confirmed KB details for this product; "
                "do not invent features, benefits, price, availability, or medical claims; "
                "no checkout pressure, name/address/payment/quantity asks"
            )
    elif bundle.surface == "catalog_product_answer":
        requested_facets = list(facts.get("requested_facets") or [])
        lines.append(f"question_kind: {facts.get('question_kind') or ''}")
        if requested_facets:
            lines.append(f"requested_facets: {', '.join(requested_facets)}")
        if facts.get("category_scope"):
            lines.append(f"category_scope: {facts.get('category_scope')}")
        if facts.get("catalog_search_query"):
            lines.append(f"catalog_search_query: {facts.get('catalog_search_query')}")
        lines.append(f"search_result_count: {facts.get('search_result_count')}")
        if str(facts.get("question_kind") or "").strip() == "search_miss":
            if facts.get("resolved_subject"):
                lines.append(f"resolved_subject: {facts.get('resolved_subject')}")
            lines.append("catalog_miss: true")
            lines.append(f"search_result_count: {facts.get('search_result_count', 0)}")
            lines.append("allow_availability_mention: false")
            lines.append("has_positive_availability: false")
            lines.append(
                "rules: zero search matches is not proof of stock status; "
                "explain no matching catalog result naturally without positive or "
                "negative availability claims or stock markers; "
                "do not invent products, prices, or availability; "
                "suggest trying the exact store product name or browsing top sellers; "
                "never re-open product type/SKU identification; brief Saudi merchant tone"
            )
            return "\n".join(lines)
        if facts.get("navigation_browse"):
            lines.append("navigation_browse: true")
            if facts.get("navigator_no_groups_fallback"):
                lines.append("navigator_no_groups_fallback: true")
        if facts.get("eligible_product_count") is not None:
            lines.append(
                f"eligible_product_count: {facts.get('eligible_product_count')}"
            )
        if not facts.get("has_eligible_products"):
            lines.append("no_confirmed_sellable_products: true")
        qkind = str(facts.get("question_kind") or "").strip()
        has_pos_avail = bool(facts.get("has_positive_availability"))
        allow_avail = bool(facts.get("allow_availability_mention"))
        ambiguous = bool(facts.get("catalog_ambiguity"))
        subject_scope = str(facts.get("subject_scope") or "").strip()
        category_existence = bool(facts.get("category_existence"))
        lines.append(f"allow_price_mention: {bool(facts.get('allow_price_mention'))}")
        lines.append(f"allow_availability_mention: {allow_avail}")
        lines.append(f"has_positive_availability: {has_pos_avail}")
        if subject_scope:
            lines.append(f"subject_scope: {subject_scope}")
        if "category_existence" in facts:
            lines.append(f"category_existence: {category_existence}")
        if facts.get("availability_evidence_kind"):
            lines.append(
                f"availability_evidence_kind: {facts.get('availability_evidence_kind')}"
            )
        if facts.get("allow_matching_set_existence_mention"):
            lines.append("allow_matching_set_existence_mention: true")
        if ambiguous:
            lines.append("catalog_ambiguity: true")
            lines.append(
                f"catalog_ambiguity_reason: {facts.get('catalog_ambiguity_reason') or ''}"
            )
            lines.append(
                f"allow_price_differentiator: {bool(facts.get('allow_price_differentiator'))}"
            )
            lines.append(
                f"require_clarification: {bool(facts.get('require_clarification'))}"
            )
            for candidate in facts.get("ambiguous_catalog_candidates") or []:
                if not isinstance(candidate, dict):
                    continue
                title = str(candidate.get("title") or "").strip()
                if not title:
                    continue
                parts = [f"ambiguous_candidate: {title}"]
                if candidate.get("variant_id") is not None:
                    parts.append(f"variant_id={candidate.get('variant_id')}")
                if candidate.get("price") is not None:
                    parts.append(f"price={candidate.get('price')} ريال")
                if "orderable" in candidate:
                    parts.append(f"orderable={candidate.get('orderable')}")
                if "available" in candidate:
                    parts.append(f"available={candidate.get('available')}")
                lines.append(" | ".join(parts))
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
            if allow_avail:
                if "orderable" in product:
                    parts.append(f"orderable={product.get('orderable')}")
                if "available" in product:
                    parts.append(f"available={product.get('available')}")
            lines.append(" | ".join(parts))
        if not facts.get("has_catalog_products"):
            lines.append("catalog_products: none")
        if facts.get("kb_retrieval_ran"):
            lines.append("kb_retrieval_ran: true")
            lines.append(f"has_kb_sections: {bool(facts.get('has_kb_sections'))}")
            lines.append(f"kb_fact_absent: {bool(facts.get('kb_fact_absent'))}")
        if facts.get("has_kb_sections"):
            for section in facts.get("kb_sections") or []:
                if not isinstance(section, dict):
                    continue
                title = str(section.get("title") or "").strip()
                body = str(section.get("body") or "").strip()
                if title or body:
                    lines.append(f"kb_section: {title} — {body}")
        if (
            not facts.get("has_eligible_products")
            and qkind not in {"price", "availability", "compound"}
            and not requested_facets
        ):
            lines.append(
                "rules: honestly explain there are no confirmed sellable catalog products; "
                "do not invent products, prices, availability, or checkout pressure; "
                "no name/address/payment/quantity prompts"
            )
        else:
            truth_rule = "rules: use only supplied catalog products"
            if facts.get("has_kb_sections"):
                truth_rule += " and supplied authorized kb_sections"
            rule_parts = [
                truth_rule,
                "brief Saudi merchant tone",
                "no invented products/prices/availability/discounts",
                "no الأفضل/superiority claims",
                "no checkout/name/address/payment/quantity/phone/contact prompts",
                "no category drift outside scope",
            ]
            if ambiguous or facts.get("require_clarification"):
                if (
                    subject_scope == "matching_set"
                    and category_existence
                    and allow_avail
                ):
                    rule_parts.append(
                        "matching-set existence is confirmed by category_existence/"
                        "eligible_product_count; you may express that matching products "
                        "exist or are orderable; still ask a concise natural clarification "
                        "to choose among ambiguous_candidate facts; do not invent a single "
                        "selected product identity; do not claim variant-level stock; "
                        "orderable/eligible is not proof that every size is in inventory; "
                        "do not ask for phone/mobile/contact details"
                    )
                else:
                    rule_parts.append(
                        "multiple exact-title catalog products are non-unique; ask a concise natural "
                        "clarification as a question using distinguishing ambiguous_candidate facts only; "
                        "allow_price_mention is false — do not present one final selected price; "
                        "when allow_price_differentiator is true, price concept may distinguish candidates; "
                        "numeric amounts only when grounded in ambiguous_candidate facts and framed as a "
                        "clarifying question (question mark); do not generalize availability across products; "
                        "do not ask for phone/mobile/contact details; "
                        "do not invent distinguishing details"
                    )
            elif qkind == "compound" or (
                "price" in requested_facets and "availability" in requested_facets
            ):
                rule_parts.append(
                    "answer both verified price and per-product availability from facts; "
                    "do not generalize availability across products"
                )
            elif qkind == "price" or "price" in requested_facets:
                rule_parts.append(
                    "answer from verified price facts only; "
                    "do not add availability or stock-status claims"
                )
            elif qkind == "availability" or "availability" in requested_facets:
                if subject_scope == "matching_set" and category_existence:
                    rule_parts.append(
                        "express matching-set existence from category_existence/"
                        "eligible orderable facts; do not invent variant stock"
                    )
                elif has_pos_avail:
                    rule_parts.append(
                        "mention positive availability only for products with "
                        "available=true or orderable=true in facts; "
                        "orderable is checkout eligibility, not variant inventory"
                    )
                else:
                    rule_parts.append(
                        "no confirmed positive stock evidence; express uncertainty without "
                        "claiming متوفر or غير متوفر/نفذ/out-of-stock"
                    )
            elif qkind == "browse" or facts.get("navigation_browse"):
                # Evidence-gated browse availability, EM review 2026-07-27.
                if allow_avail and has_pos_avail:
                    rule_parts.append(
                        "mention positive availability only for products with available=true in facts"
                    )
                else:
                    rule_parts.append("do not mention availability or stock status")
            else:
                rule_parts.append("mention prices only when listed in product facts")
            lines.append("; ".join(rule_parts))
    elif bundle.surface == PERSONA_SURFACE_CUSTOMER_CONDITIONAL_COUPON_ANSWER:
        for key in (
            "identity_status",
            "min_orders_condition_state",
            "conditional_coupon_evaluation_state",
            "order_history_completeness",
            "completed_orders_count",
            "min_orders_for_eligibility",
            "orders_shortfall",
            "allow_min_orders_condition_claim",
            "closed_reason_code",
            "facts_snapshot_id",
        ):
            if key in facts and facts.get(key) is not None:
                lines.append(f"{key}: {facts.get(key)}")
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

_CUSTOMER_CONDITIONAL_COUPON_ANSWER_ARABIC_SYSTEM = """أنت مساعد تاجر سعودي ودود على واتساب.
السطح: customer_conditional_coupon_answer
النبرة: warm_saudi_merchant
الحد الأقصى: 380 حرفاً.
أجب فقط عن سؤال كوبون الشرط بعد عدد الطلبات من الحقائق المؤكدة في رسالة المستخدم.
ممنوع: ذكر أكواد كوبون أو إصدار كوبون أو إرسال كود.
ممنوع: ادعاء أهلية نهائية إلا إذا كان allow_min_orders_condition_claim=true في الحقائق.
ممنوع: ضغط شراء، طلب عنوان/اسم/دفع، آيبان، روابط دفع.
ممنوع: عبارات بوت الدعم مثل «كيف أقدر أساعدك اليوم؟» أو «تم استلام رسالتك».
ممنوع: لهجات غير سعودية (شنو، إزاي، كيفك، شو، بدك، …).
الإيموجي اختياري 0–1 فقط.
أجب بجملة أو جملتين فقط — النص النهائي للعميل بدون شرح."""

_CUSTOMER_CONDITIONAL_COUPON_ANSWER_ENGLISH_SYSTEM = """You are a warm Saudi merchant assistant on WhatsApp.
Surface: customer_conditional_coupon_answer. Max 380 characters.
Answer only the min-orders conditional coupon question from verified facts in the user message.
Never mention coupon codes, issuance, or send a code.
Do not claim final eligibility unless allow_min_orders_condition_claim=true in facts.
No checkout pressure, slot prompts, payment credentials, or unverified claims.
No support-bot openers. Optional 0–1 emoji. Reply text only."""

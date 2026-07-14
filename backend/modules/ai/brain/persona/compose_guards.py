"""Post-compose guard chain for FactBoundPersonaComposer."""
from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass
from typing import Any, Optional

from .facts_bundle import (
    PersonaFactsBundle,
    PHASE2_SOCIAL_SURFACES,
    PERSONA_SURFACE_CATALOG_PRODUCT_ANSWER,
    PERSONA_SURFACE_KB_PRODUCT_ANSWER,
    PERSONA_SURFACE_PAYMENT_MEDIA_INTRO,
    PERSONA_SURFACE_TRUSTED_COUPON_OFFER_ANSWER,
)
from .fallback_catalog import deterministic_fallback


@dataclass(frozen=True)
class PersonaGuardResult:
    text: str
    passed: bool
    failed_reason: str = ""
    repaired: bool = False


def _count_emojis(text: str) -> int:
    from ..compose.persona_template_engine import PERSONA_ALLOWED_EMOJI  # noqa: PLC0415

    return sum(1 for ch in (text or "") if ch in PERSONA_ALLOWED_EMOJI)


def _strip_excess_emojis(text: str, *, max_emojis: int) -> tuple[str, bool]:
    from ..compose.persona_template_engine import PERSONA_ALLOWED_EMOJI  # noqa: PLC0415

    raw = str(text or "")
    if not raw.strip():
        return raw, False
    kept: list[str] = []
    emoji_seen = 0
    changed = False
    for ch in raw:
        if ch in PERSONA_ALLOWED_EMOJI:
            if emoji_seen < max_emojis:
                kept.append(ch)
                emoji_seen += 1
            else:
                changed = True
            continue
        kept.append(ch)
    return "".join(kept).strip(), changed


def _scrub_non_saudi_terms(text: str) -> tuple[str, bool]:
    from .policy_terms import NON_SAUDI_ARABIC_DIALECT_TERMS  # noqa: PLC0415

    raw = str(text or "")
    if not raw.strip():
        return raw, False
    changed = False
    cleaned = raw
    for term in NON_SAUDI_ARABIC_DIALECT_TERMS:
        pattern = re.compile(rf"(?<!\S){re.escape(term)}(?!\S)", re.UNICODE)
        if pattern.search(cleaned):
            cleaned = pattern.sub("", cleaned)
            changed = True
    cleaned = re.sub(r"\s{2,}", " ", cleaned).strip()
    return cleaned, changed


def _strip_known_customer_reasks(text: str, bundle: PersonaFactsBundle) -> tuple[str, bool]:
    from .policy_terms import (  # noqa: PLC0415
        KNOWN_CUSTOMER_BLUNT_ADDRESS_ASK_PHRASES,
        KNOWN_CUSTOMER_NAME_REASK_PHRASES,
        KNOWN_CUSTOMER_PHONE_REASK_PHRASES,
    )

    ctx = bundle.customer_context or {}
    raw = str(text or "")
    if not raw.strip():
        return raw, False
    phrases: list[str] = []
    if ctx.get("has_verified_name"):
        phrases.extend(KNOWN_CUSTOMER_NAME_REASK_PHRASES)
    if ctx.get("has_whatsapp_phone"):
        phrases.extend(KNOWN_CUSTOMER_PHONE_REASK_PHRASES)
    if ctx.get("has_saved_address"):
        phrases.extend(KNOWN_CUSTOMER_BLUNT_ADDRESS_ASK_PHRASES)
    if not phrases:
        return raw, False
    changed = False
    cleaned = raw
    for phrase in phrases:
        if phrase in cleaned:
            cleaned = cleaned.replace(phrase, "").strip(" ،،.")
            changed = True
    cleaned = re.sub(r"\s{2,}", " ", cleaned).strip()
    return cleaned, changed


def _truncate_safe(text: str, max_chars: int) -> str:
    raw = str(text or "").strip()
    if len(raw) <= max_chars:
        return raw
    cut = raw[:max_chars].rstrip()
    if cut and cut[-1] in "،.!?":
        return cut
    return cut.rstrip("،. ") + "…"


def _apply_kb_product_answer_guards(
    text: str,
    facts: dict[str, Any],
) -> PersonaGuardResult:
    working = str(text or "").strip()
    if not working:
        return PersonaGuardResult(text="", passed=False, failed_reason="empty_compose")

    if not facts.get("allow_slot_prompts", False):
        slot_markers = (
            "اسمك",
            "اسمك الكريم",
            "عنوانك",
            "وين تسكن",
            "رقم الحساب",
            "الآيبان",
            "ايبان",
            "كم الكمية",
            "كم الحبة",
            "طريقة الدفع",
        )
        if any(m in working for m in slot_markers):
            return PersonaGuardResult(
                text=working,
                passed=False,
                failed_reason="slot_prompt",
            )

    if not facts.get("allow_price_mention"):
        price_markers = ("ريال", "ر.س", "السعر", "بكم", "كم سعر")
        if any(m in working for m in price_markers):
            return PersonaGuardResult(
                text=working,
                passed=False,
                failed_reason="invented_price",
            )

    if not facts.get("allow_availability_mention"):
        availability_markers = (
            "متوفر",
            "غير متوفر",
            "نفذ",
            "available",
            "out of stock",
        )
        if any(m in working.lower() for m in availability_markers):
            return PersonaGuardResult(
                text=working,
                passed=False,
                failed_reason="invented_availability",
            )

    if not facts.get("allow_medical_claims"):
        medical_markers = (
            "يشفي",
            "يعالج",
            "علاج",
            "شفاء",
            "يقضي على",
            "يقتل الفيروس",
            "cure",
            "treat",
        )
        if any(m in working for m in medical_markers):
            return PersonaGuardResult(
                text=working,
                passed=False,
                failed_reason="medical_claim",
            )

    kb_text = str(facts.get("kb_text") or "")
    cure_markers = (
        "يشفي",
        "يعالج",
        "شفاء",
        "يقضي على",
        "يقتل الفيروس",
        "cure",
        "treat",
    )
    if any(m in working for m in cure_markers):
        if not any(m in kb_text for m in cure_markers):
            return PersonaGuardResult(
                text=working,
                passed=False,
                failed_reason="unsupported_cure_claim",
            )

    for term in ("الأفضل", "الأصلي", "مضمون"):
        if term in working and term not in kb_text:
            return PersonaGuardResult(
                text=working,
                passed=False,
                failed_reason="unsupported_superiority_claim",
            )

    return PersonaGuardResult(text=working, passed=True)


def _apply_trusted_coupon_offer_answer_guards(
    text: str,
    facts: dict[str, Any],
) -> PersonaGuardResult:
    working = str(text or "").strip()
    if not working:
        return PersonaGuardResult(text="", passed=False, failed_reason="empty_compose")

    if not facts.get("allow_code_mention"):
        code_markers = (
            "كود الخصم",
            "كود خصم",
            "الكود",
            "كوبون ",
            "coupon code",
            "discount code",
            "promo code",
        )
        if any(m.lower() in working.lower() for m in code_markers):
            return PersonaGuardResult(
                text=working,
                passed=False,
                failed_reason="coupon_code_disclosure",
            )

    applied_markers = (
        "تم تطبيق",
        "طبقنا",
        "فعلنا الكوبون",
        "applied the coupon",
        "coupon applied",
    )
    if any(m in working for m in applied_markers):
        return PersonaGuardResult(
            text=working,
            passed=False,
            failed_reason="coupon_applied_claim",
        )

    if not facts.get("allow_final_eligibility_claim"):
        final_markers = (
            "أنت مؤهل",
            "انت مؤهل",
            "مؤكد أهليتك",
            "مؤكد أهليتكم",
            "definitely eligible",
            "you are eligible",
        )
        if any(m in working for m in final_markers):
            return PersonaGuardResult(
                text=working,
                passed=False,
                failed_reason="final_eligibility_claim",
            )

    checkout_markers = (
        "نكمل الطلب",
        "اطلب الآن",
        "أرسل العنوان",
        "طريقة الدفع",
        "كم الكمية",
    )
    if any(m in working for m in checkout_markers):
        return PersonaGuardResult(
            text=working,
            passed=False,
            failed_reason="checkout_pressure",
        )

    return PersonaGuardResult(text=working, passed=True)


def _apply_catalog_product_answer_guards(
    text: str,
    facts: dict[str, Any],
) -> PersonaGuardResult:
    working = str(text or "").strip()
    if not working:
        return PersonaGuardResult(text="", passed=False, failed_reason="empty_compose")

    if not facts.get("allow_slot_prompts", False):
        slot_markers = (
            "اسمك",
            "اسمك الكريم",
            "عنوانك",
            "وين تسكن",
            "رقم الحساب",
            "الآيبان",
            "ايبان",
            "كم الكمية",
            "كم الحبة",
            "طريقة الدفع",
            "اطلبه",
            "اطلب الآن",
            "نكمل الطلب",
        )
        if any(m in working for m in slot_markers):
            return PersonaGuardResult(
                text=working,
                passed=False,
                failed_reason="slot_prompt",
            )

    order_markers = ("تم إنشاء طلبك", "رقم الطلب", "NHL-")
    if any(m in working for m in order_markers):
        return PersonaGuardResult(
            text=working,
            passed=False,
            failed_reason="order_confirmation_claim",
        )

    if not facts.get("allow_price_mention"):
        price_markers = ("ريال", "ر.س", "السعر", "بكم", "كم سعر")
        if any(m in working for m in price_markers):
            return PersonaGuardResult(
                text=working,
                passed=False,
                failed_reason="invented_price",
            )
    else:
        from modules.ai.brain.postprocess.product_claim_grounding_evidence import (  # noqa: PLC0415
            extract_reply_prices,
            parse_price_amount,
        )

        allowed_amounts = {
            amt
            for amt in (
                parse_price_amount(p.get("price"))
                for p in (facts.get("catalog_products") or [])
                if isinstance(p, dict)
            )
            if amt is not None
        }
        for claimed in extract_reply_prices(working):
            if allowed_amounts and claimed not in allowed_amounts:
                return PersonaGuardResult(
                    text=working,
                    passed=False,
                    failed_reason="invented_price_amount",
                )

    if not facts.get("allow_availability_mention"):
        availability_markers = (
            "متوفر",
            "غير متوفر",
            "نفذ",
            "available",
            "out of stock",
        )
        if any(m in working.lower() for m in availability_markers):
            return PersonaGuardResult(
                text=working,
                passed=False,
                failed_reason="invented_availability",
            )
    elif "متوفر" in working and not facts.get("has_positive_availability"):
        return PersonaGuardResult(
            text=working,
            passed=False,
            failed_reason="unsupported_available_claim",
        )

    if not facts.get("allow_superiority_claims", False):
        for term in ("الأفضل", "الأصلي", "مضمون", "أفضل عسل"):
            if term in working:
                return PersonaGuardResult(
                    text=working,
                    passed=False,
                    failed_reason="unsupported_superiority_claim",
                )

    discount_markers = ("خصم", "تخفيض", "عرض", "%")
    if any(m in working for m in discount_markers):
        return PersonaGuardResult(
            text=working,
            passed=False,
            failed_reason="invented_offer",
        )

    allowed_titles = [
        str(p.get("title") or "").strip()
        for p in (facts.get("catalog_products") or [])
        if isinstance(p, dict) and str(p.get("title") or "").strip()
    ]
    scope = str(facts.get("category_scope") or facts.get("allowed_category") or "")
    if scope == "عسل":
        cross_markers = ("كريم", "زيت", "سم النحل", "عكبر")
        inbound = str(facts.get("inbound_text") or "")
        for marker in cross_markers:
            if marker in working and marker not in inbound:
                return PersonaGuardResult(
                    text=working,
                    passed=False,
                    failed_reason="category_drift",
                )

    if working.strip() in {"منتج", "المنتج", "منتجات", "المنتجات"}:
        return PersonaGuardResult(
            text=working,
            passed=False,
            failed_reason="generic_product_label",
        )

    if allowed_titles and len(allowed_titles) == 1:
        title = allowed_titles[0]
        if title and title not in working and len(working) > 40:
            pass  # composer may paraphrase; titles not strictly required in short replies

    return PersonaGuardResult(text=working, passed=True)


def apply_persona_compose_guards(
    text: str,
    bundle: PersonaFactsBundle,
    *,
    db: Any = None,
    tenant_id: Optional[int] = None,
) -> PersonaGuardResult:
    """Run the fixed guard order from the rollout design doc."""
    working = str(text or "").strip()
    if not working:
        return PersonaGuardResult(text="", passed=False, failed_reason="empty_compose")

    repaired = False
    lang = str(bundle.language or "ar").lower()

    # 1–2 Language / non-Saudi dialect + malformed كا suffix repair
    if lang.startswith("ar"):
        from .policy_terms import (  # noqa: PLC0415
            find_malformed_saudi_ka_suffix_tokens,
            find_non_saudi_arabic_terms,
            repair_malformed_saudi_ka_suffix,
        )

        repaired_ka, did_ka = repair_malformed_saudi_ka_suffix(working)
        if did_ka and repaired_ka.strip():
            working = repaired_ka
            repaired = True
        elif find_malformed_saudi_ka_suffix_tokens(working):
            return PersonaGuardResult(
                text=working,
                passed=False,
                failed_reason="malformed_saudi_ka_suffix",
            )

        if find_non_saudi_arabic_terms(working):
            scrubbed, did = _scrub_non_saudi_terms(working)
            if did and scrubbed.strip():
                working = scrubbed
                repaired = True
            elif find_non_saudi_arabic_terms(working):
                return PersonaGuardResult(
                    text=working,
                    passed=False,
                    failed_reason="non_saudi_dialect",
                )

    # 3 Credential / payment — immediate fallback, no repair
    from .policy_terms import looks_like_invented_payment_credential  # noqa: PLC0415

    if looks_like_invented_payment_credential(working):
        return PersonaGuardResult(
            text=working,
            passed=False,
            failed_reason="payment_credential",
        )
    try:
        from ..postprocess.payment_credential_guard import (  # noqa: PLC0415
            apply_payment_credential_guard,
        )

        pcg = apply_payment_credential_guard(
            working,
            db=db,
            tenant_id=tenant_id or bundle.tenant_id,
            inbound_text=bundle.inbound_text,
        )
        if pcg.replaced:
            return PersonaGuardResult(
                text=working,
                passed=False,
                failed_reason="payment_credential_guard",
            )
        working = (pcg.reply or working).strip()
    except Exception:  # noqa: BLE001  # noqa: silent-ok — guard import must not break chain
        pass

    # 4 Fake operational claims on social / payment intro surfaces
    facts = bundle.verified_facts or {}
    if bundle.surface in PHASE2_SOCIAL_SURFACES:
        fake_markers = (
            "تم الشحن",
            "وصل الإيصال",
            "تم الدفع",
            "تم تأكيد الطلب",
        )
        if any(m in working for m in fake_markers):
            return PersonaGuardResult(
                text=working,
                passed=False,
                failed_reason="fake_operational_claim",
            )

    if bundle.surface == PERSONA_SURFACE_PAYMENT_MEDIA_INTRO:
        if not facts.get("allow_paid_claim"):
            paid_markers = (
                "تم الدفع",
                "تم تأكيد الدفع",
                "تم استلام الدفع",
                "تم اعتماد الدفع",
            )
            if any(m in working for m in paid_markers):
                return PersonaGuardResult(
                    text=working,
                    passed=False,
                    failed_reason="fake_paid_claim",
                )
        if not facts.get("media_url_present"):
            sent_markers = (
                "تفضل الباركود",
                "هذا الباركود",
                "هذا باركود",
                "تفضل رمز",
                "هذا رمز الدفع",
                "صورة الباركود",
                "تفضل صورة",
            )
            if any(m in working for m in sent_markers):
                return PersonaGuardResult(
                    text=working,
                    passed=False,
                    failed_reason="media_not_present_claim",
                )
        if not facts.get("allow_receipt_request"):
            receipt_markers = (
                "أرسل الإيصال",
                "أرسل صورة الإيصال",
                "بعد التحويل أرسل",
            )
            if any(m in working for m in receipt_markers):
                return PersonaGuardResult(
                    text=working,
                    passed=False,
                    failed_reason="receipt_ask_on_confirmed",
                )

    if bundle.surface == PERSONA_SURFACE_KB_PRODUCT_ANSWER:
        kb_guard = _apply_kb_product_answer_guards(working, facts)
        if not kb_guard.passed:
            return kb_guard
        working = kb_guard.text

    if bundle.surface == PERSONA_SURFACE_CATALOG_PRODUCT_ANSWER:
        catalog_guard = _apply_catalog_product_answer_guards(working, facts)
        if not catalog_guard.passed:
            return catalog_guard
        working = catalog_guard.text

    if bundle.surface == PERSONA_SURFACE_TRUSTED_COUPON_OFFER_ANSWER:
        coupon_guard = _apply_trusted_coupon_offer_answer_guards(working, facts)
        if not coupon_guard.passed:
            return coupon_guard
        working = coupon_guard.text

    # 5 Checkout-pressure guard
    if bundle.surface in PHASE2_SOCIAL_SURFACES or bundle.surface in {
        PERSONA_SURFACE_KB_PRODUCT_ANSWER,
        PERSONA_SURFACE_CATALOG_PRODUCT_ANSWER,
        PERSONA_SURFACE_TRUSTED_COUPON_OFFER_ANSWER,
    }:
        try:
            from ..postprocess.social_checkout_pressure_guard import (  # noqa: PLC0415
                apply_social_checkout_pressure_guard,
            )

            scpg = apply_social_checkout_pressure_guard(
                working,
                inbound_text=bundle.inbound_text,
                tenant_id=tenant_id or bundle.tenant_id,
            )
            working = (scpg.reply or "").strip()
            if scpg.stripped and not working:
                return PersonaGuardResult(
                    text=working,
                    passed=False,
                    failed_reason="checkout_pressure_empty",
                )
        except Exception:  # noqa: BLE001  # noqa: silent-ok
            pass

    # 6 Known customer re-ask
    working, did_reask = _strip_known_customer_reasks(working, bundle)
    if did_reask:
        repaired = True
    if not working.strip():
        return PersonaGuardResult(
            text="",
            passed=False,
            failed_reason="known_customer_reask_strip",
        )

    # 7 Emoji density
    max_emoji = int(bundle.constraints.max_emojis or 1)
    working, emoji_stripped = _strip_excess_emojis(working, max_emojis=max_emoji)
    if emoji_stripped:
        repaired = True
    from .policy_terms import rejects_fixed_emoji_template_opener  # noqa: PLC0415

    if rejects_fixed_emoji_template_opener(working):
        return PersonaGuardResult(
            text=working,
            passed=False,
            failed_reason="emoji_opener_spam",
        )

    # 8 Length
    if len(working) > bundle.constraints.max_chars:
        working = _truncate_safe(working, bundle.constraints.max_chars)
        repaired = True

    # 9 No silence
    if not working.strip():
        return PersonaGuardResult(text="", passed=False, failed_reason="empty_after_guards")

    from .policy_terms import rejects_social_support_bot_phrase  # noqa: PLC0415

    if bundle.surface in PHASE2_SOCIAL_SURFACES and rejects_social_support_bot_phrase(working):
        return PersonaGuardResult(
            text=working,
            passed=False,
            failed_reason="banned_support_bot_opener",
        )

    return PersonaGuardResult(
        text=working,
        passed=True,
        repaired=repaired,
    )


def apply_guards_or_fallback(
    text: str,
    bundle: PersonaFactsBundle,
    *,
    ctx: Any = None,
    db: Any = None,
    tenant_id: Optional[int] = None,
) -> tuple[str, PersonaGuardResult]:
    """One repair attempt on dialect scrub failures, then deterministic fallback."""
    guard = apply_persona_compose_guards(
        text,
        bundle,
        db=db,
        tenant_id=tenant_id,
    )
    if guard.passed:
        return guard.text, guard

    if guard.failed_reason == "non_saudi_dialect":
        scrubbed, _ = _scrub_non_saudi_terms(text)
        if scrubbed.strip():
            retry = apply_persona_compose_guards(
                scrubbed,
                bundle,
                db=db,
                tenant_id=tenant_id,
            )
            if retry.passed:
                return retry.text, retry

    fb = deterministic_fallback(bundle, ctx=ctx, reason=guard.failed_reason)
    fb_guard = apply_persona_compose_guards(
        fb,
        bundle,
        db=db,
        tenant_id=tenant_id,
    )
    if fb_guard.passed and fb_guard.text.strip():
        return fb_guard.text, PersonaGuardResult(
            text=fb_guard.text,
            passed=False,
            failed_reason=guard.failed_reason,
        )
    emergency = unicodedata.normalize("NFKC", (fb or "حياك الله 😊").strip())
    return emergency, PersonaGuardResult(
        text=emergency,
        passed=False,
        failed_reason=guard.failed_reason or "fallback_failed",
    )

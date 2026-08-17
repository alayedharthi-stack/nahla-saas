"""
Tests for inbound fragment guard and catalog fallback containment.
"""
from __future__ import annotations

from types import SimpleNamespace

import pytest

from modules.ai.brain.commerce.catalog_product_grounding import (
    build_uncertain_catalog_reply,
)
from modules.ai.brain.commerce.inbound_fragment_guard import (
    build_discount_coupon_support_reply,
    evaluate_duplicate_fragment_turn,
    is_discount_coupon_inquiry,
    reset_fragment_cache_for_tests,
    should_block_catalog_grounding_fallback,
)
from modules.ai.brain.postprocess.catalog_product_grounding_guard import (
    apply_catalog_product_grounding_guard,
)
from modules.ai.brain.postprocess.product_claim_grounding_evidence import (
    ProductClaimGroundingEvidence,
)


@pytest.fixture(autouse=True)
def _clear_fragment_cache():
    reset_fragment_cache_for_tests()
    yield
    reset_fragment_cache_for_tests()


def _claim_evidence(*titles: str) -> ProductClaimGroundingEvidence:
    products = tuple({"title": t} for t in titles)
    return ProductClaimGroundingEvidence(
        grounded_prices=frozenset(),
        grounded_text_corpus="",
        available_products=products,
        unavailable_products=(),
        catalog_products_this_turn=bool(products),
        catalog_miss_this_turn=False,
        recent_catalog_miss=False,
        recent_no_synced=False,
        has_checkout_catalog=True,
        executor_product_ids=frozenset(),
        kb_section_ids=frozenset(),
    )


def test_kod_khasem_badeel_not_catalog_fallback() -> None:
    inbound = "كود خصم بديل"
    _, reason = should_block_catalog_grounding_fallback(inbound_text=inbound)
    assert reason != "discount_coupon_inquiry"

    long_coupon = "عندكم كود خصم للطلب الحالي؟"
    blocked_long, reason_long = should_block_catalog_grounding_fallback(
        inbound_text=long_coupon,
    )
    assert blocked_long is False
    assert reason_long != "discount_coupon_inquiry"

    facts = SimpleNamespace(
        has_coupons=True,
        shareable_promotions=[{"code": "SAVE6", "source_type": "manual"}],
    )
    blocked_facts, reason_facts = should_block_catalog_grounding_fallback(
        inbound_text=inbound,
        decision_topic="promotion_inquiry",
        facts=facts,
    )
    assert blocked_facts is True
    assert reason_facts == "promotion_facts_present"

    llm_reply = "عسل السدر متوفر بخصم 10%"
    result = apply_catalog_product_grounding_guard(
        reply=llm_reply,
        inbound_text=inbound,
        evidence=_claim_evidence("عسل طلح"),
        executor_products=[{"title": "عسل طلح"}],
        inbound_metadata={"decision_topic": "promotion_inquiry"},
        facts=facts,
    )
    assert "الخيارات المؤكدة" not in result.reply
    assert result.action == "blocked_catalog_containment"


def test_repeated_short_social_fragment_only_one_clarify_turn() -> None:
    tenant_id = 1
    phone = "+966500000001"
    msg = "ههه"

    first = evaluate_duplicate_fragment_turn(
        tenant_id=tenant_id, customer_phone=phone, text=msg,
    )
    second = evaluate_duplicate_fragment_turn(
        tenant_id=tenant_id, customer_phone=phone, text=msg,
    )
    third = evaluate_duplicate_fragment_turn(
        tenant_id=tenant_id, customer_phone=phone, text=msg,
    )

    assert first.process_turn is True
    assert second.process_turn is False
    assert second.send_clarification_once is True
    assert third.process_turn is False
    assert third.send_clarification_once is False


def test_repeated_coupon_request_still_processes_brain() -> None:
    tenant_id = 1
    phone = "+966500000001"
    msg = "ابي كوبون خصم"

    first = evaluate_duplicate_fragment_turn(
        tenant_id=tenant_id, customer_phone=phone, text=msg,
    )
    second = evaluate_duplicate_fragment_turn(
        tenant_id=tenant_id, customer_phone=phone, text=msg,
    )

    assert first.process_turn is True
    assert second.process_turn is True


def test_bishrak_fragment_not_catalog() -> None:
    inbound = "بشرك"
    blocked, reason = should_block_catalog_grounding_fallback(inbound_text=inbound)
    assert blocked is True
    assert reason == "prayer_social_fragment"

    result = apply_catalog_product_grounding_guard(
        reply="عسل القطف متوفر",
        inbound_text=inbound,
        evidence=_claim_evidence("عسل طلح"),
    )
    assert "الخيارات المؤكدة" not in result.reply
    assert result.action in {
        "blocked_catalog_containment",
        "allowed_social_noncommerce",
    }


def test_unsupported_media_not_catalog() -> None:
    inbound = "[رسالة وسائط: sticker]"
    blocked, reason = should_block_catalog_grounding_fallback(
        inbound_text=inbound,
        inbound_metadata={"normalized_type": "sticker"},
    )
    assert blocked is True
    assert reason == "unsupported_media"


def test_explicit_catalog_browse_still_allowed() -> None:
    inbound = "أرسل الكتالوج"
    blocked, _ = should_block_catalog_grounding_fallback(inbound_text=inbound)
    assert blocked is False

    catalog_reply = build_uncertain_catalog_reply(catalog_titles=["عسل سدر"])
    assert "الخيارات المؤكدة" in catalog_reply


def test_real_product_browse_still_allowed() -> None:
    inbound = "وش عندكم من العسل؟"
    blocked, _ = should_block_catalog_grounding_fallback(inbound_text=inbound)
    assert blocked is False

    grounded_reply = "المتوفر عندنا عسل طلح وعسل سدر."
    result = apply_catalog_product_grounding_guard(
        reply=grounded_reply,
        inbound_text=inbound,
        evidence=_claim_evidence("عسل طلح", "عسل سدر"),
        executor_products=[
            {"title": "عسل طلح"},
            {"title": "عسل سدر"},
        ],
    )
    assert result.action == "allowed"
    assert "الخيارات المؤكدة" not in result.reply


def test_discount_coupon_support_reply_is_not_customer_facing_owner() -> None:
    assert is_discount_coupon_inquiry("عندكم كود خصم؟")
    reply = build_discount_coupon_support_reply()
    assert "الكتالوج" not in reply


def test_abshrak_full_social_not_prayer_fragment() -> None:
    inbound = "ابشرك والله بخير"
    blocked, reason = should_block_catalog_grounding_fallback(inbound_text=inbound)
    assert blocked is False
    assert reason == ""


def test_protected_final_reply_blocks_catalog_rewrite() -> None:
    inbound = "كود خصم بديل"
    result = apply_catalog_product_grounding_guard(
        reply="عسل السدر متوفر",
        inbound_text=inbound,
        evidence=_claim_evidence("عسل طلح"),
        inbound_metadata={
            "turn_owner_contract": {
                "protected_final_reply": True,
                "topic": "payment_receipt_received",
            },
        },
    )
    assert "الخيارات المؤكدة" not in result.reply
    assert result.action in {
        "allowed_catalog_push_blocked",
        "blocked_catalog_containment",
    }


def test_repeated_coupon_clarify_does_not_ask_customer_for_merchant_code() -> None:
    from modules.ai.brain.commerce.inbound_fragment_guard import (
        duplicate_fragment_clarification_reply,
    )

    reply = duplicate_fragment_clarification_reply(inbound_text="كود خصم بديل")
    assert "الكتالوج" not in reply
    assert "أرسل لي كود الخصم" not in reply

"""
tests/test_catalog_miss_hijack_operational_context.py
──────────────────────────────────────────────────────
Platform-wide gates — weak catalog queries must not hijack natural dialogue.
"""
from __future__ import annotations

import asyncio
import os
import sys
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

_here = os.path.dirname(os.path.abspath(__file__))
_backend = os.path.join(_here, "..")
if _backend not in sys.path:
    sys.path.insert(0, _backend)

from modules.ai.brain.commerce.catalog_search_evidence import (
    apply_catalog_search_evidence_gate,
    has_catalog_search_evidence,
    should_use_search_miss_template,
)
from modules.ai.brain.commerce.product_visual import (
    customer_authored_caption,
    extract_visual_product_query,
    is_product_visual_request,
)
from modules.ai.brain.compose.responder import DefaultComposer
from modules.ai.brain.decision.actions import ACTION_LLM_REPLY, ACTION_SEARCH_PRODUCTS
from modules.ai.brain.types import (
    ActionResult,
    BrainContext,
    CommerceFacts,
    Decision,
    Intent,
    MerchantConversationState,
)


def _ctx(
    message: str,
    *,
    intent_name: str = "general",
    confidence: float = 0.9,
    history: list | None = None,
    focus: dict | None = None,
) -> BrainContext:
    intent = Intent(
        name=intent_name,
        confidence=confidence,
        raw_message=message,
        extraction_method="rules",
    )
    state = MerchantConversationState(greeted=True, stage="exploring")
    if focus:
        state.current_product_focus = focus
    if history:
        state.recent_messages = history
        state.last_action = "llm_reply"
    return BrainContext(
        tenant_id=42,
        customer_phone="966500000099",
        message=message,
        intent=intent,
        state=state,
        history=history or [],
        facts=CommerceFacts(has_products=True, orderable=True, product_count=8),
    )


# ── 1. Media + caption — no cross-line query extraction ─────────────────────

def test_customer_caption_stops_before_vision_framing() -> None:
    msg = (
        "السلام عليكم الرجيع تمام\n\n"
        "[وصف الصورة] صورة عامة تظهر مدخل منزل."
    )
    assert customer_authored_caption(msg) == "السلام عليكم الرجيع تمام"


def test_extract_visual_query_not_from_caption_plus_vision_line() -> None:
    msg = (
        "السلام عليكم الرجيع تمام\n\n"
        "[وصف الصورة] صورة عامة تظهر مدخل منزل."
    )
    assert extract_visual_product_query(msg) == ""


def test_media_caption_gate_blocks_search_products_for_discourse_token() -> None:
    msg = (
        "السلام عليكم الرجيع تمام\n\n"
        "[وصف الصورة] صورة عامة تظهر مدخل منزل."
    )
    ctx = _ctx(msg, intent_name="product_visual_request")
    ctx.profile = {"inbound_metadata": {"source_type": "image"}}
    decision = Decision(
        action=ACTION_SEARCH_PRODUCTS,
        args={"query": "تمام", "after_search": "product_visual"},
        reason="customer wants image of 'تمام'",
        confidence=0.90,
    )
    out = apply_catalog_search_evidence_gate(ctx, decision)
    assert out.action == ACTION_LLM_REPLY
    assert out.action != ACTION_SEARCH_PRODUCTS


def test_search_miss_template_skipped_for_weak_discourse_subject() -> None:
    ctx = _ctx("الرجيع تمام")
    assert should_use_search_miss_template(ctx, "تمام", "تمام") is False


# ── 2. Explicit visual request still allowed ────────────────────────────────

def test_explicit_visual_talh_has_catalog_evidence() -> None:
    msg = "ابي صورة الطلح"
    ctx = _ctx(msg, intent_name="product_visual_request")
    decision = Decision(
        action=ACTION_SEARCH_PRODUCTS,
        args={"query": "طلح", "after_search": "product_visual"},
        reason="customer wants image of 'طلح'",
        confidence=0.90,
    )
    assert extract_visual_product_query(msg) == "طلح"
    assert is_product_visual_request(msg) is True
    assert has_catalog_search_evidence(ctx, "طلح", decision) is True
    out = apply_catalog_search_evidence_gate(ctx, decision)
    assert out.action == ACTION_SEARCH_PRODUCTS


# ── 3. Short price phrase in fulfillment thread ─────────────────────────────

def test_bkam_riyadh_blocked_without_product_evidence() -> None:
    history = [
        {"role": "user", "body": "ابي عسل علاجي وابي فوق يكون رجفت العسل"},
        {"role": "assistant", "body": "تقصد عسل للاستخدام الصحي؟"},
    ]
    ctx = _ctx("بكم الرياض", intent_name="ask_price", history=history)
    decision = Decision(
        action=ACTION_SEARCH_PRODUCTS,
        args={"query": "رياض"},
        reason="customer ask_price — search catalog",
        confidence=0.90,
    )
    assert has_catalog_search_evidence(ctx, "رياض", decision) is False
    out = apply_catalog_search_evidence_gate(ctx, decision)
    assert out.action == ACTION_LLM_REPLY
    assert out.args.get("topic") == "shipping_price_ambiguous"


def test_search_miss_template_skipped_for_riyadh_subject() -> None:
    history = [
        {"role": "user", "body": "ابي عسل علاجي"},
    ]
    ctx = _ctx("بكم الرياض", intent_name="ask_price", history=history)
    assert should_use_search_miss_template(ctx, "رياض", "رياض") is False


# ── 4. Real product price still works ─────────────────────────────────────

def test_bkam_talh_still_has_catalog_evidence() -> None:
    msg = "بكم الطلح"
    ctx = _ctx(msg, intent_name="ask_price")
    decision = Decision(
        action=ACTION_SEARCH_PRODUCTS,
        args={"query": "طلح"},
        reason="customer ask_price — search catalog",
        confidence=0.90,
    )
    assert has_catalog_search_evidence(ctx, "طلح", decision) is True
    out = apply_catalog_search_evidence_gate(ctx, decision)
    assert out.action == ACTION_SEARCH_PRODUCTS


def test_bkam_asal_sidr_still_searches_product() -> None:
    msg = "بكم عسل السدر"
    ctx = _ctx(msg, intent_name="ask_price")
    decision = Decision(
        action=ACTION_SEARCH_PRODUCTS,
        args={"query": "عسل السدر"},
        reason="customer ask_price — search catalog",
        confidence=0.90,
    )
    assert has_catalog_search_evidence(ctx, "عسل السدر", decision) is True
    out = apply_catalog_search_evidence_gate(ctx, decision)
    assert out.action == ACTION_SEARCH_PRODUCTS


def test_bkam_talh_not_hijacked_by_fulfillment_thread() -> None:
    """Product domain signal wins before shipping_price_ambiguous redirect."""
    history = [
        {"role": "user", "body": "ابي عسل علاجي وابي فوق يكون رجفت العسل"},
        {"role": "assistant", "body": "تقصد عسل للاستخدام الصحي؟"},
    ]
    ctx = _ctx("بكم الطلح", intent_name="ask_price", history=history)
    decision = Decision(
        action=ACTION_SEARCH_PRODUCTS,
        args={"query": "طلح"},
        reason="customer ask_price — search catalog",
        confidence=0.90,
    )
    out = apply_catalog_search_evidence_gate(ctx, decision)
    assert out.action == ACTION_SEARCH_PRODUCTS
    assert out.args.get("topic") is None


def test_bkam_riyadh_without_thread_uses_commerce_ambiguous() -> None:
    ctx = _ctx("بكم الرياض", intent_name="ask_price")
    decision = Decision(
        action=ACTION_SEARCH_PRODUCTS,
        args={"query": "رياض"},
        reason="customer ask_price — search catalog",
        confidence=0.90,
    )
    out = apply_catalog_search_evidence_gate(ctx, decision)
    assert out.action == ACTION_LLM_REPLY
    assert out.args.get("topic") == "commerce_ambiguous"


# ── 4b. Generic ask_price product queries (platform-wide) ───────────────────

_FULFILLMENT_HISTORY = [
    {"role": "user", "body": "ابي عسل علاجي وابي فوق يكون رجفت العسل"},
    {"role": "assistant", "body": "تقصد عسل للاستخدام الصحي؟"},
]


def _ask_price_search_decision(query: str) -> Decision:
    return Decision(
        action=ACTION_SEARCH_PRODUCTS,
        args={"query": query},
        reason="customer ask_price — search catalog",
        confidence=0.90,
    )


def test_generic_jacket_price_allowed_for_catalog_search() -> None:
    ctx = _ctx("كم سعر جاكيت؟", intent_name="ask_price", history=_FULFILLMENT_HISTORY)
    decision = _ask_price_search_decision("جاكيت")
    assert has_catalog_search_evidence(ctx, "جاكيت", decision) is True
    out = apply_catalog_search_evidence_gate(ctx, decision)
    assert out.action == ACTION_SEARCH_PRODUCTS
    assert out.args.get("topic") is None


def test_talh_price_regression_still_searches_catalog() -> None:
    ctx = _ctx("كم سعر الطلح؟", intent_name="ask_price")
    decision = _ask_price_search_decision("طلح")
    assert has_catalog_search_evidence(ctx, "طلح", decision) is True
    out = apply_catalog_search_evidence_gate(ctx, decision)
    assert out.action == ACTION_SEARCH_PRODUCTS
    assert out.args.get("topic") is None


def test_riyadh_in_fulfillment_thread_stays_shipping_price_ambiguous() -> None:
    ctx = _ctx("بكم الرياض", intent_name="ask_price", history=_FULFILLMENT_HISTORY)
    decision = _ask_price_search_decision("رياض")
    assert has_catalog_search_evidence(ctx, "رياض", decision) is False
    out = apply_catalog_search_evidence_gate(ctx, decision)
    assert out.action == ACTION_LLM_REPLY
    assert out.args.get("topic") == "shipping_price_ambiguous"


def test_shipping_price_question_stays_delivery_ambiguity() -> None:
    ctx = _ctx("كم سعر الشحن؟", intent_name="ask_price")
    decision = _ask_price_search_decision("الشحن")
    assert has_catalog_search_evidence(ctx, "الشحن", decision) is False
    out = apply_catalog_search_evidence_gate(ctx, decision)
    assert out.action == ACTION_LLM_REPLY
    assert out.args.get("topic") == "shipping_price_ambiguous"


def test_generic_blouse_price_allowed_for_catalog_search() -> None:
    ctx = _ctx("كم سعر البلوزة؟", intent_name="ask_price", history=_FULFILLMENT_HISTORY)
    decision = _ask_price_search_decision("بلوزة")
    assert has_catalog_search_evidence(ctx, "بلوزة", decision) is True
    out = apply_catalog_search_evidence_gate(ctx, decision)
    assert out.action == ACTION_SEARCH_PRODUCTS
    assert out.args.get("topic") is None


# ── 5. Genuine catalog-like miss may still use template ─────────────────────

def test_genuine_product_miss_may_use_search_miss_template() -> None:
    ctx = _ctx("بكم سدر الحجاز", intent_name="ask_price")
    assert should_use_search_miss_template(ctx, "سدر الحجاز", "سدر الحجاز") is True


def test_responder_search_miss_uses_deterministic_template_for_weak_subject() -> None:
    ctx = _ctx("بكم الرياض", intent_name="ask_price")
    composer = DefaultComposer()
    decision = Decision(
        action=ACTION_SEARCH_PRODUCTS,
        args={"query": "رياض"},
        reason="test",
    )
    result = ActionResult(
        success=False,
        error="no_search_hits",
        data={"message": "no_search_hits_no_top_fallback"},
    )

    with patch.object(composer, "_llm_compose", new_callable=AsyncMock) as mock_llm:
        text = asyncio.run(composer.compose(decision, result, ctx))
    mock_llm.assert_not_awaited()
    assert str(text or "").strip()
    assert "سدر الحجاز" not in text
    assert "طلح" not in text
    no_confirmed_match = (
        "تطابق" in text
        or "ما ظهر" in text
        or "ما لقيت" in text
        or "ما عندي" in text
        or "؟" in text
    )
    assert no_confirmed_match, "weak-subject miss must not invent a catalog match"


def test_responder_search_miss_uses_persona_compose_for_catalog_like_subject() -> None:
    ctx = _ctx("بكم سدر الحجاز", intent_name="ask_price")
    composer = DefaultComposer()
    decision = Decision(
        action=ACTION_SEARCH_PRODUCTS,
        args={"query": "سدر الحجاز"},
        reason="test",
    )
    result = ActionResult(
        success=False,
        error="no_search_hits",
        data={"message": "no_search_hits_no_top_fallback"},
    )

    async def _run() -> str:
        from unittest.mock import AsyncMock, patch  # noqa: PLC0415

        with patch(
            "modules.ai.brain.persona.catalog_product_answer.try_compose_catalog_search_miss_answer",
            new=AsyncMock(
                return_value=(
                    "ما لقيت تطابقاً واضحاً لسدر الحجاز في الكتالوج حالياً.",
                    None,
                    {
                        "chosen_path": "catalog_miss_resolved_subject",
                        "persona_compose": {"source": "persona_llm"},
                        "compose_source": "persona_llm",
                        "response_mode": "grounded_persona_compose",
                        "llm_candidate_present": True,
                        "final_text_transformed": False,
                        "final_transform_reasons": [],
                    },
                ),
            ),
        ):
            return await composer.compose(decision, result, ctx)

    text = asyncio.run(_run())
    assert "الكتالوج" in text
    assert result.data.get("chosen_path") == "catalog_miss_resolved_subject"
    assert result.data.get("persona_compose", {}).get("source") == "persona_llm"

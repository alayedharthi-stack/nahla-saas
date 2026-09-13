"""Regression coverage for evidence-free Commerce V2 social replies."""
from __future__ import annotations

import pytest

from modules.ai.commerce_agent_v2.context import CommerceAgentContext
from modules.ai.commerce_agent_v2.guardrails import validate_grounded_reply
from modules.ai.commerce_agent_v2.output import CommerceReply


def _context(user_input: str) -> CommerceAgentContext:
    context = CommerceAgentContext(
        tenant_id=101,
        conversation_id=202,
        customer_id=303,
        normalized_customer_phone="+966500000000",
        connection_id="404",
        inbound_trace_id="social-grounding-regression",
    )
    context.bind_run_user_input(user_input)
    return context


@pytest.mark.parametrize(
    ("user_input", "reply_text"),
    [
        ("السلام عليكم", "وعليكم السلام ورحمة الله وبركاته."),
        ("كيف حالكم", "بخير ولله الحمد، حياك الله."),
        ("مرحبا", "يا مرحبا، حياك الله."),
        ("صباح الخير", "صباح النور والسرور."),
    ],
)
def test_pure_social_reply_passes_without_tool_evidence(
    user_input: str,
    reply_text: str,
) -> None:
    reply = CommerceReply(text=reply_text, response_mode="social")

    assert validate_grounded_reply(_context(user_input), reply) == []


@pytest.mark.parametrize(
    ("user_input", "unsupported_reply"),
    [
        ("السلام عليكم، عندكم عسل طلح؟", "وعليكم السلام، لدينا عسل طلح."),
        ("كيف حالكم، كم سعر عسل الطلح؟", "بخير، سعر المنتج 100 ريال."),
        ("مرحبا، هل هذا المنتج متوفر؟", "يا مرحبا، المنتج متوفر."),
        ("صباح الخير، وين طلبي؟", "صباح النور، طلبك تم شحنه."),
        ("السلام عليكم، وش شركة الشحن؟", "وعليكم السلام، شركة الشحن سمسا."),
        (
            "كيف حالكم، أرسل رابط التتبع",
            "بخير، رابط التتبع https://example.test/track/123",
        ),
    ],
)
def test_greeting_prefix_does_not_bypass_commercial_grounding(
    user_input: str,
    unsupported_reply: str,
) -> None:
    errors = validate_grounded_reply(
        _context(user_input),
        CommerceReply(text=unsupported_reply, response_mode="social"),
    )

    assert errors
    assert any(
        error
        in {
            "evidence_free_commercial_or_factual_claim",
            "price_in_text_without_verified_claim",
            "availability_in_text_without_verified_claim",
            "url_in_text_without_verified_claim",
        }
        for error in errors
    )


@pytest.mark.parametrize(
    "unsupported_reply",
    [
        "نحن في جدة.",
        "سعر المنتج 100 ريال.",
        "المنتج متوفر.",
        "طلبك تم شحنه.",
        "شركة الشحن سمسا.",
    ],
)
def test_social_mode_cannot_escape_unsupported_factual_grounding(
    unsupported_reply: str,
) -> None:
    errors = validate_grounded_reply(
        _context("مرحبا"),
        CommerceReply(text=unsupported_reply, response_mode="social"),
    )

    assert errors


def test_social_mode_rejects_commercial_structure_and_safe_fallback_reason() -> None:
    reply = CommerceReply(
        text="حياك الله.",
        response_mode="social",
        evidence_refs=["catalog:product:1"],
        safe_fallback_reason="not_social",
    )

    errors = validate_grounded_reply(_context("مرحبا"), reply)

    assert "social_reply_with_commercial_structure" in errors
    assert "social_reply_with_safe_fallback_reason" in errors


def test_grounded_mode_without_evidence_still_fails_closed() -> None:
    errors = validate_grounded_reply(
        _context("مرحبا"),
        CommerceReply(text="حياك الله."),
    )

    assert "reply_without_tool_evidence_or_safe_fallback" in errors


def test_evidence_free_safe_fallback_cannot_claim_product_unavailability() -> None:
    errors = validate_grounded_reply(
        _context("هل المنتج متوفر؟"),
        CommerceReply(
            text="المنتج غير متوفر.",
            safe_fallback_reason="catalog_lookup_failed",
        ),
    )

    assert "availability_in_text_without_verified_claim" in errors
    assert "evidence_free_commercial_or_factual_claim" in errors

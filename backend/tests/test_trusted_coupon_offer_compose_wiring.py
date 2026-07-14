"""Wiring tests for trusted coupon/offer compose consumption."""
from __future__ import annotations

import asyncio
import inspect
import json
import os
import sys
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import pytest

_HERE = os.path.dirname(os.path.abspath(__file__))
_BACKEND = os.path.abspath(os.path.join(_HERE, ".."))
if _BACKEND not in sys.path:
    sys.path.insert(0, _BACKEND)

from modules.ai.brain.pipeline import _build_reply_state  # noqa: E402
from modules.ai.brain.truth_surface.contract import (  # noqa: E402
    TrustedContextSnapshot,
    TrustedDomain,
    TrustedFact,
    TruthSource,
)
from modules.ai.brain.types import (  # noqa: E402
    ActionResult,
    BrainContext,
    Decision,
    INTENT_GENERAL,
    Intent,
    MerchantConversationState,
    SuggestionSnapshot,
)

_MERCHANT = "متجر تجريبي عام"
_PRODUCT = "حذاء رياضي أبيض"


def _compose_facts(**overrides):
    base = {
        "schema_version": "1",
        "surface": "trusted_coupon_offer_answer",
        "question_kind": "offer",
        "coupon_availability": "none_verified",
        "promotion_availability": "active_or_eligible",
        "verified_eligible_coupon_count": 0,
        "verified_eligible_promotion_count": 1,
        "coupon_record_count": 0,
        "promotion_record_count": 1,
        "unavailability_reason_codes": [],
        "allow_code_mention": False,
        "allow_final_eligibility_claim": True,
        "facts_snapshot_id": "snap-test-001",
    }
    base.update(overrides)
    return base


def _minimal_ctx(message: str) -> BrainContext:
    facts = SimpleNamespace(
        store_name=_MERCHANT,
        store_url="https://example.test",
        store_url_resolved=True,
        store_url_source="settings",
        has_products=True,
        product_count=3,
        in_stock_count=2,
        orderable=True,
        shipping_policy="",
        shipping_methods=[],
        shipping_notes="",
        support_hours="",
        store_contact_phone="",
        store_contact_email="",
        has_coupons=True,
        coupon_eligibility=None,
    )
    return BrainContext(
        tenant_id=9001,
        customer_phone="966500000001",
        message=message,
        intent=Intent(name=INTENT_GENERAL, confidence=0.9),
        state=_state(),
        facts=facts,
        history=[],
        profile={},
    )


def _state() -> MerchantConversationState:
    return MerchantConversationState(
        stage="browsing",
        customer_goal="general_help",
        order_prep=SimpleNamespace(to_dict=lambda: {}),
    )


def test_pipeline_injects_known_facts_when_flag_on() -> None:
    ctx = _minimal_ctx("عندكم عروض؟")
    decision = Decision(action="llm_reply", args={})
    suggestion = SuggestionSnapshot()
    previous = _state()
    current = _state()
    snap = TrustedContextSnapshot(
        tenant_id=9001,
        facts=[
            TrustedFact(
                domain=TrustedDomain.PROMOTIONS,
                key="promotion:1",
                value={"promotion_id": 1, "eligible": True},
                source=TruthSource.PROMOTION_TABLE,
                path="promotion_table.id=1",
            )
        ],
        shadow_observability={"eligible_promotion_count": 1, "product_context": _PRODUCT},
    )
    snap.ensure_snapshot_id()

    with patch(
        "modules.ai.brain.truth_surface.coupon_offer_consumption_gate.is_trusted_context_coupon_offer_compose_enabled",
        return_value=True,
    ), patch(
        "modules.ai.brain.truth_surface.trusted_context.current_trusted_context",
        return_value=snap,
    ):
        reply_state = _build_reply_state(
            ctx=ctx,
            previous_state=previous,
            current_state=current,
            suggestion=suggestion,
            decision=decision,
        )
    assert "trusted_coupon_offer_facts" in reply_state.known_facts
    facts = reply_state.known_facts["trusted_coupon_offer_facts"]
    assert facts["promotion_availability"] == "active_or_eligible"
    assert "code" not in facts
    assert "code_masked" not in facts


def test_pipeline_omits_key_when_flag_off() -> None:
    ctx = _minimal_ctx("عندكم عروض؟")
    with patch(
        "modules.ai.brain.truth_surface.coupon_offer_consumption_gate.is_trusted_context_coupon_offer_compose_enabled",
        return_value=False,
    ):
        reply_state = _build_reply_state(
            ctx=ctx,
            previous_state=_state(),
            current_state=_state(),
            suggestion=SuggestionSnapshot(),
            decision=Decision(action="llm_reply", args={}),
        )
    assert "trusted_coupon_offer_facts" not in reply_state.known_facts


def test_brain_process_signature_unchanged() -> None:
    from modules.ai.brain.pipeline import MerchantBrain  # noqa: PLC0415

    sig = inspect.signature(MerchantBrain.process)
    assert "trusted_coupon" not in str(sig)


async def _run_trusted_coupon_compose_turn():
    from modules.ai.brain.compose.responder import DefaultComposer  # noqa: E402
    from modules.ai.brain.types import BrainReplyState  # noqa: E402

    compose_facts = _compose_facts()
    ctx = _minimal_ctx("عندكم عروض؟")
    ctx.reply_state = BrainReplyState(
        store_name=_MERCHANT,
        known_facts={"trusted_coupon_offer_facts": compose_facts},
    )
    result = ActionResult(success=True, data={})
    decision = Decision(action="llm_reply", args={})
    composer = DefaultComposer()

    with patch(
        "modules.ai.brain.persona.trusted_coupon_offer_answer.is_trusted_context_coupon_offer_compose_enabled",
        return_value=True,
    ), patch(
        "modules.ai.brain.persona.fact_bound_composer.FactBoundPersonaComposer.compose",
        new_callable=AsyncMock,
    ) as compose_mock:
        from modules.ai.brain.persona.facts_bundle import PersonaComposeResult  # noqa: E402

        compose_mock.return_value = PersonaComposeResult(
            text="نعم، في عروض متاحة حسب بيانات المتجر.",
            source="persona_llm",
            surface="trusted_coupon_offer_answer",
            facts_hash="abc",
            guard_passed=True,
            language="ar",
        )
        text = await composer.compose(decision, result, ctx)
    return text, result, compose_mock


def test_responder_routes_to_persona_compose_once() -> None:
    text, result, compose_mock = asyncio.run(_run_trusted_coupon_compose_turn())
    assert "عروض" in text
    compose_mock.assert_called_once()
    assert result.data.get("trusted_coupon_offer_compose_active") is True
    assert result.data.get("chosen_path") == "trusted_coupon_offer_compose"


def test_generic_merchant_eligibility_requires_context_case() -> None:
    ctx = _minimal_ctx("هل يوجد كوبون خصم؟")
    snap = TrustedContextSnapshot(
        tenant_id=9001,
        facts=[
            TrustedFact(
                domain=TrustedDomain.COUPONS,
                key="coupon:3",
                value={
                    "coupon_id": 3,
                    "eligible": None,
                    "reason_when_unavailable": "minimum_basket_unverified",
                },
                source=TruthSource.COUPON_TABLE,
                path="coupon_table.id=3",
            )
        ],
        shadow_observability={"product_context": _PRODUCT},
    )
    snap.ensure_snapshot_id()
    with patch(
        "modules.ai.brain.truth_surface.coupon_offer_consumption_gate.is_trusted_context_coupon_offer_compose_enabled",
        return_value=True,
    ), patch(
        "modules.ai.brain.truth_surface.trusted_context.current_trusted_context",
        return_value=snap,
    ):
        reply_state = _build_reply_state(
            ctx=ctx,
            previous_state=_state(),
            current_state=_state(),
            suggestion=SuggestionSnapshot(),
            decision=Decision(action="llm_reply", args={}),
        )
    facts = reply_state.known_facts.get("trusted_coupon_offer_facts") or {}
    assert facts.get("coupon_availability") == "eligibility_requires_context"
    assert facts.get("allow_final_eligibility_claim") is False


async def _run_trusted_coupon_responder_with_try_compose_mock(*, try_compose_side_effect):
    from modules.ai.brain.compose.responder import DefaultComposer  # noqa: E402
    from modules.ai.brain.types import BrainReplyState  # noqa: E402

    compose_facts = _compose_facts()
    ctx = _minimal_ctx("عندكم عروض؟")
    ctx.reply_state = BrainReplyState(
        store_name=_MERCHANT,
        known_facts={"trusted_coupon_offer_facts": compose_facts},
    )
    result = ActionResult(success=True, data={})
    decision = Decision(action="llm_reply", args={})
    composer = DefaultComposer()

    with patch(
        "modules.ai.brain.persona.trusted_coupon_offer_answer.try_compose_trusted_coupon_offer_answer",
        new_callable=AsyncMock,
        side_effect=try_compose_side_effect,
    ) as try_mock, patch.object(
        composer,
        "_llm_compose",
        new_callable=AsyncMock,
    ) as llm_mock:
        text = await composer.compose(decision, result, ctx)
    return text, result, llm_mock, try_mock


def test_responder_compose_exception_uses_tagged_fallback_not_llm() -> None:
    async def _boom(*_args, **_kwargs):
        raise RuntimeError("compose blew up")

    text, result, llm_mock, _try_mock = asyncio.run(
        _run_trusted_coupon_responder_with_try_compose_mock(
            try_compose_side_effect=_boom,
        )
    )
    llm_mock.assert_not_called()
    assert result.data.get("compose_source") == "fallback_deterministic"
    assert result.data.get("fallback_reason") == "compose_exception"
    assert result.data.get("fallback_action_type") == "trusted_coupon_offer_answer"
    assert result.data.get("chosen_path") == "trusted_coupon_offer_compose"
    assert result.data.get("trusted_coupon_offer_compose_active") is True
    assert (text or "").strip()


def test_responder_compose_empty_uses_tagged_fallback_not_llm() -> None:
    async def _empty(*_args, **_kwargs):
        return None, None, None

    text, result, llm_mock, _try_mock = asyncio.run(
        _run_trusted_coupon_responder_with_try_compose_mock(
            try_compose_side_effect=_empty,
        )
    )
    llm_mock.assert_not_called()
    assert result.data.get("compose_source") == "fallback_deterministic"
    assert result.data.get("fallback_reason") == "compose_empty"
    assert result.data.get("fallback_action_type") == "trusted_coupon_offer_answer"
    assert result.data.get("chosen_path") == "trusted_coupon_offer_compose"
    assert (text or "").strip()

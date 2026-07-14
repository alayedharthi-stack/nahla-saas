"""Integration regressions for trusted coupon/offer compose provenance wiring."""
from __future__ import annotations

import asyncio
import os
import sys
from contextlib import ExitStack
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

_HERE = os.path.dirname(os.path.abspath(__file__))
_BACKEND = os.path.abspath(os.path.join(_HERE, ".."))
if _BACKEND not in sys.path:
    sys.path.insert(0, _BACKEND)

from modules.ai.brain.decision.actions import ACTION_LLM_REPLY  # noqa: E402
from modules.ai.brain.truth_surface.contract import (  # noqa: E402
    TrustedContextSnapshot,
    TrustedDomain,
    TrustedFact,
    TruthSource,
)
from modules.ai.brain.truth_surface.trusted_context import (  # noqa: E402
    clear_trusted_context,
    set_current_trusted_context,
)
from modules.ai.brain.types import (  # noqa: E402
    CommerceFacts,
    Decision,
    INTENT_GENERAL,
    Intent,
    MerchantConversationState,
)
from tests.test_trusted_context_shadow_wireup import (  # noqa: E402
    _merchant_handler_convo,
    _merchant_handler_db,
    _merchant_handler_patch_ctx,
    _run,
)

_MERCHANT = "متجر تجريبي عام"
_PHONE = "966500000099"
_OFFER_MESSAGE = "عندكم عروض؟"
_COMPOSE_BODY = "في عروض متاحة حسب بيانات المتجر المؤكدة."
_SANITIZER_LEAK_PREFIX = "Progressive Selling policy. "
_COMPOSE_WITH_LEAK = f"{_SANITIZER_LEAK_PREFIX}{_COMPOSE_BODY}"
_TC_REPLY = (
    "نعم، في عروض متاحة حسب بيانات المتجر التجريبي العام المؤكدة "
    "حالياً لدينا عروض نشطة يمكن الاستفسار عنها."
)
_DEDUP_SUBSTITUTE = (
    "نحتاج تفاصيل إضافية عن طلبك لمساعدتك بشكل أدق في العروض المتاحة."
)


def _offer_snapshot() -> TrustedContextSnapshot:
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
        shadow_observability={"eligible_promotion_count": 1},
    )
    snap.ensure_snapshot_id()
    return snap


def _commerce_facts() -> CommerceFacts:
    return CommerceFacts(
        store_name=_MERCHANT,
        store_url="https://example.test",
        store_url_resolved=True,
        store_url_source="settings",
        has_products=True,
        product_count=3,
        in_stock_count=2,
        orderable=True,
        has_coupons=True,
    )


def _brain_process_patches(brain, snap: TrustedContextSnapshot):
    """Patches that steer get_brain().process to trusted coupon/offer compose."""
    intent = Intent(name=INTENT_GENERAL, confidence=0.92, slots={})
    decision = Decision(action=ACTION_LLM_REPLY, args={})
    state = MerchantConversationState(
        stage="browsing",
        greeted=True,
        customer_goal="general_help",
    )

    stack = ExitStack()
    stack.enter_context(
        patch(
            "core.billing.has_billing_access",
            return_value=True,
        )
    )
    stack.enter_context(
        patch(
            "core.wa_usage.check_limit",
            return_value=SimpleNamespace(
                allowed=True,
                used_total=0,
                limit=1000,
                reason="",
            ),
        )
    )
    stack.enter_context(
        patch(
            "core.ai_disabled_gate.is_ai_disabled_for_conversation",
            return_value=SimpleNamespace(disabled=False, reason=None),
        )
    )
    stack.enter_context(
        patch(
            "modules.ai.brain.truth_surface.coupon_offer_consumption_gate.is_trusted_context_coupon_offer_compose_enabled",
            return_value=True,
        )
    )
    stack.enter_context(
        patch(
            "modules.ai.brain.persona.trusted_coupon_offer_answer.is_trusted_context_coupon_offer_compose_enabled",
            return_value=True,
        )
    )
    stack.enter_context(
        patch.object(brain._classifier, "classify", return_value=intent)
    )
    stack.enter_context(
        patch.object(brain._decision_engine, "decide", return_value=decision)
    )
    stack.enter_context(
        patch.object(brain._policy_gate, "gate", side_effect=lambda d, _ctx: d)
    )
    stack.enter_context(
        patch.object(brain._state_store, "load", return_value=state)
    )
    stack.enter_context(
        patch.object(brain._state_store, "save")
    )
    stack.enter_context(
        patch.object(brain._facts_loader, "load", return_value=_commerce_facts())
    )
    stack.enter_context(
        patch.object(brain._memory_updater, "update")
    )
    compose_mock = stack.enter_context(
        patch(
            "modules.ai.brain.persona.fact_bound_composer.FactBoundPersonaComposer.compose",
            new_callable=AsyncMock,
        )
    )
    from modules.ai.brain.persona.facts_bundle import PersonaComposeResult  # noqa: PLC0415

    compose_mock.return_value = PersonaComposeResult(
        text=_COMPOSE_WITH_LEAK,
        source="persona_llm",
        surface="trusted_coupon_offer_answer",
        facts_hash="integration-hash",
        guard_passed=True,
        language="ar",
    )
    set_current_trusted_context(snap)
    return stack, compose_mock


def test_pipeline_trusted_coupon_offer_provenance_after_sanitize() -> None:
    """Real MerchantBrain.process finalizes provenance after sanitizer mutation."""
    from modules.ai.brain.pipeline import get_brain  # noqa: PLC0415

    clear_trusted_context()
    brain = get_brain()
    snap = _offer_snapshot()
    db = MagicMock()

    stack, _compose_mock = _brain_process_patches(brain, snap)
    with stack:
        result = _run(
            brain.process(
                db=db,
                tenant_id=9001,
                customer_phone=_PHONE,
                message=_OFFER_MESSAGE,
                history=[],
                profile={"preferred_language": "ar"},
                conversation_id=42,
            )
        )

    try:
        assert result.get("trusted_coupon_offer_compose_active") is True
        assert result.get("compose_source") == "persona_llm"
        assert result.get("chosen_path") == "trusted_coupon_offer_compose"
        assert result.get("final_text_transformed") is True
        assert "sanitize_outbound_text" in list(result.get("final_transform_reasons") or [])
        assert _SANITIZER_LEAK_PREFIX.strip() not in (result.get("reply") or "")
        assert _COMPOSE_BODY in (result.get("reply") or "")
    finally:
        clear_trusted_context()


def test_webhook_dedup_updates_trusted_coupon_offer_provenance_metadata() -> None:
    """Real webhook dedup substitution stamps constitutional metadata on outbound."""
    import models as _models  # noqa: PLC0415

    sys.modules.setdefault("database.models", _models)

    from routers.whatsapp_webhook import _handle_merchant_message  # noqa: PLC0415

    clear_trusted_context()
    convo = _merchant_handler_convo()
    db = _merchant_handler_db()
    saved_metadata: dict = {}

    def _capture_save(*_args, **kwargs):
        meta = kwargs.get("extra_metadata")
        if isinstance(meta, dict):
            saved_metadata.update(meta)

    def _dedup_history(*_args, **_kwargs):
        return [
            {"direction": "inbound", "body": _OFFER_MESSAGE},
            {"direction": "outbound", "body": _TC_REPLY},
        ]

    brain_return = {
        "reply": _TC_REPLY,
        "buttons": [],
        "handoff": False,
        "chosen_path": "trusted_coupon_offer_compose",
        "trusted_coupon_offer_compose_active": True,
        "compose_source": "persona_llm",
        "response_mode": "trusted_coupon_offer_answer",
        "llm_candidate_present": True,
        "final_text_transformed": False,
        "final_transform_reasons": [],
        "question_kind": "offer",
        "facts_snapshot_id": "snap-integration-dedup",
    }

    with _merchant_handler_patch_ctx(
        convo=convo,
        history_side_effect=_dedup_history,
    ) as (mock_brain, _state):
        with patch(
            "routers.whatsapp_webhook.StateManager.save_message",
            side_effect=_capture_save,
        ), patch(
            "core.order_flow.context_aware_dedup_fallback",
            return_value=_DEDUP_SUBSTITUTE,
        ):
            mock_brain.return_value.process = AsyncMock(return_value=dict(brain_return))
            _run(
                _handle_merchant_message(
                    phone_id="PH1",
                    to=_PHONE,
                    text=_OFFER_MESSAGE,
                    tenant_id=1,
                    db=db,
                )
            )

    assert saved_metadata, "expected outbound save_message with extra_metadata"
    assert saved_metadata.get("trusted_coupon_offer_compose_active") is True
    assert saved_metadata.get("compose_source") == "persona_llm"
    assert saved_metadata.get("chosen_path") == "trusted_coupon_offer_compose"
    assert saved_metadata.get("final_text_transformed") is True
    assert "chat_dedup_substitution" in list(
        saved_metadata.get("final_transform_reasons") or []
    )
    clear_trusted_context()

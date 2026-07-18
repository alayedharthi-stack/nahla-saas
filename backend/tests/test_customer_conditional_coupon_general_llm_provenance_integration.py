"""Pipeline→webhook provenance for conditional-coupon general-LLM fallthrough."""
from __future__ import annotations

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

from modules.ai.brain.commerce.inbound_fragment_guard import (  # noqa: E402
    build_discount_coupon_support_reply,
)
from modules.ai.brain.decision.actions import ACTION_LLM_REPLY  # noqa: E402
from modules.ai.brain.truth_surface.contract import (  # noqa: E402
    TrustedContextSnapshot,
    TrustedDomain,
    TrustedFact,
    TruthSource,
)
from modules.ai.brain.truth_surface.customer_conditional_coupon_contract import (  # noqa: E402
    COMPLETENESS_VERIFIED,
    EVALUATION_CONDITION_SHORTFALL,
    IDENTITY_STATUS_RESOLVED,
    MIN_ORDERS_STATE_SHORTFALL,
    build_sanitized_fact_record,
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
_PHONE = "966500000088"
_MESSAGE = "بعد كم طلب يصل الكوبون؟"
_DISCOUNT_FALLBACK = build_discount_coupon_support_reply()
_SAFE_UNCERTAINTY = (
    "قد يختلف الشرط حسب سياسة المتجر، "
    "والبيانات المتوفرة عندنا لا تؤكد العدد بدقة الآن."
)
_SANITIZER_LEAK_PREFIX = "Progressive Selling policy. "
_SAFE_WITH_LEAK = f"{_SANITIZER_LEAK_PREFIX}{_SAFE_UNCERTAINTY}"
_UNSAFE_LLM = "كود الخصم ABC123 جاهز لك."
_DEDUP_SUBSTITUTE = (
    "نحتاج تفاصيل إضافية عن طلبك لمساعدتك بشكل أدق في شروط الكوبون."
)


@pytest.fixture(autouse=True)
def _compose_master_flag(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv(
        "NAHLA_TRUSTED_CONTEXT_CUSTOMER_CONDITIONAL_COUPON_COMPOSE_ENABLED",
        "true",
    )


def _eligible_compose_ai_settings(*, tenant_id: int, phone: str) -> dict:
    return {
        "store_ai_mode": "test",
        "customer_conditional_coupon_compose_allowlist_tenants": [int(tenant_id)],
        "ai_test_allowed_numbers": [str(phone)],
    }


def _conditional_snapshot() -> TrustedContextSnapshot:
    record = build_sanitized_fact_record(
        identity_status=IDENTITY_STATUS_RESOLVED,
        customer_scope="nahla_internal_customer",
        order_history_completeness=COMPLETENESS_VERIFIED,
        order_history_completeness_source="order_customer_fk_a1_authoritative",
        completed_orders_count=1,
        min_orders_for_eligibility=3,
        orders_shortfall=2,
        min_orders_condition_state=MIN_ORDERS_STATE_SHORTFALL,
        prior_redemption_evidence_state="not_applicable",
        per_customer_usage_policy_state="verified",
        conditional_coupon_evaluation_state=EVALUATION_CONDITION_SHORTFALL,
        closed_reason_code="orders_shortfall",
        allow_min_orders_condition_claim=False,
    )
    snap = TrustedContextSnapshot(
        tenant_id=9002,
        facts=[
            TrustedFact(
                domain=TrustedDomain.CUSTOMER_CONDITIONAL_COUPON,
                key="customer_conditional_coupon:eligibility",
                value=record,
                source=TruthSource.PROMOTION_TABLE,
                path="customer_conditional_coupon_loader.layer0",
            )
        ],
        shadow_observability={"merchant_label": _MERCHANT},
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


def _brain_process_patches(brain, snap: TrustedContextSnapshot, *, llm_text: str):
    intent = Intent(name=INTENT_GENERAL, confidence=0.92, slots={})
    decision = Decision(action=ACTION_LLM_REPLY, args={})
    state = MerchantConversationState(
        stage="browsing",
        greeted=True,
        customer_goal="general_help",
    )

    stack = ExitStack()
    stack.enter_context(patch("core.billing.has_billing_access", return_value=True))
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
            "modules.ai.brain.truth_surface.customer_conditional_coupon_compose_canary_gate."
            "evaluate_customer_conditional_coupon_compose_canary",
            return_value=__import__(
                "modules.ai.brain.truth_surface.customer_conditional_coupon_compose_canary_gate",
                fromlist=["CustomerConditionalCouponComposeCanaryDecision"],
            ).CustomerConditionalCouponComposeCanaryDecision(
                allowed=True,
                reason="allowed",
                compose_master_enabled=True,
                relevance_required=True,
                relevance_satisfied=True,
            ),
        )
    )
    stack.enter_context(
        patch(
            "modules.ai.brain.truth_surface.customer_conditional_coupon_consumption_gate."
            "evaluate_customer_conditional_coupon_compose_canary",
            return_value=__import__(
                "modules.ai.brain.truth_surface.customer_conditional_coupon_compose_canary_gate",
                fromlist=["CustomerConditionalCouponComposeCanaryDecision"],
            ).CustomerConditionalCouponComposeCanaryDecision(
                allowed=True,
                reason="allowed",
                compose_master_enabled=True,
                relevance_required=True,
                relevance_satisfied=True,
            ),
        )
    )
    stack.enter_context(
        patch(
            "modules.ai.brain.persona.customer_conditional_coupon_answer."
            "evaluate_customer_conditional_coupon_compose_canary",
            return_value=__import__(
                "modules.ai.brain.truth_surface.customer_conditional_coupon_compose_canary_gate",
                fromlist=["CustomerConditionalCouponComposeCanaryDecision"],
            ).CustomerConditionalCouponComposeCanaryDecision(
                allowed=True,
                reason="allowed",
                compose_master_enabled=True,
                relevance_required=True,
                relevance_satisfied=True,
            ),
        )
    )
    stack.enter_context(
        patch(
            "modules.ai.brain.postprocess.customer_conditional_coupon_general_llm_evidence_guard."
            "is_customer_conditional_coupon_layer0_enabled",
            return_value=True,
        )
    )
    stack.enter_context(
        patch(
            "modules.ai.brain.truth_surface.coupon_offer_consumption_gate."
            "is_trusted_context_coupon_offer_compose_enabled",
            return_value=False,
        )
    )
    stack.enter_context(
        patch(
            "modules.ai.brain.persona.customer_conditional_coupon_answer."
            "try_compose_customer_conditional_coupon_answer",
            new_callable=AsyncMock,
            return_value=(None, None, None),
        )
    )
    stack.enter_context(
        patch(
            "modules.ai.brain.compose.responder.DefaultComposer._llm_compose",
            new_callable=AsyncMock,
            return_value=llm_text,
        )
    )
    stack.enter_context(patch.object(brain._classifier, "classify", return_value=intent))
    stack.enter_context(patch.object(brain._decision_engine, "decide", return_value=decision))
    stack.enter_context(patch.object(brain._policy_gate, "gate", side_effect=lambda d, _ctx: d))
    stack.enter_context(patch.object(brain._state_store, "load", return_value=state))
    stack.enter_context(patch.object(brain._state_store, "save"))
    stack.enter_context(patch.object(brain._facts_loader, "load", return_value=_commerce_facts()))
    stack.enter_context(patch.object(brain._memory_updater, "update"))
    stack.enter_context(
        patch(
            "core.store_knowledge.build_merchant_context",
            return_value={
                "ai_settings": _eligible_compose_ai_settings(
                    tenant_id=9002,
                    phone=_PHONE,
                )
            },
        )
    )
    set_current_trusted_context(snap)
    return stack


def test_pipeline_safe_general_llm_fallthrough_provenance_after_sanitize() -> None:
    from modules.ai.brain.pipeline import get_brain  # noqa: PLC0415

    clear_trusted_context()
    brain = get_brain()
    snap = _conditional_snapshot()
    expected_snapshot_id = snap.ensure_snapshot_id()

    stack = _brain_process_patches(brain, snap, llm_text=_SAFE_WITH_LEAK)
    with stack:
        result = _run(
            brain.process(
                db=MagicMock(),
                tenant_id=9002,
                customer_phone=_PHONE,
                message=_MESSAGE,
                history=[],
                profile={"preferred_language": "ar"},
                conversation_id=51,
            )
        )

    try:
        assert result.get("customer_conditional_coupon_general_llm_fallthrough") is True
        assert result.get("compose_source") == "llm"
        assert result.get("chosen_path") == "customer_conditional_coupon_general_llm_fallthrough"
        assert result.get("response_mode") == "customer_conditional_coupon_general_llm"
        assert result.get("llm_candidate_present") is True
        assert result.get("final_text_transformed") is True
        assert "sanitize_outbound_text" in list(result.get("final_transform_reasons") or [])
        assert result.get("final_customer_text_source") == "llm_postprocess"
        assert result.get("facts_snapshot_id") == expected_snapshot_id
        assert _SANITIZER_LEAK_PREFIX.strip() not in (result.get("reply") or "")
        assert _SAFE_UNCERTAINTY in (result.get("reply") or "")
        assert _DISCOUNT_FALLBACK not in (result.get("reply") or "")
    finally:
        clear_trusted_context()


def test_pipeline_safe_general_llm_fallthrough_without_text_transform() -> None:
    from modules.ai.brain.pipeline import get_brain  # noqa: PLC0415

    clear_trusted_context()
    brain = get_brain()
    snap = _conditional_snapshot()
    expected_snapshot_id = snap.ensure_snapshot_id()

    stack = _brain_process_patches(brain, snap, llm_text=_SAFE_UNCERTAINTY)
    with stack:
        result = _run(
            brain.process(
                db=MagicMock(),
                tenant_id=9002,
                customer_phone=_PHONE,
                message=_MESSAGE,
                history=[],
                profile={"preferred_language": "ar"},
                conversation_id=53,
            )
        )

    try:
        assert result.get("customer_conditional_coupon_general_llm_fallthrough") is True
        assert result.get("compose_source") == "llm"
        assert result.get("chosen_path") == "customer_conditional_coupon_general_llm_fallthrough"
        assert result.get("facts_snapshot_id") == expected_snapshot_id
        assert result.get("final_customer_text_source") == "llm"
        assert result.get("final_text_transformed") is False
        assert result.get("final_transform_reasons") == []
        assert result.get("reply") == _SAFE_UNCERTAINTY
        assert _DISCOUNT_FALLBACK not in (result.get("reply") or "")
    finally:
        clear_trusted_context()


def test_pipeline_unsafe_general_llm_fallthrough_guard_rejection_provenance() -> None:
    from modules.ai.brain.pipeline import get_brain  # noqa: PLC0415

    clear_trusted_context()
    brain = get_brain()
    snap = _conditional_snapshot()

    stack = _brain_process_patches(brain, snap, llm_text=_UNSAFE_LLM)
    with stack:
        result = _run(
            brain.process(
                db=MagicMock(),
                tenant_id=9002,
                customer_phone=_PHONE,
                message=_MESSAGE,
                history=[],
                profile={"preferred_language": "ar"},
                conversation_id=52,
            )
        )

    try:
        assert result.get("customer_conditional_coupon_general_llm_fallthrough") is True
        assert result.get("compose_source") == "llm"
        assert result.get("chosen_path") == "customer_conditional_coupon_general_llm_fallthrough"
        assert result.get("llm_candidate_present") is True
        assert result.get("final_customer_text_source") == "guard_rewrite"
        assert result.get("final_text_transformed") is True
        assert "customer_conditional_coupon_general_llm_evidence_guard" in list(
            result.get("final_transform_reasons") or []
        )
        assert result.get("conditional_coupon_guard_failed_reason") == "coupon_code_disclosure"
        assert (result.get("reply") or "").strip() == ""
        assert _DISCOUNT_FALLBACK not in (result.get("reply") or "")
    finally:
        clear_trusted_context()


_CC_REPLY = (
    "بعد 3 طلبات مكتملة يتفعل عرض الكوبون حسب بيانات المتجر التجريبي العام المؤكدة."
)


def test_webhook_dedup_updates_general_llm_fallthrough_provenance_metadata() -> None:
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
            {"direction": "inbound", "body": _MESSAGE},
            {"direction": "outbound", "body": _CC_REPLY},
        ]

    brain_return = {
        "reply": _CC_REPLY,
        "buttons": [],
        "handoff": False,
        "chosen_path": "customer_conditional_coupon_general_llm_fallthrough",
        "customer_conditional_coupon_general_llm_fallthrough": True,
        "compose_source": "llm",
        "response_mode": "customer_conditional_coupon_general_llm",
        "llm_candidate_present": True,
        "final_text_transformed": False,
        "final_transform_reasons": [],
        "final_customer_text_source": "llm",
        "facts_snapshot_id": "snap-integration-general-llm-dedup",
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
                    text=_MESSAGE,
                    tenant_id=1,
                    db=db,
                )
            )

    assert saved_metadata, "expected outbound save_message with extra_metadata"
    assert saved_metadata.get("customer_conditional_coupon_general_llm_fallthrough") is True
    assert saved_metadata.get("compose_source") == "llm"
    assert saved_metadata.get("chosen_path") == "customer_conditional_coupon_general_llm_fallthrough"
    assert saved_metadata.get("final_text_transformed") is True
    assert saved_metadata.get("final_customer_text_source") == "dedup_substitution"
    assert (
        saved_metadata.get("facts_snapshot_id")
        == "snap-integration-general-llm-dedup"
    )
    assert "chat_dedup_substitution" in list(
        saved_metadata.get("final_transform_reasons") or []
    )
    clear_trusted_context()

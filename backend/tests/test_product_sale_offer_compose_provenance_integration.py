"""Integration regressions for product_sale_offer / general_offer_discovery provenance."""
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
_STORE_WIDE_MESSAGE = "عندكم عروض؟"
_PRODUCT_SCOPED_MESSAGE = "هل المنتج مخفض؟"
_COMPOSE_BODY = "في منتجات بأسعار مخفّضة حسب بيانات الكتالوج المؤكدة."
_SANITIZER_LEAK_PREFIX = "Progressive Selling policy. "
_COMPOSE_WITH_LEAK = f"{_SANITIZER_LEAK_PREFIX}{_COMPOSE_BODY}"
_DISCOVERY_REPLY = (
    "نعم، في منتجات بأسعار مخفّضة حسب بيانات الكتالوج المؤكدة حالياً."
)
_DEDUP_SUBSTITUTE = (
    "نحتاج تفاصيل إضافية عن طلبك لمساعدتك بشكل أدق في العروض المتاحة."
)
_GUARD_REWRITE_TEXT = (
    "الرد النهائي بعد تعديل الحارس العام على صياغة العرض حسب سياسة المنصة."
)


def _store_wide_sale_record() -> dict:
    return {
        "domain": TrustedDomain.CATALOG.value,
        "bundle_namespace": "product_sale_offer",
        "question_kind": "store_wide",
        "product_sale_availability": "active_sale_present",
        "verified_on_sale_product_count": 1,
        "sample_products": [
            {"title": "حذاء رياضي أبيض", "sale_price": "80", "regular_price": "100"},
        ],
        "allow_price_mention": True,
    }


def _product_scoped_sale_record() -> dict:
    return {
        "domain": TrustedDomain.CATALOG.value,
        "bundle_namespace": "product_sale_offer",
        "question_kind": "product_scoped",
        "product_sale_availability": "active_sale_present",
        "verified_on_sale_product_count": 1,
        "allow_price_mention": True,
        "target_product": {
            "title": "عطر ورد 100ml",
            "sale_price": "199",
            "regular_price": "249",
            "is_on_sale": True,
        },
    }


def _sale_snapshot(*, record: dict) -> TrustedContextSnapshot:
    snap = TrustedContextSnapshot(
        tenant_id=9001,
        facts=[
            TrustedFact(
                domain=TrustedDomain.CATALOG,
                key="catalog:product_sale_offer",
                value=record,
                source=TruthSource.PRODUCTS_TABLE,
                path="products_table.on_sale_count",
            )
        ],
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
        has_coupons=False,
    )


def _brain_process_patches(
    brain,
    snap: TrustedContextSnapshot,
    *,
    message: str,
    discovery_enabled: bool = False,
    product_sale_enabled: bool = False,
    compose_text: str = _COMPOSE_WITH_LEAK,
    compose_surface: str = "general_offer_discovery_answer",
):
    intent = Intent(name=INTENT_GENERAL, confidence=0.92, slots={})
    decision = Decision(action=ACTION_LLM_REPLY, args={})
    state = MerchantConversationState(
        stage="browsing",
        greeted=True,
        customer_goal="general_help",
    )
    if product_sale_enabled:
        state.current_product_focus = {"product_id": 9}

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
            "modules.ai.brain.truth_surface.flags.is_general_offer_discovery_compose_enabled",
            return_value=discovery_enabled,
        )
    )
    stack.enter_context(
        patch(
            "modules.ai.brain.truth_surface.flags.is_product_sale_offer_compose_enabled",
            return_value=product_sale_enabled,
        )
    )
    stack.enter_context(
        patch(
            "modules.ai.brain.truth_surface.product_sale_offer_consumption_gate.is_general_offer_discovery_compose_enabled",
            return_value=discovery_enabled,
        )
    )
    stack.enter_context(
        patch(
            "modules.ai.brain.persona.general_offer_discovery_answer.is_general_offer_discovery_compose_enabled",
            return_value=discovery_enabled,
        )
    )
    stack.enter_context(
        patch(
            "modules.ai.brain.truth_surface.product_sale_offer_consumption_gate.is_product_sale_offer_compose_enabled",
            return_value=product_sale_enabled,
        )
    )
    stack.enter_context(
        patch(
            "modules.ai.brain.persona.product_sale_offer_answer.is_product_sale_offer_compose_enabled",
            return_value=product_sale_enabled,
        )
    )
    stack.enter_context(patch.object(brain._classifier, "classify", return_value=intent))
    stack.enter_context(patch.object(brain._decision_engine, "decide", return_value=decision))
    stack.enter_context(patch.object(brain._policy_gate, "gate", side_effect=lambda d, _ctx: d))
    stack.enter_context(patch.object(brain._state_store, "load", return_value=state))
    stack.enter_context(patch.object(brain._state_store, "save"))
    stack.enter_context(patch.object(brain._facts_loader, "load", return_value=_commerce_facts()))
    stack.enter_context(patch.object(brain._memory_updater, "update"))
    compose_mock = stack.enter_context(
        patch(
            "modules.ai.brain.persona.fact_bound_composer.FactBoundPersonaComposer.compose",
            new_callable=AsyncMock,
        )
    )
    from modules.ai.brain.persona.facts_bundle import PersonaComposeResult  # noqa: PLC0415

    compose_mock.return_value = PersonaComposeResult(
        text=compose_text,
        source="persona_llm",
        surface=compose_surface,
        facts_hash="integration-hash",
        guard_passed=True,
        language="ar",
    )
    set_current_trusted_context(snap)
    return stack, compose_mock


def test_pipeline_general_offer_discovery_provenance_after_sanitize() -> None:
    """Store-wide path: LLM compose survives generic sanitizer without source replacement."""
    from modules.ai.brain.pipeline import get_brain  # noqa: PLC0415

    clear_trusted_context()
    brain = get_brain()
    snap = _sale_snapshot(record=_store_wide_sale_record())
    stack, _compose_mock = _brain_process_patches(
        brain,
        snap,
        message=_STORE_WIDE_MESSAGE,
        discovery_enabled=True,
        compose_surface="general_offer_discovery_answer",
    )
    with stack:
        result = _run(
            brain.process(
                db=MagicMock(),
                tenant_id=9001,
                customer_phone=_PHONE,
                message=_STORE_WIDE_MESSAGE,
                history=[],
                profile={"preferred_language": "ar"},
                conversation_id=42,
            )
        )

    try:
        assert result.get("general_offer_discovery_compose_active") is True
        assert result.get("compose_source") == "persona_llm"
        assert result.get("chosen_path") == "general_offer_discovery_compose"
        assert result.get("llm_candidate_present") is True
        assert result.get("final_text_transformed") is True
        assert "sanitize_outbound_text" in list(result.get("final_transform_reasons") or [])
        assert _SANITIZER_LEAK_PREFIX.strip() not in (result.get("reply") or "")
        assert _COMPOSE_BODY in (result.get("reply") or "")
        assert result.get("fallback_reason") in (None, "")
        assert result.get("final_customer_text_source") == "persona_llm_postprocess"
    finally:
        clear_trusted_context()


def test_pipeline_general_offer_discovery_provenance_after_guard_rewrite() -> None:
    """Generic guard rewrite must stamp guard provenance without claiming final LLM text."""
    from modules.ai.brain.pipeline import get_brain  # noqa: PLC0415
    from modules.ai.brain.postprocess.saudi_dialect_guard import (  # noqa: PLC0415
        SaudiDialectGuardResult,
    )

    clear_trusted_context()
    brain = get_brain()
    snap = _sale_snapshot(record=_store_wide_sale_record())
    stack, compose_mock = _brain_process_patches(
        brain,
        snap,
        message=_STORE_WIDE_MESSAGE,
        discovery_enabled=True,
        compose_text=_COMPOSE_BODY,
        compose_surface="general_offer_discovery_answer",
    )
    assert compose_mock is not None
    with stack:
        with patch(
            "modules.ai.brain.postprocess.saudi_dialect_guard.apply_saudi_dialect_guard",
            return_value=SaudiDialectGuardResult(
                reply=_GUARD_REWRITE_TEXT,
                replaced=True,
            ),
        ):
            result = _run(
                brain.process(
                    db=MagicMock(),
                    tenant_id=9001,
                    customer_phone=_PHONE,
                    message=_STORE_WIDE_MESSAGE,
                    history=[],
                    profile={"preferred_language": "ar"},
                    conversation_id=45,
                )
            )

    try:
        assert result.get("general_offer_discovery_compose_active") is True
        assert result.get("llm_candidate_present") is True
        assert result.get("compose_source") == "persona_llm"
        assert result.get("final_customer_text_source") == "guard_rewrite"
        assert result.get("final_customer_text_source") != result.get("compose_source")
        assert result.get("final_text_transformed") is True
        assert "saudi_dialect_guard" in list(result.get("final_transform_reasons") or [])
        assert result.get("reply") == _GUARD_REWRITE_TEXT
        assert _COMPOSE_BODY not in (result.get("reply") or "")
    finally:
        clear_trusted_context()


def test_pipeline_product_sale_offer_provenance_after_sanitize() -> None:
    """Product-scoped path: LLM compose survives generic sanitizer without source replacement."""
    from modules.ai.brain.pipeline import get_brain  # noqa: PLC0415

    clear_trusted_context()
    brain = get_brain()
    snap = _sale_snapshot(record=_product_scoped_sale_record())
    stack, _compose_mock = _brain_process_patches(
        brain,
        snap,
        message=_PRODUCT_SCOPED_MESSAGE,
        product_sale_enabled=True,
        compose_surface="product_sale_offer_answer",
    )
    with stack:
        result = _run(
            brain.process(
                db=MagicMock(),
                tenant_id=9001,
                customer_phone=_PHONE,
                message=_PRODUCT_SCOPED_MESSAGE,
                history=[],
                profile={"preferred_language": "ar"},
                conversation_id=43,
            )
        )

    try:
        assert result.get("product_sale_offer_compose_active") is True
        assert result.get("compose_source") == "persona_llm"
        assert result.get("chosen_path") == "product_sale_offer_compose"
        assert result.get("llm_candidate_present") is True
        assert result.get("final_text_transformed") is True
        assert "sanitize_outbound_text" in list(result.get("final_transform_reasons") or [])
        assert _SANITIZER_LEAK_PREFIX.strip() not in (result.get("reply") or "")
        assert _COMPOSE_BODY in (result.get("reply") or "")
    finally:
        clear_trusted_context()


def test_pipeline_general_offer_discovery_fallback_on_compose_failure() -> None:
    """Compose failure must yield fallback_deterministic with full metadata only."""
    from modules.ai.brain.pipeline import get_brain  # noqa: PLC0415
    from modules.ai.brain.persona.facts_bundle import PersonaComposeResult  # noqa: PLC0415

    clear_trusted_context()
    brain = get_brain()
    snap = _sale_snapshot(record=_store_wide_sale_record())
    stack, compose_mock = _brain_process_patches(
        brain,
        snap,
        message=_STORE_WIDE_MESSAGE,
        discovery_enabled=True,
        compose_text="",
        compose_surface="general_offer_discovery_answer",
    )
    compose_mock.return_value = PersonaComposeResult(
        text="",
        source="persona_llm",
        surface="general_offer_discovery_answer",
        facts_hash="empty-hash",
        guard_passed=True,
        language="ar",
    )
    with stack:
        result = _run(
            brain.process(
                db=MagicMock(),
                tenant_id=9001,
                customer_phone=_PHONE,
                message=_STORE_WIDE_MESSAGE,
                history=[],
                profile={"preferred_language": "ar"},
                conversation_id=44,
            )
        )

    try:
        assert result.get("general_offer_discovery_compose_active") is True
        assert result.get("compose_source") == "fallback_deterministic"
        assert result.get("final_customer_text_source") == "fallback_deterministic"
        assert result.get("chosen_path") == "general_offer_discovery_compose"
        assert result.get("fallback_reason") in {"compose_empty", "compose_exception"}
        assert result.get("fallback_action_type") == "general_offer_discovery_answer"
        assert result.get("facts_snapshot_id")
        assert (result.get("reply") or "").strip()
    finally:
        clear_trusted_context()


def test_webhook_dedup_updates_general_offer_discovery_provenance_metadata() -> None:
    """Generic dedup substitution must stamp transform metadata without rewriting compose_source."""
    pytest.importorskip("observability.event_logger")

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
            {"direction": "inbound", "body": _STORE_WIDE_MESSAGE},
            {"direction": "outbound", "body": _DISCOVERY_REPLY},
        ]

    brain_return = {
        "reply": _DISCOVERY_REPLY,
        "buttons": [],
        "handoff": False,
        "chosen_path": "general_offer_discovery_compose",
        "general_offer_discovery_compose_active": True,
        "compose_source": "persona_llm",
        "response_mode": "general_offer_discovery_answer",
        "llm_candidate_present": True,
        "final_text_transformed": False,
        "final_transform_reasons": [],
        "facts_snapshot_id": "snap-integration-dedup-discovery",
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
                    text=_STORE_WIDE_MESSAGE,
                    tenant_id=1,
                    db=db,
                )
            )

    assert saved_metadata, "expected outbound save_message with extra_metadata"
    assert saved_metadata.get("general_offer_discovery_compose_active") is True
    assert saved_metadata.get("compose_source") == "persona_llm"
    assert saved_metadata.get("chosen_path") == "general_offer_discovery_compose"
    assert saved_metadata.get("final_text_transformed") is True
    assert saved_metadata.get("final_customer_text_source") == "dedup_substitution"
    assert "chat_dedup_substitution" in list(
        saved_metadata.get("final_transform_reasons") or []
    )
    clear_trusted_context()

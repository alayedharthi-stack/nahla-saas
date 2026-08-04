"""Brain-owned checkout shipping grounding regressions."""
from __future__ import annotations

import asyncio
import sys
from contextlib import ExitStack
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import pytest

ROOT = Path(__file__).resolve().parents[2]
BACKEND = ROOT / "backend"
for p in (ROOT, BACKEND):
    if str(p) not in sys.path:
        sys.path.insert(0, str(p))

from commerce_scenario_fixtures import make_scenario_db, seed_knowledge_section, seed_tenant  # noqa: E402
from core.checkout_shipping_policy import resolve_checkout_shipping_policy  # noqa: E402
from modules.ai.brain.decision.actions import ACTION_LLM_REPLY  # noqa: E402
from modules.ai.brain.postprocess.shipping_cost_truth_guard import (  # noqa: E402
    apply_shipping_cost_truth_guard,
)
from modules.ai.brain.types import (  # noqa: E402
    ActionResult,
    CommerceFacts,
    Decision,
    INTENT_GENERAL,
    Intent,
    MerchantConversationState,
)


def _run(coro):
    return asyncio.run(coro)


def _commerce_facts() -> CommerceFacts:
    return CommerceFacts(
        store_name="متجر تجريبي عام",
        store_url="https://example.test",
        store_url_resolved=True,
        store_url_source="settings",
        has_products=True,
        product_count=3,
        in_stock_count=2,
        orderable=True,
    )


def _pipeline_shipping_stack(brain, *, reply: str):
    intent = Intent(name=INTENT_GENERAL, confidence=0.92, slots={})
    decision = Decision(action=ACTION_LLM_REPLY, args={})
    state = MerchantConversationState(stage="browsing", greeted=True)

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
            "core.store_knowledge.build_merchant_context",
            return_value={},
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
        patch.object(
            brain._executor,
            "execute",
            new=AsyncMock(return_value=ActionResult(success=True, data={})),
        )
    )
    stack.enter_context(
        patch.object(
            brain._composer,
            "compose",
            new=AsyncMock(return_value=reply),
        )
    )
    return stack


def _seed_shipping_kb(db, tenant_id: int, body: str) -> None:
    seed_knowledge_section(
        db,
        tenant_id,
        kind="shipping_zones",
        title="سياسة الشحن",
        body=body,
    )


@pytest.fixture
def db_tenant():
    db, _ = make_scenario_db()
    tenant = seed_tenant(db, name="متجر تجريبي عام")
    return db, tenant


class TestBrainCheckoutShippingGrounding:
    def test_brain_checkout_summary_does_not_invent_shipping_fee(self, db_tenant) -> None:
        db, tenant = db_tenant
        invented = "المجموع 394.50 ريال (شامل شحن توصيل 29 ريال)"
        result = apply_shipping_cost_truth_guard(
            invented,
            db=db,
            tenant_id=tenant.id,
            order_prep={"line_items": [{"product_name": "حذاء رياضي أبيض", "quantity": 1}]},
        )
        assert result.replaced
        assert "29" not in result.reply

    def test_brain_checkout_summary_respects_free_shipping_policy(self, db_tenant) -> None:
        db, tenant = db_tenant
        _seed_shipping_kb(
            db,
            tenant.id,
            "food: شحن مجاني.\nclothing: شحن توصيل 29 ريال.",
        )
        resolution = resolve_checkout_shipping_policy(
            db,
            tenant_id=tenant.id,
            order_prep={
                "line_items": [{"product_name": "عصير برتقال طازج", "quantity": 1}],
            },
        )
        assert resolution.free_shipping is True
        patch = resolution.to_state_patch()
        assert patch.get("free_shipping") is True

    def test_brain_checkout_summary_uses_configured_paid_shipping_fee(self, db_tenant) -> None:
        db, tenant = db_tenant
        _seed_shipping_kb(
            db,
            tenant.id,
            "clothing: شحن توصيل 35 ريال.",
        )
        resolution = resolve_checkout_shipping_policy(
            db,
            tenant_id=tenant.id,
            order_prep={
                "line_items": [{"product_name": "قميص قطني أزرق", "quantity": 1}],
            },
        )
        assert resolution.shipping_fee_sar == 35.0
        assert resolution.free_shipping is False

    def test_brain_checkout_summary_handles_mixed_cart_deterministically(self, db_tenant) -> None:
        db, tenant = db_tenant
        _seed_shipping_kb(
            db,
            tenant.id,
            "food: شحن مجاني.\naccessories: شحن توصيل 29 ريال.",
        )
        resolution = resolve_checkout_shipping_policy(
            db,
            tenant_id=tenant.id,
            order_prep={
                "line_items": [
                    {"product_name": "عطر ورد 100ml", "quantity": 1},
                    {"product_name": "مشروب طاقة", "quantity": 1},
                ],
            },
        )
        assert resolution.merchant_review_required is True

    def test_brain_checkout_summary_unknown_shipping_policy_does_not_invent_fee(self, db_tenant) -> None:
        db, tenant = db_tenant
        result = apply_shipping_cost_truth_guard(
            "شحن توصيل 29 ريال",
            db=db,
            tenant_id=tenant.id,
            order_prep={"line_items": [{"product_name": "منتج عام", "quantity": 1}]},
        )
        assert result.replaced
        assert "29" not in result.reply


class TestPipelineShippingCostTruthGuardDbWiring:
    def test_pipeline_passes_live_db_session_to_shipping_guard(self) -> None:
        from modules.ai.brain.pipeline import get_brain  # noqa: PLC0415

        db, _ = make_scenario_db()
        tenant = seed_tenant(db, name="متجر تجريبي عام")
        _seed_shipping_kb(
            db,
            tenant.id,
            "الشحن للرياض 2-3 أيام عمل — 25 ريال.\nشحن جدة — 35 ريال خلال 4 أيام.",
        )
        verified_reply = "تكلفة الشحن للرياض 25 ريال خلال 2-3 أيام."
        captured: dict = {}
        real_guard = apply_shipping_cost_truth_guard

        def _recording_guard(reply: str, **kwargs):
            captured["db"] = kwargs.get("db")
            result = real_guard(reply, **kwargs)
            captured["replaced"] = result.replaced
            return result

        brain = get_brain()
        stack = _pipeline_shipping_stack(brain, reply=verified_reply)
        with stack:
            with patch(
                "modules.ai.brain.postprocess.shipping_cost_truth_guard.apply_shipping_cost_truth_guard",
                side_effect=_recording_guard,
            ):
                _run(
                    brain.process(
                        db=db,
                        tenant_id=tenant.id,
                        customer_phone="966500000001",
                        message="كم الشحن للرياض؟",
                        history=[],
                        profile={"preferred_language": "ar"},
                        conversation_id=42,
                    )
                )

        assert captured.get("db") is db
        assert captured.get("replaced") is False

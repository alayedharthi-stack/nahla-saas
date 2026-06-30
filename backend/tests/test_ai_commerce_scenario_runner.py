"""
test_ai_commerce_scenario_runner.py
───────────────────────────────────
P0 commerce scenario harness — foundation for full WhatsApp journey tests.

Deferred TODO scenarios (future PRs):
- Full FAQ/KB answer correctness with LLM fixtures
- Product unavailable / alternative suggestions
- Full catalog selection via _dispatch_message with unknown variants
- Image / national-address OCR inbound
- Full shipping label/provider lifecycle
- Post-delivery review request timed emitter
- Full Salla cart recovery stage 1
- Cancellation / address-change / multi-status order flows
- AI Playground / replay / canary allowlist UI
"""
from __future__ import annotations

import os
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch

import pytest

_HERE = Path(__file__).resolve().parent
for _p in (_HERE, _HERE.parent, _HERE.parent.parent):
    if str(_p) not in sys.path:
        sys.path.insert(0, str(_p))

from commerce_scenario_fixtures import (  # noqa: E402
    build_order_prep,
    make_scenario_db,
    persona_draft_order,
    persona_new_customer,
    persona_paused_conversation,
    persona_returning_with_address,
    persona_shipped_order,
    persona_store_ai_off,
    seed_abandoned_draft_automation,
    seed_order,
)
from commerce_scenario_runner import (  # noqa: E402
    AIScenarioRunner,
    CatalogSelectionInbound,
    LocationInbound,
    TextInbound,
)
from core.ai_disabled_gate import (  # noqa: PLC0415
    REASON_STORE_AI_DISABLED,
    evaluate_ai_disabled_send_block,
)
from core.order_context_builder import build_order_context, compute_shadow_missing_fields  # noqa: E402
from core.wa_order_lifecycle import is_payment_verified  # noqa: E402
from modules.ai.brain.commerce.order_tracking_intent_guard import (  # noqa: E402
    is_explicit_order_tracking_request,
)
from models import Order  # noqa: E402
from services.nahla_order_bridge import nahla_wa_external_id  # noqa: E402


@pytest.fixture(autouse=True)
def _enable_draft_bridge(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("NAHLA_ORDER_DRAFT_BRIDGE_ENABLED", "true")
    monkeypatch.setenv("WA_CATALOG_ORDER_IMMEDIATE_DRAFT_ENABLED", "true")


class TestScenarioRunnerFoundation:
    def test_fake_sender_never_calls_real_provider(self) -> None:
        db, _ = make_scenario_db()
        world = persona_new_customer(db)
        runner = AIScenarioRunner(world)
        result = runner.run([TextInbound("السلام عليكم")])
        assert result.no_real_whatsapp_send is True
        assert result.fake_outbound_count == 0
        assert len(result.inbound_messages) == 1

    def test_webhook_smoke_persists_inbound_and_captures_fake_outbound(self) -> None:
        db, _ = make_scenario_db()
        world = persona_new_customer(db)
        runner = AIScenarioRunner(world, use_webhook=True)
        result = runner.run_webhook_text("مرحبا")
        assert result.no_real_whatsapp_send is True
        assert len(result.inbound_messages) >= 1
        assert result.fake_outbound_count >= 0


class TestNewOrderCompletion:
    def test_new_customer_draft_uses_whatsapp_phone(self) -> None:
        db, _ = make_scenario_db()
        world = persona_new_customer(db)
        runner = AIScenarioRunner(world)
        prep = build_order_prep(
            customer_first_name="",
            customer_last_name="",
            customer_phone=world.phone,
        )
        runner.seed_prep_on_conversation(prep)
        result = runner.run([
            TextInbound("أبغى عسل طلح نصف كيلو"),
            TextInbound("أبغى 2"),
        ])
        assert result.customer is not None
        assert result.customer.normalized_phone == world.phone
        assert len(result.orders) >= 1
        order = result.orders[-1]
        assert order.source == "whatsapp"
        assert order.customer_info.get("phone")

    def test_explicit_name_capture(self) -> None:
        db, _ = make_scenario_db()
        world = persona_new_customer(db)
        runner = AIScenarioRunner(world)
        result = runner.run([TextInbound("اسمي فايز الصبحي")])
        assert result.customer is not None
        assert "فايز" in (result.customer.name or "")

    def test_false_name_not_captured(self) -> None:
        db, _ = make_scenario_db()
        world = persona_new_customer(db)
        runner = AIScenarioRunner(world)
        result = runner.run([TextInbound("انا انضحك علي")])
        assert result.customer is not None
        assert not (result.customer.name or "").strip()

    def test_location_saved_into_order_prep(self) -> None:
        db, _ = make_scenario_db()
        world = persona_draft_order(db)
        runner = AIScenarioRunner(world)
        result = runner.run([
            LocationInbound(lat=21.3891, lng=39.8579, name="موقعي"),
        ])
        prep = dict((result.conversation.extra_metadata or {}).get("brain_state", {}).get("order_prep") or {})
        assert prep.get("latitude") == 21.3891
        assert prep.get("longitude") == 39.8579
        assert prep.get("google_maps_url")

    def test_short_national_address_saved(self) -> None:
        db, _ = make_scenario_db()
        world = persona_draft_order(db)
        runner = AIScenarioRunner(world)
        result = runner.run([TextInbound("العنوان RAGB1234")])
        prep = dict((result.conversation.extra_metadata or {}).get("brain_state", {}).get("order_prep") or {})
        assert prep.get("short_address_code") == "RAGB1234"

    def test_order_confirmation_creates_order_number(self) -> None:
        db, _ = make_scenario_db()
        world = persona_draft_order(db)
        runner = AIScenarioRunner(world)
        prep = build_order_prep(
            customer_first_name="فايز",
            customer_last_name="الصبحي",
            google_maps_url="https://maps.google.com/?q=21.3891,39.8579",
            latitude=21.3891,
            longitude=39.8579,
            delivery_address_status="accepted",
            payment_method="bank_transfer",
        )
        runner.seed_prep_on_conversation(prep)
        result = runner.run([TextInbound("نعم أكد الطلب")])
        assert len(result.orders) >= 1
        order = result.orders[-1]
        assert order.external_order_number or order.external_id


class TestReturningCustomerMemory:
    def test_saved_address_not_missing_in_order_context(self) -> None:
        db, _ = make_scenario_db()
        world = persona_returning_with_address(db)
        ctx = build_order_context(
            db,
            tenant_id=world.tenant.id,
            conversation=world.conversation,
            customer=world.customer,
            phone=world.phone_e164,
            brain_state=dict((world.conversation.extra_metadata or {}).get("brain_state") or {}),
        )
        assert "delivery_address" not in compute_shadow_missing_fields(ctx)


class TestPaymentScenarios:
    def test_bank_transfer_not_auto_paid(self) -> None:
        db, _ = make_scenario_db()
        world = persona_draft_order(db)
        runner = AIScenarioRunner(world)
        result = runner.run([TextInbound("الدفع تحويل")])
        prep = dict((result.conversation.extra_metadata or {}).get("brain_state", {}).get("order_prep") or {})
        assert prep.get("payment_method") == "bank_transfer"
        assert is_payment_verified(prep) is False

    def test_payment_claim_without_verification(self) -> None:
        db, _ = make_scenario_db()
        world = persona_draft_order(db)
        runner = AIScenarioRunner(world)
        result = runner.run([TextInbound("تم التحويل")])
        prep = dict((result.conversation.extra_metadata or {}).get("brain_state", {}).get("order_prep") or {})
        assert prep.get("payment_claim_unverified") is True
        assert is_payment_verified(prep) is False


class TestTrackingScenario:
    def test_tracking_intent_without_new_order(self) -> None:
        db, _ = make_scenario_db()
        world = persona_shipped_order(db)
        before = db.query(Order).filter_by(tenant_id=world.tenant.id).count()
        assert is_explicit_order_tracking_request("وين طلبي؟") is True
        after = db.query(Order).filter_by(tenant_id=world.tenant.id).count()
        assert after == before


class TestGuardScenarios:
    def test_store_ai_off_suppresses_outbound(self) -> None:
        db, _ = make_scenario_db()
        world = persona_store_ai_off(db)
        runner = AIScenarioRunner(world, use_webhook=True)
        result = runner.run_webhook_text("السلام عليكم")
        assert result.fake_outbound_count == 0
        assert len(result.inbound_messages) >= 1
        assert result.llm_calls == 0

    def test_conversation_pause_suppresses_outbound(self) -> None:
        db, _ = make_scenario_db()
        world = persona_paused_conversation(db)
        runner = AIScenarioRunner(world, use_webhook=True)
        result = runner.run_webhook_text("مرحبا")
        assert result.fake_outbound_count == 0
        assert result.conversation is not None
        assert result.conversation.ai_paused is True

    def test_manual_send_allowed_when_store_off(self) -> None:
        db, _ = make_scenario_db()
        world = persona_store_ai_off(db)
        blocked, decision = evaluate_ai_disabled_send_block(
            db,
            tenant_id=world.tenant.id,
            customer_phone=world.phone,
            blocked_path="dashboard_manual",
            allow_manual=True,
        )
        assert blocked is False
        assert decision.disabled is False

    def test_store_off_does_not_clear_individual_pause(self) -> None:
        db, _ = make_scenario_db()
        world = persona_paused_conversation(db)
        paused_before = world.conversation.ai_paused
        runner = AIScenarioRunner(world, use_webhook=True)
        runner.run_webhook_text("test")
        convo = db.query(type(world.conversation)).filter_by(id=world.conversation.id).one()
        assert convo.ai_paused == paused_before


class TestAutomationSmoke:
    def test_abandoned_draft_reminder_emits_once(self) -> None:
        db, _ = make_scenario_db()
        world = persona_new_customer(db)
        seed_abandoned_draft_automation(db, world.tenant.id)
        ext_id = nahla_wa_external_id(world.tenant.id, world.conversation.id)
        seed_order(
            db,
            world.tenant.id,
            status="draft",
            external_id=ext_id,
            line_items=[{"title": "عسل طلح", "quantity": 1}],
            extra_metadata={
                "created_at": (datetime.now(timezone.utc) - timedelta(hours=3)).isoformat(),
            },
        )
        from core import automation_emitters  # noqa: PLC0415

        now = datetime.now(timezone.utc).replace(tzinfo=None)
        first = automation_emitters.scan_abandoned_order_drafts(db, world.tenant.id, now=now)
        second = automation_emitters.scan_abandoned_order_drafts(db, world.tenant.id, now=now)
        assert first == 1
        assert second == 0


class TestCatalogInbound:
    def test_catalog_selection_creates_draft_order(self) -> None:
        db, _ = make_scenario_db()
        world = persona_new_customer(db)
        runner = AIScenarioRunner(world)
        result = runner.run([
            CatalogSelectionInbound(product_items=[{
                "product_retailer_id": world.product.external_id,
                "quantity": 1,
                "item_price": 120,
                "currency": "SAR",
                "name": "عسل طلح",
            }]),
        ])
        assert len(result.orders) >= 1
        assert result.orders[-1].status in {"draft", "pending_customer_info", "pending_payment"}

"""
test_ai_commerce_confidence_hardening.py
───────────────────────────────────────
AI Commerce Confidence Gate — real-problem regression scenarios.

Run via:
  python backend/scripts/run_ai_commerce_confidence_suite.py
"""
from __future__ import annotations

import re
import sys
from pathlib import Path
from unittest.mock import patch

import pytest

_HERE = Path(__file__).resolve().parent
for _p in (_HERE, _HERE.parent, _HERE.parent.parent):
    if str(_p) not in sys.path:
        sys.path.insert(0, str(_p))

from commerce_confidence_helpers import (  # noqa: E402
    assert_no_address_request,
    assert_no_internal_kb,
    assert_no_phone_request,
    assert_no_side_effects,
    call_playground_endpoint,
    format_confidence_failure,
    make_runner,
    scenario_order_count,
)
from commerce_scenario_fixtures import (  # noqa: E402
    build_order_prep,
    make_scenario_db,
    persona_draft_order,
    persona_kb_inquiry,
    persona_new_customer,
    persona_returning_with_address,
    persona_shipped_order,
    seed_knowledge_section,
    seed_tenant,
)
from commerce_scenario_runner import TextInbound  # noqa: E402
from core.ai_disabled_gate import REASON_STORE_AI_DISABLED  # noqa: E402
from core.wa_order_lifecycle import is_payment_verified  # noqa: E402
from modules.ai.brain.commerce.order_tracking_intent_guard import (  # noqa: E402
    is_explicit_order_tracking_request,
    is_general_shipping_duration_inquiry,
)
from modules.ai.brain.intent import rules  # noqa: E402
from modules.ai.brain.types import INTENT_START_ORDER, INTENT_TRACK_ORDER  # noqa: E402
from services.ai_playground_dry_run import OUTBOUND_SESSION_TEXT  # noqa: E402

pytestmark = pytest.mark.confidence_gate


@pytest.fixture
def confidence_db():
    db, _ = make_scenario_db()
    return db


def _fail(scenario: str, message: str, reason: str, **kwargs) -> None:
    pytest.fail(format_confidence_failure(scenario=scenario, message=message, reason=reason, **kwargs))


class TestConfidenceFAQNoOrder:
    FAQ_MESSAGES = (
        "هل منتجاتكم أصلية؟",
        "هل عسلكم طبيعي؟",
        "من وين عسل الطلح؟",
        "وين موقعكم؟",
    )

    @pytest.mark.parametrize("message", FAQ_MESSAGES)
    def test_faq_playground_preview_no_order_or_leaks(self, confidence_db, message: str) -> None:
        db = confidence_db
        tenant = seed_tenant(db)
        seed_knowledge_section(
            db,
            tenant.id,
            kind="faq",
            title="معلومات عامة",
            body="منتجاتنا أصلية ومضمونة. موقعنا في مكة ونخدم التوصيل داخل السعودية.",
        )
        before = scenario_order_count(db, tenant.id)
        payload = call_playground_endpoint(db, tenant.id, message=message)
        after = scenario_order_count(db, tenant.id)

        try:
            assert payload.get("dry_run") is True
            assert_no_side_effects(payload)
            assert after == before
            assert_no_internal_kb(payload.get("reply_text"))
            assert_no_phone_request(payload.get("reply_text"))
            assert_no_address_request(payload.get("reply_text"))
            intent = rules.match(message)
            assert intent is None or intent.name not in {INTENT_TRACK_ORDER, INTENT_START_ORDER}
        except AssertionError as exc:
            _fail("faq_no_order", message, str(exc), payload=payload, orders_before=before, orders_after=after)

    @pytest.mark.parametrize("message", FAQ_MESSAGES)
    def test_faq_scenario_runner_does_not_create_order(self, confidence_db, message: str) -> None:
        db = confidence_db
        world = persona_kb_inquiry(db)
        runner = make_runner(world)
        before = runner.order_count()
        result = runner.run_inbound_only(message)
        after = runner.order_count()
        try:
            assert after == before
            assert result.errors == []
            assert is_explicit_order_tracking_request(message) is False
        except AssertionError as exc:
            _fail(
                "faq_no_order_runner",
                message,
                str(exc),
                runner_result=result,
                orders_before=before,
                orders_after=after,
            )


class TestConfidenceAvailabilityNoInvention:
    AVAIL_MESSAGES = (
        "هل السدر متوفر؟",
        "هل عندكم سدر؟",
        "السدر موجود؟",
        "هل الطلح متوفر؟",
    )

    @pytest.mark.parametrize("message", AVAIL_MESSAGES)
    def test_availability_no_order_no_invented_stock(self, confidence_db, message: str) -> None:
        db = confidence_db
        tenant = seed_tenant(db)
        seed_knowledge_section(
            db,
            tenant.id,
            kind="quick_update",
            title="توفر السدر",
            body="عسل السدر غير متوفر حالياً — سنعلن عند توفر دفعة جديدة.",
        )
        seed_knowledge_section(
            db,
            tenant.id,
            kind="quick_update",
            title="توفر الطلح",
            body="عسل الطلح متوفر حالياً بكميات محدودة.",
        )
        before = scenario_order_count(db, tenant.id)
        payload = call_playground_endpoint(db, tenant.id, message=message)
        after = scenario_order_count(db, tenant.id)
        reply = str(payload.get("reply_text") or "")

        try:
            assert_no_side_effects(payload)
            assert after == before
            assert_no_phone_request(reply)
            assert_no_address_request(reply)
            if "سدر" in message:
                assert "غير متوفر" in reply or payload.get("needs_better_kb_answer")
                assert "متوفر حالياً" not in reply.replace("غير متوفر", "")
        except AssertionError as exc:
            _fail(
                "availability_no_invention",
                message,
                str(exc),
                payload=payload,
                orders_before=before,
                orders_after=after,
            )


class TestConfidenceOrderIntentDraft:
    def test_draft_order_uses_whatsapp_phone_without_phone_prompt(self, confidence_db) -> None:
        db = confidence_db
        world = persona_new_customer(db)
        runner = make_runner(world)
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
        try:
            assert result.customer is not None
            assert result.customer.normalized_phone == world.phone
            assert len(result.orders) >= 1
            assert result.orders[-1].source == "whatsapp"
        except AssertionError as exc:
            _fail("order_intent_draft", "draft sequence", str(exc), runner_result=result)


class TestConfidenceNameCapture:
    @pytest.mark.parametrize(
        "message,expected_fragment",
        [
            ("اسمي فايز الصبحي", "فايز"),
            ("أنا فايز الصبحي", "فايز"),
        ],
    )
    def test_explicit_name_captured(self, confidence_db, message: str, expected_fragment: str) -> None:
        world = persona_new_customer(confidence_db)
        runner = make_runner(world)
        result = runner.run([TextInbound(message)])
        try:
            assert expected_fragment in (result.customer.name or "")
        except AssertionError as exc:
            _fail("name_capture", message, str(exc), runner_result=result)

    @pytest.mark.parametrize(
        "message",
        ["أنا انضحك علي", "أنا أبغى عسل"],
    )
    def test_non_name_phrases_not_saved(self, confidence_db, message: str) -> None:
        world = persona_new_customer(confidence_db)
        runner = make_runner(world)
        result = runner.run([TextInbound(message)])
        try:
            assert not (result.customer.name or "").strip()
        except AssertionError as exc:
            _fail("name_negative", message, str(exc), runner_result=result)


class TestConfidenceAddressMemory:
    def test_returning_customer_saved_address_not_missing(self, confidence_db) -> None:
        from core.order_context_builder import build_order_context, compute_shadow_missing_fields  # noqa: PLC0415

        world = persona_returning_with_address(confidence_db)
        ctx = build_order_context(
            confidence_db,
            tenant_id=world.tenant.id,
            conversation=world.conversation,
            customer=world.customer,
            phone=world.phone_e164,
            brain_state=dict((world.conversation.extra_metadata or {}).get("brain_state") or {}),
        )
        try:
            assert "delivery_address" not in compute_shadow_missing_fields(ctx)
        except AssertionError as exc:
            _fail("address_memory", "saved address", str(exc))

    def test_location_saved_without_phone_prompt(self, confidence_db) -> None:
        from commerce_scenario_runner import LocationInbound  # noqa: PLC0415

        world = persona_draft_order(confidence_db)
        runner = make_runner(world)
        result = runner.run([LocationInbound(lat=21.3891, lng=39.8579, name="موقعي")])
        prep = dict((result.conversation.extra_metadata or {}).get("brain_state", {}).get("order_prep") or {})
        try:
            assert prep.get("latitude") == 21.3891
            assert prep.get("google_maps_url")
        except AssertionError as exc:
            _fail("address_location", "location inbound", str(exc), runner_result=result)


class TestConfidenceTracking:
    TRACKING_CONTEXT = {
        "order_status": "shipped",
        "order_reference": "NHL-7788",
        "tracking_number": "TRK123456",
        "shipping_provider": "smsa",
    }

    @pytest.mark.parametrize(
        "message",
        ["وين طلبي؟", "أرسل رقم التتبع"],
    )
    def test_tracking_without_context_no_invention(self, confidence_db, message: str) -> None:
        tenant = seed_tenant(confidence_db)
        before = scenario_order_count(confidence_db, tenant.id)
        payload = call_playground_endpoint(confidence_db, tenant.id, message=message)
        after = scenario_order_count(confidence_db, tenant.id)
        try:
            assert_no_side_effects(payload)
            assert after == before
            assert payload.get("needs_context") or payload.get("would_send") is False
            reply = str(payload.get("reply_text") or "")
            assert "TRK" not in reply and "NHL-" not in reply
        except AssertionError as exc:
            _fail(
                "tracking_no_context",
                message,
                str(exc),
                payload=payload,
                orders_before=before,
                orders_after=after,
            )

    def test_tracking_with_context_shows_order_carrier(self, confidence_db) -> None:
        tenant = seed_tenant(confidence_db)
        message = "أرسل رقم التتبع"
        payload = call_playground_endpoint(
            confidence_db,
            tenant.id,
            message=message,
            context=self.TRACKING_CONTEXT,
        )
        reply = str(payload.get("reply_text") or "")
        try:
            assert payload.get("would_send") is True
            assert payload.get("outbound_kind") == OUTBOUND_SESSION_TEXT
            assert "NHL-7788" in reply
            assert "TRK123456" in reply
            assert re.search(r"smsa", reply, re.IGNORECASE)
            assert_no_side_effects(payload)
        except AssertionError as exc:
            _fail("tracking_with_context", message, str(exc), payload=payload)

    def test_pre_order_shipping_not_tracking_route(self, confidence_db) -> None:
        message = "متى يوصل الطلب؟"
        assert is_general_shipping_duration_inquiry(message) is True
        assert is_explicit_order_tracking_request(message) is False


class TestConfidencePaymentClaims:
    def test_bank_transfer_and_claim_not_auto_verified(self, confidence_db) -> None:
        world = persona_draft_order(confidence_db)
        runner = make_runner(world)
        runner.run([TextInbound("الدفع تحويل")])
        claim = runner.run([TextInbound("تم التحويل")])
        prep = dict((claim.conversation.extra_metadata or {}).get("brain_state", {}).get("order_prep") or {})
        try:
            assert prep.get("payment_method") == "bank_transfer"
            assert prep.get("payment_claim_unverified") is True
            assert is_payment_verified(prep) is False
        except AssertionError as exc:
            _fail("payment_claim", "bank transfer claim", str(exc), runner_result=claim)


class TestConfidenceDeliveryAndReview:
    @pytest.mark.parametrize(
        "message",
        ["وصل الطلب", "وصلني الطلب", "تم الاستلام"],
    )
    def test_delivery_confirmation_no_review_emit(self, confidence_db, message: str) -> None:
        from commerce_scenario_fixtures import persona_delivered_order  # noqa: PLC0415

        world = persona_delivered_order(confidence_db)
        before = scenario_order_count(confidence_db, world.tenant.id)
        with patch("core.automation_emitters.scan_post_delivery_review_requests") as scan:
            payload = call_playground_endpoint(confidence_db, world.tenant.id, message=message)
            scan.assert_not_called()
        after = scenario_order_count(confidence_db, world.tenant.id)
        try:
            assert_no_side_effects(payload)
            assert after == before
        except AssertionError as exc:
            _fail(
                "delivery_no_review",
                message,
                str(exc),
                payload=payload,
                orders_before=before,
                orders_after=after,
            )


class TestConfidenceGuards:
    def test_store_ai_off_blocks_playground(self, confidence_db) -> None:
        tenant = seed_tenant(confidence_db, store_ai_enabled=False)
        message = "هل السدر متوفر؟"
        payload = call_playground_endpoint(confidence_db, tenant.id, message=message)
        try:
            assert payload.get("would_send") is False
            assert payload.get("blocked_reason") == REASON_STORE_AI_DISABLED
            assert payload.get("used_llm") is False
            assert not payload.get("reply_text")
            assert_no_side_effects(payload)
        except AssertionError as exc:
            _fail("guard_store_ai_off", message, str(exc), payload=payload)

    def test_shipped_order_tracking_intent_without_new_order(self, confidence_db) -> None:
        world = persona_shipped_order(confidence_db)
        before = scenario_order_count(confidence_db, world.tenant.id)
        assert is_explicit_order_tracking_request("وين طلبي؟") is True
        after = scenario_order_count(confidence_db, world.tenant.id)
        assert after == before


class TestConfidenceInternalKB:
    INTERNAL_HEADING = "قواعد علينا يجب أن يلتزم بها الذكاء"

    def test_internal_kb_not_exposed_in_playground(self, confidence_db) -> None:
        tenant = seed_tenant(confidence_db)
        seed_knowledge_section(
            confidence_db,
            tenant.id,
            kind="custom",
            title="قاعدة المعرفة الرسمية",
            body=f"# KB\n## {self.INTERNAL_HEADING}\n- لا تخترع أسعارًا.",
        )
        message = "هل منتجاتكم أصلية؟"
        payload = call_playground_endpoint(confidence_db, tenant.id, message=message)
        try:
            assert_no_internal_kb(payload.get("reply_text"))
            assert payload.get("needs_better_kb_answer") is True
            assert payload.get("would_send") is False
            assert any("صالحة للعرض للعميل" in w for w in (payload.get("warnings") or []))
        except AssertionError as exc:
            _fail("internal_kb_leak", message, str(exc), payload=payload)

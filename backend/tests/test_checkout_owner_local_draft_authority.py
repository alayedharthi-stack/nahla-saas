"""Checkout owner + local draft authority regressions (platform-wide)."""
from __future__ import annotations

import os
import sys
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest

_HERE = os.path.dirname(os.path.abspath(__file__))
_BACKEND = os.path.abspath(os.path.join(_HERE, ".."))
if _BACKEND not in sys.path:
    sys.path.insert(0, _BACKEND)

from core.order_creation_evidence import resolve_track_order_fallback  # noqa: E402
from core.local_order_resolver import CustomerOrderContext, LocalOrderSnapshot  # noqa: E402
from modules.ai.brain.postprocess.commerce_reply_quality_guard import (  # noqa: E402
    select_arabic_commerce_fallback,
)
from modules.ai.brain.postprocess.payment_credential_guard import (  # noqa: E402
    apply_payment_credential_guard,
)
from modules.ai.checkout_authority import (  # noqa: E402
    LocalDraftEvidence,
    active_whatsapp_checkout,
    rehydrate_order_prep_patch,
)
from modules.ai.order_flow_v2.enforcement import resolve_order_flow_v2_operational  # noqa: E402
from modules.ai.order_flow_v2.owner import try_handle_order_flow_v2  # noqa: E402
from modules.ai.order_flow_v2.payment import build_payment_bank_mismatch_reply  # noqa: E402
from modules.ai.order_flow_v2.replies import (  # noqa: E402
    build_checkout_order_number_reply,
    build_order_created_reply,
    try_attach_creation_ack_reply,
)

_GENERIC_ITEM = {
    "product_id": "sku-sport-shoe-01",
    "product_name": "حذاء رياضي أبيض",
    "quantity": 1,
    "catalog_price": 249.0,
    "price_source": "whatsapp_catalog",
}

_PERFUME_ITEM = {
    "product_id": "sku-perfume-rose",
    "product_name": "عطر ورد 100ml",
    "quantity": 1,
    "catalog_price": 180.0,
}


@pytest.fixture(autouse=True)
def _v2_shadow(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("ORDER_FLOW_V2_ENABLED", "false")
    monkeypatch.setenv("ORDER_FLOW_V2_SHADOW_ENABLED", "true")
    monkeypatch.setattr("core.config.ORDER_FLOW_V2_ENABLED", False, raising=False)
    monkeypatch.setattr("core.config.ORDER_FLOW_V2_SHADOW_ENABLED", True, raising=False)


def _draft_evidence(*, ref: str = "NHL-1-000099", items=None) -> LocalDraftEvidence:
    return LocalDraftEvidence(
        order_id=88,
        external_id="nahla-wa-1-42",
        external_order_number=ref,
        status="draft",
        line_items=list(items or [_GENERIC_ITEM]),
        total=249.0,
        currency="SAR",
        missing_fields=["customer_name", "city", "delivery_address", "payment_method"],
    )


def _conversation(conv_id: int = 42):
    return SimpleNamespace(
        id=conv_id,
        tenant_id=1,
        extra_metadata={"brain_state": {"order_prep": {}, "cart_items": []}},
    )


def _mock_order_row(ref: str):
    return SimpleNamespace(
        id=88,
        external_order_number=ref,
        external_id="nahla-wa-1-42",
    )


def _resolver_order_ctx(*, ref: str, items=None, order_id: int = 88) -> CustomerOrderContext:
    snap = LocalOrderSnapshot(
        order_id=order_id,
        external_id="nahla-wa-1-42",
        external_order_number=ref,
        status="pending_payment",
        source="whatsapp",
        total="249.00",
        customer_name="أحمد سالم",
        line_items=list(items or [_GENERIC_ITEM]),
    )
    return CustomerOrderContext(
        active_whatsapp_draft=snap,
        latest_open_order=snap,
        latest_paid_order=None,
        latest_shipped_order=None,
        orders_by_priority=[snap],
        selected_order=snap,
        selected_reason="active_whatsapp_draft",
    )


class TestLocalDraftCheckoutAuthority:
    def test_empty_order_prep_active_when_local_draft_exists(self) -> None:
        draft = _draft_evidence()
        patch = rehydrate_order_prep_patch(draft, {}, {})
        assert patch.get("local_draft_authoritative") is True
        assert patch.get("line_items")
        assert active_whatsapp_checkout(patch, {}, draft=draft)

    @patch("core.local_order_resolver.resolve_customer_order_context")
    @patch("modules.ai.order_flow_v2.owner.operational_tuple", return_value=(True, False, "test_mode_canary_enforcement"))
    @patch("modules.ai.order_flow_v2.owner.load_local_draft_evidence")
    @patch("modules.ai.order_flow_v2.owner._load_brain_state")
    def test_active_local_draft_wins_before_brain_on_order_number(
        self, load_state, draft_mock, _op, resolver_mock,
    ) -> None:
        draft_mock.return_value = _draft_evidence(ref="NHL-1-000088")
        resolver_mock.return_value = _resolver_order_ctx(ref="NHL-1-000088")
        db = MagicMock()
        conv = _conversation()
        load_state.return_value = (conv, {"order_prep": {}})
        result = try_handle_order_flow_v2(
            db,
            tenant_id=1,
            customer_phone="966500000001",
            message="كم رقم الطلب",
        )
        assert result.handled is False
        assert result.skip_brain is False
        assert result.reason == "unstructured_requires_brain_semantic_ownership"

    @patch("modules.ai.order_flow_v2.owner.operational_tuple", return_value=(True, False, "test_mode_canary_enforcement"))
    @patch("modules.ai.order_flow_v2.owner._load_brain_state")
    def test_name_like_text_consumed_not_catalog_browse(self, load_state, _op) -> None:
        prep = {
            "order_flow_v2_active": True,
            "catalog_line_items_authoritative": True,
            "line_items": [dict(_GENERIC_ITEM)],
            "city": "الرياض",
            "short_address_code": "RRRD1234",
        }
        conv = _conversation()
        load_state.return_value = (conv, {"order_prep": prep})
        result = try_handle_order_flow_v2(
            MagicMock(),
            tenant_id=1,
            customer_phone="966500000001",
            message="نورة عبدالله",
        )
        assert result.handled is False
        assert result.skip_brain is False
        assert result.reason == "unstructured_requires_brain_semantic_ownership"
        assert result.state_patch.get("customer_first_name") in (None, "")

    @patch("modules.ai.order_flow_v2.owner.operational_tuple", return_value=(True, False, "test_mode_canary_enforcement"))
    @patch("modules.ai.order_flow_v2.owner.load_local_draft_evidence")
    @patch("modules.ai.order_flow_v2.owner._load_brain_state")
    def test_catalog_selection_ack_gets_reply_not_silence(
        self, load_state, draft_mock, _op,
    ) -> None:
        draft_mock.return_value = _draft_evidence()
        prep = {"local_draft_authoritative": True, "line_items": [dict(_GENERIC_ITEM)]}
        conv = _conversation()
        load_state.return_value = (conv, {"order_prep": prep})
        result = try_handle_order_flow_v2(
            MagicMock(),
            tenant_id=1,
            customer_phone="966500000001",
            message="انا اخترت المنتجات",
        )
        assert result.handled is False
        assert result.skip_brain is False
        assert result.reason == "unstructured_requires_brain_semantic_ownership"

    @patch("modules.ai.order_flow_v2.owner.operational_tuple", return_value=(True, False, "test_mode_canary_enforcement"))
    @patch("modules.ai.order_flow_v2.owner._load_brain_state")
    def test_delivery_intent_continues_checkout_not_payment_fallback(self, load_state, _op) -> None:
        prep = {
            "order_flow_v2_active": True,
            "line_items": [dict(_GENERIC_ITEM)],
            "customer_first_name": "أحمد",
            "customer_last_name": "سالم",
            "city": "الرياض",
            "short_address_code": "RRRD1234",
            "delivery_address_status": "accepted",
        }
        conv = _conversation()
        load_state.return_value = (conv, {"order_prep": prep})
        result = try_handle_order_flow_v2(
            MagicMock(),
            tenant_id=1,
            customer_phone="966500000001",
            message="ودوه لعنواني",
        )
        assert result.handled is False
        assert result.skip_brain is False
        assert result.reason == "unstructured_requires_brain_semantic_ownership"

    @patch("modules.ai.checkout_authority.load_local_draft_evidence")
    def test_track_fallback_uses_local_draft_not_no_orders(self, draft_mock) -> None:
        draft_mock.return_value = _draft_evidence(ref="NHL-1-000077")
        reply = resolve_track_order_fallback(
            state=SimpleNamespace(order_prep={}),
            db=MagicMock(),
            tenant_id=1,
            conversation_id=42,
        )
        assert reply
        assert "NHL-1-000077" in reply
        assert "لم أجد" not in reply

    @patch("core.local_order_resolver.resolve_customer_order_context")
    def test_build_checkout_order_number_reply_from_db(self, resolver_mock) -> None:
        resolver_mock.return_value = _resolver_order_ctx(ref="NHL-1-000066")
        db = MagicMock()
        reply = build_checkout_order_number_reply(
            db,
            tenant_id=1,
            conversation=_conversation(),
            order_prep={},
            brain_state={},
        )
        assert "NHL-1-000066" in reply

    def test_creation_ack_only_with_persisted_reference(self) -> None:
        assert build_order_created_reply(reference="") == ""
        ack = build_order_created_reply(reference="NHL-1-000055")
        assert "NHL-1-000055" in ack
        reply, patch = try_attach_creation_ack_reply(
            "تمام، الخطوة التالية؟",
            {"order_creation_status": "created", "draft_order_reference": "NHL-1-000055"},
            reference="NHL-1-000055",
        )
        assert "تم إنشاء طلبك" in reply
        assert patch.get("creation_ack_sent") is True

    def test_commerce_fallback_suppresses_name_during_local_draft(self) -> None:
        state = {
            "order_prep": {
                "local_draft_authoritative": True,
                "line_items": [dict(_GENERIC_ITEM)],
            }
        }
        reply, kind = select_arabic_commerce_fallback(
            inbound_text="نورة عبدالله",
            state=state,
        )
        assert reply == ""
        assert kind == "checkout_name_owned_suppressed"

    @patch("core.local_order_resolver.resolve_customer_order_context")
    @patch("modules.ai.order_flow_v2.owner.operational_tuple", return_value=(True, False, "test_mode_canary_enforcement"))
    @patch("modules.ai.order_flow_v2.owner.load_local_draft_evidence")
    @patch("modules.ai.order_flow_v2.owner._load_brain_state")
    def test_generic_perfume_merchant_draft_not_honey_specific(
        self, load_state, draft_mock, _op, resolver_mock,
    ) -> None:
        draft_mock.return_value = _draft_evidence(
            ref="NHL-1-000044",
            items=[_PERFUME_ITEM],
        )
        resolver_mock.return_value = _resolver_order_ctx(
            ref="NHL-1-000044",
            items=[_PERFUME_ITEM],
        )
        db = MagicMock()
        conv = _conversation()
        load_state.return_value = (conv, {"order_prep": {}})
        result = try_handle_order_flow_v2(
            db,
            tenant_id=1,
            customer_phone="966500000001",
            message="كم رقم الطلب",
        )
        assert result.handled is False
        assert result.skip_brain is False
        assert result.reason == "unstructured_requires_brain_semantic_ownership"


class TestCanaryV2Enforcement:
    def test_test_mode_does_not_force_live_v2(self) -> None:
        with patch("modules.ai.order_flow_v2.enforcement.is_ai_allowed_by_store_mode") as mode:
            from core.ai_disabled_gate import StoreAIModeDecision  # noqa: PLC0415
            from core.tenant import STORE_AI_MODE_TEST  # noqa: PLC0415

            mode.return_value = StoreAIModeDecision(allowed=True, mode=STORE_AI_MODE_TEST)
            with patch("core.billing.has_billing_access", return_value=True):
                decision = resolve_order_flow_v2_operational(
                    MagicMock(),
                    tenant_id=1,
                    customer_phone="966500000001",
                    conversation=_conversation(),
                )
        assert decision.live is False
        assert decision.shadow_log is True
        assert decision.reason == "shadow_only"

    def test_non_allowlisted_test_mode_stays_shadow(self) -> None:
        with patch("modules.ai.order_flow_v2.enforcement.is_ai_allowed_by_store_mode") as mode:
            from core.ai_disabled_gate import (  # noqa: PLC0415
                REASON_STORE_AI_TEST_MODE_NOT_ALLOWED,
                StoreAIModeDecision,
            )
            from core.tenant import STORE_AI_MODE_TEST  # noqa: PLC0415

            mode.return_value = StoreAIModeDecision(
                allowed=False,
                reason=REASON_STORE_AI_TEST_MODE_NOT_ALLOWED,
                mode=STORE_AI_MODE_TEST,
            )
            decision = resolve_order_flow_v2_operational(
                MagicMock(),
                tenant_id=1,
                customer_phone="966509999999",
                conversation=_conversation(),
            )
        assert decision.live is False
        assert decision.shadow_log is True

    @patch("modules.ai.order_flow_v2.owner.operational_tuple", return_value=(False, True, "shadow_only"))
    @patch("modules.ai.order_flow_v2.owner._load_brain_state")
    def test_shadow_only_when_not_handled(self, load_state, _op) -> None:
        prep = {"local_draft_authoritative": True, "line_items": [dict(_GENERIC_ITEM)]}
        conv = _conversation()
        load_state.return_value = (conv, {"order_prep": prep})
        result = try_handle_order_flow_v2(
            MagicMock(),
            tenant_id=1,
            customer_phone="966500000001",
            message="انا اخترت المنتجات",
        )
        assert not result.handled
        assert result.shadow_only


class TestPaymentTruthBound:
    def test_rajhi_without_verified_account_returns_honest_reply(self) -> None:
        db = MagicMock()
        db.query.return_value.filter.return_value.all.return_value = []
        reply = build_payment_bank_mismatch_reply(
            db,
            tenant_id=1,
            rejection_reason="requested_bank_not_enabled",
            requested_bank="rajhi",
        )
        assert "هذه بيانات التحويل" not in reply
        assert "غير مضبوطة" in reply or "تواصل" in reply

    def test_no_fake_iban_in_guarded_reply(self) -> None:
        db = MagicMock()
        db.query.return_value.filter.return_value.all.return_value = [
            SimpleNamespace(
                title="حساب الراجحي",
                body="البنك: الراجحي\nالآيبان: SA1111111111111111111111",
                metadata_json={"bank_brand": "rajhi"},
            )
        ]
        result = apply_payment_credential_guard(
            "رقم الآيبان: SA0380000000608010167519",
            db=db,
            tenant_id=1,
            inbound_text="الراجحي",
            requested_bank="rajhi",
        )
        assert "SA0380000000608010167519" not in (result.reply or "")

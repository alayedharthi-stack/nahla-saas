"""Follow-up regressions for checkout payment/name/city/delivery owner (#419 canary)."""
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

from modules.ai.checkout_authority import (  # noqa: E402
    brain_payment_paths_should_defer_to_checkout_owner,
)
from modules.ai.order_flow_v2.owner import try_handle_order_flow_v2  # noqa: E402
from modules.ai.order_flow_v2.payment import (  # noqa: E402
    build_payment_bank_mismatch_reply,
    requested_bank_brand,
)
from modules.ai.order_flow_v2.slot_ownership import (  # noqa: E402
    apply_explicit_name_override,
    apply_slot_ownership,
    is_explicit_customer_name_turn,
)
from modules.ai.order_flow_v2.triggers import is_short_product_keyword_in_order_flow  # noqa: E402

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


def _conversation(conv_id: int = 2868):
    return SimpleNamespace(
        id=conv_id,
        tenant_id=33,
        extra_metadata={
            "brain_state": {
                "order_prep": {
                    "local_draft_authoritative": True,
                    "order_flow_v2_active": True,
                    "line_items": [_GENERIC_ITEM],
                    "draft_order_reference": "NHL-33-000045",
                },
                "cart_items": [_GENERIC_ITEM],
            }
        },
    )


def _active_checkout_prep(**extra):
    prep = {
        "local_draft_authoritative": True,
        "order_flow_v2_active": True,
        "catalog_line_items_authoritative": True,
        "line_items": [dict(_GENERIC_ITEM)],
        "customer_first_name": "أم",
        "customer_last_name": "خالد",
        "city": "الطائف",
        "address_line": "TAPB3320، 3320 ابن تميرة، حي الحلقة الغربية، الطائف",
        "draft_order_reference": "NHL-33-000045",
    }
    prep.update(extra)
    return prep


@patch("modules.ai.order_flow_v2.owner.operational_tuple", return_value=(True, False, "test_mode_canary"))
@patch("modules.ai.order_flow_v2.owner.load_local_draft_evidence", return_value=None)
@patch("modules.ai.order_flow_v2.owner._load_brain_state")
class TestCheckoutPaymentNameSlotFollowup:
    def test_active_checkout_bank_brand_owned_by_payment_truth_not_webhook_override(
        self, load_state, _draft, _op,
    ) -> None:
        prep = _active_checkout_prep(short_address_code="TAPB3320")
        conv = _conversation()
        load_state.return_value = (conv, {"order_prep": prep, "cart_items": prep["line_items"]})
        db = MagicMock()
        with patch(
            "modules.ai.order_flow_v2.owner.apply_payment_method_selection",
            return_value=({"order_flow_v2_payment_rejected": True, "requested_bank": "rajhi"}, None),
        ):
            with patch(
                "modules.ai.order_flow_v2.owner.build_payment_bank_mismatch_reply",
                return_value="بيانات الراجحي غير مفعّلة حاليًا، تواصل مع المتجر لتأكيد بيانات التحويل.",
            ) as mismatch:
                result = try_handle_order_flow_v2(
                    db,
                    tenant_id=33,
                    customer_phone="966507283619",
                    message="الراجحي",
                )
        assert result.handled
        assert result.skip_brain
        assert result.reason in {"payment_bank_rejected", "checkout_payment_bank_unavailable"}
        mismatch.assert_called_once()
        assert "هذه بيانات التحويل" not in (result.reply or "")

    def test_rajhi_no_media_no_account_does_not_claim_transfer_details(self, load_state, _draft, _op) -> None:
        db = MagicMock()
        db.query.return_value.filter.return_value.all.return_value = []
        reply = build_payment_bank_mismatch_reply(
            db,
            tenant_id=33,
            rejection_reason="requested_bank_not_enabled",
            requested_bank="rajhi",
        )
        assert "هذه بيانات التحويل" not in reply
        assert "الراجحي" in reply
        assert "تواصل" in reply

    def test_payment_claim_requires_actual_media_or_credential_send(self, load_state, _draft, _op) -> None:
        conv = _conversation()
        conv.extra_metadata["brain_state"]["order_prep"] = _active_checkout_prep()
        assert brain_payment_paths_should_defer_to_checkout_owner(
            MagicMock(), tenant_id=33, conversation=conv,
        )

    def test_name_like_current_turn_overrides_stale_customer_name_in_local_draft(
        self, load_state, _draft, _op,
    ) -> None:
        prep = _active_checkout_prep()
        patch, reason = apply_explicit_name_override(
            message="سعدية الحارثي",
            order_prep=prep,
            checkout_active=True,
        )
        assert reason == "explicit_name_override"
        assert patch["customer_first_name"] == "سعدية"
        assert patch["customer_last_name"] == "الحارثي"
        assert is_explicit_customer_name_turn("سعدية الحارثي")

    @patch("modules.ai.order_flow_v2.owner._sync_draft_order")
    def test_name_slot_persists_to_order_db_and_order_prep(
        self, sync_draft, load_state, _draft, _op,
    ) -> None:
        prep = _active_checkout_prep()
        conv = _conversation()
        load_state.return_value = (conv, {"order_prep": prep, "cart_items": prep["line_items"]})
        result = try_handle_order_flow_v2(
            MagicMock(),
            tenant_id=33,
            customer_phone="966507283619",
            message="سعدية الحارثي",
        )
        assert result.handled
        merged = {**prep, **result.state_patch}
        assert merged.get("customer_first_name") == "سعدية"
        assert merged.get("customer_last_name") == "الحارثي"

    def test_city_text_during_active_checkout_not_product_keyword(
        self, load_state, _draft, _op,
    ) -> None:
        assert not is_short_product_keyword_in_order_flow("الطايف")
        patch, reason = apply_slot_ownership(
            message="الطايف",
            order_prep=_active_checkout_prep(city=""),
            missing_fields=["city", "delivery_address", "payment_method"],
            checkout_active=True,
        )
        assert reason == "active_checkout_city_owned"
        assert patch.get("city")

    def test_delivery_continuation_with_address_advances_to_payment_method(
        self, load_state, _draft, _op,
    ) -> None:
        prep = _active_checkout_prep(short_address_code="TAPB3320")
        conv = _conversation()
        load_state.return_value = (conv, {"order_prep": prep, "cart_items": prep["line_items"]})
        result = try_handle_order_flow_v2(
            MagicMock(),
            tenant_id=33,
            customer_phone="966507283619",
            message="ودوه لعنواني",
        )
        assert result.handled
        assert result.reason == "delivery_continuation"
        assert "طريقة الدفع" in (result.reply or "") or "الدفع" in (result.reply or "")
        assert "TAPB3320" not in (result.reply or "") or "عنوان" not in (result.reply or "").split("؟")[0]

    def test_no_brain_payment_fallback_when_local_draft_authoritative(
        self, load_state, _draft, _op,
    ) -> None:
        conv = _conversation()
        assert brain_payment_paths_should_defer_to_checkout_owner(
            MagicMock(), tenant_id=33, conversation=conv,
        )
        prep = _active_checkout_prep(short_address_code="TAPB3320")
        load_state.return_value = (conv, {"order_prep": prep})
        result = try_handle_order_flow_v2(
            MagicMock(),
            tenant_id=33,
            customer_phone="966507283619",
            message="الراجحي",
        )
        assert result.handled
        assert result.skip_brain

    def test_replay_run_20260703T004200Z_canary_failures(
        self, load_state, _draft, _op,
    ) -> None:
        """Replay the four post-#419 failures from run 20260703T004200Z."""
        conv = _conversation()
        db = MagicMock()

        prep_city = _active_checkout_prep(city="", customer_first_name="", customer_last_name="")
        load_state.return_value = (conv, {"order_prep": prep_city, "cart_items": prep_city["line_items"]})
        city_result = try_handle_order_flow_v2(
            db, tenant_id=33, customer_phone="966507283619", message="الطايف",
        )
        assert city_result.handled
        assert city_result.reason != "order_flow_product_keyword"

        prep_name = _active_checkout_prep()
        load_state.return_value = (conv, {"order_prep": prep_name, "cart_items": prep_name["line_items"]})
        name_result = try_handle_order_flow_v2(
            db, tenant_id=33, customer_phone="966507283619", message="سعدية الحارثي",
        )
        assert name_result.handled
        assert name_result.state_patch.get("customer_first_name") == "سعدية"

        prep_delivery = _active_checkout_prep(short_address_code="TAPB3320")
        load_state.return_value = (conv, {"order_prep": prep_delivery, "cart_items": prep_delivery["line_items"]})
        delivery_result = try_handle_order_flow_v2(
            db, tenant_id=33, customer_phone="966507283619", message="ودوه لعنواني",
        )
        assert delivery_result.handled
        assert "الدفع" in (delivery_result.reply or "")

        prep_bank = _active_checkout_prep(short_address_code="TAPB3320")
        load_state.return_value = (conv, {"order_prep": prep_bank, "cart_items": prep_bank["line_items"]})
        with patch(
            "modules.ai.order_flow_v2.owner.apply_payment_method_selection",
            return_value=({"order_flow_v2_payment_rejected": True, "requested_bank": "rajhi"}, None),
        ):
            with patch(
                "modules.ai.order_flow_v2.owner.build_payment_bank_mismatch_reply",
                return_value="بيانات الراجحي غير مفعّلة حاليًا، تواصل مع المتجر لتأكيد بيانات التحويل.",
            ):
                bank_result = try_handle_order_flow_v2(
                    db, tenant_id=33, customer_phone="966507283619", message="الراجحي",
                )
        assert bank_result.handled
        assert bank_result.skip_brain
        assert requested_bank_brand("الراجحي") == "rajhi"
        assert "هذه بيانات التحويل" not in (bank_result.reply or "")


class TestGenericMerchantScenarios:
    @patch("modules.ai.order_flow_v2.owner.operational_tuple", return_value=(True, False, "test_mode"))
    @patch("modules.ai.order_flow_v2.owner.load_local_draft_evidence", return_value=None)
    @patch("modules.ai.order_flow_v2.owner._load_brain_state")
    def test_perfume_merchant_name_override_generic(
        self, load_state, _draft, _op,
    ) -> None:
        prep = {
            "local_draft_authoritative": True,
            "order_flow_v2_active": True,
            "line_items": [_PERFUME_ITEM],
            "customer_first_name": "زائر",
            "customer_last_name": "قديم",
            "city": "الرياض",
            "short_address_code": "RIYD1234",
        }
        conv = SimpleNamespace(id=99, tenant_id=1, extra_metadata={"brain_state": {"order_prep": prep}})
        load_state.return_value = (conv, {"order_prep": prep, "cart_items": prep["line_items"]})
        result = try_handle_order_flow_v2(
            MagicMock(),
            tenant_id=1,
            customer_phone="966500000099",
            message="نورة عبدالله",
        )
        assert result.handled
        assert result.state_patch.get("customer_first_name") == "نورة"
        assert result.state_patch.get("customer_last_name") == "عبدالله"

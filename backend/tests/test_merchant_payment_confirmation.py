"""Tests for PR-2B merchant manual bank-transfer confirmation."""
from __future__ import annotations

import os
import sys
from types import SimpleNamespace

import pytest

_HERE = os.path.dirname(os.path.abspath(__file__))
_BACKEND = os.path.abspath(os.path.join(_HERE, ".."))
if _BACKEND not in sys.path:
    sys.path.insert(0, _BACKEND)

from core.merchant_payment_confirmation import (  # noqa: E402
    ADDRESS_MISSING_MERCHANT_NOTICE,
    apply_merchant_payment_confirmation,
    can_merchant_confirm_bank_transfer,
    can_show_confirm_bank_transfer_button,
)
from core.order_payment_policy import (  # noqa: E402
    PAYMENT_METHOD_BANK_TRANSFER,
    PAYMENT_STATUS_PAID,
    PAYMENT_STATUS_PENDING_VERIFICATION,
)
from routers.orders import _serialise_order  # noqa: E402


def _order(**kwargs):
    defaults = {
        "id": 501,
        "tenant_id": 33,
        "status": "payment_submitted",
        "customer_info": {},
        "extra_metadata": {
            "payment_method": PAYMENT_METHOD_BANK_TRANSFER,
            "payment_confirmed": False,
            "payment_status": PAYMENT_STATUS_PENDING_VERIFICATION,
            "google_maps_url": "https://maps.google.com/?q=24.7,46.6",
        },
    }
    defaults.update(kwargs)
    meta = defaults.pop("extra_metadata", {})
    defaults["extra_metadata"] = meta
    return SimpleNamespace(**defaults)


class TestMerchantPaymentConfirmation:
    def test_eligible_bank_transfer_submitted(self) -> None:
        ok, reason = can_merchant_confirm_bank_transfer(_order())
        assert ok is True
        assert reason == "eligible"

    def test_confirm_with_address_becomes_paid(self) -> None:
        order = _order()
        result = apply_merchant_payment_confirmation(order, verified_by="staff@shop.com")
        assert result["status"] == "paid"
        assert result["payment_confirmed"] is True
        assert result["payment_status"] == PAYMENT_STATUS_PAID
        assert order.extra_metadata["payment_verification_source"] == "merchant_manual"
        assert order.extra_metadata["payment_verified_by"] == "staff@shop.com"
        assert order.extra_metadata["payment_verified_at"]
        timeline = order.extra_metadata["payment_timeline"]
        assert timeline[-1]["event"] == "payment_confirmed"
        assert timeline[-1]["source"] == "merchant_manual"

    def test_confirm_without_address_keeps_pending_customer_info(self) -> None:
        order = _order(extra_metadata={
            "payment_method": PAYMENT_METHOD_BANK_TRANSFER,
            "payment_confirmed": False,
            "payment_status": PAYMENT_STATUS_PENDING_VERIFICATION,
        })
        result = apply_merchant_payment_confirmation(order, verified_by="staff@shop.com")
        assert result["status"] == "pending_customer_info"
        assert result["payment_confirmed"] is True
        assert result["merchant_notice"] == ADDRESS_MISSING_MERCHANT_NOTICE
        assert "delivery_address" in order.extra_metadata["missing_fields"]

    def test_not_bank_transfer_blocked(self) -> None:
        order = _order(extra_metadata={
            "payment_method": "cash_on_delivery",
            "payment_confirmed": False,
        })
        ok, reason = can_merchant_confirm_bank_transfer(order)
        assert ok is False
        assert reason == "not_bank_transfer"

    def test_cancelled_blocked(self) -> None:
        order = _order(status="cancelled")
        ok, reason = can_merchant_confirm_bank_transfer(order)
        assert ok is False
        assert reason.startswith("status_not_eligible")

    def test_completed_blocked(self) -> None:
        order = _order(status="completed")
        ok, _ = can_merchant_confirm_bank_transfer(order)
        assert ok is False

    def test_already_confirmed_idempotent(self) -> None:
        order = _order(
            status="paid",
            extra_metadata={
                "payment_method": PAYMENT_METHOD_BANK_TRANSFER,
                "payment_confirmed": True,
                "payment_status": PAYMENT_STATUS_PAID,
            },
        )
        ok, reason = can_merchant_confirm_bank_transfer(order)
        assert ok is True
        assert reason == "already_confirmed"
        result = apply_merchant_payment_confirmation(order, verified_by="staff@shop.com")
        assert result["idempotent"] is True

    def test_show_button_only_when_eligible(self) -> None:
        assert can_show_confirm_bank_transfer_button(_order()) is True
        assert can_show_confirm_bank_transfer_button(_order(status="paid", extra_metadata={
            "payment_method": PAYMENT_METHOD_BANK_TRANSFER,
            "payment_confirmed": True,
            "payment_status": PAYMENT_STATUS_PAID,
        })) is False

    def test_serialization_exposes_confirm_flag(self) -> None:
        from datetime import datetime, timezone

        order = _order(
            external_id="nahla-wa-33-9063",
            external_order_number="NHL-33-000001",
            total="100.00 ر.س",
            customer_name="Test",
            source="whatsapp",
            checkout_url=None,
            line_items=[{"title": "Honey", "quantity": 1}],
        )
        payload = _serialise_order(
            order,
            customer_lookup={},
            now=datetime.now(timezone.utc),
            detailed=True,
        )
        assert payload["can_confirm_bank_transfer"] is True
        assert payload["merchant_payment_alert"] is not None

    def test_moyasar_not_confirmable_via_manual(self) -> None:
        order = _order(
            status="paid",
            extra_metadata={
                "payment_method": "moyasar",
                "payment_provider": "moyasar",
                "payment_provider_status": "paid",
                "payment_confirmed": True,
            },
        )
        ok, reason = can_merchant_confirm_bank_transfer(order)
        assert ok is False
        assert reason == "not_bank_transfer"

    def test_draft_not_confirmable(self) -> None:
        order = _order(status="draft")
        ok, reason = can_merchant_confirm_bank_transfer(order)
        assert ok is False
        assert "draft" in reason

    def test_apply_raises_when_not_allowed(self) -> None:
        order = _order(status="cancelled")
        with pytest.raises(ValueError, match="status_not_eligible"):
            apply_merchant_payment_confirmation(order, verified_by="x")

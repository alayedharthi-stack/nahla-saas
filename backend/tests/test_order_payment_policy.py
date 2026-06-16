"""Tests for order payment policy (PR-2 bank transfer / COD / provider prep)."""
from __future__ import annotations

import os
import sys
from datetime import datetime, timezone
from types import SimpleNamespace

import pytest

_HERE = os.path.dirname(os.path.abspath(__file__))
_BACKEND = os.path.abspath(os.path.join(_HERE, ".."))
if _BACKEND not in sys.path:
    sys.path.insert(0, _BACKEND)

from core.order_payment_policy import (  # noqa: E402
    BANK_TRANSFER_MERCHANT_ALERT,
    PAYMENT_METHOD_BANK_TRANSFER,
    PAYMENT_METHOD_CASH_ON_DELIVERY,
    PAYMENT_METHOD_MOYASAR,
    PAYMENT_STATUS_COD_PENDING,
    PAYMENT_STATUS_PENDING_VERIFICATION,
    build_merchant_payment_alert,
    can_create_shipment,
    enrich_order_payment_metadata,
    guard_wa_target_status,
    is_provider_payment_confirmed,
)
from routers.orders import _serialise_order  # noqa: E402


class TestBankTransferPolicy:
    def test_receipt_submission_metadata(self) -> None:
        meta = enrich_order_payment_metadata(
            {"payment_receipt_received": True},
            order_prep={"payment_receipt_received": True},
            target_status="payment_submitted",
        )
        assert meta["payment_method"] == PAYMENT_METHOD_BANK_TRANSFER
        assert meta["payment_status"] == PAYMENT_STATUS_PENDING_VERIFICATION
        assert meta["payment_confirmed"] is False

    def test_no_paid_without_confirm(self) -> None:
        blocked = guard_wa_target_status(
            "paid",
            {"payment_receipt_received": True, "payment_confirmed": False},
        )
        assert blocked == "payment_submitted"

    def test_no_processing_without_confirm(self) -> None:
        blocked = guard_wa_target_status(
            "processing",
            {"payment_submission_received": True},
        )
        assert blocked == "payment_submitted"

    def test_no_ready_to_ship_without_confirm(self) -> None:
        blocked = guard_wa_target_status(
            "ready_to_ship",
            {"payment_receipt_received": True},
        )
        assert blocked == "payment_submitted"

    def test_paid_allowed_with_confirm(self) -> None:
        allowed = guard_wa_target_status(
            "paid",
            {"payment_confirmed": True, "payment_receipt_received": True},
        )
        assert allowed == "paid"

    def test_merchant_red_alert(self) -> None:
        alert = build_merchant_payment_alert(
            raw_status="payment_submitted",
            meta={
                "payment_method": PAYMENT_METHOD_BANK_TRANSFER,
                "payment_confirmed": False,
            },
        )
        assert alert is not None
        assert alert["level"] == "red"
        assert BANK_TRANSFER_MERCHANT_ALERT in alert["label"]

    def test_no_bank_alert_when_confirmed(self) -> None:
        alert = build_merchant_payment_alert(
            raw_status="payment_submitted",
            meta={
                "payment_method": PAYMENT_METHOD_BANK_TRANSFER,
                "payment_confirmed": True,
            },
        )
        assert alert is None

    def test_no_shipment_without_bank_confirm(self) -> None:
        assert can_create_shipment(
            order_status="paid",
            meta={"payment_method": PAYMENT_METHOD_BANK_TRANSFER, "payment_confirmed": False},
        ) is False

    def test_shipment_after_bank_confirm(self) -> None:
        assert can_create_shipment(
            order_status="paid",
            meta={"payment_method": PAYMENT_METHOD_BANK_TRANSFER, "payment_confirmed": True},
        ) is True


class TestProviderAndCod:
    def test_moyasar_requires_provider_status(self) -> None:
        assert is_provider_payment_confirmed({
            "payment_provider": PAYMENT_METHOD_MOYASAR,
            "payment_provider_status": "paid",
            "payment_confirmed": True,
        })
        assert not is_provider_payment_confirmed({
            "payment_provider": PAYMENT_METHOD_MOYASAR,
            "payment_confirmed": True,
        })
        assert not is_provider_payment_confirmed({
            "payment_provider": PAYMENT_METHOD_MOYASAR,
            "payment_provider_status": "paid",
            "payment_confirmed": False,
        })

    def test_no_bank_alert_for_moyasar_confirmed(self) -> None:
        alert = build_merchant_payment_alert(
            raw_status="paid",
            meta={
                "payment_method": PAYMENT_METHOD_MOYASAR,
                "payment_provider": PAYMENT_METHOD_MOYASAR,
                "payment_provider_status": "paid",
                "payment_confirmed": True,
            },
        )
        assert alert is None

    def test_cod_not_paid(self) -> None:
        meta = enrich_order_payment_metadata(
            {},
            order_prep={"payment_method": PAYMENT_METHOD_CASH_ON_DELIVERY},
            target_status="cod_pending",
        )
        assert meta["payment_method"] == PAYMENT_METHOD_CASH_ON_DELIVERY
        assert meta["payment_status"] == PAYMENT_STATUS_COD_PENDING
        assert meta["payment_confirmed"] is False
        assert guard_wa_target_status("paid", {"payment_method": PAYMENT_METHOD_CASH_ON_DELIVERY}) == "cod_pending"

    def test_cod_alert_is_blue_not_red(self) -> None:
        alert = build_merchant_payment_alert(
            raw_status="cod_pending",
            meta={"payment_method": PAYMENT_METHOD_CASH_ON_DELIVERY},
        )
        assert alert is not None
        assert alert["level"] == "blue"

    def test_cod_shipment_when_enabled(self) -> None:
        assert can_create_shipment(
            order_status="cod_pending",
            meta={"payment_method": PAYMENT_METHOD_CASH_ON_DELIVERY},
            cod_enabled=True,
        )

    def test_cod_shipment_blocked_when_disabled(self) -> None:
        assert not can_create_shipment(
            order_status="cod_pending",
            meta={"payment_method": PAYMENT_METHOD_CASH_ON_DELIVERY},
            cod_enabled=False,
        )


class TestOrdersApiSerialization:
    def test_list_includes_merchant_alert(self) -> None:
        order = SimpleNamespace(
            id=1,
            tenant_id=33,
            external_id="nahla-wa-33-9063",
            external_order_number="NHL-33-000001",
            status="payment_submitted",
            total="100.00 ر.س",
            customer_name="Test",
            customer_info={"phone": "966551308005"},
            line_items=[{"title": "Honey", "quantity": 1}],
            checkout_url=None,
            source="whatsapp",
            is_abandoned=False,
            extra_metadata={
                "payment_method": PAYMENT_METHOD_BANK_TRANSFER,
                "payment_confirmed": False,
                "payment_status": PAYMENT_STATUS_PENDING_VERIFICATION,
                "created_at": datetime.now(timezone.utc).isoformat(),
            },
        )
        payload = _serialise_order(
            order,
            customer_lookup={},
            now=datetime.now(timezone.utc),
        )
        assert payload["merchant_payment_alert"] is not None
        assert payload["merchant_payment_alert"]["level"] == "red"
        assert any(
            a["key"] == "bank_transfer_verify_before_ship"
            for a in payload["needs_action"]
        )
        assert payload["payment_method_label"] == "تحويل بنكي"
        assert payload["raw_status_label"] == "دفع مرسل — يحتاج تحقق"

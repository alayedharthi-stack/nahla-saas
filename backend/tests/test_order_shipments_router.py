"""Service-level tests for internal shipment create + label endpoints."""
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

from core.order_payment_policy import (  # noqa: E402
    ORDER_STATUS_LABEL_GENERATED,
    ORDER_STATUS_SHIPMENT_CREATED,
    PAYMENT_METHOD_BANK_TRANSFER,
)
from core.order_shipment_service import (  # noqa: E402
    create_order_shipment,
    evaluate_create_shipment,
    generate_shipment_label,
)
from core.order_shipping_policy import (  # noqa: E402
    MSG_BANK_TRANSFER_NOT_CONFIRMED,
    SHIPMENT_STATUS_CREATED,
    SHIPMENT_STATUS_LABEL_GENERATED,
)


def _eligible_order(**overrides):
    base = dict(
        id=42,
        tenant_id=7,
        status="paid",
        total="120.00 ر.س",
        customer_name="سارة",
        customer_info={"phone": "966511111111"},
        extra_metadata={
            "payment_method": PAYMENT_METHOD_BANK_TRANSFER,
            "payment_confirmed": True,
            "google_maps_url": "https://maps.google.com/?q=24.7,46.6",
        },
    )
    base.update(overrides)
    return SimpleNamespace(**base)


class TestEvaluateCreateShipment:
    def test_ineligible_bank_transfer(self) -> None:
        order = _eligible_order(
            extra_metadata={
                "payment_method": PAYMENT_METHOD_BANK_TRANSFER,
                "payment_confirmed": False,
                "google_maps_url": "https://maps.google.com/?q=24.7,46.6",
            },
        )
        gate = evaluate_create_shipment(order, cod_enabled=True)
        assert not gate.allowed
        assert gate.message_ar == MSG_BANK_TRANSFER_NOT_CONFIRMED


class TestCreateOrderShipmentService:
    def test_creates_shipment_for_eligible_order(self) -> None:
        order = _eligible_order()
        db = MagicMock()

        with patch("core.order_shipment_service.get_order_shipment", return_value=None):
            shipment, payload = create_order_shipment(
                db,
                tenant_id=7,
                order=order,
                verified_by="staff:test",
            )

        assert shipment.status == SHIPMENT_STATUS_CREATED
        assert order.status == ORDER_STATUS_SHIPMENT_CREATED
        assert payload["provider"] == "internal"
        assert payload["status"] == SHIPMENT_STATUS_CREATED
        db.add.assert_called()

    def test_blocks_ineligible_order(self) -> None:
        order = _eligible_order(status="payment_submitted")
        db = MagicMock()

        with patch("core.order_shipment_service.get_order_shipment", return_value=None):
            with pytest.raises(ValueError, match="payment_submitted"):
                create_order_shipment(
                    db,
                    tenant_id=7,
                    order=order,
                    verified_by="staff:test",
                )

    def test_blocks_duplicate_shipment(self) -> None:
        order = _eligible_order()
        existing = SimpleNamespace(id=99, status=SHIPMENT_STATUS_CREATED)
        db = MagicMock()

        with patch("core.order_shipment_service.get_order_shipment", return_value=existing):
            with pytest.raises(ValueError, match="shipment_exists"):
                create_order_shipment(
                    db,
                    tenant_id=7,
                    order=order,
                    verified_by="staff:test",
                )


class TestGenerateShipmentLabelService:
    def test_generates_placeholder_label(self) -> None:
        order = _eligible_order(status=ORDER_STATUS_SHIPMENT_CREATED)
        shipment = SimpleNamespace(
            id=5,
            order_id=42,
            status=SHIPMENT_STATUS_CREATED,
            provider="internal",
            tracking_number=None,
            label_url=None,
            label_pdf_path=None,
            recipient_name="سارة",
            recipient_phone="966511111111",
            address_type="maps_url",
            address_text=None,
            address_url="https://maps.google.com/?q=24.7,46.6",
            latitude=None,
            longitude=None,
            cod_amount=None,
            created_at=None,
            updated_at=None,
            extra_metadata={},
        )
        db = MagicMock()

        payload = generate_shipment_label(
            db,
            tenant_id=7,
            order=order,
            shipment=shipment,
            verified_by="staff:test",
        )

        assert shipment.status == SHIPMENT_STATUS_LABEL_GENERATED
        assert order.status == ORDER_STATUS_LABEL_GENERATED
        assert payload["status"] == SHIPMENT_STATUS_LABEL_GENERATED
        assert payload["label_placeholder"] is True
        assert payload["label_url"]

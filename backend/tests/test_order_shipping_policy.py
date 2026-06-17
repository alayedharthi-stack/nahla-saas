"""Tests for order shipping policy (shipment + label eligibility gates)."""
from __future__ import annotations

import os
import sys
from types import SimpleNamespace

import pytest

_HERE = os.path.dirname(os.path.abspath(__file__))
_BACKEND = os.path.abspath(os.path.join(_HERE, ".."))
if _BACKEND not in sys.path:
    sys.path.insert(0, _BACKEND)

from core.order_payment_policy import (  # noqa: E402
    PAYMENT_METHOD_BANK_TRANSFER,
    PAYMENT_METHOD_CASH_ON_DELIVERY,
    PAYMENT_STATUS_COD_PENDING,
)
from core.order_shipping_policy import (  # noqa: E402
    MSG_ADDRESS_MISSING,
    MSG_BANK_TRANSFER_NOT_CONFIRMED,
    MSG_COD_DISABLED,
    MSG_ORDER_STATUS_BLOCKED,
    MSG_SHIPMENT_EXISTS,
    SHIPMENT_STATUS_CREATED,
    SHIPMENT_STATUS_LABEL_GENERATED,
    can_create_shipment,
    can_generate_label,
    shipping_block_reason,
)


def _order(**overrides):
    base = dict(
        id=1,
        status="paid",
        customer_name="عميل",
        customer_info={"phone": "966500000000"},
        extra_metadata={
            "payment_method": PAYMENT_METHOD_BANK_TRANSFER,
            "payment_confirmed": True,
            "google_maps_url": "https://maps.google.com/?q=21.4,39.8",
        },
    )
    base.update(overrides)
    return SimpleNamespace(**base)


def _shipment(**overrides):
    base = dict(id=10, status=SHIPMENT_STATUS_CREATED, order_id=1)
    base.update(overrides)
    return SimpleNamespace(**base)


class TestCanCreateShipment:
    def test_bank_transfer_unconfirmed_blocks(self) -> None:
        order = _order(
            status="paid",
            extra_metadata={
                "payment_method": PAYMENT_METHOD_BANK_TRANSFER,
                "payment_confirmed": False,
                "google_maps_url": "https://maps.google.com/?q=21.4,39.8",
            },
        )
        result = can_create_shipment(order)
        assert not result.allowed
        assert result.reason_key == "bank_transfer_not_confirmed"
        assert result.message_ar == MSG_BANK_TRANSFER_NOT_CONFIRMED

    def test_bank_transfer_confirmed_with_address_allows(self) -> None:
        order = _order(
            status="paid",
            extra_metadata={
                "payment_method": PAYMENT_METHOD_BANK_TRANSFER,
                "payment_confirmed": True,
                "google_maps_url": "https://maps.google.com/?q=21.4,39.8",
            },
        )
        assert can_create_shipment(order).allowed

    def test_payment_submitted_blocks(self) -> None:
        order = _order(status="payment_submitted")
        result = can_create_shipment(order)
        assert not result.allowed
        assert result.reason_key == "payment_submitted"

    def test_pending_customer_info_blocks(self) -> None:
        order = _order(status="pending_customer_info")
        result = can_create_shipment(order)
        assert not result.allowed
        assert result.reason_key == "pending_customer_info"
        assert result.message_ar == MSG_ADDRESS_MISSING

    def test_cod_enabled_with_address_allows(self) -> None:
        order = _order(
            status="cod_pending",
            extra_metadata={
                "payment_method": PAYMENT_METHOD_CASH_ON_DELIVERY,
                "payment_status": PAYMENT_STATUS_COD_PENDING,
                "short_address_code": "RIYD1234",
            },
        )
        assert can_create_shipment(order, cod_enabled=True).allowed

    def test_cod_disabled_blocks(self) -> None:
        order = _order(
            status="cod_pending",
            extra_metadata={
                "payment_method": PAYMENT_METHOD_CASH_ON_DELIVERY,
                "payment_status": PAYMENT_STATUS_COD_PENDING,
                "google_maps_url": "https://maps.google.com/?q=21.4,39.8",
            },
        )
        result = can_create_shipment(order, cod_enabled=False)
        assert not result.allowed
        assert result.reason_key == "cod_disabled"
        assert result.message_ar == MSG_COD_DISABLED

    def test_cancelled_blocks(self) -> None:
        order = _order(status="cancelled")
        result = can_create_shipment(order)
        assert not result.allowed
        assert result.reason_key == "order_status_blocked"

    def test_completed_blocks(self) -> None:
        order = _order(status="completed")
        result = can_create_shipment(order)
        assert not result.allowed

    def test_no_address_blocks(self) -> None:
        order = _order(
            extra_metadata={
                "payment_method": PAYMENT_METHOD_BANK_TRANSFER,
                "payment_confirmed": True,
            },
        )
        result = can_create_shipment(order)
        assert not result.allowed
        assert result.reason_key == "address_missing"
        assert result.message_ar == MSG_ADDRESS_MISSING

    def test_existing_shipment_blocks(self) -> None:
        order = _order()
        result = can_create_shipment(order, has_existing_shipment=True)
        assert not result.allowed
        assert result.reason_key == "shipment_exists"
        assert result.message_ar == MSG_SHIPMENT_EXISTS

    def test_shipping_block_reason_none_when_allowed(self) -> None:
        order = _order()
        assert shipping_block_reason(order) is None


class TestCanGenerateLabel:
    def test_requires_shipment(self) -> None:
        order = _order()
        result = can_generate_label(order, None)
        assert not result.allowed
        assert result.reason_key == "no_shipment"

    def test_label_already_generated(self) -> None:
        order = _order(status="shipment_created")
        shipment = _shipment(status=SHIPMENT_STATUS_LABEL_GENERATED)
        result = can_generate_label(order, shipment)
        assert not result.allowed
        assert result.reason_key == "label_already_generated"

    def test_label_allowed_after_shipment_created(self) -> None:
        order = _order(status="shipment_created")
        shipment = _shipment(status=SHIPMENT_STATUS_CREATED)
        assert can_generate_label(order, shipment).allowed

    def test_label_blocked_when_payment_invalid(self) -> None:
        order = _order(
            status="payment_submitted",
            extra_metadata={
                "payment_method": PAYMENT_METHOD_BANK_TRANSFER,
                "payment_confirmed": False,
            },
        )
        shipment = _shipment()
        result = can_generate_label(order, shipment)
        assert not result.allowed
        assert result.reason_key == "payment_submitted"

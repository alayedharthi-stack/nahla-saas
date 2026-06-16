"""Tests for WhatsApp order lifecycle status resolution (P0 PR-1)."""
from __future__ import annotations

import os
import sys

import pytest

_HERE = os.path.dirname(os.path.abspath(__file__))
_BACKEND = os.path.abspath(os.path.join(_HERE, ".."))
if _BACKEND not in sys.path:
    sys.path.insert(0, _BACKEND)

from core.wa_order_lifecycle import (  # noqa: E402
    STATUS_DRAFT,
    STATUS_PAID,
    STATUS_PAYMENT_SUBMITTED,
    STATUS_PENDING_CUSTOMER_INFO,
    STATUS_PENDING_PAYMENT,
    compute_wa_missing_fields,
    has_accepted_delivery_address,
    is_wa_automation_payment_eligible,
    resolve_wa_order_status,
)


def _prep(**kwargs):
    base = {
        "product_id": "prod-1",
        "quantity": 1,
        "customer_first_name": "",
        "customer_last_name": "",
        "city": "",
    }
    base.update(kwargs)
    return base


def _brain(**kwargs):
    base = {"stage": "ordering", "current_product_focus": {"title": "Honey", "id": 9}}
    base.update(kwargs)
    return base


class TestMissingFields:
    def test_phone_never_in_missing_fields(self) -> None:
        missing = compute_wa_missing_fields(_prep(), whatsapp_phone="966551308005")
        assert "customer_phone" not in missing
        assert "phone" not in missing

    def test_delivery_address_required_without_maps_or_code(self) -> None:
        missing = compute_wa_missing_fields(
            _prep(customer_first_name="A", customer_last_name="B", city="Riyadh"),
        )
        assert "delivery_address" in missing


class TestAddressEvidence:
    def test_city_only_not_accepted(self) -> None:
        assert not has_accepted_delivery_address(_prep(city="مكة"))

    def test_district_description_not_accepted(self) -> None:
        assert not has_accepted_delivery_address(
            _prep(city="مكة", address_line="حي النزهة بجوار المسجد"),
        )

    def test_google_maps_accepted(self) -> None:
        assert has_accepted_delivery_address(
            _prep(google_maps_url="https://maps.google.com/?q=21.4,39.8"),
        )

    def test_short_national_code_accepted(self) -> None:
        assert has_accepted_delivery_address(_prep(short_address_code="RIYD1234"))


class TestStatusResolution:
    def test_product_only_is_draft(self) -> None:
        status, missing, addr = resolve_wa_order_status(_prep(), _brain())
        assert status == STATUS_DRAFT
        assert "delivery_address" in missing
        assert addr == "required"

    def test_partial_customer_info_is_pending_customer_info(self) -> None:
        status, missing, _ = resolve_wa_order_status(
            _prep(customer_first_name="Ahmad", city="Riyadh"),
            _brain(),
        )
        assert status == STATUS_PENDING_CUSTOMER_INFO
        assert "delivery_address" in missing

    def test_complete_without_address_stays_pending_customer_info(self) -> None:
        status, _, _ = resolve_wa_order_status(
            _prep(
                customer_first_name="Ahmad",
                customer_last_name="Ali",
                city="Riyadh",
            ),
            _brain(),
        )
        assert status == STATUS_PENDING_CUSTOMER_INFO

    def test_complete_with_maps_is_pending_payment(self) -> None:
        status, missing, addr = resolve_wa_order_status(
            _prep(
                customer_first_name="Ahmad",
                customer_last_name="Ali",
                city="Riyadh",
                google_maps_url="https://maps.google.com/?q=21.4,39.8",
            ),
            _brain(),
        )
        assert status == STATUS_PENDING_PAYMENT
        assert missing == []
        assert addr == "accepted"

    def test_receipt_without_verification_is_payment_submitted(self) -> None:
        status, _, _ = resolve_wa_order_status(
            _prep(
                customer_first_name="Ahmad",
                customer_last_name="Ali",
                city="Riyadh",
                short_address_code="RIYD1234",
                payment_receipt_received=True,
            ),
            _brain(),
        )
        assert status == STATUS_PAYMENT_SUBMITTED

    def test_verified_receipt_is_paid(self) -> None:
        status, _, _ = resolve_wa_order_status(
            _prep(
                customer_first_name="Ahmad",
                customer_last_name="Ali",
                city="Riyadh",
                short_address_code="RIYD1234",
                payment_receipt_received=True,
                payment_verified=True,
            ),
            _brain(),
            payment_verified=True,
        )
        assert status == STATUS_PAID


class TestAutomationEligibility:
    def test_only_pending_payment_wa_orders_eligible(self) -> None:
        meta = {"created_via": "nahla_order_bridge"}
        assert is_wa_automation_payment_eligible(STATUS_PENDING_PAYMENT, meta)
        assert not is_wa_automation_payment_eligible(STATUS_DRAFT, meta)
        assert not is_wa_automation_payment_eligible(STATUS_PENDING_CUSTOMER_INFO, meta)
        assert not is_wa_automation_payment_eligible(STATUS_PAYMENT_SUBMITTED, meta)

    def test_salla_orders_unaffected(self) -> None:
        assert is_wa_automation_payment_eligible("draft", {"source_kind": "salla"})

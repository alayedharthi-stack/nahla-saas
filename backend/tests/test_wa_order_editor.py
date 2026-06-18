"""P1 — Merchant WhatsApp draft order editor."""
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

from core.wa_order_editor import (  # noqa: E402
    MATCH_STATUS_CONFIRMED,
    MATCH_STATUS_CUSTOM_UNMATCHED,
    MATCH_STATUS_NEEDS_REVIEW,
    OrderEditError,
    add_order_line_item,
    cancel_order,
    can_cancel_order,
    can_delete_draft_order,
    confirm_order_ready,
    delete_draft_order,
    delete_order_line_item,
    is_order_editable,
    update_order_address,
    update_order_customer,
    update_order_line_item,
    update_order_shipping_meta,
)
from routers.orders import _serialise_order  # noqa: E402


def _wa_order(**overrides):
    base = dict(
        id=42,
        tenant_id=33,
        external_id="nahla-wa-33-99",
        external_order_number="NHL-33-000099",
        status="draft",
        total="100.00 ر.س",
        customer_name="أحمد",
        customer_info={"phone": "966551308005"},
        line_items=[{
            "title": "عسل سمر",
            "product_name": "عسل سمر",
            "quantity": 1,
            "unit_price": 100,
            "match_status": MATCH_STATUS_NEEDS_REVIEW,
        }],
        checkout_url=None,
        source="whatsapp",
        is_abandoned=False,
        extra_metadata={
            "created_at": datetime.now(timezone.utc).isoformat(),
            "payment_confirmed": False,
        },
    )
    base.update(overrides)
    return SimpleNamespace(**base)


class TestEditGuards:
    def test_editable_whatsapp_draft(self) -> None:
        assert is_order_editable(_wa_order()) is True

    def test_not_editable_when_paid(self) -> None:
        o = _wa_order(status="paid", extra_metadata={"payment_confirmed": True})
        assert is_order_editable(o) is False
        assert can_delete_draft_order(o) is False

    def test_delete_only_draft_or_pending_info(self) -> None:
        assert can_delete_draft_order(_wa_order(status="draft")) is True
        assert can_delete_draft_order(_wa_order(status="pending_customer_info")) is True
        assert can_delete_draft_order(_wa_order(status="pending_payment")) is False

    def test_cancel_blocked_when_paid(self) -> None:
        o = _wa_order(status="paid")
        assert can_cancel_order(o) is False
        with pytest.raises(OrderEditError, match="order_not_cancellable"):
            cancel_order(o)


class TestCustomerEdit:
    def test_updates_split_name_for_shipping(self) -> None:
        o = _wa_order()
        update_order_customer(
            o,
            first_name="حسن",
            last_name="حامد",
            phone="966500000001",
            internal_note="VIP manual fix",
        )
        assert o.customer_name == "حسن حامد"
        assert o.extra_metadata["customer_first_name"] == "حسن"
        assert o.extra_metadata["customer_last_name"] == "حامد"
        assert o.extra_metadata["internal_note"] == "VIP manual fix"
        assert o.extra_metadata["merchant_edit_locked"] is True
        assert o.customer_info["phone"] == "966500000001"

    def test_blocked_on_paid_order(self) -> None:
        with pytest.raises(OrderEditError, match="order_not_editable"):
            update_order_customer(_wa_order(status="paid"), first_name="x")


class TestAddressEdit:
    def test_sets_short_code_and_maps_url(self) -> None:
        o = _wa_order(status="pending_customer_info")
        update_order_address(
            o,
            city="مكة",
            district="الشوقية",
            street="شارع 1",
            short_address_code="abcd1234",
            google_maps_url="https://maps.google.com/?q=21,39",
            delivery_notes="اتصل قبل الوصول",
        )
        assert o.customer_info["city"] == "مكة"
        assert o.extra_metadata["short_address_code"] == "ABCD1234"
        assert o.extra_metadata["google_maps_url"].startswith("https://")


class TestLineItems:
    def test_cannot_force_confirmed_without_catalog(self) -> None:
        o = _wa_order()
        with pytest.raises(OrderEditError, match="cannot_force_confirmed"):
            update_order_line_item(
                o,
                0,
                {"match_status": MATCH_STATUS_CONFIRMED},
            )

    def test_delete_last_item_clears_total_needs_review(self) -> None:
        o = _wa_order()
        delete_order_line_item(o, 0)
        assert o.line_items == []
        assert o.extra_metadata.get("needs_amount_review") or "product" in (
            o.extra_metadata.get("missing_fields") or []
        )

    def test_add_manual_unmatched_item(self) -> None:
        o = _wa_order(line_items=[])
        add_order_line_item(
            o,
            {"product_name": "عسل خاص", "quantity": 2, "unit_price": 50},
        )
        assert len(o.line_items) == 1
        assert o.line_items[0]["match_status"] == MATCH_STATUS_CUSTOM_UNMATCHED

    def test_quantity_patch(self) -> None:
        o = _wa_order()
        update_order_line_item(o, 0, {"quantity": 3})
        assert o.line_items[0]["quantity"] == 3


class TestConfirmReady:
    def test_blocks_when_name_or_catalog_missing(self) -> None:
        o = _wa_order()
        with pytest.raises(OrderEditError, match="order_incomplete"):
            confirm_order_ready(o)

    def test_passes_when_complete(self) -> None:
        o = _wa_order(
            status="pending_customer_info",
            customer_name="حسن حامد",
            customer_info={
                "phone": "966551308005",
                "city": "مكة",
                "first_name": "حسن",
                "last_name": "حامد",
            },
            line_items=[{
                "title": "عسل",
                "product_id": "ext-1",
                "variant_id": "v-1",
                "variant": "1kg",
                "quantity": 1,
                "unit_price": 100,
                "match_status": MATCH_STATUS_CONFIRMED,
            }],
            extra_metadata={
                "customer_first_name": "حسن",
                "customer_last_name": "حامد",
                "short_address_code": "ABCD1234",
                "created_at": datetime.now(timezone.utc).isoformat(),
                "payment_confirmed": False,
            },
        )
        confirm_order_ready(o)
        assert o.status == "pending_payment"


class TestShippingMeta:
    def test_rejects_unknown_provider(self) -> None:
        with pytest.raises(OrderEditError, match="invalid_shipping_provider"):
            update_order_shipping_meta(_wa_order(), shipping_provider="dhl")

    def test_stores_foundation_fields(self) -> None:
        o = _wa_order()
        update_order_shipping_meta(
            o,
            shipping_provider="oto",
            shipping_cost=25.0,
            tracking_number="TRK-1",
            shipping_status="pending",
            delivery_notes="fragile",
        )
        meta = o.extra_metadata
        assert meta["shipping_provider"] == "oto"
        assert meta["shipping_cost"] == 25.0
        assert meta["tracking_number"] == "TRK-1"


class TestCancelAndDelete:
    def test_cancel_sets_status_and_audit(self) -> None:
        o = _wa_order(status="pending_payment")
        cancel_order(o, reason="duplicate")
        assert o.status == "cancelled"
        assert o.extra_metadata["cancel_reason"] == "duplicate"
        assert o.extra_metadata["merchant_audit_log"][-1]["action"] == "cancel"

    def test_delete_guard_on_pending_payment(self) -> None:
        with pytest.raises(OrderEditError, match="order_not_deletable"):
            delete_draft_order(_wa_order(status="pending_payment"))


class TestSerializerEditFields:
    def test_detailed_payload_includes_edit_caps(self) -> None:
        o = _wa_order(
            extra_metadata={
                "created_at": datetime.now(timezone.utc).isoformat(),
                "customer_first_name": "حسن",
                "customer_last_name": "حامد",
                "missing_fields": ["delivery_address"],
                "payment_confirmed": False,
            },
        )
        payload = _serialise_order(
            o,
            customer_lookup={},
            now=datetime.now(timezone.utc),
            detailed=True,
        )
        assert payload["is_editable"] is True
        assert payload["can_delete_draft"] is True
        assert payload["missing_fields"] == ["delivery_address"]
        assert payload["line_items"][0]["match_status"] == MATCH_STATUS_CUSTOM_UNMATCHED
        assert payload["line_items"][0]["is_catalog_matched"] is False

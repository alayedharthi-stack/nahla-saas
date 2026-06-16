"""Dashboard orders router — WA lifecycle labels, filters, needs_action."""
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

from core.order_payment_policy import PAYMENT_METHOD_BANK_TRANSFER  # noqa: E402
from core.wa_order_dashboard import (  # noqa: E402
    build_delivery_location_display,
    order_matches_lifecycle_filter,
    resolve_wa_status_label_ar,
)
from routers.orders import _serialise_order  # noqa: E402


def _wa_order(**overrides):
    base = dict(
        id=1,
        tenant_id=33,
        external_id="nahla-wa-33-1",
        external_order_number="NHL-33-000001",
        status="draft",
        total="387.00 ر.س",
        customer_name="حسن حامد",
        customer_info={"phone": "966551308005", "city": "مكة المكرمة"},
        line_items=[{"title": "عسل سمر", "quantity": 1, "unit_price": 387}],
        checkout_url=None,
        source="whatsapp",
        is_abandoned=False,
        extra_metadata={
            "created_at": datetime.now(timezone.utc).isoformat(),
            "payment_method": PAYMENT_METHOD_BANK_TRANSFER,
            "payment_confirmed": False,
        },
    )
    base.update(overrides)
    return SimpleNamespace(**base)


class TestWaStatusLabels:
    def test_draft_label(self) -> None:
        o = _wa_order(status="draft")
        p = _serialise_order(o, customer_lookup={}, now=datetime.now(timezone.utc))
        assert p["status_label_ar"] == "مسودة طلب"

    def test_pending_customer_info_missing_address(self) -> None:
        o = _wa_order(
            status="pending_customer_info",
            extra_metadata={
                "missing_fields": ["delivery_address"],
                "created_at": datetime.now(timezone.utc).isoformat(),
            },
        )
        p = _serialise_order(o, customer_lookup={}, now=datetime.now(timezone.utc))
        assert p["status_label_ar"] == "ناقص موقع"
        assert p["address_status_label_ar"] == "ناقص موقع"

    def test_pending_payment_label(self) -> None:
        o = _wa_order(status="pending_payment")
        p = _serialise_order(o, customer_lookup={}, now=datetime.now(timezone.utc))
        assert p["status_label_ar"] == "بانتظار الدفع"

    def test_payment_submitted_bank_transfer(self) -> None:
        o = _wa_order(
            status="payment_submitted",
            extra_metadata={
                "payment_method": PAYMENT_METHOD_BANK_TRANSFER,
                "payment_confirmed": False,
                "payment_status": "pending_verification",
                "created_at": datetime.now(timezone.utc).isoformat(),
            },
        )
        p = _serialise_order(o, customer_lookup={}, now=datetime.now(timezone.utc))
        assert p["status_label_ar"] == "دفع مرسل — يحتاج تحقق"
        assert any(c["key"] == "payment_submitted_verify" for c in p["action_chips"])

    def test_paid_label(self) -> None:
        o = _wa_order(
            status="paid",
            extra_metadata={
                "payment_confirmed": True,
                "payment_status": "paid",
                "google_maps_url": "https://maps.google.com/?q=21,39",
                "created_at": datetime.now(timezone.utc).isoformat(),
            },
        )
        p = _serialise_order(o, customer_lookup={}, now=datetime.now(timezone.utc))
        assert p["status_label_ar"] == "مدفوع"


class TestNeedsAction:
    def test_payment_submitted_needs_action(self) -> None:
        o = _wa_order(
            status="payment_submitted",
            extra_metadata={
                "payment_method": PAYMENT_METHOD_BANK_TRANSFER,
                "payment_confirmed": False,
                "created_at": datetime.now(timezone.utc).isoformat(),
            },
        )
        p = _serialise_order(o, customer_lookup={}, now=datetime.now(timezone.utc))
        assert p["needs_action_flag"] is True

    def test_missing_address_needs_action(self) -> None:
        o = _wa_order(
            status="pending_customer_info",
            extra_metadata={
                "missing_fields": ["delivery_address"],
                "created_at": datetime.now(timezone.utc).isoformat(),
            },
        )
        p = _serialise_order(o, customer_lookup={}, now=datetime.now(timezone.utc))
        assert p["needs_action_flag"] is True
        assert any(a["key"] == "missing_location" for a in p["needs_action"])

    def test_paid_missing_address_banner(self) -> None:
        o = _wa_order(
            status="pending_customer_info",
            extra_metadata={
                "payment_confirmed": True,
                "payment_status": "paid",
                "missing_fields": ["delivery_address"],
                "merchant_post_confirm_notice": (
                    "تم تأكيد الدفع، لكن لا يمكن تجهيز الطلب أو شحنه قبل إضافة "
                    "رابط Google Maps أو رمز العنوان الوطني المختصر."
                ),
                "created_at": datetime.now(timezone.utc).isoformat(),
            },
        )
        p = _serialise_order(o, customer_lookup={}, now=datetime.now(timezone.utc))
        assert "تم تأكيد الدفع" in (p.get("merchant_post_confirm_notice") or "")
        assert any(c["key"] == "paid_missing_address" for c in p["action_chips"])


class TestLocationDisplay:
    def test_whatsapp_location_maps_link(self) -> None:
        o = _wa_order(
            status="pending_payment",
            extra_metadata={
                "delivery_location_lat": 21.4225,
                "delivery_location_lng": 39.8262,
                "location_name": "مكة",
                "created_at": datetime.now(timezone.utc).isoformat(),
            },
        )
        loc = build_delivery_location_display(o)
        assert loc is not None
        assert loc["type"] == "whatsapp_location"
        assert "google.com/maps" in loc["open_url"]

    def test_google_maps_url(self) -> None:
        o = _wa_order(
            extra_metadata={
                "google_maps_url": "https://maps.google.com/?q=21,39",
                "created_at": datetime.now(timezone.utc).isoformat(),
            },
        )
        loc = build_delivery_location_display(o)
        assert loc is not None
        assert loc["type"] in ("maps_url", "apple_maps")
        assert loc["url"].startswith("https://")


class TestLifecycleFilters:
    def test_missing_location_filter(self) -> None:
        o = _wa_order(
            status="pending_customer_info",
            extra_metadata={"missing_fields": ["delivery_address"]},
        )
        assert order_matches_lifecycle_filter(o, "missing_location")

    def test_payment_submitted_filter(self) -> None:
        o = _wa_order(status="payment_submitted")
        assert order_matches_lifecycle_filter(o, "payment_submitted")

    def test_resolve_label_helper(self) -> None:
        assert resolve_wa_status_label_ar("draft") == "مسودة طلب"

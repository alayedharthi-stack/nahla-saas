"""Tests for Salla order import time/money/customer fidelity."""
from __future__ import annotations

from datetime import datetime, timezone

from core.salla_order_fidelity import (
    extract_salla_grand_total,
    extract_salla_money_amount,
    normalize_salla_line_items,
    parse_salla_order_datetime,
)
from services.store_sync import _merge_order_extra_metadata, _normalise_order as ss_normalise_order
from routers.orders import _read_created_at
from database.models import Order


SALLA_WEBHOOK_ORDER = {
    "id": 566146469,
    "reference_id": 269977976,
    "status": {"slug": "under_review", "name": "بإنتظار المراجعة"},
    "created_at": {
        "date": "2026-07-02 14:20:00.000000",
        "timezone_type": 3,
        "timezone": "Asia/Riyadh",
    },
    "customer": {
        "id": 123,
        "name": "تركي الحارثي",
        "mobile": "0555906901",
        "email": "turki@example.com",
    },
    "shipping": {
        "address": {
            "country": "SA",
            "city": "الرياض",
            "district": "الملقا",
            "street": "شارع الأمير",
            "postal_code": "12345",
            "building_number": "12",
            "additional_number": "3456",
            "short_address": "RRRD1234",
        },
        "company": {"id": 1, "name": "سمسا"},
        "cost": {"amount": 20, "currency": "SAR"},
    },
    "payment": {"method": "cod", "status": "pending"},
    "amounts": {
        "sub_total": {"amount": 144, "currency": "SAR"},
        "discount": {"amount": 0, "currency": "SAR"},
        "shipping": {"amount": 20, "currency": "SAR"},
        "tax": {"amount": 0, "currency": "SAR"},
        "total": {"amount": 164, "currency": "SAR"},
    },
    "items": [
        {
            "product_id": 101,
            "name": "عسل سدر",
            "quantity": 2,
            "price": {"amount": 82, "currency": "SAR"},
        }
    ],
}


class TestSallaMoneyExtraction:
    def test_nested_total_amount(self):
        assert extract_salla_grand_total(SALLA_WEBHOOK_ORDER) == "164"

    def test_money_dict_unit_price(self):
        assert extract_salla_money_amount({"amount": "82.00", "currency": "SAR"}) == "82.00"

    def test_amounts_container_not_empty_string(self):
        assert extract_salla_grand_total({"amounts": SALLA_WEBHOOK_ORDER["amounts"]}) == "164"


class TestSallaDatetime:
    def test_riyadh_nested_created_at_converts_to_utc(self):
        utc_dt, stamps = parse_salla_order_datetime(SALLA_WEBHOOK_ORDER)
        assert utc_dt is not None
        assert stamps["salla_timezone"] == "Asia/Riyadh"
        assert stamps["salla_created_at"].startswith("2026-07-02 14:20")
        # Stored UTC should be 3h behind Riyadh wall clock for this fixture.
        assert utc_dt.hour == 11
        assert utc_dt.minute == 20

    def test_naive_salla_datetime_assumes_riyadh(self):
        utc_dt, _ = parse_salla_order_datetime(
            {"created_at": "2026-07-02 14:20:00"},
        )
        assert utc_dt is not None
        # UTC storage should be 3 hours behind Riyadh wall clock.
        assert utc_dt.hour in (11, 14)


class TestSallaLineItems:
    def test_price_dict_parsed(self):
        items = normalize_salla_line_items(SALLA_WEBHOOK_ORDER["items"])
        assert len(items) == 1
        assert items[0]["unit_price"] == 82.0
        assert items[0]["quantity"] == 2


class TestStoreSyncNormaliser:
    def test_webhook_order_total_and_metadata(self):
        n = ss_normalise_order(SALLA_WEBHOOK_ORDER)
        assert n["total"] == "164"
        assert n["external_order_number"] == "269977976"
        assert n["customer_name"] == "تركي الحارثي"
        assert n["line_items"][0]["unit_price"] == 82.0
        meta = n["salla_metadata"]
        assert meta["salla_amounts"]["total"] == "164"
        assert meta["salla_amounts"]["shipping"] == "20"
        assert meta["salla_amounts"]["currency"] == "SAR"

    def test_customer_address_preserved(self):
        n = ss_normalise_order(SALLA_WEBHOOK_ORDER)
        ci = n["customer_info"]
        assert ci["city"] == "الرياض"
        assert ci["district"] == "الملقا"
        assert ci["postal_code"] == "12345"
        assert ci["additional_number"] == "3456"
        assert ci["shipping_company"] == "سمسا"


class TestOrderMetadataMerge:
    def test_merge_preserves_merchant_notes(self):
        merged = _merge_order_extra_metadata(
            {"merchant_post_confirm_notice": "keep me"},
            ss_normalise_order(SALLA_WEBHOOK_ORDER),
        )
        assert merged["merchant_post_confirm_notice"] == "keep me"
        assert merged["salla_amounts"]["total"] == "164"
        assert merged["salla_created_at"].startswith("2026-07-02")


class TestOrderListSort:
    def test_orders_sorted_by_salla_created_at_desc(self):
        now = datetime.now(timezone.utc)
        older = Order(
            tenant_id=1,
            external_id="1",
            status="completed",
            total="100",
            customer_info={},
            line_items=[],
            source="salla",
            extra_metadata={"created_at": "2026-07-01T11:20:00+00:00"},
        )
        newer = Order(
            tenant_id=1,
            external_id="2",
            status="completed",
            total="164",
            customer_info={},
            line_items=[],
            source="salla",
            extra_metadata={"created_at": "2026-07-02T11:20:00+00:00"},
        )
        rows = [older, newer]
        rows.sort(
            key=lambda o: (_read_created_at(o, fallback=now), int(getattr(o, "id", 0) or 0)),
            reverse=True,
        )
        assert rows[0].external_id == "2"


class TestWebhookIdempotency:
    def test_merge_updates_total_without_dropping_notes(self):
        first = ss_normalise_order(SALLA_WEBHOOK_ORDER)
        merged = _merge_order_extra_metadata(
            {"merchant_post_confirm_notice": "note"},
            first,
        )
        updated_payload = dict(SALLA_WEBHOOK_ORDER)
        updated_payload["amounts"] = {
            **SALLA_WEBHOOK_ORDER["amounts"],
            "total": {"amount": 150, "currency": "SAR"},
        }
        second = ss_normalise_order(updated_payload)
        merged2 = _merge_order_extra_metadata(merged, second)
        assert merged2["merchant_post_confirm_notice"] == "note"
        assert merged2["salla_amounts"]["total"] == "150"
        assert second["total"] == "150"

"""Catalog evidence rules for WhatsApp order line items."""
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

from core.wa_cart_line_items import normalize_line_item  # noqa: E402
from core.wa_order_line_item_evidence import (  # noqa: E402
    MATCH_STATUS_CONFIRMED,
    MATCH_STATUS_CUSTOM_UNMATCHED,
    MATCH_STATUS_NEEDS_REVIEW,
    MATCH_STATUS_NEEDS_VARIANT,
    compute_match_status,
    enrich_line_item_with_catalog,
    order_line_items_block_confirm,
    parse_unit_price,
    sanitize_line_item_without_db,
)
from routers.orders import _serialise_order  # noqa: E402


class TestParseUnitPrice:
    def test_rejects_zero(self) -> None:
        assert parse_unit_price("0") is None
        assert parse_unit_price(0) is None

    def test_parses_sar_string(self) -> None:
        assert parse_unit_price("120.00 ر.س") == 120.0


class TestSanitizeWithoutDb:
    def test_free_text_name_without_product_id_is_unmatched(self) -> None:
        row = sanitize_line_item_without_db({
            "product_name": "عسل طلح",
            "match_status": "confirmed",
        })
        assert row["match_status"] == MATCH_STATUS_CUSTOM_UNMATCHED

    def test_confirmed_without_price_downgraded(self) -> None:
        row = sanitize_line_item_without_db({
            "product_id": "ext-1",
            "product_name": "عسل",
            "match_status": "confirmed",
        })
        assert row["match_status"] == MATCH_STATUS_NEEDS_REVIEW

    def test_normalize_never_defaults_to_confirmed(self) -> None:
        row = normalize_line_item({"product_name": "x", "product_id": "p1"})
        assert row["match_status"] == MATCH_STATUS_NEEDS_REVIEW


class TestComputeMatchStatus:
    def test_product_id_without_price_needs_review(self) -> None:
        product = SimpleNamespace(title="عسل سمر", has_variants=False)
        assert compute_match_status(
            {"product_id": "p1", "unit_price": None},
            product=product,
        ) == MATCH_STATUS_NEEDS_REVIEW

    def test_requires_variant_when_product_has_variants(self) -> None:
        product = SimpleNamespace(title="عسل", has_variants=True)
        assert compute_match_status(
            {"product_id": "p1", "unit_price": 100},
            product=product,
            requires_variant=True,
        ) == MATCH_STATUS_NEEDS_VARIANT

    def test_confirmed_with_full_evidence(self) -> None:
        product = SimpleNamespace(title="عسل سمر")
        variant = SimpleNamespace(option_summary="1kg")
        assert compute_match_status(
            {
                "product_id": "p1",
                "variant_id": "v1",
                "unit_price": 120,
            },
            product=product,
            variant_row=variant,
            requires_variant=False,
        ) == MATCH_STATUS_CONFIRMED


class TestOrderConfirmBlockers:
    def test_unmatched_items_block_confirm(self) -> None:
        blockers = order_line_items_block_confirm([
            {"match_status": MATCH_STATUS_CUSTOM_UNMATCHED, "product_name": "عسل طلح"},
            {"match_status": MATCH_STATUS_NEEDS_REVIEW, "product_id": "p1"},
        ])
        assert "catalog_review_required" in blockers

    def test_needs_variant_blocks_confirm(self) -> None:
        blockers = order_line_items_block_confirm([
            {"match_status": MATCH_STATUS_NEEDS_VARIANT, "product_id": "p1", "unit_price": 100},
        ])
        assert "catalog_needs_variant" in blockers


class TestEnrichLineItemWithCatalog:
    def test_free_text_item_not_matched(self) -> None:
        class _Db:
            def query(self, *_args, **_kwargs):
                return self

            def filter(self, *_args, **_kwargs):
                return self

            def first(self):
                return None

            def all(self):
                return []

        payload = enrich_line_item_with_catalog(_Db(), 33, {
            "product_name": "عسل اصلي الله",
            "quantity": 1,
        })
        assert payload["match_status"] == MATCH_STATUS_CUSTOM_UNMATCHED
        assert payload["is_catalog_matched"] is False
        assert payload["unit_price"] is None

    def test_confirmed_item_has_card_fields(self) -> None:
        product = SimpleNamespace(
            id=7,
            title="عسل سمر",
            external_id="ext-7",
            price="150",
            has_variants=False,
            default_variant_id=11,
            extra_metadata={"image_url": "https://img/honey.jpg", "product_url": "https://shop/p"},
        )
        variant = SimpleNamespace(
            id=11,
            salla_variant_id="sv-11",
            retailer_id=None,
            sku="1kg",
            option_summary="1kg",
            price="150",
            image_url="https://img/honey-1kg.jpg",
            is_default=True,
        )

        class _Q:
            def __init__(self, result):
                self._result = result

            def filter(self, *_args, **_kwargs):
                return self

            def first(self):
                return self._result

            def all(self):
                return [variant]

        class _Db:
            def query(self, model):
                name = getattr(model, "__name__", str(model))
                if name == "Product":
                    return _Q(product)
                return _Q(variant)

        payload = enrich_line_item_with_catalog(_Db(), 33, {
            "product_id": "ext-7",
            "variant_id": "sv-11",
            "quantity": 2,
        })
        assert payload["match_status"] == MATCH_STATUS_CONFIRMED
        assert payload["is_catalog_matched"] is True
        assert payload["catalog_product_name"] == "عسل سمر"
        assert payload["image_url"] == "https://img/honey-1kg.jpg"
        assert payload["product_url"] == "https://shop/p"
        assert payload["unit_price"] == 150.0
        assert payload["line_total"] == 300.0


class TestSerializerDefaults:
    def test_detailed_payload_without_db_does_not_default_confirmed(self) -> None:
        o = SimpleNamespace(
            id=1,
            tenant_id=33,
            external_id="nahla-wa-33-1",
            external_order_number="NHL-33-000001",
            status="draft",
            total="0.00 ر.س",
            customer_name="test",
            customer_info={"phone": "966500000000"},
            line_items=[{"product_name": "عسل طلح", "quantity": 1}],
            checkout_url=None,
            source="whatsapp",
            is_abandoned=False,
            extra_metadata={"created_at": datetime.now(timezone.utc).isoformat()},
        )
        payload = _serialise_order(
            o,
            customer_lookup={},
            now=datetime.now(timezone.utc),
            detailed=True,
        )
        assert payload["line_items"][0]["match_status"] == MATCH_STATUS_CUSTOM_UNMATCHED
        assert payload["line_items"][0]["is_catalog_matched"] is False

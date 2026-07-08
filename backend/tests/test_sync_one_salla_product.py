"""Regression tests for one-product Salla sync."""
from __future__ import annotations

import asyncio
import os
import sys
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

_here = os.path.dirname(os.path.abspath(__file__))
_backend = os.path.dirname(_here)
for _p in [_backend, os.path.join(_backend, "..")]:
    if _p not in sys.path:
        sys.path.insert(0, _p)

from services.store_sync import StoreSyncService  # noqa: E402


class _VariantDB:
    def __init__(self) -> None:
        self.rows: list[SimpleNamespace] = []
        self.committed = False
        self.flushed = False

    def query(self, model):
        return _VariantQuery(self, model)

    def flush(self) -> None:
        self.flushed = True

    def commit(self) -> None:
        self.committed = True


class _VariantQuery:
    def __init__(self, db: _VariantDB, model) -> None:
        self._db = db
        self._model = getattr(model, "__name__", str(model))

    def filter(self, *args, **kwargs):
        return self

    def filter_by(self, **kwargs):
        return self

    def first(self):
        if self._model == "Product":
            return self._db.product
        return None

    def all(self):
        if self._model == "ProductVariant":
            return list(self._db.rows)
        return []


def _normalised_product(external_id: str = "ext-9001") -> dict:
    return {
        "external_id": external_id,
        "title": "قميص قطني أزرق",
        "description": "وصف عام",
        "price": "149.0",
        "sale_price": "99.0",
        "regular_price": "149.0",
        "sku": "SKU-9001",
        "in_stock": True,
        "quantity": 6,
        "variants": [
            {
                "salla_variant_id": "v-1",
                "is_default": False,
                "price": "99.0",
                "sale_price": "99.0",
                "regular_price": "149.0",
                "in_stock": True,
                "stock_quantity": 4,
                "option_summary": "M / أزرق",
            },
            {
                "salla_variant_id": "v-2",
                "is_default": False,
                "price": "99.0",
                "sale_price": "99.0",
                "regular_price": "149.0",
                "in_stock": True,
                "stock_quantity": 2,
                "option_summary": "L / أزرق",
            },
        ],
    }


def _existing_product(external_id: str = "ext-9001") -> SimpleNamespace:
    return SimpleNamespace(
        id=9001,
        tenant_id=77,
        external_id=external_id,
        title="قديم",
        description="",
        price="{'amount': 149, 'currency': 'SAR'}",
        sku="SKU-OLD",
        in_stock=False,
        stock_quantity=0,
        has_variants=True,
        source="salla",
        extra_metadata={
            "price": "{'amount': 149, 'currency': 'SAR'}",
            "sale_price": "{'amount': 0, 'currency': 'SAR'}",
            "regular_price": "{'amount': 149, 'currency': 'SAR'}",
        },
        meta_retailer_id=None,
        default_variant_id=None,
    )


def _fake_adapter(normalised_payload: dict | None = None):
    payload = normalised_payload or _normalised_product()
    adapter = SimpleNamespace(platform="salla")
    adapter.get_product = AsyncMock(return_value=payload)
    adapter.get_products = AsyncMock(side_effect=AssertionError("get_products must not be called"))
    return adapter


def test_sync_one_salla_product_dry_run_no_commit() -> None:
    db = _VariantDB()
    db.product = _existing_product()
    db.rows = [
        SimpleNamespace(
            id=1,
            salla_variant_id=None,
            is_default=True,
            price="149.0",
            in_stock=False,
            stock_quantity=0,
        )
    ]

    service = StoreSyncService(db, tenant_id=77)
    service._adapter = _fake_adapter()

    result = asyncio.run(service.sync_one_product_by_external_id("ext-9001", dry_run=True))

    assert result["ok"] is True
    assert result["dry_run"] is True
    assert result["diff"]["in_stock_changed"] is True
    assert result["diff"]["variants_count_changed"] is True
    assert db.committed is False
    assert db.product.in_stock is False


def test_sync_one_salla_product_confirm_updates_parent_and_variants() -> None:
    db = _VariantDB()
    product = _existing_product()
    db.product = product
    db.rows = []

    service = StoreSyncService(db, tenant_id=77)
    service._adapter = _fake_adapter()

    with patch("services.store_sync._upsert_variants_for") as upsert_mock:
        with patch("core.catalog.assign_canonical_retailer_id"):
            result = asyncio.run(
                service.sync_one_product_by_external_id(
                    "ext-9001",
                    dry_run=False,
                )
            )

    assert result["ok"] is True
    assert result["dry_run"] is False
    assert result["action"] == "updated"
    assert product.in_stock is True
    assert product.stock_quantity == 6
    assert product.price == "149.0"
    assert product.extra_metadata["sale_price"] == "99.0"
    assert db.committed is True
    upsert_mock.assert_called_once()


def test_sync_one_salla_product_rejects_adapter_unavailable() -> None:
    service = StoreSyncService(db=SimpleNamespace(), tenant_id=88)
    service._adapter = None

    with patch.object(service, "_get_adapter", return_value=None):
        result = asyncio.run(
            service.sync_one_product_by_external_id("ext-missing", dry_run=True)
        )

    assert result["ok"] is False
    assert result["error"] == "adapter_unavailable"


def test_sync_one_salla_product_requires_one_identifier() -> None:
    from scripts import sync_one_salla_product as cli  # noqa: PLC0415

    with patch.object(sys, "argv", ["sync_one_salla_product.py", "--tenant-id", "1"]):
        with pytest.raises(SystemExit):
            cli.main()

    with patch.object(
        sys,
        "argv",
        [
            "sync_one_salla_product.py",
            "--tenant-id",
            "1",
            "--external-id",
            "111",
            "--nahla-product-id",
            "22",
        ],
    ):
        with pytest.raises(SystemExit):
            cli.main()


def test_sync_one_salla_product_does_not_call_full_sync_or_get_products() -> None:
    adapter = _fake_adapter()
    service = StoreSyncService(db=SimpleNamespace(query=MagicMock(return_value=MagicMock(first=MagicMock(return_value=None)))), tenant_id=55)
    service._adapter = adapter

    with patch.object(service, "sync_products", AsyncMock(side_effect=AssertionError("full sync forbidden"))):
        with patch.object(service, "full_sync", AsyncMock(side_effect=AssertionError("full sync forbidden"))):
            with patch(
                "services.store_sync._product_db_summary",
                return_value=None,
            ):
                result = asyncio.run(
                    service.sync_one_product_by_external_id("ext-9001", dry_run=True)
                )

    assert result["ok"] is True
    adapter.get_product.assert_awaited_once_with("ext-9001")
    adapter.get_products.assert_not_called()

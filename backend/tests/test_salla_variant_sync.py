"""Salla variant sync: adapter normalization and store_sync upsert."""

from __future__ import annotations

import asyncio
from types import SimpleNamespace
from typing import Any, List

import pytest

from store_adapters.salla_adapter import (
    SallaAdapter,
    _extract_variant_options,
)
from services.store_sync import (
    _normalise_product,
    _resolve_variant_retailer_id,
    _upsert_variants_for,
)


def _adapter() -> SallaAdapter:
    return SallaAdapter(api_key="test", store_id="E-demo")


def _five_variant_payloads() -> List[dict]:
  return [
      {
          "id": 591539870,
          "price": {"amount": 59},
          "sale_price": {"amount": 59},
          "regular_price": {"amount": 119},
          "quantity": 1,
          "available": True,
          "options": [2019873167],
      },
      {
          "id": 2100252063,
          "price": {"amount": 59},
          "sale_price": {"amount": 59},
          "regular_price": {"amount": 119},
          "quantity": 1,
          "available": True,
          "options": [1245835400],
      },
      {
          "id": 1324641432,
          "price": {"amount": 59},
          "sale_price": {"amount": 59},
          "regular_price": {"amount": 119},
          "quantity": 1,
          "available": True,
          "options": [607002505],
      },
      {
          "id": 416451481,
          "price": {"amount": 59},
          "sale_price": {"amount": 59},
          "regular_price": {"amount": 119},
          "quantity": 1,
          "available": True,
          "options": [997722240],
      },
      {
          "id": 1790945946,
          "price": {"amount": 59},
          "sale_price": {"amount": 59},
          "regular_price": {"amount": 119},
          "quantity": 1,
          "available": True,
          "options": [356849537],
      },
  ]


def test_normalize_five_variants_from_dedicated_payload():
    adapter = _adapter()
    product_options = [
        {
            "id": 10,
            "name": "المقاس",
            "values": [
                {"id": 2019873167, "name": "S"},
                {"id": 1245835400, "name": "M"},
                {"id": 607002505, "name": "L"},
                {"id": 997722240, "name": "XL"},
                {"id": 356849537, "name": "XXL"},
            ],
        }
    ]
    variants = [
        adapter._normalize_variant(raw, product_options)
        for raw in _five_variant_payloads()
    ]
    assert len(variants) == 5
    assert {v.id for v in variants} == {
        "591539870", "2100252063", "1324641432", "416451481", "1790945946",
    }
    assert all(v.sale_price == 59.0 for v in variants)
    assert all(v.regular_price == 119.0 for v in variants)
    assert variants[0].options == {"المقاس": "S"}


def test_normalize_variant_does_not_invent_sale_price():
    variant = _adapter()._normalize_variant(
        {"id": 1, "price": {"amount": 80}, "quantity": 2, "available": True},
        [],
    )
    assert variant.price == 80.0
    assert variant.sale_price is None
    assert variant.regular_price is None


def test_extract_variant_options_raw_ids_fallback():
    options, summary = _extract_variant_options({"options": [90001, 90002]}, [])
    assert options == {"option_value_ids": ["90001", "90002"]}
    assert summary is None


def test_normalise_product_carries_variant_sale_prices():
    adapter = _adapter()
    raw = {
        "id": 88001,
        "name": "قميص قطني أزرق",
        "price": {"amount": 99},
        "variants": _five_variant_payloads()[:1],
        "options": [{"id": 1, "name": "المقاس", "values": [{"id": 2019873167, "name": "S"}]}],
    }
    product = adapter._normalize_product(raw)
    synced = _normalise_product(product)
    assert len(synced["variants"]) == 1
    assert synced["variants"][0]["sale_price"] == "59.0"
    assert synced["variants"][0]["regular_price"] == "119.0"


class _VariantQuery:
    def __init__(self, db: "_VariantDB") -> None:
        self._db = db

    def filter(self, *_args: Any, **_kwargs: Any) -> "_VariantQuery":
        return self

    def all(self) -> List[Any]:
        return list(self._db.rows)


class _VariantDB:
    def __init__(self) -> None:
        self.rows: List[Any] = []
        self._next_id = 1

    def query(self, model: Any) -> _VariantQuery:
        return _VariantQuery(self)

    def add(self, obj: Any) -> None:
        if getattr(obj, "id", None) is None:
            obj.id = self._next_id
            self._next_id += 1
        self.rows.append(obj)

    def flush(self) -> None:
        return None


class _ProductVariant:
    def __init__(self, **kwargs: Any) -> None:
        for key, value in kwargs.items():
            setattr(self, key, value)
        if not hasattr(self, "id"):
            self.id = None


@pytest.fixture(autouse=True)
def _patch_product_variant(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        "services.store_sync.ProductVariant",
        _ProductVariant,
        raising=False,
    )
    import services.store_sync as store_sync_mod
    store_sync_mod.ProductVariant = _ProductVariant  # type: ignore[attr-defined]


def test_upsert_five_variants_soft_prunes_default(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("CATALOG_VARIANT_SYNC", "true")
    db = _VariantDB()
    product = SimpleNamespace(
        id=100,
        tenant_id=50,
        source="salla",
        external_id="88001",
        meta_retailer_id=None,
        sku="PARENT-SKU",
        has_variants=False,
        default_variant_id=None,
    )
    default_variant = _ProductVariant(
        id=1,
        tenant_id=50,
        product_id=100,
        salla_variant_id=None,
        is_default=True,
        in_stock=True,
        retailer_id="88001",
        price="99",
        stock_quantity=5,
    )
    db.rows.append(default_variant)

    adapter = _adapter()
    product_options = [
        {
            "id": 10,
            "name": "المقاس",
            "values": [
                {"id": 2019873167, "name": "S"},
                {"id": 1245835400, "name": "M"},
            ],
        }
    ]
    variant_dicts = [
        v.model_dump()
        for v in (
            adapter._normalize_variant(raw, product_options)
            for raw in _five_variant_payloads()
        )
    ]
    normalised = {
        "external_id": "88001",
        "price": "59",
        "currency": "SAR",
        "in_stock": True,
        "stock_qty": 5,
        "variants": variant_dicts,
        "options": product_options,
    }

    _upsert_variants_for(db, product, normalised)

    real_variants = [r for r in db.rows if r.salla_variant_id]
    assert len(real_variants) == 5
    assert default_variant.in_stock is False
    assert all(r.retailer_id == f"88001-{r.salla_variant_id}" for r in real_variants)
    real_variants[0].retailer_id = f"nahla_v_{real_variants[0].id}"
    _upsert_variants_for(db, product, normalised)
    real_variants = [r for r in db.rows if r.salla_variant_id]
    assert all(r.retailer_id == f"88001-{r.salla_variant_id}" for r in real_variants)
    assert real_variants[0].extra_metadata["sale_price"] == "59.0"
    assert real_variants[0].extra_metadata["regular_price"] == "119.0"
    assert real_variants[0].options == {"المقاس": "S"}


def test_upsert_without_variants_keeps_synthetic_default(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("CATALOG_VARIANT_SYNC", "true")
    db = _VariantDB()
    product = SimpleNamespace(
        id=200,
        tenant_id=60,
        external_id="99002",
        meta_retailer_id=None,
        sku="SKU-ONLY",
        has_variants=False,
        default_variant_id=None,
    )
    normalised = {
        "external_id": "99002",
        "price": "120",
        "currency": "SAR",
        "in_stock": True,
        "stock_qty": 4,
        "variants": [],
    }

    _upsert_variants_for(db, product, normalised)

    assert len(db.rows) == 1
    row = db.rows[0]
    assert row.is_default is True
    assert row.salla_variant_id is None
    assert row.in_stock is True


def test_resolve_variant_retailer_id_uses_external_and_salla_id():
    parent = SimpleNamespace(source="salla", external_id="88001", meta_retailer_id="override-x")
    assert _resolve_variant_retailer_id(parent, 7, "591539870") == "88001-591539870"


def test_resolve_variant_retailer_id_ignores_legacy_override_for_salla():
    parent = SimpleNamespace(source="salla", external_id="863278879", meta_retailer_id="legacy-845296417")
    assert _resolve_variant_retailer_id(parent, 217, "845296417") == "863278879-845296417"


def test_enrich_normalised_variants_from_adapter():
    from services.store_sync import StoreSyncService

    class _FakeAdapter:
        async def get_raw_variants(self, product_id: str):
            assert product_id == "88001"
            return _five_variant_payloads()

        def _normalize_variant(self, raw, product_options):
            return _adapter()._normalize_variant(raw, product_options)

    service = StoreSyncService(db=SimpleNamespace(), tenant_id=50)
    normalised = {
        "external_id": "88001",
        "options": [{"id": 1, "name": "المقاس", "values": [{"id": 2019873167, "name": "S"}]}],
        "variants": [],
    }
    asyncio.run(
        service._enrich_normalised_variants_from_adapter(_FakeAdapter(), normalised)
    )
    assert len(normalised["variants"]) == 5
    assert normalised["variants"][0]["sale_price"] == "59.0"

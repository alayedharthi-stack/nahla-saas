"""Salla catalog sync — product + variant image persistence."""
from __future__ import annotations

import asyncio
import sys
from pathlib import Path

from sqlalchemy import JSON, create_engine
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import sessionmaker

REPO_ROOT = Path(__file__).resolve().parents[1]
BACKEND_DIR = REPO_ROOT / "backend"
DATABASE_DIR = REPO_ROOT / "database"
for _p in (REPO_ROOT, BACKEND_DIR, DATABASE_DIR):
    if str(_p) not in sys.path:
        sys.path.insert(0, str(_p))

from core.catalog_image import (  # noqa: E402
    coerce_image_url,
    extract_sync_additional_images,
    extract_sync_product_image,
)
from models import Base, Product, ProductVariant, Tenant  # noqa: E402
from services.store_sync import (  # noqa: E402
    StoreSyncService,
    _normalise_product,
    _preserve_existing_product_images,
    _upsert_variants_for,
)
from store_adapters.salla_adapter import SallaAdapter  # noqa: E402
from store_integration.models import NormalizedProduct, NormalizedVariant  # noqa: E402


def _make_db():
    engine = create_engine("sqlite:///:memory:")
    saved: list = []
    for table in Base.metadata.sorted_tables:
        for col in table.columns:
            if isinstance(col.type, JSONB):
                saved.append((col, col.type))
                col.type = JSON()
    Base.metadata.create_all(engine)
    for col, orig in saved:
        col.type = orig
    return sessionmaker(bind=engine)(), engine


def _seed_product(db):
    t = Tenant(name="T-img", is_active=True)
    db.add(t)
    db.commit()
    db.refresh(t)
    p = Product(
        tenant_id=t.id,
        title="Honey",
        external_id="salla-99",
        price="100",
        in_stock=True,
        source="salla",
    )
    db.add(p)
    db.commit()
    db.refresh(p)
    return p


class TestExtractSyncProductImage:
    def test_main_image_string(self):
        assert extract_sync_product_image({
            "main_image": "https://cdn.salla.sa/p.jpg",
        }) == "https://cdn.salla.sa/p.jpg"

    def test_thumbnail_object(self):
        assert extract_sync_product_image({
            "thumbnail": {"url": "https://cdn.salla.sa/thumb.jpg"},
        }) == "https://cdn.salla.sa/thumb.jpg"

    def test_images_array_fallback(self):
        assert extract_sync_product_image({
            "images": [
                {"url": "https://cdn.salla.sa/g1.jpg"},
                {"url": "https://cdn.salla.sa/g2.jpg"},
            ],
        }) == "https://cdn.salla.sa/g1.jpg"

    def test_adapter_image_url_preserved(self):
        product = NormalizedProduct(
            id="1",
            title="Jar",
            image_url="https://cdn.salla.sa/from-adapter.jpg",
        )
        assert extract_sync_product_image(product) == "https://cdn.salla.sa/from-adapter.jpg"

    def test_additional_images_excludes_primary(self):
        primary = "https://cdn.salla.sa/g1.jpg"
        extra = extract_sync_additional_images({
            "images": [
                {"url": primary},
                {"url": "https://cdn.salla.sa/g2.jpg"},
            ],
        }, primary=primary)
        assert extra == ["https://cdn.salla.sa/g2.jpg"]


class TestNormaliseProductImages:
    def test_normalized_product_image_url_lands_in_extra_metadata_shape(self):
        raw = NormalizedProduct(
            id="42",
            title="Sidr",
            image_url="https://cdn.salla.sa/sidr.jpg",
            product_url="https://shop.example.com/sidr",
            variants=[
                NormalizedVariant(id="v1", title="250g", price=90.0),
            ],
        )
        out = _normalise_product(raw)
        assert out["image_url"] == "https://cdn.salla.sa/sidr.jpg"
        assert out["product_url"] == "https://shop.example.com/sidr"
        assert out["variants"][0]["id"] == "v1"

    def test_salla_webhook_thumbnail_object(self):
        out = _normalise_product({
            "id": "77",
            "name": "Tea",
            "thumbnail": {"url": "https://cdn.salla.sa/tea.jpg"},
        })
        assert out["image_url"] == "https://cdn.salla.sa/tea.jpg"
        assert isinstance(out["image_url"], str)

    def test_missing_image_does_not_break_sync(self):
        out = _normalise_product({"id": "1", "title": "No image"})
        assert out["image_url"] == ""
        assert out["external_id"] == "1"


class TestSallaAdapterNormalize:
    def test_normalize_product_main_image(self):
        adapter = SallaAdapter(api_key="test")
        product = adapter._normalize_product({
            "id": 10,
            "name": "Honey",
            "price": {"amount": 120, "currency": "SAR"},
            "main_image": "https://cdn.salla.sa/honey.jpg",
            "url": "https://shop.example/honey",
        })
        assert product.image_url == "https://cdn.salla.sa/honey.jpg"

    def test_normalize_variant_image(self):
        adapter = SallaAdapter(api_key="test")
        variant = adapter._normalize_variant({
            "id": 201,
            "name": "500g",
            "image": {"url": "https://cdn.salla.sa/500g.jpg"},
            "available": True,
            "quantity": 3,
        })
        assert variant.image_url == "https://cdn.salla.sa/500g.jpg"


class TestVariantImageUpsert:
    def test_variant_image_url_persisted(self):
        db, _ = _make_db()
        p = _seed_product(db)
        _upsert_variants_for(db, p, {
            "external_id": "salla-99",
            "title": "Honey",
            "price": "100",
            "currency": "SAR",
            "in_stock": True,
            "image_url": "https://cdn.salla.sa/parent.jpg",
            "variants": [
                {
                    "id": "v250",
                    "title": "250g",
                    "price": 90,
                    "currency": "SAR",
                    "stock_quantity": 5,
                    "in_stock": True,
                    "image_url": "https://cdn.salla.sa/250g.jpg",
                    "options": {"size": "250g"},
                },
            ],
        })
        db.commit()
        row = db.query(ProductVariant).filter_by(product_id=p.id).one()
        assert row.image_url == "https://cdn.salla.sa/250g.jpg"
        assert row.option_summary == "250g"
        assert row.price == "90"
        assert row.currency == "SAR"
        assert row.stock_quantity == 5
        assert row.retailer_id == "salla-99-v250"

    def test_variant_without_image_does_not_copy_parent_to_column(self):
        db, _ = _make_db()
        p = _seed_product(db)
        _upsert_variants_for(db, p, {
            "external_id": "salla-99",
            "price": "100",
            "currency": "SAR",
            "in_stock": True,
            "image_url": "https://cdn.salla.sa/parent.jpg",
            "variants": [{"id": "v1", "title": "M", "in_stock": True}],
        })
        db.commit()
        row = db.query(ProductVariant).filter_by(product_id=p.id).one()
        assert row.image_url is None

    def test_resync_preserves_retailer_id_and_updates_price(self):
        db, _ = _make_db()
        p = _seed_product(db)
        base = {
            "external_id": "salla-99",
            "price": "100",
            "currency": "SAR",
            "in_stock": True,
            "image_url": "https://cdn.salla.sa/p.jpg",
            "variants": [{
                "id": "v1",
                "title": "1kg",
                "price": 150,
                "currency": "SAR",
                "stock_quantity": 2,
                "in_stock": True,
                "image_url": "https://cdn.salla.sa/1kg.jpg",
            }],
        }
        _upsert_variants_for(db, p, base)
        db.commit()
        first = db.query(ProductVariant).filter_by(product_id=p.id).one()
        rid = first.retailer_id
        pk = first.id

        second = dict(base)
        second["variants"] = [{
            **base["variants"][0],
            "price": 175,
            "stock_quantity": 0,
            "in_stock": False,
        }]
        _upsert_variants_for(db, p, second)
        db.commit()
        row = db.query(ProductVariant).filter_by(product_id=p.id).one()
        assert row.id == pk
        assert row.retailer_id == rid
        assert row.price == "175"
        assert row.in_stock is False
        assert row.stock_quantity == 0


class TestPreserveExistingProductImages:
    def test_preserves_image_url_when_sync_omits_image(self):
        normalised = {"image_url": "", "additional_images": []}
        _preserve_existing_product_images(
            normalised,
            {
                "image_url": "https://cdn.salla.sa/old.jpg",
                "additional_images": ["https://cdn.salla.sa/extra.jpg"],
            },
        )
        assert normalised["image_url"] == "https://cdn.salla.sa/old.jpg"
        assert normalised["additional_images"] == ["https://cdn.salla.sa/extra.jpg"]

    def test_replaces_image_url_when_sync_has_valid_image(self):
        normalised = {"image_url": "https://cdn.salla.sa/new.jpg", "additional_images": []}
        _preserve_existing_product_images(
            normalised,
            {"image_url": "https://cdn.salla.sa/old.jpg"},
        )
        assert normalised["image_url"] == "https://cdn.salla.sa/new.jpg"

    def test_preserves_additional_images_when_sync_returns_empty_list(self):
        normalised = {
            "image_url": "https://cdn.salla.sa/p.jpg",
            "additional_images": [],
        }
        _preserve_existing_product_images(
            normalised,
            {"additional_images": ["https://cdn.salla.sa/a.jpg", "https://cdn.salla.sa/b.jpg"]},
        )
        assert normalised["additional_images"] == [
            "https://cdn.salla.sa/a.jpg",
            "https://cdn.salla.sa/b.jpg",
        ]


class TestApplyNormalisedProductImagePreservation:
    def _svc(self, db, tenant_id: int) -> StoreSyncService:
        svc = StoreSyncService(db, tenant_id)
        svc._adapter = None
        return svc

    def test_update_without_image_preserves_existing(self):
        db, _ = _make_db()
        t = Tenant(name="T-preserve", is_active=True)
        db.add(t)
        db.commit()
        db.refresh(t)
        old_url = "https://cdn.salla.sa/shoe-old.jpg"
        p = Product(
            tenant_id=t.id,
            title="حذاء رياضي أبيض",
            external_id="407525900",
            price="199",
            in_stock=True,
            source="salla",
            extra_metadata={
                "image_url": old_url,
                "additional_images": ["https://cdn.salla.sa/shoe-alt.jpg"],
            },
        )
        db.add(p)
        db.commit()
        db.refresh(p)

        normalised = _normalise_product({"id": "407525900", "name": "حذاء رياضي أبيض"})
        assert normalised["image_url"] == ""

        result = self._svc(db, t.id)._apply_normalised_product(normalised, "salla")
        db.commit()
        db.refresh(p)

        assert result["action"] == "updated"
        assert p.extra_metadata["image_url"] == old_url
        assert p.extra_metadata["additional_images"] == ["https://cdn.salla.sa/shoe-alt.jpg"]

    def test_update_with_new_image_replaces_old(self):
        db, _ = _make_db()
        t = Tenant(name="T-replace", is_active=True)
        db.add(t)
        db.commit()
        db.refresh(t)
        p = Product(
            tenant_id=t.id,
            title="قميص قطني أزرق",
            external_id="shirt-1",
            price="89",
            in_stock=True,
            source="salla",
            extra_metadata={"image_url": "https://cdn.salla.sa/old-shirt.jpg"},
        )
        db.add(p)
        db.commit()
        db.refresh(p)

        new_url = "https://cdn.salla.sa/new-shirt.jpg"
        normalised = _normalise_product({
            "id": "shirt-1",
            "name": "قميص قطني أزرق",
            "main_image": new_url,
        })
        self._svc(db, t.id)._apply_normalised_product(normalised, "salla")
        db.commit()
        db.refresh(p)

        assert p.extra_metadata["image_url"] == new_url

    def test_create_without_image_stays_empty(self):
        db, _ = _make_db()
        t = Tenant(name="T-create", is_active=True)
        db.add(t)
        db.commit()
        db.refresh(t)

        normalised = _normalise_product({"id": "new-no-img", "name": "عطر ورد 100ml"})
        result = self._svc(db, t.id)._apply_normalised_product(normalised, "salla")
        db.commit()

        row = db.query(Product).filter_by(id=result["product_id"]).one()
        assert result["action"] == "created"
        assert row.extra_metadata["image_url"] == ""


class TestWebhookProductImagePreservation:
    def test_webhook_update_without_image_preserves_existing(self):
        db, _ = _make_db()
        t = Tenant(name="T-webhook", is_active=True)
        db.add(t)
        db.commit()
        db.refresh(t)
        old_url = "https://cdn.salla.sa/webhook-old.jpg"
        p = Product(
            tenant_id=t.id,
            title="منتج تجريبي عام",
            external_id="wh-40",
            price="120",
            in_stock=True,
            source="salla",
            extra_metadata={"image_url": old_url},
        )
        db.add(p)
        db.commit()
        db.refresh(p)

        svc = StoreSyncService(db, t.id)
        svc._get_adapter = lambda: None
        asyncio.run(svc.handle_product_webhook({"id": "wh-40", "name": "منتج تجريبي عام"}))

        db.refresh(p)
        assert p.extra_metadata["image_url"] == old_url

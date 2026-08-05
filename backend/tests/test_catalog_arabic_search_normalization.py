"""SQLite integration: Arabic orthographic catalog search normalization."""
from __future__ import annotations

import sys
from pathlib import Path
from typing import Any, Tuple

import pytest
from sqlalchemy import JSON, create_engine
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import sessionmaker

REPO_ROOT = Path(__file__).resolve().parents[2]
BACKEND_DIR = REPO_ROOT / "backend"
DATABASE_DIR = REPO_ROOT / "database"
for _p in (REPO_ROOT, BACKEND_DIR, DATABASE_DIR):
    if str(_p) not in sys.path:
        sys.path.insert(0, str(_p))

from core.store_knowledge import (  # noqa: E402
    CatalogContextBuilder,
    CatalogSearchProductsResult,
)
from models import Base, Product, Tenant  # noqa: E402
from modules.ai.brain.execution.search import (  # noqa: E402
    resolve_search_result_product_for_focus,
)
from modules.ai.brain.commerce.commerce_focus_owner import product_focus_identity  # noqa: E402


def _make_db() -> Tuple[Any, Any]:
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
    Session = sessionmaker(bind=engine)
    return Session(), engine


def _seed_tenant(db, name: str = "T") -> Tenant:
    tenant = Tenant(name=name, is_active=True)
    db.add(tenant)
    db.commit()
    db.refresh(tenant)
    return tenant


def _seed_product(
    db,
    tenant_id: int,
    *,
    title: str,
    external_id: str,
    in_stock: bool = True,
    stock_quantity: int | None = 10,
    price: str = "99",
) -> Product:
    product = Product(
        tenant_id=tenant_id,
        title=title,
        external_id=external_id,
        price=price,
        in_stock=in_stock,
        stock_quantity=stock_quantity if in_stock else 0,
        catalog_status="active",
        extra_metadata={
            "in_stock": in_stock,
            "stock_qty": stock_quantity if in_stock else 0,
            "status": "active",
        },
    )
    db.add(product)
    db.commit()
    db.refresh(product)
    return product


class TestCatalogArabicSearchNormalization:
    def test_oos_shirt_alef_variant_returns_single_fact(self) -> None:
        db, _ = _make_db()
        tenant = _seed_tenant(db)
        _seed_product(
            db,
            tenant.id,
            title="قميص قطني أزرق",
            external_id="sku-shirt-blue",
            in_stock=False,
            stock_quantity=0,
            price="89",
        )
        builder = CatalogContextBuilder(db, tenant_id=tenant.id)
        result = builder.search_products(
            "قميص قطني ازرق",
            include_non_orderable_facts=True,
        )
        assert isinstance(result, CatalogSearchProductsResult)
        assert result.products == []
        assert len(result.catalog_fact_products) == 1
        fact = result.catalog_fact_products[0]
        assert fact["external_id"] == "sku-shirt-blue"
        assert fact["title"] == "قميص قطني أزرق"
        assert fact["can_checkout"] is False
        assert fact["in_stock"] is False

    def test_orderable_bag_ta_marbuta_variant_in_products(self) -> None:
        db, _ = _make_db()
        tenant = _seed_tenant(db)
        _seed_product(
            db,
            tenant.id,
            title="حقيبة يدوية بنية",
            external_id="sku-bag-brown",
            in_stock=True,
            price="120",
        )
        builder = CatalogContextBuilder(db, tenant_id=tenant.id)
        result = builder.search_products("حقيبه يدويه بنيه")
        assert isinstance(result, list)
        assert len(result) == 1
        assert result[0]["external_id"] == "sku-bag-brown"
        assert result[0]["can_checkout"] is True

    def test_orderable_watch_ya_variant_in_products(self) -> None:
        db, _ = _make_db()
        tenant = _seed_tenant(db)
        _seed_product(
            db,
            tenant.id,
            title="ساعة فضية كلاسيك",
            external_id="sku-watch-silver",
            in_stock=True,
            price="220",
        )
        builder = CatalogContextBuilder(db, tenant_id=tenant.id)
        result = builder.search_products("ساعه فضيه كلاسيك")
        assert isinstance(result, list)
        assert len(result) == 1
        assert result[0]["external_id"] == "sku-watch-silver"

    def test_tenant_isolation_for_normalized_title(self) -> None:
        db, _ = _make_db()
        tenant_a = _seed_tenant(db, "A")
        tenant_b = _seed_tenant(db, "B")
        _seed_product(
            db,
            tenant_a.id,
            title="قميص قطني أزرق",
            external_id="sku-a-shirt",
            in_stock=False,
            stock_quantity=0,
        )
        _seed_product(
            db,
            tenant_b.id,
            title="قميص قطني أزرق",
            external_id="sku-b-shirt",
            in_stock=False,
            stock_quantity=0,
        )
        builder_a = CatalogContextBuilder(db, tenant_id=tenant_a.id)
        result = builder_a.search_products(
            "قميص قطني ازرق",
            include_non_orderable_facts=True,
        )
        assert isinstance(result, CatalogSearchProductsResult)
        assert len(result.catalog_fact_products) == 1
        assert result.catalog_fact_products[0]["external_id"] == "sku-a-shirt"

    def test_multiple_normalized_matches_not_singularized(self) -> None:
        db, _ = _make_db()
        tenant = _seed_tenant(db)
        _seed_product(
            db,
            tenant.id,
            title="عطر ورد 100ml",
            external_id="sku-rose-a",
            in_stock=False,
            stock_quantity=0,
        )
        _seed_product(
            db,
            tenant.id,
            title="عطر ورد 50ml",
            external_id="sku-rose-b",
            in_stock=False,
            stock_quantity=0,
        )
        builder = CatalogContextBuilder(db, tenant_id=tenant.id)
        result = builder.search_products(
            "عطر ورد",
            include_non_orderable_facts=True,
        )
        assert isinstance(result, CatalogSearchProductsResult)
        assert result.products == []
        assert len(result.catalog_fact_products) == 2
        ext_ids = {f["external_id"] for f in result.catalog_fact_products}
        assert ext_ids == {"sku-rose-a", "sku-rose-b"}

    def test_latin_query_unchanged(self) -> None:
        db, _ = _make_db()
        tenant = _seed_tenant(db)
        _seed_product(
            db,
            tenant.id,
            title="Classic Rose Perfume",
            external_id="sku-latin-rose",
            in_stock=True,
        )
        builder = CatalogContextBuilder(db, tenant_id=tenant.id)
        result = builder.search_products("Classic Rose")
        assert isinstance(result, list)
        assert len(result) == 1
        assert result[0]["external_id"] == "sku-latin-rose"

    def test_latin_query_no_false_match_from_arabic_norm(self) -> None:
        db, _ = _make_db()
        tenant = _seed_tenant(db)
        _seed_product(
            db,
            tenant.id,
            title="قميص قطني أزرق",
            external_id="sku-shirt-blue",
            in_stock=False,
            stock_quantity=0,
        )
        builder = CatalogContextBuilder(db, tenant_id=tenant.id)
        result = builder.search_products(
            "blue cotton",
            include_non_orderable_facts=True,
        )
        assert isinstance(result, CatalogSearchProductsResult)
        assert result.products == []
        assert result.catalog_fact_products == []


class TestCatalogArabicSearchFocusIntegration:
    def test_singular_oos_fact_with_orthographic_query_exports_focus(self) -> None:
        db, _ = _make_db()
        tenant = _seed_tenant(db)
        _seed_product(
            db,
            tenant.id,
            title="قميص قطني أزرق",
            external_id="sku-shirt-blue",
            in_stock=False,
            stock_quantity=0,
            price="89",
        )
        builder = CatalogContextBuilder(db, tenant_id=tenant.id)
        search = builder.search_products(
            "قميص قطني ازرق",
            include_non_orderable_facts=True,
        )
        assert isinstance(search, CatalogSearchProductsResult)
        resolved = resolve_search_result_product_for_focus(
            products=search.products,
            catalog_fact_products=search.catalog_fact_products,
            query="قميص قطني ازرق",
        )
        assert resolved is not None
        assert product_focus_identity(resolved) == "sku-shirt-blue"

    def test_generic_category_query_does_not_export_focus(self) -> None:
        db, _ = _make_db()
        tenant = _seed_tenant(db)
        _seed_product(
            db,
            tenant.id,
            title="عطر",
            external_id="sku-perfume-generic",
            in_stock=False,
            stock_quantity=0,
        )
        builder = CatalogContextBuilder(db, tenant_id=tenant.id)
        search = builder.search_products(
            "عطر",
            include_non_orderable_facts=True,
        )
        assert isinstance(search, CatalogSearchProductsResult)
        resolved = resolve_search_result_product_for_focus(
            products=search.products,
            catalog_fact_products=search.catalog_fact_products,
            query="عطر",
        )
        assert resolved is None

    def test_multiple_facts_do_not_export_focus(self) -> None:
        db, _ = _make_db()
        tenant = _seed_tenant(db)
        _seed_product(
            db,
            tenant.id,
            title="عطر ورد 100ml",
            external_id="sku-rose-a",
            in_stock=False,
            stock_quantity=0,
        )
        _seed_product(
            db,
            tenant.id,
            title="عطر ورد 50ml",
            external_id="sku-rose-b",
            in_stock=False,
            stock_quantity=0,
        )
        builder = CatalogContextBuilder(db, tenant_id=tenant.id)
        search = builder.search_products(
            "عطر ورد",
            include_non_orderable_facts=True,
        )
        assert isinstance(search, CatalogSearchProductsResult)
        resolved = resolve_search_result_product_for_focus(
            products=search.products,
            catalog_fact_products=search.catalog_fact_products,
            query="عطر ورد",
        )
        assert resolved is None

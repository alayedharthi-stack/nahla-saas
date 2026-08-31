"""Persisted identity keys for Meta catalog CREATE-guard.

Uses isolated Postgres at WA_CATALOG_SYNC_PG_TEST_DATABASE_URL only.
Never DATABASE_URL. Skips when the DSN is absent.
"""
from __future__ import annotations

import os
import sys
from pathlib import Path

import pytest
from sqlalchemy import create_engine, text
from sqlalchemy.engine import Engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import NullPool

_REPO_ROOT = Path(__file__).resolve().parents[2]
_BACKEND = _REPO_ROOT / "backend"
_DATABASE = _REPO_ROOT / "database"
for _entry in (str(_REPO_ROOT), str(_BACKEND), str(_DATABASE)):
    if _entry not in sys.path:
        sys.path.insert(0, _entry)

_TEST_TENANT = 880_992


def _is_postgres_url(url: str) -> bool:
    return url.split(":", 1)[0].lower().startswith("postgres")


@pytest.fixture
def postgres_engine() -> Engine:
    explicit = (os.getenv("WA_CATALOG_SYNC_PG_TEST_DATABASE_URL") or "").strip()
    if not explicit or not _is_postgres_url(explicit):
        message = (
            "WA_CATALOG_SYNC_PG_TEST_DATABASE_URL is required for isolated "
            "identity tests. DATABASE_URL is ignored and is never used."
        )
        if (os.getenv("WA_CATALOG_SYNC_PG_REQUIRED") or "").strip() == "1":
            pytest.fail(message)
        pytest.skip(message)
    try:
        engine = create_engine(explicit, poolclass=NullPool, pool_pre_ping=True)
        with engine.connect() as conn:
            conn.execute(text("SELECT 1"))
        return engine
    except Exception as exc:  # noqa: BLE001
        message = (
            "PostgreSQL unavailable for Meta catalog identity tests "
            f"at WA_CATALOG_SYNC_PG_TEST_DATABASE_URL: {exc}"
        )
        if (os.getenv("WA_CATALOG_SYNC_PG_REQUIRED") or "").strip() == "1":
            pytest.fail(message)
        pytest.skip(message)


def _ensure_orm_tables(engine: Engine) -> None:
    from database.models import Product, ProductVariant, Tenant

    Tenant.__table__.create(bind=engine, checkfirst=True)
    Product.__table__.create(bind=engine, checkfirst=True)
    ProductVariant.__table__.create(bind=engine, checkfirst=True)


def _cleanup(session) -> None:
    session.rollback()
    session.execute(text("DELETE FROM product_variants WHERE tenant_id = :tid"), {"tid": _TEST_TENANT})
    session.execute(text("DELETE FROM products WHERE tenant_id = :tid"), {"tid": _TEST_TENANT})
    session.execute(text("DELETE FROM tenants WHERE id = :tid"), {"tid": _TEST_TENANT})
    session.commit()


def test_legacy_identity_keys_from_persisted_variants(postgres_engine: Engine) -> None:
    from database.models import Product, ProductVariant, Tenant
    from services.meta_catalog_identity import (
        existing_identity_retailer_id,
        legacy_identity_retailer_ids,
        parent_would_create_in_meta,
    )

    _ensure_orm_tables(postgres_engine)
    Session = sessionmaker(bind=postgres_engine)
    db = Session()
    try:
        _cleanup(db)
        db.add(Tenant(id=_TEST_TENANT, name=f"identity-{_TEST_TENANT}"))
        db.flush()

        exact = Product(
            tenant_id=_TEST_TENANT,
            title="قميص قطني أزرق",
            source="salla",
            ownership_mode="external_managed",
            catalog_status="active",
            external_id="88001",
            extra_metadata={"currency": "SAR"},
        )
        db.add(exact)
        db.flush()
        db.add(ProductVariant(
            tenant_id=_TEST_TENANT,
            product_id=exact.id,
            salla_variant_id="591001",
            retailer_id="88001-591001",
            is_default=False,
        ))
        db.add(ProductVariant(
            tenant_id=_TEST_TENANT,
            product_id=exact.id,
            salla_variant_id=None,
            retailer_id="88001",
            is_default=True,
        ))

        missing = Product(
            tenant_id=_TEST_TENANT,
            title="حذاء رياضي أبيض",
            source="salla",
            ownership_mode="external_managed",
            catalog_status="active",
            external_id="99001",
            extra_metadata={"currency": "SAR"},
        )
        db.add(missing)
        db.flush()
        db.add(ProductVariant(
            tenant_id=_TEST_TENANT,
            product_id=missing.id,
            salla_variant_id="1",
            retailer_id="99001-1",
            is_default=True,
        ))

        ambiguous = Product(
            tenant_id=_TEST_TENANT,
            title="عطر ورد 100ml",
            source="salla",
            ownership_mode="external_managed",
            catalog_status="active",
            external_id="77001",
            meta_retailer_id="legacy-77001",
            extra_metadata={"currency": "SAR"},
        )
        db.add(ambiguous)
        db.flush()
        db.add(ProductVariant(
            tenant_id=_TEST_TENANT,
            product_id=ambiguous.id,
            salla_variant_id="A",
            retailer_id="77001-A",
            is_default=False,
        ))
        db.add(ProductVariant(
            tenant_id=_TEST_TENANT,
            product_id=ambiguous.id,
            salla_variant_id="B",
            retailer_id="77001-B",
            is_default=False,
        ))
        db.commit()

        db.refresh(exact)
        db.refresh(missing)
        db.refresh(ambiguous)

        exact_keys = legacy_identity_retailer_ids(exact, exclude_rid="88001")
        assert "88001-591001" in exact_keys
        assert "88001" not in exact_keys
        assert exact.title not in exact_keys

        exact_variants = [v for v in (exact.variants or [])]
        live_exact = {"88001-591001"}
        assert existing_identity_retailer_id(
            exact, live_exact, current_rid="88001", variants=exact_variants,
        ) == "88001-591001"
        assert parent_would_create_in_meta(exact, live_exact, variants=exact_variants) is False
        assert parent_would_create_in_meta(exact, live_exact, variants=exact_variants) is False

        missing_keys = legacy_identity_retailer_ids(missing, exclude_rid="99001-1")
        assert "99001" in missing_keys
        assert "99001-1" not in missing_keys
        missing_variants = [v for v in (missing.variants or [])]
        assert parent_would_create_in_meta(
            missing, live_exact, variants=missing_variants,
        ) is True
        assert parent_would_create_in_meta(
            missing, set(), variants=missing_variants,
        ) is True

        ambiguous_keys = legacy_identity_retailer_ids(ambiguous, exclude_rid="77001-A")
        assert "legacy-77001" in ambiguous_keys
        assert "77001-B" in ambiguous_keys
        assert "77001" in ambiguous_keys
        assert len(ambiguous_keys) >= 3
    finally:
        _cleanup(db)
        db.close()


def _payload(**overrides):
    body = {
        "price": 12000,
        "currency": "SAR",
        "availability": "in stock",
        "url": "https://store.example/p/88001",
        "image_url": "https://cdn.example/shirt.jpg",
        "name": "قميص قطني أزرق",
    }
    body.update(overrides)
    return body


def _live(meta_id="META-SIB", rid="88001-591001", **overrides):
    item = {
        "id": meta_id,
        "retailer_id": rid,
        "price": 12000,
        "currency": "SAR",
        "availability": "in stock",
        "url": "https://store.example/p/88001",
        "image_url": "https://scontent.xx.fbcdn.net/signed.jpg",
        "name": "اسم مختلف عن المحلي",
    }
    item.update(overrides)
    return item


def test_canonical_sibling_bind_gates_postgres(postgres_engine: Engine) -> None:
    from database.models import Product, ProductVariant, Tenant
    from services.meta_catalog_identity import (
        ACTION_BLOCK,
        ACTION_CREATE,
        ACTION_LINK,
        CANONICAL_SIBLING_RULE,
        ERROR_AMBIGUOUS_SIBLING,
        IDENTITY_CANONICAL_SIBLING,
        REASON_CONTENT,
        REASON_FOREIGN_META,
        REASON_LINEAGE,
        REASON_MULTIPLE,
        evaluate_canonical_sibling_bind,
        occupied_active_meta_item_ids,
    )

    _ensure_orm_tables(postgres_engine)
    Session = sessionmaker(bind=postgres_engine)
    db = Session()
    try:
        _cleanup(db)
        db.add(Tenant(id=_TEST_TENANT, name=f"canonical-sib-{_TEST_TENANT}"))
        db.flush()

        parent = Product(
            tenant_id=_TEST_TENANT,
            title="قميص قطني أزرق",
            source="salla",
            ownership_mode="external_managed",
            catalog_status="active",
            external_id="88001",
            extra_metadata={"currency": "SAR", "product_url": "https://store.example/p/88001"},
        )
        db.add(parent)
        db.flush()
        db.add(ProductVariant(
            tenant_id=_TEST_TENANT,
            product_id=parent.id,
            salla_variant_id="591001",
            retailer_id="88001-591001",
            price="120",
            currency="SAR",
            is_default=False,
        ))
        db.add(ProductVariant(
            tenant_id=_TEST_TENANT,
            product_id=parent.id,
            salla_variant_id=None,
            retailer_id="88001",
            is_default=True,
        ))

        other = Product(
            tenant_id=_TEST_TENANT,
            title="حذاء رياضي أبيض",
            source="salla",
            ownership_mode="external_managed",
            catalog_status="active",
            external_id="99001",
            meta_item_id="META-TAKEN",
            extra_metadata={"currency": "SAR"},
        )
        db.add(other)
        archived = Product(
            tenant_id=_TEST_TENANT,
            title="عطر ورد 100ml",
            source="salla",
            ownership_mode="external_managed",
            catalog_status="removed_from_meta",
            external_id="77001",
            meta_item_id="META-SIB",
            extra_metadata={"currency": "SAR"},
        )
        db.add(archived)
        db.commit()
        db.refresh(parent)
        db.refresh(other)
        db.refresh(archived)
        variants = list(parent.variants or [])

        occupied = occupied_active_meta_item_ids(
            db.query(Product).filter(Product.tenant_id == _TEST_TENANT).all(),
            exclude_product_id=int(parent.id),
        )
        assert occupied.get("META-TAKEN") == int(other.id)
        assert "META-SIB" not in occupied

        payload = _payload()
        unique = evaluate_canonical_sibling_bind(
            parent,
            current_rid="88001",
            variants=variants,
            live_by_rid={"88001-591001": _live()},
            occupied_meta_item_ids=occupied,
            sibling_payloads={"88001-591001": payload},
        )
        assert unique.allow_link is True
        assert unique.action == ACTION_LINK
        assert unique.identity_class == IDENTITY_CANONICAL_SIBLING
        assert unique.meta_product_id == "META-SIB"
        assert unique.canonical_rule == CANONICAL_SIBLING_RULE
        assert unique.error is None

        parent.meta_item_id = "META-SIB"
        again = evaluate_canonical_sibling_bind(
            parent,
            current_rid="88001",
            variants=variants,
            live_by_rid={"88001-591001": _live()},
            occupied_meta_item_ids=occupied,
            sibling_payloads={"88001-591001": payload},
        )
        assert again.allow_link is True
        assert again.idempotent is True
        assert again.meta_product_id == "META-SIB"
        parent.meta_item_id = None

        extra = ProductVariant(
            tenant_id=_TEST_TENANT,
            product_id=parent.id,
            salla_variant_id="591002",
            retailer_id="88001-591002",
            is_default=False,
        )
        db.add(extra)
        db.commit()
        db.refresh(parent)
        variants_two = list(parent.variants or [])
        ambiguous = evaluate_canonical_sibling_bind(
            parent,
            current_rid="88001",
            variants=variants_two,
            live_by_rid={
                "88001-591001": _live(meta_id="META-A", rid="88001-591001"),
                "88001-591002": _live(meta_id="META-B", rid="88001-591002"),
            },
            occupied_meta_item_ids=occupied,
            sibling_payloads={
                "88001-591001": payload,
                "88001-591002": payload,
            },
        )
        assert ambiguous.action == ACTION_BLOCK
        assert ambiguous.error == ERROR_AMBIGUOUS_SIBLING
        assert ambiguous.reason == REASON_MULTIPLE
        assert ambiguous.allow_create is False
        assert ambiguous.allow_link is False

        foreign = evaluate_canonical_sibling_bind(
            parent,
            current_rid="88001",
            variants=variants,
            live_by_rid={"88001-591001": _live(meta_id="META-TAKEN")},
            occupied_meta_item_ids=occupied,
            sibling_payloads={"88001-591001": payload},
        )
        assert foreign.action == ACTION_BLOCK
        assert foreign.reason == REASON_FOREIGN_META
        assert foreign.allow_create is False

        lineage = evaluate_canonical_sibling_bind(
            parent,
            current_rid="88001",
            variants=variants,
            live_by_rid={"88001-591001": _live(retailer_id="99001-1")},
            occupied_meta_item_ids=occupied,
            sibling_payloads={"88001-591001": payload},
        )
        assert lineage.action == ACTION_BLOCK
        assert lineage.reason == REASON_LINEAGE
        assert lineage.allow_create is False

        content = evaluate_canonical_sibling_bind(
            parent,
            current_rid="88001",
            variants=variants,
            live_by_rid={"88001-591001": _live(price=999)},
            occupied_meta_item_ids=occupied,
            sibling_payloads={"88001-591001": payload},
        )
        assert content.action == ACTION_BLOCK
        assert content.reason == REASON_CONTENT
        assert "price" in content.content_mismatches
        assert content.allow_create is False

        missing = evaluate_canonical_sibling_bind(
            parent,
            current_rid="88001",
            variants=variants,
            live_by_rid={"99001-1": _live(meta_id="META-OTHER", rid="99001-1")},
            occupied_meta_item_ids=occupied,
            sibling_payloads={"88001-591001": payload},
        )
        assert missing.action == ACTION_CREATE
        assert missing.allow_link is False

        parent.meta_item_id = "META-SIB"
        stale = evaluate_canonical_sibling_bind(
            parent,
            current_rid="88001",
            variants=variants,
            live_by_rid={},
            occupied_meta_item_ids=occupied,
            sibling_payloads={"88001-591001": payload},
        )
        assert stale.action == ACTION_BLOCK
        assert stale.reason == "bound_identity_unproven"
        assert stale.allow_create is False
    finally:
        _cleanup(db)
        db.close()


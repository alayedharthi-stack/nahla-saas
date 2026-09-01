"""PostgreSQL uniqueness for catalog variant identity.

Uses isolated Postgres at WA_CATALOG_SYNC_PG_TEST_DATABASE_URL only.
"""
from __future__ import annotations

import os
import sys
from datetime import datetime, timezone
from pathlib import Path

import pytest
from sqlalchemy import create_engine, text
from sqlalchemy.engine import Engine
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import NullPool

_REPO_ROOT = Path(__file__).resolve().parents[2]
_BACKEND = _REPO_ROOT / "backend"
_DATABASE = _REPO_ROOT / "database"
for _entry in (str(_REPO_ROOT), str(_BACKEND), str(_DATABASE)):
    if _entry not in sys.path:
        sys.path.insert(0, _entry)

from catalog_membership_uniqueness import (  # noqa: E402
    CREATE_UQ_MEMBERSHIP_META_ITEM_SQL,
    CREATE_UQ_MEMBERSHIP_VARIANT_KEY_SQL,
    DROP_UQ_MEMBERSHIP_META_ITEM_SQL,
    DROP_UQ_MEMBERSHIP_VARIANT_KEY_SQL,
    ERROR_DUPLICATE_CATALOG_IDENTITY_BLOCKED,
    raise_if_duplicate_catalog_identities,
)

_TEST_TENANT_A = 881_001
_TEST_TENANT_B = 881_002


def _is_postgres_url(url: str) -> bool:
    return url.split(":", 1)[0].lower().startswith("postgres")


@pytest.fixture
def postgres_engine() -> Engine:
    explicit = (os.getenv("WA_CATALOG_SYNC_PG_TEST_DATABASE_URL") or "").strip()
    if not explicit or not _is_postgres_url(explicit):
        message = (
            "WA_CATALOG_SYNC_PG_TEST_DATABASE_URL is required for isolated "
            "catalog identity tests. DATABASE_URL is ignored."
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
        message = f"PostgreSQL unavailable for catalog identity tests: {exc}"
        if (os.getenv("WA_CATALOG_SYNC_PG_REQUIRED") or "").strip() == "1":
            pytest.fail(message)
        pytest.skip(message)


def _ensure_tables(engine: Engine) -> None:
    from database.models import MetaCatalogMembership, Product, ProductVariant, Tenant

    Tenant.__table__.create(bind=engine, checkfirst=True)
    Product.__table__.create(bind=engine, checkfirst=True)
    ProductVariant.__table__.create(bind=engine, checkfirst=True)
    MetaCatalogMembership.__table__.create(bind=engine, checkfirst=True)
    col_names = set(MetaCatalogMembership.__table__.columns.keys())
    with engine.begin() as conn:
        if "salla_variant_id" not in col_names:
            conn.execute(
                text(
                    "ALTER TABLE meta_catalog_memberships "
                    "ADD COLUMN IF NOT EXISTS salla_variant_id VARCHAR(64)"
                )
            )
        conn.execute(text(DROP_UQ_MEMBERSHIP_VARIANT_KEY_SQL))
        conn.execute(text(DROP_UQ_MEMBERSHIP_META_ITEM_SQL))
        conn.execute(text(CREATE_UQ_MEMBERSHIP_VARIANT_KEY_SQL))
        conn.execute(text(CREATE_UQ_MEMBERSHIP_META_ITEM_SQL))


def _cleanup(session) -> None:
    session.rollback()
    session.execute(text("DELETE FROM meta_catalog_memberships WHERE tenant_id IN (:a, :b)"), {"a": _TEST_TENANT_A, "b": _TEST_TENANT_B})
    session.execute(text("DELETE FROM product_variants WHERE tenant_id IN (:a, :b)"), {"a": _TEST_TENANT_A, "b": _TEST_TENANT_B})
    session.execute(text("DELETE FROM products WHERE tenant_id IN (:a, :b)"), {"a": _TEST_TENANT_A, "b": _TEST_TENANT_B})
    session.execute(text("DELETE FROM tenants WHERE id IN (:a, :b)"), {"a": _TEST_TENANT_A, "b": _TEST_TENANT_B})
    session.commit()


def _seed(session, tenant_id: int):
    from database.models import Product, Tenant

    if session.get(Tenant, tenant_id) is None:
        session.add(Tenant(id=tenant_id, name=f"ident-{tenant_id}"))
        session.flush()
    product = Product(
        tenant_id=tenant_id,
        title="فستان",
        source="salla",
        ownership_mode="external_managed",
        catalog_status="active",
        external_id="863278879",
    )
    session.add(product)
    session.flush()
    return product


def _membership(session, *, tenant_id, catalog_id, retailer_id, product_id, salla_variant_id, meta_item_id):
    from database.models import MetaCatalogMembership

    row = MetaCatalogMembership(
        tenant_id=tenant_id,
        catalog_id=catalog_id,
        retailer_id=retailer_id,
        product_id=product_id,
        salla_variant_id=salla_variant_id,
        meta_item_id=meta_item_id,
        verified_at=datetime.now(timezone.utc),
        provenance="test",
    )
    session.add(row)
    session.flush()
    return row


def test_duplicate_salla_variant_id_in_same_catalog_is_rejected(postgres_engine):
    _ensure_tables(postgres_engine)
    Session = sessionmaker(bind=postgres_engine)
    session = Session()
    try:
        _cleanup(session)
        product = _seed(session, _TEST_TENANT_A)
        _membership(
            session,
            tenant_id=_TEST_TENANT_A,
            catalog_id="c1",
            retailer_id="863278879-1",
            product_id=product.id,
            salla_variant_id="1",
            meta_item_id="m1",
        )
        session.commit()
        with pytest.raises(IntegrityError):
            _membership(
                session,
                tenant_id=_TEST_TENANT_A,
                catalog_id="c1",
                retailer_id="863278879-1-dup",
                product_id=product.id,
                salla_variant_id="1",
                meta_item_id="m2",
            )
            session.commit()
        session.rollback()
    finally:
        _cleanup(session)
        session.close()


def test_duplicate_meta_item_id_in_same_catalog_is_rejected(postgres_engine):
    _ensure_tables(postgres_engine)
    Session = sessionmaker(bind=postgres_engine)
    session = Session()
    try:
        _cleanup(session)
        product = _seed(session, _TEST_TENANT_A)
        _membership(
            session,
            tenant_id=_TEST_TENANT_A,
            catalog_id="c1",
            retailer_id="863278879-1",
            product_id=product.id,
            salla_variant_id="1",
            meta_item_id="shared-meta",
        )
        session.commit()
        with pytest.raises(IntegrityError):
            _membership(
                session,
                tenant_id=_TEST_TENANT_A,
                catalog_id="c1",
                retailer_id="863278879-2",
                product_id=product.id,
                salla_variant_id="2",
                meta_item_id="shared-meta",
            )
            session.commit()
        session.rollback()
    finally:
        _cleanup(session)
        session.close()


def test_cross_tenant_and_cross_catalog_same_meta_item_allowed(postgres_engine):
    _ensure_tables(postgres_engine)
    Session = sessionmaker(bind=postgres_engine)
    session = Session()
    try:
        _cleanup(session)
        pa = _seed(session, _TEST_TENANT_A)
        pb = _seed(session, _TEST_TENANT_B)
        _membership(
            session,
            tenant_id=_TEST_TENANT_A,
            catalog_id="c1",
            retailer_id="863278879-1",
            product_id=pa.id,
            salla_variant_id="1",
            meta_item_id="meta-x",
        )
        _membership(
            session,
            tenant_id=_TEST_TENANT_B,
            catalog_id="c1",
            retailer_id="863278879-1",
            product_id=pb.id,
            salla_variant_id="1",
            meta_item_id="meta-x",
        )
        _membership(
            session,
            tenant_id=_TEST_TENANT_A,
            catalog_id="c2",
            retailer_id="863278879-1",
            product_id=pa.id,
            salla_variant_id="1",
            meta_item_id="meta-x",
        )
        session.commit()
    finally:
        _cleanup(session)
        session.close()


def test_audit_fails_closed_without_delete(postgres_engine):
    _ensure_tables(postgres_engine)
    with postgres_engine.begin() as conn:
        conn.execute(text(DROP_UQ_MEMBERSHIP_META_ITEM_SQL))
    Session = sessionmaker(bind=postgres_engine)
    session = Session()
    try:
        _cleanup(session)
        product = _seed(session, _TEST_TENANT_A)
        _membership(
            session, tenant_id=_TEST_TENANT_A, catalog_id="c1",
            retailer_id="r1", product_id=product.id, salla_variant_id="1", meta_item_id="dup",
        )
        session.commit()
        with postgres_engine.begin() as conn:
            conn.execute(text(DROP_UQ_MEMBERSHIP_VARIANT_KEY_SQL))
            conn.execute(text(DROP_UQ_MEMBERSHIP_META_ITEM_SQL))
        _membership(
            session, tenant_id=_TEST_TENANT_A, catalog_id="c1",
            retailer_id="r2", product_id=product.id, salla_variant_id="2", meta_item_id="dup",
        )
        session.commit()
        with pytest.raises(RuntimeError, match=ERROR_DUPLICATE_CATALOG_IDENTITY_BLOCKED):
            raise_if_duplicate_catalog_identities(postgres_engine)
        remaining = session.execute(
            text("SELECT COUNT(*) FROM meta_catalog_memberships WHERE tenant_id=:t"),
            {"t": _TEST_TENANT_A},
        ).scalar()
        assert remaining == 2
    finally:
        _cleanup(session)
        session.close()
        with postgres_engine.begin() as conn:
            conn.execute(text(DROP_UQ_MEMBERSHIP_VARIANT_KEY_SQL))
            conn.execute(text(DROP_UQ_MEMBERSHIP_META_ITEM_SQL))
            conn.execute(text(CREATE_UQ_MEMBERSHIP_VARIANT_KEY_SQL))
            conn.execute(text(CREATE_UQ_MEMBERSHIP_META_ITEM_SQL))


def test_duplicate_retailer_id_in_same_catalog_is_rejected(postgres_engine):
    _ensure_tables(postgres_engine)
    Session = sessionmaker(bind=postgres_engine)
    session = Session()
    try:
        _cleanup(session)
        product = _seed(session, _TEST_TENANT_A)
        _membership(
            session,
            tenant_id=_TEST_TENANT_A,
            catalog_id="c1",
            retailer_id="863278879-1",
            product_id=product.id,
            salla_variant_id="1",
            meta_item_id="m1",
        )
        session.commit()
        with pytest.raises(IntegrityError):
            _membership(
                session,
                tenant_id=_TEST_TENANT_A,
                catalog_id="c1",
                retailer_id="863278879-1",
                product_id=product.id,
                salla_variant_id="9",
                meta_item_id="m9",
            )
            session.commit()
        session.rollback()
    finally:
        _cleanup(session)
        session.close()


def test_catalog_id_switch_allows_same_variant_key(postgres_engine):
    _ensure_tables(postgres_engine)
    Session = sessionmaker(bind=postgres_engine)
    session = Session()
    try:
        _cleanup(session)
        product = _seed(session, _TEST_TENANT_A)
        _membership(
            session, tenant_id=_TEST_TENANT_A, catalog_id="old-cat",
            retailer_id="863278879-1", product_id=product.id,
            salla_variant_id="1", meta_item_id="old-meta",
        )
        _membership(
            session, tenant_id=_TEST_TENANT_A, catalog_id="new-cat",
            retailer_id="863278879-1", product_id=product.id,
            salla_variant_id="1", meta_item_id="new-meta",
        )
        session.commit()
        n = session.execute(
            text(
                "SELECT COUNT(*) FROM meta_catalog_memberships "
                "WHERE tenant_id=:t AND salla_variant_id='1'"
            ),
            {"t": _TEST_TENANT_A},
        ).scalar()
        assert n == 2
    finally:
        _cleanup(session)
        session.close()


def test_two_workers_cannot_bind_same_variant_twice(postgres_engine):
    import threading

    _ensure_tables(postgres_engine)
    Session = sessionmaker(bind=postgres_engine)
    setup = Session()
    try:
        _cleanup(setup)
        product = _seed(setup, _TEST_TENANT_A)
        setup.commit()
        product_id = product.id
    finally:
        setup.close()

    errors: list[str] = []
    committed = []
    barrier = threading.Barrier(2)

    def worker(meta_id: str) -> None:
        session = Session()
        try:
            barrier.wait(timeout=5)
            _membership(
                session,
                tenant_id=_TEST_TENANT_A,
                catalog_id="c1",
                retailer_id="863278879-1",
                product_id=product_id,
                salla_variant_id="1",
                meta_item_id=meta_id,
            )
            session.commit()
            committed.append(meta_id)
        except Exception as exc:  # noqa: BLE001
            errors.append(type(exc).__name__)
            session.rollback()
        finally:
            session.close()

    t1 = threading.Thread(target=worker, args=("meta-a",))
    t2 = threading.Thread(target=worker, args=("meta-b",))
    t1.start()
    t2.start()
    t1.join(timeout=10)
    t2.join(timeout=10)
    assert len(committed) == 1
    assert any(name == "IntegrityError" for name in errors)
    verify = Session()
    try:
        n = verify.execute(
            text(
                "SELECT COUNT(*) FROM meta_catalog_memberships "
                "WHERE tenant_id=:t AND catalog_id='c1' AND salla_variant_id='1'"
            ),
            {"t": _TEST_TENANT_A},
        ).scalar()
        assert n == 1
        mid = verify.execute(
            text(
                "SELECT meta_item_id FROM meta_catalog_memberships "
                "WHERE tenant_id=:t AND catalog_id='c1' AND salla_variant_id='1'"
            ),
            {"t": _TEST_TENANT_A},
        ).scalar()
        assert mid in committed
    finally:
        _cleanup(verify)
        verify.close()
    _ensure_tables(postgres_engine)
    with postgres_engine.begin() as conn:
        conn.execute(text(DROP_UQ_MEMBERSHIP_META_ITEM_SQL))
    Session = sessionmaker(bind=postgres_engine)
    session = Session()
    try:
        _cleanup(session)
        product = _seed(session, _TEST_TENANT_A)
        _membership(
            session, tenant_id=_TEST_TENANT_A, catalog_id="c1",
            retailer_id="r1", product_id=product.id, salla_variant_id="1", meta_item_id="dup",
        )
        session.commit()
        with postgres_engine.begin() as conn:
            conn.execute(text(DROP_UQ_MEMBERSHIP_VARIANT_KEY_SQL))
            conn.execute(text(DROP_UQ_MEMBERSHIP_META_ITEM_SQL))
        _membership(
            session, tenant_id=_TEST_TENANT_A, catalog_id="c1",
            retailer_id="r2", product_id=product.id, salla_variant_id="2", meta_item_id="dup",
        )
        session.commit()
        with pytest.raises(RuntimeError, match=ERROR_DUPLICATE_CATALOG_IDENTITY_BLOCKED):
            raise_if_duplicate_catalog_identities(postgres_engine)
        remaining = session.execute(
            text("SELECT COUNT(*) FROM meta_catalog_memberships WHERE tenant_id=:t"),
            {"t": _TEST_TENANT_A},
        ).scalar()
        assert remaining == 2
    finally:
        _cleanup(session)
        session.close()
        with postgres_engine.begin() as conn:
            conn.execute(text(DROP_UQ_MEMBERSHIP_VARIANT_KEY_SQL))
            conn.execute(text(DROP_UQ_MEMBERSHIP_META_ITEM_SQL))
            conn.execute(text(CREATE_UQ_MEMBERSHIP_VARIANT_KEY_SQL))
            conn.execute(text(CREATE_UQ_MEMBERSHIP_META_ITEM_SQL))

"""PostgreSQL uniqueness: one active local row ↔ one Meta item.

Uses isolated Postgres at WA_CATALOG_SYNC_PG_TEST_DATABASE_URL only.
Never DATABASE_URL.
"""
from __future__ import annotations

import os
import sys
import threading
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

from catalog_meta_item_uniqueness import (  # noqa: E402
    CREATE_UQ_PRODUCTS_ACTIVE_TENANT_META_ITEM_SQL,
    DROP_UQ_PRODUCTS_ACTIVE_TENANT_META_ITEM_SQL,
    ERROR_DUPLICATE_ACTIVE_META_BINDING_BLOCKED,
    UQ_PRODUCTS_ACTIVE_TENANT_META_ITEM,
    raise_if_duplicate_active_meta_bindings,
)

_TEST_TENANT_A = 880_993
_TEST_TENANT_B = 880_994


def _is_postgres_url(url: str) -> bool:
    return url.split(":", 1)[0].lower().startswith("postgres")


@pytest.fixture
def postgres_engine() -> Engine:
    explicit = (os.getenv("WA_CATALOG_SYNC_PG_TEST_DATABASE_URL") or "").strip()
    if not explicit or not _is_postgres_url(explicit):
        message = (
            "WA_CATALOG_SYNC_PG_TEST_DATABASE_URL is required for isolated "
            "active Meta binding tests. DATABASE_URL is ignored."
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
        message = f"PostgreSQL unavailable for active Meta binding tests: {exc}"
        if (os.getenv("WA_CATALOG_SYNC_PG_REQUIRED") or "").strip() == "1":
            pytest.fail(message)
        pytest.skip(message)


def _ensure_orm_tables(engine: Engine) -> None:
    from database.models import Product, Tenant

    Tenant.__table__.create(bind=engine, checkfirst=True)
    Product.__table__.create(bind=engine, checkfirst=True)
    with engine.begin() as conn:
        conn.execute(text(DROP_UQ_PRODUCTS_ACTIVE_TENANT_META_ITEM_SQL))
        conn.execute(text(CREATE_UQ_PRODUCTS_ACTIVE_TENANT_META_ITEM_SQL))


def _cleanup(session) -> None:
    session.rollback()
    session.execute(
        text("DELETE FROM products WHERE tenant_id IN (:a, :b)"),
        {"a": _TEST_TENANT_A, "b": _TEST_TENANT_B},
    )
    session.execute(
        text("DELETE FROM tenants WHERE id IN (:a, :b)"),
        {"a": _TEST_TENANT_A, "b": _TEST_TENANT_B},
    )
    session.commit()


def _product(session, *, tenant_id: int, title: str, **overrides):
    from database.models import Product, Tenant

    if session.get(Tenant, tenant_id) is None:
        session.add(Tenant(id=tenant_id, name=f"meta-bind-{tenant_id}"))
        session.flush()
    fields = dict(
        tenant_id=tenant_id,
        title=title,
        source="salla",
        ownership_mode="external_managed",
        catalog_status="active",
        extra_metadata={"currency": "SAR"},
    )
    fields.update(overrides)
    row = Product(**fields)
    session.add(row)
    session.flush()
    return row


def test_migration_refuses_active_duplicates_without_mutating(postgres_engine: Engine) -> None:
    from database.models import Product

    _ensure_orm_tables(postgres_engine)
    with postgres_engine.begin() as conn:
        conn.execute(text(DROP_UQ_PRODUCTS_ACTIVE_TENANT_META_ITEM_SQL))
    Session = sessionmaker(bind=postgres_engine)
    db = Session()
    try:
        _cleanup(db)
        _product(db, tenant_id=_TEST_TENANT_A, title="قميص قطني أزرق", meta_item_id="META-DUP")
        _product(db, tenant_id=_TEST_TENANT_A, title="حذاء رياضي أبيض", meta_item_id="META-DUP")
        db.commit()
        with pytest.raises(RuntimeError) as exc:
            raise_if_duplicate_active_meta_bindings(db.get_bind())
        assert ERROR_DUPLICATE_ACTIVE_META_BINDING_BLOCKED in str(exc.value)
        rows = (
            db.query(Product)
            .filter(Product.tenant_id == _TEST_TENANT_A, Product.meta_item_id == "META-DUP")
            .all()
        )
        assert len(rows) == 2
    finally:
        _cleanup(db)
        db.close()
        with postgres_engine.begin() as conn:
            conn.execute(text(CREATE_UQ_PRODUCTS_ACTIVE_TENANT_META_ITEM_SQL))


def test_partial_unique_index_allows_history_and_other_tenants(postgres_engine: Engine) -> None:
    _ensure_orm_tables(postgres_engine)
    Session = sessionmaker(bind=postgres_engine)
    db = Session()
    try:
        _cleanup(db)
        historical = _product(
            db,
            tenant_id=_TEST_TENANT_A,
            title="عطر ورد 100ml",
            meta_item_id="META-SHARE",
            catalog_status="removed_from_meta",
        )
        active = _product(
            db,
            tenant_id=_TEST_TENANT_A,
            title="قميص قطني أزرق",
            meta_item_id="META-SHARE",
        )
        other_tenant = _product(
            db,
            tenant_id=_TEST_TENANT_B,
            title="حذاء رياضي أبيض",
            meta_item_id="META-SHARE",
        )
        db.commit()
        assert historical.catalog_status == "removed_from_meta"
        assert active.catalog_status == "active"
        assert other_tenant.tenant_id != active.tenant_id
        active.catalog_status = "removed_from_meta"
        db.flush()
        replacement = _product(
            db,
            tenant_id=_TEST_TENANT_A,
            title="قميص قطني أزرق - بديل",
            meta_item_id="META-SHARE",
        )
        db.commit()
        assert replacement.meta_item_id == "META-SHARE"
        assert replacement.catalog_status == "active"
    finally:
        _cleanup(db)
        db.close()


def test_two_sessions_cannot_bind_same_active_meta_item(postgres_engine: Engine) -> None:
    from services.meta_catalog_identity import (
        DuplicateActiveMetaBinding,
        claim_active_meta_item_binding,
    )

    _ensure_orm_tables(postgres_engine)
    Session = sessionmaker(bind=postgres_engine)
    setup = Session()
    try:
        _cleanup(setup)
        left = _product(setup, tenant_id=_TEST_TENANT_A, title="قميص قطني أزرق")
        right = _product(setup, tenant_id=_TEST_TENANT_A, title="حذاء رياضي أبيض")
        setup.commit()
        left_id = int(left.id)
        right_id = int(right.id)
    finally:
        setup.close()

    barrier = threading.Barrier(2, timeout=10)
    outcomes: dict[str, str] = {}

    def _worker(name: str, product_id: int) -> None:
        from database.models import Product

        session = Session()
        try:
            row = session.get(Product, product_id)
            assert row is not None
            barrier.wait()
            try:
                claim_active_meta_item_binding(session, row, "META-RACE")
                session.commit()
                outcomes[name] = "committed"
            except DuplicateActiveMetaBinding:
                session.rollback()
                outcomes[name] = "ambiguous_sibling"
            except IntegrityError:
                session.rollback()
                outcomes[name] = "unique_violation"
            except Exception as exc:  # noqa: BLE001
                session.rollback()
                outcomes[name] = f"error:{type(exc).__name__}"
        finally:
            session.close()

    thread_a = threading.Thread(target=_worker, args=("a", left_id), daemon=True)
    thread_b = threading.Thread(target=_worker, args=("b", right_id), daemon=True)
    thread_a.start()
    thread_b.start()
    thread_a.join(timeout=15)
    thread_b.join(timeout=15)
    assert not thread_a.is_alive()
    assert not thread_b.is_alive()
    assert set(outcomes) == {"a", "b"}

    values = set(outcomes.values())
    assert "committed" in values
    assert values - {"committed"} <= {"ambiguous_sibling", "unique_violation"}
    assert outcomes["a"] == "committed" or outcomes["b"] == "committed"
    assert outcomes["a"] != "committed" or outcomes["b"] != "committed"

    verify = Session()
    try:
        from database.models import Product

        rows = (
            verify.query(Product)
            .filter(Product.tenant_id == _TEST_TENANT_A, Product.meta_item_id == "META-RACE")
            .all()
        )
        assert len(rows) == 1
        assert str(rows[0].catalog_status) == "active"
        losers = (
            verify.query(Product)
            .filter(
                Product.tenant_id == _TEST_TENANT_A,
                Product.id.in_([left_id, right_id]),
                Product.meta_item_id.is_(None),
            )
            .all()
        )
        assert len(losers) == 1
    finally:
        _cleanup(verify)
        verify.close()


def test_claim_converts_direct_writer_unique_collision(postgres_engine: Engine) -> None:
    """Index is the backstop when a writer skips the helper."""
    from database.models import Product
    from services.meta_catalog_identity import (
        DuplicateActiveMetaBinding,
        claim_active_meta_item_binding,
    )

    _ensure_orm_tables(postgres_engine)
    Session = sessionmaker(bind=postgres_engine)
    setup = Session()
    try:
        _cleanup(setup)
        left = _product(setup, tenant_id=_TEST_TENANT_A, title="قميص قطني أزرق")
        right = _product(setup, tenant_id=_TEST_TENANT_A, title="حذاء رياضي أبيض")
        setup.commit()
        left_id = int(left.id)
        right_id = int(right.id)
    finally:
        setup.close()

    barrier = threading.Barrier(2, timeout=10)
    outcomes: dict[str, str] = {}

    def _claim() -> None:
        session = Session()
        try:
            row = session.get(Product, left_id)
            assert row is not None
            barrier.wait()
            try:
                claim_active_meta_item_binding(session, row, "META-DIRECT")
                session.commit()
                outcomes["claim"] = "committed"
            except DuplicateActiveMetaBinding:
                session.rollback()
                outcomes["claim"] = "ambiguous_sibling"
            except IntegrityError:
                session.rollback()
                outcomes["claim"] = "unique_violation"
            except Exception as exc:  # noqa: BLE001
                session.rollback()
                outcomes["claim"] = f"error:{type(exc).__name__}"
        finally:
            session.close()

    def _direct() -> None:
        session = Session()
        try:
            row = session.get(Product, right_id)
            assert row is not None
            barrier.wait()
            try:
                row.meta_item_id = "META-DIRECT"
                session.commit()
                outcomes["direct"] = "committed"
            except IntegrityError:
                session.rollback()
                outcomes["direct"] = "unique_violation"
            except Exception as exc:  # noqa: BLE001
                session.rollback()
                outcomes["direct"] = f"error:{type(exc).__name__}"
        finally:
            session.close()

    thread_claim = threading.Thread(target=_claim, daemon=True)
    thread_direct = threading.Thread(target=_direct, daemon=True)
    thread_claim.start()
    thread_direct.start()
    thread_claim.join(timeout=15)
    thread_direct.join(timeout=15)
    assert not thread_claim.is_alive()
    assert not thread_direct.is_alive()
    assert set(outcomes) == {"claim", "direct"}
    assert (outcomes["claim"] == "committed") != (outcomes["direct"] == "committed")
    if outcomes["claim"] != "committed":
        assert outcomes["claim"] == "ambiguous_sibling"
        assert outcomes["direct"] == "committed"
    else:
        assert outcomes["direct"] == "unique_violation"

    verify = Session()
    try:
        rows = (
            verify.query(Product)
            .filter(Product.tenant_id == _TEST_TENANT_A, Product.meta_item_id == "META-DIRECT")
            .all()
        )
        assert len(rows) == 1
        assert str(rows[0].catalog_status) == "active"
    finally:
        _cleanup(verify)
        verify.close()


def test_raw_unique_index_rejects_second_active_bind(postgres_engine: Engine) -> None:
    _ensure_orm_tables(postgres_engine)
    Session = sessionmaker(bind=postgres_engine)
    db = Session()
    try:
        _cleanup(db)
        first = _product(db, tenant_id=_TEST_TENANT_A, title="قميص قطني أزرق", meta_item_id="META-IX")
        second = _product(db, tenant_id=_TEST_TENANT_A, title="حذاء رياضي أبيض")
        db.commit()
        second.meta_item_id = "META-IX"
        with pytest.raises(IntegrityError):
            db.flush()
        db.rollback()
        db.refresh(first)
        assert first.meta_item_id == "META-IX"
        db.refresh(second)
        assert second.meta_item_id is None
    finally:
        _cleanup(db)
        db.close()

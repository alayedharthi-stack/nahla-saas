"""Real PostgreSQL SKIP LOCKED tests for WhatsApp catalog sync.

Unit suites in this feature use ``NAHLA_TEST_NO_DB=1`` (no engine).
This file is the opposite: two independent NullPool connections against
an actual isolated Postgres server. Mocking ``with_for_update`` is not enough.

The only allowed DSN is ``WA_CATALOG_SYNC_PG_TEST_DATABASE_URL``.
``DATABASE_URL`` and any default local URL are never used.

If the explicit URL is missing:
  * ``WA_CATALOG_SYNC_PG_REQUIRED=1`` → fail with a clear message
  * otherwise → skip with a reason, without attempting any connection
"""
from __future__ import annotations

import os
import sys
import threading
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch

import pytest
from sqlalchemy import create_engine, text
from sqlalchemy.engine import Engine
from sqlalchemy.orm import Session, sessionmaker
from sqlalchemy.pool import NullPool

_REPO_ROOT = Path(__file__).resolve().parents[2]
_BACKEND = _REPO_ROOT / "backend"
_DATABASE = _REPO_ROOT / "database"
for _entry in (str(_REPO_ROOT), str(_BACKEND), str(_DATABASE)):
    if _entry not in sys.path:
        sys.path.insert(0, _entry)

_TEST_TENANT = 880_991
_LOCK_TABLE = "wa_catalog_sync_lock_itest"


def _is_postgres_url(url: str) -> bool:
    head = url.split(":", 1)[0].lower()
    return head.startswith("postgres")


def _candidate_database_urls() -> list[str]:
    """Allow only the isolated test URL. Never fall back to DATABASE_URL."""
    explicit = (os.getenv("WA_CATALOG_SYNC_PG_TEST_DATABASE_URL") or "").strip()
    if explicit and _is_postgres_url(explicit):
        return [explicit]
    return []


def _pg_required() -> bool:
    return (os.getenv("WA_CATALOG_SYNC_PG_REQUIRED") or "").strip() == "1"


def _connect_engine() -> Engine:
    urls = _candidate_database_urls()
    if not urls:
        message = (
            "WA_CATALOG_SYNC_PG_TEST_DATABASE_URL is required for isolated "
            "SKIP LOCKED tests. DATABASE_URL is ignored and is never used."
        )
        if _pg_required():
            pytest.fail(message)
        pytest.skip(message)
    url = urls[0]
    try:
        engine = create_engine(url, poolclass=NullPool, pool_pre_ping=True)
        with engine.connect() as conn:
            conn.execute(text("SELECT 1"))
        return engine
    except Exception as exc:  # noqa: BLE001
        message = (
            "PostgreSQL unavailable for WhatsApp catalog SKIP LOCKED tests "
            f"at WA_CATALOG_SYNC_PG_TEST_DATABASE_URL: {exc}"
        )
        if _pg_required():
            pytest.fail(message)
        pytest.skip(message)


def test_database_url_alone_is_never_a_candidate(monkeypatch) -> None:
    monkeypatch.delenv("WA_CATALOG_SYNC_PG_TEST_DATABASE_URL", raising=False)
    monkeypatch.delenv("WA_CATALOG_SYNC_PG_REQUIRED", raising=False)
    monkeypatch.setenv("DATABASE_URL", "postgresql://must-not-connect.example:5432/prod")
    assert _candidate_database_urls() == []


def test_database_url_alone_does_not_create_engine(monkeypatch) -> None:
    monkeypatch.delenv("WA_CATALOG_SYNC_PG_TEST_DATABASE_URL", raising=False)
    monkeypatch.delenv("WA_CATALOG_SYNC_PG_REQUIRED", raising=False)
    monkeypatch.setenv("DATABASE_URL", "postgresql://must-not-connect.example:5432/prod")
    import test_whatsapp_catalog_sync_postgres_locks as pgmod

    with patch.object(pgmod, "create_engine") as ce:
        with pytest.raises(pytest.skip.Exception):
            _connect_engine()
        ce.assert_not_called()


def test_required_without_explicit_url_fails_without_connecting(monkeypatch) -> None:
    monkeypatch.delenv("WA_CATALOG_SYNC_PG_TEST_DATABASE_URL", raising=False)
    monkeypatch.setenv("WA_CATALOG_SYNC_PG_REQUIRED", "1")
    monkeypatch.setenv("DATABASE_URL", "postgresql://must-not-connect.example:5432/prod")
    import test_whatsapp_catalog_sync_postgres_locks as pgmod

    with patch.object(pgmod, "create_engine") as ce:
        with pytest.raises(pytest.fail.Exception):
            _connect_engine()
        ce.assert_not_called()


@pytest.fixture(scope="module")
def postgres_engine() -> Engine:
    engine = _connect_engine()
    yield engine
    engine.dispose()


def _Session(engine: Engine):
    """Match production SessionLocal: autoflush off, no implicit autocommit."""
    return sessionmaker(autocommit=False, autoflush=False, bind=engine)


def _ensure_orm_tables(engine: Engine) -> None:
    """Create Tenant/Product tables on an empty isolated database.

    The lock tests must not skip when the isolated instance has no schema yet.
    """
    from database.models import Product, ProductVariant, StoreKnowledgeSnapshot, Tenant

    Tenant.__table__.create(bind=engine, checkfirst=True)
    Product.__table__.create(bind=engine, checkfirst=True)
    ProductVariant.__table__.create(bind=engine, checkfirst=True)
    StoreKnowledgeSnapshot.__table__.create(bind=engine, checkfirst=True)


def _cleanup_lock_rows(session: Session) -> None:
    """Delete itest rows without loading Tenant relationship graphs."""
    session.rollback()
    session.execute(text("DELETE FROM product_variants WHERE tenant_id = :tid"), {"tid": _TEST_TENANT})
    session.execute(text("DELETE FROM products WHERE tenant_id = :tid"), {"tid": _TEST_TENANT})
    session.execute(
        text("DELETE FROM store_knowledge_snapshots WHERE tenant_id = :tid"),
        {"tid": _TEST_TENANT},
    )
    session.execute(text("DELETE FROM tenants WHERE id = :tid"), {"tid": _TEST_TENANT})
    session.commit()


def test_skip_locked_second_connection_gets_no_row(postgres_engine: Engine) -> None:
    """Two independent connections: held FOR UPDATE SKIP LOCKED hides the row."""
    ddl = f"""
    CREATE TABLE IF NOT EXISTS {_LOCK_TABLE} (
        id INTEGER PRIMARY KEY,
        tenant_id INTEGER NOT NULL,
        sync_status VARCHAR(32)
    )
    """
    with postgres_engine.begin() as setup:
        setup.execute(text(ddl))
        setup.execute(text(f"DELETE FROM {_LOCK_TABLE} WHERE id = 1"))
        setup.execute(
            text(
                f"INSERT INTO {_LOCK_TABLE} (id, tenant_id, sync_status) "
                "VALUES (1, :tid, 'pending')"
            ),
            {"tid": _TEST_TENANT},
        )

    sql = text(
        f"SELECT id FROM {_LOCK_TABLE} WHERE id = 1 FOR UPDATE SKIP LOCKED"
    )
    conn_a = postgres_engine.connect()
    conn_b = postgres_engine.connect()
    trans_a = conn_a.begin()
    trans_b = conn_b.begin()
    try:
        held = conn_a.execute(sql).fetchone()
        skipped = conn_b.execute(sql).fetchone()
        assert held is not None
        assert int(held[0]) == 1
        assert skipped is None
    finally:
        trans_b.rollback()
        trans_a.rollback()
        conn_b.close()
        conn_a.close()
        with postgres_engine.begin() as cleanup:
            cleanup.execute(text(f"DROP TABLE IF EXISTS {_LOCK_TABLE}"))


def test_try_acquire_skips_row_locked_by_other_session(postgres_engine: Engine) -> None:
    """``_try_acquire_sync_lock`` uses skip_locked; a held row is not re-leased."""
    _ensure_orm_tables(postgres_engine)

    from database.models import Product, Tenant
    from services.native_meta_sync_orchestrator import _try_acquire_sync_lock

    Session = _Session(postgres_engine)
    holder = Session()
    contender = Session()
    product_id: int | None = None
    try:
        tenant = holder.get(Tenant, _TEST_TENANT)
        if tenant is None:
            holder.add(Tenant(id=_TEST_TENANT, name=f"wa-catalog-lock-{_TEST_TENANT}"))
            holder.flush()
        product = Product(
            tenant_id=_TEST_TENANT,
            title="حذاء رياضي أبيض",
            source="salla",
            ownership_mode="external_managed",
            catalog_status="active",
            in_stock=True,
            sync_status="pending",
            extra_metadata={"currency": "SAR", "image_url": "https://cdn.example/shoe.webp"},
        )
        holder.add(product)
        holder.commit()
        product_id = int(product.id)

        locked = (
            holder.query(Product)
            .filter(Product.id == product_id, Product.tenant_id == _TEST_TENANT)
            .with_for_update()
            .first()
        )
        assert locked is not None

        acquired = _try_acquire_sync_lock(contender, _TEST_TENANT, product_id)
        assert acquired is None
        still = contender.query(Product).filter(Product.id == product_id).first()
        assert still is not None
        assert str(still.sync_status or "") == "pending"
    finally:
        try:
            holder.rollback()
        except Exception:
            pass
        try:
            contender.rollback()
        except Exception:
            pass
        cleanup = Session()
        try:
            _cleanup_lock_rows(cleanup)
        finally:
            cleanup.close()
            holder.close()
            contender.close()


def test_try_acquire_rejects_live_syncing_without_resetting_backoff(
    postgres_engine: Engine,
) -> None:
    _ensure_orm_tables(postgres_engine)

    from datetime import datetime, timedelta, timezone

    from database.models import Product, Tenant
    from services.native_meta_sync_orchestrator import _try_acquire_sync_lock

    Session = _Session(postgres_engine)
    db = Session()
    product_id: int | None = None
    try:
        if db.get(Tenant, _TEST_TENANT) is None:
            db.add(Tenant(id=_TEST_TENANT, name=f"wa-catalog-lock-{_TEST_TENANT}"))
            db.flush()
        now = datetime.now(timezone.utc)
        product = Product(
            tenant_id=_TEST_TENANT,
            title="قميص قطني أزرق",
            source="salla",
            ownership_mode="external_managed",
            catalog_status="active",
            in_stock=True,
            sync_status="syncing",
            extra_metadata={
                "currency": "SAR",
                "sync_meta": {
                    "syncing_started_at": now.isoformat(),
                    "retry_count": 4,
                    "next_retry_at": (now + timedelta(hours=2)).isoformat(),
                    "lock_generation": 3,
                },
            },
        )
        db.add(product)
        db.commit()
        product_id = int(product.id)

        other = Session()
        try:
            acquired = _try_acquire_sync_lock(other, _TEST_TENANT, product_id)
            assert acquired is None
        finally:
            other.close()

        db.refresh(product)
        assert product.sync_status == "syncing"
        meta = (product.extra_metadata or {}).get("sync_meta") or {}
        assert int(meta.get("retry_count") or 0) == 4
        assert int(meta.get("lock_generation") or 0) == 3
    finally:
        try:
            db.rollback()
        except Exception:
            pass
        _cleanup_lock_rows(db)
        db.close()


def _seed_lock_product(session: Session, **overrides) -> int:
    from database.models import Product, Tenant

    if session.get(Tenant, _TEST_TENANT) is None:
        session.add(Tenant(id=_TEST_TENANT, name=f"wa-catalog-lock-{_TEST_TENANT}"))
        session.flush()
    fields = dict(
        tenant_id=_TEST_TENANT,
        title="قميص قطني أزرق",
        source="salla",
        ownership_mode="external_managed",
        catalog_status="active",
        in_stock=True,
        sync_status="pending",
        extra_metadata={"currency": "SAR", "image_url": "https://cdn.example/shirt.webp"},
    )
    fields.update(overrides)
    product = Product(**fields)
    session.add(product)
    session.commit()
    return int(product.id)


def test_two_sessions_load_then_only_one_acquires(postgres_engine: Engine) -> None:
    _ensure_orm_tables(postgres_engine)
    from database.models import Product
    from services.native_meta_sync_orchestrator import _try_acquire_sync_lock

    Session = _Session(postgres_engine)
    setup = Session()
    s1 = Session()
    s2 = Session()
    try:
        product_id = _seed_lock_product(setup)
        p1 = s1.query(Product).filter_by(id=product_id).first()
        p2 = s2.query(Product).filter_by(id=product_id).first()
        assert p1 is not None and p2 is not None
        assert p1.sync_status == "pending"
        first = _try_acquire_sync_lock(s1, _TEST_TENANT, product_id)
        second = _try_acquire_sync_lock(s2, _TEST_TENANT, product_id)
        assert first is not None
        assert second is None
        meta = (first.extra_metadata or {}).get("sync_meta") or {}
        assert int(meta.get("lock_generation") or 0) == 1
    finally:
        for sess in (s1, s2, setup):
            try:
                sess.rollback()
            except Exception:
                pass
        cleanup = Session()
        try:
            _cleanup_lock_rows(cleanup)
        finally:
            cleanup.close()
            s1.close()
            s2.close()
            setup.close()


def test_stale_worker_cannot_stamp_after_newer_lease(postgres_engine: Engine) -> None:
    _ensure_orm_tables(postgres_engine)
    from database.models import Product
    from services.native_meta_sync_orchestrator import (
        _stamp_with_lease,
        _try_acquire_sync_lock,
        _mark_synced,
    )

    Session = _Session(postgres_engine)
    setup = Session()
    worker_old = Session()
    worker_new = Session()
    try:
        started = (datetime.now(timezone.utc) - timedelta(minutes=20)).isoformat()
        product_id = _seed_lock_product(
            setup,
            sync_status="syncing",
            extra_metadata={
                "currency": "SAR",
                "image_url": "https://cdn.example/shirt.webp",
                "sync_meta": {"syncing_started_at": started, "lock_generation": 1, "retry_count": 2},
            },
        )
        old_row = _try_acquire_sync_lock(worker_old, _TEST_TENANT, product_id)
        assert old_row is not None
        old_lease = int((old_row.extra_metadata or {}).get("sync_meta", {}).get("lock_generation") or 0)
        # Simulate expiry then reclaim by a newer worker.
        old_row.extra_metadata["sync_meta"]["syncing_started_at"] = (
            datetime.now(timezone.utc) - timedelta(minutes=20)
        ).isoformat()
        from sqlalchemy.orm.attributes import flag_modified

        flag_modified(old_row, "extra_metadata")
        worker_old.commit()

        new_row = _try_acquire_sync_lock(worker_new, _TEST_TENANT, product_id)
        assert new_row is not None
        new_lease = int((new_row.extra_metadata or {}).get("sync_meta", {}).get("lock_generation") or 0)
        assert new_lease != old_lease

        stamped = _stamp_with_lease(
            worker_old,
            old_row,
            old_lease,
            lambda row: _mark_synced(row, meta_item_id="META-STALE", waba_linked=False),
        )
        assert stamped is False
        check = Session()
        try:
            fresh = check.get(Product, product_id)
            assert fresh is not None
            assert fresh.sync_status == "syncing"
            assert fresh.meta_item_id is None
            meta = (fresh.extra_metadata or {}).get("sync_meta") or {}
            assert int(meta.get("lock_generation") or 0) == new_lease
        finally:
            check.close()
    finally:
        for sess in (worker_old, worker_new, setup):
            try:
                sess.rollback()
            except Exception:
                pass
        cleanup = Session()
        try:
            _cleanup_lock_rows(cleanup)
        finally:
            cleanup.close()
            worker_old.close()
            worker_new.close()
            setup.close()


def test_stamp_does_not_lose_dirty_update(postgres_engine: Engine) -> None:
    _ensure_orm_tables(postgres_engine)
    from database.models import Product
    from services.native_meta_sync_orchestrator import (
        _stamp_with_lease,
        _try_acquire_sync_lock,
        _mark_synced,
        _requeue_if_dirty,
        mark_native_meta_sync_pending,
    )

    Session = _Session(postgres_engine)
    setup = Session()
    worker = Session()
    updater = Session()
    try:
        product_id = _seed_lock_product(setup)
        acquired = _try_acquire_sync_lock(worker, _TEST_TENANT, product_id)
        assert acquired is not None
        lease = int((acquired.extra_metadata or {}).get("sync_meta", {}).get("lock_generation") or 0)

        other = updater.get(Product, product_id)
        assert other is not None
        assert mark_native_meta_sync_pending(updater, other) is True
        updater.commit()

        def _finalize(row):
            _mark_synced(row, meta_item_id="META-DIRTY", waba_linked=False)
            _requeue_if_dirty(row)

        assert _stamp_with_lease(worker, acquired, lease, _finalize) is True
        check = Session()
        try:
            fresh = check.get(Product, product_id)
            assert fresh is not None
            assert fresh.sync_status == "pending"
            meta = (fresh.extra_metadata or {}).get("sync_meta") or {}
            assert meta.get("dirty") is False
            assert int(meta.get("content_generation") or 0) >= 1
        finally:
            check.close()
    finally:
        for sess in (worker, updater, setup):
            try:
                sess.rollback()
            except Exception:
                pass
        cleanup = Session()
        try:
            _cleanup_lock_rows(cleanup)
        finally:
            cleanup.close()
            worker.close()
            updater.close()
            setup.close()


def test_acquire_releases_row_lock_before_graph(postgres_engine: Engine) -> None:
    _ensure_orm_tables(postgres_engine)
    from database.models import Product
    from services.native_meta_sync_orchestrator import _try_acquire_sync_lock

    Session = _Session(postgres_engine)
    setup = Session()
    worker = Session()
    observer = Session()
    try:
        product_id = _seed_lock_product(setup)
        acquired = _try_acquire_sync_lock(worker, _TEST_TENANT, product_id)
        assert acquired is not None
        locked = (
            observer.query(Product)
            .filter(Product.id == product_id, Product.tenant_id == _TEST_TENANT)
            .with_for_update(skip_locked=True)
            .first()
        )
        assert locked is not None
        assert locked.sync_status == "syncing"
        observer.rollback()
    finally:
        for sess in (worker, observer, setup):
            try:
                sess.rollback()
            except Exception:
                pass
        cleanup = Session()
        try:
            _cleanup_lock_rows(cleanup)
        finally:
            cleanup.close()
            worker.close()
            observer.close()
            setup.close()


def test_salla_bulk_keeps_unflushed_price_with_autoflush_false(postgres_engine: Engine) -> None:
    """Salla bulk must persist 83/5 even when mark-pending locks the same session."""
    _ensure_orm_tables(postgres_engine)
    from types import SimpleNamespace
    from unittest.mock import patch

    from database.models import Product
    from services.store_sync import StoreSyncService

    Session = _Session(postgres_engine)
    setup = Session()
    try:
        product_id = _seed_lock_product(
            setup,
            external_id="salla-bulk-1",
            price="77",
            stock_quantity=2,
            sync_status="syncing",
            extra_metadata={
                "currency": "SAR",
                "image_url": "https://cdn.example/shirt.webp",
                "product_url": "https://example.test/p",
                "sync_meta": {
                    "syncing_started_at": datetime.now(timezone.utc).isoformat(),
                    "retry_count": 3,
                    "lock_generation": 4,
                    "content_generation": 1,
                    "sync_generation": 1,
                },
            },
        )
        db = Session()
        try:
            row = db.get(Product, product_id)
            assert row is not None
            svc = StoreSyncService(db, _TEST_TENANT, adapter=SimpleNamespace(platform="salla"))
            normalised = {
                "external_id": "salla-bulk-1",
                "title": "قميص قطني أزرق",
                "description": "وصف",
                "price": "83",
                "sku": "SKU-SHIRT",
                "in_stock": True,
                "stock_qty": 5,
                "currency": "SAR",
                "image_url": "https://cdn.example/shirt.webp",
                "product_url": "https://example.test/p",
                "source": "salla",
                "variants": [],
                "options": [],
                "has_required_options": False,
            }
            with patch("services.store_sync._upsert_variants_for"):
                result = svc._apply_normalised_product(normalised, "salla")
            db.commit()
            assert result["action"] == "updated"
        finally:
            db.close()
        check = Session()
        try:
            fresh = check.get(Product, product_id)
            assert fresh is not None
            assert fresh.price == "83"
            assert fresh.stock_quantity == 5
            meta = (fresh.extra_metadata or {}).get("sync_meta") or {}
            assert int(meta.get("lock_generation") or 0) == 4
            assert meta.get("dirty") is True
            assert int(meta.get("content_generation") or 0) >= 2
            assert fresh.sync_status == "syncing"
        finally:
            check.close()
    finally:
        cleanup = Session()
        try:
            _cleanup_lock_rows(cleanup)
        finally:
            cleanup.close()
            setup.close()


def test_webhook_stale_session_does_not_rewind_newer_lease(postgres_engine: Engine) -> None:
    """A webhook holding stale extra_metadata must not clobber a newer lock_generation."""
    import asyncio
    from types import SimpleNamespace
    from unittest.mock import patch

    _ensure_orm_tables(postgres_engine)
    from database.models import Product
    from services.native_meta_sync_orchestrator import (
        _mark_synced,
        _requeue_if_dirty,
        _stamp_with_lease,
        _try_acquire_sync_lock,
    )
    from services.store_sync import StoreSyncService

    Session = _Session(postgres_engine)
    setup = Session()
    web = Session()
    worker = Session()
    try:
        product_id = _seed_lock_product(
            setup,
            external_id="webhook-1",
            price="77",
            stock_quantity=2,
            sync_status="pending",
            extra_metadata={
                "currency": "SAR",
                "image_url": "https://cdn.example/shirt.webp",
                "product_url": "https://example.test/p",
                "sync_meta": {"lock_generation": 0, "content_generation": 1, "retry_count": 2},
            },
        )
        stale = web.get(Product, product_id)
        assert stale is not None
        old_lease = int(((stale.extra_metadata or {}).get("sync_meta") or {}).get("lock_generation") or 0)
        acquired = _try_acquire_sync_lock(worker, _TEST_TENANT, product_id)
        assert acquired is not None
        worker_lease = int(((acquired.extra_metadata or {}).get("sync_meta") or {}).get("lock_generation") or 0)
        assert worker_lease >= 1

        svc = StoreSyncService(web, _TEST_TENANT, adapter=SimpleNamespace(platform="salla"))
        payload = {
            "id": "webhook-1",
            "title": "قميص قطني أزرق",
            "description": "أحدث",
            "price": "83",
            "sku": "SKU-SHIRT",
            "in_stock": True,
            "quantity": 4,
            "currency": "SAR",
            "image_url": "https://cdn.example/shirt.webp",
            "product_url": "https://example.test/p",
        }
        with patch("services.store_sync._upsert_variants_for"), patch(
            "services.whatsapp_catalog_sync.schedule_whatsapp_catalog_drain"
        ):
            asyncio.run(svc.handle_product_webhook(payload, webhook_event_type="product.updated"))

        check = Session()
        try:
            after = check.get(Product, product_id)
            assert after is not None
            sm = (after.extra_metadata or {}).get("sync_meta") or {}
            assert int(sm.get("lock_generation") or 0) >= worker_lease
            assert after.price == "83"
            assert after.stock_quantity == 4
        finally:
            check.close()

        def _finish(row):
            _mark_synced(row, meta_item_id="META-OLD", waba_linked=False)
            _requeue_if_dirty(row)

        stamped = _stamp_with_lease(web, stale, old_lease, _finish)
        assert stamped is False
        final = Session()
        try:
            row = final.get(Product, product_id)
            assert row is not None
            sm = (row.extra_metadata or {}).get("sync_meta") or {}
            assert int(sm.get("lock_generation") or 0) >= worker_lease
            assert row.meta_item_id != "META-OLD"
        finally:
            final.close()
    finally:
        for sess in (web, worker, setup):
            try:
                sess.rollback()
            except Exception:
                pass
        cleanup = Session()
        try:
            _cleanup_lock_rows(cleanup)
        finally:
            cleanup.close()
            web.close()
            worker.close()
            setup.close()


def test_webhook_create_stamps_salla_ownership(postgres_engine: Engine) -> None:
    import asyncio
    from types import SimpleNamespace
    from unittest.mock import patch

    _ensure_orm_tables(postgres_engine)
    from core.catalog import (
        OWNERSHIP_EXTERNAL_MANAGED,
        is_merchant_editable_product,
        is_whatsapp_channel_publish_eligible,
    )
    from database.models import Product
    from services.store_sync import StoreSyncService

    Session = _Session(postgres_engine)
    db = Session()
    try:
        if db.get(__import__("database.models", fromlist=["Tenant"]).Tenant, _TEST_TENANT) is None:
            from database.models import Tenant

            db.add(Tenant(id=_TEST_TENANT, name=f"wa-catalog-lock-{_TEST_TENANT}"))
            db.commit()
        svc = StoreSyncService(db, _TEST_TENANT, adapter=SimpleNamespace(platform="salla"))
        payload = {
            "id": "salla-new-9",
            "title": "حذاء رياضي أبيض",
            "description": "وصف",
            "price": "120",
            "sku": "SHOE-1",
            "in_stock": True,
            "quantity": 3,
            "currency": "SAR",
            "image_url": "https://cdn.example/shoe.webp",
            "product_url": "https://example.test/shoe",
        }
        with patch("services.store_sync._upsert_variants_for"), patch(
            "services.whatsapp_catalog_sync.schedule_whatsapp_catalog_drain"
        ):
            asyncio.run(svc.handle_product_webhook(payload, webhook_event_type="product.created"))
        row = (
            db.query(Product)
            .filter(Product.tenant_id == _TEST_TENANT, Product.external_id == "salla-new-9")
            .first()
        )
        assert row is not None
        assert row.source == "salla"
        assert row.ownership_mode == OWNERSHIP_EXTERNAL_MANAGED
        assert is_merchant_editable_product(row) is False
        assert is_whatsapp_channel_publish_eligible(row) is True
    finally:
        try:
            db.rollback()
        except Exception:
            pass
        _cleanup_lock_rows(db)
        db.close()


def test_ineligible_acquire_releases_row_lock(postgres_engine: Engine) -> None:
    _ensure_orm_tables(postgres_engine)
    from database.models import Product
    from services.native_meta_sync_orchestrator import _try_acquire_sync_lock

    Session = _Session(postgres_engine)
    setup = Session()
    worker = Session()
    observer = Session()
    try:
        product_id = _seed_lock_product(
            setup,
            catalog_status="archived",
            sync_status="pending",
        )
        acquired = _try_acquire_sync_lock(worker, _TEST_TENANT, product_id)
        assert acquired is None
        locked = (
            observer.query(Product)
            .filter(Product.id == product_id, Product.tenant_id == _TEST_TENANT)
            .with_for_update(skip_locked=True)
            .first()
        )
        assert locked is not None
        observer.rollback()
    finally:
        for sess in (worker, observer, setup):
            try:
                sess.rollback()
            except Exception:
                pass
        cleanup = Session()
        try:
            _cleanup_lock_rows(cleanup)
        finally:
            cleanup.close()
            worker.close()
            observer.close()
            setup.close()


def test_failed_locked_sync_read_does_not_write_stale_or_hold_lock(postgres_engine: Engine) -> None:
    """A failed FOR UPDATE read must not persist stale sync_meta or keep the row lock."""
    _ensure_orm_tables(postgres_engine)
    from database.models import Product
    from services.native_meta_sync_orchestrator import mark_native_meta_sync_pending

    Session = _Session(postgres_engine)
    setup = Session()
    try:
        product_id = _seed_lock_product(
            setup,
            price="77",
            stock_quantity=2,
            sync_status="syncing",
            extra_metadata={
                "currency": "SAR",
                "image_url": "https://cdn.example/shirt.webp",
                "product_url": "https://example.test/p",
                "sync_meta": {
                    "syncing_started_at": datetime.now(timezone.utc).isoformat(),
                    "lock_generation": 5,
                    "content_generation": 1,
                    "sync_generation": 1,
                },
            },
        )
        db = Session()
        observer = Session()
        try:
            row = db.get(Product, product_id)
            assert row is not None
            row.price = "83"
            row.stock_quantity = 5
            extra = dict(row.extra_metadata or {})
            extra["sync_meta"] = {"lock_generation": 0, "content_generation": 1}
            row.extra_metadata = extra

            orig_execute = db.execute
            for_update_hits = {"n": 0}

            def _execute(statement, *args, **kwargs):
                sql = str(statement).upper()
                if "FOR UPDATE" in sql and "PRODUCTS" in sql:
                    for_update_hits["n"] += 1
                    if for_update_hits["n"] == 1:
                        return orig_execute(text("SELECT 1/0"))
                return orig_execute(statement, *args, **kwargs)

            db.execute = _execute  # type: ignore[method-assign]
            orig_query = db.query

            def _query(*args, **kwargs):
                raise RuntimeError("injected orm lock failure")

            db.query = _query  # type: ignore[method-assign]
            marked = mark_native_meta_sync_pending(db, row)
            db.query = orig_query  # type: ignore[method-assign]
            db.execute = orig_execute  # type: ignore[method-assign]
            assert marked is True
            db.commit()

            locked = (
                observer.query(Product)
                .filter(Product.id == product_id, Product.tenant_id == _TEST_TENANT)
                .with_for_update(skip_locked=True)
                .first()
            )
            assert locked is not None
            observer.rollback()

            fresh = observer.get(Product, product_id)
            assert fresh is not None
            sm = (fresh.extra_metadata or {}).get("sync_meta") or {}
            assert int(sm.get("lock_generation") or 0) == 5
            assert fresh.price == "83"
            assert fresh.stock_quantity == 5
            assert str(fresh.sync_status or "") == "syncing"
            assert sm.get("dirty") is True
            assert int(sm.get("content_generation") or 0) >= 2

            resumed = Session()
            try:
                live = resumed.get(Product, product_id)
                assert live is not None
                live.price = "83"
                live.stock_quantity = 5
                assert mark_native_meta_sync_pending(resumed, live) is True
                resumed.commit()
                observer.expire_all()
                after = observer.get(Product, product_id)
                meta = (after.extra_metadata or {}).get("sync_meta") or {}
                assert int(meta.get("lock_generation") or 0) == 5
                assert meta.get("dirty") is True
                assert int(meta.get("content_generation") or 0) >= 2
                assert after.price == "83"
            finally:
                resumed.close()
        finally:
            try:
                db.rollback()
            except Exception:
                pass
            db.close()
            observer.close()
    finally:
        cleanup = Session()
        try:
            _cleanup_lock_rows(cleanup)
        finally:
            cleanup.close()
            setup.close()


def test_complete_sync_read_failure_does_not_write_stale_sync_meta(postgres_engine: Engine) -> None:
    """If every locked/unlocked sync-state read fails, do not persist stale JSON."""
    _ensure_orm_tables(postgres_engine)
    from database.models import Product
    from services.native_meta_sync_orchestrator import mark_native_meta_sync_pending

    Session = _Session(postgres_engine)
    setup = Session()
    try:
        product_id = _seed_lock_product(
            setup,
            price="77",
            stock_quantity=2,
            sync_status="syncing",
            extra_metadata={
                "currency": "SAR",
                "image_url": "https://cdn.example/shirt.webp",
                "product_url": "https://example.test/p",
                "sync_meta": {
                    "syncing_started_at": datetime.now(timezone.utc).isoformat(),
                    "lock_generation": 5,
                    "content_generation": 1,
                    "sync_generation": 1,
                },
            },
        )
        db = Session()
        observer = Session()
        try:
            row = db.get(Product, product_id)
            assert row is not None
            row.price = "83"
            row.stock_quantity = 5
            extra = dict(row.extra_metadata or {})
            extra["sync_meta"] = {"lock_generation": 0, "content_generation": 1}
            row.extra_metadata = extra

            orig_execute = db.execute

            def _execute(statement, *args, **kwargs):
                sql = str(statement).upper()
                if "FOR UPDATE" in sql and "PRODUCTS" in sql:
                    return orig_execute(text("SELECT 1/0"))
                if "AS EXTRA_METADATA" in sql:
                    return orig_execute(text("SELECT 1/0"))
                return orig_execute(statement, *args, **kwargs)

            db.execute = _execute  # type: ignore[method-assign]
            orig_query = db.query

            def _query(*args, **kwargs):
                raise RuntimeError("injected orm lock failure")

            db.query = _query  # type: ignore[method-assign]
            marked = mark_native_meta_sync_pending(db, row)
            db.query = orig_query  # type: ignore[method-assign]
            db.execute = orig_execute  # type: ignore[method-assign]
            assert marked is False
            db.commit()

            locked = (
                observer.query(Product)
                .filter(Product.id == product_id, Product.tenant_id == _TEST_TENANT)
                .with_for_update(skip_locked=True)
                .first()
            )
            assert locked is not None
            observer.rollback()

            fresh = observer.get(Product, product_id)
            assert fresh is not None
            sm = (fresh.extra_metadata or {}).get("sync_meta") or {}
            assert int(sm.get("lock_generation") or 0) == 5
            assert fresh.price == "83"
            assert fresh.stock_quantity == 5
            assert str(fresh.sync_status or "") == "syncing"
            assert sm.get("dirty") is not True
            assert str(fresh.sync_status or "") != "synced"

            resumed = Session()
            try:
                live = resumed.get(Product, product_id)
                assert live is not None
                live.price = "83"
                live.stock_quantity = 5
                assert mark_native_meta_sync_pending(resumed, live) is True
                resumed.commit()
                observer.expire_all()
                after = observer.get(Product, product_id)
                meta = (after.extra_metadata or {}).get("sync_meta") or {}
                assert int(meta.get("lock_generation") or 0) == 5
                assert meta.get("dirty") is True
                assert int(meta.get("content_generation") or 0) >= 2
                assert after.price == "83"
            finally:
                resumed.close()
        finally:
            try:
                db.rollback()
            except Exception:
                pass
            db.close()
            observer.close()
    finally:
        cleanup = Session()
        try:
            _cleanup_lock_rows(cleanup)
        finally:
            cleanup.close()
            setup.close()


def test_unlocked_fallback_read_does_not_clobber_newer_lease(postgres_engine: Engine) -> None:
    """Unlocked fallback is not enough: a newer lease committed before write must win."""
    _ensure_orm_tables(postgres_engine)
    from database.models import Product
    from services.native_meta_sync_orchestrator import (
        _mark_synced,
        _merge_locked_sync_meta,
        _stamp_with_lease,
        mark_native_meta_sync_pending,
    )

    Session = _Session(postgres_engine)
    setup = Session()
    try:
        product_id = _seed_lock_product(
            setup,
            price="77",
            stock_quantity=2,
            sync_status="syncing",
            extra_metadata={
                "currency": "SAR",
                "image_url": "https://cdn.example/shirt.webp",
                "product_url": "https://example.test/p",
                "sync_meta": {
                    "syncing_started_at": datetime.now(timezone.utc).isoformat(),
                    "lock_generation": 5,
                    "content_generation": 1,
                    "sync_generation": 1,
                },
            },
        )
        session_a = Session()
        session_b = Session()
        observer = Session()
        read_done = threading.Event()
        write_gate = threading.Event()
        outcome: dict[str, object] = {}
        orig_merge = _merge_locked_sync_meta
        merge_count = {"n": 0}

        def _pause_after_fallback_merge(product, db_status, db_sm):
            orig_merge(product, db_status, db_sm)
            merge_count["n"] += 1
            if merge_count["n"] == 1:
                read_done.set()
                assert write_gate.wait(timeout=5)

        try:
            row_a = session_a.get(Product, product_id)
            assert row_a is not None
            row_a.price = "83"
            row_a.stock_quantity = 5
            extra = dict(row_a.extra_metadata or {})
            extra["sync_meta"] = {"lock_generation": 0, "content_generation": 1}
            row_a.extra_metadata = extra

            orig_execute = session_a.execute
            for_update_hits = {"n": 0}

            def _execute(statement, *args, **kwargs):
                sql = str(statement).upper()
                if "FOR UPDATE" in sql and "PRODUCTS" in sql:
                    for_update_hits["n"] += 1
                    if for_update_hits["n"] == 1:
                        return orig_execute(text("SELECT 1/0"))
                return orig_execute(statement, *args, **kwargs)

            session_a.execute = _execute  # type: ignore[method-assign]
            orig_query = session_a.query

            def _query(*args, **kwargs):
                raise RuntimeError("injected orm lock failure")

            session_a.query = _query  # type: ignore[method-assign]

            def _run_a():
                try:
                    with patch(
                        "services.native_meta_sync_orchestrator._merge_locked_sync_meta",
                        side_effect=_pause_after_fallback_merge,
                    ):
                        outcome["marked"] = mark_native_meta_sync_pending(session_a, row_a)
                    session_a.query = orig_query  # type: ignore[method-assign]
                    session_a.execute = orig_execute  # type: ignore[method-assign]
                    session_a.commit()
                    outcome["committed"] = True
                except Exception as exc:  # noqa: BLE001
                    outcome["error"] = exc
                    try:
                        session_a.rollback()
                    except Exception:
                        pass

            worker = threading.Thread(target=_run_a, daemon=True)
            worker.start()
            assert read_done.wait(timeout=5)

            row_b = session_b.get(Product, product_id)
            assert row_b is not None
            meta_b = dict(row_b.extra_metadata or {})
            sm_b = dict(meta_b.get("sync_meta") or {})
            assert int(sm_b.get("lock_generation") or 0) == 5
            sm_b["lock_generation"] = 6
            sm_b["content_generation"] = 2
            sm_b["sync_generation"] = 2
            meta_b["sync_meta"] = sm_b
            row_b.extra_metadata = meta_b
            session_b.commit()

            write_gate.set()
            worker.join(timeout=5)
            assert worker.is_alive() is False
            assert "error" not in outcome

            locked = (
                observer.query(Product)
                .filter(Product.id == product_id, Product.tenant_id == _TEST_TENANT)
                .with_for_update(skip_locked=True)
                .first()
            )
            assert locked is not None
            observer.rollback()

            fresh = observer.get(Product, product_id)
            assert fresh is not None
            sm = (fresh.extra_metadata or {}).get("sync_meta") or {}
            assert int(sm.get("lock_generation") or 0) >= 6
            assert fresh.price == "83"
            assert fresh.stock_quantity == 5
            assert str(fresh.sync_status or "") != "synced"

            stale = Session()
            try:
                stale_row = stale.get(Product, product_id)
                assert stale_row is not None
                stamped = _stamp_with_lease(
                    stale,
                    stale_row,
                    5,
                    lambda r: _mark_synced(r, meta_item_id="STALE", waba_linked=True),
                )
                assert stamped is False
                observer.expire_all()
                after_stamp = observer.get(Product, product_id)
                assert str(after_stamp.sync_status or "") != "synced"
                assert int(
                    ((after_stamp.extra_metadata or {}).get("sync_meta") or {}).get("lock_generation") or 0
                ) >= 6
            finally:
                stale.close()
        finally:
            write_gate.set()
            for sess in (session_a, session_b, observer):
                try:
                    sess.rollback()
                except Exception:
                    pass
                sess.close()
    finally:
        cleanup = Session()
        try:
            _cleanup_lock_rows(cleanup)
        finally:
            cleanup.close()
            setup.close()

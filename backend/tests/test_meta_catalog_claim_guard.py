"""Cross-tenant catalog-claim guard on every ``meta_catalog_id`` write path.

A Commerce Manager catalog id carried by one tenant's WhatsApp connection must
never be adopted by another tenant through the merchant PATCH, the admin PATCH
or the admin-debug POST — and two tenants must not adopt the same id in two
concurrent requests (PostgreSQL advisory lock per catalog id, verified on a real
PostgreSQL when WA_CATALOG_SYNC_PG_TEST_DATABASE_URL is set).

Generic merchants only (متجر تجريبي عام / متجر آخر); no production ids.
"""
from __future__ import annotations

import asyncio
import inspect
import os
import sys
import threading
import time
from pathlib import Path
from types import SimpleNamespace

import pytest
from fastapi import HTTPException
from sqlalchemy import JSON, create_engine, event, text
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import sessionmaker

_REPO = Path(__file__).resolve().parents[2]
for _p in (_REPO, _REPO / "backend", _REPO / "database"):
    if str(_p) not in sys.path:
        sys.path.insert(0, str(_p))
os.environ.setdefault("NAHLA_TEST_NO_DB", "1")

from database.models import Base, Tenant, WhatsAppConnection  # noqa: E402
from routers.catalog import CatalogConfigPatch, _apply_config_changes  # noqa: E402
from routers import admin_debug  # noqa: E402
from routers import catalog as catalog_router  # noqa: E402
from services.meta_catalog_claim import (  # noqa: E402
    ERROR_CATALOG_CLAIMED_BY_OTHER_TENANT,
    CatalogClaimError,
    guard_catalog_claim,
    other_tenants_claiming,
)

CATALOG_HELD = "CAT-HELD-7001"
CATALOG_FRESH = "CAT-FRESH-7002"


_JSONB_ORIGINALS: dict = {}


@event.listens_for(Base.metadata, "before_create")
def _remap_jsonb(target, connection, **kw):
    """SQLite only: render JSONB columns as JSON for this create_all, and remember
    the originals so ``_restore_jsonb`` puts them back — the metadata is shared
    process-wide and a later real-PostgreSQL create_all must still get jsonb."""
    if connection.dialect.name != "sqlite":
        return
    for table in target.sorted_tables:
        for col in table.columns:
            if isinstance(col.type, JSONB):
                _JSONB_ORIGINALS[(table.name, col.name)] = col.type
                col.type = JSON()


@event.listens_for(Base.metadata, "after_create")
def _restore_jsonb(target, connection, **kw):
    if connection.dialect.name != "sqlite":
        return
    for table in target.sorted_tables:
        for col in table.columns:
            orig = _JSONB_ORIGINALS.pop((table.name, col.name), None)
            if orig is not None:
                col.type = orig


def _db():
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine, tables=[Tenant.__table__, WhatsAppConnection.__table__])
    session = sessionmaker(bind=engine)()
    holder = Tenant(name="متجر تجريبي عام", is_active=True)
    newcomer = Tenant(name="متجر آخر", is_active=True)
    third = Tenant(name="متجر ثالث", is_active=True)
    session.add_all([holder, newcomer, third]); session.commit()
    session.add(WhatsAppConnection(
        tenant_id=holder.id, whatsapp_business_account_id=f"WABA-{holder.id}", phone_number_id=f"PN-{holder.id}",
        access_token="EAAB-secret", meta_catalog_id=CATALOG_HELD, catalog_enabled=True,
        provider="meta", connection_type="embedded", extra_metadata={},
    ))
    for t in (newcomer, third):
        session.add(WhatsAppConnection(
            tenant_id=t.id, whatsapp_business_account_id=f"WABA-{t.id}", phone_number_id=f"PN-{t.id}",
            access_token="EAAB-secret", meta_catalog_id=None, catalog_enabled=False,
            provider="meta", connection_type="embedded", extra_metadata={},
        ))
    session.commit()
    return session, holder.id, newcomer.id, third.id


def _conn(session, tid):
    return session.query(WhatsAppConnection).filter_by(tenant_id=tid).one()


# ── service helper ──────────────────────────────────────────────────────────


def test_guard_refuses_an_id_held_by_another_tenant_and_allows_the_holder():
    session, holder, newcomer, _ = _db()
    assert other_tenants_claiming(session, newcomer, CATALOG_HELD) == [holder]
    with pytest.raises(CatalogClaimError) as exc:
        guard_catalog_claim(session, newcomer, CATALOG_HELD)
    assert exc.value.code == ERROR_CATALOG_CLAIMED_BY_OTHER_TENANT
    assert exc.value.detail["catalog_id"] == CATALOG_HELD
    assert exc.value.detail["claimed_by_tenant_count"] == 1
    # The holder re-claiming its own id, a fresh id, or an empty id is never refused.
    guard_catalog_claim(session, holder, CATALOG_HELD)
    guard_catalog_claim(session, newcomer, CATALOG_FRESH)
    guard_catalog_claim(session, newcomer, "")
    # The guard never writes.
    assert _conn(session, newcomer).meta_catalog_id is None


# ── merchant / admin PATCH (_apply_config_changes with db) ─────────────────


def test_patch_refuses_adopting_another_tenants_catalog_with_409_and_no_mutation():
    session, holder, newcomer, _ = _db()
    conn = _conn(session, newcomer)
    with pytest.raises(HTTPException) as exc:
        _apply_config_changes(conn, CatalogConfigPatch(meta_catalog_id=CATALOG_HELD, catalog_enabled=True), db=session)
    assert exc.value.status_code == 409
    assert exc.value.detail["error"] == ERROR_CATALOG_CLAIMED_BY_OTHER_TENANT
    session.rollback()
    assert _conn(session, newcomer).meta_catalog_id is None
    assert _conn(session, newcomer).catalog_enabled is False
    assert _conn(session, holder).meta_catalog_id == CATALOG_HELD


def test_patch_allows_the_holder_a_fresh_id_and_clearing_then_blocks_the_third_tenant():
    session, holder, newcomer, third = _db()
    # Holder re-stamps its own id (idempotent) and toggles enabled: allowed.
    assert _apply_config_changes(_conn(session, holder), CatalogConfigPatch(meta_catalog_id=CATALOG_HELD), db=session) == {}
    # Newcomer adopts a fresh id: allowed and persisted.
    changes = _apply_config_changes(_conn(session, newcomer), CatalogConfigPatch(meta_catalog_id=CATALOG_FRESH, catalog_enabled=True), db=session)
    session.commit()
    assert changes["meta_catalog_id"]["after"] == CATALOG_FRESH
    # A third tenant now cannot take the fresh id.
    with pytest.raises(HTTPException) as exc:
        _apply_config_changes(_conn(session, third), CatalogConfigPatch(meta_catalog_id=CATALOG_FRESH), db=session)
    assert exc.value.status_code == 409
    session.rollback()
    # Clearing the binding is never guarded.
    changes = _apply_config_changes(_conn(session, newcomer), CatalogConfigPatch(meta_catalog_id="", catalog_enabled=False), db=session)
    session.commit()
    assert changes["meta_catalog_id"]["after"] is None
    assert _conn(session, newcomer).meta_catalog_id is None


def test_both_patch_endpoints_pass_the_session_to_the_guard():
    for fn in (catalog_router.merchant_catalog_patch, catalog_router.admin_catalog_patch):
        src = inspect.getsource(fn)
        assert "_apply_config_changes(conn, body, db=db)" in src, fn.__name__


# ── admin debug POST ────────────────────────────────────────────────────────


def test_admin_debug_catalog_config_refuses_another_tenants_catalog():
    session, holder, newcomer, _ = _db()
    # The endpoint's body annotation (``_CatalogConfigBody``) is undefined on main, so
    # FastAPI registers the route without a body parameter (pre-existing, out of scope
    # here). The handler itself still reads ``body.tenant_id`` / ``body.meta_catalog_id``
    # / ``body.catalog_enabled``, so the guard is exercised with a plain namespace.
    def body_cls(**kw):
        kw.setdefault("catalog_enabled", None)
        return SimpleNamespace(**kw)
    body = body_cls(tenant_id=newcomer, meta_catalog_id=CATALOG_HELD)
    with pytest.raises(HTTPException) as exc:
        asyncio.run(admin_debug.admin_debug_set_catalog_config(body, db=session, _admin={"sub": "admin"}))
    assert exc.value.status_code == 409
    assert exc.value.detail["error"] == ERROR_CATALOG_CLAIMED_BY_OTHER_TENANT
    session.rollback()
    assert _conn(session, newcomer).meta_catalog_id is None
    # A fresh id through the same path is accepted.
    out = asyncio.run(admin_debug.admin_debug_set_catalog_config(
        body_cls(tenant_id=newcomer, meta_catalog_id=CATALOG_FRESH), db=session, _admin={"sub": "admin"},
    ))
    assert _conn(session, newcomer).meta_catalog_id == CATALOG_FRESH
    assert isinstance(out, dict)


# ── real PostgreSQL: two tenants race for the same id ───────────────────────


def _pg_url():
    url = (os.getenv("WA_CATALOG_SYNC_PG_TEST_DATABASE_URL") or "").strip()
    if not url.startswith(("postgresql://", "postgresql+psycopg2://")):
        if (os.getenv("WA_CATALOG_SYNC_PG_REQUIRED") or "").strip() == "1":
            pytest.fail("WA_CATALOG_SYNC_PG_TEST_DATABASE_URL is required")
        pytest.skip("WA_CATALOG_SYNC_PG_TEST_DATABASE_URL not set; PostgreSQL concurrency case skipped")
    return url


@pytest.fixture
def claim_pg():
    from sqlalchemy.pool import NullPool  # noqa: PLC0415

    url = _pg_url()
    schema = f"claim_guard_itest_{os.getpid()}"
    admin = create_engine(url, poolclass=NullPool, pool_pre_ping=True)
    try:
        with admin.connect() as c:
            c.execute(text("SELECT 1"))
    except Exception as exc:  # noqa: BLE001
        pytest.skip(f"PostgreSQL unavailable: {exc}")
    with admin.begin() as c:
        c.execute(text(f'DROP SCHEMA IF EXISTS "{schema}" CASCADE'))
        c.execute(text(f'CREATE SCHEMA "{schema}"'))

    def engine():
        return create_engine(url, poolclass=NullPool, connect_args={"options": f"-c search_path={schema}"})

    e = engine()
    Base.metadata.create_all(e, tables=[Tenant.__table__, WhatsAppConnection.__table__])
    with e.begin() as c:
        c.execute(text("INSERT INTO tenants (id, name, is_active, is_platform_tenant) VALUES (35, 'متجر تجريبي عام', true, false), (47, 'متجر آخر', true, false)"))
        c.execute(text("INSERT INTO whatsapp_connections (tenant_id, status, provider) VALUES (35, 'connected', 'meta'), (47, 'connected', 'meta')"))
    yield engine
    with admin.begin() as c:
        c.execute(text(f'DROP SCHEMA IF EXISTS "{schema}" CASCADE'))


def test_postgres_two_tenants_cannot_adopt_the_same_catalog_concurrently(claim_pg):
    cid = "CAT-RACE-7003"
    a_locked = threading.Event()
    b_waiting = threading.Event()
    outcome = {}

    def tenant_a():
        s = sessionmaker(bind=claim_pg())()
        try:
            guard_catalog_claim(s, 35, cid)          # takes the per-catalog lock
            s.execute(text("UPDATE whatsapp_connections SET meta_catalog_id = :c WHERE tenant_id = 35"), {"c": cid})
            a_locked.set()
            b_waiting.wait(timeout=10)
            time.sleep(0.6)                          # B is blocked on the lock during this window
            s.commit()                               # releases the lock; B's check now sees A's row
            outcome["a"] = "committed"
        except Exception as exc:  # noqa: BLE001
            s.rollback(); outcome["a"] = f"error:{exc}"
        finally:
            s.close()

    def tenant_b():
        s = sessionmaker(bind=claim_pg())()
        a_locked.wait(timeout=10)
        b_waiting.set()
        t0 = time.monotonic()
        try:
            guard_catalog_claim(s, 47, cid)
            s.execute(text("UPDATE whatsapp_connections SET meta_catalog_id = :c WHERE tenant_id = 47"), {"c": cid})
            s.commit(); outcome["b"] = "committed"
        except CatalogClaimError as exc:
            s.rollback(); outcome["b"] = exc.code
        finally:
            outcome["b_waited"] = time.monotonic() - t0
            s.close()

    ta, tb = threading.Thread(target=tenant_a), threading.Thread(target=tenant_b)
    ta.start(); tb.start(); ta.join(timeout=30); tb.join(timeout=30)

    assert outcome["a"] == "committed"
    assert outcome["b"] == ERROR_CATALOG_CLAIMED_BY_OTHER_TENANT
    assert outcome["b_waited"] >= 0.4, outcome          # B really waited on A's lock, it did not race past the check
    with claim_pg().connect() as c:
        rows = c.execute(text("SELECT tenant_id FROM whatsapp_connections WHERE meta_catalog_id = :c ORDER BY tenant_id"), {"c": cid}).fetchall()
    assert [r[0] for r in rows] == [35]


def test_postgres_guard_is_a_no_op_for_an_empty_id_and_allows_distinct_ids(claim_pg):
    s = sessionmaker(bind=claim_pg())()
    guard_catalog_claim(s, 35, "")
    guard_catalog_claim(s, 35, "CAT-X-1")
    s.execute(text("UPDATE whatsapp_connections SET meta_catalog_id = 'CAT-X-1' WHERE tenant_id = 35"))
    s.commit()
    guard_catalog_claim(s, 47, "CAT-X-2")
    with pytest.raises(CatalogClaimError):
        guard_catalog_claim(s, 47, "CAT-X-1")
    s.rollback(); s.close()

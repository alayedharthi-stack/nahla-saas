"""Cross-tenant catalog-claim guard on real PostgreSQL: two tenants racing for one id.

The advisory lock per catalog id must make a second tenant wait for the first
and then refuse the id the first committed. Split from
``tests/test_meta_catalog_claim_guard.py`` (unit cases) so the strict required
PostgreSQL proofs run exactly these cases: each test gets a throw-away database
on the admin DSN in ``NAHLA_RELIABILITY_PG_ADMIN_DSN``, dropped at teardown.
Without the variable the module is skipped (reported as skipped, never as
passed); with ``NAHLA_RELIABILITY_REQUIRE_PG=1`` and no variable it fails.

Generic merchants only (متجر تجريبي عام / متجر آخر); no production ids.
"""
from __future__ import annotations

import os
import secrets
import sys
import threading
import time
from pathlib import Path

import pytest
from sqlalchemy import create_engine, text
from sqlalchemy.orm import sessionmaker

_REPO = Path(__file__).resolve().parents[2]
for _p in (_REPO, _REPO / "backend", _REPO / "database"):
    if str(_p) not in sys.path:
        sys.path.insert(0, str(_p))

from database.models import Base, Tenant, WhatsAppConnection  # noqa: E402
from services.meta_catalog_claim import (  # noqa: E402
    ERROR_CATALOG_CLAIMED_BY_OTHER_TENANT,
    CatalogClaimError,
    guard_catalog_claim,
)

_DB_PREFIX = "claim_guard_proof"

ADMIN_URL = (os.environ.get("NAHLA_RELIABILITY_PG_ADMIN_DSN") or "").strip()
if not ADMIN_URL and os.environ.get("NAHLA_RELIABILITY_REQUIRE_PG") == "1":
    raise RuntimeError("NAHLA_RELIABILITY_REQUIRE_PG=1 but NAHLA_RELIABILITY_PG_ADMIN_DSN is not set")
pytestmark = pytest.mark.skipif(not ADMIN_URL, reason="NAHLA_RELIABILITY_PG_ADMIN_DSN not set (no real PostgreSQL)")


def _admin(statement: str) -> None:
    from sqlalchemy import create_engine as _ce, text as _text  # noqa: PLC0415
    from sqlalchemy.pool import NullPool  # noqa: PLC0415

    eng = _ce(ADMIN_URL, poolclass=NullPool, isolation_level="AUTOCOMMIT", future=True)
    try:
        with eng.connect() as conn:
            conn.execute(_text(statement))
    finally:
        eng.dispose()


@pytest.fixture
def throwaway_pg_url():
    """A throw-away database on the admin DSN's server, dropped at teardown."""
    from sqlalchemy.engine.url import make_url  # noqa: PLC0415

    name = f"{_DB_PREFIX}_{secrets.token_hex(4)}"
    _admin(f'CREATE DATABASE "{name}"')
    try:
        yield make_url(ADMIN_URL).set(drivername="postgresql", database=name).render_as_string(hide_password=False)
    finally:
        _admin(f'DROP DATABASE IF EXISTS "{name}" WITH (FORCE)')


# ── real PostgreSQL: two tenants race for the same id ───────────────────────


@pytest.fixture
def claim_pg(throwaway_pg_url):
    from sqlalchemy.pool import NullPool  # noqa: PLC0415

    url = throwaway_pg_url
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

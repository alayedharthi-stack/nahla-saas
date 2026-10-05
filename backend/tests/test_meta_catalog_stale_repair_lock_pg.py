"""Stale-observation repair under real PostgreSQL concurrency: two sessions, one membership row.

``replace_stale_observation_after_create`` may replace a reconcile
observation's stale Graph item id with the id a verified create made. It
re-reads the row with ``SELECT … FOR UPDATE`` and refreshes the copy its
session already holds, so a writer that changed the row first is waited for
and its committed state is what the repair judges. These proofs use two real
sessions on a throw-away database per test (admin DSN in
``NAHLA_RELIABILITY_PG_ADMIN_DSN``, dropped at teardown), and confirm through
``pg_stat_activity`` that the repair really waited on the row lock:

* newer publication evidence, a remap to another product, or a delete
  committed while the repair waits is re-read and preserved — the repair
  refuses;
* two repairs of the same key serialize: the first commits, the second
  re-reads and refuses, and exactly one row remains.

Without the variable the module is skipped (reported as skipped, never as
passed); with ``NAHLA_RELIABILITY_REQUIRE_PG=1`` and no variable it fails.
Generic merchant data only (متجر تجريبي عام, «حذاء رياضي أبيض»).
"""
from __future__ import annotations

import os
import secrets
import sys
import threading
import time
from datetime import datetime, timezone
from pathlib import Path

import pytest
from sqlalchemy import create_engine, text
from sqlalchemy.orm import sessionmaker

_REPO = Path(__file__).resolve().parents[2]
for _p in (_REPO, _REPO / "backend", _REPO / "database"):
    if str(_p) not in sys.path:
        sys.path.insert(0, str(_p))

from database.models import Base, MetaCatalogMembership, Product, ProductVariant, Tenant  # noqa: E402
from core.catalog import OWNERSHIP_NAHLA_MANAGED, SOURCE_NAHLA_NATIVE  # noqa: E402
from core.meta_catalog_membership import (  # noqa: E402
    PROVENANCE_GRAPH_RECONCILE,
    PROVENANCE_NATIVE_PUSH,
    PROVENANCE_NATIVE_PUSH_RECONCILED,
)
from services.salla_variant_catalog_identity import (  # noqa: E402
    replace_stale_observation_after_create,
    upsert_native_publication_membership,
)

_DB_PREFIX = "stale_repair_proof"
CATALOG = "CAT-GENERIC-RACE"
RID = "SHOE-RACE-PG-1"

ADMIN_URL = (os.environ.get("NAHLA_RELIABILITY_PG_ADMIN_DSN") or "").strip()
if not ADMIN_URL and os.environ.get("NAHLA_RELIABILITY_REQUIRE_PG") == "1":
    raise RuntimeError("NAHLA_RELIABILITY_REQUIRE_PG=1 but NAHLA_RELIABILITY_PG_ADMIN_DSN is not set")
pytestmark = pytest.mark.skipif(not ADMIN_URL, reason="NAHLA_RELIABILITY_PG_ADMIN_DSN not set (no real PostgreSQL)")


def _admin(statement: str) -> None:
    from sqlalchemy.pool import NullPool  # noqa: PLC0415

    eng = create_engine(ADMIN_URL, poolclass=NullPool, isolation_level="AUTOCOMMIT", future=True)
    try:
        with eng.connect() as conn:
            conn.execute(text(statement))
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


@pytest.fixture
def race_pg(throwaway_pg_url):
    """Schema with one tenant, two native products and a stale reconcile observation for RID."""
    from sqlalchemy.pool import NullPool  # noqa: PLC0415

    url = throwaway_pg_url
    engine = create_engine(url, poolclass=NullPool)
    Base.metadata.create_all(engine, tables=[Tenant.__table__, Product.__table__, ProductVariant.__table__,
                                             MetaCatalogMembership.__table__])
    with sessionmaker(bind=engine)() as s:
        tenant = Tenant(name="متجر تجريبي عام", is_active=True)
        s.add(tenant)
        s.flush()
        ids = {"tenant": tenant.id}
        for key, title in (("p1", "حذاء رياضي أبيض"), ("p2", "قميص قطني أزرق")):
            product = Product(tenant_id=tenant.id, title=title, price="250", in_stock=True, stock_quantity=3,
                              source=SOURCE_NAHLA_NATIVE, ownership_mode=OWNERSHIP_NAHLA_MANAGED, catalog_status="active",
                              extra_metadata={"currency": "SAR"})
            s.add(product)
            s.flush()
            variant = ProductVariant(tenant_id=tenant.id, product_id=product.id,
                                     retailer_id=RID if key == "p1" else f"{RID}-OTHER",
                                     price="250", currency="SAR", stock_quantity=3, in_stock=True, is_default=True)
            s.add(variant)
            s.flush()
            ids[key], ids[f"{key}v"] = product.id, variant.id
        s.add(MetaCatalogMembership(
            tenant_id=tenant.id, catalog_id=CATALOG, retailer_id=RID, product_id=ids["p1"], variant_id=ids["p1v"],
            meta_item_id="GONE-1", verified_at=datetime.now(timezone.utc), provenance=PROVENANCE_GRAPH_RECONCILE,
        ))
        s.commit()
    yield engine, ids
    engine.dispose()


def _session(engine):
    return sessionmaker(bind=engine)()


def _repair(session, ids, created):
    return replace_stale_observation_after_create(
        session, tenant_id=ids["tenant"], catalog_id=CATALOG, retailer_id=RID, product_id=ids["p1"],
        variant_id=ids["p1v"], created_meta_item_id=created, corroborated_meta_item_id=created,
        publication_provenance=PROVENANCE_NATIVE_PUSH_RECONCILED,
    )


def _wait_for_lock_waiter(engine, timeout=10.0) -> bool:
    """True once some backend of this database is waiting on a lock."""
    deadline = time.monotonic() + timeout
    with engine.connect() as c:
        while time.monotonic() < deadline:
            n = c.execute(text(
                "SELECT count(*) FROM pg_stat_activity WHERE datname = current_database() "
                "AND wait_event_type = 'Lock' AND state = 'active'"
            )).scalar()
            if n:
                return True
            c.rollback()
            time.sleep(0.05)
    return False


def _rows(engine, ids):
    with engine.connect() as c:
        return c.execute(text(
            "SELECT product_id, meta_item_id, provenance FROM meta_catalog_memberships "
            "WHERE tenant_id = :t AND catalog_id = :c AND retailer_id = :r"
        ), {"t": ids["tenant"], "c": CATALOG, "r": RID}).fetchall()


def _run_repair_in_thread(engine, ids, created, *, preload=True):
    """Start a repair in its own session; returns (thread, outcome dict).

    With *preload* the session first does exactly what the create path does
    before the repair — the generic upsert, refused on the stale id — so it
    already holds a stale copy of the row when the repair runs."""
    outcome = {}
    ready = threading.Event()

    def run():
        s = _session(engine)
        try:
            if preload:
                first = upsert_native_publication_membership(
                    s, tenant_id=ids["tenant"], catalog_id=CATALOG, retailer_id=RID, product_id=ids["p1"],
                    variant_id=ids["p1v"], meta_item_id=created)
                outcome["upsert"] = first.get("reason")
                # Hold the loaded row so this session keeps its (soon stale) copy in
                # the identity map: the repair must refresh it, not trust it.
                outcome["held"] = s.query(MetaCatalogMembership).filter_by(
                    tenant_id=ids["tenant"], catalog_id=CATALOG, retailer_id=RID).first()
                outcome["held_before"] = (outcome["held"].meta_item_id, outcome["held"].provenance)
            ready.set()
            outcome["go"].wait(timeout=10)
            t0 = time.monotonic()
            result = _repair(s, ids, created)
            outcome["waited"] = time.monotonic() - t0
            outcome["result"] = result
            s.commit()
        except Exception as exc:  # noqa: BLE001
            s.rollback()
            outcome["error"] = repr(exc)
        finally:
            s.close()

    outcome["go"] = threading.Event()
    thread = threading.Thread(target=run)
    thread.start()
    assert ready.wait(timeout=10)
    return thread, outcome


@pytest.mark.parametrize("change", ["newer_publication", "remap_to_another_product", "delete"])
def test_postgres_repair_waits_for_a_concurrent_change_and_refuses_it(race_pg, change):
    engine, ids = race_pg
    thread, outcome = _run_repair_in_thread(engine, ids, "META-MINE")
    assert outcome["upsert"] == "meta_item_id_immutable"
    assert outcome["held_before"] == ("GONE-1", PROVENANCE_GRAPH_RECONCILE)   # the repair's session holds the stale copy

    writer = _session(engine)
    row = writer.execute(text(
        "SELECT id FROM meta_catalog_memberships WHERE tenant_id = :t AND catalog_id = :c AND retailer_id = :r FOR UPDATE"
    ), {"t": ids["tenant"], "c": CATALOG, "r": RID}).scalar()
    if change == "newer_publication":
        writer.execute(text("UPDATE meta_catalog_memberships SET meta_item_id = 'META-NEWER', provenance = :p WHERE id = :id"),
                       {"p": PROVENANCE_NATIVE_PUSH, "id": row})
    elif change == "remap_to_another_product":
        writer.execute(text("UPDATE meta_catalog_memberships SET product_id = :p2, variant_id = :v2 WHERE id = :id"),
                       {"p2": ids["p2"], "v2": ids["p2v"], "id": row})
    else:
        writer.execute(text("DELETE FROM meta_catalog_memberships WHERE id = :id"), {"id": row})
    outcome["go"].set()
    assert _wait_for_lock_waiter(engine), "the repair never waited on the row lock"
    time.sleep(0.3)
    writer.commit()
    writer.close()
    thread.join(timeout=30)

    assert "error" not in outcome, outcome
    assert outcome["waited"] >= 0.25, outcome
    expected = {"newer_publication": "meta_item_id_immutable",
                "remap_to_another_product": "product_id_immutable",
                "delete": "no_observation_to_replace"}[change]
    assert outcome["result"]["ok"] is False and outcome["result"]["reason"] == expected, outcome
    rows = _rows(engine, ids)
    if change == "newer_publication":
        assert [tuple(r) for r in rows] == [(ids["p1"], "META-NEWER", PROVENANCE_NATIVE_PUSH)]
    elif change == "remap_to_another_product":
        assert [tuple(r) for r in rows] == [(ids["p2"], "GONE-1", PROVENANCE_GRAPH_RECONCILE)]
    else:
        assert rows == []


@pytest.mark.parametrize("second_created", ["META-SECOND", "META-FIRST"])
def test_postgres_two_repairs_of_the_same_key_serialize(race_pg, second_created):
    engine, ids = race_pg
    first = _session(engine)
    result = _repair(first, ids, "META-FIRST")                       # holds the row lock, not yet committed
    assert result["ok"] is True

    thread, outcome = _run_repair_in_thread(engine, ids, second_created)
    outcome["go"].set()
    assert _wait_for_lock_waiter(engine), "the second repair never waited on the row lock"
    time.sleep(0.3)
    first.commit()
    first.close()
    thread.join(timeout=30)

    assert "error" not in outcome, outcome
    assert outcome["waited"] >= 0.25, outcome
    assert outcome["result"]["ok"] is False, outcome
    assert outcome["result"]["reason"] == "meta_item_id_immutable", outcome   # now publication evidence: never rebound
    assert [tuple(r) for r in _rows(engine, ids)] == [(ids["p1"], "META-FIRST", PROVENANCE_NATIVE_PUSH_RECONCILED)]

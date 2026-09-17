"""PostgreSQL proof: a ``customer_name_provenance`` row no longer blocks
customer deletion.

Migration 0107 gives ``customer_name_provenance.customer_id`` a NOT NULL
foreign key to ``customers.id`` with no ``ON DELETE CASCADE``. Every name
decision — including the no-op re-sync a Salla poll produces for an
unchanged verified name — creates that row through
``core.customer_name_provenance.record_name_decision``. ``DELETE
/customers/{id}`` and ``POST /customers/bulk-delete`` both go through
``routers.customers._delete_customer_children``, which did not delete the
provenance row, so the parent delete failed with ``ForeignKeyViolation``.

The same helper also issued ``UPDATE delivery_quality_events …`` — a table
with no model and no migration in this repository — which aborts the
transaction with ``UndefinedTable`` before the provenance FK is even
reached on a schema built from this repository.

Both are proven on real PostgreSQL. SQLite does not enforce the FK by
default, so only a PostgreSQL run is evidence. Runs when a DSN is available
(``legacy_migration_drift_postgres_fixtures.connect_engine``); REQUIRED
under ``CUSTOMER_NAME_PROVENANCE_PG_REQUIRED=1`` or
``LEGACY_MIG_PG_INTEGRATION_REQUIRED=1``.

INTELLIGENCE_NON_INTERFERENCE_POLICY=ACTIVE
MODEL_CHANGED=NO
PROMPT_CHANGED=NO
PERSONA_CHANGED=NO
PHRASE_MAP_CHANGED=NO
KEYWORD_ROUTER_CHANGED=NO
CUSTOMER_REGEX_CHANGED=NO
"""
from __future__ import annotations

import os
import sys
from pathlib import Path
from typing import Iterator

import pytest
from sqlalchemy import create_engine, text
from sqlalchemy.engine import Engine
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session, sessionmaker
from sqlalchemy.pool import NullPool

_BACKEND = Path(__file__).resolve().parents[1]
_REPO = _BACKEND.parent
_DATABASE = _REPO / "database"
for _entry in (str(_REPO), str(_BACKEND), str(_DATABASE)):
    if _entry not in sys.path:
        sys.path.insert(0, _entry)

from core.customer_identity_resolver import apply_customer_name  # noqa: E402
from legacy_migration_drift_postgres_fixtures import (  # noqa: E402
    connect_engine,
    create_ephemeral_database,
    drop_ephemeral_database,
    run_alembic,
)
from models import Base, Customer, CustomerNameProvenance, Tenant  # noqa: E402
from routers.customers import _delete_customer_children  # noqa: E402

VERIFIED_NAME = "محمد أحمد الحارثي"
PHANTOM_TABLE = "delivery_quality_events"


def _pg_required() -> bool:
    return (
        (os.getenv("CUSTOMER_NAME_PROVENANCE_PG_REQUIRED") or "").strip() == "1"
        or (os.getenv("LEGACY_MIG_PG_INTEGRATION_REQUIRED") or "").strip() == "1"
    )


def _pg_admin() -> Engine:
    try:
        engine = connect_engine()
    except pytest.skip.Exception as exc:
        if _pg_required():
            pytest.fail(f"PostgreSQL required for customer delete provenance tests: {exc}")
        raise
    if engine.dialect.name != "postgresql":
        pytest.fail("customer delete provenance tests require PostgreSQL, not SQLite")
    return engine


@pytest.fixture(scope="module")
def pg_production_shape() -> Iterator[Engine]:
    """Migrated to 0107, then ``Base.metadata.create_all`` — the shape the
    production bootstrap produces (alembic pin + create_all for ORM-only
    tables). ``delivery_quality_events`` is deliberately absent: nothing in
    this repository creates it."""
    admin = _pg_admin()
    db_name, _ = create_ephemeral_database(admin)
    engine = create_engine(
        str(admin.url.set(database=db_name).render_as_string(hide_password=False)),
        poolclass=NullPool,
        pool_pre_ping=True,
    )
    try:
        run_alembic(engine, "0107")
        Base.metadata.create_all(engine)
        yield engine
    finally:
        engine.dispose()
        drop_ephemeral_database(admin, db_name)
        admin.dispose()


def _session(engine: Engine) -> Session:
    return sessionmaker(bind=engine, autocommit=False, autoflush=True, expire_on_commit=False)()


def _seed_customer_with_provenance(db: Session, *, phone: str, tenant: Tenant | None = None) -> Customer:
    if tenant is None:
        tenant = Tenant(name=f"del-prov-{os.urandom(4).hex()}", is_active=True)
        db.add(tenant)
        db.flush()
    cust = Customer(
        tenant_id=tenant.id, phone=phone, normalized_phone=phone,
        extra_metadata={}, acquisition_channel="whatsapp_inbound",
    )
    db.add(cust)
    db.flush()
    assert apply_customer_name(cust, VERIFIED_NAME, source="salla_sync") is True
    db.commit()
    assert db.query(CustomerNameProvenance).filter(
        CustomerNameProvenance.customer_id == cust.id,
    ).count() == 1, "apply_customer_name must have written the durable provenance row"
    return cust


def _provenance_count(db: Session, customer_id: int) -> int:
    return db.query(CustomerNameProvenance).filter(
        CustomerNameProvenance.customer_id == customer_id,
    ).count()


# ── Negative control: the FK really has no cascade ────────────────────

def test_control_parent_delete_without_child_cleanup_is_rejected(pg_production_shape: Engine) -> None:
    db = _session(pg_production_shape)
    try:
        cust = _seed_customer_with_provenance(db, phone="+966500000101")
        with pytest.raises(IntegrityError) as failed:
            db.execute(text("DELETE FROM customers WHERE id = :id"), {"id": cust.id})
        assert "customer_name_provenance" in str(failed.value)
        db.rollback()
        assert _provenance_count(db, cust.id) == 1
    finally:
        db.rollback()
        db.close()


# ── The fix ───────────────────────────────────────────────────────────

def test_delete_customer_removes_provenance_row_then_parent(pg_production_shape: Engine) -> None:
    """The exact sequence of ``DELETE /customers/{id}``."""
    db = _session(pg_production_shape)
    try:
        cust = _seed_customer_with_provenance(db, phone="+966500000102")
        tenant_id, customer_id = cust.tenant_id, cust.id

        _delete_customer_children(db, [customer_id], tenant_id)
        deleted = db.query(Customer).filter(
            Customer.id == customer_id, Customer.tenant_id == tenant_id,
        ).delete(synchronize_session=False)
        db.commit()

        assert deleted == 1
        assert _provenance_count(db, customer_id) == 0
        db.expire_all()  # bulk delete used synchronize_session=False
        assert db.query(Customer).filter(Customer.id == customer_id).count() == 0
    finally:
        db.rollback()
        db.close()


def test_delete_on_schema_without_delivery_quality_events(pg_production_shape: Engine) -> None:
    """``delivery_quality_events`` does not exist in this repository's
    schema; the nullable-FK pass must skip it instead of aborting."""
    with pg_production_shape.connect() as conn:
        assert conn.execute(
            text("SELECT to_regclass(:name)"), {"name": f"public.{PHANTOM_TABLE}"}
        ).scalar() is None
    db = _session(pg_production_shape)
    try:
        cust = _seed_customer_with_provenance(db, phone="+966500000103")
        tenant_id, customer_id = cust.tenant_id, cust.id
        _delete_customer_children(db, [customer_id], tenant_id)
        db.query(Customer).filter(Customer.id == customer_id).delete(synchronize_session=False)
        db.commit()
        db.expire_all()
        assert db.query(Customer).filter(Customer.id == customer_id).count() == 0
    finally:
        db.rollback()
        db.close()


def test_bulk_delete_sequence_removes_every_provenance_row(pg_production_shape: Engine) -> None:
    """The exact sequence of ``POST /customers/bulk-delete``."""
    db = _session(pg_production_shape)
    try:
        first = _seed_customer_with_provenance(db, phone="+966500000104")
        tenant = db.get(Tenant, first.tenant_id)
        second = _seed_customer_with_provenance(db, phone="+966500000105", tenant=tenant)
        ids = [first.id, second.id]

        _delete_customer_children(db, ids, tenant.id)
        deleted = db.query(Customer).filter(
            Customer.id.in_(ids), Customer.tenant_id == tenant.id,
        ).delete(synchronize_session=False)
        db.commit()

        assert deleted == 2
        assert db.query(CustomerNameProvenance).filter(
            CustomerNameProvenance.customer_id.in_(ids),
        ).count() == 0
    finally:
        db.rollback()
        db.close()


def test_child_cleanup_is_tenant_scoped(pg_production_shape: Engine) -> None:
    """A wrong tenant id must not delete another tenant's provenance row,
    and the parent delete is then still rejected by the FK."""
    db = _session(pg_production_shape)
    try:
        victim = _seed_customer_with_provenance(db, phone="+966500000106")
        other = _seed_customer_with_provenance(db, phone="+966500000107")
        assert other.tenant_id != victim.tenant_id

        _delete_customer_children(db, [victim.id], other.tenant_id)
        assert _provenance_count(db, victim.id) == 1
        with pytest.raises(IntegrityError):
            db.execute(text("DELETE FROM customers WHERE id = :id"), {"id": victim.id})
        db.rollback()
        assert _provenance_count(db, victim.id) == 1
        assert _provenance_count(db, other.id) == 1
    finally:
        db.rollback()
        db.close()

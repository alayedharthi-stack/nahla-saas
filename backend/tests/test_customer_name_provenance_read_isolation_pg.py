"""PostgreSQL proof: the customer-name provenance READ never poisons the
caller's transaction.

Why PostgreSQL specifically: a failed statement (``UndefinedTable`` while
migration 0107 has not been applied) aborts the enclosing transaction;
every later statement fails with ``InFailedSqlTransaction`` until a
rollback. SQLite has no such semantics, so only a real PostgreSQL run
proves the fix. Two ephemeral databases are used: one migrated to
``0106`` (no ``customer_name_provenance`` table) and one to ``0107``.

Runs when a PostgreSQL DSN is available (see
``legacy_migration_drift_postgres_fixtures.connect_engine``); it is
REQUIRED under ``CUSTOMER_NAME_PROVENANCE_PG_REQUIRED=1`` or
``LEGACY_MIG_PG_INTEGRATION_REQUIRED=1`` (the CI a1-postgres job).

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
from sqlalchemy.exc import DBAPIError
from sqlalchemy.orm import Session, sessionmaker
from sqlalchemy.pool import NullPool

_BACKEND = Path(__file__).resolve().parents[1]
_REPO = _BACKEND.parent
_DATABASE = _REPO / "database"
for _entry in (str(_REPO), str(_BACKEND), str(_DATABASE)):
    if _entry not in sys.path:
        sys.path.insert(0, _entry)

from core.customer_identity_resolver import apply_customer_name  # noqa: E402
from core.customer_name_authority import NameAuthority  # noqa: E402
from core.customer_name_provenance import read_name_authority  # noqa: E402
from legacy_migration_drift_postgres_fixtures import (  # noqa: E402
    connect_engine,
    create_ephemeral_database,
    drop_ephemeral_database,
    run_alembic,
)
from models import Base, Customer, CustomerNameProvenance, Tenant  # noqa: E402

VERIFIED_NAME = "محمد أحمد الحارثي"


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
            pytest.fail(f"PostgreSQL required for provenance read-isolation tests: {exc}")
        raise
    if engine.dialect.name != "postgresql":
        pytest.fail("provenance read-isolation tests require PostgreSQL, not SQLite")
    return engine


def _ephemeral_at(revision: str) -> Iterator[Engine]:
    admin = _pg_admin()
    db_name, _ = create_ephemeral_database(admin)
    engine = create_engine(
        str(admin.url.set(database=db_name).render_as_string(hide_password=False)),
        poolclass=NullPool,
        pool_pre_ping=True,
    )
    try:
        run_alembic(engine, revision)
        yield engine
    finally:
        engine.dispose()
        drop_ephemeral_database(admin, db_name)
        admin.dispose()


@pytest.fixture(scope="module")
def pg_without_provenance_table() -> Iterator[Engine]:
    """Schema at 0106 — the migration BEFORE customer_name_provenance."""
    yield from _ephemeral_at("0106")


@pytest.fixture(scope="module")
def pg_with_provenance_table() -> Iterator[Engine]:
    """Schema at 0107 — customer_name_provenance exists."""
    yield from _ephemeral_at("0107")


def _session(engine: Engine) -> Session:
    return sessionmaker(bind=engine, autocommit=False, autoflush=True, expire_on_commit=False)()


def _seed(db: Session, *, name=None, meta=None) -> Customer:
    tenant = Tenant(name=f"prov-iso-{os.urandom(4).hex()}", is_active=True)
    db.add(tenant)
    db.flush()
    cust = Customer(
        tenant_id=tenant.id, phone="+966500000000", normalized_phone="+966500000000",
        name=name, extra_metadata=dict(meta or {}), acquisition_channel="whatsapp_inbound",
    )
    db.add(cust)
    db.flush()
    return cust


def _pending_sibling(db: Session, tenant_id: int, phone: str) -> Customer:
    sibling = Customer(tenant_id=tenant_id, phone=phone, normalized_phone=phone, name="عميل آخر")
    db.add(sibling)
    assert sibling in db.new, "sibling must still be PENDING (not flushed) before the read"
    return sibling


def test_family_profile_then_explicit_kunya_persists_on_postgres(pg_with_provenance_table: Engine) -> None:
    """The existing 0107 shape records both authorities without a new schema."""
    from core.customer_name_extractor import extract_high_confidence_name

    db = _session(pg_with_provenance_table)
    try:
        customer = _seed(db)
        apply_customer_name(customer, "الغامدي", source="whatsapp_inbound")
        db.commit()
        db.expire_all()
        assert customer.name == "الغامدي"
        hit = extract_high_confidence_name("هذا رقمي، أنا ابوسعد")
        assert hit is not None and hit.value == "أبو سعد"
        context = {"message": "هذا رقمي، أنا ابوسعد", "wa_message_id": "synthetic:pg-name"}
        apply_customer_name(customer, hit.value, source="ai_detected_name",
                            explicit_customer_entry=True, message_context=context)
        db.commit()
        db.expire_all()
        row = db.query(CustomerNameProvenance).filter_by(
            tenant_id=customer.tenant_id, customer_id=customer.id,
        ).one()
        assert customer.name == row.canonical_name == "أبو سعد"
        assert row.previous_name == row.profile_hint == "الغامدي"
        assert row.previous_authority == "WHATSAPP_PROFILE"
        assert row.authority == "CUSTOMER_SELF_REPORTED"
        assert row.evidence_ref["wa_message_id"] == "synthetic:pg-name"
        canonical_at = row.canonical_updated_at
        apply_customer_name(customer, hit.value, source="ai_detected_name",
                            explicit_customer_entry=True, message_context=context)
        db.commit()
        db.expire_all()
        assert row.canonical_updated_at == canonical_at
        assert row.previous_name == "الغامدي"
        assert row.last_decision == "noop"
        assert db.query(CustomerNameProvenance).filter_by(
            tenant_id=customer.tenant_id, customer_id=customer.id,
        ).count() == 1
    finally:
        db.close()


# ── Negative control: the hazard is real on PostgreSQL ────────────────

def test_control_unprotected_read_poisons_the_transaction(pg_without_provenance_table: Engine) -> None:
    """Without a savepoint, catching UndefinedTable is not enough on PG."""
    db = _session(pg_without_provenance_table)
    try:
        cust = _seed(db)
        with pytest.raises(DBAPIError):
            db.query(CustomerNameProvenance).filter(
                CustomerNameProvenance.customer_id == cust.id
            ).one_or_none()
        # The Python exception was caught above — but the transaction is aborted.
        with pytest.raises(DBAPIError) as failed:
            db.execute(text("SELECT 1"))
        assert "InFailedSqlTransaction" in type(failed.value.orig).__name__ or "current transaction is aborted" in str(failed.value)
    finally:
        db.rollback()
        db.close()


# ── The fix: read inside a SAVEPOINT ──────────────────────────────────

def test_missing_table_read_does_not_poison_outer_transaction(pg_without_provenance_table: Engine) -> None:
    db = _session(pg_without_provenance_table)
    try:
        cust = _seed(db, name=VERIFIED_NAME, meta={"customer_name_authority": "VERIFIED_ECOMMERCE"})
        sibling = _pending_sibling(db, cust.tenant_id, "+966511111111")
        cust.email = "pending@example.com"  # pending UPDATE on the business row
        assert db.in_transaction()

        # 1. does not raise; 2. falls back to the JSONB/source authority
        assert read_name_authority(cust) is NameAuthority.VERIFIED_ECOMMERCE

        # 3. outer transaction remains usable: another ORM query succeeds
        assert db.query(Customer).filter(Customer.normalized_phone == "+966511111111").count() == 1
        assert db.execute(text("SELECT 1")).scalar() == 1

        # 4. commit succeeds and the unrelated business mutation persists
        db.commit()
    finally:
        db.close()

    check = _session(pg_without_provenance_table)
    try:
        assert check.query(Customer).filter(Customer.normalized_phone == "+966511111111").one().name == "عميل آخر"
        assert check.get(Customer, cust.id).email == "pending@example.com"
        assert check.get(Customer, cust.id).name == VERIFIED_NAME
    finally:
        check.close()


def test_begin_nested_autoflush_side_effect_is_documented(pg_without_provenance_table: Engine) -> None:
    """``Session.begin_nested()`` flushes pending state BEFORE emitting
    SAVEPOINT (SQLAlchemy ``SessionTransaction._take_snapshot``). The
    pending sibling is therefore INSERTed into the OUTER transaction by
    the read — the same thing any autoflushing ORM query would do — and
    the savepoint rollback caused by the missing table cannot undo it."""
    db = _session(pg_without_provenance_table)
    try:
        cust = _seed(db, name=VERIFIED_NAME, meta={"customer_name_authority": "VERIFIED_ECOMMERCE"})
        sibling = _pending_sibling(db, cust.tenant_id, "+966522222222")

        read_name_authority(cust)

        assert sibling not in db.new, "begin_nested() flushed the pending sibling before SAVEPOINT"
        assert sibling.id is not None
        # Visible inside the still-open outer transaction, i.e. it survived the savepoint rollback.
        assert db.execute(
            text("SELECT count(*) FROM customers WHERE normalized_phone = :p"), {"p": "+966522222222"}
        ).scalar() == 1
        db.commit()
    finally:
        db.close()


def test_apply_customer_name_end_to_end_with_missing_table(pg_without_provenance_table: Engine) -> None:
    """The real write path (read → resolve → write) with no provenance table:
    the name is applied and committed; provenance degrades to the JSONB mirror."""
    db = _session(pg_without_provenance_table)
    try:
        cust = _seed(db)
        assert apply_customer_name(cust, VERIFIED_NAME, source="salla_sync") is True
        assert cust.name == VERIFIED_NAME
        assert cust.extra_metadata["customer_name_authority"] == "VERIFIED_ECOMMERCE"
        # precedence unchanged: WhatsApp cannot overwrite the verified name
        assert apply_customer_name(cust, "أبو خالد", source="whatsapp_inbound") is False
        db.commit()
    finally:
        db.close()
    check = _session(pg_without_provenance_table)
    try:
        assert check.get(Customer, cust.id).name == VERIFIED_NAME
        assert read_name_authority(check.get(Customer, cust.id)) is NameAuthority.VERIFIED_ECOMMERCE
    finally:
        check.close()


# ── Normal behaviour when the table exists is unchanged ───────────────

def test_durable_row_is_read_and_precedence_unchanged_when_table_exists(pg_with_provenance_table: Engine) -> None:
    db = _session(pg_with_provenance_table)
    try:
        cust = _seed(db)
        assert apply_customer_name(cust, VERIFIED_NAME, source="salla_sync") is True
        row = db.query(CustomerNameProvenance).filter(CustomerNameProvenance.customer_id == cust.id).one()
        assert row.canonical_name == VERIFIED_NAME and row.authority == "VERIFIED_ECOMMERCE"

        # The durable row (not just the JSONB mirror) is what the read returns:
        cust.extra_metadata = {}  # wipe the mirror
        assert read_name_authority(cust) is NameAuthority.VERIFIED_ECOMMERCE

        # A pending unrelated mutation survives the (released) savepoint read.
        sibling = _pending_sibling(db, cust.tenant_id, "+966533333333")
        assert read_name_authority(cust) is NameAuthority.VERIFIED_ECOMMERCE
        assert db.query(Customer).filter(Customer.normalized_phone == "+966533333333").count() == 1

        # Precedence unchanged: lower authorities cannot replace the verified name.
        assert apply_customer_name(cust, "أبو خالد", source="whatsapp_inbound") is False
        assert apply_customer_name(
            cust, "سعد الغامدي", source="ai_detected_name", explicit_customer_entry=True,
            message_context={"message": "سعد الغامدي", "awaiting_name_answer": True},
        ) is False
        assert cust.name == VERIFIED_NAME
        db.commit()
        assert db.get(Customer, sibling.id).name == "عميل آخر"
    finally:
        db.close()


# ── Production materialization path: create_all, then the table is absent ──
#
# ``backend/main.py`` does not advance alembic past ``0093``; new ORM tables
# reach production through the background ``Base.metadata.create_all`` step
# (``_run_migrations``). Until that step has run, the schema is exactly the
# previous release's create_all schema minus ``customer_name_provenance``.
# This fixture builds that shape directly instead of going through alembic.

@pytest.fixture(scope="module")
def pg_create_all_without_provenance_table() -> Iterator[Engine]:
    admin = _pg_admin()
    db_name, _ = create_ephemeral_database(admin)
    engine = create_engine(
        str(admin.url.set(database=db_name).render_as_string(hide_password=False)),
        poolclass=NullPool,
        pool_pre_ping=True,
    )
    try:
        Base.metadata.create_all(engine)
        with engine.begin() as conn:
            conn.execute(text("DROP TABLE customer_name_provenance CASCADE"))
        yield engine
    finally:
        engine.dispose()
        drop_ephemeral_database(admin, db_name)
        admin.dispose()


class _RollbackSpy:
    """Counts ``rollback()`` calls on the caller's session (must stay 0)."""

    def __init__(self, db: Session):
        self.db = db
        self.calls = 0
        self._orig = db.rollback

    def __enter__(self):
        def _spy(*a, **k):
            self.calls += 1
            return self._orig(*a, **k)

        self.db.rollback = _spy  # type: ignore[method-assign]
        return self

    def __exit__(self, *exc):
        self.db.rollback = self._orig  # type: ignore[method-assign]
        return False


def test_missing_table_read_never_rolls_back_the_caller(pg_without_provenance_table: Engine) -> None:
    """The savepoint is what recovers the transaction — never ``session.rollback()``
    on the caller (which would discard the caller's flushed business rows)."""
    db = _session(pg_without_provenance_table)
    try:
        cust = _seed(db, name=VERIFIED_NAME, meta={"customer_name_authority": "VERIFIED_ECOMMERCE"})
        _pending_sibling(db, cust.tenant_id, "+966544444444")
        with _RollbackSpy(db) as spy:
            assert read_name_authority(cust) is NameAuthority.VERIFIED_ECOMMERCE
        assert spy.calls == 0, "read_name_authority must not roll back the caller's session"
        assert db.in_transaction()
        assert db.query(Customer).filter(Customer.normalized_phone == "+966544444444").count() == 1
        db.commit()
    finally:
        db.close()


def test_missing_table_read_on_create_all_schema_keeps_caller_committable(
    pg_create_all_without_provenance_table: Engine,
) -> None:
    """Same invariant on the schema shape production actually has during the
    startup window (create_all-built, provenance table not yet created)."""
    engine = pg_create_all_without_provenance_table
    assert "customer_name_provenance" not in set(__import__("sqlalchemy").inspect(engine).get_table_names())
    db = _session(engine)
    try:
        cust = _seed(db, name="محمد أحمد", meta={"customer_name_authority": "CUSTOMER_SELF_REPORTED"})
        tenant = db.get(Tenant, cust.tenant_id)
        tenant.name = "T-renamed-in-flight"  # pending UPDATE, unrelated business row
        _pending_sibling(db, cust.tenant_id, "+966555555555")

        with _RollbackSpy(db) as spy:
            assert read_name_authority(cust) is NameAuthority.CUSTOMER_SELF_REPORTED
        assert spy.calls == 0
        assert db.query(Tenant).filter(Tenant.id == tenant.id).count() == 1
        assert db.execute(text("SELECT 1")).scalar() == 1
        db.commit()
    finally:
        db.close()

    check = _session(engine)
    try:
        assert check.get(Tenant, tenant.id).name == "T-renamed-in-flight"
        assert check.query(Customer).filter(Customer.normalized_phone == "+966555555555").count() == 1
    finally:
        check.close()


def test_missing_table_read_falls_back_for_every_metadata_shape(pg_without_provenance_table: Engine) -> None:
    db = _session(pg_without_provenance_table)
    try:
        cases = [
            ({"customer_name_authority": "VERIFIED_ECOMMERCE"}, NameAuthority.VERIFIED_ECOMMERCE),
            ({"customer_name_authority": "CUSTOMER_SELF_REPORTED"}, NameAuthority.CUSTOMER_SELF_REPORTED),
            ({"customer_name_authority": "WHATSAPP_PROFILE"}, NameAuthority.WHATSAPP_PROFILE),
            ({"customer_name_source": "salla_sync"}, NameAuthority.VERIFIED_ECOMMERCE),
            ({}, NameAuthority.WHATSAPP_PROFILE),  # acquisition_channel=whatsapp_inbound
        ]
        for meta, expected in cases:
            cust = _seed(db, name="محمد أحمد", meta=meta)
            assert read_name_authority(cust) is expected, meta
            assert db.execute(text("SELECT 1")).scalar() == 1  # still usable after each failed SELECT
        db.commit()
    finally:
        db.close()

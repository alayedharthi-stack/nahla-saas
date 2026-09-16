"""PostgreSQL proof: migration 0107 is safe whether or not
``customer_name_provenance`` already exists.

Production startup pins ``alembic upgrade 0093`` and materializes new ORM
tables through ``Base.metadata.create_all`` (``backend/main.py``), so by
the time an operator applies ``0107`` the table is usually already there
in its ORM shape. Two states are proven on real PostgreSQL, ephemeral
databases only:

* State A — table absent: ``0106 → 0107`` creates it.
* State B — table present via ``Base.metadata.create_all`` with a live
  row: ``0106 → 0107`` succeeds with no DROP / re-create, the row survives
  byte-for-byte, the resulting schema is identical to State A (columns,
  types, nullability, server defaults, primary key, foreign keys, unique
  constraint, index), ``alembic_version`` advances to ``0107``, and the
  provenance read/write helpers work on the reconciled table.

Plus: a negative control showing the unguarded ``create_table`` really
fails in State B, a partially-drifted State B (missing columns / index)
that is reconciled additively, and the downgrade path.

Runs when a PostgreSQL DSN is available (see
``legacy_migration_drift_postgres_fixtures.connect_engine``); REQUIRED
under ``MIGRATION_0107_PG_REQUIRED=1`` or
``LEGACY_MIG_PG_INTEGRATION_REQUIRED=1``. Never touches Production and
never invokes ``head``.

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
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterator

import pytest
import sqlalchemy as sa
from alembic.migration import MigrationContext
from alembic.operations import Operations
from sqlalchemy import create_engine, inspect, text
from sqlalchemy.engine import Engine
from sqlalchemy.exc import ProgrammingError
from sqlalchemy.orm import Session, sessionmaker
from sqlalchemy.pool import NullPool

_BACKEND = Path(__file__).resolve().parents[1]
_REPO = _BACKEND.parent
_DATABASE = _REPO / "database"
for _entry in (str(_REPO), str(_BACKEND), str(_DATABASE)):
    if _entry not in sys.path:
        sys.path.insert(0, _entry)

from core.customer_name_authority import (  # noqa: E402
    EVIDENCE_EXPLICIT_STATEMENT,
    NameAuthority,
    resolve_canonical_customer_name,
)
from core.customer_name_provenance import (  # noqa: E402
    read_name_authority,
    record_name_decision,
)
from legacy_migration_drift_postgres_fixtures import (  # noqa: E402
    connect_engine,
    create_ephemeral_database,
    downgrade_alembic,
    drop_ephemeral_database,
    run_alembic,
)
from models import Base, Customer, CustomerNameProvenance, Tenant  # noqa: E402

TABLE = "customer_name_provenance"
UNIQUE = "uq_customer_name_provenance_tenant_customer"
INDEX = "ix_customer_name_provenance_tenant_authority"
MIGRATION_SOURCE = _DATABASE / "migrations" / "versions" / "0107_customer_name_provenance.py"

EXPECTED_COLUMNS = (
    "id", "tenant_id", "customer_id",
    "canonical_name", "authority", "source", "evidence_kind", "evidence_ref",
    "merchant_locked", "previous_name", "previous_authority", "canonical_updated_at",
    "profile_hint", "profile_hint_classification",
    "last_decision", "last_attempt_name", "last_attempt_authority", "last_attempt_source",
    "last_attempt_classification", "last_attempt_reason", "last_attempt_at",
    "created_at", "updated_at",
)
NOT_NULL_COLUMNS = ("id", "tenant_id", "customer_id", "authority", "merchant_locked", "created_at", "updated_at")
SERVER_DEFAULTED = ("authority", "merchant_locked", "created_at", "updated_at")
VERIFIED_NAME = "محمد أحمد الحارثي"


# ── infrastructure ─────────────────────────────────────────────────────

def _pg_required() -> bool:
    return (
        (os.getenv("MIGRATION_0107_PG_REQUIRED") or "").strip() == "1"
        or (os.getenv("LEGACY_MIG_PG_INTEGRATION_REQUIRED") or "").strip() == "1"
    )


@pytest.fixture(scope="module")
def admin_engine() -> Iterator[Engine]:
    try:
        engine = connect_engine()
    except pytest.skip.Exception as exc:
        if _pg_required():
            pytest.fail(f"PostgreSQL required for 0107 reconciliation tests: {exc}")
        raise
    if engine.dialect.name != "postgresql":
        pytest.fail("0107 reconciliation tests require PostgreSQL, not SQLite")
    try:
        yield engine
    finally:
        engine.dispose()


def _ephemeral(admin: Engine) -> tuple[str, Engine]:
    db_name, _ = create_ephemeral_database(admin)
    engine = create_engine(
        str(admin.url.set(database=db_name).render_as_string(hide_password=False)),
        poolclass=NullPool,
        pool_pre_ping=True,
    )
    return db_name, engine


def _versions(engine: Engine) -> set[str]:
    with engine.connect() as conn:
        return {str(r[0]) for r in conn.execute(text("SELECT version_num FROM alembic_version"))}


def _table_names(engine: Engine) -> set[str]:
    return set(inspect(engine).get_table_names())


def _shape(engine: Engine) -> dict[str, Any]:
    """Everything about the table's physical shape, inspector-derived."""
    insp = inspect(engine)
    return {
        "columns": [
            (c["name"], str(c["type"]), bool(c["nullable"]), c.get("default"))
            for c in insp.get_columns(TABLE)
        ],
        "pk": list(insp.get_pk_constraint(TABLE)["constrained_columns"]),
        "fks": sorted(
            (fk["name"], tuple(fk["constrained_columns"]), fk["referred_table"], tuple(fk["referred_columns"]))
            for fk in insp.get_foreign_keys(TABLE)
        ),
        "uniques": sorted((u["name"], tuple(u["column_names"])) for u in insp.get_unique_constraints(TABLE)),
        "indexes": sorted(
            (ix["name"], tuple(ix["column_names"]), bool(ix["unique"])) for ix in insp.get_indexes(TABLE)
        ),
    }


def _assert_required_shape(shape: dict[str, Any]) -> None:
    """The contract every post-0107 database must satisfy."""
    by_name = {name: (typ, nullable, default) for name, typ, nullable, default in shape["columns"]}
    # Column *set*; physical order is compared by the State A/B parity test
    # (a drifted table gets its re-added columns appended, which is fine).
    assert set(by_name) == set(EXPECTED_COLUMNS), sorted(by_name)
    for name in NOT_NULL_COLUMNS:
        assert by_name[name][1] is False, f"{name} must be NOT NULL"
    for name in EXPECTED_COLUMNS:
        if name not in NOT_NULL_COLUMNS:
            assert by_name[name][1] is True, f"{name} must be nullable"
    assert "UNKNOWN" in str(by_name["authority"][2])
    assert str(by_name["merchant_locked"][2]).lower() == "false"
    assert "now()" in str(by_name["created_at"][2])
    assert "now()" in str(by_name["updated_at"][2])
    assert by_name["evidence_ref"][0] == "JSONB"
    assert by_name["canonical_updated_at"][0] == "TIMESTAMP"
    assert shape["pk"] == ["id"]
    fk_targets = {(cols, ref_table, ref_cols) for _name, cols, ref_table, ref_cols in shape["fks"]}
    assert (("tenant_id",), "tenants", ("id",)) in fk_targets
    assert (("customer_id",), "customers", ("id",)) in fk_targets
    assert (UNIQUE, ("tenant_id", "customer_id")) in shape["uniques"]
    assert (INDEX, ("tenant_id", "authority"), False) in shape["indexes"]


def _session(engine: Engine) -> Session:
    return sessionmaker(bind=engine, autocommit=False, autoflush=True, expire_on_commit=False)()


def _seed_customer(db: Session, *, name: str | None = None, meta: dict | None = None) -> Customer:
    tenant = Tenant(name=f"t-0107-{os.urandom(3).hex()}", is_active=True)
    db.add(tenant)
    db.flush()
    cust = Customer(
        tenant_id=tenant.id, phone="+966500000000", normalized_phone="+966500000000",
        name=name, extra_metadata=dict(meta or {}), acquisition_channel="whatsapp_inbound",
    )
    db.add(cust)
    db.flush()
    return cust


def _seed_state_b_row(engine: Engine) -> tuple[int, dict[str, Any]]:
    """A real provenance row written through the ORM onto the create_all table."""
    db = _session(engine)
    try:
        cust = _seed_customer(db, name=VERIFIED_NAME, meta={"customer_name_authority": "VERIFIED_ECOMMERCE"})
        row = CustomerNameProvenance(
            tenant_id=cust.tenant_id,
            customer_id=cust.id,
            canonical_name=VERIFIED_NAME,
            authority="VERIFIED_ECOMMERCE",
            source="salla_sync",
            evidence_kind=None,
            evidence_ref={"order_id": "S-1001", "note": "اختبار"},
            merchant_locked=False,
            previous_name="الحمد لله",
            previous_authority="WHATSAPP_PROFILE",
            canonical_updated_at=datetime(2026, 9, 16, 12, 0, tzinfo=timezone.utc),
            profile_hint="الحمد لله",
            profile_hint_classification="NOT_PERSON_NAME",
            last_decision="applied",
            last_attempt_name=VERIFIED_NAME,
            last_attempt_authority="VERIFIED_ECOMMERCE",
            last_attempt_source="salla_sync",
            last_attempt_reason="higher_authority",
            last_attempt_at=datetime(2026, 9, 16, 12, 0, tzinfo=timezone.utc),
        )
        db.add(row)
        db.commit()
        row_id = int(row.id)
    finally:
        db.close()
    return row_id, _raw_row(engine, row_id)


def _raw_row(engine: Engine, row_id: int) -> dict[str, Any]:
    with engine.connect() as conn:
        return dict(conn.execute(text(f"SELECT * FROM {TABLE} WHERE id = :id"), {"id": row_id}).mappings().one())


def _state_b(admin: Engine) -> tuple[str, Engine]:
    """0106 schema + ``Base.metadata.create_all`` — the production shape."""
    db_name, engine = _ephemeral(admin)
    run_alembic(engine, "0106")
    assert TABLE not in _table_names(engine)
    Base.metadata.create_all(engine)
    assert TABLE in _table_names(engine)
    assert _versions(engine) == {"0106"}
    return db_name, engine


# ── State A: table absent ──────────────────────────────────────────────

def test_migration_source_contract() -> None:
    source = MIGRATION_SOURCE.read_text(encoding="utf-8")
    assert 'revision: str = "0107"' in source
    assert 'down_revision: Union[str, None] = "0106"' in source
    assert "has_table" in source
    upgrade_src = source.split("def upgrade", 1)[1].split("def downgrade", 1)[0]
    assert "upgrade head" not in upgrade_src
    assert "drop_table" not in upgrade_src
    assert "drop_column" not in upgrade_src
    assert "DELETE FROM" not in source.upper().replace(" ", "")


def test_state_a_clean_0106_to_0107(admin_engine: Engine) -> None:
    db_name, engine = _ephemeral(admin_engine)
    try:
        run_alembic(engine, "0106")
        assert _versions(engine) == {"0106"}
        assert TABLE not in _table_names(engine)
        run_alembic(engine, "0107")
        assert _versions(engine) == {"0107"}
        assert TABLE in _table_names(engine)
        _assert_required_shape(_shape(engine))
    finally:
        engine.dispose()
        drop_ephemeral_database(admin_engine, db_name)


def test_state_a_downgrade_0107_to_0106_drops_table(admin_engine: Engine) -> None:
    db_name, engine = _ephemeral(admin_engine)
    try:
        run_alembic(engine, "0107")
        downgrade_alembic(engine, "0106")
        assert _versions(engine) == {"0106"}
        assert TABLE not in _table_names(engine)
        run_alembic(engine, "0107")  # and forward again
        assert _versions(engine) == {"0107"}
        _assert_required_shape(_shape(engine))
    finally:
        engine.dispose()
        drop_ephemeral_database(admin_engine, db_name)


# ── Negative control: the unguarded create_table really breaks State B ──

def test_control_unguarded_create_table_fails_when_create_all_already_ran(admin_engine: Engine) -> None:
    """The pre-reconciliation 0107 body (bare ``op.create_table``) raises
    ``DuplicateTable`` on the create_all shape — the hazard is real."""
    import importlib.util  # noqa: PLC0415

    spec = importlib.util.spec_from_file_location("migration_0107_under_test", MIGRATION_SOURCE)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)

    db_name, engine = _state_b(admin_engine)
    try:
        with engine.connect() as conn:
            ctx = MigrationContext.configure(conn)
            with Operations.context(ctx):
                with pytest.raises(ProgrammingError) as raised:
                    module._create_fresh()
            assert "already exists" in str(raised.value)
            conn.rollback()
        assert _versions(engine) == {"0106"}
        assert TABLE in _table_names(engine)
    finally:
        engine.dispose()
        drop_ephemeral_database(admin_engine, db_name)


# ── State B: table exists via Base.metadata.create_all ─────────────────

def test_state_b_create_all_then_0107_preserves_row_and_reaches_parity(admin_engine: Engine) -> None:
    a_name, a_engine = _ephemeral(admin_engine)
    b_name, b_engine = _state_b(admin_engine)
    try:
        run_alembic(a_engine, "0107")
        state_a = _shape(a_engine)

        # create_all shape: no server defaults (ORM defaults are Python-side).
        before = _shape(b_engine)
        defaults_before = {name: d for name, _t, _n, d in before["columns"] if name in SERVER_DEFAULTED}
        assert all(d is None for d in defaults_before.values()), defaults_before

        row_id, row_before = _seed_state_b_row(b_engine)
        with b_engine.connect() as conn:
            oid_before = conn.execute(text(f"SELECT '{TABLE}'::regclass::oid")).scalar()

        run_alembic(b_engine, "0107")

        assert _versions(b_engine) == {"0107"}
        with b_engine.connect() as conn:
            oid_after = conn.execute(text(f"SELECT '{TABLE}'::regclass::oid")).scalar()
            count = conn.execute(text(f"SELECT count(*) FROM {TABLE}")).scalar()
        assert oid_after == oid_before, "table must not be dropped and re-created"
        assert count == 1
        assert _raw_row(b_engine, row_id) == row_before, "existing row must survive unchanged"

        state_b = _shape(b_engine)
        _assert_required_shape(state_b)
        assert state_b == state_a, "State B must end in exactly the State A schema"
    finally:
        a_engine.dispose()
        b_engine.dispose()
        drop_ephemeral_database(admin_engine, a_name)
        drop_ephemeral_database(admin_engine, b_name)


def test_state_b_reconciled_table_serves_the_provenance_helpers(admin_engine: Engine) -> None:
    db_name, engine = _state_b(admin_engine)
    try:
        row_id, _ = _seed_state_b_row(engine)
        run_alembic(engine, "0107")

        db = _session(engine)
        try:
            # read_name_authority: the durable row (not the JSONB mirror) answers.
            existing = db.query(CustomerNameProvenance).filter(CustomerNameProvenance.id == row_id).one()
            cust = db.get(Customer, existing.customer_id)
            cust.extra_metadata = {}  # mirror emptied → only the row can say VERIFIED
            db.flush()
            assert read_name_authority(cust) is NameAuthority.VERIFIED_ECOMMERCE

            # record_name_decision: a fresh customer gets a row via the write path.
            newcomer = _seed_customer(db, name=None)
            decision = resolve_canonical_customer_name(
                incoming_name="سارة العتيبي",
                incoming_authority=NameAuthority.CUSTOMER_SELF_REPORTED,
                current_name=None,
                current_authority=NameAuthority.UNKNOWN,
                evidence_kind=EVIDENCE_EXPLICIT_STATEMENT,
            )
            assert decision.changed_canonical
            assert record_name_decision(
                newcomer, decision, source="whatsapp_self_intro",
                evidence_ref={"conversation_id": 7, "message_id": 9},
            ) is True
            newcomer.name = "سارة العتيبي"
            db.commit()

            db.expire_all()
            written = db.query(CustomerNameProvenance).filter(
                CustomerNameProvenance.customer_id == newcomer.id,
            ).one()
            assert written.canonical_name == "سارة العتيبي"
            assert written.authority == "CUSTOMER_SELF_REPORTED"
            assert written.evidence_kind == EVIDENCE_EXPLICIT_STATEMENT
            assert written.evidence_ref == {"conversation_id": 7, "message_id": 9}
            assert written.merchant_locked is False
            assert written.created_at is not None and written.updated_at is not None
            assert read_name_authority(db.get(Customer, newcomer.id)) is NameAuthority.CUSTOMER_SELF_REPORTED

            # Blocked attempt: canonical half untouched, attempt half recorded.
            blocked = resolve_canonical_customer_name(
                incoming_name="الحمد لله",
                incoming_authority=NameAuthority.WHATSAPP_PROFILE,
                current_name="سارة العتيبي",
                current_authority=NameAuthority.CUSTOMER_SELF_REPORTED,
            )
            assert not blocked.changed_canonical
            assert record_name_decision(newcomer, blocked, source="whatsapp_profile") is True
            db.commit()
            db.expire_all()
            again = db.query(CustomerNameProvenance).filter(
                CustomerNameProvenance.customer_id == newcomer.id,
            ).one()
            assert again.canonical_name == "سارة العتيبي"
            assert again.authority == "CUSTOMER_SELF_REPORTED"
            assert again.last_attempt_name == "الحمد لله"
            assert again.last_attempt_authority == "WHATSAPP_PROFILE"

            # Unique constraint enforced on the reconciled table.
            db.add(CustomerNameProvenance(tenant_id=newcomer.tenant_id, customer_id=newcomer.id))
            with pytest.raises(sa.exc.IntegrityError):
                db.flush()
            db.rollback()
        finally:
            db.close()

        # Server defaults now apply to raw SQL inserts too (no ORM defaults involved).
        with engine.begin() as conn:
            tid = conn.execute(text("INSERT INTO tenants (name, is_active) VALUES ('t-raw', true) RETURNING id")).scalar()
            cid = conn.execute(
                text("INSERT INTO customers (tenant_id, phone, normalized_phone) VALUES (:t, '+966511111111', '+966511111111') RETURNING id"),
                {"t": tid},
            ).scalar()
            conn.execute(text(f"INSERT INTO {TABLE} (tenant_id, customer_id) VALUES (:t, :c)"), {"t": tid, "c": cid})
            raw = conn.execute(
                text(f"SELECT authority, merchant_locked, created_at, updated_at FROM {TABLE} WHERE customer_id = :c"),
                {"c": cid},
            ).mappings().one()
        assert raw["authority"] == "UNKNOWN"
        assert raw["merchant_locked"] is False
        assert raw["created_at"] is not None and raw["updated_at"] is not None
    finally:
        engine.dispose()
        drop_ephemeral_database(admin_engine, db_name)


def test_state_b_partial_drift_is_reconciled_additively(admin_engine: Engine) -> None:
    """A create_all table that has since lost columns / index / unique
    constraint (older ORM shape) is completed, not rebuilt; rows keep
    their values and NULLs in newly NOT NULL columns get the defaults."""
    db_name, engine = _state_b(admin_engine)
    try:
        row_id, row_before = _seed_state_b_row(engine)
        with engine.begin() as conn:
            conn.execute(text(f"ALTER TABLE {TABLE} DROP COLUMN last_attempt_reason"))
            conn.execute(text(f"ALTER TABLE {TABLE} DROP COLUMN merchant_locked"))
            conn.execute(text(f"ALTER TABLE {TABLE} DROP COLUMN updated_at"))
            conn.execute(text(f"ALTER TABLE {TABLE} ALTER COLUMN authority DROP NOT NULL"))
            conn.execute(text(f"ALTER TABLE {TABLE} DROP CONSTRAINT {UNIQUE}"))
            conn.execute(text(f"DROP INDEX {INDEX}"))
            conn.execute(text(f"ALTER TABLE {TABLE} DROP CONSTRAINT {TABLE}_customer_id_fkey"))
            conn.execute(text(f"UPDATE {TABLE} SET authority = NULL WHERE id = :id"), {"id": row_id})
            oid_before = conn.execute(text(f"SELECT '{TABLE}'::regclass::oid")).scalar()

        run_alembic(engine, "0107")

        assert _versions(engine) == {"0107"}
        shape = _shape(engine)
        _assert_required_shape(shape)
        with engine.connect() as conn:
            assert conn.execute(text(f"SELECT '{TABLE}'::regclass::oid")).scalar() == oid_before
        row_after = _raw_row(engine, row_id)
        # Untouched columns keep their exact values …
        for key, value in row_before.items():
            if key in {"last_attempt_reason", "merchant_locked", "updated_at", "authority"}:
                continue
            assert row_after[key] == value, key
        # … and the reconciled ones carry the migration's defaults, never NULL.
        assert row_after["authority"] == "UNKNOWN"
        assert row_after["merchant_locked"] is False
        assert row_after["updated_at"] is not None
        assert row_after["last_attempt_reason"] is None
    finally:
        engine.dispose()
        drop_ephemeral_database(admin_engine, db_name)


def test_already_at_0107_is_a_noop(admin_engine: Engine) -> None:
    db_name, engine = _ephemeral(admin_engine)
    try:
        run_alembic(engine, "0107")
        shape = _shape(engine)
        run_alembic(engine, "0107")
        assert _versions(engine) == {"0107"}
        assert _shape(engine) == shape
    finally:
        engine.dispose()
        drop_ephemeral_database(admin_engine, db_name)

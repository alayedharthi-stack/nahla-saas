"""The catalog channel-retirements migration against REAL PostgreSQL.

The migration adopts an existing ``catalog_channel_retirements`` table (the
startup ``create_all`` builds it on any deploy of this branch) only when its
definition matches what the migration itself creates; any other table of that
name — missing or partial uniqueness or index, a foreign key elsewhere or
missing, a missing or extra column, an extra unique or check constraint, a
different type (including CHAR for VARCHAR) or nullability, or an unrelated
table — makes the upgrade raise,
and the database keeps its revision and schema.

Each test gets a throw-away database cloned from a template migrated to the
parent revision (0112) on the admin DSN in ``NAHLA_RELIABILITY_PG_ADMIN_DSN``;
everything is dropped at teardown. Without the variable the module is skipped
(reported as skipped, never as passed); with ``NAHLA_RELIABILITY_REQUIRE_PG=1``
and no variable it fails instead. Inventoried in the required PostgreSQL proofs.
"""
from __future__ import annotations

import ast
import os
import secrets
import subprocess
import sys
from pathlib import Path

import pytest
from sqlalchemy import create_engine, inspect, text
from sqlalchemy.engine.url import make_url
from sqlalchemy.pool import NullPool

_REPO = Path(__file__).resolve().parents[2]
for entry in (str(_REPO), str(_REPO / "backend"), str(_REPO / "database")):
    if entry not in sys.path:
        sys.path.insert(0, entry)

ADMIN_URL = (os.environ.get("NAHLA_RELIABILITY_PG_ADMIN_DSN") or "").strip()
if not ADMIN_URL and os.environ.get("NAHLA_RELIABILITY_REQUIRE_PG") == "1":
    raise RuntimeError("NAHLA_RELIABILITY_REQUIRE_PG=1 but NAHLA_RELIABILITY_PG_ADMIN_DSN is not set")
pytestmark = pytest.mark.skipif(not ADMIN_URL, reason="NAHLA_RELIABILITY_PG_ADMIN_DSN not set (no real PostgreSQL)")

TABLE = "catalog_channel_retirements"
_SCRIPT = next((_REPO / "database" / "migrations" / "versions").glob(f"*_{TABLE}.py"))


def _declared(name: str) -> str:
    for node in ast.parse(_SCRIPT.read_text(encoding="utf-8")).body:
        if isinstance(node, ast.Assign) and any(getattr(t, "id", None) == name for t in node.targets):
            return node.value.value
    raise AssertionError(f"{name} not declared in {_SCRIPT.name}")


REVISION = _declared("revision")
PARENT = _declared("down_revision")


def _admin(*statements: str) -> None:
    eng = create_engine(ADMIN_URL, poolclass=NullPool, isolation_level="AUTOCOMMIT", future=True)
    try:
        with eng.connect() as conn:
            for stmt in statements:
                conn.execute(text(stmt))
    finally:
        eng.dispose()


def _url(db: str) -> str:
    return make_url(ADMIN_URL).set(drivername="postgresql+psycopg2", database=db).render_as_string(hide_password=False)


def _alembic(db: str, *args: str) -> subprocess.CompletedProcess:
    env = {**os.environ, "DATABASE_URL": _url(db)}
    return subprocess.run([sys.executable, "-m", "alembic", *args], cwd=str(_REPO / "database"), env=env,
                          capture_output=True, text=True, timeout=600)


def _exec(db: str, *statements: str) -> None:
    eng = create_engine(_url(db), poolclass=NullPool, future=True)
    try:
        with eng.begin() as conn:
            for stmt in statements:
                conn.execute(text(stmt))
    finally:
        eng.dispose()


def _versions(db: str) -> set[str]:
    eng = create_engine(_url(db), poolclass=NullPool, future=True)
    try:
        with eng.connect() as conn:
            return set(conn.execute(text("SELECT version_num FROM alembic_version")).scalars())
    finally:
        eng.dispose()


def _fingerprint(db: str) -> tuple:
    eng = create_engine(_url(db), poolclass=NullPool, future=True)
    try:
        with eng.connect() as conn:
            cols = conn.execute(text(
                "SELECT table_name, column_name, data_type, character_maximum_length, is_nullable, column_default "
                "FROM information_schema.columns WHERE table_schema = 'public' ORDER BY 1, 2")).fetchall()
            cons = conn.execute(text(
                "SELECT conrelid::regclass::text, conname, pg_get_constraintdef(oid) FROM pg_constraint "
                "WHERE connamespace = 'public'::regnamespace ORDER BY 1, 2")).fetchall()
            idx = conn.execute(text(
                "SELECT indexname, indexdef FROM pg_indexes WHERE schemaname = 'public' ORDER BY 1")).fetchall()
            return tuple(cols), tuple(cons), tuple(idx)
    finally:
        eng.dispose()


def _create_with_model(db: str, *, catalog_id_not_null: bool = False) -> None:
    from database.models import CatalogChannelRetirement  # the startup create_all definition

    eng = create_engine(_url(db), poolclass=NullPool, future=True)
    try:
        CatalogChannelRetirement.__table__.create(eng)
    finally:
        eng.dispose()
    if catalog_id_not_null:  # the first model of this table (15131b50) declared catalog_id NOT NULL
        _exec(db, f"ALTER TABLE {TABLE} ALTER COLUMN catalog_id SET NOT NULL")


@pytest.fixture(scope="module")
def template():
    name = f"ccr_tmpl_{secrets.token_hex(4)}"
    _admin(f"CREATE DATABASE {name}")
    try:
        for target in ("0093", PARENT):
            proc = _alembic(name, "upgrade", target)
            assert proc.returncode == 0, proc.stderr[-3000:]
        assert _versions(name) == {PARENT}
        yield name
    finally:
        _admin(f"DROP DATABASE IF EXISTS {name} WITH (FORCE)")


@pytest.fixture()
def db(template):
    name = f"ccr_{secrets.token_hex(4)}"
    _admin(f"CREATE DATABASE {name} TEMPLATE {template}")
    try:
        yield name
    finally:
        _admin(f"DROP DATABASE IF EXISTS {name} WITH (FORCE)")


def _table_shape(db: str) -> tuple:
    eng = create_engine(_url(db), poolclass=NullPool, future=True)
    try:
        insp = inspect(eng)
        cols = sorted((c["name"], str(c["type"]), c["nullable"]) for c in insp.get_columns(TABLE))
        uniques = sorted(tuple(u["column_names"]) for u in insp.get_unique_constraints(TABLE))
        indexes = sorted(tuple(i["column_names"]) for i in insp.get_indexes(TABLE))
        fks = sorted((tuple(f["constrained_columns"]), f["referred_table"]) for f in insp.get_foreign_keys(TABLE))
        return cols, uniques, indexes, fks
    finally:
        eng.dispose()


def test_fresh_database_gets_the_table_and_the_revision(db):
    proc = _alembic(db, "upgrade", REVISION)
    assert proc.returncode == 0, proc.stderr[-3000:]
    assert _versions(db) == {REVISION}
    cols, uniques, indexes, fks = _table_shape(db)
    assert ("catalog_id", "VARCHAR(64)", True) in cols
    assert ("tenant_id", "catalog_id", "retailer_id") in uniques
    assert ("tenant_id", "status") in indexes
    assert ((("tenant_id",), "tenants")) in fks


def test_create_all_table_is_adopted_unchanged(db):
    _create_with_model(db)
    before = _table_shape(db)
    proc = _alembic(db, "upgrade", REVISION)
    assert proc.returncode == 0, proc.stderr[-3000:]
    assert _versions(db) == {REVISION}
    assert _table_shape(db) == before


def test_first_model_table_is_adopted_and_catalog_id_relaxed(db):
    _create_with_model(db, catalog_id_not_null=True)
    proc = _alembic(db, "upgrade", REVISION)
    assert proc.returncode == 0, proc.stderr[-3000:]
    assert _versions(db) == {REVISION}
    assert ("catalog_id", "VARCHAR(64)", True) in _table_shape(db)[0]


_INCOMPATIBLE = {
    "missing_unique": [f"ALTER TABLE {TABLE} DROP CONSTRAINT uq_catalog_channel_retirements_tenant_catalog_retailer"],
    "missing_status_index": ["DROP INDEX ix_catalog_channel_retirements_tenant_status"],
    "missing_tenant_fk": [f"ALTER TABLE {TABLE} DROP CONSTRAINT catalog_channel_retirements_tenant_id_fkey"],
    "missing_column": [f"ALTER TABLE {TABLE} DROP COLUMN meta_item_id"],
    "extra_column": [f"ALTER TABLE {TABLE} ADD COLUMN note TEXT"],
    "different_type": [f"ALTER TABLE {TABLE} ALTER COLUMN retailer_id TYPE VARCHAR(100)"],
    "different_nullability": [f"ALTER TABLE {TABLE} ALTER COLUMN reason DROP NOT NULL"],
    "unrelated_table": [f"DROP TABLE {TABLE}", f"CREATE TABLE {TABLE} (x INTEGER)"],
    "partial_unique_instead_of_constraint": [
        f"ALTER TABLE {TABLE} DROP CONSTRAINT uq_catalog_channel_retirements_tenant_catalog_retailer",
        f"CREATE UNIQUE INDEX uq_partial ON {TABLE} (tenant_id, catalog_id, retailer_id) WHERE status = 'pending'"],
    "char_instead_of_varchar": [f"ALTER TABLE {TABLE} ALTER COLUMN catalog_id TYPE CHAR(64)"],
    "extra_check_constraint": [f"ALTER TABLE {TABLE} ADD CONSTRAINT ck_attempts CHECK (attempts >= 0)"],
    "extra_unique_constraint": [f"ALTER TABLE {TABLE} ADD CONSTRAINT uq_retailer UNIQUE (retailer_id)"],
    "partial_status_index": [
        "DROP INDEX ix_catalog_channel_retirements_tenant_status",
        f"CREATE INDEX ix_catalog_channel_retirements_tenant_status ON {TABLE} (tenant_id, status) WHERE status = 'pending'"],
    "tenant_fk_to_another_schema": [
        "CREATE SCHEMA other_schema", "CREATE TABLE other_schema.tenants (id INTEGER PRIMARY KEY)",
        f"ALTER TABLE {TABLE} DROP CONSTRAINT catalog_channel_retirements_tenant_id_fkey",
        f"ALTER TABLE {TABLE} ADD FOREIGN KEY (tenant_id) REFERENCES other_schema.tenants (id) ON DELETE CASCADE"],
}


@pytest.mark.parametrize("case", sorted(_INCOMPATIBLE))
def test_incompatible_existing_table_fails_closed_and_changes_nothing(db, case):
    _create_with_model(db)
    _exec(db, *_INCOMPATIBLE[case])
    before = _fingerprint(db)
    proc = _alembic(db, "upgrade", REVISION)
    assert proc.returncode != 0
    assert "already exists with a different definition" in proc.stderr
    assert _versions(db) == {PARENT}
    assert _fingerprint(db) == before

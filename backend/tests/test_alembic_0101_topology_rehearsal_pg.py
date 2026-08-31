"""Ephemeral PostgreSQL rehearsal: integration path to 0101 without touching Production.

Proves:
- Repository heads stay {0092, 0101}; 0092 is not lost and is not selected.
- Fresh bootstrap 0093 then upgrade 0101 applies 0094..0101 and creates the index.
- A production-like DB at 0100 then upgrade 0101 applies only 0101.
- After 0101, upgrade 0092 can still attach as a second alembic_version row.
- Merge revision of 0092+0101 is not required and must not exist.
- Downgrade 0101 → 0100 drops the index.

Never uses Production DATABASE_URL. Never invokes ``head``.
Production read-only current is ``0099``; this PR applies ``0100`` then ``0101``.
"""
from __future__ import annotations

import os
import sys
from contextlib import contextmanager
from pathlib import Path

import pytest
from alembic.script import ScriptDirectory
from sqlalchemy import create_engine, text
from sqlalchemy.engine import Engine
from sqlalchemy.pool import NullPool

_REPO = Path(__file__).resolve().parents[2]
_DATABASE = _REPO / "database"
_BACKEND = _REPO / "backend"
for _entry in (str(_REPO), str(_BACKEND), str(_DATABASE)):
    if _entry not in sys.path:
        sys.path.insert(0, _entry)

from catalog_meta_item_uniqueness import (  # noqa: E402
    UQ_PRODUCTS_ACTIVE_TENANT_META_ITEM,
)
from scripts.operators.bootstrap_migration_contract import (  # noqa: E402
    FORBIDDEN_BOOTSTRAP_LITERALS,
    INTEGRATION_BOOTSTRAP_TARGET,
    REPOSITORY_ALEMBIC_HEADS,
)
from tests.legacy_migration_drift_postgres_fixtures import (  # noqa: E402
    connect_engine,
    create_ephemeral_database,
    downgrade_alembic,
    drop_ephemeral_database,
    run_alembic,
)

_INDEX = UQ_PRODUCTS_ACTIVE_TENANT_META_ITEM
_BOOTSTRAP = INTEGRATION_BOOTSTRAP_TARGET
_PROD_CURRENT = "0099"
_PROD_LIKE = "0100"
_TARGET = "0101"
_VALIDATE_HEAD = "0092"
_EXPECTED_FROM_BOOTSTRAP = ("0094", "0095", "0096", "0097", "0098", "0099", "0100", "0101")
_EXPECTED_FROM_PROD = ("0100", "0101")


@contextmanager
def _database_cwd():
    prev = os.getcwd()
    os.chdir(_DATABASE)
    try:
        yield
    finally:
        os.chdir(prev)


def _script() -> ScriptDirectory:
    return ScriptDirectory("migrations")


def _current_revisions(engine: Engine) -> set[str]:
    with engine.connect() as conn:
        rows = conn.execute(text("SELECT version_num FROM alembic_version ORDER BY 1")).fetchall()
    return {str(row[0]) for row in rows}


def _index_predicate(engine: Engine) -> dict[str, str | bool | None]:
    with engine.connect() as conn:
        row = conn.execute(
            text(
                """
                SELECT
                    i.relname AS index_name,
                    ix.indisunique AS is_unique,
                    pg_get_expr(ix.indpred, ix.indrelid) AS predicate
                FROM pg_index ix
                JOIN pg_class i ON i.oid = ix.indexrelid
                JOIN pg_class t ON t.oid = ix.indrelid
                JOIN pg_namespace n ON n.oid = t.relnamespace
                WHERE t.relname = 'products'
                  AND i.relname = :name
                  AND n.nspname = 'public'
                """
            ),
            {"name": _INDEX},
        ).mappings().first()
    if row is None:
        return {"index_name": None, "is_unique": False, "predicate": None}
    return {
        "index_name": str(row["index_name"]),
        "is_unique": bool(row["is_unique"]),
        "predicate": row["predicate"],
    }


def _upgrade_revisions(script: ScriptDirectory, destination: str, source: str) -> tuple[str, ...]:
    down = tuple(rev.revision for rev in script.iterate_revisions(destination, source))
    return tuple(reversed(down))


def _ephemeral_engine(admin: Engine) -> tuple[str, Engine]:
    db_name, _ = create_ephemeral_database(admin)
    engine = create_engine(
        str(admin.url.set(database=db_name).render_as_string(hide_password=False)),
        poolclass=NullPool,
        pool_pre_ping=True,
    )
    return db_name, engine


@pytest.fixture(scope="module")
def admin_engine() -> Engine:
    try:
        engine = connect_engine()
    except Exception as exc:  # noqa: BLE001
        message = f"PostgreSQL unavailable for 0101 topology rehearsal: {exc}"
        if (os.getenv("LEGACY_MIG_PG_INTEGRATION_REQUIRED") or "").strip() == "1":
            pytest.fail(message)
        pytest.skip(message)
    return engine


def test_repository_heads_stay_parallel_not_merged() -> None:
    with _database_cwd():
        script = _script()
        heads = set(script.get_heads())
        print("alembic_heads=" + ",".join(sorted(heads)))
        assert heads == REPOSITORY_ALEMBIC_HEADS == {_VALIDATE_HEAD, _TARGET}
        assert "head" in FORBIDDEN_BOOTSTRAP_LITERALS
        assert _BOOTSTRAP == "0093"
        merge_revs = [
            rev.revision
            for rev in script.walk_revisions()
            if isinstance(rev.down_revision, tuple)
            and set(rev.down_revision) >= {_VALIDATE_HEAD, _TARGET}
        ]
        assert merge_revs == [], merge_revs
        from_bootstrap = _upgrade_revisions(script, _TARGET, _BOOTSTRAP)
        from_prod = _upgrade_revisions(script, _TARGET, _PROD_CURRENT)
        from_prod_like = _upgrade_revisions(script, _TARGET, _PROD_LIKE)
        print("path_0093_to_0101=" + ",".join(from_bootstrap))
        print("path_0099_to_0101=" + ",".join(from_prod))
        print("path_0100_to_0101=" + ",".join(from_prod_like))
        assert from_bootstrap == _EXPECTED_FROM_BOOTSTRAP
        assert from_prod == _EXPECTED_FROM_PROD
        assert from_prod_like == (_TARGET,)
        assert _VALIDATE_HEAD not in from_bootstrap
        assert "0088" not in from_bootstrap
        assert _VALIDATE_HEAD not in from_prod


def test_fresh_bootstrap_then_0101_and_validate_branch_still_attachable(admin_engine: Engine) -> None:
    db_name, engine = _ephemeral_engine(admin_engine)
    try:
        print("path=fresh_bootstrap_0093_then_0101_then_attach_0092")
        run_alembic(engine, _BOOTSTRAP)
        current = _current_revisions(engine)
        print("alembic_current_after_0093=" + ",".join(sorted(current)))
        assert current == {_BOOTSTRAP}
        assert _index_predicate(engine)["index_name"] is None

        run_alembic(engine, _TARGET)
        current = _current_revisions(engine)
        print("alembic_current_after_0101=" + ",".join(sorted(current)))
        assert current == {_TARGET}
        meta = _index_predicate(engine)
        print(f"index={meta['index_name']} unique={meta['is_unique']} predicate={meta['predicate']}")
        assert meta["index_name"] == _INDEX
        assert meta["is_unique"] is True
        predicate = str(meta["predicate"] or "")
        compact = " ".join(predicate.lower().split())
        for token in ("catalog_status", "active", "meta_item_id", "btrim"):
            assert token in compact

        run_alembic(engine, _VALIDATE_HEAD)
        current = _current_revisions(engine)
        print("alembic_current_after_0092_attach=" + ",".join(sorted(current)))
        assert current == {_VALIDATE_HEAD, _TARGET}
        assert _index_predicate(engine)["index_name"] == _INDEX
    finally:
        engine.dispose()
        drop_ephemeral_database(admin_engine, db_name)


def test_production_0099_then_0101_and_downgrade(admin_engine: Engine) -> None:
    db_name, engine = _ephemeral_engine(admin_engine)
    try:
        print("path=production_current_0099_then_0101_then_downgrade_0100")
        run_alembic(engine, _PROD_CURRENT)
        current = _current_revisions(engine)
        print("alembic_current_after_0099=" + ",".join(sorted(current)))
        assert current == {_PROD_CURRENT}
        assert _index_predicate(engine)["index_name"] is None

        run_alembic(engine, _TARGET)
        current = _current_revisions(engine)
        print("alembic_current_after_0101=" + ",".join(sorted(current)))
        assert current == {_TARGET}
        meta = _index_predicate(engine)
        print(f"index={meta['index_name']} unique={meta['is_unique']} predicate={meta['predicate']}")
        assert meta["index_name"] == _INDEX
        assert meta["is_unique"] is True
        assert _VALIDATE_HEAD not in current

        downgrade_alembic(engine, _PROD_LIKE)
        current = _current_revisions(engine)
        print("alembic_current_after_downgrade=" + ",".join(sorted(current)))
        assert current == {_PROD_LIKE}
        assert _index_predicate(engine)["index_name"] is None
    finally:
        engine.dispose()
        drop_ephemeral_database(admin_engine, db_name)

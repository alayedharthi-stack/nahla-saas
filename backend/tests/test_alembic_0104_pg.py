"""Ephemeral PostgreSQL proof for Alembic 0104 unique lifecycle template index.

Proves:
- clean upgrade 0103 -> 0104
- duplicate precheck refuses safely and deletes no rows
- unique index rejects a second active visible NULL-step template
- hidden / inactive duplicates remain allowed

Never uses Production. Never invokes ``alembic upgrade head``.
"""
from __future__ import annotations

import os
import sys
from pathlib import Path

import pytest
from sqlalchemy import create_engine, text
from sqlalchemy.engine import Engine
from sqlalchemy.exc import IntegrityError
from sqlalchemy.pool import NullPool

_REPO = Path(__file__).resolve().parents[2]
_DATABASE = _REPO / "database"
_BACKEND = _REPO / "backend"
for _entry in (str(_REPO), str(_BACKEND), str(_DATABASE)):
    if _entry not in sys.path:
        sys.path.insert(0, _entry)

from tests.legacy_migration_drift_postgres_fixtures import (  # noqa: E402
    connect_engine,
    create_ephemeral_database,
    drop_ephemeral_database,
    run_alembic,
)

_PARENT = "0103"
_TARGET = "0104"
_INDEX = "uq_active_lifecycle_template_null_step"
_ERROR_DUPLICATES = "0104 refused: duplicate active lifecycle templates"


def _current_revisions(engine: Engine) -> set[str]:
    with engine.connect() as conn:
        rows = conn.execute(text("SELECT version_num FROM alembic_version ORDER BY 1")).fetchall()
    return {str(row[0]) for row in rows}


def _index_exists(engine: Engine) -> bool:
    with engine.connect() as conn:
        row = conn.execute(
            text(
                """
                SELECT 1
                FROM pg_indexes
                WHERE tablename = 'whatsapp_templates'
                  AND indexname = :name
                """
            ),
            {"name": _INDEX},
        ).first()
    return row is not None


def _template_count(engine: Engine) -> int:
    with engine.connect() as conn:
        return int(conn.execute(text("SELECT COUNT(*) FROM whatsapp_templates")).scalar_one())


def _insert_tenant(engine: Engine, name: str) -> int:
    with engine.begin() as conn:
        return int(
            conn.execute(
                text("INSERT INTO tenants (name) VALUES (:name) RETURNING id"),
                {"name": name},
            ).scalar_one()
        )


def _insert_template(
    engine: Engine,
    *,
    tenant_id: int,
    name: str,
    service_key: str = "cod_confirmation",
    is_active: bool = True,
    is_hidden: bool = False,
    step_number=None,
) -> None:
    with engine.begin() as conn:
        conn.execute(
            text(
                """
                INSERT INTO whatsapp_templates (
                    tenant_id, name, language, category, status,
                    service_key, is_active, is_hidden, step_number, revision
                ) VALUES (
                    :tenant_id, :name, 'ar', 'UTILITY', 'APPROVED',
                    :service_key, :is_active, :is_hidden, :step_number, 1
                )
                """
            ),
            {
                "tenant_id": tenant_id,
                "name": name,
                "service_key": service_key,
                "is_active": is_active,
                "is_hidden": is_hidden,
                "step_number": step_number,
            },
        )


@pytest.fixture(scope="module")
def admin_engine() -> Engine:
    try:
        engine = connect_engine()
    except Exception as exc:  # noqa: BLE001
        message = f"PostgreSQL unavailable for 0104 rehearsal: {exc}"
        if (os.getenv("LEGACY_MIG_PG_INTEGRATION_REQUIRED") or "").strip() == "1":
            pytest.fail(message)
        pytest.skip(message)
    return engine


@pytest.fixture()
def ephemeral_engine(admin_engine: Engine):
    db_name, engine = _ephemeral(admin_engine)
    try:
        yield engine
    finally:
        engine.dispose()
        drop_ephemeral_database(admin_engine, db_name)


def _ephemeral(admin_engine: Engine) -> tuple[str, Engine]:
    db_name, _ = create_ephemeral_database(admin_engine)
    engine = create_engine(
        str(admin_engine.url.set(database=db_name).render_as_string(hide_password=False)),
        poolclass=NullPool,
        pool_pre_ping=True,
    )
    return db_name, engine


def test_clean_upgrade_0103_to_0104(ephemeral_engine: Engine) -> None:
    source = (
        _DATABASE / "migrations" / "versions" / "0104_active_lifecycle_template_null_step.py"
    ).read_text(encoding="utf-8")
    assert 'down_revision = "0103"' in source
    assert "DELETE FROM" not in source.upper().replace(" ", "")
    assert "No rows were deleted" in source
    assert "alembic upgrade head" not in source

    run_alembic(ephemeral_engine, _PARENT)
    assert _current_revisions(ephemeral_engine) == {_PARENT}
    assert _index_exists(ephemeral_engine) is False

    run_alembic(ephemeral_engine, _TARGET)
    assert _current_revisions(ephemeral_engine) == {_TARGET}
    assert _index_exists(ephemeral_engine) is True
    assert _template_count(ephemeral_engine) == 0


def test_duplicate_precheck_refuses_and_keeps_rows(admin_engine: Engine) -> None:
    db_name, engine = _ephemeral(admin_engine)
    try:
        run_alembic(engine, _PARENT)
        tenant_id = _insert_tenant(engine, "t-0104-dup-precheck")
        _insert_template(engine, tenant_id=tenant_id, name="lifecycle_a")
        _insert_template(engine, tenant_id=tenant_id, name="lifecycle_b")
        before = _template_count(engine)
        assert before == 2
        with pytest.raises(Exception) as raised:  # noqa: BLE001 — Alembic wraps RuntimeError
            run_alembic(engine, _TARGET)
        text_exc = str(raised.value)
        assert _ERROR_DUPLICATES in text_exc or "duplicate active lifecycle" in text_exc
        assert _current_revisions(engine) == {_PARENT}
        assert _index_exists(engine) is False
        assert _template_count(engine) == before
    finally:
        engine.dispose()
        drop_ephemeral_database(admin_engine, db_name)


def test_unique_index_rejects_second_active_visible_null_step(
    ephemeral_engine: Engine,
) -> None:
    run_alembic(ephemeral_engine, _TARGET)
    tenant_id = _insert_tenant(ephemeral_engine, "t-0104-unique")
    _insert_template(ephemeral_engine, tenant_id=tenant_id, name="lifecycle_one")
    assert _template_count(ephemeral_engine) == 1
    with pytest.raises(IntegrityError):
        _insert_template(ephemeral_engine, tenant_id=tenant_id, name="lifecycle_two")
    assert _template_count(ephemeral_engine) == 1
    _insert_template(
        ephemeral_engine,
        tenant_id=tenant_id,
        name="lifecycle_hidden",
        is_hidden=True,
    )
    _insert_template(
        ephemeral_engine,
        tenant_id=tenant_id,
        name="lifecycle_inactive",
        is_active=False,
    )
    _insert_template(
        ephemeral_engine,
        tenant_id=tenant_id,
        name="lifecycle_step",
        step_number=1,
    )
    assert _template_count(ephemeral_engine) == 4

"""Operator regression for coexistence nonce migration 0101."""
from __future__ import annotations

import os
import sys
from pathlib import Path

import pytest
from alembic import command
from alembic.config import Config
from sqlalchemy import create_engine, inspect, text
from sqlalchemy.pool import NullPool

_REPO = Path(__file__).resolve().parents[2]
for entry in (str(_REPO), str(_REPO / "backend"), str(_REPO / "database")):
    if entry not in sys.path:
        sys.path.insert(0, entry)

from scripts.operators.bootstrap_migration_contract import (  # noqa: E402
    COEXISTENCE_NONCE_TABLE,
    assert_coexistence_nonce_migration_applied,
    assert_coexistence_nonce_migration_missing,
    build_coexistence_nonce_upgrade_argv,
)


def test_build_coexistence_nonce_upgrade_argv():
    assert build_coexistence_nonce_upgrade_argv(python_executable="python") == [
        "python", "-m", "alembic", "upgrade", "0101",
    ]


def _engine():
    url = (os.getenv("A1_PG_TEST_DATABASE_URL") or os.getenv("DATABASE_URL") or
           "postgresql://nahla:nahla_password@127.0.0.1:5433/nahla_saas")
    return create_engine(url, poolclass=NullPool, pool_pre_ping=True)


def _run(engine, revision: str) -> None:
    prev = os.getcwd()
    try:
        os.chdir(_REPO / "database")
        cfg = Config("alembic.ini")
        cfg.set_main_option("sqlalchemy.url", engine.url.render_as_string(hide_password=False))
        command.upgrade(cfg, revision)
    finally:
        os.chdir(prev)


def test_downgrade_0101_to_0100_removes_nonce_table():
    try:
        engine = _engine()
        with engine.connect() as conn:
            conn.execute(text("SELECT 1"))
    except Exception as exc:  # noqa: BLE001
        pytest.skip(f"PostgreSQL unavailable: {exc}")
    _run(engine, "0101")
    with engine.connect() as conn:
        assert_coexistence_nonce_migration_applied(conn)
    prev = os.getcwd()
    try:
        os.chdir(_REPO / "database")
        cfg = Config("alembic.ini")
        cfg.set_main_option("sqlalchemy.url", engine.url.render_as_string(hide_password=False))
        command.downgrade(cfg, "0100")
    finally:
        os.chdir(prev)
    with engine.connect() as conn:
        assert_coexistence_nonce_migration_missing(conn)
        assert COEXISTENCE_NONCE_TABLE not in inspect(conn).get_table_names()
    engine.dispose()

from scripts.operators.bootstrap_migration_contract import resolve_coexistence_nonce_migration_target  # noqa: E402


def test_resolve_target_prefers_0102_when_0100_present():
    class _Bind:
        def execute(self, *_args, **_kwargs):
            class _R:
                def __iter__(self_inner):
                    yield ("0100",)
            return _R()

    assert resolve_coexistence_nonce_migration_target(_Bind()) == "0102"


def test_resolve_target_defaults_to_0101_without_salla_branch():
    class _Bind:
        def execute(self, *_args, **_kwargs):
            class _R:
                def __iter__(self_inner):
                    yield ("0093",)
            return _R()

    assert resolve_coexistence_nonce_migration_target(_Bind()) == "0101"


def test_fresh_0093_upgrade_to_0101_creates_nonce_table():
    try:
        engine = _engine()
        with engine.connect() as conn:
            conn.execute(text("SELECT 1"))
    except Exception as exc:  # noqa: BLE001
        pytest.skip(f"PostgreSQL unavailable: {exc}")
    admin_base = engine.url.render_as_string(hide_password=False).rsplit("/", 1)[0]
    db_name = "coex_nonce_0093_path"
    admin = create_engine(f"{admin_base}/postgres", isolation_level="AUTOCOMMIT", poolclass=NullPool)
    with admin.connect() as conn:
        conn.execute(text(f'DROP DATABASE IF EXISTS "{db_name}" WITH (FORCE)'))
        conn.execute(text(f'CREATE DATABASE "{db_name}"'))
    fresh = create_engine(f"{admin_base}/{db_name}", poolclass=NullPool)
    try:
        _run(fresh, "0101")
        with fresh.connect() as conn:
            assert_coexistence_nonce_migration_applied(conn)
            revs = [row[0] for row in conn.execute(text("SELECT version_num FROM alembic_version"))]
        assert "0101" in revs
    finally:
        fresh.dispose()
        with admin.connect() as conn:
            conn.execute(text(f'DROP DATABASE "{db_name}" WITH (FORCE)'))
        admin.dispose()
        engine.dispose()


def test_salla_0100_upgrade_to_0102_creates_nonce_table():
    try:
        engine = _engine()
        with engine.connect() as conn:
            conn.execute(text("SELECT 1"))
    except Exception as exc:  # noqa: BLE001
        pytest.skip(f"PostgreSQL unavailable: {exc}")
    admin_base = engine.url.render_as_string(hide_password=False).rsplit("/", 1)[0]
    db_name = "coex_nonce_0102_path"
    admin = create_engine(f"{admin_base}/postgres", isolation_level="AUTOCOMMIT", poolclass=NullPool)
    with admin.connect() as conn:
        conn.execute(text(f'DROP DATABASE IF EXISTS "{db_name}" WITH (FORCE)'))
        conn.execute(text(f'CREATE DATABASE "{db_name}"'))
    fresh = create_engine(f"{admin_base}/{db_name}", poolclass=NullPool)
    try:
        _run(fresh, "0100")
        _run(fresh, "0102")
        with fresh.connect() as conn:
            assert_coexistence_nonce_migration_applied(conn)
            revs = [row[0] for row in conn.execute(text("SELECT version_num FROM alembic_version"))]
        assert "0102" in revs
    finally:
        fresh.dispose()
        with admin.connect() as conn:
            conn.execute(text(f'DROP DATABASE "{db_name}" WITH (FORCE)'))
        admin.dispose()
        engine.dispose()

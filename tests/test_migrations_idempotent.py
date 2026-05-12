"""
tests/test_migrations_idempotent.py
───────────────────────────────────
Locks the F16 fix for the production "alembic head=0049 but later
migrations' tables already exist" drift state.

The exact production error this test reproduces:

    psycopg2.errors.DuplicateTable: relation "manual_coupons"
    already exists

The fix: every ``create_table``, ``create_index``,
``create_unique_constraint``, and ``add_column`` in migrations
0050–0054 now consults the SQLAlchemy inspector and skips the
operation when the object already exists. These tests drive each
migration's ``upgrade()`` twice against the same in-memory DB —
the second invocation must succeed without raising.

Why not run the full Alembic chain
──────────────────────────────────
``alembic upgrade head`` from 0001 requires the entire 1-49
history to work on SQLite, which is not maintained (some early
migrations use Postgres-specific DDL). Instead we drive each
target migration's ``upgrade()`` directly via
``alembic.migration.MigrationContext`` + ``alembic.operations.Operations``
— this is the same pair Alembic uses at runtime, just without
the version-table machinery.
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest
from sqlalchemy import create_engine, inspect, text
from sqlalchemy.engine import Engine

REPO_ROOT = Path(__file__).resolve().parents[1]
BACKEND_DIR = REPO_ROOT / "backend"
DATABASE_DIR = REPO_ROOT / "database"
MIGRATIONS_DIR = DATABASE_DIR / "migrations" / "versions"
for _p in (REPO_ROOT, BACKEND_DIR, DATABASE_DIR):
    if str(_p) not in sys.path:
        sys.path.insert(0, str(_p))


# ── Tooling ─────────────────────────────────────────────────────────


def _seed_prerequisites(engine: Engine) -> None:
    """Create the absolute-minimum parent tables that the migrations
    under test reference via FK. They're not part of what we're
    testing; we just need them to exist so the FK constraints don't
    explode on SQLite."""
    with engine.begin() as conn:
        conn.execute(text(
            "CREATE TABLE IF NOT EXISTS tenants ("
            "id INTEGER PRIMARY KEY, name VARCHAR)"
        ))
        conn.execute(text(
            "CREATE TABLE IF NOT EXISTS customers ("
            "id INTEGER PRIMARY KEY, tenant_id INTEGER)"
        ))
        conn.execute(text(
            "CREATE TABLE IF NOT EXISTS campaigns ("
            "id INTEGER PRIMARY KEY, tenant_id INTEGER)"
        ))


def _run_migration_upgrade(engine: Engine, module_name: str) -> None:
    """Run ``module.upgrade()`` in a real Alembic op context bound
    to the given engine. This is what Alembic does internally — we
    skip the version-table dance because we want to test
    idempotency, not the upgrade chain.

    ``Operations.context(ctx)`` is the public API: it installs a
    thread-local proxy so the migration's
    ``from alembic import op`` calls hit our migration context.
    """
    import importlib
    from alembic.migration import MigrationContext
    from alembic.operations import Operations

    # Import the migration module fresh each call so its module-
    # level state (e.g. cached inspector results) is reset.
    mod = importlib.import_module(
        f"database.migrations.versions.{module_name}"
    )
    importlib.reload(mod)

    with engine.connect() as conn:
        ctx = MigrationContext.configure(conn)
        with Operations.context(ctx):
            mod.upgrade()
        conn.commit()


def _make_engine() -> Engine:
    """Brand-new in-memory SQLite DB with all FK-target parent tables
    pre-seeded so the migration's FK declarations are satisfied."""
    engine = create_engine("sqlite:///:memory:")
    _seed_prerequisites(engine)
    return engine


# ── Per-migration idempotency tests ────────────────────────────────


class TestMigration0050Idempotent:
    """``manual_coupons`` + ``ai_media_library`` tables."""

    def test_runs_clean_then_runs_again_without_error(self):
        engine = _make_engine()

        _run_migration_upgrade(engine, "0050_manual_coupons_ai_media_library")
        insp = inspect(engine)
        assert "manual_coupons"    in insp.get_table_names()
        assert "ai_media_library"  in insp.get_table_names()

        # Second run must NOT raise DuplicateTable / DuplicateRelation.
        _run_migration_upgrade(engine, "0050_manual_coupons_ai_media_library")

    def test_runs_when_table_pre_exists_in_drifted_state(self):
        """The exact production scenario: ``manual_coupons`` already
        exists in the DB before alembic walks 0050. Pre-F16 this
        raised DuplicateTable and aborted the whole chain.

        Mirror what legacy ``Base.metadata.create_all()`` produced:
        the table has every column the model declares, so indexes
        referencing those columns can still be created (the index
        DDL touches columns by name)."""
        engine = _make_engine()
        with engine.begin() as conn:
            conn.execute(text(
                "CREATE TABLE manual_coupons ("
                "id INTEGER PRIMARY KEY, "
                "tenant_id INTEGER NOT NULL, "
                "code VARCHAR NOT NULL, "
                "title VARCHAR, "
                "description TEXT, "
                "discount_text VARCHAR, "
                "usage_context TEXT, "
                "is_active BOOLEAN NOT NULL DEFAULT 1, "
                "priority INTEGER NOT NULL DEFAULT 100, "
                "starts_at DATETIME, "
                "expires_at DATETIME, "
                "created_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP, "
                "updated_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP)"
            ))

        # MUST NOT raise.
        _run_migration_upgrade(engine, "0050_manual_coupons_ai_media_library")

        # The sibling table that didn't pre-exist must still be created.
        insp = inspect(engine)
        assert "ai_media_library" in insp.get_table_names()


class TestMigration0051Idempotent:
    def test_runs_clean_then_runs_again_without_error(self):
        engine = _make_engine()
        _run_migration_upgrade(engine, "0051_campaign_send_logs")
        insp = inspect(engine)
        assert "campaign_send_logs" in insp.get_table_names()
        # Re-run.
        _run_migration_upgrade(engine, "0051_campaign_send_logs")

    def test_runs_when_table_pre_exists(self):
        """Mirror the columns of the 0051 schema so the
        idempotency-skipped index creations don't reference
        missing columns."""
        engine = _make_engine()
        with engine.begin() as conn:
            conn.execute(text(
                "CREATE TABLE campaign_send_logs ("
                "id INTEGER PRIMARY KEY, "
                "tenant_id INTEGER NOT NULL, "
                "campaign_id INTEGER NOT NULL, "
                "customer_id INTEGER, "
                "customer_phone_e164 VARCHAR NOT NULL, "
                "template_name VARCHAR, "
                "template_language VARCHAR, "
                "payload_hash VARCHAR, "
                "status VARCHAR NOT NULL DEFAULT 'queued', "
                "provider_message_id VARCHAR, "
                "error_code VARCHAR, "
                "error_message TEXT, "
                "skip_reason VARCHAR, "
                "attempt_count INTEGER NOT NULL DEFAULT 0, "
                "sent_at DATETIME, "
                "created_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP, "
                "updated_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP)"
            ))
        _run_migration_upgrade(engine, "0051_campaign_send_logs")


class TestMigration0052Idempotent:
    def test_runs_clean_then_runs_again(self):
        engine = _make_engine()
        _run_migration_upgrade(engine, "0052_customer_segments_manual")
        _run_migration_upgrade(engine, "0052_customer_segments_manual")

    def test_runs_when_table_pre_exists(self):
        engine = _make_engine()
        with engine.begin() as conn:
            conn.execute(text(
                "CREATE TABLE customer_segments_manual ("
                "id INTEGER PRIMARY KEY, "
                "tenant_id INTEGER NOT NULL, "
                "customer_id INTEGER NOT NULL, "
                "segment_key VARCHAR NOT NULL, "
                "source VARCHAR NOT NULL DEFAULT 'manual', "
                "created_by INTEGER, "
                "created_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP)"
            ))
        _run_migration_upgrade(engine, "0052_customer_segments_manual")


class TestMigration0053Idempotent:
    """Adds ``mode`` column to ``customer_segments_manual``."""

    def test_runs_clean_then_runs_again(self):
        engine = _make_engine()
        _run_migration_upgrade(engine, "0052_customer_segments_manual")
        _run_migration_upgrade(engine, "0053_customer_segments_manual_mode")
        # The mode column now exists.
        insp = inspect(engine)
        cols = {c["name"] for c in insp.get_columns("customer_segments_manual")}
        assert "mode" in cols
        # Second run is a no-op.
        _run_migration_upgrade(engine, "0053_customer_segments_manual_mode")

    def test_runs_when_column_pre_exists(self):
        """A partial drift where the table AND the column already
        exist. Pre-F16 this would have raised DuplicateColumn."""
        engine = _make_engine()
        with engine.begin() as conn:
            conn.execute(text(
                "CREATE TABLE customer_segments_manual ("
                "id INTEGER PRIMARY KEY, "
                "tenant_id INTEGER NOT NULL, "
                "customer_id INTEGER NOT NULL, "
                "segment_key VARCHAR NOT NULL, "
                "source VARCHAR NOT NULL DEFAULT 'manual', "
                "created_by INTEGER, "
                "created_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP, "
                "mode VARCHAR NOT NULL DEFAULT 'include')"
            ))
        _run_migration_upgrade(engine, "0053_customer_segments_manual_mode")


class TestMigration0054Idempotent:
    """Adds ``delivered_at`` / ``read_at`` / ``failed_at`` columns +
    ``ix_campaign_send_log_provider_message_id`` index."""

    def test_runs_clean_then_runs_again(self):
        engine = _make_engine()
        _run_migration_upgrade(engine, "0051_campaign_send_logs")
        _run_migration_upgrade(engine, "0054_campaign_send_log_delivery_tracking")
        # Critical: the column the production error pointed at must exist.
        insp = inspect(engine)
        cols = {c["name"] for c in insp.get_columns("campaign_send_logs")}
        assert "delivered_at" in cols
        assert "read_at"      in cols
        assert "failed_at"    in cols
        # Replay safety.
        _run_migration_upgrade(engine, "0054_campaign_send_log_delivery_tracking")

    def test_runs_when_only_some_columns_pre_exist(self):
        """Partial drift: ``delivered_at`` already exists but
        ``read_at`` / ``failed_at`` do not. Each column add must be
        evaluated independently."""
        engine = _make_engine()
        _run_migration_upgrade(engine, "0051_campaign_send_logs")
        with engine.begin() as conn:
            conn.execute(text(
                "ALTER TABLE campaign_send_logs ADD COLUMN delivered_at DATETIME"
            ))
        _run_migration_upgrade(engine, "0054_campaign_send_log_delivery_tracking")
        insp = inspect(engine)
        cols = {c["name"] for c in insp.get_columns("campaign_send_logs")}
        assert {"delivered_at", "read_at", "failed_at"}.issubset(cols)


# ── Combined chain: simulate production drift exactly ──────────────


class TestFullDriftRecoveryChain:
    """The exact production scenario: ``alembic_version='0049'`` but
    ``manual_coupons`` already exists. Run 0050→0054 in sequence and
    verify every migration succeeds without raising. Pre-F16 the
    chain aborted at 0050 with DuplicateTable."""

    def test_chain_recovers_from_drift_state(self):
        engine = _make_engine()
        # Simulate the drift: one of the "later" tables exists
        # already (mirrors what Base.metadata.create_all() would
        # have left behind, with the full column set).
        with engine.begin() as conn:
            conn.execute(text(
                "CREATE TABLE manual_coupons ("
                "id INTEGER PRIMARY KEY, "
                "tenant_id INTEGER NOT NULL, "
                "code VARCHAR NOT NULL, "
                "title VARCHAR, "
                "description TEXT, "
                "discount_text VARCHAR, "
                "usage_context TEXT, "
                "is_active BOOLEAN NOT NULL DEFAULT 1, "
                "priority INTEGER NOT NULL DEFAULT 100, "
                "starts_at DATETIME, "
                "expires_at DATETIME, "
                "created_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP, "
                "updated_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP)"
            ))

        # Walk the chain.
        for mod_name in (
            "0050_manual_coupons_ai_media_library",
            "0051_campaign_send_logs",
            "0052_customer_segments_manual",
            "0053_customer_segments_manual_mode",
            "0054_campaign_send_log_delivery_tracking",
        ):
            _run_migration_upgrade(engine, mod_name)

        # Sanity: the columns that 0054 adds must exist now —
        # that's the column the production dispatcher was crashing
        # on (campaign_send_logs.delivered_at).
        insp = inspect(engine)
        cols = {c["name"] for c in insp.get_columns("campaign_send_logs")}
        assert "delivered_at" in cols
        assert "read_at"      in cols
        assert "failed_at"    in cols

        # And the mode column on customer_segments_manual.
        cols2 = {c["name"] for c in insp.get_columns("customer_segments_manual")}
        assert "mode" in cols2

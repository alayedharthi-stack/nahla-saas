"""Legacy migration drift recovery — Alembic 0024 → 0030 on PostgreSQL."""
from __future__ import annotations

import sys
from pathlib import Path

from sqlalchemy import text
from sqlalchemy.engine import Engine

_REPO = Path(__file__).resolve().parents[2]
for p in (str(_REPO), str(_REPO / "backend"), str(_REPO / "database")):
    if p not in sys.path:
        sys.path.insert(0, p)

from tests.legacy_migration_drift_0025_0030_postgres_fixtures import (  # noqa: E402
    TARGET_REVISION,
    assert_revision,
    assert_schema_at_0030,
    downgrade_alembic,
    ephemeral_legacy_migration_engine_0024,
    run_alembic,
    seed_create_all_drift_0025_0030,
)

MIGRATION_TENANT_ID = 880_001


def test_clean_chain_upgrade_0024_to_0030(
    ephemeral_legacy_migration_engine_0024: Engine,
) -> None:
    run_alembic(ephemeral_legacy_migration_engine_0024, TARGET_REVISION)
    assert_revision(ephemeral_legacy_migration_engine_0024, TARGET_REVISION)
    assert_schema_at_0030(ephemeral_legacy_migration_engine_0024)


def test_drifted_schema_recovery_upgrade_to_0030(
    ephemeral_legacy_migration_engine_0024: Engine,
) -> None:
    seed_create_all_drift_0025_0030(ephemeral_legacy_migration_engine_0024)
    run_alembic(ephemeral_legacy_migration_engine_0024, TARGET_REVISION)
    assert_revision(ephemeral_legacy_migration_engine_0024, TARGET_REVISION)
    assert_schema_at_0030(ephemeral_legacy_migration_engine_0024)


def test_drifted_upgrade_is_idempotent_on_repeat(
    ephemeral_legacy_migration_engine_0024: Engine,
) -> None:
    seed_create_all_drift_0025_0030(ephemeral_legacy_migration_engine_0024)
    run_alembic(ephemeral_legacy_migration_engine_0024, TARGET_REVISION)
    run_alembic(ephemeral_legacy_migration_engine_0024, TARGET_REVISION)
    assert_revision(ephemeral_legacy_migration_engine_0024, TARGET_REVISION)
    assert_schema_at_0030(ephemeral_legacy_migration_engine_0024)


def test_0027_engine_backfill_survives_drift_path(
    ephemeral_legacy_migration_engine_0024: Engine,
) -> None:
    """0027 data semantics: canonical ENGINE_BY_TYPE mapping is applied on drift."""
    seed_create_all_drift_0025_0030(ephemeral_legacy_migration_engine_0024)
    with ephemeral_legacy_migration_engine_0024.begin() as conn:
        conn.execute(
            text(
                """
                INSERT INTO tenants (id, name, is_active)
                VALUES (:tid, 'متجر تجريبي عام', true)
                ON CONFLICT (id) DO NOTHING
                """
            ),
            {"tid": MIGRATION_TENANT_ID},
        )
        conn.execute(
            text(
                """
                INSERT INTO smart_automations (tenant_id, automation_type, name, enabled)
                VALUES
                    (:tid, 'vip_upgrade', 'generic-growth-automation', false),
                    (:tid, 'abandoned_cart', 'generic-recovery-automation', false)
                """
            ),
            {"tid": MIGRATION_TENANT_ID},
        )

    run_alembic(ephemeral_legacy_migration_engine_0024, TARGET_REVISION)

    with ephemeral_legacy_migration_engine_0024.connect() as conn:
        rows = conn.execute(
            text(
                """
                SELECT automation_type, engine
                FROM smart_automations
                WHERE tenant_id = :tid
                ORDER BY automation_type
                """
            ),
            {"tid": MIGRATION_TENANT_ID},
        ).mappings().all()

    assert rows == [
        {"automation_type": "abandoned_cart", "engine": "recovery"},
        {"automation_type": "vip_upgrade", "engine": "growth"},
    ]


def test_0030_platform_tenant_defaults_false_on_clean_path(
    ephemeral_legacy_migration_engine_0024: Engine,
) -> None:
    """0030 semantics: existing tenants default to merchant (is_platform_tenant=false)."""
    with ephemeral_legacy_migration_engine_0024.begin() as conn:
        conn.execute(
            text(
                """
                INSERT INTO tenants (id, name, is_active)
                VALUES (:tid, 'متجر تجريبي عام', true)
                ON CONFLICT (id) DO NOTHING
                """
            ),
            {"tid": MIGRATION_TENANT_ID},
        )

    run_alembic(ephemeral_legacy_migration_engine_0024, TARGET_REVISION)

    with ephemeral_legacy_migration_engine_0024.connect() as conn:
        flag = conn.execute(
            text(
                """
                SELECT is_platform_tenant FROM tenants WHERE id = :tid
                """
            ),
            {"tid": MIGRATION_TENANT_ID},
        ).scalar_one()

    assert flag is False


def test_0030_forward_column_drift_preserves_false_default_and_adds_index(
    ephemeral_legacy_migration_engine_0024: Engine,
) -> None:
    """0030 reconciles a forward-ORM column and its missing migration-owned index."""
    with ephemeral_legacy_migration_engine_0024.begin() as conn:
        conn.execute(
            text(
                """
                INSERT INTO tenants (id, name, is_active)
                VALUES (:tid, 'متجر تجريبي عام', true)
                ON CONFLICT (id) DO NOTHING
                """
            ),
            {"tid": MIGRATION_TENANT_ID},
        )
        # Model the narrow known drift shape: create_all added the column,
        # but Alembic-owned index creation did not run.
        conn.execute(
            text(
                """
                ALTER TABLE tenants
                ADD COLUMN is_platform_tenant BOOLEAN NOT NULL DEFAULT false
                """
            )
        )

    run_alembic(ephemeral_legacy_migration_engine_0024, TARGET_REVISION)

    with ephemeral_legacy_migration_engine_0024.connect() as conn:
        flag = conn.execute(
            text(
                """
                SELECT is_platform_tenant FROM tenants WHERE id = :tid
                """
            ),
            {"tid": MIGRATION_TENANT_ID},
        ).scalar_one()
        index_exists = conn.execute(
            text(
                """
                SELECT EXISTS (
                    SELECT 1
                    FROM pg_indexes
                    WHERE schemaname = 'public'
                      AND tablename = 'tenants'
                      AND indexname = 'ix_tenants_is_platform_tenant'
                )
                """
            )
        ).scalar_one()

    assert flag is False
    assert index_exists is True


def test_drifted_path_downgrades_0030_to_0029_coherently(
    ephemeral_legacy_migration_engine_0024: Engine,
) -> None:
    """Test-only DDL coherence; real staging rollback remains restore-first."""
    seed_create_all_drift_0025_0030(ephemeral_legacy_migration_engine_0024)
    run_alembic(ephemeral_legacy_migration_engine_0024, TARGET_REVISION)

    downgrade_alembic(ephemeral_legacy_migration_engine_0024, "0029")

    assert_revision(ephemeral_legacy_migration_engine_0024, "0029")
    with ephemeral_legacy_migration_engine_0024.connect() as conn:
        column_exists = conn.execute(
            text(
                """
                SELECT EXISTS (
                    SELECT 1
                    FROM information_schema.columns
                    WHERE table_schema = 'public'
                      AND table_name = 'tenants'
                      AND column_name = 'is_platform_tenant'
                )
                """
            )
        ).scalar_one()
        index_exists = conn.execute(
            text(
                """
                SELECT EXISTS (
                    SELECT 1
                    FROM pg_indexes
                    WHERE schemaname = 'public'
                      AND tablename = 'tenants'
                      AND indexname = 'ix_tenants_is_platform_tenant'
                )
                """
            )
        ).scalar_one()
        offer_decisions_exists = conn.execute(
            text(
                """
                SELECT EXISTS (
                    SELECT 1
                    FROM information_schema.tables
                    WHERE table_schema = 'public'
                      AND table_name = 'offer_decisions'
                )
                """
            )
        ).scalar_one()

    assert column_exists is False
    assert index_exists is False
    assert offer_decisions_exists is True

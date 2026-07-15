"""Legacy migration drift recovery — Alembic 0016 → 0024 on PostgreSQL."""
from __future__ import annotations

import sys
from pathlib import Path

import pytest
from sqlalchemy import text
from sqlalchemy.engine import Engine

_REPO = Path(__file__).resolve().parents[2]
for p in (str(_REPO), str(_REPO / "backend"), str(_REPO / "database")):
    if p not in sys.path:
        sys.path.insert(0, p)

from tests.legacy_migration_drift_postgres_fixtures import (  # noqa: E402
    TARGET_REVISION,
    assert_revision,
    assert_schema_at_0024,
    downgrade_alembic,
    ephemeral_legacy_migration_engine,
    run_alembic,
    seed_create_all_safe_alter_drift,
)

MIGRATION_TENANT_ID = 770_001
WABA_DISCONNECTED_TENANT_ID = 770_002
WABA_RETAINED_TENANT_ID = 770_003
DRIFT_WABA_ID = "waba-drift-test-1"


def test_clean_chain_upgrade_0016_to_0024(ephemeral_legacy_migration_engine: Engine) -> None:
    run_alembic(ephemeral_legacy_migration_engine, TARGET_REVISION)
    assert_revision(ephemeral_legacy_migration_engine, TARGET_REVISION)
    assert_schema_at_0024(ephemeral_legacy_migration_engine)


def test_drifted_schema_recovery_upgrade_to_0024(ephemeral_legacy_migration_engine: Engine) -> None:
    seed_create_all_safe_alter_drift(ephemeral_legacy_migration_engine)
    run_alembic(ephemeral_legacy_migration_engine, TARGET_REVISION)
    assert_revision(ephemeral_legacy_migration_engine, TARGET_REVISION)
    assert_schema_at_0024(ephemeral_legacy_migration_engine)


def test_drifted_upgrade_is_idempotent_on_repeat(ephemeral_legacy_migration_engine: Engine) -> None:
    seed_create_all_safe_alter_drift(ephemeral_legacy_migration_engine)
    run_alembic(ephemeral_legacy_migration_engine, TARGET_REVISION)
    run_alembic(ephemeral_legacy_migration_engine, TARGET_REVISION)
    assert_revision(ephemeral_legacy_migration_engine, TARGET_REVISION)
    assert_schema_at_0024(ephemeral_legacy_migration_engine)


def test_drifted_path_downgrades_0024_to_0023_coherently(
    ephemeral_legacy_migration_engine: Engine,
) -> None:
    """0024→0023 is bounded: its explicit data-only downgrade retains 0020–0023 schema."""
    seed_create_all_safe_alter_drift(ephemeral_legacy_migration_engine)
    with ephemeral_legacy_migration_engine.begin() as conn:
        conn.execute(
            text(
                """
                INSERT INTO tenants (id, name, is_active)
                VALUES (:tid, 'Drift Downgrade Tenant', true)
                """
            ),
            {"tid": MIGRATION_TENANT_ID},
        )
        conn.execute(
            text(
                """
                INSERT INTO smart_automations (tenant_id, automation_type, name, enabled)
                VALUES (:tid, 'customer_winback', 'drift-downgrade', false)
                """
            ),
            {"tid": MIGRATION_TENANT_ID},
        )

    run_alembic(ephemeral_legacy_migration_engine, TARGET_REVISION)
    downgrade_alembic(ephemeral_legacy_migration_engine, "0023")

    assert_revision(ephemeral_legacy_migration_engine, "0023")
    assert_schema_at_0024(ephemeral_legacy_migration_engine)
    with ephemeral_legacy_migration_engine.connect() as conn:
        trigger_event = conn.execute(
            text(
                """
                SELECT trigger_event FROM smart_automations
                WHERE tenant_id = :tid AND automation_type = 'customer_winback'
                """
            ),
            {"tid": MIGRATION_TENANT_ID},
        ).scalar_one()
    assert trigger_event == "customer_status_changed"


def test_0022_duplicate_waba_remediation_survives_drift_path(
    ephemeral_legacy_migration_engine: Engine,
) -> None:
    """0022 retains the highest tenant ID and disconnects the other WABA collision."""
    seed_create_all_safe_alter_drift(ephemeral_legacy_migration_engine)
    with ephemeral_legacy_migration_engine.begin() as conn:
        # The safe-alter collision seed includes this index; remove it so 0022
        # can exercise its original duplicate-remediation path before recreating it.
        conn.execute(text("DROP INDEX uq_wa_conn_waba_id"))
        conn.execute(
            text(
                """
                INSERT INTO tenants (id, name, is_active)
                VALUES
                    (:retained, 'WABA Retained Tenant', true),
                    (:disconnected, 'WABA Disconnected Tenant', true)
                """
            ),
            {
                "retained": WABA_RETAINED_TENANT_ID,
                "disconnected": WABA_DISCONNECTED_TENANT_ID,
            },
        )
        conn.execute(
            text(
                """
                INSERT INTO whatsapp_connections (
                    tenant_id, status, whatsapp_business_account_id,
                    sending_enabled, webhook_verified
                ) VALUES
                    (:retained, 'connected', :waba, true, true),
                    (:disconnected, 'connected', :waba, true, true)
                """
            ),
            {
                "retained": WABA_RETAINED_TENANT_ID,
                "disconnected": WABA_DISCONNECTED_TENANT_ID,
                "waba": DRIFT_WABA_ID,
            },
        )

    run_alembic(ephemeral_legacy_migration_engine, TARGET_REVISION)

    with ephemeral_legacy_migration_engine.connect() as conn:
        rows = conn.execute(
            text(
                """
                SELECT tenant_id, whatsapp_business_account_id, status,
                       sending_enabled, webhook_verified
                FROM whatsapp_connections
                WHERE tenant_id IN (:retained, :disconnected)
                ORDER BY tenant_id
                """
            ),
            {
                "retained": WABA_RETAINED_TENANT_ID,
                "disconnected": WABA_DISCONNECTED_TENANT_ID,
            },
        ).mappings().all()

    assert rows == [
        {
            "tenant_id": WABA_DISCONNECTED_TENANT_ID,
            "whatsapp_business_account_id": None,
            "status": "disconnected",
            "sending_enabled": False,
            "webhook_verified": False,
        },
        {
            "tenant_id": WABA_RETAINED_TENANT_ID,
            "whatsapp_business_account_id": DRIFT_WABA_ID,
            "status": "connected",
            "sending_enabled": True,
            "webhook_verified": True,
        },
    ]


def test_0023_duplicate_orders_still_merged_on_drift_path(
    ephemeral_legacy_migration_engine: Engine,
) -> None:
    """0023 data semantics: duplicate (tenant_id, external_id) rows are merged."""
    seed_create_all_safe_alter_drift(ephemeral_legacy_migration_engine)
    with ephemeral_legacy_migration_engine.begin() as conn:
        conn.execute(
            text(
                """
                INSERT INTO tenants (id, name, is_active)
                VALUES (:tid, 'Drift Dedupe Tenant', true)
                ON CONFLICT (id) DO NOTHING
                """
            ),
            {"tid": MIGRATION_TENANT_ID},
        )
        conn.execute(
            text(
                """
                INSERT INTO orders (tenant_id, external_id, status, total)
                VALUES
                    (:tid, 'DUP-EXT-1', 'pending', '10'),
                    (:tid, 'DUP-EXT-1', 'pending', '20')
                """
            ),
            {"tid": MIGRATION_TENANT_ID},
        )

    run_alembic(ephemeral_legacy_migration_engine, TARGET_REVISION)

    with ephemeral_legacy_migration_engine.connect() as conn:
        count = conn.execute(
            text(
                """
                SELECT COUNT(*) FROM orders
                WHERE tenant_id = :tid AND external_id = 'DUP-EXT-1'
                """
            ),
            {"tid": MIGRATION_TENANT_ID},
        ).scalar_one()
        kept_total = conn.execute(
            text(
                """
                SELECT total FROM orders
                WHERE tenant_id = :tid AND external_id = 'DUP-EXT-1'
                """
            ),
            {"tid": MIGRATION_TENANT_ID},
        ).scalar_one()

    assert count == 1
    assert kept_total == "20"

"""Legacy migration drift recovery — Alembic 0032 → 0083 on PostgreSQL."""
from __future__ import annotations

import sys
from pathlib import Path

import pytest
from alembic.util.exc import CommandError
from sqlalchemy import text
from sqlalchemy.engine import Engine

_REPO = Path(__file__).resolve().parents[2]
for p in (str(_REPO), str(_REPO / "backend"), str(_REPO / "database")):
    if p not in sys.path:
        sys.path.insert(0, p)

from tests.legacy_migration_drift_0032_0083_postgres_fixtures import (  # noqa: E402
    TARGET_REVISION,
    assert_revision,
    assert_schema_at_0083,
    ephemeral_legacy_migration_engine_0032,
    run_alembic,
    seed_create_all_drift_0033_0083,
    seed_inequivalent_product_variants_drift,
)

MIGRATION_TENANT_ID = 880_032
MIGRATION_PRODUCT_ID = 880_101


def test_clean_chain_upgrade_0032_to_0083(
    ephemeral_legacy_migration_engine_0032: Engine,
) -> None:
    run_alembic(ephemeral_legacy_migration_engine_0032, TARGET_REVISION)
    assert_revision(ephemeral_legacy_migration_engine_0032, TARGET_REVISION)
    assert_schema_at_0083(ephemeral_legacy_migration_engine_0032)


def test_drifted_schema_recovery_upgrade_to_0083(
    ephemeral_legacy_migration_engine_0032: Engine,
) -> None:
    seed_create_all_drift_0033_0083(ephemeral_legacy_migration_engine_0032)
    run_alembic(ephemeral_legacy_migration_engine_0032, TARGET_REVISION)
    assert_revision(ephemeral_legacy_migration_engine_0032, TARGET_REVISION)
    assert_schema_at_0083(ephemeral_legacy_migration_engine_0032)


def test_drifted_upgrade_is_idempotent_on_repeat(
    ephemeral_legacy_migration_engine_0032: Engine,
) -> None:
    seed_create_all_drift_0033_0083(ephemeral_legacy_migration_engine_0032)
    run_alembic(ephemeral_legacy_migration_engine_0032, TARGET_REVISION)
    run_alembic(ephemeral_legacy_migration_engine_0032, TARGET_REVISION)
    assert_revision(ephemeral_legacy_migration_engine_0032, TARGET_REVISION)
    assert_schema_at_0083(ephemeral_legacy_migration_engine_0032)


def test_0064_missing_parent_columns_and_zero_backfill_on_drift_path(
    ephemeral_legacy_migration_engine_0032: Engine,
) -> None:
    """0064 adds parent columns and skips backfill when variant rows already exist."""
    seed_create_all_drift_0033_0083(ephemeral_legacy_migration_engine_0032)

    with ephemeral_legacy_migration_engine_0032.begin() as conn:
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
                INSERT INTO products (id, tenant_id, name, price, in_stock)
                VALUES (:pid, :tid, 'حذاء رياضي أبيض', '199.00', true)
                ON CONFLICT (id) DO NOTHING
                """
            ),
            {"pid": MIGRATION_PRODUCT_ID, "tid": MIGRATION_TENANT_ID},
        )
        conn.execute(
            text(
                """
                INSERT INTO product_variants (
                    tenant_id, product_id, retailer_id, is_default, in_stock
                )
                VALUES (:tid, :pid, 'RRRD1234', true, true)
                """
            ),
            {"tid": MIGRATION_TENANT_ID, "pid": MIGRATION_PRODUCT_ID},
        )

    before_count = _variant_count(ephemeral_legacy_migration_engine_0032, MIGRATION_PRODUCT_ID)
    run_alembic(ephemeral_legacy_migration_engine_0032, TARGET_REVISION)
    after_count = _variant_count(ephemeral_legacy_migration_engine_0032, MIGRATION_PRODUCT_ID)

    assert before_count == after_count == 1
    with ephemeral_legacy_migration_engine_0032.connect() as conn:
        row = conn.execute(
            text(
                """
                SELECT has_variants, default_variant_id
                FROM products
                WHERE id = :pid
                """
            ),
            {"pid": MIGRATION_PRODUCT_ID},
        ).mappings().one()
    assert row["has_variants"] is False
    assert row["default_variant_id"] is not None


def test_inequivalent_product_variants_shape_fails_closed(
    ephemeral_legacy_migration_engine_0032: Engine,
) -> None:
    """Arbitrary partial drift without product_id is not reconciled silently."""
    seed_inequivalent_product_variants_drift(ephemeral_legacy_migration_engine_0032)
    with pytest.raises(CommandError):
        run_alembic(ephemeral_legacy_migration_engine_0032, TARGET_REVISION)


def _variant_count(engine: Engine, product_id: int) -> int:
    with engine.connect() as conn:
        return int(
            conn.execute(
                text("SELECT count(*)::int FROM product_variants WHERE product_id = :pid"),
                {"pid": product_id},
            ).scalar_one()
        )

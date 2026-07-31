"""PostgreSQL integration tests for guarded staging migration 0030→0087 chain."""
from __future__ import annotations

import os
import sys
from pathlib import Path

import pytest
from alembic.script import ScriptDirectory
from sqlalchemy import text
from sqlalchemy.engine import Engine

_REPO = Path(__file__).resolve().parents[2]
for p in (str(_REPO), str(_REPO / "backend"), str(_REPO / "database")):
    if p not in sys.path:
        sys.path.insert(0, p)

from scripts.operators import staging_migration_0030_to_0032 as stage_a_op  # noqa: E402
from scripts.operators import staging_migration_0083_to_0087 as stage_c_op  # noqa: E402
from tests.legacy_migration_drift_0030_0087_postgres_fixtures import (  # noqa: E402
    FINAL_TARGET,
    MIGRATION_TENANT_ID,
    STAGE_A_TARGET,
    STAGE_B_TARGET,
    STAGE_C_TARGET,
    assert_products_external_id_index_valid,
    assert_revision,
    assert_schema_at_0032,
    assert_schema_at_0083,
    assert_schema_at_0087_expand,
    ephemeral_legacy_migration_engine_0030,
    ephemeral_legacy_migration_engine_0083,
    run_alembic,
    seed_duplicate_product_external_id,
    seed_duplicate_salla_backfill_collision,
    seed_duplicate_tenant_phone,
    seed_product_with_metadata_variants,
    seed_product_without_variants,
)


def _staging_env_for_pg() -> dict[str, str]:
    return {
        "RAILWAY_PROJECT_NAME": "desirable-growth",
        "RAILWAY_ENVIRONMENT_NAME": "staging",
        "NAHLA_SKIP_DB_BOOTSTRAP": "1",
        "DATABASE_URL": (
            "postgresql+psycopg2://operator:password@"
            "postgres-staging.railway.internal:5432/nahla"
        ),
        "NAHLA_STAGING_MIGRATION_0030_TO_0032_CONFIRM": "RUN_STAGING_0030_TO_0032",
        "NAHLA_STAGING_MIGRATION_0083_TO_0087_CONFIRM": "RUN_STAGING_0083_TO_0087",
    }


def test_clean_chain_upgrade_0030_to_0087(
    ephemeral_legacy_migration_engine_0030: Engine,
) -> None:
    run_alembic(ephemeral_legacy_migration_engine_0030, STAGE_A_TARGET)
    assert_revision(ephemeral_legacy_migration_engine_0030, STAGE_A_TARGET)
    assert_schema_at_0032(ephemeral_legacy_migration_engine_0030)

    run_alembic(ephemeral_legacy_migration_engine_0030, STAGE_B_TARGET)
    assert_revision(ephemeral_legacy_migration_engine_0030, STAGE_B_TARGET)
    assert_schema_at_0083(ephemeral_legacy_migration_engine_0030)

    run_alembic(ephemeral_legacy_migration_engine_0030, STAGE_C_TARGET)
    assert_revision(ephemeral_legacy_migration_engine_0030, FINAL_TARGET)
    assert_schema_at_0087_expand(ephemeral_legacy_migration_engine_0030)


def test_repository_parallel_heads_0088_0089_while_expand_runner_stops_at_0087() -> None:
    prev_cwd = os.getcwd()
    try:
        os.chdir(_REPO / "database")
        heads = set(ScriptDirectory("migrations").get_heads())
    finally:
        os.chdir(prev_cwd)
    assert heads == frozenset({"0092", "0096"})
    assert FINAL_TARGET == "0087"


def test_stage_a_hard_duplicate_gate_blocks_0031(
    ephemeral_legacy_migration_engine_0030: Engine,
) -> None:
    seed_duplicate_tenant_phone(ephemeral_legacy_migration_engine_0030)
    with pytest.raises(Exception):
        run_alembic(ephemeral_legacy_migration_engine_0030, STAGE_A_TARGET)


def test_stage_a_salla_backfill_collision_blocks_0031(
    ephemeral_legacy_migration_engine_0030: Engine,
) -> None:
    seed_duplicate_salla_backfill_collision(ephemeral_legacy_migration_engine_0030)
    with pytest.raises(Exception):
        run_alembic(ephemeral_legacy_migration_engine_0030, STAGE_A_TARGET)


def test_stage_a_operator_duplicate_preflight_on_pg(
    ephemeral_legacy_migration_engine_0030: Engine,
) -> None:
    seed_duplicate_tenant_phone(ephemeral_legacy_migration_engine_0030)
    with ephemeral_legacy_migration_engine_0030.connect() as conn:
        failure = stage_a_op.validate_duplicate_preflight(conn)
    assert failure is not None
    assert failure.error_class == "duplicate_preflight_failed"
    assert failure.stage == "tenant_phone_duplicates_present"


def test_stage_a_operator_salla_backfill_collision_preflight_on_pg(
    ephemeral_legacy_migration_engine_0030: Engine,
) -> None:
    seed_duplicate_salla_backfill_collision(ephemeral_legacy_migration_engine_0030)
    manifest, failure = stage_a_op.run_preflight(
        ephemeral_legacy_migration_engine_0030,
        env=_staging_env_for_pg(),
        require_identity=True,
        require_bootstrap_freeze=False,
    )
    assert manifest is None
    assert failure is not None
    assert failure.error_class == "duplicate_preflight_failed"
    assert failure.stage == "tenant_salla_backfill_collisions_present"


def test_stage_b_0064_variant_backfill_semantics(
    ephemeral_legacy_migration_engine_0030: Engine,
) -> None:
    run_alembic(ephemeral_legacy_migration_engine_0030, STAGE_A_TARGET)
    variant_product_id = seed_product_with_metadata_variants(ephemeral_legacy_migration_engine_0030)
    synthetic_product_id = seed_product_without_variants(ephemeral_legacy_migration_engine_0030)
    run_alembic(ephemeral_legacy_migration_engine_0030, STAGE_B_TARGET)

    with ephemeral_legacy_migration_engine_0030.connect() as conn:
        variant_rows = conn.execute(
            text("SELECT count(*)::int FROM product_variants WHERE product_id = :pid"),
            {"pid": variant_product_id},
        ).scalar_one()
        synthetic_default = conn.execute(
            text(
                """
                SELECT pv.is_default, p.default_variant_id, p.has_variants
                FROM products p
                JOIN product_variants pv ON pv.id = p.default_variant_id
                WHERE p.id = :pid
                """
            ),
            {"pid": synthetic_product_id},
        ).mappings().one()

    assert variant_rows >= 1
    assert synthetic_default["is_default"] is True
    assert synthetic_default["default_variant_id"] is not None
    assert synthetic_default["has_variants"] is False


def test_stage_c_duplicate_product_external_id_preflight_on_pg(
    ephemeral_legacy_migration_engine_0083: Engine,
) -> None:
    seed_duplicate_product_external_id(ephemeral_legacy_migration_engine_0083)
    with ephemeral_legacy_migration_engine_0083.connect() as conn:
        failure = stage_c_op.validate_duplicate_preflight(conn)
    assert failure is not None
    assert failure.stage == "tenant_product_external_id_duplicates_present"


def test_stage_c_operator_preflight_passes_at_0083(
    ephemeral_legacy_migration_engine_0083: Engine,
) -> None:
    manifest, failure = stage_c_op.run_preflight(
        ephemeral_legacy_migration_engine_0083,
        env=_staging_env_for_pg(),
        require_identity=True,
        require_bootstrap_freeze=False,
    )
    assert failure is None
    assert manifest is not None
    assert manifest["alembic_revision"] == STAGE_B_TARGET
    assert manifest["destructive_preflight_counts"]["duplicate_tenant_product_external_id_groups"] == 0


def test_stage_c_operator_run_invokes_upgrade_on_pg(
    ephemeral_legacy_migration_engine_0083: Engine,
) -> None:
    from types import SimpleNamespace

    env = _staging_env_for_pg()

    def fake_runner(cmd, **kwargs):
        run_alembic(ephemeral_legacy_migration_engine_0083, STAGE_C_TARGET)
        return SimpleNamespace(returncode=0, stdout="", stderr="")

    manifest, failure = stage_c_op.run_controlled_migration(
        ephemeral_legacy_migration_engine_0083,
        timeout_sec=1800,
        env=env,
        alembic_runner=fake_runner,
    )
    assert failure is None
    assert manifest is not None
    assert manifest["alembic_revision"] == FINAL_TARGET
    assert_schema_at_0087_expand(ephemeral_legacy_migration_engine_0083)
    assert_products_external_id_index_valid(ephemeral_legacy_migration_engine_0083)

    with ephemeral_legacy_migration_engine_0083.connect() as conn:
        rev = conn.execute(text("SELECT version_num FROM alembic_version")).scalar_one()
    assert rev == FINAL_TARGET
    assert rev != "0089"

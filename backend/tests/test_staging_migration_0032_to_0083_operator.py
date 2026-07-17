"""Unit tests for guarded staging migration 0032→0083 operator."""
from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest
from sqlalchemy import create_engine, text
from sqlalchemy.pool import StaticPool

_REPO = Path(__file__).resolve().parents[2]
for entry in (str(_REPO), str(_REPO / "backend"), str(_REPO / "database")):
    if entry not in sys.path:
        sys.path.insert(0, entry)

from scripts.operators import staging_migration_0032_to_0083 as op  # noqa: E402
from scripts.operators.schema_fingerprint import (  # noqa: E402
    SCHEMA_FINGERPRINT_VERSION,
)
from scripts.operators.staging_migration_0032_to_0083_contract import (  # noqa: E402
    BASE_REVISION,
    CONFIRMATION_ENV,
    CONFIRMATION_TOKEN,
    TARGET_REVISION,
)
from scripts.operators import staging_migration_operator_gates as gates  # noqa: E402
from tests.legacy_migration_drift_0032_0083_postgres_fixtures import (  # noqa: E402
    REQUIRED_COLUMNS as FIXTURE_COLUMNS,
    REQUIRED_INDEXES as FIXTURE_INDEXES,
    REQUIRED_TABLES as FIXTURE_TABLES,
    TARGET_REVISION as FIXTURE_TARGET,
)


def _staging_env(**overrides: str) -> dict[str, str]:
    base = {
        "RAILWAY_PROJECT_NAME": "desirable-growth",
        "RAILWAY_ENVIRONMENT_NAME": "staging",
        "NAHLA_SKIP_DB_BOOTSTRAP": "1",
        CONFIRMATION_ENV: CONFIRMATION_TOKEN,
        "DATABASE_URL": (
            "postgresql+psycopg2://operator:password@"
            "postgres-staging.railway.internal:5432/nahla"
        ),
    }
    base.update(overrides)
    return base


def _sqlite_engine_with_revision(revision: str) -> object:
    engine = create_engine(
        "sqlite+pysqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    with engine.begin() as conn:
        conn.execute(text("CREATE TABLE alembic_version (version_num VARCHAR)"))
        conn.execute(
            text("INSERT INTO alembic_version (version_num) VALUES (:rev)"),
            {"rev": revision},
        )
    return engine


def test_contract_matches_legacy_fixtures() -> None:
    assert TARGET_REVISION == FIXTURE_TARGET == "0083"
    assert FIXTURE_TABLES == op.REQUIRED_TABLES
    assert FIXTURE_INDEXES == op.REQUIRED_INDEXES
    assert FIXTURE_COLUMNS == op.REQUIRED_COLUMNS


@pytest.mark.parametrize(
    "env,expected_stage",
    [
        ({}, "staging_project_missing"),
        ({"RAILWAY_PROJECT_NAME": "wrong"}, "staging_project_mismatch"),
        (
            {"RAILWAY_PROJECT_NAME": "desirable-growth"},
            "staging_environment_missing",
        ),
        (
            {
                "RAILWAY_PROJECT_NAME": "desirable-growth",
                "RAILWAY_ENVIRONMENT_NAME": "production",
            },
            "staging_environment_mismatch",
        ),
    ],
)
def test_identity_rejection(env: dict[str, str], expected_stage: str) -> None:
    failure = op.validate_staging_identity(env)
    assert failure is not None
    assert failure.error_class == "identity_rejected"
    assert failure.stage == expected_stage


def test_bootstrap_freeze_required() -> None:
    env = _staging_env()
    del env["NAHLA_SKIP_DB_BOOTSTRAP"]
    failure = op.validate_bootstrap_freeze(env)
    assert failure is not None
    assert failure.error_class == "bootstrap_freeze_missing"


def test_confirmation_token_exact_match() -> None:
    env = _staging_env(**{CONFIRMATION_ENV: "wrong"})
    failure = op.validate_confirmation(env)
    assert failure is not None
    assert failure.error_class == "confirmation_missing"


def test_wrong_start_revision_rejected() -> None:
    engine = _sqlite_engine_with_revision("0033")
    with engine.connect() as conn:
        failure = gates.validate_start_revision(
            conn,
            base_revision=BASE_REVISION,
            wrong_stage="start_revision_not_0032",
        )
    assert failure is not None
    assert failure.error_class == "wrong_revision"
    assert failure.stage == "start_revision_not_0032"


def test_upgrade_command_fixed_target_no_head() -> None:
    cmd = op.build_alembic_upgrade_command("python")
    assert cmd == ["python", "-m", "alembic", "upgrade", "0083"]
    assert "head" not in " ".join(cmd)
    op.assert_upgrade_command_safe(cmd)


def test_upgrade_command_rejects_head() -> None:
    with pytest.raises(ValueError, match="head"):
        op.assert_upgrade_command_safe(["python", "-m", "alembic", "upgrade", "head"])


def test_preflight_counts_include_stage_b_drift_surfaces() -> None:
    engine = _sqlite_engine_with_revision(BASE_REVISION)
    with engine.begin() as conn:
        conn.execute(
            text(
                """
                CREATE TABLE cross_merchant_signals (id INTEGER PRIMARY KEY)
                """
            )
        )
        conn.execute(
            text(
                """
                CREATE TABLE learned_sales_policies (id INTEGER PRIMARY KEY)
                """
            )
        )
        conn.execute(
            text(
                """
                CREATE TABLE product_group_items (id INTEGER PRIMARY KEY)
                """
            )
        )
        conn.execute(text("CREATE TABLE products (id INTEGER PRIMARY KEY, metadata TEXT)"))

    with engine.connect() as conn:
        counts = op.collect_destructive_preflight_counts(conn)

    assert counts["cross_merchant_signals_table_preexisting"] == 1
    assert counts["learned_sales_policies_table_preexisting"] == 1
    assert counts["product_group_items_table_preexisting"] == 1
    assert "stage_b_catalog_missing_index_count" in counts
    assert "stage_b_catalog_missing_unique_constraint_count" in counts
    assert "phone" not in json.dumps(counts).lower()
    assert "email" not in json.dumps(counts).lower()


def test_manifest_contains_no_pii_keys() -> None:
    engine = _sqlite_engine_with_revision(BASE_REVISION)
    fingerprint = {
        "schema_fingerprint_version": SCHEMA_FINGERPRINT_VERSION,
        "public_table_count": 2,
        "schema_fingerprint": "a" * 64,
        "schema_fingerprint_display": "a" * 16,
    }
    with (
        patch.object(op, "compute_public_schema_fingerprint", return_value=fingerprint),
        patch.object(gates, "read_alembic_revision", return_value=BASE_REVISION),
    ):
        manifest, failure = op.run_preflight(
            engine,
            env=_staging_env(),
            require_identity=True,
            require_bootstrap_freeze=False,
        )
    assert failure is None
    assert manifest is not None
    dumped = json.dumps(manifest).lower()
    for forbidden in ("phone", "email", "customer_name", "address"):
        assert forbidden not in dumped


def test_run_controlled_migration_rejects_post_schema_mismatch() -> None:
    engine = MagicMock()
    conn = MagicMock()
    engine.connect.return_value.__enter__.return_value = conn
    fingerprint = {
        "schema_fingerprint_version": SCHEMA_FINGERPRINT_VERSION,
        "public_table_count": 5,
        "schema_fingerprint": "c" * 64,
        "schema_fingerprint_display": "c" * 16,
    }
    with (
        patch.object(op, "run_preflight", return_value=({}, None)),
        patch.object(op, "execute_alembic_upgrade", return_value={
            "outcome": "success", "error_class": "none", "stage": "alembic_upgrade",
        }),
        patch.object(gates, "validate_post_success_revision", return_value=None),
        patch.object(gates, "read_alembic_revision", return_value=TARGET_REVISION),
        patch.object(op, "compute_public_schema_fingerprint", return_value=fingerprint),
        patch.object(op, "collect_destructive_preflight_counts", return_value={
            "cross_merchant_signals_table_preexisting": 0,
            "learned_sales_policies_table_preexisting": 0,
            "product_variants_table_preexisting": 0,
            "products_has_variants_column_preexisting": 0,
            "products_default_variant_id_column_preexisting": 0,
            "product_groups_table_preexisting": 0,
            "product_group_items_table_preexisting": 0,
            "product_relations_table_preexisting": 0,
            "product_rankings_table_preexisting": 0,
            "stage_b_catalog_missing_index_count": 0,
            "stage_b_catalog_missing_unique_constraint_count": 0,
            "total_products_count": 0,
            "products_with_metadata_variants_array": 0,
            "products_without_variant_rows": 0,
        }),
        patch.object(op, "validate_post_success_schema", return_value={
            "schema_ok": False,
            "missing_tables": ["product_group_items"],
            "missing_columns": [],
            "missing_indexes": [],
        }),
    ):
        manifest, failure = op.run_controlled_migration(
            engine, timeout_sec=3600, env=_staging_env(),
        )
    assert manifest is None
    assert failure == op.GateFailure("post_validation_failed", "schema_metadata_incomplete")


def test_migration_timeout_safe_outcome() -> None:
    def timeout_run(*_a, **_k):
        raise subprocess.TimeoutExpired(cmd=["alembic"], timeout=1)

    outcome = op.execute_alembic_upgrade(
        timeout_sec=1, env=_staging_env(), runner=timeout_run,
    )
    assert outcome["outcome"] == "timeout"
    assert outcome["error_class"] == "migration_timeout"
    assert "traceback" not in json.dumps(outcome).lower()


def test_migration_nonzero_exit_safe_outcome() -> None:
    def fail_run(*_a, **_k):
        return SimpleNamespace(returncode=1, stdout="DETAIL: secret row", stderr="")

    outcome = op.execute_alembic_upgrade(
        timeout_sec=30, env=_staging_env(), runner=fail_run,
    )
    assert outcome["outcome"] == "failed"
    assert outcome["error_class"] == "migration_nonzero_exit"
    assert "secret" not in json.dumps(outcome)

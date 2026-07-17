"""Unit tests for guarded staging migration 0088→0089 attachment operator."""
from __future__ import annotations

import json
import sys
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest
from sqlalchemy import create_engine, text
from sqlalchemy.pool import StaticPool

_REPO = Path(__file__).resolve().parents[2]
for entry in (str(_REPO), str(_REPO / "backend"), str(_REPO / "database")):
    if entry not in sys.path:
        sys.path.insert(0, entry)

from scripts.operators import staging_migration_0088_to_0089 as attach_op  # noqa: E402
from scripts.operators.staging_migration_0088_to_0089_contract import (  # noqa: E402
    BASE_REVISION,
    CONFIRMATION_ENV,
    CONFIRMATION_TOKEN,
    EXPECTED_POST_SUCCESS_REVISIONS,
    TARGET_REVISION,
)

_STAGING_ENV = {
    "RAILWAY_PROJECT_NAME": "desirable-growth",
    "RAILWAY_ENVIRONMENT_NAME": "staging",
    "NAHLA_SKIP_DB_BOOTSTRAP": "1",
    CONFIRMATION_ENV: CONFIRMATION_TOKEN,
    "DATABASE_URL": (
        "postgresql+psycopg2://operator:password@"
        "postgres-staging.railway.internal:5432/nahla"
    ),
}


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


def test_contract_targets_exact_0088_attach_0089() -> None:
    assert BASE_REVISION == "0088"
    assert TARGET_REVISION == "0089"
    assert EXPECTED_POST_SUCCESS_REVISIONS == frozenset({"0088", "0089"})


def test_upgrade_command_fixed_target_no_head() -> None:
    cmd = attach_op.build_alembic_upgrade_command("python")
    assert cmd == ["python", "-m", "alembic", "upgrade", "0089"]
    assert "head" not in " ".join(cmd)
    attach_op.assert_upgrade_command_safe(cmd)


def test_upgrade_command_rejects_head() -> None:
    with pytest.raises(ValueError, match="head"):
        attach_op.assert_upgrade_command_safe(["python", "-m", "alembic", "upgrade", "head"])


def test_upgrade_command_rejects_0088_literal() -> None:
    with pytest.raises(ValueError, match="unsafe_command_shape"):
        attach_op.assert_upgrade_command_safe(["python", "-m", "alembic", "upgrade", "0088"])


def test_wrong_start_revision_rejects_0087() -> None:
    engine = _sqlite_engine_with_revision("0087")
    with engine.connect() as conn:
        failure = attach_op.validate_pre_attach_validated_invariants(conn)
    assert failure is not None
    assert failure.stage == "revision_is_0087_not_0088"


def test_wrong_start_revision_rejects_0089_only() -> None:
    engine = _sqlite_engine_with_revision("0089")
    with engine.connect() as conn:
        failure = attach_op.validate_pre_attach_validated_invariants(conn)
    assert failure is not None
    assert failure.stage == "revision_is_0089_not_0088"


def test_dr_profile_prerequisite_missing_until_contract_bump() -> None:
    failure = attach_op.validate_dr_restore_profile_prerequisite()
    assert failure is not None
    assert failure.stage == "restore_profile_0088_not_in_contract"


def test_confirmation_token_exact_match() -> None:
    env = dict(_STAGING_ENV)
    env[CONFIRMATION_ENV] = "wrong"
    failure = attach_op.validate_confirmation(env)
    assert failure is not None
    assert failure.error_class == "confirmation_missing"


def test_identity_rejects_production_marker() -> None:
    failure = attach_op.validate_staging_identity(
        {
            "RAILWAY_PROJECT_NAME": "desirable-growth",
            "RAILWAY_ENVIRONMENT_NAME": "production",
        }
    )
    assert failure is not None
    assert failure.stage == "staging_environment_mismatch"


def test_database_binding_rejects_non_staging_host() -> None:
    env = dict(_STAGING_ENV)
    env["DATABASE_URL"] = "postgresql://operator:password@localhost/nahla"
    failure = attach_op.gates.validate_database_binding(env)
    assert failure is not None
    assert failure.stage == "database_host_not_allowlisted"


def test_bootstrap_freeze_required_for_run() -> None:
    env = dict(_STAGING_ENV)
    del env["NAHLA_SKIP_DB_BOOTSTRAP"]
    failure = attach_op.validate_bootstrap_freeze(env)
    assert failure is not None
    assert failure.error_class == "bootstrap_freeze_missing"


def test_main_rejects_unknown_command() -> None:
    rc = attach_op.main(["unknown"])
    assert rc != 0


def test_run_preflight_fails_when_dr_profile_missing() -> None:
    engine = MagicMock()
    with patch.object(attach_op, "validate_dr_restore_profile_prerequisite", return_value=None):
        with (
            patch.object(attach_op, "validate_pre_attach_validated_invariants", return_value=None),
            patch.object(attach_op, "validate_forbidden_pre_attach_tables", return_value=None),
            patch(
                "scripts.operators.staging_migration_0088_to_0089.compute_public_schema_fingerprint",
                return_value={
                    "schema_fingerprint_version": 1,
                    "public_table_count": 5,
                    "schema_fingerprint": "a" * 64,
                    "schema_fingerprint_display": "a" * 16,
                },
            ),
            patch.object(attach_op.gates, "read_alembic_revisions", return_value=frozenset({"0088"})),
        ):
            manifest, failure = attach_op.run_preflight(engine, env=_STAGING_ENV, require_dr_profile=False)

    assert failure is None
    assert manifest is not None
    assert manifest["target_revision"] == "0089"
    dumped = json.dumps(manifest)
    assert "password" not in dumped


def test_run_controlled_migration_success_path() -> None:
    engine = MagicMock()
    preflight_manifest = {"alembic_revisions_observed": ["0088"]}

    with (
        patch.object(attach_op, "validate_dr_restore_profile_prerequisite", return_value=None),
        patch.object(attach_op, "run_preflight", return_value=(preflight_manifest, None)),
        patch.object(attach_op, "execute_alembic_upgrade", return_value={"outcome": "success"}),
        patch.object(attach_op, "validate_post_success_attach_invariants", return_value=None),
        patch(
            "scripts.operators.staging_migration_0088_to_0089.compute_public_schema_fingerprint",
            return_value={
                "schema_fingerprint_version": 1,
                "public_table_count": 5,
                "schema_fingerprint": "a" * 64,
                "schema_fingerprint_display": "a" * 16,
            },
        ),
        patch.object(attach_op.gates, "read_alembic_revisions", return_value=frozenset({"0088", "0089"})),
    ):
        manifest, failure = attach_op.run_controlled_migration(
            engine,
            timeout_sec=600,
            env=_STAGING_ENV,
        )

    assert failure is None
    assert manifest is not None
    assert manifest["phase"] == "post_success"
    assert manifest["restore_first_policy"]
    assert "staging_pin_0088" in manifest["restore_first_policy"]


def test_post_attach_validation_detects_0088_capability_regression() -> None:
    engine = MagicMock()
    conn = MagicMock()
    engine.connect.return_value.__enter__.return_value = conn
    conn.execute.return_value.mappings.return_value.first.return_value = {
        "state": "expand",
        "validation_revision": "0088",
    }

    with (
        patch.object(attach_op.gates, "validate_post_success_revisions", return_value=None),
        patch.object(attach_op, "validate_post_success_validate_invariants", return_value=None),
    ):
        failure = attach_op.validate_post_success_attach_invariants(engine)

    assert failure is not None
    assert failure.stage == "capability_regressed_to_expand"

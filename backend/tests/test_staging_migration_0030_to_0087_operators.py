"""Unit tests for guarded staging migration 0030→0087 operator stages."""
from __future__ import annotations

import json
import subprocess
import sys
from contextlib import nullcontext
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest
from sqlalchemy import create_engine, text
from sqlalchemy.exc import OperationalError
from sqlalchemy.pool import StaticPool

_REPO = Path(__file__).resolve().parents[2]
for entry in (str(_REPO), str(_REPO / "backend"), str(_REPO / "database")):
    if entry not in sys.path:
        sys.path.insert(0, entry)

from scripts.operators import staging_migration_0030_to_0032 as stage_a  # noqa: E402
from scripts.operators import staging_migration_0032_to_0083 as stage_b  # noqa: E402
from scripts.operators import staging_migration_0083_to_0087 as stage_c  # noqa: E402
from scripts.operators.schema_fingerprint import SCHEMA_FINGERPRINT_VERSION  # noqa: E402
from scripts.operators.staging_migration_0030_to_0032_contract import (  # noqa: E402
    BASE_REVISION as A_BASE,
    CONFIRMATION_ENV as A_CONFIRM_ENV,
    CONFIRMATION_TOKEN as A_CONFIRM_TOKEN,
    REQUIRED_COLUMNS as A_REQUIRED_COLUMNS,
    REQUIRED_INDEXES as A_REQUIRED_INDEXES,
    REQUIRED_TABLES as A_REQUIRED_TABLES,
    TARGET_REVISION as A_TARGET,
)
from scripts.operators.staging_migration_0032_to_0083_contract import (  # noqa: E402
    BASE_REVISION as B_BASE,
    CONFIRMATION_ENV as B_CONFIRM_ENV,
    CONFIRMATION_TOKEN as B_CONFIRM_TOKEN,
    MAX_MIGRATION_TIMEOUT_SEC as B_MAX_TIMEOUT,
    MIN_MIGRATION_TIMEOUT_SEC as B_MIN_TIMEOUT,
    REQUIRED_COLUMNS as B_REQUIRED_COLUMNS,
    REQUIRED_INDEXES as B_REQUIRED_INDEXES,
    REQUIRED_TABLES as B_REQUIRED_TABLES,
    TARGET_REVISION as B_TARGET,
)
from scripts.operators.staging_migration_0083_to_0087_contract import (  # noqa: E402
    BASE_REVISION as C_BASE,
    CONFIRMATION_ENV as C_CONFIRM_ENV,
    CONFIRMATION_TOKEN as C_CONFIRM_TOKEN,
    MAX_MIGRATION_TIMEOUT_SEC as C_MAX_TIMEOUT,
    MIN_MIGRATION_TIMEOUT_SEC as C_MIN_TIMEOUT,
    PRODUCTS_TENANT_EXTERNAL_ID_INDEX,
    REPOSITORY_MERGED_BUT_OUT_OF_SCOPE_REVISIONS,
    REQUIRED_COLUMNS as C_REQUIRED_COLUMNS,
    REQUIRED_INDEXES as C_REQUIRED_INDEXES,
    REQUIRED_TABLES as C_REQUIRED_TABLES,
    TARGET_REVISION as C_TARGET,
)

STAGE_COMMAND_CASES = (
    (stage_a, A_BASE, A_TARGET, A_CONFIRM_ENV, A_CONFIRM_TOKEN),
    (stage_b, B_BASE, B_TARGET, B_CONFIRM_ENV, B_CONFIRM_TOKEN),
    (stage_c, C_BASE, C_TARGET, C_CONFIRM_ENV, C_CONFIRM_TOKEN),
)


def _staging_env(confirm_env: str, confirm_token: str, **overrides: str) -> dict[str, str]:
    base = {
        "RAILWAY_PROJECT_NAME": "desirable-growth",
        "RAILWAY_ENVIRONMENT_NAME": "staging",
        "NAHLA_SKIP_DB_BOOTSTRAP": "1",
        confirm_env: confirm_token,
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


def _fingerprint_stub() -> dict[str, str | int]:
    return {
        "schema_fingerprint_version": SCHEMA_FINGERPRINT_VERSION,
        "public_table_count": 5,
        "schema_fingerprint": "e" * 64,
        "schema_fingerprint_display": "e" * 16,
    }


def test_contract_matches_pg_fixtures() -> None:
    from tests.legacy_migration_drift_0030_0087_postgres_fixtures import (  # noqa: WPS433
        STAGE_A_TARGET,
        STAGE_B_TARGET,
        STAGE_C_TARGET,
    )

    assert A_TARGET == STAGE_A_TARGET == "0032"
    assert B_TARGET == STAGE_B_TARGET == "0083"
    assert C_TARGET == STAGE_C_TARGET == "0087"
    assert stage_a.REQUIRED_TABLES == A_REQUIRED_TABLES
    assert stage_a.REQUIRED_INDEXES == A_REQUIRED_INDEXES
    assert stage_a.REQUIRED_COLUMNS == A_REQUIRED_COLUMNS
    assert stage_b.REQUIRED_TABLES == B_REQUIRED_TABLES
    assert stage_b.REQUIRED_INDEXES == B_REQUIRED_INDEXES
    assert stage_b.REQUIRED_COLUMNS == B_REQUIRED_COLUMNS
    assert stage_c.REQUIRED_TABLES == C_REQUIRED_TABLES
    assert stage_c.REQUIRED_INDEXES == C_REQUIRED_INDEXES
    assert stage_c.REQUIRED_COLUMNS == C_REQUIRED_COLUMNS
    assert PRODUCTS_TENANT_EXTERNAL_ID_INDEX in C_REQUIRED_INDEXES["products"]


@pytest.mark.parametrize("module,base,target,confirm_env,confirm_token", STAGE_COMMAND_CASES)
def test_upgrade_command_fixed_target_no_head(module, base, target, confirm_env, confirm_token) -> None:
    cmd = module.build_alembic_upgrade_command("python")
    assert cmd == ["python", "-m", "alembic", "upgrade", target]
    assert "head" not in " ".join(cmd)
    module.assert_upgrade_command_safe(cmd)


@pytest.mark.parametrize("module,target", [(stage_a, A_TARGET), (stage_b, B_TARGET), (stage_c, C_TARGET)])
def test_upgrade_command_rejects_head(module, target) -> None:
    with pytest.raises(ValueError, match="head"):
        module.assert_upgrade_command_safe(["python", "-m", "alembic", "upgrade", "head"])


@pytest.mark.parametrize("module,target", [(stage_a, A_TARGET), (stage_b, B_TARGET), (stage_c, C_TARGET)])
def test_upgrade_command_rejects_non_literal_target(module, target) -> None:
    with pytest.raises(ValueError, match="unsafe_command_shape"):
        module.assert_upgrade_command_safe(["python", "-m", "alembic", "upgrade", "0089"])


@pytest.mark.parametrize(
    "module,confirm_env,confirm_token",
    [(stage_a, A_CONFIRM_ENV, A_CONFIRM_TOKEN), (stage_b, B_CONFIRM_ENV, B_CONFIRM_TOKEN), (stage_c, C_CONFIRM_ENV, C_CONFIRM_TOKEN)],
)
def test_confirmation_token_exact_match(module, confirm_env, confirm_token) -> None:
    failure = module.validate_confirmation(_staging_env(confirm_env, confirm_token, **{confirm_env: "wrong"}))
    assert failure is not None
    assert failure.error_class == "confirmation_missing"


def test_confirmation_tokens_distinct_per_stage() -> None:
    assert len({A_CONFIRM_TOKEN, B_CONFIRM_TOKEN, C_CONFIRM_TOKEN}) == 3
    assert len({A_CONFIRM_ENV, B_CONFIRM_ENV, C_CONFIRM_ENV}) == 3


@pytest.mark.parametrize(
    "module,confirm_env,confirm_token,env,expected_stage",
    [
        (stage_a, A_CONFIRM_ENV, A_CONFIRM_TOKEN, {}, "staging_project_missing"),
        (stage_b, B_CONFIRM_ENV, B_CONFIRM_TOKEN, {"RAILWAY_PROJECT_NAME": "wrong"}, "staging_project_mismatch"),
        (
            stage_c,
            C_CONFIRM_ENV,
            C_CONFIRM_TOKEN,
            {"RAILWAY_PROJECT_NAME": "desirable-growth", "RAILWAY_ENVIRONMENT_NAME": "production"},
            "staging_environment_mismatch",
        ),
        (
            stage_a,
            A_CONFIRM_ENV,
            A_CONFIRM_TOKEN,
            {
                "RAILWAY_PROJECT_NAME": "desirable-growth",
                "RAILWAY_ENVIRONMENT_NAME": "staging",
                "ENVIRONMENT": "production",
            },
            "production_marker_detected",
        ),
    ],
)
def test_identity_rejection(module, confirm_env, confirm_token, env, expected_stage) -> None:
    failure = module.validate_staging_identity(env)
    assert failure is not None
    assert failure.error_class == "identity_rejected"
    assert failure.stage == expected_stage


@pytest.mark.parametrize(
    "module,confirm_env,confirm_token,database_url,expected_stage",
    [
        (stage_a, A_CONFIRM_ENV, A_CONFIRM_TOKEN, "postgresql://operator:password@postgres-staging.railway.internal/nahla", None),
        (stage_b, B_CONFIRM_ENV, B_CONFIRM_TOKEN, "postgresql://operator:password@localhost/nahla", "database_host_not_allowlisted"),
        (stage_c, C_CONFIRM_ENV, C_CONFIRM_TOKEN, "postgresql://operator:password@postgres-prod.railway.internal/nahla", "database_host_production_marker"),
        (stage_a, A_CONFIRM_ENV, A_CONFIRM_TOKEN, "sqlite+pysqlite:///:memory:", "database_scheme_rejected"),
    ],
)
def test_database_binding_accepts_only_exact_staging_host(
    module, confirm_env, confirm_token, database_url, expected_stage,
) -> None:
    failure = module.gates.validate_database_binding(
        _staging_env(confirm_env, confirm_token, DATABASE_URL=database_url)
    )
    if expected_stage is None:
        assert failure is None
    else:
        assert failure is not None
        assert failure.error_class == "database_binding_rejected"
        assert failure.stage == expected_stage


@pytest.mark.parametrize(
    "module,confirm_env,confirm_token",
    [
        (stage_a, A_CONFIRM_ENV, A_CONFIRM_TOKEN),
        (stage_b, B_CONFIRM_ENV, B_CONFIRM_TOKEN),
        (stage_c, C_CONFIRM_ENV, C_CONFIRM_TOKEN),
    ],
)
def test_bootstrap_freeze_required(module, confirm_env, confirm_token) -> None:
    env = _staging_env(confirm_env, confirm_token)
    del env["NAHLA_SKIP_DB_BOOTSTRAP"]
    failure = module.validate_bootstrap_freeze(env)
    assert failure is not None
    assert failure.error_class == "bootstrap_freeze_missing"


@pytest.mark.parametrize(
    "module,base,wrong_revision",
    [(stage_a, A_BASE, "0025"), (stage_b, B_BASE, "0030"), (stage_c, C_BASE, "0080")],
)
def test_wrong_start_revision_rejected(module, base, wrong_revision) -> None:
    engine = _sqlite_engine_with_revision(wrong_revision)
    with engine.connect() as conn:
        failure = module.gates.validate_start_revision(
            conn,
            base_revision=base,
            wrong_stage=f"start_revision_not_{base}",
        )
    assert failure is not None
    assert failure.error_class == "wrong_revision"


def test_stage_b_timeout_bounded_policy() -> None:
    assert stage_b.validate_timeout_policy(B_MIN_TIMEOUT - 1) is not None
    assert stage_b.validate_timeout_policy(B_MAX_TIMEOUT + 1) is not None
    assert stage_b.validate_timeout_policy(1800) is None


def test_stage_c_timeout_bounded_policy() -> None:
    assert stage_c.validate_timeout_policy(C_MIN_TIMEOUT - 1) is not None
    assert stage_c.validate_timeout_policy(C_MAX_TIMEOUT + 1) is not None
    assert stage_c.validate_timeout_policy(1800) is None


def test_stage_c_documents_0089_merged_but_out_of_scope() -> None:
    assert "0089" in REPOSITORY_MERGED_BUT_OUT_OF_SCOPE_REVISIONS
    assert C_TARGET == "0087"


def test_stage_a_manifest_contains_no_pii_keys() -> None:
    engine = _sqlite_engine_with_revision(A_BASE)
    with (
        patch.object(stage_a, "validate_duplicate_preflight", return_value=None),
        patch.object(stage_a, "compute_public_schema_fingerprint", return_value=_fingerprint_stub()),
        patch.object(
            stage_a,
            "collect_destructive_preflight_counts",
            return_value={
                "duplicate_tenant_phone_groups": 0,
                "duplicate_tenant_salla_metadata_groups": 0,
                "duplicate_tenant_salla_backfill_groups": 0,
                "duplicate_tenant_normalized_phone_groups": 0,
            },
        ),
    ):
        manifest, failure = stage_a.run_preflight(engine, env=_staging_env(A_CONFIRM_ENV, A_CONFIRM_TOKEN))
    assert failure is None
    assert manifest is not None
    blob = json.dumps(manifest)
    for forbidden in ("customer_id", "email", "DATABASE_URL", "postgresql", "0500000001"):
        assert forbidden not in blob.lower()
    assert "duplicate_tenant_phone_groups" in blob


def test_stage_c_manifest_preserves_queried_pgcrypto_value() -> None:
    engine = _sqlite_engine_with_revision(C_BASE)
    with (
        patch.object(stage_c, "validate_catalog_audit_gate", return_value=None),
        patch.object(stage_c, "validate_duplicate_preflight", return_value=None),
        patch.object(stage_c, "validate_extension_availability", return_value=None),
        patch.object(stage_c, "compute_public_schema_fingerprint", return_value=_fingerprint_stub()),
        patch.object(
            stage_c,
            "collect_destructive_preflight_counts",
            return_value={
                "forbidden_a1_objects_present": 0,
                "external_customer_profiles_present": 0,
                "order_customer_identity_capability_state_present": 0,
                "conversation_a1_subject_bindings_present": 0,
                "duplicate_tenant_product_external_id_groups": 0,
                "pgcrypto_extension_available": 0,
            },
        ),
        patch.object(
            stage_c,
            "build_catalog_audit_manifest",
            return_value={
                "forbidden_a1_objects_present": 0,
                "catalog_audit_indicators": {},
            },
        ),
    ):
        manifest, failure = stage_c.run_preflight(engine, env=_staging_env(C_CONFIRM_ENV, C_CONFIRM_TOKEN))
    assert failure is None
    assert manifest is not None
    assert manifest["destructive_preflight_counts"]["pgcrypto_extension_available"] == 0
    assert manifest["extension_gate"]["pgcrypto_extension_available"] == 0


@pytest.mark.parametrize(
    "module,confirm_env,confirm_token,base",
    [
        (stage_a, A_CONFIRM_ENV, A_CONFIRM_TOKEN, A_BASE),
        (stage_b, B_CONFIRM_ENV, B_CONFIRM_TOKEN, B_BASE),
        (stage_c, C_CONFIRM_ENV, C_CONFIRM_TOKEN, C_BASE),
    ],
)
def test_run_aborts_without_confirmation(module, confirm_env, confirm_token, base) -> None:
    env = _staging_env(confirm_env, confirm_token)
    del env[confirm_env]
    manifest, failure = module.run_controlled_migration(
        _sqlite_engine_with_revision(base),
        timeout_sec=30,
        env=env,
        alembic_runner=MagicMock(),
    )
    assert manifest is None
    assert failure is not None
    assert failure.error_class == "confirmation_missing"


def test_stage_a_duplicate_preflight_operator_level() -> None:
    engine = _sqlite_engine_with_revision(A_BASE)
    failure_obj = stage_a.GateFailure("duplicate_preflight_failed", "tenant_phone_duplicates_present")
    with patch.object(stage_a, "validate_duplicate_preflight", return_value=failure_obj):
        manifest, failure = stage_a.run_preflight(engine, env=_staging_env(A_CONFIRM_ENV, A_CONFIRM_TOKEN))
    assert manifest is None
    assert failure == failure_obj


def test_stage_c_catalog_audit_gate_rejects_forbidden_objects() -> None:
    engine = _sqlite_engine_with_revision(C_BASE)
    failure_obj = stage_c.GateFailure("catalog_audit_rejected", "forbidden_a1_objects_present")
    with patch.object(stage_c, "validate_catalog_audit_gate", return_value=failure_obj):
        manifest, failure = stage_c.run_preflight(engine, env=_staging_env(C_CONFIRM_ENV, C_CONFIRM_TOKEN))
    assert manifest is None
    assert failure == failure_obj


def test_stage_c_duplicate_preflight_rejects_product_external_id_groups() -> None:
    engine = _sqlite_engine_with_revision(C_BASE)
    failure_obj = stage_c.GateFailure(
        "duplicate_preflight_failed",
        "tenant_product_external_id_duplicates_present",
    )
    with (
        patch.object(stage_c, "validate_catalog_audit_gate", return_value=None),
        patch.object(stage_c, "validate_duplicate_preflight", return_value=failure_obj),
    ):
        manifest, failure = stage_c.run_preflight(engine, env=_staging_env(C_CONFIRM_ENV, C_CONFIRM_TOKEN))
    assert manifest is None
    assert failure == failure_obj


@pytest.mark.parametrize(
    "module,confirm_env,confirm_token,target,wrong_stage",
    [
        (stage_a, A_CONFIRM_ENV, A_CONFIRM_TOKEN, A_TARGET, "revision_not_0032"),
        (stage_b, B_CONFIRM_ENV, B_CONFIRM_TOKEN, B_TARGET, "revision_not_0083"),
        (stage_c, C_CONFIRM_ENV, C_CONFIRM_TOKEN, C_TARGET, "revision_not_0087"),
    ],
)
def test_run_controlled_migration_rejects_post_revision_mismatch(
    module, confirm_env, confirm_token, target, wrong_stage,
) -> None:
    engine = MagicMock()
    with (
        patch.object(module, "run_preflight", return_value=({}, None)),
        patch.object(module, "execute_alembic_upgrade", return_value={
            "outcome": "success", "error_class": "none", "stage": "alembic_upgrade",
        }),
        patch.object(
            module.gates,
            "validate_post_success_revision",
            return_value=module.GateFailure("post_validation_failed", wrong_stage),
        ),
    ):
        manifest, failure = module.run_controlled_migration(
            engine, timeout_sec=1800, env=_staging_env(confirm_env, confirm_token),
        )
    assert manifest is None
    assert failure == module.GateFailure("post_validation_failed", wrong_stage)


@pytest.mark.parametrize(
    "module,confirm_env,confirm_token,target",
    [
        (stage_a, A_CONFIRM_ENV, A_CONFIRM_TOKEN, A_TARGET),
        (stage_b, B_CONFIRM_ENV, B_CONFIRM_TOKEN, B_TARGET),
        (stage_c, C_CONFIRM_ENV, C_CONFIRM_TOKEN, C_TARGET),
    ],
)
def test_run_controlled_migration_rejects_post_schema_mismatch(
    module, confirm_env, confirm_token, target,
) -> None:
    engine = MagicMock()
    conn = MagicMock()
    engine.connect.return_value.__enter__.return_value = conn
    expand_patch = (
        patch.object(module, "validate_post_success_expand_invariants", return_value=None)
        if module is stage_c
        else nullcontext()
    )
    with (
        patch.object(module, "run_preflight", return_value=({}, None)),
        patch.object(module, "execute_alembic_upgrade", return_value={
            "outcome": "success", "error_class": "none", "stage": "alembic_upgrade",
        }),
        patch.object(module.gates, "validate_post_success_revision", return_value=None),
        patch.object(module.gates, "read_alembic_revision", return_value=target),
        patch.object(module, "compute_public_schema_fingerprint", return_value=_fingerprint_stub()),
        patch.object(module, "collect_destructive_preflight_counts", return_value={}),
        patch.object(module, "validate_post_success_schema", return_value={
            "schema_ok": False,
            "missing_tables": ["missing"],
            "missing_columns": [],
            "missing_indexes": [],
        }),
        expand_patch,
    ):
        manifest, failure = module.run_controlled_migration(
            engine, timeout_sec=1800, env=_staging_env(confirm_env, confirm_token),
        )
    assert manifest is None
    assert failure == module.GateFailure("post_validation_failed", "schema_metadata_incomplete")


def test_migration_timeout_safe_outcome() -> None:
    def timeout_run(*_a, **_k):
        raise subprocess.TimeoutExpired(cmd=["alembic"], timeout=1)

    outcome = stage_b.execute_alembic_upgrade(
        timeout_sec=600,
        env=_staging_env(B_CONFIRM_ENV, B_CONFIRM_TOKEN),
        runner=timeout_run,
    )
    assert outcome["outcome"] == "timeout"
    assert outcome["error_class"] == "migration_timeout"
    assert "traceback" not in json.dumps(outcome).lower()


def test_preflight_database_operational_error_is_safe() -> None:
    engine = MagicMock()
    engine.connect.side_effect = OperationalError("SELECT secret", {}, RuntimeError("secret"))
    manifest, failure = stage_b.run_preflight(engine, env=_staging_env(B_CONFIRM_ENV, B_CONFIRM_TOKEN))
    assert manifest is None
    assert failure == stage_b.GateFailure("database_operation_failed", "preflight_database")


def test_preflight_cli_safe_error_on_identity_failure(capsys) -> None:
    code = stage_a.main(["preflight"])
    payload = json.loads(capsys.readouterr().out)
    assert code == 1
    assert payload["outcome"] == "failed"
    assert payload["error_class"] == "identity_rejected"
    assert "postgresql" not in json.dumps(payload).lower()


def test_execute_uses_list_args_not_shell() -> None:
    observed: dict[str, object] = {}

    def capture_run(cmd, **kwargs):
        observed["cmd"] = cmd
        observed["shell"] = kwargs.get("shell")
        return SimpleNamespace(returncode=0, stdout="", stderr="")

    stage_a.execute_alembic_upgrade(
        timeout_sec=10,
        env=_staging_env(A_CONFIRM_ENV, A_CONFIRM_TOKEN),
        runner=capture_run,
    )
    assert observed["shell"] is False

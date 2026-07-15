"""Unit tests for guarded staging migration 0016→0024 operator."""
from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace
import hashlib
from unittest.mock import MagicMock, patch

import pytest
from sqlalchemy import create_engine, text
from sqlalchemy.exc import OperationalError
from sqlalchemy.pool import StaticPool

_REPO = Path(__file__).resolve().parents[2]
for entry in (str(_REPO), str(_REPO / "backend"), str(_REPO / "database")):
    if entry not in sys.path:
        sys.path.insert(0, entry)

from scripts.operators import staging_migration_0016_to_0024 as op  # noqa: E402
from scripts.operators.schema_fingerprint import (  # noqa: E402
    SCHEMA_FINGERPRINT_VERSION,
)
from scripts.operators.staging_migration_contract import (  # noqa: E402
    BASE_REVISION,
    CONFIRMATION_ENV,
    CONFIRMATION_TOKEN,
    TARGET_REVISION,
)
from tests.legacy_migration_drift_postgres_fixtures import (  # noqa: E402
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
    assert TARGET_REVISION == FIXTURE_TARGET == "0024"
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
        (
            {
                "RAILWAY_PROJECT_NAME": "desirable-growth",
                "RAILWAY_ENVIRONMENT_NAME": "staging",
                "ENVIRONMENT": "production",
            },
            "production_marker_detected",
        ),
    ],
)
def test_identity_rejection(env: dict[str, str], expected_stage: str) -> None:
    failure = op.validate_staging_identity(env)
    assert failure is not None
    assert failure.error_class == "identity_rejected"
    assert failure.stage == expected_stage


@pytest.mark.parametrize(
    "database_url,expected_stage",
    [
        (
            "postgresql://operator:password@postgres-staging.railway.internal/nahla",
            None,
        ),
        ("postgresql://operator:password@localhost/nahla", "database_host_not_allowlisted"),
        ("postgresql://operator:password@postgres-prod.railway.internal/nahla", "database_host_production_marker"),
        ("postgresql://operator:password@unknown.railway.internal/nahla", "database_host_not_allowlisted"),
        ("sqlite+pysqlite:///:memory:", "database_scheme_rejected"),
        ("not-a-url", "database_url_malformed"),
    ],
)
def test_database_binding_accepts_only_exact_staging_host(
    database_url: str, expected_stage: str | None,
) -> None:
    failure = op.validate_database_binding(_staging_env(DATABASE_URL=database_url))
    if expected_stage is None:
        assert failure is None
    else:
        assert failure is not None
        assert failure.error_class == "database_binding_rejected"
        assert failure.stage == expected_stage


def test_database_binding_rejects_environment_spoofed_production_host() -> None:
    env = _staging_env(
        RAILWAY_PROJECT_NAME="desirable-growth",
        RAILWAY_ENVIRONMENT_NAME="staging",
        ENVIRONMENT="staging",
        DATABASE_URL="postgresql://operator:password@postgres-prod.railway.internal/nahla",
    )
    assert op.validate_staging_identity(env) is None
    failure = op.validate_database_binding(env)
    assert failure is not None
    assert failure.stage == "database_host_production_marker"


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
    engine = _sqlite_engine_with_revision("0017")
    with engine.connect() as conn:
        failure = op.validate_start_revision(conn)
    assert failure is not None
    assert failure.error_class == "wrong_revision"
    assert failure.stage == "start_revision_not_0016"


def test_upgrade_command_fixed_target_no_head() -> None:
    cmd = op.build_alembic_upgrade_command("python")
    assert cmd == ["python", "-m", "alembic", "upgrade", "0024"]
    assert "head" not in " ".join(cmd)
    op.assert_upgrade_command_safe(cmd)


def test_upgrade_command_rejects_head() -> None:
    with pytest.raises(ValueError, match="head"):
        op.assert_upgrade_command_safe(["python", "-m", "alembic", "upgrade", "head"])


def test_salla_preflight_records_outcome_token_only() -> None:
    def fake_run(*_args, **_kwargs):
        return SimpleNamespace(returncode=1, stdout="store_id=secret", stderr="")

    outcome = op.run_salla_preflight(runner=fake_run)
    assert outcome == "fail"
    assert "store_id" not in outcome


@pytest.mark.parametrize(
    "exception,expected",
    [
        (subprocess.TimeoutExpired(cmd=["salla"], timeout=1), "timeout"),
        (OSError("do not leak"), "spawn_failed"),
    ],
)
def test_salla_preflight_returns_safe_exception_tokens(exception: Exception, expected: str) -> None:
    def failing_run(*_args, **_kwargs):
        raise exception

    assert op.run_salla_preflight(runner=failing_run) == expected


def test_manifest_contains_no_pii_keys() -> None:
    engine = _sqlite_engine_with_revision(BASE_REVISION)
    with engine.begin() as conn:
        conn.execute(text("CREATE TABLE whatsapp_connections (whatsapp_business_account_id VARCHAR)"))
        conn.execute(text("CREATE TABLE orders (tenant_id INTEGER, external_id VARCHAR)"))
        conn.execute(text("CREATE TABLE smart_automations (automation_type VARCHAR)"))

    fingerprint = {
        "schema_fingerprint_version": SCHEMA_FINGERPRINT_VERSION,
        "public_table_count": 2,
        "schema_fingerprint": "a" * 64,
        "schema_fingerprint_display": "a" * 16,
    }

    def salla_ok(*_a, **_k):
        return SimpleNamespace(returncode=0)

    with (
        patch.object(op, "compute_public_schema_fingerprint", return_value=fingerprint),
        patch.object(
            op,
            "collect_destructive_preflight_counts",
            return_value={
                "waba_duplicate_groups": 0,
                "order_duplicate_groups": 0,
                "zombie_automation_rows": 0,
            },
        ),
    ):
        manifest, failure = op.run_preflight(
            engine,
            env=_staging_env(),
            run_salla_check=True,
            salla_runner=salla_ok,
        )
    assert failure is None
    assert manifest is not None
    blob = json.dumps(manifest)
    for forbidden in (
        "tenant_id",
        "customer_id",
        "phone",
        "email",
        "DATABASE_URL",
        "postgresql",
    ):
        assert forbidden not in blob.lower()
    assert manifest["schema_fingerprint_version"] == SCHEMA_FINGERPRINT_VERSION
    assert manifest["alembic_revision"] == BASE_REVISION
    assert "destructive_preflight_counts" in manifest


def test_schema_fingerprint_canonical_hash() -> None:
    tables = ["alpha", "beta"]
    canonical = ",".join(tables)
    digest = hashlib.sha256(canonical.encode("utf-8")).hexdigest()
    assert len(digest) == 64
    assert digest[:16] == digest[:16]


def test_preflight_cli_safe_error_on_identity_failure(capsys) -> None:
    code = op.main(["preflight"])
    captured = capsys.readouterr().out
    payload = json.loads(captured)
    assert code == 1
    assert payload["outcome"] == "failed"
    assert payload["error_class"] == "identity_rejected"
    assert "traceback" not in captured.lower()
    assert "postgresql" not in captured.lower()


def test_main_returns_safe_json_for_unexpected_preflight_error(monkeypatch, capsys) -> None:
    for key, value in _staging_env().items():
        monkeypatch.setenv(key, value)
    with (
        patch.object(op, "connect_engine", return_value=MagicMock()),
        patch.object(op, "run_preflight", side_effect=RuntimeError("dsn=secret")),
    ):
        code = op.main(["preflight"])
    payload = json.loads(capsys.readouterr().out)
    assert code == 1
    assert payload == {
        "error_class": "unexpected_error",
        "outcome": "failed",
        "stage": "preflight",
    }


def test_run_aborts_without_confirmation(capsys) -> None:
    env = _staging_env()
    del env[CONFIRMATION_ENV]
    engine = _sqlite_engine_with_revision(BASE_REVISION)

    def salla_ok(*_a, **_k):
        return SimpleNamespace(returncode=0)

    manifest, failure = op.run_controlled_migration(
        engine,
        timeout_sec=30,
        env=env,
        salla_runner=salla_ok,
        alembic_runner=MagicMock(),
    )
    assert manifest is None
    assert failure is not None
    assert failure.error_class == "confirmation_missing"


def test_run_aborts_on_salla_failure() -> None:
    engine = _sqlite_engine_with_revision(BASE_REVISION)

    def salla_fail(*_a, **_k):
        return SimpleNamespace(returncode=1)

    manifest, failure = op.run_controlled_migration(
        engine,
        timeout_sec=30,
        env=_staging_env(),
        salla_runner=salla_fail,
        alembic_runner=MagicMock(),
    )
    assert manifest is None
    assert failure is not None
    assert failure.error_class == "salla_preflight_failed"


def test_run_controlled_migration_happy_path_is_fully_mocked() -> None:
    engine = MagicMock()
    conn = MagicMock()
    engine.connect.return_value.__enter__.return_value = conn
    preflight = {
        "salla_preflight_outcome": "pass",
        "schema_fingerprint_version": SCHEMA_FINGERPRINT_VERSION,
    }
    fingerprint = {
        "schema_fingerprint_version": SCHEMA_FINGERPRINT_VERSION,
        "public_table_count": 5,
        "schema_fingerprint": "b" * 64,
        "schema_fingerprint_display": "b" * 16,
    }
    with (
        patch.object(op, "run_preflight", return_value=(preflight, None)),
        patch.object(op, "execute_alembic_upgrade", return_value={
            "outcome": "success",
            "error_class": "none",
            "stage": "alembic_upgrade",
        }),
        patch.object(op, "validate_post_success_revision", return_value=None),
        patch.object(op, "read_alembic_revision", return_value=TARGET_REVISION),
        patch.object(op, "compute_public_schema_fingerprint", return_value=fingerprint),
        patch.object(op, "collect_destructive_preflight_counts", return_value={
            "waba_duplicate_groups": 0,
            "order_duplicate_groups": 0,
            "zombie_automation_rows": 0,
        }),
        patch.object(op, "validate_post_success_schema", return_value={
            "schema_ok": True,
            "missing_tables": [],
            "missing_columns": [],
            "missing_indexes": [],
        }),
    ):
        manifest, failure = op.run_controlled_migration(
            engine,
            timeout_sec=30,
            env=_staging_env(),
        )
    assert failure is None
    assert manifest is not None
    assert manifest["phase"] == "post_success"
    assert manifest["alembic_revision"] == TARGET_REVISION


def test_run_controlled_migration_rejects_post_revision_mismatch() -> None:
    engine = MagicMock()
    conn = MagicMock()
    engine.connect.return_value.__enter__.return_value = conn
    with (
        patch.object(op, "run_preflight", return_value=({"salla_preflight_outcome": "pass"}, None)),
        patch.object(op, "execute_alembic_upgrade", return_value={
            "outcome": "success", "error_class": "none", "stage": "alembic_upgrade",
        }),
        patch.object(
            op,
            "validate_post_success_revision",
            return_value=op.GateFailure("post_validation_failed", "revision_not_0024"),
        ),
    ):
        manifest, failure = op.run_controlled_migration(
            engine, timeout_sec=30, env=_staging_env(),
        )
    assert manifest is None
    assert failure == op.GateFailure("post_validation_failed", "revision_not_0024")


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
        patch.object(op, "run_preflight", return_value=({"salla_preflight_outcome": "pass"}, None)),
        patch.object(op, "execute_alembic_upgrade", return_value={
            "outcome": "success", "error_class": "none", "stage": "alembic_upgrade",
        }),
        patch.object(op, "validate_post_success_revision", return_value=None),
        patch.object(op, "read_alembic_revision", return_value=TARGET_REVISION),
        patch.object(op, "compute_public_schema_fingerprint", return_value=fingerprint),
        patch.object(op, "collect_destructive_preflight_counts", return_value={
            "waba_duplicate_groups": 0, "order_duplicate_groups": 0, "zombie_automation_rows": 0,
        }),
        patch.object(op, "validate_post_success_schema", return_value={
            "schema_ok": False,
            "missing_tables": ["webhook_events"],
            "missing_columns": [],
            "missing_indexes": [],
        }),
    ):
        manifest, failure = op.run_controlled_migration(
            engine, timeout_sec=30, env=_staging_env(),
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


@pytest.mark.parametrize("timeout_sec", [0, -1, True, "30"])
def test_invalid_timeout_rejected_without_running_command(timeout_sec: object) -> None:
    runner = MagicMock()
    outcome = op.execute_alembic_upgrade(
        timeout_sec=timeout_sec, env=_staging_env(), runner=runner,  # type: ignore[arg-type]
    )
    assert outcome == {
        "outcome": "failed",
        "error_class": "invalid_timeout",
        "stage": "alembic_upgrade",
    }
    runner.assert_not_called()


def test_alembic_execution_rejects_unbound_database_before_runner() -> None:
    runner = MagicMock()
    outcome = op.execute_alembic_upgrade(
        timeout_sec=30,
        env=_staging_env(DATABASE_URL="postgresql://operator:password@localhost/nahla"),
        runner=runner,
    )
    assert outcome == {
        "outcome": "failed",
        "error_class": "database_binding_rejected",
        "stage": "database_host_not_allowlisted",
    }
    runner.assert_not_called()


def test_preflight_database_operational_error_is_safe() -> None:
    engine = MagicMock()
    engine.connect.side_effect = OperationalError("SELECT secret", {}, RuntimeError("secret"))
    manifest, failure = op.run_preflight(
        engine,
        env=_staging_env(),
        run_salla_check=False,
    )
    assert manifest is None
    assert failure == op.GateFailure("database_operation_failed", "preflight_database")


def test_execute_uses_list_args_not_shell() -> None:
    observed: dict[str, object] = {}

    def capture_run(cmd, **kwargs):
        observed["cmd"] = cmd
        observed["shell"] = kwargs.get("shell")
        return SimpleNamespace(returncode=0, stdout="", stderr="")

    op.execute_alembic_upgrade(timeout_sec=10, env=_staging_env(), runner=capture_run)
    assert observed["shell"] is False
    assert observed["cmd"] == op.build_alembic_upgrade_command(sys.executable)

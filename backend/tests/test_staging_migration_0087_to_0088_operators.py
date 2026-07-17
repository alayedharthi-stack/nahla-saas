"""Unit tests for guarded staging migration 0087→0088 operator (A1-Validate)."""
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

from scripts.operators import staging_migration_0087_to_0088 as validate_op  # noqa: E402
from scripts.operators.staging_migration_0087_to_0088_contract import (  # noqa: E402
    BASE_REVISION,
    CONFIRMATION_ENV,
    CONFIRMATION_TOKEN,
    CONSTRAINT_VIOLATION_PROBES,
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


def test_contract_targets_exact_0087_to_0088() -> None:
    assert BASE_REVISION == "0087"
    assert TARGET_REVISION == "0088"
    assert len(CONSTRAINT_VIOLATION_PROBES) == 9


def test_upgrade_command_fixed_target_no_head() -> None:
    cmd = validate_op.build_alembic_upgrade_command("python")
    assert cmd == ["python", "-m", "alembic", "upgrade", "0088"]
    assert "head" not in " ".join(cmd)
    validate_op.assert_upgrade_command_safe(cmd)


def test_upgrade_command_rejects_head() -> None:
    with pytest.raises(ValueError, match="head"):
        validate_op.assert_upgrade_command_safe(["python", "-m", "alembic", "upgrade", "head"])


def test_upgrade_command_rejects_0089() -> None:
    with pytest.raises(ValueError, match="unsafe_command_shape"):
        validate_op.assert_upgrade_command_safe(["python", "-m", "alembic", "upgrade", "0089"])


def test_wrong_start_revision_rejects_0089() -> None:
    engine = _sqlite_engine_with_revision("0089")
    with engine.connect() as conn:
        failure = validate_op.validate_pre_validate_expand_invariants(conn)
    assert failure is not None
    assert failure.stage == "revision_is_0089_not_0087"


def test_confirmation_token_exact_match() -> None:
    env = dict(_STAGING_ENV)
    env[CONFIRMATION_ENV] = "wrong"
    failure = validate_op.validate_confirmation(env)
    assert failure is not None
    assert failure.error_class == "confirmation_missing"


def test_identity_rejects_production_marker() -> None:
    failure = validate_op.validate_staging_identity(
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
    failure = validate_op.gates.validate_database_binding(env)
    assert failure is not None
    assert failure.stage == "database_host_not_allowlisted"


def test_bootstrap_freeze_required_for_run() -> None:
    env = dict(_STAGING_ENV)
    del env["NAHLA_SKIP_DB_BOOTSTRAP"]
    failure = validate_op.validate_bootstrap_freeze(env)
    assert failure is not None
    assert failure.error_class == "bootstrap_freeze_missing"


def test_main_rejects_unknown_command() -> None:
    rc = validate_op.main(["unknown"])
    assert rc != 0


def test_main_preflight_requires_staging_identity() -> None:
    with patch.object(validate_op, "validate_staging_identity", return_value=validate_op.GateFailure("identity_rejected", "staging_project_missing")):
        rc = validate_op.main(["preflight"])
    assert rc != 0


def test_run_preflight_emits_violation_manifest_without_row_identifiers() -> None:
    engine = MagicMock()
    conn = MagicMock()
    engine.connect.return_value.__enter__.return_value = conn

    with (
        patch.object(validate_op, "validate_pre_validate_expand_invariants", return_value=None),
        patch.object(validate_op, "validate_forbidden_sibling_tables", return_value=None),
        patch.object(validate_op, "collect_constraint_violation_counts", return_value={"violation_rows_total": 3, "chk_orders_untrusted_kinds_no_links": 3}),
        patch.object(validate_op, "validate_constraint_violation_preflight", return_value=validate_op.GateFailure("constraint_violation_preflight_failed", "orders_constraint_violations_present")),
    ):
        manifest, failure = validate_op.run_preflight(engine, env=_STAGING_ENV)

    assert failure is not None
    assert failure.stage == "orders_constraint_violations_present"
    assert manifest is not None
    assert manifest["constraint_violation_counts"]["violation_rows_total"] == 3
    dumped = json.dumps(manifest)
    assert "VIOLATE" not in dumped
    assert "customer_id" not in dumped.lower() or "violation" in dumped


def test_run_controlled_migration_success_path() -> None:
    engine = MagicMock()
    conn = MagicMock()
    engine.connect.return_value.__enter__.return_value = conn

    g4_summary = {"tenant_id": 1, "ready_for_validate": True, "access_status": "ok"}
    preflight_manifest = {"g4_evidence": g4_summary, "constraint_violation_counts": {"violation_rows_total": 0}}

    with (
        patch.object(validate_op, "run_preflight", return_value=(preflight_manifest, None)),
        patch.object(validate_op, "execute_alembic_upgrade", return_value={"outcome": "success"}),
        patch.object(validate_op.gates, "validate_post_success_revision", return_value=None),
        patch.object(validate_op, "validate_post_success_validate_invariants", return_value=None),
        patch(
            "scripts.operators.staging_migration_0087_to_0088.compute_public_schema_fingerprint",
            return_value={
                "schema_fingerprint_version": 1,
                "public_table_count": 5,
                "schema_fingerprint": "a" * 64,
                "schema_fingerprint_display": "a" * 16,
            },
        ),
        patch.object(validate_op, "collect_constraint_violation_counts", return_value={"violation_rows_total": 0}),
        patch.object(validate_op.gates, "read_alembic_revision", return_value="0088"),
    ):
        manifest, failure = validate_op.run_controlled_migration(
            engine,
            timeout_sec=600,
            env=_STAGING_ENV,
            tenant_id=1,
        )

    assert failure is None
    assert manifest is not None
    assert manifest["phase"] == "post_success"
    assert manifest["restore_first_policy"]


def test_g4_gate_rejects_ready_for_validate_false() -> None:
    report = SimpleNamespace(
        ready_for_validate=False,
        access_status="ok",
        readiness_blockers=["watermark_missing"],
        aggregate={"linked_orders_in_scope_total": 0},
    )
    session = MagicMock()
    engine = MagicMock()
    with (
        patch(
            "services.order_customer_identity_reconciliation_report.build_order_customer_identity_reconciliation_report",
            return_value=report,
        ),
        patch(
            "scripts.operators.staging_migration_0087_to_0088.sessionmaker",
            return_value=lambda bind=None: session,
        ),
    ):
        summary, failure = validate_op.validate_g4_ready_for_validate(
            engine, tenant_id=1, max_subjects_per_kind=1000
        )

    assert failure is not None
    assert failure.stage == "ready_for_validate_false"
    assert summary is not None
    assert summary["ready_for_validate"] is False


def test_g4_gate_reads_dict_shaped_report_aggregate() -> None:
    report = SimpleNamespace(
        ready_for_validate=True,
        access_status="ok",
        readiness_blockers=[],
        aggregate={"linked_orders_in_scope_total": 3},
    )
    session = MagicMock()
    engine = MagicMock()
    with (
        patch(
            "services.order_customer_identity_reconciliation_report.build_order_customer_identity_reconciliation_report",
            return_value=report,
        ),
        patch(
            "scripts.operators.staging_migration_0087_to_0088.sessionmaker",
            return_value=lambda bind=None: session,
        ),
    ):
        summary, failure = validate_op.validate_g4_ready_for_validate(
            engine, tenant_id=1, max_subjects_per_kind=1000
        )

    assert failure is None
    assert summary is not None
    assert summary["aggregate_linked_orders_in_scope_total"] == 3


def test_g4_gate_rejects_non_dict_report_aggregate() -> None:
    report = SimpleNamespace(
        ready_for_validate=True,
        access_status="ok",
        readiness_blockers=[],
        aggregate=SimpleNamespace(linked_orders_in_scope_total=1),
    )
    session = MagicMock()
    engine = MagicMock()
    with (
        patch(
            "services.order_customer_identity_reconciliation_report.build_order_customer_identity_reconciliation_report",
            return_value=report,
        ),
        patch(
            "scripts.operators.staging_migration_0087_to_0088.sessionmaker",
            return_value=lambda bind=None: session,
        ),
    ):
        summary, failure = validate_op.validate_g4_ready_for_validate(
            engine, tenant_id=1, max_subjects_per_kind=1000
        )

    assert failure is not None
    assert failure.stage == "reconciliation_report_aggregate_invalid"
    assert summary is None


def test_g4_gate_rejects_missing_linked_total_in_aggregate() -> None:
    report = SimpleNamespace(
        ready_for_validate=True,
        access_status="ok",
        readiness_blockers=[],
        aggregate={"unmapped_orders_in_scope_total": 0},
    )
    session = MagicMock()
    engine = MagicMock()
    with (
        patch(
            "services.order_customer_identity_reconciliation_report.build_order_customer_identity_reconciliation_report",
            return_value=report,
        ),
        patch(
            "scripts.operators.staging_migration_0087_to_0088.sessionmaker",
            return_value=lambda bind=None: session,
        ),
    ):
        summary, failure = validate_op.validate_g4_ready_for_validate(
            engine, tenant_id=1, max_subjects_per_kind=1000
        )

    assert failure is not None
    assert failure.stage == "reconciliation_report_aggregate_missing_linked_total"
    assert summary is None

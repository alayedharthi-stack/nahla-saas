#!/usr/bin/env python3
"""Guarded staging operator: Alembic 0087 → 0088 only (A1-Validate).

Read-only preflight with privacy-safe aggregate constraint-violation counts,
optional G4 ``ready_for_validate`` gate per tenant, and controlled execution.

Never accepts ``head``, ``0089``, or arbitrary revisions. Does not enable AI,
coupon, or reconciliation consumers.
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path
from typing import Any, Callable, Mapping, Sequence

from sqlalchemy import inspect, text
from sqlalchemy.engine import Connection, Engine
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import sessionmaker

_REPO_ROOT = Path(__file__).resolve().parents[2]
_DATABASE_DIR = _REPO_ROOT / "database"

for _entry in (str(_REPO_ROOT), str(_REPO_ROOT / "backend"), str(_DATABASE_DIR)):
    if _entry not in sys.path:
        sys.path.insert(0, _entry)

from scripts.operators.schema_fingerprint import (  # noqa: E402
    build_manifest,
    compute_public_schema_fingerprint,
)
from scripts.operators import staging_migration_operator_gates as gates  # noqa: E402
from scripts.operators.staging_migration_0087_to_0088_contract import (  # noqa: E402
    BASE_REVISION,
    BOOTSTRAP_FREEZE_ENV,
    CAPABILITY_KEY,
    CAPABILITY_STATE_EXPAND,
    CAPABILITY_STATE_VALIDATED,
    CONFIRMATION_ENV,
    CONFIRMATION_TOKEN,
    CONSTRAINT_VIOLATION_PROBES,
    DEFAULT_MIGRATION_TIMEOUT_SEC,
    DEFERRED_ORDER_INDEXES,
    FORBIDDEN_POST_0087_TABLES,
    MANIFEST_SCHEMA_VERSION,
    MAX_MIGRATION_TIMEOUT_SEC,
    MIN_MIGRATION_TIMEOUT_SEC,
    ORDER_CONSTRAINTS,
    REPOSITORY_SIBLING_OUT_OF_SCOPE_REVISIONS,
    STAGING_ENVIRONMENT_ENV,
    STAGING_ENVIRONMENT_VALUE,
    STAGING_IDENTITY_CLASS,
    STAGING_PROJECT_ENV,
    STAGING_PROJECT_VALUE,
    TARGET_REVISION,
    VALIDATION_REVISION,
)

GateFailure = gates.GateFailure

_CONSTRAINT_VALIDATED_SQL = text(
    "SELECT convalidated FROM pg_constraint WHERE conname = :name LIMIT 1"
)

_INDEX_VALID_SQL = text(
    """
    SELECT i.indisvalid
    FROM pg_class c
    JOIN pg_index i ON i.indexrelid = c.oid
    WHERE c.relname = :name
    LIMIT 1
    """
)

_CAPABILITY_DETAIL_SQL = text(
    """
    SELECT state, validation_revision
    FROM order_customer_identity_capability_state
    WHERE capability_key = :key
    LIMIT 1
    """
)


def validate_staging_identity(env: Mapping[str, str] | None = None) -> GateFailure | None:
    return gates.validate_staging_identity(
        env,
        staging_project_env=STAGING_PROJECT_ENV,
        staging_environment_env=STAGING_ENVIRONMENT_ENV,
        staging_project_value=STAGING_PROJECT_VALUE,
        staging_environment_value=STAGING_ENVIRONMENT_VALUE,
    )


def validate_bootstrap_freeze(env: Mapping[str, str] | None = None) -> GateFailure | None:
    return gates.validate_bootstrap_freeze(env, bootstrap_freeze_env=BOOTSTRAP_FREEZE_ENV)


def validate_confirmation(env: Mapping[str, str] | None = None) -> GateFailure | None:
    return gates.validate_confirmation(
        env,
        confirmation_env=CONFIRMATION_ENV,
        confirmation_token=CONFIRMATION_TOKEN,
    )


def validate_timeout_policy(timeout_sec: int) -> GateFailure | None:
    if not isinstance(timeout_sec, int) or isinstance(timeout_sec, bool):
        return GateFailure("invalid_timeout", "timeout_not_integer")
    if timeout_sec < MIN_MIGRATION_TIMEOUT_SEC or timeout_sec > MAX_MIGRATION_TIMEOUT_SEC:
        return GateFailure("invalid_timeout", "timeout_out_of_bounded_policy")
    return None


def validate_forbidden_sibling_tables(conn: Connection) -> GateFailure | None:
    insp = inspect(conn)
    tables = set(insp.get_table_names())
    for forbidden in FORBIDDEN_POST_0087_TABLES:
        if forbidden in tables:
            return GateFailure("wrong_revision", "sibling_revision_0089_objects_present")
    return None


def validate_pre_validate_expand_invariants(conn: Connection) -> GateFailure | None:
    current = gates.read_alembic_revision(conn)
    if current is None:
        return GateFailure("wrong_revision", "alembic_version_missing")
    if current == TARGET_REVISION:
        return GateFailure("wrong_revision", "revision_already_0088")
    if current in REPOSITORY_SIBLING_OUT_OF_SCOPE_REVISIONS:
        return GateFailure("wrong_revision", "revision_is_0089_not_0087")
    if current != BASE_REVISION:
        return GateFailure("wrong_revision", "revision_not_exactly_0087")

    row = conn.execute(
        _CAPABILITY_DETAIL_SQL,
        {"key": CAPABILITY_KEY},
    ).mappings().first()
    if row is None:
        return GateFailure("preflight_failed", "capability_state_missing")
    if row["state"] != CAPABILITY_STATE_EXPAND:
        return GateFailure("preflight_failed", "capability_state_not_expand")
    if row["validation_revision"] is not None:
        return GateFailure("preflight_failed", "capability_validation_revision_set")

    for chk in ORDER_CONSTRAINTS:
        validated_row = conn.execute(_CONSTRAINT_VALIDATED_SQL, {"name": chk}).first()
        if validated_row is None:
            return GateFailure("preflight_failed", "required_constraint_missing")
        if validated_row[0]:
            return GateFailure("preflight_failed", "constraint_already_validated")

    insp = inspect(conn)
    if "orders" in insp.get_table_names():
        present = {i.get("name") for i in insp.get_indexes("orders")}
        for deferred_index in DEFERRED_ORDER_INDEXES:
            if deferred_index in present:
                return GateFailure("preflight_failed", "deferred_order_index_present")
    return None


def collect_constraint_violation_counts(conn: Connection) -> dict[str, int]:
    counts: dict[str, int] = {}
    for name, probe_sql in CONSTRAINT_VIOLATION_PROBES.items():
        counts[name] = int(conn.execute(text(probe_sql)).scalar_one())
    counts["violation_rows_total"] = sum(counts.values())
    return counts


def validate_constraint_violation_preflight(conn: Connection) -> GateFailure | None:
    counts = collect_constraint_violation_counts(conn)
    if counts["violation_rows_total"] > 0:
        return GateFailure("constraint_violation_preflight_failed", "orders_constraint_violations_present")
    return None


def validate_g4_ready_for_validate(
    engine: Engine,
    *,
    tenant_id: int,
    max_subjects_per_kind: int,
) -> tuple[dict[str, Any] | None, GateFailure | None]:
    from services.order_customer_identity_reconciliation_report import (
        build_order_customer_identity_reconciliation_report,
    )

    session = sessionmaker(bind=engine)()
    try:
        report = build_order_customer_identity_reconciliation_report(
            session,
            tenant_id,
            max_subjects_per_kind=max_subjects_per_kind,
        )
        summary = {
            "tenant_id": int(tenant_id),
            "ready_for_validate": bool(report.ready_for_validate),
            "access_status": report.access_status,
            "readiness_blockers_count": len(report.readiness_blockers),
            "aggregate_linked_orders_in_scope_total": int(
                report.aggregate.linked_orders_in_scope_total
            ),
        }
        if report.access_status != "ok":
            return summary, GateFailure("g4_evidence_rejected", "reconciliation_report_access_not_ok")
        if not report.ready_for_validate:
            return summary, GateFailure("g4_evidence_rejected", "ready_for_validate_false")
        return summary, None
    except Exception:  # noqa: silent-ok - boundary returns a closed safe token.
        return None, GateFailure("g4_evidence_rejected", "reconciliation_report_failed")
    finally:
        session.close()


def _constraint_validated(conn: Connection, name: str) -> bool:
    row = conn.execute(_CONSTRAINT_VALIDATED_SQL, {"name": name}).first()
    return bool(row and row[0])


def _index_valid(conn: Connection, name: str) -> bool:
    row = conn.execute(_INDEX_VALID_SQL, {"name": name}).first()
    return bool(row and row[0])


def validate_post_success_validate_invariants(engine: Engine) -> GateFailure | None:
    with engine.connect() as conn:
        for chk in ORDER_CONSTRAINTS:
            if not _constraint_validated(conn, chk):
                return GateFailure("post_validation_failed", "constraint_not_validated")
        for idx_name in DEFERRED_ORDER_INDEXES:
            if not _index_valid(conn, idx_name):
                return GateFailure("post_validation_failed", "deferred_index_missing_or_invalid")

        row = conn.execute(
            _CAPABILITY_DETAIL_SQL,
            {"key": CAPABILITY_KEY},
        ).mappings().first()
        if row is None:
            return GateFailure("post_validation_failed", "capability_state_missing")
        if row["state"] != CAPABILITY_STATE_VALIDATED:
            return GateFailure("post_validation_failed", "capability_state_not_validated")
        if row["validation_revision"] != VALIDATION_REVISION:
            return GateFailure("post_validation_failed", "capability_validation_revision_mismatch")
    return None


def build_alembic_upgrade_command(python_executable: str | None = None) -> list[str]:
    return gates.build_alembic_upgrade_command(TARGET_REVISION, python_executable)


def assert_upgrade_command_safe(cmd: Sequence[str]) -> None:
    gates.assert_upgrade_command_safe(cmd, target_revision=TARGET_REVISION)


def execute_alembic_upgrade(**kwargs: Any) -> dict[str, str]:
    return gates.execute_alembic_upgrade(
        target_revision=TARGET_REVISION,
        database_dir=str(_DATABASE_DIR),
        **kwargs,
    )


def run_preflight(
    engine: Engine,
    *,
    env: Mapping[str, str] | None = None,
    require_identity: bool = True,
    require_bootstrap_freeze: bool = False,
    tenant_id: int | None = None,
    require_g4: bool = False,
    max_subjects_per_kind: int = 1000,
) -> tuple[dict[str, Any] | None, GateFailure | None]:
    import os

    env = env or os.environ
    if require_identity:
        failure = validate_staging_identity(env)
        if failure:
            return None, failure
    failure = gates.validate_database_binding(env)
    if failure:
        return None, failure
    if require_bootstrap_freeze:
        failure = validate_bootstrap_freeze(env)
        if failure:
            return None, failure

    try:
        with engine.connect() as conn:
            expand_failure = validate_pre_validate_expand_invariants(conn)
            if expand_failure:
                return None, expand_failure
            sibling_failure = validate_forbidden_sibling_tables(conn)
            if sibling_failure:
                return None, sibling_failure
            violation_failure = validate_constraint_violation_preflight(conn)
            if violation_failure:
                violation_counts = collect_constraint_violation_counts(conn)
                manifest = {
                    "manifest_schema_version": MANIFEST_SCHEMA_VERSION,
                    "phase": "preflight",
                    "alembic_revision": BASE_REVISION,
                    "target_revision": TARGET_REVISION,
                    "staging_identity_class": STAGING_IDENTITY_CLASS,
                    "constraint_violation_counts": violation_counts,
                    "repository_sibling_out_of_scope_revisions": list(
                        REPOSITORY_SIBLING_OUT_OF_SCOPE_REVISIONS
                    ),
                }
                return manifest, violation_failure
            fingerprint = compute_public_schema_fingerprint(conn)
            violation_counts = collect_constraint_violation_counts(conn)
            revision = gates.read_alembic_revision(conn) or BASE_REVISION
    except SQLAlchemyError:
        return None, GateFailure("database_operation_failed", "preflight_database")
    except Exception:  # noqa: silent-ok - boundary returns a closed safe token.
        return None, GateFailure("preflight_unexpected_error", "preflight_database")

    g4_summary: dict[str, Any] | None = None
    if require_g4:
        if tenant_id is None or tenant_id <= 0:
            return None, GateFailure("g4_evidence_rejected", "tenant_id_required")
        g4_summary, g4_failure = validate_g4_ready_for_validate(
            engine,
            tenant_id=tenant_id,
            max_subjects_per_kind=max_subjects_per_kind,
        )
        if g4_failure:
            manifest = {
                "manifest_schema_version": MANIFEST_SCHEMA_VERSION,
                "phase": "preflight",
                "alembic_revision": revision,
                "target_revision": TARGET_REVISION,
                "constraint_violation_counts": violation_counts,
                "g4_evidence": g4_summary,
            }
            return manifest, g4_failure

    manifest = build_manifest(
        phase="preflight",
        alembic_revision=revision,
        fingerprint=fingerprint,
        destructive_preflight_counts={
            "constraint_violation_rows_total": violation_counts["violation_rows_total"],
        },
        staging_identity_class=STAGING_IDENTITY_CLASS,
        bootstrap_freeze=gates.truthy_env_from_map(env, BOOTSTRAP_FREEZE_ENV),
    )
    manifest["manifest_schema_version"] = MANIFEST_SCHEMA_VERSION
    manifest["target_revision"] = TARGET_REVISION
    manifest["constraint_violation_counts"] = violation_counts
    if g4_summary is not None:
        manifest["g4_evidence"] = g4_summary
    manifest["repository_sibling_out_of_scope_revisions"] = list(
        REPOSITORY_SIBLING_OUT_OF_SCOPE_REVISIONS
    )
    return manifest, None


def run_controlled_migration(
    engine: Engine,
    *,
    timeout_sec: int,
    env: Mapping[str, str] | None = None,
    tenant_id: int,
    max_subjects_per_kind: int = 1000,
    alembic_runner: Callable[..., Any] | None = None,
) -> tuple[dict[str, Any] | None, GateFailure | None]:
    import os

    env = env or os.environ
    for validator in (
        validate_staging_identity,
        gates.validate_database_binding,
        validate_bootstrap_freeze,
        validate_confirmation,
    ):
        failure = validator(env)
        if failure:
            return None, failure
    timeout_failure = validate_timeout_policy(timeout_sec)
    if timeout_failure:
        return None, timeout_failure

    preflight_manifest, preflight_failure = run_preflight(
        engine,
        env=env,
        require_identity=True,
        require_bootstrap_freeze=True,
        tenant_id=tenant_id,
        require_g4=True,
        max_subjects_per_kind=max_subjects_per_kind,
    )
    if preflight_failure:
        return preflight_manifest, preflight_failure
    assert preflight_manifest is not None

    migration_outcome = execute_alembic_upgrade(
        timeout_sec=timeout_sec,
        env=env,
        runner=alembic_runner,
    )
    if migration_outcome["outcome"] != "success":
        return None, GateFailure(migration_outcome["error_class"], migration_outcome["stage"])

    try:
        with engine.connect() as conn:
            revision_failure = gates.validate_post_success_revision(
                conn,
                target_revision=TARGET_REVISION,
                wrong_stage="revision_not_0088",
            )
            if revision_failure:
                return None, revision_failure
            validate_failure = validate_post_success_validate_invariants(engine)
            if validate_failure:
                return None, validate_failure
            fingerprint = compute_public_schema_fingerprint(conn)
            violation_counts = collect_constraint_violation_counts(conn)
            revision = gates.read_alembic_revision(conn) or TARGET_REVISION
    except SQLAlchemyError:
        return None, GateFailure("database_operation_failed", "post_validation_database")
    except Exception:  # noqa: silent-ok - boundary returns a closed safe token.
        return None, GateFailure("post_validation_unexpected_error", "post_validation_database")

    manifest = build_manifest(
        phase="post_success",
        alembic_revision=revision,
        fingerprint=fingerprint,
        destructive_preflight_counts={
            "constraint_violation_rows_total": violation_counts["violation_rows_total"],
        },
        staging_identity_class=STAGING_IDENTITY_CLASS,
        bootstrap_freeze=True,
        migration_outcome=migration_outcome,
    )
    manifest["manifest_schema_version"] = MANIFEST_SCHEMA_VERSION
    manifest["target_revision"] = TARGET_REVISION
    manifest["constraint_violation_counts"] = violation_counts
    manifest["g4_evidence"] = preflight_manifest.get("g4_evidence")
    manifest["restore_first_policy"] = (
        "On any post-migration validation failure, restore staging from the latest "
        "verified backup before retrying. Do not downgrade in place."
    )
    return manifest, None


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Guarded staging operator: Alembic 0087 → 0088 only (A1-Validate).",
        exit_on_error=False,
    )
    sub = parser.add_subparsers(dest="command")
    preflight_parser = sub.add_parser(
        "preflight",
        help="Read-only preflight manifest (staging identity required).",
    )
    preflight_parser.add_argument(
        "--tenant-id",
        type=int,
        help="When set, also requires G4 ready_for_validate for this tenant.",
    )
    run_parser = sub.add_parser("run", help="Execute controlled migration after all gates pass.")
    run_parser.add_argument(
        "--tenant-id",
        type=int,
        required=True,
        help="Required tenant for G4 ready_for_validate evidence gate.",
    )
    run_parser.add_argument(
        "--max-subjects-per-kind",
        type=int,
        default=1000,
        help="G4 reconciliation report cap (default 1000).",
    )
    run_parser.add_argument(
        "--timeout-sec",
        type=int,
        default=DEFAULT_MIGRATION_TIMEOUT_SEC,
        help=(
            f"Alembic upgrade timeout (default {DEFAULT_MIGRATION_TIMEOUT_SEC}; "
            f"bounded {MIN_MIGRATION_TIMEOUT_SEC}–{MAX_MIGRATION_TIMEOUT_SEC})."
        ),
    )
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    try:
        args, unknown_args = _build_parser().parse_known_args(argv)
    except (argparse.ArgumentError, SystemExit):
        return gates.emit_safe_error(error_class="invalid_command", stage="cli")
    if args.command not in {"preflight", "run"} or unknown_args:
        return gates.emit_safe_error(error_class="invalid_command", stage="cli")

    identity_failure = validate_staging_identity()
    if identity_failure:
        return gates.emit_safe_error(error_class=identity_failure.error_class, stage=identity_failure.stage)
    database_failure = gates.validate_database_binding()
    if database_failure:
        return gates.emit_safe_error(error_class=database_failure.error_class, stage=database_failure.stage)

    if args.command == "run":
        if int(args.tenant_id) <= 0:
            return gates.emit_safe_error(error_class="invalid_tenant", stage="cli")
        for validator in (validate_bootstrap_freeze, validate_confirmation):
            failure = validator()
            if failure:
                return gates.emit_safe_error(error_class=failure.error_class, stage=failure.stage)
        timeout_failure = validate_timeout_policy(args.timeout_sec)
        if timeout_failure:
            return gates.emit_safe_error(error_class=timeout_failure.error_class, stage=timeout_failure.stage)

    try:
        engine = gates.connect_engine()
    except (ValueError, SQLAlchemyError):
        return gates.emit_safe_error(error_class="database_connection_failed", stage="database_connect")
    except Exception:  # noqa: silent-ok - top-level boundary returns a closed safe token.
        return gates.emit_safe_error(error_class="unexpected_error", stage="database_connect")

    if args.command == "preflight":
        try:
            tenant_id = getattr(args, "tenant_id", None)
            manifest, failure = run_preflight(
                engine,
                require_identity=True,
                require_bootstrap_freeze=False,
                tenant_id=int(tenant_id) if tenant_id is not None else None,
                require_g4=tenant_id is not None,
                max_subjects_per_kind=int(getattr(args, "max_subjects_per_kind", 1000) or 1000),
            )
        except Exception:  # noqa: silent-ok - top-level boundary returns a closed safe token.
            return gates.emit_safe_error(error_class="unexpected_error", stage="preflight")
        if failure:
            if manifest is not None:
                gates.emit_manifest(manifest)
            return gates.emit_safe_error(error_class=failure.error_class, stage=failure.stage)
        assert manifest is not None
        return gates.emit_manifest(manifest)

    if args.command == "run":
        try:
            manifest, failure = run_controlled_migration(
                engine,
                timeout_sec=args.timeout_sec,
                tenant_id=int(args.tenant_id),
                max_subjects_per_kind=int(args.max_subjects_per_kind),
            )
        except Exception:  # noqa: silent-ok - top-level boundary returns a closed safe token.
            return gates.emit_safe_error(error_class="unexpected_error", stage="controlled_migration")
        if failure:
            if manifest is not None:
                gates.emit_manifest(manifest)
            return gates.emit_safe_error(error_class=failure.error_class, stage=failure.stage)
        assert manifest is not None
        return gates.emit_manifest(manifest)

    return gates.emit_safe_error(error_class="invalid_command", stage="cli")


if __name__ == "__main__":
    raise SystemExit(main())

#!/usr/bin/env python3
"""Guarded staging operator: Alembic 0030 → 0032 only.

Read-only preflight manifest generation and optional controlled execution.
Designed for invocation inside the staging app container after merge/deploy.

This module never accepts arbitrary revisions, ``head``, or downgrade targets.
Boundary: stops at 0032 — 0033+ is out of scope for this stage.
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path
from typing import Any, Callable, Mapping, Sequence

from sqlalchemy import inspect, text
from sqlalchemy.engine import Connection, Engine
from sqlalchemy.exc import SQLAlchemyError

_REPO_ROOT = Path(__file__).resolve().parents[2]
_DATABASE_DIR = _REPO_ROOT / "database"

for _entry in (str(_REPO_ROOT), str(_REPO_ROOT / "backend"), str(_REPO_ROOT / "database")):
    if _entry not in sys.path:
        sys.path.insert(0, _entry)

from scripts.operators.schema_fingerprint import (  # noqa: E402
    build_manifest,
    compute_public_schema_fingerprint,
)
from scripts.operators import staging_migration_operator_gates as gates  # noqa: E402
from scripts.operators.staging_migration_0030_to_0032_contract import (  # noqa: E402
    BASE_REVISION,
    BOOTSTRAP_FREEZE_ENV,
    CONFIRMATION_ENV,
    CONFIRMATION_TOKEN,
    DEFAULT_MIGRATION_TIMEOUT_SEC,
    MAX_DUPLICATE_TENANT_PHONE_GROUPS,
    MAX_DUPLICATE_TENANT_NORMALIZED_PHONE_GROUPS,
    MAX_DUPLICATE_TENANT_SALLA_BACKFILL_GROUPS,
    MAX_DUPLICATE_TENANT_SALLA_METADATA_GROUPS,
    REQUIRED_COLUMNS,
    REQUIRED_INDEXES,
    REQUIRED_TABLES,
    STAGING_ENVIRONMENT_ENV,
    STAGING_ENVIRONMENT_VALUE,
    STAGING_IDENTITY_CLASS,
    STAGING_PROJECT_ENV,
    STAGING_PROJECT_VALUE,
    TARGET_REVISION,
)

GateFailure = gates.GateFailure

# Same normalization expression as migration 0032 (aggregate duplicate groups only).
_NORM_SQL = """
    CASE
        WHEN phone ~ '^\\+[1-9][0-9]{6,14}$'
            THEN phone
        WHEN phone ~ '^00[1-9][0-9]{6,14}$'
            THEN '+' || substring(phone FROM 3)
        WHEN phone ~ '^966[5][0-9]{8}$'
            THEN '+' || phone
        WHEN phone ~ '^05[0-9]{8}$'
            THEN '+966' || substring(phone FROM 2)
        WHEN phone ~ '^5[0-9]{8}$'
            THEN '+966' || phone
        WHEN phone ~ '^\\+[1-9][0-9]+$' AND length(phone) BETWEEN 8 AND 16
            THEN phone
        ELSE NULL
    END
"""

_DUP_TENANT_PHONE_GROUPS_SQL = text(
    """
    SELECT count(*)::int
    FROM (
        SELECT tenant_id, phone
        FROM customers
        WHERE phone IS NOT NULL AND phone != ''
        GROUP BY tenant_id, phone
        HAVING COUNT(*) > 1
    ) AS duplicate_groups
    """
)

_DUP_TENANT_SALLA_METADATA_GROUPS_SQL = text(
    """
    SELECT count(*)::int
    FROM (
        SELECT tenant_id, metadata->>'salla_id' AS salla_id
        FROM customers
        WHERE metadata->>'salla_id' IS NOT NULL AND metadata->>'salla_id' != ''
        GROUP BY tenant_id, metadata->>'salla_id'
        HAVING COUNT(*) > 1
    ) AS duplicate_groups
    """
)

_DUP_TENANT_SALLA_BACKFILL_GROUPS_SQL = text(
    """
    SELECT count(*)::int
    FROM (
        SELECT
            tenant_id,
            COALESCE(
                NULLIF(metadata->>'salla_id', ''),
                NULLIF(metadata->>'external_id', '')
            ) AS backfill_key
        FROM customers
        WHERE COALESCE(
            NULLIF(metadata->>'salla_id', ''),
            NULLIF(metadata->>'external_id', '')
        ) IS NOT NULL
        GROUP BY tenant_id, backfill_key
        HAVING COUNT(*) > 1
    ) AS duplicate_groups
    """
)

_DUP_TENANT_NORMALIZED_PHONE_GROUPS_SQL = text(
    f"""
    SELECT count(*)::int
    FROM (
        SELECT tenant_id, ({_NORM_SQL}) AS norm
        FROM customers
        WHERE phone IS NOT NULL
          AND ({_NORM_SQL}) IS NOT NULL
        GROUP BY tenant_id, ({_NORM_SQL})
        HAVING COUNT(*) > 1
    ) AS duplicate_groups
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


def collect_destructive_preflight_counts(conn: Connection) -> dict[str, int]:
    """Aggregate-only duplicate indicators for 0031/0032 hazards (no PII)."""
    return {
        "duplicate_tenant_phone_groups": int(conn.execute(_DUP_TENANT_PHONE_GROUPS_SQL).scalar_one()),
        "duplicate_tenant_salla_metadata_groups": int(
            conn.execute(_DUP_TENANT_SALLA_METADATA_GROUPS_SQL).scalar_one()
        ),
        "duplicate_tenant_salla_backfill_groups": int(
            conn.execute(_DUP_TENANT_SALLA_BACKFILL_GROUPS_SQL).scalar_one()
        ),
        "duplicate_tenant_normalized_phone_groups": int(
            conn.execute(_DUP_TENANT_NORMALIZED_PHONE_GROUPS_SQL).scalar_one()
        ),
    }


def validate_duplicate_preflight(conn: Connection) -> GateFailure | None:
    counts = collect_destructive_preflight_counts(conn)
    if counts["duplicate_tenant_phone_groups"] > MAX_DUPLICATE_TENANT_PHONE_GROUPS:
        return GateFailure("duplicate_preflight_failed", "tenant_phone_duplicates_present")
    if counts["duplicate_tenant_salla_metadata_groups"] > MAX_DUPLICATE_TENANT_SALLA_METADATA_GROUPS:
        return GateFailure("duplicate_preflight_failed", "tenant_salla_metadata_duplicates_present")
    if counts["duplicate_tenant_salla_backfill_groups"] > MAX_DUPLICATE_TENANT_SALLA_BACKFILL_GROUPS:
        return GateFailure("duplicate_preflight_failed", "tenant_salla_backfill_collisions_present")
    if counts["duplicate_tenant_normalized_phone_groups"] > MAX_DUPLICATE_TENANT_NORMALIZED_PHONE_GROUPS:
        return GateFailure("duplicate_preflight_failed", "tenant_normalized_phone_collisions_present")
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


def validate_post_success_schema(engine: Engine) -> dict[str, Any]:
    insp = inspect(engine)
    tables = set(insp.get_table_names())
    missing_tables = [t for t in REQUIRED_TABLES if t not in tables]
    missing_columns: list[str] = []
    for table, columns in REQUIRED_COLUMNS.items():
        present = {c["name"] for c in insp.get_columns(table)}
        for column in columns:
            if column not in present:
                missing_columns.append(f"{table}.{column}")
    missing_indexes: list[str] = []
    for table, indexes in REQUIRED_INDEXES.items():
        present = {i.get("name") for i in insp.get_indexes(table)}
        for index_name in indexes:
            if index_name not in present:
                missing_indexes.append(f"{table}.{index_name}")
    ok = not missing_tables and not missing_columns and not missing_indexes
    return {
        "schema_ok": ok,
        "missing_tables": missing_tables,
        "missing_columns": missing_columns,
        "missing_indexes": missing_indexes,
    }


def run_preflight(
    engine: Engine,
    *,
    env: Mapping[str, str] | None = None,
    require_identity: bool = True,
    require_bootstrap_freeze: bool = False,
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
            revision_failure = gates.validate_start_revision(
                conn,
                base_revision=BASE_REVISION,
                wrong_stage="start_revision_not_0030",
            )
            if revision_failure:
                return None, revision_failure
            duplicate_failure = validate_duplicate_preflight(conn)
            if duplicate_failure:
                return None, duplicate_failure
            fingerprint = compute_public_schema_fingerprint(conn)
            destructive = collect_destructive_preflight_counts(conn)
            revision = gates.read_alembic_revision(conn) or BASE_REVISION
    except SQLAlchemyError:
        return None, GateFailure("database_operation_failed", "preflight_database")
    except Exception:  # noqa: silent-ok - boundary returns a closed safe token.
        return None, GateFailure("preflight_unexpected_error", "preflight_database")

    manifest = build_manifest(
        phase="preflight",
        alembic_revision=revision,
        fingerprint=fingerprint,
        destructive_preflight_counts=destructive,
        staging_identity_class=STAGING_IDENTITY_CLASS,
        bootstrap_freeze=gates.truthy_env_from_map(env, BOOTSTRAP_FREEZE_ENV),
    )
    return manifest, None


def run_controlled_migration(
    engine: Engine,
    *,
    timeout_sec: int,
    env: Mapping[str, str] | None = None,
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

    preflight_manifest, preflight_failure = run_preflight(
        engine,
        env=env,
        require_identity=True,
        require_bootstrap_freeze=True,
    )
    if preflight_failure:
        return None, preflight_failure
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
                wrong_stage="revision_not_0032",
            )
            if revision_failure:
                return None, revision_failure
            fingerprint = compute_public_schema_fingerprint(conn)
            destructive = collect_destructive_preflight_counts(conn)
            revision = gates.read_alembic_revision(conn) or TARGET_REVISION
        post_validation = validate_post_success_schema(engine)
    except SQLAlchemyError:
        return None, GateFailure("database_operation_failed", "post_validation_database")
    except Exception:  # noqa: silent-ok - boundary returns a closed safe token.
        return None, GateFailure("post_validation_unexpected_error", "post_validation_database")
    if not post_validation["schema_ok"]:
        return None, GateFailure("post_validation_failed", "schema_metadata_incomplete")

    manifest = build_manifest(
        phase="post_success",
        alembic_revision=revision,
        fingerprint=fingerprint,
        destructive_preflight_counts=destructive,
        staging_identity_class=STAGING_IDENTITY_CLASS,
        bootstrap_freeze=True,
        post_validation=post_validation,
        migration_outcome=migration_outcome,
    )
    return manifest, None


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Guarded staging operator: Alembic 0030 → 0032 only.",
        exit_on_error=False,
    )
    sub = parser.add_subparsers(dest="command")
    sub.add_parser("preflight", help="Read-only preflight manifest (staging identity required).")
    run_parser = sub.add_parser("run", help="Execute controlled migration after all safety gates pass.")
    run_parser.add_argument(
        "--timeout-sec",
        type=int,
        default=DEFAULT_MIGRATION_TIMEOUT_SEC,
        help=f"Alembic upgrade timeout (default {DEFAULT_MIGRATION_TIMEOUT_SEC}).",
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
        for validator in (validate_bootstrap_freeze, validate_confirmation):
            failure = validator()
            if failure:
                return gates.emit_safe_error(error_class=failure.error_class, stage=failure.stage)

    try:
        engine = gates.connect_engine()
    except (ValueError, SQLAlchemyError):
        return gates.emit_safe_error(error_class="database_connection_failed", stage="database_connect")
    except Exception:  # noqa: silent-ok - top-level boundary returns a closed safe token.
        return gates.emit_safe_error(error_class="unexpected_error", stage="database_connect")

    if args.command == "preflight":
        try:
            manifest, failure = run_preflight(engine, require_identity=True, require_bootstrap_freeze=False)
        except Exception:  # noqa: silent-ok - top-level boundary returns a closed safe token.
            return gates.emit_safe_error(error_class="unexpected_error", stage="preflight")
        if failure:
            return gates.emit_safe_error(error_class=failure.error_class, stage=failure.stage)
        assert manifest is not None
        return gates.emit_manifest(manifest)

    if args.command == "run":
        try:
            manifest, failure = run_controlled_migration(engine, timeout_sec=args.timeout_sec)
        except Exception:  # noqa: silent-ok - top-level boundary returns a closed safe token.
            return gates.emit_safe_error(error_class="unexpected_error", stage="controlled_migration")
        if failure:
            return gates.emit_safe_error(error_class=failure.error_class, stage=failure.stage)
        assert manifest is not None
        return gates.emit_manifest(manifest)

    return gates.emit_safe_error(error_class="invalid_command", stage="cli")


if __name__ == "__main__":
    raise SystemExit(main())

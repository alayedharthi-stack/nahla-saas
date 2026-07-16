#!/usr/bin/env python3
"""Guarded staging operator: Alembic 0083 → 0087 only (A1-Expand).

Read-only preflight manifest generation and optional controlled execution.
Designed for invocation inside the staging app container after merge/deploy.

This module never accepts arbitrary revisions, ``head``, or downgrade targets.
Boundary: stops at 0087 — 0088 (A1-Validate) and 0089 are out of scope.

Repository note: migration 0089 is merged on origin/main (PR #596) but is NOT a target of
this runner. Alembic repository head may be 0089 while this operator stops at 0087.
Staging advancement 0087→0089 is a separate later operator slice.
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
from scripts.operators.staging_catalog_readonly_audit import (  # noqa: E402
    build_catalog_audit_manifest,
    validate_catalog_audit_gate,
)
from scripts.operators.staging_migration_0083_to_0087_contract import (  # noqa: E402
    BASE_REVISION,
    BOOTSTRAP_FREEZE_ENV,
    CAPABILITY_KEY,
    CAPABILITY_STATE_EXPAND,
    CONFIRMATION_ENV,
    CONFIRMATION_TOKEN,
    DEFAULT_MIGRATION_TIMEOUT_SEC,
    DEFERRED_ORDER_INDEXES,
    MAX_DUPLICATE_TENANT_PRODUCT_EXTERNAL_ID_GROUPS,
    MAX_MIGRATION_TIMEOUT_SEC,
    MIN_MIGRATION_TIMEOUT_SEC,
    PRODUCTS_TENANT_EXTERNAL_ID_INDEX,
    REPOSITORY_MERGED_BUT_OUT_OF_SCOPE_REVISIONS,
    REQUIRED_COLUMNS,
    REQUIRED_INDEXES,
    REQUIRED_NOT_VALID_CHECK_CONSTRAINTS,
    REQUIRED_NOT_VALID_FOREIGN_KEYS,
    REQUIRED_TABLES,
    STAGING_ENVIRONMENT_ENV,
    STAGING_ENVIRONMENT_VALUE,
    STAGING_IDENTITY_CLASS,
    STAGING_PROJECT_ENV,
    STAGING_PROJECT_VALUE,
    TARGET_REVISION,
)

GateFailure = gates.GateFailure

_GEN_RANDOM_UUID_PROBE_SQL = text("SELECT gen_random_uuid()")

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

_DUP_TENANT_PRODUCT_EXTERNAL_ID_GROUPS_SQL = text(
    """
    SELECT count(*)::int
    FROM (
        SELECT tenant_id, external_id
        FROM products
        WHERE external_id IS NOT NULL AND external_id != ''
        GROUP BY tenant_id, external_id
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


def validate_timeout_policy(timeout_sec: int) -> GateFailure | None:
    if not isinstance(timeout_sec, int) or isinstance(timeout_sec, bool):
        return GateFailure("invalid_timeout", "timeout_not_integer")
    if timeout_sec < MIN_MIGRATION_TIMEOUT_SEC or timeout_sec > MAX_MIGRATION_TIMEOUT_SEC:
        return GateFailure("invalid_timeout", "timeout_out_of_bounded_policy")
    return None


def validate_extension_availability(conn: Connection) -> GateFailure | None:
    try:
        conn.execute(_GEN_RANDOM_UUID_PROBE_SQL)
    except SQLAlchemyError:
        return GateFailure("extension_unavailable", "gen_random_uuid_probe_failed")
    except Exception:  # noqa: silent-ok - boundary returns a closed safe token.
        return GateFailure("extension_unavailable", "extension_probe_unexpected_error")
    return None


def collect_destructive_preflight_counts(conn: Connection) -> dict[str, int]:
    audit = build_catalog_audit_manifest(conn)
    indicators = audit.get("catalog_audit_indicators", {})
    return {
        "forbidden_a1_objects_present": int(audit.get("forbidden_a1_objects_present", 0)),
        "external_customer_profiles_present": int(indicators.get("external_customer_profiles", 0)),
        "order_customer_identity_capability_state_present": int(
            indicators.get("order_customer_identity_capability_state", 0)
        ),
        "conversation_a1_subject_bindings_present": int(
            indicators.get("conversation_a1_subject_bindings", 0)
        ),
        "duplicate_tenant_product_external_id_groups": int(
            conn.execute(_DUP_TENANT_PRODUCT_EXTERNAL_ID_GROUPS_SQL).scalar_one()
        ),
        "pgcrypto_extension_available": int(
            conn.execute(
                text("SELECT count(*)::int FROM pg_extension WHERE extname = 'pgcrypto'"),
            ).scalar_one()
        ),
    }


def validate_duplicate_preflight(conn: Connection) -> GateFailure | None:
    counts = collect_destructive_preflight_counts(conn)
    if counts["duplicate_tenant_product_external_id_groups"] > MAX_DUPLICATE_TENANT_PRODUCT_EXTERNAL_ID_GROUPS:
        return GateFailure("duplicate_preflight_failed", "tenant_product_external_id_duplicates_present")
    return None


def _index_valid(conn: Connection, name: str) -> bool:
    row = conn.execute(_INDEX_VALID_SQL, {"name": name}).first()
    return bool(row and row[0])


def _constraint_validated(conn: Connection, name: str) -> bool:
    row = conn.execute(_CONSTRAINT_VALIDATED_SQL, {"name": name}).first()
    return bool(row and row[0])


def validate_post_success_expand_invariants(engine: Engine) -> GateFailure | None:
    with engine.connect() as conn:
        for chk in REQUIRED_NOT_VALID_CHECK_CONSTRAINTS:
            if _constraint_validated(conn, chk):
                return GateFailure("post_validation_failed", "check_constraint_already_validated")
        for fk in REQUIRED_NOT_VALID_FOREIGN_KEYS:
            if _constraint_validated(conn, fk):
                return GateFailure("post_validation_failed", "foreign_key_already_validated")

        row = conn.execute(
            text(
                """
                SELECT state, validation_revision
                FROM order_customer_identity_capability_state
                WHERE capability_key = :key
                LIMIT 1
                """
            ),
            {"key": CAPABILITY_KEY},
        ).mappings().first()
        if row is None:
            return GateFailure("post_validation_failed", "capability_state_missing")
        if row["state"] != CAPABILITY_STATE_EXPAND:
            return GateFailure("post_validation_failed", "capability_state_not_expand")
        if row["validation_revision"] is not None:
            return GateFailure("post_validation_failed", "capability_validation_revision_set")

    insp = inspect(engine)
    if "orders" in insp.get_table_names():
        present = {i.get("name") for i in insp.get_indexes("orders")}
        for deferred_index in DEFERRED_ORDER_INDEXES:
            if deferred_index in present:
                return GateFailure("post_validation_failed", "deferred_order_index_present")

    with engine.connect() as conn:
        if PRODUCTS_TENANT_EXTERNAL_ID_INDEX not in {
            i.get("name") for i in insp.get_indexes("products")
        }:
            return GateFailure("post_validation_failed", "products_external_id_index_missing")
        if not _index_valid(conn, PRODUCTS_TENANT_EXTERNAL_ID_INDEX):
            return GateFailure("post_validation_failed", "products_external_id_index_invalid")
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
        "repository_merged_out_of_scope_revisions": list(REPOSITORY_MERGED_BUT_OUT_OF_SCOPE_REVISIONS),
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
                wrong_stage="start_revision_not_0083",
            )
            if revision_failure:
                return None, revision_failure
            catalog_failure = validate_catalog_audit_gate(conn)
            if catalog_failure:
                return None, catalog_failure
            duplicate_failure = validate_duplicate_preflight(conn)
            if duplicate_failure:
                return None, duplicate_failure
            extension_failure = validate_extension_availability(conn)
            if extension_failure:
                return None, extension_failure
            fingerprint = compute_public_schema_fingerprint(conn)
            destructive = collect_destructive_preflight_counts(conn)
            revision = gates.read_alembic_revision(conn) or BASE_REVISION
            catalog_audit = build_catalog_audit_manifest(conn, env=env)
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
    manifest["catalog_audit"] = {
        "forbidden_a1_objects_present": catalog_audit["forbidden_a1_objects_present"],
        "catalog_audit_indicators": catalog_audit["catalog_audit_indicators"],
    }
    manifest["extension_gate"] = {
        "gen_random_uuid_available": True,
        "pgcrypto_extension_available": destructive["pgcrypto_extension_available"],
    }
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
    timeout_failure = validate_timeout_policy(timeout_sec)
    if timeout_failure:
        return None, timeout_failure

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
                wrong_stage="revision_not_0087",
            )
            if revision_failure:
                return None, revision_failure
            expand_failure = validate_post_success_expand_invariants(engine)
            if expand_failure:
                return None, expand_failure
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
        description="Guarded staging operator: Alembic 0083 → 0087 only (A1-Expand).",
        exit_on_error=False,
    )
    sub = parser.add_subparsers(dest="command")
    sub.add_parser("preflight", help="Read-only preflight manifest (staging identity required).")
    run_parser = sub.add_parser("run", help="Execute controlled migration after all safety gates pass.")
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

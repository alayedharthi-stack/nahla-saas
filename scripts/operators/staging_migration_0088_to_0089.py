#!/usr/bin/env python3
"""Guarded staging operator: attach sibling Alembic 0089 onto validated 0088.

Read-only preflight with privacy-safe aggregate counts, staging/host/bootstrap/
confirmation gates, and controlled execution of ``alembic upgrade 0089`` only.

Never accepts ``head``, ``0087``, or arbitrary revisions. Does not re-run,
downgrade, or alter ``0088`` A1-Validate state. Does not enable AI, coupon
runtime, or reconciliation consumers.
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

for _entry in (str(_REPO_ROOT), str(_REPO_ROOT / "backend"), str(_DATABASE_DIR)):
    if _entry not in sys.path:
        sys.path.insert(0, _entry)

from scripts.operators.schema_fingerprint import (  # noqa: E402
    build_manifest,
    compute_public_schema_fingerprint,
)
from scripts.operators import staging_migration_operator_gates as gates  # noqa: E402
from scripts.operators.staging_migration_0087_to_0088 import (  # noqa: E402
    validate_post_success_validate_invariants,
)
from scripts.operators.staging_migration_0087_to_0088_contract import (  # noqa: E402
    CAPABILITY_KEY,
    CAPABILITY_STATE_EXPAND,
    CAPABILITY_STATE_VALIDATED,
    VALIDATION_REVISION,
)
from scripts.operators.staging_migration_0088_to_0089_contract import (  # noqa: E402
    BASE_REVISION,
    BOOTSTRAP_FREEZE_ENV,
    CASB_CHECK_CONSTRAINTS,
    CASB_FOREIGN_KEYS,
    CASB_PARTIAL_UNIQUE_INDEX,
    CASB_STATE_INDEX,
    CASB_TABLE,
    CONFIRMATION_ENV,
    CONFIRMATION_TOKEN,
    CONVERSATIONS_COMPOSITE_INDEX,
    DEFAULT_MIGRATION_TIMEOUT_SEC,
    DR_RESTORE_PROFILE_REVISION,
    EXPECTED_POST_SUCCESS_REVISIONS,
    FORBIDDEN_PRE_ATTACH_TABLES,
    MANIFEST_SCHEMA_VERSION,
    MAX_MIGRATION_TIMEOUT_SEC,
    MIN_MIGRATION_TIMEOUT_SEC,
    REJECTED_START_REVISIONS,
    STAGING_ENVIRONMENT_ENV,
    STAGING_ENVIRONMENT_VALUE,
    STAGING_IDENTITY_CLASS,
    STAGING_PROJECT_ENV,
    STAGING_PROJECT_VALUE,
    TARGET_REVISION,
)

GateFailure = gates.GateFailure

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


def validate_dr_restore_profile_prerequisite() -> GateFailure | None:
    from scripts.operators.staging_dr_canonical_parity_contract import (  # noqa: PLC0415
        SOURCE_ELIGIBILITY_PROFILES,
    )

    if not any(
        profile.get("alembic_revision") == DR_RESTORE_PROFILE_REVISION
        for profile in SOURCE_ELIGIBILITY_PROFILES
    ):
        return GateFailure("dr_prerequisite_missing", "restore_profile_0088_not_in_contract")
    return None


def validate_forbidden_pre_attach_tables(conn: Connection) -> GateFailure | None:
    insp = inspect(conn)
    tables = set(insp.get_table_names())
    for forbidden in FORBIDDEN_PRE_ATTACH_TABLES:
        if forbidden in tables:
            return GateFailure("wrong_revision", "sibling_revision_0089_objects_present")
    return None


def validate_pre_attach_validated_invariants(conn: Connection) -> GateFailure | None:
    revisions = gates.read_alembic_revisions(conn)
    if not revisions:
        return GateFailure("wrong_revision", "alembic_version_missing")
    if revisions == EXPECTED_POST_SUCCESS_REVISIONS:
        return GateFailure("wrong_revision", "revision_already_both_heads")
    if revisions != frozenset({BASE_REVISION}):
        if revisions == frozenset({TARGET_REVISION}):
            return GateFailure("wrong_revision", "revision_is_0089_not_0088")
        if revisions == frozenset({"0087"}):
            return GateFailure("wrong_revision", "revision_is_0087_not_0088")
        if revisions & REJECTED_START_REVISIONS:
            return GateFailure("wrong_revision", "revision_not_exactly_0088")
        return GateFailure("wrong_revision", "revision_not_exactly_0088")

    row = conn.execute(
        _CAPABILITY_DETAIL_SQL,
        {"key": CAPABILITY_KEY},
    ).mappings().first()
    if row is None:
        return GateFailure("preflight_failed", "capability_state_missing")
    if row["state"] != CAPABILITY_STATE_VALIDATED:
        return GateFailure("preflight_failed", "capability_state_not_validated")
    if row["validation_revision"] != VALIDATION_REVISION:
        return GateFailure("preflight_failed", "capability_validation_revision_mismatch")

    return validate_post_success_validate_invariants(conn.engine)


def validate_post_success_0089_schema(conn: Connection) -> GateFailure | None:
    insp = inspect(conn)
    tables = set(insp.get_table_names())
    if CASB_TABLE not in tables:
        return GateFailure("post_validation_failed", "casb_table_missing")

    conversation_indexes = {idx.get("name") for idx in insp.get_indexes("conversations")}
    if CONVERSATIONS_COMPOSITE_INDEX not in conversation_indexes:
        return GateFailure("post_validation_failed", "conversations_composite_index_missing")

    casb_indexes = {idx.get("name") for idx in insp.get_indexes(CASB_TABLE)}
    for required_index in (CASB_PARTIAL_UNIQUE_INDEX, CASB_STATE_INDEX):
        if required_index not in casb_indexes:
            return GateFailure("post_validation_failed", "casb_index_missing")

    casb_fks = {fk.get("name") for fk in insp.get_foreign_keys(CASB_TABLE)}
    for required_fk in CASB_FOREIGN_KEYS:
        if required_fk not in casb_fks:
            return GateFailure("post_validation_failed", "casb_foreign_key_missing")

    casb_checks = {chk.get("name") for chk in insp.get_check_constraints(CASB_TABLE)}
    for required_check in CASB_CHECK_CONSTRAINTS:
        if required_check not in casb_checks:
            return GateFailure("post_validation_failed", "casb_check_missing")
    return None


def validate_post_success_attach_invariants(engine: Engine) -> GateFailure | None:
    with engine.connect() as conn:
        revision_failure = gates.validate_post_success_revisions(
            conn,
            expected=EXPECTED_POST_SUCCESS_REVISIONS,
            wrong_stage="alembic_version_not_both_heads",
        )
        if revision_failure:
            return revision_failure

        validated_failure = validate_post_success_validate_invariants(engine)
        if validated_failure:
            return validated_failure

        row = conn.execute(
            _CAPABILITY_DETAIL_SQL,
            {"key": CAPABILITY_KEY},
        ).mappings().first()
        if row is None:
            return GateFailure("post_validation_failed", "capability_state_missing")
        if row["state"] == CAPABILITY_STATE_EXPAND:
            return GateFailure("post_validation_failed", "capability_regressed_to_expand")
        if row["state"] != CAPABILITY_STATE_VALIDATED:
            return GateFailure("post_validation_failed", "capability_state_not_validated")
        if row["validation_revision"] != VALIDATION_REVISION:
            return GateFailure("post_validation_failed", "capability_validation_revision_mismatch")

        schema_failure = validate_post_success_0089_schema(conn)
        if schema_failure:
            return schema_failure
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
    require_dr_profile: bool = True,
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
    if require_dr_profile:
        failure = validate_dr_restore_profile_prerequisite()
        if failure:
            return None, failure

    try:
        with engine.connect() as conn:
            attach_failure = validate_pre_attach_validated_invariants(conn)
            if attach_failure:
                return None, attach_failure
            table_failure = validate_forbidden_pre_attach_tables(conn)
            if table_failure:
                return None, table_failure
            fingerprint = compute_public_schema_fingerprint(conn)
            revisions = sorted(gates.read_alembic_revisions(conn))
    except SQLAlchemyError:
        return None, GateFailure("database_operation_failed", "preflight_database")
    except Exception:  # noqa: silent-ok - boundary returns a closed safe token.
        return None, GateFailure("preflight_unexpected_error", "preflight_database")

    manifest = build_manifest(
        phase="preflight",
        alembic_revision=BASE_REVISION,
        fingerprint=fingerprint,
        destructive_preflight_counts={},
        staging_identity_class=STAGING_IDENTITY_CLASS,
        bootstrap_freeze=gates.truthy_env_from_map(env, BOOTSTRAP_FREEZE_ENV),
    )
    manifest["manifest_schema_version"] = MANIFEST_SCHEMA_VERSION
    manifest["target_revision"] = TARGET_REVISION
    manifest["alembic_revisions_observed"] = revisions
    manifest["expected_post_success_revisions"] = sorted(EXPECTED_POST_SUCCESS_REVISIONS)
    manifest["dr_restore_profile_revision"] = DR_RESTORE_PROFILE_REVISION
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
    for validator, uses_env in (
        (validate_staging_identity, True),
        (gates.validate_database_binding, True),
        (validate_bootstrap_freeze, True),
        (validate_confirmation, True),
        (validate_dr_restore_profile_prerequisite, False),
    ):
        failure = validator(env) if uses_env else validator()
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
        require_dr_profile=True,
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
        attach_failure = validate_post_success_attach_invariants(engine)
        if attach_failure:
            return None, attach_failure
        with engine.connect() as conn:
            fingerprint = compute_public_schema_fingerprint(conn)
            revisions = sorted(gates.read_alembic_revisions(conn))
    except SQLAlchemyError:
        return None, GateFailure("database_operation_failed", "post_validation_database")
    except Exception:  # noqa: silent-ok - boundary returns a closed safe token.
        return None, GateFailure("post_validation_unexpected_error", "post_validation_database")

    manifest = build_manifest(
        phase="post_success",
        alembic_revision=BASE_REVISION,
        fingerprint=fingerprint,
        destructive_preflight_counts={},
        staging_identity_class=STAGING_IDENTITY_CLASS,
        bootstrap_freeze=True,
        migration_outcome=migration_outcome,
    )
    manifest["manifest_schema_version"] = MANIFEST_SCHEMA_VERSION
    manifest["target_revision"] = TARGET_REVISION
    manifest["alembic_revisions_observed"] = revisions
    manifest["expected_post_success_revisions"] = sorted(EXPECTED_POST_SUCCESS_REVISIONS)
    manifest["restore_first_policy"] = (
        "On any post-migration validation failure, restore staging from the latest "
        "verified backup with DR profile staging_pin_0088 before retrying. "
        "Do not downgrade in place and do not re-run 0088 validation."
    )
    return manifest, None


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Guarded staging operator: attach Alembic 0089 onto validated 0088.",
        exit_on_error=False,
    )
    sub = parser.add_subparsers(dest="command")
    sub.add_parser("preflight", help="Read-only preflight manifest (staging identity required).")
    run_parser = sub.add_parser("run", help="Execute controlled migration after all gates pass.")
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

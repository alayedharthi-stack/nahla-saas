#!/usr/bin/env python3
"""Guarded staging operator: Alembic 0024 → 0030 only.

Read-only preflight manifest generation and optional controlled execution.
Designed for invocation inside the staging app container after merge/deploy.

This module never accepts arbitrary revisions, ``head``, or downgrade targets.
Boundary: stops at 0030 — 0031+ (customer duplicate gates) is out of scope.
"""
from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Mapping, Sequence

from sqlalchemy import create_engine, inspect, text
from sqlalchemy.engine import Connection, Engine
from sqlalchemy.engine.url import make_url
from sqlalchemy.exc import ArgumentError, SQLAlchemyError
from sqlalchemy.pool import NullPool

_REPO_ROOT = Path(__file__).resolve().parents[2]
_DATABASE_DIR = _REPO_ROOT / "database"

for _entry in (str(_REPO_ROOT), str(_REPO_ROOT / "backend"), str(_REPO_ROOT / "database")):
    if _entry not in sys.path:
        sys.path.insert(0, _entry)

from scripts.operators.schema_fingerprint import (  # noqa: E402
    build_manifest,
    compute_public_schema_fingerprint,
)
from scripts.operators.staging_migration_0024_to_0030_contract import (  # noqa: E402
    BASE_REVISION,
    BOOTSTRAP_FREEZE_ENV,
    CONFIRMATION_ENV,
    CONFIRMATION_TOKEN,
    DEFAULT_MIGRATION_TIMEOUT_SEC,
    ENGINE_BACKFILL_AUTOMATION_TYPES,
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

_FORBIDDEN_ENV_MARKERS = frozenset({"production", "prod", "live"})
_ALLOWED_STAGING_DATABASE_HOST = "postgres-staging.railway.internal"
_POSTGRES_SCHEMES = frozenset({"postgresql", "postgresql+psycopg2"})

_REVISION_SQL = text("SELECT version_num FROM alembic_version LIMIT 1")

_TABLE_EXISTS_SQL = text(
    """
    SELECT count(*)::int
    FROM information_schema.tables
    WHERE table_schema = 'public'
      AND table_name = :table_name
      AND table_type = 'BASE TABLE'
    """
)

_COLUMN_EXISTS_SQL = text(
    """
    SELECT count(*)::int
    FROM information_schema.columns
    WHERE table_schema = 'public'
      AND table_name = :table_name
      AND column_name = :column_name
    """
)

_ENGINE_BACKFILL_TYPE_LITERALS = ", ".join(
    f"'{automation_type}'" for automation_type in ENGINE_BACKFILL_AUTOMATION_TYPES
)
_ENGINE_BACKFILL_ROWS_SQL = text(
    f"""
    SELECT count(*)::int
    FROM smart_automations
    WHERE automation_type IN ({_ENGINE_BACKFILL_TYPE_LITERALS})
    """
)


@dataclass(frozen=True)
class GateFailure:
    error_class: str
    stage: str


def _truthy_env_from_map(env: Mapping[str, str], name: str) -> bool:
    return (env.get(name) or "").strip().lower() in ("1", "true", "yes")


def validate_staging_identity(env: Mapping[str, str] | None = None) -> GateFailure | None:
    """Require Railway staging identity; reject production/live markers."""
    env = env or os.environ
    project = (env.get(STAGING_PROJECT_ENV) or "").strip()
    environment = (env.get(STAGING_ENVIRONMENT_ENV) or "").strip().lower()
    generic_env = (env.get("ENVIRONMENT") or "").strip().lower()

    if not project:
        return GateFailure("identity_rejected", "staging_project_missing")
    if project != STAGING_PROJECT_VALUE:
        return GateFailure("identity_rejected", "staging_project_mismatch")
    if not environment:
        return GateFailure("identity_rejected", "staging_environment_missing")
    if environment != STAGING_ENVIRONMENT_VALUE:
        return GateFailure("identity_rejected", "staging_environment_mismatch")

    for marker in _FORBIDDEN_ENV_MARKERS:
        if marker in environment or marker in generic_env:
            return GateFailure("identity_rejected", "production_marker_detected")
    return None


def validate_database_binding(env: Mapping[str, str] | None = None) -> GateFailure | None:
    """Require a parseable PostgreSQL URL bound to the exact staging service."""
    env = env or os.environ
    raw_url = (env.get("DATABASE_URL") or "").strip()
    if not raw_url:
        return GateFailure("database_binding_rejected", "database_url_missing")
    try:
        parsed = make_url(raw_url)
    except (ArgumentError, ValueError, TypeError):
        return GateFailure("database_binding_rejected", "database_url_malformed")
    if parsed.drivername not in _POSTGRES_SCHEMES:
        return GateFailure("database_binding_rejected", "database_scheme_rejected")
    host = (parsed.host or "").lower()
    if not host:
        return GateFailure("database_binding_rejected", "database_host_missing")
    if any(marker in host for marker in _FORBIDDEN_ENV_MARKERS):
        return GateFailure("database_binding_rejected", "database_host_production_marker")
    if host != _ALLOWED_STAGING_DATABASE_HOST:
        return GateFailure("database_binding_rejected", "database_host_not_allowlisted")
    return None


def validate_bootstrap_freeze(env: Mapping[str, str] | None = None) -> GateFailure | None:
    env = env or os.environ
    if not _truthy_env_from_map(env, BOOTSTRAP_FREEZE_ENV):
        return GateFailure("bootstrap_freeze_missing", "bootstrap_not_frozen")
    return None


def validate_confirmation(env: Mapping[str, str] | None = None) -> GateFailure | None:
    env = env or os.environ
    token = (env.get(CONFIRMATION_ENV) or "").strip()
    if token != CONFIRMATION_TOKEN:
        return GateFailure("confirmation_missing", "dangerous_action_not_confirmed")
    return None


def read_alembic_revision(conn: Connection) -> str | None:
    row = conn.execute(_REVISION_SQL).first()
    return row[0] if row else None


def validate_start_revision(conn: Connection) -> GateFailure | None:
    current = read_alembic_revision(conn)
    if current is None:
        return GateFailure("wrong_revision", "alembic_version_missing")
    if current != BASE_REVISION:
        return GateFailure("wrong_revision", "start_revision_not_0024")
    return None


def _table_exists(conn: Connection, table_name: str) -> bool:
    return int(conn.execute(_TABLE_EXISTS_SQL, {"table_name": table_name}).scalar_one()) > 0


def _column_exists(conn: Connection, table_name: str, column_name: str) -> bool:
    return (
        int(
            conn.execute(
                _COLUMN_EXISTS_SQL,
                {"table_name": table_name, "column_name": column_name},
            ).scalar_one()
        )
        > 0
    )


def collect_destructive_preflight_counts(conn: Connection) -> dict[str, int]:
    """Aggregate-only drift / data-sensitive indicators for 0025–0030."""
    products_stock = int(
        _column_exists(conn, "products", "stock_quantity")
        and _column_exists(conn, "products", "in_stock")
    )
    orders_dashboard = sum(
        int(_column_exists(conn, "orders", column))
        for column in ("external_order_number", "customer_name", "source")
    )
    return {
        "preexisting_product_interests_table": int(_table_exists(conn, "product_interests")),
        "preexisting_promotions_table": int(_table_exists(conn, "promotions")),
        "preexisting_offer_decisions_table": int(_table_exists(conn, "offer_decisions")),
        "products_stock_columns_preexisting": products_stock,
        "orders_dashboard_columns_preexisting": orders_dashboard,
        "platform_tenant_column_preexisting": int(
            _column_exists(conn, "tenants", "is_platform_tenant")
        ),
        "smart_automation_engine_backfill_rows": int(
            conn.execute(_ENGINE_BACKFILL_ROWS_SQL).scalar_one()
        ),
    }


def build_alembic_upgrade_command(python_executable: str | None = None) -> list[str]:
    """Construct the only permitted Alembic invocation (fixed target 0030)."""
    exe = python_executable or sys.executable
    return [exe, "-m", "alembic", "upgrade", TARGET_REVISION]


def assert_upgrade_command_safe(cmd: Sequence[str]) -> None:
    joined = " ".join(cmd)
    if "head" in joined:
        raise ValueError("unsafe_command_contains_head")
    if list(cmd[-4:]) != ["-m", "alembic", "upgrade", TARGET_REVISION]:
        raise ValueError("unsafe_command_shape")


def execute_alembic_upgrade(
    *,
    timeout_sec: int,
    env: Mapping[str, str] | None = None,
    python_executable: str | None = None,
    runner: Callable[..., subprocess.CompletedProcess[str]] | None = None,
) -> dict[str, str]:
    binding_failure = validate_database_binding(env)
    if binding_failure:
        return {
            "outcome": "failed",
            "error_class": binding_failure.error_class,
            "stage": binding_failure.stage,
        }
    if not isinstance(timeout_sec, int) or isinstance(timeout_sec, bool) or timeout_sec <= 0:
        return {
            "outcome": "failed",
            "error_class": "invalid_timeout",
            "stage": "alembic_upgrade",
        }
    cmd = build_alembic_upgrade_command(python_executable)
    assert_upgrade_command_safe(cmd)
    run = runner or subprocess.run
    try:
        completed = run(
            cmd,
            cwd=str(_DATABASE_DIR),
            capture_output=True,
            text=True,
            timeout=timeout_sec,
            shell=False,
        )
    except subprocess.TimeoutExpired:
        return {
            "outcome": "timeout",
            "error_class": "migration_timeout",
            "stage": "alembic_upgrade",
        }
    except OSError:
        return {
            "outcome": "failed",
            "error_class": "migration_spawn_failed",
            "stage": "alembic_upgrade",
        }
    except Exception:  # noqa: silent-ok - boundary returns a closed safe token.
        return {
            "outcome": "failed",
            "error_class": "migration_unexpected_error",
            "stage": "alembic_upgrade",
        }

    if completed.returncode != 0:
        return {
            "outcome": "failed",
            "error_class": "migration_nonzero_exit",
            "stage": "alembic_upgrade",
        }
    return {
        "outcome": "success",
        "error_class": "none",
        "stage": "alembic_upgrade",
    }


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


def validate_post_success_revision(conn: Connection) -> GateFailure | None:
    current = read_alembic_revision(conn)
    if current != TARGET_REVISION:
        return GateFailure("post_validation_failed", "revision_not_0030")
    return None


def connect_engine(database_url: str | None = None) -> Engine:
    url = (database_url or os.environ.get("DATABASE_URL") or "").strip()
    if not url:
        raise ValueError("database_url_missing")
    return create_engine(url, poolclass=NullPool, pool_pre_ping=True)


def emit_safe_error(*, error_class: str, stage: str) -> int:
    print(
        json.dumps(
            {"outcome": "failed", "stage": stage, "error_class": error_class},
            sort_keys=True,
            separators=(",", ":"),
        )
    )
    return 1


def emit_manifest(manifest: dict[str, Any]) -> int:
    print(json.dumps(manifest, sort_keys=True, separators=(",", ":")))
    return 0


def run_preflight(
    engine: Engine,
    *,
    env: Mapping[str, str] | None = None,
    require_identity: bool = True,
    require_bootstrap_freeze: bool = False,
) -> tuple[dict[str, Any] | None, GateFailure | None]:
    env = env or os.environ
    if require_identity:
        failure = validate_staging_identity(env)
        if failure:
            return None, failure
    failure = validate_database_binding(env)
    if failure:
        return None, failure
    if require_bootstrap_freeze:
        failure = validate_bootstrap_freeze(env)
        if failure:
            return None, failure

    try:
        with engine.connect() as conn:
            revision_failure = validate_start_revision(conn)
            if revision_failure:
                return None, revision_failure
            fingerprint = compute_public_schema_fingerprint(conn)
            destructive = collect_destructive_preflight_counts(conn)
            revision = read_alembic_revision(conn) or BASE_REVISION
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
        bootstrap_freeze=_truthy_env_from_map(env, BOOTSTRAP_FREEZE_ENV),
    )
    return manifest, None


def run_controlled_migration(
    engine: Engine,
    *,
    timeout_sec: int,
    env: Mapping[str, str] | None = None,
    alembic_runner: Callable[..., subprocess.CompletedProcess[str]] | None = None,
) -> tuple[dict[str, Any] | None, GateFailure | None]:
    env = env or os.environ

    for validator in (
        validate_staging_identity,
        validate_database_binding,
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
        return None, GateFailure(
            migration_outcome["error_class"],
            migration_outcome["stage"],
        )

    try:
        with engine.connect() as conn:
            revision_failure = validate_post_success_revision(conn)
            if revision_failure:
                return None, revision_failure
            fingerprint = compute_public_schema_fingerprint(conn)
            destructive = collect_destructive_preflight_counts(conn)
            revision = read_alembic_revision(conn) or TARGET_REVISION
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
        description="Guarded staging operator: Alembic 0024 → 0030 only.",
        exit_on_error=False,
    )
    sub = parser.add_subparsers(dest="command")

    sub.add_parser(
        "preflight",
        help="Read-only preflight manifest (staging identity required).",
    )

    run_parser = sub.add_parser(
        "run",
        help="Execute controlled migration after all safety gates pass.",
    )
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
        return emit_safe_error(error_class="invalid_command", stage="cli")
    if args.command not in {"preflight", "run"} or unknown_args:
        return emit_safe_error(error_class="invalid_command", stage="cli")

    identity_failure = validate_staging_identity()
    if identity_failure:
        return emit_safe_error(error_class=identity_failure.error_class, stage=identity_failure.stage)

    database_failure = validate_database_binding()
    if database_failure:
        return emit_safe_error(error_class=database_failure.error_class, stage=database_failure.stage)

    if args.command == "run":
        for validator in (validate_bootstrap_freeze, validate_confirmation):
            failure = validator()
            if failure:
                return emit_safe_error(error_class=failure.error_class, stage=failure.stage)

    try:
        engine = connect_engine()
    except (ValueError, SQLAlchemyError):
        return emit_safe_error(error_class="database_connection_failed", stage="database_connect")
    except Exception:  # noqa: silent-ok - top-level boundary returns a closed safe token.
        return emit_safe_error(error_class="unexpected_error", stage="database_connect")

    if args.command == "preflight":
        try:
            manifest, failure = run_preflight(
                engine,
                require_identity=True,
                require_bootstrap_freeze=False,
            )
        except Exception:  # noqa: silent-ok - top-level boundary returns a closed safe token.
            return emit_safe_error(error_class="unexpected_error", stage="preflight")
        if failure:
            return emit_safe_error(error_class=failure.error_class, stage=failure.stage)
        assert manifest is not None
        return emit_manifest(manifest)

    if args.command == "run":
        try:
            manifest, failure = run_controlled_migration(
                engine,
                timeout_sec=args.timeout_sec,
            )
        except Exception:  # noqa: silent-ok - top-level boundary returns a closed safe token.
            return emit_safe_error(error_class="unexpected_error", stage="controlled_migration")
        if failure:
            return emit_safe_error(error_class=failure.error_class, stage=failure.stage)
        assert manifest is not None
        return emit_manifest(manifest)

    return emit_safe_error(error_class="invalid_command", stage="cli")


if __name__ == "__main__":
    raise SystemExit(main())

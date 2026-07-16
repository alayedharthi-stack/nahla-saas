"""Shared safety gates for guarded staging migration operators."""
from __future__ import annotations

import json
import os
import subprocess
import sys
from dataclasses import dataclass
from typing import Any, Callable, Mapping, Sequence

from sqlalchemy import create_engine, text
from sqlalchemy.engine import Connection, Engine
from sqlalchemy.engine.url import make_url
from sqlalchemy.exc import ArgumentError, SQLAlchemyError
from sqlalchemy.pool import NullPool

_FORBIDDEN_ENV_MARKERS = frozenset({"production", "prod", "live"})
_ALLOWED_STAGING_DATABASE_HOST = "postgres-staging.railway.internal"
_POSTGRES_SCHEMES = frozenset({"postgresql", "postgresql+psycopg2"})

_REVISION_SQL = text("SELECT version_num FROM alembic_version LIMIT 1")


@dataclass(frozen=True)
class GateFailure:
    error_class: str
    stage: str


def truthy_env_from_map(env: Mapping[str, str], name: str) -> bool:
    return (env.get(name) or "").strip().lower() in ("1", "true", "yes")


def validate_staging_identity(
    env: Mapping[str, str] | None,
    *,
    staging_project_env: str,
    staging_environment_env: str,
    staging_project_value: str,
    staging_environment_value: str,
) -> GateFailure | None:
    env = env or os.environ
    project = (env.get(staging_project_env) or "").strip()
    environment = (env.get(staging_environment_env) or "").strip().lower()
    generic_env = (env.get("ENVIRONMENT") or "").strip().lower()

    if not project:
        return GateFailure("identity_rejected", "staging_project_missing")
    if project != staging_project_value:
        return GateFailure("identity_rejected", "staging_project_mismatch")
    if not environment:
        return GateFailure("identity_rejected", "staging_environment_missing")
    if environment != staging_environment_value:
        return GateFailure("identity_rejected", "staging_environment_mismatch")

    for marker in _FORBIDDEN_ENV_MARKERS:
        if marker in environment or marker in generic_env:
            return GateFailure("identity_rejected", "production_marker_detected")
    return None


def validate_database_binding(env: Mapping[str, str] | None = None) -> GateFailure | None:
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


def validate_bootstrap_freeze(env: Mapping[str, str] | None, *, bootstrap_freeze_env: str) -> GateFailure | None:
    env = env or os.environ
    if not truthy_env_from_map(env, bootstrap_freeze_env):
        return GateFailure("bootstrap_freeze_missing", "bootstrap_not_frozen")
    return None


def validate_confirmation(
    env: Mapping[str, str] | None,
    *,
    confirmation_env: str,
    confirmation_token: str,
) -> GateFailure | None:
    env = env or os.environ
    token = (env.get(confirmation_env) or "").strip()
    if token != confirmation_token:
        return GateFailure("confirmation_missing", "dangerous_action_not_confirmed")
    return None


def read_alembic_revision(conn: Connection) -> str | None:
    row = conn.execute(_REVISION_SQL).first()
    return row[0] if row else None


def validate_start_revision(
    conn: Connection,
    *,
    base_revision: str,
    wrong_stage: str,
) -> GateFailure | None:
    current = read_alembic_revision(conn)
    if current is None:
        return GateFailure("wrong_revision", "alembic_version_missing")
    if current != base_revision:
        return GateFailure("wrong_revision", wrong_stage)
    return None


def validate_post_success_revision(
    conn: Connection,
    *,
    target_revision: str,
    wrong_stage: str,
) -> GateFailure | None:
    current = read_alembic_revision(conn)
    if current != target_revision:
        return GateFailure("post_validation_failed", wrong_stage)
    return None


def build_alembic_upgrade_command(target_revision: str, python_executable: str | None = None) -> list[str]:
    exe = python_executable or sys.executable
    return [exe, "-m", "alembic", "upgrade", target_revision]


def assert_upgrade_command_safe(cmd: Sequence[str], *, target_revision: str) -> None:
    joined = " ".join(cmd)
    if "head" in joined:
        raise ValueError("unsafe_command_contains_head")
    if list(cmd[-4:]) != ["-m", "alembic", "upgrade", target_revision]:
        raise ValueError("unsafe_command_shape")


def execute_alembic_upgrade(
    *,
    target_revision: str,
    database_dir: str,
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
    cmd = build_alembic_upgrade_command(target_revision, python_executable)
    assert_upgrade_command_safe(cmd, target_revision=target_revision)
    run = runner or subprocess.run
    try:
        completed = run(
            cmd,
            cwd=database_dir,
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

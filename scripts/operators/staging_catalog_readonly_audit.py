"""Read-only staging catalog / schema audit for guarded migration preflight.

Aggregate-only indicators — no PII, tenant IDs, phone numbers, or DSNs in output.
Safe to invoke from Stage C (0083→0087) preflight inside the staging app container.
"""
from __future__ import annotations

import hashlib
import json
import os
import sys
from pathlib import Path
from typing import Any, Mapping

from sqlalchemy import create_engine, text
from sqlalchemy.engine import Connection
from sqlalchemy.pool import NullPool

_REPO_ROOT = Path(__file__).resolve().parents[2]
for _entry in (str(_REPO_ROOT), str(_REPO_ROOT / "backend"), str(_REPO_ROOT / "database")):
    if _entry not in sys.path:
        sys.path.insert(0, _entry)

from scripts.operators.staging_migration_operator_gates import (  # noqa: E402
    GateFailure,
    emit_manifest,
    emit_safe_error,
    validate_database_binding,
    validate_staging_identity,
)
from scripts.operators.staging_migration_0083_to_0087_contract import (  # noqa: E402
    BOOTSTRAP_FREEZE_ENV,
    CATALOG_AUDIT_FORBIDDEN_INDICATORS,
    STAGING_ENVIRONMENT_ENV,
    STAGING_ENVIRONMENT_VALUE,
    STAGING_IDENTITY_CLASS,
    STAGING_PROJECT_ENV,
    STAGING_PROJECT_VALUE,
)

_PUBLIC_TABLES_SQL = text(
    """
    SELECT relname
    FROM pg_class c
    JOIN pg_namespace n ON n.oid = c.relnamespace
    WHERE n.nspname = 'public'
      AND c.relkind = 'r'
    ORDER BY relname
    """
)

_TABLE_EXISTS_SQL = text(
    """
    SELECT count(*)::int
    FROM information_schema.tables
    WHERE table_schema = 'public'
      AND table_name = :table_name
      AND table_type = 'BASE TABLE'
    """
)


def _table_exists(conn: Connection, table_name: str) -> bool:
    return int(conn.execute(_TABLE_EXISTS_SQL, {"table_name": table_name}).scalar_one()) > 0


def collect_catalog_audit_indicators(conn: Connection) -> dict[str, int]:
    """Return 0/1 presence flags for schema objects relevant to Stage C gate."""
    return {
        indicator: int(_table_exists(conn, indicator))
        for indicator in CATALOG_AUDIT_FORBIDDEN_INDICATORS
    }


def build_catalog_audit_manifest(
    conn: Connection,
    *,
    env: Mapping[str, str] | None = None,
) -> dict[str, Any]:
    env = env or os.environ
    tables = [row[0] for row in conn.execute(_PUBLIC_TABLES_SQL)]
    canonical = ",".join(tables)
    digest = hashlib.sha256(canonical.encode("utf-8")).hexdigest()
    revision_row = conn.execute(text("SELECT version_num FROM alembic_version LIMIT 1")).first()
    revision = revision_row[0] if revision_row else None
    indicators = collect_catalog_audit_indicators(conn)
    forbidden_present = sum(indicators.values())
    return {
        "manifest_schema_version": "staging_catalog_audit_v1",
        "phase": "catalog_audit",
        "alembic_revision": revision,
        "public_table_count": len(tables),
        "schema_fingerprint": digest,
        "schema_fingerprint_display": digest[:16],
        "catalog_audit_indicators": indicators,
        "forbidden_a1_objects_present": forbidden_present,
        "staging_identity_class": STAGING_IDENTITY_CLASS,
        "bootstrap_freeze": (env.get(BOOTSTRAP_FREEZE_ENV) or "").strip().lower() in ("1", "true", "yes"),
    }


def validate_catalog_audit_gate(conn: Connection) -> GateFailure | None:
    """Reject when A1-expand or later objects already exist before Stage C GO."""
    indicators = collect_catalog_audit_indicators(conn)
    if any(value > 0 for value in indicators.values()):
        return GateFailure("catalog_audit_rejected", "forbidden_a1_objects_present")
    return None


def main() -> int:
    identity_failure = validate_staging_identity(
        os.environ,
        staging_project_env=STAGING_PROJECT_ENV,
        staging_environment_env=STAGING_ENVIRONMENT_ENV,
        staging_project_value=STAGING_PROJECT_VALUE,
        staging_environment_value=STAGING_ENVIRONMENT_VALUE,
    )
    if identity_failure:
        return emit_safe_error(error_class=identity_failure.error_class, stage=identity_failure.stage)

    binding_failure = validate_database_binding()
    if binding_failure:
        return emit_safe_error(error_class=binding_failure.error_class, stage=binding_failure.stage)

    try:
        engine = create_engine(
            (os.environ.get("DATABASE_URL") or "").strip(),
            poolclass=NullPool,
            pool_pre_ping=True,
        )
        with engine.connect() as conn:
            manifest = build_catalog_audit_manifest(conn)
    except Exception:  # noqa: silent-ok - boundary returns a closed safe token.
        return emit_safe_error(error_class="catalog_audit_failed", stage="catalog_audit_database")

    return emit_manifest(manifest)


if __name__ == "__main__":
    raise SystemExit(main())

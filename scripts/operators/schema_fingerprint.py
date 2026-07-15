"""Canonical public-schema fingerprint for migration preflight and DR operators."""
from __future__ import annotations

import hashlib
from datetime import datetime, timezone
from typing import Any, Mapping

from sqlalchemy import text
from sqlalchemy.engine import Connection

SCHEMA_FINGERPRINT_VERSION = "nahla_public_tables_sha256_v1"
MANIFEST_SCHEMA_VERSION = "staging_migration_manifest_v1"
FINGERPRINT_DISPLAY_CHARS = 16

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

_PUBLIC_TABLE_COUNT_SQL = text(
    """
    SELECT count(*)::int
    FROM information_schema.tables
    WHERE table_schema = 'public'
      AND table_type = 'BASE TABLE'
    """
)


def compute_public_schema_fingerprint(conn: Connection) -> dict[str, Any]:
    """SHA-256 over comma-joined sorted public table names (full hash retained)."""
    tables = [row[0] for row in conn.execute(_PUBLIC_TABLES_SQL)]
    canonical = ",".join(tables)
    digest = hashlib.sha256(canonical.encode("utf-8")).hexdigest()
    return {
        "schema_fingerprint_version": SCHEMA_FINGERPRINT_VERSION,
        "public_table_count": len(tables),
        "schema_fingerprint": digest,
        "schema_fingerprint_display": digest[:FINGERPRINT_DISPLAY_CHARS],
    }


def read_public_table_count(conn: Connection) -> int:
    return int(conn.execute(_PUBLIC_TABLE_COUNT_SQL).scalar_one())


def build_manifest(
    *,
    phase: str,
    alembic_revision: str,
    fingerprint: Mapping[str, Any],
    destructive_preflight_counts: Mapping[str, int],
    staging_identity_class: str,
    bootstrap_freeze: bool,
    salla_preflight_outcome: str | None = None,
    post_validation: Mapping[str, Any] | None = None,
    migration_outcome: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    """Return a versioned, sanitized manifest dict (no PII, IDs, or secrets)."""
    manifest: dict[str, Any] = {
        "manifest_schema_version": MANIFEST_SCHEMA_VERSION,
        "schema_fingerprint_version": fingerprint["schema_fingerprint_version"],
        "phase": phase,
        "alembic_revision": alembic_revision,
        "public_table_count": fingerprint["public_table_count"],
        "schema_fingerprint": fingerprint["schema_fingerprint"],
        "schema_fingerprint_display": fingerprint["schema_fingerprint_display"],
        "destructive_preflight_counts": dict(destructive_preflight_counts),
        "timestamp_utc": datetime.now(timezone.utc).replace(microsecond=0).isoformat(),
        "staging_identity_class": staging_identity_class,
        "bootstrap_freeze": bootstrap_freeze,
    }
    if salla_preflight_outcome is not None:
        manifest["salla_preflight_outcome"] = salla_preflight_outcome
    if post_validation is not None:
        manifest["post_validation"] = dict(post_validation)
    if migration_outcome is not None:
        manifest["migration_outcome"] = dict(migration_outcome)
    return manifest

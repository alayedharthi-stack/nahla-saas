"""Dedicated explicit 0112 operator; idle until the verified target is armed."""
from __future__ import annotations

import hashlib
import importlib.util
import json
import os
from pathlib import Path
import subprocess
import sys

from sqlalchemy import create_engine, inspect, text
from sqlalchemy.engine import make_url

ROOT = Path(__file__).resolve().parents[2]
MIGRATION = ROOT / "database/migrations/versions/0112_salla_shipment_tracking.py"
EXPECTED_DIGEST = "76b55bd3ff29e0353e756cabea93f70da23ff60cdbc96596bf1960affaba46b2"
BEFORE = {"0110", "0111"}
AFTER = {"0111", "0112"}


def emit(status, **facts):
    print(json.dumps({"status": status, **facts}, sort_keys=True), flush=True)


def validated_url(env):
    url = make_url(env["DATABASE_URL"])
    identity = f"{url.host}:{url.port or 5432}/{url.database}"
    if (url.get_backend_name() != "postgresql" or not url.host
            or url.host in {"localhost", "127.0.0.1", "::1"}
            or url.query or not url.username or not url.password
            or identity != env.get("NAHLA_0112_TARGET")):
        raise RuntimeError("target_not_verified")
    if any(key.startswith("PG") and value for key, value in env.items()):
        raise RuntimeError("inherited_libpq_override")
    return url, identity


def read_state(connection, migration):
    connection.execute(text("SET TRANSACTION READ ONLY"))
    versions = set(connection.execute(text("SELECT version_num FROM alembic_version")).scalars())
    migration._assert_existing_table_matches_foundation(connection)
    columns = {row["name"] for row in inspect(connection).get_columns("order_shipments")}
    unique = migration._has_unique_columns(inspect(connection), migration._UNIQUE,
                                         ("tenant_id", "tracking_data_source", "external_shipment_id"))
    connection.rollback()
    return versions, columns, unique


def main():
    if os.environ.get("NAHLA_0112_CONFIRM") != "APPLY_VERIFIED_0112":
        emit("idle", reason="explicit_confirmation_not_set")
        return 0
    url, identity = validated_url(os.environ)
    if hashlib.sha256(MIGRATION.read_bytes()).hexdigest() != EXPECTED_DIGEST:
        raise RuntimeError("reviewed_migration_digest_mismatch")
    spec = importlib.util.spec_from_file_location("reviewed_0112", MIGRATION)
    migration = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(migration)
    if migration.revision != "0112" or migration.down_revision != "0110":
        raise RuntimeError("unexpected_migration_ancestry")
    engine = create_engine(url, connect_args={"connect_timeout": 15})
    try:
        with engine.connect() as connection:
            versions, columns, unique = read_state(connection, migration)
        if versions == AFTER:
            if not set(migration._TRACKING_COLUMNS).issubset(columns) or not unique:
                raise RuntimeError("applied_revision_schema_mismatch")
            emit("already_applied", target=identity, alembic_versions=sorted(versions))
            return 0
        if versions != BEFORE:
            raise RuntimeError("unexpected_starting_revisions")
        emit("preflight_passed", target=identity, alembic_versions=sorted(versions),
             migration_sha256=EXPECTED_DIGEST)
        env = dict(os.environ)
        env["PGOPTIONS"] = "-c lock_timeout=5s -c statement_timeout=120s"
        result = subprocess.run([sys.executable, "-m", "alembic", "upgrade", "0112"],
                                cwd=ROOT / "database", env=env, capture_output=True,
                                text=True, timeout=150, check=False)
        for line in result.stderr.splitlines():
            if line.startswith("INFO  [alembic.runtime.migration]"):
                print(line, flush=True)
        if result.returncode:
            emit("migration_failed", returncode=result.returncode)
            return 1
        with engine.connect() as connection:
            versions, columns, unique = read_state(connection, migration)
        if versions != AFTER or not set(migration._TRACKING_COLUMNS).issubset(columns) or not unique:
            raise RuntimeError("post_migration_verification_failed")
        emit("verified", target=identity, alembic_versions=sorted(versions),
             tracking_columns=sorted(migration._TRACKING_COLUMNS), tracking_unique_verified=unique)
        return 0
    finally:
        engine.dispose()


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except Exception as exc:
        emit("failed", error_type=type(exc).__name__)
        raise SystemExit(1) from None

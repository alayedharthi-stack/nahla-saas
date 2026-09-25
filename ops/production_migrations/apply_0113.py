"""Dedicated explicit 0113 operator; idle until the verified target is armed.

Modes, by ``NAHLA_0113_CONFIRM``:

* unset / anything else — idle: prints one line and never connects;
* ``INSPECT`` — read-only: the target's identity (host:port/database, never a
  credential), its Alembic revisions and whether the navigation relation
  exists, inside a READ ONLY transaction;
* ``APPLY_VERIFIED_0113`` — applies only ``0113`` to the target named by
  ``NAHLA_0113_TARGET``, from exactly ``{0111, 0112}``, with a bounded lock
  wait; verifies the relation by definition afterwards and runs one bounded
  cleanup pass through the application's own sweep.

Every reviewed file the migration executes is pinned by digest. Every step is
bounded: the connection by a connect and TCP timeout, every read by a lock and
statement timeout, and the whole run by a watchdog that reports the step it
stopped in; each step announces itself, so a run that stops says where.
"""
from __future__ import annotations

import hashlib
import importlib.util
import json
import os
from pathlib import Path
import subprocess
import sys
import threading

from sqlalchemy import create_engine, text
from sqlalchemy.engine import make_url
from sqlalchemy.exc import OperationalError

ROOT = Path(__file__).resolve().parents[2]
for _path in (ROOT, ROOT / "backend", ROOT / "database"):
    if str(_path) not in sys.path:
        sys.path.insert(0, str(_path))

MIGRATION = ROOT / "database/migrations/versions/0113_commerce_runtime_navigation.py"
REVIEWED = {
    MIGRATION: "83f9946bd20f30630dfb0a21f146865e793da0eb8f0252d6e828cb385a0599da",
    ROOT / "backend/core/commerce_runtime/navigation_models.py":
        "f784c41976771987c0e157747270fc08d2b5feaa7f535779e64310fff83bb8a3",
    ROOT / "backend/core/commerce_runtime/models.py":
        "9b1ccf18c10bbc2cf0ba96e1779f0d8b474debab1122c424a5d137d3d4628c0e",
    ROOT / "database/runtime_schema_guarantees.py":
        "bfeeba90a5af4bc4fdcbc55fbec8d2e8a64a505abdc960fcb776a8af6f213049",
}
TABLE = "commerce_runtime_navigation_snapshots"
BEFORE = {"0111", "0112"}
AFTER = {"0111", "0113"}
APPLY = "APPLY_VERIFIED_0113"
INSPECT = "INSPECT"
WATCHDOG_SECONDS = {INSPECT: 120, APPLY: 600}
CONNECT_ARGS = {"connect_timeout": 15, "keepalives": 1, "keepalives_idle": 15,
                "keepalives_interval": 5, "keepalives_count": 3, "tcp_user_timeout": 30000}
_STEP = ["start"]
# lock_not_available, query_canceled: a read that waited out its bound.
LOCK_WAIT_SQLSTATES = {"55P03", "57014"}
# Who holds or awaits a lock on the relations this operator reads: the kind of
# statement only, never its text, which can carry data.
LOCK_HOLDERS = text("""
    SELECT a.pid, a.backend_type, a.application_name, a.state, host(a.client_addr) AS client,
           c.relname AS relation, l.mode, l.granted,
           extract(epoch FROM now() - a.xact_start)::int AS transaction_age_s,
           extract(epoch FROM now() - a.state_change)::int AS state_age_s,
           a.wait_event_type, a.wait_event,
           upper(split_part(ltrim(a.query), ' ', 1)) AS statement_kind
    FROM pg_locks l
    JOIN pg_class c ON c.oid = l.relation
    JOIN pg_stat_activity a ON a.pid = l.pid
    WHERE l.locktype = 'relation' AND a.pid <> pg_backend_pid()
      AND c.relname IN ('alembic_version', :table)
    ORDER BY l.granted DESC, a.xact_start
    LIMIT 20""")


def emit(status, **facts):
    print(json.dumps({"status": status, **facts}, sort_keys=True), flush=True)


def step(name):
    _STEP[0] = name
    emit("step", step=name)


def arm_watchdog(seconds):
    """Ends a run that stops anywhere, even inside a blocking driver call.

    A thread rather than a signal: libpq retries an interrupted wait, so a
    signal handler would never run while a read is blocked, but the driver
    releases the interpreter lock while it waits, so a thread always can.
    """
    def expired():
        emit("failed", error_type="WatchdogExpired", error="watchdog_expired:" + _STEP[0],
             sqlstate="")
        os._exit(1)

    timer = threading.Timer(seconds, expired)
    timer.daemon = True
    timer.start()
    return timer


def validated_url(env, *, require_target):
    url = make_url(env["DATABASE_URL"])
    identity = f"{url.host}:{url.port or 5432}/{url.database}"
    if (url.get_backend_name() != "postgresql" or not url.host
            or url.host in {"localhost", "127.0.0.1", "::1"}
            or url.query or not url.username or not url.password):
        raise RuntimeError("target_not_verified")
    if require_target and identity != env.get("NAHLA_0113_TARGET"):
        raise RuntimeError("target_not_verified")
    if any(key.startswith("PG") and value for key, value in env.items()):
        raise RuntimeError("inherited_libpq_override")
    return url, identity


def verify_reviewed_files():
    for path, digest in REVIEWED.items():
        if hashlib.sha256(path.read_bytes()).hexdigest() != digest:
            raise RuntimeError("reviewed_file_digest_mismatch:" + path.name)


def load_migration():
    spec = importlib.util.spec_from_file_location("reviewed_0113", MIGRATION)
    migration = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(migration)
    if migration.revision != "0113" or migration.down_revision != "0112":
        raise RuntimeError("unexpected_migration_ancestry")
    return migration


def read_state(connection, migration, *, compare=True):
    """Revisions, whether the relation exists, and how it differs from its definition.

    Without ``compare`` the transaction is READ ONLY. With it, the by-definition
    comparison builds its reference in a scratch schema — exactly as the
    migration verifies itself — so the transaction is an ordinary one, bounded
    by a lock wait, and always rolled back: nothing it creates persists.
    """
    if not compare:
        connection.execute(text("SET TRANSACTION READ ONLY"))
    connection.execute(text("SET LOCAL lock_timeout = '5s'"))
    connection.execute(text("SET LOCAL statement_timeout = '60s'"))
    try:
        versions = set(connection.execute(text("SELECT version_num FROM alembic_version")).scalars())
        exists = connection.execute(text("SELECT to_regclass(:t) IS NOT NULL"),
                                    {"t": "public." + TABLE}).scalar()
        diffs = []
        if exists and compare:
            shared = migration._shared()
            for table in migration._navigation_tables():
                diffs.extend(shared.differences(connection, table))
        return versions, bool(exists), diffs
    finally:
        connection.rollback()


def report_lock_holders(engine):
    """After a bounded read gave up waiting: who it was waiting behind."""
    try:
        with engine.connect() as connection:
            connection.execute(text("SET TRANSACTION READ ONLY"))
            connection.execute(text("SET LOCAL statement_timeout = '10s'"))
            holders = [dict(row._mapping) for row in connection.execute(LOCK_HOLDERS, {"table": TABLE})]
            connection.rollback()
        emit("lock_holders", holders=holders)
    except Exception as exc:  # noqa: BLE001 - a diagnostic never replaces the failure it explains
        emit("lock_holders_unavailable", error_type=type(exc).__name__)


def bounded_sweep(engine):
    """One pass of the application's own cleanup, within its own bounds."""
    step("cleanup")
    from core.commerce_runtime import navigation as nav  # noqa: PLC0415

    nav.reset_schema_probe()
    if not nav.schema_available(engine):
        raise RuntimeError("schema_probe_refused_after_migration")
    removed = nav.sweep_until_clean(engine)
    return {"removed": int(removed), "batch": nav.SWEEP_BATCH,
            "max_batches": nav.SWEEP_MAX_BATCHES_PER_TICK}


def main():
    mode = os.environ.get("NAHLA_0113_CONFIRM")
    if mode not in {APPLY, INSPECT}:
        emit("idle", reason="explicit_confirmation_not_set")
        return 0
    watchdog = arm_watchdog(WATCHDOG_SECONDS[mode])
    url, identity = validated_url(os.environ, require_target=mode == APPLY)
    verify_reviewed_files()
    migration = load_migration()
    engine = create_engine(url, connect_args=CONNECT_ARGS)
    try:
        step("read_state")
        try:
            with engine.connect() as connection:
                versions, exists, diffs = read_state(connection, migration, compare=mode != INSPECT)
        except OperationalError as exc:
            if getattr(exc.orig, "pgcode", None) in LOCK_WAIT_SQLSTATES:
                report_lock_holders(engine)
            raise
        if mode == INSPECT:
            emit("inspected", target=identity, alembic_versions=sorted(versions),
                 navigation_relation_exists=exists)
            return 0
        if versions == AFTER:
            if not exists or diffs:
                raise RuntimeError("applied_revision_schema_mismatch")
            emit("already_applied", target=identity, alembic_versions=sorted(versions),
                 cleanup=bounded_sweep(engine))
            return 0
        if versions != BEFORE:
            raise RuntimeError("unexpected_starting_revisions")
        if exists:
            raise RuntimeError("relation_present_before_0113")
        emit("preflight_passed", target=identity, alembic_versions=sorted(versions),
             migration_sha256=REVIEWED[MIGRATION])
        step("migrate")
        env = dict(os.environ)
        env["PGOPTIONS"] = "-c lock_timeout=5s -c statement_timeout=120s"
        result = subprocess.run([sys.executable, "-m", "alembic", "upgrade", "0113"],
                                cwd=ROOT / "database", env=env, capture_output=True,
                                text=True, timeout=150, check=False)
        for line in result.stderr.splitlines():
            if line.startswith("INFO  [alembic.runtime.migration]"):
                print(line, flush=True)
        if result.returncode:
            emit("migration_failed", returncode=result.returncode)
            return 1
        step("verify")
        with engine.connect() as connection:
            versions, exists, diffs = read_state(connection, migration)
        if versions != AFTER or not exists or diffs:
            raise RuntimeError("post_migration_verification_failed")
        emit("verified", target=identity, alembic_versions=sorted(versions),
             navigation_relation_exists=exists, cleanup=bounded_sweep(engine))
        return 0
    finally:
        engine.dispose()
        watchdog.cancel()


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except Exception as exc:
        # Only the operator's own codes and PostgreSQL's SQLSTATE: never a
        # driver message, which can carry connection details.
        emit("failed", error_type=type(exc).__name__,
             error=str(exc)[:120] if isinstance(exc, RuntimeError) else "",
             sqlstate=str(getattr(getattr(exc, "orig", None), "pgcode", "") or ""))
        raise SystemExit(1) from None

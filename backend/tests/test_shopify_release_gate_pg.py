"""Shopify release gate on real PostgreSQL: exact phase matrix, footprint, drift, locks, timeouts, old-source startup.

Every case builds the deployed topology {0111, 0113, 0120} by genuine upgrades
on a disposable server and runs the gate **through its rendered trusted
command** (``bash -c``) with an explicit synthetic environment. The gate's
target validator refuses loopback hosts, so the disposable server must be
reachable through a non-loopback-spelled name given as
``NAHLA_RELEASE_GATE_PG_HOST_ALIAS``; without it (or without
``NAHLA_RELIABILITY_PG_ADMIN_DSN``) the module is skipped and reported as
skipped, never as passed. ``DATABASE_URL`` is never inherited: each run is
given the disposable database explicitly.
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
import time
from pathlib import Path

import pytest
from sqlalchemy import create_engine, text
from sqlalchemy.engine.url import make_url
from sqlalchemy.pool import NullPool

_REPO = Path(__file__).resolve().parents[2]
if str(_REPO) not in sys.path:
    sys.path.insert(0, str(_REPO))

from backend.tests import _alembic_topology_probe as topo  # noqa: E402
from backend.tests import _shopify_release_gate_harness as harness  # noqa: E402
from scripts.operators import shopify_release_gate as gate  # noqa: E402
from scripts.operators import shopify_release_gate_contract as k  # noqa: E402
from scripts.operators import shopify_release_gate_render as render  # noqa: E402
from scripts.operators.bootstrap_migration_contract import build_normal_bootstrap_upgrade_argv  # noqa: E402

ADMIN_URL = (os.environ.get("NAHLA_RELIABILITY_PG_ADMIN_DSN") or "").strip()
HOST_ALIAS = (os.environ.get("NAHLA_RELEASE_GATE_PG_HOST_ALIAS") or "").strip()
if not ADMIN_URL and os.environ.get("NAHLA_RELIABILITY_REQUIRE_PG") == "1":
    raise RuntimeError("NAHLA_RELIABILITY_REQUIRE_PG=1 but NAHLA_RELIABILITY_PG_ADMIN_DSN is not set")
if ADMIN_URL and not HOST_ALIAS and os.environ.get("NAHLA_RELIABILITY_REQUIRE_PG") == "1":
    raise RuntimeError("NAHLA_RELIABILITY_REQUIRE_PG=1 but NAHLA_RELEASE_GATE_PG_HOST_ALIAS is not set")
pytestmark = pytest.mark.skipif(not (ADMIN_URL and HOST_ALIAS),
                                reason="NAHLA_RELIABILITY_PG_ADMIN_DSN / NAHLA_RELEASE_GATE_PG_HOST_ALIAS not set (no disposable PostgreSQL)")

EXPECTATIONS = json.loads((_REPO / k.EXPECTATIONS_RELATIVE_PATH).read_text(encoding="utf-8"))


# ── Disposable database helpers ───────────────────────────────────────────────

def _admin(*statements: str) -> None:
    engine = create_engine(ADMIN_URL, poolclass=NullPool, isolation_level="AUTOCOMMIT")
    try:
        with engine.connect() as conn:
            for statement in statements:
                conn.execute(text(statement))
    finally:
        engine.dispose()


def _loopback_url(db: str) -> str:
    return make_url(ADMIN_URL).set(database=db).render_as_string(hide_password=False)


def _alias_url(db: str) -> str:
    return make_url(ADMIN_URL).set(host=HOST_ALIAS, database=db).render_as_string(hide_password=False)


def _target(db: str) -> str:
    port = make_url(ADMIN_URL).port or 5432
    return f"{HOST_ALIAS}:{port}/{db}"


def _fresh_db(prefix: str) -> str:
    name = f"{prefix}_{os.getpid()}_{int(time.time() * 1000) % 1000000}"
    _admin(f'DROP DATABASE IF EXISTS "{name}"', f'CREATE DATABASE "{name}"')
    return name


def _drop(name: str) -> None:
    _admin(f'DROP DATABASE IF EXISTS "{name}"')


def _alembic(db: str, *args: str, database_dir: Path = _REPO / "database") -> subprocess.CompletedProcess:
    env = {**os.environ, "DATABASE_URL": _loopback_url(db)}
    for variable in ("PGHOST", "PGPORT", "PGDATABASE", "PGSERVICE", "PGOPTIONS"):
        env.pop(variable, None)
    return subprocess.run([sys.executable, "-m", "alembic", *args], cwd=str(database_dir), env=env,
                          capture_output=True, text=True, timeout=600)


def _build_s0(db: str) -> None:
    for target in ("0093", "0111", "0113", "0120"):
        proc = _alembic(db, "upgrade", target)
        assert proc.returncode == 0, proc.stderr[-3000:]
    topo.assert_rows(_loopback_url(db), topo.S0, "S0 build")


def _sql(db: str, *statements: str):
    engine = create_engine(_loopback_url(db), poolclass=NullPool, future=True)
    try:
        with engine.begin() as conn:
            rows = None
            for statement in statements:
                rows = conn.execute(text(statement))
            return rows.fetchall() if rows is not None and rows.returns_rows else None
    finally:
        engine.dispose()


def _run(root: Path, phase: str, db: str, **kwargs):
    rc, out = harness.run_gate(root, phase, _alias_url(db), _target(db), **kwargs)
    return rc, out, harness.checks(out)


def _assert_pass(root: Path, phase: str, db: str, step: str) -> str:
    rc, out, checks = _run(root, phase, db)
    assert rc == 0 and harness.result(out) == k.PASS, f"{step}: {out[-2500:]}"
    assert all(status == k.PASS for status, _ in checks.values()), (step, checks)
    assert {"root", "release_sha", "inventory", "libraries", "graph", "identity", "database_target", "approval", "shopify_flags",
            "expectations", "connect", "lock", "server", "version_rows", "shopify_footprint", "retained_schema", "unlock"} <= set(checks), step
    assert "nahla_password" not in out and "postgresql://" not in out, step
    return out


def _assert_fail(root: Path, phase: str, db: str, step: str, *, check: str, reason: str, **kwargs) -> str:
    rc, out, checks = _run(root, phase, db, **kwargs)
    assert rc == 1 and harness.result(out) == k.FAIL, f"{step}: {out[-2500:]}"
    assert checks.get(check) == (k.FAIL, reason), (step, checks)
    assert "nahla_password" not in out and "postgresql://" not in out, step
    return out


@pytest.fixture(scope="module")
def gate_root(tmp_path_factory) -> Path:
    return harness.build_root(tmp_path_factory.mktemp("gate") / "root")


# ── Phase matrix, forward and back, exactly one phase accepted per state ──────

def test_phase_matrix_accepts_exactly_the_current_state_and_bootstrap_is_a_noop(gate_root):
    db = _fresh_db("gate_matrix")
    try:
        _build_s0(db)
        url = _loopback_url(db)
        baseline = topo.schema_snapshot(url)
        planted = topo.plant_sentinels(url)
        counts = topo.row_counts(url, topo.SENTINEL_TABLES)
        _assert_pass(gate_root, "S0", db, "S0 built")
        for other in ("S1", "S2", "S3"):
            _assert_fail(gate_root, other, db, f"{other} against S0", check="version_rows", reason=k.R_VERSION_ROWS_MISMATCH)

        assert _alembic(db, "upgrade", "0121").returncode == 0
        topo.assert_step(url, topo.S1, baseline, planted, counts, "S1")
        _assert_pass(gate_root, "S1", db, "S1 after upgrade 0121")
        out = _assert_fail(gate_root, "S0", db, "S0 command against S1", check="version_rows", reason=k.R_VERSION_ROWS_MISMATCH)
        assert harness.checks(out)["shopify_footprint"] == (k.FAIL, k.R_FOOTPRINT_UNEXPECTED)
        _assert_fail(gate_root, "S2", db, "S2 against S1", check="version_rows", reason=k.R_VERSION_ROWS_MISMATCH)
        # the normal bootstrap target of the new source is an ancestor of the retained 0111 row: a no-op on every phase
        for state, expected_rows in (("S1", topo.S1),):
            proc = subprocess.run(build_normal_bootstrap_upgrade_argv(python_executable=sys.executable), cwd=str(_REPO / "database"),
                                  env={**os.environ, "DATABASE_URL": url}, capture_output=True, text=True, timeout=600)
            assert proc.returncode == 0, proc.stderr[-2000:]
            topo.assert_step(url, expected_rows, baseline, planted, counts, f"{state} after bootstrap upgrade 0093")

        assert _alembic(db, "upgrade", "0122").returncode == 0
        topo.assert_step(url, topo.S2, baseline, planted, counts, "S2")
        _assert_pass(gate_root, "S2", db, "S2 after upgrade 0122")
        _assert_fail(gate_root, "S1", db, "S1 against S2", check="version_rows", reason=k.R_VERSION_ROWS_MISMATCH)
        _assert_fail(gate_root, "S3", db, "S3 against S2", check="version_rows", reason=k.R_VERSION_ROWS_MISMATCH)

        assert _alembic(db, "upgrade", "0123").returncode == 0
        topo.assert_step(url, topo.S3, baseline, planted, counts, "S3")
        _assert_pass(gate_root, "S3", db, "S3 after upgrade 0123")
        _assert_fail(gate_root, "S0", db, "S0 against S3 (old 462 state is not whitelisted)", check="version_rows", reason=k.R_VERSION_ROWS_MISMATCH)
        proc = subprocess.run(build_normal_bootstrap_upgrade_argv(python_executable=sys.executable), cwd=str(_REPO / "database"),
                              env={**os.environ, "DATABASE_URL": url}, capture_output=True, text=True, timeout=600)
        assert proc.returncode == 0
        topo.assert_step(url, topo.S3, baseline, planted, counts, "S3 after bootstrap upgrade 0093")

        for target, expected, phase in (("0123@-1", topo.S2, "S2"), ("0122@-1", topo.S1, "S1"), ("0121@-1", topo.S0, "S0")):
            assert _alembic(db, "downgrade", target).returncode == 0
            topo.assert_step(url, expected, baseline, planted, counts, f"after downgrade {target}")
            _assert_pass(gate_root, phase, db, f"{phase} after downgrade {target}")
        assert topo.schema_diff(topo.schema_snapshot(url), baseline) == []
    finally:
        _drop(db)


def test_partial_or_stray_footprint_is_refused(gate_root):
    db = _fresh_db("gate_footprint")
    try:
        _build_s0(db)
        assert _alembic(db, "upgrade", "0121").returncode == 0
        _assert_pass(gate_root, "S1", db, "S1")
        _sql(db, "CREATE TABLE shopify_leftover (id int)")
        _assert_fail(gate_root, "S1", db, "stray table", check="shopify_footprint", reason=k.R_FOOTPRINT_UNEXPECTED)
        _sql(db, "DROP TABLE shopify_leftover")
        _sql(db, "CREATE TYPE shopify_stray_type AS ENUM ('a')")
        _assert_fail(gate_root, "S1", db, "stray type", check="shopify_footprint", reason=k.R_FOOTPRINT_UNEXPECTED)
        _sql(db, "DROP TYPE shopify_stray_type")
        _sql(db, "CREATE FUNCTION shopify_stray_fn() RETURNS int LANGUAGE sql AS 'SELECT 1'")
        _assert_fail(gate_root, "S1", db, "stray function", check="shopify_footprint", reason=k.R_FOOTPRINT_UNEXPECTED)
        _sql(db, "DROP FUNCTION shopify_stray_fn()")
        _sql(db, f"ALTER TABLE shopify_connections ADD CONSTRAINT {k.CONNECTION_KEY_CONSTRAINT} UNIQUE (id, tenant_id)")
        _assert_fail(gate_root, "S1", db, "key ahead of its revision", check="shopify_footprint", reason=k.R_FOOTPRINT_KEY)
        _sql(db, f"ALTER TABLE shopify_connections DROP CONSTRAINT {k.CONNECTION_KEY_CONSTRAINT}")
        _assert_pass(gate_root, "S1", db, "S1 restored")
        _sql(db, "DROP TABLE shopify_connection_audit")
        out = _assert_fail(gate_root, "S1", db, "partial footprint", check="shopify_footprint", reason=k.R_FOOTPRINT_MISSING)
        assert '"missing":["shopify_connection_audit"]' in out
        # nor does any other phase accept the partial state
        for phase in ("S0", "S2", "S3"):
            _assert_fail(gate_root, phase, db, f"{phase} against partial", check="version_rows", reason=k.R_VERSION_ROWS_MISMATCH)
    finally:
        _drop(db)


def test_version_rows_malformed_states_are_refused_and_never_echoed(gate_root):
    db = _fresh_db("gate_rows")
    try:
        _build_s0(db)
        # a malformed table without its primary key can hold a duplicate row; the gate still refuses it
        _sql(db, "ALTER TABLE alembic_version DROP CONSTRAINT alembic_version_pkc", "INSERT INTO alembic_version (version_num) VALUES ('0120')")
        _assert_fail(gate_root, "S0", db, "duplicate row", check="version_rows", reason=k.R_VERSION_ROWS_DUPLICATE)
        _sql(db, "DELETE FROM alembic_version WHERE ctid = (SELECT max(ctid) FROM alembic_version WHERE version_num = '0120')",
             "ALTER TABLE alembic_version ADD CONSTRAINT alembic_version_pkc PRIMARY KEY (version_num)")
        secret_row = "p-not-a-real-secret-row-value"
        _sql(db, f"INSERT INTO alembic_version (version_num) VALUES ('{secret_row}')")
        out = _assert_fail(gate_root, "S0", db, "unknown row", check="version_rows", reason=k.R_VERSION_ROWS_UNKNOWN)
        assert secret_row not in out and k.UNKNOWN in out
        _sql(db, f"DELETE FROM alembic_version WHERE version_num = '{secret_row}'")
        _sql(db, "DELETE FROM alembic_version WHERE version_num = '0113'")
        _assert_fail(gate_root, "S0", db, "missing retained row", check="version_rows", reason=k.R_VERSION_ROWS_MISMATCH)
        _sql(db, "INSERT INTO alembic_version (version_num) VALUES ('0113')")
        _assert_pass(gate_root, "S0", db, "rows restored")
        _sql(db, "ALTER TABLE alembic_version RENAME TO alembic_version_moved")
        _assert_fail(gate_root, "S0", db, "table missing", check="version_rows", reason=k.R_VERSION_TABLE_MISSING)
        _sql(db, "ALTER TABLE alembic_version_moved RENAME TO alembic_version")
    finally:
        _drop(db)


def test_retained_definition_drift_is_refused_and_exact_restoration_passes(gate_root):
    db = _fresh_db("gate_drift")
    try:
        _build_s0(db)
        _assert_pass(gate_root, "S0", db, "S0")
        table = "commerce_runtime_handover_barrier"
        name, _kind, definition = next(c[:3] for c in EXPECTATIONS["tables"][table]["constraints"] if c[1] == "c")
        _sql(db, f"ALTER TABLE {table} DROP CONSTRAINT {name}", f"ALTER TABLE {table} ADD CONSTRAINT {name} CHECK (generation >= 0 AND generation <= 2147483647)")
        out = _assert_fail(gate_root, "S0", db, "same-name CHECK with a different predicate", check="retained_schema", reason=k.R_RETAINED_DEFINITION_DRIFT)
        assert f'"drift":["{table}"]' in out and "2147483647" not in out
        _sql(db, f"ALTER TABLE {table} DROP CONSTRAINT {name}", f"ALTER TABLE {table} ADD CONSTRAINT {name} {definition}")
        _assert_pass(gate_root, "S0", db, "CHECK restored")
        index_table = "catalog_channel_retirements"
        index_name, indexdef = next(i for i in EXPECTATIONS["tables"][index_table]["indexes"] if "UNIQUE" not in i[1])
        _sql(db, f"DROP INDEX {index_name}", indexdef.replace("(tenant_id, status)", "(status)"))
        _assert_fail(gate_root, "S0", db, "same-name index with different columns", check="retained_schema", reason=k.R_RETAINED_DEFINITION_DRIFT)
        _sql(db, f"DROP INDEX {index_name}", indexdef)
        _assert_pass(gate_root, "S0", db, "index restored")
        _sql(db, "ALTER TABLE meta_catalog_authorizations ALTER COLUMN granted_scopes TYPE jsonb USING granted_scopes::jsonb")
        _assert_fail(gate_root, "S0", db, "JSON column changed to JSONB", check="retained_schema", reason=k.R_RETAINED_DEFINITION_DRIFT)
        _sql(db, "ALTER TABLE meta_catalog_authorizations ALTER COLUMN granted_scopes TYPE json USING granted_scopes::json")
        _assert_pass(gate_root, "S0", db, "column type restored")
        _sql(db, "ALTER TABLE catalog_channel_retirements ALTER COLUMN catalog_id SET NOT NULL")
        _assert_fail(gate_root, "S0", db, "nullable catalog_id tightened", check="retained_schema", reason=k.R_RETAINED_DEFINITION_DRIFT)
        _sql(db, "ALTER TABLE catalog_channel_retirements ALTER COLUMN catalog_id DROP NOT NULL")
        _sql(db, "ALTER TABLE whatsapp_oauth_nonces DROP CONSTRAINT whatsapp_oauth_nonces_tenant_id_fkey",
             "ALTER TABLE whatsapp_oauth_nonces ADD CONSTRAINT whatsapp_oauth_nonces_tenant_id_fkey FOREIGN KEY (tenant_id) REFERENCES tenants(id)")
        out = _assert_fail(gate_root, "S0", db, "nonce FK without ON DELETE CASCADE", check="retained_schema", reason=k.R_RETAINED_DEFINITION_DRIFT)
        assert f'"drift":["{k.NONCE_TABLE}"]' in out
        _sql(db, "ALTER TABLE whatsapp_oauth_nonces DROP CONSTRAINT whatsapp_oauth_nonces_tenant_id_fkey",
             "ALTER TABLE whatsapp_oauth_nonces ADD CONSTRAINT whatsapp_oauth_nonces_tenant_id_fkey FOREIGN KEY (tenant_id) REFERENCES tenants(id) ON DELETE CASCADE")
        _sql(db, "ALTER INDEX ix_whatsapp_oauth_nonces_expires_at RENAME TO ix_nonce_expires_renamed")
        _assert_pass(gate_root, "S0", db, "nonce index renamed, same shape")
        _sql(db, "ALTER INDEX ix_nonce_expires_renamed RENAME TO ix_whatsapp_oauth_nonces_expires_at")
        _sql(db, "ALTER TABLE commerce_runtime_conversations RENAME CONSTRAINT uq_commerce_runtime_conversations_scope TO uq_scope_renamed")
        _assert_pass(gate_root, "S0", db, "conversation scope key renamed, same definition")
        _sql(db, "ALTER TABLE commerce_runtime_conversations RENAME CONSTRAINT uq_scope_renamed TO uq_commerce_runtime_conversations_scope")
        _sql(db, "ALTER SEQUENCE meta_catalog_authorizations_id_seq OWNED BY NONE")
        out = _assert_fail(gate_root, "S0", db, "sequence ownership detached", check="retained_schema", reason=k.R_RETAINED_IDENTITY_DRIFT)
        assert '"identity":["sequence_ownership"]' in out
        _sql(db, "ALTER SEQUENCE meta_catalog_authorizations_id_seq OWNED BY meta_catalog_authorizations.id")
        _assert_pass(gate_root, "S0", db, "sequence ownership restored")
        _sql(db, "DROP TABLE meta_catalog_authorizations")
        _assert_fail(gate_root, "S0", db, "retained table dropped", check="retained_schema", reason=k.R_RETAINED_TABLE_MISSING)
    finally:
        _drop(db)


def test_lock_denial_lock_timeout_and_read_only_database_are_refused_within_bounds(gate_root):
    db = _fresh_db("gate_lock")
    try:
        _build_s0(db)
        holder = create_engine(_loopback_url(db), poolclass=NullPool, future=True)
        try:
            with holder.connect() as conn:
                assert conn.execute(text("SELECT pg_try_advisory_lock(:a, :b)"), {"a": k.ADVISORY_LOCK_KEYS[0], "b": k.ADVISORY_LOCK_KEYS[1]}).scalar() is True
                started = time.monotonic()
                out = _assert_fail(gate_root, "S0", db, "lock held elsewhere", check="lock", reason=k.R_LOCK_DENIED)
                assert time.monotonic() - started < 60 and "CHECK version_rows" not in out and "CHECK unlock" not in out
                assert conn.execute(text("SELECT pg_advisory_unlock(:a, :b)"), {"a": k.ADVISORY_LOCK_KEYS[0], "b": k.ADVISORY_LOCK_KEYS[1]}).scalar() is True
            with holder.connect() as conn:
                conn.execute(text("LOCK TABLE alembic_version IN ACCESS EXCLUSIVE MODE"))
                started = time.monotonic()
                out = _assert_fail(gate_root, "S0", db, "alembic_version locked", check="database", reason=k.R_TIMEOUT)
                elapsed = time.monotonic() - started
                assert elapsed < k.LOCK_TIMEOUT_MS / 1000 + 30 and harness.checks(out)["unlock"] == (k.PASS, None)
                conn.rollback()
        finally:
            holder.dispose()
        _assert_pass(gate_root, "S0", db, "locks released")
        _admin(f'ALTER DATABASE "{db}" SET default_transaction_read_only = on')
        _assert_fail(gate_root, "S0", db, "database marked read-only", check="server", reason=k.R_SERVER_READ_ONLY)
        _admin(f'ALTER DATABASE "{db}" RESET default_transaction_read_only')
        _assert_pass(gate_root, "S0", db, "read-only reset")
    finally:
        _drop(db)


def test_wrong_identity_target_override_flags_and_stale_approval_never_reach_the_database(gate_root, tmp_path):
    db = _fresh_db("gate_source_side")
    try:
        _build_s0(db)
        port = make_url(ADMIN_URL).port or 5432
        out = _assert_fail(gate_root, "S0", db, "identity mismatch", check="identity", reason=k.R_IDENTITY_MISMATCH,
                           extra_env={"RAILWAY_SERVICE_ID": "99999999-9999-4999-8999-999999999999"})
        assert "database checks skipped" in out and "99999999" not in out.split("CHECK identity")[1].split("\n")[0]
        rc, out, checks = _run(gate_root, "S0", db, env_release_sha="d" * 40)
        assert checks["release_sha"] == (k.FAIL, k.R_RELEASE_SHA_MISMATCH) and "database checks skipped" in out
        rc, out = harness.run_gate(gate_root, "S0", _alias_url(db) + "?host=other.internal", _target(db))
        assert harness.checks(out)["database_target"] == (k.FAIL, k.R_DB_TARGET_MISMATCH) and "database checks skipped" in out
        rc, out = harness.run_gate(gate_root, "S0", _alias_url(db), f"{HOST_ALIAS}:{port}/{db}_other")
        assert harness.checks(out)["database_target"] == (k.FAIL, k.R_DB_TARGET_MISMATCH) and "database checks skipped" in out
        rc, out = harness.run_gate(gate_root, "S0", _alias_url(db), _target(db), extra_env={"PGHOST": "other.internal"})
        assert harness.checks(out)["database_target"] == (k.FAIL, k.R_DB_TARGET_MISMATCH)
        rc, out = harness.run_gate(gate_root, "S0", _loopback_url(db), _target(db))
        assert harness.checks(out)["database_target"] == (k.FAIL, k.R_DB_TARGET_MISMATCH)
        for flag in k.SHOPIFY_FLAG_ENVS:
            out = _assert_fail(gate_root, "S0", db, f"{flag} on", check="shopify_flags", reason=k.R_FLAG_ENABLED, extra_env={flag: "1"})
            assert "database checks skipped" in out
        stale = harness.build_root(tmp_path / "stale", approval=None)
        record = harness.filled_approval(gate.runtime_inventory(stale), now=harness.NOW - __import__("datetime").timedelta(days=30))
        harness.write_approval(stale, record)
        out = _assert_fail(stale, "S0", db, "expired approval", check="approval", reason=k.R_APPROVAL_EXPIRED)
        assert "database checks skipped" in out
        unbound = harness.build_root(tmp_path / "unbound", approval=harness.filled_approval((1, "0" * 64)))
        _assert_fail(unbound, "S0", db, "approval for another source", check="approval", reason=k.R_APPROVAL_NOT_BOUND)
        no_restore = harness.build_root(tmp_path / "norestore", restore_verified=False)
        _assert_pass(no_restore, "S0", db, "S0 needs backup evidence but not a verified restore")
        _assert_fail(no_restore, "S1", db, "S1 without a verified restore", check="approval", reason=k.R_RESTORE_NOT_VERIFIED)
        shipped = harness.build_root(tmp_path / "shipped", approval=json.loads((_REPO / k.APPROVAL_RELATIVE_PATH).read_text()))
        out = _assert_fail(shipped, "S0", db, "shipped record is unfilled", check="approval", reason=k.R_APPROVAL_NOT_BOUND)
        assert "database checks skipped" in out
    finally:
        _drop(db)


def test_expectations_file_equals_a_fresh_snapshot_on_this_server():
    db = _fresh_db("gate_snapshot")
    try:
        _build_s0(db)
        fresh = render.snapshot(_loopback_url(db), source_sha=EXPECTATIONS["generated_from_source_sha"], root=_REPO)
        for key in ("tables", "nonce", "pk_types", "unique_definitions", "sequences", "database_py_digest"):
            assert fresh[key] == EXPECTATIONS[key], key
        with pytest.raises(ValueError):
            render.snapshot(make_url(ADMIN_URL).set(host=k.EXISTING_PLATFORM_TARGET["db"].split(":")[0], database=db).render_as_string(hide_password=False),
                            source_sha=EXPECTATIONS["generated_from_source_sha"])
    finally:
        _drop(db)


def test_legacy_462_bootstrap_refuses_a_shopify_state_without_mutating_it():
    checkout = os.environ.get("NAHLA_LEGACY_462_CHECKOUT", "").strip()
    if not checkout:
        pytest.skip("NAHLA_LEGACY_462_CHECKOUT not set (no checkout of the deployed commit available)")
    legacy_db_dir = Path(checkout) / "database"
    assert not (Path(checkout) / k.GATE_RELATIVE_PATH).exists()
    db = _fresh_db("gate_legacy")
    try:
        _build_s0(db)
        url = _loopback_url(db)
        baseline = topo.schema_snapshot(url)
        proc = _alembic(db, "upgrade", "0093", database_dir=legacy_db_dir)
        assert proc.returncode == 0, proc.stderr[-2000:]
        topo.assert_rows(url, topo.S0, "S0 after legacy bootstrap")
        assert _alembic(db, "upgrade", "0121").returncode == 0
        topo.assert_rows(url, topo.S1, "S1")
        proc = _alembic(db, "upgrade", "0093", database_dir=legacy_db_dir)
        assert proc.returncode != 0 and "0121" in (proc.stderr + proc.stdout)
        topo.assert_rows(url, topo.S1, "S1 after refused legacy bootstrap")
        assert topo.schema_diff(topo.schema_snapshot(url), baseline) == []
        assert _alembic(db, "downgrade", "0121@-1").returncode == 0
        topo.assert_rows(url, topo.S0, "S0 after rollback")
        proc = _alembic(db, "upgrade", "0093", database_dir=legacy_db_dir)
        assert proc.returncode == 0
        topo.assert_rows(url, topo.S0, "S0 after legacy bootstrap post-rollback")
    finally:
        _drop(db)

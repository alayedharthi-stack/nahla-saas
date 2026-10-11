"""Shopify release gate on real PostgreSQL: S0/S3 phase matrix, intermediate-state refusal, footprint by definition, drift, locks, timeouts.

Every case builds the deployed topology {0111, 0113, 0120} by genuine upgrades
on a disposable server and runs the gate **through its rendered trusted
bootstrap** (``bash -c``) with an explicit synthetic environment and a
synthetic external approval payload. The gate's target validator refuses
loopback hosts, so the disposable server must be reachable through a
non-loopback-spelled name given as ``NAHLA_RELEASE_GATE_PG_HOST_ALIAS``;
without it (or without ``NAHLA_RELIABILITY_PG_ADMIN_DSN``) the module is
skipped and reported as skipped, never as passed. ``DATABASE_URL`` is never
inherited: each run is given the disposable database explicitly.
"""
from __future__ import annotations

import datetime as _dt
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
SECRET_NAME = "shopify_p_not_a_real_secret_name"


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
    return f"{HOST_ALIAS}:{make_url(ADMIN_URL).port or 5432}/{db}"


def _fresh_db(prefix: str) -> str:
    name = f"{prefix}_{os.getpid()}_{int(time.time() * 1000) % 1000000}"
    _admin(f'DROP DATABASE IF EXISTS "{name}"', f'CREATE DATABASE "{name}"')
    return name


def _drop(name: str) -> None:
    _admin(f'DROP DATABASE IF EXISTS "{name}"')


def _alembic(db: str, *args: str, database_dir: Path = _REPO / "database") -> subprocess.CompletedProcess:
    env = {**os.environ, "DATABASE_URL": _loopback_url(db)}
    for variable in k.LIBPQ_TARGET_ENV_VARS:
        env.pop(variable, None)
    return subprocess.run([sys.executable, "-m", "alembic", *args], cwd=str(database_dir), env=env, capture_output=True, text=True, timeout=600)


def _build(db: str, *targets: str) -> None:
    for target in ("0093", "0111", "0113", "0120") + targets:
        proc = _alembic(db, "upgrade", target)
        assert proc.returncode == 0, proc.stderr[-3000:]


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


ALL_CHECKS = {"root", "release_sha", "inventory", "libraries", "migration_preflight", "graph", "identity", "database_target", "approval",
              "shopify_flags", "expectations", "connect", "lock", "server", "version_rows", "shopify_footprint", "retained_schema",
              "relation_identity", "unlock"}


def _assert_pass(root: Path, phase: str, db: str, step: str, **kwargs) -> str:
    rc, out, checks = _run(root, phase, db, **kwargs)
    assert rc == 0 and harness.result(out) == k.PASS and harness.bootstrap(out) == "PASS", f"{step}: {out[-2500:]}"
    assert set(checks) == ALL_CHECKS and all(status == k.PASS for status, _ in checks.values()), (step, checks)
    assert "nahla_password" not in out and "postgresql://" not in out, step
    return out


def _assert_fail(root: Path, phase: str, db: str, step: str, *, check: str, reason: str, **kwargs) -> str:
    rc, out, checks = _run(root, phase, db, **kwargs)
    assert rc == 1 and harness.result(out) == k.FAIL, f"{step}: {out[-2500:]}"
    assert checks.get(check) == (k.FAIL, reason), (step, checks)
    assert "nahla_password" not in out and "postgresql://" not in out, step
    return out


def _assert_bootstrap_refusal(root: Path, phase: str, db: str, step: str, reason: str, **kwargs) -> str:
    rc, out, checks = _run(root, phase, db, **kwargs)
    assert rc == 2 and harness.bootstrap(out) == reason and not checks, f"{step}: {out[-1200:]}"
    assert "nahla_password" not in out and "postgresql://" not in out, step
    return out


@pytest.fixture(scope="module")
def gate_root(tmp_path_factory) -> Path:
    return harness.build_root(tmp_path_factory.mktemp("gate") / "root")


# ── Phase matrix: only S0 and S3 deploy; S1 and S2 are refused under both ─────

def test_phase_matrix_deploys_only_at_s0_and_s3_and_bootstrap_target_is_a_noop(gate_root):
    db = _fresh_db("gate_matrix")
    try:
        _build(db)
        url = _loopback_url(db)
        baseline = topo.schema_snapshot(url)
        planted = topo.plant_sentinels(url)
        counts = topo.row_counts(url, topo.SENTINEL_TABLES)
        _assert_pass(gate_root, "S0", db, "S0 built")
        _assert_fail(gate_root, "S3", db, "S3 command against S0", check="version_rows", reason=k.R_VERSION_ROWS_MISMATCH)

        assert _alembic(db, "upgrade", "0121").returncode == 0
        topo.assert_step(url, topo.S1, baseline, planted, counts, "S1")
        out = _assert_fail(gate_root, "S0", db, "S0 command against S1", check="version_rows", reason=k.R_VERSION_ROWS_MISMATCH)
        assert harness.checks(out)["shopify_footprint"] == (k.FAIL, k.R_FOOTPRINT_UNEXPECTED)
        out = _assert_fail(gate_root, "S3", db, "S3 command against S1", check="version_rows", reason=k.R_VERSION_ROWS_MISMATCH)
        assert harness.checks(out)["shopify_footprint"] == (k.FAIL, k.R_FOOTPRINT_MISSING)
        proc = subprocess.run(build_normal_bootstrap_upgrade_argv(python_executable=sys.executable), cwd=str(_REPO / "database"),
                              env={**os.environ, "DATABASE_URL": url}, capture_output=True, text=True, timeout=600)
        assert proc.returncode == 0, proc.stderr[-2000:]
        topo.assert_step(url, topo.S1, baseline, planted, counts, "S1 after bootstrap upgrade 0093")

        assert _alembic(db, "upgrade", "0122").returncode == 0
        topo.assert_step(url, topo.S2, baseline, planted, counts, "S2")
        for phase in ("S0", "S3"):
            _assert_fail(gate_root, phase, db, f"{phase} command against S2", check="version_rows", reason=k.R_VERSION_ROWS_MISMATCH)

        assert _alembic(db, "upgrade", "0123").returncode == 0
        topo.assert_step(url, topo.S3, baseline, planted, counts, "S3")
        _assert_pass(gate_root, "S3", db, "S3 after upgrade 0123")
        _assert_fail(gate_root, "S0", db, "S0 command against S3 (the 462 state is never whitelisted)", check="version_rows", reason=k.R_VERSION_ROWS_MISMATCH)
        proc = subprocess.run(build_normal_bootstrap_upgrade_argv(python_executable=sys.executable), cwd=str(_REPO / "database"),
                              env={**os.environ, "DATABASE_URL": url}, capture_output=True, text=True, timeout=600)
        assert proc.returncode == 0
        topo.assert_step(url, topo.S3, baseline, planted, counts, "S3 after bootstrap upgrade 0093")

        for target, expected in (("0123@-1", topo.S2), ("0122@-1", topo.S1)):
            assert _alembic(db, "downgrade", target).returncode == 0
            topo.assert_step(url, expected, baseline, planted, counts, f"after downgrade {target}")
            for phase in ("S0", "S3"):
                _assert_fail(gate_root, phase, db, f"{phase} command after downgrade {target}", check="version_rows", reason=k.R_VERSION_ROWS_MISMATCH)
        assert _alembic(db, "downgrade", "0121@-1").returncode == 0
        topo.assert_step(url, topo.S0, baseline, planted, counts, "after downgrade 0121@-1")
        _assert_pass(gate_root, "S0", db, "S0 after full rollback")
        assert topo.schema_diff(topo.schema_snapshot(url), baseline) == []
    finally:
        _drop(db)


def test_s3_footprint_is_checked_by_definition_across_every_user_schema(gate_root):
    db = _fresh_db("gate_footprint")
    try:
        _build(db, "0121", "0122", "0123")
        _assert_pass(gate_root, "S3", db, "S3")
        _sql(db, "CREATE SCHEMA other", f'CREATE TABLE other."{SECRET_NAME}" (id int)')
        out = _assert_fail(gate_root, "S3", db, "stray table in another schema", check="shopify_footprint", reason=k.R_FOOTPRINT_UNEXPECTED)
        assert '"unexpected_count":1' in out and SECRET_NAME not in out and "other" not in out.split("CHECK shopify_footprint")[1].split("\n")[0]
        _sql(db, f'DROP TABLE other."{SECRET_NAME}"', f'CREATE TYPE other."{SECRET_NAME}" AS ENUM (\'a\')')
        out = _assert_fail(gate_root, "S3", db, "stray type in another schema", check="shopify_footprint", reason=k.R_FOOTPRINT_UNEXPECTED)
        footprint_line = out.split("CHECK shopify_footprint")[1].split("\n")[0]
        # the enum and its implicit array type are both Shopify-named strays; counted, never named
        assert json.loads(footprint_line[footprint_line.index("{"):])["stray_count"] >= 1 and SECRET_NAME not in out
        _sql(db, f'DROP TYPE other."{SECRET_NAME}"', f'CREATE FUNCTION other."{SECRET_NAME}"() RETURNS int LANGUAGE sql AS \'SELECT 1\'')
        _assert_fail(gate_root, "S3", db, "stray function in another schema", check="shopify_footprint", reason=k.R_FOOTPRINT_UNEXPECTED)
        _sql(db, f'DROP FUNCTION other."{SECRET_NAME}"()', "DROP SCHEMA other")
        _sql(db, "CREATE TABLE public.shopify_leftover (id int)")
        _assert_fail(gate_root, "S3", db, "stray public table", check="shopify_footprint", reason=k.R_FOOTPRINT_UNEXPECTED)
        _sql(db, "DROP TABLE public.shopify_leftover")
        _assert_pass(gate_root, "S3", db, "S3 restored")
        _sql(db, "DROP TABLE shopify_privacy_request_events", "CREATE TABLE shopify_privacy_request_events (id bigint primary key)")
        out = _assert_fail(gate_root, "S3", db, "same-name wrong-shaped table", check="shopify_footprint", reason=k.R_SHOPIFY_DEFINITION_DRIFT)
        assert '"drift":["shopify_privacy_request_events"]' in out
        _sql(db, "DROP TABLE shopify_privacy_request_events")
        out = _assert_fail(gate_root, "S3", db, "partial footprint", check="shopify_footprint", reason=k.R_FOOTPRINT_MISSING)
        assert '"missing":["shopify_privacy_request_events"]' in out
        assert _alembic(db, "downgrade", "0123@-1").returncode == 0 and _alembic(db, "upgrade", "0123").returncode == 0
        _assert_pass(gate_root, "S3", db, "S3 rebuilt")
        _sql(db, f"ALTER TABLE shopify_connections DROP CONSTRAINT {k.CONNECTION_KEY_CONSTRAINT}",
             f"ALTER TABLE shopify_connections ADD CONSTRAINT {k.CONNECTION_KEY_CONSTRAINT} UNIQUE (id, generation)")
        out = _assert_fail(gate_root, "S3", db, "same-name key on the wrong columns", check="shopify_footprint", reason=k.R_SHOPIFY_DEFINITION_DRIFT)
        assert '"drift":["shopify_connections"]' in out and "generation" not in out.split("CHECK shopify_footprint")[1].split("\n")[0]
        _sql(db, f"ALTER TABLE shopify_connections DROP CONSTRAINT {k.CONNECTION_KEY_CONSTRAINT}",
             f"ALTER TABLE shopify_connections ADD CONSTRAINT {k.CONNECTION_KEY_CONSTRAINT} UNIQUE (id, tenant_id)")
        _assert_pass(gate_root, "S3", db, "key restored")
        index = next(i[0] for i in EXPECTATIONS["shopify_s3"]["tables"]["shopify_oauth_states"]["indexes"])
        _sql(db, f"UPDATE pg_index SET indisvalid = false WHERE indexrelid = '{index}'::regclass")
        _assert_fail(gate_root, "S3", db, "invalid index flag", check="shopify_footprint", reason=k.R_SHOPIFY_DEFINITION_DRIFT)
        _sql(db, f"UPDATE pg_index SET indisvalid = true WHERE indexrelid = '{index}'::regclass")
        _assert_pass(gate_root, "S3", db, "index flag restored")
    finally:
        _drop(db)


def test_s0_refuses_any_shopify_object(gate_root):
    db = _fresh_db("gate_s0_strays")
    try:
        _build(db)
        _sql(db, "CREATE TABLE shopify_connections (id bigint primary key, tenant_id int)")
        out = _assert_fail(gate_root, "S0", db, "planted empty shopify_connections", check="shopify_footprint", reason=k.R_FOOTPRINT_UNEXPECTED)
        assert '"unexpected_count":1' in out
        _sql(db, "DROP TABLE shopify_connections", f'CREATE FUNCTION public."{SECRET_NAME}"() RETURNS int LANGUAGE sql AS \'SELECT 1\'')
        out = _assert_fail(gate_root, "S0", db, "planted function", check="shopify_footprint", reason=k.R_FOOTPRINT_UNEXPECTED)
        assert SECRET_NAME not in out
        _sql(db, f'DROP FUNCTION public."{SECRET_NAME}"()')
        _assert_pass(gate_root, "S0", db, "S0 clean")
    finally:
        _drop(db)


def test_version_rows_malformed_states_are_refused_and_never_echoed(gate_root):
    db = _fresh_db("gate_rows")
    try:
        _build(db)
        _sql(db, "ALTER TABLE alembic_version DROP CONSTRAINT alembic_version_pkc", "INSERT INTO alembic_version (version_num) VALUES ('0120')")
        _assert_fail(gate_root, "S0", db, "duplicate row", check="version_rows", reason=k.R_VERSION_ROWS_DUPLICATE)
        _sql(db, "DELETE FROM alembic_version WHERE ctid = (SELECT max(ctid) FROM alembic_version WHERE version_num = '0120')",
             "ALTER TABLE alembic_version ADD CONSTRAINT alembic_version_pkc PRIMARY KEY (version_num)")
        secret_row = "p-not-a-real-secret-row-value"
        _sql(db, f"INSERT INTO alembic_version (version_num) VALUES ('{secret_row}')")
        out = _assert_fail(gate_root, "S0", db, "unknown row", check="version_rows", reason=k.R_VERSION_ROWS_UNKNOWN)
        assert secret_row not in out and k.UNKNOWN in out
        _sql(db, f"DELETE FROM alembic_version WHERE version_num = '{secret_row}'", "DELETE FROM alembic_version WHERE version_num = '0113'")
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
    retained = EXPECTATIONS["retained"]
    try:
        _build(db)
        _assert_pass(gate_root, "S0", db, "S0")
        table = "commerce_runtime_handover_barrier"
        name, _kind, definition = next(c[:3] for c in retained["tables"][table]["constraints"] if c[1] == "c")
        _sql(db, f"ALTER TABLE {table} DROP CONSTRAINT {name}", f"ALTER TABLE {table} ADD CONSTRAINT {name} CHECK (generation >= 0 AND generation <= 2147483647)")
        out = _assert_fail(gate_root, "S0", db, "same-name CHECK with a different predicate", check="retained_schema", reason=k.R_RETAINED_DEFINITION_DRIFT)
        assert f'"drift":["{table}"]' in out and "2147483647" not in out
        _sql(db, f"ALTER TABLE {table} DROP CONSTRAINT {name}", f"ALTER TABLE {table} ADD CONSTRAINT {name} {definition}")
        _assert_pass(gate_root, "S0", db, "CHECK restored")
        index_table = "catalog_channel_retirements"
        index_name, indexdef = next(i[:2] for i in retained["tables"][index_table]["indexes"] if "UNIQUE" not in i[1])
        _sql(db, f"DROP INDEX {index_name}", indexdef.replace("(tenant_id, status)", "(status)"))
        _assert_fail(gate_root, "S0", db, "same-name index with different columns", check="retained_schema", reason=k.R_RETAINED_DEFINITION_DRIFT)
        _sql(db, f"DROP INDEX {index_name}", indexdef)
        _assert_pass(gate_root, "S0", db, "index restored")
        _sql(db, f"UPDATE pg_index SET indisvalid = false WHERE indexrelid = '{index_name}'::regclass")
        _assert_fail(gate_root, "S0", db, "retained index marked invalid", check="retained_schema", reason=k.R_RETAINED_DEFINITION_DRIFT)
        _sql(db, f"UPDATE pg_index SET indisvalid = true WHERE indexrelid = '{index_name}'::regclass")
        _sql(db, "ALTER TABLE meta_catalog_authorizations ALTER COLUMN granted_scopes TYPE jsonb USING granted_scopes::jsonb")
        _assert_fail(gate_root, "S0", db, "JSON column changed to JSONB", check="retained_schema", reason=k.R_RETAINED_DEFINITION_DRIFT)
        _sql(db, "ALTER TABLE meta_catalog_authorizations ALTER COLUMN granted_scopes TYPE json USING granted_scopes::json")
        _sql(db, "ALTER TABLE catalog_channel_retirements ALTER COLUMN catalog_id SET NOT NULL")
        _assert_fail(gate_root, "S0", db, "nullable catalog_id tightened", check="retained_schema", reason=k.R_RETAINED_DEFINITION_DRIFT)
        _sql(db, "ALTER TABLE catalog_channel_retirements ALTER COLUMN catalog_id DROP NOT NULL")
        _assert_pass(gate_root, "S0", db, "columns restored")
        _sql(db, "ALTER TABLE whatsapp_oauth_nonces DROP CONSTRAINT whatsapp_oauth_nonces_tenant_id_fkey",
             "ALTER TABLE whatsapp_oauth_nonces ADD CONSTRAINT whatsapp_oauth_nonces_tenant_id_fkey FOREIGN KEY (tenant_id) REFERENCES tenants(id)")
        out = _assert_fail(gate_root, "S0", db, "nonce FK without ON DELETE CASCADE", check="retained_schema", reason=k.R_RETAINED_DEFINITION_DRIFT)
        assert f'"drift":["{k.NONCE_TABLE}"]' in out
        _sql(db, "ALTER TABLE whatsapp_oauth_nonces DROP CONSTRAINT whatsapp_oauth_nonces_tenant_id_fkey",
             "ALTER TABLE whatsapp_oauth_nonces ADD CONSTRAINT whatsapp_oauth_nonces_tenant_id_fkey FOREIGN KEY (tenant_id) REFERENCES tenants(id) ON DELETE CASCADE")
        _sql(db, "ALTER TABLE whatsapp_oauth_nonces ALTER COLUMN created_at DROP DEFAULT")
        _assert_fail(gate_root, "S0", db, "nonce default dropped", check="retained_schema", reason=k.R_RETAINED_DEFINITION_DRIFT)
        _sql(db, "ALTER TABLE whatsapp_oauth_nonces ALTER COLUMN created_at SET DEFAULT CURRENT_TIMESTAMP")
        _sql(db, "ALTER INDEX ix_whatsapp_oauth_nonces_expires_at RENAME TO ix_nonce_expires_renamed",
             "ALTER TABLE whatsapp_oauth_nonces RENAME CONSTRAINT uq_whatsapp_oauth_nonces_hash TO uq_nonce_renamed")
        _assert_pass(gate_root, "S0", db, "nonce names abstracted, definition intact")
        _sql(db, "ALTER INDEX ix_nonce_expires_renamed RENAME TO ix_whatsapp_oauth_nonces_expires_at",
             "ALTER TABLE whatsapp_oauth_nonces RENAME CONSTRAINT uq_nonce_renamed TO uq_whatsapp_oauth_nonces_hash")
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


def test_legacy_lock_key_contention_timeouts_and_read_only_database(gate_root):
    db = _fresh_db("gate_lock")
    try:
        _build(db)
        holder = create_engine(_loopback_url(db), poolclass=NullPool, future=True)
        try:
            with holder.connect() as conn:
                assert conn.execute(text("SELECT pg_try_advisory_lock(:key)"), {"key": k.ADVISORY_LOCK_KEY}).scalar() is True
                started = time.monotonic()
                out = _assert_fail(gate_root, "S0", db, "legacy key held elsewhere", check="lock", reason=k.R_LOCK_DENIED)
                assert time.monotonic() - started < 60 and "CHECK version_rows" not in out and "CHECK unlock" not in out
                assert conn.execute(text("SELECT pg_advisory_unlock(:key)"), {"key": k.ADVISORY_LOCK_KEY}).scalar() is True
            with holder.connect() as conn:
                conn.execute(text("LOCK TABLE alembic_version IN ACCESS EXCLUSIVE MODE"))
                started = time.monotonic()
                out = _assert_fail(gate_root, "S0", db, "alembic_version locked", check="database", reason=k.R_TIMEOUT)
                assert time.monotonic() - started < k.LOCK_TIMEOUT_MS / 1000 + 30 and harness.checks(out)["unlock"] == (k.PASS, None)
                conn.rollback()
                # released on failure: the legacy key is immediately available to another session
                assert conn.execute(text("SELECT pg_try_advisory_lock(:key)"), {"key": k.ADVISORY_LOCK_KEY}).scalar() is True
                assert conn.execute(text("SELECT pg_advisory_unlock(:key)"), {"key": k.ADVISORY_LOCK_KEY}).scalar() is True
        finally:
            holder.dispose()
        out = _assert_pass(gate_root, "S0", db, "locks released")
        assert '"search_path":"public,pg_catalog"' in out
        _admin(f'ALTER DATABASE "{db}" SET default_transaction_read_only = on')
        _assert_fail(gate_root, "S0", db, "database marked read-only", check="server", reason=k.R_SERVER_READ_ONLY)
        _admin(f'ALTER DATABASE "{db}" RESET default_transaction_read_only')
        _assert_pass(gate_root, "S0", db, "read-only reset")
    finally:
        _drop(db)


def test_source_side_refusals_never_reach_the_database(gate_root, tmp_path):
    db = _fresh_db("gate_source_side")
    try:
        _build(db)
        port = make_url(ADMIN_URL).port or 5432
        _assert_bootstrap_refusal(gate_root, "S0", db, "identity mismatch", k.R_IDENTITY_MISMATCH,
                                  extra_env={"RAILWAY_SERVICE_ID": "99999999-9999-4999-8999-999999999999"})
        _assert_bootstrap_refusal(gate_root, "S0", db, "wrong release", k.R_RELEASE_SHA_MISMATCH, env_release_sha="d" * 40)
        _assert_bootstrap_refusal(gate_root, "S0", db, "inherited PGHOST", k.R_LIBPQ_OVERRIDE, extra_env={"PGHOST": "other.internal"})
        rc, out = harness.run_gate(gate_root, "S0", _alias_url(db) + "?host=other.internal", _target(db))
        assert harness.checks(out)["database_target"] == (k.FAIL, k.R_DB_TARGET_MISMATCH) and "database checks skipped" in out
        rc, out = harness.run_gate(gate_root, "S0", _alias_url(db), f"{HOST_ALIAS}:{port}/{db}_other")
        assert harness.checks(out)["database_target"] == (k.FAIL, k.R_DB_TARGET_MISMATCH) and "database checks skipped" in out
        rc, out = harness.run_gate(gate_root, "S0", _loopback_url(db), _target(db))
        assert harness.checks(out)["database_target"] == (k.FAIL, k.R_DB_TARGET_MISMATCH)
        for flag in k.SHOPIFY_FLAG_ENVS:
            out = _assert_fail(gate_root, "S0", db, f"{flag} on", check="shopify_flags", reason=k.R_FLAG_ENABLED, extra_env={flag: "1"})
            assert "database checks skipped" in out and "CHECK connect" not in out
        approval = harness.approval_for(gate_root, "S0", _target(db))
        other = harness.approval_for(gate_root, "S3", _target(db))
        rc, out = harness.run_command(harness.rendered(gate_root, "S0", _target(db), approval),
                                      harness.gate_environment(_alias_url(db), approval=other))
        assert rc == 2 and harness.bootstrap(out) == k.R_APPROVAL_PAYLOAD_PIN_MISMATCH
        rc, out = harness.run_command(harness.rendered(gate_root, "S0", _target(db), approval),
                                      {key: value for key, value in harness.gate_environment(_alias_url(db), approval=approval).items() if key != k.APPROVAL_PAYLOAD_ENV})
        assert rc == 2 and harness.bootstrap(out) == k.R_APPROVAL_PAYLOAD_MISSING
        stale = harness.approval_file(tmp_path / "stale", harness.filled_approval(
            release_sha=harness.SYNTHETIC_RELEASE_SHA, inventory=gate.inventory(gate_root), target=harness.target_for(_target(db)), phase="S0",
            now=_dt.datetime.now(_dt.timezone.utc) - _dt.timedelta(days=30)))
        rc, out = harness.run_command(harness.rendered(gate_root, "S0", _target(db), stale), harness.gate_environment(_alias_url(db), approval=stale))
        assert harness.checks(out)["approval"] == (k.FAIL, k.R_APPROVAL_EXPIRED) and "database checks skipped" in out
        wrong_backup = harness.approval_file(tmp_path / "wrong_backup", harness.filled_approval(
            release_sha=harness.SYNTHETIC_RELEASE_SHA, inventory=gate.inventory(gate_root), target=harness.target_for(_target(db)), phase="S0",
            backup={"service": "77777777-7777-4777-8777-777777777777", "volume": harness.SYNTHETIC_BACKUP["volume"]}))
        rc, out = harness.run_command(harness.rendered(gate_root, "S0", _target(db), wrong_backup), harness.gate_environment(_alias_url(db), approval=wrong_backup))
        assert harness.checks(out)["approval"] == (k.FAIL, k.R_BACKUP_IDENTITY_MISMATCH)
    finally:
        _drop(db)


def test_s3_deploy_needs_a_verified_restore_and_s0_does_not(gate_root):
    db = _fresh_db("gate_restore")
    try:
        _build(db)
        _assert_pass(gate_root, "S0", db, "S0 without a verified restore", restore_verified=False)
        _build(db, "0121", "0122", "0123")
        _assert_fail(gate_root, "S3", db, "S3 without a verified restore", check="approval", reason=k.R_RESTORE_NOT_VERIFIED, restore_verified=False)
        _assert_pass(gate_root, "S3", db, "S3 with a verified restore")
    finally:
        _drop(db)


def test_expectations_file_equals_fresh_snapshots_on_this_server():
    db = _fresh_db("gate_snapshot")
    try:
        _build(db)
        engine = create_engine(_loopback_url(db), poolclass=NullPool, future=True)
        try:
            with engine.connect() as conn:
                assert k.normalize(gate.observe_retained(conn)) == EXPECTATIONS["retained"]
                assert gate.observe_footprint(conn, k.DEPLOY_PHASES["S0"]) == {"tables": [], "stray_count": 0, "definitions": {}}
        finally:
            engine.dispose()
        with pytest.raises(ValueError, match="database_is_not_a_complete_s3_build"):
            render.snapshot(_loopback_url(db), source_sha=EXPECTATIONS["generated_from_source_sha"], root=_REPO)
        _build(db, "0121", "0122", "0123")
        fresh = render.snapshot(_loopback_url(db), source_sha=EXPECTATIONS["generated_from_source_sha"], root=_REPO)
        for key in ("retained", "shopify_s3", "database_py_digest", "expectations_version"):
            assert fresh[key] == EXPECTATIONS[key], key
        with pytest.raises(ValueError, match="refusing_existing_platform_database"):
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
        _build(db)
        url = _loopback_url(db)
        baseline = topo.schema_snapshot(url)
        assert _alembic(db, "upgrade", "0093", database_dir=legacy_db_dir).returncode == 0
        topo.assert_rows(url, topo.S0, "S0 after legacy bootstrap")
        for target, rows in (("0121", topo.S1), ("0122", topo.S2), ("0123", topo.S3)):
            assert _alembic(db, "upgrade", target).returncode == 0
            topo.assert_rows(url, rows, target)
            proc = _alembic(db, "upgrade", "0093", database_dir=legacy_db_dir)
            assert proc.returncode != 0 and target in (proc.stderr + proc.stdout)
            topo.assert_rows(url, rows, f"{target} after refused legacy bootstrap")
        assert topo.schema_diff(topo.schema_snapshot(url), baseline) == []
        for target in ("0123@-1", "0122@-1", "0121@-1"):
            assert _alembic(db, "downgrade", target).returncode == 0
        topo.assert_rows(url, topo.S0, "S0 after DB-first rollback")
        assert _alembic(db, "upgrade", "0093", database_dir=legacy_db_dir).returncode == 0
        topo.assert_rows(url, topo.S0, "S0 after legacy bootstrap post-rollback")
    finally:
        _drop(db)

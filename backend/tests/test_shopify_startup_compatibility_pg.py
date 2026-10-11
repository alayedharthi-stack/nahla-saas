"""Targeted startup proof on the same disposable PostgreSQL CI services.

Real source callback/cleanup/create_all/64 safe-alter statements and concurrent
DB executors; provider coroutines are withheld. Not an HTTP/router import proof.
"""
from __future__ import annotations

import base64
import json
import os
from pathlib import Path
import subprocess
import sys

import pytest
from sqlalchemy import create_engine, text
from sqlalchemy.engine.url import make_url
from sqlalchemy.pool import NullPool

from backend.tests import _alembic_topology_probe as topo
from backend.tests import _shopify_release_gate_harness as gate_harness
from backend.tests import test_shopify_release_gate_pg as pg
from scripts.operators import shopify_release_gate_contract as contract

REPO = Path(__file__).resolve().parents[2]
CANDIDATE = Path(os.environ.get("NAHLA_STARTUP_SOURCE_ROOT", str(REPO)))
LEGACY = Path(os.environ.get("NAHLA_LEGACY_462_CHECKOUT", "/missing-legacy"))
PROBE = Path(__file__).with_name("_shopify_startup_probe.py")


def _environment(url: str, root: Path, hook_dir: Path) -> dict:
    hook_dir.mkdir(parents=True, exist_ok=True)
    (hook_dir / "sitecustomize.py").write_text(
        "from _shopify_startup_probe import install_network_guard\ninstall_network_guard()\n", encoding="utf-8")
    env = {name: os.environ[name] for name in ("PATH", "HOME", "LANG", "SystemRoot", "TEMP", "TMP")
           if name in os.environ}
    env.update({"DATABASE_URL": url, "ENVIRONMENT": "production", "NAHLA_DISABLE_SCHEDULERS": "1",
                "NAHLA_STARTUP_ROOT": str(root), "NAHLA_DB_STATEMENT_TIMEOUT_MS": "15000",
                "NAHLA_BOOTSTRAP_OVERALL_TIMEOUT": "180", "NAHLA_SAFE_ALTERS_TIMEOUT": "180",
                "PYTHONDONTWRITEBYTECODE": "1", "PYTHONIOENCODING": "utf-8",
                "PYTHONPATH": os.pathsep.join((str(hook_dir), str(PROBE.parent)))})
    env.update({flag: "0" for flag in contract.SHOPIFY_FLAG_ENVS})
    return env


def _startup(root: Path, url: str, work: Path, *, dispatch_only=False, provider_canary=False) -> dict:
    argv = [sys.executable, "-B", str(PROBE), "--root", str(root)]
    if dispatch_only:
        argv.append("--dispatch-only")
    if provider_canary:
        argv.append("--provider-canary")
    result = subprocess.run(argv, env=_environment(url, root, work), capture_output=True,
                            text=True, encoding="utf-8", timeout=210)
    assert result.returncode == 0, result.stderr[-2500:]
    reports = [line.split("=", 1)[1] for line in result.stdout.splitlines() if line.startswith("STARTUP_REPORT=")]
    assert len(reports) == 1, "startup report missing"
    report = json.loads(reports[0])
    print("STARTUP_PROOF", json.dumps(report, sort_keys=True))
    assert report["network_canary_refused"] and report["provider_network_attempts"] == 0
    assert report["provider_client_calls"] == [] and report["scheduler_registrations"] == []
    assert report["lifespan_complete"]
    return report


def _sql(url: str, statement: str, values=None):
    engine = create_engine(url, poolclass=NullPool)
    try:
        with engine.begin() as conn:
            result = conn.execute(text(statement), values or {})
            return result.fetchall() if result.returns_rows else None
    finally:
        engine.dispose()


def _identities(url: str):
    return _sql(url, "SELECT n.nspname,c.relname,c.relkind,c.oid FROM pg_class c "
                "JOIN pg_namespace n ON n.oid=c.relnamespace "
                "WHERE n.nspname='public' AND c.relkind IN ('r','p','S','i') ORDER BY 1,2,3")


def _extra_rows(url: str, phase: str, existing_tenant: int) -> dict:
    tenant = _sql(url, "INSERT INTO tenants(name) VALUES ('synthetic independent Shopify merchant') RETURNING id")[0][0]
    for provider in ("salla", "shopify"):
        _sql(url, "INSERT INTO integrations(tenant_id,provider,enabled,external_store_id,config) "
                  "VALUES (:tid,:provider,true,:store,CAST(:cfg AS json))",
             {"tid": existing_tenant if provider == "salla" else tenant,
              "provider": provider, "store": "synthetic-shared-external-id",
              "cfg": json.dumps({"store_id": "synthetic-shared-external-id",
                                 "api_key": "synthetic-not-a-real-token",
                                 "refresh_token": "synthetic-not-a-real-refresh"})})
    if phase == "S3":
        _sql(url, "INSERT INTO shopify_connections(tenant_id,shop_domain,shop_gid,status,generation,"
                  "credential_version,access_token_enc,refresh_token_enc,access_token_expires_at,"
                  "refresh_token_expires_at,granted_scopes) VALUES "
                  "(:tid,'synthetic-independent.myshopify.com','gid://shopify/Shop/501','active',7,4,"
                  "'synthetic-ciphertext-access','synthetic-ciphertext-refresh',"
                  "now()+interval '1 day',now()+interval '30 days','read_products')", {"tid": tenant})
        _sql(url, "INSERT INTO shopify_shop_leases(shop_domain,lease_id,purpose,tenant_id,acquired_at,expires_at) "
                  "VALUES ('synthetic-independent.myshopify.com','synthetic-lease','refresh',:tid,"
                  "now(),now()+interval '1 hour')", {"tid": tenant})
    existing = {row[0] for row in _sql(url, "SELECT tablename FROM pg_tables WHERE schemaname='public'")}
    tables = sorted({"integrations"} | {name for name in existing if name.startswith("shopify_")})
    return {table: _sql(url, f"SELECT encode(sha256(convert_to(CAST(t AS text),'UTF8')),'hex') "
                       f"FROM {table} t ORDER BY CAST(t AS text)") for table in tables}


@pytest.fixture(scope="module", params=("S0", "S3"))
def started_state(request, tmp_path_factory):
    if not pg.ADMIN_URL or not pg.HOST_ALIAS:
        if os.environ.get("NAHLA_RELIABILITY_REQUIRE_PG") == "1":
            pytest.fail("strict startup proof requires disposable admin DSN/alias")
        pytest.skip("no disposable PostgreSQL configured")
    admin = make_url(pg.ADMIN_URL)
    assert admin.host == "127.0.0.1" and admin.port == 5433
    assert admin.username == "nahla" and admin.password == "nahla_password" and admin.database == "postgres"
    assert LEGACY.is_dir() and CANDIDATE.is_dir()
    phase = request.param
    db = pg._fresh_db("startup")
    url = pg._loopback_url(db)
    work = tmp_path_factory.mktemp("startup_" + phase)
    try:
        for revision in ("0093", "0111", "0113", "0120"):
            result = pg._alembic(db, "upgrade", revision, database_dir=CANDIDATE / "database")
            assert result.returncode == 0, result.stderr[-2000:]
        deployed = _startup(LEGACY, url, work / "legacy_hook")
        if phase == "S3":
            for revision in ("0121", "0122", "0123"):
                result = pg._alembic(db, "upgrade", revision, database_dir=CANDIDATE / "database")
                assert result.returncode == 0, result.stderr[-2000:]
        rows = topo.plant_sentinels(url)
        extra = _extra_rows(url, phase, rows["tenants"][0])
        baseline = topo.schema_snapshot(url, exclude_prefixes=(), exclude=())
        identities = _identities(url)
        before_counts = topo.row_counts(url, topo.SENTINEL_TABLES)
        candidate = [_startup(CANDIDATE, url, work / ("candidate_hook_" + str(i))) for i in range(2)]
        yield {"phase": phase, "db": db, "url": url, "work": work, "legacy": deployed,
               "candidate": candidate, "schema": baseline, "identities": identities,
               "sentinels": rows, "counts": before_counts, "extra": extra}
    finally:
        pg._drop(db)


def test_candidate_startup_preserves_schema_rows_and_is_idempotent(started_state):
    state = started_state
    for report in (state["legacy"], *state["candidate"]):
        assert report["db_tasks"] == ["_bootstrap_db_schema_bg", "_migrate_background"]
        assert report["safe_alters_ready"] and report["bootstrap_ready"]
        assert report["sql_or_bootstrap_errors"] == []
        assert report["subprocesses"] == [{"operation": "cleanup", "returncode": 0},
                                          {"operation": "upgrade 0093", "returncode": 0}]
        assert report["withheld_tasks"] == ["_coexistence_client_id_repair_bg",
                                            "_reconcile_pending_moyasar_sweep", "run_runtime_heartbeat"]
    expected = topo.S0 if state["phase"] == "S0" else topo.S3
    topo.assert_rows(state["url"], expected, "after full startup")
    actual = topo.schema_snapshot(state["url"], exclude_prefixes=(), exclude=())
    assert actual == state["schema"], topo.schema_diff(actual, state["schema"])
    assert _identities(state["url"]) == state["identities"]
    topo.assert_sentinels(state["url"], state["sentinels"], "after full startup")
    assert topo.row_counts(state["url"], topo.SENTINEL_TABLES) == state["counts"]
    for table, hashes in state["extra"].items():
        assert _sql(state["url"], f"SELECT encode(sha256(convert_to(CAST(t AS text),'UTF8')),'hex') "
                    f"FROM {table} t ORDER BY CAST(t AS text)") == hashes, table
    if state["phase"] == "S0":
        assert not any(name.startswith("shopify_") for name in actual)


def test_release_gate_accepts_the_schema_after_actual_startup(started_state):
    state = started_state
    root = gate_harness.build_root(state["work"] / "gate_root", source=CANDIDATE)
    target = pg._target(state["db"])
    rc, out = gate_harness.run_gate(root, state["phase"], pg._alias_url(state["db"]), target)
    print("STARTUP_GATE", state["phase"], rc, json.dumps(gate_harness.checks(out), sort_keys=True))
    assert rc == 0 and gate_harness.result(out) == contract.PASS, out[-3000:]


def test_disabled_scheduler_and_provider_boundaries_are_exercised(tmp_path):
    report = _startup(CANDIDATE, "postgresql://nahla:nahla_password@127.0.0.1:5433/startup_dispatch_only",
                      tmp_path / "hook", dispatch_only=True, provider_canary=True)
    assert report["db_tasks"] == [] and report["subprocesses"] == []
    assert "_subscribe_platform_phone" in report["withheld_tasks"]
    assert "_reconcile_pending_moyasar_sweep" in report["withheld_tasks"]
    assert "_migrate_background" in report["withheld_tasks"]


def test_actual_production_preflight_accepts_synthetic_config_and_refuses_missing_secret(tmp_path):
    env = _environment("postgresql://nahla:nahla_password@127.0.0.1:5433/startup_preflight",
                       CANDIDATE, tmp_path / "unused_hook")
    env.pop("PYTHONPATH")
    env.update({"JWT_SECRET": "synthetic-jwt-signing-key-with-no-live-use-000001",
                "ADMIN_PASSWORD": "synthetic-admin-password-no-live-use",
                "WHATSAPP_VERIFY_TOKEN": "synthetic-protocol-verification-no-live-use",
                "ADMIN_EMAIL": "synthetic-admin@example.invalid",
                "TOTP_ENC_KEY": base64.urlsafe_b64encode(bytes(range(32))).decode()})
    script = CANDIDATE / "scripts/preflight_check.py"
    accepted = subprocess.run([sys.executable, str(script)], env=env, capture_output=True, text=True, timeout=20)
    assert accepted.returncode == 0, accepted.stdout[-1000:]
    assert "all production checks passed" in accepted.stdout
    env.pop("JWT_SECRET")
    refused = subprocess.run([sys.executable, str(script)], env=env, capture_output=True, text=True, timeout=20)
    assert refused.returncode == 1 and "[FAIL] JWT_SECRET" in refused.stdout

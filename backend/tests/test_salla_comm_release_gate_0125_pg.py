"""Disposable PostgreSQL 18 proof of the 0125 schema gate.

The calling workflow must set NAHLA_RELIABILITY_REQUIRE_PG=1 and use only the
pre-existing synthetic loopback PostgreSQL service. No provider or production
connection is opened by this test.
"""
from __future__ import annotations

import json
from datetime import timedelta
import os
import subprocess
import sys

import pytest

from backend.tests import _shopify_release_gate_harness as harness
from backend.tests import test_shopify_release_gate_pg as h
from scripts.operators import shopify_release_gate as gate
from scripts.operators import shopify_release_gate_contract as k

pytestmark = h.pytestmark


def _isolated_healthz_smoke(db, tmp_path):
    hook = tmp_path / "health_hook"
    hook.mkdir()
    (hook / "sitecustomize.py").write_text(
        "from _shopify_startup_probe import install_network_guard\ninstall_network_guard()\n",
        encoding="utf-8",
    )
    env = {name: os.environ[name] for name in ("PATH", "HOME", "LANG", "SystemRoot", "TEMP", "TMP")
           if name in os.environ}
    env.update({
        "DATABASE_URL": h._loopback_url(db),
        "ENVIRONMENT": "production",
        "NAHLA_DISABLE_SCHEDULERS": "1",
        "NAHLA_STARTUP_ROOT": str(h._REPO),
        "NAHLA_DB_STATEMENT_TIMEOUT_MS": "15000",
        "NAHLA_BOOTSTRAP_OVERALL_TIMEOUT": "180",
        "NAHLA_SAFE_ALTERS_TIMEOUT": "180",
        "SALLA_COMM_WHATSAPP_ENABLED": "false",
        "PYTHONDONTWRITEBYTECODE": "1",
        "PYTHONPATH": os.pathsep.join((str(hook), str(h._REPO / "backend/tests"), str(h._REPO))),
    })
    env.update({flag: "false" for flag in k.SHOPIFY_FLAG_ENVS})
    script = (
        "from fastapi.testclient import TestClient\n"
        "from backend.main import app\n"
        "response = TestClient(app).get('/healthz')\n"
        "assert response.status_code == 200 and response.json().get('ok') is True\n"
        "print('HEALTHZ_ROUTE_PASS')\n"
    )
    result = subprocess.run(
        [sys.executable, "-B", "-c", script], cwd=h._REPO, env=env,
        capture_output=True, text=True, timeout=120, check=False,
    )
    assert result.returncode == 0 and "HEALTHZ_ROUTE_PASS" in result.stdout, (
        result.stderr or result.stdout
    )[-1200:]
    probe = h._REPO / "backend/tests/_shopify_startup_probe.py"
    started = subprocess.run(
        [sys.executable, "-B", str(probe), "--root", str(h._REPO)],
        cwd=h._REPO, env=env, capture_output=True, text=True, timeout=210, check=False,
    )
    assert started.returncode == 0, (started.stderr or started.stdout)[-1200:]
    reports = [line.split("=", 1)[1] for line in started.stdout.splitlines()
               if line.startswith("STARTUP_REPORT=")]
    assert len(reports) == 1
    report = json.loads(reports[0])
    assert report["lifespan_complete"] and report["bootstrap_ready"] and report["safe_alters_ready"]
    assert report["sql_or_bootstrap_errors"] == []
    assert report["network_canary_refused"] and report["provider_network_attempts"] == 0
    assert report["provider_client_calls"] == [] and report["scheduler_registrations"] == []
    assert report["subprocesses"] == [
        {"operation": "cleanup", "returncode": 0},
        {"operation": "upgrade 0093", "returncode": 0},
    ]


def _synthetic_approval(root, db):
    artifact_path = root / k.COMM_ROLLBACK_RELATIVE_PATH
    artifact = json.loads(artifact_path.read_text(encoding="utf-8"))
    flags = {name: "false" for name in k.SHOPIFY_FLAG_ENVS + (k.COMM_SEND_FLAG_ENV,)}
    prior = {
        "source_sha": "b" * 40,
        "runtime_inventory_digest": "c" * 64,
        "runtime_inventory_count": 12,
        "shopify_expectations_sha256": "d" * 64,
        "communication_expectations_sha256": gate.file_sha256(root / k.COMM_EXPECTATIONS_RELATIVE_PATH),
        "independent_review_ref": "synthetic-compatible-source-review",
    }
    artifact.update(status="READY", compatible_rollback=prior)
    artifact_path.write_text(json.dumps(artifact, sort_keys=True) + "\n", encoding="utf-8")
    record = harness.filled_approval(
        release_sha=harness.SYNTHETIC_RELEASE_SHA,
        inventory=gate.inventory(root),
        target=harness.target_for(h._target(db)),
        phase="S4_COMM_DORMANT",
    )
    snapshot = record["deployment_rollback_snapshot"]
    snapshot["source_sha"] = prior["source_sha"]
    bindings = gate.schema_rollback_bindings(root, "S4_COMM_DORMANT")
    assert bindings is not None
    verified = k.parse_utc(snapshot["verified_redeployable_at_utc"])
    record["schema_rollback"] = {
        "record_version": 2,
        "phase": "S4_COMM_DORMANT",
        "source_sha": prior["source_sha"],
        "railway_deployment_id": snapshot["railway_deployment_id"],
        "target": record["target"],
        "flags": flags,
        "database_heads": ["0111", "0113", "0125"],
        "table_names": sorted(k.INSTALL_TABLES + ("salla_communication_function_nonces",)),
        "runtime_inventory_digest": prior["runtime_inventory_digest"],
        "runtime_inventory_count": prior["runtime_inventory_count"],
        "schema_expectations_sha256": prior["shopify_expectations_sha256"],
        "communication_schema_sha256": bindings["communication_schema_sha256"],
        "artifact_sha256": bindings["artifact_sha256"],
        "independent_review_ref": prior["independent_review_ref"],
        "verified_at_utc": harness.iso(verified),
        "valid_until_utc": harness.iso(verified + timedelta(hours=24)),
    }
    return harness.approval_file(root.parent / "approval", record)


@pytest.mark.parametrize("drift", ["none", "nonce_column", "audit_type", "extra_constraint"])
def test_0125_communication_schema_is_exact_on_pg18(drift, tmp_path):
    if not h.ADMIN_URL or not h.HOST_ALIAS:
        pytest.fail("disposable PostgreSQL 18 service is required")
    db = h._fresh_db("startup_comm_0125_gate")
    try:
        h._build(db, "0125")
        assert 180000 <= int(h._sql(db, "SHOW server_version_num")[0][0]) < 190000
        graph, known = gate.check_graph(h._REPO, k.phase("S4_COMM_DORMANT"))
        assert graph.status == k.PASS and known
        expectation_check, expectations = gate.check_expectations(h._REPO, "S4_COMM_DORMANT")
        assert expectation_check.status == k.PASS and expectations is not None
        if drift == "nonce_column":
            h._sql(db, "ALTER TABLE salla_communication_function_nonces ADD COLUMN injected text")
        elif drift == "audit_type":
            h._sql(db, "ALTER TABLE webhook_events ALTER COLUMN communication_send_state TYPE text")
        elif drift == "extra_constraint":
            h._sql(db, "ALTER TABLE salla_communication_function_nonces "
                       "ADD CONSTRAINT salla_communication_extra CHECK (nonce <> '')")
        checks = gate.run_database_checks(
            h._alias_url(db), k.phase("S4_COMM_DORMANT"), known, expectations, db
        )
        by_name = {check.name: check for check in checks}
        assert by_name["version_rows"].status == k.PASS
        assert by_name["shopify_footprint"].status == k.PASS
        assert by_name["retained_schema"].status == k.PASS
        assert by_name["unlock"].status == k.PASS
        if drift == "none":
            failures = [(check.name, check.reason, check.detail) for check in checks if check.status != k.PASS]
            assert not failures, failures
            _isolated_healthz_smoke(db, tmp_path)
            root = harness.build_root(tmp_path / "gate")
            approval = _synthetic_approval(root, db)
            rc, output = harness.run_gate(
                root, "S4_COMM_DORMANT", h._alias_url(db), h._target(db), approval=approval
            )
            assert rc == 0 and "RESULT=PASS" in output, output[-2000:]
            rc, output = harness.run_gate(
                root, "S4_COMM_DORMANT", h._alias_url(db), h._target(db),
                approval=approval, extra_env={k.COMM_SEND_FLAG_ENV: "true"},
            )
            assert rc != 0 and k.R_COMM_FLAG_ENABLED in output
        else:
            assert by_name["communication_schema"].status == k.FAIL
            assert by_name["communication_schema"].reason == k.R_COMM_SCHEMA_MISMATCH
    finally:
        h._drop(db)

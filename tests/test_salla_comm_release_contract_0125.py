"""Closed, DB-free 0125 release/rollback contract tests.

The PostgreSQL 18 migration and live catalog checks are a separate required job.
"""
from __future__ import annotations

import copy
from datetime import datetime, timedelta, timezone
import hashlib
import json
from pathlib import Path

from scripts.operators import shopify_release_gate_contract as k
from scripts.operators import shopify_release_gate as gate
from backend.tests import _shopify_release_gate_harness as harness


ROOT = Path(__file__).resolve().parents[1]
COMM_PATH = ROOT / k.COMM_EXPECTATIONS_RELATIVE_PATH


def _sample():
    verified = datetime(2026, 10, 29, 11, tzinfo=timezone.utc)
    approved = verified + timedelta(hours=1)
    now = approved + timedelta(minutes=1)
    utc = lambda value: value.strftime("%Y-%m-%dT%H:%M:%SZ")
    prior = {
        "source_sha": "a" * 40,
        "runtime_inventory_digest": "b" * 64,
        "runtime_inventory_count": 12,
        "shopify_expectations_sha256": "c" * 64,
        "communication_expectations_sha256": "d" * 64,
        "independent_review_ref": "synthetic-review",
    }
    flags = {name: "false" for name in k.SHOPIFY_FLAG_ENVS + (k.COMM_SEND_FLAG_ENV,)}
    artifact = {
        "record_version": 2, "status": "READY",
        "rollback_mode": "compatible_source_flags_off",
        "phase": "S4_COMM_DORMANT",
        "database_heads": ["0111", "0113", "0125"],
        "shopify_table_count": 17, "bootstrap_target": "0093",
        "schema_action": "retain_0125_no_downgrade",
        "expectations_path": k.COMM_EXPECTATIONS_RELATIVE_PATH,
        "shopify_expectations_path": k.INSTALL_EXPECTATIONS_RELATIVE_PATH,
        "flags": flags, "compatible_rollback": prior,
    }
    bindings = {
        "artifact": artifact, "artifact_sha256": "e" * 64,
        "schema_expectations_sha256": "f" * 64,
        "communication_schema_sha256": "d" * 64,
    }
    target = dict(k.EXISTING_PLATFORM_TARGET)
    snapshot = {
        "railway_deployment_id": "11111111-1111-4111-8111-111111111111",
        "source_sha": prior["source_sha"],
        "verified_redeployable_at_utc": utc(verified),
    }
    evidence = {
        "record_version": 2, "phase": "S4_COMM_DORMANT",
        "source_sha": prior["source_sha"],
        "railway_deployment_id": snapshot["railway_deployment_id"],
        "target": target, "flags": flags,
        "database_heads": ["0111", "0113", "0125"],
        "table_names": sorted(k.INSTALL_TABLES + ("salla_communication_function_nonces",)),
        "runtime_inventory_digest": prior["runtime_inventory_digest"],
        "runtime_inventory_count": prior["runtime_inventory_count"],
        "schema_expectations_sha256": prior["shopify_expectations_sha256"],
        "communication_schema_sha256": "d" * 64,
        "artifact_sha256": "e" * 64,
        "independent_review_ref": prior["independent_review_ref"],
        "verified_at_utc": utc(verified),
        "valid_until_utc": utc(verified + timedelta(hours=24)),
    }
    args = dict(release_sha="9" * 40, target=target, approved_at=approved, now=now)
    return evidence, snapshot, bindings, args


def test_0125_phase_is_separate_and_flags_stay_off():
    current = k.phase("S4_COMM_DORMANT")
    assert current["rows"] == frozenset({"0111", "0113", "0125"})
    assert current["branch_delta"][-2:] == ("0124", "0125")
    assert current["communication_schema_required"] is True
    assert k.phase("S4_DORMANT")["rows"] == frozenset({"0111", "0113", "0124"})
    assert k.phase("S4_PILOT")["rows"] == frozenset({"0111", "0113", "0124"})
    assert "S4_COMM_DORMANT" in k.SCHEMA_CHANGING_PHASES
    assert k.evaluate_flags({}, "S4_COMM_DORMANT")[0] == k.PASS
    assert k.evaluate_flags({k.COMM_SEND_FLAG_ENV: "true"}, "S4_COMM_DORMANT")[0] == k.FAIL
    assert k.evaluate_flags({k.SHOPIFY_FLAG_ENVS[0]: "true"}, "S4_COMM_DORMANT")[0] == k.FAIL


def test_0125_schema_manifest_is_bound_to_exact_migration():
    record = json.loads(COMM_PATH.read_text(encoding="utf-8"))
    migration = ROOT / record["migration_path"]
    assert migration.is_file()
    assert hashlib.sha256(migration.read_bytes()).hexdigest() == record["migration_sha256"]
    assert record["database_heads"] == ["0111", "0113", "0125"]
    assert record["schema_action"] == "retain_0125_no_downgrade"
    assert len(record["webhook_columns"]) == 4
    assert len(record["nonce_definition"]["columns"]) == 2
    assert len(record["nonce_definition"]["constraints"]) == 1


def test_migration_bytes_and_expectations_fail_closed(tmp_path):
    root = harness.build_root(tmp_path / "root")
    check, _ = gate.check_expectations(root, "S4_COMM_DORMANT")
    assert check.status == k.PASS
    migration = root / json.loads(COMM_PATH.read_text(encoding="utf-8"))["migration_path"]
    migration.write_bytes(migration.read_bytes() + b"\\n# synthetic drift\\n")
    check, _ = gate.check_expectations(root, "S4_COMM_DORMANT")
    assert check.status == k.FAIL and check.reason == k.R_EXPECTATIONS_STALE


def test_communication_schema_refuses_drift_without_reading_rows(monkeypatch):
    expected = json.loads(COMM_PATH.read_text(encoding="utf-8"))
    observed = {
        "relations": [f"public.{name}" for name in expected["communication_relations"]],
        "functions_count": 0,
        "constraints": ["public.salla_communication_function_nonces_pkey"],
        "nonce_definition": copy.deepcopy(expected["nonce_definition"]),
        "webhook_columns": copy.deepcopy(expected["webhook_columns"]),
    }
    monkeypatch.setattr(gate, "observe_communication_schema", lambda _conn: observed)
    assert gate.check_communication_schema(None, expected).status == k.PASS
    for change in (
        lambda o: o["relations"].append("public.salla_communication_extra"),
        lambda o: o.update(functions_count=1),
        lambda o: o["constraints"].clear(),
        lambda o: o["nonce_definition"]["columns"].pop(),
        lambda o: o["webhook_columns"].pop(),
        lambda o: o["webhook_columns"][0].__setitem__(1, "text"),
    ):
        drift = copy.deepcopy(observed)
        change(drift)
        monkeypatch.setattr(gate, "observe_communication_schema", lambda _conn: drift)
        assert gate.check_communication_schema(None, expected).reason == k.R_COMM_SCHEMA_MISMATCH


def test_0125_rollback_requires_reviewed_source_and_fresh_evidence():
    evidence, snapshot, bindings, args = _sample()
    assert k._evaluate_comm_schema_rollback(evidence, snapshot, bindings, **args) is None
    for mutate in (
        lambda e, s, b: b["artifact"].update(status="DRAFT_ONLY"),
        lambda e, s, b: e.update(database_heads=["0111", "0113", "0124"]),
        lambda e, s, b: e.update(source_sha="9" * 40),
        lambda e, s, b: e["flags"].update(SALLA_COMM_WHATSAPP_ENABLED="true"),
        lambda e, s, b: e.update(communication_schema_sha256="0" * 64),
        lambda e, s, b: e.update(artifact_sha256="0" * 64),
        lambda e, s, b: e.update(table_names=sorted(k.INSTALL_TABLES)),
        lambda e, s, b: s.update(source_sha="9" * 40),
    ):
        e, s, b = copy.deepcopy((evidence, snapshot, bindings))
        mutate(e, s, b)
        assert k._evaluate_comm_schema_rollback(e, s, b, **args) == k.R_SCHEMA_ROLLBACK_INVALID
    e, s, b = copy.deepcopy((evidence, snapshot, bindings))
    assert k._evaluate_comm_schema_rollback(
        e, s, b, **dict(args, now=args["approved_at"] + timedelta(hours=25))
    ) == k.R_SCHEMA_ROLLBACK_STALE

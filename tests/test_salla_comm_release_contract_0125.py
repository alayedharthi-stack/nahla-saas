"""Closed, DB-free 0125 release/rollback contract tests.

The PostgreSQL 18 migration and live catalog checks are a separate required job.
"""
from __future__ import annotations

import copy
from datetime import datetime, timedelta, timezone
import hashlib
import io
import json
from pathlib import Path
import subprocess
import tarfile

import pytest

from scripts.operators import shopify_release_gate_contract as k
from scripts.operators import shopify_release_gate as gate
from backend.tests import _shopify_release_gate_harness as harness


ROOT = Path(__file__).resolve().parents[1]
COMM_PATH = ROOT / k.COMM_EXPECTATIONS_RELATIVE_PATH
# The production head the owner authorized as this release's rollback target.
AUTHORIZED_PRODUCTION_HEAD = "a36ad7e3442bf23bfd56cdab243df006d5f11e05"
# Real observed case: the 6a8d7fa release head still carried the binding written
# for the earlier a36 release (roll back to the then-deployed fd6 source).
STALE_FD6_BINDING = {
    "source_sha": "fd6b89617b2beaf33d77830988e0e23bcfaf9daa",
    "runtime_inventory_digest": "703df1078ea5c438ec2aaf38b4531f478feef02b5bbc6f95b3f84bc03a3501fa",
    "runtime_inventory_count": 165,
}


def _manifest():
    return json.loads((ROOT / k.COMM_ROLLBACK_RELATIVE_PATH).read_text(encoding="utf-8"))


def _rollback_conflicts(manifest, release_sha):
    """Every way the rollback binding can disagree with the authorized production head."""
    head = manifest.get("authorized_production_head")
    prior = manifest.get("compatible_rollback") or {}
    observed = manifest.get("operator_observation") or {}
    problems = []
    if not isinstance(head, str) or not k.RELEASE_SHA_RE.fullmatch(head):
        problems.append("authorized_production_head_missing")
    if prior.get("source_sha") != head:
        problems.append("rollback_source_is_not_authorized_production_head")
    if observed.get("source_sha") != head:
        problems.append("operator_observation_is_for_another_source")
    if head == release_sha:
        problems.append("rollback_source_is_the_release_itself")
    return problems


def _release_sha():
    result = subprocess.run(["git", "-C", str(ROOT), "rev-parse", "HEAD"], capture_output=True, text=True)
    return result.stdout.strip() if result.returncode == 0 else "9" * 40


def _bound_sample(manifest):
    """The synthetic gate sample, re-bound to this manifest's exact rollback pins."""
    evidence, snapshot, bindings, args = _sample()
    prior = manifest["compatible_rollback"]
    bindings["artifact"] = manifest
    bindings["communication_schema_sha256"] = prior["communication_expectations_sha256"]
    snapshot["source_sha"] = prior["source_sha"]
    evidence.update(
        source_sha=prior["source_sha"],
        runtime_inventory_digest=prior["runtime_inventory_digest"],
        runtime_inventory_count=prior["runtime_inventory_count"],
        schema_expectations_sha256=prior["shopify_expectations_sha256"],
        communication_schema_sha256=prior["communication_expectations_sha256"],
        independent_review_ref=prior["independent_review_ref"],
    )
    return evidence, snapshot, bindings, args


def test_rollback_binding_is_the_authorized_production_head_and_fails_closed_until_reviewed():
    manifest = _manifest()
    prior = manifest["compatible_rollback"]
    observed = manifest["operator_observation"]
    assert manifest["authorized_production_head"] == AUTHORIZED_PRODUCTION_HEAD
    assert _rollback_conflicts(manifest, _release_sha()) == []
    assert prior["runtime_inventory_count"] == 166
    assert prior["communication_expectations_sha256"] == gate.file_sha256(COMM_PATH)
    # Nothing observed for the a36 deployment exists in the repository yet.
    assert all(observed[name] is None for name in observed
               if name not in {"source_sha", "requires_fresh_authenticated_provider_recheck"})
    assert observed["requires_fresh_authenticated_provider_recheck"] is True
    assert manifest["approval"] == "none"
    # No existing independent review covers a36 as this release's rollback
    # source, so the binding stays unusable until one is recorded.
    assert prior["independent_review_ref"] is None
    assert manifest["status"] != "READY"
    evidence, snapshot, bindings, args = _bound_sample(copy.deepcopy(manifest))
    assert k._evaluate_comm_schema_rollback(evidence, snapshot, bindings, **args) == k.R_SCHEMA_ROLLBACK_INVALID
    # Recording a review and READY is the only missing step; every pin is otherwise accepted.
    reviewed = copy.deepcopy(manifest)
    reviewed["status"] = "READY"
    reviewed["compatible_rollback"]["independent_review_ref"] = "synthetic-review-of-a36-binding"
    evidence, snapshot, bindings, args = _bound_sample(reviewed)
    assert k._evaluate_comm_schema_rollback(evidence, snapshot, bindings, **args) is None


@pytest.mark.parametrize("mutate,problem", [
    (lambda m: m["compatible_rollback"].update(STALE_FD6_BINDING), "rollback_source_is_not_authorized_production_head"),
    (lambda m: m["operator_observation"].update(source_sha=STALE_FD6_BINDING["source_sha"]),
     "operator_observation_is_for_another_source"),
    (lambda m: m.pop("authorized_production_head"), "authorized_production_head_missing"),
    (lambda m: m.update(authorized_production_head=STALE_FD6_BINDING["source_sha"]),
     "rollback_source_is_not_authorized_production_head"),
])
def test_rollback_source_conflicting_with_authorized_head_is_refused(mutate, problem):
    manifest = copy.deepcopy(_manifest())
    mutate(manifest)
    assert problem in _rollback_conflicts(manifest, _release_sha())


def test_release_cannot_name_itself_as_rollback():
    manifest = _manifest()
    assert "rollback_source_is_the_release_itself" in _rollback_conflicts(
        manifest, manifest["authorized_production_head"])


def _commit_available(sha):
    return subprocess.run(["git", "-C", str(ROOT), "cat-file", "-e", sha + "^{commit}"],
                          capture_output=True).returncode == 0


def test_rollback_pins_are_recomputed_from_the_authorized_source_tree(tmp_path):
    manifest = _manifest()
    head = manifest["authorized_production_head"]
    if not _commit_available(head):
        pytest.skip("authorized production head is not in this (shallow) clone")
    assert subprocess.run(["git", "-C", str(ROOT), "merge-base", "--is-ancestor", head, "HEAD"]).returncode == 0
    paths = ["database", *k.LEGACY_FIXED_FILES, *k.RUNTIME_EXTRA_FILES, k.INSTALL_EXPECTATIONS_RELATIVE_PATH]
    archive = subprocess.run(["git", "-C", str(ROOT), "archive", "--format=tar", head, "--", *sorted(set(paths))],
                             capture_output=True, check=True).stdout
    with tarfile.open(fileobj=io.BytesIO(archive)) as tar:
        tar.extractall(tmp_path, filter="data")
    prior = manifest["compatible_rollback"]
    assert gate.inventory(tmp_path) == (prior["runtime_inventory_count"], prior["runtime_inventory_digest"])
    assert gate.file_sha256(tmp_path / k.INSTALL_EXPECTATIONS_RELATIVE_PATH) == prior["shopify_expectations_sha256"]
    assert gate.file_sha256(tmp_path / k.COMM_EXPECTATIONS_RELATIVE_PATH) == prior["communication_expectations_sha256"]


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

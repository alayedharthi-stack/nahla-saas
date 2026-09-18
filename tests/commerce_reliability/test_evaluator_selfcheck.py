"""Evaluator self-tests (distinct from the runtime tests).

They prove the gate can fail: a valid reference passes, every rejection
class rejects, and the reconciliation plugin turns an unexpected pass or a
changed signature into a failure. No application code is exercised here.
"""
from __future__ import annotations

import ast
import datetime as dt
import json
import os
import subprocess
import sys
import textwrap
from pathlib import Path
from typing import Dict, List

import pytest

from tests.commerce_reliability import reliability_evaluator as ev
from tests.commerce_reliability import reliability_plugin as plugin

REPO_ROOT = Path(__file__).resolve().parents[2]
MANIFEST_PATH = Path(__file__).with_name("reliability_manifest.json")


def _reference_expectation(**overrides) -> ev.TurnExpectation:
    base = dict(tenant_id=1, terminal=ev.TERMINAL_PROVIDER_ACCEPTED)
    base.update(overrides)
    return ev.TurnExpectation(**base)


def _reference_evidence(**overrides) -> ev.TurnEvidence:
    evidence = ev.TurnEvidence(
        tenant_ids_touched=[1, 1],
        accepted_wamids=["wamid.accepted.1"],
        send_attempts=[{"n": 1, "type": "text", "status": 200, "wamid": "wamid.accepted.1"}],
        terminal=ev.TERMINAL_PROVIDER_ACCEPTED,
        terminal_source="lifecycle:end_ok+wamid",
        persisted_outbound=[{"tenant_id": 1, "reply_owner": "brain"}],
        evidence_refs=["lifecycle:end_ok"],
        guardrail_results=[{"name": "availability_truth_guard", "passed": True}],
        fixture_bindings=["provider:scripted_httpx"],
    )
    for key, value in overrides.items():
        setattr(evidence, key, value)
    return evidence


# ── Turn evaluator ───────────────────────────────────────────────────────────


def test_valid_reference_turn_passes() -> None:
    verdict = ev.evaluate_turn(_reference_expectation(), _reference_evidence())
    assert verdict.passed, verdict.blockers
    assert verdict.facts["accepted_sends"] == 1


def test_null_result_fails() -> None:
    verdict = ev.evaluate_turn(_reference_expectation(), ev.TurnEvidence())
    assert not verdict.passed
    assert "null_result:no_send_attempt_no_terminal" in verdict.blockers
    assert any(b.startswith("missing_terminal:") for b in verdict.blockers)


def test_wrong_tenant_evidence_rejected() -> None:
    verdict = ev.evaluate_turn(
        _reference_expectation(),
        _reference_evidence(tenant_ids_touched=[1, 2], persisted_outbound=[{"tenant_id": 2}]),
    )
    assert not verdict.passed
    assert "tenant_mismatch:2" in verdict.blockers
    assert "tenant_mismatch:persisted_outbound:2" in verdict.blockers


def test_duplicate_effects_rejected() -> None:
    verdict = ev.evaluate_turn(
        _reference_expectation(),
        _reference_evidence(
            accepted_wamids=["wamid.1", "wamid.1"],
            side_effect_keys=["order:abc", "order:abc"],
        ),
    )
    assert not verdict.passed
    assert "duplicate_effects:accepted_sends=2" in verdict.blockers
    assert "duplicate_effects:repeated_provider_message_id" in verdict.blockers
    assert "duplicate_effects:side_effect:order:abc" in verdict.blockers


def test_missing_terminal_record_rejected() -> None:
    verdict = ev.evaluate_turn(
        _reference_expectation(),
        _reference_evidence(terminal=None, terminal_source="lifecycle:absent"),
    )
    assert not verdict.passed
    assert "missing_terminal:lifecycle:absent" in verdict.blockers
    # A terminal outside the closed vocabulary is equally rejected.
    verdict = ev.evaluate_turn(_reference_expectation(), _reference_evidence(terminal="sent_probably"))
    assert "terminal_not_closed_enum:sent_probably" in verdict.blockers
    # A closed terminal that is not the expected one is rejected too.
    verdict = ev.evaluate_turn(
        _reference_expectation(), _reference_evidence(terminal=ev.TERMINAL_HUMAN_HANDOFF),
    )
    assert any(b.startswith("terminal_mismatch:") for b in verdict.blockers)


def test_inferred_end_ok_without_wamid_is_not_a_terminal() -> None:
    terminal, source = ev.derive_terminal(ev.LIFECYCLE_END_OK, [])
    assert terminal is None
    assert source.endswith("inferred_without_provider_acceptance")
    assert ev.derive_terminal(ev.LIFECYCLE_END_OK, ["wamid.1"])[0] == ev.TERMINAL_PROVIDER_ACCEPTED
    assert ev.derive_terminal(ev.LIFECYCLE_END_DELIVERY_FAILED, [])[0] == ev.TERMINAL_EXPLICIT_FAILURE
    assert ev.derive_terminal(ev.LIFECYCLE_END_OK, [], human_handoff=True)[0] == ev.TERMINAL_HUMAN_HANDOFF
    # Claiming provider acceptance without a provider message id is rejected.
    verdict = ev.evaluate_turn(_reference_expectation(), _reference_evidence(accepted_wamids=[]))
    assert "accepted_without_provider_message_id" in verdict.blockers
    # Provider acceptance when explicit failure was expected is rejected.
    verdict = ev.evaluate_turn(
        _reference_expectation(terminal=ev.TERMINAL_EXPLICIT_FAILURE), _reference_evidence(),
    )
    assert "failure_expected_but_provider_accepted" in verdict.blockers


def test_missing_required_evidence_rejected() -> None:
    verdict = ev.evaluate_turn(
        _reference_expectation(require_evidence_refs=True), _reference_evidence(evidence_refs=[]),
    )
    assert "missing_required_evidence" in verdict.blockers


def test_unexpected_runtime_fallback_disguised_as_success_rejected() -> None:
    verdict = ev.evaluate_turn(
        _reference_expectation(), _reference_evidence(fallback_kind=ev.FALLBACK_UNEXPECTED_RUNTIME),
    )
    assert not verdict.passed
    assert "unexpected_runtime_fallback_disguised_as_success" in verdict.blockers
    verdict = ev.evaluate_turn(_reference_expectation(), _reference_evidence(fallback_kind="creative"))
    assert "fallback_kind_not_closed_enum:creative" in verdict.blockers


def test_safe_fallback_only_passes_when_case_expects_it() -> None:
    safe = _reference_evidence(fallback_kind=ev.FALLBACK_EXPECTED_SAFE)
    rejected = ev.evaluate_turn(_reference_expectation(), safe)
    assert "fallback_not_expected_by_case" in rejected.blockers
    accepted = ev.evaluate_turn(
        _reference_expectation(expected_outcome=ev.OUTCOME_SAFE_MISSING_FACT), safe,
    )
    assert accepted.passed, accepted.blockers
    # Expecting a disclosed missing fact but observing a bare grounded-looking
    # answer with no evidence is rejected.
    bare = _reference_evidence(fallback_kind=ev.FALLBACK_NONE, evidence_refs=[])
    verdict = ev.evaluate_turn(
        _reference_expectation(expected_outcome=ev.OUTCOME_SAFE_MISSING_FACT), bare,
    )
    assert "expected_safe_fallback_missing" in verdict.blockers


def test_required_guardrail_evidence_cannot_pass_when_missing() -> None:
    expectation = _reference_expectation(required_guardrails=("availability_truth_guard",))
    assert ev.evaluate_turn(expectation, _reference_evidence()).passed
    missing = ev.evaluate_turn(expectation, _reference_evidence(guardrail_results=[]))
    assert "missing_required_guardrail_evidence:availability_truth_guard" in missing.blockers
    failed = ev.evaluate_turn(
        expectation,
        _reference_evidence(guardrail_results=[{"name": "availability_truth_guard", "passed": False}]),
    )
    assert "guardrail_failed:availability_truth_guard" in failed.blockers


def test_required_fixture_binding_cannot_pass_when_missing() -> None:
    expectation = _reference_expectation(required_fixture_bindings=("provider:scripted_httpx",))
    assert ev.evaluate_turn(expectation, _reference_evidence()).passed
    verdict = ev.evaluate_turn(expectation, _reference_evidence(fixture_bindings=[]))
    assert "missing_fixture_binding:provider:scripted_httpx" in verdict.blockers


def test_terminal_vocabulary_is_closed() -> None:
    assert ev.TERMINALS == frozenset({
        "provider_accepted", "human_handoff", "explicit_delivery_failure",
    })
    assert ev.FALLBACK_KINDS == frozenset({
        "none", "expected_safe_fallback", "expected_delivery_recovery", "unexpected_runtime_fallback",
    })


# ── Gate evaluator ───────────────────────────────────────────────────────────


def _manifest(**overrides) -> Dict:
    manifest = {
        "manifest_version": 1,
        "base_sha": "0" * 40,
        "required_modules": [
            {"path": "backend/tests/test_mod.py", "content_sha256": "abc", "min_tests": 2, "tier": "unit"},
        ],
        "required_tests": [
            {"nodeid": "backend/tests/test_mod.py::test_a", "tier": "unit"},
            {"nodeid": "backend/tests/test_mod.py::test_b", "tier": "unit"},
            {"nodeid": "tests/commerce_reliability/test_runtime_x.py::test_ok", "tier": "unit"},
            {"nodeid": "tests/commerce_reliability/test_runtime_pg.py::test_pg", "tier": "postgres"},
        ],
        "baseline_failures": [
            {
                "case_id": "RB-X", "owner": "UNASSIGNED (owner review)", "evidence": "e",
                "test_id": "tests/commerce_reliability/test_runtime_x.py::test_defect",
                "failure_signature": "sig_x", "base_sha": "0" * 40, "expiry": None,
                "removal_condition": "fix merged", "tier": "unit",
            },
        ],
        "unimplemented_contracts": [
            {
                "contract_id": "UC-X", "test_id": "tests/commerce_reliability/test_runtime_x.py::test_contract",
                "signature": "contract_missing", "planned_by": "later PR", "tier": "unit",
            },
        ],
        "postgres": {"tests": [{"nodeid": "tests/commerce_reliability/test_runtime_pg.py::test_pg"}]},
    }
    manifest.update(overrides)
    return manifest


def _records() -> List[ev.TestRecord]:
    """A baseline-compatible unit-tier run."""
    base = {
        "backend/tests/test_mod.py::test_a": ("passed", ""),
        "backend/tests/test_mod.py::test_b": ("passed", ""),
        "tests/commerce_reliability/test_runtime_x.py::test_ok": ("passed", ""),
        "tests/commerce_reliability/test_runtime_x.py::test_defect": ("xfailed", "RELIABILITY_BASELINE[RB-X] sig_x"),
        "tests/commerce_reliability/test_runtime_x.py::test_contract": (
            "xfailed", "RELIABILITY_UNIMPLEMENTED[UC-X] contract_missing",
        ),
    }
    return [
        ev.TestRecord(nodeid=n, outcome=o, message=m, file=n.split("::")[0]) for n, (o, m) in base.items()
    ]


_ENV_UNIT: Dict[str, str] = {}
_HASHES = {"backend/tests/test_mod.py": "abc"}


def test_gate_accepts_exact_baseline_observation() -> None:
    verdict = ev.evaluate_gate(_manifest(), tier="unit", records=_records(), env=_ENV_UNIT, module_hashes=_HASHES)
    assert verdict.passed, verdict.blockers
    assert verdict.sections["harness_integrity"]["counts"]["collected"] == 5
    assert [a["observed"] for a in verdict.sections["outstanding_baseline_failures"]] == ["expected_failure_observed"]
    assert [a["observed"] for a in verdict.sections["unimplemented_contracts"]] == ["expected_failure_observed"]
    assert "allowance_owner_unassigned_owner_review:RB-X" in verdict.warnings
    assert "allowance_expiry_unset_owner_review:RB-X" in verdict.warnings
    assert "NOT production acceptance" in verdict.sections["rollout_readiness"]["statement"]
    report = ev.render_gate_report(verdict, header={"base_sha": "0" * 40})
    assert "result=PASS" in report and "4. ROLLOUT READINESS" in report


def test_gate_rejects_empty_selection() -> None:
    verdict = ev.evaluate_gate(_manifest(), tier="unit", records=[], env=_ENV_UNIT)
    assert not verdict.passed
    assert "empty_selection:no_tests_collected" in verdict.blockers


def test_gate_rejects_missing_required_test_and_module() -> None:
    records = [r for r in _records() if not r.nodeid.startswith("backend/tests/test_mod.py")]
    verdict = ev.evaluate_gate(_manifest(), tier="unit", records=records, env=_ENV_UNIT, module_hashes=_HASHES)
    assert "missing_required_test:backend/tests/test_mod.py::test_a" in verdict.blockers
    assert any(b.startswith("required_module_not_executed:backend/tests/test_mod.py:0<2") for b in verdict.blockers)
    # A changed PR #1084 module is a blocker until the manifest is re-reviewed.
    changed = ev.evaluate_gate(
        _manifest(), tier="unit", records=_records(), env=_ENV_UNIT,
        module_hashes={"backend/tests/test_mod.py": "different"},
    )
    assert "required_module_changed:backend/tests/test_mod.py" in changed.blockers
    # A required test that ran but did not pass is a blocker.
    failed = ev.evaluate_gate(
        _manifest(), tier="unit",
        records=[
            ev.TestRecord(r.nodeid, "failed", "boom", r.file) if r.nodeid.endswith("::test_b") else r
            for r in _records()
        ],
        env=_ENV_UNIT, module_hashes=_HASHES,
    )
    assert "required_test_not_passed:backend/tests/test_mod.py::test_b:failed" in failed.blockers
    assert "test_failed:backend/tests/test_mod.py::test_b:boom" in failed.blockers


def test_gate_rejects_unexpected_skip() -> None:
    records = [
        ev.TestRecord(r.nodeid, "skipped", "no reason", r.file) if r.nodeid.endswith("::test_ok") else r
        for r in _records()
    ]
    verdict = ev.evaluate_gate(_manifest(), tier="unit", records=records, env=_ENV_UNIT, module_hashes=_HASHES)
    assert "unexpected_skip:tests/commerce_reliability/test_runtime_x.py::test_ok:no reason" in verdict.blockers
    assert "required_test_not_passed:tests/commerce_reliability/test_runtime_x.py::test_ok:skipped" in verdict.blockers


def test_gate_rejects_unexpected_pass_of_allowance() -> None:
    records = [
        ev.TestRecord(r.nodeid, "passed", "", r.file) if r.nodeid.endswith("::test_defect") else r
        for r in _records()
    ]
    verdict = ev.evaluate_gate(_manifest(), tier="unit", records=records, env=_ENV_UNIT, module_hashes=_HASHES)
    assert "reconcile:unexpected_pass:RB-X" in verdict.blockers
    assert [a["observed"] for a in verdict.sections["outstanding_baseline_failures"]] == ["unexpected_pass"]


def test_gate_rejects_changed_failure_signature() -> None:
    records = [
        ev.TestRecord(r.nodeid, "xfailed", "RELIABILITY_BASELINE[RB-X] other_signature", r.file)
        if r.nodeid.endswith("::test_defect") else r
        for r in _records()
    ]
    verdict = ev.evaluate_gate(_manifest(), tier="unit", records=records, env=_ENV_UNIT, module_hashes=_HASHES)
    assert any(b.startswith("reconcile:RB-X:xfailed:") for b in verdict.blockers)
    # A plain failure on an allowance test is a reconciliation, never absorbed.
    records = [
        ev.TestRecord(r.nodeid, "failed", "RECONCILE signature_changed:RB-X", r.file)
        if r.nodeid.endswith("::test_defect") else r
        for r in _records()
    ]
    verdict = ev.evaluate_gate(_manifest(), tier="unit", records=records, env=_ENV_UNIT, module_hashes=_HASHES)
    assert any(b.startswith("reconcile:RB-X:failed:RECONCILE") for b in verdict.blockers)
    # A missing allowance test is a blocker too (the allowance cannot be observed).
    records = [r for r in _records() if not r.nodeid.endswith("::test_defect")]
    verdict = ev.evaluate_gate(_manifest(), tier="unit", records=records, env=_ENV_UNIT, module_hashes=_HASHES)
    assert "allowance_test_missing:RB-X:tests/commerce_reliability/test_runtime_x.py::test_defect" in verdict.blockers


def test_gate_rejects_expired_allowance() -> None:
    manifest = _manifest()
    manifest["baseline_failures"][0]["expiry"] = "2026-01-01"
    verdict = ev.evaluate_gate(
        manifest, tier="unit", records=_records(), env=_ENV_UNIT,
        today=dt.date(2026, 9, 18), module_hashes=_HASHES,
    )
    assert "expired_allowance:RB-X:2026-01-01" in verdict.blockers
    fresh = ev.evaluate_gate(
        manifest, tier="unit", records=_records(), env=_ENV_UNIT,
        today=dt.date(2025, 12, 31), module_hashes=_HASHES,
    )
    assert fresh.passed, fresh.blockers
    manifest["baseline_failures"][0]["expiry"] = "not-a-date"
    assert ev.allowance_expired(manifest["baseline_failures"][0], dt.date(2025, 1, 1))


def test_gate_rejects_unlisted_xfail() -> None:
    records = _records() + [
        ev.TestRecord("tests/commerce_reliability/test_runtime_x.py::test_extra", "xfailed",
                      "RELIABILITY_BASELINE[RB-NEW] invented", "tests/commerce_reliability/test_runtime_x.py"),
    ]
    verdict = ev.evaluate_gate(_manifest(), tier="unit", records=records, env=_ENV_UNIT, module_hashes=_HASHES)
    assert any(b.startswith("unexpected_xfail:tests/commerce_reliability/test_runtime_x.py::test_extra") for b in verdict.blockers)


def test_postgres_tier_rejects_missing_required_configuration() -> None:
    pg_records = [ev.TestRecord("tests/commerce_reliability/test_runtime_pg.py::test_pg", "passed", "",
                                "tests/commerce_reliability/test_runtime_pg.py")]
    verdict = ev.evaluate_gate(_manifest(), tier="postgres", records=pg_records, env={})
    assert f"postgres_tier_requires_{ev.PG_REQUIRE_ENV}=1" in verdict.blockers
    verdict = ev.evaluate_gate(_manifest(), tier="postgres", records=pg_records, env={ev.PG_REQUIRE_ENV: "1"})
    assert f"missing_required_postgres_configuration:{ev.PG_ADMIN_DSN_ENV}" in verdict.blockers
    ok = ev.evaluate_gate(
        _manifest(), tier="postgres", records=pg_records,
        env={ev.PG_REQUIRE_ENV: "1", ev.PG_ADMIN_DSN_ENV: "postgresql://u:p@127.0.0.1:5433/postgres"},
    )
    assert ok.passed, ok.blockers
    assert ok.sections["harness_integrity"]["postgres_config"]["dsn"] == "set"  # never the secret


def test_postgres_tier_rejects_skipped_postgres_test() -> None:
    env = {ev.PG_REQUIRE_ENV: "1", ev.PG_ADMIN_DSN_ENV: "postgresql://u:p@127.0.0.1:5433/postgres"}
    skipped = [ev.TestRecord("tests/commerce_reliability/test_runtime_pg.py::test_pg", "skipped", "no pg",
                             "tests/commerce_reliability/test_runtime_pg.py")]
    verdict = ev.evaluate_gate(_manifest(), tier="postgres", records=skipped, env=env)
    assert "postgres_test_skipped:tests/commerce_reliability/test_runtime_pg.py::test_pg:no pg" in verdict.blockers
    verdict = ev.evaluate_gate(_manifest(), tier="postgres", records=[
        ev.TestRecord("tests/commerce_reliability/test_runtime_pg.py::test_other", "passed", "",
                      "tests/commerce_reliability/test_runtime_pg.py")], env=env)
    assert "postgres_test_not_executed:tests/commerce_reliability/test_runtime_pg.py::test_pg" in verdict.blockers


def test_junit_parser_maps_xfail_and_nodeids(tmp_path: Path) -> None:
    xml = textwrap.dedent("""\
        <?xml version="1.0" encoding="utf-8"?>
        <testsuites><testsuite name="pytest" tests="4" skipped="2" failures="1" errors="0">
          <testcase classname="tests.commerce_reliability.test_runtime_x" name="test_ok"
                    file="tests/commerce_reliability/test_runtime_x.py" line="1" time="0.1"/>
          <testcase classname="tests.commerce_reliability.test_runtime_x" name="test_defect"
                    file="tests/commerce_reliability/test_runtime_x.py" line="2" time="0.1">
            <skipped type="pytest.xfail" message="RELIABILITY_BASELINE[RB-X] sig_x"/>
          </testcase>
          <testcase classname="tests.commerce_reliability.test_runtime_x" name="test_skip"
                    file="tests/commerce_reliability/test_runtime_x.py" line="3" time="0.1">
            <skipped type="pytest.skip" message="no pg"/>
          </testcase>
          <testcase classname="tests.commerce_reliability.test_runtime_x.TestK" name="test_fail[a-b]"
                    file="tests/commerce_reliability/test_runtime_x.py" line="4" time="0.1">
            <failure message="RECONCILE unexpected_pass:RB-Y">trace</failure>
          </testcase>
        </testsuite></testsuites>
    """)
    path = tmp_path / "j.xml"
    path.write_text(xml, encoding="utf-8")
    records = {r.nodeid: r for r in ev.parse_junit(path)}
    assert records["tests/commerce_reliability/test_runtime_x.py::test_ok"].outcome == "passed"
    assert records["tests/commerce_reliability/test_runtime_x.py::test_defect"].outcome == "xfailed"
    assert records["tests/commerce_reliability/test_runtime_x.py::test_defect"].message == "RELIABILITY_BASELINE[RB-X] sig_x"
    assert records["tests/commerce_reliability/test_runtime_x.py::test_skip"].outcome == "skipped"
    failed = records["tests/commerce_reliability/test_runtime_x.py::TestK::test_fail[a-b]"]
    assert failed.outcome == "failed" and failed.message.startswith("RECONCILE unexpected_pass")
    assert ev.parse_junit(tmp_path / "missing.xml") == []


# ── The manifest in the repository ───────────────────────────────────────────


def _function_names(path: Path) -> set:
    tree = ast.parse(path.read_text(encoding="utf-8"))
    return {node.name for node in tree.body if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))}


def _nodeid_parts(nodeid: str):
    file, _, rest = nodeid.partition("::")
    name = rest.split("::")[-1].split("[", 1)[0]
    return file, name


def test_manifest_in_repo_is_well_formed_and_self_consistent() -> None:
    manifest = ev.load_manifest(MANIFEST_PATH)
    assert manifest["base_sha"] == "4a270df82bcf11cdd88b1ef6d9946dc2dc46ca3e"
    # Every listed test exists as a function in the named file.
    listed = [t["nodeid"] for t in manifest["required_tests"]]
    listed += [b["test_id"] for b in manifest["baseline_failures"]]
    listed += [u["test_id"] for u in manifest["unimplemented_contracts"]]
    listed += [t["nodeid"] for t in manifest["postgres"]["tests"]]
    names_by_file: Dict[str, set] = {}
    for nodeid in listed:
        file, name = _nodeid_parts(nodeid)
        path = REPO_ROOT / file
        assert path.is_file(), f"manifest names a missing file: {file}"
        names = names_by_file.setdefault(file, _function_names(path))
        assert name in names, f"manifest names a missing test: {nodeid}"
    # Allowance tests are never also required-to-pass tests.
    required = {t["nodeid"] for t in manifest["required_tests"]}
    allowances = ev.allowance_index(manifest)
    assert not (required & set(allowances)), required & set(allowances)
    # Allowance ids are unique and every allowance is a recorded failure at base_sha.
    ids = [a["id"] for a in allowances.values()]
    assert len(ids) == len(set(ids))
    for entry in manifest["baseline_failures"]:
        assert entry["base_sha"] == manifest["base_sha"]
        assert entry["removal_condition"]
        assert entry["owner"]  # explicit, even when UNASSIGNED for owner review
    # The PR #1084 modules are pinned unchanged.
    for module in manifest["required_modules"]:
        assert ev.sha256_of(REPO_ROOT / module["path"]) == module["content_sha256"], (
            f"{module['path']} changed: update the manifest hash in a reviewed PR"
        )
    # Every PostgreSQL-tier test is declared in the postgres section.
    pg_declared = {t["nodeid"] for t in manifest["postgres"]["tests"]}
    for nodeid, entry in allowances.items():
        if entry.get("tier") == ev.TIER_POSTGRES:
            assert nodeid in pg_declared, nodeid
    for req in manifest["required_tests"]:
        if req.get("tier") == ev.TIER_POSTGRES:
            assert req["nodeid"] in pg_declared, req["nodeid"]
    assert manifest["allowed_skips"] == []


# ── Reconciliation plugin, proven in a separate pytest session ──────────────


@pytest.fixture(scope="module")
def reconciliation_session(tmp_path_factory: pytest.TempPathFactory) -> Dict[str, ev.TestRecord]:
    """Run an inner pytest session where the plugin sees a temporary manifest."""
    root = tmp_path_factory.mktemp("reconcile")
    manifest = {
        "manifest_version": 1,
        "base_sha": "0" * 40,
        "required_modules": [],
        "required_tests": [],
        "baseline_failures": [
            {
                "case_id": "RB-PASS", "owner": "UNASSIGNED (owner review)", "evidence": "e",
                "test_id": "test_probe.py::test_now_passes", "failure_signature": "sig",
                "base_sha": "0" * 40, "expiry": None, "removal_condition": "r", "tier": "unit",
            },
            {
                "case_id": "RB-CHANGED", "owner": "UNASSIGNED (owner review)", "evidence": "e",
                "test_id": "test_probe.py::test_fails_differently", "failure_signature": "sig_recorded",
                "base_sha": "0" * 40, "expiry": None, "removal_condition": "r", "tier": "unit",
            },
            {
                "case_id": "RB-EXACT", "owner": "UNASSIGNED (owner review)", "evidence": "e",
                "test_id": "test_probe.py::test_exact_signature", "failure_signature": "sig_exact",
                "base_sha": "0" * 40, "expiry": None, "removal_condition": "r", "tier": "unit",
            },
            {
                "case_id": "RB-EXPIRED", "owner": "UNASSIGNED (owner review)", "evidence": "e",
                "test_id": "test_probe.py::test_expired", "failure_signature": "sig_expired",
                "base_sha": "0" * 40, "expiry": "2026-01-01", "removal_condition": "r", "tier": "unit",
            },
            {
                "case_id": "RB-OTHER", "owner": "UNASSIGNED (owner review)", "evidence": "e",
                "test_id": "test_probe.py::test_wrong_case_id", "failure_signature": "sig_other",
                "base_sha": "0" * 40, "expiry": None, "removal_condition": "r", "tier": "unit",
            },
        ],
        "unimplemented_contracts": [
            {
                "contract_id": "UC-EXACT", "test_id": "test_probe.py::test_contract_exact",
                "signature": "contract_sig", "planned_by": "later", "tier": "unit",
            },
        ],
        "postgres": {"tests": []},
    }
    (root / "manifest.json").write_text(json.dumps(manifest), encoding="utf-8")
    (root / "pytest.ini").write_text("[pytest]\n", encoding="utf-8")
    (root / "conftest.py").write_text(textwrap.dedent(f"""\
        import sys
        sys.path.insert(0, {str(REPO_ROOT)!r})
        from tests.commerce_reliability.reliability_plugin import *  # noqa: F401,F403
    """), encoding="utf-8")
    (root / "test_probe.py").write_text(textwrap.dedent("""\
        def test_now_passes(baseline):
            assert True

        def test_fails_differently(baseline):
            baseline.defect("RB-CHANGED", "sig_observed_now")

        def test_exact_signature(baseline):
            baseline.defect("RB-EXACT", "sig_exact")

        def test_expired(baseline):
            baseline.defect("RB-EXPIRED", "sig_expired")

        def test_wrong_case_id(baseline):
            baseline.defect("RB-SOMETHING-ELSE", "sig_other")

        def test_contract_exact(unimplemented):
            unimplemented.contract("UC-EXACT", "contract_sig")

        def test_unlisted_plain_pass():
            assert True
    """), encoding="utf-8")
    junit = root / "junit.xml"
    env = dict(os.environ)
    env[plugin.MANIFEST_ENV] = str(root / "manifest.json")
    env[plugin.TODAY_ENV] = "2026-09-18"
    env.pop("PYTEST_ADDOPTS", None)
    proc = subprocess.run(
        [sys.executable, "-m", "pytest", "-p", "no:cacheprovider", "-c", str(root / "pytest.ini"),
         "--rootdir", str(root), str(root / "test_probe.py"), "-q", "-o", "junit_family=xunit1",
         f"--junitxml={junit}"],
        cwd=str(root), env=env, capture_output=True, text=True, timeout=300,
    )
    records = {r.nodeid: r for r in ev.parse_junit(junit)}
    assert records, f"inner session produced no records\n{proc.stdout}\n{proc.stderr}"
    records["__stdout__"] = ev.TestRecord("__stdout__", "info", proc.stdout + proc.stderr)
    return records


def test_reconciliation_plugin_fails_unexpected_pass(reconciliation_session) -> None:
    rec = reconciliation_session["test_probe.py::test_now_passes"]
    assert rec.outcome == "failed", (rec, reconciliation_session["__stdout__"].message)
    assert rec.message.startswith("RECONCILE unexpected_pass:RB-PASS")


def test_reconciliation_plugin_fails_changed_signature(reconciliation_session) -> None:
    rec = reconciliation_session["test_probe.py::test_fails_differently"]
    assert rec.outcome == "failed", rec
    assert rec.message.startswith("RECONCILE signature_changed:RB-CHANGED")
    assert "sig_observed_now" in rec.message and "sig_recorded" in rec.message
    expired = reconciliation_session["test_probe.py::test_expired"]
    assert expired.outcome == "failed" and expired.message.startswith("RECONCILE expired_allowance:RB-EXPIRED")
    wrong = reconciliation_session["test_probe.py::test_wrong_case_id"]
    assert wrong.outcome == "failed" and "RECONCILE" in wrong.message


def test_reconciliation_plugin_marks_exact_signature_as_expected_failure(reconciliation_session) -> None:
    rec = reconciliation_session["test_probe.py::test_exact_signature"]
    assert rec.outcome == "xfailed", rec
    assert rec.message == "RELIABILITY_BASELINE[RB-EXACT] sig_exact"
    contract = reconciliation_session["test_probe.py::test_contract_exact"]
    assert contract.outcome == "xfailed" and contract.message == "RELIABILITY_UNIMPLEMENTED[UC-EXACT] contract_sig"
    assert reconciliation_session["test_probe.py::test_unlisted_plain_pass"].outcome == "passed"

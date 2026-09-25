"""Self-tests of ``scripts/required_postgres_proofs.py`` (no database needed).

Synthetic suites prove every strict rule with a negative control: missing
collection, uninventoried collection, a skip, a failure, an error, a missing
module, and missing or wrong configuration all fail; only an exact, fully
passing inventory is PROVEN. The last test pins the committed inventory to
pytest's own collection of the real modules.
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path
from typing import Any, Dict, List, Optional

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
RUNNER = REPO_ROOT / "scripts" / "required_postgres_proofs.py"
COMMITTED_MANIFEST = REPO_ROOT / "scripts" / "required_postgres_proofs.json"

PASSING_MODULE = '''
import pytest

@pytest.mark.parametrize("label", ["عطر ورد 100ml", "plain"])
def test_alpha(label):
    assert label

def test_beta():
    assert 1 + 1 == 2
'''
ENV_OK = {"PROOF_PG_REQUIRED": "1", "PROOF_PG_DSN": "postgresql://u:p@127.0.0.1:1/never-connected"}


def _write_module(root: Path, name: str, source: str) -> str:
    module_dir = root / "proofs"
    module_dir.mkdir(exist_ok=True)
    (module_dir / name).write_text(source, encoding="utf-8")
    return f"proofs/{name}"


def _collect(root: Path, module: str) -> List[str]:
    out = subprocess.run(
        [sys.executable, "-m", "pytest", "--collect-only", "-q", "-p", "no:cacheprovider", module],
        cwd=str(root), capture_output=True, text=True, check=True,
    ).stdout
    return [line.strip() for line in out.splitlines() if line.startswith(module + "::")]


def _manifest(root: Path, module: str, nodeids: List[str], *, env: Optional[Dict[str, Any]] = None) -> Path:
    path = root / "manifest.json"
    path.write_text(json.dumps({"suites": [{
        "id": "synthetic", "module": module, "nodeids": nodeids,
        "required_env": {"PROOF_PG_REQUIRED": "1", "PROOF_PG_DSN": None} if env is None else env,
    }]}), encoding="utf-8")
    return path


def _run(root: Path, manifest: Path, env: Dict[str, str]) -> Dict[str, Any]:
    junit_dir = root / "junit"
    report = root / "report.json"
    proc = subprocess.run(
        [sys.executable, str(RUNNER), "--root", str(root), "--manifest", str(manifest),
         "--junit-dir", str(junit_dir), "--report", str(report)],
        cwd=str(root), capture_output=True, text=True,
        env={**{k: v for k, v in os.environ.items() if not k.startswith("PROOF_")}, **env},
    )
    return {
        "exit": proc.returncode, "stdout": proc.stdout, "stderr": proc.stderr,
        "report": json.loads(report.read_text(encoding="utf-8")) if report.exists() else None,
        "junit_written": (junit_dir / "synthetic.xml").exists(),
    }


@pytest.fixture
def synthetic_root(tmp_path: Path) -> Path:
    (tmp_path / "pytest.ini").write_text("[pytest]\n", encoding="utf-8")
    return tmp_path


def test_exact_passing_inventory_is_proven(synthetic_root: Path) -> None:
    module = _write_module(synthetic_root, "test_ok.py", PASSING_MODULE)
    nodeids = _collect(synthetic_root, module)
    assert len(nodeids) == 3 and any("\\u0639" in n for n in nodeids), nodeids  # escaped Arabic id round-trips
    result = _run(synthetic_root, _manifest(synthetic_root, module, nodeids), ENV_OK)
    assert result["exit"] == 0, result["stdout"]
    assert result["report"]["verdict"] == "PROVEN"
    suite = result["report"]["suites"][0]
    assert (suite["required"], suite["collected"], suite["passed"], suite["skipped"], suite["failed"], suite["errors"]) \
        == (3, 3, 3, 0, 0, 0)
    assert "0 skips tolerated" in result["stdout"]


def test_missing_collection_fails(synthetic_root: Path) -> None:
    module = _write_module(synthetic_root, "test_ok.py", PASSING_MODULE)
    nodeids = _collect(synthetic_root, module) + [f"{module}::test_never_written"]
    result = _run(synthetic_root, _manifest(synthetic_root, module, nodeids), ENV_OK)
    assert result["exit"] == 1
    assert any(b.startswith("missing_collection:") for b in result["report"]["suites"][0]["blockers"])


def test_uninventoried_collection_fails(synthetic_root: Path) -> None:
    module = _write_module(synthetic_root, "test_ok.py", PASSING_MODULE)
    nodeids = _collect(synthetic_root, module)
    result = _run(synthetic_root, _manifest(synthetic_root, module, nodeids[:-1]), ENV_OK)
    assert result["exit"] == 1
    blockers = result["report"]["suites"][0]["blockers"]
    assert any(b.startswith("uninventoried_collection:") for b in blockers)
    assert any(b.startswith("count_mismatch:") for b in blockers)


def test_a_skip_is_never_a_pass(synthetic_root: Path) -> None:
    module = _write_module(synthetic_root, "test_skip.py", PASSING_MODULE + '''
def test_gamma():
    pytest.skip("PostgreSQL not configured")
''')
    nodeids = _collect(synthetic_root, module)
    result = _run(synthetic_root, _manifest(synthetic_root, module, nodeids), ENV_OK)
    assert result["exit"] == 1
    blockers = result["report"]["suites"][0]["blockers"]
    assert any(b.startswith(f"skipped:{module}::test_gamma") for b in blockers), blockers
    assert "count_nonzero:skipped=1" in blockers
    assert result["report"]["suites"][0]["passed"] == 3


def test_a_failure_and_an_error_fail(synthetic_root: Path) -> None:
    module = _write_module(synthetic_root, "test_bad.py", PASSING_MODULE + '''
def test_delta():
    assert False, "proof broken"

@pytest.fixture
def exploding():
    raise RuntimeError("fixture exploded")

def test_epsilon(exploding):
    pass
''')
    nodeids = _collect(synthetic_root, module)
    result = _run(synthetic_root, _manifest(synthetic_root, module, nodeids), ENV_OK)
    assert result["exit"] == 1
    blockers = result["report"]["suites"][0]["blockers"]
    assert any(b.startswith(f"failed:{module}::test_delta") for b in blockers), blockers
    assert any(b.startswith(f"error:{module}::test_epsilon") for b in blockers), blockers
    assert any(b.startswith("pytest_exit_code:") for b in blockers)


def test_missing_configuration_fails_before_pytest_starts(synthetic_root: Path) -> None:
    module = _write_module(synthetic_root, "test_ok.py", PASSING_MODULE)
    manifest = _manifest(synthetic_root, module, _collect(synthetic_root, module))
    absent = _run(synthetic_root, manifest, {})
    assert absent["exit"] == 2 and not absent["junit_written"]
    assert absent["report"]["verdict"] == "NOT RUN"
    assert "missing_env:PROOF_PG_REQUIRED" in absent["report"]["suites"]["synthetic"]
    blank = _run(synthetic_root, manifest, {**ENV_OK, "PROOF_PG_DSN": "   "})
    assert blank["exit"] == 2 and "missing_env:PROOF_PG_DSN" in blank["report"]["suites"]["synthetic"]
    wrong = _run(synthetic_root, manifest, {**ENV_OK, "PROOF_PG_REQUIRED": "0"})
    assert wrong["exit"] == 2 and not wrong["junit_written"]
    assert "env_value_mismatch:PROOF_PG_REQUIRED:expected=1" in wrong["report"]["suites"]["synthetic"]


def test_missing_module_fails_without_running(synthetic_root: Path) -> None:
    manifest = _manifest(synthetic_root, "proofs/test_absent.py", ["proofs/test_absent.py::test_x"])
    result = _run(synthetic_root, manifest, ENV_OK)
    assert result["exit"] == 2 and not result["junit_written"]
    assert "module_missing:proofs/test_absent.py" in result["report"]["suites"]["synthetic"]


def test_malformed_manifest_is_rejected(synthetic_root: Path) -> None:
    module = _write_module(synthetic_root, "test_ok.py", PASSING_MODULE)
    for bad in (
        {"suites": []},
        {"suites": [{"id": "s", "module": module, "nodeids": ["other/test_x.py::test_y"]}]},
        {"suites": [{"id": "s", "module": module, "nodeids": [f"{module}::a", f"{module}::a"]}]},
        {"suites": [{"id": "s", "module": module, "nodeids": [f"{module}::a"], "required_env": {"X": 1}}]},
    ):
        path = synthetic_root / "manifest.json"
        path.write_text(json.dumps(bad), encoding="utf-8")
        result = _run(synthetic_root, path, ENV_OK)
        assert result["exit"] == 2, bad
        assert "NOT RUN" in result["stdout"]


def test_committed_inventory_matches_pytest_collection_exactly() -> None:
    """No fixed counts here on purpose: the inventory must equal pytest's own
    collection of each listed module, so a test added to a module without an
    inventory update fails this test instead of being silently omitted."""
    manifest = json.loads(COMMITTED_MANIFEST.read_text(encoding="utf-8"))
    ids = [s["id"] for s in manifest["suites"]]
    assert ids == ["commerce_runtime_foundation", "commerce_runtime_migration", "global_customer_identity",
                   "commerce_runtime_ledgers", "commerce_runtime_ledgers_migration", "commerce_runtime_agent_loop",
                   # Both branches' required proofs, in manifest order. The
                   # handover suites arrive with the commerce runtime; the
                   # address-candidate suite with the address work. Dropping
                   # either list would silently stop requiring one of them.
                   "commerce_runtime_pilot", "commerce_runtime_handover_migration",
                   "commerce_runtime_pilot_handover_controls",
                   "commerce_runtime_trial_evidence",
                   "commerce_runtime_synthetic_probe",
                   "runner_connection_regressions",
                   "salla_customer_address_candidates",
                   "salla_shipment_tracking",
                   # The navigation snapshot arrives with pagination: a page
                   # token that is not single-use, not scoped, or not bounded
                   # would be a key left in a door, so its proofs are required —
                   # as are the flow through the real runtime and the one
                   # total order the pages are cut from.
                   "commerce_runtime_navigation", "commerce_runtime_pagination",
                   "catalog_search_order"]
    harness_env = {"NAHLA_RELIABILITY_REQUIRE_PG": "1", "NAHLA_RELIABILITY_PG_ADMIN_DSN": None}
    expected = {
        "commerce_runtime_foundation": ("proof", harness_env),
        "commerce_runtime_migration": ("proof", harness_env),
        "global_customer_identity": ("proof", {"CUSTOMER_NAME_PROVENANCE_PG_REQUIRED": "1",
                                               "LEGACY_MIG_PG_TEST_DATABASE_URL": None}),
        "commerce_runtime_ledgers": ("proof", harness_env),
        "commerce_runtime_ledgers_migration": ("proof", harness_env),
        "commerce_runtime_agent_loop": ("proof", harness_env),
        "commerce_runtime_pilot": ("proof", harness_env),
        "commerce_runtime_handover_migration": ("proof", harness_env),
        "commerce_runtime_pilot_handover_controls": ("proof", harness_env),
        "commerce_runtime_trial_evidence": ("proof", harness_env),
        "commerce_runtime_synthetic_probe": ("proof", harness_env),
        "runner_connection_regressions": ("runner_regression", harness_env),
        "salla_customer_address_candidates": (
            "proof", {"LEGACY_MIG_PG_TEST_DATABASE_URL": None},
        ),
        "salla_shipment_tracking": (
            "proof", {"LEGACY_MIG_PG_TEST_DATABASE_URL": None},
        ),
        "commerce_runtime_navigation": ("proof", harness_env),
        "commerce_runtime_pagination": ("proof", harness_env),
        "catalog_search_order": ("proof", harness_env),
    }
    for suite in manifest["suites"]:
        kind, env = expected[suite["id"]]
        assert (suite["kind"], suite["required_env"]) == (kind, env), suite["id"]
        collected = _collect(REPO_ROOT, suite["module"])
        assert collected, suite["module"]
        assert collected == suite["nodeids"], (suite["id"], set(collected) ^ set(suite["nodeids"]))


def test_stale_report_and_junit_are_invalidated_before_validation(synthetic_root: Path) -> None:
    """A reused --report path never shows an earlier PROVEN after an early failure."""
    module = _write_module(synthetic_root, "test_ok.py", PASSING_MODULE)
    good = _manifest(synthetic_root, module, _collect(synthetic_root, module))
    first = _run(synthetic_root, good, ENV_OK)
    assert first["exit"] == 0 and first["report"]["verdict"] == "PROVEN" and first["junit_written"]
    # Early failure 1: unreadable manifest, same report and junit paths.
    broken = synthetic_root / "broken.json"
    broken.write_text("{not json", encoding="utf-8")
    second = _run(synthetic_root, broken, ENV_OK)
    assert second["exit"] == 2
    assert second["report"]["verdict"] == "NOT RUN" and second["report"]["reason"] == "manifest"
    assert not second["junit_written"], "stale JUnit from the earlier PROVEN run survived"
    # Early failure 2: missing configuration after a PROVEN run.
    third_ok = _run(synthetic_root, good, ENV_OK)
    assert third_ok["report"]["verdict"] == "PROVEN"
    third = _run(synthetic_root, good, {})
    assert third["exit"] == 2 and third["report"]["verdict"] == "NOT RUN" and third["report"]["reason"] == "configuration"
    assert not third["junit_written"]


def test_suite_kinds_are_validated_and_counted_separately(synthetic_root: Path) -> None:
    module = _write_module(synthetic_root, "test_ok.py", PASSING_MODULE)
    nodeids = _collect(synthetic_root, module)
    bad = synthetic_root / "manifest.json"
    bad.write_text(json.dumps({"suites": [{"id": "s", "module": module, "nodeids": nodeids, "kind": "advisory"}]}),
                   encoding="utf-8")
    assert _run(synthetic_root, bad, ENV_OK)["exit"] == 2
    good = synthetic_root / "manifest.json"
    good.write_text(json.dumps({"suites": [
        {"id": "p", "module": module, "nodeids": nodeids, "kind": "proof",
         "required_env": {"PROOF_PG_REQUIRED": "1", "PROOF_PG_DSN": None}},
    ]}), encoding="utf-8")
    result = _run(synthetic_root, good, ENV_OK)
    assert result["exit"] == 0
    assert result["report"]["by_kind"] == {"proof": {"required": 3, "passed": 3},
                                           "runner_regression": {"required": 0, "passed": 0}}
    assert "3/3 proofs + 0/0 runner/fixture regressions" in result["stdout"]


def test_inventory_drift_is_detected_not_tolerated(synthetic_root: Path) -> None:
    """A module that gains a test the inventory does not list is NOT PROVEN."""
    module = _write_module(synthetic_root, "test_ok.py", PASSING_MODULE)
    manifest = _manifest(synthetic_root, module, _collect(synthetic_root, module))
    (synthetic_root / "proofs" / "test_ok.py").write_text(PASSING_MODULE + "\ndef test_added_later():\n    assert True\n",
                                                          encoding="utf-8")
    result = _run(synthetic_root, manifest, ENV_OK)
    assert result["exit"] == 1
    assert any(b.startswith(f"uninventoried_collection:{module}::test_added_later") for b in result["report"]["suites"][0]["blockers"])

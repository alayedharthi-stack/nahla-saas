"""Include catalog ownership contracts in the existing default test discovery.

The backend replay fixtures import their own ``tests`` package. A separate
process keeps that namespace and provider patch lifetime isolated from the
root suite, while using the same interpreter and installed dependencies.
"""
from pathlib import Path
import subprocess
import sys


def test_catalog_verification_and_wire_contracts():
    root = Path(__file__).resolve().parents[1]
    modules = (
        "test_catalog_guard_ownership.py",
        "test_catalog_semantic_contracts.py",
        "test_product_availability_truth_guard.py",
        "test_product_availability_truth_guard_shadow_observation_probe.py",
        "test_reply_metadata_export.py",
        "test_outbound_final_boundary.py",
        "test_pr975_production_orchestration_replay.py",
    )
    result = subprocess.run(
        [sys.executable, "-m", "pytest",
         *(f"backend/tests/{name}" for name in modules),
         "-q", "--tb=short", "--maxfail=1"],
        cwd=root,
        text=True,
        capture_output=True,
        timeout=180,
        check=False,
    )
    assert result.returncode == 0, result.stdout + result.stderr

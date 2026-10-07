"""Strict gate for the catalog-only Meta consent unit suite.

``backend/tests/test_meta_catalog_consent.py`` lives beside the router tests
it extends, but ``pytest.ini`` collects only ``tests/`` in the required
``lint-and-test`` job. This root test runs that module in a subprocess and
applies the repository's JUnit cleanliness guard (``scripts/check_junit_clean``):
every collected case must pass — zero skips, failures or errors — and the
executed count must equal the collected count. No workflow or gate changes.
"""
from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

from scripts.check_junit_clean import evaluate_junit_report

_REPO = Path(__file__).resolve().parents[1]
MODULE = "backend/tests/test_meta_catalog_consent.py"
MIN_CASES = 152  # the exact current count: removing a case must be deliberate


def _env() -> dict:
    env = {k: v for k, v in os.environ.items() if k != "DATABASE_URL"}
    env["PYTHONDONTWRITEBYTECODE"] = "1"
    return env


def test_consent_unit_suite_runs_clean_with_no_skips(tmp_path):
    collect = subprocess.run(
        [sys.executable, "-m", "pytest", MODULE, "--collect-only", "-q", "-p", "no:cacheprovider"],
        cwd=str(_REPO), env=_env(), capture_output=True, text=True, timeout=600,
    )
    assert collect.returncode == 0, collect.stdout[-3000:] + collect.stderr[-3000:]
    collected = [line for line in collect.stdout.splitlines() if line.startswith(MODULE + "::")]
    assert len(collected) >= MIN_CASES, len(collected)

    report = tmp_path / "meta-catalog-consent.xml"
    run = subprocess.run(
        [sys.executable, "-m", "pytest", MODULE, "-q", "-p", "no:cacheprovider", f"--junitxml={report}"],
        cwd=str(_REPO), env=_env(), capture_output=True, text=True, timeout=900,
    )
    result = evaluate_junit_report(report)
    assert run.returncode == 0 and result["ok"], (result, run.stdout[-4000:])
    assert result["tests"] == len(collected), (result, len(collected))
    assert (result["skipped"], result["failures"], result["errors"]) == (0, 0, 0)

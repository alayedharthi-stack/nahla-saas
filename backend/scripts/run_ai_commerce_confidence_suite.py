#!/usr/bin/env python3
"""
run_ai_commerce_confidence_suite.py
───────────────────────────────────
Single entrypoint for AI Commerce Confidence Gate.

Usage (from repo root):
  python backend/scripts/run_ai_commerce_confidence_suite.py

Exit code 0 only when every suite passes.

Policy: AI commerce regressions must be merchant-agnostic. See AGENTS.md
「Generic Commerce Regression Tests」. New suites must use generic store/product
fixtures and assert persisted state — not honey/Al Ayed-only examples or phrase-only truth.
"""
from __future__ import annotations

import subprocess
import sys
from pathlib import Path

BACKEND_DIR = Path(__file__).resolve().parents[1]
TESTS_DIR = BACKEND_DIR / "tests"

CONFIDENCE_SUITES = (
    "tests/test_store_ai_pause.py",
    "tests/test_ai_commerce_scenario_runner.py",
    "tests/test_ai_commerce_scenario_kb_shipping.py",
    "tests/test_ai_commerce_scenario_kb_delivery_fixes.py",
    "tests/test_ai_commerce_compose_smoke.py",
    "tests/test_ai_playground_dry_run.py",
    "tests/test_ai_playground_regression_scenarios.py",
    "tests/test_post_delivery_review_request.py",
    "tests/test_order_delivered_stamp.py",
    "tests/test_ai_commerce_confidence_hardening.py",
    "tests/test_ai_test_mode_allowlist.py",
    "tests/test_ai_commerce_known_customer_address_regression.py",
    "tests/test_ai_commerce_active_checkout_resume_address_regression.py",
)


def main() -> int:
    cmd = [
        sys.executable,
        "-m",
        "pytest",
        *CONFIDENCE_SUITES,
        "-q",
        "--tb=line",
    ]
    print("AI Commerce Confidence Gate")
    print("=" * 40)
    print("Running:")
    for suite in CONFIDENCE_SUITES:
        print(f"  - {suite}")
    print()
    result = subprocess.run(cmd, cwd=str(BACKEND_DIR), check=False)
    if result.returncode == 0:
        print("\nCONFIDENCE GATE: PASS")
    else:
        print("\nCONFIDENCE GATE: FAIL")
        print("Do not re-enable live customer AI until this suite is green.")
    return int(result.returncode)


if __name__ == "__main__":
    raise SystemExit(main())

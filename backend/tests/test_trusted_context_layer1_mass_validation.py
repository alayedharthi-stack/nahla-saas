"""Automated Trusted Context Layer 1 mass validation gate."""
from __future__ import annotations

import os
import sys

import pytest

_HERE = os.path.dirname(os.path.abspath(__file__))
if _HERE not in sys.path:
    sys.path.insert(0, _HERE)

from trusted_context_layer1_harness import (  # noqa: E402
    PRIVACY_TEST_COUPON_CODE,
    PerfCollector,
    execute_scenario,
    family_counts,
    mask_coupon_code,
    unique_contracts,
)
from trusted_context_layer1_scenarios import (  # noqa: E402
    DUPLICATE_EQUIVALENCE_GROUPS,
    SCENARIOS,
)

pytestmark = pytest.mark.trusted_context_layer1

PERF = PerfCollector()
MAX_SINGLE_SCENARIO_MS = 5000.0
MAX_SUITE_SECONDS = 300.0
_SCENARIO_FAILURES: list[str] = []


@pytest.fixture(scope="module", autouse=True)
def _perf_collector() -> PerfCollector:
    return PERF


@pytest.mark.parametrize("scenario", SCENARIOS, ids=lambda item: item.scenario_id)
def test_layer1_scenario_contract(scenario) -> None:
    try:
        execute_scenario(scenario, PERF)
    except Exception:
        _SCENARIO_FAILURES.append(scenario.scenario_id)
        raise
    latest = PERF.durations_ms[-1] if PERF.durations_ms else 0.0
    assert latest < MAX_SINGLE_SCENARIO_MS, f"{scenario.scenario_id} exceeded per-scenario budget"


def test_layer1_masked_coupon_code_contract() -> None:
    masked = mask_coupon_code(PRIVACY_TEST_COUPON_CODE)
    assert PRIVACY_TEST_COUPON_CODE not in masked
    assert masked.startswith("***")


def test_layer1_duplicate_equivalence_groups_are_documented() -> None:
    ids = {scenario.scenario_id for scenario in SCENARIOS}
    for group_name, members in DUPLICATE_EQUIVALENCE_GROUPS.items():
        assert len(members) >= 2, group_name
        for member in members:
            assert member in ids, member


def test_layer1_automated_validation_summary() -> None:
    handler_count = sum(1 for scenario in SCENARIOS if scenario.handler_path)
    contracts = unique_contracts(SCENARIOS)
    families = family_counts(SCENARIOS)
    assert len(SCENARIOS) == 120
    assert len(contracts) == 120
    assert handler_count == 24
    assert families["HP"] == 24
    assert not _SCENARIO_FAILURES, f"scenario failures: {_SCENARIO_FAILURES}"
    summary = PERF.summary()
    assert summary["total_suite_s"] < MAX_SUITE_SECONDS
    report = {
        "scenario_records": len(SCENARIOS),
        "unique_contracts": len(contracts),
        "family_counts": families,
        "handler_path_scenarios": handler_count,
        "xfail_count": 0,
        "perf_mocked_ms": summary,
    }
    print("LAYER 1 AUTOMATED VALIDATION PASS", report)

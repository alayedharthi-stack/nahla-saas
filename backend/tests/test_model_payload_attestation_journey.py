"""Automated 12-turn journey proving model payload attestation boundaries."""
from __future__ import annotations

import json
import os
import sys

import pytest

_HERE = os.path.dirname(os.path.abspath(__file__))
_BACKEND = os.path.abspath(os.path.join(_HERE, ".."))
if _BACKEND not in sys.path:
    sys.path.insert(0, _BACKEND)

from model_payload_attestation_journey_harness import (  # noqa: E402
    JOURNEY_TURNS,
    run_journey,
)


def test_model_payload_attestation_journey_runs_and_writes_report(tmp_path) -> None:
    import asyncio

    outcome = asyncio.run(run_journey(artifact_dir=tmp_path))
    report = outcome["report"]
    assert report["turn_count"] == len(JOURNEY_TURNS)
    assert report["automated_harness"].endswith("model_payload_attestation_journey_harness.py")
    assert report["live_whatsapp_run"].startswith("BLOCKED:")
    assert report["architecture_change_required"] == "no"
    assert len(report["turns"]) == len(JOURNEY_TURNS)
    for turn in report["turns"]:
        assert turn["message"]
        assert turn["stage_classification"] in {"A", "B", "C", "D", "E", "F"}
        for stage, attestation in (turn.get("attestations") or {}).items():
            assert isinstance(attestation, dict), stage
            assert attestation.get("stage") == stage

    report_path = tmp_path / "model_payload_attestation_journey_report.json"
    assert report_path.is_file()
    loaded = json.loads(report_path.read_text(encoding="utf-8"))
    assert loaded["production_sha"]
    assert loaded["failures_by_stage"]


def test_journey_harness_report_fields_present(tmp_path) -> None:
    """Validate report contract fields without writing default artifacts."""
    import asyncio

    from model_payload_attestation_journey_harness import run_journey

    outcome = asyncio.run(run_journey(artifact_dir=tmp_path))
    report = outcome["report"]
    required = (
        "production_sha",
        "automated_harness",
        "live_whatsapp_run",
        "facts_loaded",
        "facts_reaching_brain",
        "facts_reaching_model",
        "model_output_correct",
        "post_model_mutation",
        "failures_by_stage",
        "first_proven_loss_boundary",
        "missing_fact_categories",
        "proven_root_cause",
        "smallest_required_fix",
        "architecture_change_required",
    )
    for key in required:
        assert key in report, key

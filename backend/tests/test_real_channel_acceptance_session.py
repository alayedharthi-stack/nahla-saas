"""Contract tests for actual-provider acceptance sessions."""
from __future__ import annotations

import json
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

_REPO = Path(__file__).resolve().parents[2]
if str(_REPO) not in sys.path:
    sys.path.insert(0, str(_REPO))

from backend.tests._arch001_signoff_v2_fixture import install_production_v2_artifact
from scripts.operators.real_channel_acceptance_session import (  # noqa: E402
    classify_inbound_candidate,
    complete_scenario,
    load_session,
    record_device_attestation,
    start_session,
)
from scripts.operators.real_channel_conversational_acceptance_contract import (  # noqa: E402
    ARCH001_SHADOW_SIGNOFF_ENV,
    CODE_ACCEPTANCE_NOT_ENABLED,
    CODE_ARCH001_SIGNOFF_MISSING,
    CODE_CHANNEL_HEALTH_BLOCKED,
    CODE_DEVICE_ATTESTATION_REQUIRED,
    CODE_HUMAN_ASSESSMENT_REQUIRED,
    CODE_STAGING_IDENTITY_REJECTED,
    CODE_TENANT_1_PASS_ARTIFACT_INVALID,
    CODE_TENANT_NOT_ALLOWED,
    EVIDENCE_CHANNEL_ACTUAL_PROVIDER,
    EVIDENCE_CHANNEL_DIRECT_CODE_PROBE,
    EVIDENCE_CHANNEL_DIRECT_SIGNED_WEBHOOK,
    EVIDENCE_HMAC_KEY_ENV,
    EXECUTION_CONFIRM_ENV,
    MASTER_ENABLE_ENV,
    PHASE_TENANT_48_SALLA_MINIMAL,
    REVIEWER_ID_ENV,
    SESSION_DIR_ENV,
    SESSION_SCHEMA_VERSION,
    SESSION_STATE_HUMAN_ASSESSED,
    SESSION_STATE_OBSERVED,
    STAGING_ENVIRONMENT_ENV,
    STAGING_PROJECT_ENV,
    TENANT_48_SALLA_MINIMAL,
    hmac_identifier,
    resolve_acceptance_phase,
)


def _live_row(*, event_id: int = 11, provider_id: str = "wamid.HBgLREALSHAPED123456") -> dict:
    return {
        "id": event_id,
        "direction": "inbound",
        "created_at": datetime.now(timezone.utc),
        "metadata": {
            "message_origin": "live_webhook",
            "historical_import": False,
            "wa_message_id": provider_id,
            "phone": "15550001111",
        },
    }


def test_valid_looking_live_webhook_is_not_actual_channel_without_device_attestation() -> None:
    key = "unit-test-evidence-key"
    result = classify_inbound_candidate(
        _live_row(),
        event_cursor=10,
        started_at_utc=(datetime.now(timezone.utc) - timedelta(seconds=2)).isoformat(),
        expected_phone_hmac=hmac_identifier("15550001111", key=key),
        hmac_key=key,
    )
    assert result["eligible_provider_candidate"] is True
    assert result["evidence_channel"] == EVIDENCE_CHANNEL_DIRECT_SIGNED_WEBHOOK
    assert result["evidence_channel"] != EVIDENCE_CHANNEL_ACTUAL_PROVIDER


def test_synthetic_provider_marker_is_rejected() -> None:
    key = "unit-test-evidence-key"
    result = classify_inbound_candidate(
        _live_row(provider_id="wamid.synthetic-fixture-123456"),
        event_cursor=10,
        started_at_utc=(datetime.now(timezone.utc) - timedelta(seconds=2)).isoformat(),
        expected_phone_hmac=hmac_identifier("15550001111", key=key),
        hmac_key=key,
    )
    assert result["eligible_provider_candidate"] is False
    assert "inbound_provider_id_rejected" in result["blockers"]
    assert result["evidence_channel"] != EVIDENCE_CHANNEL_ACTUAL_PROVIDER


def test_direct_code_probe_marker_stays_not_real_channel() -> None:
    key = "unit-test-evidence-key"
    row = _live_row(provider_id="wamid.constitution-smoke-123456")
    row["metadata"]["acceptance_evidence_channel"] = EVIDENCE_CHANNEL_DIRECT_CODE_PROBE
    result = classify_inbound_candidate(
        row,
        event_cursor=10,
        started_at_utc=(datetime.now(timezone.utc) - timedelta(seconds=2)).isoformat(),
        expected_phone_hmac=hmac_identifier("15550001111", key=key),
        hmac_key=key,
    )
    assert result["evidence_channel"] == EVIDENCE_CHANNEL_DIRECT_CODE_PROBE
    assert result["eligible_provider_candidate"] is False


def _write_session(tmp_path: Path, *, with_device: bool = False) -> str:
    session_id = "12345678-1234-1234-1234-123456789abc"
    session = {
        "session_schema_version": SESSION_SCHEMA_VERSION,
        "session_id": session_id,
        "state": SESSION_STATE_HUMAN_ASSESSED,
        "scenario_ids": ["scenario-1"],
        "scenario_index": 0,
        "scenario_results": [],
        "active_scenario": {
            "scenario_id": "scenario-1",
            "machine_observation": {
                "machine_verdict": "candidate_pass",
                "evidence_channel": EVIDENCE_CHANNEL_DIRECT_SIGNED_WEBHOOK,
                "blockers": [],
                "inbound": {"eligible_provider_candidate": True},
                "outbound": {"provider_status": "sent"},
                "budgets": {},
            },
            "device_attestation": (
                {"sent_from_private_allowlisted_device": True} if with_device else None
            ),
            "human_assessment": {
                "rubric": {
                    "naturalness": "pass",
                    "context_continuity": "pass",
                    "audio_quality": "not_applicable",
                    "operational_truthfulness": "pass",
                }
            },
        },
    }
    (tmp_path / f"{session_id}.json").write_text(json.dumps(session), encoding="utf-8")
    return session_id


def test_complete_refuses_direct_signed_webhook_even_with_human_rubric(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv(SESSION_DIR_ENV, str(tmp_path))
    session_id = _write_session(tmp_path)
    result = complete_scenario(session_id, app_root=_REPO)
    assert result["ok"] is False
    assert result["verdict"] == "fail"
    assert "real_channel_required" in result["blockers"]
    assert CODE_DEVICE_ATTESTATION_REQUIRED in result["blockers"]


def test_device_attestation_upgrades_only_eligible_provider_candidate(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv(SESSION_DIR_ENV, str(tmp_path))
    monkeypatch.setenv(EVIDENCE_HMAC_KEY_ENV, "unit-test-evidence-key")
    monkeypatch.setenv(REVIEWER_ID_ENV, "reviewer-secret-ref")
    session_id = _write_session(tmp_path)
    session = load_session(session_id, _REPO)
    session["state"] = SESSION_STATE_OBSERVED
    (tmp_path / f"{session_id}.json").write_text(json.dumps(session), encoding="utf-8")
    result = record_device_attestation(
        session_id,
        provider="meta",
        sent_from_private_device=True,
        outbound_received_on_device=True,
        app_root=_REPO,
    )
    assert result["ok"] is True
    assert result["evidence_channel"] == EVIDENCE_CHANNEL_ACTUAL_PROVIDER


def test_device_attestation_cannot_upgrade_injected_fixture(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv(SESSION_DIR_ENV, str(tmp_path))
    monkeypatch.setenv(EVIDENCE_HMAC_KEY_ENV, "unit-test-evidence-key")
    monkeypatch.setenv(REVIEWER_ID_ENV, "reviewer-secret-ref")
    session_id = _write_session(tmp_path)
    session = load_session(session_id, _REPO)
    session["state"] = SESSION_STATE_OBSERVED
    session["active_scenario"]["machine_observation"]["inbound"][
        "eligible_provider_candidate"
    ] = False
    (tmp_path / f"{session_id}.json").write_text(json.dumps(session), encoding="utf-8")
    result = record_device_attestation(
        session_id,
        provider="meta",
        sent_from_private_device=True,
        outbound_received_on_device=True,
        app_root=_REPO,
    )
    assert result["ok"] is False
    assert result["evidence_channel"] == EVIDENCE_CHANNEL_DIRECT_SIGNED_WEBHOOK


def test_complete_requires_human_assessment(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv(SESSION_DIR_ENV, str(tmp_path))
    session_id = _write_session(tmp_path)
    session = load_session(session_id, _REPO)
    session["state"] = SESSION_STATE_OBSERVED
    (tmp_path / f"{session_id}.json").write_text(json.dumps(session), encoding="utf-8")
    with pytest.raises(ValueError, match=CODE_HUMAN_ASSESSMENT_REQUIRED):
        complete_scenario(session_id, app_root=_REPO)


def test_resolve_acceptance_phase_accepts_tenant_48() -> None:
    assert resolve_acceptance_phase(TENANT_48_SALLA_MINIMAL) == PHASE_TENANT_48_SALLA_MINIMAL


def test_resolve_acceptance_phase_rejects_arbitrary_tenant() -> None:
    with pytest.raises(ValueError, match=CODE_TENANT_NOT_ALLOWED):
        resolve_acceptance_phase(99)


def test_tenant_48_start_blocked_without_gates_not_tenant_1_artifact(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv(MASTER_ENABLE_ENV, raising=False)
    result = start_session(tenant_id=TENANT_48_SALLA_MINIMAL, app_root=_REPO)
    assert result["ok"] is False
    assert CODE_ACCEPTANCE_NOT_ENABLED in result["blockers"]
    assert CODE_TENANT_1_PASS_ARTIFACT_INVALID not in result["blockers"]


def test_tenant_48_start_blocked_without_arch_signoff(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    monkeypatch.setenv(MASTER_ENABLE_ENV, "true")
    monkeypatch.setenv(EXECUTION_CONFIRM_ENV, "true")
    monkeypatch.delenv("NAHLA_ARCH001_PREPROD_SYNTHETIC_SIGNOFF_V2_ARTIFACT", raising=False)
    result = start_session(tenant_id=TENANT_48_SALLA_MINIMAL, app_root=_REPO)
    assert result["ok"] is False
    assert "arch001_shadow_signoff_missing" in result["blockers"] or "bundle_invalid" in result["blockers"]
    assert CODE_TENANT_1_PASS_ARTIFACT_INVALID not in result["blockers"]


def test_tenant_48_start_blocks_on_staging_and_channel_prerequisites(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    monkeypatch.setenv(MASTER_ENABLE_ENV, "true")
    monkeypatch.setenv(EXECUTION_CONFIRM_ENV, "true")
    install_production_v2_artifact(monkeypatch, tmp_path)
    monkeypatch.delenv(STAGING_PROJECT_ENV, raising=False)
    monkeypatch.delenv(STAGING_ENVIRONMENT_ENV, raising=False)
    monkeypatch.delenv("DATABASE_URL", raising=False)
    monkeypatch.delenv("BACKEND_URL", raising=False)
    result = start_session(tenant_id=TENANT_48_SALLA_MINIMAL, app_root=_REPO)
    assert result["ok"] is False
    assert CODE_STAGING_IDENTITY_REJECTED in result["blockers"]
    assert CODE_CHANNEL_HEALTH_BLOCKED in result["blockers"]
    assert CODE_TENANT_1_PASS_ARTIFACT_INVALID not in result["blockers"]


def test_arbitrary_tenant_start_rejected() -> None:
    with pytest.raises(ValueError, match="tenant_not_allowed"):
        start_session(tenant_id=99, app_root=_REPO)

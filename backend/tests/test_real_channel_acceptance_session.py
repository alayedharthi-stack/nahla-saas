"""Contract tests for actual-provider acceptance sessions."""
from __future__ import annotations

import json
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

_REPO = Path(__file__).resolve().parents[2]
if str(_REPO) not in sys.path:
    sys.path.insert(0, str(_REPO))

from backend.tests._arch001_signoff_v2_fixture import install_production_v2_artifact
from scripts.operators.real_channel_acceptance_session import (  # noqa: E402
    classify_inbound_candidate,
    complete_scenario,
    load_session,
    next_scenario,
    observe,
    record_device_attestation,
    start_session,
)
from scripts.operators.real_channel_acceptance_order_side_effect_signatures import (  # noqa: E402
    build_order_side_effect_snapshot,
    extract_metadata_keys_by_row,
    extract_volatile_metadata_by_row,
    redacted_order_row_key,
)
from scripts.operators.real_channel_conversational_acceptance_contract import (  # noqa: E402
    ARCH001_SHADOW_SIGNOFF_ENV,
    CODE_ACCEPTANCE_NOT_ENABLED,
    CODE_ARCH001_SIGNOFF_MISSING,
    CODE_CHANNEL_HEALTH_BLOCKED,
    CODE_DEVICE_ATTESTATION_REQUIRED,
    CODE_HUMAN_ASSESSMENT_REQUIRED,
    CODE_ORDER_SIDE_EFFECT_DETECTED,
    CODE_OUTBOUND_PROVIDER_ID_MISSING,
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
    SESSION_STATE_AWAITING_DEVICE_SEND,
    SESSION_STATE_HUMAN_ASSESSED,
    SESSION_STATE_OBSERVED,
    SESSION_STATE_STARTED,
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


def _sample_order_row(*, status: str = "pending", metadata: dict | None = None) -> dict:
    return {
        "id": 10,
        "tenant_id": 1,
        "status": status,
        "total": "120.00",
        "line_items": [{"sku": "SKU-1", "qty": 1}],
        "source": "whatsapp",
        "is_abandoned": False,
        "external_id": "ext-100",
        "external_order_number": "ORD-100",
        "customer_id": 5,
        "customer_name": "Generic Customer",
        "customer_info": {"city": "الرياض"},
        "checkout_url": "https://example.test/checkout",
        "metadata": metadata
        if metadata is not None
        else {
            "payment_method": "cod",
            "payment_status": "pending",
            "last_synced_at": "2026-07-22T17:35:00+00:00",
        },
        "order_source_kind": "whatsapp",
        "identity_namespace": None,
        "integration_connection_id": None,
        "external_customer_ref": None,
        "external_customer_profile_id": None,
        "customer_link_state": "linked",
        "customer_link_evidence_class": "verified",
        "customer_link_source": "conversation",
        "customer_linked_at": "2026-07-22T17:00:00+00:00",
        "external_identity_link_state": None,
        "external_identity_evidence_class": None,
    }


def _order_side_effect_arm_for_row(row: dict) -> dict:
    row_fp = redacted_order_row_key(row["id"])
    return {
        "snapshot": build_order_side_effect_snapshot([row], tenant_id=1),
        "volatile_metadata_by_row": extract_volatile_metadata_by_row([row]),
        "metadata_keys_by_row": {row_fp: sorted(row["metadata"])},
    }


def _write_awaiting_session(
    tmp_path: Path,
    *,
    scenario_id: str = "t1_faq_hours",
    order_side_effect_arm: dict | None = None,
) -> str:
    session_id = "12345678-1234-1234-1234-123456789abc"
    key = "unit-test-evidence-key"
    session = {
        "session_schema_version": SESSION_SCHEMA_VERSION,
        "session_id": session_id,
        "state": SESSION_STATE_AWAITING_DEVICE_SEND,
        "tenant_id": 1,
        "event_cursor": 10,
        "usage_cursor": 0,
        "order_cursor": 0,
        "test_phone_hmac": hmac_identifier("15550001111", key=key),
        "scenario_ids": [scenario_id],
        "scenario_index": 0,
        "scenario_results": [],
        "totals": {"inbound": 0, "outbound_provider": 0, "llm_calls": 0, "cost_usd": 0.0},
        "active_scenario": {
            "scenario_id": scenario_id,
            "opened_at_utc": (datetime.now(timezone.utc) - timedelta(seconds=2)).isoformat(),
            "cursor": 10,
            "order_side_effect_arm": order_side_effect_arm,
            "outbound_expected": True,
            "send_type": "text",
        },
    }
    (tmp_path / f"{session_id}.json").write_text(json.dumps(session), encoding="utf-8")
    return session_id


def _mock_observation_rows(*, include_outbound: bool = False) -> tuple[list[dict], dict]:
    inbound = {
        "id": 11,
        "direction": "inbound",
        "created_at": datetime.now(timezone.utc),
        "metadata": {
            "message_origin": "live_webhook",
            "historical_import": False,
            "wa_message_id": "wamid.HBgLREALSHAPED123456",
            "phone": "15550001111",
        },
    }
    rows = [inbound]
    if include_outbound:
        rows.append(
            {
                "id": 12,
                "direction": "outbound",
                "created_at": datetime.now(timezone.utc),
                "metadata": {
                    "wa_message_id": "wamid.HBgLOUTBOUND123456",
                    "compose_source": "persona_llm",
                    "response_mode": "normal",
                    "chosen_path": "catalog",
                    "llm_candidate_present": True,
                    "final_text_transformed": False,
                    "final_transform_reasons": [],
                },
            }
        )
    usage = {
        "llm_calls": 1,
        "cost_usd": 0.01,
        "max_usage_id": 20,
        "tool_calls": 0,
        "trace_latency_ms": 500,
        "state_evidence": {},
    }
    return rows, usage


def test_next_scenario_arms_order_side_effect_snapshot(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv(SESSION_DIR_ENV, str(tmp_path))
    armed = _order_side_effect_arm_for_row(_sample_order_row())
    session_id = "12345678-1234-1234-1234-123456789abc"
    session = {
        "session_schema_version": SESSION_SCHEMA_VERSION,
        "session_id": session_id,
        "state": SESSION_STATE_STARTED,
        "tenant_id": 1,
        "event_cursor": 10,
        "usage_cursor": 0,
        "order_cursor": 0,
        "scenario_ids": ["t1_faq_hours"],
        "scenario_index": 0,
        "scenario_results": [],
        "totals": {"inbound": 0, "outbound_provider": 0, "llm_calls": 0, "cost_usd": 0.0},
        "active_scenario": None,
    }
    (tmp_path / f"{session_id}.json").write_text(json.dumps(session), encoding="utf-8")

    with patch("scripts.operators.real_channel_acceptance_session._engine") as engine_mock:
        connection = MagicMock()
        engine_mock.return_value.connect.return_value.__enter__.return_value = connection
        engine_mock.return_value.connect.return_value.__exit__.return_value = False
        with patch(
            "scripts.operators.real_channel_acceptance_session.capture_order_side_effect_arm",
            return_value=armed,
        ) as capture_mock:
            result = next_scenario(session_id, app_root=_REPO)

    assert result["ok"] is True
    capture_mock.assert_called_once_with(connection, tenant_id=1)
    loaded = load_session(session_id, _REPO)
    assert loaded["active_scenario"]["order_side_effect_arm"] == armed


@patch("scripts.operators.real_channel_acceptance_session._engine")
@patch("scripts.operators.real_channel_acceptance_session.fetch_tenant_order_rows")
@patch("scripts.operators.real_channel_acceptance_session._query_observation")
def test_observe_reports_outbound_provider_id_missing_without_name_error(
    mock_query: MagicMock,
    mock_fetch: MagicMock,
    mock_engine: MagicMock,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv(SESSION_DIR_ENV, str(tmp_path))
    monkeypatch.setenv(EVIDENCE_HMAC_KEY_ENV, "unit-test-evidence-key")
    row = _sample_order_row()
    session_id = _write_awaiting_session(
        tmp_path,
        order_side_effect_arm=_order_side_effect_arm_for_row(row),
    )
    mock_query.return_value = _mock_observation_rows(include_outbound=False)
    mock_fetch.return_value = [row]
    connection = MagicMock()
    mock_engine.return_value.connect.return_value.__enter__.return_value = connection
    mock_engine.return_value.connect.return_value.__exit__.return_value = False

    result = observe(session_id, app_root=_REPO)

    assert CODE_OUTBOUND_PROVIDER_ID_MISSING in result["blockers"]
    assert CODE_ORDER_SIDE_EFFECT_DETECTED not in result["blockers"]
    side_effects = result["state_evidence"]["order_side_effects"]
    assert side_effects["ai_side_effect_detected"] is False


@patch("scripts.operators.real_channel_acceptance_session._engine")
@patch("scripts.operators.real_channel_acceptance_session.fetch_tenant_order_rows")
@patch("scripts.operators.real_channel_acceptance_session._query_observation")
def test_observe_classifies_volatile_only_drift_without_side_effect_failure(
    mock_query: MagicMock,
    mock_fetch: MagicMock,
    mock_engine: MagicMock,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv(SESSION_DIR_ENV, str(tmp_path))
    monkeypatch.setenv(EVIDENCE_HMAC_KEY_ENV, "unit-test-evidence-key")
    armed_row = _sample_order_row()
    after_row = _sample_order_row(
        metadata={
            **armed_row["metadata"],
            "last_synced_at": "2026-07-22T17:39:18+00:00",
        }
    )
    session_id = _write_awaiting_session(
        tmp_path,
        order_side_effect_arm=_order_side_effect_arm_for_row(armed_row),
    )
    mock_query.return_value = _mock_observation_rows(include_outbound=False)
    mock_fetch.return_value = [after_row]
    connection = MagicMock()
    mock_engine.return_value.connect.return_value.__enter__.return_value = connection
    mock_engine.return_value.connect.return_value.__exit__.return_value = False

    result = observe(session_id, app_root=_REPO)

    assert CODE_ORDER_SIDE_EFFECT_DETECTED not in result["blockers"]
    drift = result["state_evidence"]["order_side_effects"]["concurrent_sync_drift"]
    assert len(drift) == 1
    assert drift[0]["drift_class"] == "volatile_sync_metadata"
    assert drift[0]["actor_attribution"] == "unverified"


@patch("scripts.operators.real_channel_acceptance_session._engine")
@patch("scripts.operators.real_channel_acceptance_session.fetch_tenant_order_rows")
@patch("scripts.operators.real_channel_acceptance_session._query_observation")
def test_observe_appends_order_side_effect_blocker_alongside_outbound_missing(
    mock_query: MagicMock,
    mock_fetch: MagicMock,
    mock_engine: MagicMock,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv(SESSION_DIR_ENV, str(tmp_path))
    monkeypatch.setenv(EVIDENCE_HMAC_KEY_ENV, "unit-test-evidence-key")
    armed_row = _sample_order_row(status="pending")
    after_row = _sample_order_row(status="paid")
    session_id = _write_awaiting_session(
        tmp_path,
        order_side_effect_arm=_order_side_effect_arm_for_row(armed_row),
    )
    mock_query.return_value = _mock_observation_rows(include_outbound=False)
    mock_fetch.return_value = [after_row]
    connection = MagicMock()
    mock_engine.return_value.connect.return_value.__enter__.return_value = connection
    mock_engine.return_value.connect.return_value.__exit__.return_value = False

    result = observe(session_id, app_root=_REPO)

    assert CODE_OUTBOUND_PROVIDER_ID_MISSING in result["blockers"]
    assert CODE_ORDER_SIDE_EFFECT_DETECTED in result["blockers"]

"""Regression tests for Meta acceptance channel evidence contract."""
from __future__ import annotations

import copy
import json
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

_REPO = Path(__file__).resolve().parents[2]
if str(_REPO) not in sys.path:
    sys.path.insert(0, str(_REPO))

from scripts.operators.meta_acceptance_channel_evidence_contract import (  # noqa: E402
    CODE_DB_WA_BINDING_MISMATCH,
    CODE_WEBHOOK_ATTESTATION_FORGED,
    CODE_WEBHOOK_ATTESTATION_MISSING,
    CODE_WEBHOOK_ATTESTATION_REVISION_MISMATCH,
    CODE_WEBHOOK_ATTESTATION_ROUTE_UNOBSERVED,
    CODE_WEBHOOK_ATTESTATION_STALE,
    CODE_WEBHOOK_ATTESTATION_TENANT_MISMATCH,
    EVIDENCE_CLASS_OPERATOR_OBSERVED_META_WEBHOOK,
    META_DIRECT_WEBHOOK_ROUTE,
    build_rollback_snapshot_evidence,
    build_webhook_attestation_artifact,
    evaluate_meta_config_present,
    evaluate_operator_attested_channel_ready,
    sign_webhook_attestation_artifact,
    webhook_attestation_gaps,
    whatsapp_connection_binding_gaps,
)
from scripts.operators.staging_acceptance_config_consolidation_contract import (  # noqa: E402
    D360_LEGACY_DETECTION_KEYS,
)

_HMAC_KEY = "unit-test-meta-acceptance-channel-evidence-key-32b"
_META_VARS = {
    "META_APP_SECRET": "present",
    "WHATSAPP_VERIFY_TOKEN": "present",
    "WHATSAPP_TOKEN": "present",
    "BACKEND_URL": "https://nahla-saas-staging.example.com",
}
_BACKEND_URL = _META_VARS["BACKEND_URL"]
_PINNED = "abc1234567890"
_DEPLOYMENT = "deploy-staging-001"
_TENANT_ID = 1
_TENANT_33 = 33
_WABA = "waba-tenant-1-example"
_PHONE = "phone-tenant-1-example"
_NOW = datetime(2026, 7, 22, 12, 0, tzinfo=timezone.utc)


def _valid_artifact(**overrides: object) -> dict[str, object]:
    artifact = build_webhook_attestation_artifact(
        tenant_id=_TENANT_ID,
        backend_url=_BACKEND_URL,
        pinned_revision=_PINNED,
        deployment_id=_DEPLOYMENT,
        observed_callback_route=META_DIRECT_WEBHOOK_ROUTE,
        waba_id=_WABA,
        phone_number_id=_PHONE,
        hmac_key=_HMAC_KEY,
        issued_at_utc=_NOW.isoformat(),
        expires_at_utc=(_NOW + timedelta(hours=1)).isoformat(),
        observed_at_utc=(_NOW - timedelta(minutes=15)).isoformat(),
    )
    artifact.update(overrides)
    if "signature" not in overrides:
        artifact["signature"] = sign_webhook_attestation_artifact(artifact, hmac_key=_HMAC_KEY)
    return artifact


def _db_row(*, tenant_id: int = _TENANT_ID, provider: str = "meta") -> dict[str, object]:
    return {
        "tenant_id": tenant_id,
        "provider": provider,
        "status": "connected",
        "sending_enabled": True,
        "phone_number_id": _PHONE,
        "whatsapp_business_account_id": _WABA,
    }


def test_meta_config_present_without_attestation_is_not_channel_ready() -> None:
    config = evaluate_meta_config_present(_META_VARS)
    assert config["meta_config_present"] is True
    evidence = evaluate_operator_attested_channel_ready(
        variables=_META_VARS,
        tenant_id=_TENANT_ID,
        artifact=None,
        hmac_key=_HMAC_KEY,
        backend_url=_BACKEND_URL,
        pinned_revision=_PINNED,
        deployment_id=_DEPLOYMENT,
        db_row=_db_row(),
    )
    assert evidence["meta_config_present"] is True
    assert evidence["operator_attested_channel_ready"] is False
    assert evidence["channel_evidence_class"] == EVIDENCE_CLASS_OPERATOR_OBSERVED_META_WEBHOOK
    assert "webhook_attestation_artifact" in evidence["webhook_attestation_gaps"]


def test_complete_operator_attestation_passes() -> None:
    artifact = _valid_artifact()
    evidence = evaluate_operator_attested_channel_ready(
        variables=_META_VARS,
        tenant_id=_TENANT_ID,
        artifact=artifact,
        hmac_key=_HMAC_KEY,
        backend_url=_BACKEND_URL,
        pinned_revision=_PINNED,
        deployment_id=_DEPLOYMENT,
        db_row=_db_row(),
        now=_NOW,
    )
    assert evidence["operator_attested_channel_ready"] is True
    assert evidence["observed_callback_route"] == META_DIRECT_WEBHOOK_ROUTE
    assert evidence["observation_source"] == "meta_developer_console_manual_review"


def test_unknown_observation_source_fails() -> None:
    artifact = _valid_artifact(observation_source="self_asserted_route")
    gaps = webhook_attestation_gaps(
        artifact,
        tenant_id=_TENANT_ID,
        backend_url=_BACKEND_URL,
        pinned_revision=_PINNED,
        deployment_id=_DEPLOYMENT,
        hmac_key=_HMAC_KEY,
        now=_NOW,
    )
    assert "webhook_attestation.observation_source" in gaps


def test_missing_observer_id_fails() -> None:
    artifact = _valid_artifact(observer_id="")
    gaps = webhook_attestation_gaps(
        artifact,
        tenant_id=_TENANT_ID,
        backend_url=_BACKEND_URL,
        pinned_revision=_PINNED,
        deployment_id=_DEPLOYMENT,
        hmac_key=_HMAC_KEY,
        now=_NOW,
    )
    assert "webhook_attestation.observer_id" in gaps


def test_observation_after_issued_fails() -> None:
    artifact = _valid_artifact(
        observed_at_utc=(_NOW + timedelta(minutes=5)).isoformat(),
        issued_at_utc=_NOW.isoformat(),
    )
    gaps = webhook_attestation_gaps(
        artifact,
        tenant_id=_TENANT_ID,
        backend_url=_BACKEND_URL,
        pinned_revision=_PINNED,
        deployment_id=_DEPLOYMENT,
        hmac_key=_HMAC_KEY,
        now=_NOW,
    )
    assert "webhook_attestation.observation_after_issued" in gaps


def test_stale_observation_fails() -> None:
    artifact = _valid_artifact(
        observed_at_utc=(_NOW - timedelta(hours=6)).isoformat(),
        issued_at_utc=_NOW.isoformat(),
    )
    gaps = webhook_attestation_gaps(
        artifact,
        tenant_id=_TENANT_ID,
        backend_url=_BACKEND_URL,
        pinned_revision=_PINNED,
        deployment_id=_DEPLOYMENT,
        hmac_key=_HMAC_KEY,
        now=_NOW,
    )
    assert "webhook_attestation.observation_stale" in gaps


def test_malformed_observation_evidence_digest_fails() -> None:
    artifact = _valid_artifact(observation_evidence_digest="not-a-digest")
    gaps = webhook_attestation_gaps(
        artifact,
        tenant_id=_TENANT_ID,
        backend_url=_BACKEND_URL,
        pinned_revision=_PINNED,
        deployment_id=_DEPLOYMENT,
        hmac_key=_HMAC_KEY,
        now=_NOW,
    )
    assert "webhook_attestation.observation_evidence_digest" in gaps


def test_missing_rollback_snapshot_evidence_fails() -> None:
    artifact = _valid_artifact()
    artifact.pop("rollback_snapshot_evidence")
    artifact["signature"] = sign_webhook_attestation_artifact(artifact, hmac_key=_HMAC_KEY)
    gaps = webhook_attestation_gaps(
        artifact,
        tenant_id=_TENANT_ID,
        backend_url=_BACKEND_URL,
        pinned_revision=_PINNED,
        deployment_id=_DEPLOYMENT,
        hmac_key=_HMAC_KEY,
        now=_NOW,
    )
    assert "webhook_attestation.rollback_snapshot_evidence" in gaps


def test_post_observation_rollback_snapshot_fails() -> None:
    artifact = _valid_artifact()
    rollback = copy.deepcopy(artifact["rollback_snapshot_evidence"])
    rollback["captured_at_utc"] = (_NOW + timedelta(minutes=1)).isoformat()
    artifact = _valid_artifact(rollback_snapshot_evidence=rollback)
    gaps = webhook_attestation_gaps(
        artifact,
        tenant_id=_TENANT_ID,
        backend_url=_BACKEND_URL,
        pinned_revision=_PINNED,
        deployment_id=_DEPLOYMENT,
        hmac_key=_HMAC_KEY,
        now=_NOW,
    )
    assert "rollback_snapshot_evidence.captured_after_observation" in gaps


def test_incomplete_rollback_snapshot_components_fail() -> None:
    rollback = build_rollback_snapshot_evidence(
        snapshot_fingerprint="snapshot-example",
        captured_at_utc=(_NOW - timedelta(minutes=20)).isoformat(),
        component_fingerprints={
            "meta_webhook_target": "prior-webhook-target-example",
        },
        hmac_key=_HMAC_KEY,
    )
    artifact = _valid_artifact(rollback_snapshot_evidence=rollback)
    gaps = webhook_attestation_gaps(
        artifact,
        tenant_id=_TENANT_ID,
        backend_url=_BACKEND_URL,
        pinned_revision=_PINNED,
        deployment_id=_DEPLOYMENT,
        hmac_key=_HMAC_KEY,
        now=_NOW,
    )
    assert any(gap.startswith("rollback_snapshot_evidence.components.") for gap in gaps)


def test_forged_attestation_signature_fails() -> None:
    artifact = _valid_artifact(signature="deadbeef")
    gaps = webhook_attestation_gaps(
        artifact,
        tenant_id=_TENANT_ID,
        backend_url=_BACKEND_URL,
        pinned_revision=_PINNED,
        deployment_id=_DEPLOYMENT,
        hmac_key=_HMAC_KEY,
        now=_NOW,
    )
    assert "webhook_attestation_signature" in gaps


def test_stale_attestation_fails() -> None:
    artifact = _valid_artifact(
        issued_at_utc=(_NOW - timedelta(days=2)).isoformat(),
        expires_at_utc=(_NOW - timedelta(days=1)).isoformat(),
    )
    gaps = webhook_attestation_gaps(
        artifact,
        tenant_id=_TENANT_ID,
        backend_url=_BACKEND_URL,
        pinned_revision=_PINNED,
        deployment_id=_DEPLOYMENT,
        hmac_key=_HMAC_KEY,
        now=_NOW,
    )
    assert "webhook_attestation.expired" in gaps


def test_wrong_revision_fails() -> None:
    artifact = _valid_artifact(pinned_revision="wrong-sha")
    gaps = webhook_attestation_gaps(
        artifact,
        tenant_id=_TENANT_ID,
        backend_url=_BACKEND_URL,
        pinned_revision=_PINNED,
        deployment_id=_DEPLOYMENT,
        hmac_key=_HMAC_KEY,
        now=_NOW,
    )
    assert "webhook_attestation.pinned_revision" in gaps


def test_wrong_tenant_fails() -> None:
    artifact = _valid_artifact(tenant_id=33)
    gaps = webhook_attestation_gaps(
        artifact,
        tenant_id=_TENANT_ID,
        backend_url=_BACKEND_URL,
        pinned_revision=_PINNED,
        deployment_id=_DEPLOYMENT,
        hmac_key=_HMAC_KEY,
        now=_NOW,
    )
    assert "webhook_attestation.tenant_id" in gaps


def test_unobserved_route_fails() -> None:
    artifact = _valid_artifact(observed_callback_route="/webhook/whatsapp/360dialog")
    gaps = webhook_attestation_gaps(
        artifact,
        tenant_id=_TENANT_ID,
        backend_url=_BACKEND_URL,
        pinned_revision=_PINNED,
        deployment_id=_DEPLOYMENT,
        hmac_key=_HMAC_KEY,
        now=_NOW,
    )
    assert "webhook_attestation.observed_callback_route" in gaps


def test_missing_db_binding_fails_for_all_tenants() -> None:
    artifact = _valid_artifact()
    gaps = whatsapp_connection_binding_gaps(
        None,
        tenant_id=_TENANT_33,
        artifact=artifact,
        hmac_key=_HMAC_KEY,
    )
    assert "db_wa_binding.row_missing" in gaps


def test_tenant_33_d360_binding_rejected() -> None:
    artifact = _valid_artifact(tenant_id=_TENANT_33)
    gaps = whatsapp_connection_binding_gaps(
        _db_row(tenant_id=_TENANT_33, provider="360dialog"),
        tenant_id=_TENANT_33,
        artifact=artifact,
        hmac_key=_HMAC_KEY,
    )
    assert "db_wa_binding.d360_provider_rejected" in gaps


def test_sending_enabled_none_fails() -> None:
    row = _db_row()
    row["sending_enabled"] = None
    gaps = whatsapp_connection_binding_gaps(
        row,
        tenant_id=_TENANT_ID,
        artifact=_valid_artifact(),
        hmac_key=_HMAC_KEY,
    )
    assert "db_wa_binding.sending_enabled" in gaps


def test_mismatched_db_fingerprint_fails() -> None:
    artifact = _valid_artifact()
    row = _db_row()
    row["phone_number_id"] = "different-phone"
    gaps = whatsapp_connection_binding_gaps(
        row,
        tenant_id=_TENANT_ID,
        artifact=artifact,
        hmac_key=_HMAC_KEY,
    )
    assert "db_wa_binding.phone_number_id_fingerprint" in gaps


def test_d360_only_variables_fail_meta_config() -> None:
    variables = {key: "legacy-present" for key in D360_LEGACY_DETECTION_KEYS}
    config = evaluate_meta_config_present(variables)
    assert config["meta_config_present"] is False
    assert config["d360_only_legacy_path"] is True


def test_artifact_json_contains_no_raw_ids() -> None:
    artifact = _valid_artifact()
    encoded = json.dumps(artifact)
    assert _WABA not in encoded
    assert _PHONE not in encoded
    assert _BACKEND_URL not in encoded


@pytest.mark.parametrize(
    "code_attr",
    [
        CODE_WEBHOOK_ATTESTATION_MISSING,
        CODE_WEBHOOK_ATTESTATION_FORGED,
        CODE_WEBHOOK_ATTESTATION_STALE,
        CODE_WEBHOOK_ATTESTATION_REVISION_MISMATCH,
        CODE_WEBHOOK_ATTESTATION_TENANT_MISMATCH,
        CODE_WEBHOOK_ATTESTATION_ROUTE_UNOBSERVED,
        CODE_DB_WA_BINDING_MISMATCH,
    ],
)
def test_failure_codes_are_stable_strings(code_attr: str) -> None:
    assert isinstance(code_attr, str)
    assert code_attr

"""Regression tests for Meta acceptance channel evidence contract."""
from __future__ import annotations

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
    META_DIRECT_WEBHOOK_ROUTE,
    build_webhook_attestation_artifact,
    evaluate_actual_provider_channel_ready,
    evaluate_meta_config_present,
    sign_webhook_attestation_artifact,
    webhook_attestation_gaps,
    whatsapp_connection_binding_gaps,
)
from scripts.operators.staging_acceptance_config_consolidation_contract import (  # noqa: E402
    D360_LEGACY_DETECTION_KEYS,
    META_DIRECT_WEBHOOK_ROUTE,
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
        rollback_snapshot_ref="rollback-ref-001",
        hmac_key=_HMAC_KEY,
        issued_at_utc=_NOW.isoformat(),
        expires_at_utc=(_NOW + timedelta(hours=1)).isoformat(),
    )
    artifact.update(overrides)
    if "signature" not in overrides:
        artifact["signature"] = sign_webhook_attestation_artifact(artifact, hmac_key=_HMAC_KEY)
    return artifact


def _db_row() -> dict[str, object]:
    return {
        "tenant_id": _TENANT_ID,
        "provider": "meta",
        "status": "connected",
        "sending_enabled": True,
        "phone_number_id": _PHONE,
        "whatsapp_business_account_id": _WABA,
    }


def test_meta_config_present_without_attestation_is_not_channel_ready() -> None:
    config = evaluate_meta_config_present(_META_VARS)
    assert config["meta_config_present"] is True
    evidence = evaluate_actual_provider_channel_ready(
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
    assert evidence["actual_provider_channel_ready"] is False
    assert "webhook_attestation_artifact" in evidence["webhook_attestation_gaps"]


def test_hardcoded_expected_route_does_not_auto_pass_without_attestation() -> None:
    gaps = webhook_attestation_gaps(
        None,
        tenant_id=_TENANT_ID,
        backend_url=_BACKEND_URL,
        pinned_revision=_PINNED,
        deployment_id=_DEPLOYMENT,
        hmac_key=_HMAC_KEY,
        now=_NOW,
    )
    assert gaps == ["webhook_attestation_artifact"]


def test_complete_meta_evidence_passes() -> None:
    artifact = _valid_artifact()
    evidence = evaluate_actual_provider_channel_ready(
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
    assert evidence["actual_provider_channel_ready"] is True
    assert evidence["observed_callback_route"] == META_DIRECT_WEBHOOK_ROUTE


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


def test_unobserved_route_fails_even_when_constant_supplied_in_artifact() -> None:
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


def test_missing_db_binding_fails_for_tenant_1() -> None:
    artifact = _valid_artifact()
    gaps = whatsapp_connection_binding_gaps(
        None,
        tenant_id=_TENANT_ID,
        artifact=artifact,
        hmac_key=_HMAC_KEY,
    )
    assert "db_wa_binding.row_missing" in gaps


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

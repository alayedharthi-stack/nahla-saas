"""Structural-guard regressions for real-channel acceptance and egress registry."""
from __future__ import annotations

import json
import sys
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

_REPO = Path(__file__).resolve().parents[2]
if str(_REPO) not in sys.path:
    sys.path.insert(0, str(_REPO))

from scripts.operators import real_channel_conversational_acceptance as operator  # noqa: E402
from scripts.operators.meta_acceptance_channel_evidence_contract import (  # noqa: E402
    ARTIFACT_SCHEMA_VERSION,
    build_unsigned_webhook_attestation_template,
    webhook_attestation_gaps,
)
from scripts.operators.product_availability_preprod_synthetic_signoff_v2 import (  # noqa: E402
    BUNDLE_SCHEMA_VERSION,
    INITIATIVE_ID,
    build_unsigned_signoff_bundle_template,
)
from scripts.operators.real_channel_conversational_acceptance_contract import (  # noqa: E402
    ALLOWLIST_PHONES_ENV,
    CODE_ACCEPTANCE_NOT_ENABLED,
    CODE_AI_TEST_ALLOWLIST_INVALID,
    CODE_APPROVED_EGRESS_CONFIGURATION_MISSING,
    CODE_ARCH001_SIGNOFF_MISSING,
    CODE_CHANNEL_HEALTH_BLOCKED,
    CODE_DATABASE_BINDING_REJECTED,
    CODE_DATABASE_QUERY_FAILED,
    CODE_PHONE_NOT_ALLOWLISTED,
    CODE_STAGING_IDENTITY_REJECTED,
    CODE_STORE_AI_MODE_INVALID,
    CODE_TENANT_SETTINGS_MISSING,
    EVIDENCE_HMAC_KEY_ENV,
    EXECUTION_GATE_REQUIRED_PROOFS,
    MASTER_ENABLE_ENV,
    PHASE_CHANNEL_HEALTH,
    TENANT_1_INTENSIVE,
    TENANT_1_PHONE_ENV,
    evaluate_approved_egress_configuration,
    evaluate_execution_gate_chain,
)
from services.internal_conversational_e2e_contract import EGRESS_DENIAL_KINDS  # noqa: E402

GENERIC_PHONE = "966500123456"
GENERIC_ALLOWLIST = "966500123456,966500654321"


def _staging_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("RAILWAY_PROJECT_NAME", "desirable-growth")
    monkeypatch.setenv("RAILWAY_ENVIRONMENT_NAME", "staging")
    monkeypatch.setenv(
        "DATABASE_URL",
        "postgresql://user:pass@postgres-staging.railway.internal:5432/railway",
    )
    monkeypatch.setenv(MASTER_ENABLE_ENV, "true")
    monkeypatch.setenv(ALLOWLIST_PHONES_ENV, GENERIC_ALLOWLIST)
    monkeypatch.setenv(TENANT_1_PHONE_ENV, GENERIC_PHONE)


def test_config_snapshot_default_off_performs_zero_database_io(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv(MASTER_ENABLE_ENV, raising=False)
    with patch.object(operator, "create_engine") as create_engine_mock:
        result = operator.execute_config_snapshot_preflight(tenant_id=TENANT_1_INTENSIVE)
    assert result["ok"] is False
    assert result["code"] == CODE_ACCEPTANCE_NOT_ENABLED
    assert result["db_io_performed"] is False
    create_engine_mock.assert_not_called()


def test_readiness_preflight_default_off_performs_zero_database_io(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv(MASTER_ENABLE_ENV, raising=False)
    with patch.object(operator, "create_engine") as create_engine_mock:
        result = operator.execute_readiness_preflight(app_root=_REPO)
    assert result["ok"] is False
    create_engine_mock.assert_not_called()
    channel_phase = next(
        phase for phase in result["phases"] if phase.get("phase") == PHASE_CHANNEL_HEALTH
    )
    assert channel_phase["db_io_performed"] is False
    config_phase = next(
        phase for phase in result["phases"] if phase.get("phase") == "config_snapshot"
    )
    assert config_phase["db_io_performed"] is False


def test_readiness_preflight_rejects_bad_staging_identity_without_db(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv(MASTER_ENABLE_ENV, "true")
    monkeypatch.setenv("RAILWAY_PROJECT_NAME", "wrong-project")
    monkeypatch.setenv("RAILWAY_ENVIRONMENT_NAME", "staging")
    monkeypatch.setenv("DATABASE_URL", "postgresql://user:pass@postgres-staging.railway.internal:5432/railway")
    with patch.object(operator, "create_engine") as create_engine_mock:
        result = operator.execute_readiness_preflight(app_root=_REPO)
    create_engine_mock.assert_not_called()
    channel_phase = next(
        phase for phase in result["phases"] if phase.get("phase") == PHASE_CHANNEL_HEALTH
    )
    assert channel_phase["db_io_performed"] is False


def test_config_snapshot_rejects_invalid_staging_identity_without_db(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv(MASTER_ENABLE_ENV, "true")
    monkeypatch.setenv("RAILWAY_PROJECT_NAME", "wrong-project")
    monkeypatch.setenv("RAILWAY_ENVIRONMENT_NAME", "staging")
    with patch.object(operator, "create_engine") as create_engine_mock:
        result = operator.execute_config_snapshot_preflight(tenant_id=TENANT_1_INTENSIVE)
    assert result["ok"] is False
    assert result["code"] == CODE_STAGING_IDENTITY_REJECTED
    assert result["db_io_performed"] is False
    create_engine_mock.assert_not_called()


def test_config_snapshot_fails_on_db_query_error(monkeypatch: pytest.MonkeyPatch) -> None:
    _staging_env(monkeypatch)
    with patch.object(operator, "create_engine", side_effect=RuntimeError("db down")):
        result = operator.execute_config_snapshot_preflight(tenant_id=TENANT_1_INTENSIVE)
    assert result["ok"] is False
    assert result["code"] == CODE_DATABASE_QUERY_FAILED
    assert result["db_io_performed"] is True


def test_config_snapshot_fails_on_missing_tenant_settings(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _staging_env(monkeypatch)
    connection = MagicMock()
    connection.execute.return_value.scalar_one_or_none.return_value = None
    engine = MagicMock()
    engine.connect.return_value.__enter__.return_value = connection
    with patch.object(operator, "create_engine", return_value=engine):
        result = operator.execute_config_snapshot_preflight(tenant_id=TENANT_1_INTENSIVE)
    assert result["ok"] is False
    assert result["code"] == CODE_TENANT_SETTINGS_MISSING


def test_config_snapshot_uses_actual_db_settings_not_template(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _staging_env(monkeypatch)
    monkeypatch.setenv(TENANT_1_PHONE_ENV, GENERIC_PHONE)
    ai_settings = {
        "store_ai_mode": "test",
        "store_ai_enabled": True,
        "ai_test_allowed_numbers": [GENERIC_PHONE, "966500654321"],
    }
    connection = MagicMock()
    connection.execute.return_value.scalar_one_or_none.return_value = ai_settings
    engine = MagicMock()
    engine.connect.return_value.__enter__.return_value = connection
    with patch.object(operator, "create_engine", return_value=engine):
        result = operator.execute_config_snapshot_preflight(tenant_id=TENANT_1_INTENSIVE)
    assert result["ok"] is True
    snapshot = result["snapshot"]
    assert snapshot["store_ai_mode"] == "test"
    assert snapshot["source"] == "tenant_settings_read_only"
    assert snapshot["store_ai_enabled"] is True
    assert snapshot["ai_test_allowed_numbers_hashes"]


@pytest.mark.parametrize(
    ("ai_settings", "expected_code"),
    [
        ({"store_ai_mode": "live", "store_ai_enabled": True, "ai_test_allowed_numbers": [GENERIC_PHONE]}, CODE_STORE_AI_MODE_INVALID),
        ({"store_ai_mode": "test", "store_ai_enabled": True, "ai_test_allowed_numbers": []}, CODE_AI_TEST_ALLOWLIST_INVALID),
        ({"store_ai_mode": "test", "store_ai_enabled": True, "ai_test_allowed_numbers": ["966500999999"]}, CODE_PHONE_NOT_ALLOWLISTED),
    ],
)
def test_config_snapshot_fail_closed_modes(
    monkeypatch: pytest.MonkeyPatch,
    ai_settings: dict,
    expected_code: str,
) -> None:
    _staging_env(monkeypatch)
    connection = MagicMock()
    connection.execute.return_value.scalar_one_or_none.return_value = ai_settings
    engine = MagicMock()
    engine.connect.return_value.__enter__.return_value = connection
    with patch.object(operator, "create_engine", return_value=engine):
        result = operator.execute_config_snapshot_preflight(tenant_id=TENANT_1_INTENSIVE)
    assert result["ok"] is False
    assert result["code"] == expected_code


def test_preflight_performs_no_whatsapp_send(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv(MASTER_ENABLE_ENV, raising=False)
    with patch(
        "services.whatsapp_platform.service.provider_send_message",
        side_effect=AssertionError("preflight must not send"),
    ):
        result = operator.execute_readiness_preflight(app_root=_REPO)
    assert result["ok"] is False
    config_phase = next(
        phase for phase in result["phases"] if phase.get("phase") == "config_snapshot"
    )
    assert config_phase["db_io_performed"] is False


def test_shipping_in_egress_denial_kind_registry() -> None:
    assert "shipping" in EGRESS_DENIAL_KINDS


def test_unsigned_arch001_bundle_template_has_no_fake_signature() -> None:
    template = build_unsigned_signoff_bundle_template()
    assert template["bundle_schema_version"] == BUNDLE_SCHEMA_VERSION
    assert template["initiative_id"] == INITIATIVE_ID
    assert template["signature"] is None
    assert template["signed_at_utc"] in (None, "")
    assert template["human_signoff_required"] is True
    assert template["eligible_for_signoff"] is False
    assert "hmac-sha256:" not in json.dumps(template)


def test_emit_unsigned_template_cli_has_no_signing_timestamp() -> None:
    import subprocess

    proc = subprocess.run(
        [
            sys.executable,
            "-m",
            "scripts.operators.product_availability_preprod_synthetic_signoff_v2",
            "emit-unsigned-template",
        ],
        cwd=_REPO,
        capture_output=True,
        text=True,
        check=False,
    )
    assert proc.returncode == 0, proc.stderr
    payload = json.loads(proc.stdout.strip())
    assert payload.get("signature") is None
    assert payload.get("signed_at_utc") in (None, "")


def test_unsigned_webhook_attestation_template_has_no_approval_booleans() -> None:
    template = build_unsigned_webhook_attestation_template()
    assert template["artifact_schema_version"] == ARTIFACT_SCHEMA_VERSION
    assert template["signature"] is None
    assert template["human_signoff_required"] is True
    assert template["tenant_id"] is None
    assert template["observation_source"] == ""
    assert "forbidden_unlocks_respected" not in template
    rollback = template["rollback_snapshot_evidence"]
    assert "forbidden_unlocks_respected" not in rollback
    assert "rollback_required" not in rollback


def test_webhook_verifier_requires_explicit_forbidden_unlocks_respected() -> None:
    template = build_unsigned_webhook_attestation_template()
    gaps = webhook_attestation_gaps(
        template,
        tenant_id=1,
        backend_url="https://staging.example.com",
        pinned_revision="abc1234567890",
        deployment_id="deploy-001",
        hmac_key="unit-test-meta-acceptance-channel-evidence-key-32b",
    )
    assert "webhook_attestation.forbidden_unlocks_respected" in gaps


def test_approved_egress_configuration_intentional_permanent_fail_closed() -> None:
    result = evaluate_approved_egress_configuration()
    assert result["ok"] is False
    assert result["code"] == CODE_APPROVED_EGRESS_CONFIGURATION_MISSING
    assert result["policy"] == "permanent_fail_closed"
    assert result["intentional_block"] is True
    assert result["internal_acceptance_context_installable"] is False
    assert "shipping" in result["reused_egress_guard_kinds"]


def test_public_exports_restore_evidence_hmac_key_env() -> None:
    from scripts.operators import real_channel_conversational_acceptance_contract as contract

    assert "EVIDENCE_HMAC_KEY_ENV" in contract.__all__
    assert hasattr(contract, "EVIDENCE_HMAC_KEY_ENV")
    assert contract.EVIDENCE_HMAC_KEY_ENV == EVIDENCE_HMAC_KEY_ENV


@pytest.mark.parametrize(
    "proof_to_break",
    list(EXECUTION_GATE_REQUIRED_PROOFS),
)
def test_execution_gate_chain_fails_when_mandatory_proof_absent(
    monkeypatch: pytest.MonkeyPatch,
    proof_to_break: str,
) -> None:
    env = {
        "RAILWAY_PROJECT_NAME": "desirable-growth",
        "RAILWAY_ENVIRONMENT_NAME": "staging",
        "DATABASE_URL": "postgresql://user:pass@postgres-staging.railway.internal:5432/railway",
        ALLOWLIST_PHONES_ENV: GENERIC_ALLOWLIST,
        TENANT_1_PHONE_ENV: GENERIC_PHONE,
    }
    arch001_ok = True
    channel_ok = True

    if proof_to_break == "staging_identity":
        env["RAILWAY_PROJECT_NAME"] = "wrong"
    elif proof_to_break == "database_binding":
        env["DATABASE_URL"] = "postgresql://user:pass@prod.example:5432/railway"
    elif proof_to_break == "arch001_signoff":
        arch001_ok = False
    elif proof_to_break == "operator_channel_health":
        channel_ok = False
    elif proof_to_break == "allowlisted_test_number":
        env[TENANT_1_PHONE_ENV] = ""
    elif proof_to_break == "approved_egress_configuration":
        pass  # permanent intentional fail-closed by policy

    result = evaluate_execution_gate_chain(
        tenant_id=TENANT_1_INTENSIVE,
        env=env,
        arch001_signoff_ok=arch001_ok,
        channel_health_ok=channel_ok,
    )
    assert result["ok"] is False
    assert result["proofs"][proof_to_break] is False


def test_execution_gate_chain_separates_arch001_and_channel_health() -> None:
    env = {
        "RAILWAY_PROJECT_NAME": "desirable-growth",
        "RAILWAY_ENVIRONMENT_NAME": "staging",
        "DATABASE_URL": "postgresql://user:pass@postgres-staging.railway.internal:5432/railway",
        ALLOWLIST_PHONES_ENV: GENERIC_ALLOWLIST,
        TENANT_1_PHONE_ENV: GENERIC_PHONE,
    }
    arch001_only = evaluate_execution_gate_chain(
        tenant_id=TENANT_1_INTENSIVE,
        env=env,
        arch001_signoff_ok=True,
        channel_health_ok=False,
    )
    assert arch001_only["proofs"]["arch001_signoff"] is True
    assert arch001_only["proofs"]["operator_channel_health"] is False
    assert CODE_CHANNEL_HEALTH_BLOCKED in arch001_only["blockers"]

    channel_only = evaluate_execution_gate_chain(
        tenant_id=TENANT_1_INTENSIVE,
        env=env,
        arch001_signoff_ok=False,
        channel_health_ok=True,
    )
    assert channel_only["proofs"]["arch001_signoff"] is False
    assert channel_only["proofs"]["operator_channel_health"] is True
    assert CODE_ARCH001_SIGNOFF_MISSING in channel_only["blockers"]

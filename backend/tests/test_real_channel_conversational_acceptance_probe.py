"""Regression tests for real-channel conversational acceptance operator (default-off)."""
from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import pytest

_REPO = Path(__file__).resolve().parents[2]
if str(_REPO) not in sys.path:
    sys.path.insert(0, str(_REPO))

from scripts.operators import (  # noqa: E402
    real_channel_conversational_acceptance as operator,
)
from backend.tests._arch001_signoff_v2_fixture import install_production_v2_artifact
from scripts.operators.real_channel_conversational_acceptance_contract import (  # noqa: E402
    ARCH001_SHADOW_SIGNOFF_ENV,
    CODE_ACCEPTANCE_NOT_ENABLED,
    CODE_ARCH001_SIGNOFF_MISSING,
    CODE_CHANNEL_D360_ONLY_LEGACY_PATH,
    CODE_CHANNEL_READINESS_GAP,
    CODE_MANIFEST_INVALID,
    CODE_TENANT_NOT_ALLOWED,
    D360_LEGACY_OBSERVABILITY_ENV_NAMES,
    EXECUTION_CONFIRM_ENV,
    EXECUTION_PATH_REAL_CHANNEL_WEBHOOK,
    MANIFEST_SCHEMA_VERSION,
    MASTER_ENABLE_ENV,
    META_READINESS_REQUIRED_ENV_NAMES,
    PHASE_DEFAULT_OFF,
    PHASE_TENANT_1_INTENSIVE,
    PHASE_TENANT_33_LIMITED,
    PHASE_TENANT_48_SALLA_MINIMAL,
    REPORT_SCHEMA_VERSION,
    SCENARIO_TAXONOMY,
    TENANT_1_PASS_CONFIRM_ENV,
    TENANT_48_SALLA_MINIMAL,
    evaluate_meta_channel_readiness,
    evaluate_meta_channel_readiness,
    load_scenario_manifest,
    resolve_acceptance_phase,
    validate_manifest,
)
from scripts.operators.meta_acceptance_channel_evidence_contract import (  # noqa: E402
    CODE_DB_WA_BINDING_INVALID,
    CODE_DB_WA_BINDING_MISSING,
    CODE_WEBHOOK_ATTESTATION_FORGED,
    CODE_WEBHOOK_ATTESTATION_MISSING,
    CODE_WEBHOOK_ATTESTATION_REVISION_MISMATCH,
    WEBHOOK_ATTESTATION_HMAC_KEY_ENV,
    build_webhook_attestation_artifact,
)
from scripts.operators.real_channel_acceptance_manifest_builder import (  # noqa: E402
    build_manifest,
)
from scripts.operators.staging_acceptance_config_consolidation_contract import (  # noqa: E402
    D360_LEGACY_DETECTION_KEYS,
    META_DIRECT_WEBHOOK_ROUTE,
)


def test_default_off_probe_contract(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv(MASTER_ENABLE_ENV, raising=False)
    result = operator.execute_default_off_probe()
    assert result["phase"] == PHASE_DEFAULT_OFF
    assert result["report_schema_version"] == REPORT_SCHEMA_VERSION
    assert result["ok"] is True
    assert result["acceptance_enabled"] is False


def test_manifest_covers_closed_taxonomy() -> None:
    manifest = load_scenario_manifest(_REPO)
    assert manifest["manifest_schema_version"] == MANIFEST_SCHEMA_VERSION
    taxonomies = {row["taxonomy"] for row in manifest["scenarios"]}
    assert taxonomies == set(SCENARIO_TAXONOMY)


def test_manifest_scenario_counts() -> None:
    manifest = load_scenario_manifest(_REPO)
    t1 = [s for s in manifest["scenarios"] if s["phase"] == PHASE_TENANT_1_INTENSIVE]
    t33 = [s for s in manifest["scenarios"] if s["phase"] == PHASE_TENANT_33_LIMITED]
    t48 = [s for s in manifest["scenarios"] if s["phase"] == PHASE_TENANT_48_SALLA_MINIMAL]
    assert len(t1) == 50
    assert len(t33) == 16
    assert len(t48) == 16
    assert manifest["scenario_count"] == 82
    assert manifest["phase_scenario_counts"] == {
        PHASE_TENANT_1_INTENSIVE: 50,
        PHASE_TENANT_33_LIMITED: 16,
        PHASE_TENANT_48_SALLA_MINIMAL: 16,
    }


def test_tenant_48_phase_independent_of_tenant_1_pass() -> None:
    manifest = load_scenario_manifest(_REPO)
    phase_row = next(
        row for row in manifest["phases"] if row["phase"] == PHASE_TENANT_48_SALLA_MINIMAL
    )
    assert phase_row["tenant_id"] == TENANT_48_SALLA_MINIMAL
    assert phase_row["requires_tenant_1_pass"] is False
    assert phase_row["independent_of_tenant_1_pass_artifact"] is True
    for row in manifest["scenarios"]:
        if row["phase"] == PHASE_TENANT_48_SALLA_MINIMAL:
            assert row["tenant_id"] == TENANT_48_SALLA_MINIMAL
            assert row["preconditions"]["tenant_1_pass_required"] is False
            assert row["scenario_id"].startswith("t48_")


def test_resolve_acceptance_phase_rejects_arbitrary_tenant() -> None:
    with pytest.raises(ValueError, match=CODE_TENANT_NOT_ALLOWED):
        resolve_acceptance_phase(7)


def test_manifest_rejects_arbitrary_tenant_id() -> None:
    manifest = build_manifest()
    manifest["scenarios"] = list(manifest["scenarios"])
    manifest["scenarios"][0] = dict(manifest["scenarios"][0])
    manifest["scenarios"][0]["tenant_id"] = 99
    with pytest.raises(ValueError, match=CODE_MANIFEST_INVALID):
        validate_manifest(manifest)


def test_tenant_48_session_start_still_default_off(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from scripts.operators.real_channel_acceptance_session import start_session

    monkeypatch.delenv(MASTER_ENABLE_ENV, raising=False)
    result = start_session(tenant_id=TENANT_48_SALLA_MINIMAL, app_root=_REPO)
    assert result["ok"] is False
    assert CODE_ACCEPTANCE_NOT_ENABLED in result["blockers"]


def test_tenant_33_requires_tenant_1_pass_in_preconditions() -> None:
    manifest = load_scenario_manifest(_REPO)
    for row in manifest["scenarios"]:
        if row["phase"] == PHASE_TENANT_33_LIMITED:
            assert row["preconditions"]["store_ai_mode"] == "test"
            assert row["preconditions"]["store_ai_enabled"] is True
            assert row["preconditions"]["arch001_shadow_signoff"] is True
            assert row["preconditions"]["tenant_1_pass_required"] is True
            assert row["preconditions"]["real_catalog_data"] is True
            assert (
                row["preconditions"]["phone_env_ref"]
                == "NAHLA_REAL_CHANNEL_ACCEPTANCE_TENANT_33_PHONE"
            )


def test_tenant_1_generated_preconditions_remain_unchanged() -> None:
    manifest = build_manifest()
    tenant_1_rows = [
        row for row in manifest["scenarios"] if row["phase"] == PHASE_TENANT_1_INTENSIVE
    ]
    assert len(tenant_1_rows) == 50
    base_preconditions = {
        "store_ai_mode": "test",
        "store_ai_enabled": True,
        "store_label": "متجر تجريبي عام",
        "phone_env_ref": "NAHLA_REAL_CHANNEL_ACCEPTANCE_TENANT_1_PHONE",
        "arch001_shadow_signoff": True,
    }
    for row in tenant_1_rows:
        if row["scenario_id"] == "t1_catalog_dress_ambiguous":
            assert row["preconditions"] == {
                **base_preconditions,
                "trusted_catalog_fixture": {
                    "scope": "tenant_1_private_test_store_only",
                    "query_subject": "فستان",
                    "multiple_exact_title_matches": True,
                    "candidate_count_min": 2,
                },
            }
            continue
        assert row["preconditions"] == base_preconditions


def test_tenant_48_evidence_backed_scenario_contracts() -> None:
    manifest = build_manifest()
    tenant_48 = {
        row["scenario_id"]: row
        for row in manifest["scenarios"]
        if row["phase"] == PHASE_TENANT_48_SALLA_MINIMAL
    }

    saved_address = tenant_48["t48_saved_address_fail_closed"]
    assert saved_address["expected_state"] == {
        "persisted_address_lookup": True,
        "reuse_only_if_verified_persisted_address_exists": True,
        "clarify_or_collect_if_address_missing": True,
    }
    assert "previous_address_claim_without_db_state" in saved_address["prohibited_claims"]

    tracking = tenant_48["t48_tracking_existing"]
    fixture = tracking["preconditions"]["synthetic_acceptance_order_fixture"]
    assert fixture["deterministic_reference"] == "RRRD1234"
    assert fixture["scope"] == "tenant_48_private_test_tenant_only"
    assert fixture["cleanup_required"] is True
    assert "remove_tenant_48_synthetic_acceptance_order_fixture" in tracking["cleanup"]

    no_reference = tenant_48["t48_delivery_no_reference"]
    assert "synthetic_acceptance_order_fixture" not in no_reference["preconditions"]
    assert "delivery_date_without_evidence" in no_reference["prohibited_claims"]

    handoff = tenant_48["t48_handoff"]
    assert handoff["expected_state"] == {
        "staff_handoff_evidence_required": True,
        "ai_continuity": True,
        "explicit_pause_state_respected": True,
        "resume_only_after_state_transition": True,
    }

    timeout = tenant_48["t48_tool_timeout"]
    fault = timeout["preconditions"]["controlled_staging_fault_injection"]
    assert fault["scope"] == "tenant_48_tool_timeout_only"
    assert fault["cleanup_restore_required"] is True
    assert fault["production_fault_injector_added_by_this_pr"] is False
    assert "success_claim_after_tool_timeout" in timeout["prohibited_claims"]
    assert "disable_controlled_staging_fault_injection" in timeout["cleanup"]


def test_all_scenarios_require_real_channel_path() -> None:
    manifest = load_scenario_manifest(_REPO)
    for row in manifest["scenarios"]:
        assert row["execution_path"] == EXECUTION_PATH_REAL_CHANNEL_WEBHOOK


def test_scenario_preconditions_require_test_mode() -> None:
    manifest = build_manifest()
    validate_manifest(manifest)
    for row in manifest["scenarios"]:
        assert row["preconditions"]["store_ai_mode"] == "test"


def test_execution_blocked_without_gates(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv(MASTER_ENABLE_ENV, raising=False)
    result = operator.execute_scenario_plan(
        phase=PHASE_TENANT_1_INTENSIVE, app_root=_REPO, dry_run=False
    )
    assert result["ok"] is False
    assert result["code"] == "real_channel_required"


def test_tenant_33_blocked_without_tenant_1_pass(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setenv(MASTER_ENABLE_ENV, "true")
    monkeypatch.setenv(EXECUTION_CONFIRM_ENV, "true")
    install_production_v2_artifact(monkeypatch, tmp_path)
    monkeypatch.delenv(TENANT_1_PASS_CONFIRM_ENV, raising=False)
    result = operator.execute_scenario_plan(
        phase=PHASE_TENANT_33_LIMITED, app_root=_REPO, dry_run=False
    )
    assert result["ok"] is False
    assert result["code"] == "real_channel_required"


def test_arch001_signoff_gate_default_off(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("NAHLA_ARCH001_PREPROD_SYNTHETIC_SIGNOFF_V2_ARTIFACT", raising=False)
    monkeypatch.delenv("NAHLA_ARCH001_PREPROD_SYNTHETIC_SIGNOFF_V2_HMAC_KEY", raising=False)
    result = operator.gate_arch001_shadow_signoff()
    assert result["ok"] is False
    assert result["code"] in {CODE_ARCH001_SIGNOFF_MISSING, "bundle_invalid"}


def test_defect_bundle_template_writes_sanitized_file(tmp_path: Path) -> None:
    bundle_root = tmp_path / "nahla"
    (bundle_root / "backend").mkdir(parents=True)
    result = operator.build_defect_bundle(
        scenario_id="t1_faq_hours",
        failure_class="prohibited_operational_claim",
        correlation_id="corr-test-1234",
        sanitized_evidence={"compose_source": "llm", "phone_hash": "sha256:abc"},
        app_root=bundle_root,
    )
    assert result["ok"] is True
    bundle_rel = result["bundle_path"]
    bundle = json.loads((bundle_root / bundle_rel).read_text(encoding="utf-8"))
    assert bundle["classification"]["auto_merge_fixes"] is False
    assert "966" not in json.dumps(bundle)


def test_cli_default_off_exit_zero() -> None:
    proc = subprocess.run(
        [sys.executable, "-m", "scripts.operators.real_channel_conversational_acceptance", "default-off"],
        cwd=_REPO,
        capture_output=True,
        text=True,
        check=False,
    )
    assert proc.returncode == 0
    payload = json.loads(proc.stdout.strip())
    assert payload["ok"] is True


def test_tenant_1_catalog_dress_ambiguous_acceptance_contract() -> None:
    manifest = load_scenario_manifest(_REPO)
    scenario = next(
        row
        for row in manifest["scenarios"]
        if row["scenario_id"] == "t1_catalog_dress_ambiguous"
    )
    assert scenario["phase"] == PHASE_TENANT_1_INTENSIVE
    assert scenario["tenant_id"] == 1
    assert scenario["taxonomy"] == "catalog_search_availability"
    assert scenario["inbound"] == {
        "channel": "whatsapp",
        "type": "text",
        "body": "السلام عليكم، كم سعر الفستان وهل هو متوفر؟",
    }
    assert scenario["preconditions"] == {
        "store_ai_mode": "test",
        "store_ai_enabled": True,
        "store_label": "متجر تجريبي عام",
        "phone_env_ref": "NAHLA_REAL_CHANNEL_ACCEPTANCE_TENANT_1_PHONE",
        "arch001_shadow_signoff": True,
        "trusted_catalog_fixture": {
            "scope": "tenant_1_private_test_store_only",
            "query_subject": "فستان",
            "multiple_exact_title_matches": True,
            "candidate_count_min": 2,
        },
    }
    assert scenario["expected_state"] == {
        "catalog_lookup": True,
        "catalog_ambiguity_detected": True,
        "multiple_exact_title_matches": True,
        "llm_owned_clarification": True,
        "order_created": False,
        "compose_provenance_consistent": True,
    }
    assert "single_price_without_disambiguation" in scenario["prohibited_claims"]
    assert "generalized_availability_without_disambiguation" in scenario["prohibited_claims"]
    assert "fallback_deterministic_without_compose_failure" in scenario["prohibited_claims"]
    assert "foreign_catalog_leak" in scenario["prohibited_claims"]
    assert "cross_tenant_data_leak" in scenario["prohibited_claims"]
    assert scenario["automation_class"] == "hybrid"
    assert scenario["device_action"]["send_type"] == "text"
    assert scenario["channel_evidence_required"]["evidence_channel"] == "actual_provider_channel"
    assert "compose_provenance_metadata" in scenario["outbound_evidence"]


def test_cli_manifest_validate_exit_zero() -> None:
    proc = subprocess.run(
        [sys.executable, "-m", "scripts.operators.real_channel_conversational_acceptance", "manifest-validate"],
        cwd=_REPO,
        capture_output=True,
        text=True,
        check=False,
    )
    assert proc.returncode == 0


def test_public_exports_preserve_baseline_symbols() -> None:
    from scripts.operators import real_channel_conversational_acceptance_contract as contract

    baseline = {
        "DEFECT_BUNDLE_DIR",
        "MASTER_ENABLE_ENV",
        "EVIDENCE_ACCUMULATION_DIR",
        "PINNED_REVISION_ENV",
        "EXECUTION_CONFIRM_ENV",
    }
    for name in baseline:
        assert name in contract.__all__
        assert hasattr(contract, name)


def _channel_health_env(monkeypatch: pytest.MonkeyPatch) -> str:
    hmac_key = "unit-test-meta-acceptance-channel-evidence-key-32b"
    backend_url = "https://staging.example.com"
    pinned = "abc1234567890"
    deployment = "deploy-staging-001"
    monkeypatch.setenv("DATABASE_URL", "postgres://staging")
    monkeypatch.setenv("BACKEND_URL", backend_url)
    monkeypatch.setenv("NAHLA_REAL_CHANNEL_ACCEPTANCE_PINNED_REVISION", pinned)
    monkeypatch.setenv("RAILWAY_DEPLOYMENT_ID", deployment)
    for key in META_READINESS_REQUIRED_ENV_NAMES:
        if key == "BACKEND_URL":
            continue
        monkeypatch.setenv(key, "present")
    monkeypatch.setenv(WEBHOOK_ATTESTATION_HMAC_KEY_ENV, hmac_key)
    return hmac_key


def _valid_attestation(*, hmac_key: str, backend_url: str = "https://staging.example.com") -> dict:
    return build_webhook_attestation_artifact(
        tenant_id=1,
        backend_url=backend_url,
        pinned_revision="abc1234567890",
        deployment_id="deploy-staging-001",
        observed_callback_route=META_DIRECT_WEBHOOK_ROUTE,
        waba_id="waba-tenant-1-example",
        phone_number_id="phone-tenant-1-example",
        hmac_key=hmac_key,
    )


def test_channel_health_blocks_d360_only_legacy_path(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _channel_health_env(monkeypatch)
    for key in D360_LEGACY_DETECTION_KEYS:
        monkeypatch.setenv(key, "legacy-present")
    for key in META_READINESS_REQUIRED_ENV_NAMES:
        if key == "BACKEND_URL":
            continue
        monkeypatch.delenv(key, raising=False)
    result = operator.execute_channel_health_preflight(tenant_id=1)
    assert result["ok"] is False
    assert result["code"] == CODE_CHANNEL_D360_ONLY_LEGACY_PATH
    assert result["operator_attested_channel_ready"] is False


def test_meta_config_present_does_not_unlock_without_attestation(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _channel_health_env(monkeypatch)
    result = operator.execute_channel_health_preflight(tenant_id=1)
    assert result["ok"] is False
    assert result["meta_config_present"] is True
    assert result["operator_attested_channel_ready"] is False
    assert result["code"] == CODE_WEBHOOK_ATTESTATION_MISSING


def test_channel_health_passes_with_complete_meta_evidence(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    hmac_key = _channel_health_env(monkeypatch)
    artifact = _valid_attestation(hmac_key=hmac_key)
    db_row = {
        "tenant_id": 1,
        "provider": "meta",
        "status": "connected",
        "sending_enabled": True,
        "phone_number_id": "phone-tenant-1-example",
        "whatsapp_business_account_id": "waba-tenant-1-example",
    }
    result = operator.execute_channel_health_preflight(
        tenant_id=1,
        attestation_artifact=artifact,
        db_row=db_row,
    )
    assert result["ok"] is True
    assert result["meta_config_present"] is True
    assert result["operator_attested_channel_ready"] is True
    assert result["observed_callback_route"] == META_DIRECT_WEBHOOK_ROUTE


def test_channel_health_blocks_when_backend_url_missing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("DATABASE_URL", "postgres://staging")
    monkeypatch.delenv("BACKEND_URL", raising=False)
    for key in META_READINESS_REQUIRED_ENV_NAMES:
        if key != "BACKEND_URL":
            monkeypatch.setenv(key, "present")
    result = operator.execute_channel_health_preflight(tenant_id=1)
    assert result["ok"] is False
    assert result["code"] == "channel_health_blocked"
    assert "BACKEND_URL" in result["missing_credentials"]


@pytest.mark.parametrize(
    "missing_key",
    [key for key in META_READINESS_REQUIRED_ENV_NAMES if key != "BACKEND_URL"],
)
def test_channel_health_fails_when_meta_key_missing(
    monkeypatch: pytest.MonkeyPatch,
    missing_key: str,
) -> None:
    hmac_key = _channel_health_env(monkeypatch)
    monkeypatch.delenv(missing_key, raising=False)
    artifact = _valid_attestation(hmac_key=hmac_key)
    result = operator.execute_channel_health_preflight(
        tenant_id=1,
        attestation_artifact=artifact,
        db_row={
            "tenant_id": 1,
            "provider": "meta",
            "status": "connected",
            "sending_enabled": True,
            "phone_number_id": "phone-tenant-1-example",
            "whatsapp_business_account_id": "waba-tenant-1-example",
        },
    )
    assert result["ok"] is False
    assert result["operator_attested_channel_ready"] is False
    assert missing_key in (result.get("channel_evidence_gaps") or [])


def test_channel_health_fails_on_forged_attestation(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    hmac_key = _channel_health_env(monkeypatch)
    artifact = _valid_attestation(hmac_key=hmac_key)
    artifact["signature"] = "forged"
    result = operator.execute_channel_health_preflight(
        tenant_id=1,
        attestation_artifact=artifact,
        db_row={
            "tenant_id": 1,
            "provider": "meta",
            "status": "connected",
            "sending_enabled": True,
            "phone_number_id": "phone-tenant-1-example",
            "whatsapp_business_account_id": "waba-tenant-1-example",
        },
    )
    assert result["ok"] is False
    assert result["code"] == CODE_WEBHOOK_ATTESTATION_FORGED


def test_channel_health_fails_on_wrong_revision_attestation(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    hmac_key = _channel_health_env(monkeypatch)
    artifact = _valid_attestation(hmac_key=hmac_key)
    artifact["pinned_revision"] = "wrong-sha"
    from scripts.operators.meta_acceptance_channel_evidence_contract import (
        sign_webhook_attestation_artifact,
    )

    artifact["signature"] = sign_webhook_attestation_artifact(artifact, hmac_key=hmac_key)
    result = operator.execute_channel_health_preflight(
        tenant_id=1,
        attestation_artifact=artifact,
        db_row={
            "tenant_id": 1,
            "provider": "meta",
            "status": "connected",
            "sending_enabled": True,
            "phone_number_id": "phone-tenant-1-example",
            "whatsapp_business_account_id": "waba-tenant-1-example",
        },
    )
    assert result["ok"] is False
    assert result["code"] == CODE_WEBHOOK_ATTESTATION_REVISION_MISMATCH


def test_channel_health_fails_on_missing_db_binding_for_tenant_1(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    hmac_key = _channel_health_env(monkeypatch)
    artifact = _valid_attestation(hmac_key=hmac_key)
    result = operator.execute_channel_health_preflight(
        tenant_id=1,
        attestation_artifact=artifact,
        db_row=None,
    )
    assert result["ok"] is False
    assert result["code"] == CODE_DB_WA_BINDING_MISSING


def test_channel_health_fails_on_missing_db_binding_for_tenant_33(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    hmac_key = _channel_health_env(monkeypatch)
    artifact = _valid_attestation(hmac_key=hmac_key)
    artifact["tenant_id"] = 33
    from scripts.operators.meta_acceptance_channel_evidence_contract import (
        sign_webhook_attestation_artifact,
    )

    artifact["signature"] = sign_webhook_attestation_artifact(artifact, hmac_key=hmac_key)
    result = operator.execute_channel_health_preflight(
        tenant_id=33,
        attestation_artifact=artifact,
        db_row=None,
    )
    assert result["ok"] is False
    assert result["code"] == CODE_DB_WA_BINDING_MISSING


def test_channel_health_rejects_tenant_33_d360_db_binding(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    hmac_key = _channel_health_env(monkeypatch)
    artifact = _valid_attestation(hmac_key=hmac_key)
    artifact["tenant_id"] = 33
    from scripts.operators.meta_acceptance_channel_evidence_contract import (
        sign_webhook_attestation_artifact,
    )

    artifact["signature"] = sign_webhook_attestation_artifact(artifact, hmac_key=hmac_key)
    result = operator.execute_channel_health_preflight(
        tenant_id=33,
        attestation_artifact=artifact,
        db_row={
            "tenant_id": 33,
            "provider": "360dialog",
            "status": "connected",
            "sending_enabled": True,
            "phone_number_id": "phone-tenant-33-example",
            "whatsapp_business_account_id": "waba-tenant-33-example",
        },
    )
    assert result["ok"] is False
    assert result["code"] == CODE_DB_WA_BINDING_INVALID


def test_channel_health_reports_operator_observed_evidence_class(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    hmac_key = _channel_health_env(monkeypatch)
    artifact = _valid_attestation(hmac_key=hmac_key)
    result = operator.execute_channel_health_preflight(
        tenant_id=1,
        attestation_artifact=artifact,
        db_row={
            "tenant_id": 1,
            "provider": "meta",
            "status": "connected",
            "sending_enabled": True,
            "phone_number_id": "phone-tenant-1-example",
            "whatsapp_business_account_id": "waba-tenant-1-example",
        },
    )
    assert result["channel_evidence_class"] == "operator_observed_meta_webhook"


def test_meta_readiness_contract_passes_without_d360() -> None:
    variables = {key: "present" for key in META_READINESS_REQUIRED_ENV_NAMES}
    result = evaluate_meta_channel_readiness(variables)
    assert result["meta_config_present"] is True
    assert result["d360_legacy_present"] is False

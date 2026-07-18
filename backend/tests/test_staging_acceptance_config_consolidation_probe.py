"""Regression tests for staging acceptance config consolidation operator (default-off)."""
from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import pytest

_REPO = Path(__file__).resolve().parents[2]
if str(_REPO) not in sys.path:
    sys.path.insert(0, str(_REPO))

from scripts.operators import staging_acceptance_config_consolidation as operator  # noqa: E402
from scripts.operators import staging_acceptance_config_consolidation_contract as contract  # noqa: E402
from scripts.operators.staging_acceptance_config_consolidation_contract import (  # noqa: E402
    APPLY_CONFIRM_ENV,
    APPLY_CONFIRM_TOKEN,
    ARCH001_SHADOW_MODE_ENV,
    ARCH001_SHADOW_SIGNOFF_ENV,
    ARCH001_TEARDOWN_PROOF_ENV,
    CANONICAL_SERVICE_ID,
    CANONICAL_SERVICE_NAME,
    CODE_ARCH001_SHADOW_ACTIVE,
    CODE_CONFLICT_DETECTED,
    CODE_CONSOLIDATION_NOT_ENABLED,
    CODE_PRODUCTION_REJECTED,
    CODE_SECRET_LEAKAGE,
    LEGACY_SOURCE_SERVICE_ID,
    LEGACY_SOURCE_SERVICE_NAME,
    PRODUCTION_RAILWAY_ENVIRONMENT_IDS,
    STAGING_RAILWAY_ENVIRONMENT_ID,
    STAGING_RAILWAY_PROJECT_ID,
    MASTER_ENABLE_ENV,
    MIGRATABLE_VARIABLE_KEYS,
    PHASE_DEFAULT_OFF,
    REPORT_SCHEMA_VERSION,
    STAGING_ENVIRONMENT_ENV,
    STAGING_PROJECT_ENV,
    fingerprint_value,
    is_placeholder_or_sentinel_uuid,
    validate_pinned_identity_contract,
    validate_readonly_inventory_identity,
)
from scripts.operators.staging_acceptance_config_consolidation import (  # noqa: E402
    InMemoryRailwayClient,
    RailwayObservation,
    ServiceSnapshot,
    build_apply_patch,
    build_snapshot_blob,
    execute_apply,
    execute_dry_run_plan,
    execute_inventory,
    execute_rollback,
    execute_summary,
    execute_verify_post_apply,
    gate_arch001_shadow_block,
    gate_railway_allowlist,
    gate_staging_identity,
    load_fixture_observation,
    restore_snapshot_blob,
    routing_selection_guidance,
)

_FIXTURE = _REPO / "docs/engineering/staging-evidence/staging-acceptance-config-fixture.json"
_HMAC_KEY = "test-hmac-key-never-commit"


def _observation() -> RailwayObservation:
    return load_fixture_observation(_FIXTURE)


def _railway_inventory_project() -> dict[str, object]:
    """Minimal authenticated `railway list --json` project schema."""
    return {
        "id": STAGING_RAILWAY_PROJECT_ID,
        "name": "desirable-growth",
        "environments": {
            "edges": [
                {
                    "node": {
                        "id": STAGING_RAILWAY_ENVIRONMENT_ID,
                        "name": "staging",
                        "serviceInstances": {
                            "edges": [
                                {"node": {"serviceId": CANONICAL_SERVICE_ID}},
                                {"node": {"serviceId": LEGACY_SOURCE_SERVICE_ID}},
                            ]
                        },
                    }
                },
                {
                    "node": {
                        "id": next(iter(PRODUCTION_RAILWAY_ENVIRONMENT_IDS)),
                        "name": "production",
                        "serviceInstances": {
                            "edges": [{"node": {"serviceId": CANONICAL_SERVICE_ID}}]
                        },
                    }
                },
            ]
        },
        "services": {
            "edges": [
                {
                    "node": {
                        "id": CANONICAL_SERVICE_ID,
                        "name": CANONICAL_SERVICE_NAME,
                    }
                },
                {
                    "node": {
                        "id": LEGACY_SOURCE_SERVICE_ID,
                        "name": LEGACY_SOURCE_SERVICE_NAME,
                    }
                },
            ]
        },
    }


def test_default_off_probe_contract() -> None:
    result = operator.execute_default_off_probe()
    assert result["phase"] == PHASE_DEFAULT_OFF
    assert result["report_schema_version"] == REPORT_SCHEMA_VERSION
    assert result["ok"] is True
    assert result["canonical_service"] == CANONICAL_SERVICE_NAME
    assert result["legacy_source_service"] == LEGACY_SOURCE_SERVICE_NAME


def test_staging_identity_rejects_production(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv(STAGING_PROJECT_ENV, "desirable-growth")
    monkeypatch.setenv(STAGING_ENVIRONMENT_ENV, "production")
    result = gate_staging_identity()
    assert result["ok"] is False
    assert result["code"] in {CODE_PRODUCTION_REJECTED, "staging_identity_rejected"}


def test_staging_identity_accepts_staging(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv(STAGING_PROJECT_ENV, "desirable-growth")
    monkeypatch.setenv(STAGING_ENVIRONMENT_ENV, "staging")
    result = gate_staging_identity()
    assert result["ok"] is True


def test_railway_allowlist_accepts_fixture() -> None:
    obs = _observation()
    result = gate_railway_allowlist(obs)
    assert result["ok"] is True


def test_pinned_ids_are_exact_distinct_non_placeholder_uuids() -> None:
    pinned = {
        STAGING_RAILWAY_PROJECT_ID,
        STAGING_RAILWAY_ENVIRONMENT_ID,
        CANONICAL_SERVICE_ID,
        LEGACY_SOURCE_SERVICE_ID,
    }
    assert len(pinned) == 4
    assert not any(is_placeholder_or_sentinel_uuid(value) for value in pinned)
    assert validate_pinned_identity_contract() is None


@pytest.mark.parametrize(
    "sentinel",
    [
        "00000000-0000-4000-8000-000000000001",
        "11111111-1111-4111-8111-111111111111",
        "ffffffff-ffff-4fff-8fff-ffffffffffff",
        "not-a-uuid",
    ],
)
def test_placeholder_and_sentinel_uuids_are_rejected(sentinel: str) -> None:
    assert is_placeholder_or_sentinel_uuid(sentinel) is True


def test_contract_rejects_future_placeholder_regression(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        contract,
        "CANONICAL_SERVICE_ID",
        "00000000-0000-4000-8000-000000000003",
    )
    assert validate_pinned_identity_contract() == "placeholder_or_sentinel_uuid"


def test_readonly_railway_inventory_schema_matches_pinned_names_and_ids() -> None:
    inventory = _railway_inventory_project()
    assert validate_readonly_inventory_identity(inventory) is None


def test_readonly_inventory_rejects_swapped_service_names() -> None:
    inventory = _railway_inventory_project()
    service_edges = inventory["services"]["edges"]  # type: ignore[index]
    service_edges[0]["node"]["name"] = LEGACY_SOURCE_SERVICE_NAME
    service_edges[1]["node"]["name"] = CANONICAL_SERVICE_NAME
    assert (
        validate_readonly_inventory_identity(inventory)
        == "inventory_canonical_service_name_mismatch"
    )


def test_readonly_inventory_rejects_missing_staging_relationship() -> None:
    inventory = _railway_inventory_project()
    staging_node = inventory["environments"]["edges"][0]["node"]  # type: ignore[index]
    staging_node["serviceInstances"]["edges"] = [
        {"node": {"serviceId": CANONICAL_SERVICE_ID}}
    ]
    assert (
        validate_readonly_inventory_identity(inventory)
        == "inventory_staging_service_relationship_mismatch"
    )


def test_swapped_canonical_and_legacy_service_ids_are_rejected() -> None:
    obs = _observation()
    swapped = RailwayObservation(
        project_id=obs.project_id,
        environment_id=obs.environment_id,
        canonical=ServiceSnapshot(
            service_id=LEGACY_SOURCE_SERVICE_ID,
            service_name=CANONICAL_SERVICE_NAME,
            variables={},
            deployment_id="",
            source_revision="",
            domains=(),
            routes=(),
        ),
        legacy_source=ServiceSnapshot(
            service_id=CANONICAL_SERVICE_ID,
            service_name=LEGACY_SOURCE_SERVICE_NAME,
            variables={},
            deployment_id="",
            source_revision="",
            domains=(),
            routes=(),
        ),
    )
    result = gate_railway_allowlist(swapped)
    assert result["ok"] is False
    assert result["stage"] == "service_id_not_allowlisted"


def test_wrong_staging_environment_id_is_rejected() -> None:
    obs = _observation()
    wrong_environment = RailwayObservation(
        project_id=obs.project_id,
        environment_id="22222222-2222-4222-8222-222222222222",
        canonical=obs.canonical,
        legacy_source=obs.legacy_source,
    )
    result = gate_railway_allowlist(wrong_environment)
    assert result["ok"] is False
    assert result["stage"] == "environment_id_not_allowlisted"


def test_known_production_environment_id_is_always_rejected() -> None:
    obs = _observation()
    production = RailwayObservation(
        project_id=obs.project_id,
        environment_id=next(iter(PRODUCTION_RAILWAY_ENVIRONMENT_IDS)),
        canonical=obs.canonical,
        legacy_source=obs.legacy_source,
    )
    result = gate_railway_allowlist(production)
    assert result["ok"] is False
    assert result["stage"] == "production_environment_id_forbidden"


def test_arch001_shadow_active_blocks() -> None:
    obs = _observation()
    result = gate_arch001_shadow_block(obs)
    assert result["ok"] is False
    assert result["code"] == CODE_ARCH001_SHADOW_ACTIVE


def test_inventory_presence_only_no_secret_values() -> None:
    obs = _observation()
    result = execute_inventory(obs)
    assert result["ok"] is True
    encoded = json.dumps(result)
    assert "fixture-secret-meta-app" not in encoded
    assert "fixture-whatsapp-token-staging" not in encoded
    assert result["canonical_presence"]["META_APP_SECRET"] == "absent"
    assert result["legacy_source_presence"]["META_APP_SECRET"] == "present"


def test_dry_run_plan_detects_copy_candidates() -> None:
    obs = _observation()
    result = execute_dry_run_plan(obs, hmac_key=_HMAC_KEY)
    plan = result["plan"]
    assert "META_APP_SECRET" in plan["copy_from_source"]
    assert "BACKEND_URL" in plan["keep_dest"]


def test_conflict_detection_without_exposing_values() -> None:
    obs = _observation()
    shared_backend = "https://shared-staging.example.com"
    obs.canonical.variables["BACKEND_URL"] = shared_backend
    obs.legacy_source.variables["BACKEND_URL"] = shared_backend
    obs.canonical.variables["META_APP_SECRET"] = "canonical-secret-value"
    obs.legacy_source.variables["META_APP_SECRET"] = "legacy-secret-value"
    result = execute_dry_run_plan(obs, hmac_key=_HMAC_KEY)
    assert result["ok"] is False
    assert result["code"] == CODE_CONFLICT_DETECTED
    assert result["conflict_count"] >= 1
    encoded = json.dumps(result)
    assert "canonical-secret-value" not in encoded
    assert "legacy-secret-value" not in encoded
    assert "hmac-sha256:" in encoded


def test_fingerprint_equality_without_value_leak() -> None:
    fp_a = fingerprint_value("same", key="META_APP_SECRET", hmac_key=_HMAC_KEY)
    fp_b = fingerprint_value("same", key="META_APP_SECRET", hmac_key=_HMAC_KEY)
    fp_c = fingerprint_value("other", key="META_APP_SECRET", hmac_key=_HMAC_KEY)
    assert fp_a == fp_b
    assert fp_a != fp_c


def test_apply_blocked_when_master_disabled(monkeypatch: pytest.MonkeyPatch) -> None:
    obs = _observation()
    obs.canonical.variables[ARCH001_SHADOW_MODE_ENV] = "off"
    client = InMemoryRailwayClient(obs)
    monkeypatch.delenv(MASTER_ENABLE_ENV, raising=False)
    result = execute_apply(obs, hmac_key=_HMAC_KEY, mutator=client)
    assert result["ok"] is False
    assert result["code"] == CODE_CONSOLIDATION_NOT_ENABLED
    assert client.deploy_count == 0


def test_apply_blocked_when_arch001_shadow_active() -> None:
    obs = _observation()
    client = InMemoryRailwayClient(obs)
    result = execute_apply(
        obs,
        hmac_key=_HMAC_KEY,
        mutator=client,
        env={
            MASTER_ENABLE_ENV: "true",
            APPLY_CONFIRM_ENV: APPLY_CONFIRM_TOKEN,
            ARCH001_TEARDOWN_PROOF_ENV: "artifact-ref",
            ARCH001_SHADOW_SIGNOFF_ENV: "true",
        },
    )
    assert result["ok"] is False
    assert result["code"] == CODE_ARCH001_SHADOW_ACTIVE


def test_apply_succeeds_after_shadow_off(monkeypatch: pytest.MonkeyPatch) -> None:
    obs = _observation()
    obs.canonical.variables[ARCH001_SHADOW_MODE_ENV] = "off"
    obs.canonical.variables.pop("BACKEND_URL", None)
    client = InMemoryRailwayClient(obs)
    result = execute_apply(
        obs,
        hmac_key=_HMAC_KEY,
        mutator=client,
        env={
            MASTER_ENABLE_ENV: "true",
            APPLY_CONFIRM_ENV: APPLY_CONFIRM_TOKEN,
            ARCH001_TEARDOWN_PROOF_ENV: "artifact-ref",
            ARCH001_SHADOW_SIGNOFF_ENV: "true",
        },
    )
    assert result["ok"] is True
    assert "META_APP_SECRET" in result["patched_keys"]
    assert client.deploy_count == 1
    encoded = json.dumps(result)
    assert "fixture-secret-meta-app" not in encoded


def test_snapshot_roundtrip_reversible() -> None:
    obs = _observation()
    blob = build_snapshot_blob(obs, snapshot_key="snapshot-test-key")
    restored = restore_snapshot_blob(encrypted_blob=blob["encrypted_blob"], snapshot_key="snapshot-test-key")
    assert restored["services"][CANONICAL_SERVICE_NAME]["variables"][ARCH001_SHADOW_MODE_ENV] == "shadow"


def test_rollback_restores_prior_state() -> None:
    obs = _observation()
    blob = build_snapshot_blob(obs, snapshot_key="rollback-test-key")
    client = InMemoryRailwayClient(obs)
    result = execute_rollback(
        encrypted_blob=blob["encrypted_blob"],
        snapshot_key="rollback-test-key",
        mutator=client,
        env={MASTER_ENABLE_ENV: "true", APPLY_CONFIRM_ENV: APPLY_CONFIRM_TOKEN},
    )
    assert result["ok"] is True
    assert client.deploy_count == 1


def test_verify_post_apply_attestation() -> None:
    result = execute_verify_post_apply(
        pinned_revision="abc1234567890",
        health_ok=True,
        version_ok=True,
        db_ok=True,
        webhook_route_ok=True,
        tenant_routing_ok=True,
        signature_mode_ok=True,
        accidental_flags=[],
        target_app_root=_REPO,
    )
    assert result["checks"]["health"] is True
    assert result["checks"]["no_accidental_flags"] is True


def test_routing_selection_no_auto_delete() -> None:
    result = routing_selection_guidance()
    assert result["auto_delete_services"] is False
    assert result["auto_delete_domains"] is False
    assert result["canonical_public_app"] == CANONICAL_SERVICE_NAME


def test_summary_reports_block_during_arch001() -> None:
    result = execute_summary(_observation(), hmac_key=_HMAC_KEY)
    assert result["execution_status"] in {
        "BLOCK_CREDENTIAL_GAP",
        "READY_AFTER_ARCH001",
        "BLOCK",
        "BLOCK_CONFLICT",
    }


def test_build_apply_patch_never_includes_protected_keys() -> None:
    obs = _observation()
    obs.canonical.variables[ARCH001_SHADOW_MODE_ENV] = "off"
    obs.canonical.variables.pop("BACKEND_URL", None)
    patch, conflicts = build_apply_patch(obs, hmac_key=_HMAC_KEY)
    assert not conflicts
    assert ARCH001_SHADOW_MODE_ENV not in patch
    for key in patch:
        assert key in MIGRATABLE_VARIABLE_KEYS or key in ("DATABASE_URL", "REDIS_URL")


def test_production_service_id_rejected() -> None:
    obs = _observation()
    bad = RailwayObservation(
        project_id=obs.project_id,
        environment_id=obs.environment_id,
        canonical=ServiceSnapshot(
            service_id="prod-service-id",
            service_name=CANONICAL_SERVICE_NAME,
            variables={},
            deployment_id="",
            source_revision="",
            domains=(),
            routes=(),
        ),
        legacy_source=obs.legacy_source,
    )
    result = gate_railway_allowlist(bad)
    assert result["ok"] is False


def test_cli_default_off_exit_zero() -> None:
    proc = subprocess.run(
        [sys.executable, "-m", "scripts.operators.staging_acceptance_config_consolidation", "default-off"],
        cwd=_REPO,
        capture_output=True,
        text=True,
        check=False,
    )
    assert proc.returncode == 0
    payload = json.loads(proc.stdout.strip())
    assert payload["ok"] is True


def test_cli_inventory_fixture_no_secrets() -> None:
    proc = subprocess.run(
        [
            sys.executable,
            "-m",
            "scripts.operators.staging_acceptance_config_consolidation",
            "inventory",
            "--fixture",
            str(_FIXTURE),
        ],
        cwd=_REPO,
        capture_output=True,
        text=True,
        check=False,
        env={**dict(**{k: v for k, v in []}), "NAHLA_STAGING_ACCEPTANCE_CONFIG_SNAPSHOT_KEY": _HMAC_KEY},
    )
    assert proc.returncode == 0
    assert "fixture-secret" not in proc.stdout


def test_sanitize_report_rejects_value_like_payload() -> None:
    from scripts.operators.staging_acceptance_config_consolidation_contract import sanitize_report_payload

    with pytest.raises(ValueError, match=CODE_SECRET_LEAKAGE):
        sanitize_report_payload({"note": "leaked WHATSAPP_TOKEN value"})

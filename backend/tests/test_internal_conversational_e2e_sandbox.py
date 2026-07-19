"""Focused regressions for the disposable conversational E2E harness."""
from __future__ import annotations

import asyncio
import json
import subprocess
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from core.acceptance_execution_context import (
    InternalE2EEgressDenied,
    current_acceptance_context,
    deny_external_egress,
)
from services.internal_conversational_e2e_contract import (
    ATTESTATION_JSON_ENV,
    ATTESTATION_HMAC_KEY_ENV,
    ATTESTATION_SIGNATURE_ENV,
    CODE_ATTESTATION_MISSING,
    CODE_CANONICAL_DATABASE_REJECTED,
    CODE_DEFAULT_OFF,
    CODE_EVIDENCE_KEY_MISSING,
    CODE_EXECUTION_NOT_CONFIRMED,
    CODE_LLM_DEFAULT_OFF,
    CODE_LLM_HOST_ATTESTATION_INVALID,
    CODE_PHONE_NOT_ALLOWLISTED,
    CODE_STORE_AI_MODE_INVALID,
    CODE_TENANT_1_DENIED,
    CODE_TENANT_NOT_ALLOWED,
    CODE_TENANT_REQUIRED,
    CODE_TENANT_MISMATCH,
    CODE_TENANT_ROLE_REJECTED,
    CONTRACT_VERSION,
    EVIDENCE_CHANNEL,
    EVIDENCE_HMAC_KEY_ENV,
    EXECUTION_CONFIRM_ENV,
    LLM_ENABLE_ENV,
    LLM_HOST_ALLOWLIST_ENV,
    MASTER_ENABLE_ENV,
    NETWORK_FIREWALL_CONFIRM_ENV,
    PHONE_ALLOWLIST_ENV,
    PINNED_REVISION_ENV,
    TENANT_ALLOWLIST_ENV,
    TEST_PHONE_ENV,
    database_identity_fingerprint,
    evaluate_preflight,
    sign_session_evidence,
    sign_attestation,
    verify_session_evidence,
)
from services.internal_conversational_e2e_harness import (
    SandboxTurnRequest,
    _provenance_blockers,
    run_sandbox_turn,
)


TENANT_ID = 48
SESSION_ID = "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa"
REVISION = "813d27825a32f0ab931a80ed3c854bafc67a9e49"
PHONE = "966500000048"
KEY = "test-evidence-key-not-a-production-secret"
ATTESTATION_KEY = "test-attestation-key-not-an-evidence-key"


def _identity(name: str = "railway") -> dict[str, str]:
    return {
        "database_name": name,
        "server_address": "127.0.0.1",
        "server_port": "5432",
    }


def _valid_inputs() -> tuple[dict[str, str], dict[str, str], list[dict]]:
    identity = _identity()
    attestation_id = "bbbbbbbb-bbbb-4bbb-8bbb-bbbbbbbbbbbb"
    payload = {
        "contract_version": CONTRACT_VERSION,
        "attestation_id": attestation_id,
        "disposable_database": True,
        "database_identity_fingerprint": database_identity_fingerprint(identity),
        "canonical_database_identity_fingerprint": "sha256:" + ("c" * 64),
        "runtime_revision": REVISION,
        "network_policy": "default_deny",
        "allowed_hosts": ["api.generic-llm.test"],
        "expires_at_utc": (
            datetime.now(timezone.utc) + timedelta(hours=1)
        ).isoformat(),
    }
    env = {
        MASTER_ENABLE_ENV: "true",
        EXECUTION_CONFIRM_ENV: "true",
        TENANT_ALLOWLIST_ENV: str(TENANT_ID),
        TEST_PHONE_ENV: PHONE,
        PHONE_ALLOWLIST_ENV: PHONE,
        PINNED_REVISION_ENV: REVISION,
        EVIDENCE_HMAC_KEY_ENV: KEY,
        ATTESTATION_HMAC_KEY_ENV: ATTESTATION_KEY,
        ATTESTATION_JSON_ENV: json.dumps(payload),
        ATTESTATION_SIGNATURE_ENV: sign_attestation(payload, key=ATTESTATION_KEY),
        NETWORK_FIREWALL_CONFIRM_ENV: attestation_id,
        LLM_ENABLE_ENV: "true",
        LLM_HOST_ALLOWLIST_ENV: "api.generic-llm.test",
    }
    rows = [
        {
            "id": TENANT_ID,
            "is_platform_tenant": False,
            "user_roles": ["merchant"],
            "ai_settings": {
                "store_ai_mode": "test",
                "store_ai_enabled": True,
                "ai_test_allowed_numbers": [PHONE],
            },
        }
    ]
    return env, identity, rows


def _preflight(
    env: dict[str, str],
    identity: dict[str, str],
    rows: list[dict],
    *,
    tenant_id=TENANT_ID,
) -> dict:
    return evaluate_preflight(
        env=env,
        tenant_id=tenant_id,
        identity=identity,
        tenant_rows=rows,
        attested_revision=REVISION,
    )


def test_signed_disposable_railway_database_with_distinct_canonical_passes() -> None:
    env, identity, rows = _valid_inputs()
    result = _preflight(env, identity, rows)
    assert result["ok"] is True
    assert result["evidence_channel"] == EVIDENCE_CHANNEL
    assert result["database_identity_fingerprint"].startswith("sha256:")


def test_default_off_missing_confirmation_and_attestation_block() -> None:
    _, identity, rows = _valid_inputs()
    result = _preflight({}, identity, rows)
    assert result["ok"] is False
    assert CODE_DEFAULT_OFF in result["blockers"]
    assert CODE_EXECUTION_NOT_CONFIRMED in result["blockers"]
    assert CODE_ATTESTATION_MISSING in result["blockers"]


def test_attestation_and_evidence_keys_are_separate_and_required() -> None:
    env, identity, rows = _valid_inputs()
    env.pop(ATTESTATION_HMAC_KEY_ENV)
    assert CODE_ATTESTATION_MISSING in _preflight(env, identity, rows)["blockers"]

    env, identity, rows = _valid_inputs()
    env.pop(EVIDENCE_HMAC_KEY_ENV)
    assert CODE_EVIDENCE_KEY_MISSING in _preflight(env, identity, rows)["blockers"]

    env, identity, rows = _valid_inputs()
    env[ATTESTATION_SIGNATURE_ENV] = sign_attestation(
        json.loads(env[ATTESTATION_JSON_ENV]),
        key=KEY,
    )
    assert "sandbox_attestation_invalid" in _preflight(env, identity, rows)["blockers"]


def test_default_off_operator_preflight_performs_zero_database_io() -> None:
    from scripts.operators import internal_conversational_e2e_session as operator

    env = {
        "NAHLA_INTERNAL_E2E_DATABASE_URL": "postgresql://must-not-connect",
    }
    with patch.object(operator, "create_engine") as create_engine_mock:
        result = operator.execute_preflight(tenant_id=TENANT_ID, env=env)
    assert result["ok"] is False
    assert CODE_DEFAULT_OFF in result["blockers"]
    create_engine_mock.assert_not_called()


def test_real_cli_help_bootstraps_repo_backend_and_database_paths() -> None:
    repo_root = Path(__file__).resolve().parents[2]
    script = (
        repo_root
        / "scripts"
        / "operators"
        / "internal_conversational_e2e_session.py"
    )
    completed = subprocess.run(
        [sys.executable, str(script), "--help"],
        cwd=repo_root,
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
    )
    assert completed.returncode == 0, completed.stderr
    assert "preflight" in completed.stdout
    assert "run" in completed.stdout


def test_same_as_canonical_database_identity_blocks() -> None:
    env, identity, rows = _valid_inputs()
    payload = json.loads(env[ATTESTATION_JSON_ENV])
    payload["canonical_database_identity_fingerprint"] = payload[
        "database_identity_fingerprint"
    ]
    env[ATTESTATION_JSON_ENV] = json.dumps(payload)
    env[ATTESTATION_SIGNATURE_ENV] = sign_attestation(
        payload,
        key=ATTESTATION_KEY,
    )
    result = _preflight(env, identity, rows)
    assert CODE_CANONICAL_DATABASE_REJECTED in result["blockers"]


@pytest.mark.parametrize(
    ("tenant_id", "code"),
    [
        (None, CODE_TENANT_REQUIRED),
        (0, CODE_TENANT_REQUIRED),
        (1, CODE_TENANT_1_DENIED),
        (49, CODE_TENANT_NOT_ALLOWED),
    ],
)
def test_tenant_must_be_explicit_non_platform_and_allowlisted(tenant_id, code) -> None:
    env, identity, rows = _valid_inputs()
    result = _preflight(env, identity, rows, tenant_id=tenant_id)
    assert code in result["blockers"]


def test_platform_or_admin_tenant_role_blocks() -> None:
    env, identity, rows = _valid_inputs()
    rows[0]["is_platform_tenant"] = True
    rows[0]["user_roles"] = ["platform_admin"]
    assert CODE_TENANT_ROLE_REJECTED in _preflight(env, identity, rows)["blockers"]

    env, identity, rows = _valid_inputs()
    rows[0]["id"] = TENANT_ID + 1
    assert CODE_TENANT_MISMATCH in _preflight(env, identity, rows)["blockers"]


def test_test_mode_and_dual_phone_allowlists_are_required() -> None:
    env, identity, rows = _valid_inputs()
    rows[0]["ai_settings"]["store_ai_mode"] = "live"
    assert CODE_STORE_AI_MODE_INVALID in _preflight(env, identity, rows)["blockers"]

    env, identity, rows = _valid_inputs()
    env[PHONE_ALLOWLIST_ENV] = "966500000099"
    assert CODE_PHONE_NOT_ALLOWLISTED in _preflight(env, identity, rows)["blockers"]

    env, identity, rows = _valid_inputs()
    rows[0]["ai_settings"]["ai_test_allowed_numbers"] = ["966500000099"]
    assert CODE_PHONE_NOT_ALLOWLISTED in _preflight(env, identity, rows)["blockers"]


def test_llm_is_default_off_and_hosts_must_match_signed_attestation() -> None:
    env, identity, rows = _valid_inputs()
    env.pop(LLM_ENABLE_ENV)
    result = _preflight(env, identity, rows)
    assert CODE_LLM_DEFAULT_OFF in result["blockers"]
    assert CODE_LLM_HOST_ATTESTATION_INVALID in result["blockers"]

    env, identity, rows = _valid_inputs()
    env[LLM_HOST_ALLOWLIST_ENV] = "other-host.test"
    assert CODE_LLM_HOST_ATTESTATION_INVALID in _preflight(
        env, identity, rows
    )["blockers"]


class _FakeDB:
    def __init__(self) -> None:
        self.commits = 0
        self.rollbacks = 0

    def commit(self) -> None:
        self.commits += 1

    def rollback(self) -> None:
        self.rollbacks += 1


class _MemoryMessages:
    rows: list[dict] = []

    @classmethod
    def reset(cls) -> None:
        cls.rows = []

    @classmethod
    def load_history(cls, db, phone, tenant_id):
        return [
            {"direction": row["direction"], "body": row["body"]}
            for row in cls.rows
            if row["phone"] == phone and row["tenant_id"] == tenant_id
        ]

    @classmethod
    def save_message(
        cls,
        db,
        phone,
        body,
        direction,
        conversation_id,
        tenant_id,
        **kwargs,
    ):
        cls.rows.append(
            {
                "phone": phone,
                "body": body,
                "direction": direction,
                "tenant_id": tenant_id,
                "conversation_id": conversation_id,
            }
        )


class _StatefulBrain:
    def __init__(
        self,
        convo,
        *,
        attempt_egress: bool = False,
        duplicate_provider_send: bool = False,
    ) -> None:
        self.convo = convo
        self.attempt_egress = attempt_egress
        self.duplicate_provider_send = duplicate_provider_send

    async def process(self, **kwargs):
        assert current_acceptance_context() is not None
        metadata = dict(self.convo.extra_metadata or {})
        metadata["turn_count"] = int(metadata.get("turn_count") or 0) + 1
        self.convo.extra_metadata = metadata
        if self.attempt_egress or self.duplicate_provider_send:
            events = (
                ("whatsapp_provider", "send_message"),
                ("salla_integration", "create_order"),
            )
            if self.duplicate_provider_send:
                events = (
                    ("whatsapp_provider", "send_message"),
                    ("whatsapp_provider", "send_message"),
                )
            for kind, operation in events:
                try:
                    deny_external_egress(
                        egress_kind=kind,
                        operation=operation,
                        tenant_id=TENANT_ID,
                    )
                except InternalE2EEgressDenied:
                    pass
        return {
            "reply": "Generic catalog response",
            "compose_source": "persona_llm",
            "chosen_path": "generic_catalog_answer",
            "persona_ownership": {
                "persona_stamped": True,
                "expression_owner": "persona_composer",
            },
        }


def _state_probe(db, tenant_id, convo):
    return {
        "message_events": len(_MemoryMessages.rows),
        "turn_count": int((convo.extra_metadata or {}).get("turn_count") or 0),
        "llm_calls": int((convo.extra_metadata or {}).get("turn_count") or 0),
        "tool_calls": 0,
    }


async def _run_turn(
    convo,
    *,
    turn_index: int,
    attempt_egress: bool = False,
    duplicate_provider_send: bool = False,
    expected_denials: tuple[tuple[str, str], ...] = (),
):
    request = SandboxTurnRequest(
        session_id=SESSION_ID,
        scenario_id="generic_shoe_catalog",
        turn_index=turn_index,
        tenant_id=TENANT_ID,
        customer_phone=PHONE,
        text="Is the white running shoe available?",
        conversation=convo,
        allowed_tenants=frozenset({TENANT_ID}),
        evidence_hmac_key=KEY,
        runtime_revision=REVISION,
        database_identity_fingerprint=database_identity_fingerprint(_identity()),
        network_attestation_id="bbbbbbbb-bbbb-4bbb-8bbb-bbbbbbbbbbbb",
        llm_allowed_hosts=("api.generic-llm.test",),
        expected_denials=expected_denials,
        allow_llm_inference=True,
    )

    def _set_response_mode(**kwargs):
        kwargs["trace"].response_mode = "persona"
        return kwargs["reply"], False

    with (
        patch(
            "services.merchant_brain_turn._apply_brain_silent_and_welcome_guards",
            side_effect=_set_response_mode,
        ),
        patch(
            "services.merchant_brain_turn._apply_outbound_dedup",
            side_effect=lambda **kw: (kw["reply"], ""),
        ),
        patch(
            "services.merchant_brain_turn._apply_post_compose_truth_guards",
            side_effect=lambda **kw: kw["reply"],
        ),
    ):
        return await run_sandbox_turn(
            db=_FakeDB(),
            request=request,
            brain_factory=lambda: _StatefulBrain(
                convo,
                attempt_egress=attempt_egress,
                duplicate_provider_send=duplicate_provider_send,
            ),
            state_probe=_state_probe,
            message_store=_MemoryMessages,
        )


def test_harness_itself_rejects_default_off_llm_execution() -> None:
    convo = SimpleNamespace(id=6, tenant_id=TENANT_ID, extra_metadata={})
    request = SandboxTurnRequest(
        session_id=SESSION_ID,
        scenario_id="generic_default_off",
        turn_index=0,
        tenant_id=TENANT_ID,
        customer_phone=PHONE,
        text="Check a generic product.",
        conversation=convo,
        allowed_tenants=frozenset({TENANT_ID}),
        evidence_hmac_key=KEY,
        runtime_revision=REVISION,
        database_identity_fingerprint=database_identity_fingerprint(_identity()),
        network_attestation_id="bbbbbbbb-bbbb-4bbb-8bbb-bbbbbbbbbbbb",
        llm_allowed_hosts=("api.generic-llm.test",),
        allow_llm_inference=False,
    )
    with pytest.raises(ValueError, match="llm_inference_not_explicitly_enabled"):
        asyncio.run(
            run_sandbox_turn(
                db=_FakeDB(),
                request=request,
                brain_factory=lambda: _StatefulBrain(convo),
                state_probe=_state_probe,
                message_store=_MemoryMessages,
            )
        )
    assert current_acceptance_context() is None


def test_context_is_installed_and_reset_and_provenance_is_complete() -> None:
    _MemoryMessages.reset()
    convo = SimpleNamespace(id=7, tenant_id=TENANT_ID, extra_metadata={})
    outcome = asyncio.run(_run_turn(convo, turn_index=0))
    assert current_acceptance_context() is None
    assert outcome.evidence["status"] == "evaluated"
    assert outcome.evidence["verdict"] == "pass"
    assert set(outcome.evidence["provenance"]) >= {
        "compose_source",
        "response_mode",
        "chosen_path",
        "llm_candidate_present",
        "final_text_transformed",
        "final_transform_reasons",
        "reply_source",
        "fallback_source",
        "fallback_reason",
        "fallback_action_type",
    }


def test_provider_denial_audit_is_captured_without_fabricated_success() -> None:
    _MemoryMessages.reset()
    convo = SimpleNamespace(id=8, tenant_id=TENANT_ID, extra_metadata={})
    evidence = asyncio.run(
        _run_turn(
            convo,
            turn_index=0,
            attempt_egress=True,
            expected_denials=(
                ("salla_integration", "create_order"),
                ("whatsapp_provider", "send_message"),
            ),
        )
    ).evidence
    assert {audit["egress_kind"] for audit in evidence["denial_audits"]} == {
        "whatsapp_provider",
        "salla_integration",
    }
    assert evidence["provider_observation"] == {
        "source": "application_internal_e2e_context",
        "network_dispatch_success_observed": False,
        "is_actual_provider_telemetry": False,
    }
    assert evidence["actual_provider_acceptance_satisfied"] is False
    assert all("wamid" not in json.dumps(audit) for audit in evidence["denial_audits"])


def test_unexpected_or_missing_denial_cannot_pass() -> None:
    _MemoryMessages.reset()
    convo = SimpleNamespace(id=81, tenant_id=TENANT_ID, extra_metadata={})
    unexpected = asyncio.run(
        _run_turn(convo, turn_index=0, attempt_egress=True)
    ).evidence
    assert unexpected["verdict"] == "fail"
    assert "unexpected_egress_denial" in unexpected["blockers"]

    _MemoryMessages.reset()
    convo = SimpleNamespace(id=82, tenant_id=TENANT_ID, extra_metadata={})
    missing = asyncio.run(
        _run_turn(
            convo,
            turn_index=0,
            expected_denials=(("financial", "generate_payment_link"),),
        )
    ).evidence
    assert missing["verdict"] == "fail"
    assert "expected_egress_denial_missing" in missing["blockers"]

    _MemoryMessages.reset()
    convo = SimpleNamespace(id=83, tenant_id=TENANT_ID, extra_metadata={})
    operation_mismatch = asyncio.run(
        _run_turn(
            convo,
            turn_index=0,
            attempt_egress=True,
            expected_denials=(
                ("salla_integration", "get_product"),
                ("whatsapp_provider", "send_message"),
            ),
        )
    ).evidence
    assert operation_mismatch["verdict"] == "fail"
    assert "unexpected_egress_denial" in operation_mismatch["blockers"]
    assert "expected_egress_denial_missing" in operation_mismatch["blockers"]


def test_duplicate_observed_provider_denial_exceeds_single_expectation() -> None:
    _MemoryMessages.reset()
    convo = SimpleNamespace(id=84, tenant_id=TENANT_ID, extra_metadata={})
    evidence = asyncio.run(
        _run_turn(
            convo,
            turn_index=0,
            duplicate_provider_send=True,
            expected_denials=(("whatsapp_provider", "send_message"),),
        )
    ).evidence
    assert evidence["verdict"] == "fail"
    assert "unexpected_egress_denial" in evidence["blockers"]
    assert evidence["observed_denial_counts"] == [
        {
            "egress_kind": "whatsapp_provider",
            "operation": "send_message",
            "count": 2,
        }
    ]


def test_provenance_requires_semantically_valid_constitutional_metadata() -> None:
    valid = {
        "compose_source": "persona_llm",
        "response_mode": "persona",
        "chosen_path": "generic_catalog_answer",
        "llm_candidate_present": True,
        "final_text_transformed": False,
        "final_transform_reasons": [],
        "fallback_reason": "",
        "fallback_action_type": "",
    }
    assert _provenance_blockers(valid, evaluated_customer_text=True) == []

    for mutation in (
        {"compose_source": "template"},
        {"response_mode": ""},
        {"chosen_path": ""},
        {"llm_candidate_present": 1},
        {"final_text_transformed": "false"},
        {"final_transform_reasons": "none"},
    ):
        invalid = {**valid, **mutation}
        assert "provenance_incomplete" in _provenance_blockers(
            invalid,
            evaluated_customer_text=True,
        )

    fallback = {
        **valid,
        "compose_source": "fallback_deterministic",
        "fallback_reason": "",
        "fallback_action_type": "",
    }
    assert "provenance_incomplete" in _provenance_blockers(
        fallback,
        evaluated_customer_text=True,
    )


def test_multi_turn_state_and_history_persist_in_writable_sandbox() -> None:
    _MemoryMessages.reset()
    convo = SimpleNamespace(id=9, tenant_id=TENANT_ID, extra_metadata={})
    first = asyncio.run(_run_turn(convo, turn_index=0)).evidence
    second = asyncio.run(_run_turn(convo, turn_index=1)).evidence
    assert convo.extra_metadata["turn_count"] == 2
    assert first["state_delta"]["turn_count"]["after"] == 1
    assert second["state_delta"]["turn_count"]["before"] == 1
    assert len(_MemoryMessages.rows) == 4


def test_evidence_is_redacted_and_labeled_direct_code_probe() -> None:
    _MemoryMessages.reset()
    convo = SimpleNamespace(id=10, tenant_id=TENANT_ID, extra_metadata={})
    evidence = asyncio.run(_run_turn(convo, turn_index=0)).evidence
    encoded = json.dumps(evidence, ensure_ascii=False)
    assert evidence["evidence_channel"] == "direct_code_probe"
    assert PHONE not in encoded
    assert "Is the white running shoe available?" not in encoded
    assert "Generic catalog response" not in encoded
    assert evidence["test_phone_hmac"].startswith("hmac-sha256:")


def test_session_evidence_signature_detects_tampering() -> None:
    payload = {
        "session_schema_version": "internal_conversational_e2e_session_v1",
        "evidence_schema_version": "internal_conversational_e2e_evidence_v1",
        "session_id": SESSION_ID,
        "tenant_id": TENANT_ID,
        "verdict": "pass",
    }
    signed = sign_session_evidence(payload, key=KEY)
    assert verify_session_evidence(signed, key=KEY) is True
    assert signed["integrity"]["schema_version"].endswith("_v1")
    assert signed["integrity"]["key_purpose"] == "session_evidence"

    tampered = json.loads(json.dumps(signed))
    tampered["verdict"] = "fail"
    assert verify_session_evidence(tampered, key=KEY) is False
    assert verify_session_evidence(signed, key=ATTESTATION_KEY) is False


def test_invalid_scenario_manifest_returns_structured_failure_without_engine(
    tmp_path: Path,
) -> None:
    from scripts.operators import internal_conversational_e2e_session as operator

    invalid = tmp_path / "invalid.json"
    invalid.write_text("{not-json", encoding="utf-8")
    env = {
        MASTER_ENABLE_ENV: "true",
        EXECUTION_CONFIRM_ENV: "true",
        ATTESTATION_HMAC_KEY_ENV: ATTESTATION_KEY,
        EVIDENCE_HMAC_KEY_ENV: KEY,
        "NAHLA_INTERNAL_E2E_DATABASE_URL": "postgresql://must-not-connect",
    }
    with patch.object(operator, "create_engine") as create_engine_mock:
        result = asyncio.run(
            operator.run_session(
                tenant_id=TENANT_ID,
                scenario_path=invalid,
                env=env,
            )
        )
    assert result == {
        "ok": False,
        "blockers": ["scenario_manifest_invalid"],
        "evidence_channel": EVIDENCE_CHANNEL,
        "tenant_id": TENANT_ID,
        "exception_class": "JSONDecodeError",
    }
    create_engine_mock.assert_not_called()


def test_stale_test_phone_conversation_blocks_without_mutation() -> None:
    from scripts.operators import internal_conversational_e2e_session as operator

    db = MagicMock()
    stale = SimpleNamespace(id=99, tenant_id=TENANT_ID, external_id=PHONE)
    db.query.return_value.filter.return_value.all.return_value = [stale]
    with pytest.raises(ValueError, match="sandbox_test_phone_not_pristine"):
        operator._conversation(
            db,
            tenant_id=TENANT_ID,
            phone=PHONE,
            session_id=SESSION_ID,
        )
    db.add.assert_not_called()
    db.commit.assert_not_called()
    db.refresh.assert_not_called()


def test_scenario_denial_expectations_require_exact_safe_events(tmp_path: Path) -> None:
    from scripts.operators import internal_conversational_e2e_session as operator

    manifest = tmp_path / "scenarios.json"
    manifest.write_text(
        json.dumps(
            {
                "scenario_schema_version": operator.SCENARIO_SCHEMA_VERSION,
                "scenarios": [
                    {
                        "scenario_id": "generic_catalog",
                        "turns": [
                            {
                                "text": "Generic inquiry",
                                "expected_denials": [
                                    {
                                        "egress_kind": "salla_integration",
                                        "operation": "*",
                                    }
                                ],
                            }
                        ],
                    }
                ],
            }
        ),
        encoding="utf-8",
    )
    with pytest.raises(ValueError, match="expected_denials_invalid"):
        operator._load_scenarios(manifest)


@pytest.mark.parametrize(
    "scenario",
    [
        {
            "scenario_id": "../../unsafe",
            "turns": [{"text": "Generic inquiry"}],
        },
        {
            "scenario_id": "generic_catalog",
            "turns": [
                {
                    "text": "Generic inquiry",
                    "expected_status": "provider_success",
                }
            ],
        },
    ],
)
def test_unsafe_scenario_identity_or_status_blocks_before_engine(
    tmp_path: Path,
    scenario: dict,
) -> None:
    from scripts.operators import internal_conversational_e2e_session as operator

    manifest = tmp_path / "unsafe.json"
    manifest.write_text(
        json.dumps(
            {
                "scenario_schema_version": operator.SCENARIO_SCHEMA_VERSION,
                "scenarios": [scenario],
            }
        ),
        encoding="utf-8",
    )
    env = {
        MASTER_ENABLE_ENV: "true",
        EXECUTION_CONFIRM_ENV: "true",
        ATTESTATION_HMAC_KEY_ENV: ATTESTATION_KEY,
        EVIDENCE_HMAC_KEY_ENV: KEY,
        "NAHLA_INTERNAL_E2E_DATABASE_URL": "postgresql://must-not-connect",
    }
    with patch.object(operator, "create_engine") as create_engine_mock:
        result = asyncio.run(
            operator.run_session(
                tenant_id=TENANT_ID,
                scenario_path=manifest,
                env=env,
            )
        )
    assert result["ok"] is False
    assert result["blockers"] == ["scenario_manifest_invalid"]
    create_engine_mock.assert_not_called()


def test_final_session_has_signed_integrity_and_auditable_timestamps(
    tmp_path: Path,
) -> None:
    from scripts.operators import internal_conversational_e2e_session as operator
    from services.internal_conversational_e2e_harness import SandboxTurnOutcome

    env = {
        MASTER_ENABLE_ENV: "true",
        EXECUTION_CONFIRM_ENV: "true",
        ATTESTATION_HMAC_KEY_ENV: ATTESTATION_KEY,
        EVIDENCE_HMAC_KEY_ENV: KEY,
        "NAHLA_INTERNAL_E2E_DATABASE_URL": "postgresql://unused",
        TEST_PHONE_ENV: PHONE,
        TENANT_ALLOWLIST_ENV: str(TENANT_ID),
    }
    preflight = {
        "ok": True,
        "runtime_revision": REVISION,
        "database_identity_fingerprint": database_identity_fingerprint(_identity()),
        "attestation_id": "bbbbbbbb-bbbb-4bbb-8bbb-bbbbbbbbbbbb",
        "llm_allowed_hosts": ["api.generic-llm.test"],
    }
    turn_evidence = {
        "status": "evaluated",
        "state_delta": {},
        "blockers": [],
        "verdict": "pass",
        "denial_audits": [],
        "provider_observation": {
            "source": "application_internal_e2e_context",
            "network_dispatch_success_observed": False,
            "is_actual_provider_telemetry": False,
        },
    }
    captured: dict = {}

    def _capture(payload, env_map):
        captured.update(payload)
        return tmp_path / "session.json"

    db = MagicMock()
    with (
        patch.object(operator, "_load_scenarios", return_value=[
            {
                "scenario_id": "generic_catalog",
                "turns": [
                    {
                        "text": "Generic inquiry",
                        "expected_status": "evaluated",
                        "expected_state_delta_keys": [],
                        "expected_denials": (),
                    }
                ],
            }
        ]),
        patch.object(operator, "execute_preflight", return_value=preflight),
        patch.object(
            operator,
            "_conversation",
            return_value=(
                SimpleNamespace(id=15, tenant_id=TENANT_ID, extra_metadata={}),
                True,
            ),
        ),
        patch.object(operator, "sessionmaker", return_value=lambda: db),
        patch.object(
            operator,
            "run_sandbox_turn",
            new=AsyncMock(
                return_value=SandboxTurnOutcome(
                    evidence=turn_evidence,
                    evaluated_status="evaluated",
                )
            ),
        ),
        patch.object(operator, "_write_session", side_effect=_capture),
    ):
        result = asyncio.run(
            operator.run_session(
                tenant_id=TENANT_ID,
                scenario_path=tmp_path / "ignored.json",
                env=env,
                engine=MagicMock(),
            )
        )

    assert result["ok"] is True
    assert verify_session_evidence(captured, key=KEY) is True
    assert datetime.fromisoformat(captured["started_at_utc"]) <= datetime.fromisoformat(
        captured["completed_at_utc"]
    )
    assert captured["provider_observation"]["is_actual_provider_telemetry"] is False
    assert captured["actual_provider_acceptance_satisfied"] is False


def test_operator_has_no_row_deletion_cleanup_path() -> None:
    source = (
        Path(__file__).resolve().parents[2]
        / "scripts"
        / "operators"
        / "internal_conversational_e2e_session.py"
    ).read_text(encoding="utf-8")
    assert "DELETE FROM" not in source.upper()
    assert "cleanup_contract" in source
    assert "dispose_attested_sandbox_database_externally" in source

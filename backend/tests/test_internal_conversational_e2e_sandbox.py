"""Focused regressions for the disposable conversational E2E harness."""
from __future__ import annotations

import asyncio
import json
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import pytest

from core.acceptance_execution_context import (
    InternalE2EEgressDenied,
    current_acceptance_context,
    deny_external_egress,
)
from services.internal_conversational_e2e_contract import (
    ATTESTATION_JSON_ENV,
    ATTESTATION_SIGNATURE_ENV,
    CODE_ATTESTATION_MISSING,
    CODE_CANONICAL_DATABASE_REJECTED,
    CODE_DEFAULT_OFF,
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
    sign_attestation,
)
from services.internal_conversational_e2e_harness import (
    SandboxTurnRequest,
    run_sandbox_turn,
)


TENANT_ID = 48
SESSION_ID = "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa"
REVISION = "813d27825a32f0ab931a80ed3c854bafc67a9e49"
PHONE = "966500000048"
KEY = "test-evidence-key-not-a-production-secret"


def _identity(name: str = "nahla_e2e_generic_shop") -> dict[str, str]:
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
        ATTESTATION_JSON_ENV: json.dumps(payload),
        ATTESTATION_SIGNATURE_ENV: sign_attestation(payload, key=KEY),
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


def test_valid_disposable_preflight_is_direct_probe_only() -> None:
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


def test_canonical_or_shared_database_identity_blocks() -> None:
    env, identity, rows = _valid_inputs()
    shared = {**identity, "database_name": "nahla_staging"}
    payload = json.loads(env[ATTESTATION_JSON_ENV])
    payload["database_identity_fingerprint"] = database_identity_fingerprint(shared)
    payload["canonical_database_identity_fingerprint"] = payload[
        "database_identity_fingerprint"
    ]
    env[ATTESTATION_JSON_ENV] = json.dumps(payload)
    env[ATTESTATION_SIGNATURE_ENV] = sign_attestation(payload, key=KEY)
    result = _preflight(env, shared, rows)
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
    def __init__(self, convo, *, attempt_egress: bool = False) -> None:
        self.convo = convo
        self.attempt_egress = attempt_egress

    async def process(self, **kwargs):
        assert current_acceptance_context() is not None
        metadata = dict(self.convo.extra_metadata or {})
        metadata["turn_count"] = int(metadata.get("turn_count") or 0) + 1
        self.convo.extra_metadata = metadata
        if self.attempt_egress:
            for kind, operation in (
                ("whatsapp_provider", "send_message"),
                ("salla_integration", "create_order"),
            ):
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


async def _run_turn(convo, *, turn_index: int, attempt_egress: bool = False):
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
        allow_llm_inference=True,
    )
    with (
        patch(
            "services.merchant_brain_turn._apply_brain_silent_and_welcome_guards",
            side_effect=lambda **kw: (kw["reply"], False),
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
        _run_turn(convo, turn_index=0, attempt_egress=True)
    ).evidence
    assert {audit["egress_kind"] for audit in evidence["denial_audits"]} == {
        "whatsapp_provider",
        "salla_integration",
    }
    assert evidence["provider_send_success"] is False
    assert evidence["actual_provider_acceptance_satisfied"] is False
    assert all("wamid" not in json.dumps(audit) for audit in evidence["denial_audits"])


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

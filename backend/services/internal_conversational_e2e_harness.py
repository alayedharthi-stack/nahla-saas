"""Disposable-DB harness for the live merchant Brain turn boundary."""
from __future__ import annotations

import re
import time
import uuid
from dataclasses import asdict, dataclass, field
from typing import Any, Callable, Mapping, Optional

from core.acceptance_execution_context import (
    internal_conversational_e2e_context,
    recorded_egress_denials,
)
from services.internal_conversational_e2e_contract import (
    CODE_PROVENANCE_INCOMPLETE,
    EVIDENCE_CHANNEL,
    EVIDENCE_SCHEMA_VERSION,
    EXPECTED_DENIAL_KINDS,
    hmac_identifier,
    validate_explicit_tenant_id,
)
from services.merchant_brain_turn import (
    LiveMerchantBrainPreconditions,
    LiveMerchantBrainTurnInput,
    evaluate_live_merchant_brain_turn,
)


_SCENARIO_ID_RE = re.compile(r"^[a-zA-Z0-9_.:-]{1,96}$")


@dataclass(frozen=True)
class SandboxTurnRequest:
    session_id: str
    scenario_id: str
    turn_index: int
    tenant_id: int
    customer_phone: str
    text: str
    conversation: Any
    allowed_tenants: frozenset[int]
    evidence_hmac_key: str
    runtime_revision: str
    database_identity_fingerprint: str
    network_attestation_id: str
    llm_allowed_hosts: tuple[str, ...]
    expected_denial_kinds: tuple[str, ...] = ()
    allow_llm_inference: bool = False
    profile: Mapping[str, Any] = field(default_factory=dict)
    inbound_metadata: Mapping[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class SandboxTurnOutcome:
    evidence: dict[str, Any]
    evaluated_status: str


def _validate_request(request: SandboxTurnRequest) -> None:
    try:
        uuid.UUID(request.session_id)
    except (TypeError, ValueError, AttributeError) as exc:
        raise ValueError("session_id_invalid") from exc
    if not _SCENARIO_ID_RE.fullmatch(request.scenario_id):
        raise ValueError("scenario_id_invalid")
    if type(request.turn_index) is not int or request.turn_index < 0:
        raise ValueError("turn_index_invalid")
    tenant_blockers = validate_explicit_tenant_id(
        request.tenant_id,
        request.allowed_tenants,
    )
    if tenant_blockers:
        raise ValueError(tenant_blockers[0])
    if int(getattr(request.conversation, "tenant_id", 0) or 0) != request.tenant_id:
        raise ValueError("conversation_tenant_mismatch")
    if not request.customer_phone or not request.text:
        raise ValueError("turn_input_invalid")
    if not request.evidence_hmac_key:
        raise ValueError("evidence_hmac_key_missing")
    if (
        not all(isinstance(kind, str) for kind in request.expected_denial_kinds)
        or len(set(request.expected_denial_kinds)) != len(request.expected_denial_kinds)
        or not set(request.expected_denial_kinds).issubset(EXPECTED_DENIAL_KINDS)
    ):
        raise ValueError("expected_denial_kinds_invalid")
    if not request.allow_llm_inference:
        raise ValueError("llm_inference_not_explicitly_enabled")
    if (
        not request.runtime_revision
        or not request.database_identity_fingerprint.startswith("sha256:")
        or not request.network_attestation_id
        or not request.llm_allowed_hosts
    ):
        raise ValueError("sandbox_execution_attestation_incomplete")


def _state_delta(
    before: Mapping[str, Any],
    after: Mapping[str, Any],
) -> dict[str, Any]:
    keys = sorted(set(before) | set(after))
    changed: dict[str, Any] = {}
    for key in keys:
        left = before.get(key)
        right = after.get(key)
        if left != right:
            if isinstance(left, (int, float)) and isinstance(right, (int, float)):
                changed[key] = {"before": left, "after": right, "delta": right - left}
            else:
                changed[key] = {"before": left, "after": right}
    return changed


def _provenance_blockers(
    provenance: Mapping[str, Any],
    *,
    evaluated_customer_text: bool,
) -> list[str]:
    """Validate the semantic constitutional provenance contract."""
    if not evaluated_customer_text:
        return []
    from modules.ai.compose.constitutional_policy import APPROVED_COMPOSE_SOURCES

    blockers: list[str] = []
    compose_source = str(provenance.get("compose_source") or "").strip()
    if compose_source not in APPROVED_COMPOSE_SOURCES:
        blockers.append(CODE_PROVENANCE_INCOMPLETE)
    if not str(provenance.get("response_mode") or "").strip():
        blockers.append(CODE_PROVENANCE_INCOMPLETE)
    if not str(provenance.get("chosen_path") or "").strip():
        blockers.append(CODE_PROVENANCE_INCOMPLETE)
    if type(provenance.get("llm_candidate_present")) is not bool:
        blockers.append(CODE_PROVENANCE_INCOMPLETE)
    if type(provenance.get("final_text_transformed")) is not bool:
        blockers.append(CODE_PROVENANCE_INCOMPLETE)
    reasons = provenance.get("final_transform_reasons")
    if not isinstance(reasons, (list, tuple)) or not all(
        isinstance(reason, str) for reason in reasons
    ):
        blockers.append(CODE_PROVENANCE_INCOMPLETE)
    if compose_source == "fallback_deterministic" and (
        not str(provenance.get("fallback_reason") or "").strip()
        or not str(provenance.get("fallback_action_type") or "").strip()
    ):
        blockers.append(CODE_PROVENANCE_INCOMPLETE)
    return sorted(set(blockers))


async def run_sandbox_turn(
    *,
    db: Any,
    request: SandboxTurnRequest,
    brain_factory: Callable[[], Any],
    state_probe: Callable[[Any, int, Any], Mapping[str, Any]],
    message_store: Optional[Any] = None,
) -> SandboxTurnOutcome:
    """Run one stateful turn without invoking webhook or provider dispatch."""
    _validate_request(request)
    if message_store is None:
        from core.conversation_engine import StateManager

        message_store = StateManager

    from modules.ai.brain.persona_ownership import PersonaOwnershipRecord
    from services.turn_trace import new_trace

    started = time.perf_counter()
    blockers: list[str] = []
    mutations: list[str] = []
    result = None
    before: Mapping[str, Any] = {}
    after: Mapping[str, Any] = {}
    denial_audits: list[dict[str, Any]] = []

    with internal_conversational_e2e_context(
        session_id=request.session_id,
        tenant_id=request.tenant_id,
        allow_llm_inference=request.allow_llm_inference,
    ):
        before = dict(state_probe(db, request.tenant_id, request.conversation))
        history_before = message_store.load_history(
            db,
            phone=request.customer_phone,
            tenant_id=request.tenant_id,
        )
        event_metadata = {
            "acceptance_session_id": request.session_id,
            "acceptance_scenario_id": request.scenario_id,
            "acceptance_turn_index": request.turn_index,
            "acceptance_evidence_channel": EVIDENCE_CHANNEL,
            "message_origin": "internal_conversational_e2e",
            "historical_import": True,
        }
        message_store.save_message(
            db,
            request.customer_phone,
            request.text,
            "inbound",
            conversation_id=request.conversation.id,
            tenant_id=request.tenant_id,
            event_type="internal_e2e",
            extra_metadata=event_metadata,
        )
        mutations.append("sandbox_inbound_message_persisted")
        history = message_store.load_history(
            db,
            phone=request.customer_phone,
            tenant_id=request.tenant_id,
        )
        if len(history) < len(history_before):
            blockers.append("history_persistence_regressed")

        trace = new_trace(
            tenant_id=request.tenant_id,
            phone=request.customer_phone,
            message_id=f"{request.session_id}:{request.scenario_id}:{request.turn_index}",
            inbound_text=request.text,
        )
        persona_ownership = PersonaOwnershipRecord()
        turn_input = LiveMerchantBrainTurnInput(
            customer_phone=request.customer_phone,
            text=request.text,
            inbound_metadata={
                **dict(request.inbound_metadata),
                **event_metadata,
            },
            wa_msg_id=None,
            conversation_id=request.conversation.id,
            history=history,
            preconditions=LiveMerchantBrainPreconditions(
                brain_active=True,
                skip_ai=False,
                billing_allowed=True,
                conversation_quota_allowed=True,
                outbound_lock_available=True,
                store_ai_allowed=True,
                ai_disabled=False,
            ),
            profile=dict(request.profile),
        )
        result = await evaluate_live_merchant_brain_turn(
            db=db,
            tenant_id=request.tenant_id,
            phone_id="internal-direct-code-probe",
            turn_input=turn_input,
            convo=request.conversation,
            trace=trace,
            persona_ownership=persona_ownership,
            brain_factory=brain_factory,
            brain_active=True,
        )

        provenance = asdict(result.provenance)
        if result.status == "evaluated" and result.reply_text:
            message_store.save_message(
                db,
                request.customer_phone,
                result.reply_text,
                "outbound",
                conversation_id=request.conversation.id,
                tenant_id=request.tenant_id,
                event_type="internal_e2e",
                extra_metadata={
                    **event_metadata,
                    **provenance,
                },
            )
            mutations.append("sandbox_outbound_history_persisted_without_wire")
        try:
            db.commit()
        except Exception:
            db.rollback()
            blockers.append("sandbox_commit_failed")

        after = dict(state_probe(db, request.tenant_id, request.conversation))
        denial_audits = [
            {
                "code": "internal_e2e_egress_denied",
                "denial_id": audit.denial_id,
                "egress_kind": audit.egress_kind,
                "operation": audit.operation,
                "reason": audit.reason,
                "requested_tenant_id": audit.requested_tenant_id,
                "tenant_id": audit.tenant_id,
            }
            for audit in recorded_egress_denials()
        ]

    assert result is not None
    provenance = asdict(result.provenance)
    blockers.extend(
        _provenance_blockers(
            provenance,
            evaluated_customer_text=bool(
                result.status == "evaluated" and (result.reply_text or "").strip()
            ),
        )
    )
    actual_denial_kinds = {
        str(audit.get("egress_kind") or "") for audit in denial_audits
    }
    expected_denial_kinds = set(request.expected_denial_kinds)
    if actual_denial_kinds - expected_denial_kinds:
        blockers.append("unexpected_egress_denial")
    if expected_denial_kinds - actual_denial_kinds:
        blockers.append("expected_egress_denial_missing")
    if result.status == "brain_exception":
        blockers.append("brain_exception")

    evidence = {
        "evidence_schema_version": EVIDENCE_SCHEMA_VERSION,
        "evidence_channel": EVIDENCE_CHANNEL,
        "session_id": request.session_id,
        "scenario_id": request.scenario_id,
        "turn_index": request.turn_index,
        "tenant_id": request.tenant_id,
        "runtime_revision": request.runtime_revision,
        "database_identity_fingerprint": request.database_identity_fingerprint,
        "network_attestation_id": request.network_attestation_id,
        "llm_allowed_hosts": list(request.llm_allowed_hosts),
        "test_phone_hmac": hmac_identifier(
            request.customer_phone,
            key=request.evidence_hmac_key,
        ),
        "status": result.status,
        "provenance": provenance,
        "state_delta": _state_delta(before, after),
        "denial_audits": denial_audits,
        "expected_denial_kinds": sorted(expected_denial_kinds),
        "llm_calls": max(
            0,
            int(after.get("llm_calls", 0) or 0) - int(before.get("llm_calls", 0) or 0),
        ),
        "tool_calls": max(
            0,
            int(after.get("tool_calls", 0) or 0) - int(before.get("tool_calls", 0) or 0),
        ),
        "latency_ms": int((time.perf_counter() - started) * 1000),
        "mutations": mutations,
        "verdict": "pass" if not blockers else "fail",
        "blockers": sorted(set(blockers)),
        "provider_observation": {
            "source": "application_internal_e2e_context",
            "network_dispatch_success_observed": False,
            "is_actual_provider_telemetry": False,
        },
        "actual_provider_acceptance_satisfied": False,
        "sandbox_disposal_required": True,
    }
    return SandboxTurnOutcome(evidence=evidence, evaluated_status=result.status)

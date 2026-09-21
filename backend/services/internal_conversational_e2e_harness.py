"""Disposable-DB harness for the live merchant Brain turn boundary."""
from __future__ import annotations

import time
import uuid
from collections import Counter
from dataclasses import asdict, dataclass, field
from typing import Any, Callable, Mapping, Optional, Sequence

from core.acceptance_execution_context import (
    internal_conversational_e2e_context,
    recorded_egress_denials,
)
from services.internal_conversational_e2e_sql_error_audit import (
    internal_e2e_sql_error_turn,
    summarize_turn_sql_error_audit,
)
from services.internal_conversational_e2e_contract import (
    CODE_PROVENANCE_INCOMPLETE,
    EGRESS_DENIAL_KINDS,
    FAILURE_INJECTIONS,
    EVIDENCE_CHANNEL,
    EVIDENCE_SCHEMA_VERSION,
    SAFE_AUDIT_VALUE_RE,
    SAFE_SCENARIO_ID_RE,
    hmac_identifier,
    validate_explicit_tenant_id,
)
from services.merchant_brain_turn import (
    LiveMerchantBrainPreconditions,
    LiveMerchantBrainTurnInput,
    evaluate_live_merchant_brain_turn,
)


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
    expected_denials: tuple[tuple[str, str], ...] = ()
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
    if not SAFE_SCENARIO_ID_RE.fullmatch(request.scenario_id):
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
    expected_denials: set[tuple[str, str]] = set()
    for event in request.expected_denials:
        if (
            not isinstance(event, tuple)
            or len(event) != 2
            or event[0] not in EGRESS_DENIAL_KINDS
            or not isinstance(event[1], str)
            or not SAFE_AUDIT_VALUE_RE.fullmatch(event[1])
        ):
            raise ValueError("expected_denials_invalid")
        expected_denials.add((event[0], event[1]))
    if len(expected_denials) != len(request.expected_denials):
        raise ValueError("expected_denials_invalid")
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
    else:
        transformed = provenance.get("final_text_transformed")
        normalized_reasons = [str(reason).strip() for reason in reasons if str(reason or "").strip()]
        if transformed is True and not normalized_reasons:
            blockers.append(CODE_PROVENANCE_INCOMPLETE)
        if transformed is False and normalized_reasons:
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
    # Wall-clock start, so an address operation published by an EARLIER
    # turn cannot be mistaken for one this turn performed.
    turn_started_at = _utc_now()
    blockers: list[str] = []
    mutations: list[str] = []
    result = None
    before: Mapping[str, Any] = {}
    after: Mapping[str, Any] = {}
    denial_audits: list[dict[str, Any]] = []
    turn_sql_error_audit: dict[str, object] = summarize_turn_sql_error_audit(())

    with internal_conversational_e2e_context(
        session_id=request.session_id,
        tenant_id=request.tenant_id,
        allow_llm_inference=request.allow_llm_inference,
    ), internal_e2e_sql_error_turn(
        scenario_id=request.scenario_id,
        turn_index=request.turn_index,
    ) as sql_error_scope:
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
    turn_sql_error_audit = sql_error_scope.summary

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
    actual_denials = Counter(
        (
            str(audit.get("egress_kind") or ""),
            str(audit.get("operation") or ""),
        )
        for audit in denial_audits
    )
    expected_denials = Counter(request.expected_denials)
    if actual_denials - expected_denials:
        blockers.append("unexpected_egress_denial")
    if expected_denials - actual_denials:
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
        "runtime_error_audit": turn_sql_error_audit,
        "expected_denials": [
            {"egress_kind": kind, "operation": operation}
            for kind, operation in sorted(expected_denials.elements())
        ],
        "observed_denial_counts": [
            {
                "egress_kind": kind,
                "operation": operation,
                "count": count,
            }
            for (kind, operation), count in sorted(actual_denials.items())
        ],
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


# ── OrderFlowV2 address turn ──────────────────────────────────────────────
#
# ``run_sandbox_turn`` above evaluates the Brain. The OrderFlowV2 address
# path is NOT reachable from there: it lives in the webhook handler, ahead
# of the Brain, and ``merchant_brain_turn`` never mentions it. So a turn
# that needs the real owner, the real compose, the real guards, the real
# recovery and the real outbound serialization has to enter where they
# actually are — ``_handle_merchant_message`` — with the transport
# captured rather than dispatched.
#
# Nothing about the boundary is simulated: the same sanitizer, the same
# dedup decision, the same receipt derived from the payload that left. A
# synthetic delivery id proves LOCAL processing and nothing else, so every
# record here says ``transport="captured"``.


@dataclass(frozen=True)
class SandboxOf2TurnRequest:
    session_id: str
    scenario_id: str
    turn_index: int
    tenant_id: int
    customer_phone: str
    phone_id: str
    text: str
    conversation: Any
    allowed_tenants: frozenset[int]
    evidence_hmac_key: str
    runtime_revision: str
    database_identity_fingerprint: str
    network_attestation_id: str
    llm_allowed_hosts: tuple[str, ...]
    turn_ref: str
    expected_denials: tuple[tuple[str, str], ...] = ()
    allow_llm_inference: bool = False
    failure_injection: str = "none"
    expects_address_turn: bool = False
    expectations: Mapping[str, Any] = field(default_factory=dict)
    expected_state_delta_keys: tuple[str, ...] = ()
    # What this turn must DO. ``address_reply`` is a new collection turn;
    # ``structured_selection`` is a captured choice being consumed;
    # ``refusal`` is an expected gate. A turn that declares none of these
    # cannot be accepted just because the handler returned.
    expected_operational_result: str = ""
    inbound_metadata: Mapping[str, Any] = field(default_factory=dict)
    state_probe: Optional[Callable[[Any, int, Any], Mapping[str, Any]]] = None


def _payload_digest(payload: Mapping[str, Any]) -> str:
    """A digest of the payload that was captured, for binding only."""
    import hashlib  # noqa: PLC0415
    import json  # noqa: PLC0415

    try:
        canonical = json.dumps(
            dict(payload or {}),
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            default=str,
        )
        return "sha256:" + hashlib.sha256(canonical.encode()).hexdigest()
    except Exception:  # noqa: BLE001  # noqa: silent-ok — an undigestible payload fails the binding check
        return ""


OPERATIONAL_RESULT_ADDRESS_REPLY = "address_reply"
OPERATIONAL_RESULT_STRUCTURED_SELECTION = "structured_selection"
OPERATIONAL_RESULT_REFUSAL = "refusal"
OPERATIONAL_RESULTS = frozenset(
    {
        OPERATIONAL_RESULT_ADDRESS_REPLY,
        OPERATIONAL_RESULT_STRUCTURED_SELECTION,
        OPERATIONAL_RESULT_REFUSAL,
    }
)

STATUS_EVALUATED = "evaluated"
STATUS_NO_EXECUTION = "no_execution_observed"


def _derived_status(
    *,
    captured: bool,
    model_calls: list,
    state_delta: Mapping[str, Any],
    outbound_row_verified: bool,
) -> str:
    """What the turn did, read off what it did.

    Initialising the status to ``evaluated`` and leaving it there whenever
    the handler returned normally meant an early return that performed no
    work reported the same status as a fully executed turn.
    """
    if captured or model_calls or outbound_row_verified or dict(state_delta or {}):
        return STATUS_EVALUATED
    return STATUS_NO_EXECUTION


def _operational_result_blockers(
    *,
    expected: str,
    status: str,
    captured: bool,
    address_turn: Mapping[str, Any],
    state_delta: Mapping[str, Any],
    selection: Optional[Mapping[str, Any]] = None,
    refusal: Optional[Mapping[str, Any]] = None,
) -> list[str]:
    """Did the turn produce the operational result it declared?"""
    if expected not in OPERATIONAL_RESULTS:
        return ["expected_operational_result_missing"]
    if expected == OPERATIONAL_RESULT_REFUSAL:
        # Absence of transport is not a refusal. A turn that quietly did
        # nothing produced exactly this evidence, so the gate that
        # refused has to be named and its reason recorded.
        if captured:
            return ["expected_refusal_not_observed"]
        gate = dict(refusal or {})
        if not gate.get("observed") or not str(gate.get("gate") or "").strip():
            return ["refusal_gate_unestablished"]
        if not str(gate.get("reason") or "").strip():
            return ["refusal_reason_unestablished"]
        return []
    if status != STATUS_EVALUATED:
        return ["expected_operational_result_not_observed"]
    if expected == OPERATIONAL_RESULT_ADDRESS_REPLY:
        return [] if captured else ["expected_operational_result_not_observed"]

    # A structured selection has to CONSUME the action the customer
    # tapped and leave a DURABLE record of it. "Some state changed" was
    # never selection evidence: every metadata write moves the
    # conversation fingerprint, so a handler that touched an unrelated
    # counter and selected nothing passed. What is required is the
    # address the action names, recorded as selected, in this customer's
    # and tenant's scope, against the revision that was shown.
    found: list[str] = []
    changed = dict(state_delta or {})
    if not changed:
        found.append("structured_selection_changed_no_state")
    chosen = dict(selection or {})
    if not str(chosen.get("consumed_action_id") or "").strip():
        found.append("structured_selection_action_not_consumed")
    if not chosen.get("selection_scope_verified"):
        found.append("structured_selection_scope_unverified")
    # The showing the action names has to exist. An invented or
    # superseded offer resolves to nothing, and answers nothing.
    if not chosen.get("showing_exists"):
        found.append("structured_selection_showing_unknown")
    # The writer has to have published an operation for THIS turn.
    # Parsing an identifier is not consumption, and an operation left
    # behind by an earlier turn is not this turn's.
    if not chosen.get("operation_recorded"):
        found.append("structured_selection_operation_not_recorded")
    elif not chosen.get("operation_observed_in_turn"):
        found.append("structured_selection_operation_not_from_this_turn")
    if str(chosen.get("selection_state") or "") != "selected":
        found.append("structured_selection_not_durably_recorded")
    # The revision recorded has to be the revision that was SHOWN.
    shown = str(chosen.get("shown_fingerprint") or "")
    if shown and str(chosen.get("selected_fingerprint") or "") != shown:
        found.append("structured_selection_revision_mismatch")
    if not chosen.get("selection_matches_action"):
        found.append("structured_selection_address_mismatch")
    return found


# Representations a reply legitimately takes between the wire and the
# persisted row. A text reply is stored as it left; an interactive reply
# stores its BODY while the wire payload also carries the actions. That
# transformation is supported and named, so a mismatch is a finding
# rather than an unexplained difference — and nothing here compares
# wording against anything the harness would prefer it to say.
REPRESENTATION_TEXT = "text_body"
REPRESENTATION_INTERACTIVE = "interactive_body"
REPRESENTATION_UNSUPPORTED = "unsupported"
CONTENT_REPRESENTATIONS: tuple[str, ...] = (
    REPRESENTATION_TEXT,
    REPRESENTATION_INTERACTIVE,
)


def _captured_customer_text(payload: Mapping[str, Any]) -> tuple[str, str]:
    """The customer-visible text in the captured payload, and its shape."""
    data = dict(payload or {})
    body = data.get("text")
    if isinstance(body, Mapping) and str(body.get("body") or "").strip():
        return str(body.get("body")), REPRESENTATION_TEXT
    interactive = data.get("interactive")
    if isinstance(interactive, Mapping):
        inner = interactive.get("body")
        if isinstance(inner, Mapping) and str(inner.get("text") or "").strip():
            return str(inner.get("text")), REPRESENTATION_INTERACTIVE
    return "", REPRESENTATION_UNSUPPORTED


def _text_digest(value: str) -> str:
    import hashlib  # noqa: PLC0415

    normalized = " ".join(str(value or "").split())
    if not normalized:
        return ""
    return "sha256:" + hashlib.sha256(normalized.encode("utf-8")).hexdigest()


def _content_binding(
    *,
    captured_text: str,
    persisted_body: str,
    representation: str,
    captured: bool,
    row_found: bool,
) -> dict[str, Any]:
    """Does what left match what was stored, under a SUPPORTED representation?

    Only whitespace is normalised before comparison. No wording is
    preferred, rewritten or matched against a phrase list: the question is
    whether two artifacts of the same reply agree, not whether the reply
    reads the way anyone wanted.
    """
    captured_digest = _text_digest(captured_text)
    persisted_digest = _text_digest(persisted_body)
    if not captured:
        return {
            "captured_text_digest": "",
            "persisted_body_digest": "",
            "representation": "",
            "content_match": False,
            "reason": "not_captured",
        }
    if representation not in CONTENT_REPRESENTATIONS:
        return {
            "captured_text_digest": captured_digest,
            "persisted_body_digest": persisted_digest,
            "representation": REPRESENTATION_UNSUPPORTED,
            "content_match": False,
            "reason": "representation_unsupported",
        }
    if not row_found or not persisted_digest:
        return {
            "captured_text_digest": captured_digest,
            "persisted_body_digest": persisted_digest,
            "representation": representation,
            "content_match": False,
            "reason": "persisted_row_absent",
        }
    if not captured_digest:
        return {
            "captured_text_digest": "",
            "persisted_body_digest": persisted_digest,
            "representation": representation,
            "content_match": False,
            "reason": "captured_text_absent",
        }
    match = captured_digest == persisted_digest
    return {
        "captured_text_digest": captured_digest,
        "persisted_body_digest": persisted_digest,
        "representation": representation,
        "content_match": match,
        "reason": "" if match else "captured_persisted_content_differs",
    }


def _timing_projection(raw: Any) -> dict[str, Any]:
    """The identity fields of a turn measurement, and nothing else.

    The full latency snapshot carries spans and provider detail that have
    no bearing on whether the measurement belongs to this turn. Only the
    identity is projected into the artifact, so what the validator checks
    is exactly what it can check.
    """
    if not isinstance(raw, Mapping) or not raw:
        return {}
    return {
        "turn_id": str(raw.get("turn_id") or ""),
        "message_id": str(raw.get("message_id") or ""),
        "conversation_id": int(raw.get("conversation_id") or 0),
        "tenant_id": int(raw.get("tenant_id") or 0),
        "total_turn_ms": int(raw.get("total_turn_ms") or 0),
        "llm_call_count": len(list(raw.get("llm_calls") or [])),
    }


def _outbound_row_for_turn(
    db: Any,
    *,
    conversation_id: int,
    turn_ref: str,
    tenant_id: int = 0,
    customer_id: int = 0,
) -> tuple[str, dict[str, Any], bool, str]:
    """Fetch the row this turn actually wrote, and say whether it is that row.

    Returning the newest outbound id and calling it bound proved nothing:
    any non-empty string satisfied the old check. This finds the row whose
    persisted metadata names THIS turn and reports the body it stored, so
    the captured payload can be compared against something the runtime
    wrote independently rather than against itself.

    The lookup no longer assumes which conversation the runtime chose. The
    webhook resolves a customer's conversation through its own path, which
    is not necessarily the row a fixture prepared — reading "the newest
    outbound row in the conversation I guessed" found nothing at all while
    the turn had in fact written one. The turn reference is searched
    within the CUSTOMER's conversations and the owning row is then checked
    to belong to this tenant and customer, which is a stronger binding
    than a guessed conversation plus a matching label.
    """
    try:
        from models import Conversation, MessageEvent  # noqa: PLC0415

        wanted = str(turn_ref or "")
        scope_ids: list[int] = []
        if int(conversation_id or 0):
            scope_ids.append(int(conversation_id))
        if int(tenant_id or 0) and int(customer_id or 0):
            scope_ids.extend(
                int(row_id)
                for (row_id,) in db.query(Conversation.id).filter(
                    Conversation.tenant_id == int(tenant_id),
                    Conversation.customer_id == int(customer_id),
                )
                if int(row_id) not in scope_ids
            )
        if not scope_ids:
            return "", {}, False, ""

        rows = (
            db.query(MessageEvent)
            .filter(
                MessageEvent.conversation_id.in_(scope_ids),
                MessageEvent.direction == "outbound",
            )
            .order_by(MessageEvent.id.desc())
            .limit(50)
            .all()
        )
        for row in rows:
            stored = getattr(row, "extra_metadata", None)
            metadata = dict(stored) if isinstance(stored, Mapping) else {}
            if wanted and str(metadata.get("address_turn_ref") or "") == wanted:
                return (
                    str(getattr(row, "id", "") or ""),
                    metadata,
                    bool(str(getattr(row, "id", "") or "")),
                    str(getattr(row, "body", "") or getattr(row, "content", "") or ""),
                )
        if rows:
            stored = getattr(rows[0], "extra_metadata", None)
            metadata = dict(stored) if isinstance(stored, Mapping) else {}
            return str(getattr(rows[0], "id", "") or ""), metadata, False, ""
        return "", {}, False, ""
    except Exception:  # noqa: BLE001  # noqa: silent-ok — absence is reported as unbound evidence
        return "", {}, False, ""


def _recorded_offer_binding(
    *, tenant_id: int, conversation: Any, delivered_ids: Sequence[str],
) -> tuple[str, str]:
    """The offer these action ids name, and the delivery it was RECORDED against.

    ``record_presented_address_offer`` stores ``delivery_ref`` — the
    outbound identity the showing was committed from — alongside the
    offer. Reading it back is what ties the recorded showing to a
    delivery this turn actually captured; without it, "recorded" and
    "delivered" are two independent readings that merely look compatible.
    """
    try:
        from modules.ai.order_flow_v2.checkout_context import (  # noqa: PLC0415
            _OFFER_KEY,
            _OFFER_SET_KEY,
            _conversation_metadata,
            _offered_revisions,
        )

        offer_ids = {
            parts[1]
            for parts in (str(a or "").split(":") for a in delivered_ids)
            if len(parts) == 3
        }
        if len(offer_ids) != 1:
            return "", ""
        offer_id = sorted(offer_ids)[0]
        offered, _identity = _offered_revisions(
            tenant_id=int(tenant_id), conversation=conversation, offer_id=offer_id,
        )
        if not offered:
            return "", ""
        meta = _conversation_metadata(conversation)
        for key in (_OFFER_SET_KEY, _OFFER_KEY):
            stored = meta.get(key)
            if not isinstance(stored, Mapping):
                continue
            if str(stored.get("offer_id") or "") != offer_id:
                continue
            return offer_id, str(stored.get("delivery_ref") or "")
        return offer_id, ""
    except Exception:  # noqa: BLE001  # noqa: silent-ok — absence is reported as unbound evidence
        return "", ""


def _recorded_offer_address_ids(
    *, tenant_id: int, conversation: Any, delivered_ids: Sequence[str],
) -> list[str]:
    """Which addresses the platform RECORDED as shown for this showing.

    Read back through the platform's own reader, keyed by the offer
    identity carried on the ids that actually left, so the comparison is
    "what was recorded" against "what was delivered" rather than two
    readings of the same source.
    """
    try:
        from modules.ai.order_flow_v2.checkout_context import (  # noqa: PLC0415
            _offered_revisions,
            structured_consent_action,
        )

        offer_ids = set()
        for action_id in delivered_ids:
            parts = str(action_id or "").split(":")
            if len(parts) == 3 and structured_consent_action({"button_id": action_id}):
                offer_ids.add(parts[1])
        recorded: set[str] = set()
        for offer_id in sorted(offer_ids):
            offered, _ = _offered_revisions(
                tenant_id=int(tenant_id),
                conversation=conversation,
                offer_id=offer_id,
            )
            recorded.update(str(address_id) for address_id in (offered or {}))
        return sorted(recorded)
    except Exception:  # noqa: BLE001  # noqa: silent-ok — absence is reported as unbound evidence
        return []


def _delivered_address_ids(delivered_ids: Sequence[str]) -> list[str]:
    """The addresses the captured payload actually offered."""
    try:
        from modules.ai.order_flow_v2.checkout_context import (  # noqa: PLC0415
            structured_consent_action,
        )

        out: set[str] = set()
        for action_id in delivered_ids:
            resolved = structured_consent_action({"button_id": action_id})
            if resolved:
                out.add(str(resolved[1]))
        return sorted(out)
    except Exception:  # noqa: BLE001  # noqa: silent-ok — an unreadable id yields no receipt
        return []


def _showing_for_action(
    *,
    tenant_id: int,
    conversation: Any,
    inbound_metadata: Mapping[str, Any],
) -> dict[str, Any]:
    """The live showing this turn's action names, before the turn runs."""
    try:
        from modules.ai.order_flow_v2.checkout_context import (  # noqa: PLC0415
            _offered_revisions,
            structured_consent_action,
        )

        resolved = structured_consent_action(dict(inbound_metadata or {}))
        if not resolved:
            return {}
        offer_id, address_id = resolved
        offered, offer_identity = _offered_revisions(
            tenant_id=int(tenant_id), conversation=conversation, offer_id=str(offer_id),
        )
        return {
            "offer_identity": str(offer_identity or ""),
            "shown_fingerprint": str(offered.get(int(address_id)) or ""),
            "address_id": str(address_id),
        }
    except Exception:  # noqa: BLE001  # noqa: silent-ok — no readable showing proves none
        return {}


def _selection_evidence(
    db: Any,
    *,
    tenant_id: int,
    customer_id: int,
    inbound_metadata: Mapping[str, Any],
    conversation: Any,
    turn_started_at: Any = None,
    showing_before: Optional[Mapping[str, Any]] = None,
) -> dict[str, Any]:
    """What the WRITER did with the action, not what the action looks like.

    Parsing an identifier is not consumption. The previous version
    resolved the action id, then accepted any provenance row already
    marked ``selected`` for that address — so an action naming a showing
    that never existed, replayed at a customer whose address had been
    selected on an earlier turn, produced a clean PASS from a handler
    that ran no owner, consumed no showing and sent nothing.

    The selection owner already produces exactly the evidence this needs
    and publishes it for one turn (``record_turn_address_operation``:
    "the guard boundary cannot be trusted to assemble this; it would then
    be the claimant vouching for itself"). So this reads that, through
    the platform's own reader, and checks it against the showing and the
    durable row:

    * the SHOWING the action names must exist now, in this conversation,
      at this tenant and customer — a superseded or invented offer
      resolves to nothing;
    * the revision recorded as selected must be the revision that was
      SHOWN, not merely some non-empty fingerprint;
    * the writer must have published an operation FOR THIS TURN, naming
      this address, and the durable row must carry that operation's own
      reference;
    * and the publication must have happened during this turn, so an
      operation left by an earlier turn cannot stand in for one that
      never ran. A legitimate idempotent replay republishes, so it still
      passes — no second durable write is demanded.

    Read-only, and every judgement is delegated to the platform helper
    that owns it.
    """
    out: dict[str, Any] = {
        "consumed_action_id": "",
        "action_offer_id": "",
        "action_address_id": "",
        "showing_exists": False,
        "offer_identity": "",
        "shown_fingerprint": "",
        "operation": "",
        "operation_ref": "",
        "operation_address_id": "",
        "operation_fingerprint": "",
        "operation_recorded": False,
        "operation_observed_in_turn": False,
        "selected_address_id": "",
        "selection_state": "",
        "selected_fingerprint": "",
        "selection_source": "",
        "selection_operation_ref": "",
        "selection_matches_action": False,
        "selection_scope_verified": False,
    }
    try:
        from modules.ai.order_flow_v2.checkout_context import (  # noqa: PLC0415
            _TURN_OPERATION_KEY,
            _conversation_metadata,
            _offered_revisions,
            read_turn_address_operation,
            structured_consent_action,
            turn_reference,
        )

        meta = dict(inbound_metadata or {})
        resolved = structured_consent_action(meta)
        if not resolved:
            return out
        offer_id, address_id = resolved
        for key in ("button_id", "list_reply_id", "interactive_reply_id"):
            raw = str(meta.get(key) or "").strip()
            if raw.endswith(f":{address_id}") and offer_id in raw:
                out["consumed_action_id"] = raw
                break
        out["action_offer_id"] = str(offer_id)
        out["action_address_id"] = str(address_id)

        # 1. The showing, as it stood when the action was taken. Resolved
        #    through the platform's own reader, which scopes an offer to
        #    tenant, customer, conversation and its own identity, and
        #    expires it. Resolved BEFORE the turn: consuming a showing
        #    supersedes it, so reading afterwards finds nothing even for
        #    a selection that genuinely happened.
        before = dict(showing_before or {})
        if before:
            out["offer_identity"] = str(before.get("offer_identity") or "")
            shown_fingerprint = str(before.get("shown_fingerprint") or "")
        else:
            offered, offer_identity = _offered_revisions(
                tenant_id=int(tenant_id),
                conversation=conversation,
                offer_id=str(offer_id),
            )
            out["offer_identity"] = str(offer_identity or "")
            shown_fingerprint = str(offered.get(int(address_id)) or "")
        offer_identity = out["offer_identity"]
        out["showing_exists"] = bool(shown_fingerprint)
        out["shown_fingerprint"] = shown_fingerprint

        # 2. The writer's own operation, for this turn.
        writer_turn_ref = turn_reference(meta)
        attempt = read_turn_address_operation(conversation, turn_ref=writer_turn_ref)
        out["operation_recorded"] = bool(getattr(attempt, "is_actionable", False))
        operation = getattr(attempt, "operation", None)
        out["operation"] = str(getattr(operation, "value", "") or "")
        out["operation_ref"] = str(getattr(attempt, "operation_ref", "") or "")
        out["operation_address_id"] = str(getattr(attempt, "address_id", "") or "")
        out["operation_fingerprint"] = str(getattr(attempt, "fingerprint", "") or "")

        # When the writer published it. Production stamps no turn id into
        # a button turn's inbound metadata, so the platform's reader
        # scopes by conversation and TTL; without this an operation from
        # an earlier turn would answer for a turn that ran no owner.
        published_at = _parse_utc(
            (_conversation_metadata(conversation) or {}).get(_TURN_OPERATION_KEY, {}).get(
                "recorded_at"
            )
            if isinstance(
                (_conversation_metadata(conversation) or {}).get(_TURN_OPERATION_KEY),
                Mapping,
            )
            else None
        )
        started = _parse_utc(turn_started_at)
        out["operation_observed_in_turn"] = bool(
            published_at is not None
            and (started is None or published_at >= started)
        )

        # 3. The durable row.
        from models import CustomerAddressProvenance  # noqa: PLC0415

        row = (
            db.query(CustomerAddressProvenance)
            .filter(
                CustomerAddressProvenance.tenant_id == int(tenant_id),
                CustomerAddressProvenance.customer_id == int(customer_id),
                CustomerAddressProvenance.customer_address_id == int(address_id),
            )
            .first()
        )
        if row is None:
            return out
        out["selection_scope_verified"] = bool(
            int(getattr(row, "tenant_id", 0) or 0) == int(tenant_id)
            and int(getattr(row, "customer_id", 0) or 0) == int(customer_id)
        )
        out["selected_address_id"] = str(getattr(row, "customer_address_id", "") or "")
        out["selection_state"] = str(getattr(row, "selection_state", "") or "")
        out["selected_fingerprint"] = str(getattr(row, "selected_fingerprint", "") or "")
        out["selection_source"] = str(getattr(row, "selection_source", "") or "")
        out["selection_operation_ref"] = str(
            getattr(row, "selection_operation_ref", "") or ""
        )

        # 4. The conjunction. Every link, or none.
        out["selection_matches_action"] = bool(
            out["selection_scope_verified"]
            and out["showing_exists"]
            and out["operation_recorded"]
            and out["operation_observed_in_turn"]
            and out["operation_address_id"] == str(address_id)
            and out["operation_fingerprint"] == shown_fingerprint
            # The writer composes its reference from the showing it
            # consumed and the turn it answered.
            and out["operation_ref"] == f"{offer_identity}:{writer_turn_ref}"
            and out["selection_state"] == "selected"
            and out["selected_address_id"] == str(address_id)
            and out["selected_fingerprint"] == shown_fingerprint
            and out["selection_operation_ref"] == out["operation_ref"]
        )
        return out
    except Exception:  # noqa: BLE001  # noqa: silent-ok — absence is reported as unproven selection
        return out


def _utc_now() -> Any:
    from datetime import datetime, timezone  # noqa: PLC0415

    return datetime.now(timezone.utc)


def _parse_utc(raw: Any) -> Any:
    """A UTC datetime, or None. Never raises."""
    from datetime import datetime, timezone  # noqa: PLC0415

    if isinstance(raw, datetime):
        value = raw
    else:
        try:
            value = datetime.fromisoformat(str(raw or ""))
        except (TypeError, ValueError):
            return None
    return value.replace(tzinfo=timezone.utc) if value.tzinfo is None else value.astimezone(
        timezone.utc
    )


def _refusal_evidence(
    *,
    denial_audits: Sequence[Mapping[str, Any]],
    blockers: Sequence[str],
    status: str,
    outbound_meta: Mapping[str, Any],
) -> dict[str, Any]:
    """WHICH gate refused, and why — never merely "nothing was sent".

    A turn that silently did nothing produced the same evidence as a turn
    a gate deliberately stopped, so "not captured" was accepted as proof
    of refusal. A refusal has an author: an egress denial, a suppression
    the runtime recorded in its own provenance, or a handler that raised.
    """
    for audit in denial_audits or []:
        if not isinstance(audit, Mapping):
            continue
        return {
            "gate": f"egress:{audit.get('egress_kind') or ''}",
            "reason": str(audit.get("reason") or "egress_denied"),
            "observed": True,
        }
    meta = dict(outbound_meta or {})
    for key in (
        "address_save_claim_suppress_reason",
        "address_claim_send_suppressed",
        "fallback_reason",
    ):
        if str(meta.get(key) or "").strip() or meta.get(key) is True:
            return {
                "gate": f"provenance:{key}",
                "reason": str(meta.get(key) or key),
                "observed": True,
            }
    if "handler_exception" in set(blockers or []):
        return {
            "gate": "handler_exception",
            "reason": "handler_raised",
            "observed": True,
        }
    return {"gate": "", "reason": "", "observed": False}


async def run_sandbox_of2_turn(
    *,
    db: Any,
    request: SandboxOf2TurnRequest,
    handler: Optional[Callable[..., Any]] = None,
) -> SandboxTurnOutcome:
    """Run one OrderFlowV2 address turn through the real webhook handler."""
    from core.acceptance_compose_observer import (  # noqa: PLC0415
        late_model_bound_arrivals,
        late_model_bound_breakdown,
        model_bound_observation,
        recorded_model_bound_calls,
        seal_model_bound_observation,
    )
    from core.acceptance_execution_context import (  # noqa: PLC0415
        outbound_capture_sink,
    )
    from core.acceptance_failure_injection import (  # noqa: PLC0415
        arm_failure_injection,
        injection_state,
    )
    from services.internal_conversational_e2e_contract import (  # noqa: PLC0415
        address_turn_evidence_blockers,
        classify_execution_path,
        delivered_surface,
    )

    _validate_of2_request(request)
    if handler is None:
        from routers.whatsapp_webhook import (  # noqa: PLC0415
            _handle_merchant_message,
        )

        handler = _handle_merchant_message

    started = time.perf_counter()
    # Wall-clock start, so an address operation published by an EARLIER
    # turn cannot be mistaken for one this turn performed.
    turn_started_at = _utc_now()
    blockers: list[str] = []
    mutations: list[str] = []
    captured: list[Any] = []
    status = "evaluated"

    # One timing accumulator per turn. Without this the direct runner
    # inherits whatever the previous turn left bound, and every latency
    # after the first would carry the one before it.
    timing_token = None
    expected_turn_id = ""
    try:
        from core.turn_latency import (  # noqa: PLC0415
            bind_turn_latency,
            new_turn_latency,
        )

        _accumulator = new_turn_latency(
            tenant_id=int(request.tenant_id),
            conversation_id=int(getattr(request.conversation, "id", 0) or 0) or None,
            message_id=request.turn_ref,
        )
        # Remembered here so the persisted measurement can be compared
        # against THIS turn's accumulator rather than merely checked for
        # having some non-empty turn id.
        expected_turn_id = str(getattr(_accumulator, "turn_id", "") or "")
        timing_token = bind_turn_latency(_accumulator)
    except Exception:  # noqa: BLE001  # noqa: silent-ok — turn latency fail-open
        timing_token = None

    def _probe() -> dict[str, Any]:
        if request.state_probe is None:
            return {}
        try:
            return dict(
                request.state_probe(db, request.tenant_id, request.conversation)
            )
        except Exception:  # noqa: BLE001  # noqa: silent-ok — an unreadable probe is reported as an empty delta
            return {}

    before_state = _probe()
    # The showing the customer's action ANSWERS is the one that existed
    # when they tapped it. Reading it afterwards found nothing, because
    # consuming a showing supersedes it — the selection turn records the
    # next offer over the one it just answered. So it is resolved here,
    # before the handler runs.
    showing_before = _showing_for_action(
        tenant_id=int(request.tenant_id),
        conversation=request.conversation,
        inbound_metadata=request.inbound_metadata,
    )

    with internal_conversational_e2e_context(
        session_id=request.session_id,
        tenant_id=request.tenant_id,
        allow_llm_inference=request.allow_llm_inference,
    ), outbound_capture_sink(captured.append), model_bound_observation(), (
        arm_failure_injection(request.failure_injection)
    ), (
        internal_e2e_sql_error_turn(
            scenario_id=request.scenario_id,
            turn_index=request.turn_index,
        )
    ) as sql_error_scope:
        try:
            await handler(
                request.phone_id,
                request.customer_phone,
                request.text,
                request.tenant_id,
                db,
                dict(request.inbound_metadata),
                None,
                None,
                request.turn_ref,
            )
            mutations.append("sandbox_of2_turn_executed")
        except Exception as exc:  # noqa: BLE001
            status = "handler_exception"
            blockers.append("handler_exception")
            blockers.append(type(exc).__name__.lower()[:48])
        # Close the acceptance cutoff BEFORE reading, so the snapshot is
        # what was accepted rather than whatever happened to have landed
        # by the time the read ran.
        seal_model_bound_observation()
        model_calls = [call.to_dict() for call in recorded_model_bound_calls()]
        late_calls = late_model_bound_arrivals()
        late_detail = dict(late_model_bound_breakdown())
        injection = dict(injection_state())
        turn_sql_error_audit = sql_error_scope.summary
        # ``recorded_egress_denials`` yields EgressDenialAudit dataclasses.
        # ``to_audit_dict`` belongs to the EXCEPTION, not to them, so this
        # serialises the fields the same way the Brain runner does — and
        # a handler that catches a real denial no longer takes the whole
        # turn down with an AttributeError.
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
        try:
            db.commit()
        except Exception:  # noqa: BLE001
            db.rollback()
            blockers.append("sandbox_commit_failed")

    if timing_token is not None:
        try:
            from core.turn_latency import reset_turn_latency  # noqa: PLC0415

            reset_turn_latency(timing_token)
        except Exception:  # noqa: BLE001  # noqa: silent-ok — turn latency fail-open
            pass

    after_state = _probe()
    state_delta = _state_delta(before_state, after_state)

    payload = dict(captured[-1].payload) if captured else {}
    _receipt_action_ids = _delivered_action_ids(payload)
    (
        outbound_message_id,
        outbound_meta,
        outbound_row_verified,
        outbound_body,
    ) = _outbound_row_for_turn(
        db,
        conversation_id=int(getattr(request.conversation, "id", 0) or 0),
        turn_ref=request.turn_ref,
        tenant_id=int(request.tenant_id),
        customer_id=int(getattr(request.conversation, "customer_id", 0) or 0),
    )
    recorded_offer_id, recorded_offer_delivery_ref = _recorded_offer_binding(
        tenant_id=request.tenant_id,
        conversation=request.conversation,
        delivered_ids=_receipt_action_ids,
    )
    _captured_digest = _payload_digest(payload) if captured else ""
    # INDEPENDENT content binding. Hashing the captured payload and then
    # hashing it again proves only that hashing is deterministic; it says
    # nothing about the row the runtime persisted. The comparison that
    # means something is between the text the CAPTURE observed leaving and
    # the body the outbound writer stored, which are produced by two
    # different code paths from the same reply.
    _captured_text, _representation = _captured_customer_text(payload)
    _content = _content_binding(
        captured_text=_captured_text,
        persisted_body=outbound_body,
        representation=_representation,
        captured=bool(captured),
        row_found=bool(outbound_row_verified),
    )

    address_turn = {
        "turn_ref": request.turn_ref,
        # Read from what the runtime PERSISTED. Never defaulted to the
        # requested value — a default would manufacture the very binding
        # this field exists to prove.
        "outbound_metadata_turn_ref": str(
            outbound_meta.get("address_turn_ref") or ""
        ),
        "outbound_message_id": outbound_message_id,
        "outbound_message_row_verified": bool(outbound_row_verified),
        "transport": "captured" if captured else "not_captured",
        "delivery_ids": [str(record.delivery_id) for record in captured],
        "captured_payload_digest": _captured_digest,
        # Two independent artifacts of one reply, compared. The digest is
        # kept for identity; what makes it EVIDENCE is the content match
        # below, against a body this runner never wrote.
        "captured_payload_digest_verified": bool(_content["content_match"]),
        "captured_text_digest": _content["captured_text_digest"],
        "persisted_body_digest": _content["persisted_body_digest"],
        "captured_persisted_representation": _content["representation"],
        "captured_persisted_content_match": bool(_content["content_match"]),
        "content_binding_reason": _content["reason"],
        "recorded_offer_id": recorded_offer_id,
        "recorded_offer_delivery_ref": recorded_offer_delivery_ref,
        # Execution first, presentation second — and never the other way
        # round. A customer with no saved addresses gets an ordinary turn
        # with no choices, which is not a recovery.
        "execution_path": classify_execution_path(outbound_meta),
        "delivered_surface": delivered_surface(payload),
        "failure_injection": injection.get("kind") or "none",
        "injection_state": injection,
        "compose_entered": bool(model_calls),
        "late_model_bound_arrivals": int(late_calls),
        "late_model_bound_breakdown": late_detail,
        "collection_field": str(
            outbound_meta.get("order_flow_v2_last_field")
            or (model_calls[0].get("collection_field") if model_calls else "")
            or ""
        ),
        "model_bound_calls": model_calls,
        "receipt_action_ids": _receipt_action_ids,
        "receipt_address_ids": _delivered_address_ids(_receipt_action_ids),
        "recorded_action_ids": _recorded_offer_address_ids(
            tenant_id=request.tenant_id,
            conversation=request.conversation,
            delivered_ids=_receipt_action_ids,
        ),
        "outbound_provenance": {
            key: outbound_meta[key]
            for key in sorted(outbound_meta)
            if key.startswith(("address_", "compose_", "fallback_", "final_", "llm_"))
        },
        "turn_timing": _timing_projection(outbound_meta.get("turn_timing")),
        # What this runner's OWN accumulator was bound to. A measurement
        # is correlated when it names this turn, not when it merely
        # carries some turn id.
        "turn_timing_expected": {
            "turn_id": str(expected_turn_id or ""),
            "message_id": str(request.turn_ref or ""),
            "conversation_id": int(
                getattr(request.conversation, "id", 0) or 0
            ),
            "tenant_id": int(request.tenant_id),
        },
        # Stated, never implied: an absent measurement is reported as
        # unavailable rather than passed off as a complete record.
        "turn_timing_unavailable": not isinstance(
            outbound_meta.get("turn_timing"), Mapping
        )
        or not outbound_meta.get("turn_timing"),
    }
    selection = _selection_evidence(
        db,
        tenant_id=int(request.tenant_id),
        customer_id=int(getattr(request.conversation, "customer_id", 0) or 0),
        inbound_metadata=request.inbound_metadata,
        conversation=request.conversation,
        turn_started_at=turn_started_at,
        showing_before=showing_before,
    )
    address_turn["selection"] = selection
    blockers.extend(
        address_turn_evidence_blockers(
            address_turn,
            expects_address_turn=request.expects_address_turn,
            expectations=dict(request.expectations or {}),
        )
    )

    # Status is DERIVED, not initialised to success. A handler that
    # returned normally having done nothing is not an evaluated turn, and
    # must not inherit the optimistic value the runner started with.
    if status == "evaluated":
        status = _derived_status(
            captured=bool(captured),
            model_calls=model_calls,
            state_delta=state_delta,
            outbound_row_verified=bool(outbound_row_verified),
        )

    # Every turn declares what it must DO. Address evidence stays optional
    # for a genuine non-collection turn — but "no address reply expected"
    # must not also mean "nothing need be proved".
    refusal = _refusal_evidence(
        denial_audits=denial_audits,
        blockers=blockers,
        status=status,
        outbound_meta=outbound_meta,
    )
    result_blockers = _operational_result_blockers(
        expected=str(request.expected_operational_result or ""),
        status=status,
        captured=bool(captured),
        address_turn=address_turn,
        state_delta=state_delta,
        selection=selection,
        refusal=refusal,
    )
    blockers.extend(result_blockers)

    missing_delta = sorted(
        set(request.expected_state_delta_keys) - set(state_delta or {})
    )
    if missing_delta:
        blockers.append("expected_state_delta_missing")
    # The denials this scenario declared, compared against what happened.
    observed = Counter(
        (str(a.get("egress_kind") or ""), str(a.get("operation") or ""))
        for a in denial_audits
    )
    expected = Counter(request.expected_denials)
    if observed - expected:
        blockers.append("unexpected_egress_denial")
    if expected - observed:
        blockers.append("expected_egress_denial_missing")
    if int(turn_sql_error_audit.get("error_count") or 0):
        blockers.append("runtime_sql_error")

    evidence = {
        "evidence_schema_version": EVIDENCE_SCHEMA_VERSION,
        "evidence_channel": EVIDENCE_CHANNEL,
        "mode": "of2",
        "session_id": request.session_id,
        "scenario_id": request.scenario_id,
        "turn_index": request.turn_index,
        "tenant_id": request.tenant_id,
        "runtime_revision": request.runtime_revision,
        "database_identity_fingerprint": request.database_identity_fingerprint,
        "network_attestation_id": request.network_attestation_id,
        "llm_allowed_hosts": list(request.llm_allowed_hosts),
        "test_phone_hmac": hmac_identifier(
            request.customer_phone, key=request.evidence_hmac_key,
        ),
        "status": status,
        "state_delta": state_delta,
        "expected_state_delta_keys": list(request.expected_state_delta_keys),
        "expected_operational_result": str(request.expected_operational_result or ""),
        "address_turn": address_turn,
        "selection_evidence": selection,
        "refusal_evidence": refusal,
        "denial_audits": denial_audits,
        "expected_denials": [
            {"egress_kind": kind, "operation": operation}
            for kind, operation in sorted(request.expected_denials)
        ],
        "observed_denial_counts": [
            {"egress_kind": kind, "operation": operation, "count": count}
            for (kind, operation), count in sorted(observed.items())
        ],
        "runtime_error_audit": turn_sql_error_audit,
        "captured_outbound": [record.to_audit_dict() for record in captured],
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
    return SandboxTurnOutcome(evidence=evidence, evaluated_status=status)


def _delivered_action_ids(payload: Mapping[str, Any]) -> list[str]:
    try:
        from modules.ai.order_flow_v2.checkout_context import (  # noqa: PLC0415
            delivered_address_action_ids,
        )

        return sorted(str(v) for v in delivered_address_action_ids(dict(payload or {})))
    except Exception:  # noqa: BLE001  # noqa: silent-ok — an unreadable payload yields no receipt
        return []


def _validate_of2_request(request: SandboxOf2TurnRequest) -> None:
    """The same identity gate the Brain runner applies, enforced.

    ``validate_explicit_tenant_id`` RETURNS blockers; it does not raise.
    Calling it and discarding the result left tenant 1 and unallowlisted
    tenants passing this gate, so every blocker it reports is raised here.
    """
    try:
        uuid.UUID(request.session_id)
    except (TypeError, ValueError, AttributeError) as exc:
        raise ValueError("session_id_invalid") from exc
    if not SAFE_SCENARIO_ID_RE.fullmatch(str(request.scenario_id or "")):
        raise ValueError("scenario_id_invalid")
    if type(request.turn_index) is not int or request.turn_index < 0:
        raise ValueError("turn_index_invalid")
    tenant_blockers = validate_explicit_tenant_id(
        request.tenant_id,
        request.allowed_tenants,
    )
    if tenant_blockers:
        raise ValueError(tenant_blockers[0])
    # The conversation this turn runs in must belong to the tenant the
    # context authorises, or the turn would write into another tenant's
    # conversation under this tenant's authority.
    if int(getattr(request.conversation, "tenant_id", 0) or 0) != request.tenant_id:
        raise ValueError("conversation_tenant_mismatch")
    if not str(request.customer_phone or "").strip() or not str(request.text or "").strip():
        raise ValueError("turn_input_invalid")
    if not SAFE_AUDIT_VALUE_RE.fullmatch(str(request.turn_ref or "")):
        raise ValueError("turn_ref_invalid")
    if not str(request.evidence_hmac_key or ""):
        raise ValueError("evidence_hmac_key_missing")
    expected_denials: set[tuple[str, str]] = set()
    for event in request.expected_denials:
        if (
            not isinstance(event, tuple)
            or len(event) != 2
            or event[0] not in EGRESS_DENIAL_KINDS
            or not isinstance(event[1], str)
            or not SAFE_AUDIT_VALUE_RE.fullmatch(event[1])
        ):
            raise ValueError("expected_denials_invalid")
        expected_denials.add((event[0], event[1]))
    if len(expected_denials) != len(request.expected_denials):
        raise ValueError("expected_denials_invalid")
    if request.failure_injection not in FAILURE_INJECTIONS:
        raise ValueError("failure_injection_invalid")
    if not request.allow_llm_inference:
        raise ValueError("llm_inference_not_explicitly_enabled")
    if (
        not request.runtime_revision
        or not str(request.database_identity_fingerprint or "").startswith("sha256:")
        or not request.network_attestation_id
        or not request.llm_allowed_hosts
    ):
        raise ValueError("sandbox_execution_attestation_incomplete")

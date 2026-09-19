"""One inbound turn, end to end, owned by the commerce runtime.

    admit → claim → reason (loop) → dispatch → complete → release

Nahla owns every one of those steps. The provider contributes one inference
step at a time, the tools contribute read-only observations, and the transport
contributes one send; none of them decides whether the turn continues, whether
the reply is acceptable, or whether the turn is finished.

Two properties this module exists to keep:

* **One reply per inbound message.** The inbound provider message id is the
  admission identity, so a redelivered webhook resolves to the same turn. That
  turn's delivery intent is reserved once; a re-entry reuses the existing
  reservation instead of composing a second answer, and the ledger refuses a
  second dispatch of an attempt whose outcome is pending, accepted or unknown.
* **No success claimed after a failed or unknown send.** The terminal's
  processing outcome follows the recorded receipt, and its transport outcome
  and customer reach are derived by the ledger from the receipts themselves.

No commerce write happens anywhere on this path.
"""
from __future__ import annotations

import dataclasses
import logging
import threading
import time
import uuid
from typing import Any, Dict, Mapping, Optional, Tuple

from core.commerce_runtime import agent_contracts as ac
from core.commerce_runtime import agent_live_tools as alt
from core.commerce_runtime import agent_provider as ap
from core.commerce_runtime import contracts as c
from core.commerce_runtime import delivery_dispatch as dd
from core.commerce_runtime import ledger_contracts as lc
from core.commerce_runtime.agent_loop import AgentLoop
from core.commerce_runtime.ledgers import LedgerRepository

logger = logging.getLogger("nahla.commerce_runtime.runtime_entry")

NAMESPACE = c.Namespace.LIVE.value
LEASE_SECONDS = 180
OWNER_PREFIX = "commerce-runtime-pilot"

# Closed outcomes of one entry call.
HANDLED = "handled"
SCHEMA_UNAVAILABLE = "runtime_schema_unavailable"
ALREADY_TERMINAL = "turn_already_terminal"
OWNERSHIP_UNAVAILABLE = "ownership_unavailable"
CONTEXT_UNAVAILABLE = "trusted_context_unavailable"
ADMISSION_CONFLICT = "admission_conflict"
INTERNAL_ERROR = "internal_error"

_schema_state: Dict[int, str] = {}
_schema_lock = threading.Lock()


@dataclasses.dataclass(frozen=True)
class TurnReport:
    """Everything one inbound turn did, for the single end-to-end log line."""

    reason: str
    tenant_id: int
    conversation_id: int
    turn_id: Optional[int] = None
    duplicate_inbound: bool = False
    loop_status: Optional[str] = None
    stop_reason: Optional[str] = None
    delivery_sequence_id: Optional[int] = None
    reused_delivery: bool = False
    dispatch_status: Optional[str] = None
    provider_message_id: Optional[str] = None
    processing_outcome: Optional[str] = None
    transport_outcome: Optional[str] = None
    customer_reach: Optional[str] = None
    steps_used: int = 0
    tool_calls_used: int = 0
    tools_called: Tuple[str, ...] = ()
    evidence_refs: Tuple[str, ...] = ()
    input_tokens: Optional[int] = None
    output_tokens: Optional[int] = None
    model: Optional[str] = None
    reply_text: str = ""
    latency_ms: Optional[int] = None
    owner_id: Optional[str] = None

    @property
    def replied(self) -> bool:
        """True only when a send the provider itself identified was accepted."""
        return self.dispatch_status == dd.SENT_ACCEPTED and bool(self.provider_message_id)

    def as_log_fields(self) -> Dict[str, Any]:
        fields = dataclasses.asdict(self)
        fields["tools_called"] = ",".join(self.tools_called)
        fields["evidence_refs"] = ",".join(self.evidence_refs)
        fields["replied"] = self.replied
        fields["reply_chars"] = len(self.reply_text)
        fields.pop("reply_text", None)      # the log line records length, never the customer's text
        return fields


def runtime_schema_available(engine: Any) -> bool:
    """Whether this database actually holds the commerce runtime's tables.

    The migrations that create them are merged but are not part of the normal
    bootstrap target, so a database may legitimately not have them. Probing once
    per engine keeps a disabled pilot from paying for the check on every turn,
    and an incomplete schema counts as unavailable — the runtime never runs
    half-present.
    """
    key = id(engine)
    with _schema_lock:
        cached = _schema_state.get(key)
    if cached is not None:
        return cached == "complete"
    from core.commerce_runtime.repositories import CommerceRuntimeRepository  # noqa: PLC0415

    state = "absent"
    try:
        with engine.connect() as conn:
            core_present = conn.exec_driver_sql(
                "SELECT to_regclass('public.commerce_runtime_turns') IS NOT NULL"
            ).scalar()
            ledger_state, _present, _missing = CommerceRuntimeRepository._ledger_schema_state(conn)
        state = "complete" if (core_present and ledger_state == "complete") else ledger_state
    except Exception as exc:  # noqa: BLE001 - an unprobeable schema is not an available one
        logger.warning("[COMMERCE_RUNTIME] schema probe failed error=%s", type(exc).__name__)
        state = "absent"
    with _schema_lock:
        _schema_state[key] = state
    logger.info("[COMMERCE_RUNTIME] schema probe state=%s", state)
    return state == "complete"


def reset_schema_probe() -> None:
    """Forget every probed engine. For tests and for an operator re-check."""
    with _schema_lock:
        _schema_state.clear()


def build_trusted_binding(
    *,
    session_factory: Any,
    tenant_id: int,
    conversation_id: int,
    customer_id: Optional[int],
    normalized_customer_phone: str,
    connection_id: str,
    inbound_trace_id: str,
) -> Tuple[Optional[alt.LiveToolBinding], Optional[Any]]:
    """One trusted read context on its own session, or nothing at all.

    The session belongs to the tool binding and to no request: a tool call the
    loop abandons must never be left sharing a session with the webhook.
    """
    from modules.ai.commerce_agent_v2.context import CommerceAgentContext  # noqa: PLC0415

    session = session_factory()
    try:
        context = CommerceAgentContext.from_trusted_scope(
            db=session,
            tenant_id=int(tenant_id),
            conversation_id=int(conversation_id),
            customer_id=int(customer_id) if customer_id else None,
            normalized_customer_phone=normalized_customer_phone,
            connection_id=connection_id,
            inbound_trace_id=inbound_trace_id,
        )
    except Exception as exc:  # noqa: BLE001 - without a trusted scope there are no tools
        logger.warning("[COMMERCE_RUNTIME] trusted context unavailable tenant=%s error=%s",
                       tenant_id, type(exc).__name__)
        _close_quietly(session)
        return None, None
    binding = alt.LiveToolBinding(context=context, tenant_id=int(tenant_id),
                                  conversation_id=int(conversation_id))
    return binding, session


def _close_quietly(session: Any) -> None:
    try:
        session.close()
    except Exception:  # noqa: BLE001 - a session we can no longer trust is simply dropped
        logger.warning("[COMMERCE_RUNTIME] tool session could not be closed cleanly")


def run_commerce_runtime_turn(
    *,
    engine: Any,
    session_factory: Any,
    tenant_id: int,
    conversation_id: int,
    conversation_ref: str,
    connection_ref: str,
    connection_id: str,
    customer_id: Optional[int],
    normalized_customer_phone: str,
    provider_message_id: str,
    inbound_text: str,
    inbound_metadata: Optional[Mapping[str, Any]],
    transport: dd.Transport,
    instructions: str,
    budget: Optional[ac.LoopBudget] = None,
    context_preamble: Optional[Mapping[str, Any]] = None,
    history: Optional[Any] = None,
    anthropic_provider: Optional[Any] = None,
) -> TurnReport:
    """Run one admitted inbound turn to a recorded transport outcome."""
    started = time.monotonic()
    base = {"tenant_id": int(tenant_id), "conversation_id": int(conversation_id)}
    if not runtime_schema_available(engine):
        return TurnReport(reason=SCHEMA_UNAVAILABLE, **base)

    ledgers = LedgerRepository(engine)
    foundation = ledgers.foundation
    owner_id = f"{OWNER_PREFIX}:{uuid.uuid4().hex[:16]}"

    try:
        admitted = foundation.admit_turn(
            tenant_id=tenant_id, namespace=NAMESPACE, conversation_ref=conversation_ref,
            channel_connection_ref=connection_ref, provider_message_id=provider_message_id,
            payload={"text": inbound_text, "metadata": dict(inbound_metadata or {})},
        )
    except c.CommerceRuntimeError as exc:
        logger.warning("[COMMERCE_RUNTIME] admission refused tenant=%s error=%s",
                       tenant_id, type(exc).__name__)
        return TurnReport(reason=ADMISSION_CONFLICT, **base)

    turn_id = admitted.turn_id
    runtime_conversation_id = admitted.conversation_id
    report_base = dict(base, turn_id=turn_id, duplicate_inbound=bool(admitted.duplicate),
                       owner_id=owner_id)

    existing = foundation.get_terminal(tenant_id=tenant_id, namespace=NAMESPACE, turn_id=turn_id)
    if existing is not None:
        # The turn is finished. A redelivered webhook must not answer again.
        return TurnReport(reason=ALREADY_TERMINAL,
                          processing_outcome=existing.processing_outcome,
                          transport_outcome=existing.transport_outcome,
                          customer_reach=existing.customer_reach, **report_base)

    try:
        lease = foundation.claim(tenant_id=tenant_id, namespace=NAMESPACE,
                                 conversation_id=runtime_conversation_id, owner_id=owner_id,
                                 lease_seconds=LEASE_SECONDS, turn_id=turn_id)
    except c.CommerceRuntimeError as exc:
        # Another invocation holds the turn, or the turn is no longer eligible.
        logger.info("[COMMERCE_RUNTIME] ownership unavailable turn=%s reason=%s",
                    turn_id, getattr(exc, "reason", None) or type(exc).__name__)
        return TurnReport(reason=OWNERSHIP_UNAVAILABLE, **report_base)

    token = c.OwnershipToken(owner_id=owner_id, fence=lease.fence, epoch=lease.epoch,
                             tenant_id=int(tenant_id), namespace=NAMESPACE,
                             conversation_id=int(runtime_conversation_id))
    binding, session = build_trusted_binding(
        session_factory=session_factory, tenant_id=tenant_id,
        conversation_id=conversation_id, customer_id=customer_id,
        normalized_customer_phone=normalized_customer_phone,
        connection_id=connection_id, inbound_trace_id=provider_message_id,
    )
    if binding is None:
        # Without a trusted scope there is nothing to read and nothing to say.
        # The turn is finished as failed rather than left eligible: an open turn
        # would block every later turn in this conversation, and silence with no
        # record is the one outcome this runtime must never produce.
        _fail_turn(ledgers, tenant_id, turn_id, token, reason=CONTEXT_UNAVAILABLE)
        _release(foundation, tenant_id, runtime_conversation_id, token)
        return TurnReport(reason=CONTEXT_UNAVAILABLE, **report_base)

    reasoner: Optional[ap.AnthropicReasoningProvider] = None
    try:
        from modules.ai.orchestrator.providers.anthropic_provider import (  # noqa: PLC0415
            AnthropicProvider,
        )

        registry = alt.build_live_registry(binding)
        reasoner = ap.AnthropicReasoningProvider(
            instructions=instructions,
            tools_provider=anthropic_provider or AnthropicProvider(),
            audit_context={"tenant_id": int(tenant_id), "conversation_id": int(conversation_id),
                           "turn_id": int(turn_id), "channel": "whatsapp",
                           "reason": "commerce_runtime_pilot"},
            context_preamble=context_preamble,
            history=history,
        )
        loop = AgentLoop(ledgers, registry, budget=budget)
        outcome = loop.run_turn(
            tenant_id=tenant_id, namespace=NAMESPACE, conversation_id=runtime_conversation_id,
            turn_id=turn_id, token=token, provider=reasoner,
        )
        report = _after_loop(ledgers=ledgers, outcome=outcome, tenant_id=tenant_id,
                             runtime_conversation_id=runtime_conversation_id, token=token,
                             transport=transport, owner_id=owner_id, report_base=report_base,
                             reasoner=reasoner)
    except Exception as exc:  # noqa: BLE001 - one turn's failure is never the worker's
        logger.exception("[COMMERCE_RUNTIME] turn failed turn=%s error=%s", turn_id,
                         type(exc).__name__)
        # Finish it as failed when the ledgers allow. They refuse while a
        # reserved delivery has not been dispatched, and that refusal is right:
        # such a turn is not finished and a re-entry must still be able to
        # dispatch what was already reserved.
        _fail_turn(ledgers, tenant_id, turn_id, token, reason=INTERNAL_ERROR,
                   error=type(exc).__name__)
        report = TurnReport(reason=INTERNAL_ERROR, **report_base)
    finally:
        if binding.poisoned is None:
            _close_quietly(session)
        else:
            # A tool call was abandoned and may still be using this session.
            logger.warning("[COMMERCE_RUNTIME] leaving an abandoned tool session to the pool")
        _release(foundation, tenant_id, runtime_conversation_id, token)

    return dataclasses.replace(report, latency_ms=int((time.monotonic() - started) * 1000))


def _after_loop(*, ledgers: LedgerRepository, outcome: ac.LoopOutcome, tenant_id: int,
                runtime_conversation_id: int, token: c.OwnershipToken, transport: dd.Transport,
                owner_id: str, report_base: Mapping[str, Any],
                reasoner: ap.AnthropicReasoningProvider) -> TurnReport:
    """Dispatch what the loop reserved, then record the turn's single terminal."""
    turn_id = int(report_base["turn_id"])
    tools_called = tuple(event.detail.get("tool", "") for event in outcome.events
                         if event.kind == "tool_observation")
    evidence = tuple(str(ref) for ref in (outcome.detail.get("evidence_refs") or ()))
    usage_model = next((u.model for u in reversed(reasoner.usage) if u.model), None)
    common = dict(
        report_base,
        loop_status=outcome.status,
        stop_reason=outcome.stop_reason,
        delivery_sequence_id=outcome.delivery_sequence_id,
        reused_delivery=outcome.reused_delivery,
        steps_used=outcome.steps_used,
        tool_calls_used=outcome.tool_calls_used,
        tools_called=tools_called,
        evidence_refs=evidence,
        input_tokens=reasoner.total_input_tokens,
        output_tokens=reasoner.total_output_tokens,
        model=usage_model,
    )

    if outcome.status != ac.LoopStatus.PENDING_DELIVERY.value or outcome.delivery_sequence_id is None:
        # The loop stopped without an accepted reply. Nothing was reserved, so
        # nothing is sent and the turn is finished as a failure, not a success.
        terminal = dd.complete_turn(
            ledgers=ledgers, tenant_id=tenant_id, namespace=NAMESPACE, turn_id=turn_id, token=token,
            processing_outcome=c.ProcessingOutcome.FAILED.value,
            details={"stop_reason": outcome.stop_reason or "unknown", "source": "commerce_runtime_pilot"},
        )
        return TurnReport(reason=HANDLED, dispatch_status=None,
                          processing_outcome=getattr(terminal, "processing_outcome", None),
                          transport_outcome=getattr(terminal, "transport_outcome", None),
                          customer_reach=getattr(terminal, "customer_reach", None), **common)

    sequence = ledgers.get_delivery_sequence(tenant_id=tenant_id, namespace=NAMESPACE, turn_id=turn_id)
    # The text that is dispatched is the ledger's stored intent, read back rather
    # than rebuilt, so what is persisted and logged is what actually went out.
    common["reply_text"] = str((getattr(sequence, "intent_payload", None) or {}).get("text") or "")
    dispatch = dd.dispatch_reserved_delivery(
        ledgers=ledgers, tenant_id=tenant_id, namespace=NAMESPACE,
        conversation_id=runtime_conversation_id, token=token,
        sequence_id=outcome.delivery_sequence_id, transport=transport, recorded_by=owner_id,
    )
    terminal = dd.complete_turn(
        ledgers=ledgers, tenant_id=tenant_id, namespace=NAMESPACE, turn_id=turn_id, token=token,
        processing_outcome=dispatch.processing_outcome,
        details={"source": "commerce_runtime_pilot", "dispatch_status": dispatch.status,
                 **({"blocked": dispatch.blocked_reason} if dispatch.blocked_reason else {})},
    )
    return TurnReport(reason=HANDLED, dispatch_status=dispatch.status,
                      provider_message_id=dispatch.provider_message_id,
                      processing_outcome=getattr(terminal, "processing_outcome", None),
                      transport_outcome=getattr(terminal, "transport_outcome", None),
                      customer_reach=getattr(terminal, "customer_reach", None), **common)


def _fail_turn(ledgers: LedgerRepository, tenant_id: int, turn_id: int, token: c.OwnershipToken,
               *, reason: str, **detail: Any) -> None:
    """Record a failed terminal for a turn that could not run. Never raises."""
    try:
        dd.complete_turn(ledgers=ledgers, tenant_id=tenant_id, namespace=NAMESPACE, turn_id=turn_id,
                         token=token, processing_outcome=c.ProcessingOutcome.FAILED.value,
                         details={"source": "commerce_runtime_pilot", "reason": reason, **detail})
    except Exception as exc:  # noqa: BLE001 - the lease expires; the turn stays eligible for re-entry
        logger.warning("[COMMERCE_RUNTIME] could not record the failed terminal turn=%s error=%s",
                       turn_id, type(exc).__name__)


def _release(foundation: Any, tenant_id: int, conversation_id: int, token: c.OwnershipToken) -> None:
    try:
        foundation.release(tenant_id=tenant_id, namespace=NAMESPACE,
                           conversation_id=conversation_id, token=token)
    except Exception as exc:  # noqa: BLE001 - the lease expires on its own; never fail a turn on release
        logger.info("[COMMERCE_RUNTIME] lease release skipped reason=%s", type(exc).__name__)


def whatsapp_text_transport(send: Any, *, recipient: str) -> dd.Transport:
    """Adapt a WhatsApp text sender to the ledger's transport contract.

    ``send`` is handed the reply text and must report what the provider said as
    ``(classification, wamid, http_status)``. Only a classification of ``ok``
    with a provider message id becomes an accepted send; everything else is
    reported as it was, so the ledger can decide.
    """
    def transport(payload: Mapping[str, Any]) -> lc.SendResponse:
        text = str(payload.get("text") or "")
        classification, wamid, http_status = send(recipient, text)
        if classification == "ok" and wamid:
            return lc.SendResponse(http_status=int(http_status or 200),
                                   body={"messages": [{"id": str(wamid)}]})
        if http_status is not None:
            return lc.SendResponse(http_status=int(http_status),
                                   body={"error": {"code": str(classification or "unknown")}})
        # No status at all: the send is unknown, never a proven rejection.
        return lc.SendResponse(http_status=None, body={}, timed_out=True)

    return transport


__all__ = [
    "ADMISSION_CONFLICT", "ALREADY_TERMINAL", "CONTEXT_UNAVAILABLE", "HANDLED", "INTERNAL_ERROR",
    "LEASE_SECONDS", "NAMESPACE", "OWNERSHIP_UNAVAILABLE", "SCHEMA_UNAVAILABLE", "TurnReport",
    "build_trusted_binding", "reset_schema_probe", "run_commerce_runtime_turn",
    "runtime_schema_available", "whatsapp_text_transport",
]

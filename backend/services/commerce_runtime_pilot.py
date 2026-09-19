"""The one place the WhatsApp webhook can hand a turn to the commerce runtime.

The webhook asks this module once, before the legacy brain runs. Exactly one
runtime then owns the turn:

* ``handled=False`` — the legacy path runs unchanged, as it does today.
* ``handled=True``  — the legacy path must return immediately; the commerce
  runtime has taken the turn and is the only thing that may answer it.

There is no third answer and no fall-through: a commerce-runtime turn that
fails does **not** hand the customer back to the legacy brain mid-turn, because
both would then be answering the same inbound message. A failure is recorded as
a failed turn, honestly, and the customer is not told anything the runtime
cannot back up.

No customer-facing wording is composed here. The text is the model's, carried
through the loop's verification and the delivery ledger; this module only moves
it to the established WhatsApp transport and records what the transport said.
"""
from __future__ import annotations

import asyncio
import dataclasses
import logging
from typing import Any, Dict, Optional, Tuple

from services.turn_trace import SOURCE_COMMERCE_RUNTIME as TRACE_SOURCE

logger = logging.getLogger("nahla.commerce_runtime.pilot")

BLOCKED_PATH = "commerce_runtime_pilot"


@dataclasses.dataclass(frozen=True)
class PilotResult:
    handled: bool
    reason: str
    report: Optional[Any] = None


def _instructions() -> str:
    from modules.ai.commerce_agent_v2.agent import COMMERCE_AGENT_INSTRUCTIONS  # noqa: PLC0415

    return COMMERCE_AGENT_INSTRUCTIONS


def _context_preamble(convo: Any, customer_name: str) -> Dict[str, Any]:
    """Trusted facts the platform hands the model as data, never as wording."""
    preamble: Dict[str, Any] = {"channel": "whatsapp"}
    name = str(customer_name or "").strip()
    if name:
        preamble["verified_customer_name"] = name
    language = str(getattr(convo, "language", "") or "").strip()
    if language:
        preamble["conversation_language"] = language
    return preamble


def _send_factory(phone_id: str, tenant_id: int, db: Any, loop: Any) -> Any:
    """A synchronous view of the established WhatsApp text sender.

    ``_post_wa`` is async and lives in the webhook router. The commerce runtime
    turn runs in a worker thread, so the send is scheduled back onto the running
    event loop and waited for there. Every existing guard on that path — the
    sanitiser, the AI-disabled gate, the burst throttle, the outbound dedup —
    still applies; this adds none and removes none.
    """
    def send(recipient: str, text: str) -> Tuple[str, Optional[str], Optional[int]]:
        from routers.whatsapp_webhook import _send_whatsapp_message  # noqa: PLC0415

        sink: Dict[str, Any] = {}
        future = asyncio.run_coroutine_threadsafe(
            _send_whatsapp_message(
                phone_id=phone_id, to=recipient, text=text,
                _tenant_id=int(tenant_id), _db=db, _blocked_path=BLOCKED_PATH,
                _result_sink=sink,
            ),
            loop,
        )
        ok = bool(future.result())
        classification = str(sink.get("classification") or ("ok" if ok else "blocked"))
        wamid = sink.get("wamid")
        status = sink.get("http_status")
        if not ok and classification == "ok":
            # The transport reported success but the send path refused it: a
            # refusal, not an acceptance, and never an unknown.
            classification = "blocked"
            wamid = None
            status = status if status is not None else 400
        return classification, (str(wamid) if wamid else None), (int(status) if status is not None else None)

    return send


async def maybe_handle_with_commerce_runtime(
    *,
    db: Any,
    tenant_id: int,
    phone_id: str,
    to: str,
    text: str,
    convo: Any,
    wa_msg_id: Optional[str],
    inbound_metadata: Optional[Dict[str, Any]],
    trace: Any,
    legacy_already_answered: bool,
    ai_gate_skipped: bool,
    customer_name: str = "",
) -> PilotResult:
    """Route this inbound turn, and run it here when the pilot owns it."""
    from core.commerce_runtime import pilot_guard  # noqa: PLC0415

    decision = pilot_guard.evaluate_pilot_route(
        db, tenant_id=tenant_id, customer_phone=to, phone_number_id=phone_id,
        inbound_text=text, legacy_already_answered=legacy_already_answered,
        ai_gate_skipped=ai_gate_skipped,
    )
    if not decision.permitted:
        if decision.reason not in {pilot_guard.PILOT_DISABLED, pilot_guard.TENANT_NOT_ALLOWLISTED}:
            logger.info("[COMMERCE_RUNTIME_PILOT] route=legacy tenant=%s reason=%s",
                        tenant_id, decision.reason)
        return PilotResult(handled=False, reason=decision.reason)

    # From here the commerce runtime owns the turn. Nothing below may raise back
    # to the caller: if it did, the legacy brain would run for an inbound message
    # this runtime has already (possibly) answered.
    try:
        return await _own_turn(
            db=db, tenant_id=int(tenant_id), phone_id=phone_id, to=to, text=text, convo=convo,
            wa_msg_id=wa_msg_id, inbound_metadata=inbound_metadata, trace=trace,
            decision=decision, customer_name=customer_name,
        )
    except Exception:  # noqa: BLE001 - the turn stays ours; it is simply a failed turn
        logger.exception("[COMMERCE_RUNTIME_PILOT] turn failed after the route was taken tenant=%s",
                         tenant_id)
        return PilotResult(handled=True, reason="internal_error")


async def _own_turn(
    *,
    db: Any,
    tenant_id: int,
    phone_id: str,
    to: str,
    text: str,
    convo: Any,
    wa_msg_id: Optional[str],
    inbound_metadata: Optional[Dict[str, Any]],
    trace: Any,
    decision: Any,
    customer_name: str,
) -> PilotResult:
    from core.commerce_runtime import pilot_guard  # noqa: PLC0415
    from core.commerce_runtime import runtime_entry as entry  # noqa: PLC0415
    from database.session import SessionLocal, engine  # noqa: PLC0415

    conversation_id = int(getattr(convo, "id", 0) or 0)
    provider_message_id = str(wa_msg_id or "").strip() or f"conversation-{conversation_id}:no-wamid"
    loop = asyncio.get_running_loop()
    send = _send_factory(phone_id, int(tenant_id), db, loop)

    def run() -> Any:
        return entry.run_commerce_runtime_turn(
            engine=engine,
            session_factory=SessionLocal,
            tenant_id=int(tenant_id),
            conversation_id=conversation_id,
            conversation_ref=f"wa:{decision.recipient}:{conversation_id}",
            connection_ref=str(decision.connection_ref),
            connection_id=str(decision.connection_id),
            customer_id=getattr(convo, "customer_id", None),
            normalized_customer_phone=str(decision.recipient),
            provider_message_id=provider_message_id,
            inbound_text=text,
            inbound_metadata=inbound_metadata,
            transport=entry.whatsapp_text_transport(send, recipient=str(decision.recipient)),
            instructions=_instructions(),
            budget=pilot_guard.pilot_budget(),
            context_preamble=_context_preamble(convo, customer_name),
        )

    report = await asyncio.to_thread(run)
    _record(db=db, trace=trace, convo=convo, tenant_id=int(tenant_id), to=to, report=report)
    logger.info("[COMMERCE_RUNTIME_PILOT] route=commerce_runtime %s", report.as_log_fields())
    return PilotResult(handled=True, reason=report.reason, report=report)


def _record(*, db: Any, trace: Any, convo: Any, tenant_id: int, to: str, report: Any) -> None:
    """Persist and trace an accepted send. Nothing is claimed that did not happen."""
    if not report.replied:
        return
    text = str(getattr(report, "reply_text", "") or "")
    try:
        trace.mark_outbound_sent(source=TRACE_SOURCE, length=len(text))
    except Exception:  # noqa: BLE001 - tracing must not change what happened
        logger.warning("[COMMERCE_RUNTIME_PILOT] trace mark failed turn=%s", report.turn_id)
    if not text:
        logger.warning("[COMMERCE_RUNTIME_PILOT] accepted send without a readable text turn=%s",
                       report.turn_id)
        return
    try:
        from core.conversation_engine import StateManager  # noqa: PLC0415

        StateManager.save_message(
            db, to, text, "outbound",
            conversation_id=getattr(convo, "id", None), tenant_id=tenant_id,
            extra_metadata={
                "compose_source": "llm",
                "response_mode": "grounded",
                "chosen_path": "commerce_runtime_pilot",
                "llm_candidate_present": True,
                "final_text_transformed": False,
                "final_transform_reasons": [],
                "commerce_runtime_turn_id": report.turn_id,
                "commerce_runtime_delivery_sequence_id": report.delivery_sequence_id,
                "provider_message_id": report.provider_message_id,
                "evidence_refs": list(report.evidence_refs),
            },
        )
    except Exception:  # noqa: BLE001 - the send already happened; persistence must not undo it
        logger.exception("[COMMERCE_RUNTIME_PILOT] outbound persist failed turn=%s", report.turn_id)


__all__ = ["BLOCKED_PATH", "PilotResult", "TRACE_SOURCE", "maybe_handle_with_commerce_runtime"]

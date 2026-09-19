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
import concurrent.futures
import dataclasses
import hashlib
import logging
from typing import Any, Dict, List, Optional, Sequence, Tuple

from services.turn_trace import SOURCE_COMMERCE_RUNTIME as TRACE_SOURCE

logger = logging.getLogger("nahla.commerce_runtime.pilot")

BLOCKED_PATH = "commerce_runtime_pilot"

# The transport has its own timeout; this is the outer bound on waiting for it,
# so one turn can never hold the webhook request open indefinitely.
SEND_WAIT_SECONDS = 45.0

# The same window the platform already shows the model on the legacy path.
HISTORY_LIMIT = 15

# Recorded when the send path's transmitted text could not be observed. It is
# never recorded as *unchanged*: an unobserved wire is unknown, not clean.
WIRE_UNOBSERVED = "wire_text_unobserved"

# Prefixes the reason when a refused turn is nevertheless kept, because this
# runtime owns that exact inbound message: ``unfinished_`` when it still has
# work to do on it, ``owned_`` when it already finished it.
UNFINISHED_PREFIX = "unfinished_"
OWNED_PREFIX = "owned_"

# Refusals that mean the runtime was never in this conversation at all. Asking
# whether it holds unfinished work would be a query for every inbound message
# on the platform, so these answer without one.
NO_RUNTIME_WORK_POSSIBLE = frozenset({"pilot_disabled", "tenant_not_allowlisted"})


@dataclasses.dataclass(frozen=True)
class PilotResult:
    handled: bool
    reason: str
    report: Optional[Any] = None


@dataclasses.dataclass
class WireObservation:
    """What the send path actually transmitted, as opposed to what was reserved.

    The delivery ledger holds the reply's **intent**: the text the loop verified
    and reserved, which never changes. What reaches the customer is whatever the
    established send path transmits after its own sanitiser and guards have run,
    and the two are not always the same message.

    This records the transmitted one so the conversation the platform stores —
    and therefore the history the next turn reads — is what was really said.
    """

    observed: bool = False
    text: str = ""
    reasons: List[str] = dataclasses.field(default_factory=list)
    duplicate_suppressed: bool = False

    def record(self, text: str, reasons: Sequence[str], *, duplicate_suppressed: bool) -> None:
        self.observed = True
        self.text = str(text or "")
        self.reasons = [str(reason) for reason in reasons]
        self.duplicate_suppressed = bool(duplicate_suppressed)

    def resolve(self, intent: str) -> Tuple[str, bool, List[str]]:
        """``(text_to_store, transformed, reasons)`` for one accepted send."""
        if not self.observed:
            # Not observed is not unchanged. Say so rather than certify a text
            # nobody read back.
            return str(intent or ""), True, [WIRE_UNOBSERVED]
        reasons = list(self.reasons)
        if self.text != str(intent or "") and not reasons:
            reasons = ["wire_text_differs_from_reserved_intent"]
        return self.text, bool(reasons), reasons


def _instructions() -> str:
    """The merchant instructions plus the documented pilot-only correction."""
    from modules.ai.commerce_agent_v2.pilot_instructions import (  # noqa: PLC0415
        build_pilot_instructions,
    )

    return build_pilot_instructions()


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


def _wire_unobserved(metadata: Any) -> bool:
    """Whether this runtime recorded that it could not read the wire back.

    Only rows this runtime wrote carry the marker. Anything else — a legacy
    row, another path's row — is not this runtime's to judge and is left alone.
    """
    meta = metadata if isinstance(metadata, dict) else {}
    if meta.get("chosen_path") != BLOCKED_PATH:
        return False
    if meta.get("commerce_runtime_wire_observed") is False:
        return True
    reasons = meta.get("final_transform_reasons")
    return isinstance(reasons, list) and WIRE_UNOBSERVED in reasons


def _phone_variants(phone: str) -> Tuple[str, ...]:
    from services.customer_intelligence import normalize_phone  # noqa: PLC0415

    raw = str(phone or "").strip()
    normalized = str(normalize_phone(raw) or raw).strip()
    return tuple({p for p in (raw, normalized, normalized.lstrip("+")) if p})


def _phone_is_unambiguous(db: Any, *, tenant_id: int, conversation_id: int,
                          phones: Tuple[str, ...]) -> bool:
    """Whether this number belongs to exactly one conversation in this tenant.

    Messages written before conversation linking carry no conversation id and
    can only be attributed by the number they were delivered to. That is safe
    only where the number is *established* to name this one conversation and no
    other. Two other answers are both "not established" and both exclude the
    unlinked rows: several conversations carry the number, or — just as
    important — none does, because a conversation whose number lives only in
    ``external_id`` produces no association row at all. Finding no evidence of
    an association is not evidence of a unique one.
    """
    if not phones:
        return False
    from models import Conversation, Customer  # noqa: PLC0415
    from sqlalchemy import or_  # noqa: PLC0415

    rows = (
        db.query(Conversation.id)
        .outerjoin(Customer, Conversation.customer_id == Customer.id)
        .filter(
            Conversation.tenant_id == int(tenant_id),
            or_(
                Customer.normalized_phone.in_(phones),
                Customer.phone.in_(phones),
                Conversation.extra_metadata.op("->>")("customer_phone").in_(phones),
            ),
        )
        .limit(2)
        .all()
    )
    ids = {int(row[0]) for row in rows}
    return ids == {int(conversation_id)}


def _history_rows(db: Any, *, tenant_id: int, conversation_id: int,
                  phone: str) -> List[Tuple[str, str, Any]]:
    """``(direction, body, extra_metadata)`` for this conversation, newest last.

    Bound to the conversation the runtime admitted this turn under, never
    resolved from the phone number: one tenant can hold several conversations
    for one number, and a phone lookup answers with the most recent of them,
    which is not necessarily the one being answered.
    """
    from models import MessageEvent  # noqa: PLC0415
    from sqlalchemy import and_, or_  # noqa: PLC0415

    scope = MessageEvent.conversation_id == int(conversation_id)
    phones = _phone_variants(phone)
    if _phone_is_unambiguous(db, tenant_id=tenant_id, conversation_id=conversation_id,
                             phones=phones):
        scope = or_(scope, and_(
            MessageEvent.conversation_id.is_(None),
            MessageEvent.extra_metadata.op("->>")("phone").in_(phones),
        ))
    events = (
        db.query(MessageEvent)
        .filter(MessageEvent.tenant_id == int(tenant_id), scope)
        .order_by(MessageEvent.id.desc())
        .limit(HISTORY_LIMIT)
        .all()
    )
    return [(str(event.direction or ""), str(event.body or ""), event.extra_metadata)
            for event in reversed(events)]


def _prior_turns(db: Any, *, tenant_id: int, conversation_id: int, phone: str,
                 current_text: str) -> list:
    """The conversation so far, bound to the conversation being answered.

    An outbound row contributes only text that is **established** to have been
    transmitted: the wire audit's own record of it, or a body written while the
    wire was observed. A row whose wire was never observed holds the reserved
    intent, which the send path may have rewritten before it went out — it stays
    in the store for the operator, and it is left out of here, because showing
    it to the model would present a draft as the thing that was said.

    The inbound message being answered now is dropped when the store has already
    persisted it, so the model is not shown the same customer turn twice. A read
    that fails yields no history rather than a guess.
    """
    try:
        rows = _history_rows(db, tenant_id=int(tenant_id),
                             conversation_id=int(conversation_id), phone=phone)
    except Exception:  # noqa: BLE001 - missing history is never invented
        logger.warning("[COMMERCE_RUNTIME_PILOT] history unavailable tenant=%s conversation=%s",
                       tenant_id, conversation_id)
        return []
    from core.outbound_wire_audit import wire_transcript_text  # noqa: PLC0415

    turns = []
    for direction, body, metadata in rows:
        outbound = direction in {"out", "outbound"}
        if outbound:
            meta = metadata or {}
            wire_body = wire_transcript_text(meta.get("wire_attempts"))
            if wire_body is not None:
                body = wire_body
            elif _wire_unobserved(meta):
                # This runtime wrote the row and said, at the time, that it
                # could not read back what the send path transmitted. The body
                # is the reserved intent, not established wire text.
                logger.info("[COMMERCE_RUNTIME_PILOT] omitting an unobserved outbound "
                            "row from the model's history tenant=%s conversation=%s",
                            tenant_id, conversation_id)
                continue
        text = str(body or "").strip()
        if not text:
            continue
        turns.append({"role": "assistant" if outbound else "user", "text": text})
    current = str(current_text or "").strip()
    while turns and turns[-1]["role"] == "user" and turns[-1]["text"] == current:
        turns.pop()
    return turns


def _send_factory(phone_id: str, tenant_id: int, db: Any, loop: Any,
                  observation: WireObservation) -> Any:
    """A synchronous view of the established WhatsApp text sender.

    ``_post_wa`` is async and lives in the webhook router. The commerce runtime
    turn runs in a worker thread, so the send is scheduled back onto the running
    event loop and waited for there. Every existing guard on that path — the
    sanitiser, the AI-disabled gate, the burst throttle, the outbound dedup —
    still applies; this adds none and removes none.

    What it does add is an observation: the send path may rewrite the body it
    was handed, so the text it ends up transmitting is read back through the
    platform's own wire audit and recorded. Nothing is written to any row from
    there — the observation is unbound — and the send itself is unchanged.
    """
    def send(recipient: str, text: str) -> Tuple[str, Optional[str], Optional[int]]:
        from core.outbound_wire_audit import (  # noqa: PLC0415
            bind_wire_observation,
            observed_wire_text,
            reset_wire_audit,
        )
        from routers.whatsapp_webhook import _send_whatsapp_message  # noqa: PLC0415

        sink: Dict[str, Any] = {}

        async def _observed_send() -> bool:
            token = bind_wire_observation(int(tenant_id), recipient, text)
            try:
                return bool(await _send_whatsapp_message(
                    phone_id=phone_id, to=recipient, text=text,
                    _tenant_id=int(tenant_id), _db=db, _blocked_path=BLOCKED_PATH,
                    _result_sink=sink,
                ))
            finally:
                seen = observed_wire_text(int(tenant_id), recipient)
                if seen is not None:
                    observation.record(seen[0], seen[1],
                                       duplicate_suppressed=bool(sink.get("duplicate_suppressed")))
                reset_wire_audit(token)

        future = asyncio.run_coroutine_threadsafe(_observed_send(), loop)
        try:
            ok = bool(future.result(timeout=SEND_WAIT_SECONDS))
        except concurrent.futures.TimeoutError:
            # The send is still in flight and may well reach the provider. That
            # is an unknown outcome, never a rejection, and the ledger will
            # refuse to retry it.
            logger.warning("[COMMERCE_RUNTIME_PILOT] send did not answer within %ss",
                           SEND_WAIT_SECONDS)
            return "timeout", None, None
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


def commerce_runtime_claims_inbound(
    db: Any,
    *,
    tenant_id: int,
    phone_id: str,
    to: str,
    text: str,
    wa_msg_id: Optional[str],
) -> bool:
    """Whether the commerce runtime owns this inbound message, asked early.

    The dispatcher has owners of its own — the payment-receipt, payment-evidence,
    map-image and payment-claim short circuits — that mutate order state and send
    a reply *before* the merchant handler is ever entered. A routing decision
    made inside that handler therefore never sees those turns: for an
    allowlisted recipient the short circuit answers and the runtime is not asked,
    and a redelivery let through to recover a runtime turn is answered a second
    time by a different owner.

    This is the same decision, asked where it can still be exclusive. It answers
    ``True`` in two cases:

    * the pilot is configured to own this tenant and recipient, and the
      connection is verified — the ordinary case;
    * this runtime already admitted this exact inbound message — the recovery
      case, which holds even while draining, because a turn this runtime owns is
      never another owner's to answer.

    It decides **ownership**, not whether anything is answered. Every gate that
    can silence the turn — pause, handoff, blocklist, billing, conversation
    quota — still runs inside the handler and still decides. Never raises: a
    question that cannot be answered is answered ``False``, which leaves the
    dispatcher exactly as it is today.
    """
    from core.commerce_runtime import pilot_guard  # noqa: PLC0415

    try:
        decision = pilot_guard.evaluate_pilot_route(
            db, tenant_id=tenant_id, customer_phone=to, phone_number_id=phone_id,
            inbound_text=text,
        )
        if decision.permitted:
            return True
        if decision.reason in NO_RUNTIME_WORK_POSSIBLE:
            return False
        owned = _admitted_runtime_turn(
            tenant_id=int(tenant_id), phone_id=phone_id, wa_msg_id=wa_msg_id,
            refusal=decision.reason)
        if owned is None:
            return False
        logger.warning(
            "[COMMERCE_RUNTIME_PILOT] claiming inbound the runtime already owns "
            "turn=%s finished=%s tenant=%s guard_reason=%s",
            owned.turn_id, owned.finished, tenant_id, decision.reason)
        return True
    except Exception:  # noqa: BLE001 - an undecidable claim is not a claim
        logger.warning("[COMMERCE_RUNTIME_PILOT] ownership claim check failed tenant=%s",
                       tenant_id)
        return False


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

    def route(**extra: Any) -> Any:
        return pilot_guard.evaluate_pilot_route(
            db, tenant_id=tenant_id, customer_phone=to, phone_number_id=phone_id,
            inbound_text=text, legacy_already_answered=legacy_already_answered,
            ai_gate_skipped=ai_gate_skipped, **extra,
        )

    decision = route()
    looked_up: List[Any] = []

    def admitted() -> Optional[Any]:
        """Any turn this runtime admitted for this inbound message, asked once.

        Deliberately the *ownership* question, not the recovery one: a turn can
        be completed by another invocation between deduplication letting this
        retry through and this lookup, and an inbound the runtime already owns
        must not become new work for the legacy path just because it finished
        in the meantime.
        """
        if not looked_up:
            looked_up.append(_admitted_runtime_turn(
                tenant_id=tenant_id, phone_id=phone_id, wa_msg_id=wa_msg_id,
                refusal=decision.reason))
        return looked_up[0]

    if not decision.permitted and decision.reason == pilot_guard.PILOT_DRAINING:
        # A draining pilot takes no new turns and finishes its own. Only a turn
        # this runtime actually admitted and never finished re-enters here.
        owned = admitted()
        if owned is not None and owned.unfinished:
            logger.warning("[COMMERCE_RUNTIME_PILOT] draining: finishing turn=%s tenant=%s",
                           owned.turn_id, tenant_id)
            decision = route(finishing_open_work=True)

    if not decision.permitted:
        owned = admitted()
        if owned is not None:
            # This runtime admitted this exact inbound message. Whatever now
            # makes the guard refuse, and whether or not the turn has since been
            # finished, the legacy path must not answer a message this runtime
            # owns and may already have answered.
            state = "finished" if owned.finished else "unfinished"
            logger.error(
                "[COMMERCE_RUNTIME_PILOT] refused turn=%s this runtime owns (%s) "
                "tenant=%s reason=%s — the legacy path is not given it",
                owned.turn_id, state, tenant_id, decision.reason)
            prefix = OWNED_PREFIX if owned.finished else UNFINISHED_PREFIX
            return PilotResult(handled=True, reason=f"{prefix}{decision.reason}")
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
    wire = WireObservation()
    send = _send_factory(phone_id, int(tenant_id), db, loop, wire)

    def run() -> Any:
        return entry.run_commerce_runtime_turn(
            engine=engine,
            session_factory=SessionLocal,
            tenant_id=int(tenant_id),
            conversation_id=conversation_id,
            connection_ref=str(decision.connection_ref),
            connection_id=str(decision.connection_id),
            customer_id=getattr(convo, "customer_id", None),
            normalized_customer_phone=str(decision.recipient),
            provider_message_id=provider_message_id,
            inbound_text=text,
            inbound_metadata=inbound_metadata,
            transport=entry.whatsapp_text_transport(send, recipient=str(decision.recipient)),
            instructions=_instructions(),
            model=str(decision.model or ""),
            budget=pilot_guard.pilot_budget(),
            context_preamble=_context_preamble(convo, customer_name),
            history=_prior_turns(db, tenant_id=int(tenant_id), conversation_id=conversation_id,
                                 phone=to, current_text=text),
        )

    report = await asyncio.to_thread(run)
    _record(db=db, trace=trace, convo=convo, tenant_id=int(tenant_id), to=to, report=report,
            wire=wire)
    logger.info("[COMMERCE_RUNTIME_PILOT] route=commerce_runtime %s", report.as_log_fields())
    return PilotResult(handled=True, reason=report.reason, report=report)


def _admitted_runtime_turn(*, tenant_id: int, phone_id: str, wa_msg_id: Optional[str],
                           refusal: str) -> Optional[Any]:
    """Any turn this runtime admitted for this inbound message, finished or not.

    Asked only on a refusal, and only for a tenant the pilot is configured for,
    so an ordinary platform turn never pays for it. Never raises: a question
    that cannot be answered is answered ``None``, leaving the refusal as it was.
    """
    if refusal in NO_RUNTIME_WORK_POSSIBLE or not str(wa_msg_id or "").strip():
        return None
    try:
        from core.commerce_runtime import recovery  # noqa: PLC0415

        return recovery.admitted_turn_for(
            tenant_id=int(tenant_id), phone_number_id=phone_id, provider_message_id=wa_msg_id)
    except Exception:  # noqa: BLE001 - an unreadable ledger establishes nothing
        logger.warning("[COMMERCE_RUNTIME_PILOT] admitted-turn check failed tenant=%s",
                       tenant_id)
        return None


def _digest(text: str) -> str:
    return hashlib.sha256(str(text or "").encode("utf-8")).hexdigest()


def _already_recorded(db: Any, *, tenant_id: int, convo: Any,
                      provider_message_id: Optional[str]) -> bool:
    """Whether this exact provider message is already in the conversation.

    Asked only when a dispatch reported an acceptance it did not itself make.
    An unreadable answer is ``False``: the recovery case this exists for is a
    send whose row was never written, and losing that row is worse than a
    duplicate the operator can see.
    """
    identifier = str(provider_message_id or "").strip()
    conversation_id = getattr(convo, "id", None)
    if not identifier or not conversation_id:
        return False
    try:
        from models import MessageEvent  # noqa: PLC0415

        return db.query(MessageEvent.id).filter(
            MessageEvent.tenant_id == int(tenant_id),
            MessageEvent.conversation_id == int(conversation_id),
            MessageEvent.extra_metadata.op("->>")("provider_message_id") == identifier,
        ).first() is not None
    except Exception:  # noqa: BLE001 - an unreadable transcript establishes nothing
        logger.warning("[COMMERCE_RUNTIME_PILOT] could not check for an existing outbound row "
                       "tenant=%s", tenant_id)
        return False


def _record(*, db: Any, trace: Any, convo: Any, tenant_id: int, to: str, report: Any,
            wire: WireObservation) -> None:
    """Persist and trace an accepted send. Nothing is claimed that did not happen.

    What is stored is the text the send path transmitted, not the text the
    ledger reserved, because the stored conversation is what the next turn — and
    every later reviewer — reads as having been said. The reserved intent stays
    where it is, immutable in the delivery ledger, and the row points back at it
    by sequence id and records its digest, so a divergence is visible rather
    than silently resolved in either direction.
    """
    if not report.replied:
        return
    if getattr(report, "reused_dispatch", False) and _already_recorded(
            db, tenant_id=tenant_id, convo=convo,
            provider_message_id=report.provider_message_id):
        # This call sent nothing: the acceptance it is reporting belongs to an
        # earlier attempt, and that attempt's message is already in the
        # conversation. Writing it again would show the customer's transcript a
        # message they were sent once, twice.
        logger.info("[COMMERCE_RUNTIME_PILOT] reused acceptance already recorded turn=%s",
                    report.turn_id)
        return
    intent = str(getattr(report, "reply_text", "") or "")
    text, transformed, reasons = wire.resolve(intent)
    try:
        trace.mark_outbound_sent(source=TRACE_SOURCE, length=len(text))
    except Exception:  # noqa: BLE001 - tracing must not change what happened
        logger.warning("[COMMERCE_RUNTIME_PILOT] trace mark failed turn=%s", report.turn_id)
    if not text:
        logger.warning("[COMMERCE_RUNTIME_PILOT] accepted send without a readable text turn=%s",
                       report.turn_id)
        return
    if transformed:
        logger.warning("[COMMERCE_RUNTIME_PILOT] transmitted text is not the reserved intent "
                       "turn=%s reasons=%s", report.turn_id, ",".join(reasons))
    try:
        from core.conversation_engine import StateManager  # noqa: PLC0415

        StateManager.save_message(
            db, to, text, "outbound",
            conversation_id=getattr(convo, "id", None), tenant_id=tenant_id,
            extra_metadata={
                "compose_source": "llm",
                "response_mode": "grounded",
                "chosen_path": BLOCKED_PATH,
                "llm_candidate_present": True,
                "final_text_transformed": transformed,
                "final_transform_reasons": reasons,
                "final_customer_text_source": "llm_postprocess" if transformed else "llm",
                "commerce_runtime_turn_id": report.turn_id,
                "commerce_runtime_delivery_sequence_id": report.delivery_sequence_id,
                "commerce_runtime_intent_sha256": _digest(intent),
                "commerce_runtime_wire_observed": wire.observed,
                "commerce_runtime_wire_duplicate_suppressed": wire.duplicate_suppressed,
                "provider_message_id": report.provider_message_id,
                "evidence_refs": list(report.evidence_refs),
            },
        )
    except Exception:  # noqa: BLE001 - the send already happened; persistence must not undo it
        logger.exception("[COMMERCE_RUNTIME_PILOT] outbound persist failed turn=%s", report.turn_id)


__all__ = ["BLOCKED_PATH", "HISTORY_LIMIT", "NO_RUNTIME_WORK_POSSIBLE", "OWNED_PREFIX",
           "PilotResult", "SEND_WAIT_SECONDS", "TRACE_SOURCE", "UNFINISHED_PREFIX",
           "WIRE_UNOBSERVED", "WireObservation", "maybe_handle_with_commerce_runtime"]

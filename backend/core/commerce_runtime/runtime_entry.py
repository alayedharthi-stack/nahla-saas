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
import json
import logging
import threading
import time
import uuid
from typing import Any, Dict, Mapping, Optional, Tuple

from core.commerce_runtime import agent_contracts as ac
from core.commerce_runtime import agent_live_tools as alt
from core.commerce_runtime import agent_provider as ap
from core.commerce_runtime import contracts as c
from core.commerce_runtime import conversation_link as cl
from core.commerce_runtime import delivery_dispatch as dd
from core.commerce_runtime import ledger_contracts as lc
from core.commerce_runtime import recent_products as rp
from core.commerce_runtime import reply_card as rcard
from core.commerce_runtime import reply_choices as rc
from core.commerce_runtime.agent_loop import AgentLoop
from core.commerce_runtime.ledgers import LedgerRepository

logger = logging.getLogger("nahla.commerce_runtime.runtime_entry")

NAMESPACE = c.Namespace.LIVE.value
CHANNEL = "wa"
LEASE_SECONDS = 180
OWNER_PREFIX = "commerce-runtime-pilot"

# How long the reaper waits for an abandoned tool call to finish with the
# session before giving up on closing it. Longer than any tool timeout the
# guard can configure, so an ordinary slow read is waited out rather than
# leaked, and finite, so a wedged call cannot hold a thread for ever.
ABANDONED_SESSION_REAP_SECONDS = 300.0

# Closed outcomes of one entry call.
HANDLED = "handled"
SCHEMA_UNAVAILABLE = "runtime_schema_unavailable"
ALREADY_TERMINAL = "turn_already_terminal"
OWNERSHIP_UNAVAILABLE = "ownership_unavailable"
CONTEXT_UNAVAILABLE = "trusted_context_unavailable"
LINK_UNVERIFIED = "conversation_link_unverified"
MODEL_UNCONFIGURED = "model_not_configured"
HANDOVER_BARRIER = "handover_barrier_closed"
ADMISSION_CONFLICT = "admission_conflict"
INTERNAL_ERROR = "internal_error"

# Every relation the runtime needs: foundation, ledgers and handover together.
#
# All twelve, not nine. The handover three are not optional extras — the
# barrier decides whether a turn may be admitted at all, and the deferred table
# is where an accepted inbound lives between the acknowledgement and the answer.
# A database holding the first nine can admit turns it cannot record acceptance
# for, which is the shape that acknowledges a customer and keeps nothing.
FOUNDATION_RELATIONS: Tuple[str, ...] = (
    "commerce_runtime_conversations",
    "commerce_runtime_turns",
    "commerce_runtime_turn_terminals",
)

LEDGER_RELATIONS: Tuple[str, ...] = (
    "commerce_runtime_effects",
    "commerce_runtime_effect_attempts",
    "commerce_runtime_effect_results",
    "commerce_runtime_delivery_sequences",
    "commerce_runtime_delivery_attempts",
    "commerce_runtime_delivery_receipts",
)

HANDOVER_RELATIONS: Tuple[str, ...] = (
    "commerce_runtime_handover_barrier",
    "commerce_runtime_handover_workers",
    "commerce_runtime_deferred_inbound",
)

REQUIRED_RELATIONS: Tuple[str, ...] = (
    FOUNDATION_RELATIONS + LEDGER_RELATIONS + HANDOVER_RELATIONS
)

_schema_state: Dict[str, str] = {}
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
    stop_detail: Tuple[Tuple[str, str], ...] = ()   # the loop's own account of the stop, bounded
    delivery_sequence_id: Optional[int] = None
    reused_delivery: bool = False      # the loop reused an existing reservation
    reused_dispatch: bool = False      # the outcome came from an earlier attempt, not this send
    dispatch_status: Optional[str] = None
    delivery_kind: Optional[str] = None       # text, or rich when a selector was offered
    choice_rows: int = 0                      # selectable rows the customer was offered
    # The row ids that actually reached the customer, which is what a later tap
    # is checked against. Empty when the list was withheld or refused: a row
    # nobody was sent is a row nobody can have tapped.
    choice_row_ids: Tuple[str, ...] = ()
    # Why the reply carried the shape it did. ``choice_rows=0`` on its own
    # cannot tell a model that never asked for a selector from one that asked
    # and had it withheld — for an unobserved product, a missing photo, a
    # plain-http link — and those are opposite problems. The loop already
    # decides both and records them; without them here a text-only turn is
    # unreadable without a transcript.
    choices_outcome: Optional[str] = None     # reply_choices: offered, or the reason withheld
    card_outcome: Optional[str] = None        # reply_card: offered, or the reason withheld
    recovery_status: Optional[str] = None     # the bounded rich-to-text attempt, when one was made
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
    requested_model: Optional[str] = None   # what the platform asked for
    model: Optional[str] = None             # what the provider reported answering with
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
        fields["choice_row_ids"] = ",".join(self.choice_row_ids)
        fields["stop_detail"] = ";".join(f"{key}={value}" for key, value in self.stop_detail)
        fields["replied"] = self.replied
        fields["reply_chars"] = len(self.reply_text)
        fields.pop("reply_text", None)      # the log line records length, never the customer's text
        return fields


def runtime_schema_available(engine: Any) -> bool:
    """Whether this database actually holds the commerce runtime's tables.

    All **twelve** are required together: the three foundation relations the
    runtime admits, claims and completes turns in, the six ledger relations it
    reserves and records delivery in, and the three handover relations that
    decide whether a turn may be admitted and hold an inbound between the
    acknowledgement and the answer. A database holding some of them is
    ``partial`` and stays unavailable — the runtime never runs half-present, and
    a foundation-only database is exactly the shape that would otherwise pass a
    turns-plus-ledgers check while having no terminals table.

    The migrations that create them are merged but are not part of the normal
    bootstrap target, so a database may legitimately not have them. Probing once
    per database keeps a disabled pilot from paying for the check on every turn.
    """
    # Keyed by the engine's own target, not by object identity: a garbage
    # collected engine can hand its id() to the next one, and a probe result
    # must never be attributed to a different database.
    key = _engine_key(engine)
    with _schema_lock:
        cached = _schema_state.get(key)
    if cached is not None:
        return cached == "complete"
    from sqlalchemy import text as sa_text  # noqa: PLC0415

    state = "absent"
    missing: Tuple[str, ...] = REQUIRED_RELATIONS
    try:
        with engine.connect() as conn:
            present = tuple(
                name for name in REQUIRED_RELATIONS
                if conn.execute(sa_text("SELECT to_regclass(:t)"),
                                {"t": f"public.{name}"}).scalar()
            )
        missing = tuple(name for name in REQUIRED_RELATIONS if name not in present)
        if not present:
            state = "absent"
        elif not missing:
            state = "complete"
        else:
            state = "partial"
    except Exception as exc:  # noqa: BLE001 - an unprobeable schema is not an available one
        logger.warning("[COMMERCE_RUNTIME] schema probe failed error=%s", type(exc).__name__)
        state = "absent"
    with _schema_lock:
        _schema_state[key] = state
    logger.info("[COMMERCE_RUNTIME] schema probe state=%s missing=%s", state, ",".join(missing))
    return state == "complete"


def _engine_key(engine: Any) -> str:
    try:
        return str(engine.url)
    except Exception:  # noqa: BLE001 - an engine that cannot name itself is probed every time
        return f"unknown:{id(engine)}"


def reset_schema_probe() -> None:
    """Forget every probed engine. For tests and for an operator re-check."""
    with _schema_lock:
        _schema_state.clear()


def build_trusted_binding(
    *,
    session_factory: Any,
    link: cl.TrustedConversationLink,
    customer_id: Optional[int],
    normalized_customer_phone: str,
    connection_id: str,
    inbound_trace_id: str,
) -> Tuple[Optional[alt.LiveToolBinding], Optional[Any]]:
    """One trusted read context on its own session, or nothing at all.

    The context is built for the **application** conversation the link names;
    the binding checks the loop's scope against the **runtime** conversation the
    same link names. Neither identifier is ever compared with the other.

    The session belongs to the tool binding and to no request: a tool call the
    loop abandons must never be left sharing a session with the webhook.
    """
    from modules.ai.commerce_agent_v2.context import CommerceAgentContext  # noqa: PLC0415

    session = session_factory()
    try:
        context = CommerceAgentContext.from_trusted_scope(
            db=session,
            tenant_id=int(link.tenant_id),
            conversation_id=int(link.app_conversation_id),
            customer_id=int(customer_id) if customer_id else None,
            normalized_customer_phone=normalized_customer_phone,
            connection_id=connection_id,
            inbound_trace_id=inbound_trace_id,
        )
    except Exception as exc:  # noqa: BLE001 - without a trusted scope there are no tools
        logger.warning("[COMMERCE_RUNTIME] trusted context unavailable tenant=%s error=%s",
                       link.tenant_id, type(exc).__name__)
        _close_quietly(session)
        return None, None
    return alt.LiveToolBinding(context=context, link=link), session


def _with_products_shown_earlier(
    binding: Any,
    session: Any,
    *,
    tenant_id: int,
    conversation_id: int,
    turn_id: int,
    context_preamble: Optional[Mapping[str, Any]],
    inbound_metadata: Optional[Mapping[str, Any]] = None,
) -> Dict[str, Any]:
    """Carry this conversation's recently shown products into the turn.

    Two things happen together, and neither is useful alone: the products
    become identities this turn may look up, and the model is told they exist.
    Authorizing without telling leaves the model guessing an id — which is what
    it did on 2026-09-22, and the isolation guard rightly refused it. Telling
    without authorizing leaves it with a name it cannot resolve.

    The context is data, not instruction: ids and the merchant's own values, in
    the same trusted-fact block the preamble already uses. No order is claimed;
    see ``recent_products`` for why. Never raises — a turn that cannot read its
    own earlier replies runs without the aid.
    """
    preamble: Dict[str, Any] = dict(context_preamble or {})
    try:
        shown = rp.products_shown_earlier(
            session, tenant_id=int(tenant_id), conversation_id=int(conversation_id))
        if shown.products:
            binding.context.authorize_products(shown.product_ids, titles=shown.titles)
            preamble["products_shown_earlier"] = shown.as_facts()
        tapped = _tapped_product(inbound_metadata, shown)
        if tapped is not None:
            preamble["customer_tapped"] = tapped
    except Exception as exc:  # noqa: BLE001 - the aid is never the turn's precondition
        logger.warning("[COMMERCE_RUNTIME] browsing context unavailable turn=%s error=%s",
                       turn_id, type(exc).__name__)
        return preamble
    logger.info("[COMMERCE_RUNTIME] browsing context turn=%s reason=%s products=%d "
                "seconds_since_last_product_shown=%s tapped=%s", turn_id, shown.reason,
                len(shown.products), shown.seconds_since_last_product_shown,
                (preamble.get("customer_tapped") or {}).get("product_id"))
    return preamble


def _tapped_product(inbound_metadata: Optional[Mapping[str, Any]],
                    shown: Any) -> Optional[Dict[str, Any]]:
    """The product a tap on a selector row names, once it verifies.

    A row id arrives from the wire, so it is a claim and not a fact, and two
    separate things have to hold before the platform will say a row was tapped:

    * **this conversation actually sent that row.** The check is against the
      row ids the send path put on the wire and the platform persisted with
      that reply, not against products the conversation merely mentioned. A
      product named in prose was never a row anyone could tap, so a crafted id
      naming one resolves to nothing. When the tap names the message it was
      made in — WhatsApp supplies that — the check is to **that one list**, so
      a row from some other list this conversation still holds is not a tap on
      this one either;
    * **the product is still carried**, under the same browsing-context clock
      as every other reference, and re-read in the merchant's catalogue now.

    A tap on a list old enough to have lapsed, on a row this runtime never
    sent, or on another tenant's product therefore resolves to nothing at all,
    and the turn simply proceeds on the row title the tap delivered as ordinary
    text — the customer is still answered.
    """
    metadata = inbound_metadata if isinstance(inbound_metadata, Mapping) else {}
    product_id = rc.product_id_from_row_id(metadata.get("list_reply_id"))
    if product_id is None:
        return None
    named = str(metadata.get("list_reply_context_id") or "").strip()
    if named:
        # The customer's tap names the message it was made in, so the answer is
        # exact: the row belongs to that one list or to none. A named message
        # this conversation never sent, or one whose rows do not include this
        # product, is not a tap on anything.
        if product_id not in shown.rows_offered_in(named):
            logger.info("[COMMERCE_RUNTIME] a tapped row does not belong to the list it names "
                        "product_id=%s", product_id)
            return None
    elif product_id not in (getattr(shown, "offered_as_rows", ()) or ()):
        # No message named — some payloads carry none. The weaker but still
        # real check stands: some list this conversation sent, still inside the
        # lapse, offered this row.
        logger.info("[COMMERCE_RUNTIME] a tapped row was never offered in this conversation "
                    "product_id=%s", product_id)
        return None
    for product in getattr(shown, "products", ()) or ():
        if int(getattr(product, "product_id", 0)) == product_id:
            return product.as_fact()
    logger.info("[COMMERCE_RUNTIME] a tapped row named a product this conversation no longer "
                "carries product_id=%s", product_id)
    return None


def _close_quietly(session: Any) -> None:
    try:
        session.close()
    except Exception:  # noqa: BLE001 - a session we can no longer trust is simply dropped
        logger.warning("[COMMERCE_RUNTIME] tool session could not be closed cleanly")


class ExclusiveCloser:
    """One session, one close, whoever gets there first.

    Two owners race for an abandoned session: the thread that finishes the
    abandoned call, and the reaper. Deciding with a check-then-set on a flag —
    read it, see it unset, set it — is not arbitration: both threads can read it
    unset before either writes, and both then close. A session closed twice is a
    connection returned to the pool twice.

    The claim is therefore taken under a mutex and is the only thing the mutex
    guards: closing itself happens outside it, so a slow or hanging ``close``
    never blocks the loser, which has nothing left to do anyway.

    ``lock`` exists so a test can drive a chosen interleaving through the very
    code production runs, rather than a copy of it. Production supplies none.
    """

    def __init__(self, session: Any, *, turn_id: Optional[int] = None,
                 lock: Optional[Any] = None) -> None:
        self._session = session
        self._turn_id = turn_id
        self._lock = lock if lock is not None else threading.Lock()
        self._claimed = False

    def claim(self) -> bool:
        """``True`` for exactly one caller, ever."""
        with self._lock:
            if self._claimed:
                return False
            self._claimed = True
            return True

    @property
    def claimed(self) -> bool:
        return self._claimed

    def close(self, source: str) -> bool:
        """Close the session if this caller won the claim. Never raises."""
        if not self.claim():
            return False
        _close_quietly(self._session)
        logger.info("[COMMERCE_RUNTIME] tool session closed by %s turn=%s",
                    source, self._turn_id)
        return True


def _retire_tool_session(binding: alt.LiveToolBinding, session: Any, *,
                         turn_id: Optional[int] = None,
                         reap_seconds: float = ABANDONED_SESSION_REAP_SECONDS) -> Optional[Any]:
    """Close the tool session, or give it an owner that will — with no deadline.

    The common case is that every tool call returned and the session closes
    here, on the turn's own thread. When a call was abandoned and has not come
    back, the session is still in use by a thread nobody is waiting for, and
    closing it now would pull a connection out from under a live statement.

    Two things then own it, and they are not the same thing:

    * an **idle callback** on the binding, which the thread finishing the
      abandoned call runs. This is the owner, and it does not expire: however
      long that call takes, the session is closed when it ends.
    * a bounded **reaper** thread, so the ordinary case — a call that finishes
      seconds later — is closed promptly and observably rather than only when
      something else happens to look.

    Whichever gets there first closes it; closing is idempotent. When the
    reaper's wait runs out it says so and stops waiting, but it does not hand
    the responsibility back to nobody: the callback is still registered.

    Returns the reaper thread so a caller can observe it; ``None`` when the
    session was closed here.
    """
    closer = ExclusiveCloser(session, turn_id=turn_id)

    def close_once(source: str) -> None:
        closer.close(source)

    if binding.on_idle(lambda: close_once("the abandoned call's own thread")):
        # Nothing held it: closed synchronously, on this turn's thread.
        return None

    def reap() -> None:
        if binding.wait_until_idle(reap_seconds):
            close_once("the reaper")
            return
        logger.warning("[COMMERCE_RUNTIME] abandoned tool call has not returned within %ss "
                       "turn=%s; its session stays open and is closed by the call itself "
                       "when it ends", reap_seconds, turn_id)

    thread = threading.Thread(target=reap, name=f"commerce-runtime-session-reaper-{turn_id}",
                              daemon=True)
    thread.start()
    logger.warning("[COMMERCE_RUNTIME] tool session handed to the reaper turn=%s abandoned=%s",
                   turn_id, binding.abandoned_calls)
    return thread


def run_commerce_runtime_turn(
    *,
    engine: Any,
    session_factory: Any,
    tenant_id: int,
    conversation_id: int,
    connection_ref: str,
    connection_id: str,
    customer_id: Optional[int],
    normalized_customer_phone: str,
    provider_message_id: str,
    inbound_text: str,
    inbound_metadata: Optional[Mapping[str, Any]],
    transport: dd.Transport,
    instructions: str,
    model: str,
    admission_barrier: Optional[Any] = None,
    budget: Optional[ac.LoopBudget] = None,
    context_preamble: Optional[Mapping[str, Any]] = None,
    history: Optional[Any] = None,
    channel: str = CHANNEL,
    anthropic_provider: Optional[Any] = None,
) -> TurnReport:
    """Run one admitted inbound turn to a recorded transport outcome.

    ``conversation_id`` is the **application** conversation. The runtime's own
    conversation is admitted under a reference derived from it here, so a caller
    cannot supply a reference that names a different conversation, and the
    association is verified by reading the row back before anything is bound.

    ``model`` is the caller's explicit choice and has no default: this entry
    refuses rather than let an unconfigured pilot inherit whatever the legacy
    path happens to resolve.

    ``admission_barrier`` is an optional veto on taking **new** work, called
    with the admission transaction's own connection. It is the handover's
    mechanism: see the ``AdmissionRefused`` branch below for why it is asked
    there and nowhere else.
    """
    started = time.monotonic()
    base = {"tenant_id": int(tenant_id), "conversation_id": int(conversation_id)}
    requested_model = str(model or "").strip()
    if not requested_model:
        # The loop is model-neutral and selects nothing. Without an explicitly
        # configured model there is no approved choice to run on, and inheriting
        # the legacy path's resolution would be selecting one silently.
        logger.warning("[COMMERCE_RUNTIME] no model configured for this turn tenant=%s", tenant_id)
        return TurnReport(reason=MODEL_UNCONFIGURED, **base)
    if not runtime_schema_available(engine):
        return TurnReport(reason=SCHEMA_UNAVAILABLE, **base)

    ledgers = LedgerRepository(engine)
    foundation = ledgers.foundation
    owner_id = f"{OWNER_PREFIX}:{uuid.uuid4().hex[:16]}"

    try:
        conversation_ref = cl.conversation_ref_for(
            channel=channel, app_conversation_id=int(conversation_id))
        admitted = foundation.admit_turn(
            tenant_id=tenant_id, namespace=NAMESPACE, conversation_ref=conversation_ref,
            channel_connection_ref=connection_ref, provider_message_id=provider_message_id,
            payload={"text": inbound_text, "metadata": dict(inbound_metadata or {})},
            admission_guard=admission_barrier,
        )
    except c.AdmissionRefused:
        # The window between selecting this route and writing the turn is where
        # a handover loses work: a drain can land inside it, and a turn admitted
        # after that is one the settlement check already counted as absent. The
        # barrier is read on this transaction's own connection, so the database
        # orders the two: either the turn is visible to the drain, or the drain
        # is visible here and nothing is written. A re-entry for a turn already
        # admitted never reaches the barrier, which is what lets a draining
        # runtime still finish its own work.
        logger.warning("[COMMERCE_RUNTIME] admission refused by the handover barrier "
                       "tenant=%s", tenant_id)
        return TurnReport(reason=HANDOVER_BARRIER, **base)
    except c.CommerceRuntimeError as exc:
        logger.warning("[COMMERCE_RUNTIME] admission refused tenant=%s error=%s",
                       tenant_id, type(exc).__name__)
        return TurnReport(reason=ADMISSION_CONFLICT, **base)

    turn_id = admitted.turn_id
    report_base = dict(base, turn_id=turn_id, duplicate_inbound=bool(admitted.duplicate),
                       owner_id=owner_id, requested_model=requested_model)

    # The two conversation identifiers come from independent sequences. Establish
    # the association by reading the runtime row back, before anything is scoped
    # by either of them.
    try:
        link = cl.verify_conversation_link(
            foundation, tenant_id=tenant_id, namespace=NAMESPACE, channel=channel,
            app_conversation_id=int(conversation_id),
            runtime_conversation_id=int(admitted.conversation_id),
        )
    except (cl.ConversationLinkUnverified, c.CommerceRuntimeError) as exc:
        logger.warning("[COMMERCE_RUNTIME] conversation link unverified turn=%s reason=%s",
                       turn_id, getattr(exc, "reason", None) or type(exc).__name__)
        return TurnReport(reason=LINK_UNVERIFIED, **report_base)
    runtime_conversation_id = link.runtime_conversation_id

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
        session_factory=session_factory, link=link, customer_id=customer_id,
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

        # What earlier replies in this conversation were grounded on, so a
        # follow-up question about something already shown has an identity to
        # bind to instead of a phrase to search for. Identity only: every fact
        # still has to be read by a tool in this turn and cited as this turn's
        # evidence. Inside the try, so the session below is always retired.
        preamble = _with_products_shown_earlier(
            binding, session, tenant_id=int(tenant_id), conversation_id=int(conversation_id),
            turn_id=turn_id, context_preamble=context_preamble,
            inbound_metadata=inbound_metadata)
        registry = alt.build_live_registry(binding)
        reasoner = ap.AnthropicReasoningProvider(
            instructions=instructions,
            tools_provider=anthropic_provider or AnthropicProvider(),
            max_tool_requests_per_step=_tool_requests_per_step(budget),
            audit_context={"tenant_id": int(tenant_id), "conversation_id": int(conversation_id),
                           "turn_id": int(turn_id), "channel": "whatsapp",
                           "reason": "commerce_runtime_pilot",
                           # The provider resolves the call's model from this key.
                           # It is the configured pilot value, never a fallback.
                           "model": requested_model},
            context_preamble=preamble,
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
        # The session belongs to the tool binding. It is closed here only when
        # no call still holds it; otherwise it is handed to the reaper, which
        # is this runtime's named owner for exactly that case. Closing it under
        # a live call would break a statement in flight, and leaving it with no
        # owner would leak it.
        _retire_tool_session(binding, session, turn_id=turn_id)
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
    # The loop's own account of the reply it accepted, so a text-only turn says
    # why it was text-only rather than leaving it to be guessed.
    accepted = next((event for event in reversed(outcome.events)
                     if event.kind == "reply_accepted"), None)
    accepted_detail = dict(getattr(accepted, "detail", None) or {})
    evidence = tuple(str(ref) for ref in (outcome.detail.get("evidence_refs") or ()))
    usage_model = next((u.model for u in reversed(reasoner.usage) if u.model), None)
    stop_detail = _stop_detail(outcome)
    common = dict(
        report_base,
        loop_status=outcome.status,
        stop_reason=outcome.stop_reason,
        stop_detail=stop_detail,
        delivery_sequence_id=outcome.delivery_sequence_id,
        reused_delivery=outcome.reused_delivery,
        steps_used=outcome.steps_used,
        tool_calls_used=outcome.tool_calls_used,
        tools_called=tools_called,
        choices_outcome=str(accepted_detail.get("choices") or "") or None,
        card_outcome=str(accepted_detail.get("card") or "") or None,
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
            details={"stop_reason": outcome.stop_reason or "unknown", "source": "commerce_runtime_pilot",
                     **({"stop_detail": dict(stop_detail)} if stop_detail else {})},
        )
        return TurnReport(reason=HANDLED, dispatch_status=None,
                          processing_outcome=getattr(terminal, "processing_outcome", None),
                          transport_outcome=getattr(terminal, "transport_outcome", None),
                          customer_reach=getattr(terminal, "customer_reach", None), **common)

    sequence = ledgers.get_delivery_sequence(tenant_id=tenant_id, namespace=NAMESPACE, turn_id=turn_id)
    intent_payload = dict(getattr(sequence, "intent_payload", None) or {})
    # The text that is dispatched is the ledger's stored intent, read back rather
    # than rebuilt, so what is persisted and logged is what actually went out.
    common["reply_text"] = str(intent_payload.get("text") or "")
    common["delivery_kind"] = getattr(sequence, "intent_kind", None)
    offered_rows, _button = rc.payload_rows(intent_payload)
    common["choice_rows"] = len(offered_rows)
    common["choice_row_ids"] = tuple(str(row.get("id") or "") for row in offered_rows
                                     if row.get("id"))
    dispatch = dd.dispatch_reserved_delivery(
        ledgers=ledgers, tenant_id=tenant_id, namespace=NAMESPACE,
        conversation_id=runtime_conversation_id, token=token,
        sequence_id=outcome.delivery_sequence_id, transport=transport, recorded_by=owner_id,
    )
    recovery = _recover_without_the_selector(
        ledgers=ledgers, tenant_id=tenant_id, runtime_conversation_id=runtime_conversation_id,
        token=token, dispatch=dispatch, sequence=sequence, intent_payload=intent_payload,
        transport=transport, owner_id=owner_id)
    if recovery is not None:
        common["recovery_status"] = recovery.status
        # The recovery is the send that reached the customer, and it carried no
        # rows. Reporting the withheld ones would let a later tap verify
        # against a list nobody received.
        common["choice_row_ids"] = ()
        dispatch = recovery
    terminal = dd.complete_turn(
        ledgers=ledgers, tenant_id=tenant_id, namespace=NAMESPACE, turn_id=turn_id, token=token,
        processing_outcome=dispatch.processing_outcome,
        details={"source": "commerce_runtime_pilot", "dispatch_status": dispatch.status,
                 **({"blocked": dispatch.blocked_reason} if dispatch.blocked_reason else {})},
    )
    return TurnReport(reason=HANDLED, dispatch_status=dispatch.status,
                      provider_message_id=dispatch.provider_message_id,
                      reused_dispatch=dispatch.reused_outcome,
                      processing_outcome=getattr(terminal, "processing_outcome", None),
                      transport_outcome=getattr(terminal, "transport_outcome", None),
                      customer_reach=getattr(terminal, "customer_reach", None), **common)


def _recover_without_the_selector(
    *, ledgers: LedgerRepository, tenant_id: int, runtime_conversation_id: int,
    token: c.OwnershipToken, dispatch: dd.DispatchOutcome, sequence: Any,
    intent_payload: Mapping[str, Any], transport: dd.Transport, owner_id: str,
) -> Optional[dd.DispatchOutcome]:
    """Send the answer without its selector when the rich send was refused.

    Only on a **proven** rejection of a rich message: an unknown send may
    already have reached the customer, and a second one would be a duplicate,
    not a recovery. The ledger refuses everything else on its own; this only
    declines to ask when the case plainly is not one.

    A rejection this call did not itself produce still counts. It is the crash
    window the bounded recovery exists for — the list was refused, the receipt
    written, and the process died before the terminal — and refusing to act on
    it would leave that customer with no answer at all. Nothing is taken on
    trust: the ledger re-reads the outcome, the attempt kind and the bound, and
    an accepted, unknown or already-recovered reservation permits nothing.

    The reply itself is unchanged — same text, same evidence, no selector — so
    a provider that will not render the rows costs the customer the tapping and
    nothing else. A recovery that is itself refused leaves the first outcome
    standing rather than inventing a better one.
    """
    if dispatch.status != dd.SENT_REJECTED:
        return None
    if str(getattr(sequence, "intent_kind", "") or "") != lc.DeliveryKind.RICH.value:
        return None
    payload = rc.text_only_payload(intent_payload)
    if not str(payload.get("text") or "").strip():
        return None
    recovery = dd.dispatch_delivery_recovery(
        ledgers=ledgers, tenant_id=tenant_id, namespace=NAMESPACE,
        conversation_id=runtime_conversation_id, token=token,
        sequence_id=dispatch.sequence_id, payload=payload, transport=transport,
        recorded_by=owner_id,
    )
    if recovery.status == dd.NOT_ATTEMPTED:
        logger.info("[COMMERCE_RUNTIME] selector recovery not made sequence=%s reason=%s",
                    dispatch.sequence_id, recovery.blocked_reason)
        return None
    logger.info("[COMMERCE_RUNTIME] the selector was refused; the same answer was sent as text "
                "sequence=%s outcome=%s", dispatch.sequence_id, recovery.status)
    return recovery


STOP_DETAIL_MAX_ITEMS = 12
STOP_DETAIL_MAX_CHARS = 160


def _tool_requests_per_step(budget: Optional[ac.LoopBudget]) -> int:
    """How many tool requests one provider step may carry.

    The API never tells the model a per-step ceiling, so a well-formed bundle
    the turn's tool budget can pay for must not be refused as invalid for its
    size alone: four product-detail requests in one step against a ceiling of
    three ended a real turn with no reply at all. The ceiling is therefore the
    turn's whole tool budget, within the contract's own maximum. A bundle
    beyond the budget is still stopped whole, by name, as ``budget_exhausted``.
    """
    effective = budget if budget is not None else ac.LoopBudget()   # the loop's own default
    return max(1, min(int(ac.MAX_TOOL_REQUESTS_PER_STEP), int(effective.max_tool_calls)))


def _stop_detail(outcome: ac.LoopOutcome) -> Tuple[Tuple[str, str], ...]:
    """The loop's own account of why it stopped, bounded for a log line and a terminal.

    Only what the loop recorded as its stop detail is carried — the provider's
    stated reason, the validation message, the capability or limit that was
    exceeded, the tool that was repeated — so the next ``provider_invalid``
    names its cause instead of only its class. Evidence references have their
    own field. No stop detail carries the customer's text or the reply.
    """
    if outcome.status != ac.LoopStatus.STOPPED.value:
        return ()
    items = []
    for key, value in sorted(outcome.detail.items(), key=lambda item: str(item[0])):
        if key == "evidence_refs":
            continue
        if isinstance(value, (Mapping, list, tuple)):
            rendered = json.dumps(value, ensure_ascii=False, sort_keys=True, default=str)
        else:
            rendered = str(value)
        if len(rendered) > STOP_DETAIL_MAX_CHARS:
            rendered = rendered[:STOP_DETAIL_MAX_CHARS - 1] + "…"
        items.append((str(key), rendered))
        if len(items) >= STOP_DETAIL_MAX_ITEMS:
            break
    return tuple(items)


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


def whatsapp_reply_transport(send: Any, send_list: Any, *, recipient: str,
                             send_card: Any = None) -> dd.Transport:
    """One transport for both shapes of the same reply.

    The stored payload says which it is: a reply that carries verified
    selectable rows is sent as an interactive list, and everything else — plain
    replies, and the bounded recovery after a refused list — goes out as text.
    Reading the shape off the payload rather than off a flag is what lets the
    recovery attempt reuse this same transport unchanged.

    Both senders report what the provider said as ``(classification, wamid,
    http_status)``; only ``ok`` with a provider message id is an accepted send.
    A list sender that is not available is not a reason to drop the answer: the
    text goes out instead, which is exactly what the recovery would have done.
    """
    text_only = whatsapp_text_transport(send, recipient=recipient)

    def transport(payload: Mapping[str, Any]) -> lc.SendResponse:
        card = rcard.payload_card(payload)
        if card is not None and send_card is not None:
            classification, wamid, http_status = send_card(
                recipient, str(payload.get("text") or ""),
                card["image_url"], card["button_url"], card["button_label"])
            return _sent(classification, wamid, http_status)
        rows, button = rc.payload_rows(payload)
        if not rows or send_list is None:
            return text_only(payload)
        classification, wamid, http_status = send_list(
            recipient, str(payload.get("text") or ""), rows, button)
        return _sent(classification, wamid, http_status)

    return transport


def _sent(classification: Any, wamid: Any, http_status: Any) -> lc.SendResponse:
    """One provider answer, read the same way whatever shape was sent.

    Only ``ok`` with a provider message id is an accepted send; a status
    without one is a rejection carrying its reason, and no status at all is
    unknown — never a proven rejection.
    """
    if classification == "ok" and wamid:
        return lc.SendResponse(http_status=int(http_status or 200),
                               body={"messages": [{"id": str(wamid)}]})
    if http_status is not None:
        return lc.SendResponse(http_status=int(http_status),
                               body={"error": {"code": str(classification or "unknown")}})
    return lc.SendResponse(http_status=None, body={}, timed_out=True)


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
    "ADMISSION_CONFLICT", "ALREADY_TERMINAL", "CHANNEL", "CONTEXT_UNAVAILABLE", "HANDLED",
    "HANDOVER_BARRIER",
    "FOUNDATION_RELATIONS", "HANDOVER_RELATIONS", "LEDGER_RELATIONS",
    "INTERNAL_ERROR", "LINK_UNVERIFIED", "MODEL_UNCONFIGURED", "REQUIRED_RELATIONS",
    "ABANDONED_SESSION_REAP_SECONDS", "ExclusiveCloser",
    "LEASE_SECONDS", "NAMESPACE", "OWNERSHIP_UNAVAILABLE", "SCHEMA_UNAVAILABLE", "TurnReport",
    "build_trusted_binding", "reset_schema_probe", "run_commerce_runtime_turn",
    "runtime_schema_available", "whatsapp_reply_transport", "whatsapp_text_transport",
]

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
import contextlib
import dataclasses
import datetime as _dt
import hashlib
import logging
from typing import Any, Dict, List, Mapping, Optional, Sequence, Tuple

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

# The bases a claim can be established on, most to least specific.
BASIS_CONFIGURED = "configured"
BASIS_ADMITTED_OPEN = "admitted_open"
BASIS_ADMITTED_FINISHED = "admitted_finished"
BASIS_DRAIN_BUFFERED = "drain_buffered"
# Established that this inbound is the runtime's, then could not read the state
# that decides what to do with it. The turn is held — refused execution, and
# given to nobody else.
BASIS_OWNERSHIP_UNAVAILABLE = "ownership_unavailable"
# Accepted work handed back by a recovery run while the barrier is draining:
# not new work, admitted under a grant the runner states for one replay and the
# seam verifies against the durable record before honouring.
BASIS_RECOVERY_ADMITTED = "recovery_admitted"


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
    # The selectable rows the send path actually put on the wire, read back the
    # same way the text is. A later tap is checked against these, so they have
    # to be what was sent rather than what was asked for: the sender
    # de-duplicates ids and caps the list, and the bounded text recovery sends
    # none at all. Each send replaces this, so what remains is the send that
    # reached the customer.
    row_ids: List[str] = dataclasses.field(default_factory=list)

    def record(self, text: str, reasons: Sequence[str], *, duplicate_suppressed: bool,
               row_ids: Optional[Sequence[str]] = None) -> None:
        self.observed = True
        self.text = str(text or "")
        self.reasons = [str(reason) for reason in reasons]
        self.duplicate_suppressed = bool(duplicate_suppressed)
        self.row_ids = [str(value) for value in (row_ids or ()) if str(value or "").strip()]

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


def _saved_ai_settings(db: Any, tenant_id: int) -> Optional[Mapping[str, Any]]:
    """The AI settings the merchant saved, as stored — ``{}`` when none are.

    Read from the column each turn — never a cached ORM entity or merged
    defaults. ``None`` when the settings could not be read, or are not an
    object: the turn then goes without anything derived from them rather than
    with a guess, and it still goes. This runs after the route was taken and
    before admission, so a failure here escaping would leave a customer
    unanswered and nothing recorded — the one outcome this runtime must never
    produce.
    """
    from models import TenantSettings  # noqa: PLC0415

    connection = getattr(db, "connection", None)
    try:
        # A connection savepoint: a read that fails in the database leaves the
        # turn's session usable, and — unlike Session.begin_nested — it flushes
        # nothing the session is holding.
        with (connection().begin_nested() if callable(connection) else contextlib.nullcontext()):
            settings = db.query(TenantSettings.ai_settings).filter(
                TenantSettings.tenant_id == int(tenant_id)).scalar()
    except Exception as exc:  # noqa: BLE001 - the turn is answered without them
        logger.error("[COMMERCE_RUNTIME_PILOT] assistant name unreadable tenant=%s error=%s",
                     tenant_id, type(exc).__name__)
        return None
    if settings is None:
        return {}
    if not isinstance(settings, Mapping):
        logger.error("[COMMERCE_RUNTIME_PILOT] assistant settings are not an object with a text "
                     "name tenant=%s", tenant_id)
        return None
    return settings


def _assistant_name_in(settings: Optional[Mapping[str, Any]], tenant_id: int) -> Optional[str]:
    """The saved name, the platform's own when none is saved, ``None`` when unusable."""
    from core.tenant import DEFAULT_AI  # noqa: PLC0415

    if settings is None:
        return None
    configured = settings.get("assistant_name")
    if not isinstance(configured, (str, type(None))):
        logger.error("[COMMERCE_RUNTIME_PILOT] assistant settings are not an object with a text "
                     "name tenant=%s", tenant_id)
        return None
    return configured if (configured or "").strip() else str(DEFAULT_AI["assistant_name"])


def _saved_assistant_name(db: Any, tenant_id: int) -> Optional[str]:
    """The name the merchant saved for its assistant, or the platform's own when none is."""
    return _assistant_name_in(_saved_ai_settings(db, tenant_id), tenant_id)


def _reply_style_in(settings: Optional[Mapping[str, Any]], tenant_id: int) -> Dict[str, str]:
    """The reply language and tone the merchant chose, as the platform defines them.

    Only the two structured choices, and only in the meaning the platform
    already gives them on its legacy path (``tenant_overlay``): one definition
    of what "arabic" or "friendly" asks of a reply, whichever runtime answers.
    A merchant who chose English or both languages is told exactly that; no
    dialect is inferred from anything else. Missing or blank means the
    platform's own default, exactly as for the name.

    Deliberately not carried, and why:

    * ``reply_length`` — every meaning the platform defines for it is a line
      cap, and a cap would decide how much detail a comparison or a requested
      explanation may carry, which is the model's to judge.
    * ``owner_instructions`` and ``assistant_role`` — free text. The default
      ones already mix a line cap, a gendered persona and style advice, and a
      merchant's may carry operational claims the truth rules exist to keep out
      of the reply. Carrying them needs a reviewed scope of its own.

    A language value the platform defines no meaning for is left out rather
    than guessed. A tone the platform defines no meaning for — the dashboard's
    ``professional`` and ``sales`` — is carried as the merchant saved it: the
    choice is theirs, and the word names it.
    """
    from core.tenant import DEFAULT_AI  # noqa: PLC0415
    from modules.ai.prompts.tenant_overlay import LANGUAGE_MAP, TONE_MAP  # noqa: PLC0415

    style: Dict[str, str] = {}
    if settings is None:
        return style

    def chosen(key: str) -> Optional[str]:
        value = settings.get(key)
        if value is None or (isinstance(value, str) and not value.strip()):
            value = DEFAULT_AI.get(key)
        if not isinstance(value, str) or not value.strip():
            logger.warning("[COMMERCE_RUNTIME_PILOT] reply %s setting is not text tenant=%s",
                           key, tenant_id)
            return None
        return value.strip()

    language = chosen("default_language")
    if language is not None:
        meaning = LANGUAGE_MAP.get(language)
        if meaning:
            style["reply_language"] = meaning
        else:
            logger.warning("[COMMERCE_RUNTIME_PILOT] reply language setting has no platform "
                           "meaning tenant=%s", tenant_id)
    tone = chosen("reply_tone")
    if tone is not None:
        style["reply_tone"] = TONE_MAP.get(tone) or tone
    return style


def _context_preamble(db: Any, tenant_id: int, convo: Any, customer_name: str) -> Dict[str, Any]:
    """Trusted facts the platform hands the model as data, never as wording."""
    preamble: Dict[str, Any] = {"channel": "whatsapp"}
    name = str(customer_name or "").strip()
    if name:
        preamble["verified_customer_name"] = name
    language = str(getattr(convo, "language", "") or "").strip()
    if language:
        preamble["conversation_language"] = language
    settings = _saved_ai_settings(db, tenant_id)
    assistant_name = _assistant_name_in(settings, tenant_id)
    if assistant_name is not None:
        preamble["assistant_name"] = assistant_name
    preamble.update(_reply_style_in(settings, tenant_id))
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
                    observation.record(seen[0], seen[1], row_ids=(),
                                       duplicate_suppressed=bool(sink.get("duplicate_suppressed")))
                reset_wire_audit(token)

        return _awaited(_observed_send, loop, sink)

    return send


def _awaited(observed_send: Any, loop: Any, sink: Dict[str, Any]) -> Tuple[str, Optional[str], Optional[int]]:
    """Wait on a send scheduled back onto the event loop and read its outcome."""
    future = asyncio.run_coroutine_threadsafe(observed_send(), loop)
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


def _send_card_factory(phone_id: str, tenant_id: int, db: Any, loop: Any,
                       observation: WireObservation) -> Any:
    """The same established sender, for a reply that carries one product card.

    Identical in every guarantee to the two senders beside it — the same
    ``_post_wa``, so the same sanitiser, AI-disabled gate, burst throttle and
    outbound dedup — and the same wire observation over the body text the model
    wrote. The photo and the link are the platform's structured payload,
    composed from this turn's observations and verified before they reach here.

    WhatsApp's ``cta_url`` message carries the image header, the body and the
    button as **one** message, so this is one send: the loop still reserves one
    delivery intent for the turn and the ledger still records one receipt.

    ``keep_textual_url=True`` because the link must remain readable in the body
    as well: a customer whose client does not render the button still has the
    address, and removing it would be the platform deciding the answer is
    poorer than the model wrote it.
    """
    def send_card(recipient: str, text: str, image_url: str, button_url: str,
                  button_label: str) -> Tuple[str, Optional[str], Optional[int]]:
        from core.outbound_wire_audit import (  # noqa: PLC0415
            bind_wire_observation,
            observed_wire_text,
            reset_wire_audit,
        )
        from routers.whatsapp_webhook import _send_cta_url  # noqa: PLC0415

        sink: Dict[str, Any] = {}

        async def _observed_send() -> bool:
            token = bind_wire_observation(int(tenant_id), recipient, text)
            try:
                return bool(await _send_cta_url(
                    phone_id, recipient, text,
                    button_label, button_url,
                    int(tenant_id), db,
                    header_image_url=image_url,
                    keep_textual_url=True,
                    _result_sink=sink,
                ))
            finally:
                seen = observed_wire_text(int(tenant_id), recipient)
                if seen is not None:
                    observation.record(seen[0], seen[1],
                                       duplicate_suppressed=bool(sink.get("duplicate_suppressed")))
                reset_wire_audit(token)

        return _awaited(_observed_send, loop, sink)

    return send_card


def _send_list_factory(phone_id: str, tenant_id: int, db: Any, loop: Any,
                       observation: WireObservation) -> Any:
    """The same established sender, for a reply that carries selectable rows.

    Identical in every guarantee to the text sender above — the same
    ``_post_wa``, so the same sanitiser, AI-disabled gate, burst throttle and
    outbound dedup — and the same wire observation over the body text the model
    wrote. The rows beside it are the platform's structured payload, composed
    from this turn's observations and already verified before they reach here.

    A refusal is reported as a refusal. That matters more here than on the text
    path: a definitively rejected list is what permits the ledger's one bounded
    recovery, which sends this same answer as plain text.
    """
    def send_list(recipient: str, text: str, rows: Sequence[Mapping[str, Any]],
                  button: str) -> Tuple[str, Optional[str], Optional[int]]:
        from core.outbound_wire_audit import (  # noqa: PLC0415
            bind_wire_observation,
            observed_wire_text,
            reset_wire_audit,
        )
        from routers.whatsapp_webhook import _send_list_reply  # noqa: PLC0415

        sink: Dict[str, Any] = {}

        async def _observed_send() -> bool:
            token = bind_wire_observation(int(tenant_id), recipient, text)
            try:
                return bool(await _send_list_reply(
                    phone_id=phone_id, to=recipient, body_text=text,
                    rows=[dict(row) for row in rows], button_label=button,
                    _tenant_id=int(tenant_id), _db=db, _blocked_path=BLOCKED_PATH,
                    _result_sink=sink,
                ))
            finally:
                seen = observed_wire_text(int(tenant_id), recipient)
                if seen is not None:
                    observation.record(seen[0], seen[1], row_ids=list(sink.get("list_row_ids") or ()),
                                       duplicate_suppressed=bool(sink.get("duplicate_suppressed")))
                reset_wire_audit(token)

        return _awaited(_observed_send, loop, sink)

    return send_list


@dataclasses.dataclass(frozen=True)
class RuntimeClaim:
    """One inbound message this runtime has positively established it owns.

    Scoped on purpose. A bare "the runtime owns something" boolean cannot be
    checked later, and a stale or mis-plumbed one would silence a turn that was
    never claimed; this names the tenant, the normalised recipient and the
    provider message id it was established for, so whoever honours it can first
    confirm it is about the turn in front of them.
    """

    tenant_id: int
    recipient: str                 # normalised, as the guard normalises it
    provider_message_id: str
    basis: str                     # configured | admitted_open | admitted_finished

    def applies_to(self, *, tenant_id: Any, recipient: Any, provider_message_id: Any) -> bool:
        """Whether this claim is about that turn. Never raises.

        A claim that cannot be checked is **held**, not dropped: it was
        established positively, and the one outcome that must not follow from a
        failure here is handing the turn to another owner.
        """
        try:
            if int(tenant_id) != self.tenant_id:
                return False
            if str(provider_message_id or "").strip() != self.provider_message_id:
                return False
            from core.commerce_runtime import pilot_guard  # noqa: PLC0415

            normalized = pilot_guard.normalize_recipient(recipient)
            return not normalized or normalized == self.recipient
        except Exception:  # noqa: BLE001 - an uncheckable claim still holds
            logger.warning("[COMMERCE_RUNTIME_PILOT] claim scope could not be checked; "
                           "holding it rather than releasing the turn")
            return True


def _established_by_record(db: Any, *, tenant_id: Any, phone_id: str, recipient: str,
                           identity: str) -> Optional[RuntimeClaim]:
    """A held claim when a durable acceptance record names this inbound, else ``None``.

    Asked only after something failed and only when the guard did **not**
    already decide positively — one read of one row, never a second evaluation
    of the guard. The provider was told we had this message; something owes it
    an answer, and that something is not another owner.
    """
    try:
        from core.commerce_runtime import handover  # noqa: PLC0415

        accepted = handover.accepted_inbound(
            db, tenant_id=int(tenant_id),
            channel_connection_ref=f"wa:{str(phone_id or '').strip()}",
            provider_message_id=identity)
        if accepted is not None:
            return RuntimeClaim(tenant_id=int(tenant_id), recipient=recipient,
                                provider_message_id=identity,
                                basis=BASIS_OWNERSHIP_UNAVAILABLE)
    except Exception:  # noqa: BLE001 - an unreadable record establishes nothing
        logger.warning("[COMMERCE_RUNTIME_PILOT] deferred record unreadable while "
                       "establishing scope tenant=%s", tenant_id)
    return None


def _recovery_grant(db: Any, *, tenant_id: Any, phone_id: str, identity: str,
                    barrier: Any) -> Optional[Any]:
    """The recovery grant covering this exact inbound, verified, or ``None``.

    A grant is the runner's statement that it is replaying one accepted entry.
    It is honoured only when it names this tenant, this channel connection and
    this provider message id, when the barrier is open or draining (never
    settled or released), and when a **pending durable acceptance** exists for
    the identity — the grant authorises nothing the database does not already
    owe. Never raises.
    """
    try:
        from core.commerce_runtime import handover  # noqa: PLC0415
        from services import commerce_runtime_recovery as runner  # noqa: PLC0415

        grant = runner.current_grant()
        if grant is None:
            return None
        channel = f"wa:{str(phone_id or '').strip()}"
        if not grant.names(tenant_id=tenant_id, channel_connection_ref=channel,
                           provider_message_id=identity):
            return None
        if getattr(barrier, "state", None) not in (handover.STATE_OPEN, handover.STATE_DRAINING):
            return None
        record = handover.accepted_inbound(
            db, tenant_id=int(tenant_id), channel_connection_ref=channel,
            provider_message_id=identity)
        if record is None or not record.pending:
            return None
        return grant
    except Exception:  # noqa: BLE001 - an unverifiable grant authorises nothing
        logger.warning("[COMMERCE_RUNTIME_PILOT] recovery grant could not be verified "
                       "tenant=%s", tenant_id)
        return None


def commerce_runtime_claims_inbound(
    db: Any,
    *,
    tenant_id: int,
    phone_id: str,
    to: str,
    text: str,
    wa_msg_id: Optional[str],
) -> Optional[RuntimeClaim]:
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
    question that cannot be answered yields no claim, which leaves the
    dispatcher exactly as it is today.

    The scope decision is captured the moment it is made. Everything after it
    is a dependent read that can fail — the barrier, the fleet row, the ledger —
    and when one does, the held claim is built from **that** decision, not from
    a second evaluation of the guard that could fail in its own way and release
    a turn the first one had already established as the runtime's.
    """
    from core.commerce_runtime import pilot_guard  # noqa: PLC0415

    try:
        recipient = pilot_guard.normalize_recipient(to)
        identity = str(wa_msg_id or "").strip()
    except Exception:  # noqa: silent-ok — without a normalised recipient and a provider message id there is no scope to claim
        return None
    if not recipient or not identity:
        # Nothing to scope a claim to. The dispatcher keeps its own owners.
        return None

    def claim(basis: str) -> RuntimeClaim:
        return RuntimeClaim(tenant_id=int(tenant_id), recipient=recipient,
                            provider_message_id=identity, basis=basis)

    decision = None
    try:
        decision = pilot_guard.evaluate_pilot_route(
            db, tenant_id=tenant_id, customer_phone=to, phone_number_id=phone_id,
            inbound_text=text,
        )
        if decision.reason in NO_RUNTIME_WORK_POSSIBLE:
            return None

        # This tenant's shared barrier, not this process's flag. It is the only
        # thing that can say the same word to every replica at the same moment.
        from core.commerce_runtime import handover  # noqa: PLC0415

        barrier = handover.read_barrier(db, tenant_id=int(tenant_id))
        # Report the barrier this process *observed*, exactly as observed. A
        # heartbeat that re-read the generation at write time would claim
        # convergence this worker has not reached.
        handover.note_worker(db, tenant_id=int(tenant_id),
                             observed_generation=barrier.generation,
                             observed_state=barrier.state)

        # Only a conversation this pilot would otherwise own is affected by a
        # handover. Everything else — a recipient outside the allowlist, an
        # unverified connection, an empty inbound — was never the runtime's and
        # keeps exactly the behaviour it has today, drain or no drain.
        process_draining = decision.reason == pilot_guard.PILOT_DRAINING
        affected = decision.permitted or process_draining
        # A conversation is deferred when it is one this pilot would own and the
        # tenant is not admitting new work: draining, settled-but-not-reopened,
        # released, or this process taken out of rotation.
        deferring = affected and (barrier.draining or barrier.settled or barrier.released
                                  or process_draining)

        # A turn this runtime already admitted is never new work, so a drain
        # does not buffer it: draining is what finishes outstanding work, and
        # withholding a redelivery of it from its own runtime is how it would be
        # abandoned instead.
        owned = None
        if deferring or not decision.permitted:
            owned = _admitted_runtime_turn(
                tenant_id=int(tenant_id), phone_id=phone_id, wa_msg_id=wa_msg_id,
                refusal=decision.reason)
        if owned is not None:
            logger.warning(
                "[COMMERCE_RUNTIME_PILOT] claiming inbound the runtime already owns "
                "turn=%s finished=%s tenant=%s guard_reason=%s",
                owned.turn_id, owned.finished, tenant_id, decision.reason)
            return claim(BASIS_ADMITTED_FINISHED if owned.finished else BASIS_ADMITTED_OPEN)

        if deferring:
            # Accepted work a recovery run is handing back is not new work: the
            # provider was told we had it and the settlement counts it. Under a
            # verified grant it is claimed for the runtime rather than deferred
            # a second time.
            grant = _recovery_grant(db, tenant_id=int(tenant_id), phone_id=phone_id,
                                    identity=identity, barrier=barrier)
            if grant is not None:
                logger.warning(
                    "[COMMERCE_RUNTIME_PILOT] claiming accepted inbound under a recovery "
                    "grant entry=%s tenant=%s barrier=%s", grant.entry_id, tenant_id,
                    barrier.state)
                return claim(BASIS_RECOVERY_ADMITTED)

            # Draining does not release this conversation to the legacy path:
            # the runtime it is handing over from may still have a send in
            # flight, and a second answer is exactly what must not happen. The
            # inbound is recorded with its own identity and payload, and
            # answered by nobody until an operator disposes of it.
            #
            # A tenant that is *settled* is in the window between "nothing is
            # outstanding" and an operator reopening ingress. New work must not
            # start there either, and it must not be lost: it is recorded the
            # same way.
            reason = (handover.REASON_DRAIN_BUFFERED if barrier.draining
                      else handover.REASON_PROCESS_DRAINING if process_draining
                      else handover.REASON_SETTLED_WINDOW)
            record = _defer(db, tenant_id=int(tenant_id), decision=decision, phone_id=phone_id,
                            recipient=recipient, identity=identity, text=text,
                            reason=reason, generation=barrier.generation)
            if record is None:
                # Nothing durable was written, so nothing may be answered and
                # nothing may be claimed as accounted for. The turn is still
                # withheld — releasing it to another owner while a send may be
                # in flight is the worse failure — and the operator is told.
                logger.error("[COMMERCE_RUNTIME_PILOT] could not defer inbound tenant=%s "
                             "provider_message_id=%s — withheld but UNRECORDED; account for "
                             "it by hand before settling", tenant_id, identity)
            else:
                logger.warning("[COMMERCE_RUNTIME_PILOT] deferred during handover tenant=%s "
                               "generation=%s barrier=%s reason=%s entry=%s",
                               tenant_id, barrier.generation, barrier.state, reason, record.id)
            return claim(BASIS_DRAIN_BUFFERED)

        if decision.permitted:
            return claim(BASIS_CONFIGURED)
        return None
    except Exception:
        # Up to the guard, an undecidable question is not a claim: nothing
        # established that this inbound was ever the runtime's, and the
        # dispatcher keeps the owners it has today.
        #
        # After the guard has said this tenant, recipient and connection are
        # the pilot's, the opposite is true. A barrier that cannot be read, a
        # schema that is not there, a ledger that will not answer — none of
        # them are evidence that the runtime does not own this turn, and
        # handing it to the legacy path or the COD route on the strength of a
        # failed read is how a conversation the runtime may still be answering
        # gets a second answer. The obligation is kept and execution is
        # refused: nobody answers, and the operator is told. The decision that
        # is held is the one already taken — nothing is evaluated again.
        if decision is not None and (decision.permitted
                                     or decision.reason == pilot_guard.PILOT_DRAINING):
            logger.exception(
                "[COMMERCE_RUNTIME_PILOT] ownership established, then a dependent read "
                "failed tenant=%s provider_message_id=%s — holding the turn; nobody "
                "answers it until the runtime can", tenant_id, identity)
            return claim(BASIS_OWNERSHIP_UNAVAILABLE)
        established = _established_by_record(db, tenant_id=tenant_id, phone_id=phone_id,
                                             recipient=recipient, identity=identity)
        if established is not None:
            logger.exception(
                "[COMMERCE_RUNTIME_PILOT] a durable acceptance names this inbound and its "
                "ownership could not be evaluated tenant=%s provider_message_id=%s — "
                "holding the turn", tenant_id, identity)
            return established
        logger.warning("[COMMERCE_RUNTIME_PILOT] ownership could not be decided and "
                       "nothing establishes it tenant=%s — dispatcher unchanged", tenant_id)
        return None


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

    grant = None
    identity = _inbound_identity(wa_msg_id, convo)

    def granted() -> Optional[Any]:
        """A verified recovery grant for this exact inbound, asked at most once."""
        nonlocal grant
        if grant is None:
            try:
                from core.commerce_runtime import handover  # noqa: PLC0415

                barrier = handover.read_barrier(db, tenant_id=int(tenant_id))
            except Exception:  # noqa: silent-ok — an unreadable barrier grants nothing; the caller then defers exactly as it would without a grant, and the barrier read below logs the failure
                return None
            grant = _recovery_grant(db, tenant_id=int(tenant_id), phone_id=phone_id,
                                    identity=identity, barrier=barrier)
        return grant

    if not decision.permitted and decision.reason == pilot_guard.PILOT_DRAINING:
        # A draining pilot takes no new turns and finishes its own. Only a turn
        # this runtime actually admitted and never finished re-enters here —
        # or accepted work a recovery run is handing back under a verified
        # grant, which is not new work either.
        owned = admitted()
        if owned is not None and owned.unfinished:
            logger.warning("[COMMERCE_RUNTIME_PILOT] draining: finishing turn=%s tenant=%s",
                           owned.turn_id, tenant_id)
            decision = route(finishing_open_work=True)
        elif owned is None and granted() is not None:
            logger.warning("[COMMERCE_RUNTIME_PILOT] draining: meeting accepted entry=%s "
                           "under a recovery grant tenant=%s", grant.entry_id, tenant_id)
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

    # The tenant's shared barrier. A drain stops new work everywhere at once,
    # so a turn refused by it is refused here rather than built and then thrown
    # away at admission. A re-entry that finishes a turn this runtime already
    # admitted is *not* new work and still runs: that is what a drain is for.
    if not _barrier_admits_new_work(db, tenant_id=int(tenant_id)):
        owned = admitted()
        if (owned is None or not owned.unfinished) and (owned is not None or granted() is None):
            from core.commerce_runtime import handover  # noqa: PLC0415
            from core.commerce_runtime import runtime_entry as _entry  # noqa: PLC0415

            recorded = _defer(
                db, tenant_id=int(tenant_id), decision=decision, phone_id=phone_id,
                recipient=str(decision.recipient or to), identity=identity,
                text=text, reason=handover.REASON_ADMISSION_REFUSED,
                generation=handover.read_barrier(db, tenant_id=int(tenant_id)).generation)
            logger.warning("[COMMERCE_RUNTIME_PILOT] handover barrier closed tenant=%s "
                           "deferred=%s — no new turn, and the turn is given to nobody else",
                           tenant_id, None if recorded is None else recorded.id)
            return PilotResult(handled=True, reason=_entry.HANDOVER_BARRIER)
        if owned is None and grant is not None:
            # Accepted, never admitted, and a recovery run is handing it back:
            # the barrier is draining and this is the work a drain exists to
            # finish. Admission is still ordered against the barrier on the
            # admitting connection — open or draining admits it, settled or
            # released refuses it.
            logger.warning("[COMMERCE_RUNTIME_PILOT] barrier closed to new work; admitting "
                           "accepted entry=%s under a recovery grant tenant=%s",
                           grant.entry_id, tenant_id)

    # From here the commerce runtime owns the turn. Nothing below may raise back
    # to the caller: if it did, the legacy brain would run for an inbound message
    # this runtime has already (possibly) answered.
    try:
        return await _own_turn(
            db=db, tenant_id=int(tenant_id), phone_id=phone_id, to=to, text=text, convo=convo,
            wa_msg_id=wa_msg_id, inbound_metadata=inbound_metadata, trace=trace,
            decision=decision, customer_name=customer_name, recovery_grant=grant,
        )
    except Exception:  # noqa: BLE001 - the turn stays ours; it is simply a failed turn
        logger.exception("[COMMERCE_RUNTIME_PILOT] turn failed after the route was taken tenant=%s",
                         tenant_id)
        return PilotResult(handled=True, reason="internal_error")


def _defer(db: Any, *, tenant_id: int, decision: Any, phone_id: str, recipient: str,
           identity: str, text: str, reason: str, generation: int) -> Optional[Any]:
    """Record one inbound the runtime accepted but will not start now.

    Durable, scoped and recoverable: the connection this arrived on, the
    recipient, the provider's own message id and the text, so a replay is a
    replay rather than a note that something was lost. Never raises.
    """
    from core.commerce_runtime import handover  # noqa: PLC0415

    try:
        return handover.record_inbound(
            db, tenant_id=int(tenant_id), phone_number_id=str(phone_id or ""),
            channel_connection_ref=str(decision.connection_ref or f"wa:{phone_id}"),
            recipient=str(recipient), provider_message_id=str(identity),
            payload={"text": str(text or "")}, reason=str(reason),
            barrier_generation=int(generation))
    except Exception as exc:  # noqa: BLE001
        logger.error("[COMMERCE_RUNTIME_PILOT] deferred record failed tenant=%s error=%s",
                     tenant_id, type(exc).__name__)
        return None


def _inbound_identity(wa_msg_id: Optional[str], convo: Any) -> str:
    """The identity a deferred record and an admitted turn agree on."""
    return (str(wa_msg_id or "").strip()
            or f"conversation-{int(getattr(convo, 'id', 0) or 0)}:no-wamid")


def _settle_deferred(db: Any, *, tenant_id: int, decision: Any, phone_id: str, recipient: str,
                     identity: str, text: str, report: Any) -> None:
    """Close the durable record this turn was accepted under, or open one.

    A turn that reached a terminal is finished, so its record stops counting
    against settlement. One the barrier refused at admission never started, so a
    record is written for it instead: the provider was told we had the message
    and nothing has answered it.
    """
    from core.commerce_runtime import handover  # noqa: PLC0415
    from core.commerce_runtime import runtime_entry as _entry  # noqa: PLC0415

    connection = str(getattr(decision, "connection_ref", "") or f"wa:{phone_id}")
    try:
        if getattr(report, "reason", "") == _entry.HANDOVER_BARRIER:
            _defer(db, tenant_id=tenant_id, decision=decision, phone_id=phone_id,
                   recipient=recipient, identity=identity, text=text,
                   reason=handover.REASON_ADMISSION_REFUSED,
                   generation=handover.read_barrier(db, tenant_id=tenant_id).generation)
            return
        # A turn id is not completion evidence. It means a row was admitted,
        # not that anything reached a terminal — ``ownership_unavailable``,
        # ``admission_conflict`` and an internal error all carry one. The
        # obligation is closed only against the authoritative terminal record
        # for this exact tenant, channel and provider message id, and the
        # terminal it was closed against is stored with it.
        from core.commerce_runtime import recovery  # noqa: PLC0415

        owned = recovery.admitted_turn_for(
            tenant_id=int(tenant_id), phone_number_id=phone_id,
            provider_message_id=identity)
        if owned is not None and owned.finished:
            handover.resolve_inbound(
                db, tenant_id=tenant_id, channel_connection_ref=connection,
                provider_message_id=identity,
                evidence={"terminal_for_turn_id": int(owned.turn_id),
                          "reported_reason": str(getattr(report, "reason", "")),
                          "checked_at": _dt.datetime.now(_dt.timezone.utc).isoformat()})
            return
        logger.warning(
            "[COMMERCE_RUNTIME_PILOT] deferred record stays pending tenant=%s "
            "provider_message_id=%s reason=%s turn=%s — no terminal to close it against",
            tenant_id, identity, getattr(report, "reason", ""),
            None if owned is None else owned.turn_id)
    except Exception as exc:  # noqa: BLE001 - the turn is finished either way
        logger.warning("[COMMERCE_RUNTIME_PILOT] deferred bookkeeping failed tenant=%s "
                       "error=%s", tenant_id, type(exc).__name__)


def _barrier_admits_new_work(db: Any, *, tenant_id: int) -> bool:
    """Whether this tenant's shared barrier still admits new work. Fails closed."""
    from core.commerce_runtime import handover  # noqa: PLC0415

    return handover.barrier_admits_new_work(db, tenant_id=int(tenant_id))


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
    recovery_grant: Any = None,
) -> PilotResult:
    from core.commerce_runtime import pilot_guard  # noqa: PLC0415
    from core.commerce_runtime import runtime_entry as entry  # noqa: PLC0415
    from database.session import SessionLocal, engine  # noqa: PLC0415

    conversation_id = int(getattr(convo, "id", 0) or 0)
    provider_message_id = str(wa_msg_id or "").strip() or f"conversation-{conversation_id}:no-wamid"
    loop = asyncio.get_running_loop()
    wire = WireObservation()
    send = _send_factory(phone_id, int(tenant_id), db, loop, wire)
    send_list = _send_list_factory(phone_id, int(tenant_id), db, loop, wire)
    send_card = _send_card_factory(phone_id, int(tenant_id), db, loop, wire)

    def admission_barrier(conn: Any) -> bool:
        """Read the shared barrier on the admission transaction's own connection.

        The claim was taken a moment ago on the request thread; this is asked
        again where it can be *ordered* against a drain rather than merely
        raced with one. It runs only for a new admission — a re-entry that
        finishes work this runtime already owns never reaches it.
        """
        from core.commerce_runtime import handover  # noqa: PLC0415

        if recovery_grant is not None:
            # Accepted work under a recovery grant: admitted while the barrier
            # is open or draining, refused while it is settled or released —
            # read on this same connection, under the same shared lock. The
            # grant's durable entry is locked and checked **here** as well: it
            # must still be pending and still name this tenant, connection and
            # identity, or an operator's disposition has withdrawn what the
            # grant rested on and nothing is admitted.
            return handover.admits_recovery_on(
                conn, tenant_id=int(tenant_id),
                entry_id=int(recovery_grant.entry_id),
                channel_connection_ref=str(recovery_grant.channel_connection_ref),
                provider_message_id=str(recovery_grant.provider_message_id))
        return handover.admits_new_work_on(conn, tenant_id=int(tenant_id))

    context_preamble = _context_preamble(db, int(tenant_id), convo, customer_name)

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
            transport=entry.whatsapp_reply_transport(send, send_list,
                                                     recipient=str(decision.recipient),
                                                     send_card=send_card),
            instructions=_instructions(),
            model=str(decision.model or ""),
            admission_barrier=admission_barrier,
            budget=pilot_guard.pilot_budget(),
            context_preamble=context_preamble,
            history=_prior_turns(db, tenant_id=int(tenant_id), conversation_id=conversation_id,
                                 phone=to, current_text=text),
        )

    report = await asyncio.to_thread(run)
    _settle_deferred(db, tenant_id=int(tenant_id), decision=decision, phone_id=phone_id,
                     recipient=str(decision.recipient or to),
                     identity=provider_message_id, text=text, report=report)
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
        from core.commerce_runtime import recent_products as rp  # noqa: PLC0415
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
                # The rows this message actually carried, read back from the
                # wire rather than from the reserved intent. A later tap is
                # checked against these, so a reply that offered no list — or
                # whose list the provider refused — leaves nothing tappable.
                rp.CHOICE_ROW_IDS_KEY: list(wire.row_ids),
                # The card this message actually delivered, for the same reason
                # and with the same discipline: it is written only because this
                # send was accepted and identified, so recent-card suppression
                # rests on a card the customer saw rather than on one a payload
                # once held. A reply that carried none stores nothing.
                **({rp.CARD_PRODUCT_ID_KEY: int(report.card_product_id)}
                   if report.card_product_id else {}),
            },
        )
    except Exception:  # noqa: BLE001 - the send already happened; persistence must not undo it
        logger.exception("[COMMERCE_RUNTIME_PILOT] outbound persist failed turn=%s", report.turn_id)


__all__ = ["BLOCKED_PATH", "HISTORY_LIMIT", "NO_RUNTIME_WORK_POSSIBLE", "OWNED_PREFIX",
           "PilotResult", "RuntimeClaim", "SEND_WAIT_SECONDS", "TRACE_SOURCE",
           "UNFINISHED_PREFIX", "WIRE_UNOBSERVED", "WireObservation",
           "commerce_runtime_claims_inbound", "maybe_handle_with_commerce_runtime"]

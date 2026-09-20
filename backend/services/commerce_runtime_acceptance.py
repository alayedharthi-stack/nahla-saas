"""What "we have your message" is allowed to mean.

Both WhatsApp entry points acknowledge first: they parse the body, return 200,
and process in a background task. For the legacy path that is the right trade —
the provider gets its answer inside its timeout and the existing deduplication
makes a retry safe.

For a **pilot-scoped** message it is not, because a 200 ends the provider's
retries. Between that 200 and the runtime writing its turn there is a window in
which the worker can die, and the message is then gone: nobody will send it
again and nothing recorded that it arrived. Received is not processed, and
deduplicated is not processed either.

So before a pilot-scoped inbound is acknowledged, it is written to
``commerce_runtime_deferred_inbound`` — tenant, channel connection, recipient,
the provider's own message id and the text — and the acknowledgement is a
promise the database can keep. The runtime resolves that row when the turn
reaches a terminal; until then it is a customer owed an answer, and the
settlement check counts it as one.

If the write fails, the response must say so. A 200 would claim an acceptance
that did not happen, and a log line is not recovery. The request is answered
with a retryable status instead, **before** anything is spawned, so the provider
redelivers the whole batch and nothing in it has been processed once already.

Two more things "we have your message" must never mean:

* **that we took it from somebody we could not authenticate.** The legacy path
  may run Meta's signature in audit mode; a pilot obligation may not. A batch
  carrying pilot-scoped work is recorded and processed only when the caller
  states the signature was valid — otherwise nothing is recorded, nothing is
  spawned and the request is answered retryable, because the only sender whose
  retries matter is Meta, and a Meta request we could not authenticate is a
  configuration fault on our side that a retry survives;
* **that a nonce is an acceptance.** Replay protection claims a nonce before
  anything is durable. A process that dies in between leaves the nonce and no
  record, and the provider's retry then looks like a replay of something that
  was accepted. :func:`durable_status` is what the route asks before it lets a
  nonce alone answer 200.

Scope and cost
==============
Nothing here runs while the pilot is disabled, which is its state by default:
the first check is the switch and it returns immediately. For an enabled pilot
it is one indexed lookup per message plus one insert per pilot-scoped message.
A batch that mixes pilot-scoped and unrelated messages is handled as a batch:
the unrelated ones are untouched and unrecorded, and a refusal for one
pilot-scoped message refuses the request rather than half-processing it.
"""
from __future__ import annotations

import dataclasses
import logging
from typing import Any, Dict, List, Mapping, Optional, Tuple

logger = logging.getLogger("nahla.commerce_runtime.acceptance")


# Why a request was *not* accepted. Each is answered retryable by the route,
# and each names something an operator can act on.
REFUSED_NOT_PERSISTED = "not_persisted"
REFUSED_UNDECIDABLE = "scope_undecidable"
REFUSED_UNAUTHENTICATED = "unauthenticated"
REFUSED_RELEASED = "barrier_released"


@dataclasses.dataclass(frozen=True)
class Acceptance:
    """Whether this request may be acknowledged, and what was recorded."""

    accepted: bool
    recorded: Tuple[str, ...] = ()
    failed: Tuple[str, ...] = ()
    scoped: int = 0
    reason: str = ""

    @property
    def ok(self) -> bool:
        return self.accepted


@dataclasses.dataclass(frozen=True)
class DurableStatus:
    """Whether every pilot-scoped message in a body already has a record.

    Asked when replay protection says a body was seen before. ``undurable``
    names the pilot-scoped identities with **no** record — the provider was
    answered for them, or was about to be, by a process that never wrote them
    down — and ``undecidable`` the ones whose scope could not be established.
    Either one means a nonce is not evidence of an acceptance.
    """

    scoped: int = 0
    undurable: Tuple[str, ...] = ()
    undecidable: Tuple[str, ...] = ()

    @property
    def retryable(self) -> bool:
        """Whether the request has to be treated as a first attempt."""
        return bool(self.undurable or self.undecidable)


def _messages(body: Mapping[str, Any]) -> List[Dict[str, Any]]:
    """Every customer message in this body, with the connection it arrived on.

    Statuses, echoes and lifecycle events are not customer messages and are not
    returned: nothing owes them an answer.
    """
    found: List[Dict[str, Any]] = []
    try:
        for entry in body.get("entry", []) or []:
            for change in (entry or {}).get("changes", []) or []:
                value = (change or {}).get("value") or {}
                phone_number_id = str((value.get("metadata") or {}).get(
                    "phone_number_id", "") or "")
                for message in value.get("messages", []) or []:
                    if not isinstance(message, dict):
                        continue
                    found.append({"phone_number_id": phone_number_id, "message": message})
    except Exception as exc:  # noqa: BLE001 - a body we cannot walk carries nothing for us
        logger.warning("[COMMERCE_RUNTIME_ACCEPT] body could not be walked error=%s",
                       type(exc).__name__)
        return []
    return found


def _text_of(message: Mapping[str, Any]) -> str:
    """The customer's words, as the payload a replay would need."""
    for reader in (
        lambda m: (m.get("text") or {}).get("body"),
        lambda m: ((m.get("interactive") or {}).get("button_reply") or {}).get("title"),
        lambda m: ((m.get("interactive") or {}).get("list_reply") or {}).get("title"),
        lambda m: (m.get("button") or {}).get("text"),
        lambda m: (m.get("image") or {}).get("caption"),
        lambda m: (m.get("document") or {}).get("caption"),
    ):
        try:
            value = reader(message)
        except Exception:  # noqa: BLE001 - a shape we do not know carries no text
            continue
        if isinstance(value, str) and value.strip():
            return value
    return ""


# What one message in the batch turned out to be.
IN_SCOPE = "in_scope"            # pilot traffic: it must be durable before we ack
OUT_OF_SCOPE = "out_of_scope"    # verified as not the pilot's: untouched, as today
UNDECIDABLE = "undecidable"      # the lookup failed, or two tenants claim the number


def _classify(db: Any, *, phone_number_id: str, recipient: str) -> Tuple[str, Any, str]:
    """``(verdict, target, detail)`` for one message.

    Three outcomes, because two would be a lie. "Verified out of scope" is a
    statement about the traffic — an allowlisted tenant does not own this
    connection, or this recipient is not one the pilot answers — and that
    traffic keeps exactly the behaviour it has today. "Undecidable" is a
    statement about *us*: the scope lookup failed, or more than one allowlisted
    tenant claims the number. Treating the second as the first is how
    pilot-owned work gets acknowledged and dropped, so it is answered
    separately and the request is not acknowledged as accepted.

    The connection that comes back is the **verified** one — the row the guard
    checked — and it is what travels into persistence. Nothing re-derives a
    channel reference from the payload.
    """
    from core.commerce_runtime import pilot_guard  # noqa: PLC0415

    scope = pilot_guard.resolve_pilot_scope(db, phone_number_id=phone_number_id)
    if scope.status == pilot_guard.SCOPE_NOT_OURS:
        return OUT_OF_SCOPE, None, scope.detail
    if not scope.resolved:
        return UNDECIDABLE, None, f"scope_{scope.status}:{scope.detail}"

    try:
        # The verified row travels in. The guard would otherwise look the
        # connection up a second time, and a second lookup that fails answers
        # ``connection_not_verified`` — which maps to "verified out of scope"
        # and would let a failed read acknowledge and drop pilot-owned work.
        decision = pilot_guard.evaluate_pilot_route(
            db, tenant_id=scope.tenant_id, customer_phone=recipient,
            phone_number_id=phone_number_id, inbound_text="_",
            verified=(scope.connection_ref, scope.connection_id),
        )
    except Exception as exc:  # noqa: BLE001 - an unanswerable guard decides nothing
        logger.error("[COMMERCE_RUNTIME_ACCEPT] guard failed tenant=%s error=%s",
                     scope.tenant_id, type(exc).__name__)
        return UNDECIDABLE, None, f"guard_error:{type(exc).__name__}"

    if decision.reason in {pilot_guard.PILOT_DISABLED, pilot_guard.TENANT_NOT_ALLOWLISTED,
                           pilot_guard.RECIPIENT_MISSING,
                           pilot_guard.RECIPIENT_UNNORMALIZABLE,
                           pilot_guard.RECIPIENT_NOT_ALLOWLISTED,
                           pilot_guard.CONNECTION_NOT_VERIFIED}:
        return OUT_OF_SCOPE, None, decision.reason
    if decision.reason == pilot_guard.GUARD_ERROR:
        return UNDECIDABLE, None, decision.reason
    normalized = decision.recipient or pilot_guard.normalize_recipient(recipient)
    if not normalized:
        return OUT_OF_SCOPE, None, pilot_guard.RECIPIENT_UNNORMALIZABLE
    return IN_SCOPE, (scope.tenant_id,
                      decision.connection_ref or scope.connection_ref,
                      normalized, scope.connection_id), decision.reason


def _addressable(inbounds: List[Dict[str, Any]]) -> List[Tuple[Dict[str, Any], str, str, str]]:
    """``(message, identity, recipient, phone_number_id)`` for each message that
    names all three. One that does not is nobody's to hold."""
    out = []
    for item in inbounds:
        message = item["message"]
        identity = str(message.get("id") or "").strip()
        recipient = str(message.get("from") or "").strip()
        phone_number_id = item["phone_number_id"]
        if identity and recipient and phone_number_id:
            out.append((message, identity, recipient, phone_number_id))
    return out


def _session(session_factory: Optional[Any]) -> Any:
    if session_factory is None:
        from database.session import SessionLocal as session_factory  # noqa: PLC0415,N813
    return session_factory()


def record_before_acknowledging(body: Mapping[str, Any], *,
                                session_factory: Optional[Any] = None,
                                authenticated: bool = False) -> Acceptance:
    """Persist every pilot-scoped inbound in this body. Nothing else.

    Returns an :class:`Acceptance` whose ``ok`` is the only thing the caller
    should turn into a status code: true means every pilot-scoped message in the
    batch is durable, false means at least one is not and the request must not
    be acknowledged as accepted.

    ``authenticated`` is the caller's statement that the provider's signature on
    this request was **valid** — not merely audited. It defaults to false so a
    caller that says nothing takes no pilot obligation: a message the platform
    cannot attribute to Meta is recorded by nobody and processed by nobody, and
    the request is answered retryable rather than as an acceptance.
    """
    from core.commerce_runtime import pilot_guard  # noqa: PLC0415

    if not pilot_guard.pilot_enabled():
        return Acceptance(accepted=True, reason="pilot_disabled")

    inbounds = _messages(body)
    if not inbounds:
        return Acceptance(accepted=True, reason="no_messages")

    from core.commerce_runtime import handover  # noqa: PLC0415

    recorded: List[str] = []
    failed: List[str] = []
    undecided: List[str] = []
    scoped = 0
    db = None
    try:
        db = _session(session_factory)
        # Classified first, recorded second: whether this request may take any
        # pilot obligation at all is decided on the whole batch, before one
        # row of it is written.
        classified = []
        for message, identity, recipient, phone_number_id in _addressable(inbounds):
            verdict, target, detail = _classify(
                db, phone_number_id=phone_number_id, recipient=recipient)
            classified.append((message, identity, recipient, phone_number_id,
                               verdict, target, detail))
        in_scope = [c for c in classified if c[4] == IN_SCOPE]
        undecidable = [c for c in classified if c[4] == UNDECIDABLE]

        if not authenticated and (in_scope or undecidable):
            # The signature was missing, invalid or unverifiable, and this
            # batch carries work that is — or may be — the pilot's. It is not
            # recorded and it will not be processed: an obligation is taken
            # only from a sender we could authenticate. Retryable, so a Meta
            # request refused by a misconfigured secret is not lost.
            logger.error("[COMMERCE_RUNTIME_ACCEPT] pilot-scoped work in an unauthenticated "
                         "request scoped=%s undecidable=%s — refusing to acknowledge; "
                         "check META_APP_SECRET and the signature audit",
                         len(in_scope), len(undecidable))
            return Acceptance(
                accepted=False, scoped=len(in_scope),
                failed=tuple(c[1] for c in in_scope + undecidable),
                reason=REFUSED_UNAUTHENTICATED)

        for message, identity, recipient, phone_number_id, verdict, target, detail in classified:
            if verdict == OUT_OF_SCOPE:
                continue                                  # verified unrelated, untouched
            if verdict == UNDECIDABLE:
                # Not "not ours". We could not establish whose this is, and an
                # acknowledgement would end the provider's retries for a message
                # that may be the pilot's and has nothing recorded for it.
                logger.error("[COMMERCE_RUNTIME_ACCEPT] scope undecidable "
                             "phone_number_id=%s detail=%s — refusing to acknowledge",
                             phone_number_id, detail)
                undecided.append(identity or "(no id)")
                continue
            tenant_id, connection_ref, normalized, connection_id = target
            scoped += 1
            try:
                generation = handover.read_barrier(db, tenant_id=tenant_id).generation
            except Exception as exc:  # noqa: BLE001
                logger.error("[COMMERCE_RUNTIME_ACCEPT] barrier unreadable tenant=%s "
                             "error=%s — refusing to acknowledge",
                             tenant_id, type(exc).__name__)
                failed.append(identity)
                continue
            record = handover.record_inbound(
                db, tenant_id=tenant_id, phone_number_id=phone_number_id,
                channel_connection_ref=connection_ref, recipient=normalized,
                provider_message_id=identity,
                payload={"text": _text_of(message), "type": str(message.get("type") or ""),
                         "connection_id": connection_id,
                         "raw": _replayable(message)},
                reason=handover.REASON_ACCEPTED,
                barrier_generation=generation)
            if record is None:
                failed.append(identity)
            else:
                recorded.append(identity)
    except handover.BarrierReleased as released:
        # The operator verified the release and is switching the pilot off.
        # Nothing new is accepted for it: the provider is answered retryable,
        # and once the flag is off this request is the legacy path's as today.
        logger.error("[COMMERCE_RUNTIME_ACCEPT] pilot released tenant=%s — refusing to "
                     "acknowledge new pilot-scoped work until the switch is off",
                     released.tenant_id)
        return Acceptance(accepted=False, recorded=tuple(recorded), failed=tuple(failed),
                          scoped=scoped, reason=REFUSED_RELEASED)
    except Exception as exc:  # noqa: BLE001
        logger.error("[COMMERCE_RUNTIME_ACCEPT] could not record pilot inbound error=%s "
                     "— the request is not acknowledged as accepted", type(exc).__name__)
        return Acceptance(accepted=False, recorded=tuple(recorded), failed=tuple(failed),
                          scoped=scoped, reason=f"error:{type(exc).__name__}")
    finally:
        if db is not None:
            try:
                db.close()
            except Exception:  # noqa: BLE001 - a session we cannot close is dropped
                logger.warning("[COMMERCE_RUNTIME_ACCEPT] session close failed")

    if undecided:
        return Acceptance(accepted=False, recorded=tuple(recorded), failed=tuple(undecided),
                          scoped=scoped, reason=REFUSED_UNDECIDABLE)
    if failed:
        logger.error("[COMMERCE_RUNTIME_ACCEPT] %s pilot inbound(s) were not persisted "
                     "— refusing to acknowledge the batch", len(failed))
        return Acceptance(accepted=False, recorded=tuple(recorded), failed=tuple(failed),
                          scoped=scoped, reason=REFUSED_NOT_PERSISTED)
    if recorded:
        logger.info("[COMMERCE_RUNTIME_ACCEPT] recorded=%s scoped=%s", len(recorded), scoped)
    return Acceptance(accepted=True, recorded=tuple(recorded), scoped=scoped, reason="recorded")


def durable_status(body: Mapping[str, Any], *,
                   session_factory: Optional[Any] = None) -> DurableStatus:
    """Whether every pilot-scoped message in this body is already on record.

    Asked by the route when replay protection reports the body was seen before.
    The nonce says a request with this body reached us; it does not say the
    process that took it lived long enough to write anything down. Only a
    durable record does, so each pilot-scoped identity is looked up — the same
    scope resolution acceptance uses, the same verified connection — and any
    that has none is named. Never raises: what cannot be established is
    reported as not durable, which is the direction that retries.
    """
    from core.commerce_runtime import pilot_guard  # noqa: PLC0415

    if not pilot_guard.pilot_enabled():
        return DurableStatus()
    inbounds = _messages(body)
    if not inbounds:
        return DurableStatus()

    from core.commerce_runtime import handover  # noqa: PLC0415

    scoped = 0
    undurable: List[str] = []
    undecidable: List[str] = []
    db = None
    try:
        db = _session(session_factory)
        for _message, identity, recipient, phone_number_id in _addressable(inbounds):
            verdict, target, _detail = _classify(
                db, phone_number_id=phone_number_id, recipient=recipient)
            if verdict == OUT_OF_SCOPE:
                continue
            if verdict == UNDECIDABLE:
                undecidable.append(identity)
                continue
            tenant_id, connection_ref, _normalized, _connection_id = target
            scoped += 1
            if handover.accepted_inbound(db, tenant_id=tenant_id,
                                         channel_connection_ref=connection_ref,
                                         provider_message_id=identity) is None:
                undurable.append(identity)
    except Exception as exc:  # noqa: BLE001 - unknown is not durable
        logger.error("[COMMERCE_RUNTIME_ACCEPT] durable status could not be read error=%s "
                     "— a nonce alone will not answer this request", type(exc).__name__)
        return DurableStatus(scoped=scoped, undurable=tuple(undurable),
                             undecidable=tuple(undecidable) + ("(unreadable)",))
    finally:
        if db is not None:
            try:
                db.close()
            except Exception:  # noqa: BLE001 - a session we cannot close is dropped
                logger.warning("[COMMERCE_RUNTIME_ACCEPT] session close failed")
    return DurableStatus(scoped=scoped, undurable=tuple(undurable),
                         undecidable=tuple(undecidable))


def _replayable(message: Mapping[str, Any]) -> Dict[str, Any]:
    """The inbound itself, kept small enough to store and whole enough to replay.

    Recovery rebuilds a provider webhook body from this, so it keeps the parts a
    dispatcher reads — id, sender, type, timestamp, the typed payload and the
    reply context — and drops everything else rather than archiving a payload of
    unbounded size next to every accepted message.
    """
    keep = ("id", "from", "type", "timestamp", "text", "button", "interactive",
            "context", "image", "document", "audio", "video", "sticker", "location",
            "order", "referral")
    out: Dict[str, Any] = {}
    for key in keep:
        value = message.get(key)
        if value not in (None, "", {}, []):
            out[key] = value
    return out


__all__ = ["Acceptance", "DurableStatus", "IN_SCOPE", "OUT_OF_SCOPE",
           "REFUSED_NOT_PERSISTED", "REFUSED_RELEASED", "REFUSED_UNAUTHENTICATED",
           "REFUSED_UNDECIDABLE", "UNDECIDABLE", "durable_status",
           "record_before_acknowledging"]

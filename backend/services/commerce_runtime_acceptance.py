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
        decision = pilot_guard.evaluate_pilot_route(
            db, tenant_id=scope.tenant_id, customer_phone=recipient,
            phone_number_id=phone_number_id, inbound_text="_",
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


def record_before_acknowledging(body: Mapping[str, Any], *,
                                session_factory: Optional[Any] = None) -> Acceptance:
    """Persist every pilot-scoped inbound in this body. Nothing else.

    Returns an :class:`Acceptance` whose ``ok`` is the only thing the caller
    should turn into a status code: true means every pilot-scoped message in the
    batch is durable, false means at least one is not and the request must not
    be acknowledged as accepted.
    """
    from core.commerce_runtime import pilot_guard  # noqa: PLC0415

    if not pilot_guard.pilot_enabled():
        return Acceptance(accepted=True, reason="pilot_disabled")

    inbounds = _messages(body)
    if not inbounds:
        return Acceptance(accepted=True, reason="no_messages")

    if session_factory is None:
        from database.session import SessionLocal as session_factory  # noqa: PLC0415,N813

    from core.commerce_runtime import handover  # noqa: PLC0415

    recorded: List[str] = []
    failed: List[str] = []
    undecided: List[str] = []
    scoped = 0
    db = None
    try:
        db = session_factory()
        for item in inbounds:
            message = item["message"]
            identity = str(message.get("id") or "").strip()
            recipient = str(message.get("from") or "").strip()
            phone_number_id = item["phone_number_id"]
            if not identity or not recipient or not phone_number_id:
                continue                                  # not addressable; not ours to hold
            verdict, target, detail = _classify(
                db, phone_number_id=phone_number_id, recipient=recipient)
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
                          scoped=scoped, reason="scope_undecidable")
    if failed:
        logger.error("[COMMERCE_RUNTIME_ACCEPT] %s pilot inbound(s) were not persisted "
                     "— refusing to acknowledge the batch", len(failed))
        return Acceptance(accepted=False, recorded=tuple(recorded), failed=tuple(failed),
                          scoped=scoped, reason="not_persisted")
    if recorded:
        logger.info("[COMMERCE_RUNTIME_ACCEPT] recorded=%s scoped=%s", len(recorded), scoped)
    return Acceptance(accepted=True, recorded=tuple(recorded), scoped=scoped, reason="recorded")


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


__all__ = ["Acceptance", "IN_SCOPE", "OUT_OF_SCOPE", "UNDECIDABLE",
           "record_before_acknowledging"]

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


def _scoped_tenant(db: Any, *, phone_number_id: str, recipient: str) -> Optional[Any]:
    """The pilot decision for this message, or ``None`` when it is not ours.

    Uses the pilot's own guard, so "scoped" here means exactly what it means
    everywhere else: an allowlisted tenant and recipient on a verified
    connection, with a model configured.
    """
    from core.commerce_runtime import pilot_guard  # noqa: PLC0415

    connection = pilot_guard.tenant_for_phone_number_id(db, phone_number_id=phone_number_id)
    if connection is None:
        return None
    tenant_id, connection_ref, _connection_id = connection
    decision = pilot_guard.evaluate_pilot_route(
        db, tenant_id=tenant_id, customer_phone=recipient,
        phone_number_id=phone_number_id, inbound_text="_",
    )
    if decision.reason in {pilot_guard.PILOT_DISABLED, pilot_guard.TENANT_NOT_ALLOWLISTED,
                           pilot_guard.RECIPIENT_MISSING,
                           pilot_guard.RECIPIENT_UNNORMALIZABLE,
                           pilot_guard.RECIPIENT_NOT_ALLOWLISTED,
                           pilot_guard.CONNECTION_NOT_VERIFIED}:
        return None
    return (tenant_id, connection_ref or f"wa:{phone_number_id}",
            decision.recipient or pilot_guard.normalize_recipient(recipient))


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
            target = _scoped_tenant(db, phone_number_id=phone_number_id, recipient=recipient)
            if target is None:
                continue                                  # unrelated traffic, untouched
            tenant_id, connection_ref, normalized = target
            scoped += 1
            record = handover.record_inbound(
                db, tenant_id=tenant_id, phone_number_id=phone_number_id,
                channel_connection_ref=connection_ref, recipient=normalized,
                provider_message_id=identity,
                payload={"text": _text_of(message), "type": str(message.get("type") or "")},
                reason=handover.REASON_ACCEPTED,
                barrier_generation=handover.read_barrier(
                    db, tenant_id=tenant_id).generation)
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

    if failed:
        logger.error("[COMMERCE_RUNTIME_ACCEPT] %s pilot inbound(s) were not persisted "
                     "— refusing to acknowledge the batch", len(failed))
        return Acceptance(accepted=False, recorded=tuple(recorded), failed=tuple(failed),
                          scoped=scoped, reason="not_persisted")
    if recorded:
        logger.info("[COMMERCE_RUNTIME_ACCEPT] recorded=%s scoped=%s", len(recorded), scoped)
    return Acceptance(accepted=True, recorded=tuple(recorded), scoped=scoped, reason="recorded")


__all__ = ["Acceptance", "record_before_acknowledging"]

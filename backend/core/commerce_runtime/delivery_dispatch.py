"""From a reserved delivery intent to a recorded transport outcome.

The loop ends a successful turn by reserving one delivery intent and stopping.
This module is the only thing that turns such a reservation into an actual
send, and it does exactly one attempt per call:

    reserve the attempt → send → classify the response → record the receipt

Every step of that order is enforced by the ledger, not by this module. The
reservation refuses a second dispatch of an attempt whose outcome is pending,
already accepted, or **unknown** — so an uncertain send is never blindly
retried here or anywhere else. Recording an accepted receipt requires the
provider's own message id; a 2xx without one is ``unknown``, never success.

Completion is deliberately separate. ``complete_turn`` writes the turn's single
terminal, and the transport outcome and customer reach in that terminal are
derived by the ledger from the receipts, not supplied by the caller. A turn
whose delivery failed or is unknown is never recorded as processing-completed.
"""
from __future__ import annotations

import dataclasses
import logging
from typing import Any, Callable, Mapping, Optional

from core.commerce_runtime import contracts as c
from core.commerce_runtime import ledger_contracts as lc
from core.commerce_runtime.ledgers import LedgerRepository

logger = logging.getLogger("nahla.commerce_runtime.delivery_dispatch")

Transport = Callable[[Mapping[str, Any]], lc.SendResponse]


# Closed outcomes of one dispatch call.
SENT_ACCEPTED = "accepted"          # the provider accepted the send and named the message
SENT_REJECTED = "rejected"          # the provider definitively refused; nothing was sent
SENT_UNKNOWN = "unknown"            # timeout, no status, 5xx, or 2xx without a message id
NOT_ATTEMPTED = "not_attempted"     # the ledger refused the reservation; no send was made

_TERMINAL_FOR = {
    SENT_ACCEPTED: c.ProcessingOutcome.COMPLETED.value,
    SENT_REJECTED: c.ProcessingOutcome.FAILED.value,
    SENT_UNKNOWN: c.ProcessingOutcome.FAILED.value,
    NOT_ATTEMPTED: c.ProcessingOutcome.FAILED.value,
}


@dataclasses.dataclass(frozen=True)
class DispatchOutcome:
    """What one dispatch call established. ``unknown`` is never a success."""

    status: str
    sequence_id: int
    attempt_id: Optional[int]
    provider_message_id: Optional[str]
    blocked_reason: Optional[str] = None
    detail: Mapping[str, Any] = dataclasses.field(default_factory=dict)

    @property
    def delivered(self) -> bool:
        """True only for an acceptance the provider itself identified."""
        return self.status == SENT_ACCEPTED and bool(self.provider_message_id)

    @property
    def processing_outcome(self) -> str:
        return _TERMINAL_FOR[self.status]


def dispatch_reserved_delivery(
    *,
    ledgers: LedgerRepository,
    tenant_id: int,
    namespace: Any,
    conversation_id: int,
    token: c.OwnershipToken,
    sequence_id: int,
    transport: Transport,
    recorded_by: str,
) -> DispatchOutcome:
    """Dispatch one reserved delivery intent exactly once.

    ``transport`` is handed the sequence's own stored payload and must report
    what the provider said as a :class:`~...ledger_contracts.SendResponse`. A
    transport that raises is an **unknown** send, not a failed one: the request
    may already have reached the provider, so the attempt is recorded as
    unknown and is never retried automatically.
    """
    try:
        attempt = ledgers.reserve_delivery_dispatch(
            tenant_id=tenant_id, namespace=namespace, conversation_id=conversation_id,
            token=token, sequence_id=sequence_id,
        )
    except lc.DeliveryDispatchBlocked as blocked:
        logger.info("[COMMERCE_RUNTIME_DISPATCH] blocked sequence=%s reason=%s",
                    sequence_id, blocked.reason.value)
        return DispatchOutcome(status=NOT_ATTEMPTED, sequence_id=sequence_id, attempt_id=None,
                               provider_message_id=None, blocked_reason=blocked.reason.value)
    except c.OwnershipRejected as rejected:
        # Ownership was lost, or the turn is no longer the eligible one — for
        # instance because it already has its terminal. Either way this caller
        # may not send, and nothing was sent.
        logger.info("[COMMERCE_RUNTIME_DISPATCH] refused sequence=%s reason=%s",
                    sequence_id, rejected.reason.value)
        return DispatchOutcome(status=NOT_ATTEMPTED, sequence_id=sequence_id, attempt_id=None,
                               provider_message_id=None, blocked_reason=rejected.reason.value)

    try:
        response = transport(attempt.payload)
        if not isinstance(response, lc.SendResponse):
            raise TypeError("transport must report a SendResponse")
    except Exception as exc:  # noqa: BLE001 - an exception is an unknown send, never a proven failure
        logger.warning("[COMMERCE_RUNTIME_DISPATCH] transport raised sequence=%s attempt=%s error=%s",
                       sequence_id, attempt.attempt_id, type(exc).__name__)
        response = lc.SendResponse(http_status=None, body={}, timed_out=True)
        evidence: dict = {"transport_error": type(exc).__name__}
    else:
        evidence = {"http_status": response.http_status, "timed_out": bool(response.timed_out)}

    kind, provider_message_id = lc.classify_send_response(response)
    ledgers.record_delivery_receipt(
        tenant_id=tenant_id, namespace=namespace, conversation_id=conversation_id,
        attempt_id=attempt.attempt_id, kind=kind, provider_message_id=provider_message_id,
        evidence=evidence, recorded_by=recorded_by,
    )
    status = {
        lc.ReceiptKind.ACCEPTED: SENT_ACCEPTED,
        lc.ReceiptKind.REJECTED: SENT_REJECTED,
        lc.ReceiptKind.UNKNOWN: SENT_UNKNOWN,
    }[kind]
    logger.info("[COMMERCE_RUNTIME_DISPATCH] sequence=%s attempt=%s outcome=%s identified=%s",
                sequence_id, attempt.attempt_id, status, bool(provider_message_id))
    return DispatchOutcome(status=status, sequence_id=sequence_id, attempt_id=attempt.attempt_id,
                           provider_message_id=provider_message_id, detail=evidence)


def complete_turn(
    *,
    ledgers: LedgerRepository,
    tenant_id: int,
    namespace: Any,
    turn_id: int,
    token: c.OwnershipToken,
    processing_outcome: str,
    details: Optional[Mapping[str, Any]] = None,
) -> Optional[c.TerminalRecord]:
    """Write the turn's single terminal, or report that it already has one.

    The transport outcome and the customer reach are derived from the ledgers
    inside the same transaction; nothing a caller believes about the send can
    override what the receipts say. ``CompletionBlocked`` is returned as
    ``None`` after logging: a turn with an intent reserved but never dispatched,
    or an attempt with no established outcome, is not finished and must not be
    stamped as if it were.
    """
    try:
        return ledgers.finalize_turn(
            tenant_id=tenant_id, namespace=namespace, turn_id=turn_id, token=token,
            processing_outcome=processing_outcome, details=dict(details or {}),
        )
    except c.TerminalAlreadyRecorded as already:
        logger.info("[COMMERCE_RUNTIME_DISPATCH] turn=%s already terminal outcome=%s",
                    turn_id, getattr(already.existing, "processing_outcome", None))
        return already.existing
    except c.CompletionBlocked as blocked:
        logger.warning("[COMMERCE_RUNTIME_DISPATCH] completion refused turn=%s reason=%s",
                       turn_id, getattr(blocked, "reason", None) or str(blocked))
        return None


__all__ = [
    "DispatchOutcome", "NOT_ATTEMPTED", "SENT_ACCEPTED", "SENT_REJECTED", "SENT_UNKNOWN",
    "Transport", "complete_turn", "dispatch_reserved_delivery",
]

"""Contracts of the dormant effect and delivery ledgers.

Pure definitions only: closed vocabularies, bounded intents, result records,
explicit errors, the legal transition tables, the payload hash, the
WhatsApp-shaped send-response classifier and the human-transfer evidence
rule. Nothing here touches a database, a model, a provider or the network.
Authority: ``docs/architecture/commerce-runtime-effect-and-delivery-ledgers.md``.

Vocabulary
==========
* An **effect** is one reserved external mutation (business action). It is
  identified by a stable *business* idempotency key inside a tenant and
  namespace; a provider tool-call id is not such a key.
* An **effect attempt** is one durable dispatch reservation of an effect.
  Attempts are append-only; their outcomes are append-only **result** rows.
* A **delivery sequence** is the single logical outbound delivery of one
  eligible turn, made of ordered **delivery attempts** whose outcomes and
  customer-reach evidence are append-only **receipts**.

Honest outcome semantics
========================
``unknown`` means the external outcome is not established. It is retained
until evidence arrives for the *same* attempt; it never authorises a new
attempt, a fallback, a retry or an alternate provider. The absence of a local
success record is never treated as proof that nothing happened externally.
"""
from __future__ import annotations

import dataclasses
import datetime as _dt
import enum
import hashlib
import json
from typing import Any, Dict, FrozenSet, Mapping, Optional, Sequence, Tuple

from core.commerce_runtime import contracts as c

# ── Bounds (closed; changing one is a reviewed contract change) ──────────────

MAX_ACTION_TYPE_LENGTH = 64
MAX_IDEMPOTENCY_KEY_LENGTH = 128
MAX_DISPATCH_KEY_LENGTH = 64
MAX_EVIDENCE_BYTES = 16 * 1024          # result / receipt evidence, canonical JSON
MAX_EFFECT_ATTEMPTS = 1                 # an effect is dispatched at most once; a rejection is final
MAX_DELIVERY_ATTEMPTS = 2               # one attempt plus one bounded rich-to-text recovery
PAYLOAD_HASH_LENGTH = 64                # sha256 hex
KEY_ENCODING_VERSION = "k1"             # business-key encoding; a new encoding is a new version, never a silent change


# ── Closed vocabularies ──────────────────────────────────────────────────────


class ActionType(str, enum.Enum):
    """External mutations this slice can record. Extending it is a contract change."""

    ORDER_CREATE = "order_create"
    ORDER_UPDATE = "order_update"
    ORDER_CANCEL = "order_cancel"
    PAYMENT_LINK_CREATE = "payment_link_create"
    COUPON_APPLY = "coupon_apply"
    HANDOFF_REQUEST = "handoff_request"


class EffectStatus(str, enum.Enum):
    """Projection of an effect's ledger; the append-only rows are the evidence."""

    RESERVED = "reserved"          # reserved, not dispatched
    DISPATCHING = "dispatching"    # dispatch started; completion not yet established
    CONFIRMED = "confirmed"        # confirmed success; the result is reusable
    REJECTED = "rejected"          # definitive rejection or failure
    UNKNOWN = "unknown"            # unknown external outcome; retained until evidence


class EffectOutcome(str, enum.Enum):
    """What one append-only result row asserts about one attempt."""

    CONFIRMED = "confirmed"
    REJECTED = "rejected"
    UNKNOWN = "unknown"


class DeliveryKind(str, enum.Enum):
    RICH = "rich"
    TEXT = "text"


class DeliveryOutcome(str, enum.Enum):
    """Transport projection of a delivery sequence (its latest attempt)."""

    PENDING = "pending"            # no attempt, or the latest attempt has no outcome receipt yet
    ACCEPTED = "accepted"
    REJECTED = "rejected"
    UNKNOWN = "unknown"


class ReceiptKind(str, enum.Enum):
    """Append-only evidence attached to one delivery attempt."""

    ACCEPTED = "accepted"          # provider accepted the send; a provider message id is required
    REJECTED = "rejected"          # definitive rejection: nothing was sent
    UNKNOWN = "unknown"            # timeout, missing message id or an ambiguous response
    DELIVERED = "delivered"        # customer reach evidence
    READ = "read"                  # customer reach evidence
    FAILED = "failed"              # the provider reported a failure after acceptance


OUTCOME_RECEIPTS: FrozenSet[ReceiptKind] = frozenset({ReceiptKind.ACCEPTED, ReceiptKind.REJECTED, ReceiptKind.UNKNOWN})
REACH_RECEIPTS: FrozenSet[ReceiptKind] = frozenset({ReceiptKind.DELIVERED, ReceiptKind.READ, ReceiptKind.FAILED})


class DispatchBlock(str, enum.Enum):
    """Why a dispatch reservation was refused. Never a reason to try anyway."""

    ATTEMPT_PENDING = "attempt_pending"          # an attempt exists whose completion is not established
    OUTCOME_UNKNOWN = "outcome_unknown"          # the external outcome is unknown; no blind redispatch
    ALREADY_CONFIRMED = "already_confirmed"      # reuse the confirmed result instead
    REJECTED_FINAL = "rejected_final"            # a definitive rejection closes the effect
    RECOVERY_ONLY = "recovery_only"              # a rejected delivery continues only through the bounded recovery
    ATTEMPTS_EXHAUSTED = "attempts_exhausted"


class RecoveryRefusal(str, enum.Enum):
    NO_ATTEMPT = "no_attempt"                    # nothing was dispatched yet
    ATTEMPT_PENDING = "attempt_pending"
    OUTCOME_ACCEPTED = "outcome_accepted"        # an accepted send is never retried or fallen back
    OUTCOME_UNKNOWN = "outcome_unknown"          # unknown never permits a fallback
    ATTEMPTS_EXHAUSTED = "attempts_exhausted"
    NOT_RICH_TO_TEXT = "not_rich_to_text"        # the only bounded recovery is rich → text


# ── Legal transitions (closed tables) ────────────────────────────────────────

EFFECT_TRANSITIONS: Mapping[EffectStatus, FrozenSet[EffectStatus]] = {
    EffectStatus.RESERVED: frozenset({EffectStatus.DISPATCHING}),
    EffectStatus.DISPATCHING: frozenset({EffectStatus.CONFIRMED, EffectStatus.REJECTED, EffectStatus.UNKNOWN}),
    EffectStatus.UNKNOWN: frozenset({EffectStatus.CONFIRMED, EffectStatus.REJECTED}),   # late evidence, same attempt
    EffectStatus.CONFIRMED: frozenset(),
    EffectStatus.REJECTED: frozenset(),
}

DELIVERY_TRANSITIONS: Mapping[DeliveryOutcome, FrozenSet[DeliveryOutcome]] = {
    DeliveryOutcome.PENDING: frozenset({DeliveryOutcome.ACCEPTED, DeliveryOutcome.REJECTED, DeliveryOutcome.UNKNOWN}),
    DeliveryOutcome.REJECTED: frozenset({DeliveryOutcome.PENDING}),   # only through one bounded recovery attempt
    DeliveryOutcome.UNKNOWN: frozenset({DeliveryOutcome.ACCEPTED, DeliveryOutcome.REJECTED}),  # late evidence
    DeliveryOutcome.ACCEPTED: frozenset(),
}


def effect_transition_allowed(current: Any, target: Any) -> bool:
    return EffectStatus(target) in EFFECT_TRANSITIONS[EffectStatus(current)]


def delivery_transition_allowed(current: Any, target: Any) -> bool:
    return DeliveryOutcome(target) in DELIVERY_TRANSITIONS[DeliveryOutcome(current)]


# ── Errors (explicit; nothing is discarded silently) ─────────────────────────


class LedgerError(c.CommerceRuntimeError):
    """Base class of every error raised by the ledgers."""


class EffectNotFound(LedgerError):
    """No effect for this tenant + namespace + conversation + id (foreign scopes look identical)."""


class AttemptNotFound(LedgerError):
    """No attempt for this scope + id (foreign scopes look identical)."""


class SequenceNotFound(LedgerError):
    """No delivery sequence for this scope + id (foreign scopes look identical)."""


class EffectConflict(LedgerError):
    """The business idempotency key is already bound to a different action, payload or conversation."""

    def __init__(self, reason: str, existing: "EffectRecord") -> None:
        self.reason = reason
        self.existing = existing
        super().__init__(f"idempotency key {existing.idempotency_key!r} already bound with a different {reason}")


class DeliveryConflict(LedgerError):
    """The eligible turn already has a delivery sequence with a different intent."""

    def __init__(self, reason: str, existing: "DeliverySequenceRecord") -> None:
        self.reason = reason
        self.existing = existing
        super().__init__(f"turn {existing.turn_id} already has a delivery sequence with a different {reason}")


class IllegalTransition(LedgerError):
    """A contradictory or out-of-order transition was requested; nothing was changed."""

    def __init__(self, message: str, *, current: str, attempted: str) -> None:
        self.current = current
        self.attempted = attempted
        super().__init__(f"{message} (current={current}, attempted={attempted})")


class DispatchBlocked(LedgerError):
    """A dispatch reservation was refused for an exact reason."""

    def __init__(self, reason: DispatchBlock, effect: "EffectRecord",
                 open_attempt: Optional["EffectAttemptRecord"] = None) -> None:
        self.reason = reason
        self.effect = effect
        self.open_attempt = open_attempt
        super().__init__(f"dispatch blocked: {reason.value} (effect {effect.effect_id}, status {effect.status})")


class DeliveryDispatchBlocked(LedgerError):
    def __init__(self, reason: DispatchBlock, sequence: "DeliverySequenceRecord") -> None:
        self.reason = reason
        self.sequence = sequence
        super().__init__(f"delivery dispatch blocked: {reason.value} (sequence {sequence.sequence_id})")


class RecoveryNotPermitted(LedgerError):
    def __init__(self, reason: RecoveryRefusal, sequence: "DeliverySequenceRecord") -> None:
        self.reason = reason
        self.sequence = sequence
        super().__init__(f"recovery not permitted: {reason.value} (sequence {sequence.sequence_id})")


# Completion is enforced by the foundation's terminal path for every entry
# point; the error class lives in ``contracts`` and is re-exported here.
CompletionBlocked = c.CompletionBlocked


# ── Intents and records ──────────────────────────────────────────────────────


@dataclasses.dataclass(frozen=True)
class EffectIntent:
    action_type: str
    idempotency_key: str
    payload: Mapping[str, Any] = dataclasses.field(default_factory=dict)


@dataclasses.dataclass(frozen=True)
class DeliveryIntent:
    kind: str
    payload: Mapping[str, Any] = dataclasses.field(default_factory=dict)


@dataclasses.dataclass(frozen=True)
class EffectRecord:
    effect_id: int
    tenant_id: int
    namespace: str
    conversation_id: int
    turn_id: int
    action_type: str
    idempotency_key: str
    payload: Dict[str, Any]
    payload_hash: str
    status: str
    attempt_count: int
    reserved_by: str
    reserved_fence: int
    reserved_epoch: int
    confirmed_result: Optional[Dict[str, Any]]
    created_at: _dt.datetime
    updated_at: _dt.datetime


@dataclasses.dataclass(frozen=True)
class EffectReservation:
    effect: EffectRecord
    created: bool                       # False: the existing record was returned


@dataclasses.dataclass(frozen=True)
class EffectAttemptRecord:
    attempt_id: int
    effect_id: int
    attempt_no: int
    dispatch_key: str                   # this attempt's own identity, usable towards a provider
    reserved_by: str
    reserved_fence: int
    reserved_epoch: int
    reserved_at: _dt.datetime


@dataclasses.dataclass(frozen=True)
class EffectResultRecord:
    result_id: int
    attempt_id: int
    effect_id: int
    result_no: int
    outcome: str
    evidence: Dict[str, Any]
    recorded_by: str
    recorded_at: _dt.datetime


@dataclasses.dataclass(frozen=True)
class DeliverySequenceRecord:
    sequence_id: int
    tenant_id: int
    namespace: str
    conversation_id: int
    turn_id: int
    intent_kind: str
    intent_payload: Dict[str, Any]
    intent_hash: str
    attempt_count: int
    outcome: str
    reserved_by: str
    reserved_fence: int
    reserved_epoch: int
    created_at: _dt.datetime
    updated_at: _dt.datetime


@dataclasses.dataclass(frozen=True)
class DeliveryAttemptRecord:
    attempt_id: int
    sequence_id: int
    attempt_no: int
    kind: str
    dispatch_key: str
    payload: Dict[str, Any]
    reserved_by: str
    reserved_fence: int
    reserved_epoch: int
    reserved_at: _dt.datetime


@dataclasses.dataclass(frozen=True)
class DeliveryReceiptRecord:
    receipt_id: int
    attempt_id: int
    sequence_id: int
    receipt_no: int
    kind: str
    provider_message_id: Optional[str]
    evidence: Dict[str, Any]
    recorded_by: str
    recorded_at: _dt.datetime


@dataclasses.dataclass(frozen=True)
class TurnDecision:
    """What one atomic decision commit persisted."""

    conversation_id: int
    turn_id: int
    state: Optional[c.StateCommit]
    effects: Tuple[EffectReservation, ...]
    delivery: Optional[DeliverySequenceRecord]


@dataclasses.dataclass(frozen=True)
class TurnLedgerSummary:
    """The ledger facts of one turn, as read in one transaction."""

    turn_id: int
    effects_by_status: Dict[str, int]
    pending_effect_attempts: int         # attempts without an established outcome
    delivery_outcome: str                # DeliveryOutcome or 'not_attempted'
    delivery_attempt_count: int
    delivery_pending: bool               # the latest attempt has no outcome receipt
    transport_outcome: str               # TransportOutcome for the terminal
    customer_reach: str                  # CustomerReach for the terminal


@dataclasses.dataclass(frozen=True)
class SendResponse:
    """What a WhatsApp-shaped transport reported for one attempt (scripted in tests)."""

    http_status: Optional[int]
    body: Mapping[str, Any] = dataclasses.field(default_factory=dict)
    timed_out: bool = False


# ── Validation (fail closed) ─────────────────────────────────────────────────


def canonical_json(value: Mapping[str, Any]) -> str:
    return json.dumps(dict(value), sort_keys=True, ensure_ascii=False, allow_nan=False, separators=(",", ":"))


def payload_hash(payload: Mapping[str, Any]) -> str:
    """Stable identity of a validated payload (sha256 of its canonical JSON)."""
    return hashlib.sha256(canonical_json(payload).encode("utf-8")).hexdigest()


def _encode_component(part: str) -> str:
    """Escape the two characters that carry structure so a component can never
    be read as two components or as part of the prefix."""
    return part.replace("%", "%25").replace(":", "%3A")


def derive_business_key(action_type: Any, *parts: Any) -> str:
    """A deterministic business idempotency key from ordered business identifiers.

    Callers derive keys from what the action *is* (order reference, coupon
    code, payment reference), never from a provider tool-call id, so a
    reasoning retry or a provider failover reproduces the same key.

    Encoding ``k1``: ``<action>:k1:<c1>:<c2>...`` where every string component
    is percent-escaped (``%`` → ``%25``, ``:`` → ``%3A``) before joining, so
    ``["a:b", "c"]`` and ``["a", "b:c"]`` differ and so do different component
    counts. Integers render as decimal digits; empty strings, booleans and
    ``None`` are refused. When the encoded key exceeds the bound it becomes
    ``<action>:k1#sha256:<hex>`` over the *encoded* string; the ``#`` after the
    version cannot be produced by the plain form, so the two forms never
    collide. Identical components always yield the identical key.
    """
    action = validate_action_type(action_type)
    if not parts:
        raise c.ValidationError("derive_business_key needs at least one business identifier")
    rendered = []
    for part in parts:
        if isinstance(part, bool) or part is None:
            raise c.ValidationError("business identifiers must be strings or integers")
        if isinstance(part, int):
            rendered.append(str(part))
        elif isinstance(part, str):
            rendered.append(_encode_component(
                c.validate_ref(part, field="business identifier", max_length=MAX_IDEMPOTENCY_KEY_LENGTH)))
        else:
            raise c.ValidationError("business identifiers must be strings or integers")
    encoded = f"{action}:{KEY_ENCODING_VERSION}:" + ":".join(rendered)
    if len(encoded) > MAX_IDEMPOTENCY_KEY_LENGTH:
        digest = hashlib.sha256(encoded.encode("utf-8")).hexdigest()
        return f"{action}:{KEY_ENCODING_VERSION}#sha256:{digest}"
    return encoded


def validate_action_type(value: Any) -> str:
    return c.validate_enum(value, ActionType, field="action_type")


def validate_idempotency_key(value: Any) -> str:
    return c.validate_ref(value, field="idempotency_key", max_length=MAX_IDEMPOTENCY_KEY_LENGTH)


def validate_evidence(value: Any) -> Dict[str, Any]:
    return c.validate_payload(value, field="evidence", max_bytes=MAX_EVIDENCE_BYTES)


def validate_effect_intent(intent: Any) -> EffectIntent:
    if not isinstance(intent, EffectIntent):
        raise c.ValidationError("effect intent must be an EffectIntent")
    return EffectIntent(
        action_type=validate_action_type(intent.action_type),
        idempotency_key=validate_idempotency_key(intent.idempotency_key),
        payload=c.validate_payload(intent.payload, field="payload", max_bytes=c.MAX_PAYLOAD_BYTES),
    )


def validate_delivery_intent(intent: Any) -> DeliveryIntent:
    if not isinstance(intent, DeliveryIntent):
        raise c.ValidationError("delivery intent must be a DeliveryIntent")
    return DeliveryIntent(
        kind=c.validate_enum(intent.kind, DeliveryKind, field="kind"),
        payload=c.validate_payload(intent.payload, field="payload", max_bytes=c.MAX_PAYLOAD_BYTES),
    )


# ── WhatsApp-shaped send-response classifier (pure) ──────────────────────────


def _provider_message_id(body: Mapping[str, Any]) -> Optional[str]:
    messages = body.get("messages") if isinstance(body, Mapping) else None
    if not isinstance(messages, Sequence) or isinstance(messages, (str, bytes)) or not messages:
        return None
    first = messages[0]
    candidate = first.get("id") if isinstance(first, Mapping) else None
    if not isinstance(candidate, str) or not candidate:
        return None
    try:
        return c.validate_ref(candidate, field="provider_message_id", max_length=c.MAX_PROVIDER_MESSAGE_ID_LENGTH)
    except c.ValidationError:
        return None


def classify_send_response(response: SendResponse) -> Tuple[ReceiptKind, Optional[str]]:
    """Map a transport response to the receipt it justifies.

    * HTTP 2xx **with** a valid provider message id → ``accepted`` + id.
    * HTTP 2xx **without** one → ``unknown``: success without an id is not proof.
    * timeout, no status, 1xx/3xx, 5xx → ``unknown``.
    * HTTP 4xx → ``rejected`` (the provider refused; nothing was sent).
    Acceptance is not delivery and not reading; those are separate receipts.
    """
    if not isinstance(response, SendResponse):
        raise c.ValidationError("response must be a SendResponse")
    if response.timed_out or response.http_status is None:
        return ReceiptKind.UNKNOWN, None
    status = response.http_status
    if 200 <= status <= 299:
        pmid = _provider_message_id(response.body)
        return (ReceiptKind.ACCEPTED, pmid) if pmid else (ReceiptKind.UNKNOWN, None)
    if 400 <= status <= 499:
        return ReceiptKind.REJECTED, None
    return ReceiptKind.UNKNOWN, None


# ── Derivations for the terminal (pure) ──────────────────────────────────────


def transport_outcome_for(delivery_outcome: Optional[str]) -> str:
    if delivery_outcome is None:
        return c.TransportOutcome.NOT_ATTEMPTED.value
    return {
        DeliveryOutcome.ACCEPTED.value: c.TransportOutcome.ACCEPTED.value,
        DeliveryOutcome.REJECTED.value: c.TransportOutcome.REJECTED_DEFINITIVE.value,
        DeliveryOutcome.UNKNOWN.value: c.TransportOutcome.UNKNOWN.value,
        DeliveryOutcome.PENDING.value: c.TransportOutcome.UNKNOWN.value,
    }[DeliveryOutcome(delivery_outcome).value]


def customer_reach_for(delivery_outcome: Optional[str], reach_receipts: Sequence[str]) -> str:
    """Reach is evidence, not inference: acceptance alone leaves it unknown."""
    if delivery_outcome is None:
        return c.CustomerReach.NOT_APPLICABLE.value
    outcome = DeliveryOutcome(delivery_outcome)
    if outcome is DeliveryOutcome.REJECTED:
        return c.CustomerReach.NOT_REACHED.value
    if outcome is not DeliveryOutcome.ACCEPTED:
        return c.CustomerReach.UNKNOWN.value
    kinds = {ReceiptKind(k) for k in reach_receipts}
    if ReceiptKind.READ in kinds or ReceiptKind.DELIVERED in kinds:
        return c.CustomerReach.REACHED.value
    if ReceiptKind.FAILED in kinds:
        return c.CustomerReach.NOT_REACHED.value
    return c.CustomerReach.UNKNOWN.value


def human_transfer_established(effect: EffectRecord) -> bool:
    """A handoff request is a request. Only explicit transfer evidence in a
    confirmed result proves that a human took ownership; a ``needs_human``
    flag or a recorded request never does."""
    if effect.action_type != ActionType.HANDOFF_REQUEST.value or effect.status != EffectStatus.CONFIRMED.value:
        return False
    transfer = (effect.confirmed_result or {}).get("transfer")
    if not isinstance(transfer, Mapping):
        return False
    owner, accepted_at = transfer.get("human_owner_ref"), transfer.get("accepted_at")
    return isinstance(owner, str) and bool(owner.strip()) and isinstance(accepted_at, str) and bool(accepted_at.strip())


__all__ = [
    "ActionType", "AttemptNotFound", "CompletionBlocked", "DELIVERY_TRANSITIONS", "DeliveryAttemptRecord",
    "DeliveryConflict", "DeliveryDispatchBlocked", "DeliveryIntent", "DeliveryKind", "DeliveryOutcome",
    "DeliveryReceiptRecord", "DeliverySequenceRecord", "DispatchBlock", "DispatchBlocked", "EFFECT_TRANSITIONS",
    "EffectAttemptRecord", "EffectConflict", "EffectIntent", "EffectNotFound", "EffectOutcome", "EffectRecord",
    "EffectReservation", "EffectResultRecord", "EffectStatus", "IllegalTransition", "LedgerError",
    "MAX_ACTION_TYPE_LENGTH", "MAX_DELIVERY_ATTEMPTS", "MAX_DISPATCH_KEY_LENGTH", "MAX_EFFECT_ATTEMPTS",
    "KEY_ENCODING_VERSION", "MAX_EVIDENCE_BYTES", "MAX_IDEMPOTENCY_KEY_LENGTH", "OUTCOME_RECEIPTS",
    "PAYLOAD_HASH_LENGTH", "REACH_RECEIPTS",
    "ReceiptKind", "RecoveryNotPermitted", "RecoveryRefusal", "SendResponse", "SequenceNotFound", "TurnDecision",
    "TurnLedgerSummary", "canonical_json", "classify_send_response", "customer_reach_for",
    "delivery_transition_allowed", "derive_business_key", "effect_transition_allowed", "human_transfer_established",
    "payload_hash", "transport_outcome_for", "validate_action_type", "validate_delivery_intent",
    "validate_effect_intent", "validate_evidence", "validate_idempotency_key",
]

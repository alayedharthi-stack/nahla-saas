"""Contracts of the dormant commerce runtime foundation.

Pure definitions only: namespaces, bounded identities, closed vocabularies,
result records, explicit conflict errors, input validation and the rejection
classifier. Nothing here touches a database, a model, a provider or the
network. Authority: ``docs/architecture/commerce-runtime-foundation-contract.md``.
"""
from __future__ import annotations

import dataclasses
import datetime as _dt
import enum
import json
from typing import Any, Dict, Mapping, Optional, Sequence

# ── Bounds (closed; changing one is a reviewed contract change) ──────────────

MAX_REF_LENGTH = 128                    # conversation_ref, channel_connection_ref
MAX_PROVIDER_MESSAGE_ID_LENGTH = 256
MAX_OWNER_ID_LENGTH = 128
MAX_PAYLOAD_BYTES = 32 * 1024           # turn payload and state payload, canonical JSON
MAX_DETAILS_BYTES = 16 * 1024           # terminal details, canonical JSON
MIN_LEASE_SECONDS = 1
MAX_LEASE_SECONDS = 15 * 60


# ── Closed vocabularies ──────────────────────────────────────────────────────


class Namespace(str, enum.Enum):
    """Execution namespace. ``shadow`` work never touches ``live`` rows."""

    LIVE = "live"
    SHADOW = "shadow"


class ProcessingOutcome(str, enum.Enum):
    """How processing of a turn ended. Independent of transport and reach."""

    COMPLETED = "completed"
    FAILED = "failed"
    ABANDONED = "abandoned"


class TransportOutcome(str, enum.Enum):
    """What the external transport reported. ``unknown`` stays unknown."""

    ACCEPTED = "accepted"
    REJECTED_DEFINITIVE = "rejected_definitive"
    UNKNOWN = "unknown"
    NOT_ATTEMPTED = "not_attempted"


class CustomerReach(str, enum.Enum):
    """Whether the customer was reached, recorded separately from transport."""

    REACHED = "reached"
    NOT_REACHED = "not_reached"
    UNKNOWN = "unknown"
    NOT_APPLICABLE = "not_applicable"


class RejectReason(str, enum.Enum):
    LEASE_HELD = "lease_held"              # claim: an unexpired lease exists
    SUPERSEDED_FENCE = "superseded_fence"  # a later claim issued a higher fence
    INVALID_FENCE = "invalid_fence"        # fence never issued for this conversation
    OBSOLETE_EPOCH = "obsolete_epoch"      # ownership epoch advanced since the token was issued
    STALE_OWNER = "stale_owner"            # lease released, or held by another owner at this fence
    EXPIRED_LEASE = "expired_lease"        # token current but the lease lapsed (database time)
    STALE_REVISION = "stale_revision"      # expected state revision is not the current one
    TURN_NOT_ELIGIBLE = "turn_not_eligible"  # the named turn is not the oldest unresolved turn
    UNCLASSIFIED = "unclassified"          # a guard failed and no rule explains it: fail closed


# ── Errors (explicit; nothing is discarded silently) ─────────────────────────


class CommerceRuntimeError(Exception):
    """Base class of every error raised by the foundation."""


class ValidationError(CommerceRuntimeError, ValueError):
    """An input violates a bound or a closed vocabulary."""


class ConversationNotFound(CommerceRuntimeError):
    """No conversation for this tenant + namespace + reference.

    Raised identically for a foreign tenant's conversation: the foundation
    never reveals whether another tenant owns the reference.
    """


class TurnNotFound(CommerceRuntimeError):
    """No turn for this tenant + namespace + id (same secrecy rule)."""


class AdmissionConflict(CommerceRuntimeError):
    """The inbound identity is already bound to a different conversation."""


@dataclasses.dataclass(frozen=True)
class ConversationSnapshot:
    conversation_id: int
    tenant_id: int
    namespace: str
    conversation_ref: str
    next_sequence: int
    ownership_epoch: int
    lease_owner: Optional[str]
    lease_fence: int
    lease_expires_at: Optional[_dt.datetime]
    state_revision: int
    state_payload: Dict[str, Any]
    db_now: _dt.datetime
    eligible_turn_id: Optional[int] = None      # oldest turn without a terminal, if any
    eligible_sequence: Optional[int] = None


class ScopeMismatch(CommerceRuntimeError):
    """A token issued for one scope was presented against another.

    Raised before any database read or write. The token binds tenant,
    namespace and conversation; a target outside that binding is refused even
    when the owner id, fence and epoch would match.
    """

    def __init__(self, token: "OwnershipToken", *, tenant_id: int, namespace: str, conversation_id: int) -> None:
        self.token = token
        self.target = (tenant_id, namespace, conversation_id)
        super().__init__(
            f"token scope {(token.tenant_id, token.namespace, token.conversation_id)} "
            f"does not match target scope {self.target}"
        )


class OwnershipRejected(CommerceRuntimeError):
    """A claim, renewal, release, commit or terminal was refused.

    ``reason`` is exact and ``snapshot`` is the row as the database saw it in
    the same transaction, so the caller can decide; nothing was changed.
    """

    def __init__(self, reason: RejectReason, snapshot: ConversationSnapshot) -> None:
        self.reason = reason
        self.snapshot = snapshot
        super().__init__(f"{reason.value} (conversation {snapshot.conversation_id})")


class StateConflict(OwnershipRejected):
    """Compare-and-set failed: the expected revision is not the current one."""


class TerminalAlreadyRecorded(CommerceRuntimeError):
    """A turn already has its single immutable terminal record."""

    def __init__(self, existing: "TerminalRecord") -> None:
        self.existing = existing
        super().__init__(f"terminal already recorded for turn {existing.turn_id}")


class CompletionBlocked(CommerceRuntimeError):
    """A terminal may not be recorded for this turn yet, or not by this path.

    Raised under the conversation lock, before anything is written, when the
    turn's ledgers (revision ``0109`` tables, when present) hold effect or
    delivery intents that were reserved but never dispatched, or attempts
    whose outcome is not established (``actionable_work_remains``); or when
    a ledger-bearing turn is completed through the foundation entry point,
    which cannot derive transport and reach from the ledgers
    (``ledger_bearing_turn``). Turns without ledger records, and databases
    without the ledger tables, are unaffected.
    """

    def __init__(self, reason: str, blockers: Sequence[str] = ()) -> None:
        self.reason = reason
        self.blockers = tuple(blockers)
        detail = "; ".join(self.blockers)
        super().__init__(f"{reason}: {detail}" if detail else reason)


# ── Records ──────────────────────────────────────────────────────────────────


@dataclasses.dataclass(frozen=True)
class OwnershipToken:
    """What a worker presents on every guarded operation.

    The token is bound to the scope it was issued for (tenant, namespace,
    conversation) as well as to the owner, fence and epoch; every
    token-bearing operation validates the complete binding.
    """

    owner_id: str
    fence: int
    epoch: int
    tenant_id: int
    namespace: str
    conversation_id: int


@dataclasses.dataclass(frozen=True)
class TurnRecord:
    turn_id: int
    conversation_id: int
    tenant_id: int
    namespace: str
    sequence: int
    channel_connection_ref: str
    provider_message_id: str
    admitted_at: _dt.datetime
    payload: Dict[str, Any]


@dataclasses.dataclass(frozen=True)
class AdmittedTurn(TurnRecord):
    duplicate: bool = False


@dataclasses.dataclass(frozen=True)
class Lease:
    conversation_id: int
    tenant_id: int
    namespace: str
    owner_id: str
    fence: int
    epoch: int
    expires_at: _dt.datetime
    db_now: _dt.datetime
    takeover: bool
    eligible_turn_id: Optional[int] = None      # oldest unresolved turn at claim time, if any
    eligible_sequence: Optional[int] = None

    @property
    def token(self) -> OwnershipToken:
        return OwnershipToken(
            owner_id=self.owner_id, fence=self.fence, epoch=self.epoch,
            tenant_id=self.tenant_id, namespace=self.namespace, conversation_id=self.conversation_id,
        )


@dataclasses.dataclass(frozen=True)
class StateCommit:
    conversation_id: int
    revision: int
    fence: int
    epoch: int
    committed_at: _dt.datetime


@dataclasses.dataclass(frozen=True)
class StateTransition:
    """Optional compare-and-set applied atomically with a terminal record."""

    expected_revision: int
    payload: Mapping[str, Any]


@dataclasses.dataclass(frozen=True)
class TerminalRecord:
    turn_id: int
    conversation_id: int
    tenant_id: int
    namespace: str
    processing_outcome: str
    transport_outcome: str
    customer_reach: str
    recorded_fence: int
    recorded_epoch: int
    recorded_by: str
    details: Dict[str, Any]
    recorded_at: _dt.datetime


# ── Validation (fail closed) ─────────────────────────────────────────────────


def _is_int(value: Any) -> bool:
    return isinstance(value, int) and not isinstance(value, bool)


def validate_tenant_id(value: Any) -> int:
    if not _is_int(value) or value < 1:
        raise ValidationError("tenant_id must be a positive integer")
    return int(value)


def validate_namespace(value: Any) -> Namespace:
    try:
        return Namespace(value.value if isinstance(value, Namespace) else value)
    except ValueError:
        raise ValidationError(f"namespace must be one of {[n.value for n in Namespace]}") from None


def validate_enum(value: Any, enum_cls: type, *, field: str) -> str:
    try:
        return enum_cls(value.value if isinstance(value, enum_cls) else value).value
    except ValueError:
        raise ValidationError(f"{field} must be one of {[m.value for m in enum_cls]}") from None


def validate_ref(value: Any, *, field: str, max_length: int) -> str:
    """A bounded, printable, whitespace-free opaque reference."""
    if not isinstance(value, str) or not value:
        raise ValidationError(f"{field} must be a non-empty string")
    if len(value) > max_length:
        raise ValidationError(f"{field} exceeds {max_length} characters")
    if any((not ch.isprintable()) or ch.isspace() for ch in value):
        raise ValidationError(f"{field} contains whitespace or non-printable characters")
    return value


def validate_owner_id(value: Any) -> str:
    return validate_ref(value, field="owner_id", max_length=MAX_OWNER_ID_LENGTH)


def validate_counter(value: Any, *, field: str) -> int:
    if not _is_int(value) or value < 0:
        raise ValidationError(f"{field} must be a non-negative integer")
    return int(value)


def validate_lease_seconds(value: Any) -> int:
    if not _is_int(value) or not MIN_LEASE_SECONDS <= value <= MAX_LEASE_SECONDS:
        raise ValidationError(
            f"lease_seconds must be an integer between {MIN_LEASE_SECONDS} and {MAX_LEASE_SECONDS}"
        )
    return int(value)


def validate_payload(value: Any, *, field: str, max_bytes: int) -> Dict[str, Any]:
    """A JSON object, canonically serialisable, bounded in encoded size.

    Returns a plain ``dict`` copy produced by a JSON round-trip, so the stored
    value is exactly what the bound was measured on.
    """
    if value is None:
        return {}
    if not isinstance(value, Mapping) or any(not isinstance(k, str) for k in value):
        raise ValidationError(f"{field} must be a JSON object with string keys")
    try:
        canonical = json.dumps(
            dict(value), sort_keys=True, ensure_ascii=False, allow_nan=False, separators=(",", ":"),
        )
    except (TypeError, ValueError) as exc:
        raise ValidationError(f"{field} is not JSON-serialisable: {exc}") from None
    size = len(canonical.encode("utf-8"))
    if size > max_bytes:
        raise ValidationError(f"{field} is {size} bytes; the bound is {max_bytes}")
    return json.loads(canonical)


def validate_token(token: Any) -> OwnershipToken:
    if not isinstance(token, OwnershipToken):
        raise ValidationError("token must be an OwnershipToken")
    return OwnershipToken(
        owner_id=validate_owner_id(token.owner_id),
        fence=validate_counter(token.fence, field="fence"),
        epoch=validate_counter(token.epoch, field="epoch"),
        tenant_id=validate_tenant_id(token.tenant_id),
        namespace=validate_namespace(token.namespace).value,
        conversation_id=validate_counter(token.conversation_id, field="conversation_id"),
    )


def require_token_scope(token: OwnershipToken, *, tenant_id: int, namespace: str, conversation_id: int) -> None:
    """Refuse a token presented outside the scope it was issued for."""
    if (token.tenant_id, token.namespace, token.conversation_id) != (tenant_id, namespace, conversation_id):
        raise ScopeMismatch(token, tenant_id=tenant_id, namespace=namespace, conversation_id=conversation_id)


# ── Rejection classifier (pure) ──────────────────────────────────────────────


def classify_rejection(
    *,
    current_owner: Optional[str],
    current_fence: int,
    current_epoch: int,
    current_expires_at: Optional[_dt.datetime],
    current_revision: int,
    token: OwnershipToken,
    db_now: _dt.datetime,
    expected_revision: Optional[int] = None,
) -> Optional[RejectReason]:
    """Exact reason a presented token may not act on the current row.

    ``None`` means every guard holds. The order is deliberate: a superseded
    or unknown fence is reported before the epoch, the epoch before the
    owner, the owner before expiry, and the state revision last, so the
    caller learns the earliest fact that invalidates its work.
    """
    if token.fence < current_fence:
        return RejectReason.SUPERSEDED_FENCE
    if token.fence > current_fence:
        return RejectReason.INVALID_FENCE
    if token.epoch != current_epoch:
        return RejectReason.OBSOLETE_EPOCH
    if current_owner is None or current_owner != token.owner_id:
        return RejectReason.STALE_OWNER
    if current_expires_at is None or current_expires_at <= db_now:
        return RejectReason.EXPIRED_LEASE
    if expected_revision is not None and expected_revision != current_revision:
        return RejectReason.STALE_REVISION
    return None


__all__ = [
    "AdmissionConflict", "AdmittedTurn", "CommerceRuntimeError", "CompletionBlocked", "ConversationNotFound",
    "ConversationSnapshot", "CustomerReach", "Lease", "MAX_DETAILS_BYTES", "MAX_LEASE_SECONDS",
    "MAX_OWNER_ID_LENGTH", "MAX_PAYLOAD_BYTES", "MAX_PROVIDER_MESSAGE_ID_LENGTH", "MAX_REF_LENGTH",
    "MIN_LEASE_SECONDS", "Namespace", "OwnershipRejected", "OwnershipToken", "ProcessingOutcome",
    "RejectReason", "ScopeMismatch", "StateCommit", "StateConflict", "StateTransition", "TerminalAlreadyRecorded",
    "TerminalRecord", "TransportOutcome", "TurnNotFound", "TurnRecord", "ValidationError",
    "classify_rejection", "require_token_scope", "validate_counter", "validate_enum", "validate_lease_seconds",
    "validate_namespace", "validate_owner_id", "validate_payload", "validate_ref",
    "validate_tenant_id", "validate_token",
]

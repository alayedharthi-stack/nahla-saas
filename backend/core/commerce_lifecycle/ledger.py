"""
Lifecycle notification ledger — idempotency reservation and send audit.

Owns cross-worker outbound template send deduplication plus structured audit
metadata. Shadow reservation remains for diagnostics; production dispatch
uses ``reserve_send_decision`` / ``finalize_send_outcome``.

Transaction ownership: callers pass an open SQLAlchemy ``Session`` and own
``commit()`` / ``rollback()``. Reservation uses a SAVEPOINT via
``session.begin_nested()`` so duplicate-key conflicts never roll back
unrelated caller work in the outer transaction.
"""
from __future__ import annotations

import hashlib
import json
import os
import re
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from enum import Enum
from typing import Any, FrozenSet, Mapping, Optional, Sequence, Tuple, Union

from sqlalchemy import or_
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from core.commerce_lifecycle.definitions import (
    KNOWN_CAPABILITY_FIELDS,
    KNOWN_EVIDENCE_FIELDS,
)
from core.commerce_lifecycle.intents import BusinessIntent

_ALLOWED_CHANNELS: FrozenSet[str] = frozenset({
    "whatsapp",
})

_ALLOWED_DISPATCH_DECISION_KEYS: FrozenSet[str] = frozenset({
    "handoff_kind",
    "intent",
    "open_window_strategy",
    "closed_window_strategy",
    "service_key",
    "reason_code",
    "business_evidence_valid",
    "capabilities_valid",
    "template_evidence_valid",
    "template_missing_evidence",
    "send_method",
    "window_source",
})

_ALLOWED_SEND_METHODS: FrozenSet[str] = frozenset({
    "session_message",
    "approved_template",
})

_FORBIDDEN_PAYLOAD_KEYS: FrozenSet[str] = frozenset({
    "message_text",
    "prompt",
    "system_prompt",
    "model",
    "access_token",
    "token",
    "api_key",
    "authorization",
    "payment_url",
    "checkout_url",
    "tracking_url",
    "review_url",
    "coupon_code",
    "bank_account",
    "iban",
    "card_number",
    "customer_name",
    "customer_phone",
    "phone",
    "destination",
    "destination_hash",
})

_SENT_OUTCOMES: FrozenSet[str] = frozenset({
    "sent",
    "delivered",
    "message_sent",
})


class ShadowLedgerOutcome(str, Enum):
    """Ledger-only outcomes — no customer send states in PR 2B."""

    SHADOW_RESERVED = "shadow_reserved"
    SHADOW_ELIGIBLE = "shadow_eligible"
    SHADOW_BLOCKED = "shadow_blocked"
    SHADOW_NO_NOTIFICATION = "shadow_no_notification"
    SHADOW_MERCHANT_ACTION = "shadow_merchant_action"
    SHADOW_ERROR = "shadow_error"


_MARKABLE_OUTCOMES: FrozenSet[ShadowLedgerOutcome] = frozenset({
    ShadowLedgerOutcome.SHADOW_ELIGIBLE,
    ShadowLedgerOutcome.SHADOW_BLOCKED,
    ShadowLedgerOutcome.SHADOW_NO_NOTIFICATION,
    ShadowLedgerOutcome.SHADOW_MERCHANT_ACTION,
    ShadowLedgerOutcome.SHADOW_ERROR,
})

class SendLedgerOutcome(str, Enum):
    """Outbound template send lifecycle outcomes."""

    SEND_RESERVED = "send_reserved"
    SEND_SENDING = "send_sending"
    SEND_SKIPPED = "send_skipped"
    SEND_BLOCKED = "send_blocked"
    SENT = "sent"
    FAILED = "failed"
    AMBIGUOUS = "ambiguous"


_SEND_STATE_RESERVED = "reserved"
_SEND_STATE_SENDING = "sending"
_SEND_STATE_SENT = "sent"
_SEND_STATE_FAILED = "failed"
_SEND_STATE_AMBIGUOUS = "ambiguous"
_SEND_STATE_SKIPPED = "skipped"
_SEND_STATE_BLOCKED = "blocked"

_TERMINAL_NO_RESEND_STATES: FrozenSet[str] = frozenset({
    _SEND_STATE_SENT,
    _SEND_STATE_AMBIGUOUS,
})

_REEVALUABLE_BLOCK_ERROR_CODES: FrozenSet[str] = frozenset({
    "missing_customer_phone",
    "no_approved_template",
})

_MAX_SEND_ATTEMPTS = 2
_MAX_RECLAIM_COUNT = 5
_DEFAULT_STALE_SEND_SECONDS = 300

_SHADOW_OUTCOME_PREFIX = "shadow_"

_VALID_INTENT_VALUES: FrozenSet[str] = frozenset(i.value for i in BusinessIntent)


@dataclass(frozen=True)
class ReserveShadowResult:
    ledger_id: int
    idempotency_key: str
    duplicate: bool
    outcome: str


@dataclass(frozen=True)
class MarkShadowOutcomeResult:
    ledger_id: int
    outcome: str
    reason_code: Optional[str]


@dataclass(frozen=True)
class ReserveSendResult:
    ledger_id: int
    idempotency_key: str
    duplicate: bool
    outcome: str
    send_state: Optional[str]
    recovered: bool = False


@dataclass(frozen=True)
class FinalizeSendResult:
    ledger_id: int
    outcome: str
    send_state: str
    provider_message_id: Optional[str]
    send_error_code: Optional[str]


@dataclass(frozen=True)
class MarkSendSendingResult:
    ledger_id: int
    outcome: str
    send_state: str
    provider_message_id: Optional[str]
    send_error_code: Optional[str]
    transitioned: bool


def _normalize_required_text(value: str, *, field: str) -> str:
    text = str(value or "").strip()
    if not text:
        raise ValueError(f"{field} must not be empty")
    return text


def _normalize_optional_transition_field(value: Optional[str], *, field: str) -> Optional[str]:
    if value is None:
        return None
    text = str(value).strip()
    if not text:
        raise ValueError(f"{field} must be None or non-empty text")
    return text


def build_lifecycle_idempotency_key(
    *,
    tenant_id: int,
    order_id: int,
    business_intent: Union[BusinessIntent, str],
    channel: str,
    source_event_id: Optional[str] = None,
    transition_version: Optional[str] = None,
) -> str:
    """
    Canonical idempotency digest for ``(tenant_id, idempotency_key)`` uniqueness.

    ``tenant_id`` is enforced by the DB unique constraint and is intentionally
    omitted from the hashed payload to avoid redundant encoding. Components are
    canonicalized as JSON then hashed so delimiter characters in event ids cannot
    collide. ``None`` and ``""`` are not interchangeable — absent fields are
    ``null`` in the payload; whitespace-only values are rejected.
    """
    _validate_tenant_order(tenant_id, order_id)
    intent_value = _validate_business_intent(business_intent)
    channel_value = _normalize_required_text(channel, field="channel").lower()
    if channel_value not in _ALLOWED_CHANNELS:
        raise ValueError(f"unsupported channel: {channel_value!r}")

    payload = {
        "order_id": int(order_id),
        "business_intent": intent_value,
        "channel": channel_value,
        "source_event_id": _normalize_optional_transition_field(
            source_event_id,
            field="source_event_id",
        ),
        "transition_version": _normalize_optional_transition_field(
            transition_version,
            field="transition_version",
        ),
    }
    canonical = json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=True)
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def build_idempotency_key_from_fields(
    key_fields: Sequence[str],
    context: Mapping[str, Any],
) -> str:
    """Build a digest using declarative ``idempotency_key_fields`` from PR 2A."""
    if not key_fields:
        raise ValueError("idempotency_key_fields must not be empty")
    payload: dict[str, Any] = {}
    for field in key_fields:
        name = str(field).strip()
        if not name:
            raise ValueError("empty idempotency key field name")
        if name == "tenant_id":
            continue
        value = context.get(name)
        if value is None:
            payload[name] = None
        else:
            payload[name] = str(value).strip()
    canonical = json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=True)
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def _validate_business_intent(business_intent: Union[BusinessIntent, str]) -> str:
    value = (
        business_intent.value
        if isinstance(business_intent, BusinessIntent)
        else str(business_intent or "").strip()
    )
    if value not in _VALID_INTENT_VALUES:
        raise ValueError(f"unsupported business_intent: {value!r}")
    return value


def _validate_tenant_order(tenant_id: int, order_id: int) -> Tuple[int, int]:
    tid = int(tenant_id)
    oid = int(order_id)
    if tid <= 0:
        raise ValueError("tenant_id must be positive")
    if oid <= 0:
        raise ValueError("order_id must be positive")
    return tid, oid


def _walk_forbidden_keys(value: Any, *, label: str, path: str = "") -> None:
    if isinstance(value, Mapping):
        for key, nested in value.items():
            key_text = str(key)
            lowered = key_text.lower()
            current = f"{path}.{key_text}" if path else key_text
            if lowered in _FORBIDDEN_PAYLOAD_KEYS:
                raise ValueError(f"{label} must not contain {current!r}")
            if any(lowered.endswith(suffix) for suffix in ("_url", "_link")):
                raise ValueError(f"{label} must not contain URL-like key {current!r}")
            _walk_forbidden_keys(nested, label=label, path=current)
    elif isinstance(value, (list, tuple, set)):
        for idx, nested in enumerate(value):
            _walk_forbidden_keys(nested, label=label, path=f"{path}[{idx}]")


def sanitize_capabilities_snapshot(
    capabilities: Optional[Mapping[str, Any]],
) -> dict[str, bool]:
    if not capabilities:
        return {}
    out: dict[str, bool] = {}
    for key, value in capabilities.items():
        name = str(key).strip()
        if name not in KNOWN_CAPABILITY_FIELDS:
            raise ValueError(f"unknown capability field {name!r}")
        if not isinstance(value, bool):
            raise ValueError(f"capability {name!r} must be boolean")
        out[name] = value
    return out


def sanitize_evidence_present(fields: Optional[Sequence[str]]) -> Tuple[str, ...]:
    if not fields:
        return ()
    seen: list[str] = []
    for item in fields:
        name = str(item).strip()
        if not name:
            raise ValueError("evidence field names must not be empty")
        if name not in KNOWN_EVIDENCE_FIELDS:
            raise ValueError(f"unknown evidence field {name!r}")
        if name not in seen:
            seen.append(name)
    return tuple(seen)



def normalize_send_method(value: Optional[str]) -> Optional[str]:
    """Return validated send_method or None (legacy / undecided)."""
    if value is None:
        return None
    method = str(value).strip()
    if not method:
        return None
    if method not in _ALLOWED_SEND_METHODS:
        raise ValueError(f"unsupported send_method: {method!r}")
    return method

def sanitize_dispatch_decision(payload: Optional[Mapping[str, Any]]) -> dict[str, str]:
    if not payload:
        return {}
    _walk_forbidden_keys(payload, label="dispatch_decision_json")
    out: dict[str, str] = {}
    for key, value in payload.items():
        name = str(key).strip()
        if name not in _ALLOWED_DISPATCH_DECISION_KEYS:
            raise ValueError(f"unknown dispatch_decision key {name!r}")
        if isinstance(value, (dict, list, tuple, set)):
            raise ValueError(f"dispatch_decision value for {name!r} must be scalar")
        text = str(value).strip()
        if not text:
            raise ValueError(f"dispatch_decision value for {name!r} must not be empty")
        out[name] = text
    return out


def reserve_shadow_decision(
    db: Session,
    *,
    tenant_id: int,
    order_id: int,
    business_intent: Union[BusinessIntent, str],
    channel: str,
    source_event_id: Optional[str] = None,
    transition_version: Optional[str] = None,
    dispatch_decision: Optional[Mapping[str, Any]] = None,
    capabilities_snapshot: Optional[Mapping[str, Any]] = None,
    evidence_present: Optional[Sequence[str]] = None,
    automation_execution_id: Optional[int] = None,
    commit: bool = False,
) -> ReserveShadowResult:
    """
    Reserve a shadow ledger row inside a SAVEPOINT. Duplicate keys return the
    existing row without mutating its outcome and without rolling back unrelated
    caller work in the outer transaction.
    """
    from models import CommerceLifecycleNotificationLedger  # noqa: PLC0415

    tid, oid = _validate_tenant_order(tenant_id, order_id)
    intent_value = _validate_business_intent(business_intent)
    channel_value = _normalize_required_text(channel, field="channel").lower()
    if channel_value not in _ALLOWED_CHANNELS:
        raise ValueError(f"unsupported channel: {channel_value!r}")

    normalized_source_event_id = _normalize_optional_transition_field(
        source_event_id,
        field="source_event_id",
    )
    normalized_transition_version = _normalize_optional_transition_field(
        transition_version,
        field="transition_version",
    )

    idempotency_key = build_lifecycle_idempotency_key(
        tenant_id=tid,
        order_id=oid,
        business_intent=intent_value,
        channel=channel_value,
        source_event_id=normalized_source_event_id,
        transition_version=normalized_transition_version,
    )

    row = CommerceLifecycleNotificationLedger(
        tenant_id=tid,
        order_id=oid,
        business_intent=intent_value,
        channel=channel_value,
        source_event_id=normalized_source_event_id,
        transition_version=normalized_transition_version,
        idempotency_key=idempotency_key,
        outcome=ShadowLedgerOutcome.SHADOW_RESERVED.value,
        dispatch_decision_json=sanitize_dispatch_decision(dispatch_decision),
        capabilities_snapshot_json=sanitize_capabilities_snapshot(capabilities_snapshot),
        evidence_present_json=list(sanitize_evidence_present(evidence_present)),
        automation_execution_id=automation_execution_id,
    )

    try:
        with db.begin_nested():
            db.add(row)
            db.flush()
    except IntegrityError:
        existing = (
            db.query(CommerceLifecycleNotificationLedger)
            .filter_by(tenant_id=tid, idempotency_key=idempotency_key)
            .one()
        )
        return ReserveShadowResult(
            ledger_id=int(existing.id),
            idempotency_key=idempotency_key,
            duplicate=True,
            outcome=existing.outcome,
        )

    if commit:
        db.commit()
    return ReserveShadowResult(
        ledger_id=int(row.id),
        idempotency_key=idempotency_key,
        duplicate=False,
        outcome=row.outcome,
    )


def mark_shadow_outcome(
    db: Session,
    *,
    ledger_id: int,
    tenant_id: int,
    outcome: Union[ShadowLedgerOutcome, str],
    reason_code: Optional[str] = None,
    commit: bool = False,
) -> MarkShadowOutcomeResult:
    """Update ledger outcome only — no customer-facing action."""
    from models import CommerceLifecycleNotificationLedger  # noqa: PLC0415

    tid = int(tenant_id)
    if tid <= 0:
        raise ValueError("tenant_id must be positive")

    outcome_value = outcome.value if isinstance(outcome, ShadowLedgerOutcome) else str(outcome)
    if outcome_value in _SENT_OUTCOMES:
        raise ValueError(f"outcome {outcome_value!r} is not permitted")

    try:
        parsed_outcome = ShadowLedgerOutcome(outcome_value)
    except ValueError as exc:
        raise ValueError(f"invalid shadow ledger outcome: {outcome_value!r}") from exc

    if parsed_outcome not in _MARKABLE_OUTCOMES:
        raise ValueError(f"outcome {outcome_value!r} is not markable")

    if reason_code is not None:
        reason_text = str(reason_code).strip()
        if len(reason_text) > 64:
            raise ValueError("reason_code exceeds maximum length")
        if not re.fullmatch(r"[a-z][a-z0-9_]*", reason_text):
            raise ValueError("reason_code must be machine-readable snake_case")
    else:
        reason_text = None

    row = (
        db.query(CommerceLifecycleNotificationLedger)
        .filter_by(id=int(ledger_id), tenant_id=tid)
        .one()
    )

    if row.outcome == parsed_outcome.value:
        return MarkShadowOutcomeResult(
            ledger_id=int(row.id),
            outcome=row.outcome,
            reason_code=row.reason_code,
        )

    if row.outcome != ShadowLedgerOutcome.SHADOW_RESERVED.value:
        raise ValueError(
            f"ledger {ledger_id} outcome transition not allowed from {row.outcome!r}"
        )

    row.outcome = parsed_outcome.value
    row.reason_code = reason_text
    db.flush()
    if commit:
        db.commit()

    return MarkShadowOutcomeResult(
        ledger_id=int(row.id),
        outcome=row.outcome,
        reason_code=row.reason_code,
    )


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


def lifecycle_send_stale_seconds() -> int:
    raw = str(os.environ.get("COMMERCE_LIFECYCLE_SEND_STALE_SECONDS", "")).strip()
    if raw:
        try:
            return max(0, int(raw))
        except ValueError:
            pass
    return _DEFAULT_STALE_SEND_SECONDS


def _row_activity_at(row: Any) -> Optional[datetime]:
    for attr in ("send_attempted_at", "send_reserved_at", "updated_at", "created_at"):
        value = getattr(row, attr, None)
        if value is not None:
            return value
    return None


def _row_is_stale(row: Any, *, now: Optional[datetime] = None) -> bool:
    threshold = lifecycle_send_stale_seconds()
    if threshold <= 0:
        return True
    activity = _row_activity_at(row)
    if activity is None:
        return True
    if activity.tzinfo is None:
        activity = activity.replace(tzinfo=timezone.utc)
    current = now or _utcnow()
    return activity <= current - timedelta(seconds=threshold)


def _failed_retry_allowed(row: Any) -> bool:
    if str(row.send_state or "").strip() != _SEND_STATE_FAILED:
        return False
    if str(row.provider_message_id or "").strip():
        return False
    if str(row.send_state or "").strip() == _SEND_STATE_AMBIGUOUS:
        return False
    attempt_count = int(getattr(row, "send_attempt_count", 0) or 0)
    return attempt_count < _MAX_SEND_ATTEMPTS


def _blocked_reevaluation_allowed(row: Any) -> bool:
    if str(row.send_state or "").strip() != _SEND_STATE_BLOCKED:
        return False
    code = str(row.send_error_code or "").strip()
    return code in _REEVALUABLE_BLOCK_ERROR_CODES


def try_conditional_reclaim_send_row(
    db: Session,
    *,
    tenant_id: int,
    ledger_id: int,
    now: Optional[datetime] = None,
) -> bool:
    """
    Atomically reclaim a stale reserved/sending row for another worker.

    Returns True when this worker won the conditional transition.
    """
    from models import CommerceLifecycleNotificationLedger  # noqa: PLC0415

    current = now or _utcnow()
    stale_before = current - timedelta(seconds=max(lifecycle_send_stale_seconds(), 0))
    table = CommerceLifecycleNotificationLedger

    updated = (
        db.query(table)
        .filter(
            table.id == int(ledger_id),
            table.tenant_id == int(tenant_id),
            table.send_state.in_((_SEND_STATE_RESERVED, _SEND_STATE_SENDING)),
            table.reclaim_count < _MAX_RECLAIM_COUNT,
            table.send_attempt_count < _MAX_SEND_ATTEMPTS,
            table.provider_message_id.is_(None),
            or_(
                table.send_attempted_at.is_(None),
                table.send_attempted_at < stale_before,
                table.send_reserved_at.is_(None),
                table.send_reserved_at < stale_before,
            ),
        )
        .update(
            {
                table.outcome: SendLedgerOutcome.SEND_RESERVED.value,
                table.send_state: _SEND_STATE_RESERVED,
                table.send_reserved_at: current,
                table.last_reclaimed_at: current,
                table.reclaim_count: table.reclaim_count + 1,
                table.send_error_code: None,
                table.send_completed_at: None,
            },
            synchronize_session=False,
        )
    )
    if updated:
        db.flush()
    return bool(updated)


def try_conditional_retry_failed_send_row(
    db: Session,
    *,
    tenant_id: int,
    ledger_id: int,
    now: Optional[datetime] = None,
) -> bool:
    """Allow one bounded automatic retry for failed rows without a WAMID."""
    from models import CommerceLifecycleNotificationLedger  # noqa: PLC0415

    current = now or _utcnow()
    table = CommerceLifecycleNotificationLedger

    updated = (
        db.query(table)
        .filter(
            table.id == int(ledger_id),
            table.tenant_id == int(tenant_id),
            table.send_state == _SEND_STATE_FAILED,
            table.provider_message_id.is_(None),
            table.send_attempt_count < _MAX_SEND_ATTEMPTS,
        )
        .update(
            {
                table.outcome: SendLedgerOutcome.SEND_RESERVED.value,
                table.send_state: _SEND_STATE_RESERVED,
                table.send_reserved_at: current,
                table.send_error_code: None,
                table.send_completed_at: None,
            },
            synchronize_session=False,
        )
    )
    if updated:
        db.flush()
    return bool(updated)


def try_conditional_promote_shadow_send_row(
    db: Session,
    *,
    tenant_id: int,
    ledger_id: int,
    dispatch_decision: Optional[Mapping[str, Any]] = None,
    capabilities_snapshot: Optional[Mapping[str, Any]] = None,
    evidence_present: Optional[Sequence[str]] = None,
    service_key_audit: Optional[str] = None,
    template_name_audit: Optional[str] = None,
    now: Optional[datetime] = None,
) -> bool:
    """
    Atomically promote a shadow-only ledger row to ``send_reserved``.

    Returns True when this worker won the conditional transition.
    """
    from models import CommerceLifecycleNotificationLedger  # noqa: PLC0415

    current = now or _utcnow()
    table = CommerceLifecycleNotificationLedger
    values: dict[Any, Any] = {
        table.outcome: SendLedgerOutcome.SEND_RESERVED.value,
        table.send_state: _SEND_STATE_RESERVED,
        table.send_reserved_at: current,
        table.send_attempt_count: 0,
        table.reclaim_count: 0,
    }
    if dispatch_decision is not None:
        values[table.dispatch_decision_json] = sanitize_dispatch_decision(dispatch_decision)
    if capabilities_snapshot is not None:
        values[table.capabilities_snapshot_json] = sanitize_capabilities_snapshot(
            capabilities_snapshot
        )
    if evidence_present is not None:
        values[table.evidence_present_json] = list(sanitize_evidence_present(evidence_present))
    if service_key_audit is not None:
        values[table.template_service_key] = service_key_audit
    if template_name_audit is not None:
        values[table.template_name] = template_name_audit

    updated = (
        db.query(table)
        .filter(
            table.id == int(ledger_id),
            table.tenant_id == int(tenant_id),
            table.send_state.is_(None),
            table.outcome.like(f"{_SHADOW_OUTCOME_PREFIX}%"),
        )
        .update(values, synchronize_session=False)
    )
    if updated:
        db.flush()
    return bool(updated)


def try_conditional_reevaluate_blocked_send_row(
    db: Session,
    *,
    tenant_id: int,
    ledger_id: int,
    now: Optional[datetime] = None,
) -> bool:
    """Re-open blocked rows for eligibility checks without a provider resend."""
    from models import CommerceLifecycleNotificationLedger  # noqa: PLC0415

    current = now or _utcnow()
    table = CommerceLifecycleNotificationLedger

    updated = (
        db.query(table)
        .filter(
            table.id == int(ledger_id),
            table.tenant_id == int(tenant_id),
            table.send_state == _SEND_STATE_BLOCKED,
            table.send_error_code.in_(tuple(_REEVALUABLE_BLOCK_ERROR_CODES)),
        )
        .update(
            {
                table.outcome: SendLedgerOutcome.SEND_RESERVED.value,
                table.send_state: _SEND_STATE_RESERVED,
                table.send_reserved_at: current,
                table.send_error_code: None,
                table.send_completed_at: None,
            },
            synchronize_session=False,
        )
    )
    if updated:
        db.flush()
    return bool(updated)


def _resolve_existing_send_reservation(
    db: Session,
    existing: Any,
    *,
    tenant_id: int,
    idempotency_key: str,
    dispatch_decision: Optional[Mapping[str, Any]],
    capabilities_snapshot: Optional[Mapping[str, Any]],
    evidence_present: Optional[Sequence[str]],
    service_key_audit: Optional[str],
    template_name_audit: Optional[str],
    commit: bool,
) -> ReserveSendResult:
    existing_state = str(existing.send_state or "").strip()

    if existing_state in _TERMINAL_NO_RESEND_STATES:
        return ReserveSendResult(
            ledger_id=int(existing.id),
            idempotency_key=idempotency_key,
            duplicate=True,
            outcome=existing.outcome,
            send_state=existing_state or None,
        )

    if existing_state in {_SEND_STATE_RESERVED, _SEND_STATE_SENDING}:
        if not _row_is_stale(existing):
            return ReserveSendResult(
                ledger_id=int(existing.id),
                idempotency_key=idempotency_key,
                duplicate=True,
                outcome=existing.outcome,
                send_state=existing_state,
            )
        if try_conditional_reclaim_send_row(
            db,
            tenant_id=tenant_id,
            ledger_id=int(existing.id),
        ):
            db.refresh(existing)
            if commit:
                db.commit()
            return ReserveSendResult(
                ledger_id=int(existing.id),
                idempotency_key=idempotency_key,
                duplicate=False,
                recovered=True,
                outcome=existing.outcome,
                send_state=existing.send_state,
            )
        return ReserveSendResult(
            ledger_id=int(existing.id),
            idempotency_key=idempotency_key,
            duplicate=True,
            outcome=existing.outcome,
            send_state=existing_state,
        )

    if existing_state == _SEND_STATE_FAILED:
        if _failed_retry_allowed(existing) and try_conditional_retry_failed_send_row(
            db,
            tenant_id=tenant_id,
            ledger_id=int(existing.id),
        ):
            db.refresh(existing)
            if commit:
                db.commit()
            return ReserveSendResult(
                ledger_id=int(existing.id),
                idempotency_key=idempotency_key,
                duplicate=False,
                recovered=True,
                outcome=existing.outcome,
                send_state=existing.send_state,
            )
        return ReserveSendResult(
            ledger_id=int(existing.id),
            idempotency_key=idempotency_key,
            duplicate=True,
            outcome=existing.outcome,
            send_state=existing_state,
        )

    if existing_state == _SEND_STATE_BLOCKED:
        if _blocked_reevaluation_allowed(existing) and try_conditional_reevaluate_blocked_send_row(
            db,
            tenant_id=tenant_id,
            ledger_id=int(existing.id),
        ):
            db.refresh(existing)
            if commit:
                db.commit()
            return ReserveSendResult(
                ledger_id=int(existing.id),
                idempotency_key=idempotency_key,
                duplicate=False,
                recovered=True,
                outcome=existing.outcome,
                send_state=existing.send_state,
            )
        return ReserveSendResult(
            ledger_id=int(existing.id),
            idempotency_key=idempotency_key,
            duplicate=True,
            outcome=existing.outcome,
            send_state=existing_state,
        )

    if _is_shadow_only_row(existing):
        if try_conditional_promote_shadow_send_row(
            db,
            tenant_id=tenant_id,
            ledger_id=int(existing.id),
            dispatch_decision=dispatch_decision,
            capabilities_snapshot=capabilities_snapshot,
            evidence_present=evidence_present,
            service_key_audit=service_key_audit,
            template_name_audit=template_name_audit,
        ):
            db.refresh(existing)
            if commit:
                db.commit()
            return ReserveSendResult(
                ledger_id=int(existing.id),
                idempotency_key=idempotency_key,
                duplicate=False,
                outcome=existing.outcome,
                send_state=existing.send_state,
            )
        db.refresh(existing)
        return ReserveSendResult(
            ledger_id=int(existing.id),
            idempotency_key=idempotency_key,
            duplicate=True,
            outcome=existing.outcome,
            send_state=existing.send_state,
        )

    return ReserveSendResult(
        ledger_id=int(existing.id),
        idempotency_key=idempotency_key,
        duplicate=True,
        outcome=existing.outcome,
        send_state=existing_state or None,
    )


def _is_shadow_only_row(row: Any) -> bool:
    send_state = str(getattr(row, "send_state", None) or "").strip()
    if send_state:
        return False
    outcome = str(getattr(row, "outcome", "") or "")
    return outcome.startswith(_SHADOW_OUTCOME_PREFIX)


def _send_state_for_outcome(outcome: SendLedgerOutcome) -> str:
    if outcome == SendLedgerOutcome.SEND_RESERVED:
        return _SEND_STATE_RESERVED
    if outcome == SendLedgerOutcome.SEND_SENDING:
        return _SEND_STATE_SENDING
    if outcome == SendLedgerOutcome.SENT:
        return _SEND_STATE_SENT
    if outcome == SendLedgerOutcome.FAILED:
        return _SEND_STATE_FAILED
    if outcome == SendLedgerOutcome.AMBIGUOUS:
        return _SEND_STATE_AMBIGUOUS
    if outcome == SendLedgerOutcome.SEND_SKIPPED:
        return _SEND_STATE_SKIPPED
    if outcome == SendLedgerOutcome.SEND_BLOCKED:
        return _SEND_STATE_BLOCKED
    raise ValueError(f"unsupported send outcome: {outcome.value!r}")


def reserve_send_decision(
    db: Session,
    *,
    tenant_id: int,
    order_id: int,
    business_intent: Union[BusinessIntent, str],
    channel: str,
    source_event_id: Optional[str] = None,
    transition_version: Optional[str] = None,
    dispatch_decision: Optional[Mapping[str, Any]] = None,
    capabilities_snapshot: Optional[Mapping[str, Any]] = None,
    evidence_present: Optional[Sequence[str]] = None,
    template_service_key: Optional[str] = None,
    template_name: Optional[str] = None,
    commit: bool = False,
) -> ReserveSendResult:
    """
    Reserve an outbound send slot inside a SAVEPOINT.

    Commits before provider send when ``commit=True``. Duplicate rows in a
  non-resendable send state return ``duplicate=True`` and must not trigger a
    provider call. Shadow-only rows with the same idempotency key are promoted
    to ``send_reserved``.
    """
    from models import CommerceLifecycleNotificationLedger  # noqa: PLC0415

    tid, oid = _validate_tenant_order(tenant_id, order_id)
    intent_value = _validate_business_intent(business_intent)
    channel_value = _normalize_required_text(channel, field="channel").lower()
    if channel_value not in _ALLOWED_CHANNELS:
        raise ValueError(f"unsupported channel: {channel_value!r}")

    normalized_source_event_id = _normalize_optional_transition_field(
        source_event_id,
        field="source_event_id",
    )
    normalized_transition_version = _normalize_optional_transition_field(
        transition_version,
        field="transition_version",
    )

    idempotency_key = build_lifecycle_idempotency_key(
        tenant_id=tid,
        order_id=oid,
        business_intent=intent_value,
        channel=channel_value,
        source_event_id=normalized_source_event_id,
        transition_version=normalized_transition_version,
    )

    service_key_audit = None
    if template_service_key is not None:
        service_key_audit = _normalize_required_text(
            template_service_key,
            field="template_service_key",
        )
    template_name_audit = None
    if template_name is not None:
        template_name_audit = _normalize_required_text(template_name, field="template_name")

    row = CommerceLifecycleNotificationLedger(
        tenant_id=tid,
        order_id=oid,
        business_intent=intent_value,
        channel=channel_value,
        source_event_id=normalized_source_event_id,
        transition_version=normalized_transition_version,
        idempotency_key=idempotency_key,
        outcome=SendLedgerOutcome.SEND_RESERVED.value,
        send_state=_SEND_STATE_RESERVED,
        send_reserved_at=_utcnow(),
        send_attempt_count=0,
        reclaim_count=0,
        dispatch_decision_json=sanitize_dispatch_decision(dispatch_decision),
        capabilities_snapshot_json=sanitize_capabilities_snapshot(capabilities_snapshot),
        evidence_present_json=list(sanitize_evidence_present(evidence_present)),
        template_service_key=service_key_audit,
        template_name=template_name_audit,
    )

    try:
        with db.begin_nested():
            db.add(row)
            db.flush()
    except IntegrityError:
        db.expire_all()
        existing = (
            db.query(CommerceLifecycleNotificationLedger)
            .filter_by(tenant_id=tid, idempotency_key=idempotency_key)
            .one()
        )
        return _resolve_existing_send_reservation(
            db,
            existing,
            tenant_id=tid,
            idempotency_key=idempotency_key,
            dispatch_decision=dispatch_decision,
            capabilities_snapshot=capabilities_snapshot,
            evidence_present=evidence_present,
            service_key_audit=service_key_audit,
            template_name_audit=template_name_audit,
            commit=commit,
        )

    if commit:
        db.commit()
    return ReserveSendResult(
        ledger_id=int(row.id),
        idempotency_key=idempotency_key,
        duplicate=False,
        outcome=row.outcome,
        send_state=row.send_state,
    )


def mark_send_sending(
    db: Session,
    *,
    ledger_id: int,
    tenant_id: int,
    template_name: Optional[str] = None,
    template_service_key: Optional[str] = None,
    send_method: Optional[str] = None,
    commit: bool = False,
) -> MarkSendSendingResult:
    """Atomically transition a reserved row to sending before the provider call."""
    from models import CommerceLifecycleNotificationLedger  # noqa: PLC0415

    tid = int(tenant_id)
    if tid <= 0:
        raise ValueError("tenant_id must be positive")

    current = _utcnow()
    table = CommerceLifecycleNotificationLedger
    values: dict[Any, Any] = {
        table.outcome: SendLedgerOutcome.SEND_SENDING.value,
        table.send_state: _SEND_STATE_SENDING,
        table.send_attempted_at: current,
        table.send_attempt_count: table.send_attempt_count + 1,
    }
    if template_name is not None:
        values[table.template_name] = _normalize_required_text(
            template_name,
            field="template_name",
        )
    if template_service_key is not None:
        values[table.template_service_key] = _normalize_required_text(
            template_service_key,
            field="template_service_key",
        )
    method = normalize_send_method(send_method)
    if method is not None:
        values[table.send_method] = method

    updated = (
        db.query(table)
        .filter(
            table.id == int(ledger_id),
            table.tenant_id == tid,
            table.send_state == _SEND_STATE_RESERVED,
        )
        .update(values, synchronize_session=False)
    )
    if updated:
        db.flush()
        if commit:
            db.commit()

    row = (
        db.query(CommerceLifecycleNotificationLedger)
        .filter_by(id=int(ledger_id), tenant_id=tid)
        .one()
    )
    if not updated and commit:
        db.commit()

    return MarkSendSendingResult(
        ledger_id=int(row.id),
        outcome=row.outcome,
        send_state=row.send_state,
        provider_message_id=row.provider_message_id,
        send_error_code=row.send_error_code,
        transitioned=bool(updated),
    )


def finalize_send_outcome(
    db: Session,
    *,
    ledger_id: int,
    tenant_id: int,
    outcome: Union[SendLedgerOutcome, str],
    provider_message_id: Optional[str] = None,
    send_error_code: Optional[str] = None,
    template_name: Optional[str] = None,
    send_method: Optional[str] = None,
    commit: bool = False,
) -> FinalizeSendResult:
    """Finalize sent / failed / ambiguous — never auto-resend ambiguous rows."""
    from models import CommerceLifecycleNotificationLedger  # noqa: PLC0415

    tid = int(tenant_id)
    if tid <= 0:
        raise ValueError("tenant_id must be positive")

    outcome_value = outcome.value if isinstance(outcome, SendLedgerOutcome) else str(outcome)
    try:
        parsed_outcome = SendLedgerOutcome(outcome_value)
    except ValueError as exc:
        raise ValueError(f"invalid send ledger outcome: {outcome_value!r}") from exc

    if parsed_outcome not in {
        SendLedgerOutcome.SENT,
        SendLedgerOutcome.FAILED,
        SendLedgerOutcome.AMBIGUOUS,
        SendLedgerOutcome.SEND_SKIPPED,
        SendLedgerOutcome.SEND_BLOCKED,
    }:
        raise ValueError(f"outcome {outcome_value!r} is not a terminal send outcome")

    row = (
        db.query(CommerceLifecycleNotificationLedger)
        .filter_by(id=int(ledger_id), tenant_id=tid)
        .one()
    )

    if row.send_state in {_SEND_STATE_SENT, _SEND_STATE_AMBIGUOUS}:
        return FinalizeSendResult(
            ledger_id=int(row.id),
            outcome=row.outcome,
            send_state=row.send_state,
            provider_message_id=row.provider_message_id,
            send_error_code=row.send_error_code,
        )

    row.outcome = parsed_outcome.value
    row.send_state = _send_state_for_outcome(parsed_outcome)
    row.send_completed_at = _utcnow()
    if template_name is not None:
        row.template_name = _normalize_required_text(template_name, field="template_name")
    method = normalize_send_method(send_method)
    if method is not None:
        row.send_method = method
    if provider_message_id is not None:
        wamid = str(provider_message_id).strip()
        row.provider_message_id = wamid or None
    if send_error_code is not None:
        code = str(send_error_code).strip()
        if len(code) > 64:
            raise ValueError("send_error_code exceeds maximum length")
        row.send_error_code = code or None

    db.flush()
    if commit:
        db.commit()

    return FinalizeSendResult(
        ledger_id=int(row.id),
        outcome=row.outcome,
        send_state=row.send_state,
        provider_message_id=row.provider_message_id,
        send_error_code=row.send_error_code,
    )


def finalize_send_dispatch_error(
    db: Session,
    *,
    ledger_id: int,
    tenant_id: int,
    send_error_code: str = "dispatch_error",
    commit: bool = False,
) -> FinalizeSendResult:
    """
    Persist a recoverable failure for a reserved/sending row after dispatch crash.

    Leaves stale reserved/sending rows reclaimable; terminalizes other states as failed.
    """
    from models import CommerceLifecycleNotificationLedger  # noqa: PLC0415

    tid = int(tenant_id)
    row = (
        db.query(CommerceLifecycleNotificationLedger)
        .filter_by(id=int(ledger_id), tenant_id=tid)
        .one()
    )
    state = str(row.send_state or "").strip()
    if state in {_SEND_STATE_RESERVED, _SEND_STATE_SENDING}:
        row.send_error_code = str(send_error_code).strip() or "dispatch_error"
        db.flush()
        if commit:
            db.commit()
        return FinalizeSendResult(
            ledger_id=int(row.id),
            outcome=row.outcome,
            send_state=row.send_state,
            provider_message_id=row.provider_message_id,
            send_error_code=row.send_error_code,
        )
    return finalize_send_outcome(
        db,
        ledger_id=ledger_id,
        tenant_id=tid,
        outcome=SendLedgerOutcome.FAILED,
        send_error_code=send_error_code,
        commit=commit,
    )


__all__ = [
    "FinalizeSendResult",
    "MarkSendSendingResult",
    "MarkShadowOutcomeResult",
    "ReserveSendResult",
    "ReserveShadowResult",
    "SendLedgerOutcome",
    "ShadowLedgerOutcome",
    "build_idempotency_key_from_fields",
    "build_lifecycle_idempotency_key",
    "finalize_send_dispatch_error",
    "finalize_send_outcome",
    "lifecycle_send_stale_seconds",
    "mark_send_sending",
    "mark_shadow_outcome",
    "reserve_send_decision",
    "reserve_shadow_decision",
    "sanitize_capabilities_snapshot",
    "normalize_send_method",
    "sanitize_dispatch_decision",
    "sanitize_evidence_present",
    "try_conditional_promote_shadow_send_row",
    "try_conditional_reclaim_send_row",
    "try_conditional_reevaluate_blocked_send_row",
    "try_conditional_retry_failed_send_row",
]

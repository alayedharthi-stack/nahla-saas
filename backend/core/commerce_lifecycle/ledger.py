"""
Shadow-only lifecycle notification ledger (PR 2B).

Owns idempotency reservation and structured audit metadata for future
lifecycle dispatch. Does not send messages, select templates, or call AI.

Transaction ownership: callers pass an open SQLAlchemy ``Session`` and own
``commit()`` / ``rollback()``. Reservation uses a SAVEPOINT via
``session.begin_nested()`` so duplicate-key conflicts never roll back
unrelated caller work in the outer transaction.
"""
from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass
from enum import Enum
from typing import Any, FrozenSet, Mapping, Optional, Sequence, Tuple, Union

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


__all__ = [
    "MarkShadowOutcomeResult",
    "ReserveShadowResult",
    "ShadowLedgerOutcome",
    "build_idempotency_key_from_fields",
    "build_lifecycle_idempotency_key",
    "mark_shadow_outcome",
    "reserve_shadow_decision",
    "sanitize_capabilities_snapshot",
    "sanitize_dispatch_decision",
    "sanitize_evidence_present",
]

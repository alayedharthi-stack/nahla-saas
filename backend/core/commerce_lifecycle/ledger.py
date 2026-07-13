"""
Shadow-only lifecycle notification ledger (PR 2B).

Owns idempotency reservation and structured audit metadata for future
lifecycle dispatch. Does not send messages, select templates, or call AI.

Transaction ownership: callers pass an open SQLAlchemy ``Session`` and are
responsible for ``commit()`` / ``rollback()`` unless ``commit=True`` is
passed explicitly to a helper.
"""
from __future__ import annotations

import hashlib
import os
import re
from dataclasses import dataclass
from enum import Enum
from typing import Any, Dict, FrozenSet, Mapping, Optional, Sequence, Tuple, Union

from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from core.commerce_lifecycle.intents import BusinessIntent

COMMERCE_LIFECYCLE_SHADOW_LEDGER_ENABLED_ENV = "COMMERCE_LIFECYCLE_SHADOW_LEDGER_ENABLED"

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
})

_SENSITIVE_URL_KEY_SUFFIXES = ("_url", "_link")


class ShadowLedgerOutcome(str, Enum):
    """Ledger-only outcomes — no customer send states in PR 2B."""

    SHADOW_RESERVED = "shadow_reserved"
    SHADOW_ELIGIBLE = "shadow_eligible"
    SHADOW_BLOCKED = "shadow_blocked"
    SHADOW_DUPLICATE = "shadow_duplicate"
    SHADOW_NO_NOTIFICATION = "shadow_no_notification"
    SHADOW_MERCHANT_ACTION = "shadow_merchant_action"
    SHADOW_ERROR = "shadow_error"


_MARKABLE_OUTCOMES: FrozenSet[ShadowLedgerOutcome] = frozenset({
    ShadowLedgerOutcome.SHADOW_ELIGIBLE,
    ShadowLedgerOutcome.SHADOW_BLOCKED,
    ShadowLedgerOutcome.SHADOW_DUPLICATE,
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


def is_shadow_ledger_enabled() -> bool:
    """Feature flag — default false; not wired to runtime producers in PR 2B."""
    return os.getenv(COMMERCE_LIFECYCLE_SHADOW_LEDGER_ENABLED_ENV, "false").lower() in {
        "1",
        "true",
        "yes",
    }


def build_lifecycle_idempotency_key(
    *,
    tenant_id: int,
    order_id: int,
    business_intent: Union[BusinessIntent, str],
    channel: str,
    source_event_id: Optional[str] = None,
    transition_version: Optional[str] = None,
) -> str:
    """Canonical idempotency key aligned with PR 2A field names."""
    intent_value = (
        business_intent.value
        if isinstance(business_intent, BusinessIntent)
        else str(business_intent)
    )
    parts = (
        str(int(tenant_id)),
        str(int(order_id)),
        intent_value,
        str(channel or "").strip().lower(),
        str(source_event_id or "").strip(),
        str(transition_version or "").strip(),
    )
    return ":".join(parts)


def build_idempotency_key_from_fields(
    key_fields: Sequence[str],
    context: Mapping[str, Any],
) -> str:
    """Build a key using declarative ``idempotency_key_fields`` from PR 2A."""
    if not key_fields:
        raise ValueError("idempotency_key_fields must not be empty")
    parts = []
    for field in key_fields:
        name = str(field).strip()
        if not name:
            raise ValueError("empty idempotency key field name")
        parts.append(str(context.get(name, "")).strip())
    return ":".join(parts)


def hash_destination_reference(destination: str) -> str:
    """Minimized destination reference — SHA-256 hex, no reversible phone storage."""
    normalized = str(destination or "").strip()
    if not normalized:
        raise ValueError("destination reference must not be empty")
    return hashlib.sha256(normalized.encode("utf-8")).hexdigest()


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


def _reject_forbidden_keys(payload: Mapping[str, Any], *, label: str) -> None:
    for key in payload:
        lowered = str(key).lower()
        if lowered in _FORBIDDEN_PAYLOAD_KEYS:
            raise ValueError(f"{label} must not contain {key!r}")
        if any(lowered.endswith(suffix) for suffix in _SENSITIVE_URL_KEY_SUFFIXES):
            raise ValueError(f"{label} must not contain URL-like key {key!r}")


def sanitize_capabilities_snapshot(
    capabilities: Optional[Mapping[str, Any]],
) -> Dict[str, bool]:
    if not capabilities:
        return {}
    out: Dict[str, bool] = {}
    for key, value in capabilities.items():
        name = str(key).strip()
        if not name:
            raise ValueError("capability snapshot keys must not be empty")
        if not isinstance(value, bool):
            raise ValueError(f"capability {name!r} must be boolean")
        out[name] = value
    return out


def sanitize_evidence_present(fields: Optional[Sequence[str]]) -> Tuple[str, ...]:
    if not fields:
        return ()
    seen = []
    for item in fields:
        name = str(item).strip()
        if not name:
            raise ValueError("evidence field names must not be empty")
        if name not in seen:
            seen.append(name)
    return tuple(seen)


def sanitize_dispatch_decision(payload: Optional[Mapping[str, Any]]) -> Dict[str, Any]:
    if not payload:
        return {}
    _reject_forbidden_keys(payload, label="dispatch_decision_json")
    return dict(payload)


def reserve_shadow_decision(
    db: Session,
    *,
    tenant_id: int,
    order_id: int,
    business_intent: Union[BusinessIntent, str],
    channel: str,
    source_event_id: Optional[str] = None,
    transition_version: Optional[str] = None,
    destination_hash: Optional[str] = None,
    dispatch_decision: Optional[Mapping[str, Any]] = None,
    capabilities_snapshot: Optional[Mapping[str, Any]] = None,
    evidence_present: Optional[Sequence[str]] = None,
    automation_execution_id: Optional[int] = None,
    commit: bool = False,
) -> ReserveShadowResult:
    """
    Atomically reserve a shadow ledger row. Duplicate keys return the existing row.
    Does not send messages or mutate orders/conversations.
    """
    from models import CommerceLifecycleNotificationLedger  # noqa: PLC0415

    tid, oid = _validate_tenant_order(tenant_id, order_id)
    intent_value = _validate_business_intent(business_intent)
    channel_value = str(channel or "").strip().lower()
    if not channel_value:
        raise ValueError("channel must not be empty")

    idempotency_key = build_lifecycle_idempotency_key(
        tenant_id=tid,
        order_id=oid,
        business_intent=intent_value,
        channel=channel_value,
        source_event_id=source_event_id,
        transition_version=transition_version,
    )

    row = CommerceLifecycleNotificationLedger(
        tenant_id=tid,
        order_id=oid,
        business_intent=intent_value,
        channel=channel_value,
        destination_hash=str(destination_hash).strip() if destination_hash else None,
        source_event_id=str(source_event_id).strip() if source_event_id else None,
        transition_version=str(transition_version).strip() if transition_version else None,
        idempotency_key=idempotency_key,
        outcome=ShadowLedgerOutcome.SHADOW_RESERVED.value,
        dispatch_decision_json=sanitize_dispatch_decision(dispatch_decision),
        capabilities_snapshot_json=sanitize_capabilities_snapshot(capabilities_snapshot),
        evidence_present_json=list(sanitize_evidence_present(evidence_present)),
        automation_execution_id=automation_execution_id,
    )

    try:
        db.add(row)
        db.flush()
        if commit:
            db.commit()
        return ReserveShadowResult(
            ledger_id=int(row.id),
            idempotency_key=idempotency_key,
            duplicate=False,
            outcome=row.outcome,
        )
    except IntegrityError:
        db.rollback()
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
    try:
        parsed_outcome = ShadowLedgerOutcome(outcome_value)
    except ValueError as exc:
        raise ValueError(f"invalid shadow ledger outcome: {outcome_value!r}") from exc

    if parsed_outcome not in _MARKABLE_OUTCOMES:
        raise ValueError(f"outcome {outcome_value!r} is not markable")

    if reason_code is not None and not re.fullmatch(r"[a-z][a-z0-9_]*", str(reason_code)):
        raise ValueError("reason_code must be machine-readable snake_case")

    row = (
        db.query(CommerceLifecycleNotificationLedger)
        .filter_by(id=int(ledger_id), tenant_id=tid)
        .one()
    )

    if row.outcome != ShadowLedgerOutcome.SHADOW_RESERVED.value:
        raise ValueError(
            f"ledger {ledger_id} outcome transition not allowed from {row.outcome!r}"
        )

    row.outcome = parsed_outcome.value
    row.reason_code = str(reason_code).strip() if reason_code else None
    db.flush()
    if commit:
        db.commit()

    return MarkShadowOutcomeResult(
        ledger_id=int(row.id),
        outcome=row.outcome,
        reason_code=row.reason_code,
    )


__all__ = [
    "COMMERCE_LIFECYCLE_SHADOW_LEDGER_ENABLED_ENV",
    "MarkShadowOutcomeResult",
    "ReserveShadowResult",
    "ShadowLedgerOutcome",
    "build_idempotency_key_from_fields",
    "build_lifecycle_idempotency_key",
    "hash_destination_reference",
    "is_shadow_ledger_enabled",
    "mark_shadow_outcome",
    "reserve_shadow_decision",
    "sanitize_capabilities_snapshot",
    "sanitize_dispatch_decision",
    "sanitize_evidence_present",
]

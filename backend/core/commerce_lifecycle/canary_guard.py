"""
Fail-closed Tenant canary isolation for order-lifecycle WhatsApp sends.

Used by the new lifecycle dispatcher and every legacy sender that can
still emit order / COD / unpaid / abandoned-cart WhatsApp. Other tenants
and unrelated automations (winback, campaigns) stay unchanged.

No customer-facing prose. No model calls.
"""
from __future__ import annotations

import hashlib
import logging
import os
from dataclasses import dataclass
from typing import Optional

logger = logging.getLogger("nahla.commerce_lifecycle.canary_guard")

_ENV_DISPATCH_ENABLED = "COMMERCE_LIFECYCLE_DISPATCH_ENABLED"
_ENV_DISPATCH_TENANT_ALLOWLIST = "COMMERCE_LIFECYCLE_DISPATCH_TENANT_ALLOWLIST"
_ENV_DISPATCH_RECIPIENT_ALLOWLIST = "COMMERCE_LIFECYCLE_DISPATCH_RECIPIENT_ALLOWLIST"

MODE_NEW_LIFECYCLE = "new_lifecycle"
MODE_LEGACY_LIFECYCLE = "legacy_lifecycle"

LIFECYCLE_CANARY_AUTOMATION_TYPES = frozenset({
    "order_notifications",
    "cod_confirmation",
    "unpaid_order_reminder",
    "abandoned_cart",
    "abandoned_order_draft",
})

LIFECYCLE_OWNED_AUTOMATION_TYPES = frozenset({"order_notifications"})

REASON_PERMITTED = "permitted"
REASON_DISPATCH_DISABLED = "dispatch_disabled"
REASON_TENANT_NOT_ALLOWLISTED = "tenant_not_allowlisted"
REASON_RECIPIENT_NOT_ALLOWLISTED = "recipient_not_allowlisted"
REASON_RECIPIENT_MISSING = "recipient_missing"
REASON_RECIPIENT_UNNORMALIZABLE = "recipient_unnormalizable"
REASON_LIFECYCLE_OWNS_EVENT = "lifecycle_dispatch_owns_event"
REASON_LEGACY_NOT_IN_SCOPE = "legacy_not_in_scope"

# Audit is intentionally narrow: only canary-tenant send blocks that
# involve a recipient or lifecycle-ownership skip. Tenant-not-allowlisted
# and dispatch-disabled skips are not audited as blocked customer sends.
_AUDIT_REASONS = frozenset({
    REASON_RECIPIENT_NOT_ALLOWLISTED,
    REASON_RECIPIENT_MISSING,
    REASON_RECIPIENT_UNNORMALIZABLE,
    REASON_LIFECYCLE_OWNS_EVENT,
})
_PHONE_FINGERPRINT_PREFIX = "nahla.lifecycle_canary.v1:"


@dataclass(frozen=True)
class CanaryDecision:
    allowed: bool
    reason: str
    tenant_id: int
    sender_path: str
    mode: str
    automation_type: Optional[str] = None
    phone_normalized: Optional[str] = None


def commerce_lifecycle_dispatch_enabled() -> bool:
    val = str(os.environ.get(_ENV_DISPATCH_ENABLED, "false")).strip().lower()
    return val in {"1", "true", "yes", "on"}


def _parse_dispatch_tenant_allowlist() -> frozenset[int]:
    raw = str(os.environ.get(_ENV_DISPATCH_TENANT_ALLOWLIST, "")).strip()
    if not raw:
        return frozenset()
    allowed: set[int] = set()
    for part in raw.split(","):
        piece = part.strip()
        if not piece:
            continue
        try:
            tenant_id = int(piece)
        except ValueError:
            continue
        if tenant_id > 0:
            allowed.add(tenant_id)
    return frozenset(allowed)


def _parse_dispatch_recipient_allowlist() -> frozenset[str]:
    from services.customer_intelligence import normalize_phone  # noqa: PLC0415

    raw = str(os.environ.get(_ENV_DISPATCH_RECIPIENT_ALLOWLIST, "")).strip()
    if not raw:
        return frozenset()
    allowed: set[str] = set()
    for part in raw.split(","):
        piece = part.strip()
        if not piece:
            continue
        normalized = normalize_phone(piece) or piece
        allowed.add(normalized)
    return frozenset(allowed)


def commerce_lifecycle_dispatch_tenant_allowlist() -> frozenset[int]:
    return _parse_dispatch_tenant_allowlist()


def commerce_lifecycle_dispatch_recipient_allowlist() -> frozenset[str]:
    return _parse_dispatch_recipient_allowlist()


def commerce_lifecycle_dispatch_tenant_permitted(tenant_id: int) -> bool:
    if not commerce_lifecycle_dispatch_enabled():
        return False
    allowlist = commerce_lifecycle_dispatch_tenant_allowlist()
    if not allowlist:
        return False
    return int(tenant_id) in allowlist


def commerce_lifecycle_dispatch_recipient_permitted(phone: str) -> bool:
    """True only when dispatch is on, allowlist is non-empty, and phone normalizes into it."""
    if not commerce_lifecycle_dispatch_enabled():
        return False
    allowlist = commerce_lifecycle_dispatch_recipient_allowlist()
    if not allowlist:
        return False
    from services.customer_intelligence import normalize_phone  # noqa: PLC0415

    normalized = normalize_phone(str(phone or "").strip())
    if not normalized:
        return False
    return normalized in allowlist


def _phone_audit_fields(normalized: Optional[str]) -> dict[str, str]:
    """Correlation fields that must never include the full customer phone."""
    if not normalized:
        return {"phone_last4": "", "phone_fingerprint": ""}
    digits = "".join(ch for ch in str(normalized) if ch.isdigit())
    last4 = digits[-4:] if digits else ""
    digest = hashlib.sha256(
        f"{_PHONE_FINGERPRINT_PREFIX}{normalized}".encode("utf-8")
    ).hexdigest()
    return {"phone_last4": last4, "phone_fingerprint": digest}


def _normalize_candidate_phone(phone: str) -> tuple[str, Optional[str], Optional[str]]:
    """Return (raw, normalized_or_none, fail_reason_or_none)."""
    from services.customer_intelligence import normalize_phone  # noqa: PLC0415

    raw = str(phone or "").strip()
    if not raw:
        return raw, None, REASON_RECIPIENT_MISSING
    normalized = normalize_phone(raw)
    if not normalized:
        return raw, None, REASON_RECIPIENT_UNNORMALIZABLE
    return raw, normalized, None


def evaluate_lifecycle_canary_send(
    tenant_id: int,
    *,
    phone: str,
    sender_path: str,
    mode: str,
    automation_type: Optional[str] = None,
) -> CanaryDecision:
    """Deterministic fail-closed canary decision. Never sends. Never calls a model."""
    tid = int(tenant_id)
    path = str(sender_path or "").strip() or "unknown"
    atype = str(automation_type or "").strip() or None
    raw, normalized, phone_fail = _normalize_candidate_phone(phone)

    if mode == MODE_LEGACY_LIFECYCLE:
        if not commerce_lifecycle_dispatch_tenant_permitted(tid):
            return CanaryDecision(
                allowed=True,
                reason=REASON_LEGACY_NOT_IN_SCOPE,
                tenant_id=tid,
                sender_path=path,
                mode=mode,
                automation_type=atype,
                phone_normalized=normalized,
            )
        if atype not in LIFECYCLE_CANARY_AUTOMATION_TYPES:
            return CanaryDecision(
                allowed=True,
                reason=REASON_LEGACY_NOT_IN_SCOPE,
                tenant_id=tid,
                sender_path=path,
                mode=mode,
                automation_type=atype,
                phone_normalized=normalized,
            )
        if atype in LIFECYCLE_OWNED_AUTOMATION_TYPES:
            return CanaryDecision(
                allowed=False,
                reason=REASON_LIFECYCLE_OWNS_EVENT,
                tenant_id=tid,
                sender_path=path,
                mode=mode,
                automation_type=atype,
                phone_normalized=normalized,
            )
        if phone_fail:
            return CanaryDecision(
                allowed=False,
                reason=phone_fail,
                tenant_id=tid,
                sender_path=path,
                mode=mode,
                automation_type=atype,
                phone_normalized=None,
            )
        if not commerce_lifecycle_dispatch_recipient_permitted(raw):
            return CanaryDecision(
                allowed=False,
                reason=REASON_RECIPIENT_NOT_ALLOWLISTED,
                tenant_id=tid,
                sender_path=path,
                mode=mode,
                automation_type=atype,
                phone_normalized=normalized,
            )
        return CanaryDecision(
            allowed=True,
            reason=REASON_PERMITTED,
            tenant_id=tid,
            sender_path=path,
            mode=mode,
            automation_type=atype,
            phone_normalized=normalized,
        )

    # MODE_NEW_LIFECYCLE — the new dispatcher and its last-mile senders.
    if not commerce_lifecycle_dispatch_enabled():
        return CanaryDecision(
            allowed=False,
            reason=REASON_DISPATCH_DISABLED,
            tenant_id=tid,
            sender_path=path,
            mode=mode,
            automation_type=atype,
            phone_normalized=normalized,
        )
    if not commerce_lifecycle_dispatch_tenant_permitted(tid):
        return CanaryDecision(
            allowed=False,
            reason=REASON_TENANT_NOT_ALLOWLISTED,
            tenant_id=tid,
            sender_path=path,
            mode=mode,
            automation_type=atype,
            phone_normalized=normalized,
        )
    if phone_fail:
        return CanaryDecision(
            allowed=False,
            reason=phone_fail,
            tenant_id=tid,
            sender_path=path,
            mode=mode,
            automation_type=atype,
            phone_normalized=None,
        )
    if not commerce_lifecycle_dispatch_recipient_permitted(raw):
        return CanaryDecision(
            allowed=False,
            reason=REASON_RECIPIENT_NOT_ALLOWLISTED,
            tenant_id=tid,
            sender_path=path,
            mode=mode,
            automation_type=atype,
            phone_normalized=normalized,
        )
    return CanaryDecision(
        allowed=True,
        reason=REASON_PERMITTED,
        tenant_id=tid,
        sender_path=path,
        mode=mode,
        automation_type=atype,
        phone_normalized=normalized,
    )


def evaluate_and_audit(
    tenant_id: int,
    *,
    phone: str,
    sender_path: str,
    mode: str,
    automation_type: Optional[str] = None,
) -> CanaryDecision:
    decision = evaluate_lifecycle_canary_send(
        tenant_id,
        phone=phone,
        sender_path=sender_path,
        mode=mode,
        automation_type=automation_type,
    )
    if decision.allowed or decision.reason not in _AUDIT_REASONS:
        return decision
    try:
        from core.audit import audit  # noqa: PLC0415

        audit(
            "lifecycle_canary_blocked",
            tenant_id=decision.tenant_id,
            sender_path=decision.sender_path,
            mode=decision.mode,
            automation_type=decision.automation_type or "",
            reason=decision.reason,
            **_phone_audit_fields(decision.phone_normalized),
        )
    except Exception:
        logger.warning(
            "[CanaryGuard] audit failed tenant=%s path=%s reason=%s",
            decision.tenant_id,
            decision.sender_path,
            decision.reason,
        )
    logger.info(
        "[CanaryGuard] blocked tenant=%s path=%s type=%s reason=%s",
        decision.tenant_id,
        decision.sender_path,
        decision.automation_type,
        decision.reason,
    )
    return decision


__all__ = [
    "CanaryDecision",
    "LIFECYCLE_CANARY_AUTOMATION_TYPES",
    "LIFECYCLE_OWNED_AUTOMATION_TYPES",
    "MODE_LEGACY_LIFECYCLE",
    "MODE_NEW_LIFECYCLE",
    "commerce_lifecycle_dispatch_enabled",
    "commerce_lifecycle_dispatch_recipient_allowlist",
    "commerce_lifecycle_dispatch_recipient_permitted",
    "commerce_lifecycle_dispatch_tenant_allowlist",
    "commerce_lifecycle_dispatch_tenant_permitted",
    "evaluate_and_audit",
    "evaluate_lifecycle_canary_send",
]

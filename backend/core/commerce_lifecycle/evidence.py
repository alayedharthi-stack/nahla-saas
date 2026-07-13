"""
Per-order evidence model and validation — truth before capability or delivery.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, fields
from datetime import datetime
from typing import Any, FrozenSet, Optional, Tuple
from urllib.parse import urlparse

from core.commerce_lifecycle.definitions import (
    BusinessIntentDefinition,
    KNOWN_CAPABILITY_FIELDS,
    KNOWN_EVIDENCE_FIELDS,
)
from core.merchant_capabilities import MerchantCapabilities

_PLACEHOLDER_HOST_SUFFIXES = (
    ".example",
    ".example.com",
    ".invalid",
    ".localhost",
    ".test",
)
_BLOCKED_HOSTS = frozenset({
    "localhost",
    "127.0.0.1",
    "0.0.0.0",
    "example.com",
    "www.example.com",
    "example.org",
    "test",
    "invalid",
})
_URL_FIELD_NAMES = frozenset({
    "checkout_url",
    "payment_url",
    "tracking_url",
    "review_url",
})


@dataclass(frozen=True)
class OrderLifecycleEvidence:
    """Per-order facts — payment_url and checkout_url are distinct fields."""

    order_number: Optional[str] = None
    checkout_url: Optional[str] = None
    payment_url: Optional[str] = None
    tracking_url: Optional[str] = None
    tracking_number: Optional[str] = None
    carrier: Optional[str] = None
    delivered_at: Optional[datetime] = None
    payment_method: Optional[str] = None
    review_url: Optional[str] = None
    coupon_code: Optional[str] = None
    customer_phone: Optional[str] = None
    customer_name: Optional[str] = None
    status: Optional[str] = None
    source_event_id: Optional[str] = None
    transition_version: Optional[str] = None
    missing_fields: Tuple[str, ...] = ()


@dataclass(frozen=True)
class EvidenceValidationResult:
    valid: bool
    missing_fields: Tuple[str, ...] = ()
    invalid_fields: Tuple[str, ...] = ()
    present_fields: Tuple[str, ...] = ()


@dataclass(frozen=True)
class CapabilityValidationResult:
    valid: bool
    missing_capabilities: Tuple[str, ...] = ()
    forbidden_capabilities: Tuple[str, ...] = ()


def is_valid_https_evidence_url(url: Optional[str]) -> bool:
    """True for customer-deliverable HTTPS URLs — rejects placeholders and test hosts."""
    raw = str(url or "").strip()
    if not raw:
        return False
    if "{{" in raw or "}}" in raw:
        return False
    try:
        parsed = urlparse(raw)
    except Exception:
        return False
    if parsed.scheme != "https":
        return False
    if not parsed.netloc:
        return False
    host = parsed.netloc.lower().split(":")[0]
    if host in _BLOCKED_HOSTS:
        return False
    if any(host == suffix.lstrip(".") or host.endswith(suffix) for suffix in _PLACEHOLDER_HOST_SUFFIXES):
        return False
    if re.search(r"/\s*$", raw):
        return False
    return True


def _non_empty_text(value: Optional[str]) -> bool:
    return bool(str(value or "").strip())


def _field_value(evidence: OrderLifecycleEvidence, name: str) -> Any:
    if name not in KNOWN_EVIDENCE_FIELDS:
        return None
    return getattr(evidence, name)


def _field_is_valid(name: str, value: Any) -> bool:
    if name in _URL_FIELD_NAMES:
        return is_valid_https_evidence_url(value if isinstance(value, str) else None)
    if name == "delivered_at":
        return value is not None
    if name == "missing_fields":
        return isinstance(value, tuple) and len(value) > 0
    if name == "order_number":
        return _non_empty_text(value if isinstance(value, str) else None)
    if name == "tracking_number":
        return _non_empty_text(value if isinstance(value, str) else None)
    if name == "carrier":
        return _non_empty_text(value if isinstance(value, str) else None)
    if name == "coupon_code":
        return _non_empty_text(value if isinstance(value, str) else None)
    if name == "payment_method":
        return _non_empty_text(value if isinstance(value, str) else None)
    if value is None:
        return False
    if isinstance(value, str):
        return _non_empty_text(value)
    return True


def _present_field_names(evidence: OrderLifecycleEvidence) -> Tuple[str, ...]:
    present = []
    for f in fields(evidence):
        val = getattr(evidence, f.name)
        if f.name == "missing_fields":
            if val:
                present.append(f.name)
            continue
        if val is None:
            continue
        if isinstance(val, str) and not val.strip():
            continue
        present.append(f.name)
    return tuple(present)


def validate_evidence(
    definition: BusinessIntentDefinition,
    evidence: OrderLifecycleEvidence,
) -> EvidenceValidationResult:
    """Return field-level missing/invalid keys — never secret values."""
    missing: list[str] = []
    invalid: list[str] = []

    for name in definition.required_evidence:
        value = _field_value(evidence, name)
        if not _field_is_valid(name, value):
            if value is None or (isinstance(value, str) and not value.strip()):
                missing.append(name)
            else:
                invalid.append(name)

    if definition.required_evidence_groups:
        group_satisfied = False
        for group in definition.required_evidence_groups:
            group_missing = []
            group_invalid = []
            for name in group:
                value = _field_value(evidence, name)
                if not _field_is_valid(name, value):
                    if value is None or (isinstance(value, str) and not value.strip()):
                        group_missing.append(name)
                    else:
                        group_invalid.append(name)
            if not group_missing and not group_invalid:
                group_satisfied = True
                break
        if not group_satisfied:
            # Report the first group's deficits for actionable diagnostics.
            first_group = definition.required_evidence_groups[0]
            for name in first_group:
                value = _field_value(evidence, name)
                if not _field_is_valid(name, value):
                    if value is None or (isinstance(value, str) and not value.strip()):
                        if name not in missing:
                            missing.append(name)
                    elif name not in invalid:
                        invalid.append(name)

    for name in definition.optional_evidence:
        value = _field_value(evidence, name)
        if value is None or (isinstance(value, str) and not value.strip()):
            continue
        if not _field_is_valid(name, value) and name in _URL_FIELD_NAMES:
            invalid.append(name)

    return EvidenceValidationResult(
        valid=not missing and not invalid,
        missing_fields=tuple(missing),
        invalid_fields=tuple(invalid),
        present_fields=_present_field_names(evidence),
    )


def validate_capabilities(
    definition: BusinessIntentDefinition,
    merchant_capabilities: MerchantCapabilities,
) -> CapabilityValidationResult:
    """Pure capability gate — never substitutes for per-order evidence."""
    missing: list[str] = []
    forbidden: list[str] = []

    for name in definition.required_capabilities:
        if name not in KNOWN_CAPABILITY_FIELDS:
            missing.append(name)
            continue
        if not bool(getattr(merchant_capabilities, name, False)):
            missing.append(name)

    for name in definition.forbidden_capabilities:
        if name not in KNOWN_CAPABILITY_FIELDS:
            forbidden.append(name)
            continue
        if bool(getattr(merchant_capabilities, name, False)):
            forbidden.append(name)

    return CapabilityValidationResult(
        valid=not missing and not forbidden,
        missing_capabilities=tuple(missing),
        forbidden_capabilities=tuple(forbidden),
    )


__all__ = [
    "CapabilityValidationResult",
    "EvidenceValidationResult",
    "OrderLifecycleEvidence",
    "is_valid_https_evidence_url",
    "validate_capabilities",
    "validate_evidence",
]

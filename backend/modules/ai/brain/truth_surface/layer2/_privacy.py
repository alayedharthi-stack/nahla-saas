"""Closed-vocabulary privacy validation for Layer 2 shadow contracts."""
from __future__ import annotations

import re
from typing import Any, Dict, FrozenSet, Iterable, Mapping, Tuple

ALLOWED_ENTITY_KINDS: FrozenSet[str] = frozenset({
    "coupon_code",
    "discount_code",
    "applied_coupon_code",
    "promotion_id",
    "product_id",
    "order_ref",
    "draft_order_id",
})

KNOWN_TRIGGER_IDS: FrozenSet[str] = frozenset({
    "always_base",
    "coupon_intent",
    "discount_intent",
    "cart_discount",
    "offer_intent",
    "promotion_intent",
    "order_ref",
    "order_status",
    "checkout_active",
    "payment_query",
    "receipt",
    "tracking_query",
    "shipping_query",
    "product_query",
    "catalog_browse",
    "price_query",
})

ALLOWED_REASON_CODES: FrozenSet[str] = frozenset({
    "snapshot_missing",
    "missing_required_domains",
    "eligible_empty_ok",
    "facts_available",
})

ALLOWED_SAFETY_FLAGS: FrozenSet[str] = frozenset({
    "shadow_only",
    "no_enforcement",
})

ALLOWED_CONSTRAINTS: FrozenSet[str] = frozenset()

REGISTERED_DOMAIN_IDS: FrozenSet[str] = frozenset({
    "customer",
    "order",
    "payment",
    "shipment",
    "catalog",
    "capabilities",
    "merchant_policy",
    "coupons",
    "promotions",
})

_ENTITY_KEY = "entity_kind"
_FORBIDDEN_ENTITY_KEYS: FrozenSet[str] = frozenset({
    "value",
    "phone",
    "code",
    "text",
    "raw",
    "metadata",
})

_SOURCE_TURN_REF_RE = re.compile(r"^[a-zA-Z0-9][a-zA-Z0-9_-]{0,63}$")
_PHONE_LIKE_RE = re.compile(r"\d{8,}")
_COUPON_LIKE_RE = re.compile(r"^[A-Z0-9]{4,}$")
_ARABIC_RE = re.compile(r"[\u0600-\u06FF]")
_WHITESPACE_RE = re.compile(r"\s")
_EVIDENCE_REF_RE = re.compile(r"^trigger:[a-z0-9_]+$")
_DOMAIN_FACT_RE = re.compile(r"^domain:[a-z_]+$")
_SNAPSHOT_REF_RE = re.compile(r"^[a-zA-Z0-9][a-zA-Z0-9_-]{0,127}$")
_UUID_HEX_REF_RE = re.compile(r"^[a-f0-9]{32}$")


def _is_uuid_hex_snapshot_ref(value: str) -> bool:
    """TrustedContextSnapshot.snapshot_id uses uuid.uuid4().hex (32 lowercase hex)."""
    return _UUID_HEX_REF_RE.fullmatch(value) is not None


def _reject_sensitive_text(value: str, *, field_name: str) -> None:
    if _ARABIC_RE.search(value):
        raise ValueError(f"{field_name} must not contain customer text")
    if _WHITESPACE_RE.search(value):
        raise ValueError(f"{field_name} must not contain whitespace text")
    if _PHONE_LIKE_RE.search(value):
        raise ValueError(f"{field_name} must not contain phone-like values")
    if _COUPON_LIKE_RE.fullmatch(value.strip()):
        raise ValueError(f"{field_name} must not contain coupon-like values")


def validate_entity(entity: Mapping[str, Any]) -> Dict[str, str]:
    keys = frozenset(entity.keys())
    if keys != {_ENTITY_KEY}:
        extra = keys - {_ENTITY_KEY}
        forbidden = extra & _FORBIDDEN_ENTITY_KEYS
        if forbidden:
            raise ValueError(f"entity contains forbidden keys: {sorted(forbidden)}")
        raise ValueError("entity must contain exactly one key: entity_kind")
    kind = entity[_ENTITY_KEY]
    if not isinstance(kind, str):
        raise ValueError("entity_kind must be a string")
    if kind not in ALLOWED_ENTITY_KINDS:
        raise ValueError(f"unsupported entity_kind: {kind!r}")
    return {_ENTITY_KEY: kind}


def validate_entities(entities: Iterable[Mapping[str, Any]]) -> Tuple[Dict[str, str], ...]:
    return tuple(validate_entity(entity) for entity in entities)


def validate_source_turn_ref(value: str) -> str:
    text = str(value or "")
    if not text:
        return ""
    if _is_uuid_hex_snapshot_ref(text):
        return text
    if not _SOURCE_TURN_REF_RE.fullmatch(text):
        raise ValueError("source_turn_ref has invalid opaque identifier format")
    _reject_sensitive_text(text, field_name="source_turn_ref")
    return text


def validate_evidence_ref(value: str) -> str:
    if not isinstance(value, str) or not _EVIDENCE_REF_RE.fullmatch(value):
        raise ValueError("evidence_refs must use trigger:<known_trigger_id>")
    trigger_id = value.split(":", 1)[1]
    if trigger_id not in KNOWN_TRIGGER_IDS:
        raise ValueError(f"unsupported trigger id in evidence_ref: {trigger_id!r}")
    return value


def validate_evidence_refs(values: Iterable[str]) -> Tuple[str, ...]:
    return tuple(validate_evidence_ref(value) for value in values)


def validate_trigger_ids(values: Iterable[str]) -> Tuple[str, ...]:
    out = tuple(str(value) for value in values)
    unknown = [value for value in out if value not in KNOWN_TRIGGER_IDS]
    if unknown:
        raise ValueError(f"unsupported trigger_ids: {unknown}")
    return out


def validate_required_domains(values: Iterable[str]) -> Tuple[str, ...]:
    out = tuple(str(value) for value in values)
    unknown = [value for value in out if value not in REGISTERED_DOMAIN_IDS]
    if unknown:
        raise ValueError(f"unsupported required_domains: {unknown}")
    return out


def validate_reason_codes(values: Iterable[str]) -> Tuple[str, ...]:
    out = tuple(str(value) for value in values)
    unknown = [value for value in out if value not in ALLOWED_REASON_CODES]
    if unknown:
        raise ValueError(f"unsupported reason_codes: {unknown}")
    for value in out:
        _reject_sensitive_text(value, field_name="reason_codes")
    return out


def validate_safety_flags(values: Iterable[str]) -> Tuple[str, ...]:
    out = tuple(str(value) for value in values)
    unknown = [value for value in out if value not in ALLOWED_SAFETY_FLAGS]
    if unknown:
        raise ValueError(f"unsupported safety_flags: {unknown}")
    return out


def validate_constraints(values: Iterable[str]) -> Tuple[str, ...]:
    out = tuple(str(value) for value in values)
    unknown = [value for value in out if value not in ALLOWED_CONSTRAINTS]
    if unknown:
        raise ValueError(f"unsupported constraints: {unknown}")
    for value in out:
        _reject_sensitive_text(value, field_name="constraints")
    return out


def validate_domain_fact_keys(values: Iterable[str]) -> Tuple[str, ...]:
    out = tuple(str(value) for value in values)
    for value in out:
        if not _DOMAIN_FACT_RE.fullmatch(value):
            raise ValueError(f"invalid domain fact key: {value!r}")
        domain = value.split(":", 1)[1]
        if domain not in REGISTERED_DOMAIN_IDS:
            raise ValueError(f"unsupported domain in fact key: {domain!r}")
    return out


def validate_loaded_coverage(values: Iterable[str]) -> Tuple[str, ...]:
    return validate_required_domains(values)


def validate_snapshot_ref(value: str) -> str:
    text = str(value or "")
    if not text:
        return ""
    if _is_uuid_hex_snapshot_ref(text):
        return text
    if not _SNAPSHOT_REF_RE.fullmatch(text):
        raise ValueError("snapshot_ref has invalid opaque identifier format")
    _reject_sensitive_text(text, field_name="snapshot_ref")
    return text


__all__ = [
    "ALLOWED_CONSTRAINTS",
    "ALLOWED_ENTITY_KINDS",
    "ALLOWED_REASON_CODES",
    "ALLOWED_SAFETY_FLAGS",
    "KNOWN_TRIGGER_IDS",
    "validate_constraints",
    "validate_domain_fact_keys",
    "validate_entities",
    "validate_evidence_refs",
    "validate_loaded_coverage",
    "validate_reason_codes",
    "validate_required_domains",
    "validate_safety_flags",
    "validate_snapshot_ref",
    "validate_source_turn_ref",
    "validate_trigger_ids",
]

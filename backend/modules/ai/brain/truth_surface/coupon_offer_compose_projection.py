"""
coupon_offer_compose_projection.py
──────────────────────────────────
Pure compose-safe projection from TrustedContextSnapshot coupon/promotion facts.

Read-only: no DB, no loader calls, no snapshot mutation.
"""
from __future__ import annotations

import re
from typing import Any, Dict, FrozenSet, List, Optional, Tuple

from .contract import TrustedContextSnapshot, TrustedDomain, TrustedFact

SCHEMA_VERSION = "1"
SURFACE = "trusted_coupon_offer_answer"

Availability = str  # closed enum literal

AVAILABILITY_ACTIVE_OR_ELIGIBLE = "active_or_eligible"
AVAILABILITY_PRESENT_BUT_NOT_ELIGIBLE = "present_but_not_eligible"
AVAILABILITY_NONE_VERIFIED = "none_verified"
AVAILABILITY_REQUIRES_CONTEXT = "eligibility_requires_context"

_AVAILABILITY_VALUES: FrozenSet[str] = frozenset(
    {
        AVAILABILITY_ACTIVE_OR_ELIGIBLE,
        AVAILABILITY_PRESENT_BUT_NOT_ELIGIBLE,
        AVAILABILITY_NONE_VERIFIED,
        AVAILABILITY_REQUIRES_CONTEXT,
    }
)

_QUESTION_KIND_VALUES: FrozenSet[str] = frozenset({"coupon", "offer", "combined"})

_CLOSED_SCHEMA_KEYS: FrozenSet[str] = frozenset(
    {
        "schema_version",
        "surface",
        "question_kind",
        "coupon_availability",
        "promotion_availability",
        "verified_eligible_coupon_count",
        "verified_eligible_promotion_count",
        "coupon_record_count",
        "promotion_record_count",
        "unavailability_reason_codes",
        "allow_code_mention",
        "allow_final_eligibility_claim",
        "facts_snapshot_id",
    }
)

_FORBIDDEN_OUTPUT_KEYS: FrozenSet[str] = frozenset(
    {
        "code",
        "code_masked",
        "customer_phone",
        "applicable_products",
        "conditions",
        "promotion_conditions",
        "raw",
        "path",
    }
)

_CLOSED_REASON_CODES: FrozenSet[str] = frozenset(
    {
        "tenant_mismatch",
        "expired",
        "disabled",
        "usage_limit_reached",
        "customer_restriction",
        "customer_unverified",
        "minimum_basket_not_met",
        "minimum_basket_unverified",
        "product_restriction_not_met",
        "product_category_advisory_unverified",
        "already_applied",
        "personalised_unverified",
        "outside_active_window",
        "segment_mismatch",
        "below_min_order_amount",
        "advisory_conditions_unverified",
        "multiple_active_unresolved",
        "no_coupon_data",
        "no_promotion_data",
        "none",
    }
)

_COUPON_QUESTION_RE = re.compile(
    r"كوبون|كود\s*خصم|\bcoupon\b|\bdiscount\b",
    re.IGNORECASE | re.UNICODE,
)
_OFFER_QUESTION_RE = re.compile(
    r"عرض(?:ات)?|عروض|\bpromotion\b|\boffer\b",
    re.IGNORECASE | re.UNICODE,
)

_SENTINEL_COUPON_REASONS = frozenset({"no_coupon_data"})
_SENTINEL_PROMOTION_REASONS = frozenset({"no_promotion_data"})


class CouponOfferComposeProjectionError(ValueError):
    """Schema or privacy validation failure for compose projection."""


def classify_coupon_offer_question_kind(message: str) -> str:
    text = str(message or "").strip()
    has_coupon = bool(_COUPON_QUESTION_RE.search(text))
    has_offer = bool(_OFFER_QUESTION_RE.search(text))
    if has_coupon and has_offer:
        return "combined"
    if has_coupon:
        return "coupon"
    if has_offer:
        return "offer"
    return "combined"


def _record_from_fact(fact: TrustedFact) -> Dict[str, Any]:
    value = fact.value
    if not isinstance(value, dict):
        return {}
    return dict(value)


def _is_sentinel_record(record: Dict[str, Any], sentinel_reasons: FrozenSet[str]) -> bool:
    reason = str(record.get("reason_when_unavailable") or "").strip()
    return reason in sentinel_reasons


def _filter_reason_codes(codes: List[str]) -> List[str]:
    out: List[str] = []
    for code in codes:
        normalized = str(code or "").strip()
        if not normalized or normalized not in _CLOSED_REASON_CODES:
            continue
        if normalized not in out:
            out.append(normalized)
    return sorted(out)


def _availability_for_domain(
    records: List[Dict[str, Any]],
    *,
    sentinel_reasons: FrozenSet[str],
) -> Tuple[Availability, List[str]]:
    real_records = [r for r in records if not _is_sentinel_record(r, sentinel_reasons)]
    if not real_records:
        return AVAILABILITY_NONE_VERIFIED, []

    if any(r.get("eligible") is True for r in real_records):
        reasons = _filter_reason_codes(
            [
                str(r.get("reason_when_unavailable") or "")
                for r in real_records
                if r.get("eligible") is not True and r.get("reason_when_unavailable")
            ]
        )
        return AVAILABILITY_ACTIVE_OR_ELIGIBLE, reasons

    if any(r.get("eligible") is None for r in real_records):
        reasons = _filter_reason_codes(
            [
                str(r.get("reason_when_unavailable") or "")
                for r in real_records
                if r.get("reason_when_unavailable")
            ]
        )
        return AVAILABILITY_REQUIRES_CONTEXT, reasons

    reasons = _filter_reason_codes(
        [
            str(r.get("reason_when_unavailable") or "")
            for r in real_records
            if r.get("reason_when_unavailable")
        ]
    )
    return AVAILABILITY_PRESENT_BUT_NOT_ELIGIBLE, reasons


def _eligible_count(records: List[Dict[str, Any]], sentinel_reasons: FrozenSet[str]) -> int:
    return sum(
        1
        for r in records
        if not _is_sentinel_record(r, sentinel_reasons) and r.get("eligible") is True
    )


def _record_count(records: List[Dict[str, Any]], sentinel_reasons: FrozenSet[str]) -> int:
    return sum(1 for r in records if not _is_sentinel_record(r, sentinel_reasons))


def _scan_forbidden_keys(obj: Any, *, depth: int = 0) -> List[str]:
    if depth > 4:
        return []
    if isinstance(obj, dict):
        found: List[str] = []
        for key, value in obj.items():
            key_l = str(key).lower()
            if key_l in _FORBIDDEN_OUTPUT_KEYS:
                found.append(key_l)
            found.extend(_scan_forbidden_keys(value, depth=depth + 1))
        return found
    if isinstance(obj, list):
        found = []
        for item in obj:
            found.extend(_scan_forbidden_keys(item, depth=depth + 1))
        return found
    return []


def validate_trusted_coupon_offer_compose_facts(payload: Dict[str, Any]) -> None:
    if not isinstance(payload, dict):
        raise CouponOfferComposeProjectionError("payload_not_dict")

    extra = set(payload.keys()) - _CLOSED_SCHEMA_KEYS
    if extra:
        raise CouponOfferComposeProjectionError(f"unknown_fields:{','.join(sorted(extra))}")

    missing = _CLOSED_SCHEMA_KEYS - set(payload.keys())
    if missing:
        raise CouponOfferComposeProjectionError(f"missing_fields:{','.join(sorted(missing))}")

    if str(payload.get("schema_version")) != SCHEMA_VERSION:
        raise CouponOfferComposeProjectionError("invalid_schema_version")
    if str(payload.get("surface")) != SURFACE:
        raise CouponOfferComposeProjectionError("invalid_surface")

    question_kind = str(payload.get("question_kind") or "")
    if question_kind not in _QUESTION_KIND_VALUES:
        raise CouponOfferComposeProjectionError("invalid_question_kind")

    for key in ("coupon_availability", "promotion_availability"):
        if str(payload.get(key) or "") not in _AVAILABILITY_VALUES:
            raise CouponOfferComposeProjectionError(f"invalid_{key}")

    for key in (
        "verified_eligible_coupon_count",
        "verified_eligible_promotion_count",
        "coupon_record_count",
        "promotion_record_count",
    ):
        value = payload.get(key)
        if not isinstance(value, int) or value < 0:
            raise CouponOfferComposeProjectionError(f"invalid_{key}")

    reasons = payload.get("unavailability_reason_codes")
    if not isinstance(reasons, list):
        raise CouponOfferComposeProjectionError("invalid_unavailability_reason_codes")
    for code in reasons:
        if str(code) not in _CLOSED_REASON_CODES:
            raise CouponOfferComposeProjectionError("invalid_reason_code")

    if payload.get("allow_code_mention") is not False:
        raise CouponOfferComposeProjectionError("allow_code_mention_must_be_false")
    if not isinstance(payload.get("allow_final_eligibility_claim"), bool):
        raise CouponOfferComposeProjectionError("invalid_allow_final_eligibility_claim")

    snapshot_id = str(payload.get("facts_snapshot_id") or "").strip()
    if not snapshot_id:
        raise CouponOfferComposeProjectionError("missing_facts_snapshot_id")

    leaks = _scan_forbidden_keys(payload)
    if leaks:
        raise CouponOfferComposeProjectionError(f"forbidden_keys:{','.join(sorted(set(leaks)))}")


def project_trusted_coupon_offer_compose_facts(
    *,
    snapshot: TrustedContextSnapshot,
    message: str,
) -> Dict[str, Any]:
    """
  Build closed compose contract from snapshot coupon/promotion domains only.
    """
    coupon_records = [
        _record_from_fact(f) for f in snapshot.facts_for_domain(TrustedDomain.COUPONS)
    ]
    promo_records = [
        _record_from_fact(f) for f in snapshot.facts_for_domain(TrustedDomain.PROMOTIONS)
    ]

    coupon_availability, coupon_reasons = _availability_for_domain(
        coupon_records,
        sentinel_reasons=_SENTINEL_COUPON_REASONS,
    )
    promotion_availability, promo_reasons = _availability_for_domain(
        promo_records,
        sentinel_reasons=_SENTINEL_PROMOTION_REASONS,
    )

    obs = dict(snapshot.shadow_observability or {})
    verified_eligible_coupon_count = int(obs.get("eligible_coupon_count") or 0)
    verified_eligible_promotion_count = int(obs.get("eligible_promotion_count") or 0)

    counted_coupon_eligible = _eligible_count(coupon_records, _SENTINEL_COUPON_REASONS)
    counted_promo_eligible = _eligible_count(promo_records, _SENTINEL_PROMOTION_REASONS)
    if counted_coupon_eligible != verified_eligible_coupon_count:
        verified_eligible_coupon_count = counted_coupon_eligible
    if counted_promo_eligible != verified_eligible_promotion_count:
        verified_eligible_promotion_count = counted_promo_eligible

    combined_reasons = _filter_reason_codes(coupon_reasons + promo_reasons)

    requires_context = (
        coupon_availability == AVAILABILITY_REQUIRES_CONTEXT
        or promotion_availability == AVAILABILITY_REQUIRES_CONTEXT
    )

    payload: Dict[str, Any] = {
        "schema_version": SCHEMA_VERSION,
        "surface": SURFACE,
        "question_kind": classify_coupon_offer_question_kind(message),
        "coupon_availability": coupon_availability,
        "promotion_availability": promotion_availability,
        "verified_eligible_coupon_count": verified_eligible_coupon_count,
        "verified_eligible_promotion_count": verified_eligible_promotion_count,
        "coupon_record_count": _record_count(coupon_records, _SENTINEL_COUPON_REASONS),
        "promotion_record_count": _record_count(promo_records, _SENTINEL_PROMOTION_REASONS),
        "unavailability_reason_codes": combined_reasons,
        "allow_code_mention": False,
        "allow_final_eligibility_claim": not requires_context,
        "facts_snapshot_id": snapshot.ensure_snapshot_id(),
    }
    validate_trusted_coupon_offer_compose_facts(payload)
    return payload


__all__ = [
    "AVAILABILITY_ACTIVE_OR_ELIGIBLE",
    "AVAILABILITY_NONE_VERIFIED",
    "AVAILABILITY_PRESENT_BUT_NOT_ELIGIBLE",
    "AVAILABILITY_REQUIRES_CONTEXT",
    "CouponOfferComposeProjectionError",
    "classify_coupon_offer_question_kind",
    "project_trusted_coupon_offer_compose_facts",
    "validate_trusted_coupon_offer_compose_facts",
]

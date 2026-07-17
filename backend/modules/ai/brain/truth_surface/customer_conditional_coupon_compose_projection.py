"""
customer_conditional_coupon_compose_projection.py
───────────────────────────────────────────────────
Pure compose-safe projection from sanitized v8 CUSTOMER_CONDITIONAL_COUPON facts.

Read-only: no DB, no loader calls, no snapshot mutation.
"""
from __future__ import annotations

from typing import Any, Dict, FrozenSet, List, Optional

from .contract import TrustedContextSnapshot, TrustedDomain, TrustedFact
from .customer_conditional_coupon_contract import (
    COMPLETENESS_UNVERIFIED,
    COMPLETENESS_VERIFIED,
    EVALUATION_CONDITION_SATISFIED,
    EVALUATION_CONDITION_SHORTFALL,
    EVALUATION_REQUIRES_CONTEXT,
    EVALUATION_UNAVAILABLE,
    FACT_DOMAIN,
    FACT_SCHEMA_VERSION,
    IDENTITY_STATUS_AMBIGUOUS,
    IDENTITY_STATUS_RESOLVED,
    IDENTITY_STATUS_UNRESOLVED,
    MIN_ORDERS_STATE_NOT_EVALUATED,
    MIN_ORDERS_STATE_SATISFIED,
    MIN_ORDERS_STATE_SHORTFALL,
    assert_fact_record_sanitized,
)

SCHEMA_VERSION = "1"
SURFACE = "customer_conditional_coupon_answer"

_IDENTITY_STATUS_VALUES: FrozenSet[str] = frozenset(
    {
        IDENTITY_STATUS_RESOLVED,
        IDENTITY_STATUS_UNRESOLVED,
        IDENTITY_STATUS_AMBIGUOUS,
    }
)
_MIN_ORDERS_STATE_VALUES: FrozenSet[str] = frozenset(
    {
        MIN_ORDERS_STATE_SATISFIED,
        MIN_ORDERS_STATE_SHORTFALL,
        MIN_ORDERS_STATE_NOT_EVALUATED,
    }
)
_EVALUATION_STATE_VALUES: FrozenSet[str] = frozenset(
    {
        EVALUATION_CONDITION_SATISFIED,
        EVALUATION_CONDITION_SHORTFALL,
        EVALUATION_REQUIRES_CONTEXT,
        EVALUATION_UNAVAILABLE,
    }
)
_COMPLETENESS_VALUES: FrozenSet[str] = frozenset(
    {COMPLETENESS_VERIFIED, COMPLETENESS_UNVERIFIED}
)

_CLOSED_SCHEMA_KEYS: FrozenSet[str] = frozenset(
    {
        "schema_version",
        "surface",
        "identity_status",
        "min_orders_condition_state",
        "conditional_coupon_evaluation_state",
        "order_history_completeness",
        "completed_orders_count",
        "min_orders_for_eligibility",
        "orders_shortfall",
        "allow_min_orders_condition_claim",
        "closed_reason_code",
        "facts_snapshot_id",
    }
)

_FORBIDDEN_OUTPUT_KEYS: FrozenSet[str] = frozenset(
    {
        "code",
        "coupon_id",
        "promotion_id",
        "customer_id",
        "customer_phone",
        "phone",
        "external_customer_ref",
        "external_id",
        "order_id",
        "raw",
        "path",
    }
)

_CLOSED_REASON_CODES: FrozenSet[str] = frozenset(
    {
        "order_history_identity_unverified",
        "order_history_coverage_incomplete",
        "order_history_sync_stale",
        "order_history_sync_degraded",
        "customer_unverified",
        "orders_shortfall",
        "target_budget_exceeded",
        "no_conditional_target",
        "declarative_usage_policy",
        "loader_failure",
        "subject_scope_ambiguous",
        "authoritative_history_proof_absent",
        "count_query_failure",
        "none",
    }
)


class CustomerConditionalCouponComposeProjectionError(ValueError):
    """Schema or privacy validation failure for compose projection."""


def _record_from_fact(fact: TrustedFact) -> Dict[str, Any]:
    value = fact.value
    if not isinstance(value, dict):
        return {}
    return dict(value)


def _scan_forbidden_keys(obj: Any, *, depth: int = 0, allowed_keys: Optional[FrozenSet[str]] = None) -> List[str]:
    allowed = allowed_keys or frozenset()
    if depth > 4:
        return []
    if isinstance(obj, dict):
        found: List[str] = []
        for key, value in obj.items():
            key_l = str(key).lower()
            if key_l not in allowed:
                if key_l in _FORBIDDEN_OUTPUT_KEYS or key_l.endswith("_id") or "phone" in key_l:
                    found.append(key_l)
            found.extend(
                _scan_forbidden_keys(value, depth=depth + 1, allowed_keys=allowed_keys)
            )
        return found
    if isinstance(obj, list):
        found: List[str] = []
        for item in obj:
            found.extend(
                _scan_forbidden_keys(item, depth=depth + 1, allowed_keys=allowed_keys)
            )
        return found
    return []


def _normalize_closed_reason(code: Any) -> Optional[str]:
    if code is None:
        return None
    normalized = str(code or "").strip()
    if not normalized:
        return None
    if normalized not in _CLOSED_REASON_CODES:
        raise CustomerConditionalCouponComposeProjectionError("invalid_closed_reason_code")
    return normalized


def _optional_non_negative_int(value: Any) -> Optional[int]:
    if value is None:
        return None
    if not isinstance(value, int) or value < 0:
        raise CustomerConditionalCouponComposeProjectionError("invalid_count_field")
    return int(value)


def validate_customer_conditional_coupon_compose_facts(payload: Dict[str, Any]) -> None:
    if not isinstance(payload, dict):
        raise CustomerConditionalCouponComposeProjectionError("payload_not_dict")

    extra = set(payload.keys()) - _CLOSED_SCHEMA_KEYS
    if extra:
        raise CustomerConditionalCouponComposeProjectionError(
            f"unknown_fields:{','.join(sorted(extra))}"
        )

    missing = _CLOSED_SCHEMA_KEYS - set(payload.keys())
    if missing:
        raise CustomerConditionalCouponComposeProjectionError(
            f"missing_fields:{','.join(sorted(missing))}"
        )

    if str(payload.get("schema_version")) != SCHEMA_VERSION:
        raise CustomerConditionalCouponComposeProjectionError("invalid_schema_version")
    if str(payload.get("surface")) != SURFACE:
        raise CustomerConditionalCouponComposeProjectionError("invalid_surface")

    if str(payload.get("identity_status") or "") not in _IDENTITY_STATUS_VALUES:
        raise CustomerConditionalCouponComposeProjectionError("invalid_identity_status")
    if str(payload.get("min_orders_condition_state") or "") not in _MIN_ORDERS_STATE_VALUES:
        raise CustomerConditionalCouponComposeProjectionError("invalid_min_orders_condition_state")
    if (
        str(payload.get("conditional_coupon_evaluation_state") or "")
        not in _EVALUATION_STATE_VALUES
    ):
        raise CustomerConditionalCouponComposeProjectionError(
            "invalid_conditional_coupon_evaluation_state"
        )
    if str(payload.get("order_history_completeness") or "") not in _COMPLETENESS_VALUES:
        raise CustomerConditionalCouponComposeProjectionError("invalid_order_history_completeness")

    for key in (
        "completed_orders_count",
        "min_orders_for_eligibility",
        "orders_shortfall",
    ):
        value = payload.get(key)
        if value is not None and (not isinstance(value, int) or value < 0):
            raise CustomerConditionalCouponComposeProjectionError(f"invalid_{key}")

    reason = payload.get("closed_reason_code")
    if reason is not None and str(reason) not in _CLOSED_REASON_CODES:
        raise CustomerConditionalCouponComposeProjectionError("invalid_closed_reason_code")

    if not isinstance(payload.get("allow_min_orders_condition_claim"), bool):
        raise CustomerConditionalCouponComposeProjectionError(
            "invalid_allow_min_orders_condition_claim"
        )

    snapshot_id = str(payload.get("facts_snapshot_id") or "").strip()
    if not snapshot_id:
        raise CustomerConditionalCouponComposeProjectionError("missing_facts_snapshot_id")

    leaks = _scan_forbidden_keys(payload, allowed_keys=_CLOSED_SCHEMA_KEYS)
    if leaks:
        raise CustomerConditionalCouponComposeProjectionError(
            f"forbidden_keys:{','.join(sorted(set(leaks)))}"
        )


def project_customer_conditional_coupon_compose_facts(
    *,
    snapshot: TrustedContextSnapshot,
    expected_tenant_id: Optional[int] = None,
) -> Dict[str, Any]:
    """Build closed compose contract from snapshot CUSTOMER_CONDITIONAL_COUPON domain."""
    if expected_tenant_id is not None and int(snapshot.tenant_id) != int(expected_tenant_id):
        raise CustomerConditionalCouponComposeProjectionError("tenant_mismatch")

    domain_facts = list(snapshot.facts_for_domain(TrustedDomain.CUSTOMER_CONDITIONAL_COUPON))
    if len(domain_facts) != 1:
        raise CustomerConditionalCouponComposeProjectionError("fact_count_invalid")

    record = _record_from_fact(domain_facts[0])
    if not record:
        raise CustomerConditionalCouponComposeProjectionError("fact_record_empty")

    if str(record.get("domain") or "") != FACT_DOMAIN:
        raise CustomerConditionalCouponComposeProjectionError("invalid_fact_domain")
    if str(record.get("fact_schema_version") or "") != FACT_SCHEMA_VERSION:
        raise CustomerConditionalCouponComposeProjectionError("invalid_fact_schema_version")

    try:
        assert_fact_record_sanitized(record)
    except ValueError as exc:
        raise CustomerConditionalCouponComposeProjectionError(
            f"sanitizer_rejected:{exc}"
        ) from exc

    identity_status = str(record.get("identity_status") or "")
    if identity_status == IDENTITY_STATUS_AMBIGUOUS:
        raise CustomerConditionalCouponComposeProjectionError("identity_ambiguous")
    if identity_status not in _IDENTITY_STATUS_VALUES:
        raise CustomerConditionalCouponComposeProjectionError("invalid_identity_status")

    min_orders_state = str(record.get("min_orders_condition_state") or "")
    if min_orders_state not in _MIN_ORDERS_STATE_VALUES:
        raise CustomerConditionalCouponComposeProjectionError("invalid_min_orders_condition_state")

    evaluation_state = str(record.get("conditional_coupon_evaluation_state") or "")
    if evaluation_state not in _EVALUATION_STATE_VALUES:
        raise CustomerConditionalCouponComposeProjectionError(
            "invalid_conditional_coupon_evaluation_state"
        )

    completeness = str(record.get("order_history_completeness") or "")
    if completeness not in _COMPLETENESS_VALUES:
        raise CustomerConditionalCouponComposeProjectionError("invalid_order_history_completeness")

    payload: Dict[str, Any] = {
        "schema_version": SCHEMA_VERSION,
        "surface": SURFACE,
        "identity_status": identity_status,
        "min_orders_condition_state": min_orders_state,
        "conditional_coupon_evaluation_state": evaluation_state,
        "order_history_completeness": completeness,
        "completed_orders_count": _optional_non_negative_int(
            record.get("completed_orders_count")
        ),
        "min_orders_for_eligibility": _optional_non_negative_int(
            record.get("min_orders_for_eligibility")
        ),
        "orders_shortfall": _optional_non_negative_int(record.get("orders_shortfall")),
        "allow_min_orders_condition_claim": bool(
            record.get("allow_min_orders_condition_claim")
        ),
        "closed_reason_code": _normalize_closed_reason(record.get("closed_reason_code")),
        "facts_snapshot_id": snapshot.ensure_snapshot_id(),
    }
    validate_customer_conditional_coupon_compose_facts(payload)
    return payload


__all__ = [
    "CustomerConditionalCouponComposeProjectionError",
    "SCHEMA_VERSION",
    "SURFACE",
    "project_customer_conditional_coupon_compose_facts",
    "validate_customer_conditional_coupon_compose_facts",
]

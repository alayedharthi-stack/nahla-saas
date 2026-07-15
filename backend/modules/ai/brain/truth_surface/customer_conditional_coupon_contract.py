"""
customer_conditional_coupon_contract.py
───────────────────────────────────────
Closed v8 fact contract for Layer 0 conditional-coupon evidence.

No customer-facing prose. No operational eligibility claims.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Dict, Iterable, Optional

BUNDLE_NAMESPACE = "customer_conditional_coupon"
FACT_DOMAIN = "customer_conditional_coupon"
FACT_SCHEMA_VERSION = "v8_layer0"

# Closed identity / scope states
IDENTITY_STATUS_RESOLVED = "resolved"
IDENTITY_STATUS_UNRESOLVED = "unresolved"
IDENTITY_STATUS_AMBIGUOUS = "ambiguous"

CUSTOMER_SCOPE_INTERNAL = "nahla_internal_customer"
CUSTOMER_SCOPE_EXTERNAL = "external_customer_profile"
CUSTOMER_SCOPE_UNRESOLVED = "unresolved"

# Completeness (mirrors A1 coverage vocabulary — read-only mirror)
COMPLETENESS_VERIFIED = "verified"
COMPLETENESS_UNVERIFIED = "unverified"

COMPLETENESS_SOURCE_A1_AUTHORITATIVE = "order_customer_fk_a1_authoritative"

# Condition evaluation states (conditional slice only — not final coupon eligibility)
MIN_ORDERS_STATE_SATISFIED = "satisfied"
MIN_ORDERS_STATE_SHORTFALL = "shortfall"
MIN_ORDERS_STATE_NOT_EVALUATED = "not_evaluated"

PRIOR_REDEMPTION_EVIDENCE_AVAILABLE = "available"
PRIOR_REDEMPTION_EVIDENCE_UNAVAILABLE = "unavailable"
PRIOR_REDEMPTION_EVIDENCE_NOT_APPLICABLE = "not_applicable"

USAGE_POLICY_VERIFIED = "verified"
USAGE_POLICY_DECLARATIVE_ONLY = "declarative_only"
USAGE_POLICY_UNAVAILABLE = "unavailable"

EVALUATION_CONDITION_SATISFIED = "condition_satisfied"
EVALUATION_CONDITION_SHORTFALL = "condition_shortfall"
EVALUATION_REQUIRES_CONTEXT = "requires_context"
EVALUATION_UNAVAILABLE = "unavailable"

# Closed reason codes (v8 §10 subset used at Layer 0)
REASON_ORDER_HISTORY_IDENTITY_UNVERIFIED = "order_history_identity_unverified"
REASON_ORDER_HISTORY_COVERAGE_INCOMPLETE = "order_history_coverage_incomplete"
REASON_ORDER_HISTORY_SYNC_STALE = "order_history_sync_stale"
REASON_ORDER_HISTORY_SYNC_DEGRADED = "order_history_sync_degraded"
REASON_CUSTOMER_UNVERIFIED = "customer_unverified"
REASON_ORDERS_SHORTFALL = "orders_shortfall"
REASON_TARGET_BUDGET_EXCEEDED = "target_budget_exceeded"
REASON_NO_CONDITIONAL_TARGET = "no_conditional_target"
REASON_DECLARATIVE_USAGE_POLICY = "declarative_usage_policy"
REASON_LOADER_FAILURE = "loader_failure"
REASON_SUBJECT_AMBIGUOUS = "subject_scope_ambiguous"
REASON_PROOF_ABSENT = "authoritative_history_proof_absent"
REASON_COUNT_QUERY_FAILURE = "count_query_failure"

MAX_CONDITIONAL_TARGETS = 5

FORBIDDEN_FACT_KEYS = frozenset({
    "customer_id",
    "external_customer_profile_id",
    "coupon_id",
    "promotion_id",
    "offer_id",
    "offer_ref",
    "phone",
    "customer_phone",
    "external_customer_ref",
    "external_id",
    "order_id",
})


@dataclass(frozen=True)
class ConditionalTargetSummary:
    """Internal-only scan result — never serialized into TrustedFact."""

    min_orders_for_eligibility: int
    has_personalised_usage_gate: bool


def build_sanitized_fact_record(
    *,
    identity_status: str,
    customer_scope: str,
    order_history_completeness: str,
    order_history_completeness_source: Optional[str],
    completed_orders_count: Optional[int],
    min_orders_for_eligibility: Optional[int],
    orders_shortfall: Optional[int],
    min_orders_condition_state: str,
    prior_redemption_evidence_state: str,
    per_customer_usage_policy_state: str,
    conditional_coupon_evaluation_state: str,
    closed_reason_code: Optional[str],
    allow_min_orders_condition_claim: bool,
) -> Dict[str, Any]:
    """Build the closed v8 Layer 0 fact payload (sanitized — no IDs)."""
    record: Dict[str, Any] = {
        "domain": FACT_DOMAIN,
        "bundle_namespace": BUNDLE_NAMESPACE,
        "fact_schema_version": FACT_SCHEMA_VERSION,
        "identity_status": identity_status,
        "customer_scope": customer_scope,
        "order_history_completeness": order_history_completeness,
        "order_history_completeness_source": order_history_completeness_source,
        "completed_orders_count": completed_orders_count,
        "min_orders_for_eligibility": min_orders_for_eligibility,
        "orders_shortfall": orders_shortfall,
        "min_orders_condition_state": min_orders_condition_state,
        "prior_redemption_evidence_state": prior_redemption_evidence_state,
        "per_customer_usage_policy_state": per_customer_usage_policy_state,
        "conditional_coupon_evaluation_state": conditional_coupon_evaluation_state,
        "closed_reason_code": closed_reason_code,
        "allow_min_orders_condition_claim": bool(allow_min_orders_condition_claim),
    }
    return record


def build_sanitized_telemetry(
    *,
    conditional_target_count: int,
    order_history_completeness: str,
    forward_sync_health: Optional[str],
    source_contract_version: Optional[str],
    order_count_query_count: int,
    usage_evidence_query_count: int,
    budget_exceeded: bool,
    loader_duration_ms: int,
    gate_skipped_reason: Optional[str] = None,
) -> Dict[str, Any]:
    return {
        "conditional_target_count": int(conditional_target_count),
        "order_history_completeness": order_history_completeness,
        "forward_sync_health": forward_sync_health,
        "source_contract_version": source_contract_version,
        "order_count_query_count": int(order_count_query_count),
        "usage_evidence_query_count": int(usage_evidence_query_count),
        "budget_exceeded": bool(budget_exceeded),
        "loader_duration_ms": int(loader_duration_ms),
        "gate_skipped_reason": gate_skipped_reason,
    }


def _walk_payload_keys(value: Any) -> Iterable[str]:
    if isinstance(value, dict):
        for key, nested in value.items():
            yield str(key)
            yield from _walk_payload_keys(nested)
    elif isinstance(value, (list, tuple, set)):
        for nested in value:
            yield from _walk_payload_keys(nested)


def assert_fact_record_sanitized(record: Dict[str, Any]) -> None:
    """Reject forbidden identifier or PII-like keys at every nested depth."""
    for key in _walk_payload_keys(record):
        normalized = key.strip().lower()
        if (
            normalized in FORBIDDEN_FACT_KEYS
            or normalized.endswith("_id")
            or "phone" in normalized
            or "external_ref" in normalized
        ):
            raise ValueError(f"forbidden_fact_key:{normalized}")


__all__ = [
    "BUNDLE_NAMESPACE",
    "COMPLETENESS_SOURCE_A1_AUTHORITATIVE",
    "COMPLETENESS_UNVERIFIED",
    "COMPLETENESS_VERIFIED",
    "ConditionalTargetSummary",
    "CUSTOMER_SCOPE_EXTERNAL",
    "CUSTOMER_SCOPE_INTERNAL",
    "CUSTOMER_SCOPE_UNRESOLVED",
    "EVALUATION_CONDITION_SATISFIED",
    "EVALUATION_CONDITION_SHORTFALL",
    "EVALUATION_REQUIRES_CONTEXT",
    "EVALUATION_UNAVAILABLE",
    "FACT_DOMAIN",
    "FACT_SCHEMA_VERSION",
    "FORBIDDEN_FACT_KEYS",
    "IDENTITY_STATUS_AMBIGUOUS",
    "IDENTITY_STATUS_RESOLVED",
    "IDENTITY_STATUS_UNRESOLVED",
    "MAX_CONDITIONAL_TARGETS",
    "MIN_ORDERS_STATE_NOT_EVALUATED",
    "MIN_ORDERS_STATE_SATISFIED",
    "MIN_ORDERS_STATE_SHORTFALL",
    "PRIOR_REDEMPTION_EVIDENCE_NOT_APPLICABLE",
    "PRIOR_REDEMPTION_EVIDENCE_UNAVAILABLE",
    "REASON_COUNT_QUERY_FAILURE",
    "REASON_CUSTOMER_UNVERIFIED",
    "REASON_DECLARATIVE_USAGE_POLICY",
    "REASON_LOADER_FAILURE",
    "REASON_NO_CONDITIONAL_TARGET",
    "REASON_ORDER_HISTORY_COVERAGE_INCOMPLETE",
    "REASON_ORDER_HISTORY_IDENTITY_UNVERIFIED",
    "REASON_ORDER_HISTORY_SYNC_DEGRADED",
    "REASON_ORDER_HISTORY_SYNC_STALE",
    "REASON_ORDERS_SHORTFALL",
    "REASON_PROOF_ABSENT",
    "REASON_SUBJECT_AMBIGUOUS",
    "REASON_TARGET_BUDGET_EXCEEDED",
    "USAGE_POLICY_DECLARATIVE_ONLY",
    "USAGE_POLICY_UNAVAILABLE",
    "USAGE_POLICY_VERIFIED",
    "assert_fact_record_sanitized",
    "build_sanitized_fact_record",
    "build_sanitized_telemetry",
]

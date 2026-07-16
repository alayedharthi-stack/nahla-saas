"""
customer_conditional_coupon_shadow_readiness.py
───────────────────────────────────────────────
Manual shadow-review checklist encoder for Layer 0 conditional-coupon facts.

Summarizes operator-supplied, schema-validated observations and manual
attestations. Does **not** independently prove CI, A1 capability, subject
proof, or deployment state. Never enables flags, compose, dispatch, or
customer-facing behavior.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Dict, FrozenSet, Iterable, List, Optional, Tuple

from .customer_conditional_coupon_contract import (
    BUNDLE_NAMESPACE,
    FACT_DOMAIN,
    FACT_SCHEMA_VERSION,
    FORBIDDEN_FACT_KEYS,
    MAX_CONDITIONAL_TARGETS,
    REASON_COUNT_QUERY_FAILURE,
    REASON_CUSTOMER_UNVERIFIED,
    REASON_DECLARATIVE_USAGE_POLICY,
    REASON_LOADER_FAILURE,
    REASON_NO_CONDITIONAL_TARGET,
    REASON_ORDER_HISTORY_COVERAGE_INCOMPLETE,
    REASON_ORDER_HISTORY_IDENTITY_UNVERIFIED,
    REASON_ORDER_HISTORY_SYNC_DEGRADED,
    REASON_ORDER_HISTORY_SYNC_STALE,
    REASON_ORDERS_SHORTFALL,
    REASON_PROOF_ABSENT,
    REASON_SUBJECT_AMBIGUOUS,
    REASON_TARGET_BUDGET_EXCEEDED,
    assert_fact_record_sanitized,
)

READINESS_SCHEMA_VERSION = "coupon_shadow_manual_checklist_v2"
ARTIFACT_KIND = "manual_shadow_checklist"
INDEPENDENT_VERIFICATION = "none"

OUTCOME_MANUAL_CHECKLIST_COMPLETE = "manual_shadow_checklist_complete"
OUTCOME_MANUAL_CHECKLIST_INCOMPLETE = "manual_checklist_incomplete"
OUTCOME_BLOCKED_INVALID_INPUT = "blocked_by_invalid_input"
OUTCOME_BLOCKED_SANITIZATION = "blocked_by_sanitization"
OUTCOME_BLOCKED_A1_ATTESTATION = "blocked_by_a1_attestation"
OUTCOME_BLOCKED_SUBJECT_PROOF = "blocked_by_subject_proof"
OUTCOME_BLOCKED_BUDGET_TELEMETRY = "blocked_by_budget_telemetry"

SHADOW_READINESS_OUTCOMES = frozenset({
    OUTCOME_MANUAL_CHECKLIST_COMPLETE,
    OUTCOME_MANUAL_CHECKLIST_INCOMPLETE,
    OUTCOME_BLOCKED_INVALID_INPUT,
    OUTCOME_BLOCKED_SANITIZATION,
    OUTCOME_BLOCKED_A1_ATTESTATION,
    OUTCOME_BLOCKED_SUBJECT_PROOF,
    OUTCOME_BLOCKED_BUDGET_TELEMETRY,
})

SUBJECT_BRIDGE_OUTCOME_RESOLVED = "resolved"
SUBJECT_BRIDGE_OUTCOME_UNRESOLVED = "unresolved"
SUBJECT_BRIDGE_OUTCOME_AMBIGUOUS = "ambiguous"

SUBJECT_BRIDGE_OUTCOMES = frozenset({
    SUBJECT_BRIDGE_OUTCOME_RESOLVED,
    SUBJECT_BRIDGE_OUTCOME_UNRESOLVED,
    SUBJECT_BRIDGE_OUTCOME_AMBIGUOUS,
})

MANUAL_CHECKLIST_ITEMS = frozenset({
    "ci_layer0_unit_tests_attested",
    "ci_bridge_consumer_tests_attested",
    "ci_pg_e2e_a1_chain_tests_attested",
    "a1_capability_validated_attested",
    "deployment_shadow_flag_default_off_attested",
})

LAYER0_FACT_RECORD_KEYS = frozenset({
    "domain",
    "bundle_namespace",
    "fact_schema_version",
    "identity_status",
    "customer_scope",
    "order_history_completeness",
    "order_history_completeness_source",
    "completed_orders_count",
    "min_orders_for_eligibility",
    "orders_shortfall",
    "min_orders_condition_state",
    "prior_redemption_evidence_state",
    "per_customer_usage_policy_state",
    "conditional_coupon_evaluation_state",
    "closed_reason_code",
    "allow_min_orders_condition_claim",
})

LAYER0_TELEMETRY_KEYS = frozenset({
    "conditional_target_count",
    "order_history_completeness",
    "forward_sync_health",
    "source_contract_version",
    "order_count_query_count",
    "usage_evidence_query_count",
    "budget_exceeded",
    "loader_duration_ms",
    "gate_skipped_reason",
})

CLOSED_REASON_CODES = frozenset({
    REASON_ORDER_HISTORY_IDENTITY_UNVERIFIED,
    REASON_ORDER_HISTORY_COVERAGE_INCOMPLETE,
    REASON_ORDER_HISTORY_SYNC_STALE,
    REASON_ORDER_HISTORY_SYNC_DEGRADED,
    REASON_CUSTOMER_UNVERIFIED,
    REASON_ORDERS_SHORTFALL,
    REASON_TARGET_BUDGET_EXCEEDED,
    REASON_NO_CONDITIONAL_TARGET,
    REASON_DECLARATIVE_USAGE_POLICY,
    REASON_LOADER_FAILURE,
    REASON_SUBJECT_AMBIGUOUS,
    REASON_PROOF_ABSENT,
    REASON_COUNT_QUERY_FAILURE,
})

SUBJECT_PROOF_CLOSED_REASONS = frozenset({
    REASON_CUSTOMER_UNVERIFIED,
    REASON_SUBJECT_AMBIGUOUS,
    REASON_PROOF_ABSENT,
    REASON_ORDER_HISTORY_IDENTITY_UNVERIFIED,
    REASON_ORDER_HISTORY_COVERAGE_INCOMPLETE,
    REASON_ORDER_HISTORY_SYNC_STALE,
    REASON_ORDER_HISTORY_SYNC_DEGRADED,
})

BUDGET_TELEMETRY_CLOSED_REASONS = frozenset({
    REASON_TARGET_BUDGET_EXCEEDED,
    REASON_COUNT_QUERY_FAILURE,
    REASON_LOADER_FAILURE,
})

FORBIDDEN_EVIDENCE_TOP_LEVEL_KEYS = frozenset({
    "phone",
    "customer_phone",
    "message",
    "inbound_metadata",
    "customer_id",
    "conversation_id",
    "tenant_id",
    "policy_eligibility_ready",
    "sanitizer_passed",
})

MAX_ORDER_COUNT_QUERIES_PER_SHADOW_TURN = 1
MAX_USAGE_EVIDENCE_QUERIES_PER_SHADOW_TURN = 1


@dataclass(frozen=True)
class CouponShadowReadinessEvidence:
    """
    Operator-supplied observations and manual attestations.

    Direct construction is allowed for tests/operators, but
    ``evaluate_coupon_shadow_readiness`` always re-validates fact schema,
    telemetry-shaped counters, PII/forbidden keys, and sanitizer status.
    Caller-supplied sanitizer booleans are not accepted.
    """

    shadow_flag_default_off_observed: bool
    a1_proof_gate_attested: bool
    subject_bridge_outcome: str
    layer0_fact_record: Optional[Dict[str, Any]]
    conditional_target_count: int
    order_count_query_count: int
    usage_evidence_query_count: int
    budget_exceeded: bool
    gate_skipped_reason: Optional[str]
    closed_reason_code: Optional[str]
    manual_checklist_attested: FrozenSet[str]
    tenant_scope_label: Optional[str] = None


@dataclass(frozen=True)
class CouponShadowReadinessResult:
    readiness_schema_version: str = READINESS_SCHEMA_VERSION
    artifact_kind: str = ARTIFACT_KIND
    independent_verification: str = INDEPENDENT_VERIFICATION
    outcome: str = OUTCOME_MANUAL_CHECKLIST_INCOMPLETE
    ready_for_manual_shadow_review: bool = False
    canary_or_compose_forbidden: bool = True
    checklist_gates: Dict[str, bool] = field(default_factory=dict)
    readiness_blockers: Tuple[str, ...] = ()
    missing_checklist_items: Tuple[str, ...] = ()

    def to_dict(self) -> Dict[str, Any]:
        return {
            "readiness_schema_version": self.readiness_schema_version,
            "artifact_kind": self.artifact_kind,
            "independent_verification": self.independent_verification,
            "outcome": self.outcome,
            "ready_for_manual_shadow_review": bool(self.ready_for_manual_shadow_review),
            "canary_or_compose_forbidden": True,
            "checklist_gates": dict(self.checklist_gates),
            "readiness_blockers": list(self.readiness_blockers),
            "missing_checklist_items": list(self.missing_checklist_items),
        }


def _missing_checklist_items(attested: FrozenSet[str]) -> Tuple[str, ...]:
    return tuple(sorted(MANUAL_CHECKLIST_ITEMS - set(attested)))


def _walk_forbidden_keys(value: Any, *, path: str = "") -> Optional[str]:
    if isinstance(value, dict):
        for key, nested in value.items():
            normalized = str(key).strip().lower()
            if normalized in FORBIDDEN_FACT_KEYS or normalized in FORBIDDEN_EVIDENCE_TOP_LEVEL_KEYS:
                return f"forbidden_key:{normalized}"
            if normalized.endswith("_id"):
                return f"forbidden_key:{normalized}"
            if "phone" in normalized or "external_ref" in normalized:
                return f"forbidden_key:{normalized}"
            if normalized in {"message", "inbound_metadata", "metadata"}:
                return f"forbidden_key:{normalized}"
            nested_path = f"{path}.{normalized}" if path else normalized
            blocked = _walk_forbidden_keys(nested, path=nested_path)
            if blocked:
                return blocked
    elif isinstance(value, (list, tuple, set)):
        for index, nested in enumerate(value):
            blocked = _walk_forbidden_keys(nested, path=f"{path}[{index}]")
            if blocked:
                return blocked
    elif isinstance(value, str) and path.endswith("message"):
        return "forbidden_key:message"
    return None


def validate_layer0_fact_schema(record: Any) -> Tuple[bool, Optional[str]]:
    if not isinstance(record, dict):
        return False, "fact_record_not_object"
    keys = frozenset(record.keys())
    unknown = keys - LAYER0_FACT_RECORD_KEYS
    if unknown:
        return False, f"fact_record_unknown_key:{sorted(unknown)[0]}"
    missing = LAYER0_FACT_RECORD_KEYS - keys
    if missing:
        return False, f"fact_record_missing_key:{sorted(missing)[0]}"
    if record.get("domain") != FACT_DOMAIN:
        return False, "fact_domain_mismatch"
    if record.get("bundle_namespace") != BUNDLE_NAMESPACE:
        return False, "fact_bundle_namespace_mismatch"
    if record.get("fact_schema_version") != FACT_SCHEMA_VERSION:
        return False, "fact_schema_version_mismatch"
    closed_reason = record.get("closed_reason_code")
    if closed_reason is not None and closed_reason not in CLOSED_REASON_CODES:
        return False, "fact_closed_reason_unknown"
    forbidden = _walk_forbidden_keys(record)
    if forbidden:
        return False, forbidden
    return True, None


def validate_layer0_telemetry_schema(telemetry: Any) -> Tuple[bool, Optional[str]]:
    if not isinstance(telemetry, dict):
        return False, "telemetry_not_object"
    keys = frozenset(telemetry.keys())
    unknown = keys - LAYER0_TELEMETRY_KEYS
    if unknown:
        return False, f"telemetry_unknown_key:{sorted(unknown)[0]}"
    missing = LAYER0_TELEMETRY_KEYS - keys
    if missing:
        return False, f"telemetry_missing_key:{sorted(missing)[0]}"
    forbidden = _walk_forbidden_keys(telemetry)
    if forbidden:
        return False, forbidden
    return True, None


def verify_layer0_fact_sanitized(record: Dict[str, Any]) -> Tuple[bool, Optional[str]]:
    schema_ok, schema_blocker = validate_layer0_fact_schema(record)
    if not schema_ok:
        return False, schema_blocker
    try:
        assert_fact_record_sanitized(record)
    except ValueError:
        return False, "sanitizer_rejected_fact_record"
    return True, None


def build_evidence_from_layer0_observation(
    *,
    shadow_flag_default_off_observed: bool,
    a1_proof_gate_attested: bool,
    subject_bridge_outcome: str,
    fact_record: Optional[Dict[str, Any]],
    telemetry: Dict[str, Any],
    manual_checklist_attested: Iterable[str],
    tenant_scope_label: Optional[str] = None,
) -> CouponShadowReadinessEvidence:
    """
    Preferred builder for operator-supplied sanitized observations.

    Rejects telemetry with unknown keys (fail-closed). Fact records must pass
    the Layer 0 v8 top-level allowlist before acceptance.
    """
    telemetry_ok, telemetry_blocker = validate_layer0_telemetry_schema(telemetry)
    if not telemetry_ok:
        raise ValueError(telemetry_blocker or "telemetry_invalid")

    if fact_record is not None:
        fact_ok, fact_blocker = validate_layer0_fact_schema(fact_record)
        if not fact_ok:
            raise ValueError(fact_blocker or "fact_record_invalid")

    closed_reason_code: Optional[str] = None
    if isinstance(fact_record, dict):
        raw_reason = fact_record.get("closed_reason_code")
        if isinstance(raw_reason, str) and raw_reason.strip():
            closed_reason_code = raw_reason.strip()

    gate_skipped = telemetry.get("gate_skipped_reason")
    return CouponShadowReadinessEvidence(
        shadow_flag_default_off_observed=bool(shadow_flag_default_off_observed),
        a1_proof_gate_attested=bool(a1_proof_gate_attested),
        subject_bridge_outcome=str(subject_bridge_outcome).strip(),
        layer0_fact_record=dict(fact_record) if isinstance(fact_record, dict) else None,
        conditional_target_count=int(telemetry["conditional_target_count"]),
        order_count_query_count=int(telemetry["order_count_query_count"]),
        usage_evidence_query_count=int(telemetry["usage_evidence_query_count"]),
        budget_exceeded=bool(telemetry["budget_exceeded"]),
        gate_skipped_reason=(
            str(gate_skipped).strip() if gate_skipped is not None else None
        ),
        closed_reason_code=closed_reason_code,
        manual_checklist_attested=frozenset(str(item) for item in manual_checklist_attested),
        tenant_scope_label=(
            str(tenant_scope_label).strip() if tenant_scope_label is not None else None
        ),
    )


def evaluate_coupon_shadow_readiness(
    evidence: CouponShadowReadinessEvidence,
) -> CouponShadowReadinessResult:
    """
    Encode a manual shadow-review checklist from operator evidence.

    Evaluation order:
    1. blocked_by_invalid_input
    2. manual_checklist_incomplete
    3. blocked_by_sanitization
    4. blocked_by_a1_attestation
    5. blocked_by_subject_proof
    6. blocked_by_budget_telemetry
    7. manual_shadow_checklist_complete
    """
    blockers: List[str] = []

    if evidence.subject_bridge_outcome not in SUBJECT_BRIDGE_OUTCOMES:
        blockers.append(f"subject_bridge_outcome_invalid:{evidence.subject_bridge_outcome}")

    if evidence.closed_reason_code is not None and (
        evidence.closed_reason_code not in CLOSED_REASON_CODES
    ):
        blockers.append(f"closed_reason_unknown:{evidence.closed_reason_code}")

    fact_schema_ok = False
    sanitizer_ok = False
    sanitizer_blocker: Optional[str] = None
    if evidence.layer0_fact_record is None:
        pass
    else:
        fact_schema_ok, fact_blocker = validate_layer0_fact_schema(
            evidence.layer0_fact_record,
        )
        if not fact_schema_ok and fact_blocker:
            blockers.append(fact_blocker)
        elif fact_schema_ok:
            sanitizer_ok, sanitizer_blocker = verify_layer0_fact_sanitized(
                evidence.layer0_fact_record,
            )

    invalid_blockers = list(blockers)
    sanitization_blockers: List[str] = []
    if evidence.layer0_fact_record is None:
        sanitization_blockers.append("layer0_fact_absent")
    elif not sanitizer_ok:
        if sanitizer_blocker:
            sanitization_blockers.append(sanitizer_blocker)

    missing = _missing_checklist_items(evidence.manual_checklist_attested)
    checklist_gates: Dict[str, bool] = {
        "shadow_flag_default_off_observed": evidence.shadow_flag_default_off_observed,
        "a1_proof_gate_attested": evidence.a1_proof_gate_attested,
        "manual_checklist_complete": len(missing) == 0,
        "subject_bridge_outcome_valid": (
            evidence.subject_bridge_outcome in SUBJECT_BRIDGE_OUTCOMES
        ),
        "layer0_fact_schema_valid": fact_schema_ok,
        "layer0_fact_sanitizer_ok": sanitizer_ok,
        "budget_within_target_cap": (
            int(evidence.conditional_target_count) <= MAX_CONDITIONAL_TARGETS
        ),
        "order_count_query_within_turn_budget": (
            int(evidence.order_count_query_count) <= MAX_ORDER_COUNT_QUERIES_PER_SHADOW_TURN
        ),
        "usage_evidence_query_within_turn_budget": (
            int(evidence.usage_evidence_query_count)
            <= MAX_USAGE_EVIDENCE_QUERIES_PER_SHADOW_TURN
        ),
        "budget_not_exceeded": not evidence.budget_exceeded,
        "shadow_loader_not_skipped_for_gate": evidence.gate_skipped_reason is None,
    }
    for item in MANUAL_CHECKLIST_ITEMS:
        checklist_gates[f"checklist_item_attested:{item}"] = (
            item in evidence.manual_checklist_attested
        )

    if invalid_blockers:
        return CouponShadowReadinessResult(
            outcome=OUTCOME_BLOCKED_INVALID_INPUT,
            ready_for_manual_shadow_review=False,
            checklist_gates=checklist_gates,
            readiness_blockers=tuple(sorted(set(invalid_blockers))),
            missing_checklist_items=missing,
        )

    if missing:
        incomplete_blockers: List[str] = []
        if not evidence.shadow_flag_default_off_observed:
            incomplete_blockers.append("shadow_flag_not_observed_default_off")
        incomplete_blockers.extend(
            f"checklist_item_missing:{item}" for item in missing
        )
        return CouponShadowReadinessResult(
            outcome=OUTCOME_MANUAL_CHECKLIST_INCOMPLETE,
            ready_for_manual_shadow_review=False,
            checklist_gates=checklist_gates,
            readiness_blockers=tuple(incomplete_blockers),
            missing_checklist_items=missing,
        )

    if sanitization_blockers:
        return CouponShadowReadinessResult(
            outcome=OUTCOME_BLOCKED_SANITIZATION,
            ready_for_manual_shadow_review=False,
            checklist_gates=checklist_gates,
            readiness_blockers=tuple(sorted(set(sanitization_blockers))),
            missing_checklist_items=(),
        )

    if not evidence.shadow_flag_default_off_observed:
        a1_blockers = ["shadow_flag_not_observed_default_off"]
        return CouponShadowReadinessResult(
            outcome=OUTCOME_BLOCKED_A1_ATTESTATION,
            ready_for_manual_shadow_review=False,
            checklist_gates=checklist_gates,
            readiness_blockers=tuple(a1_blockers),
            missing_checklist_items=(),
        )

    subject_blockers: List[str] = []
    if not evidence.a1_proof_gate_attested:
        subject_blockers.append("a1_proof_gate_not_attested")

    if evidence.subject_bridge_outcome == SUBJECT_BRIDGE_OUTCOME_AMBIGUOUS:
        subject_blockers.append("subject_bridge_ambiguous")
    elif evidence.subject_bridge_outcome != SUBJECT_BRIDGE_OUTCOME_RESOLVED:
        subject_blockers.append("subject_bridge_unresolved")
    if (
        evidence.closed_reason_code
        and evidence.closed_reason_code in SUBJECT_PROOF_CLOSED_REASONS
    ):
        subject_blockers.append(f"closed_reason:{evidence.closed_reason_code}")

    if subject_blockers:
        return CouponShadowReadinessResult(
            outcome=OUTCOME_BLOCKED_SUBJECT_PROOF,
            ready_for_manual_shadow_review=False,
            checklist_gates=checklist_gates,
            readiness_blockers=tuple(sorted(set(subject_blockers))),
            missing_checklist_items=(),
        )

    budget_blockers: List[str] = []
    if evidence.budget_exceeded:
        budget_blockers.append("conditional_target_budget_exceeded")
    if not checklist_gates["budget_within_target_cap"]:
        budget_blockers.append("conditional_target_count_over_cap")
    if not checklist_gates["order_count_query_within_turn_budget"]:
        budget_blockers.append("order_count_query_budget_exceeded")
    if not checklist_gates["usage_evidence_query_within_turn_budget"]:
        budget_blockers.append("usage_evidence_query_budget_exceeded")
    if evidence.gate_skipped_reason:
        budget_blockers.append(f"gate_skipped:{evidence.gate_skipped_reason}")
    if (
        evidence.closed_reason_code
        and evidence.closed_reason_code in BUDGET_TELEMETRY_CLOSED_REASONS
    ):
        budget_blockers.append(f"closed_reason:{evidence.closed_reason_code}")

    if budget_blockers:
        return CouponShadowReadinessResult(
            outcome=OUTCOME_BLOCKED_BUDGET_TELEMETRY,
            ready_for_manual_shadow_review=False,
            checklist_gates=checklist_gates,
            readiness_blockers=tuple(sorted(set(budget_blockers))),
            missing_checklist_items=(),
        )

    return CouponShadowReadinessResult(
        outcome=OUTCOME_MANUAL_CHECKLIST_COMPLETE,
        ready_for_manual_shadow_review=True,
        checklist_gates=checklist_gates,
        readiness_blockers=(),
        missing_checklist_items=(),
    )


__all__ = [
    "ARTIFACT_KIND",
    "BUDGET_TELEMETRY_CLOSED_REASONS",
    "CouponShadowReadinessEvidence",
    "CouponShadowReadinessResult",
    "INDEPENDENT_VERIFICATION",
    "LAYER0_FACT_RECORD_KEYS",
    "LAYER0_TELEMETRY_KEYS",
    "MANUAL_CHECKLIST_ITEMS",
    "MAX_ORDER_COUNT_QUERIES_PER_SHADOW_TURN",
    "MAX_USAGE_EVIDENCE_QUERIES_PER_SHADOW_TURN",
    "OUTCOME_BLOCKED_A1_ATTESTATION",
    "OUTCOME_BLOCKED_BUDGET_TELEMETRY",
    "OUTCOME_BLOCKED_INVALID_INPUT",
    "OUTCOME_BLOCKED_SANITIZATION",
    "OUTCOME_BLOCKED_SUBJECT_PROOF",
    "OUTCOME_MANUAL_CHECKLIST_COMPLETE",
    "OUTCOME_MANUAL_CHECKLIST_INCOMPLETE",
    "READINESS_SCHEMA_VERSION",
    "SHADOW_READINESS_OUTCOMES",
    "SUBJECT_BRIDGE_OUTCOME_AMBIGUOUS",
    "SUBJECT_BRIDGE_OUTCOME_RESOLVED",
    "SUBJECT_BRIDGE_OUTCOME_UNRESOLVED",
    "SUBJECT_BRIDGE_OUTCOMES",
    "SUBJECT_PROOF_CLOSED_REASONS",
    "build_evidence_from_layer0_observation",
    "evaluate_coupon_shadow_readiness",
    "validate_layer0_fact_schema",
    "validate_layer0_telemetry_schema",
    "verify_layer0_fact_sanitized",
]

"""
customer_conditional_coupon_loader.py
─────────────────────────────────────
Layer 0 read-only conditional-coupon eligibility facts (v8 contract).

No compose, no customer-facing claims, no coupon issuance/redemption.
"""
from __future__ import annotations

import re
import time
from collections import OrderedDict
from typing import Any, Dict, List, Optional, Tuple

from services.order_customer_identity_contract import (
    SOURCE_HISTORY_COMPLETE,
    SOURCE_HISTORY_INCOMPLETE,
    SYNC_HEALTH_DEGRADED,
    SYNC_HEALTH_HEALTHY,
    SYNC_HEALTH_STALE,
)
from services.conversation_a1_subject_read_contract import (
    BoundAuthoritativeA1PolicyProofSnapshot,
)

from .contract import TrustedDomain, TrustedFact, TruthSource
from .customer_conditional_coupon_contract import (
    COMPLETENESS_SOURCE_A1_AUTHORITATIVE,
    COMPLETENESS_UNVERIFIED,
    COMPLETENESS_VERIFIED,
    EVALUATION_CONDITION_SATISFIED,
    EVALUATION_CONDITION_SHORTFALL,
    EVALUATION_REQUIRES_CONTEXT,
    EVALUATION_UNAVAILABLE,
    FACT_DOMAIN,
    IDENTITY_STATUS_AMBIGUOUS,
    IDENTITY_STATUS_RESOLVED,
    IDENTITY_STATUS_UNRESOLVED,
    MAX_CONDITIONAL_TARGETS,
    MIN_ORDERS_STATE_NOT_EVALUATED,
    MIN_ORDERS_STATE_SATISFIED,
    MIN_ORDERS_STATE_SHORTFALL,
    PRIOR_REDEMPTION_EVIDENCE_NOT_APPLICABLE,
    PRIOR_REDEMPTION_EVIDENCE_UNAVAILABLE,
    REASON_COUNT_QUERY_FAILURE,
    REASON_CUSTOMER_UNVERIFIED,
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
    USAGE_POLICY_DECLARATIVE_ONLY,
    USAGE_POLICY_UNAVAILABLE,
    USAGE_POLICY_VERIFIED,
    assert_fact_record_sanitized,
    build_sanitized_fact_record,
    build_sanitized_telemetry,
)
from .customer_conditional_coupon_repository import (
    ConditionalCouponRepositoryError,
    count_countable_orders_for_subject,
    extract_min_orders_threshold,
    row_has_personalised_usage_gate,
    scan_conditional_targets,
)
from .customer_conditional_coupon_subject import (
    ConditionalCouponSubjectHandle,
    bound_proof_snapshot_from_handle,
    customer_scope_for_handle,
    resolve_conditional_coupon_subject_handle,
)
from .customer_conditional_coupon_compose_canary_gate import (
    should_load_customer_conditional_coupon_layer0_for_turn,
)

_CONDITIONAL_COUPON_PATTERNS: Tuple[re.Pattern[str], ...] = tuple(
    re.compile(p, re.IGNORECASE | re.UNICODE)
    for p in (
        r"بعد\s*(?:كم|كام)\s*طلب",
        r"(?:ثلاث|3|٣)\s*طلب",
        r"طلب(?:ات)?\s*(?:مكتمل|مؤهل|سابق)",
        r"كوبون\s*(?:بعد|للعملاء|المخلص|الدائم)",
        r"عرض\s*(?:بعد|للعملاء|المخلص)",
        r"min\s*orders",
        r"order\s*threshold",
        r"loyalty\s*coupon",
        r"conditional\s*coupon",
    )
)

_MAX_TURN_CACHE_ENTRIES = 32
_turn_cache: OrderedDict[
    Tuple[int, str, str],
    Tuple[List[TrustedFact], Dict[str, Any]],
] = OrderedDict()


def should_load_customer_conditional_coupon_facts(
    message: str = "",
    inbound_metadata: Optional[Dict[str, Any]] = None,
) -> bool:
    """Lazy relevance gate — conditional order-count intent only."""
    text = (message or "").strip()
    meta = dict(inbound_metadata or {})
    if meta.get("conditional_coupon_intent") is True:
        return True
    if meta.get("min_orders_for_eligibility") not in (None, "", 0):
        return True
    return any(pattern.search(text) for pattern in _CONDITIONAL_COUPON_PATTERNS)


def _cache_key(
    tenant_id: int,
    handle: Optional[ConditionalCouponSubjectHandle],
    message: str,
) -> Tuple[int, str, str]:
    if handle is None:
        subject_token = "unresolved"
    elif handle.subject_kind == "external_customer_profile":
        subject_token = f"ext:{handle.external_customer_profile_id}"
    else:
        subject_token = f"int:{handle.customer_id}"
    return (int(tenant_id), subject_token, (message or "").strip()[:120])


def _snapshot_policy_closed_reason(
    snapshot: BoundAuthoritativeA1PolicyProofSnapshot,
) -> Optional[str]:
    """Return a closed reason when resolver-issued snapshot fails pre-scan policy gates."""
    if not snapshot.policy_eligibility_ready():
        if snapshot.authoritative_source_history_completeness() != SOURCE_HISTORY_COMPLETE:
            return REASON_ORDER_HISTORY_COVERAGE_INCOMPLETE
        if snapshot.forward_sync_health() == SYNC_HEALTH_STALE:
            return REASON_ORDER_HISTORY_SYNC_STALE
        if snapshot.forward_sync_health() == SYNC_HEALTH_DEGRADED:
            return REASON_ORDER_HISTORY_SYNC_DEGRADED
        return REASON_ORDER_HISTORY_IDENTITY_UNVERIFIED
    if snapshot.authoritative_source_history_completeness() != SOURCE_HISTORY_COMPLETE:
        return REASON_ORDER_HISTORY_COVERAGE_INCOMPLETE
    if snapshot.forward_sync_health() == SYNC_HEALTH_STALE:
        return REASON_ORDER_HISTORY_SYNC_STALE
    if snapshot.forward_sync_health() == SYNC_HEALTH_DEGRADED:
        return REASON_ORDER_HISTORY_SYNC_DEGRADED
    return None


def _completeness_from_snapshot(
    snapshot: BoundAuthoritativeA1PolicyProofSnapshot,
) -> Tuple[str, Optional[str], Optional[str]]:
    if not snapshot.policy_eligibility_ready():
        return COMPLETENESS_UNVERIFIED, None, None
    if snapshot.authoritative_source_history_completeness() != SOURCE_HISTORY_COMPLETE:
        return COMPLETENESS_UNVERIFIED, None, snapshot.forward_sync_health()
    if snapshot.forward_sync_health() != SYNC_HEALTH_HEALTHY:
        return COMPLETENESS_UNVERIFIED, None, snapshot.forward_sync_health()
    return COMPLETENESS_VERIFIED, COMPLETENESS_SOURCE_A1_AUTHORITATIVE, snapshot.forward_sync_health()


def _fail_closed_record(
    *,
    identity_status: str,
    customer_scope: str,
    evaluation_state: str,
    closed_reason_code: str,
    min_orders_state: str = MIN_ORDERS_STATE_NOT_EVALUATED,
    usage_policy_state: str = USAGE_POLICY_UNAVAILABLE,
) -> Dict[str, Any]:
    record = build_sanitized_fact_record(
        identity_status=identity_status,
        customer_scope=customer_scope,
        order_history_completeness=COMPLETENESS_UNVERIFIED,
        order_history_completeness_source=None,
        completed_orders_count=None,
        min_orders_for_eligibility=None,
        orders_shortfall=None,
        min_orders_condition_state=min_orders_state,
        prior_redemption_evidence_state=PRIOR_REDEMPTION_EVIDENCE_UNAVAILABLE,
        per_customer_usage_policy_state=usage_policy_state,
        conditional_coupon_evaluation_state=evaluation_state,
        closed_reason_code=closed_reason_code,
        allow_min_orders_condition_claim=False,
    )
    assert_fact_record_sanitized(record)
    return record


def load_customer_conditional_coupon_facts(
    *,
    db: Any,
    tenant_id: int,
    message: str = "",
    conversation: Any = None,
    inbound_metadata: Optional[Dict[str, Any]] = None,
    customer_phone: str = "",
    ai_settings: Optional[Dict[str, Any]] = None,
) -> Tuple[List[TrustedFact], Dict[str, Any]]:
    """
    Load sanitized conditional-coupon Layer 0 facts.

    Inert unless shadow flag + relevance gate pass.

    I/O budget after both gates and a resolved subject:
    - Subject resolution: one Platform bridge read (one binding query + one A1
      proof build) that issues the bound proof snapshot consumed here.
    - Loader: no separate A1 proof read. Snapshot policy gates run before any
      promotion scan. Only when they pass: one eligible-target scan and at most
      one subject-scoped count query.
    - The bounded cache is keyed by tenant plus authoritative subject scope and
      never enters facts or telemetry.
    """
    started = time.perf_counter()

    should_load, gate_skipped_reason = should_load_customer_conditional_coupon_layer0_for_turn(
        tenant_id=int(tenant_id),
        customer_phone=customer_phone,
        message=message,
        inbound_metadata=inbound_metadata,
        ai_settings=ai_settings,
    )
    if not should_load:
        return [], build_sanitized_telemetry(
            conditional_target_count=0,
            order_history_completeness=COMPLETENESS_UNVERIFIED,
            forward_sync_health=None,
            source_contract_version=None,
            order_count_query_count=0,
            usage_evidence_query_count=0,
            budget_exceeded=False,
            loader_duration_ms=int((time.perf_counter() - started) * 1000),
            gate_skipped_reason=gate_skipped_reason,
        )

    resolution = resolve_conditional_coupon_subject_handle(
        tenant_id=int(tenant_id),
        db=db,
        conversation=conversation,
        inbound_metadata=inbound_metadata,
    )
    cache_key = _cache_key(int(tenant_id), resolution.handle, message)
    cached = _turn_cache.get(cache_key)
    if cached is not None:
        _turn_cache.move_to_end(cache_key)
        return cached

    customer_scope = (
        customer_scope_for_handle(resolution.handle)
        if resolution.handle is not None
        else "unresolved"
    )

    if resolution.status == IDENTITY_STATUS_AMBIGUOUS:
        record = _fail_closed_record(
            identity_status=IDENTITY_STATUS_AMBIGUOUS,
            customer_scope=customer_scope,
            evaluation_state=EVALUATION_REQUIRES_CONTEXT,
            closed_reason_code=REASON_SUBJECT_AMBIGUOUS,
        )
        return _finalize(record, obs_base={
            "conditional_target_count": 0,
            "order_history_completeness": COMPLETENESS_UNVERIFIED,
            "forward_sync_health": None,
            "source_contract_version": None,
            "order_count_query_count": 0,
            "usage_evidence_query_count": 0,
            "budget_exceeded": False,
            "loader_duration_ms": int((time.perf_counter() - started) * 1000),
        }, cache_key=cache_key)

    if resolution.status != IDENTITY_STATUS_RESOLVED or resolution.handle is None:
        record = _fail_closed_record(
            identity_status=IDENTITY_STATUS_UNRESOLVED,
            customer_scope=customer_scope,
            evaluation_state=EVALUATION_REQUIRES_CONTEXT,
            closed_reason_code=REASON_CUSTOMER_UNVERIFIED,
        )
        return _finalize(record, obs_base={
            "conditional_target_count": 0,
            "order_history_completeness": COMPLETENESS_UNVERIFIED,
            "forward_sync_health": None,
            "source_contract_version": None,
            "order_count_query_count": 0,
            "usage_evidence_query_count": 0,
            "budget_exceeded": False,
            "loader_duration_ms": int((time.perf_counter() - started) * 1000),
        }, cache_key=cache_key)

    handle = resolution.handle
    if db is None:
        record = _fail_closed_record(
            identity_status=IDENTITY_STATUS_RESOLVED,
            customer_scope=customer_scope,
            evaluation_state=EVALUATION_UNAVAILABLE,
            closed_reason_code=REASON_LOADER_FAILURE,
        )
        return _finalize(record, obs_base={
            "conditional_target_count": 0,
            "order_history_completeness": COMPLETENESS_UNVERIFIED,
            "forward_sync_health": None,
            "source_contract_version": None,
            "order_count_query_count": 0,
            "usage_evidence_query_count": 0,
            "budget_exceeded": False,
            "loader_duration_ms": int((time.perf_counter() - started) * 1000),
        }, cache_key=cache_key)

    proof_snapshot = bound_proof_snapshot_from_handle(handle)
    if proof_snapshot is None:
        record = _fail_closed_record(
            identity_status=IDENTITY_STATUS_RESOLVED,
            customer_scope=customer_scope,
            evaluation_state=EVALUATION_REQUIRES_CONTEXT,
            closed_reason_code=REASON_PROOF_ABSENT,
        )
        return _finalize(record, obs_base={
            "conditional_target_count": 0,
            "order_history_completeness": COMPLETENESS_UNVERIFIED,
            "forward_sync_health": None,
            "source_contract_version": None,
            "order_count_query_count": 0,
            "usage_evidence_query_count": 0,
            "budget_exceeded": False,
            "loader_duration_ms": int((time.perf_counter() - started) * 1000),
        }, cache_key=cache_key)

    completeness, completeness_source, forward_health = _completeness_from_snapshot(proof_snapshot)
    source_contract_version = proof_snapshot.identity_namespace()
    policy_closed_reason = _snapshot_policy_closed_reason(proof_snapshot)
    if policy_closed_reason is not None:
        record = _fail_closed_record(
            identity_status=IDENTITY_STATUS_RESOLVED,
            customer_scope=customer_scope,
            evaluation_state=EVALUATION_REQUIRES_CONTEXT,
            closed_reason_code=policy_closed_reason,
        )
        record["order_history_completeness"] = completeness
        record["order_history_completeness_source"] = completeness_source
        assert_fact_record_sanitized(record)
        return _finalize(record, obs_base={
            "conditional_target_count": 0,
            "order_history_completeness": completeness,
            "forward_sync_health": forward_health,
            "source_contract_version": source_contract_version,
            "order_count_query_count": 0,
            "usage_evidence_query_count": 0,
            "budget_exceeded": False,
            "loader_duration_ms": int((time.perf_counter() - started) * 1000),
        }, cache_key=cache_key)

    usage_query_count = 0
    order_count_query_count = 0
    try:
        all_targets = scan_conditional_targets(
            db,
            tenant_id=int(tenant_id),
            limit=MAX_CONDITIONAL_TARGETS + 1,
        )
        usage_query_count = 1
    except ConditionalCouponRepositoryError:
        record = _fail_closed_record(
            identity_status=IDENTITY_STATUS_RESOLVED,
            customer_scope=customer_scope,
            evaluation_state=EVALUATION_UNAVAILABLE,
            closed_reason_code=REASON_LOADER_FAILURE,
        )
        return _finalize(record, obs_base={
            "conditional_target_count": 0,
            "order_history_completeness": COMPLETENESS_UNVERIFIED,
            "forward_sync_health": None,
            "source_contract_version": None,
            "order_count_query_count": 0,
            "usage_evidence_query_count": 0,
            "budget_exceeded": False,
            "loader_duration_ms": int((time.perf_counter() - started) * 1000),
        }, cache_key=cache_key)

    conditional_target_count = len(all_targets)
    if conditional_target_count == 0:
        record = _fail_closed_record(
            identity_status=IDENTITY_STATUS_RESOLVED,
            customer_scope=customer_scope,
            evaluation_state=EVALUATION_REQUIRES_CONTEXT,
            closed_reason_code=REASON_NO_CONDITIONAL_TARGET,
            usage_policy_state=USAGE_POLICY_UNAVAILABLE,
        )
        record["prior_redemption_evidence_state"] = PRIOR_REDEMPTION_EVIDENCE_NOT_APPLICABLE
        assert_fact_record_sanitized(record)
        return _finalize(record, obs_base={
            "conditional_target_count": 0,
            "order_history_completeness": COMPLETENESS_UNVERIFIED,
            "forward_sync_health": None,
            "source_contract_version": None,
            "order_count_query_count": 0,
            "usage_evidence_query_count": usage_query_count,
            "budget_exceeded": False,
            "loader_duration_ms": int((time.perf_counter() - started) * 1000),
        }, cache_key=cache_key)

    if conditional_target_count > MAX_CONDITIONAL_TARGETS:
        record = _fail_closed_record(
            identity_status=IDENTITY_STATUS_RESOLVED,
            customer_scope=customer_scope,
            evaluation_state=EVALUATION_REQUIRES_CONTEXT,
            closed_reason_code=REASON_TARGET_BUDGET_EXCEEDED,
        )
        return _finalize(record, obs_base={
            "conditional_target_count": conditional_target_count,
            "order_history_completeness": COMPLETENESS_UNVERIFIED,
            "forward_sync_health": None,
            "source_contract_version": None,
            "order_count_query_count": 0,
            "usage_evidence_query_count": usage_query_count,
            "budget_exceeded": True,
            "loader_duration_ms": int((time.perf_counter() - started) * 1000),
        }, cache_key=cache_key)

    thresholds = [
        value
        for value in (extract_min_orders_threshold(row) for row in all_targets[:MAX_CONDITIONAL_TARGETS])
        if value is not None
    ]
    min_threshold = min(thresholds) if thresholds else None
    has_personalised_gate = any(
        row_has_personalised_usage_gate(row)
        for row in all_targets[:MAX_CONDITIONAL_TARGETS]
    )

    try:
        completed_count = count_countable_orders_for_subject(db, handle=handle)
        order_count_query_count = 1
    except ConditionalCouponRepositoryError:
        record = _fail_closed_record(
            identity_status=IDENTITY_STATUS_RESOLVED,
            customer_scope=customer_scope,
            evaluation_state=EVALUATION_UNAVAILABLE,
            closed_reason_code=REASON_COUNT_QUERY_FAILURE,
        )
        record["order_history_completeness"] = completeness
        record["order_history_completeness_source"] = completeness_source
        assert_fact_record_sanitized(record)
        return _finalize(record, obs_base={
            "conditional_target_count": conditional_target_count,
            "order_history_completeness": completeness,
            "forward_sync_health": forward_health,
            "source_contract_version": source_contract_version,
            "order_count_query_count": 0,
            "usage_evidence_query_count": usage_query_count,
            "budget_exceeded": False,
            "loader_duration_ms": int((time.perf_counter() - started) * 1000),
        }, cache_key=cache_key)

    usage_policy_state = (
        USAGE_POLICY_DECLARATIVE_ONLY if has_personalised_gate else USAGE_POLICY_VERIFIED
    )
    min_orders_state = MIN_ORDERS_STATE_NOT_EVALUATED
    evaluation_state = EVALUATION_REQUIRES_CONTEXT
    closed_reason: Optional[str] = None
    orders_shortfall: Optional[int] = None
    allow_claim = False

    if min_threshold is None:
        min_orders_state = MIN_ORDERS_STATE_NOT_EVALUATED
        evaluation_state = EVALUATION_REQUIRES_CONTEXT
        closed_reason = REASON_NO_CONDITIONAL_TARGET
    elif completed_count >= min_threshold:
        min_orders_state = MIN_ORDERS_STATE_SATISFIED
        evaluation_state = EVALUATION_CONDITION_SATISFIED
        if (
            completeness == COMPLETENESS_VERIFIED
            and forward_health == SYNC_HEALTH_HEALTHY
            and usage_policy_state == USAGE_POLICY_VERIFIED
        ):
            allow_claim = True
    else:
        min_orders_state = MIN_ORDERS_STATE_SHORTFALL
        evaluation_state = EVALUATION_CONDITION_SHORTFALL
        orders_shortfall = int(min_threshold - completed_count)
        closed_reason = REASON_ORDERS_SHORTFALL

    record = build_sanitized_fact_record(
        identity_status=IDENTITY_STATUS_RESOLVED,
        customer_scope=customer_scope,
        order_history_completeness=completeness,
        order_history_completeness_source=completeness_source,
        completed_orders_count=int(completed_count),
        min_orders_for_eligibility=min_threshold,
        orders_shortfall=orders_shortfall,
        min_orders_condition_state=min_orders_state,
        prior_redemption_evidence_state=PRIOR_REDEMPTION_EVIDENCE_NOT_APPLICABLE,
        per_customer_usage_policy_state=usage_policy_state,
        conditional_coupon_evaluation_state=evaluation_state,
        closed_reason_code=closed_reason,
        allow_min_orders_condition_claim=allow_claim,
    )
    assert_fact_record_sanitized(record)
    return _finalize(record, obs_base={
        "conditional_target_count": conditional_target_count,
        "order_history_completeness": completeness,
        "forward_sync_health": forward_health,
        "source_contract_version": source_contract_version,
        "order_count_query_count": order_count_query_count,
        "usage_evidence_query_count": usage_query_count,
        "budget_exceeded": False,
        "loader_duration_ms": int((time.perf_counter() - started) * 1000),
    }, cache_key=cache_key)


def _finalize(
    record: Dict[str, Any],
    *,
    obs_base: Dict[str, Any],
    cache_key: Tuple[int, str, str],
) -> Tuple[List[TrustedFact], Dict[str, Any]]:
    facts = [
        TrustedFact(
            domain=TrustedDomain.CUSTOMER_CONDITIONAL_COUPON,
            key=f"{FACT_DOMAIN}:eligibility",
            value=record,
            source=TruthSource.PROMOTION_TABLE,
            path="customer_conditional_coupon_loader.layer0",
        )
    ]
    telemetry = build_sanitized_telemetry(
        conditional_target_count=int(obs_base.get("conditional_target_count") or 0),
        order_history_completeness=str(obs_base.get("order_history_completeness") or COMPLETENESS_UNVERIFIED),
        forward_sync_health=obs_base.get("forward_sync_health"),
        source_contract_version=obs_base.get("source_contract_version"),
        order_count_query_count=int(obs_base.get("order_count_query_count") or 0),
        usage_evidence_query_count=int(obs_base.get("usage_evidence_query_count") or 0),
        budget_exceeded=bool(obs_base.get("budget_exceeded")),
        loader_duration_ms=int(obs_base.get("loader_duration_ms") or 0),
        gate_skipped_reason=obs_base.get("gate_skipped_reason"),
    )
    _turn_cache[cache_key] = (facts, telemetry)
    _turn_cache.move_to_end(cache_key)
    while len(_turn_cache) > _MAX_TURN_CACHE_ENTRIES:
        _turn_cache.popitem(last=False)
    return facts, telemetry


def clear_customer_conditional_coupon_turn_cache() -> None:
    _turn_cache.clear()


__all__ = [
    "clear_customer_conditional_coupon_turn_cache",
    "load_customer_conditional_coupon_facts",
    "should_load_customer_conditional_coupon_facts",
]

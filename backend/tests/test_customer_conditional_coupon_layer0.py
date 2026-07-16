"""Layer 0 customer_conditional_coupon facts — contract, loader, repository tests."""
from __future__ import annotations

import os
import re
import sys
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from unittest.mock import MagicMock, patch
from uuid import uuid4

import pytest

_HERE = os.path.dirname(os.path.abspath(__file__))
_BACKEND = os.path.abspath(os.path.join(_HERE, ".."))
if _BACKEND not in sys.path:
    sys.path.insert(0, _BACKEND)

from modules.ai.brain.truth_surface.contract import (  # noqa: E402
    TrustedDomain,
    TrustedFact,
    TruthSource,
)
from modules.ai.brain.truth_surface.customer_conditional_coupon_contract import (  # noqa: E402
    COMPLETENESS_UNVERIFIED,
    COMPLETENESS_VERIFIED,
    EVALUATION_CONDITION_SATISFIED,
    EVALUATION_CONDITION_SHORTFALL,
    EVALUATION_REQUIRES_CONTEXT,
    EVALUATION_UNAVAILABLE,
    FORBIDDEN_FACT_KEYS,
    MIN_ORDERS_STATE_SATISFIED,
    MIN_ORDERS_STATE_SHORTFALL,
    REASON_CUSTOMER_UNVERIFIED,
    REASON_COUNT_QUERY_FAILURE,
    REASON_ORDER_HISTORY_IDENTITY_UNVERIFIED,
    REASON_ORDER_HISTORY_SYNC_DEGRADED,
    REASON_ORDER_HISTORY_SYNC_STALE,
    REASON_PROOF_ABSENT,
    REASON_TARGET_BUDGET_EXCEEDED,
    assert_fact_record_sanitized,
    build_sanitized_fact_record,
)
from modules.ai.brain.truth_surface.customer_conditional_coupon_loader import (  # noqa: E402
    clear_customer_conditional_coupon_turn_cache,
    load_customer_conditional_coupon_facts,
    should_load_customer_conditional_coupon_facts,
)
from modules.ai.brain.truth_surface.customer_conditional_coupon_repository import (  # noqa: E402
    ConditionalCouponRepositoryError,
    count_countable_orders_for_subject,
    extract_min_orders_threshold,
    promotion_liveness_sql_predicate,
)
from modules.ai.brain.truth_surface.customer_conditional_coupon_subject import (  # noqa: E402
    HANDLE_SOURCE_BRIDGE,
    ConditionalCouponSubjectHandle,
    SubjectResolutionResult,
    resolve_conditional_coupon_subject_handle,
)
from modules.ai.brain.truth_surface.trusted_context import (  # noqa: E402
    build_trusted_context_snapshot,
)
from services.conversation_a1_subject_binding_contract import (  # noqa: E402
    BINDING_SOURCE_WA_ORDER_BRIDGE_AUTHORITATIVE_INTERNAL,
    SUBJECT_KIND_NAHL_INTERNAL_CUSTOMER,
)
from services.conversation_a1_subject_read_contract import (  # noqa: E402
    _issue_authoritative_a1_subject_pair,
)
from services.order_customer_identity_contract import (  # noqa: E402
    EVIDENCE_AUTHORITATIVE,
    EXTERNAL_PROVIDER_SALLA_V1,
    NAHLA_INTERNAL_ORDER_V1,
    SOURCE_HISTORY_COMPLETE,
    SOURCE_HISTORY_INCOMPLETE,
    SYNC_HEALTH_HEALTHY,
    SYNC_HEALTH_DEGRADED,
    SYNC_HEALTH_STALE,
)


@pytest.fixture(autouse=True)
def _clear_turn_cache() -> None:
    clear_customer_conditional_coupon_turn_cache()


def _resolver_proof_snapshot(
    *,
    ready: bool = True,
    forward_health: str = SYNC_HEALTH_HEALTHY,
    completeness: str = SOURCE_HISTORY_COMPLETE,
) -> SimpleNamespace:
    return SimpleNamespace(
        subject_kind=SUBJECT_KIND_NAHL_INTERNAL_CUSTOMER,
        identity_namespace=NAHLA_INTERNAL_ORDER_V1,
        policy_eligibility_ready=ready,
        authoritative_source_history_completeness=completeness,
        forward_sync_health=forward_health,
    )


def _promo(min_orders: int, *, tenant_id: int = 1) -> SimpleNamespace:
    return SimpleNamespace(
        id=101,
        tenant_id=tenant_id,
        conditions={"min_orders_for_eligibility": min_orders},
        extra_metadata={},
        status="active",
        starts_at=None,
        ends_at=None,
        usage_count=0,
        usage_limit=None,
    )


def _trusted_bridge_resolution(
    *,
    tenant_id: int = 1,
    customer_id: int = 55,
    conversation_id: int = 1,
    ready: bool = True,
    forward_health: str = SYNC_HEALTH_HEALTHY,
    completeness: str = SOURCE_HISTORY_COMPLETE,
    include_bound_scope: bool = True,
) -> SubjectResolutionResult:
    bridge_handle, bound_scope = _issue_authoritative_a1_subject_pair(
        binding_key=uuid4(),
        tenant_id=tenant_id,
        conversation_id=conversation_id,
        subject_kind=SUBJECT_KIND_NAHL_INTERNAL_CUSTOMER,
        identity_namespace=NAHLA_INTERNAL_ORDER_V1,
        binding_source=BINDING_SOURCE_WA_ORDER_BRIDGE_AUTHORITATIVE_INTERNAL,
        binding_evidence_class=EVIDENCE_AUTHORITATIVE,
        proof=_resolver_proof_snapshot(
            ready=ready,
            forward_health=forward_health,
            completeness=completeness,
        ),
        internal_customer_id=customer_id,
    )
    return SubjectResolutionResult(
        status="resolved",
        handle=ConditionalCouponSubjectHandle(
            subject_kind="nahla_internal_customer",
            tenant_id=tenant_id,
            identity_namespace=NAHLA_INTERNAL_ORDER_V1,
            handle_source=HANDLE_SOURCE_BRIDGE,
            customer_id=customer_id,
            authoritative_a1_subject_handle=bridge_handle,
            bound_authoritative_a1_subject_scope=bound_scope if include_bound_scope else None,
        ),
    )


def test_should_load_conditional_intent_generic_merchant() -> None:
    assert should_load_customer_conditional_coupon_facts(
        message="متى يصل كوبون بعد كم طلب؟",
    )
    assert should_load_customer_conditional_coupon_facts(
        message="هل عندكم عرض loyalty coupon بعد 3 orders؟",
    )


def test_shadow_flag_default_off_no_io(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("NAHLA_TRUSTED_CONTEXT_CUSTOMER_CONDITIONAL_COUPON_SHADOW_ENABLED", raising=False)
    facts, obs = load_customer_conditional_coupon_facts(
        db=MagicMock(),
        tenant_id=1,
        message="بعد كم طلب يصل الكوبون؟",
        conversation=SimpleNamespace(customer_id=9),
    )
    assert facts == []
    assert obs["gate_skipped_reason"] == "shadow_flag_disabled"
    assert obs["order_count_query_count"] == 0


def test_not_relevant_skips_db(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("NAHLA_TRUSTED_CONTEXT_CUSTOMER_CONDITIONAL_COUPON_SHADOW_ENABLED", "true")
    db = MagicMock()
    facts, obs = load_customer_conditional_coupon_facts(
        db=db,
        tenant_id=1,
        message="مرحبا",
    )
    assert facts == []
    assert obs["gate_skipped_reason"] == "not_relevant"
    db.query.assert_not_called()


def test_unresolved_subject_fail_closed(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("NAHLA_TRUSTED_CONTEXT_CUSTOMER_CONDITIONAL_COUPON_SHADOW_ENABLED", "true")
    with patch(
        "modules.ai.brain.truth_surface.customer_conditional_coupon_loader.scan_conditional_targets",
        return_value=[_promo(3)],
    ):
        facts, _obs = load_customer_conditional_coupon_facts(
            db=MagicMock(),
            tenant_id=1,
            message="بعد كم طلب؟",
        )
    record = facts[0].value
    assert record["identity_status"] == "unresolved"
    assert record["closed_reason_code"] == REASON_CUSTOMER_UNVERIFIED
    assert record["allow_min_orders_condition_claim"] is False
    assert record["completed_orders_count"] is None


def test_untrusted_inbound_metadata_is_ignored_fail_closed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("NAHLA_TRUSTED_CONTEXT_CUSTOMER_CONDITIONAL_COUPON_SHADOW_ENABLED", "true")
    facts, _obs = load_customer_conditional_coupon_facts(
        db=MagicMock(),
        tenant_id=1,
        message="بعد كم طلب؟",
        inbound_metadata={
            "customer_id": 55,
            "external_customer_profile_id": str(uuid4()),
        },
    )
    record = facts[0].value
    assert record["identity_status"] == "unresolved"
    assert record["closed_reason_code"] == REASON_CUSTOMER_UNVERIFIED


def test_internal_subject_shortfall_generic_commerce(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("NAHLA_TRUSTED_CONTEXT_CUSTOMER_CONDITIONAL_COUPON_SHADOW_ENABLED", "true")
    db = MagicMock()
    convo = SimpleNamespace(customer_id=55)
    with patch(
        "modules.ai.brain.truth_surface.customer_conditional_coupon_loader.scan_conditional_targets",
        return_value=[_promo(3)],
    ), patch(
        "modules.ai.brain.truth_surface.customer_conditional_coupon_loader.resolve_conditional_coupon_subject_handle",
        return_value=_trusted_bridge_resolution(),
    ), patch(
        "modules.ai.brain.truth_surface.customer_conditional_coupon_loader.count_countable_orders_for_subject",
        return_value=1,
    ):
        facts, obs = load_customer_conditional_coupon_facts(
            db=db,
            tenant_id=1,
            message="بعد كم طلب يصل كوبون متجر تجريبي عام؟",
            conversation=convo,
        )
    record = facts[0].value
    assert record["customer_scope"] == "nahla_internal_customer"
    assert record["order_history_completeness"] == COMPLETENESS_VERIFIED
    assert record["completed_orders_count"] == 1
    assert record["min_orders_for_eligibility"] == 3
    assert record["orders_shortfall"] == 2
    assert record["min_orders_condition_state"] == MIN_ORDERS_STATE_SHORTFALL
    assert record["conditional_coupon_evaluation_state"] == EVALUATION_CONDITION_SHORTFALL
    assert record["allow_min_orders_condition_claim"] is False
    assert obs["order_count_query_count"] == 1


def test_happy_path_claim_requires_all_authoritative_gates(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Satisfied min-orders is Layer 0 evidence, never final coupon eligibility."""
    monkeypatch.setenv("NAHLA_TRUSTED_CONTEXT_CUSTOMER_CONDITIONAL_COUPON_SHADOW_ENABLED", "true")
    active_target = _promo(2)
    active_target.starts_at = datetime.now(timezone.utc) - timedelta(minutes=1)
    active_target.ends_at = datetime.now(timezone.utc) + timedelta(minutes=1)
    active_target.usage_limit = 4
    active_target.usage_count = 1
    with patch(
        "modules.ai.brain.truth_surface.customer_conditional_coupon_loader.resolve_conditional_coupon_subject_handle",
        return_value=_trusted_bridge_resolution(),
    ), patch(
        "modules.ai.brain.truth_surface.customer_conditional_coupon_loader.scan_conditional_targets",
        return_value=[active_target],
    ), patch(
        "modules.ai.brain.truth_surface.customer_conditional_coupon_loader.count_countable_orders_for_subject",
        return_value=2,
    ):
        facts, _obs = load_customer_conditional_coupon_facts(
            db=MagicMock(), tenant_id=1, message="بعد كم طلب?",
        )
    record = facts[0].value
    assert record["min_orders_condition_state"] == MIN_ORDERS_STATE_SATISFIED
    assert record["conditional_coupon_evaluation_state"] == EVALUATION_CONDITION_SATISFIED
    assert record["per_customer_usage_policy_state"] == "verified"
    assert record["allow_min_orders_condition_claim"] is True
    assert "overall_eligibility_state" not in record
    assert "eligible" not in record
    assert all(not isinstance(value, str) or "\n" not in value for value in record.values())


def test_external_subject_unresolved_until_trusted_bridge(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("NAHLA_TRUSTED_CONTEXT_CUSTOMER_CONDITIONAL_COUPON_SHADOW_ENABLED", "true")
    facts, _obs = load_customer_conditional_coupon_facts(
        db=MagicMock(),
        tenant_id=1,
        message="conditional coupon after min orders",
        inbound_metadata={"external_customer_profile_id": str(uuid4())},
    )
    record = facts[0].value
    assert record["customer_scope"] == "unresolved"
    assert record["identity_status"] == "unresolved"
    assert record["allow_min_orders_condition_claim"] is False


def test_proof_not_ready_fail_closed(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("NAHLA_TRUSTED_CONTEXT_CUSTOMER_CONDITIONAL_COUPON_SHADOW_ENABLED", "true")
    with patch(
        "modules.ai.brain.truth_surface.customer_conditional_coupon_loader.scan_conditional_targets",
    ) as discover, patch(
        "modules.ai.brain.truth_surface.customer_conditional_coupon_loader.resolve_conditional_coupon_subject_handle",
        return_value=_trusted_bridge_resolution(ready=False),
    ), patch(
        "modules.ai.brain.truth_surface.customer_conditional_coupon_loader.count_countable_orders_for_subject",
    ) as count_mock:
        facts, obs = load_customer_conditional_coupon_facts(
            db=MagicMock(),
            tenant_id=1,
            message="بعد كم طلب؟",
            conversation=SimpleNamespace(customer_id=1),
        )
    discover.assert_not_called()
    count_mock.assert_not_called()
    record = facts[0].value
    assert record["order_history_completeness"] == COMPLETENESS_UNVERIFIED
    assert record["closed_reason_code"] == REASON_ORDER_HISTORY_IDENTITY_UNVERIFIED
    assert record["completed_orders_count"] is None
    assert obs["order_count_query_count"] == 0
    assert obs["usage_evidence_query_count"] == 0


def test_target_budget_exceeded_not_unavailable(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("NAHLA_TRUSTED_CONTEXT_CUSTOMER_CONDITIONAL_COUPON_SHADOW_ENABLED", "true")
    targets = [_promo(i) for i in range(1, 8)]
    with patch(
        "modules.ai.brain.truth_surface.customer_conditional_coupon_loader.scan_conditional_targets",
        return_value=targets,
    ), patch(
        "modules.ai.brain.truth_surface.customer_conditional_coupon_loader.resolve_conditional_coupon_subject_handle",
        return_value=_trusted_bridge_resolution(),
    ):
        facts, obs = load_customer_conditional_coupon_facts(
            db=MagicMock(),
            tenant_id=1,
            message="بعد كم طلب؟",
            conversation=SimpleNamespace(customer_id=1),
        )
    record = facts[0].value
    assert record["conditional_coupon_evaluation_state"] == EVALUATION_REQUIRES_CONTEXT
    assert record["closed_reason_code"] == REASON_TARGET_BUDGET_EXCEEDED
    assert record["conditional_coupon_evaluation_state"] != EVALUATION_UNAVAILABLE
    assert obs["budget_exceeded"] is True


def test_turn_dedup_single_count_query(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("NAHLA_TRUSTED_CONTEXT_CUSTOMER_CONDITIONAL_COUPON_SHADOW_ENABLED", "true")
    count_mock = MagicMock(return_value=2)
    with patch(
        "modules.ai.brain.truth_surface.customer_conditional_coupon_loader.scan_conditional_targets",
        return_value=[_promo(2)],
    ), patch(
        "modules.ai.brain.truth_surface.customer_conditional_coupon_loader.resolve_conditional_coupon_subject_handle",
        return_value=_trusted_bridge_resolution(customer_id=3),
    ), patch(
        "modules.ai.brain.truth_surface.customer_conditional_coupon_loader.count_countable_orders_for_subject",
        count_mock,
    ):
        load_customer_conditional_coupon_facts(
            db=MagicMock(),
            tenant_id=1,
            message="بعد كم طلب؟",
            conversation=SimpleNamespace(customer_id=3),
        )
        load_customer_conditional_coupon_facts(
            db=MagicMock(),
            tenant_id=1,
            message="بعد كم طلب؟",
            conversation=SimpleNamespace(customer_id=3),
        )
    assert count_mock.call_count == 1


def test_subject_resolution_requires_separately_owned_authoritative_bridge() -> None:
    internal_only = resolve_conditional_coupon_subject_handle(
        tenant_id=1,
        conversation=SimpleNamespace(customer_id=10, id=5),
    )
    assert internal_only.status == "unresolved"
    assert internal_only.handle is None
    assert internal_only.reason_code == "authoritative_subject_handle_unavailable"

    external_only = resolve_conditional_coupon_subject_handle(
        tenant_id=1,
        inbound_metadata={"external_customer_profile_id": str(uuid4())},
    )
    assert external_only.status == "unresolved"


def test_fact_record_forbidden_keys_scan() -> None:
    record = build_sanitized_fact_record(
        identity_status="resolved",
        customer_scope="nahla_internal_customer",
        order_history_completeness=COMPLETENESS_VERIFIED,
        order_history_completeness_source="order_customer_fk_a1_authoritative",
        completed_orders_count=2,
        min_orders_for_eligibility=3,
        orders_shortfall=1,
        min_orders_condition_state=MIN_ORDERS_STATE_SHORTFALL,
        prior_redemption_evidence_state="not_applicable",
        per_customer_usage_policy_state="verified",
        conditional_coupon_evaluation_state=EVALUATION_CONDITION_SHORTFALL,
        closed_reason_code="orders_shortfall",
        allow_min_orders_condition_claim=False,
    )
    assert_fact_record_sanitized(record)
    polluted = dict(record)
    polluted["customer_id"] = 9
    with pytest.raises(ValueError, match="forbidden_fact_key"):
        assert_fact_record_sanitized(polluted)
    with pytest.raises(ValueError, match="forbidden_fact_key"):
        assert_fact_record_sanitized({"safe": [{"nested": {"phone": "x"}}]})


def test_telemetry_and_facts_exclude_pii_patterns() -> None:
    record = build_sanitized_fact_record(
        identity_status="resolved",
        customer_scope="external_customer_profile",
        order_history_completeness=COMPLETENESS_UNVERIFIED,
        order_history_completeness_source=None,
        completed_orders_count=None,
        min_orders_for_eligibility=None,
        orders_shortfall=None,
        min_orders_condition_state="not_evaluated",
        prior_redemption_evidence_state="unavailable",
        per_customer_usage_policy_state="unavailable",
        conditional_coupon_evaluation_state=EVALUATION_REQUIRES_CONTEXT,
        closed_reason_code=REASON_CUSTOMER_UNVERIFIED,
        allow_min_orders_condition_claim=False,
    )
    blob = str(record)
    for forbidden in FORBIDDEN_FACT_KEYS:
        assert forbidden not in record
    assert not re.search(r"\+966\d{8,}", blob)


def test_repository_uses_shared_countability_predicate() -> None:
    source = open(
        os.path.join(
            _BACKEND,
            "modules",
            "ai",
            "brain",
            "truth_surface",
            "customer_conditional_coupon_repository.py",
        ),
        encoding="utf-8",
    ).read()
    assert "countable_order_sql_predicate" in source
    assert "COUNTABLE_ORDER_STATUSES" not in source


def test_count_query_failure_unavailable(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("NAHLA_TRUSTED_CONTEXT_CUSTOMER_CONDITIONAL_COUPON_SHADOW_ENABLED", "true")
    with patch(
        "modules.ai.brain.truth_surface.customer_conditional_coupon_loader.scan_conditional_targets",
        return_value=[_promo(1)],
    ), patch(
        "modules.ai.brain.truth_surface.customer_conditional_coupon_loader.resolve_conditional_coupon_subject_handle",
        return_value=_trusted_bridge_resolution(),
    ), patch(
        "modules.ai.brain.truth_surface.customer_conditional_coupon_loader.count_countable_orders_for_subject",
        side_effect=ConditionalCouponRepositoryError("db_down"),
    ):
        facts, _obs = load_customer_conditional_coupon_facts(
            db=MagicMock(),
            tenant_id=1,
            message="بعد كم طلب؟",
            conversation=SimpleNamespace(customer_id=1),
        )
    assert facts[0].value["conditional_coupon_evaluation_state"] == EVALUATION_UNAVAILABLE


def test_extract_min_orders_threshold_from_promotion() -> None:
    promo = SimpleNamespace(
        conditions={"min_orders_for_eligibility": 3},
        extra_metadata={},
    )
    assert extract_min_orders_threshold(promo) == 3


def test_trusted_fact_domain_and_no_compose_imports() -> None:
    fact = TrustedFact(
        domain=TrustedDomain.CUSTOMER_CONDITIONAL_COUPON,
        key="customer_conditional_coupon:eligibility",
        value={"domain": "customer_conditional_coupon"},
        source=TruthSource.PROMOTION_TABLE,
        path="test",
    )
    assert fact.domain == TrustedDomain.CUSTOMER_CONDITIONAL_COUPON
    compose_path = os.path.join(_BACKEND, "modules", "ai", "brain", "compose", "responder.py")
    compose_source = open(compose_path, encoding="utf-8").read()
    assert "customer_conditional_coupon" not in compose_source


def test_internal_count_scoped_query() -> None:
    db = MagicMock()
    db.query.return_value.filter.return_value.scalar.return_value = 4
    handle = ConditionalCouponSubjectHandle(
        subject_kind="nahla_internal_customer",
        tenant_id=1,
        identity_namespace=NAHLA_INTERNAL_ORDER_V1,
        handle_source="conversation.customer_id",
        customer_id=77,
    )
    count = count_countable_orders_for_subject(db, handle=handle)
    assert count == 4
    db.query.assert_called_once()


def test_tenant_isolation_external_profile_missing(monkeypatch: pytest.MonkeyPatch) -> None:
    db = MagicMock()
    db.query.return_value.filter.return_value.first.return_value = None
    handle = ConditionalCouponSubjectHandle(
        subject_kind="external_customer_profile",
        tenant_id=2,
        identity_namespace=EXTERNAL_PROVIDER_SALLA_V1,
        handle_source="inbound_metadata.external_customer_profile_id",
        external_customer_profile_id=uuid4(),
    )
    with pytest.raises(ConditionalCouponRepositoryError):
        count_countable_orders_for_subject(db, handle=handle)


@pytest.mark.parametrize(
    ("forward_health", "reason"),
    [
        (SYNC_HEALTH_STALE, REASON_ORDER_HISTORY_SYNC_STALE),
        (SYNC_HEALTH_DEGRADED, REASON_ORDER_HISTORY_SYNC_DEGRADED),
    ],
)
def test_stale_or_degraded_proof_fails_closed(
    monkeypatch: pytest.MonkeyPatch,
    forward_health: str,
    reason: str,
) -> None:
    monkeypatch.setenv("NAHLA_TRUSTED_CONTEXT_CUSTOMER_CONDITIONAL_COUPON_SHADOW_ENABLED", "true")
    with patch(
        "modules.ai.brain.truth_surface.customer_conditional_coupon_loader.resolve_conditional_coupon_subject_handle",
        return_value=_trusted_bridge_resolution(ready=False, forward_health=forward_health),
    ), patch(
        "modules.ai.brain.truth_surface.customer_conditional_coupon_loader.scan_conditional_targets",
    ) as discover, patch(
        "modules.ai.brain.truth_surface.customer_conditional_coupon_loader.count_countable_orders_for_subject",
    ) as count_mock:
        facts, obs = load_customer_conditional_coupon_facts(
            db=MagicMock(),
            tenant_id=1,
            message="بعد كم طلب؟",
        )
    discover.assert_not_called()
    count_mock.assert_not_called()
    assert facts[0].value["closed_reason_code"] == reason
    assert obs["order_count_query_count"] == 0
    assert obs["usage_evidence_query_count"] == 0


def test_absent_proof_fails_closed(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("NAHLA_TRUSTED_CONTEXT_CUSTOMER_CONDITIONAL_COUPON_SHADOW_ENABLED", "true")
    with patch(
        "modules.ai.brain.truth_surface.customer_conditional_coupon_loader.resolve_conditional_coupon_subject_handle",
        return_value=_trusted_bridge_resolution(include_bound_scope=False),
    ), patch(
        "modules.ai.brain.truth_surface.customer_conditional_coupon_loader.scan_conditional_targets",
        return_value=[_promo(2)],
    ):
        facts, _obs = load_customer_conditional_coupon_facts(
            db=MagicMock(), tenant_id=1, message="بعد كم طلب?",
        )
    assert facts[0].value["closed_reason_code"] == REASON_PROOF_ABSENT


def test_target_scan_failure_is_unavailable(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("NAHLA_TRUSTED_CONTEXT_CUSTOMER_CONDITIONAL_COUPON_SHADOW_ENABLED", "true")
    with patch(
        "modules.ai.brain.truth_surface.customer_conditional_coupon_loader.resolve_conditional_coupon_subject_handle",
        return_value=_trusted_bridge_resolution(),
    ), patch(
        "modules.ai.brain.truth_surface.customer_conditional_coupon_loader.scan_conditional_targets",
        side_effect=ConditionalCouponRepositoryError("query_failed"),
    ):
        facts, _obs = load_customer_conditional_coupon_facts(
            db=MagicMock(), tenant_id=1, message="بعد كم طلب?",
        )
    assert facts[0].value["conditional_coupon_evaluation_state"] == EVALUATION_UNAVAILABLE


def test_no_targets_is_not_false_missing_from_unfiltered_scan(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("NAHLA_TRUSTED_CONTEXT_CUSTOMER_CONDITIONAL_COUPON_SHADOW_ENABLED", "true")
    with patch(
        "modules.ai.brain.truth_surface.customer_conditional_coupon_loader.resolve_conditional_coupon_subject_handle",
        return_value=_trusted_bridge_resolution(),
    ), patch(
        "modules.ai.brain.truth_surface.customer_conditional_coupon_loader.scan_conditional_targets",
        return_value=[],
    ):
        facts, obs = load_customer_conditional_coupon_facts(
            db=MagicMock(), tenant_id=1, message="بعد كم طلب?",
        )
    assert facts[0].value["closed_reason_code"] == "no_conditional_target"
    assert obs["usage_evidence_query_count"] == 1


@pytest.mark.parametrize(
    "liveness_case",
    ["draft", "paused", "expired", "future", "usage_exhausted"],
)
def test_non_live_promotion_yields_no_target_and_no_claim(
    monkeypatch: pytest.MonkeyPatch,
    liveness_case: str,
) -> None:
    """
    Discovery excludes every non-live promotion before it reaches the loader.

    The mock models the SQL-side liveness predicate result; the resulting
    Layer 0 record remains a closed no-target state with no claim.
    """
    monkeypatch.setenv("NAHLA_TRUSTED_CONTEXT_CUSTOMER_CONDITIONAL_COUPON_SHADOW_ENABLED", "true")
    with patch(
        "modules.ai.brain.truth_surface.customer_conditional_coupon_loader.resolve_conditional_coupon_subject_handle",
        return_value=_trusted_bridge_resolution(),
    ), patch(
        "modules.ai.brain.truth_surface.customer_conditional_coupon_loader.scan_conditional_targets",
        return_value=[],
    ) as discover:
        facts, _obs = load_customer_conditional_coupon_facts(
            db=MagicMock(), tenant_id=1, message="بعد كم طلب?",
        )
    assert discover.call_count == 1, liveness_case
    assert facts[0].value["closed_reason_code"] == "no_conditional_target"
    assert facts[0].value["allow_min_orders_condition_claim"] is False


def test_personalized_usage_gate_blocks_min_orders_claim(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("NAHLA_TRUSTED_CONTEXT_CUSTOMER_CONDITIONAL_COUPON_SHADOW_ENABLED", "true")
    target = _promo(2)
    target.conditions["customer_segments"] = ["repeat_buyers"]
    with patch(
        "modules.ai.brain.truth_surface.customer_conditional_coupon_loader.resolve_conditional_coupon_subject_handle",
        return_value=_trusted_bridge_resolution(),
    ), patch(
        "modules.ai.brain.truth_surface.customer_conditional_coupon_loader.scan_conditional_targets",
        return_value=[target],
    ), patch(
        "modules.ai.brain.truth_surface.customer_conditional_coupon_loader.count_countable_orders_for_subject",
        return_value=3,
    ):
        facts, _obs = load_customer_conditional_coupon_facts(
            db=MagicMock(), tenant_id=1, message="بعد كم طلب?",
        )
    assert facts[0].value["per_customer_usage_policy_state"] == "declarative_only"
    assert facts[0].value["allow_min_orders_condition_claim"] is False


def test_discovery_is_filtered_ordered_and_overflow_bounded() -> None:
    source = open(
        os.path.join(
            _BACKEND, "modules", "ai", "brain", "truth_surface",
            "customer_conditional_coupon_repository.py",
        ),
        encoding="utf-8",
    ).read()
    assert 'Promotion.conditions["min_orders_for_eligibility"].astext' in source
    assert ".order_by(Promotion.id.asc())" in source
    assert ".limit(int(limit))" in source
    assert "from models import Coupon" not in source
    assert "max(limit * 2, 20)" not in source
    assert "promotion_liveness_sql_predicate(Promotion)" in source


def test_promotion_liveness_predicate_has_engine_parity_fields() -> None:
    from sqlalchemy.dialects import postgresql
    from models import Promotion

    sql = str(
        promotion_liveness_sql_predicate(Promotion).compile(
            dialect=postgresql.dialect(),
            compile_kwargs={"literal_binds": True},
        ),
    )
    assert "promotions.status = 'active'" in sql
    assert "promotions.starts_at" in sql
    assert "promotions.ends_at" in sql
    assert "promotions.usage_limit IS NULL" in sql
    assert "coalesce(promotions.usage_count, 0)" in sql


def test_tenant_cache_isolation(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("NAHLA_TRUSTED_CONTEXT_CUSTOMER_CONDITIONAL_COUPON_SHADOW_ENABLED", "true")
    count_mock = MagicMock(return_value=2)
    with patch(
        "modules.ai.brain.truth_surface.customer_conditional_coupon_loader.resolve_conditional_coupon_subject_handle",
        side_effect=[
            _trusted_bridge_resolution(tenant_id=1, customer_id=55),
            _trusted_bridge_resolution(tenant_id=2, customer_id=55),
        ],
    ), patch(
        "modules.ai.brain.truth_surface.customer_conditional_coupon_loader.scan_conditional_targets",
        return_value=[_promo(2)],
    ), patch(
        "modules.ai.brain.truth_surface.customer_conditional_coupon_loader.count_countable_orders_for_subject",
        count_mock,
    ):
        load_customer_conditional_coupon_facts(db=MagicMock(), tenant_id=1, message="بعد كم طلب?")
        load_customer_conditional_coupon_facts(db=MagicMock(), tenant_id=2, message="بعد كم طلب?")
    assert count_mock.call_count == 2


def test_trusted_context_double_gate_default_off(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("NAHLA_TRUSTED_CONTEXT_CUSTOMER_CONDITIONAL_COUPON_SHADOW_ENABLED", raising=False)
    with patch(
        "modules.ai.brain.truth_surface.customer_conditional_coupon_loader.load_customer_conditional_coupon_facts",
    ) as loader:
        build_trusted_context_snapshot(
            db=None,
            tenant_id=1,
            customer_phone="",
            message="بعد كم طلب؟",
        )
    loader.assert_not_called()

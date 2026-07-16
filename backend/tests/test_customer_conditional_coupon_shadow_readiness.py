"""Manual shadow-review checklist encoder for Layer 0 conditional-coupon facts."""
from __future__ import annotations

import json
import os
import sys
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest

_HERE = os.path.dirname(os.path.abspath(__file__))
_BACKEND = os.path.abspath(os.path.join(_HERE, ".."))
if _BACKEND not in sys.path:
    sys.path.insert(0, _BACKEND)

from modules.ai.brain.truth_surface.customer_conditional_coupon_contract import (  # noqa: E402
    COMPLETENESS_VERIFIED,
    EVALUATION_CONDITION_SATISFIED,
    MIN_ORDERS_STATE_SATISFIED,
    REASON_CUSTOMER_UNVERIFIED,
    REASON_PROOF_ABSENT,
    REASON_SUBJECT_AMBIGUOUS,
    REASON_TARGET_BUDGET_EXCEEDED,
    build_sanitized_fact_record,
    build_sanitized_telemetry,
)
from modules.ai.brain.truth_surface.customer_conditional_coupon_loader import (  # noqa: E402
    clear_customer_conditional_coupon_turn_cache,
    load_customer_conditional_coupon_facts,
)
from modules.ai.brain.truth_surface.customer_conditional_coupon_shadow_readiness import (  # noqa: E402
    ARTIFACT_KIND,
    INDEPENDENT_VERIFICATION,
    MANUAL_CHECKLIST_ITEMS,
    OUTCOME_BLOCKED_A1_ATTESTATION,
    OUTCOME_BLOCKED_BUDGET_TELEMETRY,
    OUTCOME_BLOCKED_INVALID_INPUT,
    OUTCOME_BLOCKED_SANITIZATION,
    OUTCOME_BLOCKED_SUBJECT_PROOF,
    OUTCOME_MANUAL_CHECKLIST_COMPLETE,
    OUTCOME_MANUAL_CHECKLIST_INCOMPLETE,
    SUBJECT_BRIDGE_OUTCOME_AMBIGUOUS,
    SUBJECT_BRIDGE_OUTCOME_RESOLVED,
    SUBJECT_BRIDGE_OUTCOME_UNRESOLVED,
    CouponShadowReadinessEvidence,
    build_evidence_from_layer0_observation,
    evaluate_coupon_shadow_readiness,
)
from modules.ai.brain.truth_surface.customer_conditional_coupon_subject import (  # noqa: E402
    ConditionalCouponSubjectHandle,
    SubjectResolutionResult,
)
from services.order_customer_identity_contract import (  # noqa: E402
    NAHLA_INTERNAL_ORDER_V1,
)


@pytest.fixture(autouse=True)
def _clear_turn_cache() -> None:
    clear_customer_conditional_coupon_turn_cache()


def _full_checklist() -> frozenset[str]:
    return frozenset(MANUAL_CHECKLIST_ITEMS)


def _happy_fact_record() -> dict:
    return build_sanitized_fact_record(
        identity_status="resolved",
        customer_scope="nahla_internal_customer",
        order_history_completeness=COMPLETENESS_VERIFIED,
        order_history_completeness_source="order_customer_fk_a1_authoritative",
        completed_orders_count=2,
        min_orders_for_eligibility=2,
        orders_shortfall=0,
        min_orders_condition_state=MIN_ORDERS_STATE_SATISFIED,
        prior_redemption_evidence_state="not_applicable",
        per_customer_usage_policy_state="verified",
        conditional_coupon_evaluation_state=EVALUATION_CONDITION_SATISFIED,
        closed_reason_code=None,
        allow_min_orders_condition_claim=True,
    )


def _happy_telemetry() -> dict:
    return build_sanitized_telemetry(
        conditional_target_count=1,
        order_history_completeness=COMPLETENESS_VERIFIED,
        forward_sync_health="healthy",
        source_contract_version="v8_layer0",
        order_count_query_count=1,
        usage_evidence_query_count=1,
        budget_exceeded=False,
        loader_duration_ms=12,
        gate_skipped_reason=None,
    )


def _base_evidence(**overrides: object) -> CouponShadowReadinessEvidence:
    defaults = dict(
        shadow_flag_default_off_observed=True,
        a1_proof_gate_attested=True,
        subject_bridge_outcome=SUBJECT_BRIDGE_OUTCOME_RESOLVED,
        layer0_fact_record=_happy_fact_record(),
        conditional_target_count=1,
        order_count_query_count=1,
        usage_evidence_query_count=1,
        budget_exceeded=False,
        gate_skipped_reason=None,
        closed_reason_code=None,
        manual_checklist_attested=_full_checklist(),
        tenant_scope_label="generic_merchant_a",
    )
    defaults.update(overrides)
    return CouponShadowReadinessEvidence(**defaults)  # type: ignore[arg-type]


def test_manual_checklist_complete_generic_merchant() -> None:
    result = evaluate_coupon_shadow_readiness(_base_evidence())
    assert result.outcome == OUTCOME_MANUAL_CHECKLIST_COMPLETE
    assert result.ready_for_manual_shadow_review is True
    assert result.artifact_kind == ARTIFACT_KIND
    assert result.independent_verification == INDEPENDENT_VERIFICATION
    assert result.canary_or_compose_forbidden is True
    archived = json.dumps(result.to_dict())
    assert "policy_eligibility_ready" not in archived
    assert "eligible" not in archived.lower()
    assert "tests_passed" not in archived
    assert "ci_proof" not in archived


def test_manual_checklist_items_are_attestations_not_ci_proof() -> None:
    for item in MANUAL_CHECKLIST_ITEMS:
        assert item.endswith("_attested")
        assert "passed" not in item
        assert "proof" not in item or item == "a1_proof_gate_attested"


def test_manual_checklist_incomplete_outcome() -> None:
    result = evaluate_coupon_shadow_readiness(
        _base_evidence(
            manual_checklist_attested=frozenset({"ci_layer0_unit_tests_attested"}),
        ),
    )
    assert result.outcome == OUTCOME_MANUAL_CHECKLIST_INCOMPLETE
    assert result.ready_for_manual_shadow_review is False
    assert "ci_pg_e2e_a1_chain_tests_attested" in result.missing_checklist_items


def test_a1_capability_attested_separate_from_proof_gate() -> None:
    missing_capability = evaluate_coupon_shadow_readiness(
        _base_evidence(
            manual_checklist_attested=_full_checklist()
            - frozenset({"a1_capability_validated_attested"}),
        ),
    )
    assert missing_capability.outcome == OUTCOME_MANUAL_CHECKLIST_INCOMPLETE
    assert "a1_capability_validated_attested" in missing_capability.missing_checklist_items

    proof_not_attested = evaluate_coupon_shadow_readiness(
        _base_evidence(a1_proof_gate_attested=False),
    )
    assert proof_not_attested.outcome == OUTCOME_BLOCKED_SUBJECT_PROOF
    assert "a1_proof_gate_not_attested" in proof_not_attested.readiness_blockers


def test_blocked_by_a1_attestation_shadow_flag_observation() -> None:
    result = evaluate_coupon_shadow_readiness(
        _base_evidence(shadow_flag_default_off_observed=False),
    )
    assert result.outcome == OUTCOME_BLOCKED_A1_ATTESTATION
    assert "shadow_flag_not_observed_default_off" in result.readiness_blockers


def test_blocked_by_subject_proof_unresolved_bridge() -> None:
    result = evaluate_coupon_shadow_readiness(
        _base_evidence(
            subject_bridge_outcome=SUBJECT_BRIDGE_OUTCOME_UNRESOLVED,
            closed_reason_code=REASON_CUSTOMER_UNVERIFIED,
        ),
    )
    assert result.outcome == OUTCOME_BLOCKED_SUBJECT_PROOF
    assert "subject_bridge_unresolved" in result.readiness_blockers


def test_blocked_by_subject_proof_ambiguous_bridge() -> None:
    result = evaluate_coupon_shadow_readiness(
        _base_evidence(
            subject_bridge_outcome=SUBJECT_BRIDGE_OUTCOME_AMBIGUOUS,
            closed_reason_code=REASON_SUBJECT_AMBIGUOUS,
        ),
    )
    assert result.outcome == OUTCOME_BLOCKED_SUBJECT_PROOF


def test_blocked_by_subject_proof_absent_history() -> None:
    result = evaluate_coupon_shadow_readiness(
        _base_evidence(closed_reason_code=REASON_PROOF_ABSENT),
    )
    assert result.outcome == OUTCOME_BLOCKED_SUBJECT_PROOF
    assert "closed_reason:authoritative_history_proof_absent" in result.readiness_blockers


def test_blocked_by_invalid_input_unknown_bridge_outcome() -> None:
    result = evaluate_coupon_shadow_readiness(
        _base_evidence(subject_bridge_outcome="corrupted"),
    )
    assert result.outcome == OUTCOME_BLOCKED_INVALID_INPUT
    assert any("subject_bridge_outcome_invalid" in b for b in result.readiness_blockers)


def test_blocked_by_invalid_input_unknown_fact_key() -> None:
    polluted = dict(_happy_fact_record())
    polluted["overall_eligibility_state"] = "eligible"
    result = evaluate_coupon_shadow_readiness(
        _base_evidence(layer0_fact_record=polluted),
    )
    assert result.outcome == OUTCOME_BLOCKED_INVALID_INPUT
    assert any("fact_record_unknown_key" in b for b in result.readiness_blockers)


def test_blocked_by_invalid_input_unknown_telemetry_key_in_builder() -> None:
    telemetry = dict(_happy_telemetry())
    telemetry["tenant_id"] = 1
    with pytest.raises(ValueError, match="telemetry_unknown_key"):
        build_evidence_from_layer0_observation(
            shadow_flag_default_off_observed=True,
            a1_proof_gate_attested=True,
            subject_bridge_outcome=SUBJECT_BRIDGE_OUTCOME_RESOLVED,
            fact_record=_happy_fact_record(),
            telemetry=telemetry,
            manual_checklist_attested=_full_checklist(),
        )


def test_blocked_by_sanitization_forbidden_id_key() -> None:
    polluted = dict(_happy_fact_record())
    polluted["customer_id"] = 42
    result = evaluate_coupon_shadow_readiness(
        _base_evidence(layer0_fact_record=polluted),
    )
    assert result.outcome == OUTCOME_BLOCKED_INVALID_INPUT
    assert any("fact_record_unknown_key" in b for b in result.readiness_blockers)


def test_blocked_by_sanitization_nested_phone_key() -> None:
    polluted = dict(_happy_fact_record())
    polluted["nested"] = {"phone": "secret"}
    result = evaluate_coupon_shadow_readiness(
        _base_evidence(layer0_fact_record=polluted),
    )
    assert result.outcome == OUTCOME_BLOCKED_INVALID_INPUT


def test_evaluator_recomputes_sanitizer_cannot_be_bypassed() -> None:
    clean = _happy_fact_record()
    polluted = dict(clean)
    polluted["customer_id"] = 42
    clean_evidence = _base_evidence(layer0_fact_record=clean)
    polluted_evidence = _base_evidence(layer0_fact_record=polluted)
    assert evaluate_coupon_shadow_readiness(clean_evidence).outcome == (
        OUTCOME_MANUAL_CHECKLIST_COMPLETE
    )
    assert evaluate_coupon_shadow_readiness(polluted_evidence).outcome in {
        OUTCOME_BLOCKED_INVALID_INPUT,
        OUTCOME_BLOCKED_SANITIZATION,
    }


def test_blocked_by_budget_telemetry_target_overflow() -> None:
    result = evaluate_coupon_shadow_readiness(
        _base_evidence(
            budget_exceeded=True,
            conditional_target_count=8,
            closed_reason_code=REASON_TARGET_BUDGET_EXCEEDED,
        ),
    )
    assert result.outcome == OUTCOME_BLOCKED_BUDGET_TELEMETRY


def test_blocked_by_budget_telemetry_gate_skipped() -> None:
    result = evaluate_coupon_shadow_readiness(
        _base_evidence(gate_skipped_reason="shadow_flag_disabled"),
    )
    assert result.outcome == OUTCOME_BLOCKED_BUDGET_TELEMETRY


def test_build_evidence_from_layer0_observation() -> None:
    evidence = build_evidence_from_layer0_observation(
        shadow_flag_default_off_observed=True,
        a1_proof_gate_attested=True,
        subject_bridge_outcome=SUBJECT_BRIDGE_OUTCOME_RESOLVED,
        fact_record=_happy_fact_record(),
        telemetry=_happy_telemetry(),
        manual_checklist_attested=_full_checklist(),
        tenant_scope_label="generic_merchant_b",
    )
    result = evaluate_coupon_shadow_readiness(evidence)
    assert result.outcome == OUTCOME_MANUAL_CHECKLIST_COMPLETE


def test_tenant_isolation_evidence_inputs_not_mixed() -> None:
    tenant_a = _base_evidence(
        tenant_scope_label="tenant_scope_alpha",
        conditional_target_count=1,
    )
    tenant_b = _base_evidence(
        tenant_scope_label="tenant_scope_beta",
        conditional_target_count=4,
    )
    result_a = evaluate_coupon_shadow_readiness(tenant_a)
    result_b = evaluate_coupon_shadow_readiness(tenant_b)
    assert result_a.ready_for_manual_shadow_review is True
    assert result_b.ready_for_manual_shadow_review is True
    assert tenant_a.tenant_scope_label != tenant_b.tenant_scope_label
    assert tenant_a.conditional_target_count != tenant_b.conditional_target_count


def test_shadow_flag_default_off_no_loader_io(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv(
        "NAHLA_TRUSTED_CONTEXT_CUSTOMER_CONDITIONAL_COUPON_SHADOW_ENABLED",
        raising=False,
    )
    facts, obs = load_customer_conditional_coupon_facts(
        db=MagicMock(),
        tenant_id=1,
        message="بعد كم طلب يصل كوبون متجر تجريبي عام؟",
        conversation=SimpleNamespace(customer_id=9),
    )
    assert facts == []
    assert obs["gate_skipped_reason"] == "shadow_flag_disabled"
    assert obs["order_count_query_count"] == 0

    evidence = build_evidence_from_layer0_observation(
        shadow_flag_default_off_observed=True,
        a1_proof_gate_attested=True,
        subject_bridge_outcome=SUBJECT_BRIDGE_OUTCOME_RESOLVED,
        fact_record=None,
        telemetry=obs,
        manual_checklist_attested=_full_checklist(),
    )
    result = evaluate_coupon_shadow_readiness(evidence)
    assert result.outcome == OUTCOME_BLOCKED_SANITIZATION
    assert result.ready_for_manual_shadow_review is False
    assert "layer0_fact_absent" in result.readiness_blockers


def test_shadow_observation_happy_path_maps_to_manual_checklist_complete(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv(
        "NAHLA_TRUSTED_CONTEXT_CUSTOMER_CONDITIONAL_COUPON_SHADOW_ENABLED",
        "true",
    )
    promo = SimpleNamespace(
        id=101,
        tenant_id=1,
        conditions={"min_orders_for_eligibility": 2},
        extra_metadata={},
        status="active",
        starts_at=None,
        ends_at=None,
        usage_count=0,
        usage_limit=None,
    )
    resolution = SubjectResolutionResult(
        status="resolved",
        handle=ConditionalCouponSubjectHandle(
            subject_kind="nahla_internal_customer",
            tenant_id=1,
            identity_namespace=NAHLA_INTERNAL_ORDER_V1,
            handle_source="conversation_a1_subject_read_bridge",
            customer_id=55,
        ),
    )
    with patch(
        "modules.ai.brain.truth_surface.customer_conditional_coupon_loader."
        "resolve_conditional_coupon_subject_handle",
        return_value=resolution,
    ), patch(
        "modules.ai.brain.truth_surface.customer_conditional_coupon_loader."
        "scan_conditional_targets",
        return_value=[promo],
    ), patch(
        "modules.ai.brain.truth_surface.customer_conditional_coupon_loader."
        "bound_proof_snapshot_from_handle",
    ) as proof_mock, patch(
        "modules.ai.brain.truth_surface.customer_conditional_coupon_loader."
        "count_countable_orders_for_subject",
        return_value=2,
    ):
        snapshot = MagicMock()
        snapshot.policy_eligibility_ready.return_value = True
        snapshot.authoritative_source_history_completeness.return_value = "complete"
        snapshot.forward_sync_health.return_value = "healthy"
        proof_mock.return_value = snapshot
        facts, obs = load_customer_conditional_coupon_facts(
            db=MagicMock(),
            tenant_id=1,
            message="conditional coupon after min orders for عطر ورد 100ml",
        )

    evidence = build_evidence_from_layer0_observation(
        shadow_flag_default_off_observed=True,
        a1_proof_gate_attested=True,
        subject_bridge_outcome=SUBJECT_BRIDGE_OUTCOME_RESOLVED,
        fact_record=facts[0].value if facts else None,
        telemetry=obs,
        manual_checklist_attested=_full_checklist(),
        tenant_scope_label="generic_perfume_merchant",
    )
    result = evaluate_coupon_shadow_readiness(evidence)
    assert result.outcome == OUTCOME_MANUAL_CHECKLIST_COMPLETE
    assert "policy_eligibility_ready" not in json.dumps(result.to_dict())

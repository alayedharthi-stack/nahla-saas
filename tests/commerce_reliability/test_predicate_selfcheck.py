"""Adversarial self-tests for the evidence adapter and the baseline predicates.

They prove that a changed cause is not absorbed by an existing allowance and
that provenance is classified fail-closed. No application code runs here.
"""
from __future__ import annotations

from tests.commerce_reliability import reliability_evaluator as ev
from tests.commerce_reliability import runtime_support as rs


# ── Fallback provenance classifier ───────────────────────────────────────────


PROVENANCE_MATRIX = [
        # Model-owned text, accepted: no fallback.
        ([{"compose_source": "persona_llm", "llm_candidate_present": True}], [{"product_reply_outcome": "rich_accepted"}], ev.FALLBACK_NONE),
        # PR #1084 deterministic grounded recovery after suppression.
        ([{"compose_source": "fallback_deterministic", "final_customer_text_source": "fallback_deterministic",
           "fallback_reason": "pre_provider_suppression", "delivery_recovery": {"trigger": "pre_provider_suppression", "text_source": "fallback_deterministic"}}],
         [{"product_reply_outcome": "suppressed_text_recovered"}], ev.FALLBACK_EXPECTED_DELIVERY_RECOVERY),
        # Guarded reply resent after a definitive interactive rejection.
        ([{"compose_source": "persona_llm"}], [{"product_reply_outcome": "rich_rejected_text_recovered"}], ev.FALLBACK_EXPECTED_DELIVERY_RECOVERY),
        # Deterministic text because the model timed out: runtime fallback (Astra probe E).
        ([{"compose_source": "fallback_deterministic", "fallback_reason": "timeout"}], [], ev.FALLBACK_UNEXPECTED_RUNTIME),
        ([{"final_customer_text_source": "fallback_deterministic", "fallback_reason": "compose_exception"}], [], ev.FALLBACK_UNEXPECTED_RUNTIME),
        # V2 owner: completed run is model-owned; failed run is the safe_fallback_reply runtime fallback.
        ([{"reply_owner": "commerce_agent_v2", "v2_status": "completed"}], [], ev.FALLBACK_NONE),
        ([{"reply_owner": "commerce_agent_v2", "v2_status": "failed"}], [], ev.FALLBACK_UNEXPECTED_RUNTIME),
        # Policy guard withholding an unknown claim: safe fallback (passes only when the case expects it).
        ([{"compose_source": "fallback_deterministic", "fallback_reason": "merchant_policy_unknown_claim"}], [], ev.FALLBACK_EXPECTED_SAFE),
        # Missing or unrecognised provenance is never "none".
        ([], [], ev.FALLBACK_UNKNOWN_PROVENANCE),
        ([{}], [{"product_reply_outcome": "rich_accepted"}], ev.FALLBACK_UNKNOWN_PROVENANCE),
        ([{"compose_source": "mystery_source"}], [], ev.FALLBACK_UNKNOWN_PROVENANCE),
        ([{"compose_source": "fallback_deterministic", "fallback_reason": "never_seen_before"}], [], ev.FALLBACK_UNKNOWN_PROVENANCE),
        ([{"reply_owner": "commerce_agent_v2"}], [], ev.FALLBACK_UNKNOWN_PROVENANCE),
        # One unknown row poisons the turn even next to a model-owned row.
        ([{"compose_source": "persona_llm"}, {}], [], ev.FALLBACK_UNKNOWN_PROVENANCE),
        # A runtime-failure fallback row dominates a model-owned row.
        ([{"compose_source": "persona_llm"}, {"compose_source": "fallback_deterministic", "fallback_reason": "empty_llm"}], [], ev.FALLBACK_UNEXPECTED_RUNTIME),
]


def test_fallback_provenance_classifier_matrix() -> None:
    assert len(PROVENANCE_MATRIX) == 15
    for index, (rows, outcomes, expected) in enumerate(PROVENANCE_MATRIX):
        kind, detail = rs.classify_fallback_provenance(rows, outcomes)
        assert kind == expected, (index, rows, outcomes, kind, detail)
        # Whatever the classifier says feeds the evaluator's closed vocabulary.
        assert kind in ev.FALLBACK_KINDS


# ── UC-01 predicate ──────────────────────────────────────────────────────────

_UC01_OK = dict(
    blockers=sorted(rs.UC01_EXPECTED_BLOCKERS), final_token="end_ok", accepted_wamids=[],
    persisted_outbound_count=0, outcome_count=0, transport_outcome=ev.TRANSPORT_REJECTED_DEFINITIVE,
)


def test_uc01_predicate_rejects_changed_causes() -> None:
    assert rs.classify_uc01_observation(**_UC01_OK) == "recorded_defect"
    assert rs.classify_uc01_observation(**{**_UC01_OK, "blockers": []}) == "passes"
    # Any additional blocker (tenant, duplicate, dispatch safety...) is a separate failure.
    extra = rs.classify_uc01_observation(**{**_UC01_OK, "blockers": [*_UC01_OK["blockers"], "tenant_mismatch:2"]})
    assert extra.startswith("changed_cause:unrelated_blockers=tenant_mismatch:2")
    # A partial signature (terminal missing but provenance recorded) is not the recorded defect.
    partial = rs.classify_uc01_observation(**{**_UC01_OK, "blockers": ["missing_terminal:lifecycle:end_ok:inferred_without_provider_acceptance"]})
    assert partial.startswith("changed_cause:partial_signature")
    assert rs.classify_uc01_observation(**{**_UC01_OK, "final_token": "end_delivery_failed"}).startswith("changed_cause:final_token")
    assert rs.classify_uc01_observation(**{**_UC01_OK, "accepted_wamids": ["wamid.1"]}) == "changed_cause:provider_accepted"
    assert rs.classify_uc01_observation(**{**_UC01_OK, "transport_outcome": ev.TRANSPORT_UNKNOWN}).startswith("changed_cause:transport_outcome")
    assert rs.classify_uc01_observation(**{**_UC01_OK, "persisted_outbound_count": 1}).startswith("changed_cause:records_present")


# ── UC-02 predicate ──────────────────────────────────────────────────────────

_EXPECTED_STATE = {"last_search_candidates": [{"id": 11}], "current_product_focus": {"id": 13}}
_BASELINE_STATE = {"last_search_candidates": [], "current_product_focus": None}  # pre-race defaults
_SAVED = {"last_search_candidates": "saved", "current_product_focus": "saved"}


def _uc02(persisted, worker_status=_SAVED):
    return rs.classify_state_save_outcome(
        worker_status=worker_status, persisted=persisted, expected=_EXPECTED_STATE, baseline=_BASELINE_STATE,
    )


def test_uc02_predicate_distinguishes_lost_update_from_no_op() -> None:
    assert _uc02(dict(_EXPECTED_STATE)) == "both_persisted"
    # The losing worker overwrote the other field with its stale (baseline) value.
    assert _uc02({"last_search_candidates": [], "current_product_focus": {"id": 13}}) == "single_lost_update"
    assert _uc02({"last_search_candidates": [{"id": 11}], "current_product_focus": None}) == "single_lost_update"
    # Neither update persisting is a broken / no-op persistence path, not the recorded overwrite.
    assert _uc02({"last_search_candidates": [], "current_product_focus": None}) == "neither_persisted"
    # A worker that did not complete its save cannot produce the recorded defect.
    failed = {"last_search_candidates": "saved", "current_product_focus": "error:OperationalError:boom"}
    assert _uc02({"last_search_candidates": [], "current_product_focus": {"id": 13}}, failed) == "worker_failed:current_product_focus"
    # A field holding neither the written value nor the pre-race value is a changed cause.
    assert _uc02({"last_search_candidates": [], "current_product_focus": {"id": 99}}) == "value_mismatch:current_product_focus"
    assert _uc02({"last_search_candidates": None, "current_product_focus": {"id": 13}}) == "value_mismatch:last_search_candidates"


# ── RB-05 predicate ──────────────────────────────────────────────────────────

_RB05_OK = dict(
    locked=True, action="llm_reply", reason="active order — generic alternate-product enquiry (not a confirmation)",
    interpreter_result=None, control_action="search_products",
)


def test_rb05_predicate_requires_recorded_conditions() -> None:
    assert rs.classify_lock_decision(**_RB05_OK) == "recorded_defect"
    assert rs.classify_lock_decision(**{**_RB05_OK, "action": "search_products"}) == "passes"
    # A different non-search decision is a changed cause, not the recorded lock defect.
    assert rs.classify_lock_decision(**{**_RB05_OK, "action": "propose_draft_order"}) == "changed_cause:action=propose_draft_order"
    assert rs.classify_lock_decision(**{**_RB05_OK, "reason": "absence of positive commerce signal"}).startswith("changed_cause:reason=")
    assert rs.classify_lock_decision(**{**_RB05_OK, "locked": False}) == "changed_cause:session_not_locked"
    assert rs.classify_lock_decision(**{**_RB05_OK, "interpreter_result": object()}) == "changed_cause:interpreter_not_gated"
    assert rs.classify_lock_decision(**{**_RB05_OK, "control_action": "llm_reply"}) == "changed_cause:unlocked_control_action=llm_reply"

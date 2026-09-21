"""The read-only promotion tool's projection, offline.

What is proved here is the boundary between the platform's promotion-truth
resolver, the merchant's AI coupon policy and the evidence the commerce
runtime's loop verifies a reply against: only what the resolver returned for
this customer becomes citable, a personal code issued to anyone else is never
projected, the merchant's policy can keep coupons away from the AI entirely or
by level, an offer never gets a code, eligibility is never claimed, an
unreadable source is never reported as "no promotions", conditions stay
bounded, and the trusted scope is re-checked before the read. The resolver
itself is proved in its own modules.
"""
from __future__ import annotations

import asyncio
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
for p in [str(REPO_ROOT), str(REPO_ROOT / "backend"), str(REPO_ROOT / "database")]:
    if p not in sys.path:
        sys.path.insert(0, p)

from modules.ai.brain.commerce import promotion_truth as pt  # noqa: E402
from modules.ai.commerce_agent_v2.tools import promotions as tool  # noqa: E402

DEFAULT_POLICY: Dict[str, Any] = {"enabled": True, "allowed_levels": ["bronze", "silver"],
                                  "min_remaining_hours": 3, "pool_mode": "pool_first"}


class Context:
    """The trusted context as the tool sees it: scope, session, tenant, customer, evidence."""

    def __init__(self, tenant_id: int = 7, customer_id: Optional[int] = 41) -> None:
        self.tenant_id = tenant_id
        self.customer_id = customer_id
        self.db = object()
        self.scope_checks = 0
        self.registered: List[Any] = []

    def assert_scope(self) -> None:
        self.scope_checks += 1

    def register_evidence(self, records: List[Any]) -> None:
        self.registered.extend(records)


def coupon_fact(promotion_id: int = 5, code: str = "WELCOME10", **overrides: Any) -> Dict[str, Any]:
    fact = {
        "id": promotion_id, "code": code, "discount_type": "percentage", "discount_value": "10",
        "description": "خصم ترحيبي على أول طلب", "expires_at": "2027-01-01T00:00:00+00:00",
        "source_type": "manual", "allocation_channel": "", "coupon_level": "",
        "conditions": {"min_order_amount": "100"}, "customer_bound": False, "bound_customer_id": None,
        "eligibility_determined": False, "eligibility_note": "conditions_not_fully_evaluated",
        "record_kind": "coupon",
    }
    fact.update(overrides)
    return fact


def offer_fact(promotion_id: int = 9, **overrides: Any) -> Dict[str, Any]:
    fact = {
        "id": promotion_id, "name": "شحن مجاني", "description": "شحن مجاني فوق 200 ريال",
        "promotion_type": "free_shipping", "discount_value": "", "ends_at": "", "conditions": {},
        "code": "", "eligibility_determined": False,
        "eligibility_note": "offer_terms_only_no_code_invented", "record_kind": "offer",
        "source_type": "promotion_rule",
    }
    fact.update(overrides)
    return fact


def truth(*, shareable: List[Dict[str, Any]] = (), offers: List[Dict[str, Any]] = (),
          query_failed: bool = False, outcome: str = pt.QUERY_OK) -> pt.PromotionTruthResult:
    return pt.PromotionTruthResult(
        tenant_id=7, query_run=True, candidate_count=len(shareable) + len(offers),
        shareable=list(shareable), offers=list(offers), query_failed=query_failed,
        query_outcome=outcome,
    )


def run(context: Context, monkeypatch: pytest.MonkeyPatch, result: pt.PromotionTruthResult,
        *, limit: int = tool.MAX_PROMOTIONS, policy: Any = None) -> Any:
    """Run the tool with the resolver and the merchant policy replaced by doubles.
    ``policy`` may be a dict, or an exception instance the policy read raises."""
    import services.coupon_generator as generator

    calls: List[Any] = []

    def resolver(db: Any, tenant_id: int, **kwargs: Any) -> pt.PromotionTruthResult:
        calls.append((db, tenant_id, kwargs))
        return result

    def read_policy(db: Any, tenant_id: int) -> Dict[str, Any]:
        if isinstance(policy, BaseException):
            raise policy
        return dict(DEFAULT_POLICY if policy is None else policy)

    monkeypatch.setattr(tool, "resolve_shareable_promotions", resolver)
    monkeypatch.setattr(generator, "_get_ai_policy", read_policy)
    outcome = asyncio.run(tool.list_shareable_promotions_impl(context, limit=limit))
    return outcome, calls


# ── Projection ───────────────────────────────────────────────────────────────


def test_a_shareable_coupon_and_an_offer_become_citable_evidence(monkeypatch) -> None:
    context = Context()
    result, calls = run(context, monkeypatch, truth(shareable=[coupon_fact()], offers=[offer_fact()]))
    assert result.status == "ok" and result.partial is False
    assert [p.evidence_ref for p in result.promotions] == ["promotion:coupon:5", "promotion:offer:9"]
    assert [e.ref for e in result.evidence] == ["promotion:coupon:5", "promotion:offer:9"]
    assert [e.source for e in result.evidence] == ["promotion_coupon", "promotion_offer"]
    coupon, offer = result.promotions
    assert coupon.code == "WELCOME10" and coupon.conditions == {"min_order_amount": "100"}
    assert coupon.discount_type == "percentage" and coupon.discount_value == "10"
    assert coupon.bound_to_this_customer is False
    assert offer.code == "" and offer.discount_type == "free_shipping" and offer.name == "شحن مجاني"
    assert all(p.eligibility_determined is False for p in result.promotions)
    # The resolver was asked for this tenant, on this session, for this customer,
    # and the evidence was registered on the trusted context.
    assert calls == [(context.db, 7, {"limit": tool.MAX_PROMOTIONS, "customer_id": 41})]
    assert [e.ref for e in context.registered] == ["promotion:coupon:5", "promotion:offer:9"]


def test_the_scope_is_rechecked_before_anything_is_read(monkeypatch) -> None:
    context = Context()
    run(context, monkeypatch, truth(shareable=[coupon_fact()]))
    assert context.scope_checks == 1


def test_a_coupon_without_a_code_and_an_offer_with_one_are_never_cited(monkeypatch) -> None:
    context = Context()
    result, _ = run(context, monkeypatch, truth(
        shareable=[coupon_fact(promotion_id=1, code="")],
        offers=[offer_fact(promotion_id=2, code="LEAKED")]))
    assert [p.evidence_ref for p in result.promotions] == ["promotion:offer:2"]
    assert result.promotions[0].code == ""


# ── Whose code it is ─────────────────────────────────────────────────────────


def test_a_personal_code_issued_to_another_customer_is_never_projected(monkeypatch) -> None:
    """Even if the resolver handed it over, the projection refuses it."""
    context = Context(customer_id=41)
    result, _ = run(context, monkeypatch, truth(shareable=[
        coupon_fact(promotion_id=1, code="PERSONAL42", customer_bound=True, bound_customer_id=42),
        coupon_fact(promotion_id=2, code="WELCOME10"),
    ]))
    assert [p.code for p in result.promotions] == ["WELCOME10"]


def test_this_customer_s_own_personal_code_is_projected_and_marked(monkeypatch) -> None:
    context = Context(customer_id=41)
    result, _ = run(context, monkeypatch, truth(shareable=[
        coupon_fact(promotion_id=1, code="PERSONAL41", customer_bound=True, bound_customer_id=41)]))
    assert [p.code for p in result.promotions] == ["PERSONAL41"]
    assert result.promotions[0].bound_to_this_customer is True


def test_without_a_customer_no_personal_code_is_projected_and_none_is_asked_for(monkeypatch) -> None:
    context = Context(customer_id=None)
    result, calls = run(context, monkeypatch, truth(shareable=[
        coupon_fact(promotion_id=1, code="PERSONAL41", customer_bound=True, bound_customer_id=41),
        coupon_fact(promotion_id=2, code="WELCOME10"),
    ]))
    assert [p.code for p in result.promotions] == ["WELCOME10"]
    assert calls[0][2]["customer_id"] is None


# ── The merchant's policy ────────────────────────────────────────────────────


def test_a_merchant_who_disabled_ai_coupons_gets_a_denial_and_no_read(monkeypatch) -> None:
    context = Context()
    result, calls = run(context, monkeypatch, truth(shareable=[coupon_fact()]),
                        policy={**DEFAULT_POLICY, "enabled": False})
    assert result.status == "denied" and result.failure_reason == "merchant_ai_coupon_policy_disabled"
    assert result.promotions == [] and calls == [] and context.registered == []


def test_an_unreadable_policy_is_a_closed_gate_not_an_open_one(monkeypatch) -> None:
    context = Context()
    result, calls = run(context, monkeypatch, truth(shareable=[coupon_fact()]),
                        policy=RuntimeError("settings unavailable"))
    assert result.status == "error"
    assert result.failure_reason == "merchant_ai_coupon_policy_unreadable:RuntimeError"
    assert calls == [] and context.registered == []


def test_a_coupon_level_the_policy_does_not_allow_is_left_out(monkeypatch) -> None:
    context = Context()
    result, _ = run(context, monkeypatch, truth(shareable=[
        coupon_fact(promotion_id=1, code="GOLD50", coupon_level="gold"),
        coupon_fact(promotion_id=2, code="SILVER15", coupon_level="silver"),
        coupon_fact(promotion_id=3, code="WELCOME10", coupon_level=""),
    ]), policy={**DEFAULT_POLICY, "allowed_levels": ["bronze", "silver"]})
    assert [p.code for p in result.promotions] == ["SILVER15", "WELCOME10"]
    assert result.promotions[0].coupon_level == "silver"


# ── Honest empties, errors and partial reads ─────────────────────────────────


def test_no_valid_promotion_is_an_honest_empty_answer_that_registers_nothing(monkeypatch) -> None:
    context = Context()
    result, _ = run(context, monkeypatch, truth(outcome=pt.NO_VALID_PROMOTIONS))
    assert result.status == "not_found" and result.promotions == [] and result.evidence == []
    assert result.failure_reason == "no_valid_shareable_promotions"
    assert result.query_outcome == pt.NO_VALID_PROMOTIONS
    assert context.registered == []


def test_an_unreadable_source_is_reported_as_an_error_never_as_no_promotions(monkeypatch) -> None:
    context = Context()
    result, _ = run(context, monkeypatch, truth(query_failed=True, outcome=pt.PROMOTION_QUERY_FAILED))
    assert result.status == "error" and result.failure_reason == "promotion_query_failed"
    assert result.promotions == [] and context.registered == []


def test_a_partial_read_is_marked_partial_on_its_way_to_the_model(monkeypatch) -> None:
    """The coupons query failed, the offers query succeeded: what was read is
    returned, and the answer says a code may be missing from it."""
    context = Context()
    result, _ = run(context, monkeypatch, truth(offers=[offer_fact()], outcome=pt.PROMOTION_PARTIAL_FAILURE))
    assert result.status == "ok" and result.partial is True
    assert result.query_outcome == pt.PROMOTION_PARTIAL_FAILURE
    assert [p.evidence_ref for p in result.promotions] == ["promotion:offer:9"]


# ── Bounds ───────────────────────────────────────────────────────────────────


def test_coupons_and_offers_are_each_bounded_by_the_declared_maximum(monkeypatch) -> None:
    context = Context()
    many_coupons = [coupon_fact(promotion_id=i, code=f"C{i}") for i in range(1, 30)]
    many_offers = [offer_fact(promotion_id=100 + i) for i in range(1, 30)]
    result, calls = run(context, monkeypatch, truth(shareable=many_coupons, offers=many_offers), limit=50)
    kinds = [p.record_kind for p in result.promotions]
    assert kinds.count("coupon") == tool.MAX_PROMOTIONS and kinds.count("offer") == tool.MAX_PROMOTIONS
    assert calls[0][2]["limit"] == tool.MAX_PROMOTIONS


def test_conditions_with_hundreds_of_ids_are_cut_and_counted(monkeypatch) -> None:
    context = Context()
    result, _ = run(context, monkeypatch, truth(shareable=[coupon_fact(conditions={
        "product_ids": list(range(1, 501)), "min_order_amount": "100", "note": "x" * 500})]))
    conditions = result.promotions[0].conditions
    assert len(conditions["product_ids"]) == tool.MAX_CONDITION_IDS
    assert conditions["product_ids_count"] == 500
    assert conditions["min_order_amount"] == "100" and len(conditions["note"]) == 120


def test_a_malformed_fact_is_skipped_rather_than_crashing_the_read(monkeypatch) -> None:
    context = Context()
    result, _ = run(context, monkeypatch, truth(shareable=[
        {"id": "x", "code": "BAD", "record_kind": "coupon"},
        {"id": 3, "code": "OK3", "record_kind": "mystery"},
        coupon_fact(promotion_id=4, code="OK4", conditions="not-a-dict"),
        coupon_fact(promotion_id=5, code="BOUNDBADLY", customer_bound=True, bound_customer_id="nope"),
    ]))
    assert [p.evidence_ref for p in result.promotions] == ["promotion:coupon:4"]
    assert result.promotions[0].conditions == {}


# ── The discount reading and the merchant's minimum remaining life ──────────


def test_the_one_discount_reading_travels_with_the_raw_fields(monkeypatch) -> None:
    """Tenant 1, September 2026: a record saying "percentage" beside a money
    object made the model quote "5 SAR" for a 5% code. The resolver now hands
    one reading; the projection carries it next to the raw fields."""
    context = Context()
    outcome, _ = run(context, monkeypatch, truth(shareable=[coupon_fact(discount="10%")]))
    assert outcome.status == "ok"
    assert outcome.promotions[0].discount == "10%" and outcome.promotions[0].discount_value == "10"
    assert outcome.evidence[0].fields["discount"] == "10%"


def _fixed_now(monkeypatch) -> datetime:
    now = datetime(2026, 9, 21, 20, 0, tzinfo=timezone.utc)
    monkeypatch.setattr(tool, "_now", lambda: now)
    return now


def test_a_code_with_less_life_left_than_the_merchant_s_minimum_is_left_out(monkeypatch) -> None:
    """The dashboard's ``min_remaining_hours`` is the merchant's word that the
    AI does not hand out a code about to expire; the read honours it. An
    expiry the tool cannot read does not exclude: the resolver already judged
    the code valid, and this rule only shortens that window."""
    now = _fixed_now(monkeypatch)
    soon = coupon_fact(1, "SOON1", expires_at=(now + timedelta(hours=2)).isoformat())
    later = coupon_fact(2, "LATER2", expires_at=(now + timedelta(hours=4)).isoformat())
    unreadable = coupon_fact(3, "ODD3", expires_at="tomorrow")
    outcome, _ = run(Context(), monkeypatch, truth(shareable=[soon, later, unreadable]),
                     policy={**DEFAULT_POLICY, "min_remaining_hours": 3})
    assert [p.code for p in outcome.promotions] == ["LATER2", "ODD3"]
    assert [record.ref for record in outcome.evidence] == ["promotion:coupon:2", "promotion:coupon:3"]


def test_with_no_minimum_every_currently_valid_code_is_kept(monkeypatch) -> None:
    now = _fixed_now(monkeypatch)
    soon = coupon_fact(1, "SOON1", expires_at=(now + timedelta(minutes=30)).isoformat())
    for policy in ({**DEFAULT_POLICY, "min_remaining_hours": 0},
                   {**DEFAULT_POLICY, "min_remaining_hours": "x"},
                   {k: v for k, v in DEFAULT_POLICY.items() if k != "min_remaining_hours"}):
        outcome, _ = run(Context(), monkeypatch, truth(shareable=[soon]), policy=policy)
        assert [p.code for p in outcome.promotions] == ["SOON1"]


def test_when_the_minimum_leaves_nothing_the_answer_is_an_honest_not_found(monkeypatch) -> None:
    now = _fixed_now(monkeypatch)
    soon = coupon_fact(1, "SOON1", expires_at=(now + timedelta(hours=1)).isoformat())
    outcome, _ = run(Context(), monkeypatch, truth(shareable=[soon]),
                     policy={**DEFAULT_POLICY, "min_remaining_hours": 3})
    assert outcome.status == "not_found" and outcome.failure_reason == "no_valid_shareable_promotions"


def test_an_offer_is_not_subject_to_the_coupon_minimum(monkeypatch) -> None:
    now = _fixed_now(monkeypatch)
    offer = offer_fact(9, ends_at=(now + timedelta(hours=1)).isoformat())
    outcome, _ = run(Context(), monkeypatch, truth(offers=[offer]),
                     policy={**DEFAULT_POLICY, "min_remaining_hours": 3})
    assert [p.record_kind for p in outcome.promotions] == ["offer"]

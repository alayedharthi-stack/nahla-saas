"""The read-only promotion tool's projection, offline.

What is proved here is the boundary between the platform's promotion-truth
resolver, the merchant's AI coupon policy and the evidence the commerce
runtime's loop verifies a reply against: only what the resolver returned for
this customer becomes citable, a personal code issued to anyone else is never
projected, the merchant's policy can keep coupons away from the AI entirely or
by level, a coupon tied to a loyalty rung reaches only a customer who earned
that rung while one tied to none is never withheld for want of one, an offer
never gets a code, no eligibility is claimed beyond what was actually read, an
unreadable source is never reported as "no promotions", conditions stay
bounded, and the trusted scope is re-checked before the read. The resolver and
the entitlement contract are proved in their own modules.
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
from services import coupon_entitlement_read as cer  # noqa: E402

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
        self.standing_reads: List[Any] = []

    def assert_scope(self) -> None:
        self.scope_checks += 1

    def register_evidence(self, records: List[Any]) -> None:
        self.registered.extend(records)


def coupon_fact(promotion_id: int = 5, code: str = "WELCOME10", **overrides: Any) -> Dict[str, Any]:
    fact = {
        "id": promotion_id, "code": code, "discount_type": "percentage", "discount_value": "10",
        "description": "خصم ترحيبي على أول طلب", "expires_at": "2027-01-01T00:00:00+00:00",
        # A merchant-created coupon the merchant put on a shared surface: the
        # two acts that make an unleveled code a general offer.
        "source_type": "manual", "allocation_channel": "shared", "coupon_level": "",
        # What the create endpoint stamps when the merchant makes a coupon in
        # their own dashboard. Default False: a fixture must say the act
        # happened, exactly as a row must.
        "merchant_authored": False,
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


def entitled(level: Optional[str] = None, *, orders: int = 4, customer_id: Optional[int] = 41,
             reason: str = cer.REASON_ENTITLED, first_purchase: bool = False) -> cer.LevelEntitlement:
    """A customer standing on exactly one rung — the shape the contract
    resolves. The contract itself is proved in its own module; here the tool is
    shown obeying whatever it is told."""
    return cer.LevelEntitlement(
        customer_id=customer_id, countable_orders=orders, resolved_level=level,
        entitled_levels=((level,) if level else ()),
        reason=reason, first_purchase_applied=first_purchase)


def unknown(reason: str = cer.REASON_IDENTITY_NOT_ESTABLISHED,
            customer_id: Optional[int] = None) -> cer.LevelEntitlement:
    """A customer whose standing could not be determined at all."""
    return cer.LevelEntitlement(customer_id=customer_id, countable_orders=None, resolved_level=None,
                                entitled_levels=(), reason=reason)


def _normalised_levels() -> Any:
    """The merchant's ladder as the dashboard saves it, shipped defaults."""
    from backend.routers.coupons import DEFAULT_COUPON_LEVELS, _normalise_levels

    rows = []
    for row in _normalise_levels(DEFAULT_COUPON_LEVELS):
        item = dict(row)
        # The walk is asked about rungs the assistant may serve, so every rung
        # is open on this channel here; the store policy under test is the
        # ``allowed_levels`` list each case passes.
        item["allowed_channels"] = ["ai", "campaign", "autopilot"]
        rows.append(item)
    return rows


def run(context: Context, monkeypatch: pytest.MonkeyPatch, result: pt.PromotionTruthResult,
        *, limit: int = tool.MAX_PROMOTIONS, policy: Any = None,
        entitlement: Optional[cer.LevelEntitlement] = None, levels: Any = None) -> Any:
    """Run the tool with the resolver, the merchant policy and the entitlement
    replaced by doubles. ``policy`` may be a dict, or an exception instance the
    policy read raises. ``entitlement`` defaults to a customer who reached every
    canonical rung, so a test that says nothing about levels is testing
    something else."""
    import services.coupon_generator as generator

    calls: List[Any] = []

    def resolver(db: Any, tenant_id: int, **kwargs: Any) -> pt.PromotionTruthResult:
        calls.append((db, tenant_id, kwargs))
        return result

    def read_policy(db: Any, tenant_id: int) -> Dict[str, Any]:
        if isinstance(policy, BaseException):
            raise policy
        return dict(DEFAULT_POLICY if policy is None else policy)

    def read_entitlement(db: Any, tenant_id: int, customer_id: Any) -> cer.LevelEntitlement:
        context.standing_reads.append((db, tenant_id, customer_id))
        return entitlement if entitlement is not None else entitled("vip")

    monkeypatch.setattr(tool, "resolve_shareable_promotions", resolver)
    def read_block(db: Any, tenant_id: int) -> Dict[str, Any]:
        if isinstance(levels, BaseException):
            raise levels
        return {"levels": _normalised_levels() if levels is None else levels}

    monkeypatch.setattr(tool, "resolve_level_entitlement", read_entitlement)
    monkeypatch.setattr(generator, "_get_ai_policy", read_policy)
    monkeypatch.setattr(generator, "_get_coupon_dashboard_block", read_block)
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
    """The merchant's store-wide gate, on its own. The customer here has
    reached every rung, so only the policy can be what removes gold."""
    context = Context()
    result, _ = run(context, monkeypatch, truth(shareable=[
        coupon_fact(promotion_id=1, code="GOLD50", coupon_level="gold"),
        coupon_fact(promotion_id=2, code="SILVER15", coupon_level="silver"),
        coupon_fact(promotion_id=3, code="WELCOME10", coupon_level=""),
    ]), policy={**DEFAULT_POLICY, "allowed_levels": ["bronze", "silver"]},
        entitlement=entitled("silver"))
    assert [p.code for p in result.promotions] == ["SILVER15", "WELCOME10"]
    assert result.promotions[0].coupon_level == "silver"


# ── The customer's own standing ──────────────────────────────────────────────


def _ladder_facts():
    """One coupon per rung plus one tied to no rung, in a neutral store."""
    return [
        coupon_fact(promotion_id=1, code="GOLD50", coupon_level="gold", conditions={}),
        coupon_fact(promotion_id=2, code="SILVER15", coupon_level="silver", conditions={}),
        coupon_fact(promotion_id=3, code="BRONZE5", coupon_level="bronze", conditions={}),
        # Unleveled, and published to a shared surface by the merchant.
        coupon_fact(promotion_id=4, code="WELCOME10", coupon_level="", conditions={},
                    source_type="manual", allocation_channel="shared"),
    ]


def test_a_rung_this_customer_has_not_reached_never_leaves_the_records(monkeypatch) -> None:
    """«وجود كوبون صالح لا يعني أن كل عميل مؤهل له». The gold code is live,
    shareable and allowed by the store — and this customer is silver, so it is
    not in the list at all. Withheld, not labelled: the assistant cannot offer
    what it never saw."""
    context = Context()
    result, _ = run(context, monkeypatch, truth(shareable=_ladder_facts()),
                    policy={**DEFAULT_POLICY, "allowed_levels": None},
                    entitlement=entitled("silver", orders=4))
    assert [p.code for p in result.promotions] == ["SILVER15", "WELCOME10"]
    assert [e.ref for e in result.evidence] == ["promotion:coupon:2", "promotion:coupon:4"]
    assert result.entitlement["resolved_level"] == "silver"
    assert result.entitlement["entitled_levels"] == ["silver"]
    assert result.entitlement["determined"] is True
    # Restated from an earlier draft that also returned BRONZE5, on the
    # reasoning that ``min_orders`` is a minimum and this customer had passed
    # bronze's. That put three rungs of one ladder in front of the model at
    # once and so recreated, smaller, the problem this gate exists to solve.
    # A customer has one standing, and the contract already says which.


def test_both_gates_must_open_and_either_one_can_close(monkeypatch) -> None:
    """The store's policy and the customer's standing are separate questions,
    and each answers a different one.

    The policy says which rungs the assistant may ever mention in this store.
    The standing says how far up the ladder this customer has come. Neither
    overrides the other, and the rung actually served is the highest the policy
    permits *at or below* the standing.
    """
    facts = truth(shareable=_ladder_facts())
    allowed_but_unearned, _ = run(Context(), monkeypatch, facts,
                                  policy={**DEFAULT_POLICY, "allowed_levels": ["gold"]},
                                  entitlement=entitled("bronze"))
    # A store that allows gold does not make this conversation gold. Upward is
    # the direction the gate must never move, and it still does not.
    assert [p.code for p in allowed_but_unearned.promotions] == ["WELCOME10"]

    earned_but_unallowed, _ = run(Context(), monkeypatch, facts,
                                  policy={**DEFAULT_POLICY, "allowed_levels": ["bronze", "silver"]},
                                  entitlement=entitled("gold"))
    # Downward is different, and this is the owner's decision of 2026-09-23:
    # a gold customer in a store that keeps gold from the assistant is a silver
    # customer *on this channel*, not a customer with nothing. Silver is what
    # the store said the assistant may give, so silver is what they get — one
    # rung, the best permitted, never bronze beside it and never gold.
    assert [p.code for p in earned_but_unallowed.promotions] == ["SILVER15", "WELCOME10"]
    assert earned_but_unallowed.withheld == {tool.WITHHELD_LEVEL_NOT_ALLOWED: 1,
                                             tool.WITHHELD_LEVEL_NOT_SERVED: 1}
    # And their standing is untouched: serving silver demotes nobody.
    assert earned_but_unallowed.entitlement["resolved_level"] == "gold"
    assert earned_but_unallowed.entitlement["served_level"] == "silver"


def test_a_customer_who_could_not_be_classified_still_sees_what_was_never_about_class(monkeypatch) -> None:
    """The owner's rule: «عند غياب مستوى مستحق، لا تُحجب العروض العامة غير
    المشروطة بذلك المستوى». No rung is granted on a standing nobody could
    read — and nothing tied to no rung is taken away for it either."""
    context = Context(customer_id=None)
    result, _ = run(context, monkeypatch,
                    truth(shareable=_ladder_facts(), offers=[offer_fact()]),
                    policy={**DEFAULT_POLICY, "allowed_levels": None}, entitlement=unknown())
    assert [p.code for p in result.promotions] == ["WELCOME10", ""]
    assert [p.record_kind for p in result.promotions] == ["coupon", "offer"]
    assert result.entitlement["determined"] is False
    assert result.entitlement["reason"] == cer.REASON_IDENTITY_NOT_ESTABLISHED
    # Nothing about this customer was settled, and the projection says so
    # rather than presenting an unconditioned code as verified for them.
    unconditioned = result.promotions[0]
    assert unconditioned.level_eligibility == "merchant_authorized_general"
    assert unconditioned.customer_level == ""
    assert unconditioned.eligibility_determined is False
    assert unconditioned.eligibility_note == (
        f"customer_standing_not_determined:{cer.REASON_IDENTITY_NOT_ESTABLISHED}")


def test_a_known_customer_with_no_purchases_is_told_apart_from_one_nobody_could_verify(monkeypatch) -> None:
    """Two customers, no rung either way, two different reasons on the record.
    One was read and had not bought yet; the other could not be read at all."""
    facts = truth(shareable=_ladder_facts())
    read_and_empty, _ = run(Context(), monkeypatch, facts, policy={**DEFAULT_POLICY, "allowed_levels": None},
                            entitlement=entitled(None, orders=0, reason=cer.REASON_NO_ENTITLED_LEVEL))
    unreadable, _ = run(Context(), monkeypatch, facts, policy={**DEFAULT_POLICY, "allowed_levels": None},
                        entitlement=unknown(cer.REASON_CUSTOMER_RECORD_UNAVAILABLE, customer_id=41))

    assert [p.code for p in read_and_empty.promotions] == ["WELCOME10"]
    assert [p.code for p in unreadable.promotions] == ["WELCOME10"]
    assert read_and_empty.entitlement["determined"] is True
    assert read_and_empty.entitlement["countable_orders"] == 0
    assert unreadable.entitlement["determined"] is False
    assert unreadable.entitlement["countable_orders"] is None
    # Same coupons, different standing — and the difference survives into the
    # projection, where a reader can act on it.
    assert read_and_empty.promotions[0].eligibility_determined is True
    assert unreadable.promotions[0].eligibility_determined is False


def test_the_merchants_first_purchase_welcome_reaches_a_new_customer(monkeypatch) -> None:
    """«نريد أيضًا دعم تشجيع العميل الجديد بكوبون شراء أول وفق إعدادات
    التاجر». With the merchant's rule on, a customer with no orders reaches
    bronze — and bronze alone."""
    context = Context()
    result, _ = run(context, monkeypatch, truth(shareable=_ladder_facts()),
                    policy={**DEFAULT_POLICY, "allowed_levels": None},
                    entitlement=entitled("bronze", orders=0, reason=cer.REASON_FIRST_PURCHASE,
                                         first_purchase=True))
    assert [p.code for p in result.promotions] == ["BRONZE5", "WELCOME10"]
    assert result.entitlement["first_purchase_applied"] is True
    assert result.promotions[0].level_reason == cer.REASON_FIRST_PURCHASE


def test_a_personal_code_survives_a_standing_that_could_not_be_read(monkeypatch) -> None:
    """The merchant's own record names this customer, which settles who they
    are whatever the order index says. It is still the same customer's code and
    nobody else's."""
    context = Context(customer_id=41)
    mine = coupon_fact(promotion_id=6, code="AHMED20", customer_bound=True, bound_customer_id=41,
                       conditions={})
    result, _ = run(context, monkeypatch, truth(shareable=[mine]),
                    entitlement=unknown(cer.REASON_ORDER_HISTORY_UNREADABLE, customer_id=41))
    assert [p.code for p in result.promotions] == ["AHMED20"]
    assert result.promotions[0].bound_to_this_customer is True
    assert result.promotions[0].eligibility_determined is True
    assert result.entitlement["determined"] is False


def test_the_standing_is_read_once_for_this_tenant_and_this_customer(monkeypatch) -> None:
    """One reading, before anything is projected, so every coupon in a list is
    judged against the same customer — an order landing mid-turn cannot make
    one rung's code appear beside another's."""
    context = Context(tenant_id=7, customer_id=41)
    run(context, monkeypatch, truth(shareable=_ladder_facts()))
    assert context.standing_reads == [(context.db, 7, 41)]


def test_a_generic_store_holds_the_same_rule_across_categories(monkeypatch) -> None:
    """متجر تجريبي عام: shoes and perfume, a gold ladder and a customer who
    reached silver. Nothing here is specific to one merchant or one category."""
    context = Context(tenant_id=88, customer_id=502)
    facts = [
        coupon_fact(promotion_id=11, code="SHOES30", coupon_level="gold",
                    description="خصم على الأحذية الرياضية", conditions={}),
        coupon_fact(promotion_id=12, code="SCENT10", coupon_level="silver",
                    description="عطر ورد 100ml", conditions={"min_order_amount": "150"}),
        coupon_fact(promotion_id=13, code="OPEN5", coupon_level="", description="خصم عام",
                    conditions={}, source_type="manual", allocation_channel="shared"),
    ]
    result, _ = run(context, monkeypatch, truth(shareable=facts),
                    policy={**DEFAULT_POLICY, "allowed_levels": None},
                    entitlement=entitled("silver", orders=3, customer_id=502))
    assert [p.code for p in result.promotions] == ["SCENT10", "OPEN5"]
    scent, open_code = result.promotions
    assert scent.level_eligibility == "entitled" and scent.customer_level == "silver"
    # The rung is settled; the basket minimum is not, and is named.
    assert scent.eligibility_determined is False
    assert scent.eligibility_note == "conditions_not_fully_evaluated:min_order_amount"
    assert open_code.level_eligibility == "merchant_authorized_general"
    assert open_code.eligibility_determined is True and open_code.eligibility_note == ""


def test_an_offer_never_claims_its_own_eligibility(monkeypatch) -> None:
    """An offer is terms, not a grant, and a record with no conditions cannot
    be told apart from conditions nobody read. Its note keeps the resolver's
    own word for that."""
    context = Context()
    result, _ = run(context, monkeypatch, truth(offers=[offer_fact()]))
    offer = result.promotions[0]
    assert offer.eligibility_determined is False
    assert offer.eligibility_note == "offer_terms_only_no_code_invented"
    assert offer.level_eligibility == "merchant_authorized_general"


def test_an_honest_empty_answer_still_says_on_what_reading_it_was_empty(monkeypatch) -> None:
    """The difference between "this store has nothing for anyone" and "we could
    not work out who you are" is the difference between an answer and a shrug."""
    context = Context(customer_id=None)
    result, _ = run(context, monkeypatch, truth(outcome=pt.NO_VALID_PROMOTIONS), entitlement=unknown())
    assert result.status == "not_found" and result.promotions == []
    assert result.entitlement["determined"] is False
    assert result.entitlement["reason"] == cer.REASON_IDENTITY_NOT_ESTABLISHED


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


def test_the_evidence_names_where_the_entitlement_came_from(monkeypatch) -> None:
    """Provenance is a trace, not a label. The record says the coupon exists
    and is live; who may use it was settled somewhere else, and the evidence
    says where and on what reading — so a reviewer can follow the claim back
    without reading this module."""
    context = Context()
    result, _ = run(context, monkeypatch,
                    truth(shareable=[coupon_fact(promotion_id=2, code="SILVER15",
                                                 coupon_level="silver", conditions={})]),
                    policy={**DEFAULT_POLICY, "allowed_levels": None},
                    entitlement=entitled("silver"))
    provenance = result.evidence[0].provenance
    assert provenance["service"].endswith("resolve_shareable_promotions")
    assert provenance["entitlement_service"] == (
        "services.coupon_entitlement_read.resolve_level_entitlement")
    assert provenance["entitlement_reason"] == cer.REASON_ENTITLED
    # And the snapshot's own fields agree with it.
    assert result.evidence[0].fields["level_eligibility"] == "entitled"
    assert result.evidence[0].fields["customer_level"] == "silver"


# ── A general offer is a merchant's act, never an absence ────────────────────


def test_an_unleveled_code_the_merchant_never_published_is_withheld(monkeypatch) -> None:
    """«كون الكود shared وحده لا يثبت أنه متاح للجميع». Carrying no rung says
    only that the record does not name one. It is not evidence the merchant
    meant the code for everyone, so without a positive act it does not reach
    the assistant at all."""
    context = Context()
    unplaced = coupon_fact(promotion_id=21, code="NOCHAN", coupon_level="",
                           source_type="manual", allocation_channel="")
    result, _ = run(context, monkeypatch, truth(shareable=[unplaced]),
                    entitlement=entitled("silver"))
    assert result.status == "not_found" and result.promotions == []


def test_the_warm_pool_s_defaulted_shared_channel_is_not_a_merchant_decision(monkeypatch) -> None:
    """The exact trap. ``coupon_generator`` writes ``allocation_channel``
    "shared" by default on every pool coupon it creates, so "shared" alone
    records a default rather than a choice. A system-sourced code is therefore
    not a general offer however its channel reads."""
    context = Context()
    pooled = coupon_fact(promotion_id=22, code="POOLED", coupon_level="",
                         source_type="system", allocation_channel="shared")
    result, _ = run(context, monkeypatch, truth(shareable=[pooled]),
                    entitlement=entitled("silver"))
    assert result.status == "not_found" and result.promotions == []


def test_a_code_the_merchant_created_and_placed_is_a_general_offer(monkeypatch) -> None:
    """The two acts together: created in the merchant's own dashboard, and put
    on an AI-reachable surface. Neither is inferred from an absence."""
    context = Context()
    published = coupon_fact(promotion_id=23, code="OPEN10", coupon_level="",
                            source_type="manual", allocation_channel="shared", conditions={})
    result, _ = run(context, monkeypatch, truth(shareable=[published]),
                    entitlement=entitled("silver"))
    assert [p.code for p in result.promotions] == ["OPEN10"]
    assert result.promotions[0].level_eligibility == "merchant_authorized_general"

    on_the_ai_channel = coupon_fact(promotion_id=24, code="AIONLY", coupon_level="",
                                    source_type="manual", allocation_channel="ai", conditions={})
    also, _ = run(Context(), monkeypatch, truth(shareable=[on_the_ai_channel]),
                  entitlement=entitled("silver"))
    assert [p.code for p in also.promotions] == ["AIONLY"]


def test_a_general_offer_reaches_a_customer_nobody_could_classify(monkeypatch) -> None:
    """The rule the owner asked to preserve, now resting on the merchant's act
    rather than on the absence of a rung: an unidentified conversation still
    receives what the merchant published for everyone."""
    context = Context(customer_id=None)
    published = coupon_fact(promotion_id=25, code="OPEN10", coupon_level="",
                            source_type="manual", allocation_channel="shared", conditions={})
    levelled = coupon_fact(promotion_id=26, code="GOLD50", coupon_level="gold")
    result, _ = run(context, monkeypatch, truth(shareable=[levelled, published]),
                    policy={**DEFAULT_POLICY, "allowed_levels": None}, entitlement=unknown())
    assert [p.code for p in result.promotions] == ["OPEN10"]
    assert result.entitlement["determined"] is False


def test_an_imported_code_expresses_no_nahla_intent(monkeypatch) -> None:
    """A coupon synced in from the store platform was never placed on an AI
    surface in Nahla's dashboard, so it carries no authorisation to read."""
    context = Context()
    imported = coupon_fact(promotion_id=27, code="FROMSALLA", coupon_level="",
                           source_type="imported", allocation_channel="shared")
    result, _ = run(context, monkeypatch, truth(shareable=[imported]),
                    entitlement=entitled("silver"))
    assert result.status == "not_found" and result.promotions == []


# ── Why nothing arrived ──────────────────────────────────────────────────────


def test_every_gate_that_closes_names_itself_in_the_withheld_count(monkeypatch) -> None:
    """Six live promotions, six different reasons none of them reached this
    customer. The visible outcome is identical in all six cases — no codes —
    and the counts are the only thing that tells a merchant which rule fired."""
    context = Context()
    facts = [
        coupon_fact(promotion_id=0, code="NOID"),                                # unusable id
        coupon_fact(promotion_id=31, code=""),                                   # coupon with no code
        coupon_fact(promotion_id=32, code="HERS", customer_bound=True, bound_customer_id=99),
        coupon_fact(promotion_id=33, code="VIP50", coupon_level="vip"),          # policy forbids vip
        coupon_fact(promotion_id=34, code="GOLD50", coupon_level="gold"),        # allowed, not earned
        coupon_fact(promotion_id=35, code="FROMSALLA", coupon_level="", source_type="imported"),
    ]
    result, _ = run(context, monkeypatch, truth(shareable=facts),
                    policy={**DEFAULT_POLICY, "allowed_levels": ["bronze", "silver", "gold"]},
                    entitlement=entitled("silver"))
    assert result.status == "not_found" and result.promotions == []
    assert result.withheld == {
        tool.WITHHELD_UNUSABLE_RECORD: 2,
        tool.WITHHELD_PERSONAL_TO_ANOTHER: 1,
        tool.WITHHELD_LEVEL_NOT_ALLOWED: 1,
        tool.WITHHELD_LEVEL_NOT_EARNED: 1,
        tool.WITHHELD_NOT_PUBLISHED: 1,
    }


def test_a_code_dropped_for_expiring_too_soon_is_counted_under_its_own_reason(monkeypatch) -> None:
    """The merchant's minimum-life rule is a separate gate from eligibility,
    and a merchant asking "why did my customer get nothing?" needs to see that
    it was the clock and not the classification."""
    now = _fixed_now(monkeypatch)
    context = Context()
    expiring = coupon_fact(promotion_id=36, code="TONIGHT",
                           expires_at=(now + timedelta(hours=2)).isoformat())
    result, _ = run(context, monkeypatch, truth(shareable=[expiring]),
                    policy={**DEFAULT_POLICY, "min_remaining_hours": 24},
                    entitlement=entitled("silver"))
    assert result.status == "not_found"
    assert result.withheld == {tool.WITHHELD_EXPIRING_TOO_SOON: 1}


def test_a_store_with_nothing_to_share_is_told_apart_from_a_store_that_held_it_back(monkeypatch) -> None:
    """Both answers are "no codes". One means the merchant published none; the
    other means the merchant published eight and this customer earned none.
    Before the counts existed these were the same reply to the model."""
    nothing_published, _ = run(Context(), monkeypatch, truth(shareable=[]),
                               entitlement=entitled("bronze", orders=1))
    held_back, _ = run(Context(), monkeypatch, truth(shareable=[
        coupon_fact(promotion_id=40 + i, code=f"SILVER{i}", coupon_level="silver") for i in range(8)]),
        entitlement=entitled("bronze", orders=1))
    assert nothing_published.status == held_back.status == "not_found"
    assert nothing_published.withheld == {}
    assert held_back.withheld == {tool.WITHHELD_LEVEL_NOT_EARNED: 8}
    # And the standing is on both, so "earned none" can be read against orders.
    assert nothing_published.entitlement["resolved_level"] == "bronze"
    assert held_back.entitlement["countable_orders"] == 1


def test_the_withheld_counts_carry_reasons_only_never_a_code(monkeypatch) -> None:
    """A code held back is still a code. The counts explain a refusal; they must
    never become a second way to read what was refused."""
    context = Context()
    result, _ = run(context, monkeypatch, truth(shareable=[
        coupon_fact(promotion_id=50, code="SECRETVIP", coupon_level="vip"),
        coupon_fact(promotion_id=51, code="HERS", customer_bound=True, bound_customer_id=99)]),
        policy={**DEFAULT_POLICY, "allowed_levels": ["bronze"]}, entitlement=entitled("bronze"))
    blob = repr(result.withheld)
    assert "SECRETVIP" not in blob and "HERS" not in blob and "99" not in blob
    assert all(isinstance(count, int) for count in result.withheld.values())


def test_a_kept_result_still_reports_what_was_held_back_beside_it(monkeypatch) -> None:
    """A short list is as much a question as an empty one: the merchant wants to
    know that six of the seven were withheld, and why."""
    context = Context()
    facts = [coupon_fact(promotion_id=60, code="OPEN10", coupon_level="", conditions={})]
    facts += [coupon_fact(promotion_id=61 + i, code=f"SILVER{i}", coupon_level="silver") for i in range(6)]
    result, _ = run(context, monkeypatch, truth(shareable=facts), entitlement=entitled("bronze"))
    assert result.status == "ok" and [p.code for p in result.promotions] == ["OPEN10"]
    assert result.withheld == {tool.WITHHELD_LEVEL_NOT_EARNED: 6}


# ── The rung a store may serve, when the earned one is closed ────────────────
#
# Reproduced from production. Tenant 1, customer 66, conversation 9, turn 26,
# 2026-09-23 18:30:50 UTC, on the deployed 5bd70210:
#
#   [PROMOTION_PROJECTION] tenant=1 customer=yes considered=8 kept=0
#   withheld=level_not_allowed_by_store_policy:2,level_not_earned_by_customer:6
#   standing=entitled_by_order_count level=gold determined=True
#
# A gold customer, gold closed to the assistant, and six lower-rung codes the
# store *does* allow — and the customer left with nothing. The issuance service
# already walks down to the best permitted rung; this tool did not, so reading
# and issuing disagreed about who the customer is on this channel.


def test_the_production_incident_a_gold_customer_and_a_store_that_closed_gold(monkeypatch) -> None:
    """The shape that reached the customer: considered many, kept none.

    Two gold codes the store keeps from the assistant, six silver ones it
    allows. Standing is gold, so under exact-rung matching the silver codes are
    "not earned" — by a customer who has earned more than silver.
    """
    facts = [coupon_fact(promotion_id=100 + i, code=f"GOLD{i}", coupon_level="gold") for i in range(2)]
    facts += [coupon_fact(promotion_id=110 + i, code=f"SILVER{i}", coupon_level="silver") for i in range(6)]
    result, _ = run(Context(), monkeypatch, truth(shareable=facts),
                    policy={**DEFAULT_POLICY, "allowed_levels": ["bronze", "silver"]},
                    entitlement=entitled("gold", orders=7))
    assert result.status == "ok", "a store that allows silver has something for a gold customer"
    assert [p.code for p in result.promotions] == [f"SILVER{i}" for i in range(6)]
    # The two gold codes are still refused, by the gate that really refused them.
    assert result.withheld == {tool.WITHHELD_LEVEL_NOT_ALLOWED: 2}
    # And the customer is still gold. Serving silver does not demote anyone.
    assert result.entitlement["resolved_level"] == "gold"
    assert result.entitlement["served_level"] == "silver"


def test_serving_a_lower_rung_never_becomes_serving_every_lower_rung(monkeypatch) -> None:
    """One rung, still. The highest the store permits at or below the earned one.

    This is the failure the entitlement module was written to prevent: a gold
    customer seeing bronze, silver and gold codes at once. Walking down must
    not reopen it.
    """
    facts = [coupon_fact(promotion_id=120, code="BRONZE5", coupon_level="bronze"),
             coupon_fact(promotion_id=121, code="SILVER20", coupon_level="silver")]
    result, _ = run(Context(), monkeypatch, truth(shareable=facts),
                    policy={**DEFAULT_POLICY, "allowed_levels": ["bronze", "silver"]},
                    entitlement=entitled("gold", orders=7))
    assert [p.code for p in result.promotions] == ["SILVER20"]
    assert result.withheld == {tool.WITHHELD_LEVEL_NOT_SERVED: 1}


def test_an_intermediate_blocked_rung_is_stepped_over_not_stopped_at(monkeypatch) -> None:
    """Silver closed, bronze open: the walk continues rather than giving up."""
    facts = [coupon_fact(promotion_id=130, code="SILVER20", coupon_level="silver"),
             coupon_fact(promotion_id=131, code="BRONZE5", coupon_level="bronze")]
    result, _ = run(Context(), monkeypatch, truth(shareable=facts),
                    policy={**DEFAULT_POLICY, "allowed_levels": ["bronze"]},
                    entitlement=entitled("gold", orders=7))
    assert [p.code for p in result.promotions] == ["BRONZE5"]
    assert result.withheld == {tool.WITHHELD_LEVEL_NOT_ALLOWED: 1}
    assert result.entitlement["served_level"] == "bronze"


def test_the_walk_never_goes_upward(monkeypatch) -> None:
    """A bronze customer in a store that allows only gold still gets nothing.

    Walking down serves less than was earned. Walking up would invent an
    entitlement, which is the failure the whole gate exists to prevent.
    """
    result, _ = run(Context(), monkeypatch,
                    truth(shareable=[coupon_fact(promotion_id=140, code="GOLD50", coupon_level="gold")]),
                    policy={**DEFAULT_POLICY, "allowed_levels": ["gold"]},
                    entitlement=entitled("bronze", orders=1))
    assert result.status == "not_found" and result.promotions == []
    assert result.withheld == {tool.WITHHELD_LEVEL_NOT_EARNED: 1}
    assert result.entitlement["resolved_level"] == "bronze"
    assert result.entitlement["served_level"] == ""


def test_a_disabled_merchant_policy_still_denies_before_any_rung_is_chosen(monkeypatch) -> None:
    """The store switch comes first. No walk, no list, and the reason says so."""
    result, _ = run(Context(), monkeypatch,
                    truth(shareable=[coupon_fact(promotion_id=150, code="SILVER20", coupon_level="silver")]),
                    policy={**DEFAULT_POLICY, "enabled": False},
                    entitlement=entitled("gold", orders=7))
    assert result.status == "denied"
    assert result.failure_reason == "merchant_ai_coupon_policy_disabled"
    assert result.promotions == []


def test_a_standing_that_could_not_be_read_serves_no_rung_at_all(monkeypatch) -> None:
    """An unreadable history entitles nothing, and walking down from nothing
    reaches nothing. A failure to determine never becomes a lower tier."""
    for reason in (cer.REASON_IDENTITY_NOT_ESTABLISHED, cer.REASON_ORDER_HISTORY_NOT_SEARCHABLE,
                   cer.REASON_ORDER_HISTORY_UNREADABLE, cer.REASON_CUSTOMER_RECORD_UNAVAILABLE):
        result, _ = run(Context(), monkeypatch,
                        truth(shareable=[coupon_fact(promotion_id=160, code="SILVER20",
                                                     coupon_level="silver")]),
                        policy={**DEFAULT_POLICY, "allowed_levels": ["bronze", "silver"]},
                        entitlement=unknown(reason))
        assert result.status == "not_found", reason
        assert result.withheld == {tool.WITHHELD_LEVEL_NOT_EARNED: 1}, reason
        assert result.entitlement["served_level"] == "", reason


def test_walking_down_never_reaches_another_customer_s_personal_code(monkeypatch) -> None:
    """Tenant and personal isolation are untouched by which rung is served."""
    facts = [coupon_fact(promotion_id=170, code="HERS", coupon_level="silver",
                         customer_bound=True, bound_customer_id=99),
             coupon_fact(promotion_id=171, code="MINE", coupon_level="silver",
                         customer_bound=True, bound_customer_id=41)]
    result, _ = run(Context(), monkeypatch, truth(shareable=facts),
                    policy={**DEFAULT_POLICY, "allowed_levels": ["bronze", "silver"]},
                    entitlement=entitled("gold", orders=7))
    assert [p.code for p in result.promotions] == ["MINE"]
    assert result.withheld == {tool.WITHHELD_PERSONAL_TO_ANOTHER: 1}


def test_the_served_rung_does_not_excuse_the_store_s_expiry_minimum(monkeypatch) -> None:
    """Every other gate still runs on the rung that gets served."""
    now = _fixed_now(monkeypatch)
    facts = [coupon_fact(promotion_id=180, code="TONIGHT", coupon_level="silver",
                         expires_at=(now + timedelta(hours=2)).isoformat()),
             coupon_fact(promotion_id=181, code="NEXTWEEK", coupon_level="silver",
                         expires_at=(now + timedelta(days=7)).isoformat())]
    result, _ = run(Context(), monkeypatch, truth(shareable=facts),
                    policy={**DEFAULT_POLICY, "allowed_levels": ["bronze", "silver"],
                            "min_remaining_hours": 24},
                    entitlement=entitled("gold", orders=7))
    assert [p.code for p in result.promotions] == ["NEXTWEEK"]
    assert result.withheld == {tool.WITHHELD_EXPIRING_TOO_SOON: 1}


def test_reading_and_issuing_choose_the_same_rung(monkeypatch) -> None:
    """Parity. The tool and the issuance service must not disagree about which
    rung a customer is served, or the assistant offers one code and the
    platform hands over another."""
    from services.coupon_level_contract import policy_served_level

    levels = _normalised_levels()
    for earned, allowed, expected in (("gold", ["bronze", "silver"], "silver"),
                                      ("gold", ["bronze"], "bronze"),
                                      ("vip", ["bronze", "gold"], "gold"),
                                      ("gold", ["gold", "silver"], "gold"),
                                      ("bronze", ["gold"], None)):
        assert policy_served_level(levels, earned, channel="ai", policy_levels=allowed) == expected
        result, _ = run(Context(), monkeypatch,
                        truth(shareable=[coupon_fact(promotion_id=190, code="X",
                                                     coupon_level=expected or "gold")]),
                        policy={**DEFAULT_POLICY, "allowed_levels": allowed},
                        entitlement=entitled(earned, orders=7))
        assert result.entitlement["served_level"] == (expected or ""), (earned, allowed)


def test_a_generic_two_rung_store_serves_below_the_earned_rung_too(monkeypatch) -> None:
    """Platform-wide: a plain catalogue store, nothing like the shipped ladder."""
    result, _ = run(Context(), monkeypatch,
                    truth(shareable=[coupon_fact(promotion_id=200, code="GEN5",
                                                 coupon_level="bronze")]),
                    policy={**DEFAULT_POLICY, "allowed_levels": ["bronze"]},
                    entitlement=entitled("silver", orders=5))
    assert [p.code for p in result.promotions] == ["GEN5"]
    assert result.entitlement["resolved_level"] == "silver"
    assert result.entitlement["served_level"] == "bronze"


def test_an_unreadable_source_still_reports_the_gates_that_ran_before_it_failed(monkeypatch) -> None:
    """``error`` is about the records. Whatever was settled before the read
    failed is still true, and throwing it away makes the failure look total."""
    context = Context()
    result, _ = run(context, monkeypatch,
                    truth(shareable=[coupon_fact(promotion_id=70, code="SILVER50", coupon_level="silver")],
                          query_failed=True, outcome=pt.PROMOTION_QUERY_FAILED),
                    entitlement=entitled("bronze"))
    assert result.status == "error"
    assert result.withheld == {tool.WITHHELD_LEVEL_NOT_EARNED: 1}
    assert result.entitlement["resolved_level"] == "bronze"


def test_a_generic_store_reports_the_same_reasons_as_any_other(monkeypatch) -> None:
    """Platform-wide, not one category: a clothing store's withheld counts read
    exactly like a perfume store's, because the gates are the platform's."""
    for code, level in (("QAMEES20", "silver"), ("ATTAR15", "silver"), ("HITHAA30", "silver")):
        result, _ = run(Context(), monkeypatch,
                        truth(shareable=[coupon_fact(promotion_id=80, code=code, coupon_level=level)]),
                        entitlement=entitled("bronze"))
        assert result.status == "not_found"
        assert result.withheld == {tool.WITHHELD_LEVEL_NOT_EARNED: 1}


def test_the_projection_log_says_how_many_were_considered_and_what_took_them(monkeypatch) -> None:
    """One line a person can read in production without a transcript. It carries
    counts and reasons — no code, no customer identifier.

    Captured off the module's own logger rather than through propagation, so
    whatever the rest of the suite has done to the root logger cannot decide
    whether this passes."""
    import logging

    records: List[logging.LogRecord] = []

    class Capture(logging.Handler):
        def emit(self, record: logging.LogRecord) -> None:
            records.append(record)

    handler = Capture(level=logging.INFO)
    previous_level, previously_disabled = tool.logger.level, tool.logger.disabled
    tool.logger.addHandler(handler)
    tool.logger.setLevel(logging.INFO)
    # Alembic's ``fileConfig``, run by a migration suite earlier in the same
    # process, disables every logger created before it. The line exists in
    # production; this makes sure the measurement can hear it.
    tool.logger.disabled = False
    context = Context()
    facts = [coupon_fact(promotion_id=90, code="OPEN10", coupon_level="", conditions={})]
    facts += [coupon_fact(promotion_id=91 + i, code=f"SILVER{i}", coupon_level="silver") for i in range(3)]
    try:
        run(context, monkeypatch, truth(shareable=facts), entitlement=entitled("bronze"))
    finally:
        tool.logger.removeHandler(handler)
        tool.logger.setLevel(previous_level)
        tool.logger.disabled = previously_disabled
    line = next(r.getMessage() for r in records if "[PROMOTION_PROJECTION]" in r.getMessage())
    assert "considered=4" in line and "kept=1" in line
    assert f"{tool.WITHHELD_LEVEL_NOT_EARNED}:3" in line
    assert "standing=entitled_by_order_count" in line and "level=bronze" in line
    assert "OPEN10" not in line and "SILVER0" not in line


# ── The merchant's declared general coupon ───────────────────────────────────


def test_a_general_promotional_coupon_the_merchant_declared_reaches_the_customer(monkeypatch) -> None:
    """«الكوبون الترويجي العام يبقى مشتركاً» — the dashboard's own words for the
    option, and the merchant choosing it is the declaration this gate asks for.
    The code carries no rung and names no channel because a general coupon has
    neither; what it does carry is the merchant's creation act."""
    context = Context()
    declared = coupon_fact(promotion_id=100, code="EID20", coupon_level="",
                           source_type="manual", allocation_channel="",
                           merchant_authored=True, conditions={})
    result, _ = run(context, monkeypatch, truth(shareable=[declared]),
                    entitlement=entitled("silver"))
    assert [p.code for p in result.promotions] == ["EID20"]
    assert result.promotions[0].level_eligibility == "merchant_authorized_general"
    assert result.withheld == {}


def test_a_code_carrying_no_rung_and_no_act_is_still_withheld(monkeypatch) -> None:
    """The rule that has not moved: absence proves nothing. Same row, same empty
    channel, minus the merchant's creation marker — an import, a legacy row, a
    code that arrived from somewhere nobody recorded — and it stays out."""
    context = Context()
    unmarked = coupon_fact(promotion_id=101, code="MYSTERY", coupon_level="",
                           source_type="manual", allocation_channel="",
                           merchant_authored=False, conditions={})
    result, _ = run(context, monkeypatch, truth(shareable=[unmarked]),
                    entitlement=entitled("silver"))
    assert result.status == "not_found" and result.promotions == []
    assert result.withheld == {tool.WITHHELD_NOT_PUBLISHED: 1}


def test_a_channel_the_merchant_chose_elsewhere_keeps_the_code_there(monkeypatch) -> None:
    """A merchant who placed a code on the campaign or autopilot surface put it
    somewhere that is not this conversation. The creation act does not override
    a placement the same merchant made."""
    context = Context()
    for channel in ("campaign", "autopilot"):
        placed = coupon_fact(promotion_id=102, code="ELSEWHERE", coupon_level="",
                             source_type="manual", allocation_channel=channel,
                             merchant_authored=True, conditions={})
        result, _ = run(Context(), monkeypatch, truth(shareable=[placed]),
                        entitlement=entitled("silver"))
        assert result.status == "not_found", channel
        assert result.withheld == {tool.WITHHELD_NOT_PUBLISHED: 1}


def test_the_creation_act_never_opens_a_pool_or_an_imported_code(monkeypatch) -> None:
    """``merchant_authored`` is the dashboard's marker and nothing else writes
    it, but the source gate runs first regardless: a pool coupon and a synced
    coupon stay out even if their metadata ever claimed otherwise."""
    context = Context()
    for source in ("system", "imported", "promotion_rule"):
        claimed = coupon_fact(promotion_id=103, code="NOTYOURS", coupon_level="",
                              source_type=source, allocation_channel="",
                              merchant_authored=True, conditions={})
        result, _ = run(Context(), monkeypatch, truth(shareable=[claimed]),
                        entitlement=entitled("silver"))
        assert result.status == "not_found", source
        assert result.withheld == {tool.WITHHELD_NOT_PUBLISHED: 1}


def test_a_declared_general_coupon_reaches_a_customer_nobody_could_classify(monkeypatch) -> None:
    """The protection that must survive this change: an unidentified
    conversation still receives what the merchant published for everyone, and
    still receives nothing that was conditioned on a rung."""
    context = Context(customer_id=None)
    declared = coupon_fact(promotion_id=104, code="EID20", coupon_level="",
                           source_type="manual", allocation_channel="",
                           merchant_authored=True, conditions={})
    levelled = coupon_fact(promotion_id=105, code="GOLD50", coupon_level="gold")
    result, _ = run(context, monkeypatch, truth(shareable=[levelled, declared]),
                    policy={**DEFAULT_POLICY, "allowed_levels": None}, entitlement=unknown())
    assert [p.code for p in result.promotions] == ["EID20"]
    assert result.entitlement["determined"] is False


def test_a_generic_store_declares_its_general_coupon_the_same_way(monkeypatch) -> None:
    """Platform-wide: the declaration is the platform's, not one category's."""
    for code in ("QAMEES20", "ATTAR15", "HITHAA30"):
        declared = coupon_fact(promotion_id=106, code=code, coupon_level="",
                               source_type="manual", allocation_channel="",
                               merchant_authored=True, conditions={})
        result, _ = run(Context(), monkeypatch, truth(shareable=[declared]),
                        entitlement=entitled("bronze"))
        assert [p.code for p in result.promotions] == [code]

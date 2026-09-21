"""The read-only promotion tool's projection, offline.

What is proved here is the boundary between the platform's promotion-truth
resolver and the evidence the commerce runtime's loop verifies a reply
against: only what the resolver returned becomes citable, an offer never
gets a code, eligibility is never claimed, an unreadable source is never
reported as "no promotions", and the trusted scope is re-checked before the
read. The resolver itself is proved in its own module.
"""
from __future__ import annotations

import asyncio
import sys
from pathlib import Path
from typing import Any, Dict, List

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
for p in [str(REPO_ROOT), str(REPO_ROOT / "backend"), str(REPO_ROOT / "database")]:
    if p not in sys.path:
        sys.path.insert(0, p)

from modules.ai.brain.commerce import promotion_truth as pt  # noqa: E402
from modules.ai.commerce_agent_v2.tools import promotions as tool  # noqa: E402


class Context:
    """The trusted context as the tool sees it: scope, session, tenant, evidence."""

    def __init__(self, tenant_id: int = 7) -> None:
        self.tenant_id = tenant_id
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
        "source_type": "manual", "allocation_channel": "", "conditions": {"min_order_total": "100"},
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
        *, limit: int = tool.MAX_PROMOTIONS) -> Any:
    calls: List[Any] = []

    def resolver(db: Any, tenant_id: int, **kwargs: Any) -> pt.PromotionTruthResult:
        calls.append((db, tenant_id, kwargs))
        return result

    monkeypatch.setattr(tool, "resolve_shareable_promotions", resolver)
    outcome = asyncio.run(tool.list_shareable_promotions_impl(context, limit=limit))
    return outcome, calls


def test_a_shareable_coupon_and_an_offer_become_citable_evidence(monkeypatch) -> None:
    context = Context()
    result, calls = run(context, monkeypatch, truth(shareable=[coupon_fact()], offers=[offer_fact()]))
    assert result.status == "ok"
    assert [p.evidence_ref for p in result.promotions] == ["promotion:coupon:5", "promotion:offer:9"]
    assert [e.ref for e in result.evidence] == ["promotion:coupon:5", "promotion:offer:9"]
    assert [e.source for e in result.evidence] == ["promotion_coupon", "promotion_offer"]
    coupon, offer = result.promotions
    assert coupon.code == "WELCOME10" and coupon.conditions == {"min_order_total": "100"}
    assert coupon.discount_type == "percentage" and coupon.discount_value == "10"
    assert offer.code == "" and offer.discount_type == "free_shipping" and offer.name == "شحن مجاني"
    assert all(p.eligibility_determined is False for p in result.promotions)
    # The resolver was asked for this tenant, on this session, and the evidence
    # was registered on the trusted context.
    assert calls == [(context.db, 7, {"limit": tool.MAX_PROMOTIONS})]
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
    # The codeless coupon is dropped; the offer keeps its terms and gets no code.
    assert [p.evidence_ref for p in result.promotions] == ["promotion:offer:2"]
    assert result.promotions[0].code == ""


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


def test_the_projection_is_bounded_by_the_declared_maximum(monkeypatch) -> None:
    context = Context()
    many = [coupon_fact(promotion_id=i, code=f"C{i}") for i in range(1, 30)]
    result, calls = run(context, monkeypatch, truth(shareable=many), limit=50)
    assert len(result.promotions) == tool.MAX_PROMOTIONS
    assert calls[0][2] == {"limit": tool.MAX_PROMOTIONS}


def test_a_malformed_fact_is_skipped_rather_than_crashing_the_read(monkeypatch) -> None:
    context = Context()
    result, _ = run(context, monkeypatch, truth(shareable=[
        {"id": "x", "code": "BAD", "record_kind": "coupon"},
        {"id": 3, "code": "OK3", "record_kind": "mystery"},
        coupon_fact(promotion_id=4, code="OK4", conditions="not-a-dict"),
    ]))
    assert [p.evidence_ref for p in result.promotions] == ["promotion:coupon:4"]
    assert result.promotions[0].conditions == {}

"""PR-D8 — turn owner contract observability and pre-brain branch isolation."""
from __future__ import annotations

import os
import sys
from typing import Any, List, Optional, Sequence, Type

import pytest

_HERE = os.path.dirname(os.path.abspath(__file__))
_BACKEND = os.path.abspath(os.path.join(_HERE, ".."))
if _BACKEND not in sys.path:
    sys.path.insert(0, _BACKEND)

HEALTH_MSG = (
    "عندي أطفال عندهم تأخر نطق واحتمال طيف توحد ومشاكل أمعاء "
    "وش تنصحني من منتجاتكم؟"
)

PAYMENT_META = {
    "normalized_type": "document",
    "has_attached_media": True,
    "pdf_kind": "payment_receipt",
    "payment_evidence_status": "confirmed",
    "receipt_data": {"amount": 120},
}


class _BranchRow:
    def __init__(self, **kwargs: Any) -> None:
        for k, v in kwargs.items():
            setattr(self, k, v)


class _StructuredQuery:
    def __init__(self, rows: Sequence[Any]) -> None:
        self._rows = list(rows)

    def filter(self, *args: Any, **kwargs: Any) -> "_StructuredQuery":
        return self

    def order_by(self, *args: Any, **kwargs: Any) -> "_StructuredQuery":
        return self

    def first(self) -> Optional[Any]:
        return self._rows[0] if self._rows else None

    def all(self) -> List[Any]:
        return list(self._rows)


class _StructuredDB:
    def __init__(
        self,
        *,
        branches: Optional[List[Any]] = None,
        contacts: Optional[List[Any]] = None,
        steps: Optional[List[Any]] = None,
        keywords: Optional[List[Any]] = None,
    ) -> None:
        self.branches = branches or []
        self.contacts = contacts or []
        self.steps = steps or []
        self.keywords = keywords or []

    def query(self, model: Type[Any]) -> _StructuredQuery:
        name = getattr(model, "__name__", str(model))
        if name == "MerchantBranch":
            return _StructuredQuery(self.branches)
        if name == "BranchContact":
            return _StructuredQuery(self.contacts)
        if name == "BranchEscalationStep":
            return _StructuredQuery(self.steps)
        if name == "BranchArrivalKeyword":
            return _StructuredQuery(self.keywords)
        return _StructuredQuery([])


def _branch(**kwargs: Any) -> _BranchRow:
    defaults = dict(
        id=1,
        tenant_id=10,
        name="المعرض",
        city="",
        district="",
        address="",
        maps_url="https://maps.google.com/?q=branch1",
        sort_order=0,
        is_active=True,
        location_response_mode="location_only",
        arrival_response_mode="reception_only",
        location_instructions_text="",
    )
    defaults.update(kwargs)
    return _BranchRow(**defaults)


@pytest.fixture(autouse=True)
def _enable_structured_flag(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("USE_STRUCTURED_BRANCH_CONTACTS", "1")


def test_prebrain_health_contract_suppresses_branch_routing() -> None:
    from modules.ai.brain.commerce.branch_trigger_router import (
        evaluate_branch_trigger_routing,
    )
    from modules.ai.brain.turn_owner_contract import build_prebrain_route_contract

    contract = build_prebrain_route_contract(message=HEALTH_MSG)
    assert contract.block_staff_contact is True
    assert contract.block_showroom_location is True
    assert contract.suppress_reason == "health_advisory_current_turn"

    db = _StructuredDB(branches=[_branch()])
    decision = evaluate_branch_trigger_routing(
        db,
        tenant_id=10,
        message=HEALTH_MSG,
    )
    assert decision is None


def test_prebrain_payment_contract_suppresses_branch_routing() -> None:
    from modules.ai.brain.commerce.branch_trigger_router import (
        evaluate_branch_trigger_routing,
    )
    from modules.ai.brain.turn_owner_contract import build_prebrain_route_contract

    contract = build_prebrain_route_contract(
        message="",
        inbound_metadata=PAYMENT_META,
    )
    assert contract.owner == "payment_evidence"
    assert contract.block_catalog_push is True
    assert contract.suppress_reason == "payment_evidence_current_turn"

    db = _StructuredDB(branches=[_branch()])
    decision = evaluate_branch_trigger_routing(
        db,
        tenant_id=10,
        message="",
        inbound_metadata=PAYMENT_META,
    )
    assert decision is None


def test_explicit_location_still_routes_when_not_protected() -> None:
    from modules.ai.brain.commerce.branch_trigger_router import (
        evaluate_branch_trigger_routing,
    )
    from modules.ai.brain.turn_owner_contract import build_prebrain_route_contract

    contract = build_prebrain_route_contract(message="وين موقعكم؟")
    assert contract.suppresses_branch_routing() is False

    db = _StructuredDB(branches=[_branch()])
    decision = evaluate_branch_trigger_routing(
        db,
        tenant_id=10,
        message="وين موقعكم؟",
    )
    assert decision is not None
    assert decision.trigger_type == "location_request"


def test_summarize_turn_owner_contract_includes_blocked_postprocess() -> None:
    from modules.ai.brain.decision.actions import ACTION_LLM_REPLY
    from modules.ai.brain.turn_owner_contract import (
        POSTPROCESS_CATALOG_GROUNDING,
        TurnOwnerContract,
        build_turn_owner_contract,
        summarize_turn_owner_contract,
    )
    from modules.ai.brain.types import Decision

    decision = Decision(
        action=ACTION_LLM_REPLY,
        args={
            "topic": "health_advisory_product_safety",
            "block_catalog_push": True,
            "block_staff_contact": True,
        },
    )
    contract = build_turn_owner_contract(decision)
    summary = summarize_turn_owner_contract(contract)

    assert summary["owner"] == "health_advisory"
    assert summary["topic"] == "health_advisory_product_safety"
    assert summary["protected_final_reply"] is True
    assert summary["block_staff_contact"] is True
    assert POSTPROCESS_CATALOG_GROUNDING in summary["blocked_postprocess"]

    empty = summarize_turn_owner_contract(TurnOwnerContract())
    assert empty["protected_final_reply"] is False
    assert empty["blocked_postprocess"] == []

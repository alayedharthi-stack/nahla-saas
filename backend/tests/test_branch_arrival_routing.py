"""PR-C — branch arrival keyword routing and mode-aware delivery."""
from __future__ import annotations

import os
import sys
from typing import Any, List, Optional, Sequence, Type
from unittest.mock import patch

import pytest

_HERE = os.path.dirname(os.path.abspath(__file__))
_BACKEND = os.path.abspath(os.path.join(_HERE, ".."))
if _BACKEND not in sys.path:
    sys.path.insert(0, _BACKEND)


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


def _reception(**kwargs: Any) -> _BranchRow:
    defaults = dict(
        id=12,
        branch_id=1,
        display_name="استقبال",
        role="reception",
        phone_e164="966500000099",
        whatsapp_e164="",
        sort_order=0,
        is_active=True,
        is_default_reception=True,
    )
    defaults.update(kwargs)
    return _BranchRow(**defaults)


@pytest.fixture(autouse=True)
def _enable_structured_flag(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("USE_STRUCTURED_BRANCH_CONTACTS", "1")


def test_location_only_does_not_deliver_reception() -> None:
    from modules.ai.brain.commerce.branch_trigger_router import evaluate_branch_trigger_routing

    db = _StructuredDB(
        branches=[_branch(location_response_mode="location_only")],
        contacts=[_reception()],
    )
    decision = evaluate_branch_trigger_routing(
        db, tenant_id=10, message="وين موقعكم؟",
    )
    assert decision is not None
    assert decision.trigger_type == "location_request"
    assert decision.maps_url
    assert decision.deliver_reception_after_maps is False
    assert decision.deliver_contact is False


def test_location_plus_reception_delivers_reception() -> None:
    from modules.ai.brain.commerce.branch_trigger_router import evaluate_branch_trigger_routing

    db = _StructuredDB(
        branches=[_branch(location_response_mode="location_plus_reception")],
        contacts=[_reception()],
    )
    decision = evaluate_branch_trigger_routing(
        db, tenant_id=10, message="وين موقعكم؟",
    )
    assert decision is not None
    assert decision.deliver_reception_after_maps is True
    assert decision.reception_call_target is not None


def test_arrival_soft_does_not_escalate_or_vcard() -> None:
    from modules.ai.brain.commerce.branch_trigger_router import evaluate_branch_trigger_routing

    db = _StructuredDB(
        branches=[_branch()],
        contacts=[_reception()],
        steps=[
            _BranchRow(
                id=101,
                branch_id=1,
                escalation_level=1,
                display_name="بائع",
                role="showroom",
                phone_e164="966511111111",
                sort_order=0,
                is_active=True,
            ),
        ],
    )
    decision = evaluate_branch_trigger_routing(
        db, tenant_id=10, message="أنا في الطريق",
    )
    assert decision is not None
    assert decision.trigger_type == "arrival_soft"
    assert decision.deliver_contact is False
    assert decision.persist_contact is False


def test_arrival_confirmed_sends_reception() -> None:
    from modules.ai.brain.commerce.branch_trigger_router import evaluate_branch_trigger_routing

    db = _StructuredDB(
        branches=[_branch()],
        contacts=[_reception()],
    )
    decision = evaluate_branch_trigger_routing(
        db, tenant_id=10, message="وصلت",
    )
    assert decision is not None
    assert decision.trigger_type == "arrival_confirmed"
    assert decision.deliver_contact is True
    assert decision.call_target is not None


def test_no_response_advances_escalation() -> None:
    from modules.ai.brain.commerce.branch_trigger_router import evaluate_branch_trigger_routing

    db = _StructuredDB(
        branches=[_branch(maps_url="")],
        contacts=[_reception()],
        steps=[
            _BranchRow(
                id=101,
                branch_id=1,
                escalation_level=1,
                display_name="بائع",
                role="showroom",
                phone_e164="966511111111",
                sort_order=0,
                is_active=True,
            ),
            _BranchRow(
                id=102,
                branch_id=1,
                escalation_level=2,
                display_name="خدمة العملاء",
                role="customer_service",
                phone_e164="966522222222",
                sort_order=0,
                is_active=True,
            ),
        ],
    )
    with patch(
        "modules.ai.brain.commerce.branch_trigger_router._load_contacts_sent",
        return_value=[{"name": "بائع", "phone": "966511111111", "turn": 1}],
    ):
        decision = evaluate_branch_trigger_routing(
            db,
            tenant_id=10,
            message="ما يرد",
            customer_phone="966500000001",
        )
    assert decision is not None
    assert decision.trigger_type == "no_response"
    assert decision.deliver_contact is True
    assert decision.reason == "no_response_escalation_advance"


def test_custom_keyword_alhosh_works() -> None:
    from modules.ai.brain.commerce.branch_trigger_router import evaluate_branch_trigger_routing

    db = _StructuredDB(
        branches=[_branch()],
        contacts=[_reception()],
        keywords=[
            _BranchRow(
                id=1,
                branch_id=1,
                phrase="الحوش",
                trigger_type="arrival_confirmed",
                sort_order=0,
                is_active=True,
            ),
        ],
    )
    decision = evaluate_branch_trigger_routing(
        db, tenant_id=10, message="أنا في الحوش",
    )
    assert decision is not None
    assert decision.trigger_type == "arrival_confirmed"
    assert decision.matched_phrase == "الحوش"


def test_flag_off_allows_legacy_keyword_router_skip(monkeypatch: pytest.MonkeyPatch) -> None:
    from modules.ai.brain.commerce.branch_trigger_router import evaluate_branch_trigger_routing

    monkeypatch.setenv("USE_STRUCTURED_BRANCH_CONTACTS", "0")
    db = _StructuredDB(branches=[_branch()], contacts=[_reception()])
    decision = evaluate_branch_trigger_routing(
        db, tenant_id=10, message="وين موقعكم؟",
    )
    assert decision is None


def test_structured_mode_blocks_kb_arrival_compile() -> None:
    from modules.ai.brain.commerce.arrival_contact_delivery_policy import (
        resolve_arrival_contact_evidence,
    )

    db = _StructuredDB(
        branches=[_branch(maps_url="https://maps.example/x")],
        contacts=[],
    )
    with patch(
        "modules.ai.brain.commerce.arrival_contact_policy.resolve_arrival_contact_policy",
    ) as mock_policy:
        evidence = resolve_arrival_contact_evidence(db, 10, message="وصلت")
        assert evidence is None
        mock_policy.assert_not_called()

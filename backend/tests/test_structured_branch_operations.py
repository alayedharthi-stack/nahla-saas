"""Operations Center PR-A — structured branches, contacts, escalation."""
from __future__ import annotations

import os
import sys
from typing import Any, List, Optional, Sequence, Type

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

    def all(self) -> List[Any]:
        return list(self._rows)


class _StructuredDB:
    def __init__(
        self,
        *,
        branches: Optional[List[Any]] = None,
        contacts: Optional[List[Any]] = None,
        steps: Optional[List[Any]] = None,
    ) -> None:
        self.branches = branches or []
        self.contacts = contacts or []
        self.steps = steps or []

    def query(self, model: Type[Any]) -> _StructuredQuery:
        name = getattr(model, "__name__", str(model))
        if name == "MerchantBranch":
            return _StructuredQuery(self.branches)
        if name == "BranchContact":
            return _StructuredQuery(self.contacts)
        if name == "BranchEscalationStep":
            return _StructuredQuery(self.steps)
        return _StructuredQuery([])


@pytest.fixture(autouse=True)
def _enable_structured_flag(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("USE_STRUCTURED_BRANCH_CONTACTS", "1")


def test_lookup_structured_maps_url_returns_branch_maps() -> None:
    from modules.operations.branch_contact_evidence import lookup_structured_maps_url

    db = _StructuredDB(
        branches=[
            _BranchRow(
                id=1,
                tenant_id=10,
                name="المعرض",
                city="الرياض",
                district="",
                address="",
                maps_url="https://maps.google.com/?q=branch1",
                sort_order=0,
                is_active=True,
            ),
        ],
    )
    url, source, branch_id = lookup_structured_maps_url(db, 10)
    assert url == "https://maps.google.com/?q=branch1"
    assert source == "structured_branch"
    assert branch_id == 1


def test_resolve_reception_contact_prefers_showroom_role() -> None:
    from modules.operations.branch_contact_evidence import resolve_reception_contact

    db = _StructuredDB(
        branches=[
            _BranchRow(
                id=1,
                tenant_id=10,
                name="فرع 1",
                city="",
                district="",
                address="",
                maps_url="",
                sort_order=0,
                is_active=True,
            ),
        ],
        contacts=[
            _BranchRow(
                id=11,
                branch_id=1,
                display_name="أمين",
                role="admin",
                phone_e164="+966500000001",
                whatsapp_e164="",
                sort_order=1,
                is_active=True,
            ),
            _BranchRow(
                id=12,
                branch_id=1,
                display_name="بائع المعرض",
                role="showroom",
                phone_e164="+966500000002",
                whatsapp_e164="",
                sort_order=0,
                is_active=True,
            ),
        ],
    )
    contact = resolve_reception_contact(db, 10)
    assert contact is not None
    assert contact.display_name == "بائع المعرض"
    assert contact.phone_e164 == "+966500000002"


def test_arrival_delivery_uses_structured_reception() -> None:
    from modules.ai.brain.commerce import arrival_contact_delivery_policy as mod

    db = _StructuredDB(
        branches=[
            _BranchRow(
                id=1,
                tenant_id=10,
                name="المعرض",
                city="",
                district="",
                address="",
                maps_url="",
                sort_order=0,
                is_active=True,
            ),
        ],
        contacts=[
            _BranchRow(
                id=12,
                branch_id=1,
                display_name="استقبال",
                role="reception",
                phone_e164="966500000099",
                whatsapp_e164="",
                sort_order=0,
                is_active=True,
            ),
        ],
    )

    decision = mod.evaluate_arrival_contact_delivery(
        db,
        tenant_id=10,
        message="أنا جاي",
    )
    assert decision is not None
    assert decision.deliver_contact is True
    assert decision.contact_phone == "966500000099"
    assert decision.reason == "structured_branch_reception"


def test_structured_escalation_advances_linearly() -> None:
    from modules.ai.brain.commerce.staff_contact_fallback_v0 import (
        resolve_staff_contact_fallback_v0,
    )
    from modules.operations.branch_escalation_evidence import (
        load_structured_escalation_chain,
        resolve_next_structured_escalation,
    )

    db = _StructuredDB(
        branches=[
            _BranchRow(
                id=1,
                tenant_id=10,
                name="فرع",
                city="",
                district="",
                address="",
                maps_url="",
                sort_order=0,
                is_active=True,
            ),
        ],
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
            _BranchRow(
                id=103,
                branch_id=1,
                escalation_level=3,
                display_name="الإدارة",
                role="admin",
                phone_e164="966533333333",
                sort_order=0,
                is_active=True,
            ),
        ],
    )

    chain = load_structured_escalation_chain(db, 10)
    assert len(chain) == 3

    sent_l1 = [{"name": "بائع", "phone": "966511111111", "turn": 1}]
    nxt = resolve_next_structured_escalation(chain, sent_l1)
    assert nxt is not None
    assert nxt.phone == "966522222222"

    verdict = resolve_staff_contact_fallback_v0(
        [],
        contacts_sent=sent_l1,
        customer_msg="ما يرد",
        trigger="employee_not_responding",
        tenant_id=10,
        db=db,
    )
    assert verdict.enabled is True
    assert verdict.next_phone == "966522222222"


def test_maps_resolver_prefers_structured_when_flag_on() -> None:
    from modules.ai.postprocess import safety_nets as sn

    db = _StructuredDB(
        branches=[
            _BranchRow(
                id=1,
                tenant_id=33,
                name="فرع",
                city="",
                district="",
                address="",
                maps_url="https://maps.google.com/?q=structured",
                sort_order=0,
                is_active=True,
            ),
        ],
    )

    url, source = sn._lookup_tenant_maps_url(db, 33)
    assert url == "https://maps.google.com/?q=structured"
    assert source == "structured_branch"


def test_structured_registry_named_lookup() -> None:
    from modules.ai.brain.commerce.staff_contact_evidence import (
        classify_staff_contact_request,
        load_staff_contact_registry,
        resolve_staff_contact,
    )

    db = _StructuredDB(
        branches=[
            _BranchRow(
                id=1,
                tenant_id=10,
                name="المعرض",
                city="",
                district="",
                address="",
                maps_url="",
                sort_order=0,
                is_active=True,
            ),
        ],
        contacts=[
            _BranchRow(
                id=12,
                branch_id=1,
                display_name="هشام",
                role="showroom",
                phone_e164="966544444444",
                whatsapp_e164="",
                sort_order=0,
                is_active=True,
            ),
        ],
    )

    registry = load_staff_contact_registry(db, 10)
    request = classify_staff_contact_request("رقم هشام")
    resolution = resolve_staff_contact(registry, request, message="رقم هشام")
    assert resolution.found is True
    assert resolution.record is not None
    assert resolution.record.phone == "966544444444"


def test_flag_off_falls_back_to_kb_registry(monkeypatch: pytest.MonkeyPatch) -> None:
    from modules.ai.brain.commerce.staff_contact_evidence import load_staff_contact_registry

    monkeypatch.setenv("USE_STRUCTURED_BRANCH_CONTACTS", "0")

    class _Section:
        id = 1
        kind = "branches"
        body = "بائع المعرض: 966555555555"
        title = ""
        metadata = {}
        metadata_json = {}

    class _Q:
        def filter(self, *a: Any, **k: Any) -> "_Q":
            return self

        def order_by(self, *a: Any, **k: Any) -> "_Q":
            return self

        def limit(self, _n: int) -> "_Q":
            return self

        def all(self) -> List[_Section]:
            return [_Section()]

    class _DB:
        def query(self, _m: Any) -> _Q:
            return _Q()

    registry = load_staff_contact_registry(_DB(), 99)
    assert registry.records
    assert registry.records[0].phone

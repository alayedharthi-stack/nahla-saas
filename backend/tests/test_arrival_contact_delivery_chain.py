"""Arrival contact delivery + tiered escalation chain tests."""
from __future__ import annotations

import os
import sys
import types as _types
from typing import Any, List

import pytest

_HERE = os.path.dirname(os.path.abspath(__file__))
_BACKEND = os.path.abspath(os.path.join(_HERE, ".."))
if _BACKEND not in sys.path:
    sys.path.insert(0, _BACKEND)

from modules.ai.brain.commerce.arrival_contact_delivery_policy import (
    MSG_ARRIVAL_CONTACT_NOT_CONFIGURED,
    evaluate_arrival_contact_delivery,
)
from modules.ai.brain.commerce.contact_route_policy import is_arrival_or_visit_signal
from modules.ai.brain.commerce.location_link_policy import (
    LocationLinkPolicyDecision,
    evaluate_location_link_policy,
)
from modules.ai.brain.commerce.staff_contact_escalation_chain import (
    classify_contact_tier,
    resolve_next_tiered_contact,
)
from modules.ai.brain.commerce.staff_contact_fallback_v0 import (
    StaffChainEntry,
    extract_staff_chain_from_sections,
    resolve_staff_contact_fallback_v0,
)
from modules.ai.brain.commerce.staff_contact_recovery import (
    evaluate_staff_contact_recovery,
)


class _Section:
    def __init__(
        self,
        *,
        id: int,
        kind: str,
        body: str,
        title: str = "",
        metadata: dict | None = None,
    ) -> None:
        self.id = id
        self.kind = kind
        self.body = body
        self.title = title
        self.metadata = metadata or {}
        self.metadata_json = metadata or {}
        self.updated_at = id


class _StubDB:
    def __init__(self, sections: List[_Section]) -> None:
        self._sections = sections

    def query(self, _model: Any) -> "_Query":
        return _Query(self._sections)


class _Query:
    def __init__(self, sections: List[_Section]) -> None:
        self._sections = list(sections)

    def filter(self, *args: Any, **kwargs: Any) -> "_Query":
        return self

    def order_by(self, *args: Any, **kwargs: Any) -> "_Query":
        return self

    def limit(self, _n: int) -> "_Query":
        return self

    def all(self) -> List[_Section]:
        return self._sections

    def first(self) -> None:
        return None


def _install_stubs(monkeypatch: pytest.MonkeyPatch) -> None:
    call_stub = _types.ModuleType("services.call_resolver")

    class _CallTarget:
        def __init__(self, name: str, wa_id: str, phone_display: str, raw_phone: str) -> None:
            self.name = name
            self.wa_id = wa_id
            self.phone_display = phone_display
            self.raw_phone = raw_phone

    def _fake_normalize(phone: str) -> str:
        digits = "".join(c for c in phone if c.isdigit())
        if digits.startswith("966"):
            return digits
        if digits.startswith("0") and len(digits) >= 10:
            return "966" + digits[1:]
        if len(digits) == 9 and digits.startswith("5"):
            return "966" + digits
        return digits

    call_stub.CallTarget = _CallTarget  # type: ignore[attr-defined]
    call_stub._normalize_saudi_phone = _fake_normalize  # type: ignore[attr-defined]
    call_stub._pretty_phone = lambda w: w  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "services.call_resolver", call_stub)
    monkeypatch.setenv("ARRIVAL_CONTACT_DELIVERY_ENABLED", "1")
    monkeypatch.setenv("STAFF_CONTACT_RECOVERY_ENABLED", "1")
    monkeypatch.setenv("LOCATION_LINK_POLICY_ENABLED", "1")


def _merchant_sections() -> List[_Section]:
    return [
        _Section(
            id=10,
            kind="escalation_rules",
            body="عند الوصول للمعرض يُنسّق مع البائع.",
        ),
        _Section(
            id=20,
            kind="custom",
            body="بائع المعرض — 0541690226",
        ),
        _Section(
            id=30,
            kind="escalation_rules",
            body="هشام — خدمة العملاء — 0549815590",
        ),
        _Section(
            id=31,
            kind="escalation_rules",
            body="هيثم — خدمة العملاء — 0542980511",
        ),
        _Section(
            id=40,
            kind="owner_identity",
            body="أبو هشام — الإدارة — 0555906901",
        ),
    ]


def test_arrival_signal_triggers_delivery_policy(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _install_stubs(monkeypatch)
    assert is_arrival_or_visit_signal("انا جاي")
    db = _StubDB(_merchant_sections())
    decision = evaluate_arrival_contact_delivery(db, tenant_id=33, message="انا جاي")
    assert decision is not None
    assert decision.deliver_contact is True
    assert decision.call_target is not None
    assert "بائع" in (decision.call_target.name or "").lower() or decision.contact_lookup_name


def test_arrival_without_evidence_no_name(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _install_stubs(monkeypatch)
    db = _StubDB([])
    decision = evaluate_arrival_contact_delivery(db, tenant_id=33, message="وصلت")
    assert decision is not None
    assert decision.deliver_contact is False
    assert decision.reply_text == MSG_ARRIVAL_CONTACT_NOT_CONFIGURED
    assert "هشام" not in decision.reply_text
    assert "هيثم" not in decision.reply_text


def test_tier_classify_showroom_cs_admin() -> None:
    showroom = StaffChainEntry(
        lookup_name="بائع المعرض",
        phone="0541690226",
        section_id=1,
        kind="custom",
        is_owner=False,
        chain_index=0,
    )
    cs = StaffChainEntry(
        lookup_name="موظف أ",
        phone="0549815590",
        section_id=2,
        kind="escalation_rules",
        is_owner=False,
        chain_index=1,
    )
    admin = StaffChainEntry(
        lookup_name="الإدارة",
        phone="0555906901",
        section_id=3,
        kind="owner_identity",
        is_owner=True,
        chain_index=2,
        role="owner",
    )
    assert classify_contact_tier(showroom) == "showroom"
    assert classify_contact_tier(cs) == "customer_service"
    assert classify_contact_tier(admin) == "admin"


def test_tiered_escalation_showroom_to_cs_to_admin() -> None:
    sections = _merchant_sections()
    chain = extract_staff_chain_from_sections(sections)
    assert len(chain) >= 3

    showroom_phone = next(
        e.phone for e in chain if classify_contact_tier(e) == "showroom"
    )
    sent_showroom = [{"name": "بائع المعرض", "phone": showroom_phone, "turn": 1}]
    nxt = resolve_next_tiered_contact(chain, sent_showroom)
    assert nxt is not None
    assert classify_contact_tier(nxt) == "customer_service"

    cs_entries = [e for e in chain if classify_contact_tier(e) == "customer_service"]
    sent_cs1 = [
        *sent_showroom,
        {"name": cs_entries[0].lookup_name, "phone": cs_entries[0].phone, "turn": 2},
    ]
    nxt2 = resolve_next_tiered_contact(chain, sent_cs1)
    assert nxt2 is not None
    assert nxt2.phone == cs_entries[1].phone

    sent_cs2 = [
        *sent_cs1,
        {"name": cs_entries[1].lookup_name, "phone": cs_entries[1].phone, "turn": 3},
    ]
    nxt3 = resolve_next_tiered_contact(chain, sent_cs2)
    assert nxt3 is not None
    assert classify_contact_tier(nxt3) == "admin"


def test_ma_yerd_recovery_advances_with_call_target(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _install_stubs(monkeypatch)
    db = _StubDB(_merchant_sections())
    sections = _merchant_sections()
    chain = extract_staff_chain_from_sections(sections)
    showroom = next(e for e in chain if classify_contact_tier(e) == "showroom")

    decision = evaluate_staff_contact_recovery(
        db,
        tenant_id=33,
        phone="966500000000",
        message="مايرد",
        contacts_sent_raw=[{
            "name": showroom.lookup_name,
            "phone": showroom.phone,
            "turn": 1,
        }],
    )
    assert decision is not None
    assert decision.deliver_contact is True
    assert decision.call_target is not None
    assert decision.next_contact_phone != showroom.phone


def test_fallback_v0_uses_tier_chain() -> None:
    sections = _merchant_sections()
    chain = extract_staff_chain_from_sections(sections)
    showroom = next(e for e in chain if classify_contact_tier(e) == "showroom")
    verdict = resolve_staff_contact_fallback_v0(
        sections,
        contacts_sent=[{"name": showroom.lookup_name, "phone": showroom.phone, "turn": 1}],
        customer_msg="مايرد",
        trigger="employee_not_responding",
        tenant_id=33,
    )
    assert verdict.enabled is True
    assert verdict.reason == "next_in_tier_chain"
    assert verdict.next_phone != showroom.phone


def test_location_policy_sets_cta_flag(monkeypatch: pytest.MonkeyPatch) -> None:
    _install_stubs(monkeypatch)

    def _fake_maps(_db: Any, _tid: int) -> tuple[str, str]:
        return "https://maps.google.com/?q=shop", "kb:branches"

    monkeypatch.setattr(
        "modules.ai.postprocess.safety_nets._lookup_tenant_maps_url",
        _fake_maps,
    )
    db = _StubDB([])
    decision = evaluate_location_link_policy(db, tenant_id=33, message="وين موقعكم")
    assert decision is not None
    assert decision.maps_url
    assert getattr(decision, "use_cta", False) is True
    assert decision.reply_text == "موقعنا 📍"

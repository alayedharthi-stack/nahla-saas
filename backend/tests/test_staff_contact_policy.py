"""Phase A — staff contact policy + evidence guard tests."""
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

from modules.ai.brain.commerce.staff_contact_evidence import (
    MSG_ESCALATION_NOT_CONFIGURED,
    StaffContactRequest,
    classify_staff_contact_request,
    compile_staff_contact_registry,
    resolve_staff_contact,
)
from modules.ai.brain.commerce.staff_contact_policy import (
    evaluate_generic_handoff_contact_policy,
    evaluate_staff_contact_policy,
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


def _install_call_resolver(monkeypatch: pytest.MonkeyPatch) -> None:
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
    monkeypatch.setenv("STAFF_CONTACT_POLICY_ENABLED", "1")
    monkeypatch.setenv("STAFF_CONTACT_RECOVERY_ENABLED", "1")


def _cs_sections() -> List[_Section]:
    return [
        _Section(
            id=1,
            kind="escalation_rules",
            body="خدمة العملاء: 0501111111",
            metadata={"role": "customer_service"},
        ),
    ]


def _named_sections(staff_name: str = "موظف أ", phone: str = "0502222222") -> List[_Section]:
    return [
        _Section(
            id=2,
            kind="custom",
            body=f"{staff_name}: {phone}",
        ),
    ]


def test_customer_service_with_configured_number(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _install_call_resolver(monkeypatch)
    db = _StubDB(_cs_sections())
    decision = evaluate_staff_contact_policy(
        db, tenant_id=10, message="ابي رقم خدمة العملاء",
    )
    assert decision is None


def test_customer_service_without_configured_number(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _install_call_resolver(monkeypatch)
    db = _StubDB([])
    decision = evaluate_staff_contact_policy(
        db, tenant_id=10, message="ابي رقم خدمة العملاء",
    )
    assert decision is None


def test_named_staff_configured_in_kb(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _install_call_resolver(monkeypatch)
    db = _StubDB(_named_sections("هيثم", "0503333333"))
    decision = evaluate_staff_contact_policy(
        db, tenant_id=10, message="ارسل رقم هيثم",
    )
    assert decision is None


def test_named_staff_not_configured(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _install_call_resolver(monkeypatch)
    db = _StubDB([])
    decision = evaluate_staff_contact_policy(
        db, tenant_id=10, message="ارسل رقم هيثم",
    )
    assert decision is None


def test_generic_talk_to_staff_with_contact(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _install_call_resolver(monkeypatch)
    db = _StubDB(_named_sections("بائع المعرض", "0504444444"))
    decision = evaluate_generic_handoff_contact_policy(
        db, tenant_id=10, message="ابي اكلم موظف",
    )
    assert decision is not None
    assert decision.deliver_contact is True
    assert decision.call_target is not None
    assert "أقدر أوصلك" not in decision.reply_text


def test_generic_talk_to_staff_without_contact(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _install_call_resolver(monkeypatch)
    db = _StubDB([])
    decision = evaluate_generic_handoff_contact_policy(
        db, tenant_id=10, message="ابي اكلم موظف",
    )
    assert decision is not None
    assert decision.deliver_contact is False
    assert decision.reply_text == MSG_ESCALATION_NOT_CONFIGURED
    assert "أقدر أوصلك" not in decision.reply_text


def test_not_responding_requires_prior_contacts(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _install_call_resolver(monkeypatch)
    db = _StubDB(_named_sections("البائع", "0505555555"))
    assert classify_staff_contact_request("البائع ما يرد").kind == "not_responding"
    policy = evaluate_staff_contact_policy(
        db, tenant_id=10, message="البائع ما يرد",
    )
    assert policy is None
    recovery = evaluate_staff_contact_recovery(
        db,
        tenant_id=10,
        phone="966500000001",
        message="البائع ما يرد",
        contacts_sent_raw=[],
    )
    assert recovery is None


def test_not_responding_advances_only_with_prior_sent(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _install_call_resolver(monkeypatch)
    sections = [
        _Section(id=1, kind="custom", body="بائع المعرض: 0506666666"),
        _Section(id=2, kind="escalation_rules", body="خدمة العملاء: 0507777777"),
    ]
    db = _StubDB(sections)
    recovery = evaluate_staff_contact_recovery(
        db,
        tenant_id=10,
        phone="966500000001",
        message="ما يرد",
        contacts_sent_raw=[{
            "name": "بائع المعرض",
            "phone": "966506666666",
            "turn": 1,
        }],
    )
    assert recovery is None


def test_arrival_deferred_to_existing_policy() -> None:
    req = classify_staff_contact_request("انا جاي")
    assert req.kind == "none"


def test_payment_request_not_staff_policy() -> None:
    req = classify_staff_contact_request("ارسل حساب الراجحي")
    assert req.kind == "none"


def test_registry_excludes_owner_from_general_contact() -> None:
    sections = [
        _Section(
            id=1,
            kind="owner_identity",
            body="المالك: 0509999999",
        ),
        _Section(
            id=2,
            kind="custom",
            body="بائع المعرض: 0508888888",
        ),
    ]
    registry = compile_staff_contact_registry(sections)
    general = registry.first_general_contact()
    assert general is not None
    assert general.phone == "0508888888"
    assert "0509999999" not in (general.phone or "")


def test_resolve_named_unknown_without_hardcode() -> None:
    registry = compile_staff_contact_registry([])
    resolution = resolve_staff_contact(
        registry,
        StaffContactRequest(kind="named", target_tier="named_person"),
        message="ارسل رقم شخص_غير_موجود",
    )
    assert resolution.found is False
    assert resolution.unknown_name is True

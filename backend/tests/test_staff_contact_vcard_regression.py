"""Regression tests — vCard delivery, display_name, escalation chain."""
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
    MSG_NO_NEXT_ESCALATION,
    build_staff_call_target,
    is_usable_display_name,
    resolve_contact_display_name,
)
from modules.ai.brain.commerce.staff_contact_policy import (
    evaluate_generic_handoff_contact_policy,
    evaluate_staff_contact_policy,
)
from modules.ai.brain.commerce.staff_contact_recovery import (
    evaluate_staff_contact_recovery,
)
from modules.ai.postprocess.safety_nets import (
    _candidate_token_present,
    strip_embedded_phones_from_reply,
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


def _chain_sections() -> List[_Section]:
    return [
        _Section(id=1, kind="custom", body="موظف أ: 0501111111"),
        _Section(id=2, kind="custom", body="موظف ب: 0502222222"),
        _Section(id=3, kind="custom", body="موظف ج: 0503333333"),
    ]


def test_contact_evidence_sends_vcard_not_plain_text_only(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _install_call_resolver(monkeypatch)
    db = _StubDB([
        _Section(
            id=1,
            kind="escalation_rules",
            body="خدمة العملاء: 0504444444",
            metadata={"role": "customer_service"},
        ),
    ])
    decision = evaluate_staff_contact_policy(
        db, tenant_id=10, message="ابي رقم خدمة العملاء",
    )
    assert decision is not None
    assert decision.deliver_contact is True
    assert decision.call_target is not None
    assert decision.call_target.wa_id == "966504444444"
    assert decision.call_target.name == "خدمة العملاء"


def test_named_staff_vcard_has_display_name(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _install_call_resolver(monkeypatch)
    staff = "هيثم"
    db = _StubDB([_Section(id=2, kind="custom", body=f"{staff}: 0503333333")])
    decision = evaluate_staff_contact_policy(
        db, tenant_id=10, message="ارسل رقم هيثم",
    )
    assert decision is not None
    assert decision.call_target is not None
    assert decision.call_target.name == staff
    assert decision.call_target.wa_id == "966503333333"


def test_seller_not_responding_advances_chain(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _install_call_resolver(monkeypatch)
    db = _StubDB(_chain_sections())
    recovery = evaluate_staff_contact_recovery(
        db,
        tenant_id=10,
        phone="966500000001",
        message="مايرد",
        contacts_sent_raw=[{
            "name": "موظف أ",
            "phone": "966501111111",
            "turn": 1,
        }],
    )
    assert recovery is not None
    assert recovery.deliver_contact is True
    assert recovery.call_target is not None
    assert recovery.call_target.wa_id == "966502222222"
    assert "موظف ب" in recovery.call_target.name or recovery.call_target.wa_id == "966502222222"


def test_no_next_contact_returns_explicit_message(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _install_call_resolver(monkeypatch)
    db = _StubDB(_chain_sections())
    recovery = evaluate_staff_contact_recovery(
        db,
        tenant_id=10,
        phone="966500000001",
        message="مايرد",
        contacts_sent_raw=[
            {"name": "موظف أ", "phone": "966501111111", "turn": 1},
            {"name": "موظف ب", "phone": "966502222222", "turn": 2},
            {"name": "موظف ج", "phone": "966503333333", "turn": 3},
        ],
    )
    assert recovery is not None
    assert recovery.deliver_contact is False
    assert recovery.call_target is None
    assert recovery.reply_text == MSG_NO_NEXT_ESCALATION
    assert "المالك" not in recovery.reply_text


def test_invalid_display_name_uses_role_fallback() -> None:
    assert is_usable_display_name("لم") is False
    assert resolve_contact_display_name("لم", role="customer_service") == "خدمة العملاء"
    target = build_staff_call_target(
        lookup_name="لم",
        phone="0505555555",
        role="customer_service",
    )
    assert target is not None
    assert target.name == "خدمة العملاء"


def test_arrival_fragment_not_matched_inside_word() -> None:
    haystack = "امين في المعرض يستقبلك"
    assert _candidate_token_present(haystack, "لم") is False
    assert _candidate_token_present(haystack, "امين") is True


def test_strip_phones_from_reply_when_vcard_will_send() -> None:
    reply = "حاضر جرب موظف ب على 0502222222"
    cleaned = strip_embedded_phones_from_reply(reply)
    assert "0502222222" not in cleaned
    assert "حاضر" in cleaned


def test_generic_staff_without_contact_still_honest(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _install_call_resolver(monkeypatch)
    db = _StubDB([])
    decision = evaluate_generic_handoff_contact_policy(
        db, tenant_id=10, message="ابي اكلم موظف",
    )
    assert decision is not None
    assert decision.deliver_contact is False
    assert decision.call_target is None


def test_unknown_name_still_not_configured(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _install_call_resolver(monkeypatch)
    db = _StubDB([])
    decision = evaluate_staff_contact_policy(
        db, tenant_id=10, message="ارسل رقم اسم غير موجود",
    )
    assert decision is not None
    assert decision.deliver_contact is False


def test_bare_named_contact_sends_vcard(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _install_call_resolver(monkeypatch)
    staff = "موظف أ"
    db = _StubDB([_Section(id=2, kind="custom", body=f"{staff}: 0503333333")])
    decision = evaluate_staff_contact_policy(
        db, tenant_id=10, message=staff,
    )
    assert decision is not None
    assert decision.deliver_contact is True
    assert decision.call_target is not None
    assert decision.call_target.wa_id == "966503333333"


def test_chain_after_arrival_role_label(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _install_call_resolver(monkeypatch)
    sections = [
        _Section(id=1, kind="custom", body="موظف أ: 0501111111"),
        _Section(id=2, kind="custom", body="موظف ب: 0502222222"),
    ]
    db = _StubDB(sections)
    recovery = evaluate_staff_contact_recovery(
        db,
        tenant_id=10,
        phone="966500000001",
        message="مايرد",
        contacts_sent_raw=[{
            "name": "بائع المعرض",
            "phone": "966501111111",
            "turn": 1,
        }],
    )
    assert recovery is not None
    assert recovery.deliver_contact is True
    assert recovery.call_target is not None
    assert recovery.call_target.wa_id == "966502222222"


def test_llm_reply_with_phone_converts_to_vcard(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from modules.ai.postprocess.safety_nets import apply_staff_contact_safety_net

    _install_call_resolver(monkeypatch)
    staff = "موظف أ"
    db = _StubDB([_Section(id=1, kind="custom", body=f"{staff}: 0504444444")])
    result = apply_staff_contact_safety_net(
        customer_msg=staff,
        reply_text=f"تفضل رقم {staff} 0504444444",
        existing_call_targets=[],
        detected_call_markers=0,
        db=db,
        tenant_id=10,
    )
    assert result.fired is True
    assert result.extra_call_target is not None
    assert result.strip_phones_from_reply is True
    assert "0504444444" not in (
        __import__(
            "modules.ai.postprocess.safety_nets",
            fromlist=["strip_embedded_phones_from_reply"],
        ).strip_embedded_phones_from_reply(f"تفضل رقم {staff} 0504444444")
    )

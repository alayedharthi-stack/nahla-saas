"""Staff contact fallback v0 — chain resolution and safety-net wiring."""
from __future__ import annotations

import logging
import os
import re
import sys
import types as _types
from typing import Any, List, Optional

import pytest

_HERE = os.path.dirname(os.path.abspath(__file__))
_BACKEND = os.path.abspath(os.path.join(_HERE, ".."))
if _BACKEND not in sys.path:
    sys.path.insert(0, _BACKEND)

from modules.ai.brain.commerce.staff_contact_fallback_v0 import (
    classify_explicit_owner_request,
    extract_staff_chain_from_sections,
    resolve_staff_contact_fallback_v0,
)


class _Section:
    def __init__(
        self,
        *,
        id: int,
        kind: str,
        body: str,
        title: str = "",
    ) -> None:
        self.id = id
        self.kind = kind
        self.body = body
        self.title = title


def _chain_sections() -> List[_Section]:
    return [
        _Section(
            id=10,
            kind="escalation_rules",
            body="عند الوصول للمعرض تواصل مع البائع.",
        ),
        _Section(
            id=20,
            kind="custom",
            body=(
                "أمين بائع المعرض: 0541690226\n"
                "هشام: 0501112233\n"
                "هيثم: 0507654321"
            ),
        ),
    ]


def test_chain_order_from_kb():
    chain = extract_staff_chain_from_sections(_chain_sections())
    assert len(chain) == 3
    assert "أمين" in chain[0].lookup_name or "بائع" in chain[0].lookup_name
    assert chain[1].lookup_name == "هشام"
    assert chain[2].lookup_name == "هيثم"


def test_fallback_advances_after_first_sent():
    sent = [{"name": "بائع المعرض", "phone": "966541690226", "turn": 3}]
    verdict = resolve_staff_contact_fallback_v0(
        _chain_sections(),
        contacts_sent=sent,
        customer_msg="البائع مايرد",
        trigger="employee_not_responding",
        tenant_id=42,
    )
    assert verdict.enabled is True
    assert verdict.next_lookup_name == "هشام"
    assert "0501112233" in verdict.next_phone


def test_repeated_mayrd_advances_again():
    sent = [
        {"name": "أمين", "phone": "966541690226", "turn": 1},
        {"name": "هشام", "phone": "966501112233", "turn": 2},
    ]
    verdict = resolve_staff_contact_fallback_v0(
        _chain_sections(),
        contacts_sent=sent,
        customer_msg="مايرد",
        trigger="employee_not_responding",
        tenant_id=42,
    )
    assert verdict.enabled is True
    assert verdict.next_lookup_name == "هيثم"


def test_owner_not_sent_without_explicit_request():
    sections = [
        _Section(
            id=1,
            kind="owner_identity",
            body="صاحب المتجر: 0555906901",
        ),
        _Section(
            id=2,
            kind="custom",
            body="أمين بائع المعرض: 0541690226\nهشام: 0501112233",
        ),
    ]
    sent = [{"name": "أمين", "phone": "966541690226", "turn": 1}]
    verdict = resolve_staff_contact_fallback_v0(
        sections,
        contacts_sent=sent,
        customer_msg="مايرد",
        trigger="employee_not_responding",
        tenant_id=42,
    )
    assert verdict.enabled is True
    assert verdict.next_lookup_name == "هشام"
    assert "0555906901" not in verdict.next_phone


def test_owner_sent_when_explicitly_requested():
    sections = [
        _Section(
            id=1,
            kind="owner_identity",
            body="صاحب المحل: 0555906901",
        ),
        _Section(
            id=2,
            kind="custom",
            body="أمين بائع المعرض: 0541690226",
        ),
    ]
    assert classify_explicit_owner_request("ابي رقم المالك") is True
    verdict = resolve_staff_contact_fallback_v0(
        sections,
        contacts_sent=[{"name": "أمين", "phone": "966541690226", "turn": 1}],
        customer_msg="ابي رقم المالك",
        trigger="employee_not_responding",
        tenant_id=42,
    )
    assert verdict.enabled is True
    assert "0555906901" in verdict.next_phone


class _StubKBSection:
    def __init__(
        self,
        *,
        section_id: int,
        kind: str,
        body: str,
        title: str = "",
        is_active: bool = True,
        priority: int = 100,
    ) -> None:
        self.id = section_id
        self.kind = kind
        self.body = body
        self.title = title
        self.is_active = is_active
        self.priority = priority
        self.updated_at = section_id


class _KBQuery:
    def __init__(self, sections: List[_StubKBSection]) -> None:
        self._sections = list(sections)

    def filter(self, *args: Any, **kwargs: Any) -> "_KBQuery":
        for expr in args:
            kinds = getattr(expr, "_kinds", None)
            if kinds:
                self._sections = [s for s in self._sections if s.kind in kinds]
        return self

    def order_by(self, *_: Any) -> "_KBQuery":
        self._sections.sort(key=lambda s: (s.priority, -s.updated_at))
        return self

    def limit(self, _n: int) -> "_KBQuery":
        return self

    def all(self) -> List[_StubKBSection]:
        return list(self._sections)

    def first(self) -> None:
        return None


class _StubDB:
    def __init__(self, sections: Optional[List[_StubKBSection]] = None) -> None:
        self._sections = list(sections or [])

    def query(self, _model: Any) -> _KBQuery:
        return _KBQuery(self._sections)


def _install_stubs(monkeypatch: pytest.MonkeyPatch, sections: List[_StubKBSection]) -> _StubDB:
    models_stub = _types.ModuleType("models")

    class _Col:
        def __init__(self, name: str) -> None:
            self.name = name

        def __eq__(self, other: Any) -> _types.SimpleNamespace:
            return _types.SimpleNamespace(col_name=self.name, value=other)

        def is_(self, other: Any) -> _types.SimpleNamespace:
            return _types.SimpleNamespace(col_name=self.name, value=other)

        def in_(self, values: Any) -> _types.SimpleNamespace:
            return _types.SimpleNamespace(col_name=self.name, _kinds=tuple(values))

        def asc(self) -> "_Col":
            return self

        def desc(self) -> "_Col":
            return self

    class _MksStub:
        tenant_id = _Col("tenant_id")
        kind = _Col("kind")
        is_active = _Col("is_active")
        priority = _Col("priority")
        updated_at = _Col("updated_at")

    class _TsStub:
        tenant_id = _Col("tenant_id")

    models_stub.MerchantKnowledgeSection = _MksStub  # type: ignore[attr-defined]
    models_stub.TenantSettings = _TsStub  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "models", models_stub)

    call_stub = _types.ModuleType("services.call_resolver")

    class _CallTarget:
        def __init__(self, **kwargs: Any) -> None:
            self.name = kwargs.get("name", "")
            self.wa_id = kwargs.get("wa_id", "")
            self.phone_display = kwargs.get("phone_display", "")
            self.raw_phone = kwargs.get("raw_phone", "")

    def _fake_normalize(phone: str) -> str:
        digits = re.sub(r"\D+", "", phone or "")
        if digits.startswith("966"):
            return digits
        if digits.startswith("05"):
            return "966" + digits[1:]
        return digits

    call_stub.CallTarget = _CallTarget  # type: ignore[attr-defined]
    call_stub._normalize_saudi_phone = _fake_normalize  # type: ignore[attr-defined]
    call_stub._pretty_phone = lambda w: w  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "services.call_resolver", call_stub)
    monkeypatch.setenv("STAFF_CONTACT_SAFETY_NET_ENABLED", "1")
    return _StubDB(sections)


def _kb_sections() -> List[_StubKBSection]:
    return [
        _StubKBSection(
            section_id=10,
            kind="escalation_rules",
            body="عند الوصول للمعرض يُنسّق مع البائع.",
        ),
        _StubKBSection(
            section_id=20,
            kind="custom",
            body=(
                "تواصل مع بائع المعرض\n"
                "أمين بائع المعرض: 0541690226\n"
                "هشام: 0501112233\n"
                "هيثم: 0507654321"
            ),
        ),
    ]


def test_arrival_then_fallback_sends_next_contact(
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    from modules.ai.postprocess.safety_nets import apply_staff_contact_safety_net

    caplog.set_level(logging.INFO)
    db = _install_stubs(monkeypatch, _kb_sections())
    history = [{"direction": "in", "body": "وين موقعكم؟"}]

    turn1 = apply_staff_contact_safety_net(
        customer_msg="أنا في الطريق",
        reply_text="أهلاً بك 🌷",
        existing_call_targets=[],
        detected_call_markers=0,
        db=db,
        tenant_id=42,
        history=history,
        staff_contacts_sent=[],
    )
    assert turn1.fired is True
    assert turn1.wa_id == "966541690226"

    turn2 = apply_staff_contact_safety_net(
        customer_msg="البائع مايرد",
        reply_text="نعتذر عن التأخير",
        existing_call_targets=[],
        detected_call_markers=0,
        db=db,
        tenant_id=42,
        history=history + [
            {"direction": "in", "body": "أنا في الطريق"},
            {"direction": "out", "body": "أهلاً بك 🌷"},
        ],
        staff_contacts_sent=[
            {"name": "بائع المعرض", "phone": "966541690226", "turn": 2},
        ],
    )
    assert turn2.fired is True
    assert turn2.wa_id == "966501112233"
    assert turn2.skipped_reason != "no_staff_name"

    logs = "\n".join(r.message for r in caplog.records)
    assert "[STAFF_CONTACT_FALLBACK_POLICY]" in logs
    assert "[STAFF_CONTACT_FALLBACK_RESOLVE]" in logs
    assert "staff_contact_fallback_v0" in logs or turn2.source.startswith("kb:")


def test_no_arrival_policy_still_advances_escalation_chain(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Fallback chain is KB-driven — no arrival policy section required."""
    from modules.ai.postprocess.safety_nets import apply_staff_contact_safety_net

    db = _install_stubs(
        monkeypatch,
        [
            _StubKBSection(
                section_id=1,
                kind="custom",
                body="أمين: 0541690226\nهشام: 0501112233",
            ),
        ],
    )
    result = apply_staff_contact_safety_net(
        customer_msg="البائع مايرد",
        reply_text="",
        existing_call_targets=[],
        detected_call_markers=0,
        db=db,
        tenant_id=42,
        staff_contacts_sent=[
            {"name": "أمين", "phone": "966541690226", "turn": 1},
        ],
    )
    assert result.fired is True
    assert result.wa_id == "966501112233"
    assert result.wa_id != "966541690226"


def test_explicit_staff_ask_without_prior_sent_still_works(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from modules.ai.postprocess.safety_nets import apply_staff_contact_safety_net

    db = _install_stubs(
        monkeypatch,
        [
            _StubKBSection(
                section_id=1,
                kind="branches",
                body="أمين بائع المعرض: 0541690226",
            ),
        ],
    )
    result = apply_staff_contact_safety_net(
        customer_msg="ابي رقم أمين",
        reply_text="تفضل",
        existing_call_targets=[],
        detected_call_markers=0,
        db=db,
        tenant_id=42,
        staff_contacts_sent=[],
    )
    assert result.fired is True
    assert result.wa_id == "966541690226"

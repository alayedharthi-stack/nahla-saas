"""KB-gated arrival staff-contact — safety net regression suite."""
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


class _StubKBSection:
    def __init__(
        self,
        *,
        section_id: int,
        kind: str,
        body: str,
        title: str = "",
        metadata: Optional[dict] = None,
        is_active: bool = True,
        priority: int = 100,
    ) -> None:
        self.id = section_id
        self.kind = kind
        self.body = body
        self.title = title
        self.metadata = metadata or {}
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


def _install_stubs(
    monkeypatch: pytest.MonkeyPatch,
    sections: Optional[List[_StubKBSection]] = None,
) -> _StubDB:
    models_stub = _types.ModuleType("models")

    class _Col:
        def __init__(self, name: str) -> None:
            self.name = name

        def __eq__(self, other: Any) -> _types.SimpleNamespace:
            return _types.SimpleNamespace(col_name=self.name, value=other)

        def is_(self, other: Any) -> _types.SimpleNamespace:
            return _types.SimpleNamespace(col_name=self.name, value=other)

        def in_(self, values: Any) -> _types.SimpleNamespace:
            return _types.SimpleNamespace(
                col_name=self.name, _kinds=tuple(values),
            )

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

    def _fake_pretty(wa_id: str) -> str:
        return wa_id

    call_stub.CallTarget = _CallTarget  # type: ignore[attr-defined]
    call_stub._normalize_saudi_phone = _fake_normalize  # type: ignore[attr-defined]
    call_stub._pretty_phone = _fake_pretty  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "services.call_resolver", call_stub)
    monkeypatch.setenv("STAFF_CONTACT_SAFETY_NET_ENABLED", "1")
    return _StubDB(sections or [])


def _policy_section() -> _StubKBSection:
    return _StubKBSection(
        section_id=149,
        kind="escalation_rules",
        body="عند الوصول للمعرض تواصل مع بائع المعرض على الرقم المسجل.",
    )


def _staff_section() -> _StubKBSection:
    return _StubKBSection(
        section_id=5,
        kind="branches",
        body="أمين بائع المعرض: 0541690226",
    )


def test_kb_allows_jaykom_reply_offer_fires_vcard(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from modules.ai.postprocess.safety_nets import apply_staff_contact_safety_net

    db = _install_stubs(
        monkeypatch,
        sections=[_policy_section(), _staff_section()],
    )
    result = apply_staff_contact_safety_net(
        customer_msg="أنا جايكم",
        reply_text="أبشر 🌷 تواصل مع أمين عند الوصول للمعرض",
        existing_call_targets=[],
        detected_call_markers=0,
        db=db,
        tenant_id=42,
    )
    assert result.fired is True
    assert result.wa_id == "966541690226"


def test_kb_allows_wasilat_after_location_reply_offer_fires(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from modules.ai.postprocess.safety_nets import apply_staff_contact_safety_net

    db = _install_stubs(
        monkeypatch,
        sections=[_policy_section(), _staff_section()],
    )
    history = [{"direction": "in", "body": "وين موقعكم؟"}]
    result = apply_staff_contact_safety_net(
        customer_msg="وصلت",
        reply_text="تواصل مع أمين عند البوابة",
        existing_call_targets=[],
        detected_call_markers=0,
        db=db,
        tenant_id=42,
        history=history,
    )
    assert result.fired is True


def test_kb_allows_maql_after_location_reply_offer_fires(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from modules.ai.postprocess.safety_nets import apply_staff_contact_safety_net

    db = _install_stubs(
        monkeypatch,
        sections=[_policy_section(), _staff_section()],
    )
    history = [{"direction": "in", "body": "أبغى الفروع"}]
    result = apply_staff_contact_safety_net(
        customer_msg="مقفل",
        reply_text="تواصل مع أمين بائع المعرض",
        existing_call_targets=[],
        detected_call_markers=0,
        db=db,
        tenant_id=42,
        history=history,
    )
    assert result.fired is True


def test_no_arrival_policy_same_messages_no_vcard(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from modules.ai.postprocess.safety_nets import apply_staff_contact_safety_net

    db = _install_stubs(
        monkeypatch,
        sections=[_staff_section()],
    )
    result = apply_staff_contact_safety_net(
        customer_msg="أنا جايكم",
        reply_text="تواصل مع أمين عند الوصول للمعرض",
        existing_call_targets=[],
        detected_call_markers=0,
        db=db,
        tenant_id=42,
    )
    assert result.fired is False
    assert result.skipped_reason == "arrival_policy_denied"


def test_shipping_wasilat_no_staff_contact(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from modules.ai.brain.commerce.contact_escalation import classify_store_arrival
    from modules.ai.postprocess.safety_nets import apply_staff_contact_safety_net

    assert classify_store_arrival("وصلت الشحنة") is None

    db = _install_stubs(
        monkeypatch,
        sections=[_policy_section(), _staff_section()],
    )
    result = apply_staff_contact_safety_net(
        customer_msg="وصلت الشحنة",
        reply_text="تواصل مع أمين",
        existing_call_targets=[],
        detected_call_markers=0,
        db=db,
        tenant_id=42,
    )
    assert result.fired is False


def test_staff_phone_only_no_arrival_policy_denied(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from modules.ai.postprocess.safety_nets import apply_staff_contact_safety_net

    db = _install_stubs(
        monkeypatch,
        sections=[
            _StubKBSection(
                section_id=1,
                kind="custom",
                body="بائع المعرض: 0541690226 — للاستفسارات العامة.",
            ),
        ],
    )
    result = apply_staff_contact_safety_net(
        customer_msg="وصلت",
        reply_text="تواصل مع أمين عند الوصول",
        existing_call_targets=[],
        detected_call_markers=0,
        db=db,
        tenant_id=42,
    )
    assert result.fired is False
    assert result.skipped_reason == "arrival_policy_denied"


def test_telemetry_policy_and_escalation(
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    from modules.ai.postprocess.safety_nets import apply_staff_contact_safety_net

    caplog.set_level(logging.INFO)
    db = _install_stubs(
        monkeypatch,
        sections=[_policy_section(), _staff_section()],
    )
    apply_staff_contact_safety_net(
        customer_msg="وصلت",
        reply_text="تواصل مع أمين عند الوصول",
        existing_call_targets=[],
        detected_call_markers=0,
        db=db,
        tenant_id=42,
        conversation_id=9063,
    )
    policy_lines = [
        r.message for r in caplog.records if "[ARRIVAL_CONTACT_POLICY]" in r.message
    ]
    esc_lines = [
        r.message for r in caplog.records if "[CONTACT_ESCALATION]" in r.message
    ]
    assert any("allow=true" in ln for ln in policy_lines)
    assert any(
        "policy_allowed=true" in ln and "selected_contact=" in ln
        for ln in esc_lines
    )

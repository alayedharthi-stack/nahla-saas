"""P0 — staff escalation must be intent-based, not keyword-based."""
from __future__ import annotations

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

from modules.ai.brain.commerce.contact_escalation import classify_store_arrival  # noqa: E402
from modules.ai.brain.commerce.staff_escalation_decision_guard import (  # noqa: E402
    is_delivery_received_phrase,
    validate_staff_contact_action,
)


class _StubKBSection:
    def __init__(
        self,
        *,
        section_id: int,
        kind: str,
        body: str,
        title: str = "",
        metadata: Optional[dict] = None,
    ) -> None:
        self.id = section_id
        self.kind = kind
        self.body = body
        self.title = title
        self.metadata = metadata or {}
        self.is_active = True
        self.priority = 100
        self.updated_at = section_id


class _StubDB:
    def __init__(self, sections: List[_StubKBSection]) -> None:
        self._sections = sections

    def query(self, _model: Any) -> "_Query":
        return _Query(self._sections)


class _Query:
    def __init__(self, sections: List[_StubKBSection]) -> None:
        self._sections = list(sections)

    def filter(self, *args: Any, **kwargs: Any) -> "_Query":
        return self

    def order_by(self, *args: Any, **kwargs: Any) -> "_Query":
        return self

    def limit(self, _n: int) -> "_Query":
        return self

    def all(self) -> List[_StubKBSection]:
        return self._sections

    def first(self) -> None:
        return None


def _install_staff_stubs(
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
        deleted_at = _Col("deleted_at")

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


def _apply_net(
    monkeypatch: pytest.MonkeyPatch,
    *,
    customer_msg: str,
    reply_text: str = "تقدر تتواصل مع أمين بائع المعرض.",
    sections: Optional[List[_StubKBSection]] = None,
    history: Optional[list] = None,
):
    from modules.ai.postprocess.safety_nets import apply_staff_contact_safety_net

    db = _install_staff_stubs(
        monkeypatch,
        sections=sections or [_policy_section(), _staff_section()],
    )
    return apply_staff_contact_safety_net(
        customer_msg=customer_msg,
        reply_text=reply_text,
        existing_call_targets=[],
        detected_call_markers=0,
        db=db,
        tenant_id=42,
        history=history or [],
    )


class TestDeliveryReceivedNotStaffEscalation:
    """Production failure: «وصل والله يبيض وجهك» must not send vCard."""

    def test_delivery_blessing_not_store_arrival(self) -> None:
        msg = "وصل والله يبيض وجهك ويبارك لكم في مالكم وحلالكم يا رب"
        assert is_delivery_received_phrase(msg) is True
        assert classify_store_arrival(msg) is None

    def test_no_vcard_for_delivery_blessing(
        self, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        msg = "وصل والله يبيض وجهك ويبارك لكم في مالكم وحلالكم يا رب"
        result = _apply_net(monkeypatch, customer_msg=msg)
        assert result.fired is False
        assert result.skipped_reason in {
            "blocked:delivery_received_phrase",
            "no_staff_intent",
            "blocked:thanks_or_blessing",
        }


class TestShipmentReceivedNotStaffEscalation:
    def test_honey_delivered_phrase(self) -> None:
        assert is_delivery_received_phrase("العسل وصل") is True
        assert classify_store_arrival("العسل وصل") is None

    def test_no_vcard_for_product_delivered(
        self, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        result = _apply_net(monkeypatch, customer_msg="العسل وصل")
        assert result.fired is False


class TestCityMentionNotStaffEscalation:
    def test_taif_city_not_arrival(self) -> None:
        assert classify_store_arrival("أنا في الطائف") is None

    def test_no_vcard_for_city_mention(
        self, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        result = _apply_net(monkeypatch, customer_msg="أنا في الطائف")
        assert result.fired is False


class TestStoreArrivalWithPolicy:
    def test_door_arrival_detected(self) -> None:
        assert classify_store_arrival("أنا عند باب المعرض") is not None

    def test_vcard_allowed_with_kb_policy(
        self, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        result = _apply_net(
            monkeypatch,
            customer_msg="أنا عند باب المعرض",
            reply_text="تواصل مع أمين عند البوابة",
        )
        assert result.fired is True
        assert result.wa_id == "966541690226"


class TestExplicitStaffRequest:
    def test_explicit_number_request_allowed_by_guard(self) -> None:
        verdict = validate_staff_contact_action(
            customer_msg="أرسل رقم أمين",
        )
        assert verdict.allowed is True
        assert verdict.evidence == "explicit_staff_contact_intent"

    def test_vcard_allowed_for_explicit_staff_request(
        self, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        result = _apply_net(
            monkeypatch,
            customer_msg="أرسل رقم أمين",
            reply_text="تواصل مع أمين بائع المعرض",
        )
        assert result.fired is True
        assert result.wa_id == "966541690226"

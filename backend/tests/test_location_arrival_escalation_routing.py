"""Location / arrival / staff escalation route separation tests."""
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

from modules.ai.brain.commerce.contact_route_policy import (
    is_arrival_or_visit_signal,
    is_location_query,
    should_defer_staff_contact_policy,
)
from modules.ai.brain.commerce.staff_contact_evidence import (
    MSG_ESCALATION_NOT_CONFIGURED,
    classify_staff_contact_request,
)
from modules.ai.brain.commerce.staff_contact_policy import (
    evaluate_staff_contact_policy,
)
from modules.ai.brain.postprocess.availability_guard_policy import (
    inbound_exempt_from_availability_rewrite,
)


class _Section:
    def __init__(self, *, id: int, kind: str, body: str) -> None:
        self.id = id
        self.kind = kind
        self.body = body
        self.title = ""
        self.metadata = {}
        self.metadata_json = {}
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

    call_stub.CallTarget = _CallTarget  # type: ignore[attr-defined]
    call_stub._normalize_saudi_phone = lambda p: "966501111111" if p else ""  # type: ignore[attr-defined]
    call_stub._pretty_phone = lambda w: w  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "services.call_resolver", call_stub)
    monkeypatch.setenv("STAFF_CONTACT_POLICY_ENABLED", "1")


def test_location_query_not_staff_contact_kind() -> None:
    assert is_location_query("موقعكم وين")
    assert classify_staff_contact_request("موقعكم وين").kind == "none"
    assert should_defer_staff_contact_policy("موقعكم وين")


def test_location_query_policy_does_not_short_circuit(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _install_call_resolver(monkeypatch)
    db = _StubDB([])
    assert evaluate_staff_contact_policy(db, tenant_id=10, message="موقعكم وين") is None


def test_arrival_signals_defer_staff_policy() -> None:
    assert is_arrival_or_visit_signal("انا في الطريق")
    assert is_arrival_or_visit_signal("انا جاي")
    assert is_arrival_or_visit_signal("وصلت")
    assert classify_staff_contact_request("انا جاي").kind == "none"


def test_arrival_exempt_from_availability_rewrite() -> None:
    assert inbound_exempt_from_availability_rewrite("انا جاي")
    assert inbound_exempt_from_availability_rewrite("انا في الطريق")
    assert inbound_exempt_from_availability_rewrite("وصلت")


def test_pronoun_followup_defers_staff_policy() -> None:
    assert should_defer_staff_contact_policy("وين رقمه")
    assert evaluate_staff_contact_policy(
        _StubDB([]), tenant_id=10, message="وين رقمه",
    ) is None


def test_bare_staff_name_still_named(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _install_call_resolver(monkeypatch)
    staff = "هيثم"
    db = _StubDB([_Section(id=1, kind="custom", body=f"{staff}: 0501111111")])
    decision = evaluate_staff_contact_policy(db, tenant_id=10, message=staff)
    assert decision is not None
    assert decision.deliver_contact is True


def test_generic_two_word_not_escalation_not_configured(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _install_call_resolver(monkeypatch)
    db = _StubDB([])
    decision = evaluate_staff_contact_policy(db, tenant_id=10, message="موقعكم وين")
    assert decision is None


def test_explicit_staff_ask_still_works(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _install_call_resolver(monkeypatch)
    staff = "موظف أ"
    db = _StubDB([_Section(id=1, kind="custom", body=f"{staff}: 0502222222")])
    decision = evaluate_staff_contact_policy(
        db, tenant_id=10, message=f"ارسل رقم {staff}",
    )
    assert decision is not None
    assert decision.deliver_contact is True


def test_availability_browse_unaffected() -> None:
    assert not inbound_exempt_from_availability_rewrite("هل السمر متوفر؟")


def test_location_not_configured_message_not_escalation(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _install_call_resolver(monkeypatch)
    db = _StubDB([])
    # Policy defers — escalation message must not appear from this path.
    decision = evaluate_staff_contact_policy(db, tenant_id=10, message="وين المعرض")
    assert decision is None
    assert MSG_ESCALATION_NOT_CONFIGURED not in (decision.reply_text if decision else "")

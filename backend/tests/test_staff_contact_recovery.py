"""Pre-LLM staff contact recovery — tenant-agnostic KB chain advance."""
from __future__ import annotations

import logging
import os
import sys
import types as _types
from typing import Any, List, Optional

import pytest

_HERE = os.path.dirname(os.path.abspath(__file__))
_BACKEND = os.path.abspath(os.path.join(_HERE, ".."))
if _BACKEND not in sys.path:
    sys.path.insert(0, _BACKEND)

from modules.ai.brain.commerce.staff_contact_recovery import (
    evaluate_staff_contact_recovery,
    staff_contact_recovery_enabled,
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


class _StubDB:
    def __init__(self, sections: List[_Section]) -> None:
        self._sections = sections

    def query(self, _model: Any) -> "_Query":
        return _Query(self._sections)


class _Query:
    def __init__(self, sections: List[_Section]) -> None:
        self._sections = sections

    def filter(self, *args: Any, **kwargs: Any) -> "_Query":
        return self

    def order_by(self, *args: Any, **kwargs: Any) -> "_Query":
        return self

    def limit(self, _n: int) -> "_Query":
        return self

    def all(self) -> List[_Section]:
        return self._sections


def _chain_sections() -> List[_Section]:
    """Generic three-contact chain — no merchant-specific names in logic."""
    return [
        _Section(
            id=10,
            kind="escalation_rules",
            body="عند الوصول تواصل مع موظف المعرض.",
        ),
        _Section(
            id=20,
            kind="custom",
            body=(
                "موظف المعرض الأول: 0541111111\n"
                "موظف المعرض الثاني: 0542222222\n"
                "موظف المعرض الثالث: 0543333333"
            ),
        ),
    ]


def _install_stubs(monkeypatch: pytest.MonkeyPatch, sections: List[_Section]) -> _StubDB:
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
    monkeypatch.setenv("STAFF_CONTACT_RECOVERY_ENABLED", "1")
    return _StubDB(sections)


def _contact1_sent() -> List[dict]:
    return [{"name": "موظف المعرض الأول", "phone": "966541111111", "turn": 1}]


def _contact1_and_2_sent() -> List[dict]:
    return [
        {"name": "موظف المعرض الأول", "phone": "966541111111", "turn": 1},
        {"name": "موظف المعرض الثاني", "phone": "966542222222", "turn": 2},
    ]


def test_recovery_skips_without_contacts_sent(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    db = _install_stubs(monkeypatch, _chain_sections())
    result = evaluate_staff_contact_recovery(
        db,
        tenant_id=42,
        phone="966500000001",
        message="ما يرد",
        contacts_sent_raw=[],
    )
    assert result is None


def test_recovery_skips_without_employee_not_responding(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    db = _install_stubs(monkeypatch, _chain_sections())
    result = evaluate_staff_contact_recovery(
        db,
        tenant_id=42,
        phone="966500000001",
        message="كيف أقدر أخدمك؟",
        contacts_sent_raw=_contact1_sent(),
    )
    assert result is None


def test_recovery_advances_to_second_contact(
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    db = _install_stubs(monkeypatch, _chain_sections())
    caplog.set_level(logging.INFO)

    result = evaluate_staff_contact_recovery(
        db,
        tenant_id=42,
        phone="966500000001",
        message="ما يرد",
        contacts_sent_raw=_contact1_sent(),
    )

    assert result is not None
    assert result.call_target.wa_id == "966542222222"
    assert "الثاني" in result.next_contact_name or result.call_target.wa_id == "966542222222"
    assert "حاضر" in result.reply_text

    logs = "\n".join(r.message for r in caplog.records)
    assert "[STAFF_CONTACT_RECOVERY]" in logs
    assert "fired=true" in logs
    assert "[STAFF_CONTACT_FALLBACK_POLICY]" in logs


def test_recovery_advances_to_third_contact_on_second_no_response(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    db = _install_stubs(monkeypatch, _chain_sections())

    result = evaluate_staff_contact_recovery(
        db,
        tenant_id=42,
        phone="966500000001",
        message="البائع مايرد",
        contacts_sent_raw=_contact1_and_2_sent(),
    )

    assert result is not None
    assert result.call_target.wa_id == "966543333333"


def test_recovery_respects_kill_switch(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    db = _install_stubs(monkeypatch, _chain_sections())
    monkeypatch.setenv("STAFF_CONTACT_RECOVERY_ENABLED", "0")
    assert staff_contact_recovery_enabled() is False

    result = evaluate_staff_contact_recovery(
        db,
        tenant_id=42,
        phone="966500000001",
        message="ما يرد",
        contacts_sent_raw=_contact1_sent(),
    )
    assert result is None


def test_full_chain_scenario_after_first_vcard(
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Simulates: contact1 sent → «ما يرد» → contact2 → «ما يرد» → contact3."""
    db = _install_stubs(monkeypatch, _chain_sections())
    caplog.set_level(logging.INFO)

    turn2 = evaluate_staff_contact_recovery(
        db,
        tenant_id=99,
        phone="966500000099",
        message="ما يرد",
        contacts_sent_raw=_contact1_sent(),
    )
    assert turn2 is not None
    assert turn2.call_target.wa_id == "966542222222"

    turn3 = evaluate_staff_contact_recovery(
        db,
        tenant_id=99,
        phone="966500000099",
        message="ما يرد",
        contacts_sent_raw=_contact1_and_2_sent()
        + [{"name": turn2.next_contact_name, "phone": turn2.call_target.wa_id, "turn": 3}],
    )
    assert turn3 is not None
    assert turn3.call_target.wa_id == "966543333333"

    logs = "\n".join(r.message for r in caplog.records)
    assert "[STAFF_CONTACT_FALLBACK_POLICY]" in logs

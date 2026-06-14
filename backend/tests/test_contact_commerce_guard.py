"""Commerce guard — contact policies must not intercept product/cart flow."""
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
    evaluate_arrival_contact_delivery,
)
from modules.ai.brain.commerce.contact_route_policy import (
    is_commerce_or_product_flow_message,
    is_explicit_arrival_intent,
    is_short_commerce_affirmative,
    should_defer_contact_policies_for_commerce,
)
from modules.ai.brain.commerce.staff_contact_evidence import (
    MSG_NAME_NOT_CONFIGURED,
    classify_staff_contact_request,
)
from modules.ai.brain.commerce.staff_contact_policy import (
    evaluate_staff_contact_policy,
)
from modules.ai.brain.commerce.staff_contact_recovery import (
    evaluate_staff_contact_recovery,
)
from modules.ai.brain.postprocess.availability_guard_policy import (
    inbound_exempt_from_availability_rewrite,
)
from modules.ai.postprocess.safety_nets import apply_staff_contact_safety_net


class _Section:
    def __init__(self, *, id: int, kind: str, body: str, title: str = "") -> None:
        self.id = id
        self.kind = kind
        self.body = body
        self.title = title
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
    monkeypatch.setenv("ARRIVAL_CONTACT_DELIVERY_ENABLED", "1")


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
            body="هشام: 0549815590",
        ),
        _Section(
            id=31,
            kind="escalation_rules",
            body="هيثم: 0542980511",
        ),
    ]


@pytest.mark.parametrize(
    "message",
    [
        "تبي تفاصيل السمر؟",
        "ابي 3 حجم 250",
        "أبي ٢ كيلو",
        "هل السمر متوفر؟",
        "كم سعر السمر",
        "أي والله",
        "نعم",
        "تمام",
    ],
)
def test_commerce_messages_defer_contact_policies(message: str) -> None:
    assert should_defer_contact_policies_for_commerce(message)
    assert classify_staff_contact_request(message).kind == "none"


def test_affirmative_does_not_trigger_arrival_delivery(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _install_call_resolver(monkeypatch)
    db = _StubDB(_merchant_sections())
    assert evaluate_arrival_contact_delivery(db, tenant_id=33, message="أي والله") is None
    assert not is_explicit_arrival_intent("أي والله")


def test_product_quantity_not_name_not_configured(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _install_call_resolver(monkeypatch)
    db = _StubDB(_merchant_sections())
    decision = evaluate_staff_contact_policy(
        db, tenant_id=33, message="ابي 3 حجم 250",
    )
    assert decision is None
    assert is_commerce_or_product_flow_message("ابي 3 حجم 250")


def test_affirmative_blocks_reply_offer_vcard(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _install_call_resolver(monkeypatch)
    db = _StubDB(_merchant_sections())
    result = apply_staff_contact_safety_net(
        customer_msg="أي والله",
        reply_text="أبشر 🌷 تواصل مع أمين عند الوصول للمعرض",
        existing_call_targets=[],
        detected_call_markers=0,
        db=db,
        tenant_id=33,
    )
    assert result.fired is False
    assert result.skipped_reason == "commerce_deferred"


def test_arrival_still_delivers_showroom_contact(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _install_call_resolver(monkeypatch)
    db = _StubDB(_merchant_sections())
    for msg in ("أنا جاي", "وصلت"):
        assert is_explicit_arrival_intent(msg)
        decision = evaluate_arrival_contact_delivery(db, tenant_id=33, message=msg)
        assert decision is not None
        assert decision.deliver_contact is True
        assert decision.call_target is not None


def test_ma_yerd_escalation_still_works(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _install_call_resolver(monkeypatch)
    db = _StubDB(_merchant_sections())
    decision = evaluate_staff_contact_recovery(
        db,
        tenant_id=33,
        phone="966500000000",
        message="مايرد",
        contacts_sent_raw=[{
            "name": "بائع المعرض",
            "phone": "0541690226",
            "turn": 1,
        }],
    )
    assert decision is not None
    assert decision.deliver_contact is True
    assert decision.call_target is not None


def test_explicit_staff_ask_still_works(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _install_call_resolver(monkeypatch)
    db = _StubDB(_merchant_sections())
    decision = evaluate_staff_contact_policy(
        db, tenant_id=33, message="ارسل رقم هشام",
    )
    assert decision is not None
    assert decision.deliver_contact is True
    assert MSG_NAME_NOT_CONFIGURED not in decision.reply_text


def test_availability_unaffected() -> None:
    assert is_commerce_or_product_flow_message("هل السمر متوفر؟")
    assert not inbound_exempt_from_availability_rewrite("هل السمر متوفر؟")

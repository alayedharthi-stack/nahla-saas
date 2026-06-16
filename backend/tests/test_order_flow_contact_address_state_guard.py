"""P0 — contact defer, address evidence, commerce state preservation."""
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

from modules.ai.brain.commerce.address_evidence_gate import (
    has_sa_address_evidence,
    is_valid_shippable_freeform_sa_address,
)
from modules.ai.brain.commerce.contact_route_policy import (
    has_explicit_contact_intent,
    is_customer_defer_or_return_later,
    should_defer_staff_contact_policy,
)
from modules.ai.brain.commerce.staff_contact_evidence import classify_staff_contact_request
from modules.ai.brain.commerce.staff_contact_policy import evaluate_staff_contact_policy
from modules.ai.brain.execution.orders import _has_sa_checkout_address, _missing_checkout_fields
from modules.ai.brain.types import OrderPreparationState


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


def _merchant_sections() -> List[_Section]:
    return [
        _Section(
            id=1,
            kind="staff_chain",
            title="Staff",
            body="1. هشام — 0541690226 — seller",
        ),
    ]


def _install_call_resolver(monkeypatch: pytest.MonkeyPatch) -> None:
    mod = _types.ModuleType("services.call_resolver")

    def _normalize_saudi_phone(phone: str) -> str:
        digits = "".join(c for c in str(phone or "") if c.isdigit())
        if digits.startswith("966"):
            return digits
        if digits.startswith("0"):
            return "966" + digits[1:]
        return digits

    def _pretty_phone(wa_id: str) -> str:
        return wa_id

    class CallTarget:
        def __init__(self, *, name: str, wa_id: str, phone_display: str, raw_phone: str) -> None:
            self.name = name
            self.wa_id = wa_id
            self.phone_display = phone_display
            self.raw_phone = raw_phone

    mod.CallTarget = CallTarget
    mod._normalize_saudi_phone = _normalize_saudi_phone
    mod._pretty_phone = _pretty_phone
    monkeypatch.setitem(sys.modules, "services.call_resolver", mod)


# ── Layer A — Contact defer guard ────────────────────────────────────────────

class TestContactDeferGuard:
    def test_prayer_defer_does_not_trigger_name_stub(self) -> None:
        msg = "الآن أروح أصلي صلاة الفجر، الله يسعدك وأتواصل معاك إن شاء الله"
        assert is_customer_defer_or_return_later(msg)
        assert should_defer_staff_contact_policy(msg)
        assert not has_explicit_contact_intent(msg)
        assert classify_staff_contact_request(msg).kind == "none"

    def test_aklmk_ba3dain_does_not_trigger_contact_resolver(self) -> None:
        msg = "أكلمك بعدين"
        assert is_customer_defer_or_return_later(msg)
        assert should_defer_staff_contact_policy(msg)
        assert classify_staff_contact_request(msg).kind == "none"

    def test_arja3_lak_defers_contact_routing(self) -> None:
        msg = "أرجع لك بعد شوي"
        assert is_customer_defer_or_return_later(msg)
        assert classify_staff_contact_request(msg).kind == "none"

    def test_abi_raqm_albaie_still_triggers_contact_resolver(
        self, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        _install_call_resolver(monkeypatch)
        db = _StubDB(_merchant_sections())
        msg = "أبي رقم البائع"
        assert has_explicit_contact_intent(msg)
        request = classify_staff_contact_request(msg)
        assert request.kind != "none"
        decision = evaluate_staff_contact_policy(db, tenant_id=33, message=msg)
        assert decision is not None


# ── Layer B — Address evidence gate ───────────────────────────────────────────

class TestAddressEvidenceGate:
    def _prep(self, **kwargs: Any) -> OrderPreparationState:
        prep = OrderPreparationState(
            customer_first_name="محمد",
            customer_last_name="الجيلاني",
            city="مكة المكرمة",
        )
        for key, val in kwargs.items():
            setattr(prep, key, val)
        return prep

    def test_descriptive_address_alone_is_not_enough(self) -> None:
        prep = self._prep(
            address_line=(
                "مكة المكرمة / محافظة الجموم بجوار هايبر بندة "
                "أو شركة سمسا بالجموم"
            ),
        )
        assert not is_valid_shippable_freeform_sa_address(prep)
        assert not has_sa_address_evidence(prep)
        missing = _missing_checkout_fields(prep, is_sa=True)
        assert "address_location" in missing

    def test_google_maps_link_is_enough(self) -> None:
        prep = self._prep(
            google_maps_url="https://maps.google.com/maps?q=21.4225,39.8262",
        )
        assert has_sa_address_evidence(prep)
        assert _has_sa_checkout_address(prep)
        assert "address_location" not in _missing_checkout_fields(prep, is_sa=True)

    def test_short_national_code_is_enough(self) -> None:
        prep = self._prep(short_address_code="RIYD1234")
        assert has_sa_address_evidence(prep)
        assert "address_location" not in _missing_checkout_fields(prep, is_sa=True)

    def test_valid_freeform_with_street_and_number(self) -> None:
        prep = self._prep(
            address_line="حي النزهة شارع الملك فهد 2456",
        )
        assert is_valid_shippable_freeform_sa_address(prep)
        assert has_sa_address_evidence(prep)


# ── Layer C — Commerce state / product question guard ─────────────────────────

class TestCommerceStatePreservation:
    def test_order_prep_product_id_survives_roundtrip(self) -> None:
        prep = OrderPreparationState(product_id="999", quantity=2)
        restored = OrderPreparationState.from_dict(prep.to_dict())
        assert restored.product_id == "999"
        assert restored.quantity == 2

    def test_product_saved_missing_address_asks_address_not_product(self) -> None:
        prep = OrderPreparationState(
            product_id="ext-42",
            city="مكة المكرمة",
            customer_first_name="محمد",
            customer_last_name="الجيلاني",
        )
        missing = _missing_checkout_fields(prep, is_sa=True)
        assert bool(prep.product_id)
        assert "address_location" in missing
        assert "customer_first_name" not in missing

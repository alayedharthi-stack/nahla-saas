"""Tests for core/customer_name_validator.py and customer_identity_resolver.py"""
from __future__ import annotations

import os
import sys
from types import SimpleNamespace

_here = os.path.dirname(os.path.abspath(__file__))
_backend = os.path.dirname(_here)
for _p in [_backend, os.path.join(_backend, "..")]:
    if _p not in sys.path:
        sys.path.insert(0, _p)

from core.customer_identity_resolver import (  # noqa: E402
    STATUS_CUSTOMER_ENTERED,
    STATUS_PROPOSED,
    STATUS_VERIFIED,
    apply_customer_name,
    can_use_name_for_operations,
    is_official_name_status,
    read_customer_identity,
)
from core.customer_name_validator import validate_customer_name  # noqa: E402


class TestCustomerNameValidator:
    def test_rejects_tayyib_duplicate(self):
        assert not validate_customer_name("طيب طيب").valid

    def test_rejects_product_phrase(self):
        assert not validate_customer_name("ورد عسل السم").valid

    def test_rejects_conversational(self):
        assert not validate_customer_name("تمام").valid
        assert not validate_customer_name("نعم").valid

    def test_rejects_city(self):
        assert not validate_customer_name("الرياض").valid
        assert not validate_customer_name("جدة").valid

    def test_accepts_real_names(self):
        for name in (
            "تركي الحارثي",
            "محمد عبدالله",
            "أبو نواف",
            "أم هشام",
            "نورة القحطاني",
        ):
            hit = validate_customer_name(name)
            assert hit.valid, name


class TestCustomerIdentityResolver:
    def _customer(self, **meta):
        return SimpleNamespace(
            id=1,
            name=meta.pop("name", None),
            acquisition_channel=meta.pop("acquisition_channel", None),
            extra_metadata=meta or None,
        )

    def test_whatsapp_profile_stays_proposed(self):
        c = self._customer()
        apply_customer_name(c, "Mohammed Ali", source="whatsapp_inbound")
        snap = read_customer_identity(c)
        assert c.name is None
        assert snap.proposed_name == "Mohammed Ali"
        assert snap.customer_name_status == STATUS_PROPOSED
        assert not can_use_name_for_operations(c)

    def test_salla_order_verified(self):
        c = self._customer()
        apply_customer_name(c, "محمد العمري", source="salla_sync")
        snap = read_customer_identity(c)
        assert c.name == "محمد العمري"
        assert snap.customer_name_status == STATUS_VERIFIED
        assert can_use_name_for_operations(c)

    def test_customer_message_validated(self):
        c = self._customer()
        apply_customer_name(
            c,
            "فهد العتيبي",
            source="ai_detected_name",
            explicit_customer_entry=True,
        )
        snap = read_customer_identity(c)
        assert snap.customer_name_status == STATUS_CUSTOMER_ENTERED
        assert is_official_name_status(snap.customer_name_status)

    def test_verified_not_overwritten_by_proposed(self):
        c = self._customer(
            name="محمد العمري",
            customer_name_status=STATUS_VERIFIED,
            customer_name_source="salla_order",
            name_source="salla_sync",
        )
        apply_customer_name(c, "طيب", source="whatsapp_inbound")
        assert c.name == "محمد العمري"

    def test_invalid_name_rejected(self):
        c = self._customer()
        apply_customer_name(c, "طيب طيب", source="ai_detected_name", explicit_customer_entry=True)
        assert c.name is None

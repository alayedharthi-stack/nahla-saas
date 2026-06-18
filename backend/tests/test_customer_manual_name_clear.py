"""Regression: merchant manual name clear + later AI explicit refill."""
from __future__ import annotations

import os
import sys
from types import SimpleNamespace

import pytest

_here = os.path.dirname(os.path.abspath(__file__))
_backend = os.path.dirname(_here)
for _p in [_backend, os.path.join(_backend, "..")]:
    if _p not in sys.path:
        sys.path.insert(0, _p)

from core.customer_identity_resolver import (  # noqa: E402
    SOURCE_CUSTOMER_MESSAGE,
    STATUS_CUSTOMER_ENTERED,
    STATUS_MISSING,
    apply_customer_name,
    display_name_for_customer,
    read_customer_identity,
)
from core.customer_name_extractor import extract_high_confidence_name  # noqa: E402
from routers.customers import CustomerPatchIn  # noqa: E402


def _customer(**kwargs):
    return SimpleNamespace(
        id=kwargs.get("id", 42),
        tenant_id=kwargs.get("tenant_id", 1),
        phone=kwargs.get("phone", "966536867615"),
        normalized_phone=kwargs.get("normalized_phone", "+966536867615"),
        name=kwargs.get("name"),
        email=kwargs.get("email"),
        acquisition_channel=kwargs.get("acquisition_channel", "whatsapp_inbound"),
        extra_metadata=dict(kwargs.get("extra_metadata") or {}),
    )


def _simulate_admin_clear(customer) -> None:
    """Mirror ``update_customer`` name-clear branch."""
    customer.name = None
    meta = dict(customer.extra_metadata or {})
    meta["customer_name_status"] = STATUS_MISSING
    meta["customer_name_source"] = "manual_admin"
    meta["name_source"] = "manual_admin"
    meta.pop("proposed_name", None)
    meta["manual_name_override"] = True
    meta["manual_name_cleared"] = True
    meta["manual_name_source"] = "manual_admin"
    customer.extra_metadata = meta


class TestManualClearIdentity:
    def test_cleared_name_does_not_resurrect_proposed_display(self) -> None:
        customer = _customer(
            name=None,
            phone="966536867615",
            extra_metadata={
                "manual_name_override": True,
                "manual_name_cleared": True,
                "customer_name_status": STATUS_MISSING,
                "proposed_name": "انضحك عليه",
            },
        )

        snap = read_customer_identity(customer)
        assert snap.customer_name == ""
        assert snap.display_name == ""
        assert display_name_for_customer(
            customer,
            phone_fallback="+966536867615",
        ) == "+966536867615"

    def test_admin_clear_wrong_name_then_ai_explicit_refill(self) -> None:
        customer = _customer(
            name="انضحك عليه",
            extra_metadata={
                "customer_name_source": SOURCE_CUSTOMER_MESSAGE,
                "customer_name_status": STATUS_CUSTOMER_ENTERED,
                "proposed_name": "WhatsApp Alias",
            },
        )

        _simulate_admin_clear(customer)
        assert customer.name is None
        assert customer.extra_metadata.get("manual_name_cleared") is True
        assert "proposed_name" not in customer.extra_metadata

        hit = extract_high_confidence_name("أنا اسمي عبدالله")
        assert hit is not None
        assert hit.value == "عبدالله"

        applied = apply_customer_name(
            customer,
            hit.value,
            source="ai_detected_name",
            explicit_customer_entry=True,
        )
        assert applied is True
        assert customer.name == "عبدالله"
        assert customer.extra_metadata.get("manual_name_cleared") is False

    def test_complaint_message_does_not_refill_after_manual_clear(self) -> None:
        customer = _customer(
            name=None,
            extra_metadata={
                "manual_name_override": True,
                "manual_name_cleared": True,
                "customer_name_status": STATUS_MISSING,
            },
        )

        assert extract_high_confidence_name("انا انضحك علي") is None
        assert customer.name is None


class TestCustomerPatchEmptyName:
    @pytest.mark.parametrize("payload", [{"name": ""}, {"name": "   "}, {"name": None}])
    def test_patch_model_treats_empty_as_explicit_clear(self, payload: dict) -> None:
        body = CustomerPatchIn(**payload)
        fields = getattr(body, "model_fields_set", None) or getattr(body, "__fields_set__", set())
        assert "name" in fields
        assert body.name is None

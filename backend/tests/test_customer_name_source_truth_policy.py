"""Regression tests for customer-name source-of-truth policy."""
from __future__ import annotations

import os
import sys
import asyncio
from types import SimpleNamespace

_here = os.path.dirname(os.path.abspath(__file__))
_backend = os.path.dirname(_here)
for _p in [_backend, os.path.join(_backend, "..")]:
    if _p not in sys.path:
        sys.path.insert(0, _p)

from core.customer_identity_resolver import (  # noqa: E402
    SOURCE_CUSTOMER_MESSAGE,
    SOURCE_MERCHANT,
    STATUS_CUSTOMER_ENTERED,
    STATUS_MISSING,
    STATUS_VERIFIED,
    apply_customer_name,
    read_customer_identity,
)
from core.customer_name_adoption_guard import (  # noqa: E402
    can_ai_update_customer_name,
)
from core.customer_name_extractor import extract_high_confidence_name  # noqa: E402
from modules.ai.brain.commerce.catalog_checkout_customer_identity import (  # noqa: E402
    filter_missing_for_known_catalog_customer,
)
from modules.ai.order_flow_v2.slot_ownership import apply_slot_ownership  # noqa: E402
from services.store_sync import StoreSyncService  # noqa: E402


def _customer(*, name: str | None = None, source: str = "", status: str = "", **meta):
    extra = dict(meta)
    if source:
        extra["customer_name_source"] = source
        extra["name_source"] = source
    if status:
        extra["customer_name_status"] = status
    return SimpleNamespace(
        id=42,
        name=name,
        phone="966500000000",
        normalized_phone="+966500000000",
        acquisition_channel="whatsapp_inbound",
        extra_metadata=extra,
    )


def _try_ai_update(customer, message: str, candidate: str) -> bool:
    return apply_customer_name(
        customer,
        candidate,
        source="ai_detected_name",
        explicit_customer_entry=True,
        message_context={
            "message": message,
            "source": "ai_detected_name",
            "explicit_customer_entry": True,
        },
    )


def test_salla_name_cannot_be_overwritten_by_courier_role_phrase() -> None:
    customer = _customer(name="هشام تركي", source="salla", status=STATUS_VERIFIED)

    changed = _try_ai_update(customer, "معك مندوب سمسا", "مندوب سمسا")

    assert changed is False
    assert customer.name == "هشام تركي"
    assert "proposed_name" not in (customer.extra_metadata or {})


def test_zid_name_cannot_be_overwritten_by_role_phrase() -> None:
    customer = _customer(name="هشام تركي", source="zid", status=STATUS_VERIFIED)

    changed = _try_ai_update(customer, "معك مندوب سمسا", "مندوب سمسا")

    assert changed is False
    assert customer.name == "هشام تركي"


def test_shopify_name_cannot_be_overwritten_by_role_phrase() -> None:
    customer = _customer(name="هشام تركي", source="shopify", status=STATUS_VERIFIED)

    changed = _try_ai_update(customer, "أنا مندوب الشحن", "مندوب الشحن")

    assert changed is False
    assert customer.name == "هشام تركي"


def test_merchant_manual_non_empty_name_cannot_be_overwritten_by_ai() -> None:
    customer = _customer(
        name="فهد العتيبي",
        source=SOURCE_MERCHANT,
        status=STATUS_CUSTOMER_ENTERED,
        manual_name_override=True,
        manual_name_cleared=False,
    )

    changed = _try_ai_update(customer, "اسمي هشام تركي", "هشام تركي")

    assert changed is False
    assert customer.name == "فهد العتيبي"


def test_merchant_manual_cleared_empty_name_accepts_explicit_valid_customer_name() -> None:
    customer = _customer(
        name=None,
        source="manual_admin",
        status=STATUS_MISSING,
        manual_name_override=True,
        manual_name_cleared=True,
    )
    hit = extract_high_confidence_name("اسمي هشام تركي")
    assert hit is not None

    changed = _try_ai_update(customer, "اسمي هشام تركي", hit.value)

    assert changed is True
    assert customer.name == "هشام تركي"
    snap = read_customer_identity(customer)
    assert snap.customer_name_source == SOURCE_CUSTOMER_MESSAGE
    assert snap.customer_name_status == STATUS_CUSTOMER_ENTERED
    assert customer.extra_metadata.get("manual_name_cleared") is False


def test_merchant_manual_cleared_empty_name_rejects_role_context_phrase() -> None:
    customer = _customer(
        name=None,
        source="manual_admin",
        status=STATUS_MISSING,
        manual_name_override=True,
        manual_name_cleared=True,
    )

    changed = _try_ai_update(customer, "معك مندوب سمسا", "مندوب سمسا")

    assert changed is False
    assert customer.name is None


def test_unknown_customer_can_accept_explicit_self_declared_name() -> None:
    customer = _customer(name=None)
    hit = extract_high_confidence_name("اسمي هشام تركي")
    assert hit is not None

    changed = _try_ai_update(customer, "اسمي هشام تركي", hit.value)

    assert changed is True
    assert customer.name == "هشام تركي"


def test_bare_ana_valid_name_allowed_but_role_context_blocked() -> None:
    name_hit = extract_high_confidence_name("أنا هشام تركي")
    role_hit = extract_high_confidence_name("أنا مندوب الشحن")
    location_hit = extract_high_confidence_name("أنا في الموقع")

    assert name_hit is not None
    assert name_hit.value == "هشام تركي"
    assert role_hit is None
    assert location_hit is None


def test_customer_entered_validated_requires_explicit_correction() -> None:
    customer = _customer(
        name="عبدالله علي",
        source=SOURCE_CUSTOMER_MESSAGE,
        status=STATUS_CUSTOMER_ENTERED,
    )

    ordinary = _try_ai_update(customer, "معك هشام تركي", "هشام تركي")
    correction = _try_ai_update(customer, "اسمي الصحيح هشام تركي", "هشام تركي")

    assert ordinary is False
    assert correction is True
    assert customer.name == "هشام تركي"


def test_can_ai_update_customer_name_reports_platform_lock() -> None:
    customer = _customer(name="هشام تركي", source="commerce_platform", status=STATUS_VERIFIED)

    assert can_ai_update_customer_name(
        customer,
        "هشام تركي",
        {"message": "اسمي الصحيح هشام تركي", "explicit_customer_entry": True},
    ) is False


def test_unknown_catalog_checkout_can_capture_customer_name_and_continue() -> None:
    prep = {
        "catalog_line_items_authoritative": True,
        "line_items": [{"product_retailer_id": "sku-1", "quantity": 1}],
        "missing_fields": ["customer_name", "city", "delivery_address"],
    }

    patch, reason = apply_slot_ownership(
        message="هشام تركي",
        order_prep=prep,
        missing_fields=["customer_name", "city", "delivery_address"],
    )

    assert reason == "customer_name_owned"
    assert patch["customer_first_name"] == "هشام"
    assert patch["customer_last_name"] == "تركي"
    assert patch["order_flow_v2_last_field"] == "city"


def test_phase_2_8_known_name_skips_catalog_checkout_name_prompt() -> None:
    missing = filter_missing_for_known_catalog_customer(
        ["customer_name", "customer_first_name", "city"],
        known_facts={"customer_name_known": True, "customer_name": "هشام تركي"},
        phone="966500000000",
    )

    assert "customer_name" not in missing
    assert "customer_first_name" not in missing
    assert "city" in missing


def test_phone_known_rule_remains_unchanged() -> None:
    missing = filter_missing_for_known_catalog_customer(
        ["customer_phone", "phone", "city"],
        known_facts={"phone_known": True},
        phone="966500000000",
    )

    assert "customer_phone" not in missing
    assert "phone" not in missing
    assert "city" in missing


class _FakeCustomerQuery:
    def __init__(self, customer):
        self._customer = customer

    def filter(self, *_args, **_kwargs):
        return self

    def first(self):
        return self._customer


class _FakeStoreSyncDB:
    def __init__(self, customer):
        self.customer = customer
        self.flushed = 0
        self.added = []

    def query(self, *_args, **_kwargs):
        return _FakeCustomerQuery(self.customer)

    def flush(self):
        self.flushed += 1

    def add(self, obj):
        self.added.append(obj)


class _FakeCustomerAdapter:
    platform = "salla"

    async def get_customers(self, *, updated_since=None):
        return [
            {
                "id": "salla-1",
                "name": "اسم سلة",
                "email": "salla@example.com",
                "mobile": "966500000000",
                "city": "الرياض",
                "country": "SA",
            }
        ]


def test_store_sync_respects_merchant_manual_customer_name_lock() -> None:
    customer = _customer(
        name="اسم التاجر",
        source=SOURCE_MERCHANT,
        status=STATUS_CUSTOMER_ENTERED,
        manual_name_override=True,
        manual_name_cleared=False,
    )
    customer.tenant_id = 33
    customer.salla_customer_id = "salla-1"
    customer.acquisition_channel = "manual"
    db = _FakeStoreSyncDB(customer)
    service = StoreSyncService(db, tenant_id=33)
    service._adapter = _FakeCustomerAdapter()

    synced = asyncio.run(service.sync_customers())

    assert synced == 1
    assert customer.name == "اسم التاجر"
    assert customer.email == "salla@example.com"
    assert customer.extra_metadata["salla_id"] == "salla-1"
    assert customer.extra_metadata["customer_name_source"] == SOURCE_MERCHANT

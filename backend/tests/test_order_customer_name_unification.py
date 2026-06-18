"""Customer name unification from WA order editor through serializers."""
from __future__ import annotations

import sys
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest

BACKEND_ROOT = Path(__file__).resolve().parents[1]
if str(BACKEND_ROOT) not in sys.path:
    sys.path.insert(0, str(BACKEND_ROOT))

from core.customer_display import is_valid_customer_display_name  # noqa: E402
from core.customer_identity_resolver import (  # noqa: E402
    SOURCE_MERCHANT,
    apply_customer_name,
    display_name_for_customer,
)
from core.order_customer_display import (  # noqa: E402
    compose_customer_full_name,
    resolve_order_customer_display_name,
    sync_order_customer_identity,
)
from core.wa_order_editor import update_order_customer  # noqa: E402
from routers.orders import _resolve_customer_display  # noqa: E402


def _wa_order(**overrides):
    base = dict(
        id=1,
        tenant_id=33,
        external_id="nahla-wa-33-1",
        status="draft",
        customer_name="966551308005",
        customer_info={"phone": "966551308005", "name": "."},
        line_items=[],
        source="whatsapp",
        extra_metadata={"customer_name": "966551308005"},
    )
    base.update(overrides)
    return SimpleNamespace(**base)


class TestDisplayHelpers:
    def test_never_validates_dot_as_name(self) -> None:
        assert is_valid_customer_display_name(".") is False
        assert is_valid_customer_display_name("966551308005") is False
        assert is_valid_customer_display_name("دكتور صالح") is True

    def test_compose_full_name(self) -> None:
        assert compose_customer_full_name("دكتور", "صالح") == "دكتور صالح"


class TestOrderCustomerDisplay:
    def test_split_names_win_over_stale_metadata_phone(self) -> None:
        order = _wa_order(
            extra_metadata={
                "customer_name": "966551308005",
                "customer_first_name": "دكتور",
                "customer_last_name": "صالح",
            },
            customer_name="966551308005",
        )
        assert resolve_order_customer_display_name(order, {}) == "دكتور صالح"
        assert _resolve_customer_display(order, {}) == "دكتور صالح"

    def test_serializer_does_not_return_dot(self) -> None:
        order = _wa_order(
            customer_name=".",
            customer_info={"phone": "966551308005", "name": "."},
            extra_metadata={"customer_name": "."},
        )
        assert _resolve_customer_display(order, {}) == "966551308005"


class TestUpdateOrderCustomer:
    def test_writes_unified_name_fields(self) -> None:
        o = _wa_order()
        update_order_customer(
            o,
            first_name="دكتور",
            last_name="صالح",
            phone="966551308005",
        )
        assert o.customer_name == "دكتور صالح"
        assert o.extra_metadata["customer_name"] == "دكتور صالح"
        assert o.customer_info["name"] == "دكتور صالح"
        assert o.extra_metadata["customer_first_name"] == "دكتور"
        assert o.extra_metadata["customer_last_name"] == "صالح"


class TestCustomerSync:
    def test_sync_updates_customer_by_tenant_phone(self) -> None:
        order = _wa_order(
            extra_metadata={
                "customer_first_name": "دكتور",
                "customer_last_name": "صالح",
            },
            customer_info={"phone": "966551308005", "first_name": "دكتور", "last_name": "صالح"},
            customer_name="دكتور صالح",
        )
        customer = SimpleNamespace(
            id=9,
            tenant_id=33,
            phone="966551308005",
            normalized_phone="+966551308005",
            name=".",
            extra_metadata={},
        )
        db = MagicMock()
        svc = MagicMock()
        svc.find_customer_by_phone.return_value = customer
        svc.upsert_customer_identity.return_value = customer

        with patch(
            "services.customer_intelligence.CustomerIntelligenceService",
            return_value=svc,
        ):
            sync_order_customer_identity(db, 33, order)

        assert customer.name == "دكتور صالح"

    def test_whatsapp_profile_does_not_overwrite_merchant_name(self) -> None:
        c = SimpleNamespace(
            id=1,
            name="دكتور صالح",
            extra_metadata={
                "manual_name_override": True,
                "customer_name_source": SOURCE_MERCHANT,
                "customer_name_status": "customer_entered_validated",
            },
        )
        blocked = apply_customer_name(c, "WA Profile", source="whatsapp_inbound")
        assert blocked is False
        assert c.name == "دكتور صالح"

    def test_display_name_for_customer_skips_dot(self) -> None:
        c = SimpleNamespace(
            id=1,
            name=".",
            extra_metadata={},
            acquisition_channel="",
        )
        assert display_name_for_customer(c, phone_fallback="966551308005") == "966551308005"

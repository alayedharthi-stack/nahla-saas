"""Generic platform tests for unified local order resolver (thin slice)."""
from __future__ import annotations

import os
import sys
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import pytest

_HERE = os.path.dirname(os.path.abspath(__file__))
_BACKEND = os.path.abspath(os.path.join(_HERE, ".."))
if _BACKEND not in sys.path:
    sys.path.insert(0, _BACKEND)

from core.local_order_resolver import (  # noqa: E402
    resolve_customer_order_context,
)
from core.order_creation_evidence import resolve_track_order_fallback  # noqa: E402
from modules.ai.commerce.runtime import CommerceToolRuntime  # noqa: E402
from modules.ai.order_flow_v2.replies import build_checkout_order_number_reply  # noqa: E402
from tests.commerce_scenario_fixtures import (  # noqa: E402
    DEFAULT_PHONE_E164,
    make_scenario_db,
    seed_conversation,
    seed_customer,
    seed_order,
    seed_tenant,
)

_GENERIC_ITEM = {
    "product_id": "sku-shirt-blue",
    "product_name": "قميص قطني أزرق",
    "quantity": 1,
    "unit_price": 149.0,
}


@pytest.fixture()
def db():
    session, _engine = make_scenario_db()
    yield session
    session.close()


@pytest.fixture()
def tenant_ctx(db):
    tenant = seed_tenant(db, name="متجر تجريبي عام")
    customer = seed_customer(db, tenant.id, name="أحمد سالم")
    conv = seed_conversation(db, tenant.id, customer_id=customer.id)
    return SimpleNamespace(
        tenant_id=tenant.id,
        customer_id=customer.id,
        conversation_id=conv.id,
        phone=DEFAULT_PHONE_E164,
    )


@pytest.mark.parametrize(
    "source,external_id,external_order_number,status",
    [
        ("whatsapp", "nahla-wa-1-99", "NHL-1-000101", "pending_payment"),
        ("salla", "salla-ord-9001", "SAL-9001", "processing"),
        ("shopify", "shopify-ord-7001", "SHP-7001", "paid"),
        ("zid", "zid-ord-5001", "ZID-5001", "shipped"),
        ("manual", "manual-ord-3001", "MAN-3001", "pending_payment"),
    ],
)
def test_order_number_returns_local_reference_per_source(
    db, tenant_ctx, source, external_id, external_order_number, status,
) -> None:
    wa_ext = f"nahla-wa-{tenant_ctx.tenant_id}-{tenant_ctx.conversation_id}"
    ext_id = wa_ext if source == "whatsapp" else external_id
    seed_order(
        db,
        tenant_ctx.tenant_id,
        source=source,
        external_id=ext_id,
        external_order_number=external_order_number,
        status=status,
        customer_info={"phone": tenant_ctx.phone},
        line_items=[_GENERIC_ITEM],
    )
    ctx = resolve_customer_order_context(
        db,
        tenant_id=tenant_ctx.tenant_id,
        phone=tenant_ctx.phone,
        intent="order_number",
    )
    assert ctx.selected_order is not None
    assert ctx.selected_order.display_reference == external_order_number
    assert ctx.selected_order.source == source

    reply = build_checkout_order_number_reply(
        db,
        tenant_id=tenant_ctx.tenant_id,
        conversation=SimpleNamespace(
            id=tenant_ctx.conversation_id,
            customer_id=tenant_ctx.customer_id,
        ),
        order_prep={},
        brain_state={},
        customer_phone=tenant_ctx.phone,
    )
    assert external_order_number in reply


def test_track_order_finds_local_order_not_no_orders(db, tenant_ctx) -> None:
    seed_order(
        db,
        tenant_ctx.tenant_id,
        source="salla",
        external_id="salla-import-1",
        external_order_number="SAL-IMPORT-1",
        status="processing",
        customer_info={"phone": tenant_ctx.phone},
        line_items=[_GENERIC_ITEM],
    )
    ctx = resolve_customer_order_context(
        db,
        tenant_id=tenant_ctx.tenant_id,
        phone=tenant_ctx.phone,
        intent="track_order",
    )
    assert ctx.selected_order is not None
    assert ctx.selected_reason in {
        "latest_open_order",
        "active_whatsapp_draft",
        "most_recent_order",
    }

    fallback = resolve_track_order_fallback(
        db=db,
        tenant_id=tenant_ctx.tenant_id,
        conversation_id=tenant_ctx.conversation_id,
        phone=tenant_ctx.phone,
    )
    assert fallback is not None
    assert "SAL-IMPORT-1" in fallback


def test_track_tool_local_db_before_adapter(db, tenant_ctx) -> None:
    import asyncio

    seed_order(
        db,
        tenant_ctx.tenant_id,
        source="manual",
        external_id="manual-local-1",
        external_order_number="MAN-LOCAL-1",
        status="paid",
        customer_info={"phone": tenant_ctx.phone},
        line_items=[_GENERIC_ITEM],
    )
    runtime = CommerceToolRuntime(
        db,
        tenant_id=tenant_ctx.tenant_id,
        customer_phone=tenant_ctx.phone,
        customer_id=tenant_ctx.customer_id,
    )
    with patch(
        "store_integration.order_service.get_customer_orders",
        new_callable=AsyncMock,
    ) as adapter_list, patch(
        "store_integration.order_service.get_order",
        new_callable=AsyncMock,
    ) as adapter_one:
        adapter_list.return_value = []
        adapter_one.return_value = None
        result = asyncio.run(
            runtime.execute(
                "track_order",
                {"conversation_id": tenant_ctx.conversation_id},
            )
        )
    adapter_list.assert_not_called()
    adapter_one.assert_not_called()
    assert result.ok is True
    assert result.payload["local_resolver"] is True
    assert result.payload["order"]["reference_id"] == "MAN-LOCAL-1"


def test_latest_open_order_wins_over_older_closed(db, tenant_ctx) -> None:
    seed_order(
        db,
        tenant_ctx.tenant_id,
        source="shopify",
        external_id="shop-old",
        external_order_number="SHP-OLD",
        status="delivered",
        customer_info={"phone": tenant_ctx.phone},
    )
    seed_order(
        db,
        tenant_ctx.tenant_id,
        source="shopify",
        external_id="shop-open",
        external_order_number="SHP-OPEN",
        status="pending_payment",
        customer_info={"phone": tenant_ctx.phone},
    )
    ctx = resolve_customer_order_context(
        db,
        tenant_id=tenant_ctx.tenant_id,
        phone=tenant_ctx.phone,
        intent="track_order",
    )
    assert ctx.latest_open_order is not None
    assert ctx.latest_open_order.external_order_number == "SHP-OPEN"
    assert ctx.selected_order is not None
    assert ctx.selected_order.external_order_number == "SHP-OPEN"


def test_active_whatsapp_draft_priority_in_conversation(db, tenant_ctx) -> None:
    seed_order(
        db,
        tenant_ctx.tenant_id,
        source="salla",
        external_id="salla-other",
        external_order_number="SAL-OTHER",
        status="pending_payment",
        customer_info={"phone": tenant_ctx.phone},
    )
    wa_ext = f"nahla-wa-{tenant_ctx.tenant_id}-{tenant_ctx.conversation_id}"
    seed_order(
        db,
        tenant_ctx.tenant_id,
        source="whatsapp",
        external_id=wa_ext,
        external_order_number="NHL-1-000222",
        status="pending_payment",
        customer_info={"phone": tenant_ctx.phone},
        extra_metadata={
            "lifecycle": "whatsapp_draft",
            "conversation_id": tenant_ctx.conversation_id,
        },
    )
    ctx = resolve_customer_order_context(
        db,
        tenant_id=tenant_ctx.tenant_id,
        conversation_id=tenant_ctx.conversation_id,
        phone=tenant_ctx.phone,
        intent="order_number",
    )
    assert ctx.active_whatsapp_draft is not None
    assert ctx.selected_order is not None
    assert ctx.selected_reason == "active_whatsapp_draft"
    assert ctx.selected_order.external_order_number == "NHL-1-000222"


def test_tenant_isolation(db, tenant_ctx) -> None:
    other = seed_tenant(db, name="متجر آخر")
    seed_order(
        db,
        other.id,
        source="zid",
        external_id="zid-tenant-b",
        external_order_number="ZID-B-1",
        status="pending_payment",
        customer_info={"phone": tenant_ctx.phone},
    )
    seed_order(
        db,
        tenant_ctx.tenant_id,
        source="zid",
        external_id="zid-tenant-a",
        external_order_number="ZID-A-1",
        status="pending_payment",
        customer_info={"phone": tenant_ctx.phone},
    )
    ctx = resolve_customer_order_context(
        db,
        tenant_id=tenant_ctx.tenant_id,
        phone=tenant_ctx.phone,
        intent="order_number",
    )
    assert ctx.selected_order is not None
    assert ctx.selected_order.external_order_number == "ZID-A-1"


def test_explicit_order_number_match(db, tenant_ctx) -> None:
    seed_order(
        db,
        tenant_ctx.tenant_id,
        source="salla",
        external_id="salla-a",
        external_order_number="SAL-A",
        status="delivered",
        customer_info={"phone": tenant_ctx.phone},
    )
    seed_order(
        db,
        tenant_ctx.tenant_id,
        source="salla",
        external_id="salla-b",
        external_order_number="SAL-B",
        status="pending_payment",
        customer_info={"phone": tenant_ctx.phone},
    )
    ctx = resolve_customer_order_context(
        db,
        tenant_id=tenant_ctx.tenant_id,
        phone=tenant_ctx.phone,
        intent="track_order",
        order_number="SAL-A",
    )
    assert ctx.selected_order is not None
    assert ctx.selected_reason == "explicit_order_number"
    assert ctx.selected_order.external_order_number == "SAL-A"

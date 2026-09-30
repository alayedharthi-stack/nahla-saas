"""The customer's order list beside the resolved order, on real PostgreSQL.

A real-PostgreSQL proof of the read behind ``resolve_customer_order``.

Production, 2026-09-30 (tenant 1, runtime turn 112): the customer asked for
their previous orders. The resolver read twenty orders for that customer — all
matched by the phone in ``customer_info`` (JSONB), none carrying a customer id,
nine still open and eleven closed — and the tool handed the model one of them.
The reply said the customer had one order. What is proved here is the list the
tool now carries on the database it runs on: every order counted by the same
JSONB phone match the resolver uses, split into current and previous, bounded,
complete only when the read held everything, and scoped exactly as the resolved
order is — never another tenant's, never an order linked to another customer.
A listed order is not authorized for the details read.

The data is a generic merchant, not any production store.
"""
from __future__ import annotations

import asyncio
import sys
import uuid
from pathlib import Path
from typing import Any, List

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
for _p in (str(REPO_ROOT), str(REPO_ROOT / "backend"), str(REPO_ROOT / "database")):
    if _p not in sys.path:
        sys.path.insert(0, _p)

from core.local_order_resolver import CUSTOMER_ORDER_READ_LIMIT  # noqa: E402
from modules.ai.commerce_agent_v2.tools import orders as tool  # noqa: E402
from modules.ai.security.tenant_isolation import TenantIsolationViolation  # noqa: E402

PHONE = "+966500000001"


class Store:
    """One tenant with one customer and one conversation, plus a second tenant."""

    def __init__(self, session: Any) -> None:
        from models import Conversation, Customer, Tenant, WhatsAppConnection

        self.session = session
        tag = uuid.uuid4().hex[:8]
        self.tenant = Tenant(name=f"متجر تجريبي عام — طلبات {tag}", is_active=True)
        self.other_tenant = Tenant(name=f"متجر تجريبي آخر — طلبات {tag}", is_active=True)
        session.add_all([self.tenant, self.other_tenant])
        session.flush()
        self.customer = Customer(tenant_id=self.tenant.id, name="أحمد سالم", phone="0500000001",
                                 normalized_phone=PHONE)
        self.neighbour = Customer(tenant_id=self.tenant.id, name="نورة عبدالله", phone="0500000002",
                                  normalized_phone="+966500000002")
        session.add_all([self.customer, self.neighbour])
        session.flush()
        self.conversation = Conversation(tenant_id=self.tenant.id, customer_id=self.customer.id,
                                         status="active")
        self.connection = WhatsAppConnection(tenant_id=self.tenant.id, status="connected")
        session.add_all([self.conversation, self.connection])
        session.flush()
        self._n = 0

    def order(self, status: str, *, phone: str = PHONE, customer_id: Any = None,
              tenant_id: Any = None) -> Any:
        from models import Order

        self._n += 1
        row = Order(tenant_id=tenant_id or self.tenant.id, customer_id=customer_id,
                    external_id=f"pg-order-{self.tenant.id}-{self._n}",
                    external_order_number=f"GEN-{self.tenant.id}-{self._n}", status=status, total="99",
                    source="salla", customer_info={"phone": phone},
                    line_items=[{"name": "عطر ورد 100ml", "quantity": 1}])
        self.session.add(row)
        self.session.flush()
        return row

    def context(self) -> Any:
        from modules.ai.commerce_agent_v2.context import CommerceAgentContext

        self.session.flush()          # visible to this session; the fixture rolls it all back
        return CommerceAgentContext.from_trusted_scope(
            db=self.session, tenant_id=self.tenant.id, conversation_id=self.conversation.id,
            customer_id=self.customer.id, normalized_customer_phone=PHONE,
            connection_id=str(self.connection.id), inbound_trace_id="order-list-pg")


@pytest.fixture()
def store(disposable_pg):
    session = disposable_pg.session_factory()
    try:
        yield Store(session)
    finally:
        session.rollback()
        session.close()


def resolve(context: Any, **kwargs: Any) -> Any:
    return asyncio.run(tool.resolve_customer_order_impl(context, **kwargs))


def refs(entries: List[Any]) -> List[str]:
    return [e.order_reference for e in entries]


def test_the_observed_shape_is_counted_by_the_jsonb_phone_match(store) -> None:
    closed = [store.order(s) for s in ("cancelled", "cancelled", "abandoned", "delivered", "cancelled",
                                       "canceled", "cancelled", "cancelled", "cancelled", "cancelled",
                                       "cancelled")]
    open_ = [store.order("in_progress") for _ in range(9)]
    result = resolve(store.context())
    listing = result.customer_orders
    assert result.order.order_reference == open_[-1].external_order_number
    assert (listing.status, listing.current_count, listing.previous_count, listing.counts_complete) == (
        "ok", 9, 11, True)
    assert refs(listing.current) == [o.external_order_number for o in reversed(open_)][:5]
    assert refs(listing.previous) == [o.external_order_number for o in reversed(closed)][:5]


def test_the_phone_formats_the_resolver_matches_are_all_counted(store) -> None:
    for phone in ("0500000001", "966500000001", "+966500000001"):
        store.order("processing", phone=phone)
    listing = resolve(store.context()).customer_orders
    assert listing.current_count == 3 and listing.counts_complete is True


def test_another_tenants_and_another_customers_orders_are_not_counted(store) -> None:
    store.order("processing", customer_id=store.neighbour.id)          # this phone, another customer
    store.order("processing", tenant_id=store.other_tenant.id)         # this phone, another store
    store.order("delivered", phone="+966500000009")                    # another phone
    mine = store.order("processing")
    listing = resolve(store.context()).customer_orders
    assert (listing.current_count, listing.previous_count) == (1, 0)
    assert refs(listing.current) == [mine.external_order_number]


def test_a_read_that_reaches_its_limit_is_not_reported_complete(store) -> None:
    for _ in range(CUSTOMER_ORDER_READ_LIMIT + 2):
        store.order("cancelled")
    store.order("processing")
    listing = resolve(store.context()).customer_orders
    assert listing.counts_complete is False
    assert listing.current_count == 1


def test_a_listed_order_is_not_authorized_for_the_details_read(store) -> None:
    older = store.order("delivered")
    store.order("processing")
    context = store.context()
    result = resolve(context)
    assert older.external_order_number in refs(result.customer_orders.previous)
    with pytest.raises(TenantIsolationViolation):
        asyncio.run(tool.get_order_details_impl(context, order_id=older.id))

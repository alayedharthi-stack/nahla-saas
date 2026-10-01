"""The customer's order history, read on request, on real PostgreSQL.

Production, 2026-09-30 (tenant 1, runtime turn 112): the customer asked for
their previous orders. The runtime had read twenty orders for that customer —
all matched by the phone in ``customer_info`` (JSONB), none carrying a customer
id — and handed the model one. The history is now its own read. What is proved
here, on the database it runs on:

* every order is counted by the same JSONB phone match the resolver uses, in
  every stored phone format, and never another store's or another customer's;
* an abandoned cart, kept by store sync as an order row, is not counted;
* a read at its limit gives no total, only a lower bound;
* a Salla ``completed`` order is ongoing (fulfilled, not yet shipped);
* the one-order lookup picks the order the history calls the newest ongoing
  one, and never a cart;
* listing an order does not authorize reading it;
* a history read that fails inside the database leaves the session's
  transaction usable: on PostgreSQL a failed statement aborts the transaction,
  and the read's savepoint is what keeps the tools after it working.

The data is a generic merchant, not any production store.
"""
from __future__ import annotations

import asyncio
import sys
import uuid
from pathlib import Path
from typing import Any

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
for _p in (str(REPO_ROOT), str(REPO_ROOT / "backend"), str(REPO_ROOT / "database")):
    if _p not in sys.path:
        sys.path.insert(0, _p)

from sqlalchemy import text  # noqa: E402

from core.local_order_resolver import CUSTOMER_ORDER_HISTORY_LIMIT  # noqa: E402
from modules.ai.commerce_agent_v2.tools import orders as tool  # noqa: E402
from modules.ai.security.tenant_isolation import TenantIsolationViolation  # noqa: E402

PHONE = "+966500000001"


class Store:
    """One tenant with one customer and one conversation, plus a second tenant."""

    def __init__(self, session: Any) -> None:
        from models import Conversation, Customer, Tenant, WhatsAppConnection

        self.session = session
        tag = uuid.uuid4().hex[:8]
        self.tenant = Tenant(name=f"متجر تجريبي عام — سجل {tag}", is_active=True)
        self.other_tenant = Tenant(name=f"متجر تجريبي آخر — سجل {tag}", is_active=True)
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
              tenant_id: Any = None, source: str = "salla", cart: bool | None = False) -> Any:
        from models import Order

        self._n += 1
        row = Order(tenant_id=tenant_id or self.tenant.id, customer_id=customer_id,
                    external_id=(f"cart-{self.tenant.id}-{self._n}" if cart
                                 else f"pg-order-{self.tenant.id}-{self._n}"),
                    is_abandoned=cart,
                    external_order_number=f"GEN-{self.tenant.id}-{self._n}", status=status, total="99",
                    source=source, customer_info={"phone": phone},
                    line_items=[{"name": "عطر ورد 100ml", "quantity": 1}])
        self.session.add(row)
        self.session.flush()
        if cart is None:                  # the column's default fills a None on insert
            self.session.query(Order).filter(Order.id == row.id).update(
                {Order.is_abandoned: None}, synchronize_session="fetch")
        return row

    def context(self) -> Any:
        from modules.ai.commerce_agent_v2.context import CommerceAgentContext

        self.session.flush()          # visible to this session; the fixture rolls it all back
        return CommerceAgentContext.from_trusted_scope(
            db=self.session, tenant_id=self.tenant.id, conversation_id=self.conversation.id,
            customer_id=self.customer.id, normalized_customer_phone=PHONE,
            connection_id=str(self.connection.id), inbound_trace_id="order-history-pg")


@pytest.fixture()
def store(disposable_pg):
    session = disposable_pg.session_factory()
    try:
        yield Store(session)
    finally:
        session.rollback()
        session.close()


def history(context: Any) -> Any:
    return asyncio.run(tool.list_customer_orders_impl(context))


def refs(group: Any) -> list:
    return [entry.order_reference for entry in group.orders]


def test_the_observed_shape_is_counted_by_the_jsonb_phone_match(store) -> None:
    for status in ("cancelled", "cancelled", "delivered", "cancelled", "canceled",
                   "cancelled", "cancelled", "cancelled", "cancelled", "cancelled"):
        store.order(status)
    store.order("abandoned", cart=True)
    for _ in range(9):
        store.order("in_progress")
    result = history(store.context())
    assert (result.read_complete, result.total_orders) == (True, 19)
    assert (result.ongoing.count, result.finished.count) == (9, 10)


def test_an_abandoned_cart_is_not_counted(store) -> None:
    store.order("abandoned", cart=True)
    store.order("abandoned")
    store.order("pending", cart=True)
    store.order("delivered")
    store.order("processing", cart=None)          # no flag stored (NULL): an order, not a cart
    result = history(store.context())
    assert (result.read_complete, result.total_orders) == (True, 2)


def test_every_stored_phone_format_is_counted_and_no_other_store_or_customer(store) -> None:
    for phone in ("0500000001", "966500000001", "+966500000001"):
        store.order("processing", phone=phone)
    store.order("processing", customer_id=store.neighbour.id)          # this phone, another customer
    store.order("processing", tenant_id=store.other_tenant.id)         # this phone, another store
    store.order("delivered", phone="+966500000009")                    # another phone
    result = history(store.context())
    assert (result.read_complete, result.total_orders, result.ongoing.count) == (True, 3, 3)


def test_a_read_at_its_limit_gives_no_total(store) -> None:
    for _ in range(CUSTOMER_ORDER_HISTORY_LIMIT + 2):
        store.order("cancelled")
    result = history(store.context())
    assert (result.read_complete, result.total_orders, result.finished.count) == (False, None, None)
    assert result.total_orders_at_least == CUSTOMER_ORDER_HISTORY_LIMIT


def test_a_salla_completed_order_is_ongoing(store) -> None:
    order = store.order("completed")
    result = history(store.context())
    assert refs(result.ongoing) == [order.external_order_number] and result.finished.count == 0


def test_the_lookup_picks_the_newest_order_the_history_calls_ongoing(store) -> None:
    store.order("in_progress")
    fulfilled = store.order("completed")              # Salla: fulfilled, not handed over
    store.order("refunded")                           # finished
    store.order("pending", cart=True)                 # a cart, newest of all
    context = store.context()
    listed = history(context)
    resolved = asyncio.run(tool.resolve_customer_order_impl(context))
    assert resolved.selection_reason == "latest_open_order"
    assert resolved.order.order_reference == fulfilled.external_order_number
    assert listed.ongoing.orders[0].order_reference == fulfilled.external_order_number
    assert resolved.order.status_label == listed.ongoing.orders[0].status_label


def test_a_listed_order_is_not_authorized_for_the_details_read(store) -> None:
    older = store.order("delivered")
    store.order("processing")
    context = store.context()
    assert older.external_order_number in refs(history(context).finished)
    with pytest.raises(TenantIsolationViolation):
        asyncio.run(tool.get_order_details_impl(context, order_id=older.id))


def test_a_failed_history_read_leaves_the_transaction_usable(store, monkeypatch) -> None:
    """A statement that fails inside PostgreSQL aborts the transaction it runs
    in; without the read's savepoint every later statement in the turn would be
    refused. The history is reported unavailable and the next read works."""
    open_order = store.order("processing")
    context = store.context()

    def broken(db: Any, **kwargs: Any) -> Any:
        db.execute(text("SELECT 1 / 0"))

    monkeypatch.setattr(tool, "read_customer_order_history", broken)
    assert history(context).status == "unavailable"
    assert store.session.execute(text("SELECT 1")).scalar() == 1
    resolved = asyncio.run(tool.resolve_customer_order_impl(context))
    assert resolved.order.order_reference == open_order.external_order_number

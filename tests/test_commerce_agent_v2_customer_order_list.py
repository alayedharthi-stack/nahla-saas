"""The customer's orders listed beside the resolved one (Tenant 1, turn 112).

A customer asked «وش طلباتي السابقة». The order lookup resolved one order and
handed the model nothing else, although the resolver had read twenty of that
customer's orders; the reply said the customer had one order. These tests drive
the real resolver and the real ``resolve_customer_order_impl`` against a
database and assert what the lookup now carries: the resolved order unchanged,
the customer's other orders split into current and previous, bounded, counted,
and marked complete only when the read held every order. What is proved is the
data, never a sentence. Generic store data only.
"""
from __future__ import annotations

import asyncio
from typing import Any

import pytest
from sqlalchemy import JSON, create_engine
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import sessionmaker

from core.local_order_resolver import CUSTOMER_ORDER_READ_LIMIT
from models import Base, Conversation, Customer, Order, Tenant, WhatsAppConnection
from modules.ai.commerce_agent_v2.context import CommerceAgentContext
from modules.ai.commerce_agent_v2.tools import orders as orders_module
from modules.ai.commerce_agent_v2.tools.orders import (
    MAX_LISTED_CURRENT_ORDERS,
    MAX_LISTED_PREVIOUS_ORDERS,
    get_order_details_impl,
    resolve_customer_order_impl,
)
from modules.ai.security.tenant_isolation import TenantIsolationViolation

PHONE = "+966500000001"
OTHER_PHONE = "+966500000002"


def _db() -> Any:
    engine = create_engine("sqlite:///:memory:")
    saved = []
    for table in Base.metadata.sorted_tables:
        for column in table.columns:
            if isinstance(column.type, JSONB):
                saved.append((column, column.type))
                column.type = JSON()
    Base.metadata.create_all(engine)
    for column, original in saved:
        column.type = original
    return sessionmaker(bind=engine)()


class Store:
    def __init__(self) -> None:
        self.db = _db()
        self.tenant = Tenant(name="متجر تجريبي عام", is_active=True)
        self.other_tenant = Tenant(name="متجر تجريبي آخر", is_active=True)
        self.db.add_all([self.tenant, self.other_tenant])
        self.db.flush()
        self.customer = Customer(tenant_id=self.tenant.id, name="أحمد سالم", phone="0500000001",
                                 normalized_phone=PHONE)
        self.neighbour = Customer(tenant_id=self.tenant.id, name="نورة عبدالله", phone="0500000002",
                                  normalized_phone=OTHER_PHONE)
        self.db.add_all([self.customer, self.neighbour])
        self.db.flush()
        self.conversation = Conversation(tenant_id=self.tenant.id, customer_id=self.customer.id,
                                         status="active")
        self.connection = WhatsAppConnection(tenant_id=self.tenant.id, status="connected")
        self.db.add_all([self.conversation, self.connection])
        self.db.flush()
        self._n = 0

    def order(self, status: str, *, linked: bool = True, phone: str = PHONE, customer_id: Any = "self",
              tenant_id: Any = None) -> Order:
        """An order of this customer's, by customer id (``linked``) or by phone only."""
        self._n += 1
        if customer_id == "self":
            customer_id = self.customer.id if linked else None
        row = Order(tenant_id=tenant_id or self.tenant.id, customer_id=customer_id,
                    external_id=f"platform-order-{self._n}", external_order_number=f"GEN-{1000 + self._n}",
                    status=status, total="99", source="salla", customer_name="أحمد سالم",
                    customer_info={"phone": phone},
                    line_items=[{"name": "قميص قطني أزرق", "quantity": 1}])
        self.db.add(row)
        self.db.flush()
        return row

    def context(self) -> CommerceAgentContext:
        self.db.commit()
        return CommerceAgentContext.from_trusted_scope(
            db=self.db, tenant_id=self.tenant.id, conversation_id=self.conversation.id,
            customer_id=self.customer.id, normalized_customer_phone=PHONE,
            connection_id=str(self.connection.id), inbound_trace_id="order-list-test")


def resolve(context: CommerceAgentContext, **kwargs: Any) -> Any:
    return asyncio.run(resolve_customer_order_impl(context, **kwargs))


def refs(entries: Any) -> list[str]:
    return [e.order_reference for e in entries]


# ── The observed shape: many orders, linked by phone only ────────────────────


def test_the_observed_customer_gets_every_order_counted_and_a_bounded_list():
    """Tenant 1 turn 112: twenty orders linked by phone only (no customer id),
    nine still open and eleven closed. The lookup resolved one and said nothing
    else; now it counts both kinds and lists the newest of each."""
    store = Store()
    closed = [store.order(s, linked=False) for s in
              ("cancelled", "cancelled", "abandoned", "delivered", "cancelled", "canceled",
               "cancelled", "cancelled", "cancelled", "cancelled", "cancelled")]
    open_ = [store.order("in_progress", linked=False) for _ in range(9)]
    result = resolve(store.context())

    assert result.status == "ok"
    assert result.order.order_reference == open_[-1].external_order_number     # unchanged selection
    assert result.selection_reason == "latest_open_order"
    listing = result.customer_orders
    assert listing.status == "ok"
    assert (listing.current_count, listing.previous_count) == (9, 11)
    assert listing.counts_complete is True
    assert refs(listing.current) == [o.external_order_number for o in reversed(open_)][:MAX_LISTED_CURRENT_ORDERS]
    assert refs(listing.previous) == [o.external_order_number for o in reversed(closed)][:MAX_LISTED_PREVIOUS_ORDERS]
    assert all(e.status_label for e in [*listing.current, *listing.previous])


def test_every_listed_order_carries_evidence_the_reply_can_cite():
    store = Store()
    store.order("delivered")
    store.order("processing")
    context = store.context()
    result = resolve(context)
    listed = [*result.customer_orders.current, *result.customer_orders.previous]
    assert {e.evidence_ref for e in listed} <= set(context.evidence)
    assert {e.evidence_ref for e in listed} <= {r.ref for r in result.evidence}
    assert len({r.ref for r in result.evidence}) == len(result.evidence)    # no duplicate record


# ── One order ────────────────────────────────────────────────────────────────


def test_a_customer_with_one_order_is_listed_as_exactly_one():
    store = Store()
    only = store.order("processing")
    listing = resolve(store.context()).customer_orders
    assert (listing.current_count, listing.previous_count, listing.counts_complete) == (1, 0, True)
    assert refs(listing.current) == [only.external_order_number] and listing.previous == []


def test_a_customer_whose_only_order_is_closed_has_no_current_order():
    store = Store()
    done = store.order("delivered")
    result = resolve(store.context())
    assert result.order.order_reference == done.external_order_number
    assert (result.customer_orders.current_count, result.customer_orders.previous_count) == (0, 1)


def test_a_customer_with_no_orders_still_gets_no_orders_and_no_list():
    store = Store()
    result = resolve(store.context())
    assert result.status == "not_found"
    assert result.failure_reason == "no_orders_in_trusted_customer_record"
    assert result.customer_orders is None


# ── Bounds and completeness ──────────────────────────────────────────────────


def test_the_lists_are_bounded_but_the_counts_are_not():
    store = Store()
    for _ in range(8):
        store.order("cancelled")
    for _ in range(7):
        store.order("processing")
    listing = resolve(store.context()).customer_orders
    assert len(listing.current) == MAX_LISTED_CURRENT_ORDERS
    assert len(listing.previous) == MAX_LISTED_PREVIOUS_ORDERS
    assert (listing.current_count, listing.previous_count, listing.counts_complete) == (7, 8, True)


def test_a_read_that_reached_its_limit_never_claims_complete_counts():
    """The customer may have older orders than one read holds; the counts are
    then only what was read and say so."""
    store = Store()
    for _ in range(CUSTOMER_ORDER_READ_LIMIT + 3):
        store.order("cancelled")
    store.order("processing")
    listing = resolve(store.context()).customer_orders
    assert listing.status == "ok"
    assert listing.counts_complete is False
    assert listing.current_count + listing.previous_count <= CUSTOMER_ORDER_READ_LIMIT


# ── A failed read ────────────────────────────────────────────────────────────


def test_a_list_that_cannot_be_read_is_reported_unavailable_and_the_order_still_stands(monkeypatch):
    store = Store()
    store.order("delivered")
    open_order = store.order("processing")

    def boom(status: str) -> bool:
        raise RuntimeError("list read failed")

    monkeypatch.setattr(orders_module, "_is_open_status", boom)
    result = resolve(store.context())
    assert result.status == "ok"
    assert result.order.order_reference == open_order.external_order_number
    assert result.customer_orders.status == "unavailable"
    assert result.customer_orders.current == [] and result.customer_orders.previous == []
    assert result.customer_orders.current_count is None
    assert result.customer_orders.counts_complete is False


def test_an_order_read_that_fails_outright_is_still_an_error_not_an_empty_list(monkeypatch):
    store = Store()
    store.order("processing")

    def broken(*args: Any, **kwargs: Any) -> Any:
        raise RuntimeError("database unavailable")

    monkeypatch.setattr(orders_module, "resolve_customer_order_context", broken)
    with pytest.raises(RuntimeError):
        resolve(store.context())


# ── Orders that are not this customer's ──────────────────────────────────────


def test_orders_of_another_customer_or_another_store_are_neither_listed_nor_counted():
    """The resolver's read matches by customer id OR phone, so it can hold an
    order that carries this customer's phone but is linked to another customer.
    The list holds every order to the resolved order's own scope check. (When
    such an order is the one selected, the lookup already fails closed; that is
    covered in test_commerce_agent_v2_phase2 and unchanged here.)"""
    store = Store()
    # Same store, carries this customer's phone, but linked to another customer.
    store.order("processing", customer_id=store.neighbour.id)
    # Same store, another customer's phone, no customer link.
    store.order("delivered", linked=False, phone=OTHER_PHONE)
    # Another store, this customer's phone.
    store.order("processing", linked=False, tenant_id=store.other_tenant.id)
    mine = store.order("processing")
    listing = resolve(store.context()).customer_orders
    assert (listing.current_count, listing.previous_count) == (1, 0)
    assert refs(listing.current) == [mine.external_order_number]


def test_listing_an_order_does_not_authorize_reading_it():
    """The details and shipment reads stay limited to the order resolved in this
    run; a listed order is read by resolving it by its reference."""
    store = Store()
    older = store.order("delivered")
    store.order("processing")
    context = store.context()
    result = resolve(context)
    assert older.external_order_number in refs(result.customer_orders.previous)
    assert older.id not in context.authorized_order_ids
    with pytest.raises(TenantIsolationViolation):
        asyncio.run(get_order_details_impl(context, order_id=older.id))

    by_reference = resolve(context, order_number=older.external_order_number)
    assert by_reference.order.order_reference == older.external_order_number
    assert older.id in context.authorized_order_ids


def test_listed_entries_carry_no_internal_order_id():
    store = Store()
    store.order("delivered")
    store.order("processing")
    listing = resolve(store.context()).customer_orders
    for entry in [*listing.current, *listing.previous]:
        assert set(entry.model_dump()) == {"order_reference", "status", "status_label", "evidence_ref"}

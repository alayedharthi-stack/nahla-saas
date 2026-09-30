"""The customer's orders listed beside the resolved one (Tenant 1, turn 112).

A customer asked «وش طلباتي السابقة». The order lookup resolved one order and
handed the model nothing else, although the resolver had read twenty of that
customer's orders; the reply said the customer had one order. These tests drive
the real resolver and the real ``resolve_customer_order_impl`` against a
database and assert what the lookup now carries: the resolved order unchanged,
the customer's orders grouped by what their status says (in progress, finished,
or a status the platform does not know), bounded, and counted only when the
read held every order. What is proved is the data, never a sentence. Generic
store data only.
"""
from __future__ import annotations

import asyncio
import dataclasses
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
    MAX_LISTED_OTHER_ORDERS,
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
              tenant_id: Any = None, source: str = "salla", external_id: str = "",
              metadata: Any = None) -> Order:
        """An order of this customer's, by customer id (``linked``) or by phone only."""
        self._n += 1
        if customer_id == "self":
            customer_id = self.customer.id if linked else None
        row = Order(tenant_id=tenant_id or self.tenant.id, customer_id=customer_id,
                    external_id=external_id or f"platform-order-{self._n}",
                    external_order_number=f"GEN-{1000 + self._n}",
                    status=status, total="99", source=source, customer_name="أحمد سالم",
                    customer_info={"phone": phone} if phone else {},
                    line_items=[{"name": "قميص قطني أزرق", "quantity": 1}], extra_metadata=metadata)
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


def test_a_read_that_reached_its_limit_gives_no_counts():
    """The customer may have older orders than one read holds; the listed orders
    stand, and no count is given that could be taken for the total."""
    store = Store()
    for _ in range(CUSTOMER_ORDER_READ_LIMIT + 3):
        store.order("cancelled")
    store.order("processing")
    listing = resolve(store.context()).customer_orders
    assert listing.status == "ok"
    assert listing.counts_complete is False
    assert (listing.current_count, listing.previous_count, listing.other_count) == (None, None, None)
    assert len(listing.current) == 1 and len(listing.previous) == MAX_LISTED_PREVIOUS_ORDERS


@pytest.mark.parametrize("orders,complete", [(CUSTOMER_ORDER_READ_LIMIT - 1, True),
                                             (CUSTOMER_ORDER_READ_LIMIT, False)])
def test_completeness_at_the_read_limit_is_conservative(orders: int, complete: bool):
    """A read that came back full cannot tell whether an older order exists."""
    store = Store()
    for _ in range(orders):
        store.order("delivered")
    listing = resolve(store.context()).customer_orders
    assert listing.counts_complete is complete
    assert listing.previous_count == (orders if complete else None)


def test_an_order_gone_between_the_two_reads_withholds_the_counts(monkeypatch):
    store = Store()
    older = store.order("delivered")
    store.order("processing")
    real = orders_module.resolve_customer_order_context

    def then_deleted(*args: Any, **kwargs: Any) -> Any:
        resolved = real(*args, **kwargs)
        store.db.delete(older)
        store.db.flush()
        return resolved

    monkeypatch.setattr(orders_module, "resolve_customer_order_context", then_deleted)
    listing = resolve(store.context()).customer_orders
    assert listing.status == "ok" and listing.counts_complete is False
    assert listing.previous == [] and listing.current_count is None


# ── A failed read ────────────────────────────────────────────────────────────


def test_a_list_that_cannot_be_read_is_reported_unavailable_and_the_order_still_stands(monkeypatch):
    store = Store()
    store.order("delivered")
    open_order = store.order("processing")

    def boom(*args: Any) -> str:
        raise RuntimeError("list read failed")

    monkeypatch.setattr(orders_module, "_order_list_group", boom)
    result = resolve(store.context())
    assert result.status == "ok"
    assert result.order.order_reference == open_order.external_order_number
    assert result.customer_orders.status == "unavailable"
    assert result.customer_orders.current == [] and result.customer_orders.previous == []
    assert result.customer_orders.current_count is None
    assert result.customer_orders.counts_complete is False


def test_a_listed_order_whose_evidence_changed_costs_the_list_not_the_order():
    """Evidence is registered once per run; a listed order that changed since it
    was listed collides with its earlier record. The list is then reported
    unavailable and the resolved order still stands."""
    store = Store()
    older = store.order("processing")
    newest = store.order("processing")
    context = store.context()
    assert older.external_order_number in refs(resolve(context).customer_orders.current)
    older.status = "delivered"
    store.db.commit()
    again = resolve(context)
    assert again.status == "ok"
    assert again.order.order_reference == newest.external_order_number
    assert again.customer_orders.status == "unavailable"


def test_an_unavailable_list_leaves_none_of_its_evidence_registered():
    """A collision on a previous order, found after a current one was built,
    registers nothing from the list: no record the model was never shown can be
    cited."""
    store = Store()
    older = store.order("processing")
    done = store.order("delivered")
    store.order("processing")
    context = store.context()
    resolve(context)
    done.status = "cancelled"           # its summary evidence now differs
    unseen = store.order("processing")  # a current order not yet registered
    store.db.commit()
    before = set(context.evidence)
    again = resolve(context, order_number=older.external_order_number)
    assert again.order.order_reference == older.external_order_number
    assert again.customer_orders.status == "unavailable"
    assert f"order:summary:{unseen.id}" not in context.evidence
    assert set(context.evidence) == before


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


def _with_extra_candidate(monkeypatch: Any, order: Order) -> None:
    """The resolver's read, with one more order handed to the list than it matched."""
    real = orders_module.resolve_customer_order_context

    def widened(*args: Any, **kwargs: Any) -> Any:
        resolved = real(*args, **kwargs)
        extra = dataclasses.replace(resolved.orders_by_priority[0], order_id=order.id)
        return dataclasses.replace(resolved, orders_by_priority=[*resolved.orders_by_priority, extra])

    monkeypatch.setattr(orders_module, "resolve_customer_order_context", widened)


def test_the_list_filters_by_store_itself_whatever_the_read_handed_it(monkeypatch):
    store = Store()
    foreign = store.order("processing", linked=False, tenant_id=store.other_tenant.id)
    mine = store.order("processing")
    _with_extra_candidate(monkeypatch, foreign)
    listing = resolve(store.context()).customer_orders
    assert refs(listing.current) == [mine.external_order_number]
    assert listing.counts_complete is False and listing.current_count is None


def test_an_order_whose_owner_cannot_be_proven_is_left_out_and_withholds_the_counts(monkeypatch):
    """Not linked to another customer, not carrying this customer's phone: it may
    be the customer's, so it is not listed and no count is given as a total."""
    store = Store()
    unproven = store.order("processing", linked=False, phone=OTHER_PHONE)
    mine = store.order("processing")
    _with_extra_candidate(monkeypatch, unproven)
    listing = resolve(store.context()).customer_orders
    assert refs(listing.current) == [mine.external_order_number]
    assert listing.counts_complete is False and listing.current_count is None


def test_an_order_linked_to_another_customer_does_not_withhold_the_counts(monkeypatch):
    store = Store()
    theirs = store.order("processing", customer_id=store.neighbour.id, phone=OTHER_PHONE)
    mine = store.order("processing")
    _with_extra_candidate(monkeypatch, theirs)
    listing = resolve(store.context()).customer_orders
    assert refs(listing.current) == [mine.external_order_number]
    assert (listing.current_count, listing.counts_complete) == (1, True)


def test_this_conversations_draft_is_listed_as_current():
    from services.nahla_order_bridge import nahla_wa_external_id

    store = Store()
    delivered = store.order("delivered")
    context = store.context()
    draft = store.order("draft", linked=False, phone="", source="whatsapp",
                        external_id=nahla_wa_external_id(store.tenant.id, store.conversation.id),
                        metadata={"lifecycle": "whatsapp_draft"})
    store.db.commit()
    result = resolve(context)
    assert result.order.order_reference == draft.external_order_number
    listing = result.customer_orders
    assert refs(listing.current) == [draft.external_order_number]
    assert refs(listing.previous) == [delivered.external_order_number]
    assert (listing.current_count, listing.previous_count, listing.counts_complete) == (1, 1, True)


# ── What a status says ───────────────────────────────────────────────────────


def test_finished_orders_are_previous_and_unknown_statuses_are_not_claimed_either_way():
    """Refunded, returned and failed orders are finished, not current. A status
    the platform does not know is listed apart: neither in progress nor done."""
    store = Store()
    finished = [store.order(s) for s in ("refunded", "returned", "failed", "delivered", "cancelled")]
    unknown = [store.order(s) for s in ("merchant_custom_stage", "")]
    in_progress = [store.order(s) for s in ("pending_payment", "under_review", "shipped", "processing")]
    listing = resolve(store.context()).customer_orders
    assert (listing.current_count, listing.previous_count, listing.other_count) == (4, 5, 2)
    assert set(refs(listing.current)) == {o.external_order_number for o in in_progress}
    assert set(refs(listing.previous)) == {o.external_order_number for o in finished}
    assert set(refs(listing.other)) == {o.external_order_number for o in unknown}
    assert len(listing.other) <= MAX_LISTED_OTHER_ORDERS


@pytest.mark.parametrize("source,group", [("salla", "current"), ("manual", "previous")])
def test_a_status_word_is_read_as_its_own_store_means_it(source: str, group: str):
    """On Salla, ``completed`` is the merchant's «تنفيذ»: fulfilled, not yet
    shipped (store_adapters/salla_lifecycle). The list reads each status through
    its store's adapter, so that order is still current there; a store without
    such a meaning keeps the word's plain reading."""
    store = Store()
    order = store.order("completed", source=source)
    listing = resolve(store.context()).customer_orders
    assert refs(getattr(listing, group)) == [order.external_order_number]


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
    for entry in [*listing.current, *listing.previous, *listing.other]:
        assert set(entry.model_dump()) == {"order_reference", "status", "status_label", "evidence_ref"}

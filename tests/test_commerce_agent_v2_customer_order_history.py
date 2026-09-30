"""The customer's order history, read on request (Tenant 1, runtime turn 112).

A customer asked «وش طلباتي السابقة». The runtime had read twenty of that
customer's orders, but the one tool it had resolves one order, so the model saw
one and said the customer had one order. These tests drive the real history
read (``list_customer_orders_impl``) and the unchanged one-order lookup against
a database and assert the data each carries:

* the one-order lookup still returns one order and nothing about a history;
* the history counts every order, groups it by where it stands — read through
  the order's own store's lifecycle adapter — and lists a few per group, saying
  how many it lists;
* a total only when the read held every order and each was proven the
  customer's; a failed read is unavailable, never "no orders";
* nothing it lists is authorized, and nothing it shows is an internal number.

What is proved is the data, never a sentence. Generic store data only.
"""
from __future__ import annotations

import asyncio
import re
from typing import Any

import pytest
from sqlalchemy import JSON, create_engine, text
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import sessionmaker

from core.local_order_resolver import CUSTOMER_ORDER_HISTORY_LIMIT
from core.order_status_label import ORDER_STATUS_LABELS_AR
from models import Base, Conversation, Customer, Order, Tenant, WhatsAppConnection
from modules.ai.commerce_agent_v2.context import CommerceAgentContext
from modules.ai.commerce_agent_v2.output import OrderResolveResult
from modules.ai.commerce_agent_v2.tools import orders as orders_module
from modules.ai.commerce_agent_v2.tools.orders import (
    MAX_LISTED_FINISHED_ORDERS,
    MAX_LISTED_ONGOING_ORDERS,
    MAX_LISTED_UNKNOWN_ORDERS,
    get_order_details_impl,
    list_customer_orders_impl,
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

    def order(self, status: str, *, linked: bool = False, phone: str = PHONE, customer_id: Any = "self",
              tenant_id: Any = None, source: str = "salla", number: Any = "auto") -> Order:
        """An order of this customer's: by phone only (as observed), or by customer id."""
        self._n += 1
        if customer_id == "self":
            customer_id = self.customer.id if linked else None
        row = Order(tenant_id=tenant_id or self.tenant.id, customer_id=customer_id,
                    external_id=f"platform-{700000 + self._n}",
                    external_order_number=(f"GEN-{1000 + self._n}" if number == "auto" else number),
                    status=status, total="99", source=source, customer_name="أحمد سالم",
                    customer_info={"phone": phone} if phone else {},
                    line_items=[{"name": "قميص قطني أزرق", "quantity": 1}])
        self.db.add(row)
        self.db.flush()
        return row

    def context(self) -> CommerceAgentContext:
        self.db.commit()
        return CommerceAgentContext.from_trusted_scope(
            db=self.db, tenant_id=self.tenant.id, conversation_id=self.conversation.id,
            customer_id=self.customer.id, normalized_customer_phone=PHONE,
            connection_id=str(self.connection.id), inbound_trace_id="order-history-test")


def history(context: CommerceAgentContext) -> Any:
    return asyncio.run(list_customer_orders_impl(context))


def resolve(context: CommerceAgentContext, **kwargs: Any) -> Any:
    return asyncio.run(resolve_customer_order_impl(context, **kwargs))


def refs(group: Any) -> list:
    return [entry.order_reference for entry in group.orders]


# ── The observed shape ───────────────────────────────────────────────────────


def test_the_observed_customer_gets_every_order_counted_and_a_few_listed():
    """Tenant 1 turn 112: twenty orders matched by phone only, nine in
    progress and eleven closed. The history counts them all."""
    store = Store()
    closed = [store.order(s) for s in ("cancelled", "cancelled", "abandoned", "delivered", "cancelled",
                                       "canceled", "cancelled", "cancelled", "cancelled", "cancelled",
                                       "cancelled")]
    open_ = [store.order("in_progress") for _ in range(9)]
    result = history(store.context())
    assert result.status == "ok" and result.read_complete is True and result.incomplete_reasons == []
    assert result.total_orders == 20
    assert (result.ongoing.count, result.ongoing.listed) == (9, MAX_LISTED_ONGOING_ORDERS)
    assert (result.finished.count, result.finished.listed) == (11, MAX_LISTED_FINISHED_ORDERS)
    assert refs(result.ongoing) == [o.external_order_number for o in reversed(open_)][:MAX_LISTED_ONGOING_ORDERS]
    assert refs(result.finished) == [o.external_order_number for o in reversed(closed)][:MAX_LISTED_FINISHED_ORDERS]
    assert sum(result.finished.by_status.values()) == 11          # the unlisted ones are counted too
    assert sum(result.ongoing.by_status.values()) == 9


def test_the_one_order_lookup_still_returns_one_order_and_no_history():
    """«وين طلبي؟» stays on the order it resolves: the lookup carries no history."""
    store = Store()
    store.order("delivered")
    newest_open = store.order("in_progress")
    result = resolve(store.context())
    assert result.order.order_reference == newest_open.external_order_number
    assert result.selection_reason == "latest_open_order"
    assert result.order.stage == "ongoing"
    assert set(OrderResolveResult.model_fields) == {"status", "order", "selection_reason", "evidence",
                                                    "failure_reason"}


# ── One, finished only, none ─────────────────────────────────────────────────


def test_a_customer_with_one_order_has_a_history_of_exactly_one():
    store = Store()
    only = store.order("processing")
    result = history(store.context())
    assert (result.total_orders, result.ongoing.count, result.finished.count, result.unknown.count) == (1, 1, 0, 0)
    assert refs(result.ongoing) == [only.external_order_number]


def test_a_customer_whose_orders_are_all_finished_has_nothing_ongoing():
    store = Store()
    done = [store.order(s) for s in ("delivered", "cancelled", "delivered")]
    result = history(store.context())
    assert (result.total_orders, result.ongoing.count, result.finished.count) == (3, 0, 3)
    assert refs(result.finished) == [o.external_order_number for o in reversed(done)]


def test_no_orders_is_said_only_by_a_complete_read():
    store = Store()
    result = history(store.context())
    assert (result.status, result.read_complete, result.total_orders) == ("ok", True, 0)
    assert result.ongoing.orders == result.finished.orders == result.unknown.orders == []


# ── Bounds and completeness ──────────────────────────────────────────────────


def test_a_read_at_its_limit_gives_no_total_and_no_counts():
    store = Store()
    for _ in range(CUSTOMER_ORDER_HISTORY_LIMIT + 3):
        store.order("cancelled")
    store.order("processing")
    result = history(store.context())
    assert result.status == "ok" and result.read_complete is False
    assert result.incomplete_reasons == ["read_limit_reached"]
    assert result.total_orders is None
    for group in (result.ongoing, result.finished, result.unknown):
        assert group.count is None and group.by_status is None
    assert result.ongoing.listed == 1 and result.finished.listed == MAX_LISTED_FINISHED_ORDERS


@pytest.mark.parametrize("orders,complete", [(CUSTOMER_ORDER_HISTORY_LIMIT - 1, True),
                                             (CUSTOMER_ORDER_HISTORY_LIMIT, False)])
def test_completeness_at_the_limit_is_conservative(orders: int, complete: bool):
    """A read that came back full cannot tell whether an older order exists."""
    store = Store()
    for _ in range(orders):
        store.order("delivered")
    result = history(store.context())
    assert result.read_complete is complete
    assert result.total_orders == (orders if complete else None)


# ── A failed read ────────────────────────────────────────────────────────────


def test_a_failed_read_is_unavailable_registers_nothing_and_leaves_the_session_usable(monkeypatch):
    store = Store()
    store.order("delivered")
    open_order = store.order("processing")
    context = store.context()
    before = dict(context.evidence)

    def broken(db: Any, **kwargs: Any) -> Any:
        db.execute(text("SELECT * FROM no_such_table"))

    monkeypatch.setattr(orders_module, "read_customer_order_history", broken)
    result = history(context)
    assert result.status == "unavailable" and result.read_complete is False
    assert result.total_orders is None and result.evidence == []
    assert context.evidence == before
    after = resolve(context)                      # the next tool still reads
    assert after.order.order_reference == open_order.external_order_number


def test_an_unavailable_history_leaves_none_of_its_evidence_registered():
    """Evidence is registered all or nothing: a listed order that changed since
    it was first listed collides, and nothing of the second read is kept."""
    store = Store()
    first = store.order("processing")
    store.order("delivered")
    context = store.context()
    assert history(context).status == "ok"
    registered = dict(context.evidence)
    first.status = "cancelled"
    store.order("processing")                     # a new order, never registered
    store.db.commit()
    again = history(context)
    assert again.status == "unavailable"
    assert context.evidence == registered


# ── Where an order stands ────────────────────────────────────────────────────


def test_a_status_the_platform_cannot_read_stays_unknown():
    store = Store()
    unknown = [store.order(s) for s in ("merchant_custom_stage", "")]
    finished = [store.order(s) for s in ("refunded", "returned", "failed")]
    ongoing = [store.order(s) for s in ("pending_payment", "under_review", "shipped")]
    result = history(store.context())
    assert (result.ongoing.count, result.finished.count, result.unknown.count) == (3, 3, 2)
    assert set(refs(result.unknown)) == {o.external_order_number for o in unknown}
    assert set(refs(result.finished)) == {o.external_order_number for o in finished}
    assert set(refs(result.ongoing)) == {o.external_order_number for o in ongoing}
    assert result.unknown.listed <= MAX_LISTED_UNKNOWN_ORDERS


def test_a_salla_completed_order_is_fulfilled_not_finished_in_both_reads():
    """On Salla ``completed`` is the merchant's «تنفيذ»: fulfilled, not yet
    shipped (store_adapters/salla_lifecycle). It is ongoing, labelled as the
    fulfilled order it is — never with the plain label of a finished order —
    and the one-order lookup and the history say the same."""
    store = Store()
    order = store.order("completed")
    context = store.context()
    listed = history(context)
    assert refs(listed.ongoing) == [order.external_order_number] and listed.finished.count == 0
    entry = listed.ongoing.orders[0]
    assert entry.status_label == ORDER_STATUS_LABELS_AR["fulfilled"]
    assert entry.status_label != ORDER_STATUS_LABELS_AR["completed"]
    resolved = resolve(context)
    assert (resolved.order.stage, resolved.order.status_label) == ("ongoing", entry.status_label)


def test_a_completed_order_from_a_store_without_that_meaning_is_finished():
    store = Store()
    order = store.order("completed", source="manual")
    result = history(store.context())
    assert refs(result.finished) == [order.external_order_number]
    assert result.finished.orders[0].status_label == ORDER_STATUS_LABELS_AR["completed"]


def test_shipment_and_delivery_stages_follow_the_store():
    store = Store()
    shipped = store.order("shipped")
    delivered = store.order("delivered")
    result = history(store.context())
    assert refs(result.ongoing) == [shipped.external_order_number]
    assert refs(result.finished) == [delivered.external_order_number]


# ── Orders that are not this customer's ──────────────────────────────────────


def test_orders_of_another_store_customer_or_phone_are_neither_listed_nor_counted():
    store = Store()
    store.order("processing", customer_id=store.neighbour.id)          # this phone, another customer
    store.order("processing", tenant_id=store.other_tenant.id)         # this phone, another store
    store.order("delivered", phone=OTHER_PHONE)                        # another phone
    mine = store.order("processing")
    result = history(store.context())
    assert (result.total_orders, result.read_complete) == (1, True)
    assert refs(result.ongoing) == [mine.external_order_number]


def _with_extra_row(monkeypatch: Any, extra: Order) -> None:
    real = orders_module.read_customer_order_history

    def widened(db: Any, **kwargs: Any) -> Any:
        rows, complete = real(db, **kwargs)
        return [*rows, extra], complete

    monkeypatch.setattr(orders_module, "read_customer_order_history", widened)


def test_an_order_whose_owner_cannot_be_proven_is_left_out_and_withholds_the_total(monkeypatch):
    store = Store()
    unproven = store.order("processing", phone=OTHER_PHONE)
    mine = store.order("processing")
    _with_extra_row(monkeypatch, unproven)
    result = history(store.context())
    assert refs(result.ongoing) == [mine.external_order_number]
    assert result.read_complete is False and result.incomplete_reasons == ["order_scope_unverified"]
    assert result.total_orders is None and result.ongoing.count is None


def test_an_order_linked_to_another_customer_is_left_out_without_withholding_the_total(monkeypatch):
    store = Store()
    theirs = store.order("processing", customer_id=store.neighbour.id, phone=OTHER_PHONE)
    mine = store.order("processing")
    _with_extra_row(monkeypatch, theirs)
    result = history(store.context())
    assert refs(result.ongoing) == [mine.external_order_number]
    assert (result.read_complete, result.total_orders) == (True, 1)


def test_the_history_is_filtered_by_store_before_anything_is_listed(monkeypatch):
    store = Store()
    foreign = store.order("processing", tenant_id=store.other_tenant.id)
    store.order("processing")
    _with_extra_row(monkeypatch, foreign)
    result = history(store.context())
    assert foreign.external_order_number not in refs(result.ongoing)
    assert result.read_complete is False


# ── Authorization and internal numbers ───────────────────────────────────────


def test_listing_an_order_does_not_authorize_reading_it():
    store = Store()
    older = store.order("delivered")
    store.order("processing")
    context = store.context()
    history(context)
    assert older.id not in context.authorized_order_ids
    with pytest.raises(TenantIsolationViolation):
        asyncio.run(get_order_details_impl(context, order_id=older.id))


def test_nothing_listed_is_an_internal_number():
    """No order id in an entry, an opaque evidence reference, and no reference
    at all for an order the store gave no customer-facing number — its internal
    identifiers are never offered in its place, by either read."""
    store = Store()
    numbered = store.order("delivered")
    unnumbered = store.order("processing", number=None)
    context = store.context()
    result = history(context)
    for entry in [*result.ongoing.orders, *result.finished.orders]:
        assert set(entry.model_dump()) == {"order_reference", "status_label", "evidence_ref"}
        assert re.fullmatch(r"order:history:h[0-9a-f]{12}", entry.evidence_ref)
        for internal in (str(numbered.id), str(unnumbered.id), unnumbered.external_id):
            assert internal not in entry.evidence_ref.split(":")
    assert refs(result.ongoing) == [None] and refs(result.finished) == [numbered.external_order_number]
    assert resolve(context).order.order_reference is None

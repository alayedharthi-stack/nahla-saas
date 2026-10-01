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
from core.local_order_resolver import resolve_customer_order_context
from core.order_status_label import LIFECYCLE_STATE_LABELS_AR, ORDER_STATUS_LABELS_AR, order_status_label_ar
from models import Base, Conversation, Customer, Order, Tenant, WhatsAppConnection
from modules.ai.commerce_agent_v2.context import CommerceAgentContext
from modules.ai.commerce_agent_v2.output import OrderResolveResult
from modules.ai.commerce_agent_v2.tools import orders as orders_module
from modules.ai.commerce_agent_v2.tools.orders import (
    MAX_LISTED_FINISHED_ORDERS,
    MAX_LISTED_ONGOING_ORDERS,
    MAX_LISTED_UNKNOWN_ORDERS,
    get_order_details_impl,
    get_order_shipment_impl,
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
              tenant_id: Any = None, source: str = "salla", number: Any = "auto",
              cart: bool | None = False) -> Order:
        """An order of this customer's: by phone only (as observed), or by customer id.
        ``cart`` stores it as store sync stores an abandoned cart (``None``: no flag stored)."""
        self._n += 1
        if customer_id == "self":
            customer_id = self.customer.id if linked else None
        row = Order(tenant_id=tenant_id or self.tenant.id, customer_id=customer_id,
                    external_id=(f"cart-{900000 + self._n}" if cart else f"platform-{700000 + self._n}"),
                    is_abandoned=cart,
                    external_order_number=(f"GEN-{1000 + self._n}" if number == "auto" else number),
                    status=status, total="99", source=source, customer_name="أحمد سالم",
                    customer_info={"phone": phone} if phone else {},
                    line_items=[{"name": "قميص قطني أزرق", "quantity": 1}])
        self.db.add(row)
        self.db.flush()
        if cart is None:                  # the column's default fills a None on insert
            self.db.query(Order).filter(Order.id == row.id).update(
                {Order.is_abandoned: None}, synchronize_session="fetch")
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
    """Tenant 1 turn 112: twenty rows matched by phone only, nine in progress
    and eleven closed — one of them abandoned, which is how store sync keeps a
    cart. The history counts every order and leaves the cart out."""
    store = Store()
    closed = [store.order(s) for s in ("cancelled", "cancelled", "delivered", "cancelled", "canceled",
                                       "cancelled", "cancelled", "cancelled", "cancelled", "cancelled")]
    cart = store.order("abandoned", cart=True)
    open_ = [store.order("in_progress") for _ in range(9)]
    result = history(store.context())
    assert result.status == "ok" and result.read_complete is True and result.incomplete_reasons == []
    assert result.total_orders == 19 and result.total_orders_at_least is None
    assert result.ongoing.count_at_least is None and result.finished.count_at_least is None
    assert (result.ongoing.count, result.ongoing.listed) == (9, MAX_LISTED_ONGOING_ORDERS)
    assert (result.finished.count, result.finished.listed) == (10, MAX_LISTED_FINISHED_ORDERS)
    assert refs(result.ongoing) == [o.external_order_number for o in reversed(open_)][:MAX_LISTED_ONGOING_ORDERS]
    assert refs(result.finished) == [o.external_order_number for o in reversed(closed)][:MAX_LISTED_FINISHED_ORDERS]
    assert cart.external_order_number not in refs(result.finished)
    assert sum(result.finished.by_status.values()) == 10          # the unlisted ones are counted too
    assert sum(result.ongoing.by_status.values()) == 9


def test_an_abandoned_cart_is_not_an_order():
    """Store sync keeps a cart as an order row (``is_abandoned``, status
    ``abandoned``, a cart id). The platform's customer ledger counts either
    mark as abandoned; the history lists and counts neither."""
    store = Store()
    store.order("abandoned", cart=True)
    store.order("abandoned")                              # the status alone
    flagged = store.order("pending", cart=True)           # the flag alone
    mine = store.order("delivered")
    unflagged = store.order("processing", cart=None)      # no flag stored: an order, not a cart
    result = history(store.context())
    assert (result.total_orders, result.read_complete) == (2, True)
    assert refs(result.finished) == [mine.external_order_number]
    assert refs(result.ongoing) == [unflagged.external_order_number]
    assert flagged.external_order_number not in refs(result.ongoing)


def test_the_one_order_lookup_still_returns_one_order_and_no_history():
    """«وين طلبي؟» stays on the order it resolves: the lookup carries no history."""
    store = Store()
    store.order("delivered")
    newest_open = store.order("in_progress")
    result = resolve(store.context())
    assert result.order.order_reference == newest_open.external_order_number
    assert result.selection_reason == "latest_open_order"
    assert set(OrderResolveResult.model_fields) == {"status", "order", "selection_reason", "evidence",
                                                    "failure_reason"}
    # The order in the fields it always had: where it stands is the history's grouping.
    assert set(result.model_dump()["order"]) == {"order_id", "order_reference", "status", "status_label",
                                                 "evidence_ref"}
    assert set(result.evidence[0].fields) == {"order_id", "order_reference", "status", "status_label"}


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
    assert result.total_orders_at_least == CUSTOMER_ORDER_HISTORY_LIMIT     # a bound, never a total
    for group in (result.ongoing, result.finished, result.unknown):
        assert group.count is None and group.by_status is None
    assert result.ongoing.listed == 1 and result.finished.listed == MAX_LISTED_FINISHED_ORDERS
    # Per stage, a bound of the orders read - never the number listed.
    assert (result.ongoing.count_at_least, result.finished.count_at_least) == (1, CUSTOMER_ORDER_HISTORY_LIMIT - 1)


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


# Labels that claim a handover the status does not prove: shipment, transit,
# delivery, completion — or "prepared for shipping", untrue of a pickup order.
_HANDOVER_LABELS = {ORDER_STATUS_LABELS_AR[k] for k in (
    "fulfilled", "shipped", "in_transit", "on_the_way", "out_for_delivery", "delivering", "delivered",
    "completed")}


def test_a_salla_completed_order_is_ongoing_and_labelled_without_assuming_shipment():
    """On Salla ``completed`` is the merchant's «تنفيذ»: fulfilled and not yet
    handed over (store_adapters/salla_lifecycle), so it is ongoing. No order
    row carries whether it is a shipping or a pickup order, so its label is the
    platform's "ready" state, true of both: never «مكتمل», never delivered,
    never "prepared for shipping". The lookup and the history say the same."""
    store = Store()
    order = store.order("completed")
    context = store.context()
    listed = history(context)
    assert refs(listed.ongoing) == [order.external_order_number] and listed.finished.count == 0
    entry = listed.ongoing.orders[0]
    assert entry.status_label == LIFECYCLE_STATE_LABELS_AR["ready"]
    assert entry.status_label not in _HANDOVER_LABELS
    resolved = resolve(context)
    assert resolved.order.status_label == entry.status_label


@pytest.mark.parametrize("status", ["ready", "packed", "ready_for_pickup", "preparing", "in_review"])
def test_statuses_the_salla_adapter_reads_as_under_way_are_ongoing_and_keep_their_own_label(status: str):
    """The adapter places them; the label stays the word's own reading, which
    claims no more than the word (a pickup order is not prepared for shipping,
    an order in review is not confirmed)."""
    store = Store()
    order = store.order(status)
    result = history(store.context())
    assert refs(result.ongoing) == [order.external_order_number]
    label = result.ongoing.orders[0].status_label
    assert label == order_status_label_ar(status)
    assert label not in _HANDOVER_LABELS | {ORDER_STATUS_LABELS_AR["confirmed"]}


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


# ── The lookup picks by the history's reading ────────────────────────────────


def _lookup_and_history(store: Store) -> tuple:
    context = store.context()
    return resolve(context), history(context)


@pytest.mark.parametrize("older,newer,picked", [
    ("in_progress", "completed", "newer"),        # Salla completed: still under way, and newer
    ("in_progress", "refunded", "older"),         # refunded is finished, never the open order
    ("in_progress", "returned", "older"),
    ("in_progress", "merchant_custom_stage", "older"),   # unknown is never called open
    ("processing", "shipped", "newer"),
])
def test_the_order_the_lookup_calls_open_is_the_newest_the_history_calls_ongoing(older: str, newer: str,
                                                                                   picked: str):
    store = Store()
    first = store.order(older)
    second = store.order(newer)
    lookup, listed = _lookup_and_history(store)
    expected = (second if picked == "newer" else first).external_order_number
    assert lookup.selection_reason == "latest_open_order"
    assert lookup.order.order_reference == expected == listed.ongoing.orders[0].order_reference


def test_with_nothing_ongoing_the_lookup_never_calls_an_order_open():
    """Unknown and finished only: the history has nothing ongoing, and the
    lookup does not name any order the latest open one."""
    store = Store()
    store.order("delivered")
    store.order("merchant_custom_stage")
    lookup, listed = _lookup_and_history(store)
    assert listed.ongoing.count == 0
    assert lookup.status == "ok" and lookup.selection_reason != "latest_open_order"


def test_a_cart_is_never_the_order_the_lookup_picks():
    """A newer cart (flag only, or status only) is not an order to the history
    and is not picked by the lookup."""
    store = Store()
    real = store.order("delivered")
    store.order("pending", cart=True)
    store.order("abandoned")
    lookup, listed = _lookup_and_history(store)
    assert listed.total_orders == 1
    assert lookup.order.order_reference == real.external_order_number


def test_the_lookup_and_the_history_agree_on_every_order_they_both_name():
    store = Store()
    for status in ("delivered", "completed", "refunded", "in_progress", "merchant_custom_stage", "shipped"):
        store.order(status)
    context = store.context()
    listed = history(context)
    labels = {entry.order_reference: entry.status_label
              for group in (listed.ongoing, listed.finished, listed.unknown) for entry in group.orders}
    resolved = resolve(context)
    assert resolved.order.order_reference == listed.ongoing.orders[0].order_reference
    assert resolved.order.status_label == labels[resolved.order.order_reference]


@pytest.mark.parametrize("status", ["cod_pending", "payment_submitted", "ready_to_process", "ready_to_ship",
                                    "pending_customer_info", "pending_confirmation"])
def test_statuses_the_platform_writes_on_its_own_orders_are_ongoing(status: str):
    """Nahla's own order statuses (WhatsApp lifecycle, payment and fulfilment
    policy, COD confirmation) are not unknown to the platform: the history
    groups them as ongoing and the lookup picks the newest of them."""
    store = Store()
    store.order("processing", source="whatsapp")
    own = store.order(status, source="whatsapp")
    lookup, listed = _lookup_and_history(store)
    assert listed.unknown.count == 0 and listed.ongoing.count == 2
    assert lookup.selection_reason == "latest_open_order"
    assert lookup.order.order_reference == own.external_order_number == listed.ongoing.orders[0].order_reference


def test_the_platform_status_list_is_the_cod_flows_own():
    from core.order_lifecycle_reading import PLATFORM_ONGOING_STATUSES
    from services.cod_confirmation import STATUS_PENDING_CUSTOMER, STATUS_PENDING_MERCHANT

    assert {STATUS_PENDING_CUSTOMER, STATUS_PENDING_MERCHANT} <= PLATFORM_ONGOING_STATUSES


def test_another_customers_order_on_the_same_phone_is_never_the_one_picked():
    """A shared household phone: the newest ongoing order is linked to another
    customer. The history leaves it out; the lookup picks this customer's own
    order instead of failing on it."""
    store = Store()
    mine = store.order("processing")
    store.order("in_progress", customer_id=store.neighbour.id)          # this phone, another customer
    lookup, listed = _lookup_and_history(store)
    assert lookup.status == "ok" and lookup.selection_reason == "latest_open_order"
    assert lookup.order.order_reference == mine.external_order_number == listed.ongoing.orders[0].order_reference
    assert listed.total_orders == 1


def test_an_order_number_shared_with_a_newer_cart_finds_the_order():
    store = Store()
    real = store.order("delivered", number="GEN-SAME")
    store.order("pending", cart=True, number="GEN-SAME")
    lookup = resolve(store.context(), order_number="GEN-SAME")
    assert lookup.selection_reason == "explicit_order_number"
    assert lookup.order.order_reference == real.external_order_number
    assert lookup.order.order_id == real.id


def test_a_conversation_draft_kept_as_a_cart_is_not_the_active_draft():
    from services.nahla_order_bridge import nahla_wa_external_id

    store = Store()
    mine = store.order("in_progress")
    store.db.add(Order(tenant_id=store.tenant.id, customer_id=None, source="whatsapp", status="draft",
                       external_id=nahla_wa_external_id(store.tenant.id, store.conversation.id) + "-1",
                       is_abandoned=True, extra_metadata={"lifecycle": "whatsapp_draft"},
                       customer_info={}, line_items=[]))
    store.db.flush()
    lookup = resolve(store.context())
    assert lookup.selection_reason == "latest_open_order"
    assert lookup.order.order_reference == mine.external_order_number


def test_the_resolvers_priority_list_puts_ongoing_orders_first_when_lifecycle_aware():
    store = Store()
    fulfilled = store.order("completed")           # Salla: still under way
    store.order("refunded")                        # newer, finished
    store.db.commit()
    aware = resolve_customer_order_context(store.db, tenant_id=store.tenant.id, customer_id=store.customer.id,
                                           phone=PHONE, lifecycle_aware=True)
    assert aware.orders_by_priority[0].order_id == fulfilled.id


def test_other_readers_of_the_resolver_keep_their_status_list():
    """The lifecycle reading is the agent lookup's: the resolver's other
    callers (default arguments) still read Salla ``completed`` as closed."""
    store = Store()
    store.order("completed")
    store.db.commit()
    default = resolve_customer_order_context(store.db, tenant_id=store.tenant.id, customer_id=store.customer.id,
                                             phone=PHONE)
    aware = resolve_customer_order_context(store.db, tenant_id=store.tenant.id, customer_id=store.customer.id,
                                           phone=PHONE, lifecycle_aware=True)
    assert default.latest_open_order is None
    assert aware.latest_open_order is not None and aware.selected_reason == "latest_open_order"


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
    assert result.total_orders_at_least == 1 and result.ongoing.count_at_least == 1


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
    unnumbered = store.order("shipped", number=None)
    context = store.context()
    result = history(context)
    for entry in [*result.ongoing.orders, *result.finished.orders]:
        assert set(entry.model_dump()) == {"order_reference", "status_label", "evidence_ref"}
        assert re.fullmatch(r"order:history:h[0-9a-f]{12}", entry.evidence_ref)
        for internal in (str(numbered.id), str(unnumbered.id), unnumbered.external_id):
            assert internal not in entry.evidence_ref.split(":")
    assert refs(result.ongoing) == [None] and refs(result.finished) == [numbered.external_order_number]
    resolved = resolve(context)
    assert resolved.order.order_reference is None
    details = asyncio.run(get_order_details_impl(context, order_id=resolved.order.order_id))
    assert details.order.order_reference is None                 # the same in every order tool
    shipment = asyncio.run(get_order_shipment_impl(context, order_id=resolved.order.order_id))
    assert shipment.status == "ok" and shipment.shipment.order_reference is None

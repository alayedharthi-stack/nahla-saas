"""A conversation reads only its own WhatsApp draft, and payment evidence links
only to an order proven to be the customer's.

Every WhatsApp draft of a conversation is written as ``nahla-wa-{tenant}-{conv}``
or that id followed by ``-msg-…``. The draft readers matched the bare prefix,
so conversation 1 also read conversations 10–19's drafts — another customer's
items, name and address in the order context, checkout authority and the
order lookup. The payment linker fell back to any order in the store when the
sender had no identity to match.

These tests drive the real readers against a database and assert which order
each one returns or links, never a sentence. Generic store data only.
"""
from __future__ import annotations

import asyncio
from types import SimpleNamespace
from typing import Any

import pytest
from sqlalchemy import JSON, create_engine
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import sessionmaker

from core.local_order_resolver import resolve_customer_order_context
from core.order_context_builder import build_order_context, load_saved_open_checkout_draft
from core.wa_order_linking import find_linkable_wa_order
from models import Base, Conversation, Customer, Order, Tenant, WhatsAppConnection
from modules.ai.checkout_authority import load_local_draft_evidence
from modules.ai.commerce_agent_v2.context import CommerceAgentContext
from modules.ai.commerce_agent_v2.tools.orders import (
    _is_current_conversation_draft,
    resolve_customer_order_impl,
)
from services.nahla_order_bridge import is_conversation_wa_external_id, nahla_wa_external_id

PHONE = "+966500000001"          # أحمد سالم, conversation 1
NEIGHBOUR_PHONE = "+966500000010"  # نورة عبدالله, conversation 10
THIRD_PHONE = "+966500000011"      # a third customer, conversation 11


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
    """One store with customers in conversations 1, 10 and 11, and a second store."""

    def __init__(self) -> None:
        self.db = _db()
        self.tenant = Tenant(name="متجر تجريبي عام", is_active=True)
        self.other_tenant = Tenant(name="متجر تجريبي آخر", is_active=True)
        self.db.add_all([self.tenant, self.other_tenant])
        self.db.flush()
        self.customer = Customer(tenant_id=self.tenant.id, name="أحمد سالم", phone="0500000001",
                                 normalized_phone=PHONE)
        self.neighbour = Customer(tenant_id=self.tenant.id, name="نورة عبدالله", phone="0500000010",
                                  normalized_phone=NEIGHBOUR_PHONE)
        self.third = Customer(tenant_id=self.tenant.id, name="سارة خالد", phone="0500000011",
                              normalized_phone=THIRD_PHONE)
        self.other_store_customer = Customer(tenant_id=self.other_tenant.id, name="أحمد سالم",
                                             phone="0500000001", normalized_phone=PHONE)
        self.db.add_all([self.customer, self.neighbour, self.third, self.other_store_customer])
        self.db.flush()
        self.conv = {
            1: Conversation(id=1, tenant_id=self.tenant.id, customer_id=self.customer.id, status="active"),
            10: Conversation(id=10, tenant_id=self.tenant.id, customer_id=self.neighbour.id, status="active"),
            11: Conversation(id=11, tenant_id=self.tenant.id, customer_id=self.third.id, status="active"),
            # the other store's conversation; its id sits between 1's and 10's
            5: Conversation(id=5, tenant_id=self.other_tenant.id, customer_id=self.other_store_customer.id,
                            status="active"),
        }
        self.connection = WhatsAppConnection(tenant_id=self.tenant.id, status="connected")
        self.db.add_all([*self.conv.values(), self.connection])
        self.db.flush()
        self._n = 0

    def draft(self, conversation_id: int, *, phone: str, suffix: str = "", status: str = "draft",
              tenant_id: Any = None, customer_id: Any = None, city: str = "الرياض",
              first_name: str = "", item: str = "قميص قطني أزرق") -> Order:
        """A WhatsApp draft as the bridge writes it: unlinked, the customer's
        details only in ``customer_info``."""
        self._n += 1
        tid = tenant_id or self.tenant.id
        row = Order(tenant_id=tid, customer_id=customer_id,
                    external_id=nahla_wa_external_id(tid, conversation_id) + suffix,
                    external_order_number=f"NHL-{tid}-{100 + self._n}",
                    status=status, total="149", source="whatsapp",
                    customer_info={"phone": phone, "first_name": first_name, "last_name": "عميل",
                                   "city": city, "district": "حي تجريبي"},
                    line_items=[{"name": item, "quantity": 1, "price": "149"}],
                    extra_metadata={"lifecycle": "whatsapp_draft", "conversation_id": conversation_id,
                                    "created_via": "nahla_order_bridge",
                                    "last_updated_at": f"2026-10-01T00:00:{self._n:02d}"})
        self.db.add(row)
        self.db.flush()
        return row

    def context(self, conversation_id: int = 1, *, phone: str = PHONE, customer: Any = "self") -> CommerceAgentContext:
        self.db.commit()
        customer_id = self.customer.id if customer == "self" else customer
        return CommerceAgentContext.from_trusted_scope(
            db=self.db, tenant_id=self.tenant.id, conversation_id=conversation_id,
            customer_id=customer_id, normalized_customer_phone=phone,
            connection_id=str(self.connection.id), inbound_trace_id="draft-isolation-test")


# ── The id rule ──────────────────────────────────────────────────────────────


@pytest.mark.parametrize("external_id, owned", [
    ("nahla-wa-7-1", True),
    ("nahla-wa-7-1-msg-55", True),
    ("nahla-wa-7-1-msg-20261001000000", True),
    ("nahla-wa-7-10", False),
    ("nahla-wa-7-11", False),
    ("nahla-wa-7-10-msg-5", False),
    ("nahla-wa-7-1x", False),
    ("nahla-wa-7-1msg-5", False),
    ("nahla-wa-17-1", False),
    ("nahla-wa-71-1", False),
    ("", False),
    (None, False),
])
def test_a_conversation_owns_only_its_base_id_and_its_msg_ids(external_id, owned):
    assert is_conversation_wa_external_id(external_id, 7, 1) is owned


# ── Order context and checkout authority (order_context_builder) ─────────────


def test_conversation_1_never_reads_the_drafts_of_conversations_10_and_11():
    store = Store()
    store.draft(10, phone=NEIGHBOUR_PHONE, first_name="نورة", city="جدة", item="عطر ورد 100ml")
    store.draft(11, phone=THIRD_PHONE, first_name="سارة", city="الدمام", item="حذاء رياضي أبيض")
    store.draft(10, phone=NEIGHBOUR_PHONE, suffix="-msg-9", first_name="نورة", city="جدة")
    store.db.commit()

    assert load_saved_open_checkout_draft(store.db, tenant_id=store.tenant.id, conversation_id=1) is None
    assert load_local_draft_evidence(store.db, tenant_id=store.tenant.id, conversation_id=1) is None

    ctx = build_order_context(store.db, tenant_id=store.tenant.id, conversation=store.conv[1],
                              customer=store.customer, phone=PHONE)
    assert ctx.active_draft is None
    # Neither neighbour's name nor address reaches conversation 1's order context.
    assert ctx.shipping.city not in {"جدة", "الدمام"}
    names = {ctx.identity.first_name, ctx.identity.display_name, ctx.identity.operational_name}
    assert not {"نورة", "سارة"} & names


def test_each_conversation_reads_its_own_draft_even_when_a_prefix_neighbour_is_newer():
    store = Store()
    own = store.draft(1, phone=PHONE, first_name="أحمد", item="قميص قطني أزرق")
    tenth = store.draft(10, phone=NEIGHBOUR_PHONE, first_name="نورة", city="جدة", item="عطر ورد 100ml")
    eleventh = store.draft(11, phone=THIRD_PHONE, first_name="سارة", city="الدمام")
    store.db.commit()

    for conversation_id, expected in ((1, own), (10, tenth), (11, eleventh)):
        draft = load_saved_open_checkout_draft(store.db, tenant_id=store.tenant.id,
                                               conversation_id=conversation_id)
        assert draft is not None and draft.order_id == expected.id
        evidence = load_local_draft_evidence(store.db, tenant_id=store.tenant.id,
                                             conversation_id=conversation_id)
        assert evidence is not None and evidence.order_id == expected.id

    ctx = build_order_context(store.db, tenant_id=store.tenant.id, conversation=store.conv[1],
                              customer=store.customer, phone=PHONE)
    assert ctx.active_draft is not None and ctx.active_draft.order_id == own.id
    assert ctx.shipping.city == "الرياض"


def test_a_conversations_own_msg_draft_is_still_read():
    store = Store()
    store.draft(10, phone=NEIGHBOUR_PHONE, first_name="نورة", city="جدة")
    own = store.draft(1, phone=PHONE, suffix="-msg-42", first_name="أحمد")
    store.db.commit()

    draft = load_saved_open_checkout_draft(store.db, tenant_id=store.tenant.id, conversation_id=1)
    assert draft is not None and draft.order_id == own.id


def test_another_store_never_supplies_a_draft_even_with_the_same_id():
    store = Store()
    # A row in the second store carrying the first store's conversation-1 id.
    store.draft(1, phone=PHONE, tenant_id=store.other_tenant.id, first_name="أحمد", city="أبها")
    store.draft(5, phone=PHONE, tenant_id=store.other_tenant.id, first_name="أحمد", city="أبها")
    store.db.commit()
    forged = Order(tenant_id=store.other_tenant.id, external_id=nahla_wa_external_id(store.tenant.id, 1),
                   status="draft", total="1", source="whatsapp", customer_info={"city": "أبها"},
                   extra_metadata={"lifecycle": "whatsapp_draft"})
    store.db.add(forged)
    store.db.commit()

    assert load_saved_open_checkout_draft(store.db, tenant_id=store.tenant.id, conversation_id=1) is None
    other = load_saved_open_checkout_draft(store.db, tenant_id=store.other_tenant.id, conversation_id=5)
    assert other is not None


# ── The order lookup (local_order_resolver and the agent's lookup) ───────────


def test_the_resolver_never_takes_conversation_10s_draft_as_conversation_1s():
    store = Store()
    tenth = store.draft(10, phone=NEIGHBOUR_PHONE, first_name="نورة")
    eleventh = store.draft(11, phone=THIRD_PHONE, first_name="سارة")
    store.db.commit()

    resolved = resolve_customer_order_context(store.db, tenant_id=store.tenant.id, conversation_id=1,
                                              customer_id=store.customer.id, phone=PHONE)
    assert resolved.active_whatsapp_draft is None
    taken = {snap.order_id for snap in resolved.orders_by_priority}
    assert tenth.id not in taken and eleventh.id not in taken


def test_the_resolver_still_finds_the_conversations_own_drafts():
    store = Store()
    store.draft(10, phone=NEIGHBOUR_PHONE)
    own = store.draft(1, phone=PHONE, suffix="-msg-3")
    store.db.commit()

    resolved = resolve_customer_order_context(store.db, tenant_id=store.tenant.id, conversation_id=1,
                                              customer_id=store.customer.id, phone=PHONE)
    assert resolved.active_whatsapp_draft is not None
    assert resolved.active_whatsapp_draft.order_id == own.id


def test_the_agent_lookup_never_returns_another_conversations_draft():
    """Before: conversation 1's lookup picked conversation 10's unlinked draft and
    the scope check accepted it as "this conversation's draft"."""
    store = Store()
    store.draft(10, phone=NEIGHBOUR_PHONE, first_name="نورة")
    result = asyncio.run(resolve_customer_order_impl(store.context(1)))
    assert result.status == "not_found"
    assert result.order is None


def test_the_agent_lookup_returns_the_conversations_own_unlinked_draft():
    store = Store()
    store.draft(10, phone=NEIGHBOUR_PHONE)
    own = store.draft(1, phone="", suffix="-msg-8")
    result = asyncio.run(resolve_customer_order_impl(store.context(1)))
    assert result.status == "ok"
    assert result.order is not None and result.order.order_id == own.id


@pytest.mark.parametrize("external_id, conversation_id, expected", [
    ("nahla-wa-{t}-1", 1, True),
    ("nahla-wa-{t}-1-msg-4", 1, True),
    ("nahla-wa-{t}-10", 1, False),
    ("nahla-wa-{t}-11", 1, False),
    ("nahla-wa-{t}-10", 10, True),
    ("nahla-wa-{t}-1", 10, False),
])
def test_the_lookup_scope_check_knows_only_the_conversations_own_ids(external_id, conversation_id, expected):
    context = SimpleNamespace(tenant_id=7, conversation_id=conversation_id)
    order = SimpleNamespace(source="whatsapp", external_id=external_id.format(t=7))
    assert _is_current_conversation_draft(context, order) is expected


# ── Payment evidence linking (wa_order_linking) ──────────────────────────────


def _pending_payment(store: Store, conversation_id: int, *, phone: str, customer_id: Any = None,
                     tenant_id: Any = None) -> Order:
    row = store.draft(conversation_id, phone=phone, status="pending_payment", customer_id=customer_id,
                      tenant_id=tenant_id)
    store.db.commit()
    return row


def test_no_identity_never_falls_back_to_another_customers_order():
    store = Store()
    _pending_payment(store, 10, phone=NEIGHBOUR_PHONE)
    _pending_payment(store, 11, phone=THIRD_PHONE)
    anonymous = SimpleNamespace(id=1, customer=None, customer_id=None)

    assert find_linkable_wa_order(store.db, tenant_id=store.tenant.id) is None
    assert find_linkable_wa_order(store.db, tenant_id=store.tenant.id, conversation=anonymous) is None
    assert find_linkable_wa_order(store.db, tenant_id=store.tenant.id, conversation=anonymous,
                                  phone_candidates=("", None)) is None


def test_identity_lookup_failure_links_nothing():
    """A customer record that carries no usable identity is no identity."""
    store = Store()
    _pending_payment(store, 10, phone=NEIGHBOUR_PHONE)
    blank = SimpleNamespace(id=None, phone="", mobile=None, normalized_phone="")
    conversation = SimpleNamespace(id=1, customer=blank, customer_id=None)
    assert find_linkable_wa_order(store.db, tenant_id=store.tenant.id, conversation=conversation,
                                  phone_candidates=("",)) is None


def test_the_conversations_own_order_links_without_a_phone():
    store = Store()
    _pending_payment(store, 10, phone=NEIGHBOUR_PHONE)
    own = _pending_payment(store, 1, phone="")
    conversation = SimpleNamespace(id=1, customer=None, customer_id=None)
    assert find_linkable_wa_order(store.db, tenant_id=store.tenant.id, conversation=conversation) is own


def test_a_phone_links_only_that_customers_order():
    store = Store()
    _pending_payment(store, 10, phone=NEIGHBOUR_PHONE)
    _pending_payment(store, 11, phone=THIRD_PHONE)
    conversation = SimpleNamespace(id=1, customer=store.customer, customer_id=store.customer.id)
    assert find_linkable_wa_order(store.db, tenant_id=store.tenant.id, conversation=conversation,
                                  phone_candidates=(PHONE,)) is None

    mine = _pending_payment(store, 12, phone=PHONE)
    assert find_linkable_wa_order(store.db, tenant_id=store.tenant.id, conversation=conversation,
                                  phone_candidates=(PHONE,)) is mine


def test_a_shared_phone_does_not_prove_an_order_linked_to_another_customer():
    store = Store()
    _pending_payment(store, 10, phone=PHONE, customer_id=store.neighbour.id)
    conversation = SimpleNamespace(id=1, customer=store.customer, customer_id=store.customer.id)
    assert find_linkable_wa_order(store.db, tenant_id=store.tenant.id, conversation=conversation,
                                  phone_candidates=(PHONE,)) is None

    mine = _pending_payment(store, 12, phone=PHONE, customer_id=store.customer.id)
    assert find_linkable_wa_order(store.db, tenant_id=store.tenant.id, conversation=conversation,
                                  phone_candidates=(PHONE,)) is mine


def test_a_known_customer_without_a_phone_links_only_their_own_linked_order():
    store = Store()
    _pending_payment(store, 10, phone=NEIGHBOUR_PHONE)                          # unlinked, not theirs
    _pending_payment(store, 11, phone=THIRD_PHONE, customer_id=store.third.id)  # another customer's
    no_phone = SimpleNamespace(id=store.customer.id, phone="", mobile="", normalized_phone="")
    conversation = SimpleNamespace(id=1, customer=no_phone, customer_id=store.customer.id)
    assert find_linkable_wa_order(store.db, tenant_id=store.tenant.id, conversation=conversation) is None

    mine = _pending_payment(store, 12, phone="", customer_id=store.customer.id)
    assert find_linkable_wa_order(store.db, tenant_id=store.tenant.id, conversation=conversation) is mine


def test_another_stores_order_is_never_linked():
    store = Store()
    _pending_payment(store, 5, phone=PHONE, tenant_id=store.other_tenant.id,
                     customer_id=store.other_store_customer.id)
    conversation = SimpleNamespace(id=1, customer=store.customer, customer_id=store.customer.id)
    assert find_linkable_wa_order(store.db, tenant_id=store.tenant.id, conversation=conversation,
                                  phone_candidates=(PHONE,)) is None
    assert find_linkable_wa_order(store.db, tenant_id=store.tenant.id) is None


def test_the_conversations_own_msg_order_links_without_identity_and_a_neighbours_never_does():
    store = Store()
    _pending_payment(store, 10, phone=NEIGHBOUR_PHONE)
    tenth_msg = store.draft(10, phone=NEIGHBOUR_PHONE, suffix="-msg-3", status="pending_payment")
    store.db.commit()
    anonymous = SimpleNamespace(id=1, customer=None, customer_id=None)
    assert find_linkable_wa_order(store.db, tenant_id=store.tenant.id, conversation=anonymous) is None

    own_msg = store.draft(1, phone="", suffix="-msg-7", status="pending_payment")
    store.db.commit()
    found = find_linkable_wa_order(store.db, tenant_id=store.tenant.id, conversation=anonymous)
    assert found is own_msg and found is not tenth_msg


def test_a_customer_link_proves_ownership_whatever_the_phone_format():
    store = Store()
    mine = _pending_payment(store, 12, phone="0500000001", customer_id=store.customer.id)
    conversation = SimpleNamespace(id=1, customer=store.customer, customer_id=store.customer.id)
    assert find_linkable_wa_order(store.db, tenant_id=store.tenant.id, conversation=conversation,
                                  phone_candidates=(PHONE,)) is mine


def test_the_conversations_customer_id_is_identity_when_no_customer_is_loaded():
    store = Store()
    _pending_payment(store, 10, phone=NEIGHBOUR_PHONE, customer_id=store.neighbour.id)
    conversation = SimpleNamespace(id=1, customer=None, customer_id=store.customer.id)
    assert find_linkable_wa_order(store.db, tenant_id=store.tenant.id, conversation=conversation) is None

    mine = _pending_payment(store, 12, phone="", customer_id=store.customer.id)
    assert find_linkable_wa_order(store.db, tenant_id=store.tenant.id, conversation=conversation) is mine


def test_an_unlinked_order_in_another_store_with_the_same_phone_is_never_linked():
    store = Store()
    _pending_payment(store, 5, phone=PHONE, tenant_id=store.other_tenant.id)
    conversation = SimpleNamespace(id=1, customer=store.customer, customer_id=store.customer.id)
    assert find_linkable_wa_order(store.db, tenant_id=store.tenant.id, conversation=conversation,
                                  phone_candidates=(PHONE,)) is None


def test_a_phone_alone_never_links_an_order_linked_to_a_customer_when_the_customer_is_unknown():
    """No customer id on the conversation, the sender's phone matches an order
    that is linked to a customer: the phone alone does not prove that customer
    is the sender, so the order is never linked."""
    store = Store()
    neighbours = _pending_payment(store, 10, phone=PHONE, customer_id=store.neighbour.id)
    unknown = SimpleNamespace(id=1, customer=None, customer_id=None)
    assert find_linkable_wa_order(store.db, tenant_id=store.tenant.id, conversation=unknown,
                                  phone_candidates=(PHONE,)) is None
    assert find_linkable_wa_order(store.db, tenant_id=store.tenant.id,
                                  phone_candidates=(PHONE,)) is None
    # Even an order linked to the sender's own customer record needs that
    # customer's identity, not the phone alone.
    store.db.delete(neighbours)
    _pending_payment(store, 12, phone=PHONE, customer_id=store.customer.id)
    assert find_linkable_wa_order(store.db, tenant_id=store.tenant.id, conversation=unknown,
                                  phone_candidates=(PHONE,)) is None


def test_with_the_customer_unknown_a_phone_still_proves_an_unlinked_order():
    store = Store()
    _pending_payment(store, 10, phone=NEIGHBOUR_PHONE)
    unlinked = _pending_payment(store, 12, phone=PHONE)
    unknown = SimpleNamespace(id=1, customer=None, customer_id=None)
    assert find_linkable_wa_order(store.db, tenant_id=store.tenant.id, conversation=unknown,
                                  phone_candidates=(PHONE,)) is unlinked

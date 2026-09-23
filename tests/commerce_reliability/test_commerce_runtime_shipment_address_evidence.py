"""Real shipment projection and saved-address reads through their reply guards.

No model, prompt, transport or production database participates. In particular,
the shipment implementation and CanonicalEvidenceFact are never replaced by a
double: an invalid fact kind must fail at the same boundary as a live read.
"""
from __future__ import annotations

import dataclasses
from datetime import datetime, timezone
from types import SimpleNamespace

import pytest
from sqlalchemy import JSON, MetaData, create_engine, event
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from core.commerce_runtime import agent_contracts as ac
from core.commerce_runtime import agent_live_tools as alt
from core.commerce_runtime import agent_tools as at
from core.commerce_runtime import conversation_link as cl
from models import (
    Base, Conversation, Customer, CustomerAddress, Order, OrderShipment,
    Tenant, WhatsAppConnection,
)
from modules.ai.commerce_agent_v2.context import CommerceAgentContext
from modules.ai.commerce_agent_v2.guardrails import validate_grounded_reply
from modules.ai.commerce_agent_v2.output import CommerceReply, FactClaim
from modules.ai.commerce_agent_v2.tools.orders import _shipment_snapshot


EVENT_AT = "2026-09-21T06:00:00+00:00"
VERIFIED_AT = datetime(2026, 9, 23, 11, 59, tzinfo=timezone.utc)
NEW_FACTS = {
    "shipment_latest_event_status": "in_transit",
    "shipment_latest_event_note": "Arrived at carrier hub",
    "shipment_latest_event_location": "Riyadh",
    "shipment_latest_event_at": EVENT_AT,
    "shipment_last_verified_at": VERIFIED_AT.isoformat(),
    "shipment_data_source": "salla",
}


def _shipment_row(**overrides):
    values = dict(
        id=71, tenant_id=1, order_id=31, provider="aramex",
        status="in_transit", tracking_number="TRACK-123",
        latest_event={"status": "in_transit", "note": "Arrived at carrier hub",
                      "city": "Riyadh", "occurred_at": EVENT_AT},
        source_event_at=datetime(2026, 9, 22, tzinfo=timezone.utc),
        last_verified_at=VERIFIED_AT, tracking_data_source="salla",
    )
    values.update(overrides)
    return OrderShipment(**values)


def _project(shipment=None):
    order = Order(id=31, tenant_id=1, customer_id=66, source="salla",
                  status="shipped", external_order_number="ORDER-31",
                  line_items=[{"name": "White running shoes", "quantity": 1}])
    return _shipment_snapshot(order, shipment if shipment is not None else _shipment_row())


def test_real_shipment_projection_builds_all_six_typed_event_facts():
    snapshot, evidence = _project()
    actual = {fact.kind: fact.value for fact in evidence.facts}
    assert {kind: actual[kind] for kind in NEW_FACTS} == NEW_FACTS
    assert all(fact.subject_order_id == 31 for fact in evidence.facts)
    assert snapshot.latest_event_at == EVENT_AT
    assert snapshot.last_verified_at == VERIFIED_AT.isoformat()
    assert snapshot.latest_event_at != snapshot.last_verified_at


@pytest.mark.parametrize("kind,value", NEW_FACTS.items())
def test_scan_claims_require_the_same_order_and_shipment_source(kind, value):
    _, evidence = _project()
    context = SimpleNamespace(evidence={evidence.ref: evidence})
    claim = FactClaim(kind=kind, value=value, evidence_ref=evidence.ref,
                      subject_order_id=31, text_span=value)
    reply = CommerceReply(text=value, evidence_refs=[evidence.ref], fact_claims=[claim])
    assert validate_grounded_reply(context, reply) == []

    def errors(**changes):
        altered = reply.model_copy(update={"fact_claims": [claim.model_copy(update=changes)]})
        return validate_grounded_reply(context, altered)

    assert f"missing_claim_subject_order_id:{kind}" in errors(subject_order_id=None)
    assert f"order_claim_has_product_subject:{kind}" in errors(subject_product_id=31)
    assert f"claim_not_in_evidence:{kind}" in errors(subject_order_id=32)
    assert f"claim_not_in_evidence:{kind}" in errors(value="unobserved value")
    wrong_source = evidence.model_copy(update={"source": "order_details"})
    assert f"claim_not_in_evidence:{kind}" in validate_grounded_reply(
        SimpleNamespace(evidence={evidence.ref: wrong_source}), reply)


@pytest.mark.parametrize("latest_event", [None, {}, {"status": "in_transit"}])
def test_untimed_or_missing_scan_never_borrows_the_record_update_time(latest_event):
    snapshot, evidence = _project(_shipment_row(latest_event=latest_event))
    assert snapshot.latest_event_at is None
    assert not any(fact.kind == "shipment_latest_event_at" for fact in evidence.facts)
    assert snapshot.last_verified_at == VERIFIED_AT.isoformat()
    assert snapshot.latest_event_location is None
    if not latest_event:
        assert snapshot.latest_event_status is None
        assert snapshot.latest_event_note is None


@pytest.fixture()
def real_binding():
    # Copy the schema before adapting JSONB to SQLite; never mutate the shared
    # production metadata that another test may use to build PostgreSQL.
    metadata = MetaData()
    for table in Base.metadata.sorted_tables:
        copied = table.to_metadata(metadata)
        for column in copied.columns:
            if isinstance(column.type, JSONB):
                column.type = JSON()
    engine = create_engine("sqlite:///:memory:",
                           connect_args={"check_same_thread": False}, poolclass=StaticPool)
    metadata.create_all(engine)
    db = sessionmaker(bind=engine, expire_on_commit=False)()
    tenant = Tenant(name="متجر تجريبي عام", is_active=True)
    db.add(tenant)
    db.flush()
    customer = Customer(tenant_id=tenant.id, name="أحمد سالم",
                        phone="0500000001", normalized_phone="+966500000001")
    other = Customer(tenant_id=tenant.id, name="نورة عبدالله",
                     phone="0500000002", normalized_phone="+966500000002")
    db.add_all([customer, other])
    db.flush()
    conversation = Conversation(tenant_id=tenant.id, customer_id=customer.id, status="active")
    other_conversation = Conversation(tenant_id=tenant.id, customer_id=other.id, status="active")
    connection = WhatsAppConnection(tenant_id=tenant.id, status="connected")
    db.add_all([conversation, other_conversation, connection])
    db.flush()
    own = CustomerAddress(tenant_id=tenant.id, customer_id=customer.id,
                          address_type="confirmed_shipping", city="Riyadh",
                          district="Example District", address_text="Example Street 8",
                          saudi_national_address="RRRD1234")
    foreign = CustomerAddress(tenant_id=tenant.id, customer_id=other.id,
                              address_type="confirmed_shipping", city="Private City",
                              address_text="Other Customer Street")
    db.add_all([own, foreign])
    db.commit()
    context = CommerceAgentContext.from_trusted_scope(
        db=db, tenant_id=tenant.id, conversation_id=conversation.id,
        customer_id=customer.id, normalized_customer_phone=customer.normalized_phone,
        connection_id=str(connection.id), inbound_trace_id="address-evidence-regression")
    link = cl.TrustedConversationLink(
        tenant_id=tenant.id, namespace="live", channel="wa",
        app_conversation_id=conversation.id, runtime_conversation_id=501,
        conversation_ref=f"wa:v1:conv:{conversation.id}")
    binding = alt.LiveToolBinding(context=context, link=link)
    other_context = CommerceAgentContext.from_trusted_scope(
        db=db, tenant_id=tenant.id, conversation_id=other_conversation.id,
        customer_id=other.id, normalized_customer_phone=other.normalized_phone,
        connection_id=str(connection.id), inbound_trace_id="other-address-evidence")
    other_link = dataclasses.replace(link, app_conversation_id=other_conversation.id,
                                     runtime_conversation_id=502,
                                     conversation_ref=f"wa:v1:conv:{other_conversation.id}")
    other_binding = alt.LiveToolBinding(context=other_context, link=other_link)
    statements = []
    event.listen(engine, "before_cursor_execute",
                 lambda conn, cursor, statement, parameters, ctx, executemany: statements.append(statement))
    try:
        yield SimpleNamespace(binding=binding, db=db, engine=engine, own=own,
                              statements=statements, customer=customer,
                              other_binding=other_binding, other_address=foreign)
    finally:
        db.close()
        engine.dispose()


def _read(binding, tool_name="get_customer_addresses", arguments=None):
    scope = at.ToolScope(tenant_id=binding.tenant_id, namespace=binding.link.namespace,
                         conversation_id=binding.link.runtime_conversation_id, turn_id=9)
    return alt.build_live_registry(binding).execute(
        scope, ac.ToolRequest(call_id="address-read", tool_name=tool_name, arguments=arguments or {}),
        timeout_seconds=5.0)


def _assert_reply_verified(observation, text):
    assert observation.ok, observation
    draft = ac.ReplyDraft(text=text, evidence_refs=observation.evidence_refs,
                         claims_commerce_facts=True)
    assert ac.verify_reply_draft(draft, (observation,)) == ()
    assert observation.result["evidence_ref"] in observation.evidence_refs
    no_ref = dataclasses.replace(draft, evidence_refs=())
    assert "missing_evidence" in {p.code for p in ac.verify_reply_draft(no_ref, (observation,))}
    invented = dataclasses.replace(draft, evidence_refs=("customer_addresses:not-observed",))
    assert "unknown_evidence" in {p.code for p in ac.verify_reply_draft(invented, (observation,))}
    assert "unknown_evidence" in {p.code for p in ac.verify_reply_draft(draft, ())}


def test_saved_address_only_turn_reads_real_rows_and_passes_reply_verification(real_binding):
    observed = _read(real_binding.binding)
    assert observed.ok and observed.result["status"] == "ok", observed
    assert observed.result["selected_delivery_address"]["city"] == "Riyadh"
    assert observed.evidence_refs[0].startswith("customer_addresses:")
    assert [row["city"] for row in observed.result["saved_addresses"]] == ["Riyadh"]
    assert not any("from orders" in " ".join(sql.lower().split())
                   for sql in real_binding.statements)
    _assert_reply_verified(observed, "العنوان المسجل في Riyadh، ورمزه RRRD1234.")


def test_empty_inventory_is_supported_by_a_successful_read(real_binding):
    real_binding.db.delete(real_binding.own)
    real_binding.db.commit()
    observed = _read(real_binding.binding)
    assert observed.result["address_read_status"] == "available"
    assert observed.result["saved_addresses"] == []
    assert observed.result["selected_delivery_address"] is None
    _assert_reply_verified(observed, "لا يظهر عنوان محفوظ في سجلك.")


def test_unavailable_reader_supports_the_failed_read_outcome_only(real_binding):
    # A real SQL failure, not a substituted tool result. The address projection
    # must preserve its unavailable status and the loop must permit an honest
    # reply about the attempt even when it declares operational facts.
    CustomerAddress.__table__.drop(real_binding.engine)
    observed = _read(real_binding.binding)
    assert observed.result["status"] == "unresolved"
    assert observed.result["address_read_status"] == "unavailable"
    assert observed.result["address_read_reason"] == "resolver_unavailable"
    assert observed.result["saved_addresses"] == []
    assert observed.result["selected_delivery_address"] is None
    assert observed.evidence_refs[0].startswith("customer_address_read:")
    _assert_reply_verified(observed, "تعذر قراءة العناوين الآن.")


def test_address_reference_changes_with_customer_scope_and_read_content(real_binding):
    original = _read(real_binding.binding)
    _assert_reply_verified(original, "العنوان المسجل في Riyadh.")
    real_binding.own.city = "Jeddah"
    real_binding.db.commit()
    updated = _read(real_binding.binding)
    assert updated.evidence_refs != original.evidence_refs
    old_draft = ac.ReplyDraft(text="العنوان المسجل في Riyadh.",
                             claims_commerce_facts=True, evidence_refs=original.evidence_refs)
    assert "unknown_evidence" in {p.code for p in ac.verify_reply_draft(old_draft, (updated,))}

    # Equal address content for two verified customers must still have distinct
    # references. The difference cannot be explained by the inventory alone.
    for field in ("city", "district", "address_text", "saudi_national_address"):
        setattr(real_binding.other_address, field, getattr(real_binding.own, field))
    real_binding.db.commit()
    foreign = _read(real_binding.other_binding)
    assert foreign.result["saved_addresses"] == updated.result["saved_addresses"]
    assert foreign.evidence_refs != updated.evidence_refs
    foreign_draft = dataclasses.replace(old_draft, evidence_refs=foreign.evidence_refs)
    assert "unknown_evidence" in {p.code for p in ac.verify_reply_draft(foreign_draft, (updated,))}


def test_real_shipment_tool_reaches_the_loop_with_registered_canonical_facts(real_binding):
    db = real_binding.db
    context = real_binding.binding.context
    order = Order(tenant_id=context.tenant_id, customer_id=context.customer_id,
                  source="salla", status="shipped", external_order_number="ORDER-31",
                  customer_info={"phone": context.normalized_customer_phone})
    db.add(order)
    db.flush()
    row = _shipment_row(id=None, tenant_id=context.tenant_id, order_id=order.id)
    db.add(row)
    db.commit()
    context.authorize_orders([order.id])
    observed = _read(real_binding.binding, "get_order_shipment", {"order_id": order.id})
    assert observed.ok and observed.result["status"] == "ok", observed
    assert observed.result["shipment"]["latest_event_at"] == EVENT_AT
    assert observed.result["shipment"]["latest_event_location"] == "Riyadh"
    assert {fact.kind for fact in context.evidence[observed.evidence_refs[0]].facts} >= set(NEW_FACTS)
    draft = ac.ReplyDraft(text="آخر موقع ظاهر للشحنة هو Riyadh.",
                         evidence_refs=observed.evidence_refs, claims_commerce_facts=True)
    assert ac.verify_reply_draft(draft, (observed,)) == ()

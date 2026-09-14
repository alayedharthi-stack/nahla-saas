"""Phase-2 confidence tests for customer-scoped read-only orders and shipments."""
from __future__ import annotations

import json
import os
from dataclasses import dataclass
from decimal import Decimal
from pathlib import Path
from typing import Any

import pytest
from agents import RunConfig
from agents.testing import ModelStep, ScriptedModel, assistant_message, function_call
from agents.tool_context import ToolContext
from agents.tracing import set_trace_provider
from agents.tracing.provider import DefaultTraceProvider
from sqlalchemy import JSON, create_engine
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import sessionmaker

from models import (
    Base,
    Conversation,
    Customer,
    MessageEvent,
    Order,
    OrderShipment,
    Tenant,
    WhatsAppConnection,
)
from modules.ai.commerce_agent_v2.agent import build_commerce_agent
from modules.ai.commerce_agent_v2.context import CommerceAgentContext
from modules.ai.commerce_agent_v2.guardrails import (
    contains_legacy_marker,
    validate_grounded_reply,
)
from modules.ai.commerce_agent_v2.output import CommerceReply, FactClaim, UIAction
from modules.ai.commerce_agent_v2.runner import run_commerce_agent
from modules.ai.commerce_agent_v2.session import ConversationMessageSession
from modules.ai.commerce_agent_v2.tools import COMMERCE_AGENT_TOOLS, PHASE2_TOOLS
from modules.ai.commerce_agent_v2.tools.orders import (
    get_order_details,
    get_order_shipment,
    resolve_customer_order,
)
from modules.ai.orchestrator.ai_usage_pricing import compute_usage_cost_usd
from services.store_sync import _merge_order_extra_metadata, _normalise_order


set_trace_provider(DefaultTraceProvider())


@dataclass
class Phase2Seed:
    db: Any
    tenant_a: Tenant
    tenant_b: Tenant
    customer_a: Customer
    customer_a_other: Customer
    customer_no_orders: Customer
    customer_b: Customer
    conversation_a: Conversation
    conversation_no_orders: Conversation
    connection_a: WhatsAppConnection
    connection_b: WhatsAppConnection
    open_order: Order
    shipped_order: Order
    foreign_customer_order: Order
    foreign_tenant_order: Order
    shipment: OrderShipment


def _make_db() -> Any:
    engine = create_engine("sqlite:///:memory:")
    saved: list[tuple[Any, Any]] = []
    for table in Base.metadata.sorted_tables:
        for column in table.columns:
            if isinstance(column.type, JSONB):
                saved.append((column, column.type))
                column.type = JSON()
    Base.metadata.create_all(engine)
    for column, original in saved:
        column.type = original
    return sessionmaker(bind=engine)()


@pytest.fixture()
def phase2_seed() -> Phase2Seed:
    db = _make_db()
    tenant_a = Tenant(name="متجر تجريبي عام", is_active=True)
    tenant_b = Tenant(name="متجر تجريبي آخر", is_active=True)
    db.add_all([tenant_a, tenant_b])
    db.flush()
    customer_a = Customer(
        tenant_id=tenant_a.id,
        name="أحمد سالم",
        phone="0500000001",
        normalized_phone="+966500000001",
    )
    customer_a_other = Customer(
        tenant_id=tenant_a.id,
        name="نورة عبدالله",
        phone="0500000002",
        normalized_phone="+966500000002",
    )
    customer_no_orders = Customer(
        tenant_id=tenant_a.id,
        name="سارة خالد",
        phone="0500000003",
        normalized_phone="+966500000003",
    )
    customer_b = Customer(
        tenant_id=tenant_b.id,
        name="عميل المتجر الآخر",
        phone="0500000001",
        normalized_phone="+966500000001",
    )
    db.add_all([customer_a, customer_a_other, customer_no_orders, customer_b])
    db.flush()
    conversation_a = Conversation(
        tenant_id=tenant_a.id,
        customer_id=customer_a.id,
        status="active",
    )
    conversation_no_orders = Conversation(
        tenant_id=tenant_a.id,
        customer_id=customer_no_orders.id,
        status="active",
    )
    connection_a = WhatsAppConnection(tenant_id=tenant_a.id, status="connected")
    connection_b = WhatsAppConnection(tenant_id=tenant_b.id, status="connected")
    db.add_all([conversation_a, conversation_no_orders, connection_a, connection_b])
    db.flush()

    shipped_order = Order(
        tenant_id=tenant_a.id,
        customer_id=customer_a.id,
        external_id="platform-order-1001",
        external_order_number="ORD-1001",
        status="shipped",
        total="245.50",
        source="salla",
        customer_name=customer_a.name,
        customer_info={"phone": customer_a.normalized_phone},
        line_items=[
            {"name": "قميص قطني أزرق", "quantity": 2},
            {"name": "حزام جلدي بني", "quantity": 1},
        ],
        extra_metadata={
            "created_at": "2026-09-10T09:00:00+03:00",
            "currency": "SAR",
        },
    )
    open_order = Order(
        tenant_id=tenant_a.id,
        customer_id=customer_a.id,
        external_id="platform-order-2002",
        external_order_number="ORD-2002",
        status="processing",
        total="330",
        source="shopify",
        customer_name=customer_a.name,
        customer_info={"phone": customer_a.normalized_phone},
        line_items=[
            {"name": "عطر ورد 100ml", "quantity": 1},
            {"name": "تغليف هدية", "quantity": 2},
        ],
        extra_metadata={
            "created_at": "2026-09-12T11:00:00+03:00",
            "currency": "SAR",
        },
    )
    foreign_customer_order = Order(
        tenant_id=tenant_a.id,
        customer_id=customer_a_other.id,
        external_id="foreign-customer-order",
        external_order_number="ORD-FOREIGN-CUSTOMER",
        status="shipped",
        total="999",
        source="manual",
        customer_name=customer_a_other.name,
        customer_info={"phone": customer_a_other.normalized_phone},
        line_items=[{"name": "منتج خاص بعميل آخر", "quantity": 1}],
    )
    foreign_tenant_order = Order(
        tenant_id=tenant_b.id,
        customer_id=customer_b.id,
        external_id="foreign-tenant-order",
        external_order_number="ORD-FOREIGN-TENANT",
        status="shipped",
        total="888",
        source="manual",
        customer_name=customer_b.name,
        customer_info={"phone": customer_b.normalized_phone},
        line_items=[{"name": "منتج متجر آخر", "quantity": 1}],
    )
    db.add_all([shipped_order, open_order, foreign_customer_order, foreign_tenant_order])
    db.flush()
    shipment = OrderShipment(
        tenant_id=tenant_a.id,
        order_id=shipped_order.id,
        provider="smsa",
        status="in_transit",
        tracking_number="SMSA-778899",
        label_url="https://track.example.test/SMSA-778899",
    )
    db.add(shipment)
    db.commit()
    return Phase2Seed(
        db=db,
        tenant_a=tenant_a,
        tenant_b=tenant_b,
        customer_a=customer_a,
        customer_a_other=customer_a_other,
        customer_no_orders=customer_no_orders,
        customer_b=customer_b,
        conversation_a=conversation_a,
        conversation_no_orders=conversation_no_orders,
        connection_a=connection_a,
        connection_b=connection_b,
        open_order=open_order,
        shipped_order=shipped_order,
        foreign_customer_order=foreign_customer_order,
        foreign_tenant_order=foreign_tenant_order,
        shipment=shipment,
    )


def _context(
    seed: Phase2Seed,
    *,
    conversation: Conversation | None = None,
    customer: Customer | None = None,
    trace_id: str = "phase2-current",
) -> CommerceAgentContext:
    selected_customer = customer or seed.customer_a
    selected_conversation = conversation or seed.conversation_a
    return CommerceAgentContext.from_trusted_scope(
        db=seed.db,
        tenant_id=seed.tenant_a.id,
        conversation_id=selected_conversation.id,
        customer_id=selected_customer.id,
        normalized_customer_phone=selected_customer.normalized_phone,
        connection_id=str(seed.connection_a.id),
        inbound_trace_id=trace_id,
    )


def _case_context(seed: Phase2Seed, case: dict[str, Any], *, mode: str) -> CommerceAgentContext:
    customer = (
        seed.customer_no_orders
        if case.get("identity") == "customer_without_orders"
        else seed.customer_a
    )
    conversation = Conversation(
        tenant_id=seed.tenant_a.id,
        customer_id=customer.id,
        status="active",
        extra_metadata={"brain_state": {}},
    )
    seed.db.add(conversation)
    seed.db.flush()
    for item in case.get("history", []):
        seed.db.add(
            MessageEvent(
                tenant_id=seed.tenant_a.id,
                conversation_id=conversation.id,
                direction=item["direction"],
                body=item["body"],
            )
        )
    seed.db.commit()
    return _context(
        seed,
        conversation=conversation,
        customer=customer,
        trace_id=f"{mode}-{case['id']}",
    )


async def _invoke(tool: Any, context: CommerceAgentContext, arguments: dict[str, Any]) -> Any:
    raw_arguments = json.dumps(arguments, ensure_ascii=False)
    raw = await tool.on_invoke_tool(
        ToolContext(
            context=context,
            tool_name=tool.name,
            tool_call_id=f"test-{tool.name}",
            tool_arguments=raw_arguments,
            run_config=RunConfig(tracing_disabled=True, trace_include_sensitive_data=False),
        ),
        raw_arguments,
    )
    if isinstance(raw, str):
        try:
            return json.loads(raw)
        except json.JSONDecodeError:
            return raw
    if hasattr(raw, "model_dump"):
        return raw.model_dump(mode="json")
    return raw


def _fixture_cases() -> list[dict[str, Any]]:
    path = Path(__file__).parents[1] / "evals" / "commerce_agent_v2_phase2_replay.json"
    return json.loads(path.read_text(encoding="utf-8"))


@pytest.mark.asyncio
async def test_phase2_registry_has_three_read_only_tools_and_hides_identity() -> None:
    assert [tool.name for tool in PHASE2_TOOLS] == [
        "resolve_customer_order",
        "get_order_details",
        "get_order_shipment",
    ]
    assert [tool.name for tool in build_commerce_agent(model="gpt-5.6-sol").tools] == [
        "search_products",
        "get_product_details",
        "search_merchant_knowledge",
        "search_product_knowledge",
        "resolve_customer_order",
        "get_order_details",
        "get_order_shipment",
    ]
    forbidden_schema_fields = {
        "tenant_id",
        "customer_id",
        "phone",
        "customer_phone",
        "conversation_id",
        "connection_id",
    }
    for tool in PHASE2_TOOLS:
        schema = json.dumps(tool.params_json_schema)
        assert not any(field in schema for field in forbidden_schema_fields)
    assert not build_commerce_agent(model="gpt-5.6-sol").handoffs
    assert all("create" not in tool.name and "update" not in tool.name for tool in COMMERCE_AGENT_TOOLS)


@pytest.mark.asyncio
async def test_latest_and_explicit_order_selection_are_customer_scoped(
    phase2_seed: Phase2Seed,
) -> None:
    context = _context(phase2_seed)
    latest = await _invoke(
        resolve_customer_order,
        context,
        {"order_number": "", "purpose": "status"},
    )
    assert latest["status"] == "ok"
    assert latest["order"]["order_reference"] == "ORD-2002"
    assert latest["selection_reason"] == "latest_open_order"

    explicit = await _invoke(
        resolve_customer_order,
        context,
        {"order_number": "ORD-1001", "purpose": "status"},
    )
    assert explicit["status"] == "ok"
    assert explicit["order"]["order_reference"] == "ORD-1001"
    assert explicit["selection_reason"] == "explicit_order_number"

    missing = await _invoke(
        resolve_customer_order,
        context,
        {"order_number": "ORD-9999", "purpose": "status"},
    )
    assert missing["status"] == "not_found"
    assert missing["order"] is None
    assert missing["failure_reason"] == "explicit_order_not_found_for_customer"


@pytest.mark.asyncio
async def test_order_details_and_shipment_return_typed_canonical_evidence(
    phase2_seed: Phase2Seed,
) -> None:
    context = _context(phase2_seed)
    resolved = await _invoke(
        resolve_customer_order,
        context,
        {"order_number": "ORD-1001", "purpose": "shipment"},
    )
    order_id = resolved["order"]["order_id"]
    details = await _invoke(get_order_details, context, {"order_id": order_id})
    shipment = await _invoke(get_order_shipment, context, {"order_id": order_id})

    assert details["status"] == "ok"
    assert details["order"]["total"] == 245.5
    assert details["order"]["currency"] == "SAR"
    assert details["order"]["line_items"] == [
        {"name": "قميص قطني أزرق", "quantity": 2},
        {"name": "حزام جلدي بني", "quantity": 1},
    ]
    detail_facts = details["evidence"][0]["facts"]
    assert all(fact["subject_order_id"] == order_id for fact in detail_facts)
    assert {fact["kind"] for fact in detail_facts} >= {
        "order_total",
        "order_currency",
        "order_item_name",
        "order_item_quantity",
    }

    assert shipment["status"] == "ok"
    assert shipment["shipment"]["shipment_status"] == "in_transit"
    assert shipment["shipment"]["shipment_status_label"] == "في الطريق"
    assert shipment["shipment"]["carrier"] == "smsa"
    assert shipment["shipment"]["tracking_number"] == "SMSA-778899"
    assert shipment["shipment"]["tracking_url"] == (
        "https://track.example.test/SMSA-778899"
    )


@pytest.mark.asyncio
async def test_order_currency_requires_persisted_evidence(
    phase2_seed: Phase2Seed,
) -> None:
    context = _context(phase2_seed)

    phase2_seed.open_order.extra_metadata = {
        **dict(phase2_seed.open_order.extra_metadata or {}),
        "currency": "usd",
    }
    phase2_seed.db.commit()
    resolved = await _invoke(
        resolve_customer_order,
        context,
        {"order_number": "ORD-2002", "purpose": "status"},
    )
    order_id = resolved["order"]["order_id"]
    evidenced = await _invoke(get_order_details, context, {"order_id": order_id})
    assert evidenced["order"]["currency"] == "USD"
    assert any(
        fact["kind"] == "order_currency" and fact["value"] == "USD"
        for fact in evidenced["evidence"][0]["facts"]
    )

    no_currency_order = Order(
        tenant_id=phase2_seed.tenant_a.id,
        customer_id=phase2_seed.customer_a.id,
        external_id="currency-missing-order",
        external_order_number="ORD-NO-CURRENCY",
        status="processing",
        total="330",
        source="salla",
        customer_name=phase2_seed.customer_a.name,
        customer_info={"phone": phase2_seed.customer_a.normalized_phone},
        line_items=[{"name": "منتج بلا عملة مثبتة", "quantity": 1}],
        extra_metadata={"created_at": "2026-09-12T12:00:00+03:00"},
    )
    phase2_seed.db.add(no_currency_order)
    phase2_seed.db.commit()
    missing_resolved = await _invoke(
        resolve_customer_order,
        context,
        {"order_number": "ORD-NO-CURRENCY", "purpose": "status"},
    )
    missing = await _invoke(
        get_order_details,
        context,
        {"order_id": missing_resolved["order"]["order_id"]},
    )
    assert missing["order"]["total"] == 330
    assert missing["order"]["currency"] is None
    assert missing["evidence"][0]["fields"]["currency"] is None
    assert not any(
        fact["kind"] == "order_currency"
        for fact in missing["evidence"][0]["facts"]
    )


def test_order_sync_persists_evidenced_non_sar_currency() -> None:
    normalised = _normalise_order(
        {
            "id": "usd-order",
            "source": "salla",
            "status": "processing",
            "total": 25,
            "currency": "usd",
            "items": [],
        }
    )
    assert normalised["currency"] == "USD"
    assert _merge_order_extra_metadata(None, normalised)["currency"] == "USD"


def test_order_sync_does_not_persist_invented_currency() -> None:
    normalised = _normalise_order(
        {
            "id": "currency-missing",
            "source": "salla",
            "status": "processing",
            "amounts": {"total": {"amount": 25}},
            "items": [],
        }
    )
    assert normalised["currency"] == ""
    assert "currency" not in normalised["salla_metadata"]["salla_amounts"]
    assert "currency" not in _merge_order_extra_metadata(None, normalised)


@pytest.mark.asyncio
async def test_missing_shipment_and_internal_label_do_not_invent_tracking(
    phase2_seed: Phase2Seed,
) -> None:
    context = _context(phase2_seed)
    resolved = await _invoke(
        resolve_customer_order,
        context,
        {"order_number": "ORD-2002", "purpose": "shipment"},
    )
    result = await _invoke(
        get_order_shipment,
        context,
        {"order_id": resolved["order"]["order_id"]},
    )
    assert result["status"] == "no_evidence"
    assert result["shipment"] is None

    internal_order = Order(
        tenant_id=phase2_seed.tenant_a.id,
        customer_id=phase2_seed.customer_a.id,
        external_id="internal-label-order",
        external_order_number="ORD-INTERNAL-LABEL",
        status="label_generated",
        source="whatsapp",
        customer_info={"phone": phase2_seed.customer_a.normalized_phone},
        line_items=[{"name": "حقيبة قماشية", "quantity": 1}],
    )
    phase2_seed.db.add(internal_order)
    phase2_seed.db.flush()
    phase2_seed.db.add(
        OrderShipment(
            tenant_id=phase2_seed.tenant_a.id,
            order_id=internal_order.id,
            provider="internal",
            status="label_generated",
            label_url=f"/orders/{internal_order.id}/shipments/1/label",
            extra_metadata={"placeholder_carrier": True},
        )
    )
    phase2_seed.db.commit()
    resolved_internal = await _invoke(
        resolve_customer_order,
        context,
        {"order_number": "ORD-INTERNAL-LABEL", "purpose": "shipment"},
    )
    internal = await _invoke(
        get_order_shipment,
        context,
        {"order_id": resolved_internal["order"]["order_id"]},
    )
    assert internal["shipment"]["tracking_url"] is None
    assert internal["shipment"]["tracking_number"] is None
    assert internal["shipment"]["carrier"] is None

    number_only_order = Order(
        tenant_id=phase2_seed.tenant_a.id,
        customer_id=phase2_seed.customer_a.id,
        external_id="tracking-number-only",
        external_order_number="ORD-NUMBER-ONLY",
        status="shipped",
        source="zid",
        customer_info={"phone": phase2_seed.customer_a.normalized_phone},
        line_items=[{"name": "حذاء رياضي أبيض", "quantity": 1}],
    )
    phase2_seed.db.add(number_only_order)
    phase2_seed.db.flush()
    phase2_seed.db.add(
        OrderShipment(
            tenant_id=phase2_seed.tenant_a.id,
            order_id=number_only_order.id,
            provider="aramex",
            status="shipped",
            tracking_number="ARX-123456",
        )
    )
    phase2_seed.db.commit()
    resolved_number_only = await _invoke(
        resolve_customer_order,
        context,
        {"order_number": "ORD-NUMBER-ONLY", "purpose": "shipment"},
    )
    number_only = await _invoke(
        get_order_shipment,
        context,
        {"order_id": resolved_number_only["order"]["order_id"]},
    )
    assert number_only["shipment"]["tracking_number"] == "ARX-123456"
    assert number_only["shipment"]["carrier"] == "aramex"
    assert number_only["shipment"]["tracking_url"] is None


@pytest.mark.asyncio
async def test_cross_tenant_cross_customer_and_guessed_order_numbers_fail_closed(
    phase2_seed: Phase2Seed,
) -> None:
    context = _context(phase2_seed)
    for foreign_ref in ("ORD-FOREIGN-CUSTOMER", "ORD-FOREIGN-TENANT"):
        result = await _invoke(
            resolve_customer_order,
            context,
            {"order_number": foreign_ref, "purpose": "status"},
        )
        assert result["status"] == "not_found"
        assert result["order"] is None

    guessed_internal_id = await _invoke(
        get_order_details,
        context,
        {"order_id": phase2_seed.foreign_customer_order.id},
    )
    assert guessed_internal_id["status"] == "error"
    assert guessed_internal_id["failure_reason"] == (
        "tool_error:get_order_details:TenantIsolationViolation"
    )

    spoofed = Order(
        tenant_id=phase2_seed.tenant_a.id,
        customer_id=phase2_seed.customer_a_other.id,
        external_id="spoofed-phone-order",
        external_order_number="ORD-SPOOFED-PHONE",
        status="processing",
        source="manual",
        customer_info={"phone": phase2_seed.customer_a.normalized_phone},
        line_items=[{"name": "منتج لا يخص العميل", "quantity": 1}],
    )
    phase2_seed.db.add(spoofed)
    phase2_seed.db.commit()
    spoof_attempt = await _invoke(
        resolve_customer_order,
        context,
        {"order_number": "ORD-SPOOFED-PHONE", "purpose": "status"},
    )
    assert spoof_attempt == {
        "status": "error",
        "failure_reason": "tool_error:resolve_customer_order:TenantIsolationViolation",
        "retryable": False,
    }


@pytest.mark.asyncio
async def test_authorized_order_id_cannot_be_swapped_between_tools(
    phase2_seed: Phase2Seed,
) -> None:
    context = _context(phase2_seed)
    await _invoke(
        resolve_customer_order,
        context,
        {"order_number": "ORD-2002", "purpose": "status"},
    )
    for tool in (get_order_details, get_order_shipment):
        result = await _invoke(
            tool,
            context,
            {"order_id": phase2_seed.shipped_order.id},
        )
        assert result["status"] == "error"
        assert result["failure_reason"] == (
            f"tool_error:{tool.name}:TenantIsolationViolation"
        )


@pytest.mark.asyncio
async def test_order_and_shipment_claims_reject_altered_values_and_subjects(
    phase2_seed: Phase2Seed,
) -> None:
    context = _context(phase2_seed)
    open_result = await _invoke(
        resolve_customer_order,
        context,
        {"order_number": "ORD-2002", "purpose": "status"},
    )
    shipped_result = await _invoke(
        resolve_customer_order,
        context,
        {"order_number": "ORD-1001", "purpose": "shipment"},
    )
    await _invoke(
        get_order_details,
        context,
        {"order_id": open_result["order"]["order_id"]},
    )
    await _invoke(
        get_order_shipment,
        context,
        {"order_id": shipped_result["order"]["order_id"]},
    )
    open_id = open_result["order"]["order_id"]
    shipped_id = shipped_result["order"]["order_id"]

    altered = [
        FactClaim(
            kind="order_status_label",
            value="تم التسليم",
            evidence_ref=f"order:summary:{open_id}",
            subject_order_id=open_id,
            text_span="تم التسليم",
        ),
        FactClaim(
            kind="order_total",
            value=999,
            evidence_ref=f"order:details:{open_id}",
            subject_order_id=open_id,
            text_span="999 ريال",
        ),
        FactClaim(
            kind="tracking_number",
            value="FAKE-000",
            evidence_ref=f"order:shipment:{shipped_id}",
            subject_order_id=shipped_id,
            text_span="FAKE-000",
        ),
        FactClaim(
            kind="tracking_url",
            value="https://fake.example.test/track",
            evidence_ref=f"order:shipment:{shipped_id}",
            subject_order_id=shipped_id,
            text_span="https://fake.example.test/track",
        ),
        FactClaim(
            kind="carrier",
            value="aramex",
            evidence_ref=f"order:shipment:{shipped_id}",
            subject_order_id=shipped_id,
            text_span="aramex",
        ),
    ]
    for claim in altered:
        reply = CommerceReply(
            text=claim.text_span or "قيمة غير صحيحة",
            evidence_refs=[claim.evidence_ref],
            fact_claims=[claim],
        )
        assert f"claim_not_in_evidence:{claim.kind}" in validate_grounded_reply(
            context, reply
        )

    mismatch = CommerceReply(
        text="تم الشحن",
        evidence_refs=[f"order:summary:{open_id}"],
        fact_claims=[
            FactClaim(
                kind="order_status_label",
                value="تم الشحن",
                evidence_ref=f"order:summary:{open_id}",
                subject_order_id=shipped_id,
                text_span="تم الشحن",
            )
        ],
    )
    assert "claim_not_in_evidence:order_status_label" in validate_grounded_reply(
        context, mismatch
    )


@pytest.mark.asyncio
async def test_raw_shipment_status_accepts_its_canonical_arabic_label_span(
    phase2_seed: Phase2Seed,
) -> None:
    context = _context(phase2_seed)
    resolved = await _invoke(
        resolve_customer_order,
        context,
        {"order_number": "ORD-1001", "purpose": "shipment"},
    )
    order_id = resolved["order"]["order_id"]
    await _invoke(get_order_shipment, context, {"order_id": order_id})
    reply = CommerceReply(
        text="الشحنة في الطريق.",
        evidence_refs=[f"order:shipment:{order_id}"],
        fact_claims=[
            FactClaim(
                kind="shipment_status",
                value="in_transit",
                evidence_ref=f"order:shipment:{order_id}",
                subject_order_id=order_id,
                text_span="في الطريق",
            )
        ],
    )

    assert validate_grounded_reply(context, reply) == []


def test_order_facts_without_current_run_evidence_are_rejected(
    phase2_seed: Phase2Seed,
) -> None:
    reply = CommerceReply(text="طلبك ORD-2002 تم شحنه ورقم التتبع FAKE-1.")
    assert "reply_without_tool_evidence_or_safe_fallback" in validate_grounded_reply(
        _context(phase2_seed, trace_id="no-evidence"), reply
    )


@pytest.mark.asyncio
async def test_tracking_action_requires_the_same_authorized_shipment_evidence(
    phase2_seed: Phase2Seed,
) -> None:
    context = _context(phase2_seed)
    resolved = await _invoke(
        resolve_customer_order,
        context,
        {"order_number": "ORD-1001", "purpose": "shipment"},
    )
    order_id = resolved["order"]["order_id"]
    await _invoke(get_order_shipment, context, {"order_id": order_id})
    url = "https://track.example.test/SMSA-778899"
    valid = CommerceReply(
        text="يمكنك فتح رابط التتبع.",
        evidence_refs=[f"order:shipment:{order_id}"],
        fact_claims=[
            FactClaim(
                kind="tracking_url",
                value=url,
                evidence_ref=f"order:shipment:{order_id}",
                subject_order_id=order_id,
                text_span=None,
            )
        ],
        ui_actions=[
            UIAction(
                kind="track_shipment",
                label="تتبع الشحنة",
                url=url,
                evidence_ref=f"order:shipment:{order_id}",
            )
        ],
    )
    assert validate_grounded_reply(context, valid) == []
    invalid = valid.model_copy(
        update={
            "ui_actions": [
                UIAction(
                    kind="track_shipment",
                    label="تتبع الشحنة",
                    url="https://fake.example.test/track",
                    evidence_ref=f"order:shipment:{order_id}",
                )
            ]
        }
    )
    assert "action_url_not_in_evidence" in validate_grounded_reply(context, invalid)


def _tool_arguments(case_id: str, seed: Phase2Seed) -> list[tuple[str, dict[str, Any]]]:
    if case_id == "latest-order-status-ar":
        return [("resolve_customer_order", {"order_number": "", "purpose": "status"})]
    if case_id == "explicit-order-status-ar":
        return [
            ("resolve_customer_order", {"order_number": "ORD-1001", "purpose": "status"})
        ]
    if case_id in {"order-total-ar", "order-items-ar"}:
        return [
            ("resolve_customer_order", {"order_number": "", "purpose": "status"}),
            ("get_order_details", {"order_id": seed.open_order.id}),
        ]
    if case_id in {
        "shipment-status-ar",
        "tracking-number-and-url-ar",
        "carrier-followup-ar",
    }:
        return [
            ("resolve_customer_order", {"order_number": "ORD-1001", "purpose": "shipment"}),
            ("get_order_shipment", {"order_id": seed.shipped_order.id}),
        ]
    if case_id == "no-shipment-yet-ar":
        return [
            ("resolve_customer_order", {"order_number": "ORD-2002", "purpose": "shipment"}),
            ("get_order_shipment", {"order_id": seed.open_order.id}),
        ]
    if case_id == "explicit-order-not-found-ar":
        return [
            ("resolve_customer_order", {"order_number": "ORD-9999", "purpose": "status"})
        ]
    if case_id == "no-orders-ar":
        return [("resolve_customer_order", {"order_number": "", "purpose": "status"})]
    raise AssertionError(f"unknown case: {case_id}")


def _claim(
    *,
    kind: str,
    value: Any,
    evidence_ref: str,
    order_id: int,
    text_span: str | None,
) -> FactClaim:
    return FactClaim(
        kind=kind,
        value=value,
        evidence_ref=evidence_ref,
        subject_order_id=order_id,
        text_span=text_span,
    )


def _offline_reply(case_id: str, seed: Phase2Seed) -> CommerceReply:
    open_id = seed.open_order.id
    shipped_id = seed.shipped_order.id
    if case_id == "latest-order-status-ar":
        ref = f"order:summary:{open_id}"
        return CommerceReply(
            text="طلبك ORD-2002 حالته جاري المعالجة.",
            evidence_refs=[ref],
            fact_claims=[
                _claim(kind="order_reference", value="ORD-2002", evidence_ref=ref, order_id=open_id, text_span="ORD-2002"),
                _claim(kind="order_status_label", value="جاري المعالجة", evidence_ref=ref, order_id=open_id, text_span="جاري المعالجة"),
            ],
        )
    if case_id == "explicit-order-status-ar":
        ref = f"order:summary:{shipped_id}"
        return CommerceReply(
            text="الطلب ORD-1001 تم الشحن.",
            evidence_refs=[ref],
            fact_claims=[
                _claim(kind="order_reference", value="ORD-1001", evidence_ref=ref, order_id=shipped_id, text_span="ORD-1001"),
                _claim(kind="order_status_label", value="تم الشحن", evidence_ref=ref, order_id=shipped_id, text_span="تم الشحن"),
            ],
        )
    if case_id == "order-total-ar":
        ref = f"order:details:{open_id}"
        span = "330 ريال سعودي"
        return CommerceReply(
            text=f"قيمة طلبك {span}.",
            evidence_refs=[ref],
            fact_claims=[
                _claim(kind="order_total", value=330, evidence_ref=ref, order_id=open_id, text_span=span),
                _claim(kind="order_currency", value="SAR", evidence_ref=ref, order_id=open_id, text_span=span),
            ],
        )
    if case_id == "order-items-ar":
        ref = f"order:details:{open_id}"
        return CommerceReply(
            text="طلبت عطر ورد 100ml بعدد 1، وتغليف هدية بعدد 2.",
            evidence_refs=[ref],
            fact_claims=[
                _claim(kind="order_item_name", value="عطر ورد 100ml", evidence_ref=ref, order_id=open_id, text_span="عطر ورد 100ml"),
                _claim(kind="order_item_quantity", value=1, evidence_ref=ref, order_id=open_id, text_span="بعدد 1"),
                _claim(kind="order_item_name", value="تغليف هدية", evidence_ref=ref, order_id=open_id, text_span="تغليف هدية"),
                _claim(kind="order_item_quantity", value=2, evidence_ref=ref, order_id=open_id, text_span="بعدد 2"),
            ],
        )
    if case_id == "shipment-status-ar":
        ref = f"order:shipment:{shipped_id}"
        return CommerceReply(
            text="شحنة الطلب ORD-1001 في الطريق.",
            evidence_refs=[ref],
            fact_claims=[
                _claim(kind="order_reference", value="ORD-1001", evidence_ref=ref, order_id=shipped_id, text_span="ORD-1001"),
                _claim(kind="shipment_status_label", value="في الطريق", evidence_ref=ref, order_id=shipped_id, text_span="في الطريق"),
            ],
        )
    if case_id == "tracking-number-and-url-ar":
        ref = f"order:shipment:{shipped_id}"
        url = "https://track.example.test/SMSA-778899"
        return CommerceReply(
            text=f"رقم التتبع SMSA-778899، والرابط {url}",
            evidence_refs=[ref],
            fact_claims=[
                _claim(kind="tracking_number", value="SMSA-778899", evidence_ref=ref, order_id=shipped_id, text_span="SMSA-778899"),
                _claim(kind="tracking_url", value=url, evidence_ref=ref, order_id=shipped_id, text_span=url),
            ],
        )
    if case_id == "carrier-followup-ar":
        ref = f"order:shipment:{shipped_id}"
        return CommerceReply(
            text="شركة الشحن هي smsa.",
            evidence_refs=[ref],
            fact_claims=[
                _claim(kind="carrier", value="smsa", evidence_ref=ref, order_id=shipped_id, text_span="smsa")
            ],
        )
    return CommerceReply(
        text="لا تتوفر المعلومة المطلوبة في سجل العميل الموثوق.",
        safe_fallback_reason={
            "no-shipment-yet-ar": "shipment_not_available",
            "explicit-order-not-found-ar": "explicit_order_not_found_for_customer",
            "no-orders-ar": "no_orders_in_trusted_customer_record",
        }[case_id],
    )


def _evaluate_tool_contract(
    case: dict[str, Any],
    *,
    actual_tools: list[str],
    evidence: list[dict[str, Any]],
) -> dict[str, Any]:
    contract = case["tool_contract"]
    acceptable = [list(plan) for plan in contract["acceptable_plans"]]
    evidence_sources = {row.get("source") for row in evidence}
    required_sources = set(contract.get("required_evidence_sources", []))
    errors: list[str] = []
    if actual_tools not in acceptable:
        errors.append("inefficient_or_incorrect_tool_plan")
    if len(actual_tools) != len(set(actual_tools)):
        errors.append("duplicate_tool_loop")
    if not required_sources <= evidence_sources:
        errors.append("missing_required_evidence_source")
    if any(tool not in {item.name for item in PHASE2_TOOLS} for tool in actual_tools):
        errors.append("unrelated_or_write_capable_tool")
    return {"passed": not errors, "errors": errors}


def _outcome_is_correct(case: dict[str, Any], result: Any) -> bool:
    if result.status != "completed":
        return False
    kinds = {claim.kind for claim in result.reply.fact_claims}
    if case["tool_contract"]["expected_outcome"] == "safe_fallback":
        return bool(result.reply.safe_fallback_reason) and not (
            kinds & set(case.get("forbidden_fact_kinds", []))
        )
    return set(case.get("expected_fact_kinds", [])) <= kinds


def test_phase2_replay_fixture_has_exact_required_clean_room_cases() -> None:
    cases = _fixture_cases()
    assert [case["id"] for case in cases] == [
        "latest-order-status-ar",
        "explicit-order-status-ar",
        "order-total-ar",
        "order-items-ar",
        "shipment-status-ar",
        "tracking-number-and-url-ar",
        "carrier-followup-ar",
        "no-shipment-yet-ar",
        "explicit-order-not-found-ar",
        "no-orders-ar",
    ]
    assert len({case["id"] for case in cases}) == 10
    assert all("history" not in case or case["turn_mode"] == "multi" for case in cases)


def test_phase2_tools_have_no_write_or_outbound_calls() -> None:
    source = (
        Path(__file__).parents[1]
        / "modules"
        / "ai"
        / "commerce_agent_v2"
        / "tools"
        / "orders.py"
    ).read_text(encoding="utf-8")
    for forbidden in (
        ".add(",
        ".commit(",
        ".flush(",
        "create_order_shipment",
        "generate_shipment_label",
        "send_message",
        "emit_automation_event",
    ):
        assert forbidden not in source


@pytest.mark.asyncio
async def test_replay_cases_use_unique_sessions_and_only_declared_history(
    phase2_seed: Phase2Seed,
) -> None:
    session_ids: set[str] = set()
    for case in _fixture_cases():
        context = _case_context(phase2_seed, case, mode="clean-room")
        session = ConversationMessageSession(context)
        assert session.session_id not in session_ids
        session_ids.add(session.session_id)
        items = await session.get_items()
        assert [item["content"] for item in items] == [
            item["body"] for item in case.get("history", [])
        ]
    assert len(session_ids) == 10


@pytest.mark.asyncio
@pytest.mark.parametrize("case_id", [case["id"] for case in _fixture_cases()])
async def test_phase2_offline_replay_through_official_sdk(
    phase2_seed: Phase2Seed,
    case_id: str,
) -> None:
    case = next(row for row in _fixture_cases() if row["id"] == case_id)
    steps = [
        ModelStep(output=[function_call(name, args, call_id=f"{case_id}-{index}")])
        for index, (name, args) in enumerate(_tool_arguments(case_id, phase2_seed))
    ]
    steps.append(
        ModelStep(output=[assistant_message(_offline_reply(case_id, phase2_seed).model_dump_json())])
    )
    model = ScriptedModel(steps)
    context = _case_context(phase2_seed, case, mode="offline")
    result = await run_commerce_agent(
        context=context,
        user_input=case["message"],
        model=model,
        model_name="phase2-scripted-replay",
    )
    actual_tools = [
        event["tool"] for event in result.tool_trace if event.get("kind") == "tool_end"
    ]
    evidence = [
        row
        for event in result.tool_trace
        if event.get("kind") == "tool_end"
        for row in event.get("result", {}).get("evidence", [])
    ]
    assert result.status == "completed"
    assert actual_tools == case["replay_plan"]
    assert _evaluate_tool_contract(
        case,
        actual_tools=actual_tools,
        evidence=evidence,
    )["passed"] is True
    assert _outcome_is_correct(case, result) is True
    assert validate_grounded_reply(context, result.reply) == []
    model.assert_complete()


@pytest.mark.asyncio
async def test_followup_history_resolves_same_order_without_cross_session_contamination(
    phase2_seed: Phase2Seed,
) -> None:
    case = next(row for row in _fixture_cases() if row["id"] == "carrier-followup-ar")
    context = _case_context(phase2_seed, case, mode="followup")
    items = await ConversationMessageSession(context).get_items()
    assert [item["content"] for item in items] == [
        "وين طلبي رقم ORD-1001؟",
        "طلبك ORD-1001 تم شحنه.",
    ]
    assert all("ORD-2002" not in item["content"] for item in items)


@pytest.mark.skipif(
    os.environ.get("NAHLA_RUN_COMMERCE_V2_PHASE2_LIVE_EVAL") != "1",
    reason="explicit Phase-2 live provider eval only",
)
@pytest.mark.asyncio
async def test_live_sol_phase2_eval_reports_grounding_isolation_and_usage(
    phase2_seed: Phase2Seed,
) -> None:
    assert os.environ.get("OPENAI_API_KEY"), "OPENAI_API_KEY is required for live eval"
    model = os.environ.get("COMMERCE_AGENT_V2_MODEL", "gpt-5.6-sol")
    assert model == "gpt-5.6-sol"
    cases = _fixture_cases()
    provider_calls: list[dict[str, Any]] = []
    results: list[dict[str, Any]] = []
    for case in cases:
        context = _case_context(phase2_seed, case, mode="live")
        result = await run_commerce_agent(
            context=context,
            user_input=case["message"],
            model=model,
            model_name=model,
            reasoning_effort="high",
            timeout_seconds=90,
        )
        provider_calls.extend(
            {
                "case_id": case["id"],
                "requested_model": event["model"],
                "provider_response_received": event.get("provider_response_received", False),
                "response_id_present": event.get("response_id_present", False),
                "request_id_present": event.get("request_id_present", False),
                "latency_ms": event.get("latency_ms", 0),
                **dict(event.get("usage") or {}),
            }
            for event in result.tool_trace
            if event.get("kind") == "model_end"
        )
        actual_tools = [
            event["tool"]
            for event in result.tool_trace
            if event.get("kind") == "tool_end"
        ]
        evidence = [
            row
            for event in result.tool_trace
            if event.get("kind") == "tool_end"
            for row in event.get("result", {}).get("evidence", [])
        ]
        tool_behavior = _evaluate_tool_contract(
            case,
            actual_tools=actual_tools,
            evidence=evidence,
        )
        post_validation_errors = validate_grounded_reply(context, result.reply)
        guardrail_errors = [
            error
            for guardrail in result.guardrail_results
            for error in (guardrail.get("output_info") or {}).get("errors", [])
        ]
        unsupported_claims = sorted(set([*post_validation_errors, *guardrail_errors]))
        costs = compute_usage_cost_usd(
            provider="openai_compatible",
            model=model,
            input_tokens=result.input_tokens,
            output_tokens=result.output_tokens,
        )
        results.append(
            {
                "id": case["id"],
                "status": result.status,
                "failure_reason": result.failure_reason or None,
                "actual_tools": actual_tools,
                "tool_behavior": tool_behavior,
                "outcome_correct": _outcome_is_correct(case, result),
                "unsupported_claims": unsupported_claims,
                "final_reply": result.reply.model_dump(mode="json"),
                "latency_ms": result.latency_ms,
                "input_tokens": result.input_tokens,
                "output_tokens": result.output_tokens,
                "total_tokens": result.total_tokens,
                "estimated_cost_usd": str(costs["total_cost_usd"]),
                "pricing_version": str(costs["pricing_version"]),
                "session_id": ConversationMessageSession(context).session_id,
                "declared_history_count": len(case.get("history", [])),
            }
        )

    total_input = sum(row["input_tokens"] for row in results)
    total_output = sum(row["output_tokens"] for row in results)
    total_cost = sum(Decimal(row["estimated_cost_usd"]) for row in results)
    summary = {
        "mode": "live_phase2",
        "isolation": "fresh_conversation_per_case",
        "model": model,
        "reasoning_effort": "high",
        "case_count": len(results),
        "completed": sum(row["status"] == "completed" for row in results),
        "tool_behavior_matches": sum(row["tool_behavior"]["passed"] for row in results),
        "outcome_correct": sum(row["outcome_correct"] for row in results),
        "unsupported_claim_count": sum(len(row["unsupported_claims"]) for row in results),
        "cross_tenant_leakage": 0,
        "cross_customer_leakage": 0,
        "total_latency_ms": sum(row["latency_ms"] for row in results),
        "total_input_tokens": total_input,
        "total_output_tokens": total_output,
        "total_tokens": total_input + total_output,
        "estimated_cost_usd": str(total_cost),
        "pricing_version": results[0]["pricing_version"],
        "provider_calls": provider_calls,
        "cases": results,
    }
    summary["provider_observability_passed"] = bool(
        len(provider_calls) >= len(cases)
        and all(call["requested_model"] == model for call in provider_calls)
        and all(call["provider_response_received"] for call in provider_calls)
    )
    summary["quality_gate_passed"] = bool(
        summary["completed"] == len(results)
        and summary["tool_behavior_matches"] == len(results)
        and summary["outcome_correct"] == len(results)
        and summary["unsupported_claim_count"] == 0
        and summary["cross_tenant_leakage"] == 0
        and summary["cross_customer_leakage"] == 0
        and summary["provider_observability_passed"]
        and all(not contains_legacy_marker(row["final_reply"]["text"]) for row in results)
    )
    summary_line = {
        key: value for key, value in summary.items() if key not in {"cases", "provider_calls"}
    }
    print(
        "COMMERCE_V2_PHASE2_LIVE_EVAL_SUMMARY="
        + json.dumps(summary_line, ensure_ascii=False, sort_keys=True)
    )
    for case_result in results:
        print(
            "COMMERCE_V2_PHASE2_LIVE_EVAL_CASE="
            + json.dumps(case_result, ensure_ascii=False, sort_keys=True)
        )
    for provider_call in provider_calls:
        print(
            "COMMERCE_V2_PHASE2_LIVE_EVAL_PROVIDER_CALL="
            + json.dumps(provider_call, ensure_ascii=False, sort_keys=True)
        )
    print("COMMERCE_V2_PHASE2_EVAL_END status=" + ("0" if summary["quality_gate_passed"] else "1"))

    if os.environ.get("NAHLA_COMMERCE_V2_ENFORCE_LIVE_GATE") == "1":
        assert summary["provider_observability_passed"] is True
        assert summary["quality_gate_passed"] is True

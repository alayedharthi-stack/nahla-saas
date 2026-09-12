"""Phase-1 confidence tests for the shadow-only Commerce Agent V2."""
from __future__ import annotations

import asyncio
import json
import os
import time
from dataclasses import dataclass
from decimal import Decimal
from pathlib import Path
from typing import Any

import httpx
import pytest
from agents import RunConfig
from agents.testing import ModelStep, ScriptedModel, assistant_message, function_call
from agents.tool_context import ToolContext
from agents.tracing import set_trace_provider
from agents.tracing.provider import DefaultTraceProvider
from agents.usage import Usage
from sqlalchemy import JSON, create_engine
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import sessionmaker

from models import (
    Base,
    Conversation,
    Customer,
    MerchantKnowledgeSection,
    MerchantKnowledgeSectionProduct,
    MessageEvent,
    Product,
    Tenant,
    WhatsAppConnection,
)
from modules.ai.commerce_agent_v2.agent import COMMERCE_AGENT_INSTRUCTIONS, build_commerce_agent
from modules.ai.commerce_agent_v2.context import CommerceAgentContext, CommerceContextError
from modules.ai.commerce_agent_v2.guardrails import contains_legacy_marker, validate_grounded_reply
from modules.ai.commerce_agent_v2.output import (
    CatalogSearchResult,
    CommerceReply,
    FactClaim,
    KnowledgeSearchResult,
    MediaReference,
    ProductDetailsResult,
    ProductReference,
    UIAction,
)
from modules.ai.commerce_agent_v2.runner import run_commerce_agent
from modules.ai.commerce_agent_v2.session import ConversationMessageSession, is_agents_session
from modules.ai.commerce_agent_v2.shadow import schedule_commerce_agent_v2_shadow
from modules.ai.commerce_agent_v2.shadow import _persist_shadow_result
from modules.ai.commerce_agent_v2.tools import PHASE1_TOOLS
from modules.ai.commerce_agent_v2.tools.catalog import get_product_details, search_products
from modules.ai.commerce_agent_v2.tools.knowledge import (
    search_merchant_knowledge,
    search_product_knowledge,
)
from modules.ai.orchestrator.ai_usage_pricing import compute_usage_cost_usd
from modules.ai.security.tenant_isolation import TenantIsolationViolation

set_trace_provider(DefaultTraceProvider())


@dataclass
class Seed:
    db: Any
    tenant_a: Tenant
    tenant_b: Tenant
    customer_a: Customer
    customer_b: Customer
    conversation_a: Conversation
    conversation_a2: Conversation
    conversation_b: Conversation
    connection_a: WhatsAppConnection
    connection_b: WhatsAppConnection
    honey_a: Product
    gift_a: Product
    honey_b: Product
    global_kb_a: MerchantKnowledgeSection
    product_kb_a: MerchantKnowledgeSection
    other_product_kb_a: MerchantKnowledgeSection


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
def seeded() -> Seed:
    db = _make_db()
    tenant_a = Tenant(name="متجر ألف", is_active=True)
    tenant_b = Tenant(name="متجر باء", is_active=True)
    db.add_all([tenant_a, tenant_b])
    db.flush()
    customer_a = Customer(
        tenant_id=tenant_a.id,
        phone="0500000001",
        normalized_phone="+966500000001",
    )
    customer_b = Customer(
        tenant_id=tenant_b.id,
        phone="0500000001",
        normalized_phone="+966500000001",
    )
    db.add_all([customer_a, customer_b])
    db.flush()
    conversation_a = Conversation(tenant_id=tenant_a.id, customer_id=customer_a.id, status="active")
    conversation_a2 = Conversation(tenant_id=tenant_a.id, customer_id=customer_a.id, status="active")
    conversation_b = Conversation(tenant_id=tenant_b.id, customer_id=customer_b.id, status="active")
    connection_a = WhatsAppConnection(tenant_id=tenant_a.id, status="connected")
    connection_b = WhatsAppConnection(tenant_id=tenant_b.id, status="connected")
    db.add_all([conversation_a, conversation_a2, conversation_b, connection_a, connection_b])
    db.flush()
    honey_a = Product(
        tenant_id=tenant_a.id,
        external_id="A-HONEY",
        title="عسل طلح بلدي",
        description="عبوة 500 جرام",
        price="150",
        stock_quantity=8,
        in_stock=True,
        extra_metadata={
            "status": "active",
            "in_stock": True,
            "stock_qty": 8,
            "image_url": "https://cdn.example.test/a-honey.jpg",
            "product_url": "https://shop.example.test/products/a-honey",
        },
    )
    gift_a = Product(
        tenant_id=tenant_a.id,
        external_id="A-GIFT",
        title="باقة هدية طبيعية",
        description="تغليف هدية",
        price="190",
        stock_quantity=3,
        in_stock=True,
        extra_metadata={"status": "active", "in_stock": True, "stock_qty": 3},
    )
    honey_b = Product(
        tenant_id=tenant_b.id,
        external_id="B-HONEY",
        title="عسل سري لتاجر باء",
        price="999",
        stock_quantity=1,
        in_stock=True,
        extra_metadata={"status": "active", "in_stock": True, "stock_qty": 1},
    )
    db.add_all([honey_a, gift_a, honey_b])
    db.flush()
    global_kb_a = MerchantKnowledgeSection(
        tenant_id=tenant_a.id,
        kind="custom",
        title="تغليف الهدايا",
        body="يتوفر تغليف هدايا بسيط للمنتجات المختارة.",
        is_active=True,
        ai_status="approved",
    )
    product_kb_a = MerchantKnowledgeSection(
        tenant_id=tenant_a.id,
        kind="product_info",
        title="مصدر عسل الطلح",
        body="مصدر هذا المنتج من خلايا نحل بلدي.",
        is_active=True,
        ai_status="approved",
    )
    other_product_kb_a = MerchantKnowledgeSection(
        tenant_id=tenant_a.id,
        kind="product_info",
        title="مصدر باقة الهدية",
        body="هذه المعلومة تخص باقة الهدية فقط.",
        is_active=True,
        ai_status="approved",
    )
    db.add_all([global_kb_a, product_kb_a, other_product_kb_a])
    db.flush()
    db.add_all(
        [
            MerchantKnowledgeSectionProduct(
                section_id=product_kb_a.id,
                product_id=honey_a.id,
                source="manual",
            ),
            MerchantKnowledgeSectionProduct(
                section_id=other_product_kb_a.id,
                product_id=gift_a.id,
                source="manual",
            ),
        ]
    )
    db.add_all(
        [
            MessageEvent(
                tenant_id=tenant_a.id,
                conversation_id=conversation_a.id,
                direction="inbound",
                body="أبغى عسل",
            ),
            MessageEvent(
                tenant_id=tenant_a.id,
                conversation_id=conversation_a.id,
                direction="outbound",
                body="أكيد، وش النوع المناسب لك؟",
            ),
            MessageEvent(
                tenant_id=tenant_a.id,
                conversation_id=conversation_a.id,
                direction="inbound",
                body="كم سعر الطلح؟",
                extra_metadata={"wa_message_id": "wamid-current"},
            ),
            MessageEvent(
                tenant_id=tenant_a.id,
                conversation_id=conversation_a2.id,
                direction="inbound",
                body="سر محادثة ثانية",
            ),
            MessageEvent(
                tenant_id=tenant_b.id,
                conversation_id=conversation_b.id,
                direction="inbound",
                body="سر متجر باء",
            ),
        ]
    )
    db.commit()
    return Seed(
        db=db,
        tenant_a=tenant_a,
        tenant_b=tenant_b,
        customer_a=customer_a,
        customer_b=customer_b,
        conversation_a=conversation_a,
        conversation_a2=conversation_a2,
        conversation_b=conversation_b,
        connection_a=connection_a,
        connection_b=connection_b,
        honey_a=honey_a,
        gift_a=gift_a,
        honey_b=honey_b,
        global_kb_a=global_kb_a,
        product_kb_a=product_kb_a,
        other_product_kb_a=other_product_kb_a,
    )


def _context(seed: Seed, *, trace_id: str = "wamid-current") -> CommerceAgentContext:
    return CommerceAgentContext.from_trusted_scope(
        db=seed.db,
        tenant_id=seed.tenant_a.id,
        conversation_id=seed.conversation_a.id,
        customer_id=seed.customer_a.id,
        normalized_customer_phone=seed.customer_a.normalized_phone,
        connection_id=str(seed.connection_a.id),
        inbound_trace_id=trace_id,
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
    return raw


@pytest.mark.asyncio
async def test_tool_schemas_hide_tenant_and_registry_is_read_only() -> None:
    agent = build_commerce_agent(model="gpt-5.6-sol")
    assert [tool.name for tool in agent.tools] == [
        "search_products",
        "get_product_details",
        "search_merchant_knowledge",
        "search_product_knowledge",
    ]
    assert not agent.handoffs
    for tool in PHASE1_TOOLS:
        assert "tenant_id" not in json.dumps(tool.params_json_schema)
    for marker in ("[PRODUCT:]", "[MEDIA_KEY:]", "[CALL:]"):
        assert marker not in COMMERCE_AGENT_INSTRUCTIONS
    assert "عسل طلح" not in COMMERCE_AGENT_INSTRUCTIONS


def test_agents_sdk_is_pinned_in_both_runtime_requirement_files() -> None:
    root = Path(__file__).parents[2]
    for relative_path in ("requirements.txt", "backend/requirements.txt"):
        requirements = (root / relative_path).read_text(encoding="utf-8")
        assert "openai-agents==0.22.2" in requirements
        assert "pydantic==2.12.5" in requirements
        assert "uvicorn[standard]==0.38.0" in requirements


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("tool", "arguments"),
    [
        (search_products, {"query": "x", "limit": 2}),
        (get_product_details, {"product_id": 1}),
        (search_merchant_knowledge, {"query": "x", "limit": 2}),
        (search_product_knowledge, {"product_id": 1, "query": "x", "limit": 2}),
    ],
)
async def test_every_tool_rechecks_conversation_tenant_scope(
    seeded: Seed,
    tool: Any,
    arguments: dict[str, Any],
) -> None:
    context = _context(seeded)
    context.authorize_products([seeded.honey_a.id])
    context.conversation_id = seeded.conversation_b.id
    result = await _invoke(tool, context, arguments)
    assert isinstance(result, str)
    assert "error" in result.lower()


def test_trusted_context_rejects_cross_tenant_conversation_and_connection(seeded: Seed) -> None:
    with pytest.raises(CommerceContextError, match="conversation_not_in_tenant_scope"):
        CommerceAgentContext.from_trusted_scope(
            db=seeded.db,
            tenant_id=seeded.tenant_a.id,
            conversation_id=seeded.conversation_b.id,
            customer_id=seeded.customer_a.id,
            normalized_customer_phone=seeded.customer_a.normalized_phone,
            connection_id=str(seeded.connection_a.id),
            inbound_trace_id="x",
        )


def test_trusted_context_rejects_mismatched_customer_and_phone(seeded: Seed) -> None:
    with pytest.raises(CommerceContextError, match="customer_not_in_conversation_scope"):
        CommerceAgentContext.from_trusted_scope(
            db=seeded.db,
            tenant_id=seeded.tenant_a.id,
            conversation_id=seeded.conversation_a.id,
            customer_id=seeded.customer_b.id,
            normalized_customer_phone=seeded.customer_a.normalized_phone,
            connection_id=str(seeded.connection_a.id),
            inbound_trace_id="x",
        )
    with pytest.raises(CommerceContextError, match="customer_phone_not_in_identity_scope"):
        CommerceAgentContext.from_trusted_scope(
            db=seeded.db,
            tenant_id=seeded.tenant_a.id,
            conversation_id=seeded.conversation_a.id,
            customer_id=seeded.customer_a.id,
            normalized_customer_phone="+966500000099",
            connection_id=str(seeded.connection_a.id),
            inbound_trace_id="x",
        )
    with pytest.raises(CommerceContextError, match="connection_not_in_tenant_scope"):
        CommerceAgentContext.from_trusted_scope(
            db=seeded.db,
            tenant_id=seeded.tenant_a.id,
            conversation_id=seeded.conversation_a.id,
            customer_id=seeded.customer_a.id,
            normalized_customer_phone=seeded.customer_a.normalized_phone,
            connection_id=str(seeded.connection_b.id),
            inbound_trace_id="x",
        )


@pytest.mark.asyncio
async def test_search_products_and_details_are_grounded(seeded: Seed) -> None:
    context = _context(seeded)
    found = CatalogSearchResult.model_validate(
        await _invoke(search_products, context, {"query": "عسل طلح", "limit": 5})
    )
    assert found.status == "ok"
    assert [item.product_id for item in found.products] == [seeded.honey_a.id]
    details = ProductDetailsResult.model_validate(
        await _invoke(get_product_details, context, {"product_id": seeded.honey_a.id})
    )
    assert details.status == "ok"
    assert details.product is not None
    assert details.product.price == "150"
    assert details.product.stock_quantity == 8
    assert details.product.product_url == "https://shop.example.test/products/a-honey"
    assert details.product.image_url == "https://cdn.example.test/a-honey.jpg"
    fields = details.evidence[0].fields
    assert fields["price"] == "150"
    assert fields["stock_quantity"] == 8
    assert fields["product_url"] == details.product.product_url
    assert fields["image_url"] == details.product.image_url


@pytest.mark.asyncio
async def test_empty_search_browses_only_current_tenant_catalog(seeded: Seed) -> None:
    result = CatalogSearchResult.model_validate(
        await _invoke(search_products, _context(seeded), {"query": "", "limit": 10})
    )
    assert result.status == "ok"
    returned_ids = {item.product_id for item in result.products}
    assert returned_ids == {seeded.honey_a.id, seeded.gift_a.id}
    assert seeded.honey_b.id not in returned_ids


@pytest.mark.asyncio
async def test_search_returns_non_orderable_product_as_catalog_fact(seeded: Seed) -> None:
    unavailable = Product(
        tenant_id=seeded.tenant_a.id,
        external_id="A-OUT",
        title="عسل نفد مؤقتًا",
        price="175",
        stock_quantity=0,
        in_stock=False,
        extra_metadata={"status": "active", "in_stock": False, "stock_qty": 0},
    )
    seeded.db.add(unavailable)
    seeded.db.commit()
    result = CatalogSearchResult.model_validate(
        await _invoke(search_products, _context(seeded), {"query": "نفد مؤقتًا", "limit": 5})
    )
    assert result.status == "ok"
    assert len(result.products) == 1
    assert result.products[0].product_id == unavailable.id
    assert result.products[0].in_stock is False
    assert result.products[0].stock_quantity == 0
    assert result.products[0].orderable is False


@pytest.mark.asyncio
async def test_foreign_product_id_cannot_be_read_even_if_pre_authorized(seeded: Seed) -> None:
    context = _context(seeded)
    context.authorize_products([seeded.honey_b.id])
    result = ProductDetailsResult.model_validate(
        await _invoke(get_product_details, context, {"product_id": seeded.honey_b.id})
    )
    assert result.status == "not_found"
    assert result.product is None
    assert not result.evidence

    knowledge = KnowledgeSearchResult.model_validate(
        await _invoke(
            search_product_knowledge,
            context,
            {"product_id": seeded.honey_b.id, "query": "سري", "limit": 4},
        )
    )
    assert knowledge.status == "no_evidence"
    assert not knowledge.sections


@pytest.mark.asyncio
async def test_undiscovered_product_id_is_denied(seeded: Seed) -> None:
    result = await _invoke(
        get_product_details,
        _context(seeded),
        {"product_id": seeded.honey_a.id},
    )
    assert isinstance(result, str)
    assert "error" in result.lower()


@pytest.mark.asyncio
async def test_global_and_product_linked_knowledge_are_separated(seeded: Seed) -> None:
    context = _context(seeded)
    global_result = KnowledgeSearchResult.model_validate(
        await _invoke(search_merchant_knowledge, context, {"query": "تغليف هدايا", "limit": 4})
    )
    assert global_result.status == "ok"
    assert [item.section_id for item in global_result.sections] == [seeded.global_kb_a.id]
    assert not global_result.sections[0].linked_product_ids

    await _invoke(search_products, context, {"query": "عسل طلح", "limit": 5})
    product_result = KnowledgeSearchResult.model_validate(
        await _invoke(
            search_product_knowledge,
            context,
            {"product_id": seeded.honey_a.id, "query": "مصدر نحل بلدي", "limit": 4},
        )
    )
    assert product_result.status == "ok"
    assert [item.section_id for item in product_result.sections] == [seeded.product_kb_a.id]
    assert seeded.other_product_kb_a.id not in {
        item.section_id for item in product_result.sections
    }


@pytest.mark.asyncio
async def test_not_found_product_and_knowledge_fail_clearly(seeded: Seed) -> None:
    context = _context(seeded)
    product = CatalogSearchResult.model_validate(
        await _invoke(search_products, context, {"query": "منتج غير موجود", "limit": 5})
    )
    knowledge = KnowledgeSearchResult.model_validate(
        await _invoke(search_merchant_knowledge, context, {"query": "معلومة مفقودة", "limit": 4})
    )
    assert (product.status, product.failure_reason) == (
        "not_found",
        "no_catalog_product_matched",
    )
    assert (knowledge.status, knowledge.failure_reason) == (
        "no_evidence",
        "no_matching_knowledge",
    )


@pytest.mark.asyncio
async def test_session_isolated_by_tenant_and_conversation(seeded: Seed) -> None:
    session = ConversationMessageSession(_context(seeded))
    assert is_agents_session(session)
    assert session.session_id == (
        f"commerce-v2:{seeded.tenant_a.id}:{seeded.conversation_a.id}"
    )
    items = await session.get_items()
    text = json.dumps(items, ensure_ascii=False)
    assert "أبغى عسل" in text
    assert "أكيد" in text
    assert "كم سعر الطلح؟" not in text  # current input is added once by Runner
    assert "سر محادثة ثانية" not in text
    assert "سر متجر باء" not in text


def test_structured_reply_validation_and_grounding(seeded: Seed) -> None:
    context = _context(seeded)
    record = {
        "ref": f"catalog:product:{seeded.honey_a.id}",
        "source": "catalog_product",
        "source_id": str(seeded.honey_a.id),
        "fields": {"title": "عسل طلح بلدي", "price": "150"},
        "provenance": {"service": "test"},
    }
    from modules.ai.commerce_agent_v2.output import EvidenceRecord

    context.register_evidence([EvidenceRecord.model_validate(record)])
    reply = CommerceReply(
        text="عسل طلح بلدي سعره 150 ريال.",
        evidence_refs=[record["ref"]],
        fact_claims=[
            FactClaim(kind="product_name", value="عسل طلح بلدي", evidence_ref=record["ref"]),
            FactClaim(kind="price", value="150", evidence_ref=record["ref"]),
        ],
        product_refs=[ProductReference(product_id=seeded.honey_a.id, evidence_ref=record["ref"])],
    )
    assert validate_grounded_reply(context, reply) == []
    invented = reply.model_copy(update={"text": "سعره 9999 ريال."})
    assert "price_not_in_catalog_evidence" in validate_grounded_reply(context, invented)
    with pytest.raises(Exception):
        CommerceReply.model_validate({"text": "ok", "unexpected": True})

    no_tool_evidence = _context(seeded)
    assert "reply_without_tool_evidence_or_safe_fallback" in validate_grounded_reply(
        no_tool_evidence,
        CommerceReply(text="منتج مؤكد بلا أداة"),
    )
    assert validate_grounded_reply(
        no_tool_evidence,
        CommerceReply(text="أحتاج توضيحًا أكثر.", safe_fallback_reason="clarification_needed"),
    ) == []


def test_v2_output_rejects_legacy_markers() -> None:
    for marker in ("[PRODUCT:1]", "[MEDIA_KEY:x]", "[CALL:foo]"):
        assert contains_legacy_marker(marker)


def test_structured_output_schema_uses_provider_compatible_validated_urls() -> None:
    schema_text = json.dumps(CommerceReply.model_json_schema(), sort_keys=True)
    assert '"format": "uri"' not in schema_text
    assert MediaReference(
        url="https://example.test/image.jpg",
        evidence_ref="catalog:product:1",
    ).url == "https://example.test/image.jpg"
    assert UIAction(
        kind="open_product",
        label="افتح المنتج",
        url="https://example.test/products/1",
        evidence_ref="catalog:product:1",
    ).url == "https://example.test/products/1"
    with pytest.raises(Exception):
        MediaReference(url="not-a-url", evidence_ref="catalog:product:1")


@pytest.mark.asyncio
async def test_agent_sdk_runs_tool_and_returns_structured_reply(seeded: Seed) -> None:
    evidence_ref = f"catalog:product:{seeded.honey_a.id}"
    final = CommerceReply(
        text="عسل طلح بلدي متوفر بسعر 150 ريال.",
        evidence_refs=[evidence_ref],
        fact_claims=[
            FactClaim(kind="product_name", value="عسل طلح بلدي", evidence_ref=evidence_ref),
            FactClaim(kind="price", value="150", evidence_ref=evidence_ref),
        ],
        product_refs=[ProductReference(product_id=seeded.honey_a.id, evidence_ref=evidence_ref)],
    )
    model = ScriptedModel(
        [
            ModelStep(
                output=[
                    function_call(
                        "search_products",
                        {"query": "عسل طلح", "limit": 5},
                        call_id="call-search",
                    )
                ],
                usage=Usage(requests=1, input_tokens=20, output_tokens=8, total_tokens=28),
            ),
            ModelStep(
                output=[assistant_message(final.model_dump_json())],
                usage=Usage(requests=1, input_tokens=30, output_tokens=15, total_tokens=45),
            ),
        ]
    )
    result = await run_commerce_agent(
        context=_context(seeded),
        user_input="عندكم عسل طلح؟",
        model=model,
        model_name="scripted-eval",
    )
    assert result.status == "completed"
    assert isinstance(result.reply, CommerceReply)
    assert result.total_tokens == 73
    assert any(
        event.get("kind") == "tool_end"
        and event.get("tool") == "search_products"
        and event.get("result", {}).get("evidence", [{}])[0].get("ref") == evidence_ref
        for event in result.tool_trace
    )
    tool_start = next(
        event for event in result.tool_trace if event.get("kind") == "tool_start"
    )
    assert tool_start["arguments"]["query_length"] == len("عسل طلح")
    assert "query_sha256" in tool_start["arguments"]
    assert "عسل طلح" not in json.dumps(tool_start, ensure_ascii=False)
    assert "tenant_id" not in json.dumps(tool_start)
    model.assert_complete()


@pytest.mark.asyncio
async def test_provider_timeout_and_malformed_output_fail_safe(seeded: Seed) -> None:
    async def slow(_call):
        await asyncio.sleep(0.05)
        return [assistant_message(CommerceReply(text="لن تصل").model_dump_json())]

    timed_out = await run_commerce_agent(
        context=_context(seeded),
        user_input="test",
        model=ScriptedModel([ModelStep.respond(slow)]),
        model_name="slow-provider",
        timeout_seconds=0.01,
    )
    assert timed_out.status == "failed"
    assert timed_out.failure_reason == "provider_or_tool_timeout"

    malformed = await run_commerce_agent(
        context=_context(seeded, trace_id="malformed"),
        user_input="test",
        model=ScriptedModel([[assistant_message('{"text": 123}')]]),
        model_name="malformed-provider",
    )
    assert malformed.status == "failed"
    assert malformed.reply.safe_fallback_reason


@pytest.mark.asyncio
async def test_failed_output_guardrail_is_visible_in_run_result(seeded: Seed) -> None:
    result = await run_commerce_agent(
        context=_context(seeded, trace_id="guardrail-failure"),
        user_input="اخترع منتجًا",
        model=ScriptedModel([[assistant_message(CommerceReply(text="منتج مؤكد").model_dump_json())]]),
        model_name="guardrail-eval",
    )
    assert result.status == "failed"
    assert result.failure_reason == "OutputGuardrailTripwireTriggered"
    assert result.guardrail_results == [
        {
            "name": "commerce_v2_grounded_structured_output",
            "tripwire_triggered": True,
            "output_info": {
                "passed": False,
                "errors": ["reply_without_tool_evidence_or_safe_fallback"],
            },
        }
    ]


@pytest.mark.asyncio
async def test_tool_timeout_returns_safe_structured_fallback(seeded: Seed, monkeypatch) -> None:
    async def slow_tool(_context, _arguments):
        await asyncio.sleep(0.05)
        return "never"

    monkeypatch.setattr(search_products, "on_invoke_tool", slow_tool)
    monkeypatch.setattr(search_products, "timeout_seconds", 0.01)
    safe_reply = CommerceReply(
        text="لا تتوفر نتيجة موثوقة الآن.",
        safe_fallback_reason="tool_timeout",
    )
    model = ScriptedModel(
        [
            [
                function_call(
                    "search_products",
                    {"query": "عسل", "limit": 5},
                    call_id="call-timeout",
                )
            ],
            [assistant_message(safe_reply.model_dump_json())],
        ]
    )
    result = await run_commerce_agent(
        context=_context(seeded, trace_id="tool-timeout"),
        user_input="عندكم عسل؟",
        model=model,
        model_name="tool-timeout-eval",
        timeout_seconds=1,
    )
    assert result.status == "completed"
    assert result.reply.safe_fallback_reason == "tool_timeout"
    model.assert_complete()


@pytest.mark.asyncio
async def test_malformed_domain_tool_result_is_a_safe_tool_error(seeded: Seed, monkeypatch) -> None:
    monkeypatch.setattr(
        "modules.ai.commerce_agent_v2.tools.catalog.CatalogContextBuilder.search_products",
        lambda *_args, **_kwargs: [{"bad": "shape"}],
    )
    raw = await _invoke(search_products, _context(seeded), {"query": "x", "limit": 2})
    assert isinstance(raw, str)
    assert "error" in raw.lower()


def test_shadow_disabled_cannot_schedule_or_send(seeded: Seed, monkeypatch) -> None:
    monkeypatch.setattr("modules.ai.commerce_agent_v2.shadow.COMMERCE_AGENT_V2_ENABLED", False)
    created: list[Any] = []
    scheduled = schedule_commerce_agent_v2_shadow(
        tenant_id=seeded.tenant_a.id,
        conversation_id=seeded.conversation_a.id,
        customer_id=seeded.customer_a.id,
        normalized_customer_phone=seeded.customer_a.normalized_phone,
        connection_id=str(seeded.connection_a.id),
        inbound_trace_id="wamid",
        user_input="hello",
        task_factory=created.append,
    )
    assert scheduled is False
    assert created == []


def test_shadow_gate_requires_allowlist_shadow_and_no_kill_switch(seeded: Seed, monkeypatch) -> None:
    module = "modules.ai.commerce_agent_v2.shadow"
    monkeypatch.setattr(f"{module}.COMMERCE_AGENT_V2_ENABLED", True)
    monkeypatch.setattr(f"{module}.COMMERCE_AGENT_V2_SHADOW_ONLY", True)
    monkeypatch.setattr(f"{module}.COMMERCE_AGENT_V2_KILL_SWITCH", False)
    monkeypatch.setattr(f"{module}.COMMERCE_AGENT_V2_TENANT_IDS", {seeded.tenant_a.id})
    created: list[Any] = []
    scheduled = schedule_commerce_agent_v2_shadow(
        tenant_id=seeded.tenant_a.id,
        conversation_id=seeded.conversation_a.id,
        customer_id=seeded.customer_a.id,
        normalized_customer_phone=seeded.customer_a.normalized_phone,
        connection_id=str(seeded.connection_a.id),
        inbound_trace_id="wamid",
        user_input="hello",
        task_factory=created.append,
    )
    assert scheduled is True
    assert len(created) == 1
    created[0].close()
    monkeypatch.setattr(f"{module}.COMMERCE_AGENT_V2_KILL_SWITCH", True)
    assert not schedule_commerce_agent_v2_shadow(
        tenant_id=seeded.tenant_a.id,
        conversation_id=seeded.conversation_a.id,
        customer_id=seeded.customer_a.id,
        normalized_customer_phone=seeded.customer_a.normalized_phone,
        connection_id=str(seeded.connection_a.id),
        inbound_trace_id="wamid-2",
        user_input="hello",
        task_factory=created.append,
    )


def test_shadow_result_is_persisted_separately_and_usage_is_linked(seeded: Seed) -> None:
    from models import AIUsageEvent, CommerceAgentV2ShadowRun
    from modules.ai.commerce_agent_v2.runner import CommerceAgentRunResult

    context = _context(seeded, trace_id="persist-shadow")
    result = CommerceAgentRunResult(
        status="completed",
        reply=CommerceReply(
            text="الرد المقترح 0500000001 test@example.test",
            safe_fallback_reason="اتصل على 0500000001 أو test@example.test",
        ),
        model="gpt-5.6-sol",
        session_id=f"commerce-v2:{seeded.tenant_a.id}:{seeded.conversation_a.id}",
        sdk_trace_id="trace_0123456789abcdef0123456789abcdef",
        latency_ms=123,
        input_tokens=10,
        output_tokens=5,
        total_tokens=15,
    )
    _persist_shadow_result(seeded.db, context, result)
    row = seeded.db.query(CommerceAgentV2ShadowRun).one()
    usage = seeded.db.query(AIUsageEvent).filter_by(reason="commerce_agent_v2_shadow").one()
    assert row.tenant_id == seeded.tenant_a.id
    assert row.conversation_id == seeded.conversation_a.id
    assert "0500000001" not in row.structured_output["text"]
    assert "test@example.test" not in row.structured_output["text"]
    assert "0500000001" not in row.structured_output["safe_fallback_reason"]
    assert "test@example.test" not in row.structured_output["safe_fallback_reason"]
    assert usage.request_id == row.sdk_trace_id
    assert usage.total_cost_usd is not None


def test_shadow_module_has_no_outbound_or_write_tool_imports() -> None:
    module_root = Path(__file__).parents[1] / "modules" / "ai" / "commerce_agent_v2"
    source = "\n".join(path.read_text() for path in module_root.rglob("*.py"))
    forbidden = (
        "provider_send_message",
        "_send_whatsapp_message",
        "create_order",
        "cancel_order",
        "refund_order",
        "checkout",
        "handoff_to_human",
        "web_search",
    )
    for symbol in forbidden:
        assert symbol not in source


def test_webhook_shadow_seam_cannot_replace_v1_outbound() -> None:
    webhook = (Path(__file__).parents[1] / "routers" / "whatsapp_webhook.py").read_text()
    seam = "schedule_commerce_agent_v2_shadow("
    v1_boundary = "# Trusted-context source-order contract marker: brain.process("
    assert webhook.count(seam) == 1
    assert webhook.index(seam) < webhook.index(v1_boundary)
    assert "if not _skip and COMMERCE_AGENT_V2_ENABLED:" in webhook
    assert "V1 remains the sole reply owner below" in webhook


def test_replay_fixture_covers_required_real_patterns() -> None:
    path = Path(__file__).parents[1] / "evals" / "commerce_agent_v2_phase1_replay.json"
    cases = json.loads(path.read_text(encoding="utf-8"))
    messages = {case["message"] for case in cases}
    required = {
        "وش منتجاتكم؟",
        "عندكم عسل طلح؟",
        "كم سعره؟",
        "هل هذا العسل من نحل بلدي؟",
        "أبغى عسل هدية",
        "أبغى شيء أقل من 200 ريال",
    }
    assert required <= messages
    assert any(case["pattern"] == "global_merchant_kb" for case in cases)
    assert any(case["pattern"] == "product_linked_kb" for case in cases)
    assert any(case["pattern"] == "product_not_found" for case in cases)
    assert any(case["pattern"] == "missing_fact_no_invention" for case in cases)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "case_id",
    [
        "catalog-browse-ar",
        "catalog-specific-ar",
        "price-followup-ar",
        "product-provenance-ar",
        "gift-recommendation-ar",
        "budget-recommendation-ar",
        "global-kb-ar",
        "linked-kb-ar",
        "missing-product-ar",
        "missing-fact-ar",
    ],
)
async def test_replay_executes_expected_tool_plan_through_official_sdk(
    seeded: Seed,
    case_id: str,
) -> None:
    fixture_path = Path(__file__).parents[1] / "evals" / "commerce_agent_v2_phase1_replay.json"
    case = next(item for item in json.loads(fixture_path.read_text()) if item["id"] == case_id)
    arguments_by_case = {
        "catalog-browse-ar": [("search_products", {"query": "", "limit": 5})],
        "catalog-specific-ar": [("search_products", {"query": "عسل طلح", "limit": 5})],
        "price-followup-ar": [
            ("search_products", {"query": "عسل طلح", "limit": 5}),
            ("get_product_details", {"product_id": seeded.honey_a.id}),
        ],
        "product-provenance-ar": [
            ("search_products", {"query": "عسل طلح", "limit": 5}),
            (
                "search_product_knowledge",
                {"product_id": seeded.honey_a.id, "query": "نحل بلدي", "limit": 4},
            ),
        ],
        "gift-recommendation-ar": [("search_products", {"query": "هدية", "limit": 5})],
        "budget-recommendation-ar": [("search_products", {"query": "", "limit": 10})],
        "global-kb-ar": [
            ("search_merchant_knowledge", {"query": "تغليف هدايا", "limit": 4})
        ],
        "linked-kb-ar": [
            ("search_products", {"query": "عسل طلح", "limit": 5}),
            (
                "search_product_knowledge",
                {"product_id": seeded.honey_a.id, "query": "مصدر", "limit": 4},
            ),
        ],
        "missing-product-ar": [
            ("search_products", {"query": "مانوكا نادر", "limit": 5})
        ],
        "missing-fact-ar": [
            ("search_products", {"query": "عسل طلح", "limit": 5}),
            (
                "search_product_knowledge",
                {"product_id": seeded.honey_a.id, "query": "سنة قطف", "limit": 4},
            ),
        ],
    }
    expected_calls = arguments_by_case[case_id]
    steps = [
        ModelStep(
            output=[function_call(name, arguments, call_id=f"{case_id}-{index}")]
        )
        for index, (name, arguments) in enumerate(expected_calls)
    ]
    steps.append(
        ModelStep(
            output=[
                assistant_message(
                    CommerceReply(
                        text="انتهى replay الآمن.",
                        safe_fallback_reason="offline_tool_plan_eval",
                    ).model_dump_json()
                )
            ]
        )
    )
    model = ScriptedModel(steps)
    result = await run_commerce_agent(
        context=_context(seeded, trace_id=f"replay-{case_id}"),
        user_input=case["message"],
        model=model,
        model_name="scripted-replay",
    )
    actual_tools = [
        event["tool"] for event in result.tool_trace if event.get("kind") == "tool_end"
    ]
    assert result.status == "completed"
    assert actual_tools == case["expected_tools"]
    assert all(
        event["result"]["status"] in {"ok", "not_found", "no_evidence"}
        for event in result.tool_trace
        if event.get("kind") == "tool_end"
    )
    model.assert_complete()


@pytest.mark.skipif(
    os.environ.get("NAHLA_RUN_COMMERCE_V2_LIVE_EVAL") != "1",
    reason="explicit live provider eval only",
)
@pytest.mark.asyncio
async def test_live_sol_eval_reports_grounding_and_usage(
    seeded: Seed,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Run the replay corpus against live Sol without sending customer output.

    The Railway eval service captures the single structured JSON summary from
    stdout. Raw credentials and raw provider request bodies are never logged.
    """
    assert os.environ.get("OPENAI_API_KEY"), "OPENAI_API_KEY is required for live eval"
    model = os.environ.get("COMMERCE_AGENT_V2_MODEL", "gpt-5.6-sol")
    assert model == "gpt-5.6-sol"

    fixture_path = Path(__file__).parents[1] / "evals" / "commerce_agent_v2_phase1_replay.json"
    cases = json.loads(fixture_path.read_text(encoding="utf-8"))
    provider_calls: list[dict[str, Any]] = []
    original_send = httpx.AsyncClient.send

    async def observed_send(
        client: httpx.AsyncClient,
        request: httpx.Request,
        **kwargs: Any,
    ) -> httpx.Response:
        started = time.monotonic()
        response = await original_send(client, request, **kwargs)
        if request.method == "POST" and request.url.path.rstrip("/") == "/v1/responses":
            await response.aread()
            try:
                request_body = json.loads(request.content)
            except (TypeError, ValueError):
                request_body = {}
            try:
                response_body = response.json()
            except ValueError:
                response_body = {}
            usage = response_body.get("usage") if isinstance(response_body, dict) else {}
            provider_calls.append(
                {
                    "requested_model": request_body.get("model"),
                    "actual_model": response_body.get("model"),
                    "http_status": response.status_code,
                    "latency_ms": int((time.monotonic() - started) * 1000),
                    "input_tokens": (usage or {}).get("input_tokens"),
                    "output_tokens": (usage or {}).get("output_tokens"),
                    "total_tokens": (usage or {}).get("total_tokens"),
                }
            )
        return response

    monkeypatch.setattr(httpx.AsyncClient, "send", observed_send)
    results: list[dict[str, Any]] = []
    for case in cases:
        context = _context(seeded, trace_id=f"live-{case['id']}")
        result = await run_commerce_agent(
            context=context,
            user_input=case["message"],
            model=model,
            model_name=model,
            reasoning_effort="high",
            timeout_seconds=90,
        )
        actual_tools = [
            event["tool"]
            for event in result.tool_trace
            if event.get("kind") == "tool_end"
        ]
        tool_arguments = [
            {
                "tool": event["tool"],
                "arguments": event.get("arguments", {}),
            }
            for event in result.tool_trace
            if event.get("kind") == "tool_start"
        ]
        evidence = [
            evidence_row
            for event in result.tool_trace
            if event.get("kind") == "tool_end"
            for evidence_row in event.get("result", {}).get("evidence", [])
        ]
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
                "message": case["message"],
                "expected_tools": case["expected_tools"],
                "actual_tools": actual_tools,
                "tool_match": actual_tools == case["expected_tools"],
                "tool_arguments": tool_arguments,
                "evidence": evidence,
                "final_reply": result.reply.model_dump(mode="json"),
                "unsupported_claims": unsupported_claims,
                "status": result.status,
                "failure_reason": result.failure_reason or None,
                "latency_ms": result.latency_ms,
                "input_tokens": result.input_tokens,
                "output_tokens": result.output_tokens,
                "total_tokens": result.total_tokens,
                "estimated_cost_usd": str(costs["total_cost_usd"]),
                "pricing_version": str(costs["pricing_version"]),
                "sdk_trace_id": result.sdk_trace_id,
            }
        )

    total_input = sum(row["input_tokens"] for row in results)
    total_output = sum(row["output_tokens"] for row in results)
    total_cost = sum(Decimal(row["estimated_cost_usd"]) for row in results)
    summary = {
        "mode": "live",
        "model": model,
        "reasoning_effort": "high",
        "case_count": len(results),
        "completed": sum(row["status"] == "completed" for row in results),
        "exact_tool_matches": sum(row["tool_match"] for row in results),
        "unsupported_claim_count": sum(len(row["unsupported_claims"]) for row in results),
        "failure_count": sum(row["status"] != "completed" for row in results),
        "total_latency_ms": sum(row["latency_ms"] for row in results),
        "total_input_tokens": total_input,
        "total_output_tokens": total_output,
        "total_tokens": total_input + total_output,
        "estimated_cost_usd": str(total_cost),
        "pricing_version": results[0]["pricing_version"],
        "provider_calls": provider_calls,
        "cases": results,
    }
    summary_line = {key: value for key, value in summary.items() if key not in {"cases", "provider_calls"}}
    print(
        "COMMERCE_V2_LIVE_EVAL_SUMMARY="
        + json.dumps(summary_line, ensure_ascii=False, sort_keys=True)
    )
    for case_result in results:
        print(
            "COMMERCE_V2_LIVE_EVAL_CASE="
            + json.dumps(case_result, ensure_ascii=False, sort_keys=True)
        )
    for provider_call in provider_calls:
        print(
            "COMMERCE_V2_LIVE_EVAL_PROVIDER_CALL="
            + json.dumps(provider_call, ensure_ascii=False, sort_keys=True)
        )

    assert len(provider_calls) >= len(cases)
    assert all(call["requested_model"] == model for call in provider_calls)
    assert all(call["http_status"] == 200 for call in provider_calls)
    assert all(
        call["actual_model"] == model
        or str(call["actual_model"] or "").startswith(model + "-")
        for call in provider_calls
    )
    assert all(row["status"] == "completed" for row in results)
    assert all(not row["unsupported_claims"] for row in results)
    assert all(not contains_legacy_marker(row["final_reply"]["text"]) for row in results)

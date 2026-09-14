"""Phase-1 confidence tests for the shadow-only Commerce Agent V2."""
from __future__ import annotations

import asyncio
import json
import os
from dataclasses import dataclass
from decimal import Decimal
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
from agents import AgentOutputSchema, RunConfig
from agents.testing import ModelStep, ScriptedModel, assistant_message, function_call
from agents.tool_context import ToolContext
from agents.tracing import set_trace_provider
from agents.tracing.provider import DefaultTraceProvider
from agents.usage import Usage
from sqlalchemy import JSON, create_engine, event
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import sessionmaker

from models import (
    Base,
    Conversation,
    ConversationHistorySummary,
    Customer,
    HandoffSession,
    MerchantKnowledgeSection,
    MerchantKnowledgeSectionProduct,
    MessageEvent,
    Product,
    Tenant,
    WhatsAppConnection,
)
from core.admin_conversation_reset import (
    ConversationContextResetError,
    reset_conversation_context,
)
from modules.ai.commerce_agent_v2.agent import COMMERCE_AGENT_INSTRUCTIONS, build_commerce_agent
from modules.ai.commerce_agent_v2.context import CommerceAgentContext, CommerceContextError
from modules.ai.commerce_agent_v2.guardrails import contains_legacy_marker, validate_grounded_reply
from modules.ai.commerce_agent_v2.output import (
    CatalogSearchResult,
    CanonicalEvidenceFact,
    CommerceReply,
    EvidenceRecord,
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
from modules.ai.commerce_agent_v2.tools.catalog import _catalog_search_enabled
from modules.ai.commerce_agent_v2.tools.knowledge import (
    _merchant_knowledge_enabled,
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
            "currency": "SAR",
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


def _context(
    seed: Seed,
    *,
    trace_id: str = "wamid-current",
    conversation: Conversation | None = None,
) -> CommerceAgentContext:
    selected_conversation = conversation or seed.conversation_a
    return CommerceAgentContext.from_trusted_scope(
        db=seed.db,
        tenant_id=seed.tenant_a.id,
        conversation_id=selected_conversation.id,
        customer_id=seed.customer_a.id,
        normalized_customer_phone=seed.customer_a.normalized_phone,
        connection_id=str(seed.connection_a.id),
        inbound_trace_id=trace_id,
    )


def _case_context(seed: Seed, case: dict[str, Any], *, mode: str) -> CommerceAgentContext:
    conversation = Conversation(
        tenant_id=seed.tenant_a.id,
        customer_id=seed.customer_a.id,
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
        trace_id=f"{mode}-{case['id']}",
        conversation=conversation,
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
        "resolve_customer_order",
        "get_order_details",
        "get_order_shipment",
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
    assert result["status"] == "error"
    assert result["failure_reason"].startswith(f"tool_error:{tool.name}:")
    assert "TenantIsolationViolation" in result["failure_reason"]


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
    facts = {fact.kind: fact for fact in details.evidence[0].facts}
    assert facts["product_name"].value == "عسل طلح بلدي"
    assert facts["price"].value == 150
    assert facts["currency"].value == "SAR"
    assert facts["availability"].value is True
    assert facts["stock_quantity"].value == 8
    assert facts["product_url"].value == details.product.product_url
    assert facts["image_url"].value == details.product.image_url


@pytest.mark.asyncio
async def test_sale_and_regular_prices_are_numeric_canonical_facts(seeded: Seed) -> None:
    seeded.honey_a.extra_metadata = {
        **seeded.honey_a.extra_metadata,
        "sale_price": "125.00",
        "regular_price": "150,00",
    }
    seeded.db.commit()
    result = CatalogSearchResult.model_validate(
        await _invoke(search_products, _context(seeded), {"query": "عسل طلح", "limit": 5})
    )
    facts = {fact.kind: fact.value for fact in result.evidence[0].facts}
    assert facts["sale_price"] == 125
    assert facts["regular_price"] == 150


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
    assert result["status"] == "error"
    assert result["failure_reason"] == (
        "tool_error:get_product_details:TenantIsolationViolation"
    )


@pytest.mark.asyncio
async def test_global_and_product_linked_knowledge_are_separated(seeded: Seed) -> None:
    context = _context(seeded)
    global_result = KnowledgeSearchResult.model_validate(
        await _invoke(search_merchant_knowledge, context, {"query": "تغليف هدايا", "limit": 4})
    )
    assert global_result.status == "ok"
    assert [item.section_id for item in global_result.sections] == [seeded.global_kb_a.id]
    assert not global_result.sections[0].linked_product_ids
    assert [(fact.kind, fact.subject_product_id) for fact in global_result.evidence[0].facts] == [
        ("merchant_knowledge", None)
    ]

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
    assert [
        (fact.kind, fact.subject_product_id) for fact in product_result.evidence[0].facts
    ] == [("product_knowledge", seeded.honey_a.id)]


def test_merchant_knowledge_availability_requires_relevant_global_evidence(
    seeded: Seed,
) -> None:
    wrapper = SimpleNamespace(context=_context(seeded))

    wrapper.context.bind_run_user_input("وش مصدر عسل الطلح؟")
    assert _merchant_knowledge_enabled(wrapper, None) is False

    wrapper.context.bind_run_user_input("وش سنة قطف هذا العسل؟")
    assert _merchant_knowledge_enabled(wrapper, None) is False

    wrapper.context.bind_run_user_input("هل عندكم تغليف هدايا؟")
    assert _merchant_knowledge_enabled(wrapper, None) is True


def test_merchant_knowledge_remains_available_for_product_plus_global_policy(
    seeded: Seed,
) -> None:
    context = _context(seeded)
    context.bind_run_user_input("هل هذا المنتج يشمله التغليف المجاني؟")
    wrapper = SimpleNamespace(context=context)

    context.authorize_products([seeded.honey_a.id])
    context.register_evidence(
        [
            EvidenceRecord(
                ref=f"catalog:product:{seeded.honey_a.id}",
                source="catalog_product",
                source_id=str(seeded.honey_a.id),
            )
        ]
    )

    assert _merchant_knowledge_enabled(wrapper, None) is True


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
async def test_catalog_search_allows_one_reformulation_then_disables_after_two_misses(
    seeded: Seed,
) -> None:
    context = _context(seeded, trace_id="bounded-catalog-misses")
    final = CommerceReply(
        text="لم أجد منتجًا مطابقًا ضمن نتائج الكتالوج.",
        safe_fallback_reason="no_catalog_product_matched_after_reformulation",
    )

    def after_two_misses(call: Any) -> ModelStep:
        assert {tool.name for tool in call.tools} == set()
        return ModelStep(output=[assistant_message(final.model_dump_json())])

    model = ScriptedModel(
        [
            ModelStep(
                output=[
                    function_call(
                        "search_products",
                        {"query": "عطر بإصدار محدود", "limit": 5},
                        call_id="catalog-miss-1",
                    )
                ]
            ),
            ModelStep(
                output=[
                    function_call(
                        "search_products",
                        {"query": "إصدار محدود", "limit": 5},
                        call_id="catalog-miss-2",
                    )
                ]
            ),
            ModelStep.respond(after_two_misses),
        ]
    )
    result = await run_commerce_agent(
        context=context,
        user_input="هل لديكم عطر بإصدار محدود؟",
        model=model,
        model_name="bounded-search-eval",
    )

    assert result.status == "completed"
    assert result.reply.safe_fallback_reason
    assert [
        event["tool"]
        for event in result.tool_trace
        if event.get("kind") == "tool_end"
    ] == ["search_products", "search_products"]
    assert context.consecutive_catalog_misses == 2
    model.assert_complete()


@pytest.mark.asyncio
async def test_successful_catalog_search_resets_consecutive_miss_budget(seeded: Seed) -> None:
    context = _context(seeded, trace_id="catalog-miss-reset")
    wrapper = SimpleNamespace(context=context)
    await _invoke(search_products, context, {"query": "منتج مفقود", "limit": 5})
    assert context.consecutive_catalog_misses == 1
    assert _catalog_search_enabled(wrapper, None) is True

    found = CatalogSearchResult.model_validate(
        await _invoke(search_products, context, {"query": "عسل طلح", "limit": 5})
    )
    assert found.status == "ok"
    assert context.consecutive_catalog_misses == 0
    assert _catalog_search_enabled(wrapper, None) is True


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


@pytest.mark.asyncio
async def test_session_history_window_is_applied_in_sql(seeded: Seed) -> None:
    statements: list[str] = []

    def capture(_conn, _cursor, statement, _parameters, _context, _executemany):
        if "message_events" in statement.lower():
            statements.append(statement.lower())

    engine = seeded.db.get_bind()
    event.listen(engine, "before_cursor_execute", capture)
    try:
        await ConversationMessageSession(_context(seeded)).get_items(limit=2)
    finally:
        event.remove(engine, "before_cursor_execute", capture)

    assert statements
    assert "limit" in statements[-1]


def test_structured_reply_validation_and_grounding(seeded: Seed) -> None:
    context = _context(seeded)
    record = {
        "ref": f"catalog:product:{seeded.honey_a.id}",
        "source": "catalog_product",
        "source_id": str(seeded.honey_a.id),
        "facts": [
            {
                "kind": "product_name",
                "value": "عسل طلح بلدي",
                "subject_product_id": seeded.honey_a.id,
            },
            {
                "kind": "price",
                "value": 150,
                "subject_product_id": seeded.honey_a.id,
            },
        ],
        "fields": {"title": "عسل طلح بلدي", "price": "150"},
        "provenance": {"service": "test"},
    }
    context.register_evidence([EvidenceRecord.model_validate(record)])
    reply = CommerceReply(
        text="عسل طلح بلدي سعره 150 ريال.",
        evidence_refs=[record["ref"]],
        fact_claims=[
            FactClaim(
                kind="product_name",
                value="عسل طلح بلدي",
                evidence_ref=record["ref"],
                subject_product_id=seeded.honey_a.id,
                text_span="عسل طلح بلدي",
            ),
            FactClaim(
                kind="price",
                value=150,
                evidence_ref=record["ref"],
                subject_product_id=seeded.honey_a.id,
                text_span="سعره 150 ريال",
            ),
        ],
        product_refs=[ProductReference(product_id=seeded.honey_a.id, evidence_ref=record["ref"])],
    )
    assert validate_grounded_reply(context, reply) == []
    invented = reply.model_copy(update={"text": "سعره 9999 ريال."})
    assert "claim_span_not_in_text:price" in validate_grounded_reply(context, invented)
    assert "price_in_text_without_verified_claim" in validate_grounded_reply(context, invented)
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


def test_canonical_catalog_claims_accept_formatting_and_natural_arabic(seeded: Seed) -> None:
    context = _context(seeded)
    ref = f"catalog:product:{seeded.honey_a.id}"
    context.register_evidence(
        [
            EvidenceRecord(
                ref=ref,
                source="catalog_product",
                source_id=str(seeded.honey_a.id),
                facts=[
                    CanonicalEvidenceFact(
                        kind="product_name",
                        value="عسل طلح بلدي",
                        subject_product_id=seeded.honey_a.id,
                    ),
                    CanonicalEvidenceFact(
                        kind="price", value=150.00, subject_product_id=seeded.honey_a.id
                    ),
                    CanonicalEvidenceFact(
                        kind="currency", value="SAR", subject_product_id=seeded.honey_a.id
                    ),
                    CanonicalEvidenceFact(
                        kind="availability", value=True, subject_product_id=seeded.honey_a.id
                    ),
                    CanonicalEvidenceFact(
                        kind="stock_quantity", value=8, subject_product_id=seeded.honey_a.id
                    ),
                ],
                fields={"title": "عسل طلح بلدي", "price": "150", "in_stock": True},
            )
        ]
    )
    reply = CommerceReply(
        text="عسل طلح بلدي متوفر، وباقي منه 8 عبوات. سعره 150 ريال.",
        evidence_refs=[ref],
        fact_claims=[
            FactClaim(
                kind="product_name",
                value="عسل طلح بلدي",
                evidence_ref=ref,
                subject_product_id=seeded.honey_a.id,
                text_span="عسل طلح بلدي",
            ),
            FactClaim(
                kind="availability",
                value=True,
                evidence_ref=ref,
                subject_product_id=seeded.honey_a.id,
                text_span="متوفر",
            ),
            FactClaim(
                kind="stock_quantity",
                value=8,
                evidence_ref=ref,
                subject_product_id=seeded.honey_a.id,
                text_span="باقي منه 8 عبوات",
            ),
            FactClaim(
                kind="price",
                value=150,
                evidence_ref=ref,
                subject_product_id=seeded.honey_a.id,
                text_span="سعره 150 ريال",
            ),
            FactClaim(
                kind="currency",
                value="ريال",
                evidence_ref=ref,
                subject_product_id=seeded.honey_a.id,
                text_span="150 ريال",
            ),
        ],
    )
    assert validate_grounded_reply(context, reply) == []

    decimal_rendering = reply.model_copy(
        update={
            "text": reply.text.replace("150 ريال", "150.00 ريال"),
            "fact_claims": [
                claim.model_copy(
                    update={"text_span": claim.text_span.replace("150 ريال", "150.00 ريال")}
                )
                for claim in reply.fact_claims
            ],
        }
    )
    assert validate_grounded_reply(context, decimal_rendering) == []

    compact_spans = CommerceReply(
        text="عسل طلح بلدي متوفرة، والكمية 8 عبوات، والسعر 150 ر.س.",
        evidence_refs=[ref],
        fact_claims=[
            reply.fact_claims[0],
            reply.fact_claims[1].model_copy(
                update={"text_span": "متوفرة"}
            ),
            reply.fact_claims[2].model_copy(update={"text_span": "8"}),
            reply.fact_claims[3].model_copy(update={"text_span": "150"}),
            reply.fact_claims[4].model_copy(update={"text_span": "ر.س"}),
        ],
    )
    assert validate_grounded_reply(context, compact_spans) == []

    quantity_bound_availability = CommerceReply(
        text=(
            "نعم، متوفر لدينا عسل طلح بلدي. "
            "المتاح حاليًا 8 عبوات."
        ),
        evidence_refs=[ref],
        fact_claims=[
            reply.fact_claims[0],
            reply.fact_claims[1].model_copy(update={"text_span": "متوفر لدينا"}),
            reply.fact_claims[2].model_copy(update={"text_span": "8 عبوات"}),
        ],
    )
    assert validate_grounded_reply(context, quantity_bound_availability) == []

    wrong_quantity = quantity_bound_availability.model_copy(
        update={"text": "نعم، متوفر لدينا عسل طلح بلدي. المتاح حاليًا 9 عبوات."}
    )
    wrong_quantity_errors = validate_grounded_reply(context, wrong_quantity)
    assert "stock_quantity_in_text_without_verified_claim" in wrong_quantity_errors
    assert "availability_in_text_without_verified_claim" in wrong_quantity_errors

    negative_availability = quantity_bound_availability.model_copy(
        update={"text": "نعم، متوفر لدينا عسل طلح بلدي. غير متاح حاليًا 8 عبوات."}
    )
    assert "availability_in_text_without_verified_claim" in validate_grounded_reply(
        context, negative_availability
    )

    plural_availability = compact_spans.model_copy(
        update={
            "text": compact_spans.text.replace("متوفرة", "جاهزة للطلب"),
            "fact_claims": [
                claim.model_copy(update={"text_span": "جاهزة للطلب"})
                if claim.kind == "availability"
                else claim
                for claim in compact_spans.fact_claims
            ],
        }
    )
    assert validate_grounded_reply(context, plural_availability) == []

    grounded_upper_bound = CommerceReply(
        text="سعره 150 ريال، لذلك هو أقل من 200 ريال.",
        evidence_refs=[ref],
        fact_claims=[
            reply.fact_claims[3].model_copy(update={"text_span": "150 ريال"}),
            reply.fact_claims[4].model_copy(update={"text_span": "150 ريال"}),
        ],
    )
    assert validate_grounded_reply(context, grounded_upper_bound) == []

    false_upper_bound = grounded_upper_bound.model_copy(
        update={"text": "سعره 150 ريال، لذلك هو أقل من 100 ريال."}
    )
    assert "price_in_text_without_verified_claim" in validate_grounded_reply(
        context, false_upper_bound
    )


def test_description_claim_accepts_supported_shorter_natural_span(seeded: Seed) -> None:
    context = _context(seeded)
    ref = f"catalog:product:{seeded.honey_a.id}"
    context.register_evidence(
        [
            EvidenceRecord(
                ref=ref,
                source="catalog_product",
                source_id=str(seeded.honey_a.id),
                facts=[
                    CanonicalEvidenceFact(
                        kind="description",
                        value="عبوة 500 جرام",
                        subject_product_id=seeded.honey_a.id,
                    )
                ],
            )
        ]
    )
    assert validate_grounded_reply(
        context,
        CommerceReply(
            text="وزنها 500 جرام.",
            evidence_refs=[ref],
            fact_claims=[
                FactClaim(
                    kind="description",
                    value="عبوة 500 جرام",
                    evidence_ref=ref,
                    subject_product_id=seeded.honey_a.id,
                    text_span="وزنها 500 جرام",
                )
            ],
        ),
    ) == []

    unsupported = CommerceReply(
        text="وزنها 500 جرام ومستوردة.",
        evidence_refs=[ref],
        fact_claims=[
            FactClaim(
                kind="description",
                value="عبوة 500 جرام",
                evidence_ref=ref,
                subject_product_id=seeded.honey_a.id,
                text_span="وزنها 500 جرام ومستوردة",
            )
        ],
    )
    assert "claim_span_not_equivalent:description" in validate_grounded_reply(
        context, unsupported
    )


def test_quantity_bound_availability_requires_same_product_evidence(seeded: Seed) -> None:
    context = _context(seeded)
    honey_ref = f"catalog:product:{seeded.honey_a.id}"
    gift_ref = f"catalog:product:{seeded.gift_a.id}"
    context.register_evidence(
        [
            EvidenceRecord(
                ref=honey_ref,
                source="catalog_product",
                source_id=str(seeded.honey_a.id),
                facts=[
                    CanonicalEvidenceFact(
                        kind="availability",
                        value=True,
                        subject_product_id=seeded.honey_a.id,
                    )
                ],
            ),
            EvidenceRecord(
                ref=gift_ref,
                source="catalog_product",
                source_id=str(seeded.gift_a.id),
                facts=[
                    CanonicalEvidenceFact(
                        kind="stock_quantity",
                        value=8,
                        subject_product_id=seeded.gift_a.id,
                    )
                ],
            ),
        ]
    )
    reply = CommerceReply(
        text="عسل الطلح متوفر لدينا. المتاح حاليًا 8 عبوات.",
        evidence_refs=[honey_ref, gift_ref],
        fact_claims=[
            FactClaim(
                kind="availability",
                value=True,
                evidence_ref=honey_ref,
                subject_product_id=seeded.honey_a.id,
                text_span="متوفر لدينا",
            ),
            FactClaim(
                kind="stock_quantity",
                value=8,
                evidence_ref=gift_ref,
                subject_product_id=seeded.gift_a.id,
                text_span="8 عبوات",
            ),
        ],
    )
    assert "availability_in_text_without_verified_claim" in validate_grounded_reply(
        context, reply
    )


def test_structured_ui_and_media_can_render_verified_urls_without_raw_text_url(
    seeded: Seed,
) -> None:
    context = _context(seeded)
    ref = f"catalog:product:{seeded.honey_a.id}"
    product_url = "https://shop.example.test/products/a-honey"
    image_url = "https://cdn.example.test/a-honey.jpg"
    context.register_evidence(
        [
            EvidenceRecord(
                ref=ref,
                source="catalog_product",
                source_id=str(seeded.honey_a.id),
                facts=[
                    CanonicalEvidenceFact(
                        kind="product_url",
                        value=product_url,
                        subject_product_id=seeded.honey_a.id,
                    ),
                    CanonicalEvidenceFact(
                        kind="image_url",
                        value=image_url,
                        subject_product_id=seeded.honey_a.id,
                    ),
                ],
            )
        ]
    )
    reply = CommerceReply(
        text="تقدر تفتح صفحة المنتج وتشوف صورته.",
        evidence_refs=[ref],
        fact_claims=[
            FactClaim(
                kind="product_url",
                value=product_url,
                evidence_ref=ref,
                subject_product_id=seeded.honey_a.id,
                text_span=None,
            ),
            FactClaim(
                kind="image_url",
                value=image_url,
                evidence_ref=ref,
                subject_product_id=seeded.honey_a.id,
                text_span=None,
            ),
        ],
        media_refs=[MediaReference(url=image_url, evidence_ref=ref)],
        ui_actions=[
            UIAction(
                kind="open_product",
                label="عرض المنتج",
                url=product_url,
                evidence_ref=ref,
            )
        ],
    )
    assert validate_grounded_reply(context, reply) == []


def test_unicode_product_url_representation_is_grounded_after_canonicalization(
    seeded: Seed,
) -> None:
    context = _context(seeded)
    ref = f"catalog:product:{seeded.honey_a.id}"
    raw_product_url = (
        "https://demostore.salla.sa/dev-cgcaqkpx5wgewsyv/"
        "فستان/p398551325"
    )
    context.register_evidence(
        [
            EvidenceRecord(
                ref=ref,
                source="catalog_product",
                source_id=str(seeded.honey_a.id),
                facts=[
                    CanonicalEvidenceFact(
                        kind="product_url",
                        value=raw_product_url,
                        subject_product_id=seeded.honey_a.id,
                    )
                ],
            )
        ]
    )
    reply = CommerceReply(
        text="تقدر تفتح صفحة المنتج.",
        evidence_refs=[ref],
        fact_claims=[
            FactClaim(
                kind="product_url",
                value=raw_product_url,
                evidence_ref=ref,
                subject_product_id=seeded.honey_a.id,
                text_span=None,
            )
        ],
        ui_actions=[
            UIAction(
                kind="open_product",
                label="عرض المنتج",
                url=raw_product_url,
                evidence_ref=ref,
            )
        ],
    )

    assert str(reply.ui_actions[0].url) == (
        "https://demostore.salla.sa/dev-cgcaqkpx5wgewsyv/"
        "%D9%81%D8%B3%D8%AA%D8%A7%D9%86/p398551325"
    )
    assert validate_grounded_reply(context, reply) == []


def test_canonical_claim_types_reject_string_prices() -> None:
    with pytest.raises(Exception, match="price must be a JSON number"):
        FactClaim(
            kind="price",
            value="150",
            evidence_ref="catalog:product:1",
            subject_product_id=1,
            text_span="150 ريال",
        )


def test_knowledge_claim_allows_supported_paraphrase_but_rejects_changed_fact(
    seeded: Seed,
) -> None:
    context = _context(seeded)
    ref = f"kb:section:{seeded.product_kb_a.id}"
    body = "مصدر هذا المنتج من خلايا نحل بلدي."
    context.register_evidence(
        [
            EvidenceRecord(
                ref=ref,
                source="product_knowledge",
                source_id=str(seeded.product_kb_a.id),
                facts=[
                    CanonicalEvidenceFact(
                        kind="product_knowledge",
                        value=body,
                        subject_product_id=seeded.honey_a.id,
                    )
                ],
                fields={"title": "مصدر عسل الطلح", "body": body},
            )
        ]
    )
    claim = FactClaim(
        kind="product_knowledge",
        value=body,
        evidence_ref=ref,
        subject_product_id=seeded.honey_a.id,
        text_span="هذا العسل مصدره نحل بلدي",
    )
    assert validate_grounded_reply(
        context,
        CommerceReply(
            text="هذا العسل مصدره نحل بلدي.",
            evidence_refs=[ref],
            fact_claims=[claim],
            product_refs=[
                ProductReference(product_id=seeded.honey_a.id, evidence_ref=ref)
            ],
        ),
    ) == []

    informational_availability = claim.model_copy(
        update={"text_span": "مصدر هذا المنتج من خلايا نحل بلدي"}
    )
    assert validate_grounded_reply(
        context,
        CommerceReply(
            text="المتوفر لدينا أن مصدر هذا المنتج من خلايا نحل بلدي.",
            evidence_refs=[ref],
            fact_claims=[informational_availability],
        ),
    ) == []

    assert "availability_in_text_without_verified_claim" in validate_grounded_reply(
        context,
        CommerceReply(
            text="هذا المنتج متوفر.",
        ),
    )

    wrong = claim.model_copy(update={"text_span": "هذا العسل مستورد من نيوزيلندا"})
    errors = validate_grounded_reply(
        context,
        CommerceReply(
            text="هذا العسل مستورد من نيوزيلندا.",
            evidence_refs=[ref],
            fact_claims=[wrong],
        ),
    )
    assert "claim_span_not_equivalent:product_knowledge" in errors

    assert "invalid_product_reference" in validate_grounded_reply(
        context,
        CommerceReply(
            text="هذا العسل مصدره نحل بلدي.",
            evidence_refs=[ref],
            fact_claims=[claim],
            product_refs=[
                ProductReference(product_id=seeded.gift_a.id, evidence_ref=ref)
            ],
        ),
    )


def test_merchant_knowledge_claim_uses_canonical_body_with_natural_paraphrase(
    seeded: Seed,
) -> None:
    context = _context(seeded)
    ref = f"kb:section:{seeded.global_kb_a.id}"
    body = "يتوفر تغليف هدايا بسيط للمنتجات المختارة."
    context.register_evidence(
        [
            EvidenceRecord(
                ref=ref,
                source="merchant_knowledge",
                source_id=str(seeded.global_kb_a.id),
                facts=[CanonicalEvidenceFact(kind="merchant_knowledge", value=body)],
                fields={"title": "تغليف الهدايا", "body": body},
            )
        ]
    )
    supported = FactClaim(
        kind="merchant_knowledge",
        value=body,
        evidence_ref=ref,
        subject_product_id=None,
        text_span="عندنا تغليف هدايا بسيط للمنتجات المختارة",
    )
    assert validate_grounded_reply(
        context,
        CommerceReply(
            text="عندنا تغليف هدايا بسيط للمنتجات المختارة.",
            evidence_refs=[ref],
            fact_claims=[supported],
        ),
    ) == []

    unsupported = supported.model_copy(update={"text_span": "التغليف مجاني لكل المنتجات"})
    assert "claim_span_not_equivalent:merchant_knowledge" in validate_grounded_reply(
        context,
        CommerceReply(
            text="التغليف مجاني لكل المنتجات.",
            evidence_refs=[ref],
            fact_claims=[unsupported],
        ),
    )

    dropped_scope = supported.model_copy(update={"text_span": "عندنا تغليف هدايا للمنتجات"})
    assert "claim_span_not_equivalent:merchant_knowledge" in validate_grounded_reply(
        context,
        CommerceReply(
            text="عندنا تغليف هدايا للمنتجات.",
            evidence_refs=[ref],
            fact_claims=[dropped_scope],
        ),
    )


def test_claim_rejects_wrong_evidence_ref_and_other_product_subject(seeded: Seed) -> None:
    context = _context(seeded)
    ref = f"catalog:product:{seeded.honey_a.id}"
    context.register_evidence(
        [
            EvidenceRecord(
                ref=ref,
                source="catalog_product",
                source_id=str(seeded.honey_a.id),
                facts=[
                    CanonicalEvidenceFact(
                        kind="price", value=150, subject_product_id=seeded.honey_a.id
                    )
                ],
            )
        ]
    )
    wrong_ref = CommerceReply(
        text="سعره 150 ريال.",
        evidence_refs=["catalog:product:missing"],
        fact_claims=[
            FactClaim(
                kind="price",
                value=150,
                evidence_ref="catalog:product:missing",
                subject_product_id=seeded.honey_a.id,
                text_span="سعره 150 ريال",
            )
        ],
    )
    assert "unknown_evidence_refs:catalog:product:missing" in validate_grounded_reply(
        context, wrong_ref
    )

    wrong_product = CommerceReply(
        text="سعره 150 ريال.",
        evidence_refs=[ref],
        fact_claims=[
            FactClaim(
                kind="price",
                value=150,
                evidence_ref=ref,
                subject_product_id=seeded.gift_a.id,
                text_span="سعره 150 ريال",
            )
        ],
    )
    assert "claim_not_in_evidence:price" in validate_grounded_reply(context, wrong_product)


def test_sensitive_price_in_text_requires_verified_fact_claim(seeded: Seed) -> None:
    context = _context(seeded)
    ref = f"catalog:product:{seeded.honey_a.id}"
    context.register_evidence(
        [
            EvidenceRecord(
                ref=ref,
                source="catalog_product",
                source_id=str(seeded.honey_a.id),
                facts=[
                    CanonicalEvidenceFact(
                        kind="price", value=150, subject_product_id=seeded.honey_a.id
                    )
                ],
            )
        ]
    )
    errors = validate_grounded_reply(
        context,
        CommerceReply(text="سعره 150 ريال.", evidence_refs=[ref]),
    )
    assert "price_in_text_without_verified_claim" in errors


def test_v2_output_rejects_legacy_markers() -> None:
    for marker in ("[PRODUCT:1]", "[MEDIA_KEY:x]", "[CALL:foo]"):
        assert contains_legacy_marker(marker)


def test_structured_output_schema_uses_provider_compatible_validated_urls() -> None:
    schema_text = json.dumps(CommerceReply.model_json_schema(), sort_keys=True)
    assert '"format": "uri"' not in schema_text
    strict_schema = AgentOutputSchema(CommerceReply, strict_json_schema=True).json_schema()
    assert strict_schema["additionalProperties"] is False
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
            FactClaim(
                kind="product_name",
                value="عسل طلح بلدي",
                evidence_ref=evidence_ref,
                subject_product_id=seeded.honey_a.id,
                text_span="عسل طلح بلدي",
            ),
            FactClaim(
                kind="price",
                value=150,
                evidence_ref=evidence_ref,
                subject_product_id=seeded.honey_a.id,
                text_span="بسعر 150 ريال",
            ),
            FactClaim(
                kind="availability",
                value=True,
                evidence_ref=evidence_ref,
                subject_product_id=seeded.honey_a.id,
                text_span="متوفر",
            ),
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
    provider_events = [
        event for event in result.tool_trace if event.get("kind") == "model_end"
    ]
    assert [event["usage"]["total_tokens"] for event in provider_events] == [28, 45]
    assert all(event["provider_response_received"] for event in provider_events)
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
    assert timed_out.failure_reason == "run_deadline_exceeded"

    model_timed_out = await run_commerce_agent(
        context=_context(seeded, trace_id="model-timeout"),
        user_input="test",
        model=ScriptedModel([ModelStep.respond(slow)]),
        model_name="slow-provider",
        model_timeout_seconds=0.01,
        run_deadline_seconds=1,
        execution_mode="outbound",
    )
    assert model_timed_out.status == "failed"
    assert model_timed_out.failure_reason == "model_timeout:attempt_1"
    assert not any(
        event.get("kind") == "model_retry_decision" and event.get("retry") is True
        for event in model_timed_out.tool_trace
    )

    malformed = await run_commerce_agent(
        context=_context(seeded, trace_id="malformed"),
        user_input="test",
        model=ScriptedModel([[assistant_message('{"text": 123}')]]),
        model_name="malformed-provider",
    )
    assert malformed.status == "failed"
    assert malformed.reply.safe_fallback_reason


@pytest.mark.asyncio
async def test_cached_input_usage_is_exposed_without_prompt_changes(seeded: Seed) -> None:
    reply = CommerceReply(text="حياك الله.", response_mode="social")
    usage = Usage(
        requests=1,
        input_tokens=40,
        input_tokens_details={"cached_tokens": 30, "cache_write_tokens": 0},
        output_tokens=5,
        total_tokens=45,
    )
    result = await run_commerce_agent(
        context=_context(seeded, trace_id="cached-input"),
        user_input="مرحبا",
        model=ScriptedModel([ModelStep(output=[assistant_message(reply.model_dump_json())], usage=usage)]),
        model_name="cached-usage-eval",
    )

    assert result.status == "completed"
    assert result.cached_input_tokens == 30
    summary = next(event for event in result.tool_trace if event["kind"] == "usage_summary")
    assert summary["cache_percentage"] == 75.0


@pytest.mark.asyncio
async def test_transient_model_error_is_retried_once_by_runner(seeded: Seed) -> None:
    class TransientProviderError(RuntimeError):
        status_code = 429

    reply = CommerceReply(text="حياك الله.", response_mode="social")
    model = ScriptedModel(
        [
            ModelStep(error=TransientProviderError("sensitive provider detail")),
            ModelStep(output=[assistant_message(reply.model_dump_json())]),
        ]
    )
    result = await run_commerce_agent(
        context=_context(seeded, trace_id="retry-429"),
        user_input="مرحبا",
        model=model,
        model_name="retry-eval",
        execution_mode="outbound",
    )

    assert result.status == "completed"
    assert sum(event.get("kind") == "model_start" for event in result.tool_trace) == 1
    retry = next(
        event for event in result.tool_trace if event.get("kind") == "model_retry_decision"
    )
    assert retry["retry"] is True
    assert retry["status_code"] == 429
    assert retry["latency_ms"] >= 0
    completed_attempt = next(
        event for event in result.tool_trace if event.get("kind") == "model_end"
    )
    assert completed_attempt["model_attempt"] == 2
    assert "sensitive provider detail" not in json.dumps(result.tool_trace)
    model.assert_complete()


@pytest.mark.asyncio
async def test_failed_output_guardrail_is_visible_in_run_result(seeded: Seed) -> None:
    result = await run_commerce_agent(
        context=_context(seeded, trace_id="guardrail-failure"),
        user_input="اخترع منتجًا",
        model=ScriptedModel([[assistant_message(CommerceReply(text="منتج مؤكد").model_dump_json())]]),
        model_name="guardrail-eval",
    )
    assert result.status == "failed"
    assert result.failure_reason == (
        "output_guardrail_tripwire:reply_without_tool_evidence_or_safe_fallback"
    )
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
async def test_rejected_url_output_preserves_safe_diagnostic_and_customer_fallback(
    seeded: Seed,
) -> None:
    context = _context(seeded, trace_id="rejected-url-diagnostic")
    ref = f"catalog:product:{seeded.honey_a.id}"
    evidence_url = "https://shop.example.test/products/a-honey"
    invented_url = "https://shop.example.test/products/invented"
    context.register_evidence(
        [
            EvidenceRecord(
                ref=ref,
                source="catalog_product",
                source_id=str(seeded.honey_a.id),
                facts=[
                    CanonicalEvidenceFact(
                        kind="product_url",
                        value=evidence_url,
                        subject_product_id=seeded.honey_a.id,
                    )
                ],
            )
        ]
    )
    rejected = CommerceReply(
        text="افتح صفحة المنتج من الزر.",
        evidence_refs=[ref],
        fact_claims=[
            FactClaim(
                kind="product_url",
                value=evidence_url,
                evidence_ref=ref,
                subject_product_id=seeded.honey_a.id,
                text_span=None,
            )
        ],
        ui_actions=[
            UIAction(
                kind="open_product",
                label="عرض المنتج",
                url=invented_url,
                evidence_ref=ref,
            )
        ],
    )

    result = await run_commerce_agent(
        context=context,
        user_input="أرسل رابط المنتج",
        model=ScriptedModel([[assistant_message(rejected.model_dump_json())]]),
        model_name="guardrail-url-diagnostic",
    )

    assert result.status == "failed"
    assert result.failure_reason == "output_guardrail_tripwire:action_url_not_in_evidence"
    assert result.reply.safe_fallback_reason == result.failure_reason
    assert result.reply.text == "لا تتوفر لدي معلومة موثوقة كافية للإجابة الآن."
    output_info = result.guardrail_results[0]["output_info"]
    diagnostic = output_info["rejected_output_diagnostic"]
    assert diagnostic["artifact"] == "rejected_model_commerce_reply"
    assert diagnostic["customer_delivered"] is False
    assert diagnostic["customer_fallback_artifact"] == "safe_fallback_reply"
    assert "action_url_not_in_evidence" in diagnostic["guardrail_error_codes"]
    action_comparison = next(
        item
        for item in diagnostic["url_comparisons"]
        if item["comparison_stage"] == "ui_action_to_evidence"
    )
    assert action_comparison["canonical_equal"] is False
    assert action_comparison["evidence_ref_exists"] is True
    assert action_comparison["subject_binding_matches"] is True
    persisted_shape = json.dumps(result.guardrail_results, ensure_ascii=False)
    assert evidence_url not in persisted_shape
    assert invented_url not in persisted_shape
    assert rejected.text not in persisted_shape

    from models import CommerceAgentV2ShadowRun

    _persist_shadow_result(seeded.db, context, result)
    row = (
        seeded.db.query(CommerceAgentV2ShadowRun)
        .filter(CommerceAgentV2ShadowRun.sdk_trace_id == result.sdk_trace_id)
        .one()
    )
    assert row.structured_output["text"] == result.reply.text
    assert row.structured_output["safe_fallback_reason"] == result.failure_reason
    stored_diagnostic = row.guardrail_results[0]["output_info"][
        "rejected_output_diagnostic"
    ]
    assert stored_diagnostic == diagnostic
    assert stored_diagnostic["artifact"] == "rejected_model_commerce_reply"


@pytest.mark.asyncio
async def test_rejected_availability_output_preserves_safe_semantic_diagnostic(
    seeded: Seed,
) -> None:
    context = _context(seeded, trace_id="rejected-availability-diagnostic")
    ref = f"catalog:product:{seeded.honey_a.id}"
    context.register_evidence(
        [
            EvidenceRecord(
                ref=ref,
                source="catalog_product",
                source_id=str(seeded.honey_a.id),
                facts=[
                    CanonicalEvidenceFact(
                        kind="availability",
                        value=True,
                        subject_product_id=seeded.honey_a.id,
                    )
                ],
            )
        ]
    )
    rejected = CommerceReply(
        text="هذا المنتج غير متوفر.",
        evidence_refs=[ref],
    )

    result = await run_commerce_agent(
        context=context,
        user_input="هل المنتج متوفر؟",
        model=ScriptedModel([[assistant_message(rejected.model_dump_json())]]),
        model_name="guardrail-availability-diagnostic",
    )

    assert result.status == "failed"
    assert result.failure_reason == (
        "output_guardrail_tripwire:availability_in_text_without_verified_claim"
    )
    assert result.reply.safe_fallback_reason == result.failure_reason
    output_info = result.guardrail_results[0]["output_info"]
    diagnostic = output_info["rejected_output_diagnostic"]
    lexical = diagnostic["lexical_commercial_diagnostics"]
    assert diagnostic["customer_delivered"] is False
    assert len(lexical) == 1
    assert lexical[0]["semantic_scope"] == "PRODUCT"
    assert lexical[0]["matched_normalized_lexeme"] == "غير متوفر"
    assert lexical[0]["referenced_evidence_source_types"] == ["catalog_product"]
    assert lexical[0]["product_subject_present"] is True
    persisted_shape = json.dumps(result.guardrail_results, ensure_ascii=False)
    assert rejected.text not in persisted_shape
    assert seeded.customer_a.normalized_phone not in persisted_shape

    from models import CommerceAgentV2ShadowRun

    _persist_shadow_result(seeded.db, context, result)
    row = (
        seeded.db.query(CommerceAgentV2ShadowRun)
        .filter(CommerceAgentV2ShadowRun.sdk_trace_id == result.sdk_trace_id)
        .one()
    )
    stored_diagnostic = row.guardrail_results[0]["output_info"][
        "rejected_output_diagnostic"
    ]
    assert stored_diagnostic == diagnostic
    assert row.structured_output["safe_fallback_reason"] == result.failure_reason


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("user_input", "reply_text"),
    [
        ("السلام عليكم", "وعليكم السلام ورحمة الله وبركاته."),
        ("كيف حالكم", "بخير ولله الحمد، حياك الله."),
        ("مرحبا", "يا مرحبا، حياك الله."),
        ("صباح الخير", "صباح النور والسرور."),
    ],
)
async def test_evidence_free_social_reply_completes_without_safe_fallback(
    seeded: Seed,
    user_input: str,
    reply_text: str,
) -> None:
    expected = CommerceReply(text=reply_text, response_mode="social")
    model = ScriptedModel([[assistant_message(expected.model_dump_json())]])

    result = await run_commerce_agent(
        context=_context(seeded, trace_id=f"social:{user_input}"),
        user_input=user_input,
        model=model,
        model_name="scripted-social-regression",
        execution_mode="outbound",
    )

    assert result.status == "completed"
    assert result.reply == expected
    assert result.reply.safe_fallback_reason is None
    assert not any(event.get("kind") == "tool_start" for event in result.tool_trace)
    model.assert_complete()


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
    assert any(
        event.get("kind") == "tool_timeout"
        and event.get("failure_reason") == "tool_timeout:search_products"
        for event in result.tool_trace
    )
    assert sum(event.get("kind") == "tool_start" for event in result.tool_trace) == 1
    model.assert_complete()


@pytest.mark.asyncio
async def test_malformed_domain_tool_result_is_a_safe_tool_error(seeded: Seed, monkeypatch) -> None:
    monkeypatch.setattr(
        "modules.ai.commerce_agent_v2.tools.catalog.CatalogContextBuilder.search_products",
        lambda *_args, **_kwargs: [{"bad": "shape"}],
    )
    raw = await _invoke(search_products, _context(seeded), {"query": "x", "limit": 2})
    assert raw["status"] == "error"
    assert raw["failure_reason"] == "tool_error:search_products:AttributeError"


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


def test_real_whatsapp_observer_correlates_exact_persisted_turn(seeded: Seed) -> None:
    from models import MessageEvent
    from modules.ai.commerce_agent_v2.runner import CommerceAgentRunResult
    from modules.ai.commerce_agent_v2.tracing import sdk_trace_id
    from services.commerce_v2_whatsapp_e2e_observer import observe_persisted_turn

    trace_id = sdk_trace_id("wamid-current")
    context = _context(seeded)
    result = CommerceAgentRunResult(
        status="completed",
        reply=CommerceReply(text="نتيجة موثوقة", response_mode="social"),
        model="gpt-5.6-sol",
        session_id=f"commerce-v2:{seeded.tenant_a.id}:{seeded.conversation_a.id}",
        sdk_trace_id=trace_id,
        latency_ms=120,
        input_tokens=40,
        output_tokens=5,
        total_tokens=45,
        cached_input_tokens=20,
        requested_service_tier="fast",
        tool_trace=[
            {"kind": "model_start", "model_turn": 1, "model_attempt": 1},
            {"kind": "model_end", "model_turn": 1, "model_attempt": 1, "latency_ms": 100},
            {
                "kind": "usage_summary",
                "input_tokens": 40,
                "cached_input_tokens": 20,
                "requested_service_tier": "fast",
            },
        ],
        guardrail_results=[{"name": "grounding", "tripwire_triggered": False}],
    )
    _persist_shadow_result(seeded.db, context, result)
    seeded.db.add(
        MessageEvent(
            tenant_id=seeded.tenant_a.id,
            conversation_id=seeded.conversation_a.id,
            direction="outbound",
            body="نتيجة موثوقة",
            extra_metadata={
                "reply_owner": "commerce_agent_v2",
                "sdk_trace_id": trace_id,
                "v1_bypassed": True,
                "outbound_provider_wamid": "wamid.outbound.test",
                "outbound_provider_duration_ms": 20,
            },
        )
    )
    seeded.db.commit()

    observed = observe_persisted_turn(
        seeded.db,
        account_alias="A",
        case_id="A-001",
        inbound_wamid="wamid-current",
    )

    assert observed["trace_id"] == trace_id
    assert observed["outbound_wamids"] == ["wamid.outbound.test"]
    assert observed["owner"] == "commerce_agent_v2"
    assert observed["model_attempts"] == 1
    assert observed["first_model_latency_ms"] == 100
    assert observed["cached_input_tokens"] == 20
    assert observed["cost_usd"] == pytest.approx(observed["base_cost_usd"] * 2)


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


def _evaluate_tool_contract(
    case: dict[str, Any],
    *,
    actual_tools: list[str],
    tool_arguments: list[dict[str, Any]],
    evidence: list[dict[str, Any]],
) -> dict[str, Any]:
    contract = case["tool_contract"]
    required = set(contract["required_tools"])
    forbidden = set(contract["forbidden_tools"])
    evidence_sources = {row.get("source") for row in evidence}
    required_evidence = set(contract["required_evidence_sources"])
    signatures = [
        json.dumps(item, sort_keys=True, separators=(",", ":")) for item in tool_arguments
    ]
    identical_duplicate_calls = len(signatures) != len(set(signatures))
    checks = {
        "required_tools_present": required <= set(actual_tools),
        "acceptable_plan": actual_tools in contract["acceptable_plans"],
        "forbidden_tools_absent": not (forbidden & set(actual_tools)),
        "required_evidence_present": required_evidence <= evidence_sources,
        "no_identical_duplicate_calls": not (
            contract.get("forbid_identical_duplicate_calls", False)
            and identical_duplicate_calls
        ),
    }
    return {
        **checks,
        "passed": all(checks.values()),
        "evidence_sources": sorted(source for source in evidence_sources if source),
        "identical_duplicate_calls": identical_duplicate_calls,
    }


def _outcome_is_correct(case: dict[str, Any], result: Any) -> bool:
    expected = case["tool_contract"]["expected_outcome"]
    if expected == "grounded_reply":
        return bool(
            result.status == "completed"
            and result.reply.evidence_refs
            and result.reply.fact_claims
        )
    if expected == "safe_fallback":
        return bool(result.status == "completed" and result.reply.safe_fallback_reason)
    raise AssertionError(f"unknown expected_outcome: {expected}")


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
    assert all(case["turn_mode"] in {"single", "multi"} for case in cases)
    assert all(not case.get("history") for case in cases if case["turn_mode"] == "single")
    assert all(case.get("history") for case in cases if case["turn_mode"] == "multi")
    assert all(case["replay_plan"] in case["tool_contract"]["acceptable_plans"] for case in cases)
    assert all(
        set(case["tool_contract"]["required_tools"]) <= set(case["replay_plan"])
        for case in cases
    )
    assert {
        case["tool_contract"]["expected_outcome"] for case in cases
    } == {"grounded_reply", "safe_fallback"}


def test_tool_eval_accepts_evidence_equivalent_price_plans_and_rejects_forbidden_calls() -> None:
    case = {
        "tool_contract": {
            "required_tools": ["search_products"],
            "acceptable_plans": [
                ["search_products"],
                ["search_products", "get_product_details"],
            ],
            "forbidden_tools": ["search_product_knowledge"],
            "required_evidence_sources": ["catalog_product"],
        }
    }
    evidence = [{"source": "catalog_product"}]
    for plan in (["search_products"], ["search_products", "get_product_details"]):
        evaluated = _evaluate_tool_contract(
            case,
            actual_tools=plan,
            tool_arguments=[{"tool": name, "arguments": {}} for name in plan],
            evidence=evidence,
        )
        assert evaluated["passed"] is True

    rejected = _evaluate_tool_contract(
        case,
        actual_tools=["search_products", "search_product_knowledge"],
        tool_arguments=[],
        evidence=evidence,
    )
    assert rejected["passed"] is False
    assert rejected["forbidden_tools_absent"] is False


def test_tool_eval_accepts_one_details_check_after_missing_product_knowledge() -> None:
    case = {
        "tool_contract": {
            "required_tools": ["search_products", "search_product_knowledge"],
            "acceptable_plans": [
                ["search_products", "search_product_knowledge"],
                [
                    "search_products",
                    "search_product_knowledge",
                    "get_product_details",
                ],
            ],
            "forbidden_tools": ["search_merchant_knowledge"],
            "required_evidence_sources": ["catalog_product"],
            "forbid_identical_duplicate_calls": True,
        }
    }
    evaluated = _evaluate_tool_contract(
        case,
        actual_tools=[
            "search_products",
            "search_product_knowledge",
            "get_product_details",
        ],
        tool_arguments=[
            {"tool": "search_products", "arguments": {"query": "عسل طلح"}},
            {
                "tool": "search_product_knowledge",
                "arguments": {"product_id": 1, "query": "سنة قطف"},
            },
            {"tool": "get_product_details", "arguments": {"product_id": 1}},
        ],
        evidence=[{"source": "catalog_product"}],
    )
    assert evaluated["passed"] is True


def test_outcome_eval_accepts_grounded_partial_answer_with_scoped_fallback() -> None:
    case = {"tool_contract": {"expected_outcome": "grounded_reply"}}
    result = SimpleNamespace(
        status="completed",
        reply=CommerceReply(
            text="السعر 150 ريال، لكن منطقة المصدر غير مذكورة.",
            evidence_refs=["catalog:product:1"],
            fact_claims=[
                FactClaim(
                    kind="price",
                    value=150,
                    evidence_ref="catalog:product:1",
                    subject_product_id=1,
                    text_span="150 ريال",
                )
            ],
            safe_fallback_reason="منطقة المصدر غير متوفرة في الدليل.",
        ),
    )
    assert _outcome_is_correct(case, result) is True


@pytest.mark.asyncio
async def test_replay_cases_use_unique_clean_room_sessions_with_only_declared_history(
    seeded: Seed,
) -> None:
    fixture_path = Path(__file__).parents[1] / "evals" / "commerce_agent_v2_phase1_replay.json"
    cases = json.loads(fixture_path.read_text(encoding="utf-8"))
    session_ids: list[str] = []
    for case in cases:
        context = _case_context(seeded, case, mode="isolation-proof")
        session = ConversationMessageSession(context)
        session_ids.append(session.session_id)
        items = await session.get_items()
        expected = [
            {
                "role": "user" if item["direction"] in {"in", "inbound"} else "assistant",
                "content": item["body"],
            }
            for item in case.get("history", [])
        ]
        assert items == expected
        if case["turn_mode"] == "single":
            assert items == []
    assert len(session_ids) == len(set(session_ids)) == len(cases)


@pytest.mark.asyncio
async def test_admin_reset_creates_latest_empty_v2_session_and_clears_decision_state(
    seeded: Seed,
) -> None:
    source_conversation_id = seeded.conversation_a.id
    original_product_count = seeded.db.query(Product).count()
    original_kb_count = seeded.db.query(MerchantKnowledgeSection).count()
    seeded.db.add_all(
        [
            ConversationHistorySummary(
                tenant_id=seeded.tenant_a.id,
                customer_id=seeded.customer_a.id,
                summary_text="ملخص قديم يجب ألا يصل إلى التجربة التالية",
                products_mentioned=[seeded.honey_a.id],
                last_intent="inquiry",
            ),
            HandoffSession(
                tenant_id=seeded.tenant_a.id,
                customer_phone=seeded.customer_a.normalized_phone,
                status="active",
                context_snapshot={"conversation_id": source_conversation_id},
            ),
        ]
    )
    seeded.db.commit()

    preview = reset_conversation_context(
        seeded.db,
        tenant_id=seeded.tenant_a.id,
        conversation_id=source_conversation_id,
        actor="phase1-eval",
        apply=False,
    )
    assert preview == {
        "applied": False,
        "tenant_id": seeded.tenant_a.id,
        "source_conversation_id": source_conversation_id,
        "customer_id": seeded.customer_a.id,
        "preserved_message_count": 3,
        "removed_history_summary_count": 1,
        "resolved_handoff_count": 1,
        "new_conversation_id": None,
        "preserved_business_records": True,
    }
    assert seeded.db.query(ConversationHistorySummary).count() == 1
    assert seeded.db.query(HandoffSession).filter_by(status="active").count() == 1

    applied = reset_conversation_context(
        seeded.db,
        tenant_id=seeded.tenant_a.id,
        conversation_id=source_conversation_id,
        actor="phase1-eval",
        apply=True,
    )
    assert applied["applied"] is True
    assert applied["new_conversation_id"] != source_conversation_id
    assert seeded.db.query(ConversationHistorySummary).count() == 0
    assert seeded.db.query(HandoffSession).filter_by(status="active").count() == 0
    assert seeded.db.query(HandoffSession).filter_by(status="resolved").count() == 1
    assert seeded.db.query(MessageEvent).filter_by(
        tenant_id=seeded.tenant_a.id,
        conversation_id=source_conversation_id,
    ).count() == 3
    assert seeded.db.query(Product).count() == original_product_count
    assert seeded.db.query(MerchantKnowledgeSection).count() == original_kb_count

    clean_conversation = seeded.db.get(Conversation, applied["new_conversation_id"])
    assert clean_conversation is not None
    assert clean_conversation.extra_metadata["brain_state"] == {}
    assert clean_conversation.extra_metadata["test_context_reset"][
        "source_conversation_id"
    ] == source_conversation_id
    assert clean_conversation.is_human_handoff is False
    assert clean_conversation.paused_by_human is False
    assert clean_conversation.ai_paused is False
    assert clean_conversation.needs_human is False
    assert clean_conversation.handoff_active is False
    latest = (
        seeded.db.query(Conversation)
        .filter_by(
            tenant_id=seeded.tenant_a.id,
            customer_id=seeded.customer_a.id,
        )
        .order_by(Conversation.id.desc())
        .first()
    )
    assert latest.id == clean_conversation.id
    clean_context = _context(
        seeded,
        trace_id="after-admin-reset",
        conversation=clean_conversation,
    )
    assert await ConversationMessageSession(clean_context).get_items() == []

    with pytest.raises(
        ConversationContextResetError,
        match="conversation_not_in_tenant_scope",
    ):
        reset_conversation_context(
            seeded.db,
            tenant_id=seeded.tenant_b.id,
            conversation_id=source_conversation_id,
            actor="phase1-eval",
            apply=False,
        )


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
        context=_case_context(seeded, case, mode="replay"),
        user_input=case["message"],
        model=model,
        model_name="scripted-replay",
    )
    actual_tools = [
        event["tool"] for event in result.tool_trace if event.get("kind") == "tool_end"
    ]
    tool_arguments = [
        {"tool": event["tool"], "arguments": event.get("arguments", {})}
        for event in result.tool_trace
        if event.get("kind") == "tool_start"
    ]
    evidence = [
        evidence_row
        for event in result.tool_trace
        if event.get("kind") == "tool_end"
        for evidence_row in event.get("result", {}).get("evidence", [])
    ]
    assert result.status == "completed"
    assert actual_tools == case["replay_plan"]
    assert _evaluate_tool_contract(
        case,
        actual_tools=actual_tools,
        tool_arguments=tool_arguments,
        evidence=evidence,
    )["passed"] is True
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
    results: list[dict[str, Any]] = []
    for case in cases:
        context = _case_context(seeded, case, mode="live")
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
        tool_contract = _evaluate_tool_contract(
            case,
            actual_tools=actual_tools,
            tool_arguments=tool_arguments,
            evidence=evidence,
        )
        post_validation_errors = validate_grounded_reply(context, result.reply)
        guardrail_errors = [
            error
            for guardrail in result.guardrail_results
            for error in (guardrail.get("output_info") or {}).get("errors", [])
        ]
        rejected_eval_reply = next(
            (
                (guardrail.get("output_info") or {}).get("rejected_eval_reply")
                for guardrail in result.guardrail_results
                if (guardrail.get("output_info") or {}).get("rejected_eval_reply")
            ),
            None,
        )
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
                "turn_mode": case["turn_mode"],
                "declared_history_count": len(case.get("history", [])),
                "session_id": ConversationMessageSession(context).session_id,
                "tool_contract": case["tool_contract"],
                "actual_tools": actual_tools,
                "tool_behavior": tool_contract,
                "outcome_correct": _outcome_is_correct(case, result),
                "tool_arguments": tool_arguments,
                "evidence": evidence,
                "final_reply": result.reply.model_dump(mode="json"),
                "rejected_eval_reply": rejected_eval_reply,
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
        "isolation": "fresh_conversation_per_case",
        "model": model,
        "reasoning_effort": "high",
        "case_count": len(results),
        "completed": sum(row["status"] == "completed" for row in results),
        "tool_behavior_matches": sum(row["tool_behavior"]["passed"] for row in results),
        "outcome_correct": sum(row["outcome_correct"] for row in results),
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
    summary["quality_gate_passed"] = bool(
        summary["completed"] == len(results)
        and summary["tool_behavior_matches"] == len(results)
        and summary["outcome_correct"] == len(results)
        and summary["unsupported_claim_count"] == 0
        and all(not contains_legacy_marker(row["final_reply"]["text"]) for row in results)
    )
    summary["provider_observability_passed"] = bool(
        len(provider_calls) >= len(cases)
        and all(call["requested_model"] == model for call in provider_calls)
        and all(call["provider_response_received"] for call in provider_calls)
    )
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

    if os.environ.get("NAHLA_COMMERCE_V2_ENFORCE_LIVE_GATE") == "1":
        assert summary["provider_observability_passed"] is True
        assert summary["quality_gate_passed"] is True

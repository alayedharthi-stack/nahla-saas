"""Phase 2.7B: every product or store-information turn must consult tenant knowledge.

Phase 2.7A left knowledge retrieval to the model's discretion, so a browse or a
product-detail turn answered from the catalog alone whenever the model did not
choose the knowledge tool.  These tests hold the new contract: retrieval is part
of the orchestration, every attempt is recorded, structured Salla data stays
authoritative, and nothing the merchant did not write is ever invented.

Collected by the default root suite.  No provider is reached: the tools are
invoked directly and the acceptance cases are scored from synthetic artifacts.
"""
from __future__ import annotations

import asyncio
import json
import os
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import pytest
from sqlalchemy import JSON, create_engine
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
for _p in (REPO, os.path.join(REPO, "backend"), os.path.join(REPO, "database")):
    if _p not in sys.path:
        sys.path.insert(0, _p)

from models import (  # noqa: E402
    Base,
    Conversation,
    Customer,
    MerchantKnowledgeSection,
    MerchantKnowledgeSectionProduct,
    Product,
    Tenant,
    TenantSettings,
    WhatsAppConnection,
)
from modules.ai.commerce_agent_v2.context import (  # noqa: E402
    MAX_KNOWLEDGE_LOOKUPS_PER_TURN,
    CommerceAgentContext,
)
from modules.ai.commerce_agent_v2 import knowledge_retrieval  # noqa: E402
from modules.ai.commerce_agent_v2.knowledge_retrieval import (  # noqa: E402
    SCOPE_PRODUCT,
    SCOPE_TURN,
    STATUS_ERROR,
    STATUS_NO_RESULTS,
    STATUS_OK,
    STATUS_TIMEOUT,
    detect_catalog_conflicts,
    normalize_lookup_query,
    run_knowledge_lookup,
    run_knowledge_lookup_async,
)
from modules.ai.commerce_agent_v2.tools.catalog import (  # noqa: E402
    get_product_details,
    search_products,
)
from modules.ai.commerce_agent_v2.tools.knowledge import (  # noqa: E402
    _merchant_knowledge_enabled,
    search_product_knowledge,
)
from services.commerce_v2_phase_2_7b_knowledge_acceptance import (  # noqa: E402
    KNOWLEDGE_CASES_TOTAL,
    KNOWLEDGE_CONTRACT_VERSION,
    KNOWLEDGE_MATRIX_PATH,
    KnowledgeAcceptanceError,
    load_knowledge_acceptance_matrix,
    score_knowledge_turn,
)


@dataclass
class Seed:
    db: Any
    tenant: Tenant
    other_tenant: Tenant
    customer: Customer
    conversation: Conversation
    connection: WhatsAppConnection
    jacket: Product
    skirt: Product
    origin_section: MerchantKnowledgeSection
    policy_section: MerchantKnowledgeSection
    foreign_section: MerchantKnowledgeSection


def _engine() -> Any:
    engine = create_engine(
        "sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool
    )
    saved: list[tuple[Any, Any]] = []
    for table in Base.metadata.sorted_tables:
        for column in table.columns:
            if isinstance(column.type, JSONB):
                saved.append((column, column.type))
                column.type = JSON()
    Base.metadata.create_all(engine)
    for column, original in saved:
        column.type = original
    return engine


def _seed(db: Any) -> Seed:
    tenant = Tenant(name="متجر الاختبار", is_active=True)
    other_tenant = Tenant(name="متجر آخر", is_active=True)
    db.add_all([tenant, other_tenant])
    db.flush()
    db.add(TenantSettings(tenant_id=tenant.id, ai_settings={"locale": "ar-SA"}))
    customer = Customer(
        tenant_id=tenant.id, phone="0500000001", normalized_phone="+966500000001"
    )
    db.add(customer)
    db.flush()
    conversation = Conversation(
        tenant_id=tenant.id, customer_id=customer.id, status="active"
    )
    connection = WhatsAppConnection(tenant_id=tenant.id, status="connected")
    db.add_all([conversation, connection])
    db.flush()
    jacket = Product(
        tenant_id=tenant.id,
        external_id="T-JACKET",
        title="جاكيت شتوي",
        description="جاكيت خفيف",
        price="169",
        stock_quantity=2,
        in_stock=True,
        extra_metadata={
            "status": "active",
            "in_stock": True,
            "stock_qty": 2,
            "currency": "SAR",
            "product_url": "https://shop.example.test/products/jacket",
        },
    )
    skirt = Product(
        tenant_id=tenant.id,
        external_id="T-SKIRT",
        title="تنورة قطنية",
        price="114",
        stock_quantity=1,
        in_stock=True,
        extra_metadata={"status": "active", "in_stock": True, "stock_qty": 1},
    )
    db.add_all([jacket, skirt])
    db.flush()
    origin_section = MerchantKnowledgeSection(
        tenant_id=tenant.id,
        kind="product_info",
        title="مصدر الجاكيت",
        body="مصدر هذا الجاكيت من ورشة محلية وخامته قطن مخلوط.",
        is_active=True,
        ai_status="approved",
    )
    policy_section = MerchantKnowledgeSection(
        tenant_id=tenant.id,
        kind="custom",
        title="سياسة الاسترجاع",
        body="الاسترجاع متاح خلال سبعة أيام من الاستلام.",
        is_active=True,
        ai_status="approved",
    )
    foreign_section = MerchantKnowledgeSection(
        tenant_id=other_tenant.id,
        kind="custom",
        title="سياسة الاسترجاع",
        body="سياسة متجر آخر لا يجوز أن تظهر هنا إطلاقًا.",
        is_active=True,
        ai_status="approved",
    )
    db.add_all([origin_section, policy_section, foreign_section])
    db.flush()
    db.add(
        MerchantKnowledgeSectionProduct(
            section_id=origin_section.id, product_id=jacket.id, source="manual"
        )
    )
    db.commit()
    return Seed(
        db=db,
        tenant=tenant,
        other_tenant=other_tenant,
        customer=customer,
        conversation=conversation,
        connection=connection,
        jacket=jacket,
        skirt=skirt,
        origin_section=origin_section,
        policy_section=policy_section,
        foreign_section=foreign_section,
    )


@pytest.fixture()
def seeded() -> Any:
    engine = _engine()
    db = sessionmaker(bind=engine)()
    seed = _seed(db)
    yield seed
    db.close()
    engine.dispose()


def _context(seed: Seed, *, user_input: str) -> CommerceAgentContext:
    context = CommerceAgentContext.from_trusted_scope(
        db=seed.db,
        tenant_id=seed.tenant.id,
        conversation_id=seed.conversation.id,
        customer_id=seed.customer.id,
        normalized_customer_phone=seed.customer.normalized_phone,
        connection_id=str(seed.connection.id),
        inbound_trace_id="wamid-p27b",
    )
    context.bind_run_user_input(user_input)
    return context


def _wrapper(context: CommerceAgentContext) -> Any:
    class _Wrapper:
        def __init__(self, ctx: CommerceAgentContext) -> None:
            self.context = ctx

    return _Wrapper(context)


async def _invoke(tool: Any, context: CommerceAgentContext, arguments: dict[str, Any]) -> Any:
    from agents import RunConfig
    from agents.tool_context import ToolContext

    raw = await tool.on_invoke_tool(
        ToolContext(
            context=context,
            tool_name=tool.name,
            tool_call_id=f"test-{tool.name}",
            tool_arguments=json.dumps(arguments),
            run_config=RunConfig(tracing_disabled=True, trace_include_sensitive_data=False),
        ),
        json.dumps(arguments),
    )
    return raw


# ── 1. Retrieval is part of the orchestration, not a model choice ────────────

@pytest.mark.parametrize("kind", [
    "shipping_policy", "return_policy", "refund_policy", "exchange_policy",
    "terms_policy", "privacy_policy", "warranty", "store_story", "faq",
    "working_hours", "branches", "payment_method", "bank_transfer", "cod",
    "shipping_carrier", "shipping_zones", "cold_shipping", "summer_note",
])
def test_merchant_tool_retrieves_visible_policy_evidence(seeded: Seed, kind: str) -> None:
    """A real policy row must not disappear behind the product-only kind filter."""
    from modules.ai.commerce_agent_v2.tools.knowledge import search_merchant_knowledge_impl

    row = MerchantKnowledgeSection(
        tenant_id=seeded.tenant.id, kind=kind, title="سياسة المتجر العامة",
        body="سياسة المتجر العامة: الشحن من ثلاثة إلى خمسة أيام عمل.",
        is_active=True, ai_status="approved",
    )
    seeded.db.add(row)
    seeded.db.commit()
    context = _context(seeded, user_input="سياسة المتجر العامة")
    result = asyncio.run(search_merchant_knowledge_impl(context, "سياسة المتجر العامة", 4))
    assert result.status == "ok"
    section = next(item for item in result.sections if item.section_id == row.id)
    assert section.kind == kind and section.body == row.body
    evidence = next(item for item in result.evidence if item.ref == section.evidence_ref)
    assert evidence.source == "merchant_knowledge"
    assert evidence.provenance["record"] == "merchant_knowledge_sections"
    assert evidence.fields["section_id"] == row.id


@pytest.mark.parametrize("excluded", ["tenant", "inactive", "deleted", "draft", "product_link"])
def test_merchant_policy_retrieval_preserves_visibility_and_scope(seeded: Seed, excluded: str) -> None:
    from datetime import datetime, timezone
    from modules.ai.commerce_agent_v2.tools.knowledge import search_merchant_knowledge_impl

    row = MerchantKnowledgeSection(
        tenant_id=seeded.other_tenant.id if excluded == "tenant" else seeded.tenant.id,
        kind="shipping_policy", title="الشحن والتوصيل", body="الشحن والتوصيل خلال خمسة أيام.",
        is_active=excluded != "inactive", ai_status="draft" if excluded == "draft" else "approved",
        deleted_at=datetime.now(timezone.utc) if excluded == "deleted" else None,
    )
    seeded.db.add(row)
    seeded.db.flush()
    if excluded == "product_link":
        seeded.db.add(MerchantKnowledgeSectionProduct(section_id=row.id, product_id=seeded.jacket.id))
    seeded.db.commit()
    context = _context(seeded, user_input="الشحن والتوصيل")
    result = asyncio.run(search_merchant_knowledge_impl(context, "الشحن والتوصيل", 4))
    assert all(item.section_id != row.id for item in result.sections)
    assert all(item.ref != f"kb:section:{row.id}" for item in result.evidence)


def test_catalog_default_keeps_policy_out_of_product_facts(seeded: Seed) -> None:
    from modules.ai.brain.commerce.product_knowledge_or_comparison import retrieve_catalog_candidate_kb_sections

    row = MerchantKnowledgeSection(
        tenant_id=seeded.tenant.id, kind="shipping_policy", title="الشحن والتوصيل",
        body="الشحن والتوصيل خلال خمسة أيام.", is_active=True, ai_status="approved",
    )
    seeded.db.add(row)
    seeded.db.commit()
    result = retrieve_catalog_candidate_kb_sections(
        seeded.db, seeded.tenant.id, subject="الشحن والتوصيل", message="الشحن والتوصيل",
    )
    assert all(item["section_id"] != row.id for item in result["kb_sections"])


def test_shipping_policy_real_tool_keeps_limits_and_relevance(seeded: Seed) -> None:
    from modules.ai.commerce_agent_v2.tools.knowledge import search_merchant_knowledge_impl

    rows = [MerchantKnowledgeSection(
        tenant_id=seeded.tenant.id, kind="shipping_policy", title="الشحن والتوصيل",
        body="مدة الشحن والتوصيل ثلاثة إلى خمسة أيام عمل. " * 100,
        is_active=True, ai_status="approved",
    ) for _ in range(6)]
    unrelated = MerchantKnowledgeSection(
        tenant_id=seeded.tenant.id, kind="store_story", title="الحرف اليدوية",
        body="تأسس المتجر لتطوير الحرف اليدوية المحلية.", is_active=True, ai_status="approved",
    )
    seeded.db.add_all(rows + [unrelated])
    seeded.db.commit()
    context = _context(seeded, user_input="عندكم حذاء رياضي؟ ومتى يوصل؟")
    result = asyncio.run(search_merchant_knowledge_impl(context, "الشحن والتوصيل", 4))
    assert result.status == "ok" and 1 <= len(result.sections) <= 4
    assert all(section.section_id != unrelated.id for section in result.sections)
    assert all(len(section.body) <= 800 for section in result.sections)
    assert all(section.body == rows[0].body[:800] for section in result.sections)


@pytest.mark.parametrize("kind", [
    "reply_style", "dialect", "forbidden_phrases", "allowed_style", "escalation_rules",
    "compliance_rules", "response_tone", "emoji_policy", "owner_identity", "assistant_identity",
])
def test_store_knowledge_does_not_promote_behavioral_rules_to_facts(seeded: Seed, kind: str) -> None:
    from modules.ai.commerce_agent_v2.tools.knowledge import search_merchant_knowledge_impl

    row = MerchantKnowledgeSection(
        tenant_id=seeded.tenant.id, kind=kind, title="الشحن والتوصيل",
        body="الشحن والتوصيل: تعليمات داخلية وليست حقائق للعميل.",
        is_active=True, ai_status="approved",
    )
    seeded.db.add(row)
    seeded.db.commit()
    context = _context(seeded, user_input="الشحن والتوصيل")
    result = asyncio.run(search_merchant_knowledge_impl(context, "الشحن والتوصيل", 4))
    assert all(item.section_id != row.id for item in result.sections)
    assert all(item.ref != f"kb:section:{row.id}" for item in result.evidence)

@pytest.mark.parametrize(
    "turn_id, user_input, query",
    [
        ("A2", "وش المنتجات المتوفرة عندكم؟", ""),
        ("A3", "أبغى تفاصيل أول منتج عندكم", "جاكيت"),
        ("A4", "كم سعر أول منتج وهل هو متوفر؟", "جاكيت"),
        ("B1", "السلام عليكم، أبغى أتصفح المنتجات", ""),
        ("B2", "اختر لي واحداً منها", "جاكيت"),
        ("B3", "كم سعره وهل هو متوفر؟", "جاكيت"),
        ("B4", "طيب هل عندكم معلومات إضافية عنه؟", "جاكيت"),
    ],
)
def test_every_product_turn_attempts_a_tenant_scoped_knowledge_lookup(
    seeded: Seed, turn_id: str, user_input: str, query: str
) -> None:
    context = _context(seeded, user_input=user_input)
    result = asyncio.run(_invoke(search_products, context, {"query": query, "limit": 5}))

    assert result.status == "ok", turn_id
    assert context.knowledge_lookup_attempted is True, turn_id
    lookups = context.knowledge_lookups
    assert [item["scope"] for item in lookups] == [SCOPE_PRODUCT]
    assert lookups[0]["tenant_id"] == seeded.tenant.id
    assert lookups[0]["status"] in {STATUS_OK, STATUS_NO_RESULTS}
    assert lookups[0]["product_ids"] == sorted(p.product_id for p in result.products)
    assert lookups[0]["purpose"] == "catalog_product_knowledge"


def test_a_pure_order_turn_records_no_product_knowledge_lookup(seeded: Seed) -> None:
    """C1–C4 need no product knowledge: nothing runs, and nothing is claimed to have run."""
    context = _context(seeded, user_input="وش حالة آخر طلب لي؟")
    assert context.knowledge_lookups == []
    assert context.knowledge_lookup_attempted is False


def test_store_information_turn_runs_the_turn_scope_lookup_before_the_model_chooses(
    seeded: Seed,
) -> None:
    context = _context(seeded, user_input="وش سياسة الاسترجاع عندكم؟")

    exposed = _merchant_knowledge_enabled(_wrapper(context), None)

    assert exposed is True
    lookups = context.knowledge_lookups
    assert [item["scope"] for item in lookups] == [SCOPE_TURN]
    assert lookups[0]["status"] == STATUS_OK
    assert lookups[0]["section_ids"] == [seeded.policy_section.id]
    assert lookups[0]["evidence_refs"] == [f"kb:section:{seeded.policy_section.id}"]


def test_a_lookup_that_finds_nothing_is_still_recorded(seeded: Seed) -> None:
    context = _context(seeded, user_input="هل عندكم معلومات عن طريقة التخزين؟")

    exposed = _merchant_knowledge_enabled(_wrapper(context), None)

    assert exposed is False
    assert context.knowledge_lookup_attempted is True
    assert context.knowledge_lookups[0]["status"] == STATUS_NO_RESULTS
    assert context.knowledge_lookups[0]["hit_count"] == 0


# ── 2. What retrieval returns: relevance, scope, freshness ───────────────────

def test_product_knowledge_reaches_the_catalog_answer_as_linked_evidence(
    seeded: Seed,
) -> None:
    context = _context(seeded, user_input="وش مصدر الجاكيت؟")
    result = asyncio.run(_invoke(search_products, context, {"query": "جاكيت", "limit": 5}))

    assert [section.section_id for section in result.knowledge_sections] == [
        seeded.origin_section.id
    ]
    section = result.knowledge_sections[0]
    assert section.linked_product_ids == [seeded.jacket.id]
    assert section.evidence_ref == f"kb:section:{seeded.origin_section.id}"
    assert section.body == seeded.origin_section.body
    assert context.evidence[section.evidence_ref].source == "product_knowledge"


def test_irrelevant_knowledge_is_not_attached_to_the_catalog_answer(seeded: Seed) -> None:
    context = _context(seeded, user_input="كم سعر التنورة؟")
    result = asyncio.run(_invoke(search_products, context, {"query": "تنورة", "limit": 5}))

    assert result.status == "ok"
    assert result.knowledge_sections == []
    assert context.knowledge_lookups[0]["status"] == STATUS_NO_RESULTS
    assert not [ref for ref in context.evidence if ref.startswith("kb:section:")]


def test_dialect_and_spelling_variants_still_retrieve_the_section(seeded: Seed) -> None:
    for variant in ("وش مصدر الجاكيت ذا؟", "وش مصدر هذا الجاكيت", "ايش مصدر الجاكيت"):
        context = _context(seeded, user_input=variant)
        asyncio.run(_invoke(search_products, context, {"query": "جاكيت", "limit": 5}))
        assert context.knowledge_lookups[0]["section_ids"] == [seeded.origin_section.id], variant


def test_a_deleted_or_hidden_section_never_returns(seeded: Seed) -> None:
    context = _context(seeded, user_input="وش مصدر الجاكيت؟")
    asyncio.run(_invoke(search_products, context, {"query": "جاكيت", "limit": 5}))
    assert context.knowledge_lookups[0]["section_ids"] == [seeded.origin_section.id]

    seeded.origin_section.is_active = False
    seeded.db.commit()

    after = _context(seeded, user_input="وش مصدر الجاكيت؟")
    result = asyncio.run(_invoke(search_products, after, {"query": "جاكيت", "limit": 5}))
    assert result.knowledge_sections == []
    assert after.knowledge_lookups[0]["status"] == STATUS_NO_RESULTS


def test_an_updated_section_is_re_read_on_the_next_turn(seeded: Seed) -> None:
    first = _context(seeded, user_input="وش مصدر الجاكيت؟")
    first_result = asyncio.run(_invoke(search_products, first, {"query": "جاكيت", "limit": 5}))
    assert first_result.knowledge_sections[0].body == seeded.origin_section.body

    seeded.origin_section.body = "مصدر هذا الجاكيت من ورشة جديدة بعد التحديث."
    seeded.db.commit()

    second = _context(seeded, user_input="وش مصدر الجاكيت؟")
    second_result = asyncio.run(_invoke(search_products, second, {"query": "جاكيت", "limit": 5}))
    assert second_result.knowledge_sections[0].body == seeded.origin_section.body
    assert second_result.knowledge_sections[0].body != first_result.knowledge_sections[0].body


def test_another_tenants_knowledge_is_never_retrieved(seeded: Seed) -> None:
    context = _context(seeded, user_input="وش سياسة الاسترجاع عندكم؟")
    _merchant_knowledge_enabled(_wrapper(context), None)

    section_ids = context.knowledge_lookups[0]["section_ids"]
    assert seeded.foreign_section.id not in section_ids
    assert section_ids == [seeded.policy_section.id]
    assert all(item["tenant_id"] == seeded.tenant.id for item in context.knowledge_lookups)


# ── 3. Bounded cost, failures and structured-data authority ──────────────────

def test_repeating_the_same_lookup_does_not_re_query_or_duplicate_evidence(
    seeded: Seed, monkeypatch: pytest.MonkeyPatch
) -> None:
    context = _context(seeded, user_input="وش مصدر الجاكيت؟")
    calls = {"n": 0}
    original = knowledge_retrieval.retrieve_catalog_candidate_kb_sections

    def _counted(*args: Any, **kwargs: Any) -> Any:
        calls["n"] += 1
        return original(*args, **kwargs)

    monkeypatch.setattr(knowledge_retrieval, "retrieve_catalog_candidate_kb_sections", _counted)

    asyncio.run(_invoke(search_products, context, {"query": "جاكيت", "limit": 5}))
    asyncio.run(_invoke(get_product_details, context, {"product_id": seeded.jacket.id}))
    asyncio.run(
        _invoke(
            search_product_knowledge,
            context,
            {"product_id": seeded.jacket.id, "query": "وش مصدر الجاكيت؟", "limit": 4},
        )
    )

    assert calls["n"] == 1
    assert len(context.knowledge_lookups) == 1
    assert len(context.knowledge_lookups) <= MAX_KNOWLEDGE_LOOKUPS_PER_TURN


def test_lookups_stay_bounded_when_a_turn_keeps_asking(seeded: Seed) -> None:
    """A model that reformulates forever still cannot spend more than the budget."""
    context = _context(seeded, user_input="وش مصدر الجاكيت؟")
    questions = [
        "مصدر الجاكيت",
        "خامة الجاكيت",
        "مقاسات الجاكيت",
        "ألوان الجاكيت",
        "تغليف الجاكيت",
        "ضمان الجاكيت",
        "تنظيف الجاكيت",
    ]
    assert len(questions) == MAX_KNOWLEDGE_LOOKUPS_PER_TURN + 3
    for question in questions:
        run_knowledge_lookup(
            context, scope=SCOPE_TURN, purpose="model_store_knowledge", query=question
        )

    recorded = context.knowledge_lookups
    assert len(recorded) == len(questions)
    spent = [item for item in recorded if item["status"] != "budget_exhausted"]
    assert len(spent) == MAX_KNOWLEDGE_LOOKUPS_PER_TURN
    assert all(item["hit_count"] == 0 for item in recorded if item["status"] == "budget_exhausted")


def test_a_retrieval_failure_is_an_outcome_not_absent_knowledge(
    seeded: Seed, monkeypatch: pytest.MonkeyPatch
) -> None:
    def _boom(*_args: Any, **_kwargs: Any) -> Any:
        raise RuntimeError("kb_unavailable")

    monkeypatch.setattr(knowledge_retrieval, "retrieve_catalog_candidate_kb_sections", _boom)
    context = _context(seeded, user_input="وش مصدر الجاكيت؟")

    result = asyncio.run(_invoke(search_products, context, {"query": "جاكيت", "limit": 5}))

    assert result.status == "ok"
    assert [product.title for product in result.products] == ["جاكيت شتوي"]
    assert result.knowledge_sections == []
    record = context.knowledge_lookups[0]
    assert record["status"] == STATUS_ERROR
    assert record["failure_reason"] == "knowledge_retrieval_exception:RuntimeError"
    assert record["hit_count"] == 0


def test_a_slow_knowledge_base_times_out_without_blocking_the_turn(
    seeded: Seed, monkeypatch: pytest.MonkeyPatch
) -> None:
    import time as _time

    def _slow(*_args: Any, **_kwargs: Any) -> Any:
        _time.sleep(0.5)
        return {"kb_sections": [], "kb_retrieval_failed": False}

    monkeypatch.setattr(knowledge_retrieval, "retrieve_catalog_candidate_kb_sections", _slow)
    context = _context(seeded, user_input="وش مصدر الجاكيت؟")

    record = asyncio.run(
        run_knowledge_lookup_async(
            context,
            scope=SCOPE_PRODUCT,
            purpose="catalog_product_knowledge",
            query="وش مصدر الجاكيت؟",
            product_ids=[seeded.jacket.id],
            timeout_seconds=0.05,
        )
    )

    assert record["status"] == STATUS_TIMEOUT
    assert record["failure_reason"] == "knowledge_retrieval_timeout"
    assert context.knowledge_lookup_attempted is True


def test_knowledge_that_contradicts_the_catalog_price_is_recorded_as_a_conflict(
    seeded: Seed,
) -> None:
    seeded.origin_section.body = "سعر هذا الجاكيت 99 ريال حسب نشرتنا القديمة."
    seeded.db.commit()
    context = _context(seeded, user_input="كم سعر الجاكيت؟")

    asyncio.run(_invoke(search_products, context, {"query": "جاكيت", "limit": 5}))
    conflicts = detect_catalog_conflicts(context)

    assert [item["kind"] for item in conflicts] == ["price"]
    assert conflicts[0]["product_id"] == seeded.jacket.id
    assert conflicts[0]["catalog_value"] == 169.0
    assert 99.0 in conflicts[0]["knowledge_values"]
    assert conflicts[0]["resolution"] == "structured_catalog_wins"


def test_a_matching_knowledge_price_is_not_a_conflict(seeded: Seed) -> None:
    seeded.origin_section.body = "سعر هذا الجاكيت 169 ريال كما في المتجر."
    seeded.db.commit()
    context = _context(seeded, user_input="كم سعر الجاكيت؟")

    asyncio.run(_invoke(search_products, context, {"query": "جاكيت", "limit": 5}))

    assert detect_catalog_conflicts(context) == []


def test_normalized_query_drops_punctuation_and_keeps_customer_word_order() -> None:
    assert normalize_lookup_query("وش مصدر الجاكيت؟") == "مصدر جاكيت"
    assert normalize_lookup_query("!!!") == ""
    long_query = " ".join(f"كلمه{index}" for index in range(30))
    assert len(normalize_lookup_query(long_query).split()) == 12


# ── 4. The Phase 2.7B acceptance artifact ────────────────────────────────────

def _artifact(**overrides: Any) -> dict[str, Any]:
    artifact: dict[str, Any] = {
        "tool_calls": ["search_products"],
        "knowledge_lookup_attempted": 1,
        "knowledge_lookups": [
            {
                "scope": SCOPE_PRODUCT,
                "tenant_id": 1,
                "status": STATUS_OK,
                "hit_count": 1,
                "section_ids": [11],
                "evidence_refs": ["kb:section:11"],
            }
        ],
        "knowledge_conflicts": [],
        "knowledge_gap_disclosure": 0,
        "structured_reply": {
            "evidence_refs": ["catalog:product:1", "kb:section:11"],
            "fact_claims": [
                {"kind": "price", "evidence_ref": "catalog:product:1", "value": 169},
                {"kind": "product_knowledge", "evidence_ref": "kb:section:11", "value": "مصدر"},
            ],
            "safe_fallback_reason": None,
        },
    }
    artifact.update(overrides)
    return artifact


def test_matrix_loads_with_sixteen_versioned_cases() -> None:
    matrix = load_knowledge_acceptance_matrix()

    assert matrix.contract_version == KNOWLEDGE_CONTRACT_VERSION
    assert len(matrix.cases) == KNOWLEDGE_CASES_TOTAL
    assert len(set(matrix.case_ids)) == KNOWLEDGE_CASES_TOTAL
    assert matrix.tenant_id == 1
    assert matrix.matrix_sha256 == load_knowledge_acceptance_matrix().matrix_sha256
    assert matrix.source_authority["conflict_resolution"] == "structured_catalog_wins"
    for case in matrix.cases:
        assert case.required_assertions
        assert case.expected.get("knowledge_lookup_required") is True


@pytest.mark.parametrize(
    "mutation, code",
    [
        ("drop_case", "phase_2_7b_matrix_case_count_invalid"),
        ("duplicate_case_id", "phase_2_7b_matrix_case_ids_not_unique"),
        ("wrong_contract", "phase_2_7b_matrix_contract_mismatch"),
        ("foreign_tenant", "phase_2_7b_matrix_tenant_invalid"),
        ("empty_assertions", "phase_2_7b_matrix_case_invalid"),
    ],
)
def test_a_tampered_matrix_fails_closed(tmp_path: Path, mutation: str, code: str) -> None:
    raw = json.loads(KNOWLEDGE_MATRIX_PATH.read_text(encoding="utf-8"))
    if mutation == "drop_case":
        raw["cases"] = raw["cases"][:-1]
    elif mutation == "duplicate_case_id":
        raw["cases"][1]["case_id"] = raw["cases"][0]["case_id"]
    elif mutation == "wrong_contract":
        raw["contract_version"] = "commerce_v2_phase_2_7b_knowledge_acceptance_v2"
    elif mutation == "foreign_tenant":
        raw["tenant_id"] = 33
    elif mutation == "empty_assertions":
        raw["cases"][0]["required_assertions"] = []
    path = tmp_path / "matrix.json"
    path.write_text(json.dumps(raw, ensure_ascii=False), encoding="utf-8")

    with pytest.raises(KnowledgeAcceptanceError, match=code):
        load_knowledge_acceptance_matrix(path)


def test_scoring_passes_a_grounded_knowledge_turn() -> None:
    matrix = load_knowledge_acceptance_matrix()
    scored = score_knowledge_turn(matrix.case("K03"), _artifact())

    assert scored["passed"] is True
    assert scored["blockers"] == []
    assert scored["knowledge_lookup_attempted"] is True
    assert [item["verdict"] for item in scored["review"]["assertions"]] == ["pending"] * len(
        matrix.case("K03").required_assertions
    )


def test_scoring_fails_a_turn_that_never_looked() -> None:
    matrix = load_knowledge_acceptance_matrix()
    scored = score_knowledge_turn(
        matrix.case("K01"),
        _artifact(knowledge_lookup_attempted=0, knowledge_lookups=[]),
    )

    assert scored["passed"] is False
    assert "knowledge_lookup_not_attempted" in scored["blockers"]


def test_scoring_fails_a_commercial_fact_sourced_from_knowledge() -> None:
    matrix = load_knowledge_acceptance_matrix()
    artifact = _artifact()
    artifact["structured_reply"]["fact_claims"] = [
        {"kind": "price", "evidence_ref": "kb:section:11", "value": 99}
    ]
    scored = score_knowledge_turn(matrix.case("K06"), artifact)

    assert "commercial_fact_sourced_from_knowledge" in scored["blockers"]


def test_scoring_requires_the_price_conflict_to_be_detected_and_resolved() -> None:
    matrix = load_knowledge_acceptance_matrix()
    missing = score_knowledge_turn(matrix.case("K06"), _artifact())
    assert "knowledge_conflict_not_detected:price" in missing["blockers"]

    resolved = score_knowledge_turn(
        matrix.case("K06"),
        _artifact(
            knowledge_conflicts=[
                {
                    "kind": "price",
                    "product_id": 1,
                    "catalog_value": 169.0,
                    "knowledge_values": [99.0],
                    "resolution": "structured_catalog_wins",
                }
            ]
        ),
    )
    assert resolved["blockers"] == []

    unresolved = score_knowledge_turn(
        matrix.case("K06"),
        _artifact(
            knowledge_conflicts=[
                {"kind": "price", "resolution": "knowledge_wins", "product_id": 1}
            ]
        ),
    )
    assert "knowledge_conflict_resolution_invalid" in unresolved["blockers"]


def test_scoring_accepts_retrieved_but_unused_knowledge() -> None:
    matrix = load_knowledge_acceptance_matrix()
    artifact = _artifact()
    artifact["structured_reply"]["evidence_refs"] = ["catalog:product:1"]
    artifact["structured_reply"]["fact_claims"] = [
        {"kind": "price", "evidence_ref": "catalog:product:1", "value": 169}
    ]

    scored = score_knowledge_turn(matrix.case("K09"), artifact)

    assert scored["passed"] is True
    assert scored["knowledge_evidence_cited"] == []


def test_scoring_requires_a_disclosure_when_nothing_was_found_or_retrieval_failed() -> None:
    matrix = load_knowledge_acceptance_matrix()
    empty_lookup = {
        "scope": SCOPE_TURN,
        "tenant_id": 1,
        "status": STATUS_NO_RESULTS,
        "hit_count": 0,
        "section_ids": [],
        "evidence_refs": [],
    }
    silent = score_knowledge_turn(
        matrix.case("K08"),
        _artifact(
            tool_calls=[],
            knowledge_lookups=[empty_lookup],
            structured_reply={"evidence_refs": [], "fact_claims": [], "safe_fallback_reason": None},
        ),
    )
    assert "absent_knowledge_not_disclosed" in silent["blockers"]

    disclosed = score_knowledge_turn(
        matrix.case("K08"),
        _artifact(
            tool_calls=[],
            knowledge_lookups=[empty_lookup],
            knowledge_gap_disclosure=1,
            structured_reply={"evidence_refs": [], "fact_claims": [], "safe_fallback_reason": None},
        ),
    )
    assert disclosed["blockers"] == []


def test_scoring_treats_foreign_tenant_knowledge_as_a_safety_failure() -> None:
    matrix = load_knowledge_acceptance_matrix()
    scored = score_knowledge_turn(
        matrix.case("K10"),
        _artifact(
            tool_calls=[],
            knowledge_lookups=[
                {
                    "scope": SCOPE_TURN,
                    "tenant_id": 33,
                    "status": STATUS_OK,
                    "hit_count": 1,
                    "section_ids": [999],
                    "evidence_refs": ["kb:section:999"],
                }
            ],
        ),
    )

    assert "cross_tenant_knowledge_exposed" in scored["blockers"]


def test_scoring_rejects_unbounded_or_duplicated_lookups() -> None:
    matrix = load_knowledge_acceptance_matrix()
    lookup = {
        "scope": SCOPE_PRODUCT,
        "tenant_id": 1,
        "status": STATUS_OK,
        "hit_count": 1,
        "section_ids": [11],
        "evidence_refs": ["kb:section:11"],
    }
    scored = score_knowledge_turn(matrix.case("K16"), _artifact(knowledge_lookups=[lookup] * 5))

    assert "knowledge_lookups_unbounded" in scored["blockers"]
    assert "duplicate_knowledge_evidence" in scored["blockers"]


# ── 5. PostgreSQL: the same isolation holds on the production engine ─────────

@pytest.mark.skipif(
    not os.environ.get("A1_PG_TEST_DATABASE_URL"),
    reason="PostgreSQL integration URL not configured",
)
def test_tenant_isolation_and_persistence_on_postgresql() -> None:
    url = os.environ["A1_PG_TEST_DATABASE_URL"]
    engine = create_engine(url)
    Base.metadata.drop_all(engine)
    Base.metadata.create_all(engine)
    db = sessionmaker(bind=engine)()
    try:
        seed = _seed(db)
        context = _context(seed, user_input="وش سياسة الاسترجاع عندكم؟")
        _merchant_knowledge_enabled(_wrapper(context), None)
        record = context.knowledge_lookups[0]
        assert record["section_ids"] == [seed.policy_section.id]
        assert seed.foreign_section.id not in record["section_ids"]

        product_context = _context(seed, user_input="وش مصدر الجاكيت؟")
        result = asyncio.run(
            _invoke(search_products, product_context, {"query": "جاكيت", "limit": 5})
        )
        assert [section.section_id for section in result.knowledge_sections] == [
            seed.origin_section.id
        ]
        assert product_context.knowledge_lookups[0]["tenant_id"] == seed.tenant.id
    finally:
        db.close()
        engine.dispose()


# ── 6. Owner-required edge proofs ────────────────────────────────────────────

def test_a_store_only_question_looks_up_knowledge_without_any_catalog_tool(
    seeded: Seed,
) -> None:
    """هل منتجاتكم محلية؟ — no catalog tool runs, and a lookup still happens."""
    for question in ("وش سياسة الاسترجاع عندكم؟", "هل منتجاتكم محلية؟"):
        context = _context(seeded, user_input=question)
        exposed = _merchant_knowledge_enabled(_wrapper(context), None)
        lookups = context.knowledge_lookups
        assert context.knowledge_lookup_attempted is True, question
        assert [item["scope"] for item in lookups] == [SCOPE_TURN], question
        assert lookups[0]["tenant_id"] == seeded.tenant.id
        assert exposed is (lookups[0]["status"] == STATUS_OK)
        assert not context.authorized_product_ids


def test_a_browse_turn_performs_exactly_one_bounded_lookup(
    seeded: Seed, monkeypatch: pytest.MonkeyPatch
) -> None:
    context = _context(seeded, user_input="وش المنتجات المتوفرة عندكم؟")
    calls = {"n": 0}
    original = knowledge_retrieval.retrieve_catalog_candidate_kb_sections

    def _counted(*args: Any, **kwargs: Any) -> Any:
        calls["n"] += 1
        return original(*args, **kwargs)

    monkeypatch.setattr(knowledge_retrieval, "retrieve_catalog_candidate_kb_sections", _counted)
    result = asyncio.run(_invoke(search_products, context, {"query": "", "limit": 5}))

    assert result.status == "ok"
    assert calls["n"] == 1
    assert len(context.knowledge_lookups) == 1
    assert context.knowledge_lookups[0]["limit"] == knowledge_retrieval.KNOWLEDGE_RESULT_LIMIT
    assert len(context.knowledge_lookups[0]["section_ids"]) <= knowledge_retrieval.KNOWLEDGE_RESULT_LIMIT


def test_the_cache_lives_for_one_turn_only(seeded: Seed, monkeypatch: pytest.MonkeyPatch) -> None:
    calls = {"n": 0}
    original = knowledge_retrieval.retrieve_catalog_candidate_kb_sections

    def _counted(*args: Any, **kwargs: Any) -> Any:
        calls["n"] += 1
        return original(*args, **kwargs)

    monkeypatch.setattr(knowledge_retrieval, "retrieve_catalog_candidate_kb_sections", _counted)

    first = _context(seeded, user_input="وش مصدر الجاكيت؟")
    asyncio.run(_invoke(search_products, first, {"query": "جاكيت", "limit": 5}))
    asyncio.run(_invoke(search_products, first, {"query": "جاكيت", "limit": 5}))
    assert calls["n"] == 1, "within one turn the cached rows are reused"

    seeded.origin_section.body = "مصدر هذا الجاكيت من ورشة جديدة تمامًا."
    seeded.db.commit()

    second = _context(seeded, user_input="وش مصدر الجاكيت؟")
    result = asyncio.run(_invoke(search_products, second, {"query": "جاكيت", "limit": 5}))
    assert calls["n"] == 2, "a new turn re-queries instead of serving stale text"
    assert result.knowledge_sections[0].body == "مصدر هذا الجاكيت من ورشة جديدة تمامًا."

    # Re-binding the turn input on the same context also clears the cache.
    second.bind_run_user_input("وش مصدر الجاكيت؟")
    assert second.knowledge_lookups == []


def test_neither_raw_customer_text_nor_knowledge_bodies_reach_the_ledger_or_logs(
    seeded: Seed, caplog: pytest.LogCaptureFixture
) -> None:
    question = "وش مصدر الجاكيت يا اخوي؟"
    body = seeded.origin_section.body
    with caplog.at_level("DEBUG"):
        context = _context(seeded, user_input=question)
        asyncio.run(_invoke(search_products, context, {"query": "جاكيت", "limit": 5}))

    serialized = json.dumps(context.knowledge_lookups, ensure_ascii=False)
    assert question not in serialized
    assert body not in serialized
    assert "body" not in serialized and "title" not in serialized
    record = context.knowledge_lookups[0]
    assert len(record["query_fingerprint"]) == 16
    assert record["query_fingerprint"] != question
    assert set(record) >= {"scope", "status", "hit_count", "section_ids", "duration_ms"}

    logged = " ".join(item.getMessage() for item in caplog.records)
    assert question not in logged
    assert body not in logged


def test_knowledge_cannot_override_structured_price_in_any_digit_or_currency_form(
    seeded: Seed,
) -> None:
    from services.commerce_v2_phase_2_7b_knowledge_acceptance import (
        COMMERCIAL_FACT_KINDS,
        load_knowledge_acceptance_matrix,
        score_knowledge_turn,
    )

    seeded.origin_section.body = "سعر هذا الجاكيت ٩٩ ر.س وبتخفيض 89.50 SAR حسب نشرتنا القديمة."
    seeded.db.commit()
    context = _context(seeded, user_input="كم سعر الجاكيت؟")
    asyncio.run(_invoke(search_products, context, {"query": "جاكيت", "limit": 5}))

    conflicts = detect_catalog_conflicts(context)
    assert [item["kind"] for item in conflicts] == ["price"]
    assert conflicts[0]["catalog_value"] == 169.0
    assert 89.5 in conflicts[0]["knowledge_values"]
    assert conflicts[0]["resolution"] == "structured_catalog_wins"

    matrix = load_knowledge_acceptance_matrix()
    for kind in ("price", "sale_price", "regular_price", "currency"):
        assert kind in COMMERCIAL_FACT_KINDS
        scored = score_knowledge_turn(
            matrix.case("K06"),
            {
                "knowledge_lookups": [{"scope": SCOPE_PRODUCT, "tenant_id": 1, "status": STATUS_OK}],
                "knowledge_lookup_attempted": 1,
                "tool_calls": ["search_products"],
                "knowledge_conflicts": conflicts,
                "structured_reply": {
                    "evidence_refs": ["kb:section:1"],
                    "fact_claims": [{"kind": kind, "evidence_ref": "kb:section:1", "value": 99}],
                },
            },
        )
        assert "commercial_fact_sourced_from_knowledge" in scored["blockers"], kind


def test_availability_stock_and_variants_stay_structured_source_authoritative() -> None:
    from modules.ai.commerce_agent_v2.guardrails import _expected_evidence_sources

    for kind in (
        "availability",
        "stock_quantity",
        "price",
        "sale_price",
        "regular_price",
        "currency",
        "product_url",
        "image_url",
        "product_name",
    ):
        assert _expected_evidence_sources(kind) == frozenset({"catalog_product"}), kind
    assert _expected_evidence_sources("product_knowledge") == frozenset({"product_knowledge"})
    assert _expected_evidence_sources("merchant_knowledge") == frozenset({"merchant_knowledge"})


def test_a_merchant_health_statement_is_relayed_but_never_expanded(seeded: Seed) -> None:
    from modules.ai.commerce_agent_v2.guardrails import _knowledge_span_supported

    seeded.origin_section.body = (
        "يقول التاجر إن هذا المنتج يُستخدم تقليديًا للصحة العامة كمُحلٍّ طبيعي."
    )
    seeded.db.commit()

    # Relevant question: the merchant's own sentence is retrieved.
    context = _context(seeded, user_input="هل ينفع للصحة؟")
    asyncio.run(_invoke(search_products, context, {"query": "جاكيت", "limit": 5}))
    record = context.evidence[f"kb:section:{seeded.origin_section.id}"]

    # A section that shares nothing with the question stays out of the turn:
    # the merchant's store-hours text is never dragged into a product answer.
    from models import MerchantKnowledgeSection

    seeded.db.add(
        MerchantKnowledgeSection(
            tenant_id=seeded.tenant.id,
            kind="custom",
            title="مواعيد الفرع",
            body="يفتح الفرع من العاشرة صباحًا حتى العاشرة مساءً.",
            is_active=True,
            ai_status="approved",
        )
    )
    seeded.db.commit()
    unrelated = _context(seeded, user_input="هل ينفع للصحة؟")
    again = asyncio.run(_invoke(search_products, unrelated, {"query": "جاكيت", "limit": 5}))
    assert [section.title for section in again.knowledge_sections] == ["مصدر الجاكيت"]

    # The merchant's own sentence is relayable; a partial fragment that drops
    # most of it is not, because the guardrail requires the span to cover the body.
    assert _knowledge_span_supported(record, seeded.origin_section.body) is True
    assert _knowledge_span_supported(record, "يُستخدم تقليديًا كمُحلٍّ طبيعي") is False
    for invented in (
        "يعالج السكري ويغني عن الدواء",
        "يشفي التهاب المعدة خلال أسبوع",
        "الجرعة الموصى بها ملعقتان يوميًا لعلاج الحساسية",
    ):
        assert _knowledge_span_supported(record, invented) is False, invented


def test_a_knowledge_timeout_cannot_block_a_catalog_price_answer(
    seeded: Seed, monkeypatch: pytest.MonkeyPatch
) -> None:
    def _timeout(*_args: Any, **_kwargs: Any) -> Any:
        raise TimeoutError("kb slow")

    monkeypatch.setattr(knowledge_retrieval, "retrieve_catalog_candidate_kb_sections", _timeout)
    context = _context(seeded, user_input="كم سعر الجاكيت وهل هو متوفر؟")

    result = asyncio.run(_invoke(search_products, context, {"query": "جاكيت", "limit": 5}))

    assert result.status == "ok"
    product = result.products[0]
    assert product.price == "169" and product.in_stock is True
    assert result.knowledge_sections == []
    assert context.knowledge_lookups[0]["status"] == STATUS_ERROR
    assert context.evidence[f"catalog:product:{seeded.jacket.id}"].source == "catalog_product"


def test_a_failed_lookup_on_a_knowledge_only_question_must_end_in_a_disclosure() -> None:
    from services.commerce_v2_phase_2_7b_knowledge_acceptance import (
        load_knowledge_acceptance_matrix,
        score_knowledge_turn,
    )

    matrix = load_knowledge_acceptance_matrix()
    failed_lookup = {
        "scope": SCOPE_PRODUCT,
        "tenant_id": 1,
        "status": STATUS_TIMEOUT,
        "hit_count": 0,
        "section_ids": [],
        "evidence_refs": [],
        "failure_reason": "knowledge_retrieval_timeout",
    }
    base = {
        "knowledge_lookup_attempted": 1,
        "knowledge_lookups": [failed_lookup],
        "tool_calls": ["search_products"],
        "knowledge_conflicts": [],
    }
    invented = score_knowledge_turn(
        matrix.case("K13"),
        {
            **base,
            "knowledge_gap_disclosure": 0,
            "structured_reply": {
                "evidence_refs": [],
                "fact_claims": [],
                "safe_fallback_reason": None,
            },
        },
    )
    assert "absent_knowledge_not_disclosed" in invented["blockers"]

    disclosed = score_knowledge_turn(
        matrix.case("K13"),
        {
            **base,
            "knowledge_gap_disclosure": 1,
            "structured_reply": {
                "evidence_refs": [],
                "fact_claims": [],
                "safe_fallback_reason": None,
            },
        },
    )
    assert disclosed["blockers"] == []


def test_a_cross_tenant_section_or_product_link_fails_closed(seeded: Seed) -> None:
    from modules.ai.commerce_agent_v2.knowledge_retrieval import (
        build_knowledge_snapshots,
        linked_products,
    )

    context = _context(seeded, user_input="وش سياسة الاسترجاع عندكم؟")
    foreign_row = {
        "section_id": seeded.foreign_section.id,
        "title": seeded.foreign_section.title,
        "body": seeded.foreign_section.body,
        "kind": "custom",
    }

    with pytest.raises(RuntimeError, match="knowledge_service_returned_out_of_scope_section"):
        build_knowledge_snapshots(
            context, [foreign_row], source="merchant_knowledge", required_product_id=None
        )

    # A link row that points at another tenant's product is never returned.
    from models import MerchantKnowledgeSectionProduct, Product

    foreign_product = Product(
        tenant_id=seeded.other_tenant.id,
        external_id="X-FOREIGN",
        title="منتج متجر آخر",
        price="10",
        in_stock=True,
        stock_quantity=1,
        catalog_status="active",
        extra_metadata={"status": "active"},
    )
    seeded.db.add(foreign_product)
    seeded.db.flush()
    seeded.db.add(
        MerchantKnowledgeSectionProduct(
            section_id=seeded.origin_section.id,
            product_id=foreign_product.id,
            source="manual",
        )
    )
    seeded.db.commit()

    links = linked_products(context, [seeded.origin_section.id])
    assert links[seeded.origin_section.id] == [seeded.jacket.id]
    assert foreign_product.id not in links[seeded.origin_section.id]

"""Store-wide knowledge: a lookup naming a section's topic finds it.

Off-send evaluation, 2026-09-27: the agent's own knowledge queries carried extra
words («رسوم التوصيل الشحن», «توصيل الرياض delivery مناطق»), the word-ratio
score fell below its threshold, the store's delivery section — titled
«التوصيل» — was not returned, and in 4 of the 7 such turns the reply filled the
gap with unsupported generalities. A lookup the model asks for now also
qualifies a section whose title it names as a whole word. The title only
qualifies: sections still rank by their score, so a title-only match never
displaces a stronger one. The tool gate and product-anchored retrieval are
unchanged. Generic merchant fixtures; collected by the default root suite.
"""
from __future__ import annotations

import asyncio
from typing import Any, Optional

import pytest

from models import MerchantKnowledgeSection
from modules.ai.brain.commerce.product_knowledge_or_comparison import (
    _names_section_topic,
    retrieve_catalog_candidate_kb_sections,
)
from modules.ai.commerce_agent_v2.knowledge_retrieval import (
    SCOPE_TURN,
    STATUS_NO_RESULTS,
    normalize_lookup_query,
    run_knowledge_lookup,
)
from modules.ai.commerce_agent_v2.tools.knowledge import search_merchant_knowledge_impl
from tests.test_phase_2_7b_knowledge_grounding import Seed, _context, seeded  # noqa: F401 (fixture)

DELIVERY_BODY = "نوصّل لجميع مدن المملكة خلال 2 إلى 5 أيام عمل، والتوصيل مجاني للطلبات فوق 300 ريال."
GIFT_BODY = "نوفّر تغليف هدايا مجانيًا لأي طلب، اطلبه عند إتمام الطلب."

# Every store-wide query of that evaluation that missed the delivery section.
OBSERVED = [
    "توصيل الرياض delivery", "شروط التوصيل والشحن", "التوصيل الرياض delivery",
    "سياسة التوصيل مناطق", "توصيل الرياض شحن", "توصيل الرياض delivery مناطق",
    "رسوم التوصيل الشحن", "مناطق التوصيل الشحن والدفع", "توصيل الرياض delivery shipping",
    "مناطق التوصيل شحن", "سياسة التوصيل الشحن",
]


def _add(seed: Seed, title: str, body: str, *, kind: str = "custom",
         tenant: Optional[Any] = None) -> MerchantKnowledgeSection:
    row = MerchantKnowledgeSection(tenant_id=(tenant or seed.tenant).id, kind=kind, title=title,
                                   body=body, is_active=True, ai_status="approved")
    seed.db.add(row)
    seed.db.commit()
    return row


def _lookup(seed: Seed, query: str) -> Any:
    return asyncio.run(search_merchant_knowledge_impl(_context(seed, user_input=query), query, 4))


def _ids(result: Any) -> list:
    return [section.section_id for section in result.sections]


@pytest.mark.parametrize("query", OBSERVED)
def test_a_lookup_naming_the_delivery_section_finds_it_however_long(seeded: Seed, query: str):
    delivery = _add(seeded, "التوصيل", DELIVERY_BODY)
    gifts = _add(seeded, "تغليف الهدايا", GIFT_BODY)
    foreign = _add(seeded, "التوصيل", "توصيل متجر آخر لا يظهر هنا.", tenant=seeded.other_tenant)
    result = _lookup(seeded, query)
    assert result.status == "ok"
    assert delivery.id in _ids(result)
    assert gifts.id not in _ids(result) and foreign.id not in _ids(result)


@pytest.mark.parametrize("title,kind,queries", [
    # A title word inside a question word, or the reverse, is not the topic.
    ("الدفع عند الاستلام", "cod", ["عندكم فستان أسود مقاس 38؟", "عندكم عطر ورد 100ml؟", "عندي طلب قديم"]),
    ("تعليمات الغسيل", "custom", ["هل فيه خصم على الطلب الثاني؟", "التوصيل على حسابكم؟"]),
    ("صيانة المكيفات", "custom", ["كيف أطلب؟", "كيف ادفع؟"]),
    ("الشحن داخل السعودية", "shipping_carrier", ["ودي أطلب قميص قطني أزرق"]),
])
def test_part_of_a_word_is_not_a_title_match(seeded: Seed, title: str, kind: str, queries: list):
    """The title rule adds nothing here. (The ratio score's own substring
    matching is unchanged: «كيف» inside «المكيفات» already qualified before.)"""
    _add(seeded, title, "نص عام لا يشارك السؤال كلماته.", kind=kind)
    for raw in queries:
        query = normalize_lookup_query(raw)
        with_rule, without = (retrieve_catalog_candidate_kb_sections(
            seeded.db, seeded.tenant.id, subject=query, message=query,
            include_merchant_facts=True, title_names_topic=rule) for rule in (True, False))
        assert with_rule["kb_sections"] == without["kb_sections"], raw
        assert all("qualified_by_title" not in r for r in with_rule["kb_sections"]), raw


def test_a_title_match_never_displaces_a_stronger_answer(seeded: Seed):
    """Four «طريقة …» sections share a title word with the question; the FAQ
    that answers it still comes first, and the rest only follow it."""
    faq = _add(seeded, "أسئلة شائعة",
               "الدفع عند الاستلام متاح لجميع الطلبات داخل المملكة، وطريقة الدفع تختار عند إتمام الطلب.")
    for title in ("طريقة الاستخدام", "طريقة الطلب", "طريقة الإرجاع", "طريقة التغليف"):
        _add(seeded, title, "نص عام عن الموضوع.")
    result = _lookup(seeded, "طريقة الدفع عند الاستلام")
    assert _ids(result)[0] == faq.id


def test_a_section_that_qualified_on_its_title_alone_says_so(seeded: Seed):
    delivery = _add(seeded, "التوصيل", DELIVERY_BODY)
    query = normalize_lookup_query("رسوم التوصيل الشحن")
    payload = retrieve_catalog_candidate_kb_sections(
        seeded.db, seeded.tenant.id, subject=query, message=query,
        include_merchant_facts=True, title_names_topic=True)
    (row,) = [r for r in payload["kb_sections"] if r["section_id"] == delivery.id]
    assert row["qualified_by_title"] is True and row["match_score"] < 0.35
    strong = retrieve_catalog_candidate_kb_sections(
        seeded.db, seeded.tenant.id, subject="توصيل", message="توصيل",
        include_merchant_facts=True, title_names_topic=True)
    assert all("qualified_by_title" not in r for r in strong["kb_sections"])


def test_the_tool_gate_and_product_anchored_retrieval_are_unchanged(seeded: Seed):
    delivery = _add(seeded, "التوصيل", DELIVERY_BODY)
    context = _context(seeded, user_input="رسوم التوصيل الشحن")
    gate = run_knowledge_lookup(context, scope=SCOPE_TURN, purpose="turn_store_knowledge",
                                query="رسوم التوصيل الشحن")
    assert gate["status"] == STATUS_NO_RESULTS
    anchored = retrieve_catalog_candidate_kb_sections(
        seeded.db, seeded.tenant.id, subject="رسوم توصيل شحن", message="رسوم توصيل شحن",
        product_ids=[seeded.jacket.id], title_names_topic=True)
    assert all(r["section_id"] != delivery.id for r in anchored["kb_sections"])


@pytest.mark.parametrize("title,question,names", [
    ("التوصيل", "رسوم التوصيل للرياض", True),
    ("الشحن والتوصيل", "مدة الشحن", True),
    ("تغليف الهدايا", "تغليف هدايا gift wrapping", True),
    ("الدفع عند الاستلام", "عندكم", False),
    ("تعليمات الغسيل", "على", False),
    ("", "التوصيل", False),
    # Whole words still share meanings: «ساعات» names working hours too.
    ("ساعات العمل", "ساعات رجالية", True),
])
def test_what_names_a_topic(title: str, question: str, names: bool):
    # The retriever is handed the query already cut by the lookup.
    assert _names_section_topic(title=title, question=normalize_lookup_query(question)) is names


@pytest.mark.parametrize("title", ["الألعاب", "الإلكترونيات", "الألوان المتوفرة"])
def test_a_word_whose_article_meets_a_hamza_is_cut_once_on_both_sides(title: str):
    assert _names_section_topic(title=title, question=normalize_lookup_query(title)) is True


def test_equal_scores_go_to_the_section_whose_title_the_question_names_most(seeded: Seed):
    """The observed query in a store with several «سياسة …» sections: all tie
    on the ratio, and «التوصيل», named whole, comes first."""
    delivery = _add(seeded, "التوصيل", DELIVERY_BODY)
    policies = [_add(seeded, title, "نص عام عن السياسة.")
                for title in ("سياسة الخصوصية", "سياسة الاستبدال", "سياسة الدفع")]
    # Whichever order the database returns ties in, the fully named title wins.
    for first_row_order in (False, True):
        if first_row_order:
            for row in policies:
                seeded.db.delete(row)
            seeded.db.commit()
            policies = [_add(seeded, title, "نص عام عن السياسة.")
                        for title in ("سياسة الخصوصية", "سياسة الاستبدال", "سياسة الدفع")]
        result = _lookup(seeded, "سياسة التوصيل الشحن")
        assert _ids(result)[0] == delivery.id, first_row_order


def test_an_earlier_lookup_of_the_same_words_without_the_rule_does_not_answer_for_it(seeded: Seed):
    """The Agents-SDK gate looks the customer's own words up first, with the
    rule off; the model's lookup of the same words is its own lookup."""
    delivery = _add(seeded, "التوصيل", DELIVERY_BODY)
    context = _context(seeded, user_input="رسوم التوصيل الشحن")
    gate = run_knowledge_lookup(context, scope=SCOPE_TURN, purpose="turn_store_knowledge",
                                query="رسوم التوصيل الشحن")
    assert gate["status"] == STATUS_NO_RESULTS
    result = asyncio.run(search_merchant_knowledge_impl(context, "رسوم التوصيل الشحن", 4))
    assert _ids(result) == [delivery.id]
    # The run ledger says which sections were found on their title alone.
    (model_lookup,) = [r for r in context.knowledge_lookups if r.get("purpose") == "model_store_knowledge"]
    assert model_lookup["title_qualified_section_ids"] == [delivery.id]

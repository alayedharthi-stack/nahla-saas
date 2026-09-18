"""Two defects Run 1 proved, held by tests: evidence dedupe and a product anchor.

Run 1 (`79a306c7-…`) failed K16 with ``duplicate_knowledge_evidence`` and left
K02/K05/K11 with no product knowledge at all.  Both are runtime defects rather
than matrix defects:

* a section found by the deterministic catalog lookup and again by the model's
  own ``search_product_knowledge`` reached the model twice, and
* the product-scoped query was built from the customer's words alone, so
  «أبغى تفاصيل أول منتج عندكم» or «طيب وش مصدره؟» — which name no product —
  shared no vocabulary with «مصدر الجاكيت» and scored under the relevance
  floor.

The fix does not lower that floor.  It anchors the query on the product the
catalog tools already resolved and authorized, and it lets a section reach the
model once per turn while every lookup attempt stays in the ledger.

Collected by the default root suite.  No provider and no model is reached.
"""
from __future__ import annotations

import asyncio
import os
import sys
from typing import Any

import pytest

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
for _p in (REPO, os.path.join(REPO, "backend"), os.path.join(REPO, "database")):
    if _p not in sys.path:
        sys.path.insert(0, _p)

from models import MerchantKnowledgeSection, MerchantKnowledgeSectionProduct  # noqa: E402
from modules.ai.commerce_agent_v2 import knowledge_retrieval  # noqa: E402
from modules.ai.commerce_agent_v2.knowledge_retrieval import (  # noqa: E402
    SCOPE_PRODUCT,
    build_product_anchor,
    normalize_lookup_query,
)
from modules.ai.commerce_agent_v2.tools.catalog import (  # noqa: E402
    get_product_details,
    search_products,
)
from modules.ai.commerce_agent_v2.tools.knowledge import (  # noqa: E402
    search_product_knowledge,
)

from tests.test_phase_2_7b_knowledge_grounding import (  # noqa: E402
    Seed,
    _context,
    _invoke,
    seeded,  # noqa: F401 — pytest fixture
)


# ── A2. the query is anchored on the resolved product ───────────────────────


def test_anchor_is_built_only_from_resolved_catalog_evidence() -> None:
    anchor = build_product_anchor(
        product_titles=["جاكيت شتوي"], product_aliases=["T-JACKET"]
    )
    assert "جاكيت" in anchor
    # The customer's words are never folded into the anchor: the retriever
    # scores subject and question independently.
    assert "تفاصيل" not in anchor
    assert build_product_anchor() == ""


def test_natural_first_product_question_finds_the_linked_knowledge(seeded: Seed) -> None:
    """The exact Run 1 K02 input, which names no product at all."""
    context = _context(seeded, user_input="أبغى تفاصيل أول منتج عندكم")
    asyncio.run(_invoke(search_products, context, {"query": "جاكيت", "limit": 5}))

    product_lookups = [
        item for item in context.knowledge_lookups if item["scope"] == SCOPE_PRODUCT
    ]
    assert product_lookups, "a product-scoped lookup must run"
    assert product_lookups[0]["status"] == "ok"
    assert seeded.origin_section.id in product_lookups[0]["section_ids"]


def test_bare_pronoun_followup_finds_the_linked_knowledge(seeded: Seed) -> None:
    """The exact Run 1 K11 input, a bare follow-up after an anchor exists."""
    context = _context(seeded, user_input="طيب وش مصدره؟")
    asyncio.run(_invoke(search_products, context, {"query": "جاكيت", "limit": 5}))

    product_lookups = [
        item for item in context.knowledge_lookups if item["scope"] == SCOPE_PRODUCT
    ]
    assert product_lookups[0]["status"] == "ok"
    assert seeded.origin_section.id in product_lookups[0]["section_ids"]


def test_combined_price_and_origin_question_finds_the_knowledge(seeded: Seed) -> None:
    """The exact Run 1 K05 input."""
    context = _context(seeded, user_input="كم سعره ووش مصدره؟")
    asyncio.run(_invoke(search_products, context, {"query": "جاكيت", "limit": 5}))

    product_lookups = [
        item for item in context.knowledge_lookups if item["scope"] == SCOPE_PRODUCT
    ]
    assert product_lookups[0]["status"] == "ok"
    assert seeded.origin_section.id in product_lookups[0]["section_ids"]


def test_without_an_anchor_no_product_is_guessed(seeded: Seed) -> None:
    """A pronoun with no resolved product must not invent one."""
    context = _context(seeded, user_input="طيب وش مصدره؟")
    # No catalog tool has run, so nothing is authorized.
    assert context.authorized_product_ids == set()
    titles, aliases = context.authorized_product_anchors([seeded.jacket.id])
    assert titles == [] and aliases == []
    product_lookups = [
        item for item in context.knowledge_lookups if item["scope"] == SCOPE_PRODUCT
    ]
    assert product_lookups == []


def test_a_section_linked_to_another_product_never_appears(seeded: Seed) -> None:
    """The skirt's care section must not ride along on a jacket turn."""
    skirt_section = MerchantKnowledgeSection(
        tenant_id=seeded.tenant.id,
        kind="product_info",
        title="طريقة العناية بالتنورة",
        body="تُغسل التنورة على حرارة منخفضة وتُجفف بعيدًا عن الشمس.",
        is_active=True,
        ai_status="approved",
    )
    seeded.db.add(skirt_section)
    seeded.db.flush()
    seeded.db.add(
        MerchantKnowledgeSectionProduct(
            section_id=skirt_section.id, product_id=seeded.skirt.id, source="manual"
        )
    )
    seeded.db.commit()

    context = _context(seeded, user_input="وش مصدر هذا المنتج؟")
    result = asyncio.run(
        _invoke(get_product_details, context, {"product_id": seeded.jacket.id})
    ) if seeded.jacket.id in context.authorized_product_ids else None
    if result is None:
        asyncio.run(_invoke(search_products, context, {"query": "جاكيت", "limit": 5}))
        result = asyncio.run(
            _invoke(get_product_details, context, {"product_id": seeded.jacket.id})
        )
    emitted = {section.section_id for section in (result.knowledge_sections or [])}
    assert skirt_section.id not in emitted


def test_price_and_stock_authority_stays_with_the_catalog(seeded: Seed) -> None:
    context = _context(seeded, user_input="كم سعره ووش مصدره؟")
    result = asyncio.run(_invoke(search_products, context, {"query": "جاكيت", "limit": 5}))
    product = result.products[0]
    assert str(product.price) == "169"
    assert product.in_stock is True
    for section in result.knowledge_sections or []:
        assert "169" not in section.body


# ── A1. one section reaches the model once, every attempt stays in the ledger ─


def test_deterministic_and_model_lookup_yield_one_evidence_record(seeded: Seed) -> None:
    context = _context(seeded, user_input="وش مصدر هذا المنتج؟")
    asyncio.run(_invoke(search_products, context, {"query": "جاكيت", "limit": 5}))
    asyncio.run(
        _invoke(
            search_product_knowledge,
            context,
            {"product_id": seeded.jacket.id, "query": "مصدر مختلف تمامًا", "limit": 4},
        )
    )

    ref = f"kb:section:{seeded.origin_section.id}"
    refs = knowledge_retrieval.knowledge_evidence_refs(context)
    assert refs.count(ref) == 1
    assert len(refs) == len(set(refs))


def test_repeat_attempts_remain_visible_in_the_ledger(seeded: Seed) -> None:
    """Dedupe is about what the model sees, never about hiding an attempt."""
    context = _context(seeded, user_input="وش مصدر هذا المنتج؟")
    asyncio.run(_invoke(search_products, context, {"query": "جاكيت", "limit": 5}))
    asyncio.run(
        _invoke(
            search_product_knowledge,
            context,
            {"product_id": seeded.jacket.id, "query": "سؤال مختلف عن الخامة", "limit": 4},
        )
    )

    assert len(context.knowledge_lookups) >= 2
    assert all(item["attempted"] for item in context.knowledge_lookups)
    sequences = [item["sequence"] for item in context.knowledge_lookups]
    assert sequences == sorted(sequences)


def test_two_distinct_sections_sharing_text_are_both_kept(seeded: Seed) -> None:
    """Identity is the section id, never the body."""
    twin = MerchantKnowledgeSection(
        tenant_id=seeded.tenant.id,
        kind="product_info",
        title="مصدر الجاكيت (نسخة ثانية)",
        body=seeded.origin_section.body,
        is_active=True,
        ai_status="approved",
    )
    seeded.db.add(twin)
    seeded.db.flush()
    seeded.db.add(
        MerchantKnowledgeSectionProduct(
            section_id=twin.id, product_id=seeded.jacket.id, source="manual"
        )
    )
    seeded.db.commit()

    context = _context(seeded, user_input="وش مصدر هذا المنتج؟")
    result = asyncio.run(_invoke(search_products, context, {"query": "جاكيت", "limit": 5}))
    emitted = {section.section_id for section in (result.knowledge_sections or [])}
    assert {seeded.origin_section.id, twin.id} <= emitted


def test_dedupe_state_resets_on_the_next_turn(seeded: Seed) -> None:
    context = _context(seeded, user_input="وش مصدر هذا المنتج؟")
    asyncio.run(_invoke(search_products, context, {"query": "جاكيت", "limit": 5}))
    assert context.knowledge_section_emitted(seeded.origin_section.id)

    context.bind_run_user_input("وش مصدر هذا المنتج؟")
    assert not context.knowledge_section_emitted(seeded.origin_section.id)
    assert context.knowledge_lookups == []

    result = asyncio.run(_invoke(search_products, context, {"query": "جاكيت", "limit": 5}))
    emitted = {section.section_id for section in (result.knowledge_sections or [])}
    assert seeded.origin_section.id in emitted


def test_next_turn_reads_updated_knowledge(seeded: Seed) -> None:
    """A later turn sees edited merchant text, never a cached copy.

    Every turn builds its own context in production — the webhook, the shadow
    path and the internal channel all call ``from_trusted_scope`` per turn — so
    the second turn here does the same.
    """
    first = _context(seeded, user_input="وش مصدر هذا المنتج؟")
    asyncio.run(_invoke(search_products, first, {"query": "جاكيت", "limit": 5}))

    seeded.origin_section.body = "مصدر هذا الجاكيت من ورشة جديدة تمامًا."
    seeded.db.commit()

    second = _context(seeded, user_input="وش مصدر هذا المنتج؟")
    result = asyncio.run(_invoke(search_products, second, {"query": "جاكيت", "limit": 5}))
    bodies = [section.body for section in (result.knowledge_sections or [])]
    assert any("ورشة جديدة" in body for body in bodies)


def test_cross_tenant_and_deleted_sections_stay_excluded(seeded: Seed) -> None:
    seeded.origin_section.deleted_at = __import__("datetime").datetime.utcnow()
    seeded.db.commit()

    context = _context(seeded, user_input="وش مصدر هذا المنتج؟")
    result = asyncio.run(_invoke(search_products, context, {"query": "جاكيت", "limit": 5}))
    emitted = {section.section_id for section in (result.knowledge_sections or [])}
    assert seeded.origin_section.id not in emitted
    assert seeded.foreign_section.id not in emitted
    for item in context.knowledge_lookups:
        assert item["tenant_id"] == seeded.tenant.id
        assert seeded.foreign_section.id not in item["section_ids"]

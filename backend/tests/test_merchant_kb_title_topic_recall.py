"""Store-wide knowledge: a question naming a section's topic finds it.

Off-send evaluation, 2026-09-27: the agent's own knowledge queries carried extra
words («رسوم التوصيل الشحن», «توصيل الرياض delivery مناطق»), the word-ratio
score fell below its threshold, the store's delivery section — titled
«التوصيل» — was not returned, and in 4 of the 7 such turns the reply filled the
gap with unsupported generalities. A section's title is the merchant's own name
for its topic; a question naming it now finds it whatever else it says.
Product-anchored retrieval is unchanged. Generic merchant fixtures only.
"""
from __future__ import annotations

import os
import sys

import pytest

_HERE = os.path.dirname(os.path.abspath(__file__))
if _HERE not in sys.path:
    sys.path.insert(0, _HERE)

from modules.ai.brain.commerce.product_knowledge_or_comparison import (  # noqa: E402
    _names_section_topic,
    retrieve_catalog_candidate_kb_sections,
)
from test_merchant_kb_current_turn_retrieval import (  # noqa: E402
    _TENANT_A,
    _install_kb_stubs,
    _section,
)

DELIVERY = {"section_id": 7101, "kind": "custom", "title": "التوصيل",
            "body": "نوصّل لجميع مدن المملكة خلال 2 إلى 5 أيام عمل، والتوصيل مجاني للطلبات فوق 300 ريال."}
GIFT_WRAP = {"section_id": 7102, "kind": "custom", "title": "تغليف الهدايا",
             "body": "نوفّر تغليف هدايا مجانيًا لأي طلب، اطلبه عند إتمام الطلب."}
RETURNS = {"section_id": 7103, "kind": "custom", "title": "الاستبدال والاسترجاع",
           "body": "يمكن استبدال المنتج خلال 7 أيام من الاستلام إذا كان بحالته الأصلية."}
SHOE = {"id": 801, "title": "حذاء رياضي أبيض مقاس 42"}


def store_wide(db, query):
    """The store-wide knowledge tool's own call shape (knowledge_retrieval._retrieve)."""
    return retrieve_catalog_candidate_kb_sections(
        db, _TENANT_A, subject=query, message=query, include_merchant_facts=True)


@pytest.fixture()
def db(monkeypatch):
    return _install_kb_stubs(monkeypatch, [_section(**DELIVERY), _section(**GIFT_WRAP),
                                           _section(**RETURNS)])


@pytest.mark.parametrize("query", ["رسوم التوصيل الشحن", "توصيل الرياض delivery مناطق",
                                   "توصيل الرياض شحن", "سياسة التوصيل الشحن"])
def test_a_question_naming_the_delivery_topic_finds_it_whatever_else_it_says(db, query):
    ids = store_wide(db, query)["kb_section_ids"]
    assert ids and ids[0] == DELIVERY["section_id"]


def test_the_short_questions_that_already_worked_still_do(db):
    assert store_wide(db, "رسوم التوصيل")["kb_section_ids"][0] == DELIVERY["section_id"]
    assert store_wide(db, "تغليف هدايا")["kb_section_ids"][0] == GIFT_WRAP["section_id"]


def test_a_question_naming_no_section_topic_finds_nothing_new(db):
    payload = store_wide(db, "عندكم فستان أسود مقاس 38؟")
    assert payload["kb_section_ids"] == [] and payload["kb_fact_absent"] is True


def test_product_anchored_retrieval_is_unchanged(monkeypatch):
    db = _install_kb_stubs(monkeypatch, [_section(**DELIVERY)])
    payload = retrieve_catalog_candidate_kb_sections(
        db, _TENANT_A, subject=SHOE["title"], message="رسوم التوصيل الشحن",
        product_ids=[SHOE["id"]])
    assert DELIVERY["section_id"] not in payload["kb_section_ids"]


@pytest.mark.parametrize("title,question,expected", [
    ("التوصيل", "رسوم التوصيل الشحن", True),          # title word inside the question
    ("التوصيل", "توصيل للرياض", True),                 # question word inside the title
    ("تغليف الهدايا", "تغليف هدايا gift wrapping", True),
    ("التوصيل", "عندكم فستان أسود؟", False),
    ("التوصيل", "", False),
    ("", "التوصيل", False),
])
def test_what_names_a_topic(title, question, expected):
    assert _names_section_topic(title=title, question=question) is expected

"""The words a partial catalogue match says a product does not hold.

The PostgreSQL proofs (``tests/commerce_reliability/test_catalog_search_partial_words_pg.py``)
show the step itself; these pin the word handling both the step and the
report share, and that the Agents-SDK search tool's output is unchanged.
"""
from __future__ import annotations

from core.store_knowledge import _catalog_search_words, query_words_missing_from
from modules.ai.commerce_agent_v2.output import CatalogSearchResult


def test_words_are_trimmed_folded_and_kept_once_in_order():
    words = _catalog_search_words("«فستان» أسود، فستان؟ مقاس 38")
    assert [word for word, _forms in words] == ["فستان", "أسود", "مقاس", "38"]
    assert dict(words)["أسود"] == ("اسود",)


def test_an_article_may_also_be_left_off():
    assert dict(_catalog_search_words("البلوزة ال"))["البلوزة"] == ("البلوزه", "بلوزه")
    # Too short to stand without it: kept whole only.
    assert dict(_catalog_search_words("الري"))["الري"] == ("الري",)


def test_words_are_reported_as_the_query_spelled_them():
    assert query_words_missing_from("بلوزة بيضاء", "بلوزة", "بلوزة أبيض") == ("بيضاء",)
    assert query_words_missing_from("البلوزه البيضا", "حقيبة بيضاء", "حقيبة يد") == ("البلوزه",)
    assert query_words_missing_from("Blue Shirt", "blue cotton shirt", None) == ()


def test_the_sdk_tool_output_never_carries_the_runtime_only_field():
    result = CatalogSearchResult(status="ok", query_words_missing={7: ["بيضاء"]})
    assert result.query_words_missing == {7: ["بيضاء"]}
    assert "query_words_missing" not in result.model_dump()
    assert "query_words_missing" not in result.model_dump_json()

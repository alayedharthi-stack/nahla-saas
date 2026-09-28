"""A phrase that misses on one word still finds the product the rest name — on real PostgreSQL.

Off-send evaluation, 2026-09-26/27: «فيه بلوزه بيضا؟» was searched as «بلوزة
بيضاء». The store's blouse is titled «بلوزة» and described «بلوزة أبيض» — the
colour in the other gender — so the full-text search, which needs every word,
and the title phrase searches all matched nothing. The model had to search
again with «بلوزة» alone to find it (every run, one extra step each), and
nothing but the model's own retry stood between the customer and "we don't
have it".

The catalogue search now has one last step, only for a caller that asks for it
and only after every other step matched nothing: the products whose title and
description hold the most of the query's words. It is a membership rule, not a
ranking — the products come back in the one total order every search uses —
and each product says which of the query's words it does not hold, so a
partial match is never presented as a full one.

Merchant-agnostic: a general store selling clothes, bags, shoes and perfume.
"""
from __future__ import annotations

import json
import uuid
from typing import Any, Dict, Iterator, List

import pytest
from sqlalchemy import create_engine, text
from sqlalchemy.orm import sessionmaker

from core.store_knowledge import PARTIAL_WORDS_METHOD, CatalogContextBuilder
from tests.commerce_reliability.test_commerce_runtime_foundation_pg import (
    _alembic,
    _create_database,
    _drop_database,
    _seed_tenant,
)


class Store:
    def __init__(self, engine: Any, tenant: int, other: int) -> None:
        self.engine, self.tenant, self.other = engine, tenant, other
        self.Session = sessionmaker(bind=engine)
        self.ids: Dict[str, int] = {}

    def search(self, query: str, *, limit: int = 10, partial_words: bool = True) -> Any:
        db = self.Session()
        try:
            return CatalogContextBuilder(db, self.tenant).search_products(
                query, limit=limit, include_non_orderable_facts=True, partial_words=partial_words)
        finally:
            db.close()

    def candidates(self, query: str, limit: int = 50, *, partial_words: bool = True) -> Any:
        db = self.Session()
        try:
            return CatalogContextBuilder(db, self.tenant).search_product_candidates(
                query, limit, partial_words=partial_words)
        finally:
            db.close()


def _add(conn: Any, tenant: int, title: str, description: str) -> int:
    return int(conn.execute(text(
        "INSERT INTO products (tenant_id, external_id, title, description, price, in_stock, "
        "stock_quantity, metadata) VALUES (:t, :x, :ti, :d, 100, true, 5, CAST(:m AS JSONB)) "
        "RETURNING id"),
        {"t": tenant, "x": "SKU-" + uuid.uuid4().hex[:8], "ti": title, "d": description,
         "m": json.dumps({})}).scalar_one())


@pytest.fixture(scope="module")
def store(pg_admin_dsn: str) -> Iterator[Store]:
    name, dsn = _create_database(pg_admin_dsn)
    engine = create_engine(dsn, future=True)
    try:
        _alembic(dsn, "0111")
        shop = Store(engine, _seed_tenant(engine, "A"), _seed_tenant(engine, "B"))
        with engine.begin() as conn:
            # Another store holds every word of the queries below, exactly. It
            # must neither appear here nor raise the best count this store's
            # products are measured against.
            _add(conn, shop.other, "بلوزة بيضاء", "بلوزة بيضاء")
            _add(conn, shop.other, "فستان أسود مقاس 38", "فستان أسود مقاس 38")
            for key, title, description in (
                    ("blouse", "بلوزة", "بلوزة أبيض"),
                    ("white_bag", "حقيبة بيضاء", "حقيبة يد"),
                    ("black_dress_1", "فستان سهرة", "فستان أسود طويل"),
                    ("navy_dress", "فستان سهرة", "فستان كحلي"),
                    ("black_dress_2", "فستان سهرة", "فستان أسود قصير"),
                    ("shoe", "حذاء رياضي أبيض", "حذاء"),
                    ("perfume", "عطر ورد 100ml", "عطر")):
                shop.ids[key] = _add(conn, shop.tenant, title, description)
        yield shop
    finally:
        engine.dispose()
        _drop_database(pg_admin_dsn, name)


def _ids(result: Any) -> List[int]:
    return [row["id"] for row in result.products + result.catalog_fact_products]


def test_a_phrase_that_misses_on_one_word_finds_the_products_holding_the_rest(store: Store):
    result = store.search("بلوزة بيضاء")
    blouse, bag = store.ids["blouse"], store.ids["white_bag"]
    assert result.method == PARTIAL_WORDS_METHOD
    # One word each: the blouse holds «بلوزة», the bag «بيضاء». Both are what
    # the store has for this phrase; neither is presented as a full match.
    assert _ids(result) == [blouse, bag]
    assert result.query_words_missing == {blouse: ("بيضاء",), bag: ("بلوزة",)}


def test_without_the_request_the_search_is_exactly_what_it_was(store: Store):
    result = store.search("بلوزة بيضاء", partial_words=False)
    assert _ids(result) == [] and result.method == "" and result.query_words_missing == {}
    assert store.candidates("بلوزة بيضاء", partial_words=False).product_ids == ()


def test_the_most_words_held_decides_membership_never_the_order(store: Store):
    result = store.search("فستان أسود مقاس 38")
    # The black dresses hold two words; the navy one only «فستان». The other
    # store's product holding all four counts for nothing here.
    assert _ids(result) == [store.ids["black_dress_1"], store.ids["black_dress_2"]]
    assert set(result.query_words_missing.values()) == {("مقاس", "38")}


def test_the_search_folds_and_article_apply_to_each_word(store: Store):
    result = store.search("البلوزه البيضا")
    blouse, bag = store.ids["blouse"], store.ids["white_bag"]
    assert result.method == PARTIAL_WORDS_METHOD
    assert _ids(result) == [blouse, bag]
    assert result.query_words_missing == {blouse: ("البيضا",), bag: ("البلوزه",)}


@pytest.mark.parametrize("query,method", [("بلوزة", "fts"), ("حذاء رياضي أبيض", "fts"),
                                          ("عطر ورد", "fts")])
def test_a_search_that_already_matched_is_unchanged(store: Store, query: str, method: str):
    with_step, without = store.search(query), store.search(query, partial_words=False)
    assert with_step.method == method
    assert _ids(with_step) == _ids(without) and _ids(with_step)
    assert with_step.query_words_missing == {}


@pytest.mark.parametrize("query", ["عيال محمد", "ساعة", "ساعة يد فضية"])
def test_words_no_product_holds_and_single_words_find_nothing(store: Store, query: str):
    """A single word has nothing to leave out; words no product holds match nothing."""
    result = store.search(query)
    assert _ids(result) == [] and result.method == ""
    candidates = store.candidates(query)
    assert candidates.product_ids == () and candidates.exhausted is True


def test_like_wildcards_in_a_word_match_only_themselves(store: Store):
    # Unescaped, «ب%ة» would match «بلوزة» and «ح_يبة» would match «حقيبة».
    assert _ids(store.search("ب%ة ح_يبة")) == []


def test_the_candidates_are_the_row_search_in_the_same_order(store: Store):
    query = "فستان أسود مقاس 38"
    candidates = store.candidates(query)
    black = [store.ids["black_dress_1"], store.ids["black_dress_2"]]
    assert candidates.method == PARTIAL_WORDS_METHOD
    assert list(candidates.product_ids) == black and candidates.exhausted is True
    assert _ids(store.search(query, limit=1)) == black[:1]
    bounded = store.candidates(query, limit=1)
    assert list(bounded.product_ids) == black[:1] and bounded.exhausted is False


def test_the_words_missing_read_is_this_stores_and_the_steps_own(store: Store):
    db = store.Session()
    try:
        builder = CatalogContextBuilder(db, store.tenant)
        missing = builder.query_words_missing(
            "بلوزة بيضاء", [store.ids["blouse"], store.ids["white_bag"], store.ids["shoe"]])
        other = CatalogContextBuilder(db, store.other).query_words_missing(
            "بلوزة بيضاء", [store.ids["blouse"]])
    finally:
        db.close()
    assert missing == {store.ids["blouse"]: ("بيضاء",), store.ids["white_bag"]: ("بلوزة",),
                       store.ids["shoe"]: ("بلوزة", "بيضاء")}
    assert other == {}


def test_a_partial_match_needs_the_result_that_can_say_so(store: Store):
    db = store.Session()
    try:
        with pytest.raises(ValueError):
            CatalogContextBuilder(db, store.tenant).search_products("بلوزة بيضاء", partial_words=True)
    finally:
        db.close()

"""The catalogue search returns one total order, on every path — on real PostgreSQL.

Before this, the full-text path selected ids ``ORDER BY in_stock DESC, id`` and
then hydrated them with ``id IN (...)``, which has no order of its own: the rows
came back however the heap held them. The ILIKE and Arabic-normalised paths
ordered by ``in_stock`` alone, which every stocked product ties on. Two identical
searches could put a different product first.

These cases shuffle the heap on purpose — an ``UPDATE`` moves a row's live
version to the end of the table — so an unordered read would visibly disagree
with the id order, and then prove every path returns
``in_stock DESC, id`` anyway, run after run.

They also prove the candidate read is the same search: its first *n* ids are
exactly what a search limited to *n* returns, and ``exhausted`` is true only
when the read held every match — never merely because it reached its bound.
"""
from __future__ import annotations

import json
import uuid
from typing import Any, Dict, Iterator, List

import pytest
from sqlalchemy import create_engine, event, text
from sqlalchemy.orm import sessionmaker

from core.store_knowledge import CatalogContextBuilder
from tests.commerce_reliability.test_commerce_runtime_foundation_pg import (
    _alembic,
    _create_database,
    _drop_database,
    _seed_tenant,
)

MATCHES = 12


class Catalogue:
    def __init__(self, engine: Any, tenant: int, other: int) -> None:
        self.engine, self.tenant, self.other = engine, tenant, other
        self.Session = sessionmaker(bind=engine)
        self.ids: Dict[str, List[int]] = {}

    def builder(self, db: Any, tenant: int = 0) -> CatalogContextBuilder:
        return CatalogContextBuilder(db, tenant or self.tenant)


def _add(conn: Any, tenant: int, title: str, *, in_stock: bool = True,
         external: bool = True) -> int:
    return int(conn.execute(text(
        "INSERT INTO products (tenant_id, external_id, title, description, price, in_stock, "
        "stock_quantity, metadata) VALUES (:t, :x, :ti, 'عام', 10, :s, 5, CAST(:m AS JSONB)) "
        "RETURNING id"),
        {"t": tenant, "x": ("SKU-" + uuid.uuid4().hex[:8]) if external else None, "ti": title,
         "s": in_stock, "m": json.dumps({})}).scalar_one())


@pytest.fixture(scope="module")
def catalogue(pg_admin_dsn: str) -> Iterator[Catalogue]:
    name, dsn = _create_database(pg_admin_dsn)
    engine = create_engine(dsn, future=True)
    try:
        _alembic(dsn, "0111")
        shop = Catalogue(engine, _seed_tenant(engine, "A"), _seed_tenant(engine, "B"))
        with engine.begin() as conn:
            for key, title in (("fts", "قميص قطني"), ("ilike", "Tee Shirt"),
                               ("norm", "أحذية رياضية")):
                ids = []
                for n in range(MATCHES):
                    # Every third is out of stock: the order puts them last,
                    # and among equals the id decides.
                    ids.append(_add(conn, shop.tenant, f"{title} {n}", in_stock=n % 3 != 2))
                    _add(conn, shop.other, f"{title} {n}")
                shop.ids[key] = ids
            # Shuffle the heap: rewriting the earliest rows moves their live
            # versions to the end, so physical order is no longer id order.
            for ids in shop.ids.values():
                for pid in ids[:6]:
                    conn.execute(text("UPDATE products SET description = description || '.' WHERE id = :p"),
                                 {"p": pid})
        yield shop
    finally:
        engine.dispose()
        _drop_database(pg_admin_dsn, name)


def _expected(ids: List[int]) -> List[int]:
    stocked = [pid for n, pid in enumerate(ids) if n % 3 != 2]
    return sorted(stocked) + sorted(pid for pid in ids if pid not in stocked)


QUERIES = {"fts": "قميص", "ilike": "ee sh", "norm": "احذية"}
METHODS = {"fts": "fts", "ilike": "ilike", "norm": "ilike_arabic_norm"}


def test_the_heap_really_is_shuffled(catalogue: Catalogue):
    """Guards the guard: if physical order were id order, nothing below proves much."""
    with catalogue.engine.connect() as conn:
        physical = [row[0] for row in conn.execute(text(
            "SELECT id FROM products WHERE tenant_id = :t AND id = ANY(:ids)"),
            {"t": catalogue.tenant, "ids": catalogue.ids["fts"]})]
    assert physical != sorted(physical)


@pytest.mark.parametrize("path", sorted(QUERIES))
def test_every_search_path_returns_one_total_order(catalogue: Catalogue, path: str):
    db = catalogue.Session()
    try:
        runs = []
        for _ in range(5):
            result = catalogue.builder(db).search_products(QUERIES[path], limit=MATCHES,
                                                           include_non_orderable_facts=True)
            # The row search reports orderable and non-orderable rows apart;
            # the order under test is the one both came out of.
            runs.append([row["id"] for row in result.products + result.catalog_fact_products])
        candidates = catalogue.builder(db).search_product_candidates(QUERIES[path], 50)
    finally:
        db.close()
    expected = _expected(catalogue.ids[path])
    assert candidates.method == METHODS[path]
    assert list(candidates.product_ids) == expected
    # The stocked products are the orderable ones and the rest are fact rows,
    # so orderable-then-facts is exactly the total order — every run, as a
    # sequence, not merely the same set.
    assert all(run == expected for run in runs), runs


@pytest.mark.parametrize("path", sorted(QUERIES))
def test_the_candidates_head_is_what_a_shorter_search_returns(catalogue: Catalogue, path: str):
    db = catalogue.Session()
    try:
        head = catalogue.builder(db).search_product_candidates(QUERIES[path], 50).product_ids
        for limit in range(1, MATCHES + 1):
            result = catalogue.builder(db).search_products(QUERIES[path], limit=limit,
                                                           include_non_orderable_facts=True)
            window = {row["id"] for row in result.products + result.catalog_fact_products}
            assert window == set(head[:limit]), f"limit {limit}"
    finally:
        db.close()


@pytest.mark.parametrize("limit,exhausted", [(MATCHES - 1, False), (MATCHES, True),
                                             (MATCHES + 1, True), (5, False), (1, False)])
def test_exhausted_is_proven_never_inferred_from_the_bound(catalogue: Catalogue, limit, exhausted):
    db = catalogue.Session()
    try:
        read = catalogue.builder(db).search_product_candidates(QUERIES["fts"], limit)
    finally:
        db.close()
    assert len(read.product_ids) == min(limit, MATCHES)
    assert read.exhausted is exhausted


def test_a_search_that_matches_nothing_is_exhaustively_empty(catalogue: Catalogue):
    db = catalogue.Session()
    try:
        read = catalogue.builder(db).search_product_candidates("zzqqxx", 50)
    finally:
        db.close()
    assert read.product_ids == () and read.exhausted is True


def test_candidates_never_cross_tenants(catalogue: Catalogue):
    db = catalogue.Session()
    try:
        ours = catalogue.builder(db).search_product_candidates(QUERIES["fts"], 50).product_ids
        theirs = catalogue.builder(db, catalogue.other).search_product_candidates(
            QUERIES["fts"], 50).product_ids
    finally:
        db.close()
    assert set(ours) == set(catalogue.ids["fts"]) and not set(ours) & set(theirs)


def test_the_general_browse_candidates_follow_its_own_order(catalogue: Catalogue):
    db = catalogue.Session()
    try:
        top = catalogue.builder(db).get_top_products(limit=5)
        candidates = catalogue.builder(db).top_product_candidates(50)
        small = catalogue.builder(db).top_product_candidates(3)
    finally:
        db.close()
    assert [row["id"] for row in top] == list(candidates.product_ids[:5])
    assert small.exhausted is False and len(small.product_ids) == 3
    # 24 orderable of 36 fit a window of 50+21, so the read holds them all.
    assert candidates.exhausted is True and len(candidates.product_ids) == 24


def test_a_listing_read_is_two_statements_and_this_tenants_only(catalogue: Catalogue):
    statements: List[str] = []

    def listen(_conn, _cursor, statement, *_rest):
        statements.append(statement)

    ids = catalogue.ids["fts"][:9]
    db = catalogue.Session()
    event.listen(catalogue.engine, "before_cursor_execute", listen)
    try:
        other_tenants = catalogue.builder(db, catalogue.other).search_product_candidates(
            QUERIES["fts"], 1).product_ids
        statements.clear()
        rows = catalogue.builder(db).get_by_ids(ids + list(other_tenants))
    finally:
        event.remove(catalogue.engine, "before_cursor_execute", listen)
        db.close()
    assert sorted(rows) == sorted(ids), "another tenant's id is simply absent"
    assert len(statements) == 2, statements

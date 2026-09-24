"""A page token is spendable once, here, and only while it lasts — on real PostgreSQL.

The navigation snapshot exists so a browse too long for one list can be paged
without asking the catalogue twice. Everything that could go wrong with a token
the customer holds is exercised here against a disposable database migrated with
the repository's own chain to revision ``0113``: a forged one, a replayed one,
two taps racing on the same one, an expired one, one minted in another
conversation, one minted for another tenant, and one whose reply was never
reserved.

Tokens are spent and minted exactly as the runtime does it: by
``navigation.apply`` on the connection of an open transaction, the way the
reply reservation calls it. A spend whose transaction rolls back leaves the
token spendable.

Cleanup is proven through its real owner: the scheduler loop the application
registers at startup, running against this database.

Nothing is sent, no model is called, and no catalogue is touched: the store and
its contract are what is under test. Merchant-agnostic throughout — the products
are opaque integers, because to this layer that is all they ever are.
"""
from __future__ import annotations

import asyncio
import threading
import uuid
from datetime import datetime, timedelta
from typing import Any, List, Optional, Tuple

import pytest
from sqlalchemy import create_engine, inspect, text
from sqlalchemy.exc import IntegrityError

from core.commerce_runtime import navigation as nav
from core.commerce_runtime import navigation_models as nm
from core.commerce_runtime import reply_choices as rc
from tests.commerce_reliability.test_commerce_runtime_foundation_pg import (
    _alembic,
    _create_database,
    _drop_database,
    _seed_tenant,
)

REVISION = "0113"
LIVE = "live"


# ── Harness ──────────────────────────────────────────────────────────────────


class Nav:
    def __init__(self, dsn: str, engine: Any, tenant_a: int, tenant_b: int) -> None:
        self.dsn, self.engine = dsn, engine
        self.tenant_a, self.tenant_b = tenant_a, tenant_b

    def conversation(self, tenant_id: int) -> int:
        """One runtime conversation row, which the snapshot's scope points at."""
        with self.engine.begin() as conn:
            return int(conn.execute(text(
                "INSERT INTO commerce_runtime_conversations "
                "(tenant_id, namespace, conversation_ref) "
                "VALUES (:t, :n, :r) RETURNING id"),
                {"t": tenant_id, "n": LIVE,
                 "r": "conv:" + uuid.uuid4().hex[:12]}).scalar_one())

    def rows(self) -> int:
        with self.engine.connect() as conn:
            return int(conn.execute(text(
                f"SELECT count(*) FROM {nm.NAVIGATION_TABLE}")).scalar())

    def now(self) -> datetime:
        with self.engine.connect() as conn:
            return conn.execute(text("SELECT now()")).scalar_one()

    def apply(self, tenant: int, conversation: int, plan: nav.Plan, *, turn_id: int = 1) -> None:
        """Spend and mint the way the reservation does: inside one transaction."""
        with self.engine.begin() as conn:
            moment = conn.execute(text("SELECT now()")).scalar_one()
            nav.apply(conn, plan, tenant_id=tenant, namespace=LIVE, conversation_id=conversation,
                      turn_id=turn_id, db_now=moment)

    def peek(self, tenant: int, conversation: int, token: str) -> nav.Continuation:
        return nav.peek(self.engine, token=token, tenant_id=tenant, namespace=LIVE,
                        conversation_id=conversation)

    def open(self, tenant: int, conversation: int, ids: List[int], *,
             complete: bool = True) -> str:
        """Store a browse whose page one is being sent, and return page two's token."""
        token = nav.new_token()
        self.apply(tenant, conversation, nav.Plan(mint=_mint(token, ids, offset=nm.PAGE_SIZE,
                                                              complete=complete)))
        return token


def _mint(token: str, ids: List[int], *, offset: int, series: Optional[str] = None,
          complete: bool = True) -> nav.Mint:
    return nav.Mint(token=token, series=series or nav.new_token(), product_ids=tuple(ids),
                    page_offset=offset, complete=complete, more_label="More",
                    button_label="Options", origin_turn_id=1, origin_call_id="call_search_1",
                    search_method="fts", query_digest="d" * 32)


@pytest.fixture(scope="module")
def navdb(pg_admin_dsn: str):
    name, dsn = _create_database(pg_admin_dsn)
    engine = None
    try:
        _alembic(dsn, REVISION)
        engine = create_engine(dsn, pool_pre_ping=True)
        nav.reset_schema_probe()
        yield Nav(dsn, engine, _seed_tenant(engine, "A"), _seed_tenant(engine, "B"))
    finally:
        nav.reset_schema_probe()
        if engine is not None:
            engine.dispose()
        _drop_database(pg_admin_dsn, name)


def _walk(navdb: Nav, tenant: int, conversation: int, token: str) -> Tuple[List[int], int]:
    """Spend page after page until no token follows. Returns what was shown, and pages."""
    shown: List[int] = []
    pages = 0
    while token:
        page = navdb.peek(tenant, conversation, token)
        assert page.resolved, page.status
        bounds = page.bounds()
        shown.extend(page.page_ids())
        pages += 1
        nxt = nav.new_token() if bounds.has_next else ""
        navdb.apply(tenant, conversation, nav.Plan(
            spend=token, mint=_mint(nxt, list(page.product_ids), offset=bounds.end,
                                    series=page.series) if nxt else None), turn_id=100 + pages)
        token = nxt
    return shown, pages


# ── The relation the revision creates ────────────────────────────────────────


def test_the_revision_creates_the_relation_the_model_declares(navdb: Nav):
    insp = inspect(navdb.engine)
    assert nm.NAVIGATION_TABLE in insp.get_table_names()
    declared = {c.name for c in nm.NavigationSnapshot.__table__.columns}
    assert {c["name"] for c in insp.get_columns(nm.NAVIGATION_TABLE)} == declared
    uniques = {u["name"] for u in insp.get_unique_constraints(nm.NAVIGATION_TABLE)}
    assert {"uq_commerce_runtime_navigation_token",
            "uq_commerce_runtime_navigation_page"} <= uniques
    checks = {c["name"] for c in insp.get_check_constraints(nm.NAVIGATION_TABLE)}
    assert {"ck_commerce_runtime_navigation_products", "ck_commerce_runtime_navigation_offset",
            "ck_commerce_runtime_navigation_consumed", "ck_commerce_runtime_navigation_words",
            "ck_commerce_runtime_navigation_namespace",
            "ck_commerce_runtime_navigation_page_size"} <= checks
    # Beyond the indexes the two unique constraints carry, only the expiry
    # index the sweep reads: nothing else reads this relation.
    assert {i["name"] for i in insp.get_indexes(nm.NAVIGATION_TABLE)} - uniques == {
        "ix_commerce_runtime_navigation_expiry"}
    assert nav.schema_available(navdb.engine)


@pytest.mark.parametrize("change,why", [
    ({"page_offset": 0}, "page one never needs a token"),
    ({"page_offset": 3}, "a token must open a page that exists"),
    ({"product_ids": list(range(1, 52))}, "at most MAX_STORED_PRODUCTS"),
    ({"product_ids": []}, "a stored result is never empty"),
    ({"more_label": ""}, "the model's word is required"),
    ({"consumed_at": "now()"}, "spent means both when and by whom"),
    ({"page_size": 10}, "a page with a successor carries nine"),
])
def test_the_database_refuses_a_token_row_that_would_lead_nowhere(navdb: Nav, change, why):
    conversation = navdb.conversation(navdb.tenant_a)
    row = {"token": nav.new_token(), "series": nav.new_token(), "tenant_id": navdb.tenant_a,
           "namespace": LIVE, "conversation_id": conversation, "minted_by_turn_id": 1,
           "origin_turn_id": 1, "origin_call_id": "c", "search_method": "fts",
           "query_digest": "d", "product_ids": [1, 2, 3], "complete": True, "page_offset": 2,
           "page_size": 9, "more_label": "More", "button_label": "Options",
           "consumed_at": None}
    row.update(change)
    columns = ", ".join(key for key in row if key != "consumed_at")
    values = ", ".join(f"CAST(:{key} AS jsonb)" if key == "product_ids" else f":{key}"
                       for key in row if key != "consumed_at")
    consumed = ", consumed_at" if row["consumed_at"] else ""
    consumed_value = ", now()" if row["consumed_at"] else ""
    import json
    params = {key: (json.dumps(value) if key == "product_ids" else value)
              for key, value in row.items() if key != "consumed_at"}
    with pytest.raises(IntegrityError), navdb.engine.begin() as conn:
        conn.execute(text(
            f"INSERT INTO {nm.NAVIGATION_TABLE} ({columns}, expires_at{consumed}) "
            f"VALUES ({values}, now() + interval '1 hour'{consumed_value})"), params)


def test_the_scope_is_the_databases_and_not_the_applications(navdb: Nav):
    """A token cannot name a conversation that does not exist, whatever the caller says."""
    with pytest.raises(nav.NavigationNotPersisted) as refused:
        navdb.apply(navdb.tenant_a, 2_000_000_000,
                    nav.Plan(mint=_mint(nav.new_token(), list(range(1, 31)), offset=9)))
    assert refused.value.reason == nav.NOT_STORED


# ── Walking a browse ─────────────────────────────────────────────────────────


@pytest.mark.parametrize("total,pages", [(11, 2), (18, 2), (19, 2), (20, 3), (28, 3),
                                         (29, 4), (50, 6)])
def test_the_pages_walk_the_whole_browse_in_the_order_it_was_shown(navdb: Nav, total, pages):
    """Every stored product exactly once, in order, whatever the size.

    Page one's nine are shown by the reply that opened the browse; the tokens
    then walk the rest, nine at a time, with a final page of up to ten.
    """
    conversation = navdb.conversation(navdb.tenant_a)
    ids = list(range(5000 + total * 100, 5000 + total * 100 + total))
    token = navdb.open(navdb.tenant_a, conversation, ids)
    shown, walked = _walk(navdb, navdb.tenant_a, conversation, token)
    assert ids[:nm.PAGE_SIZE] + shown == ids, "a product was skipped, repeated or reordered"
    assert 1 + walked == pages


def test_the_stored_order_is_what_page_two_shows_even_if_the_catalogue_moves(navdb: Nav):
    """Page two reads the stored result. There is no second search to disagree with it."""
    conversation = navdb.conversation(navdb.tenant_a)
    ids = list(range(301, 331))
    token = navdb.open(navdb.tenant_a, conversation, ids)
    with navdb.engine.begin() as conn:
        conn.execute(text("UPDATE tenants SET name = name || ' (moved)' WHERE id = :t"),
                     {"t": navdb.tenant_a})
    assert navdb.peek(navdb.tenant_a, conversation, token).page_ids() == tuple(ids[9:18])


# ── Refusals ─────────────────────────────────────────────────────────────────


@pytest.mark.parametrize("forged", ["", "x", nm.NAVIGATION_ROW_PREFIX, "' OR 1=1 --",
                                    "a" * 64])
def test_a_forged_token_is_not_found(navdb: Nav, forged):
    conversation = navdb.conversation(navdb.tenant_a)
    navdb.open(navdb.tenant_a, conversation, list(range(401, 431)))
    assert navdb.peek(navdb.tenant_a, conversation, forged).status == nav.NOT_FOUND
    with pytest.raises(nav.NavigationNotPersisted):
        navdb.apply(navdb.tenant_a, conversation, nav.Plan(spend=forged or "x"))


def test_a_token_is_spendable_once(navdb: Nav):
    conversation = navdb.conversation(navdb.tenant_a)
    token = navdb.open(navdb.tenant_a, conversation, list(range(501, 531)))
    navdb.apply(navdb.tenant_a, conversation, nav.Plan(spend=token), turn_id=7)
    assert navdb.peek(navdb.tenant_a, conversation, token).status == nav.REPLAYED
    with pytest.raises(nav.NavigationNotPersisted) as refused:
        navdb.apply(navdb.tenant_a, conversation, nav.Plan(spend=token), turn_id=8)
    assert refused.value.reason == nav.CLAIM_LOST
    with navdb.engine.connect() as conn:
        spent_by = conn.execute(text(f"SELECT consumed_by_turn_id FROM {nm.NAVIGATION_TABLE} "
                                     "WHERE token = :t"), {"t": token}).scalar_one()
    assert spent_by == 7, "the spend must name the turn whose reply showed the page"


def test_a_spend_whose_reply_was_not_reserved_leaves_the_token_spendable(navdb: Nav):
    """The spend is part of the reservation. A reservation that fails spent nothing."""
    conversation = navdb.conversation(navdb.tenant_a)
    token = navdb.open(navdb.tenant_a, conversation, list(range(551, 581)))
    with pytest.raises(RuntimeError), navdb.engine.begin() as conn:
        moment = conn.execute(text("SELECT now()")).scalar_one()
        nav.apply(conn, nav.Plan(spend=token, mint=_mint(nav.new_token(), list(range(551, 581)),
                                                         offset=18)),
                  tenant_id=navdb.tenant_a, namespace=LIVE, conversation_id=conversation,
                  turn_id=9, db_now=moment)
        raise RuntimeError("the reply reservation failed after the navigation writes")
    assert navdb.peek(navdb.tenant_a, conversation, token).resolved
    before = navdb.rows()
    navdb.apply(navdb.tenant_a, conversation, nav.Plan(spend=token), turn_id=10)
    assert navdb.rows() == before, "the rolled-back mint must not have survived"


def test_two_taps_racing_on_one_token_produce_exactly_one_page(navdb: Nav):
    """Two independent connections, both past the read, racing to spend."""
    conversation = navdb.conversation(navdb.tenant_a)
    token = navdb.open(navdb.tenant_a, conversation, list(range(601, 631)))
    barrier = threading.Barrier(2)
    outcomes: List[str] = []
    lock = threading.Lock()

    def spend(turn_id: int) -> None:
        with navdb.engine.connect() as conn:
            transaction = conn.begin()
            moment = conn.execute(text("SELECT now()")).scalar_one()
            barrier.wait(timeout=10)
            try:
                nav.apply(conn, nav.Plan(spend=token), tenant_id=navdb.tenant_a, namespace=LIVE,
                          conversation_id=conversation, turn_id=turn_id, db_now=moment)
                transaction.commit()
                result = "won"
            except nav.NavigationNotPersisted as refused:
                transaction.rollback()
                result = refused.reason
        with lock:
            outcomes.append(result)

    threads = [threading.Thread(target=spend, args=(turn,)) for turn in (21, 22)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=20)
    assert sorted(outcomes) == sorted(["won", nav.CLAIM_LOST])


def test_an_expired_token_is_refused_and_says_so(navdb: Nav):
    conversation = navdb.conversation(navdb.tenant_a)
    token = navdb.open(navdb.tenant_a, conversation, list(range(701, 731)))
    with navdb.engine.begin() as conn:
        conn.execute(text(f"UPDATE {nm.NAVIGATION_TABLE} SET expires_at = now() - interval "
                          "'1 second' WHERE token = :t"), {"t": token})
    assert navdb.peek(navdb.tenant_a, conversation, token).status == nav.EXPIRED
    with pytest.raises(nav.NavigationNotPersisted):
        navdb.apply(navdb.tenant_a, conversation, nav.Plan(spend=token))


def test_a_token_minted_for_another_tenant_is_not_found_here(navdb: Nav):
    theirs = navdb.conversation(navdb.tenant_b)
    ours = navdb.conversation(navdb.tenant_a)
    token = navdb.open(navdb.tenant_b, theirs, list(range(801, 831)))
    assert navdb.peek(navdb.tenant_a, ours, token).status == nav.NOT_FOUND
    assert navdb.peek(navdb.tenant_a, theirs, token).status == nav.NOT_FOUND
    with pytest.raises(nav.NavigationNotPersisted):
        navdb.apply(navdb.tenant_a, ours, nav.Plan(spend=token))
    # ... and it is still spendable where it belongs.
    assert navdb.peek(navdb.tenant_b, theirs, token).resolved
    navdb.apply(navdb.tenant_b, theirs, nav.Plan(spend=token))


def test_a_token_minted_in_another_conversation_is_not_found_here(navdb: Nav):
    first = navdb.conversation(navdb.tenant_a)
    second = navdb.conversation(navdb.tenant_a)
    token = navdb.open(navdb.tenant_a, second, list(range(851, 881)))
    assert navdb.peek(navdb.tenant_a, first, token).status == nav.NOT_FOUND
    with pytest.raises(nav.NavigationNotPersisted):
        navdb.apply(navdb.tenant_a, first, nav.Plan(spend=token))
    assert navdb.peek(navdb.tenant_a, second, token).resolved


def test_the_two_row_namespaces_cannot_be_read_as_each_other(navdb: Nav):
    conversation = navdb.conversation(navdb.tenant_a)
    token = navdb.open(navdb.tenant_a, conversation, list(range(901, 931)))
    more = nav.row_id(token)
    assert more.startswith(nm.NAVIGATION_ROW_PREFIX)
    assert rc.product_id_from_row_id(more) is None, "a page token read as a product"
    assert nav.token_from_row_id(rc.row_id(901)) is None, "a product read as a page token"
    assert not any(str(pid) in more for pid in range(901, 931))


# ── Retention is bounded, and something actually runs it ─────────────────────


def _age(navdb: Nav, token: str, *, expired_days_ago: float) -> None:
    with navdb.engine.begin() as conn:
        conn.execute(text(f"UPDATE {nm.NAVIGATION_TABLE} SET expires_at = now() - "
                          "make_interval(secs => :s) WHERE token = :t"),
                     {"s": expired_days_ago * 86400, "t": token})


def test_cleanup_removes_old_tokens_and_leaves_live_ones(navdb: Nav):
    conversation = navdb.conversation(navdb.tenant_a)
    live = navdb.open(navdb.tenant_a, conversation, list(range(1001, 1031)))
    recent = navdb.open(navdb.tenant_a, conversation, list(range(1101, 1131)))
    stale = navdb.open(navdb.tenant_a, conversation, list(range(1201, 1231)))
    _age(navdb, recent, expired_days_ago=1)                    # expired, still retained
    _age(navdb, stale, expired_days_ago=7)                     # past retention
    removed = nav.sweep_until_clean(navdb.engine)
    assert removed >= 1
    assert navdb.peek(navdb.tenant_a, conversation, stale).status == nav.NOT_FOUND
    assert navdb.peek(navdb.tenant_a, conversation, recent).status == nav.EXPIRED
    assert navdb.peek(navdb.tenant_a, conversation, live).resolved


def test_one_sweep_pass_is_bounded(navdb: Nav):
    conversation = navdb.conversation(navdb.tenant_a)
    tokens = [navdb.open(navdb.tenant_a, conversation, list(range(2001 + i * 40, 2031 + i * 40)))
              for i in range(5)]
    for token in tokens:
        _age(navdb, token, expired_days_ago=8)
    assert nav.sweep(navdb.engine, batch=2) == 2
    assert nav.sweep_until_clean(navdb.engine, batch=2) == 3


def test_a_sweep_with_nothing_old_enough_removes_nothing(navdb: Nav):
    nav.sweep_until_clean(navdb.engine)
    assert nav.sweep(navdb.engine) == 0


def test_the_scheduler_the_application_registers_runs_the_sweep(navdb: Nav):
    """Cleanup has an owner that runs: the startup-registered loop, one tick.

    The loop is the one ``main.py`` queues at startup; here it runs a single
    tick against this database with its waits removed.
    """
    conversation = navdb.conversation(navdb.tenant_a)
    token = navdb.open(navdb.tenant_a, conversation, list(range(3001, 3031)))
    _age(navdb, token, expired_days_ago=9)
    waited: List[float] = []

    async def no_wait(seconds: float) -> None:
        waited.append(seconds)

    before = nav.scheduler_state()["ticks"]
    asyncio.run(nav.run_navigation_sweep_scheduler(engine=navdb.engine, max_ticks=1,
                                                   sleep=no_wait))
    assert nav.scheduler_state()["ticks"] == before + 1
    assert waited == [nav.SWEEP_FIRST_DELAY_SECONDS]
    assert navdb.peek(navdb.tenant_a, conversation, token).status == nav.NOT_FOUND


# ── The revision applies and steps back on its own ───────────────────────────


def test_the_revision_applies_at_0112_and_rolls_back_alone(pg_admin_dsn: str):
    """``0113`` is a plain child of ``0112`` and owns exactly one relation.

    Stepping back removes that relation and leaves everything the chain brought
    before it — a rollback costs unfinished browses and nothing else.
    """
    name, dsn = _create_database(pg_admin_dsn)
    engine = None
    try:
        _alembic(dsn, "0112")
        engine = create_engine(dsn)
        before = set(inspect(engine).get_table_names())
        assert nm.NAVIGATION_TABLE not in before
        nav.reset_schema_probe()
        assert not nav.schema_available(engine)
        assert nav.sweep(engine) == 0, "a database without the relation has nothing to sweep"

        _alembic(dsn, "0113")
        after = set(inspect(engine).get_table_names())
        assert after - before == {nm.NAVIGATION_TABLE}, "0113 touched more than its own relation"
        nav.reset_schema_probe()
        assert nav.schema_available(engine)

        _alembic(dsn, "0113@-1", downgrade=True)
        assert set(inspect(engine).get_table_names()) == before
        with engine.connect() as conn:
            assert {r[0] for r in conn.execute(text("SELECT version_num FROM alembic_version"))} \
                == {"0112"}
        # And forward again: the step is repeatable, not one-way.
        _alembic(dsn, "0113")
        assert nm.NAVIGATION_TABLE in set(inspect(engine).get_table_names())
    finally:
        nav.reset_schema_probe()
        if engine is not None:
            engine.dispose()
        _drop_database(pg_admin_dsn, name)


def test_the_revision_refuses_a_relation_that_only_looks_right(pg_admin_dsn: str):
    """The check is by definition, not by name.

    A table with the right name and every column, but without the unique
    constraint that makes a token single-use, would let the same page be spent
    twice. The revision stops rather than adopting it.
    """
    name, dsn = _create_database(pg_admin_dsn)
    engine = None
    try:
        _alembic(dsn, "0112")
        engine = create_engine(dsn)
        with engine.begin() as conn:
            conn.execute(text(f"""
                CREATE TABLE {nm.NAVIGATION_TABLE} (
                    id BIGSERIAL PRIMARY KEY, token VARCHAR(64) NOT NULL,
                    series VARCHAR(64) NOT NULL, tenant_id INTEGER NOT NULL,
                    namespace VARCHAR(16) NOT NULL, conversation_id BIGINT NOT NULL,
                    minted_by_turn_id BIGINT NOT NULL, origin_turn_id BIGINT NOT NULL,
                    origin_call_id VARCHAR(128) NOT NULL, search_method VARCHAR(64) NOT NULL,
                    query_digest VARCHAR(64) NOT NULL, product_ids JSONB NOT NULL,
                    complete BOOLEAN NOT NULL, page_offset INTEGER NOT NULL,
                    page_size INTEGER NOT NULL, more_label VARCHAR(24) NOT NULL,
                    button_label VARCHAR(20) NOT NULL, expires_at TIMESTAMPTZ NOT NULL,
                    consumed_at TIMESTAMPTZ, consumed_by_turn_id BIGINT,
                    created_at TIMESTAMPTZ NOT NULL DEFAULT now())
            """))
        with pytest.raises(Exception) as refused:
            _alembic(dsn, "0113")
        assert "incompatible" in str(refused.value).lower()
    finally:
        if engine is not None:
            engine.dispose()
        _drop_database(pg_admin_dsn, name)


def test_the_revision_refuses_the_earlier_draft_of_this_relation(pg_admin_dsn: str):
    """A database that ran an earlier draft of ``0113`` is not quietly adopted.

    The first draft of this revision (PR #1143, never merged) had no
    provenance, no completeness flag, no stored words and no spender. A
    database where it was ever applied must stop here, not be reconciled.
    """
    name, dsn = _create_database(pg_admin_dsn)
    engine = None
    try:
        _alembic(dsn, "0112")
        engine = create_engine(dsn)
        with engine.begin() as conn:
            conn.execute(text(f"""
                CREATE TABLE {nm.NAVIGATION_TABLE} (
                    id BIGSERIAL PRIMARY KEY, token VARCHAR(64) NOT NULL UNIQUE,
                    series VARCHAR(64) NOT NULL, tenant_id INTEGER NOT NULL,
                    namespace VARCHAR(16) NOT NULL, conversation_id BIGINT NOT NULL,
                    minted_by_turn_id BIGINT NOT NULL, product_ids JSONB NOT NULL,
                    page_offset INTEGER NOT NULL DEFAULT 0, page_size INTEGER NOT NULL,
                    expires_at TIMESTAMPTZ NOT NULL, consumed_at TIMESTAMPTZ,
                    created_at TIMESTAMPTZ NOT NULL DEFAULT now())
            """))
        with pytest.raises(Exception) as refused:
            _alembic(dsn, "0113")
        assert "incompatible" in str(refused.value).lower()
        nav.reset_schema_probe()
        assert not nav.schema_available(engine), "the old shape must not switch paging on"
    finally:
        nav.reset_schema_probe()
        if engine is not None:
            engine.dispose()
        _drop_database(pg_admin_dsn, name)

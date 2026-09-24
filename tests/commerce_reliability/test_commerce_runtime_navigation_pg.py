"""A page token is spendable once, here, and only while it lasts — on real PostgreSQL.

The navigation snapshot exists so a browse too long for one list can be paged
without asking the catalogue twice. Everything that could go wrong with a token
the customer holds is exercised here against a disposable database migrated with
the repository's own chain to revision ``0113``: a forged one, a replayed one,
two taps racing on the same one, an expired one, one minted in another
conversation, and one minted for another tenant.

The order a customer pages through is the order they were shown: a case changes
the merchant's catalogue between pages and shows page two is unmoved, because
page two reads the stored order and never searches again.

Nothing is sent, no model is called, and no catalogue is touched: the store and
its contract are what is under test. Merchant-agnostic throughout — the products
are opaque integers, because to this layer that is all they ever are.
"""
from __future__ import annotations

import uuid
from datetime import datetime, timedelta, timezone
from typing import Any, List, Tuple

import pytest
from sqlalchemy import create_engine, inspect, text
from sqlalchemy.orm import sessionmaker

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
        self.Session = sessionmaker(bind=engine)

    def conversation(self, tenant_id: int) -> int:
        """One runtime conversation row, which the snapshot's scope points at."""
        with self.engine.begin() as conn:
            return int(conn.execute(text(
                "INSERT INTO commerce_runtime_conversations "
                "(tenant_id, namespace, conversation_ref) "
                "VALUES (:t, :n, :r) RETURNING id"),
                {"t": tenant_id, "n": LIVE,
                 "r": "conv:" + uuid.uuid4().hex[:12]}).scalar_one())

    def session(self):
        return self.Session()

    def rows(self) -> int:
        with self.engine.connect() as conn:
            return int(conn.execute(text(
                f"SELECT count(*) FROM {nm.NAVIGATION_TABLE}")).scalar())


@pytest.fixture(scope="module")
def navdb(pg_admin_dsn: str):
    name, dsn = _create_database(pg_admin_dsn)
    engine = None
    try:
        _alembic(dsn, REVISION)
        engine = create_engine(dsn, pool_pre_ping=True)
        yield Nav(dsn, engine, _seed_tenant(engine, "A"), _seed_tenant(engine, "B"))
    finally:
        if engine is not None:
            engine.dispose()
        _drop_database(pg_admin_dsn, name)


def _open(navdb: Nav, tenant: int, conversation: int, ids: List[int],
          **kw: Any) -> Tuple[nav.Page, Any]:
    db = navdb.session()
    page = nav.open_browse(db, tenant_id=tenant, namespace=LIVE, conversation_id=conversation,
                           turn_id=1, product_ids=ids, **kw)
    db.commit()
    return page, db


# ── The relation the revision creates ────────────────────────────────────────


def test_the_revision_creates_the_relation_the_model_declares(navdb: Nav):
    insp = inspect(navdb.engine)
    assert nm.NAVIGATION_TABLE in insp.get_table_names()
    declared = {c.name for c in nm.NavigationSnapshot.__table__.columns}
    assert {c["name"] for c in insp.get_columns(nm.NAVIGATION_TABLE)} == declared
    # The guarantees, by name — their definitions are compared by the revision
    # itself, which refuses an incompatible pre-existing relation.
    uniques = {u["name"] for u in insp.get_unique_constraints(nm.NAVIGATION_TABLE)}
    assert {"uq_commerce_runtime_navigation_token",
            "uq_commerce_runtime_navigation_page"} <= uniques
    assert {i["name"] for i in insp.get_indexes(nm.NAVIGATION_TABLE)} >= {
        "ix_commerce_runtime_navigation_open", "ix_commerce_runtime_navigation_expiry"}


def test_the_scope_is_the_databases_and_not_the_applications(navdb: Nav):
    """A token cannot name a conversation that does not exist, whatever the caller says."""
    db = navdb.session()
    page = nav.open_browse(db, tenant_id=navdb.tenant_a, namespace=LIVE,
                           conversation_id=2_000_000_000, turn_id=1,
                           product_ids=list(range(1, 31)))
    # The foreign key refuses it; the browse still answers with its first page.
    assert page.reason == nav.UNAVAILABLE and not page.has_next
    assert len(page.product_ids) == nm.PAGE_SIZE
    db.close()


# ── Opening a browse ─────────────────────────────────────────────────────────


def test_a_browse_that_fits_stores_nothing_at_all(navdb: Nav):
    conversation = navdb.conversation(navdb.tenant_a)
    before = navdb.rows()
    page, db = _open(navdb, navdb.tenant_a, conversation, [1, 2, 3])
    assert (page.reason, page.has_next) == (nav.NO_NEXT_PAGE, False)
    assert page.product_ids == (1, 2, 3)
    assert navdb.rows() == before, "a browse with no continuation left a row behind"
    db.close()


def test_a_browse_that_does_not_fit_shows_nine_and_stores_the_rest(navdb: Nav):
    conversation = navdb.conversation(navdb.tenant_a)
    ids = list(range(101, 131))                       # thirty products
    page, db = _open(navdb, navdb.tenant_a, conversation, ids)
    assert page.reason == nav.OPENED
    assert page.product_ids == tuple(ids[:9]) and len(page.product_ids) == nm.PAGE_SIZE
    assert page.has_next and page.total == 30
    db.close()


def test_the_pages_walk_the_whole_browse_in_the_order_it_was_shown(navdb: Nav):
    conversation = navdb.conversation(navdb.tenant_a)
    ids = list(range(201, 226))                       # twenty-five products
    page, db = _open(navdb, navdb.tenant_a, conversation, ids)
    seen = list(page.product_ids)
    token = page.next_token
    while token:
        nxt = nav.continue_browse(db, token=token, tenant_id=navdb.tenant_a, namespace=LIVE,
                                  conversation_id=conversation)
        db.commit()
        assert nxt.reason == nav.RESOLVED
        seen.extend(nxt.product_ids)
        token = nxt.next_token
    assert seen == ids, "paging did not reproduce the browse, in order, exactly once"
    db.close()


def test_the_stored_order_is_what_page_two_shows_even_if_the_catalogue_moves(navdb: Nav):
    """The rows a customer pages through are the rows they were shown.

    A second search could return a different set — something sold out, a new
    arrival — and "the next nine" would then quietly mean something else. Page
    two reads the stored order, so nothing outside it can change the answer.
    """
    conversation = navdb.conversation(navdb.tenant_a)
    ids = list(range(301, 321))
    page, db = _open(navdb, navdb.tenant_a, conversation, ids)
    # Whatever else happens to the catalogue between the two turns is beside the
    # point: the snapshot is the only thing page two reads.
    later = nav.continue_browse(db, token=page.next_token, tenant_id=navdb.tenant_a,
                                namespace=LIVE, conversation_id=conversation)
    db.commit()
    assert later.product_ids == tuple(ids[9:18])
    db.close()


# ── Every way a token can be wrong ───────────────────────────────────────────


def test_a_forged_token_is_not_found(navdb: Nav):
    conversation = navdb.conversation(navdb.tenant_a)
    db = navdb.session()
    for forged in (nav.new_token(), "", "   ", "nahla:more:", "0", "' OR 1=1 --"):
        out = nav.continue_browse(db, token=forged, tenant_id=navdb.tenant_a, namespace=LIVE,
                                  conversation_id=conversation)
        assert (out.reason, out.product_ids) == (nav.NOT_FOUND, ()), forged
    db.close()


def test_a_token_is_spendable_once(navdb: Nav):
    conversation = navdb.conversation(navdb.tenant_a)
    ids = list(range(401, 421))
    page, db = _open(navdb, navdb.tenant_a, conversation, ids)
    first = nav.continue_browse(db, token=page.next_token, tenant_id=navdb.tenant_a,
                                namespace=LIVE, conversation_id=conversation)
    db.commit()
    assert first.reason == nav.RESOLVED and first.product_ids == tuple(ids[9:18])

    again = nav.continue_browse(db, token=page.next_token, tenant_id=navdb.tenant_a,
                                namespace=LIVE, conversation_id=conversation)
    db.commit()
    assert (again.reason, again.product_ids, again.next_token) == (nav.REPLAYED, (), "")
    db.close()


def test_two_taps_racing_on_one_token_produce_exactly_one_page(navdb: Nav):
    """The claim is the read, so the database arbitrates rather than the code.

    Two independent connections spend the same token inside their own
    transactions. A check-then-act would let both through; one statement cannot.
    """
    conversation = navdb.conversation(navdb.tenant_a)
    page, opener = _open(navdb, navdb.tenant_a, conversation, list(range(501, 531)))
    opener.close()

    outcomes = []
    left, right = navdb.session(), navdb.session()
    try:
        for db in (left, right):
            outcomes.append(nav.continue_browse(db, token=page.next_token,
                                                tenant_id=navdb.tenant_a, namespace=LIVE,
                                                conversation_id=conversation))
            db.commit()
    finally:
        left.close()
        right.close()
    assert [o.reason for o in outcomes].count(nav.RESOLVED) == 1
    assert [o.reason for o in outcomes].count(nav.REPLAYED) == 1


def test_an_expired_token_is_refused_and_says_so(navdb: Nav):
    conversation = navdb.conversation(navdb.tenant_a)
    page, db = _open(navdb, navdb.tenant_a, conversation, list(range(601, 631)),
                     lifetime_seconds=1)
    later = datetime.now(timezone.utc) + timedelta(seconds=10)
    out = nav.continue_browse(db, token=page.next_token, tenant_id=navdb.tenant_a,
                              namespace=LIVE, conversation_id=conversation, now=later)
    db.commit()
    assert (out.reason, out.product_ids) == (nav.EXPIRED, ())
    db.close()


def test_a_token_minted_for_another_tenant_is_not_found_here(navdb: Nav):
    """Isolation, and it discloses nothing: another tenant's token is
    indistinguishable from one nobody ever minted."""
    theirs = navdb.conversation(navdb.tenant_b)
    page, db = _open(navdb, navdb.tenant_b, theirs, list(range(701, 731)))
    mine = navdb.conversation(navdb.tenant_a)
    out = nav.continue_browse(db, token=page.next_token, tenant_id=navdb.tenant_a,
                              namespace=LIVE, conversation_id=mine)
    db.commit()
    assert (out.reason, out.product_ids) == (nav.NOT_FOUND, ())

    # And it is still spendable where it belongs — the refusal above consumed
    # nothing that was not the caller's.
    theirs_again = nav.continue_browse(db, token=page.next_token, tenant_id=navdb.tenant_b,
                                       namespace=LIVE, conversation_id=theirs)
    db.commit()
    assert theirs_again.reason == nav.RESOLVED
    db.close()


def test_a_token_minted_in_another_conversation_is_not_found_here(navdb: Nav):
    one = navdb.conversation(navdb.tenant_a)
    two = navdb.conversation(navdb.tenant_a)
    page, db = _open(navdb, navdb.tenant_a, one, list(range(801, 831)))
    out = nav.continue_browse(db, token=page.next_token, tenant_id=navdb.tenant_a,
                              namespace=LIVE, conversation_id=two)
    db.commit()
    assert (out.reason, out.product_ids) == (nav.NOT_FOUND, ())
    assert nav.continue_browse(db, token=page.next_token, tenant_id=navdb.tenant_a,
                               namespace=LIVE, conversation_id=one).reason == nav.RESOLVED
    db.commit()
    db.close()


# ── A navigation row is not a product ────────────────────────────────────────


def test_the_two_row_namespaces_cannot_be_read_as_each_other(navdb: Nav):
    conversation = navdb.conversation(navdb.tenant_a)
    page, db = _open(navdb, navdb.tenant_a, conversation, list(range(901, 931)))
    more = nav.row_id(page.next_token)
    assert more.startswith(nm.NAVIGATION_ROW_PREFIX)
    assert rc.product_id_from_row_id(more) is None, "a page token read as a product"
    assert nav.token_from_row_id(rc.row_id(901)) is None, "a product read as a page token"
    # And the affordance's id carries no catalogue identity to be mistaken for one.
    assert not any(str(pid) in more for pid in (901, 902, 903))
    db.close()


# ── Retention is bounded ─────────────────────────────────────────────────────


def test_cleanup_removes_old_tokens_and_leaves_live_ones(navdb: Nav):
    conversation = navdb.conversation(navdb.tenant_a)
    fresh, db = _open(navdb, navdb.tenant_a, conversation, list(range(1001, 1031)))
    stale, other = _open(navdb, navdb.tenant_a, navdb.conversation(navdb.tenant_a),
                         list(range(1101, 1131)))
    other.close()
    with navdb.engine.begin() as conn:
        conn.execute(text(f"UPDATE {nm.NAVIGATION_TABLE} SET created_at = now() - interval '30 days' "
                          "WHERE token = :t"), {"t": stale.next_token})

    removed = nav.sweep(db, retention_seconds=nm.RETENTION_SECONDS)
    assert removed >= 1
    assert nav.continue_browse(db, token=stale.next_token, tenant_id=navdb.tenant_a,
                               namespace=LIVE, conversation_id=conversation).reason == nav.NOT_FOUND
    db.commit()
    # The live one is untouched, so cleanup never costs a customer their place.
    assert nav.continue_browse(db, token=fresh.next_token, tenant_id=navdb.tenant_a,
                               namespace=LIVE, conversation_id=conversation).reason == nav.RESOLVED
    db.commit()
    db.close()


def test_a_sweep_with_nothing_old_enough_removes_nothing(navdb: Nav):
    db = navdb.session()
    assert nav.sweep(db, retention_seconds=nm.RETENTION_SECONDS) == 0
    db.close()


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

        _alembic(dsn, "0113")
        after = set(inspect(engine).get_table_names())
        assert after - before == {nm.NAVIGATION_TABLE}, "0113 touched more than its own relation"

        # The branch-qualified step: this revision only, wherever more than one
        # head exists. (``0111`` is a sibling on this chain and is unaffected.)
        _alembic(dsn, "0113@-1", downgrade=True)
        assert set(inspect(engine).get_table_names()) == before
    finally:
        if engine is not None:
            engine.dispose()
        _drop_database(pg_admin_dsn, name)


def test_the_revision_refuses_a_relation_that_only_looks_right(pg_admin_dsn: str):
    """The check is by definition, not by name.

    A table with the right name and the right columns, but without the unique
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
                    minted_by_turn_id BIGINT NOT NULL, product_ids JSONB NOT NULL,
                    page_offset INTEGER NOT NULL DEFAULT 0, page_size INTEGER NOT NULL,
                    expires_at TIMESTAMPTZ NOT NULL, consumed_at TIMESTAMPTZ,
                    created_at TIMESTAMPTZ NOT NULL DEFAULT now())
            """))
        with pytest.raises(Exception) as refused:
            _alembic(dsn, "0113")
        assert "incompatible" in str(refused.value).lower()
    finally:
        if engine is not None:
            engine.dispose()
        _drop_database(pg_admin_dsn, name)

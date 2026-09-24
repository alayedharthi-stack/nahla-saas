"""One durable page of a browse the customer has not finished reading.

WhatsApp shows ten rows. A merchant with thirty matching products cannot be
answered in one list, and the honest ways to fail — trimming to fit, or
withholding the whole selector — either take a real option away from the
customer or take the tapping away. Pagination is the third way, and it needs
something the runtime did not have: **a result set that outlives the turn that
produced it.**

Why this is its own relation, and not the delivery ledger
=========================================================
The delivery ledger's ``intent_payload`` was checked against the seven
properties such a snapshot needs and failed five of them: no opaque-token
lookup, no expiry, no replay rejection, no bounded retention, and — decisively —
the sequence row is *deliberately mutable* (``outcome``, ``attempt_count`` and
``updated_at`` all move during a send) while a snapshot must not be. There is a
purpose mismatch on top: ``UniqueConstraint("turn_id")`` binds a sequence to the
one turn that created it, and a browsing snapshot exists to be read by **later**
turns. The full proof is in
``docs/engineering/commerce-runtime-product-presentation.md``.

What one row is
===============
One row is **one page token**: the opaque string the customer's next tap will
present, the tenant and conversation it is only valid inside, the whole ordered
result the browse produced, where this page starts in that order, when the token
stops being usable, and whether it has already been used.

The ordered result is copied onto each page of the same series rather than
referenced, and that is the point: page two must be able to continue from the
order page one was composed against **without asking the catalogue again**. A
second search could return a different set — a product sold out, a price
changed, a new arrival — and "the next ten" would then silently mean something
else. The rows a customer paged through are the rows they were shown.

Single use, by construction
===========================
``consumed_at`` is what makes a token a key and not a password. It is set in the
same statement that reads the row, conditional on it still being unset and
unexpired, so two taps racing on the same token produce exactly one page: the
database arbitrates, not the application. A replayed token therefore resolves to
nothing at all, and the turn proceeds on whatever text the tap delivered — the
customer is still answered.

A navigation token is not a product
===================================
Its row id lives in its own namespace (``nahla:more:``), disjoint from the
choice namespace (``nahla:choice:``) a product row uses. Nothing here is a
catalogue identity, nothing here is commerce evidence, and no reply may cite it.
That separation is why a "More" row can never be mistaken for a thirty-first
product, and why no fake product id has to be invented to carry it.

Like the foundation, the ledgers and the handover, this lives on ``RuntimeBase``
metadata, outside the application's ``models.Base``: production startup
materialises ``models.Base`` and pins ``alembic upgrade 0093``, so nothing here
reaches a database until revision ``0113`` is applied on purpose.

PostgreSQL semantics are assumed (``now()``, JSONB, partial indexes).
"""
from __future__ import annotations

from sqlalchemy import (
    BigInteger,
    CheckConstraint,
    Column,
    DateTime,
    ForeignKeyConstraint,
    Index,
    Integer,
    String,
    UniqueConstraint,
    text,
)
from sqlalchemy.dialects.postgresql import JSONB

from core.commerce_runtime.models import CONVERSATIONS_TABLE, RuntimeBase

_SURROGATE_KEY = BigInteger().with_variant(Integer, "sqlite")

NAVIGATION_TABLE = "commerce_runtime_navigation_snapshots"

NAVIGATION_TABLES = (NAVIGATION_TABLE,)

# The row-id namespace a navigation affordance uses. Deliberately disjoint from
# ``reply_choices.ROW_ID_PREFIX``: a tap resolves to a product **or** to a page,
# never ambiguously to both, and neither resolver can be fooled by the other's
# ids.
NAVIGATION_ROW_PREFIX = "nahla:more:"

# Meta shows ten rows. A page that has a successor spends one of them on the
# affordance that reaches the successor, so nine carry products. A final page
# needs no affordance and may carry ten.
MAX_ROWS = 10
PAGE_SIZE = MAX_ROWS - 1

# How long a page token stays usable. A browse the customer returns to tomorrow
# is a new question, not the next page of yesterday's answer — and a token that
# never expires is a key left in a door. Deliberately shorter than the
# browsing-context lapse, which governs what a product still *means*, not how
# long an unfinished list stays open.
TOKEN_LIFETIME_SECONDS = 24 * 3600

# Rows are kept a while past expiry so an operator can still see what a customer
# was offered, then removed. Retention is bounded by this, never unbounded.
RETENTION_SECONDS = 7 * 24 * 3600

_NAMESPACE_SQL = "namespace IN ('live', 'shadow')"


class NavigationSnapshot(RuntimeBase):
    """One page token: what it may show, to whom, until when, and once."""

    __tablename__ = NAVIGATION_TABLE

    id = Column(_SURROGATE_KEY, primary_key=True)
    # The opaque continuation token. Random, unguessable, and meaningless
    # outside this table: it names no product, no offset and no tenant, so
    # nothing can be inferred from it or forged by editing it.
    token = Column(String(64), nullable=False)
    # The series this page belongs to. Every page of one browse shares it, which
    # is what lets an operator — and the cleanup — see a whole abandoned browse
    # rather than a scatter of unrelated rows.
    series = Column(String(64), nullable=False)
    tenant_id = Column(Integer, nullable=False)
    namespace = Column(String(16), nullable=False)
    conversation_id = Column(BigInteger, nullable=False)
    # The turn that minted this token. Provenance only: the token's validity
    # never depends on it, because a page is answered by a *later* turn.
    minted_by_turn_id = Column(BigInteger, nullable=False)
    # The whole ordered result the browse produced. Never re-derived: page two
    # continues this order rather than searching again, so the customer pages
    # through the rows they were actually shown.
    product_ids = Column(JSONB, nullable=False)
    # Where this page starts in that order.
    page_offset = Column(Integer, nullable=False, server_default=text("0"))
    page_size = Column(Integer, nullable=False)
    expires_at = Column(DateTime(timezone=True), nullable=False)
    # Set by the same statement that reads the row. A token is a single use.
    consumed_at = Column(DateTime(timezone=True), nullable=True)
    created_at = Column(DateTime(timezone=True), nullable=False, server_default=text("now()"))

    __table_args__ = (
        ForeignKeyConstraint(["tenant_id"], ["tenants.id"],
                             name="fk_commerce_runtime_navigation_tenant"),
        # Scope, not decoration: a token is only ever resolvable inside the
        # conversation it was minted in, and the database holds that shape.
        ForeignKeyConstraint(
            ["conversation_id", "tenant_id", "namespace"],
            [f"{CONVERSATIONS_TABLE}.id", f"{CONVERSATIONS_TABLE}.tenant_id",
             f"{CONVERSATIONS_TABLE}.namespace"],
            name="fk_commerce_runtime_navigation_conversation_scope",
        ),
        UniqueConstraint("token", name="uq_commerce_runtime_navigation_token"),
        # One page per offset per series: minting the same page twice is a bug,
        # and the database says so rather than serving it twice.
        UniqueConstraint("series", "page_offset", name="uq_commerce_runtime_navigation_page"),
        CheckConstraint(_NAMESPACE_SQL, name="ck_commerce_runtime_navigation_namespace"),
        CheckConstraint("page_offset >= 0", name="ck_commerce_runtime_navigation_offset"),
        CheckConstraint(f"page_size >= 1 AND page_size <= {MAX_ROWS}",
                        name="ck_commerce_runtime_navigation_page_size"),
        # Resolution always asks "this token, in this conversation, for this
        # tenant" — so that is the index, and an unconsumed token is the only
        # kind worth finding fast.
        Index("ix_commerce_runtime_navigation_open", "tenant_id", "namespace", "conversation_id",
              postgresql_where=text("consumed_at IS NULL")),
        # Cleanup sweeps by age and nothing else.
        Index("ix_commerce_runtime_navigation_expiry", "expires_at"),
    )


NAVIGATION_TABLE_OBJECTS = (NavigationSnapshot.__table__,)


def create_navigation_tables(bind) -> None:
    """Create exactly this relation. Used by tests, never by startup."""
    RuntimeBase.metadata.create_all(bind, tables=list(NAVIGATION_TABLE_OBJECTS))


__all__ = [
    "MAX_ROWS", "NAVIGATION_ROW_PREFIX", "NAVIGATION_TABLE", "NAVIGATION_TABLES",
    "NAVIGATION_TABLE_OBJECTS", "NavigationSnapshot", "PAGE_SIZE", "RETENTION_SECONDS",
    "TOKEN_LIFETIME_SECONDS", "create_navigation_tables",
]

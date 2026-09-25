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
result the originating search produced, where the page it opens starts in that
order, when the token stops being usable, and whether it has been used.

The ordered result is copied onto each page of the same series rather than
referenced, and that is the point: page two continues from the order page one
was composed against **without asking the catalogue again**. A second search
could return a different set — a product sold out, a price changed, a new
arrival — and "the next nine" would then silently mean something else.

Provenance is kept on the row: the turn and the search call that produced the
result, the strategy that matched, a digest of the query (never the query), and
whether the result is **complete** — proven to hold every match — or stopped at
the platform's cap. A browse that is not complete never lets anything
downstream say "that is everything".

The two words a paged list shows that are not the merchant's — the row that
reaches the next page, and the button that opens the list — are the model's,
in the customer's language, given when the browse was opened. They are carried
here so later pages show the model's words and never a platform default.

Single use, by construction
===========================
``consumed_at`` is what makes a token a key and not a password. It is set in the
same statement that reads the row, conditional on it still being unset and
unexpired, **inside the transaction that reserves the reply carrying the next
page** — so a page is spent if and only if its answer was reserved, and two
taps racing on one token produce exactly one page.

A navigation token is not a product
===================================
Its row id lives in its own namespace (``nahla:more:``), disjoint from the
choice namespace (``nahla:choice:``) a product row uses. Nothing here is a
catalogue identity, nothing here is commerce evidence, and no reply may cite it.

Like the foundation, the ledgers and the handover, this lives on ``RuntimeBase``
metadata, outside the application's ``models.Base``: production startup
materialises ``models.Base`` and pins ``alembic upgrade 0093``, so nothing here
reaches a database until revision ``0113`` is applied on purpose.

PostgreSQL semantics are assumed (``now()``, JSONB).
"""
from __future__ import annotations

from sqlalchemy import (
    BigInteger,
    Boolean,
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

# How long a row is kept once its token stopped being usable, so an operator can
# still see what a customer was offered; then the sweep removes it. Measured
# from expiry so the sweep can use the expiry index: with a 24h lifetime a row
# lives at most seven days in all. Retention is bounded, never open-ended.
RETENTION_AFTER_EXPIRY_SECONDS = 6 * 24 * 3600

# The most identities one stored browse holds. The search hands the platform at
# most this many (``search_candidates.CANDIDATE_CAP``) and the database refuses
# more, so the bound holds whoever writes the row.
MAX_STORED_PRODUCTS = 50

# The model's words a paged list carries, bounded as the channel renders them.
MAX_MORE_LABEL = 24
MAX_BUTTON_LABEL = 20

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
    # is what lets an operator see a whole abandoned browse rather than a
    # scatter of unrelated rows.
    series = Column(String(64), nullable=False)
    tenant_id = Column(Integer, nullable=False)
    namespace = Column(String(16), nullable=False)
    conversation_id = Column(BigInteger, nullable=False)
    # The turn that minted this token. Provenance only: the token's validity
    # never depends on it, because a page is answered by a *later* turn.
    minted_by_turn_id = Column(BigInteger, nullable=False)
    # Where the stored result came from: the turn and the search call that
    # produced it, the strategy that matched, and a digest of the query — never
    # the words the customer typed.
    origin_turn_id = Column(BigInteger, nullable=False)
    origin_call_id = Column(String(128), nullable=False)
    search_method = Column(String(64), nullable=False)
    query_digest = Column(String(64), nullable=False)
    # The whole ordered result the search produced, up to the platform's cap.
    # Never re-derived: page two continues this order rather than searching
    # again, so the customer pages through the result they were shown.
    product_ids = Column(JSONB, nullable=False)
    # Proven, not assumed: true only when the search read fewer matches than
    # it asked for, so ``product_ids`` holds every one of them.
    complete = Column(Boolean, nullable=False)
    # Where the page this token opens starts in that order, and how many
    # products a page that has a successor carries.
    page_offset = Column(Integer, nullable=False)
    page_size = Column(Integer, nullable=False)
    # The model's own words, given when the browse was opened, in the
    # customer's language. The platform has none of its own to use instead.
    more_label = Column(String(MAX_MORE_LABEL), nullable=False)
    button_label = Column(String(MAX_BUTTON_LABEL), nullable=False)
    expires_at = Column(DateTime(timezone=True), nullable=False)
    # Set by the same statement that reads the row, in the transaction that
    # reserves the reply showing the page. A token is a single use.
    consumed_at = Column(DateTime(timezone=True), nullable=True)
    consumed_by_turn_id = Column(BigInteger, nullable=True)
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
        CheckConstraint(
            f"jsonb_typeof(product_ids) = 'array' AND jsonb_array_length(product_ids) >= 1 "
            f"AND jsonb_array_length(product_ids) <= {MAX_STORED_PRODUCTS}",
            name="ck_commerce_runtime_navigation_products"),
        # A token opens a page after the first — page one never needs one — and
        # a page that exists: an offset past the stored result is a token that
        # leads nowhere, which the database refuses to store.
        CheckConstraint("page_offset >= 1 AND page_offset < jsonb_array_length(product_ids)",
                        name="ck_commerce_runtime_navigation_offset"),
        CheckConstraint(f"page_size >= 1 AND page_size < {MAX_ROWS}",
                        name="ck_commerce_runtime_navigation_page_size"),
        CheckConstraint("char_length(more_label) >= 1 AND char_length(button_label) >= 1",
                        name="ck_commerce_runtime_navigation_words"),
        # Spent means both: when, and by which turn. One without the other is a
        # row nobody can account for.
        CheckConstraint("(consumed_at IS NULL) = (consumed_by_turn_id IS NULL)",
                        name="ck_commerce_runtime_navigation_consumed"),
        # Cleanup sweeps by expiry and nothing else. A token is found by its own
        # unique index; nothing reads this relation any other way.
        Index("ix_commerce_runtime_navigation_expiry", "expires_at"),
    )


NAVIGATION_TABLE_OBJECTS = (NavigationSnapshot.__table__,)


def create_navigation_tables(bind) -> None:
    """Create exactly this relation. Used by tests, never by startup."""
    RuntimeBase.metadata.create_all(bind, tables=list(NAVIGATION_TABLE_OBJECTS))


__all__ = [
    "MAX_BUTTON_LABEL", "MAX_MORE_LABEL", "MAX_ROWS", "MAX_STORED_PRODUCTS",
    "NAVIGATION_ROW_PREFIX", "NAVIGATION_TABLE", "NAVIGATION_TABLES",
    "NAVIGATION_TABLE_OBJECTS", "NavigationSnapshot", "PAGE_SIZE",
    "RETENTION_AFTER_EXPIRY_SECONDS", "TOKEN_LIFETIME_SECONDS", "create_navigation_tables",
]

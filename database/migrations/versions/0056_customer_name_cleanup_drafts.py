"""0056 — customer_name_cleanup_drafts: incremental review session.

Revision ID: 0056
Revises:    0055

Why this migration exists
─────────────────────────
The merchant-facing "تنظيف أسماء العملاء" tool surfaces every
customer whose stored name needs work. On large tenants (8 000+
customers, 1 500+ matches) it is unrealistic to review every row
in a single sitting — merchants need to edit some, close the
modal, come back later, and pick up exactly where they left off.

This migration creates ``customer_name_cleanup_drafts`` which
persists the merchant's in-progress edit state per customer:

  * ``removed_word_indices`` — JSONB array of token indices the
    merchant chip-toggled OFF (into the whitespace-split tokens of
    ``original_name``). NULL = use cleaner's default removal set.
  * ``cleared`` — True if the merchant pressed
    "مسح الاسم بالكامل" for the row (forces null name on apply).
  * ``status`` — ``"edited"`` (default; merchant touched the row)
    or ``"skipped"`` (merchant explicitly opted out so the row
    stops appearing in future review sessions).
  * ``original_name`` — Customer.name snapshot at creation. The
    preview endpoint compares this against the live value and
    discards the draft if it drifted (merchant edited the row
    in another tab between sessions).

Workflow:
  1. Merchant opens modal → preview endpoint returns matches
     merged with whatever drafts exist (so chip state is restored).
  2. Merchant edits chips → frontend autosaves to this table via
     POST /customers/name-cleanup/draft/save (debounced).
  3. "تطبيق المحدد" / "تطبيق ذوي الثقة العالية" → applies the
     pending edits to Customer.name + writes audit rows + DELETES
     the corresponding draft rows (no longer interesting).
  4. "تجاهل المسودة" wipes every draft for the tenant.

Tenant isolation:
  * Unique constraint on (tenant_id, customer_id) — a single draft
    per customer per tenant. The application also asserts
    customers belong to the requesting tenant on every write,
    so cross-tenant aliasing is impossible.
  * Covering indexes on (tenant_id, updated_at) and
    (tenant_id, status) for fast list / count queries.

Idempotency (F16)
─────────────────
``create_table`` and ``create_index`` are guarded by inspector
checks so re-running on a DB where the table or indexes already
exist is a no-op rather than a DuplicateTable / DuplicateRelation
error.
"""
from __future__ import annotations

import sqlalchemy as sa
from alembic import op
from sqlalchemy import inspect
from sqlalchemy.dialects.postgresql import JSONB

revision = "0056"
down_revision = "0055"
branch_labels = None
depends_on = None


def _has_table(bind, table_name: str) -> bool:
    return table_name in inspect(bind).get_table_names()


def _has_index(bind, table_name: str, index_name: str) -> bool:
    if not _has_table(bind, table_name):
        return False
    return any(
        ix["name"] == index_name
        for ix in inspect(bind).get_indexes(table_name)
    )


def _has_constraint(bind, table_name: str, constraint_name: str) -> bool:
    if not _has_table(bind, table_name):
        return False
    insp = inspect(bind)
    unique = insp.get_unique_constraints(table_name)
    return any(c.get("name") == constraint_name for c in unique)


def upgrade() -> None:
    bind = op.get_bind()

    # SQLite test envs use JSON; Postgres uses JSONB. SA picks the
    # right one via ``JSONB().with_variant``.
    jsonb_type = JSONB().with_variant(sa.JSON(), "sqlite")

    if not _has_table(bind, "customer_name_cleanup_drafts"):
        op.create_table(
            "customer_name_cleanup_drafts",
            sa.Column("id", sa.Integer(), primary_key=True),
            sa.Column(
                "tenant_id", sa.Integer(),
                sa.ForeignKey("tenants.id"), nullable=False,
            ),
            sa.Column(
                "customer_id", sa.Integer(),
                sa.ForeignKey("customers.id"), nullable=False,
            ),
            sa.Column("original_name", sa.String(), nullable=True),
            sa.Column("removed_word_indices", jsonb_type, nullable=True),
            sa.Column(
                "cleared", sa.Boolean(),
                server_default=sa.text("false"), nullable=False,
            ),
            sa.Column(
                "status", sa.String(),
                server_default=sa.text("'edited'"), nullable=False,
            ),
            sa.Column(
                "actor_user_id", sa.Integer(),
                sa.ForeignKey("users.id"), nullable=True,
            ),
            sa.Column(
                "created_at",
                sa.DateTime(timezone=True),
                server_default=sa.text("CURRENT_TIMESTAMP"),
                nullable=False,
            ),
            sa.Column(
                "updated_at",
                sa.DateTime(timezone=True),
                server_default=sa.text("CURRENT_TIMESTAMP"),
                nullable=False,
            ),
            sa.UniqueConstraint(
                "tenant_id", "customer_id",
                name="uq_cleanup_draft_tenant_customer",
            ),
        )

    if not _has_index(
        bind, "customer_name_cleanup_drafts",
        "ix_cleanup_draft_tenant_updated",
    ):
        op.create_index(
            "ix_cleanup_draft_tenant_updated",
            "customer_name_cleanup_drafts",
            ["tenant_id", "updated_at"],
        )

    if not _has_index(
        bind, "customer_name_cleanup_drafts",
        "ix_cleanup_draft_tenant_status",
    ):
        op.create_index(
            "ix_cleanup_draft_tenant_status",
            "customer_name_cleanup_drafts",
            ["tenant_id", "status"],
        )


def downgrade() -> None:
    op.drop_index(
        "ix_cleanup_draft_tenant_status",
        table_name="customer_name_cleanup_drafts",
    )
    op.drop_index(
        "ix_cleanup_draft_tenant_updated",
        table_name="customer_name_cleanup_drafts",
    )
    op.drop_table("customer_name_cleanup_drafts")

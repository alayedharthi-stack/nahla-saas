"""0053 — customer_segments_manual.mode (include / exclude).

Revision ID: 0053
Revises: 0052

Why this exists
───────────────
Manual segments started as a one-direction "pin this customer to a
cohort" feature. Real merchant workflow turned out to need the
opposite too — "this customer was auto-classified as VIP but I
disagree, hide them from VIP filters / campaigns." Without a way
to *exclude*, merchants had no escape hatch from a wrong RFM call.

Design
──────
A single ``mode`` column with two values:

  * ``include`` — adds the customer to the segment (or keeps them
    when they're already in it via auto-classification). This is
    the default for legacy rows; semantically identical to the
    pre-0053 behaviour.
  * ``exclude`` — removes the customer from segment-membership
    queries even if the auto-classifier matched. The auto match
    itself is NOT mutated (we never overwrite RFM output) — the
    filter / campaign-snapshot layer subtracts excludes after the
    union.

Filter formula (cemented in services.manual_segments + customers
list endpoint):

   final_member ⇔ (auto_match ∨ manual_include) ∧ ¬ manual_exclude

The unique index on ``(tenant_id, customer_id, segment_key)``
stays — a customer can be in at most one mode per segment at a
time. Toggling include⇄exclude updates the row in place rather
than creating a second row.

Backwards compatibility
───────────────────────
Existing rows are auto-set to ``mode='include'`` because that was
the only behaviour before. The constraint is set ``NOT NULL`` with
a server default so any future row that omits ``mode`` lands
correctly as ``include``.

Idempotency (F16)
─────────────────
Guarded by inspector checks so re-running on a DB where the column
or the index already exists is a no-op. Critical because some
production schemas were built before Alembic was wired in.
"""
from __future__ import annotations

import sqlalchemy as sa
from alembic import op
from sqlalchemy import inspect

revision = "0053"
down_revision = "0052"
branch_labels = None
depends_on = None


def _has_table(bind, table_name: str) -> bool:
    return table_name in inspect(bind).get_table_names()


def _has_column(bind, table_name: str, column_name: str) -> bool:
    if not _has_table(bind, table_name):
        return False
    return any(
        c["name"] == column_name
        for c in inspect(bind).get_columns(table_name)
    )


def _has_index(bind, table_name: str, index_name: str) -> bool:
    if not _has_table(bind, table_name):
        return False
    return any(
        ix["name"] == index_name
        for ix in inspect(bind).get_indexes(table_name)
    )


def upgrade() -> None:
    bind = op.get_bind()

    if not _has_table(bind, "customer_segments_manual"):
        # The parent table is missing entirely. This would be a
        # pre-0052 DB, which shouldn't happen because Alembic walks
        # the chain in order — but guarding here makes
        # ``alembic upgrade head`` resilient if someone runs this
        # revision in isolation via ``alembic upgrade 0053``.
        return

    if not _has_column(bind, "customer_segments_manual", "mode"):
        # Postgres + SQLite both support ``ADD COLUMN`` with a default,
        # which back-fills existing rows in one shot. We use a plain
        # default rather than a CHECK constraint so old data lands as
        # ``include`` (the legacy semantic).
        op.add_column(
            "customer_segments_manual",
            sa.Column(
                "mode",
                sa.String(length=16),
                nullable=False,
                server_default="include",
            ),
        )

    # Index the (tenant, segment, mode) tuple — the customers list
    # filter and the campaign snapshot both want "all include rows
    # for VIP" or "all exclude rows for VIP" with no other filtering.
    # Without this index the planner falls back to a full scan of
    # the table for tenants with millions of tags.
    if not _has_index(
        bind, "customer_segments_manual",
        "ix_customer_segments_manual_tenant_segment_mode",
    ):
        op.create_index(
            "ix_customer_segments_manual_tenant_segment_mode",
            "customer_segments_manual",
            ["tenant_id", "segment_key", "mode"],
        )


def downgrade() -> None:
    op.drop_index(
        "ix_customer_segments_manual_tenant_segment_mode",
        table_name="customer_segments_manual",
    )
    op.drop_column("customer_segments_manual", "mode")

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
"""
from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0053"
down_revision = "0052"
branch_labels = None
depends_on = None


def upgrade() -> None:
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

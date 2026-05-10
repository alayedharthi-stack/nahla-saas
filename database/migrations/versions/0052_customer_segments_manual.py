"""0052 — customer_segments_manual: merchant-curated segment tags.

Revision ID: 0052
Revises: 0051

What this is
────────────
A merchant-curated link table that lets a tenant pin a Customer to
one or more of Nahla's *official* marketing cohorts (vip, new,
unsubscribed, no_purchase_60, …) regardless of what the auto-classifier
inferred from RFM / behavioural signals.

Why we don't ship a free-form tag system
────────────────────────────────────────
The product owner explicitly rejected open-ended tags. The reason is
that Nahla's whole UX rests on a single, agreed list of cohorts that
campaigns / autopilot / analytics all consume. Letting merchants
invent ad-hoc tag strings would:

  * fragment the segmentation language across stores;
  * silently break campaign targeting (a typo in a tag = empty audience);
  * undermine the canonical registry in
    ``services.nahla_segments.SEGMENTS``.

So this table only stores ``segment_key`` values that match the
canonical registry. Validation is enforced at the API layer
(``services.manual_segments.add_manual_segment``) — the DB column is
plain text only because Postgres / Alembic don't have a portable way
to express "value MUST be one of {dynamic Python set}", but the
single insert path is policed.

Idempotency
───────────
``UNIQUE(tenant_id, customer_id, segment_key)`` means a re-tag is a
no-op rather than a duplicate row. The API layer translates an insert
conflict into a 200 OK so merchants see the same outcome whether the
tag was already there or not.
"""
from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0052"
down_revision = "0051"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "customer_segments_manual",
        sa.Column("id", sa.BigInteger().with_variant(sa.Integer(), "sqlite"),
                  primary_key=True, autoincrement=True),
        sa.Column("tenant_id",   sa.Integer(),     sa.ForeignKey("tenants.id"), nullable=False),
        sa.Column("customer_id", sa.Integer(),
                  sa.ForeignKey("customers.id", ondelete="CASCADE"), nullable=False),
        sa.Column("segment_key", sa.String(length=64), nullable=False),
        # source kept for future "system-applied" rows (e.g. a bulk
        # importer that pre-tags VIPs from a CSV). Today everything
        # written here is ``manual``.
        sa.Column("source",      sa.String(length=16), nullable=False, server_default="manual"),
        sa.Column("created_by",  sa.Integer(),         nullable=True),
        sa.Column("created_at",  sa.DateTime(),        nullable=False, server_default=sa.func.now()),
    )

    # Idempotency: same (tenant, customer, segment) tuple appears at
    # most once. Without this a merchant could "re-tag VIP" 1000 times
    # and the customer would silently grow 1000 rows.
    op.create_index(
        "uq_customer_segments_manual_tenant_customer_segment",
        "customer_segments_manual",
        ["tenant_id", "customer_id", "segment_key"],
        unique=True,
    )

    # Filter "all customers tagged X" — used by the customers page
    # filter and the campaign wizard exclude/include lists.
    op.create_index(
        "ix_customer_segments_manual_tenant_segment",
        "customer_segments_manual",
        ["tenant_id", "segment_key"],
    )

    # Filter "all manual segments for one customer" — used by the
    # drawer and the campaign snapshot.
    op.create_index(
        "ix_customer_segments_manual_customer",
        "customer_segments_manual",
        ["customer_id"],
    )


def downgrade() -> None:
    op.drop_index("ix_customer_segments_manual_customer", table_name="customer_segments_manual")
    op.drop_index("ix_customer_segments_manual_tenant_segment", table_name="customer_segments_manual")
    op.drop_index("uq_customer_segments_manual_tenant_customer_segment", table_name="customer_segments_manual")
    op.drop_table("customer_segments_manual")

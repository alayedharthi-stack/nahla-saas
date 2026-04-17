"""Order dashboard fields: external_order_number, customer_name, source.

Revision ID: 0026
Revises: 0025
Create Date: 2026-04-17

Why
───
The merchant dashboard's orders table was rendering meaningless data:

  • The "الطلب" column showed our internal `Order.id` (e.g. 11, 10) instead
    of the platform's human-visible order number (e.g. Salla
    `reference_id` 1585297702 → "#1585297702").
  • The customer column was blank because we only stored the customer
    inside `customer_info` JSONB; rows where that blob was empty (legacy
    syncs / stripped webhooks) had nothing to display.
  • There was no way to tell whether an order came from Salla, Zid,
    Shopify, WhatsApp (AI sales), or was created manually.

This migration adds three first-class columns:

  • `external_order_number` — VARCHAR, indexed. Populated from the
    platform's human reference (Salla `reference_id`, Zid `code`, Shopify
    `name`). Falls back to `external_id` when no separate number exists.
  • `customer_name` — VARCHAR. Denormalised from `customer_info.name` (or
    the AI-sales create-order payload) so the dashboard cell is never
    blank.
  • `source` — VARCHAR, indexed. One of `salla` | `zid` | `shopify` |
    `whatsapp` | `manual`. Used both for the dashboard "المصدر" badge
    and for analytics filtering.

All three are nullable so the migration is safe on a populated table.
A follow-up backfill (`scripts/backfill_order_dashboard_fields.py`) is
provided to repair existing rows from `customer_info` and `extra_metadata`.
"""
from alembic import op
import sqlalchemy as sa


revision = "0026"
down_revision = "0025"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "orders",
        sa.Column("external_order_number", sa.String(), nullable=True),
    )
    op.add_column(
        "orders",
        sa.Column("customer_name", sa.String(), nullable=True),
    )
    op.add_column(
        "orders",
        sa.Column("source", sa.String(), nullable=True),
    )
    op.create_index(
        "ix_orders_external_order_number",
        "orders",
        ["external_order_number"],
        unique=False,
    )
    op.create_index(
        "ix_orders_source",
        "orders",
        ["source"],
        unique=False,
    )


def downgrade() -> None:
    op.drop_index("ix_orders_source", table_name="orders")
    op.drop_index("ix_orders_external_order_number", table_name="orders")
    op.drop_column("orders", "source")
    op.drop_column("orders", "customer_name")
    op.drop_column("orders", "external_order_number")

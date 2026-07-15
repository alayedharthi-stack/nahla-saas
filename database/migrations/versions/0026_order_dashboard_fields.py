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

Idempotency (F16)
─────────────────
Forward ORM / ``create_all`` drift may have pre-created the dashboard
columns and indexes while ``alembic_version`` lags behind 0026.
Inspector-guarded DDL adds only missing pieces; clean upgrades unchanged.
"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op
from sqlalchemy import inspect

revision = "0026"
down_revision = "0025"
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

    if not _has_column(bind, "orders", "external_order_number"):
        op.add_column(
            "orders",
            sa.Column("external_order_number", sa.String(), nullable=True),
        )
    if not _has_column(bind, "orders", "customer_name"):
        op.add_column(
            "orders",
            sa.Column("customer_name", sa.String(), nullable=True),
        )
    if not _has_column(bind, "orders", "source"):
        op.add_column(
            "orders",
            sa.Column("source", sa.String(), nullable=True),
        )
    if not _has_index(bind, "orders", "ix_orders_external_order_number"):
        op.create_index(
            "ix_orders_external_order_number",
            "orders",
            ["external_order_number"],
            unique=False,
        )
    if not _has_index(bind, "orders", "ix_orders_source"):
        op.create_index(
            "ix_orders_source",
            "orders",
            ["source"],
            unique=False,
        )


def downgrade() -> None:
    bind = op.get_bind()
    if _has_index(bind, "orders", "ix_orders_source"):
        op.drop_index("ix_orders_source", table_name="orders")
    if _has_index(bind, "orders", "ix_orders_external_order_number"):
        op.drop_index("ix_orders_external_order_number", table_name="orders")
    if _has_column(bind, "orders", "source"):
        op.drop_column("orders", "source")
    if _has_column(bind, "orders", "customer_name"):
        op.drop_column("orders", "customer_name")
    if _has_column(bind, "orders", "external_order_number"):
        op.drop_column("orders", "external_order_number")

"""Product back-in-stock waitlist + product stock tracking columns.

Revision ID: 0025
Revises: 0024
Create Date: 2026-04-16

Why
───
Building Part A of the four-feature WhatsApp automation set: "Product Back
In Stock". The merchant needs two things the schema doesn't expose today:

  1. A first-class waitlist of customers who asked to be notified when a
     specific product is restocked. Currently no such table exists, so
     `back_in_stock` automations have nothing to fan out to.

  2. A reliable place to detect a 0 → >0 stock transition. Stock data
     today only lives in `products.metadata->>'in_stock'` (and
     `metadata->>'stock_qty'`). That works for AI context but is awkward
     for transition detection because every product upsert overwrites the
     whole JSONB blob, so we can't compare old-vs-new at the column level.

What this migration does
────────────────────────
  1. Creates `product_interests` — one row per (tenant, product, customer)
     pending notify-me request. Mirrors `database.models.ProductInterest`.

  2. Adds two real columns on `products` for stock tracking:
       • `stock_quantity` (int, nullable) — last known integer stock level
       • `in_stock`       (bool, server default true)
     The store_sync upsert path will populate these from the adapter
     response. Detection is then a column-level compare:
        was_zero  = (existing.stock_quantity == 0 OR existing.in_stock = false)
        now_avail = (new.stock_quantity > 0 AND new.in_stock = true)
     Triggers a `product_back_in_stock` AutomationEvent fan-out.

Rollback drops the table and the two columns. The JSONB form continues
to be populated by the upsert path so historical data is not lost.

Idempotency (F16)
─────────────────
``Base.metadata.create_all()`` may have pre-created ``product_interests``
and the ``products`` stock columns while ``alembic_version`` lags behind
0025. Inspector-guarded DDL skips present objects and adds missing
indexes/constraints; clean upgrades are unchanged.
"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op
from sqlalchemy import inspect

revision: str = "0025"
down_revision: Union[str, None] = "0024"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


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


def _has_unique_constraint(bind, table_name: str, constraint_name: str) -> bool:
    if not _has_table(bind, table_name):
        return False
    insp = inspect(bind)
    try:
        cons = insp.get_unique_constraints(table_name)
    except NotImplementedError:
        return False
    return any(c.get("name") == constraint_name for c in cons)


def upgrade() -> None:
    bind = op.get_bind()

    # ── 1) product_interests: notify-me waitlist ──────────────────────────────
    if not _has_table(bind, "product_interests"):
        op.create_table(
            "product_interests",
            sa.Column("id", sa.Integer(), primary_key=True, nullable=False),
            sa.Column("tenant_id", sa.Integer(),
                      sa.ForeignKey("tenants.id"), nullable=False, index=True),
            sa.Column("product_id", sa.Integer(),
                      sa.ForeignKey("products.id"), nullable=False, index=True),
            sa.Column("customer_id", sa.Integer(),
                      sa.ForeignKey("customers.id"), nullable=False, index=True),
            sa.Column("customer_phone", sa.String(), nullable=True),
            sa.Column("source", sa.String(), nullable=True),
            sa.Column("notified", sa.Boolean(),
                      nullable=False, server_default=sa.text("false")),
            sa.Column("notified_at", sa.DateTime(), nullable=True),
            sa.Column("created_at", sa.DateTime(),
                      nullable=False, server_default=sa.func.now()),
            sa.Column("metadata", sa.JSON(), nullable=True),
            sa.UniqueConstraint(
                "tenant_id", "product_id", "customer_id", "notified",
                name="uq_product_interest_pending_per_customer",
            ),
        )
    if (
        _has_table(bind, "product_interests")
        and not _has_unique_constraint(
            bind, "product_interests", "uq_product_interest_pending_per_customer"
        )
        and bind.dialect.name != "sqlite"
    ):
        op.create_unique_constraint(
            "uq_product_interest_pending_per_customer",
            "product_interests",
            ["tenant_id", "product_id", "customer_id", "notified"],
        )
    if not _has_index(bind, "product_interests", "ix_product_interests_pending"):
        op.create_index(
            "ix_product_interests_pending",
            "product_interests",
            ["tenant_id", "product_id", "notified"],
        )

    # ── 2) products: stock columns ────────────────────────────────────────────
    if not _has_column(bind, "products", "stock_quantity"):
        op.add_column(
            "products",
            sa.Column("stock_quantity", sa.Integer(), nullable=True),
        )
    if not _has_column(bind, "products", "in_stock"):
        op.add_column(
            "products",
            sa.Column(
                "in_stock", sa.Boolean(),
                nullable=False, server_default=sa.text("true"),
            ),
        )


def downgrade() -> None:
    bind = op.get_bind()
    if _has_column(bind, "products", "in_stock"):
        op.drop_column("products", "in_stock")
    if _has_column(bind, "products", "stock_quantity"):
        op.drop_column("products", "stock_quantity")
    if _has_index(bind, "product_interests", "ix_product_interests_pending"):
        op.drop_index("ix_product_interests_pending", table_name="product_interests")
    if _has_table(bind, "product_interests"):
        op.drop_table("product_interests")

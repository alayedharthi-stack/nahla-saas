"""0080 — Order shipments foundation (internal fulfillment, no carrier API).

Revision ID: 0080
Revises:    0079
"""
from __future__ import annotations

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import JSONB


revision = "0080"
down_revision = "0079"
branch_labels = None
depends_on = None


def _has_table(bind, table: str) -> bool:
    return table in sa.inspect(bind).get_table_names()


def upgrade() -> None:
    bind = op.get_bind()
    if _has_table(bind, "order_shipments"):
        return

    op.create_table(
        "order_shipments",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("tenant_id", sa.Integer(), sa.ForeignKey("tenants.id"), nullable=False),
        sa.Column("order_id", sa.Integer(), sa.ForeignKey("orders.id"), nullable=False),
        sa.Column("provider", sa.String(), nullable=False, server_default="internal"),
        sa.Column("status", sa.String(), nullable=False, server_default="shipment_created"),
        sa.Column("tracking_number", sa.String(), nullable=True),
        sa.Column("label_url", sa.String(), nullable=True),
        sa.Column("label_pdf_path", sa.String(), nullable=True),
        sa.Column("recipient_name", sa.String(), nullable=True),
        sa.Column("recipient_phone", sa.String(), nullable=True),
        sa.Column("address_type", sa.String(), nullable=True),
        sa.Column("address_text", sa.Text(), nullable=True),
        sa.Column("address_url", sa.String(), nullable=True),
        sa.Column("latitude", sa.String(), nullable=True),
        sa.Column("longitude", sa.String(), nullable=True),
        sa.Column("cod_amount", sa.String(), nullable=True),
        sa.Column("created_at", sa.DateTime(), nullable=True),
        sa.Column("updated_at", sa.DateTime(), nullable=True),
        sa.Column("metadata", JSONB(), nullable=True),
        sa.UniqueConstraint("order_id", name="uq_order_shipments_order_id"),
    )
    op.create_index("ix_order_shipments_tenant_id", "order_shipments", ["tenant_id"])
    op.create_index("ix_order_shipments_order_id", "order_shipments", ["order_id"])


def downgrade() -> None:
    bind = op.get_bind()
    if not _has_table(bind, "order_shipments"):
        return
    op.drop_index("ix_order_shipments_order_id", table_name="order_shipments")
    op.drop_index("ix_order_shipments_tenant_id", table_name="order_shipments")
    op.drop_table("order_shipments")

"""Durable channel-retirement requests for deleted catalog products.

Revision ID: 0116
Revises: 0112

Creates ``catalog_channel_retirements`` (see ``database/models.py``
``CatalogChannelRetirement``). Additive only, idempotent (skips when the
table already exists, e.g. created by the startup ``create_all``). Chained
on 0112 as an explicit sibling of the dormant 0113/0114 runtime branch and
the 0115 payments branch, exactly like 0115, so applying it never pulls
those branches in. No data is touched; downgrade drops only this table.
"""
from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0116"
down_revision = "0112"
branch_labels = None
depends_on = None

TABLE = "catalog_channel_retirements"


def upgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    if TABLE in set(inspector.get_table_names()):
        # A database that built the table from an earlier head's create_all may
        # still carry NOT NULL on catalog_id; relax it so a tenant without a
        # stamped catalog can record a retirement (the drain resolves it).
        for column in inspector.get_columns(TABLE):
            if column["name"] == "catalog_id" and not column.get("nullable", True):
                op.alter_column(TABLE, "catalog_id", existing_type=sa.String(length=64), nullable=True)
        return
    op.create_table(
        TABLE,
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("tenant_id", sa.Integer(), sa.ForeignKey("tenants.id", ondelete="CASCADE"), nullable=False),
        sa.Column("catalog_id", sa.String(length=64), nullable=True),
        sa.Column("retailer_id", sa.String(length=255), nullable=False),
        sa.Column("meta_item_id", sa.String(length=128), nullable=True),
        sa.Column("product_id", sa.Integer(), nullable=True),
        sa.Column("reason", sa.String(length=64), nullable=False),
        sa.Column("status", sa.String(length=32), nullable=False, server_default="pending"),
        sa.Column("attempts", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("next_attempt_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("last_error", sa.String(length=255), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("done_at", sa.DateTime(timezone=True), nullable=True),
        sa.UniqueConstraint(
            "tenant_id", "catalog_id", "retailer_id",
            name="uq_catalog_channel_retirements_tenant_catalog_retailer",
        ),
    )
    op.create_index(
        "ix_catalog_channel_retirements_tenant_status", TABLE, ["tenant_id", "status"],
    )


def downgrade() -> None:
    bind = op.get_bind()
    if TABLE not in set(sa.inspect(bind).get_table_names()):
        return
    op.drop_index("ix_catalog_channel_retirements_tenant_status", table_name=TABLE)
    op.drop_table(TABLE)

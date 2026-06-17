"""0082 — Scope WhatsApp usage counters to billing subscription period.

Revision ID: 0082
Revises:    0081

Adds:
  - whatsapp_usage.subscription_id (nullable FK → billing_subscriptions.id)
  - composite index (tenant_id, subscription_id) for period lookup

Paid merchants are keyed by subscription_id so each new billing period
starts a fresh usage counter. Trial / no-sub tenants keep calendar month rows
(subscription_id IS NULL).
"""
from __future__ import annotations

import sqlalchemy as sa
from alembic import op


revision = "0082"
down_revision = "0081"
branch_labels = None
depends_on = None


def upgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    cols = {c["name"] for c in inspector.get_columns("whatsapp_usage")}

    if "subscription_id" not in cols:
        op.add_column(
            "whatsapp_usage",
            sa.Column(
                "subscription_id",
                sa.Integer(),
                sa.ForeignKey("billing_subscriptions.id"),
                nullable=True,
            ),
        )

    existing_indexes = {idx["name"] for idx in inspector.get_indexes("whatsapp_usage")}
    if "ix_whatsapp_usage_tenant_subscription" not in existing_indexes:
        op.create_index(
            "ix_whatsapp_usage_tenant_subscription",
            "whatsapp_usage",
            ["tenant_id", "subscription_id"],
            unique=False,
        )


def downgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    existing_indexes = {idx["name"] for idx in inspector.get_indexes("whatsapp_usage")}
    if "ix_whatsapp_usage_tenant_subscription" in existing_indexes:
        op.drop_index("ix_whatsapp_usage_tenant_subscription", table_name="whatsapp_usage")

    cols = {c["name"] for c in inspector.get_columns("whatsapp_usage")}
    if "subscription_id" in cols:
        op.drop_column("whatsapp_usage", "subscription_id")

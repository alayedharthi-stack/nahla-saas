"""Add is_platform_tenant flag to tenants.

Revision ID: 0030
Revises: 0029
Create Date: 2026-04-18

Why
───
Until today the WhatsApp inbound router decided "is this message for the
Nahla platform sales bot, or for a merchant's own store AI?" by hard-
coding `PLATFORM_TENANT_ID = 1`. In production tenant_id=1 happens to be
a real merchant store ("متجر رقم 1"), so any number that connected
under tenant_id=1 was wrongly routed to the platform sales bot — the
merchant's customers received "سجّل في نحلة" CTAs instead of the
store's AI replies, and the store-AI / store-knowledge / store-context
pipeline was bypassed entirely.

This migration introduces an explicit `is_platform_tenant` boolean on
the `tenants` table so the platform-vs-merchant distinction lives in
the data, not in code constants. Every existing tenant defaults to
`False` (= merchant), which immediately fixes the symptom: the next
inbound message lands in `_handle_merchant_message()` and the store's
AI takes over.

A future Platform-Brain workspace can be enabled by setting the flag to
True on exactly one tenant (recommended via an admin-only operation,
not from the merchant UI).

Schema change
─────────────
  tenants.is_platform_tenant  BOOLEAN NOT NULL DEFAULT FALSE
  ix_tenants_is_platform_tenant   (partial helper index — small table)
"""
from alembic import op
import sqlalchemy as sa


revision = "0030"
down_revision = "0029"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "tenants",
        sa.Column(
            "is_platform_tenant",
            sa.Boolean(),
            nullable=False,
            server_default=sa.text("false"),
        ),
    )
    op.create_index(
        "ix_tenants_is_platform_tenant",
        "tenants",
        ["is_platform_tenant"],
    )


def downgrade() -> None:
    op.drop_index("ix_tenants_is_platform_tenant", table_name="tenants")
    op.drop_column("tenants", "is_platform_tenant")

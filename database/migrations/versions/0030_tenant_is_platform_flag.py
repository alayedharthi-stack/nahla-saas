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

Idempotency (F16)
─────────────────
Forward ORM / ``create_all`` drift may have pre-created
``tenants.is_platform_tenant`` while ``alembic_version`` lags behind 0030.
Inspector-guarded DDL adds only missing columns and indexes; clean
upgrades unchanged.
"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op
from sqlalchemy import inspect

revision = "0030"
down_revision = "0029"
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

    if not _has_column(bind, "tenants", "is_platform_tenant"):
        op.add_column(
            "tenants",
            sa.Column(
                "is_platform_tenant",
                sa.Boolean(),
                nullable=False,
                server_default=sa.text("false"),
            ),
        )
    if not _has_index(bind, "tenants", "ix_tenants_is_platform_tenant"):
        op.create_index(
            "ix_tenants_is_platform_tenant",
            "tenants",
            ["is_platform_tenant"],
        )


def downgrade() -> None:
    bind = op.get_bind()
    if _has_index(bind, "tenants", "ix_tenants_is_platform_tenant"):
        op.drop_index("ix_tenants_is_platform_tenant", table_name="tenants")
    if _has_column(bind, "tenants", "is_platform_tenant"):
        op.drop_column("tenants", "is_platform_tenant")

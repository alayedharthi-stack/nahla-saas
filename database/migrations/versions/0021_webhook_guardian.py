"""Add last_webhook_received_at and guardian audit log table.

Revision ID: 0021
Revises: 0020
Create Date: 2026-04-16

Idempotency (F16)
─────────────────
Startup safe-alters may already have created ``last_webhook_received_at`` and
``webhook_guardian_log`` while ``alembic_version`` lags behind 0021.
Inspector-guarded DDL adds only missing columns, tables, and indexes.
"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op
from sqlalchemy import inspect


revision: str = "0021"
down_revision: Union[str, None] = "0020"
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


def upgrade() -> None:
    bind = op.get_bind()

    # ── whatsapp_connections: activity tracking column ────────────────────────
    if not _has_column(bind, "whatsapp_connections", "last_webhook_received_at"):
        op.add_column(
            "whatsapp_connections",
            sa.Column("last_webhook_received_at", sa.DateTime(timezone=True), nullable=True),
        )

    # ── webhook_guardian_log: structured guardian audit history ───────────────
    if not _has_table(bind, "webhook_guardian_log"):
        op.create_table(
            "webhook_guardian_log",
            sa.Column("id", sa.Integer(), primary_key=True, nullable=False),
            sa.Column(
                "tenant_id",
                sa.Integer(),
                sa.ForeignKey("tenants.id", ondelete="CASCADE"),
                nullable=False,
                index=True,
            ),
            sa.Column("phone_number_id", sa.String(), nullable=True),
            sa.Column("waba_id", sa.String(), nullable=True),
            sa.Column("event", sa.String(), nullable=False),
            # webhook_subscribed | webhook_resubscribed | webhook_verification_failed
            # webhook_recovered   | webhook_stalled      | critical_error_detected
            sa.Column("success", sa.Boolean(), nullable=False, server_default="true"),
            sa.Column("detail", sa.Text(), nullable=True),
            sa.Column(
                "created_at",
                sa.DateTime(timezone=True),
                nullable=False,
                server_default=sa.func.now(),
            ),
        )
    if not _has_index(bind, "webhook_guardian_log", "ix_webhook_guardian_log_tenant_created"):
        op.create_index(
            "ix_webhook_guardian_log_tenant_created",
            "webhook_guardian_log",
            ["tenant_id", "created_at"],
        )
    if not _has_index(bind, "webhook_guardian_log", "ix_webhook_guardian_log_event"):
        op.create_index(
            "ix_webhook_guardian_log_event",
            "webhook_guardian_log",
            ["event"],
        )


def downgrade() -> None:
    bind = op.get_bind()
    if _has_index(bind, "webhook_guardian_log", "ix_webhook_guardian_log_event"):
        op.drop_index("ix_webhook_guardian_log_event", table_name="webhook_guardian_log")
    if _has_index(bind, "webhook_guardian_log", "ix_webhook_guardian_log_tenant_created"):
        op.drop_index(
            "ix_webhook_guardian_log_tenant_created",
            table_name="webhook_guardian_log",
        )
    if _has_table(bind, "webhook_guardian_log"):
        op.drop_table("webhook_guardian_log")
    if _has_column(bind, "whatsapp_connections", "last_webhook_received_at"):
        op.drop_column("whatsapp_connections", "last_webhook_received_at")

"""0063 — Conversation.last_payment_confirmed_at for the "طلبات مدفوعة" filter.

Revision ID: 0063
Revises:    0062

Why this migration exists
─────────────────────────
Merchants asked for a dedicated inbox filter that surfaces the
conversations where the customer has actually paid (uploaded a
receipt the platform recognised as ``payment_evidence_status="confirmed"``).
Until now this signal was buried in
``Conversation.extra_metadata['brain_state']['order_prep']``, which is
not indexable and not directly filterable from the conversations
listing query.

This migration adds a stable, indexable timestamp column
``last_payment_confirmed_at`` and an index on
``(tenant_id, last_payment_confirmed_at DESC NULLS LAST)`` so the
``filter=paid`` slug can stream rows in recency order without a
sequential scan.

Idempotency
───────────
Same pattern as 0058 / 0059 / 0061 / 0062: every column add + index
add is wrapped in an inspector check so re-running the migration on
a populated DB is a safe no-op.
"""
from __future__ import annotations

import sqlalchemy as sa
from alembic import op
from sqlalchemy import inspect

revision = "0063"
down_revision = "0062"
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

    if not _has_column(bind, "conversations", "last_payment_confirmed_at"):
        op.add_column(
            "conversations",
            sa.Column(
                "last_payment_confirmed_at",
                sa.DateTime(timezone=True),
                nullable=True,
            ),
        )

    if not _has_index(
        bind, "conversations", "ix_conversations_paid_filter",
    ):
        op.create_index(
            "ix_conversations_paid_filter",
            "conversations",
            ["tenant_id", "last_payment_confirmed_at"],
            unique=False,
        )


def downgrade() -> None:
    bind = op.get_bind()
    if _has_index(bind, "conversations", "ix_conversations_paid_filter"):
        op.drop_index("ix_conversations_paid_filter", "conversations")
    if _has_column(bind, "conversations", "last_payment_confirmed_at"):
        op.drop_column("conversations", "last_payment_confirmed_at")

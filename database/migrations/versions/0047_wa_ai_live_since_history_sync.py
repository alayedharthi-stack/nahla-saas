"""0047 — WhatsApp AI live cutoff + history sync bookkeeping.

Adds per-connection columns used to ensure conversational AI never
responds to inbound messages whose WhatsApp business timestamp is
strictly *before* `whatsapp_ai_live_since` (set once at first successful
connection / activation and only moved forward via explicit admin
reset).

Also adds optional history-sync phase counters/status for future bulk
import — defaults keep current behaviour (`completed`).

Backfill (connected rows only, NULL cutoff): set cutoff to migration
time (UTC) so delayed delivery of old history does not trigger AI.

Revision ID: 0047
Revises: 0046

Idempotency (F16)
─────────────────
Guarded by inspector checks — safe when forward-ORM drift pre-created
columns while ``alembic_version`` is still at 0046. Backfill runs only
when ``whatsapp_ai_live_since`` exists.
"""
from __future__ import annotations

import sqlalchemy as sa
from alembic import op

from migration_inspector_helpers import has_column

revision = "0047"
down_revision = "0046"
branch_labels = None
depends_on = None


def upgrade() -> None:
    bind = op.get_bind()

    for column_name, column in (
        (
            "whatsapp_ai_live_since",
            sa.Column("whatsapp_ai_live_since", sa.DateTime(timezone=True), nullable=True),
        ),
        (
            "whatsapp_history_sync_status",
            sa.Column(
                "whatsapp_history_sync_status",
                sa.String(),
                nullable=False,
                server_default="completed",
            ),
        ),
        ("history_sync_started_at", sa.Column("history_sync_started_at", sa.DateTime(timezone=True), nullable=True)),
        ("history_sync_completed_at", sa.Column("history_sync_completed_at", sa.DateTime(timezone=True), nullable=True)),
        (
            "synced_conversations_count",
            sa.Column("synced_conversations_count", sa.Integer(), nullable=False, server_default="0"),
        ),
        (
            "synced_messages_count",
            sa.Column("synced_messages_count", sa.Integer(), nullable=False, server_default="0"),
        ),
    ):
        if not has_column(bind, "whatsapp_connections", column_name):
            op.add_column("whatsapp_connections", column)

    if has_column(bind, "whatsapp_connections", "whatsapp_ai_live_since"):
        op.execute(
            """
            UPDATE whatsapp_connections
            SET whatsapp_ai_live_since = NOW() AT TIME ZONE 'utc'
            WHERE status = 'connected'
              AND whatsapp_ai_live_since IS NULL
            """
        )


def downgrade() -> None:
    op.drop_column("whatsapp_connections", "synced_messages_count")
    op.drop_column("whatsapp_connections", "synced_conversations_count")
    op.drop_column("whatsapp_connections", "history_sync_completed_at")
    op.drop_column("whatsapp_connections", "history_sync_started_at")
    op.drop_column("whatsapp_connections", "whatsapp_history_sync_status")
    op.drop_column("whatsapp_connections", "whatsapp_ai_live_since")

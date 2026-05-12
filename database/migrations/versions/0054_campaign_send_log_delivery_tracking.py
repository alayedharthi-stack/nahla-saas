"""0054 — campaign_send_logs: per-recipient delivery & read tracking.

Revision ID: 0054
Revises:    0053

Why this migration exists
─────────────────────────
Today the only "did the customer receive my campaign?" signal we
persist is the aggregate ``Campaign.delivered_count`` /
``Campaign.read_count`` integer counters, incremented by the
WhatsApp status webhook (`_handle_message_status` in
`backend/routers/whatsapp_webhook.py`).

That's not enough for the merchant-facing debug panel: we cannot
tell which SPECIFIC recipient received vs read vs failed-after-
accept, only how many in aggregate. When a merchant says "the
campaign shows 4 sent but my friend didn't get it", support has
no row-level audit trail.

This migration adds three nullable timestamps to
``campaign_send_logs`` so we can:

  * Distinguish "Meta accepted" (sent_at, provider_message_id set)
    from "delivered to customer" (delivered_at) from "read by
    customer" (read_at).
  * Detect "failed-after-accept" — Meta accepted then sent a
    failure status afterwards (failed_at populated while
    provider_message_id is also set).
  * Build a `delivery_summary` aggregation in the debug endpoint
    by counting rows where each timestamp is set.

Backwards compatibility:
  * All three columns are NULLABLE — every existing row stays valid
    with all three NULL ("unknown delivery").
  * No status-string changes; we don't introduce a "delivered"
    status because the row's lifecycle is "did we send it from
    OUR side?" — delivery is a parallel post-hoc dimension.

Indexes:
  * ``ix_campaign_send_log_provider_message_id`` (NEW) — the
    status webhook looks up by `provider_message_id` to attribute
    the receipt to the right row. Without this index the lookup
    is a full scan per webhook event, which becomes pathological
    once a single campaign exceeds a few thousand recipients.

Idempotency (F16)
─────────────────
Every ``add_column`` and ``create_index`` is guarded by an
inspector check so re-running on a DB where the columns or index
already exist is a no-op rather than a DuplicateColumn /
DuplicateRelation error.
"""
from __future__ import annotations

import sqlalchemy as sa
from alembic import op
from sqlalchemy import inspect

revision = "0054"
down_revision = "0053"
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

    if not _has_table(bind, "campaign_send_logs"):
        # Should never happen — 0051 created it. But guarding makes
        # ``alembic upgrade 0054`` from a wedged state safe.
        return

    # ``batch_alter_table`` is needed for SQLite (test envs) so that
    # the ALTER TABLE statements are emitted via the recreate-and-copy
    # workaround. On Postgres each ADD COLUMN is a fast metadata-only
    # operation. We still guard each column individually so a partial
    # drift state (one column already exists, the other two don't)
    # is handled correctly.
    with op.batch_alter_table("campaign_send_logs") as batch:
        if not _has_column(bind, "campaign_send_logs", "delivered_at"):
            batch.add_column(sa.Column("delivered_at", sa.DateTime(), nullable=True))
        if not _has_column(bind, "campaign_send_logs", "read_at"):
            batch.add_column(sa.Column("read_at", sa.DateTime(), nullable=True))
        if not _has_column(bind, "campaign_send_logs", "failed_at"):
            batch.add_column(sa.Column("failed_at", sa.DateTime(), nullable=True))

    # The status webhook joins by provider_message_id; without this
    # index, every incoming WhatsApp status event triggers a full
    # table scan. Campaigns can hit ~50k recipients per blast.
    if not _has_index(
        bind, "campaign_send_logs",
        "ix_campaign_send_log_provider_message_id",
    ):
        op.create_index(
            "ix_campaign_send_log_provider_message_id",
            "campaign_send_logs",
            ["provider_message_id"],
        )


def downgrade() -> None:
    op.drop_index(
        "ix_campaign_send_log_provider_message_id",
        table_name="campaign_send_logs",
    )
    with op.batch_alter_table("campaign_send_logs") as batch:
        batch.drop_column("failed_at")
        batch.drop_column("read_at")
        batch.drop_column("delivered_at")

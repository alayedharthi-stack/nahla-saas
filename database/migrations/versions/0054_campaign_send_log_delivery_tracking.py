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
"""
from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0054"
down_revision = "0053"
branch_labels = None
depends_on = None


def upgrade() -> None:
    with op.batch_alter_table("campaign_send_logs") as batch:
        batch.add_column(sa.Column("delivered_at", sa.DateTime(), nullable=True))
        batch.add_column(sa.Column("read_at",      sa.DateTime(), nullable=True))
        batch.add_column(sa.Column("failed_at",    sa.DateTime(), nullable=True))

    # The status webhook joins by provider_message_id; without this
    # index, every incoming WhatsApp status event triggers a full
    # table scan. Campaigns can hit ~50k recipients per blast.
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

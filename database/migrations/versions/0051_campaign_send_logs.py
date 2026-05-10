"""0051 — campaign_send_logs: per-recipient idempotency log for manual marketing campaigns.

Revision ID: 0051
Revises: 0050

Why this table exists
─────────────────────
Manual marketing campaigns (broadcast / promotion / custom etc. — NOT
cart-recovery, order messages, or 24h-service replies) used to keep no
durable per-recipient state. If a campaign crashed mid-dispatch and the
merchant clicked "Run again", the dispatcher iterated the same audience
from scratch and could re-send the same message to customers who had
already received it. That is a reputation risk on Meta's side and an
annoying UX for the customer.

Schema contract:
  * One row per (campaign, recipient phone) — guaranteed by the
    UNIQUE(tenant_id, campaign_id, customer_phone_e164) constraint.
  * Frequency-cap queries hit
    ix_campaign_send_log_tenant_phone_status_sent.
  * Idempotency: a row in `status='sent'` is NEVER re-sent except via
    the explicit admin-only "Force resend" path, which writes a new
    Campaign row instead of mutating sent rows.

Statuses (string column, kept open for future values):
  queued                — snapshot row, not yet attempted
  sending               — provider call in flight
  sent                  — provider returned a wamid
  failed                — provider error or transient failure (retryable)
  skipped_duplicate     — frequency cap hit (sent_at within last N days)
  skipped_invalid       — phone empty / unparseable / not normalizable
  skipped_unsubscribed  — customer opted out before send
  skipped_unreachable   — customer has no normalized_phone after audit
"""
from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0051"
down_revision = "0050"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "campaign_send_logs",
        # BigInteger on Postgres so the table can grow indefinitely
        # without overflowing INT4. SQLite (test only) maps it back to
        # plain INTEGER which aliases ROWID for autoincrement.
        sa.Column("id", sa.BigInteger().with_variant(sa.Integer(), "sqlite"), primary_key=True, autoincrement=True),
        sa.Column("tenant_id",           sa.Integer(),     sa.ForeignKey("tenants.id"), nullable=False),
        sa.Column("campaign_id",         sa.Integer(),     sa.ForeignKey("campaigns.id", ondelete="CASCADE"), nullable=False),
        sa.Column("customer_id",         sa.Integer(),     sa.ForeignKey("customers.id"), nullable=True),
        sa.Column("customer_phone_e164", sa.String(),      nullable=False),
        sa.Column("template_name",       sa.String(),      nullable=True),
        sa.Column("template_language",   sa.String(),      nullable=True),
        sa.Column("payload_hash",        sa.String(length=64), nullable=True),
        sa.Column("status",              sa.String(length=32), nullable=False, server_default="queued"),
        sa.Column("provider_message_id", sa.String(),      nullable=True),
        sa.Column("error_code",          sa.String(),      nullable=True),
        sa.Column("error_message",       sa.Text(),        nullable=True),
        sa.Column("skip_reason",         sa.String(length=64), nullable=True),
        sa.Column("attempt_count",       sa.Integer(),     nullable=False, server_default="0"),
        sa.Column("sent_at",             sa.DateTime(),    nullable=True),
        sa.Column("created_at",          sa.DateTime(),    nullable=False, server_default=sa.func.now()),
        sa.Column("updated_at",          sa.DateTime(),    nullable=False, server_default=sa.func.now()),
    )

    # Idempotency: a single (campaign, recipient phone) tuple may only
    # appear once. Combined with the per-row status, this is what makes
    # "click Run twice in a row" safe — the second snapshot insert is a
    # no-op for already-recorded recipients.
    op.create_index(
        "uq_campaign_send_log_campaign_phone",
        "campaign_send_logs",
        ["tenant_id", "campaign_id", "customer_phone_e164"],
        unique=True,
    )

    # Frequency-cap query: "did this phone receive a sent marketing
    # campaign for this tenant within the last N days?". Pre-filters by
    # status so the planner skips queued/failed rows.
    op.create_index(
        "ix_campaign_send_log_tenant_phone_status_sent",
        "campaign_send_logs",
        ["tenant_id", "customer_phone_e164", "status", "sent_at"],
    )

    # Status counter aggregations for the report endpoint.
    op.create_index(
        "ix_campaign_send_log_campaign_status",
        "campaign_send_logs",
        ["campaign_id", "status"],
    )


def downgrade() -> None:
    op.drop_index("ix_campaign_send_log_campaign_status", table_name="campaign_send_logs")
    op.drop_index("ix_campaign_send_log_tenant_phone_status_sent", table_name="campaign_send_logs")
    op.drop_index("uq_campaign_send_log_campaign_phone", table_name="campaign_send_logs")
    op.drop_table("campaign_send_logs")

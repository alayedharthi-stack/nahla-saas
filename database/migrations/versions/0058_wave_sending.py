"""0058 — Wave/Batch sending architecture.

Revision ID: 0058
Revises:    0057

Why this migration exists
─────────────────────────
The legacy dispatcher paces sends inside a single process loop
(``MARKETING_CAMPAIGN_BATCH_SIZE`` + ``INTER_MESSAGE_DELAY``).
That worked when the platform was sending to thousands at a time
in one shot, but Meta's published guidance for marketing
broadcasts explicitly calls for staggered, observable batches —
both to protect WABA reputation and to give the merchant a place
to pause / resume.

We elevate "batch" from an in-process variable to a first-class
persisted concept:

  1. ``campaign_waves``
     One row per scheduled wave of a campaign. Carries
     ``scheduled_at``, ``planned_recipients``, status (pending /
     dispatching / completed / failed / paused / cancelled), and
     denormalised ``total_waves`` so the UI can render
     "wave 2 of 8" without a count query.

  2. ``campaigns.send_strategy`` (+ ``batch_size`` +
     ``delay_between_batches_sec``)
     The merchant-visible choice: immediate (legacy), batched
     (explicit plan), or adaptive (Nahla computes from quality).
     Default ``immediate`` preserves existing behaviour for every
     campaign already in flight at migration time.

  3. ``campaign_send_logs.wave_id``
     Nullable FK back to ``campaign_waves``. NULL = legacy
     immediate path; populated = the wave scheduler dispatches
     this row when its wave becomes due. ``ON DELETE SET NULL``
     so cancelling a wave doesn't drop the idempotency anchor
     on the send_log row.

Idempotency
───────────
Every ``create_table`` / ``create_index`` / ``add_column`` is
guarded by an inspector check so re-running the migration on a
DB where any of these objects already exist is a no-op — same
convention as 0057.
"""
from __future__ import annotations

import sqlalchemy as sa
from alembic import op
from sqlalchemy import inspect

revision = "0058"
down_revision = "0057"
branch_labels = None
depends_on = None


# ──────────────────────────────────────────────────────────────────
# Helpers (mirror 0057 style for consistency)
# ──────────────────────────────────────────────────────────────────


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


# ──────────────────────────────────────────────────────────────────
# upgrade
# ──────────────────────────────────────────────────────────────────


def upgrade() -> None:
    bind = op.get_bind()

    # ── 1. campaign_waves ──────────────────────────────────────────
    if not _has_table(bind, "campaign_waves"):
        op.create_table(
            "campaign_waves",
            sa.Column("id", sa.Integer(), primary_key=True),
            sa.Column(
                "campaign_id", sa.Integer(),
                sa.ForeignKey("campaigns.id", ondelete="CASCADE"),
                nullable=False,
            ),
            sa.Column(
                "tenant_id", sa.Integer(),
                sa.ForeignKey("tenants.id"),
                nullable=False,
            ),
            sa.Column("wave_index", sa.Integer(), nullable=False),
            sa.Column("total_waves", sa.Integer(), nullable=False),
            sa.Column(
                "status", sa.String(length=24),
                server_default=sa.text("'pending'"), nullable=False,
            ),
            sa.Column(
                "scheduled_at", sa.DateTime(timezone=True),
                server_default=sa.text("CURRENT_TIMESTAMP"),
                nullable=False,
            ),
            sa.Column("started_at",   sa.DateTime(timezone=True), nullable=True),
            sa.Column("completed_at", sa.DateTime(timezone=True), nullable=True),
            sa.Column(
                "planned_recipients", sa.Integer(),
                server_default=sa.text("0"), nullable=False,
            ),
            sa.Column(
                "sent_count", sa.Integer(),
                server_default=sa.text("0"), nullable=False,
            ),
            sa.Column(
                "failed_count", sa.Integer(),
                server_default=sa.text("0"), nullable=False,
            ),
            sa.Column("plan_strategy",  sa.String(length=24), nullable=True),
            sa.Column("plan_rationale", sa.Text(),            nullable=True),
            sa.Column(
                "created_at", sa.DateTime(timezone=True),
                server_default=sa.text("CURRENT_TIMESTAMP"),
                nullable=False,
            ),
            sa.Column(
                "updated_at", sa.DateTime(timezone=True),
                server_default=sa.text("CURRENT_TIMESTAMP"),
                nullable=False,
            ),
            sa.UniqueConstraint(
                "campaign_id", "wave_index",
                name="uq_campaign_waves_campaign_index",
            ),
        )

    # Indexes for the wave scheduler's two hot paths:
    #   * "give me the next due wave" — (status, scheduled_at)
    #   * "give me wave N of this campaign" — (campaign_id, wave_index)
    for ix_name, cols in (
        ("ix_campaign_waves_due",      ["status", "scheduled_at"]),
        ("ix_campaign_waves_campaign", ["campaign_id", "wave_index"]),
    ):
        if not _has_index(bind, "campaign_waves", ix_name):
            op.create_index(ix_name, "campaign_waves", cols)

    # ── 2. campaigns: send_strategy + batch_size + delay ───────────
    # Default ``immediate`` so every existing campaign keeps its
    # historic behaviour. nullable=False with server_default is the
    # safe pattern on PostgreSQL for adding NOT NULL columns to a
    # populated table.
    if not _has_column(bind, "campaigns", "send_strategy"):
        op.add_column(
            "campaigns",
            sa.Column(
                "send_strategy", sa.String(length=24),
                server_default=sa.text("'immediate'"),
                nullable=False,
            ),
        )
    if not _has_column(bind, "campaigns", "batch_size"):
        op.add_column(
            "campaigns",
            sa.Column("batch_size", sa.Integer(), nullable=True),
        )
    if not _has_column(bind, "campaigns", "delay_between_batches_sec"):
        op.add_column(
            "campaigns",
            sa.Column(
                "delay_between_batches_sec", sa.Integer(), nullable=True,
            ),
        )

    # ── 3. campaign_send_logs.wave_id ──────────────────────────────
    # ``ON DELETE SET NULL`` — cancelling a wave must not drop the
    # idempotency anchor on the send_log rows that belonged to it.
    if not _has_column(bind, "campaign_send_logs", "wave_id"):
        op.add_column(
            "campaign_send_logs",
            sa.Column(
                "wave_id", sa.Integer(),
                sa.ForeignKey("campaign_waves.id", ondelete="SET NULL"),
                nullable=True,
            ),
        )
    if not _has_index(bind, "campaign_send_logs", "ix_campaign_send_log_wave_status"):
        op.create_index(
            "ix_campaign_send_log_wave_status",
            "campaign_send_logs",
            ["wave_id", "status"],
        )


# ──────────────────────────────────────────────────────────────────
# downgrade
# ──────────────────────────────────────────────────────────────────


def downgrade() -> None:
    bind = op.get_bind()

    if _has_index(bind, "campaign_send_logs", "ix_campaign_send_log_wave_status"):
        op.drop_index("ix_campaign_send_log_wave_status", "campaign_send_logs")
    if _has_column(bind, "campaign_send_logs", "wave_id"):
        op.drop_column("campaign_send_logs", "wave_id")

    for col in ("delay_between_batches_sec", "batch_size", "send_strategy"):
        if _has_column(bind, "campaigns", col):
            op.drop_column("campaigns", col)

    for ix_name in ("ix_campaign_waves_due", "ix_campaign_waves_campaign"):
        if _has_index(bind, "campaign_waves", ix_name):
            op.drop_index(ix_name, "campaign_waves")
    if _has_table(bind, "campaign_waves"):
        op.drop_table("campaign_waves")

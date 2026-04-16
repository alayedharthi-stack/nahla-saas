"""Durable webhook event log + orders deduplication constraint.

Revision ID: 0023
Revises: 0022
Create Date: 2026-04-16

Changes:
  1. webhook_events — append-only durable queue for every inbound webhook.
     Every provider (salla, zid, whatsapp, moyasar, ...) now persists the raw
     payload BEFORE any business processing. An async dispatcher claims rows,
     processes them, and records the outcome on the same row. Failures retry
     with exponential backoff, then land in status=dead_letter for admin replay.

  2. orders unique(tenant_id, external_id) — enforces at the DB level what
     the application currently only checks in Python, preventing double-insert
     races when two concurrent workers process the same Salla order id.
     Any pre-existing duplicates are merged before the constraint is added.
"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op


revision: str = "0023"
down_revision: Union[str, None] = "0022"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    bind = op.get_bind()

    # ── 1. webhook_events: durable event log ──────────────────────────────────
    op.create_table(
        "webhook_events",
        sa.Column("id", sa.Integer(), primary_key=True, nullable=False),
        # tenant_id is nullable: some events (e.g. app.installed) arrive before
        # tenant mapping exists. The dispatcher resolves it later.
        sa.Column("tenant_id", sa.Integer(), nullable=True, index=True),
        sa.Column("provider", sa.String(), nullable=False, index=True),
        # event_type examples: order.created, order.updated, customer.created,
        # app.installed, app.uninstalled, product.created, ...
        sa.Column("event_type", sa.String(), nullable=True, index=True),
        sa.Column("external_event_id", sa.String(), nullable=True),
        sa.Column("store_id", sa.String(), nullable=True),
        sa.Column("raw_headers", sa.JSON(), nullable=True),
        sa.Column("raw_body", sa.Text(), nullable=True),
        sa.Column("parsed_payload", sa.JSON(), nullable=True),
        sa.Column("signature_valid", sa.Boolean(), nullable=True),
        # FSM: received → processing → processed
        #                          → failed → processing (retry)
        #                                  → dead_letter
        sa.Column(
            "status", sa.String(), nullable=False, server_default="received", index=True,
        ),
        sa.Column("attempts", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("last_error", sa.Text(), nullable=True),
        sa.Column("last_error_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("next_retry_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column(
            "received_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
        sa.Column("processed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
    )
    op.create_index(
        "ix_webhook_events_status_retry",
        "webhook_events",
        ["status", "next_retry_at"],
    )
    op.create_index(
        "ix_webhook_events_tenant_received",
        "webhook_events",
        ["tenant_id", "received_at"],
    )
    # Partial unique: one row per (provider, external_event_id) when the
    # provider includes an event id in the payload. Prevents the same webhook
    # from being double-processed if Salla ever retries without our idempotency.
    bind.execute(
        sa.text(
            """
            CREATE UNIQUE INDEX IF NOT EXISTS uq_webhook_events_provider_event
            ON webhook_events (provider, external_event_id)
            WHERE external_event_id IS NOT NULL
            """
        )
    )

    # ── 2. orders: dedupe + unique(tenant_id, external_id) ────────────────────
    # 2a. Find duplicates and keep only the row with the highest id (most recent).
    dup_rows = list(
        bind.execute(
            sa.text(
                """
                SELECT tenant_id, external_id, COUNT(*) AS cnt
                FROM orders
                WHERE external_id IS NOT NULL AND external_id != ''
                GROUP BY tenant_id, external_id
                HAVING COUNT(*) > 1
                """
            )
        )
    )
    if dup_rows:
        import logging
        mig_logger = logging.getLogger("alembic.migration.0023")
        for row in dup_rows:
            mig_logger.warning(
                "0023: Duplicate orders detected — tenant=%s external_id=%s count=%s. "
                "Keeping MAX(id), deleting older duplicates.",
                row.tenant_id, row.external_id, row.cnt,
            )
            bind.execute(
                sa.text(
                    """
                    DELETE FROM orders
                    WHERE tenant_id = :tid
                      AND external_id = :eid
                      AND id NOT IN (
                          SELECT MAX(id) FROM orders
                          WHERE tenant_id = :tid AND external_id = :eid
                      )
                    """
                ),
                {"tid": row.tenant_id, "eid": row.external_id},
            )

    # 2b. Add the constraint (partial — ignore NULL/empty external_id rows).
    bind.execute(
        sa.text(
            """
            CREATE UNIQUE INDEX IF NOT EXISTS uq_orders_tenant_external_id
            ON orders (tenant_id, external_id)
            WHERE external_id IS NOT NULL AND external_id != ''
            """
        )
    )


def downgrade() -> None:
    op.execute("DROP INDEX IF EXISTS uq_orders_tenant_external_id")
    op.execute("DROP INDEX IF EXISTS uq_webhook_events_provider_event")
    op.drop_index(
        "ix_webhook_events_tenant_received", table_name="webhook_events"
    )
    op.drop_index(
        "ix_webhook_events_status_retry", table_name="webhook_events"
    )
    op.drop_table("webhook_events")

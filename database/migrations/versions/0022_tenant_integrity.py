"""Tenant integrity enforcement: unique waba_id index + integrity_events table.

Revision ID: 0022
Revises: 0021
Create Date: 2026-04-16

Changes:
  1. Partial unique index on whatsapp_connections(whatsapp_business_account_id)
     for non-null values — one WABA per tenant, no cross-tenant sharing.
  2. integrity_events table — append-only structured audit trail for all
     identity-resolution and cross-tenant conflict events.
"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op


revision: str = "0022"
down_revision: Union[str, None] = "0021"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    bind = op.get_bind()

    # ── 1. Detect and report duplicate WABA IDs before adding the constraint ──
    dup_wabas = list(
        bind.execute(
            sa.text(
                """
                SELECT whatsapp_business_account_id, COUNT(*) AS cnt,
                       array_agg(tenant_id) AS tenant_ids
                FROM whatsapp_connections
                WHERE whatsapp_business_account_id IS NOT NULL
                GROUP BY whatsapp_business_account_id
                HAVING COUNT(*) > 1
                """
            )
        )
    )
    if dup_wabas:
        import logging
        logger = logging.getLogger("alembic.migration.0022")
        for row in dup_wabas:
            logger.warning(
                "0022: Duplicate WABA ID detected before constraint — "
                "waba_id=%s tenants=%s. Nulling out the older row(s).",
                row.whatsapp_business_account_id,
                row.tenant_ids,
            )
            # Keep the connection with the highest tenant_id (most recently onboarded);
            # set the others to NULL so the constraint can be applied safely.
            max_tenant = max(row.tenant_ids)
            bind.execute(
                sa.text(
                    """
                    UPDATE whatsapp_connections
                    SET whatsapp_business_account_id = NULL,
                        status = 'disconnected',
                        sending_enabled = false,
                        webhook_verified = false
                    WHERE whatsapp_business_account_id = :wid
                      AND tenant_id != :keep
                    """
                ),
                {"wid": row.whatsapp_business_account_id, "keep": max_tenant},
            )

    # ── 2. Partial unique index: one WABA ID per active connection ─────────────
    bind.execute(
        sa.text(
            """
            DO $$ BEGIN
                IF NOT EXISTS (
                    SELECT 1 FROM pg_indexes
                    WHERE tablename = 'whatsapp_connections'
                    AND indexname = 'uq_wa_conn_waba_id'
                ) THEN
                    CREATE UNIQUE INDEX uq_wa_conn_waba_id
                    ON whatsapp_connections (whatsapp_business_account_id)
                    WHERE whatsapp_business_account_id IS NOT NULL;
                END IF;
            END $$
            """
        )
    )

    # ── 3. integrity_events: structured identity/cross-tenant event log ────────
    op.create_table(
        "integrity_events",
        sa.Column("id", sa.Integer(), primary_key=True, nullable=False),
        sa.Column("event", sa.String(), nullable=False, index=True),
        # event types:
        #   tenant_resolved          – normal routing, phone_number_id → tenant
        #   duplicate_identity       – same phone/waba/store_id on >1 tenant
        #   cross_tenant_conflict    – WA conn and store on different tenants
        #   write_blocked            – write rejected by integrity guard
        #   reconciliation_started   – merge workflow initiated (dry_run or live)
        #   reconciliation_completed – merge workflow finished
        #   orphaned_wa_connection   – WA conn has no store integration
        #   orphaned_store           – store integration has no WA conn
        sa.Column("tenant_id", sa.Integer(), nullable=True, index=True),
        sa.Column("other_tenant_id", sa.Integer(), nullable=True),
        sa.Column("phone_number_id", sa.String(), nullable=True),
        sa.Column("waba_id", sa.String(), nullable=True),
        sa.Column("store_id", sa.String(), nullable=True),
        sa.Column("provider", sa.String(), nullable=True),
        sa.Column("action", sa.String(), nullable=True),
        sa.Column("result", sa.String(), nullable=True),    # ok | blocked | conflict | fixed
        sa.Column("detail", sa.Text(), nullable=True),
        sa.Column("actor", sa.String(), nullable=True),     # system | admin:<email>
        sa.Column("dry_run", sa.Boolean(), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
    )
    op.create_index(
        "ix_integrity_events_created_at",
        "integrity_events",
        ["created_at"],
    )


def downgrade() -> None:
    op.drop_index("ix_integrity_events_created_at", table_name="integrity_events")
    op.drop_table("integrity_events")
    op.execute(
        "DROP INDEX IF EXISTS uq_wa_conn_waba_id"
    )

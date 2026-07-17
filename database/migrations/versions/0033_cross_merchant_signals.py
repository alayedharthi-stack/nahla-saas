"""
Cross-merchant anonymized learning signals.

Revision ID: 0033
Revises: 0032
Create Date: 2026-04-19

WHY
───
Phase 3 of the Sales Brain architecture introduces cross-merchant
learning that powers a global policy layer + a per-vertical adaptation
layer.  These layers must NEVER read raw store data; they consume only
anonymized categorical signals (hashed tenant id, industry tag, intent,
action, outcome, value bucket, …).

This migration creates ``cross_merchant_signals`` — a deliberately
non-tenant-scoped table — together with the indexes required for
distribution / outcome aggregation queries.

CHANGES
───────
+ table cross_merchant_signals
    id            SERIAL PRIMARY KEY
    tenant_hash   VARCHAR(64)  NOT NULL  -- salted SHA-256 truncation
    industry      VARCHAR(64)  NOT NULL  DEFAULT 'unknown'
    intent        VARCHAR(64)  NOT NULL  DEFAULT 'unknown'
    action        VARCHAR(64)  NOT NULL  DEFAULT 'unknown'
    ui_mode       VARCHAR(32)  NOT NULL  DEFAULT 'unknown'
    outcome       VARCHAR(32)  NOT NULL  DEFAULT 'unknown'
    value_bucket  VARCHAR(32)  NOT NULL  DEFAULT 'unknown'
    turn_index    INTEGER      NOT NULL  DEFAULT 0
    model_path    VARCHAR(32)  NOT NULL  DEFAULT 'rule'
    latency_ms    INTEGER      NOT NULL  DEFAULT 0
    tier          VARCHAR(16)  NOT NULL  DEFAULT 'global'
    extra         JSONB        NULL
    created_at    TIMESTAMPTZ  NOT NULL  DEFAULT now()

+ indexes
    ix_xms_tenant_hash    on (tenant_hash)
    ix_xms_industry_action on (industry, action)
    ix_xms_action_outcome  on (action, outcome)
    ix_xms_tier_industry   on (tier, industry)
    ix_xms_created_at      on (created_at)

ANTI-LEAK GUARANTEES
────────────────────
* No FK to tenants — schema cannot be joined back to a real tenant
  without the runtime salt held by the application.
* No raw text / id / money columns — only categorical buckets.
* JSONB ``extra`` is filtered through ``sanitize_extra`` at write time
  so unknown keys are dropped before they reach the database.

Idempotency (F16)
─────────────────
Guarded by inspector checks — safe when ``Base.metadata.create_all()``
pre-created semantically equivalent objects while ``alembic_version`` is
still at 0032.
"""
from __future__ import annotations

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import JSONB

from migration_inspector_helpers import has_index, has_table

revision = "0033"
down_revision = "0032"
branch_labels = None
depends_on = None


def upgrade() -> None:
    bind = op.get_bind()

    if not has_table(bind, "cross_merchant_signals"):
        op.create_table(
            "cross_merchant_signals",
            sa.Column("id", sa.Integer(), primary_key=True),
            sa.Column("tenant_hash", sa.String(length=64), nullable=False),
            sa.Column("industry", sa.String(length=64), nullable=False, server_default="unknown"),
            sa.Column("intent", sa.String(length=64), nullable=False, server_default="unknown"),
            sa.Column("action", sa.String(length=64), nullable=False, server_default="unknown"),
            sa.Column("ui_mode", sa.String(length=32), nullable=False, server_default="unknown"),
            sa.Column("outcome", sa.String(length=32), nullable=False, server_default="unknown"),
            sa.Column("value_bucket", sa.String(length=32), nullable=False, server_default="unknown"),
            sa.Column("turn_index", sa.Integer(), nullable=False, server_default="0"),
            sa.Column("model_path", sa.String(length=32), nullable=False, server_default="rule"),
            sa.Column("latency_ms", sa.Integer(), nullable=False, server_default="0"),
            sa.Column("tier", sa.String(length=16), nullable=False, server_default="global"),
            sa.Column("extra", JSONB(), nullable=True),
            sa.Column(
                "created_at",
                sa.DateTime(timezone=True),
                server_default=sa.text("now()"),
                nullable=False,
            ),
        )

    for index_name, columns in (
        ("ix_cross_merchant_signals_tenant_hash", ("tenant_hash",)),
        ("ix_xms_industry_action", ("industry", "action")),
        ("ix_xms_action_outcome", ("action", "outcome")),
        ("ix_xms_tier_industry", ("tier", "industry")),
        ("ix_xms_created_at", ("created_at",)),
    ):
        if has_table(bind, "cross_merchant_signals") and not has_index(
            bind, "cross_merchant_signals", index_name,
        ):
            op.create_index(index_name, "cross_merchant_signals", list(columns))


def downgrade() -> None:
    op.drop_index("ix_xms_created_at", table_name="cross_merchant_signals")
    op.drop_index("ix_xms_tier_industry", table_name="cross_merchant_signals")
    op.drop_index("ix_xms_action_outcome", table_name="cross_merchant_signals")
    op.drop_index("ix_xms_industry_action", table_name="cross_merchant_signals")
    op.drop_index(
        "ix_cross_merchant_signals_tenant_hash",
        table_name="cross_merchant_signals",
    )
    op.drop_table("cross_merchant_signals")

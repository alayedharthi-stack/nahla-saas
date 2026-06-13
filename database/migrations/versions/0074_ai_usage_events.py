"""0074 — Persist per-LLM-call AI usage cost ledger rows.

Revision ID: 0074
Revises:    0073

One row per LLM call with token counts, cost breakdown, and attribution.
No message or prompt content is stored.
"""
from __future__ import annotations

import sqlalchemy as sa
from alembic import op


revision = "0074"
down_revision = "0073"
branch_labels = None
depends_on = None


def _has_table(bind, table: str) -> bool:
    insp = sa.inspect(bind)
    return table in insp.get_table_names()


def upgrade() -> None:
    bind = op.get_bind()
    if _has_table(bind, "ai_usage_events"):
        return

    op.create_table(
        "ai_usage_events",
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column("tenant_id", sa.Integer(), sa.ForeignKey("tenants.id"), nullable=True),
        sa.Column("store_id", sa.Integer(), nullable=True),
        sa.Column("conversation_id", sa.Integer(), nullable=True),
        sa.Column("turn_id", sa.Integer(), nullable=True),
        sa.Column("provider", sa.String(length=64), nullable=False),
        sa.Column("model", sa.String(length=128), nullable=False),
        sa.Column("reason", sa.String(length=128), nullable=False),
        sa.Column("input_tokens", sa.Integer(), nullable=True),
        sa.Column("output_tokens", sa.Integer(), nullable=True),
        sa.Column("cache_read_tokens", sa.Integer(), nullable=True),
        sa.Column("cache_write_tokens", sa.Integer(), nullable=True),
        sa.Column("estimated_input_tokens", sa.Integer(), nullable=True),
        sa.Column("estimated_output_tokens", sa.Integer(), nullable=True),
        sa.Column("token_source", sa.String(length=16), nullable=False),
        sa.Column("input_cost_usd", sa.Numeric(18, 8), nullable=True),
        sa.Column("output_cost_usd", sa.Numeric(18, 8), nullable=True),
        sa.Column("cache_cost_usd", sa.Numeric(18, 8), nullable=True),
        sa.Column("total_cost_usd", sa.Numeric(18, 8), nullable=True),
        sa.Column("pricing_version", sa.String(length=32), nullable=True),
        sa.Column("request_id", sa.String(length=128), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
    )
    op.create_index("ix_ai_usage_events_tenant_created", "ai_usage_events", ["tenant_id", "created_at"])
    op.create_index("ix_ai_usage_events_created_at", "ai_usage_events", ["created_at"])
    op.create_index("ix_ai_usage_events_provider", "ai_usage_events", ["provider"])
    op.create_index("ix_ai_usage_events_model", "ai_usage_events", ["model"])
    op.create_index("ix_ai_usage_events_reason", "ai_usage_events", ["reason"])
    op.create_index("ix_ai_usage_events_token_source", "ai_usage_events", ["token_source"])


def downgrade() -> None:
    bind = op.get_bind()
    if not _has_table(bind, "ai_usage_events"):
        return
    for idx in (
        "ix_ai_usage_events_token_source",
        "ix_ai_usage_events_reason",
        "ix_ai_usage_events_model",
        "ix_ai_usage_events_provider",
        "ix_ai_usage_events_created_at",
        "ix_ai_usage_events_tenant_created",
    ):
        try:
            op.drop_index(idx, table_name="ai_usage_events")
        except Exception:
            pass
    op.drop_table("ai_usage_events")

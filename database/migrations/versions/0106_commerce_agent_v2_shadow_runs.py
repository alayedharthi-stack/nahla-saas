"""Commerce Agent V2 shadow observations.

Revision ID: 0106
Revises: 0105
"""
from __future__ import annotations

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql


revision = "0106"
down_revision = "0105"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "commerce_agent_v2_shadow_runs",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("tenant_id", sa.Integer(), nullable=False),
        sa.Column("conversation_id", sa.Integer(), nullable=False),
        sa.Column("sdk_trace_id", sa.String(length=64), nullable=False),
        sa.Column("model", sa.String(length=128), nullable=False),
        sa.Column("status", sa.String(length=32), nullable=False),
        sa.Column("structured_output", postgresql.JSONB(), nullable=True),
        sa.Column("tool_trace", postgresql.JSONB(), nullable=True),
        sa.Column("guardrail_results", postgresql.JSONB(), nullable=True),
        sa.Column("latency_ms", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("input_tokens", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("output_tokens", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("total_tokens", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("failure_reason", sa.String(length=240), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
        sa.ForeignKeyConstraint(["tenant_id"], ["tenants.id"]),
        sa.ForeignKeyConstraint(["conversation_id"], ["conversations.id"]),
    )
    op.create_index(
        "ix_commerce_v2_shadow_tenant_created",
        "commerce_agent_v2_shadow_runs",
        ["tenant_id", "created_at"],
    )
    op.create_index(
        "ix_commerce_v2_shadow_conversation_created",
        "commerce_agent_v2_shadow_runs",
        ["conversation_id", "created_at"],
    )
    op.create_index(
        "ix_commerce_agent_v2_shadow_runs_sdk_trace_id",
        "commerce_agent_v2_shadow_runs",
        ["sdk_trace_id"],
    )


def downgrade() -> None:
    op.drop_index(
        "ix_commerce_agent_v2_shadow_runs_sdk_trace_id",
        table_name="commerce_agent_v2_shadow_runs",
    )
    op.drop_index(
        "ix_commerce_v2_shadow_conversation_created",
        table_name="commerce_agent_v2_shadow_runs",
    )
    op.drop_index(
        "ix_commerce_v2_shadow_tenant_created",
        table_name="commerce_agent_v2_shadow_runs",
    )
    op.drop_table("commerce_agent_v2_shadow_runs")

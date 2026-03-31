"""Add payment_sessions, handoff_sessions, system_events, conversation_traces.

Revision ID: 0008
Revises: 0007
Create Date: 2026-03-31
"""
from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import JSONB

revision = "0008"
down_revision = "0007"
branch_labels = None
depends_on = None


def upgrade():
    # ── payment_sessions ──────────────────────────────────────────────────────
    op.create_table(
        "payment_sessions",
        sa.Column("id",                 sa.Integer(),  primary_key=True),
        sa.Column("tenant_id",          sa.Integer(),  sa.ForeignKey("tenants.id"), nullable=False),
        sa.Column("order_id",           sa.Integer(),  sa.ForeignKey("orders.id"),  nullable=True),
        sa.Column("gateway",            sa.String(),   nullable=False, server_default="moyasar"),
        sa.Column("gateway_payment_id", sa.String(),   nullable=True),
        sa.Column("amount_sar",         sa.Float(),    nullable=False),
        sa.Column("currency",           sa.String(),   nullable=False, server_default="SAR"),
        sa.Column("status",             sa.String(),   nullable=False, server_default="pending"),
        sa.Column("payment_link",       sa.String(),   nullable=True),
        sa.Column("callback_data",      JSONB,         nullable=True),
        sa.Column("created_at",         sa.DateTime(), nullable=True),
        sa.Column("updated_at",         sa.DateTime(), nullable=True),
    )
    op.create_index("ix_payment_sessions_gateway_payment_id", "payment_sessions", ["gateway_payment_id"])
    op.create_index("ix_payment_sessions_tenant_id",          "payment_sessions", ["tenant_id"])

    # ── handoff_sessions ──────────────────────────────────────────────────────
    op.create_table(
        "handoff_sessions",
        sa.Column("id",                sa.Integer(),  primary_key=True),
        sa.Column("tenant_id",         sa.Integer(),  sa.ForeignKey("tenants.id"), nullable=False),
        sa.Column("customer_phone",    sa.String(),   nullable=False),
        sa.Column("customer_name",     sa.String(),   nullable=True),
        sa.Column("status",            sa.String(),   nullable=False, server_default="active"),
        sa.Column("handoff_reason",    sa.Text(),     nullable=True),
        sa.Column("last_message",      sa.Text(),     nullable=True),
        sa.Column("notification_sent", sa.Boolean(),  nullable=False, server_default="false"),
        sa.Column("resolved_by",       sa.String(),   nullable=True),
        sa.Column("resolved_at",       sa.DateTime(), nullable=True),
        sa.Column("context_snapshot",  JSONB,         nullable=True),
        sa.Column("created_at",        sa.DateTime(), nullable=True),
    )
    op.create_index("ix_handoff_sessions_tenant_id",      "handoff_sessions", ["tenant_id"])
    op.create_index("ix_handoff_sessions_customer_phone", "handoff_sessions", ["customer_phone"])
    op.create_index("ix_handoff_sessions_status",         "handoff_sessions", ["status"])

    # ── system_events ─────────────────────────────────────────────────────────
    op.create_table(
        "system_events",
        sa.Column("id",           sa.Integer(),  primary_key=True),
        sa.Column("tenant_id",    sa.Integer(),  sa.ForeignKey("tenants.id"), nullable=False),
        sa.Column("category",     sa.String(),   nullable=False),
        sa.Column("event_type",   sa.String(),   nullable=False),
        sa.Column("severity",     sa.String(),   nullable=False, server_default="info"),
        sa.Column("summary",      sa.String(),   nullable=True),
        sa.Column("payload",      JSONB,         nullable=True),
        sa.Column("reference_id", sa.String(),   nullable=True),
        sa.Column("created_at",   sa.DateTime(), nullable=True),
    )
    op.create_index("ix_system_events_tenant_id",    "system_events", ["tenant_id"])
    op.create_index("ix_system_events_category",     "system_events", ["category"])
    op.create_index("ix_system_events_reference_id", "system_events", ["reference_id"])
    op.create_index("ix_system_events_created_at",   "system_events", ["created_at"])

    # ── conversation_traces ───────────────────────────────────────────────────
    op.create_table(
        "conversation_traces",
        sa.Column("id",                  sa.Integer(),  primary_key=True),
        sa.Column("tenant_id",           sa.Integer(),  sa.ForeignKey("tenants.id"), nullable=False),
        sa.Column("customer_phone",      sa.String(),   nullable=False),
        sa.Column("session_id",          sa.String(),   nullable=True),
        sa.Column("turn",                sa.Integer(),  nullable=True, server_default="1"),
        sa.Column("message",             sa.Text(),     nullable=True),
        sa.Column("detected_intent",     sa.String(),   nullable=True),
        sa.Column("confidence",          sa.Float(),    nullable=True),
        sa.Column("response_type",       sa.String(),   nullable=True),
        sa.Column("orchestrator_used",   sa.Boolean(),  nullable=False, server_default="false"),
        sa.Column("model_used",          sa.String(),   nullable=True),
        sa.Column("fact_guard_modified", sa.Boolean(),  nullable=False, server_default="false"),
        sa.Column("fact_guard_claims",   JSONB,         nullable=True),
        sa.Column("actions_triggered",   JSONB,         nullable=True),
        sa.Column("response_text",       sa.Text(),     nullable=True),
        sa.Column("order_started",       sa.Boolean(),  nullable=False, server_default="false"),
        sa.Column("payment_link_sent",   sa.Boolean(),  nullable=False, server_default="false"),
        sa.Column("handoff_triggered",   sa.Boolean(),  nullable=False, server_default="false"),
        sa.Column("latency_ms",          sa.Integer(),  nullable=True),
        sa.Column("created_at",          sa.DateTime(), nullable=True),
    )
    op.create_index("ix_conversation_traces_tenant_id",      "conversation_traces", ["tenant_id"])
    op.create_index("ix_conversation_traces_customer_phone", "conversation_traces", ["customer_phone"])
    op.create_index("ix_conversation_traces_session_id",     "conversation_traces", ["session_id"])


def downgrade():
    op.drop_index("ix_conversation_traces_session_id",     table_name="conversation_traces")
    op.drop_index("ix_conversation_traces_customer_phone", table_name="conversation_traces")
    op.drop_index("ix_conversation_traces_tenant_id",      table_name="conversation_traces")
    op.drop_table("conversation_traces")

    op.drop_index("ix_system_events_created_at",   table_name="system_events")
    op.drop_index("ix_system_events_reference_id", table_name="system_events")
    op.drop_index("ix_system_events_category",     table_name="system_events")
    op.drop_index("ix_system_events_tenant_id",    table_name="system_events")
    op.drop_table("system_events")

    op.drop_index("ix_handoff_sessions_status",         table_name="handoff_sessions")
    op.drop_index("ix_handoff_sessions_customer_phone", table_name="handoff_sessions")
    op.drop_index("ix_handoff_sessions_tenant_id",      table_name="handoff_sessions")
    op.drop_table("handoff_sessions")

    op.drop_index("ix_payment_sessions_tenant_id",          table_name="payment_sessions")
    op.drop_index("ix_payment_sessions_gateway_payment_id", table_name="payment_sessions")
    op.drop_table("payment_sessions")

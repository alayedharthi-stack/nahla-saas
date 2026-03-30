"""Add intelligence engine tables: smart_automations, automation_events, predictive_reorder_estimates

Revision ID: 0007
Revises: 0006
Create Date: 2026-03-30
"""
from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import JSONB

revision = "0007"
down_revision = "0006"
branch_labels = None
depends_on = None


def upgrade():
    # ── smart_automations ──────────────────────────────────────────────────────
    op.create_table(
        "smart_automations",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("tenant_id", sa.Integer(), sa.ForeignKey("tenants.id"), nullable=False),
        sa.Column("automation_type", sa.String(), nullable=False),
        sa.Column("name", sa.String(), nullable=False),
        sa.Column("enabled", sa.Boolean(), nullable=False, server_default="false"),
        sa.Column("config", JSONB, nullable=True),
        sa.Column("template_id", sa.Integer(), sa.ForeignKey("whatsapp_templates.id"), nullable=True),
        sa.Column("stats_triggered", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("stats_sent", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("stats_converted", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("created_at", sa.DateTime(), nullable=True),
        sa.Column("updated_at", sa.DateTime(), nullable=True),
    )
    op.create_index("ix_smart_automations_tenant_id", "smart_automations", ["tenant_id"])

    # ── automation_events ──────────────────────────────────────────────────────
    op.create_table(
        "automation_events",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("tenant_id", sa.Integer(), sa.ForeignKey("tenants.id"), nullable=False),
        sa.Column("event_type", sa.String(), nullable=False),
        sa.Column("customer_id", sa.Integer(), sa.ForeignKey("customers.id"), nullable=True),
        sa.Column("payload", JSONB, nullable=True),
        sa.Column("processed", sa.Boolean(), nullable=False, server_default="false"),
        sa.Column("automation_id", sa.Integer(), sa.ForeignKey("smart_automations.id"), nullable=True),
        sa.Column("created_at", sa.DateTime(), nullable=True),
    )
    op.create_index("ix_automation_events_tenant_id", "automation_events", ["tenant_id"])
    op.create_index("ix_automation_events_event_type", "automation_events", ["event_type"])
    op.create_index("ix_automation_events_processed", "automation_events", ["processed"])

    # ── predictive_reorder_estimates ───────────────────────────────────────────
    op.create_table(
        "predictive_reorder_estimates",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("tenant_id", sa.Integer(), sa.ForeignKey("tenants.id"), nullable=False),
        sa.Column("customer_id", sa.Integer(), sa.ForeignKey("customers.id"), nullable=False),
        sa.Column("product_id", sa.Integer(), sa.ForeignKey("products.id"), nullable=False),
        sa.Column("quantity_purchased", sa.Float(), nullable=True),
        sa.Column("purchase_date", sa.DateTime(), nullable=True),
        sa.Column("consumption_rate_days", sa.Integer(), nullable=True),
        sa.Column("predicted_reorder_date", sa.DateTime(), nullable=True),
        sa.Column("notified", sa.Boolean(), nullable=False, server_default="false"),
        sa.Column("created_at", sa.DateTime(), nullable=True),
        sa.Column("updated_at", sa.DateTime(), nullable=True),
    )
    op.create_index("ix_pred_reorder_tenant_id", "predictive_reorder_estimates", ["tenant_id"])
    op.create_index("ix_pred_reorder_customer_id", "predictive_reorder_estimates", ["customer_id"])
    op.create_index("ix_pred_reorder_date", "predictive_reorder_estimates", ["predicted_reorder_date"])


def downgrade():
    op.drop_index("ix_pred_reorder_date", table_name="predictive_reorder_estimates")
    op.drop_index("ix_pred_reorder_customer_id", table_name="predictive_reorder_estimates")
    op.drop_index("ix_pred_reorder_tenant_id", table_name="predictive_reorder_estimates")
    op.drop_table("predictive_reorder_estimates")

    op.drop_index("ix_automation_events_processed", table_name="automation_events")
    op.drop_index("ix_automation_events_event_type", table_name="automation_events")
    op.drop_index("ix_automation_events_tenant_id", table_name="automation_events")
    op.drop_table("automation_events")

    op.drop_index("ix_smart_automations_tenant_id", table_name="smart_automations")
    op.drop_table("smart_automations")

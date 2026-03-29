"""Customer Intelligence layer — 6 new tables for AI memory.

Revision ID: 0002
Revises: 0001
Create Date: 2026-03-29
"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import JSONB

revision: str = "0002"
down_revision: Union[str, None] = "0001"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:

    # ── customer_profiles ─────────────────────────────────────────────────────
    op.create_table(
        "customer_profiles",
        sa.Column("id",                     sa.Integer,  primary_key=True),
        sa.Column("customer_id",            sa.Integer,  sa.ForeignKey("customers.id"), nullable=False, unique=True),
        sa.Column("tenant_id",              sa.Integer,  sa.ForeignKey("tenants.id"),   nullable=False),
        sa.Column("total_orders",           sa.Integer,  default=0,   server_default="0"),
        sa.Column("total_spend_sar",        sa.Float,    default=0.0, server_default="0"),
        sa.Column("average_order_value_sar",sa.Float,    default=0.0, server_default="0"),
        sa.Column("max_single_order_sar",   sa.Float,    default=0.0, server_default="0"),
        sa.Column("first_seen_at",          sa.DateTime, nullable=True),
        sa.Column("last_seen_at",           sa.DateTime, nullable=True),
        sa.Column("last_order_at",          sa.DateTime, nullable=True),
        sa.Column("segment",                sa.String,   default="new", server_default="new"),
        sa.Column("churn_risk_score",       sa.Float,    default=0.0, server_default="0"),
        sa.Column("lifetime_value_score",   sa.Float,    default=0.0, server_default="0"),
        sa.Column("is_returning",           sa.Boolean,  default=False, server_default="false"),
        sa.Column("preferred_language",     sa.String,   default="ar", server_default="ar"),
        sa.Column("updated_at",             sa.DateTime, nullable=True),
    )
    op.create_index("ix_customer_profiles_tenant_id", "customer_profiles", ["tenant_id"])

    # ── customer_preferences ──────────────────────────────────────────────────
    op.create_table(
        "customer_preferences",
        sa.Column("id",                       sa.Integer, primary_key=True),
        sa.Column("customer_id",              sa.Integer, sa.ForeignKey("customers.id"), nullable=False, unique=True),
        sa.Column("tenant_id",                sa.Integer, sa.ForeignKey("tenants.id"),   nullable=False),
        sa.Column("preferred_categories",     JSONB,      nullable=True),
        sa.Column("preferred_brands",         JSONB,      nullable=True),
        sa.Column("price_range_min_sar",      sa.Float,   nullable=True),
        sa.Column("price_range_max_sar",      sa.Float,   nullable=True),
        sa.Column("preferred_payment_method", sa.String,  nullable=True),
        sa.Column("preferred_delivery_type",  sa.String,  nullable=True),
        sa.Column("communication_style",      sa.String,  default="neutral", server_default="neutral"),
        sa.Column("language",                 sa.String,  default="ar",      server_default="ar"),
        sa.Column("inferred_notes",           JSONB,      nullable=True),
        sa.Column("updated_at",               sa.DateTime, nullable=True),
    )
    op.create_index("ix_customer_preferences_tenant_id", "customer_preferences", ["tenant_id"])

    # ── product_affinities ────────────────────────────────────────────────────
    op.create_table(
        "product_affinities",
        sa.Column("id",                   sa.Integer, primary_key=True),
        sa.Column("customer_id",          sa.Integer, sa.ForeignKey("customers.id"), nullable=False),
        sa.Column("product_id",           sa.Integer, sa.ForeignKey("products.id"),  nullable=False),
        sa.Column("tenant_id",            sa.Integer, sa.ForeignKey("tenants.id"),   nullable=False),
        sa.Column("view_count",           sa.Integer, default=0,   server_default="0"),
        sa.Column("purchase_count",       sa.Integer, default=0,   server_default="0"),
        sa.Column("recommendation_count", sa.Integer, default=0,   server_default="0"),
        sa.Column("affinity_score",       sa.Float,   default=0.0, server_default="0"),
        sa.Column("last_recommended_at",  sa.DateTime, nullable=True),
        sa.Column("last_purchased_at",    sa.DateTime, nullable=True),
        sa.Column("updated_at",           sa.DateTime, nullable=True),
    )
    op.create_index("ix_product_affinities_customer_product",
                    "product_affinities", ["customer_id", "product_id"], unique=True)
    op.create_index("ix_product_affinities_tenant_id", "product_affinities", ["tenant_id"])

    # ── price_sensitivity_scores ──────────────────────────────────────────────
    op.create_table(
        "price_sensitivity_scores",
        sa.Column("id",                      sa.Integer, primary_key=True),
        sa.Column("customer_id",             sa.Integer, sa.ForeignKey("customers.id"), nullable=False, unique=True),
        sa.Column("tenant_id",               sa.Integer, sa.ForeignKey("tenants.id"),   nullable=False),
        sa.Column("score",                   sa.Float,   default=0.5, server_default="0.5"),
        sa.Column("avg_order_value_sar",     sa.Float,   default=0.0, server_default="0"),
        sa.Column("max_observed_spend_sar",  sa.Float,   default=0.0, server_default="0"),
        sa.Column("coupon_usage_count",      sa.Integer, default=0,   server_default="0"),
        sa.Column("coupon_usage_rate",       sa.Float,   default=0.0, server_default="0"),
        sa.Column("discount_response_rate",  sa.Float,   default=0.0, server_default="0"),
        sa.Column("recommended_discount_pct",sa.Integer, default=0,   server_default="0"),
        sa.Column("updated_at",              sa.DateTime, nullable=True),
    )
    op.create_index("ix_price_sensitivity_tenant_id", "price_sensitivity_scores", ["tenant_id"])

    # ── conversation_history_summaries ────────────────────────────────────────
    op.create_table(
        "conversation_history_summaries",
        sa.Column("id",                     sa.Integer, primary_key=True),
        sa.Column("customer_id",            sa.Integer, sa.ForeignKey("customers.id"), nullable=False, unique=True),
        sa.Column("tenant_id",              sa.Integer, sa.ForeignKey("tenants.id"),   nullable=False),
        sa.Column("summary_text",           sa.Text,    nullable=True),
        sa.Column("topics_discussed",       JSONB,      nullable=True),
        sa.Column("products_mentioned",     JSONB,      nullable=True),
        sa.Column("coupons_used",           JSONB,      nullable=True),
        sa.Column("last_intent",            sa.String,  nullable=True),
        sa.Column("sentiment",              sa.String,  default="neutral", server_default="neutral"),
        sa.Column("escalation_count",       sa.Integer, default=0, server_default="0"),
        sa.Column("last_escalation_reason", sa.Text,    nullable=True),
        sa.Column("total_conversations",    sa.Integer, default=0, server_default="0"),
        sa.Column("updated_at",             sa.DateTime, nullable=True),
    )
    op.create_index("ix_conv_history_summaries_tenant_id",
                    "conversation_history_summaries", ["tenant_id"])

    # ── ai_action_logs ────────────────────────────────────────────────────────
    op.create_table(
        "ai_action_logs",
        sa.Column("id",               sa.Integer, primary_key=True),
        sa.Column("tenant_id",        sa.Integer, sa.ForeignKey("tenants.id"),   nullable=False),
        sa.Column("customer_id",      sa.Integer, sa.ForeignKey("customers.id"), nullable=True),
        sa.Column("action_type",      sa.String,  nullable=False),
        sa.Column("proposed_payload", JSONB,      nullable=True),
        sa.Column("policy_result",    sa.String,  nullable=False),
        sa.Column("policy_notes",     sa.Text,    nullable=True),
        sa.Column("final_payload",    JSONB,      nullable=True),
        sa.Column("applied",          sa.Boolean, default=False, server_default="false"),
        sa.Column("created_at",       sa.DateTime, nullable=True),
    )
    op.create_index("ix_ai_action_logs_tenant_customer",
                    "ai_action_logs", ["tenant_id", "customer_id"])
    op.create_index("ix_ai_action_logs_action_type",
                    "ai_action_logs", ["action_type"])


def downgrade() -> None:
    op.drop_index("ix_ai_action_logs_action_type",          table_name="ai_action_logs")
    op.drop_index("ix_ai_action_logs_tenant_customer",      table_name="ai_action_logs")
    op.drop_table("ai_action_logs")

    op.drop_index("ix_conv_history_summaries_tenant_id",    table_name="conversation_history_summaries")
    op.drop_table("conversation_history_summaries")

    op.drop_index("ix_price_sensitivity_tenant_id",         table_name="price_sensitivity_scores")
    op.drop_table("price_sensitivity_scores")

    op.drop_index("ix_product_affinities_tenant_id",        table_name="product_affinities")
    op.drop_index("ix_product_affinities_customer_product", table_name="product_affinities")
    op.drop_table("product_affinities")

    op.drop_index("ix_customer_preferences_tenant_id",      table_name="customer_preferences")
    op.drop_table("customer_preferences")

    op.drop_index("ix_customer_profiles_tenant_id",         table_name="customer_profiles")
    op.drop_table("customer_profiles")

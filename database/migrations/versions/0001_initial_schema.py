"""Initial schema — all Nahla SaaS tables.

Revision ID: 0001
Revises: —
Create Date: 2026-03-29
"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import JSONB

revision: str = "0001"
down_revision: Union[str, None] = None
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # ── tenants ──────────────────────────────────────────────────────────────
    op.create_table(
        "tenants",
        sa.Column("id",                       sa.Integer,  primary_key=True),
        sa.Column("name",                     sa.String,   nullable=False,  unique=True),
        sa.Column("domain",                   sa.String,   nullable=True,   unique=True),
        sa.Column("is_active",                sa.Boolean,  default=True),
        sa.Column("created_at",               sa.DateTime, nullable=True),
        sa.Column("store_address",            sa.Text,     nullable=True),
        sa.Column("google_maps_link",         sa.String,   nullable=True),
        sa.Column("apple_maps_link",          sa.String,   nullable=True),
        sa.Column("same_day_delivery_enabled",sa.Boolean,  default=False),
        sa.Column("pickup_enabled",           sa.Boolean,  default=True),
        sa.Column("branding",                 JSONB,       nullable=True),
        sa.Column("recommendation_controls",  JSONB,       nullable=True),
        sa.Column("coupon_policy",            JSONB,       nullable=True),
    )

    # ── tenant_settings ───────────────────────────────────────────────────────
    op.create_table(
        "tenant_settings",
        sa.Column("id",                  sa.Integer, primary_key=True),
        sa.Column("tenant_id",           sa.Integer, sa.ForeignKey("tenants.id"), nullable=False, unique=True),
        sa.Column("show_nahla_branding", sa.Boolean, default=True,  nullable=False),
        sa.Column("branding_text",       sa.String,  default="🐝 Powered by Nahla", nullable=False),
        sa.Column("created_at",          sa.DateTime, nullable=True),
        sa.Column("updated_at",          sa.DateTime, nullable=True),
        sa.Column("metadata",            JSONB,       nullable=True),
    )

    # ── users ────────────────────────────────────────────────────────────────
    op.create_table(
        "users",
        sa.Column("id",        sa.Integer, primary_key=True),
        sa.Column("username",  sa.String,  nullable=False, unique=True),
        sa.Column("email",     sa.String,  nullable=False, unique=True),
        sa.Column("tenant_id", sa.Integer, sa.ForeignKey("tenants.id"), nullable=False),
    )

    # ── whatsapp_numbers ──────────────────────────────────────────────────────
    op.create_table(
        "whatsapp_numbers",
        sa.Column("id",        sa.Integer, primary_key=True),
        sa.Column("number",    sa.String,  nullable=False, unique=True),
        sa.Column("config",    JSONB,      nullable=True),
        sa.Column("tenant_id", sa.Integer, sa.ForeignKey("tenants.id"), nullable=False),
    )

    # ── products ─────────────────────────────────────────────────────────────
    op.create_table(
        "products",
        sa.Column("id",                  sa.Integer, primary_key=True),
        sa.Column("external_id",         sa.String,  nullable=True),
        sa.Column("sku",                 sa.String,  nullable=True),
        sa.Column("title",               sa.String,  nullable=False),
        sa.Column("description",         sa.Text,    nullable=True),
        sa.Column("price",               sa.String,  nullable=True),
        sa.Column("metadata",            JSONB,      nullable=True),
        sa.Column("recommendation_tags", JSONB,      nullable=True),
        sa.Column("tenant_id",           sa.Integer, sa.ForeignKey("tenants.id"), nullable=False),
    )
    op.create_index("ix_products_external_id", "products", ["external_id"])

    # ── customers ─────────────────────────────────────────────────────────────
    op.create_table(
        "customers",
        sa.Column("id",        sa.Integer, primary_key=True),
        sa.Column("name",      sa.String,  nullable=True),
        sa.Column("email",     sa.String,  nullable=True),
        sa.Column("phone",     sa.String,  nullable=True),
        sa.Column("metadata",  JSONB,      nullable=True),
        sa.Column("tenant_id", sa.Integer, sa.ForeignKey("tenants.id"), nullable=False),
    )

    # ── orders ────────────────────────────────────────────────────────────────
    op.create_table(
        "orders",
        sa.Column("id",            sa.Integer, primary_key=True),
        sa.Column("external_id",   sa.String,  nullable=True),
        sa.Column("status",        sa.String,  nullable=False),
        sa.Column("total",         sa.String,  nullable=True),
        sa.Column("customer_info", JSONB,      nullable=True),
        sa.Column("line_items",    JSONB,      nullable=True),
        sa.Column("checkout_url",  sa.String,  nullable=True),
        sa.Column("is_abandoned",  sa.Boolean, default=False),
        sa.Column("metadata",      JSONB,      nullable=True),
        sa.Column("tenant_id",     sa.Integer, sa.ForeignKey("tenants.id"), nullable=False),
    )
    op.create_index("ix_orders_external_id", "orders", ["external_id"])

    # ── customer_addresses ────────────────────────────────────────────────────
    op.create_table(
        "customer_addresses",
        sa.Column("id",                    sa.Integer, primary_key=True),
        sa.Column("raw_address",           sa.Text,    nullable=True),
        sa.Column("saudi_national_address",sa.Text,    nullable=True),
        sa.Column("google_maps_link",      sa.String,  nullable=True),
        sa.Column("apple_maps_link",       sa.String,  nullable=True),
        sa.Column("whatsapp_location",     JSONB,      nullable=True),
        sa.Column("lat",                   sa.String,  nullable=True),
        sa.Column("lng",                   sa.String,  nullable=True),
        sa.Column("city",                  sa.String,  nullable=True),
        sa.Column("district",              sa.String,  nullable=True),
        sa.Column("address_text",          sa.Text,    nullable=True),
        sa.Column("address_type",          sa.String,  nullable=True),
        sa.Column("customer_id",           sa.Integer, sa.ForeignKey("customers.id"), nullable=True),
        sa.Column("order_id",              sa.Integer, sa.ForeignKey("orders.id"),    nullable=True),
        sa.Column("tenant_id",             sa.Integer, sa.ForeignKey("tenants.id"),   nullable=False),
    )

    # ── coupons ───────────────────────────────────────────────────────────────
    op.create_table(
        "coupons",
        sa.Column("id",             sa.Integer,  primary_key=True),
        sa.Column("code",           sa.String,   nullable=False, unique=True),
        sa.Column("description",    sa.Text,     nullable=True),
        sa.Column("discount_type",  sa.String,   nullable=True),
        sa.Column("discount_value", sa.String,   nullable=True),
        sa.Column("metadata",       JSONB,       nullable=True),
        sa.Column("expires_at",     sa.DateTime, nullable=True),
        sa.Column("tenant_id",      sa.Integer,  sa.ForeignKey("tenants.id"), nullable=False),
    )

    op.create_table(
        "coupon_rules",
        sa.Column("id",          sa.Integer, primary_key=True),
        sa.Column("rule_type",   sa.String,  nullable=False),
        sa.Column("rule_config", JSONB,      nullable=True),
        sa.Column("coupon_id",   sa.Integer, sa.ForeignKey("coupons.id"), nullable=False),
    )

    # ── integrations & sync ──────────────────────────────────────────────────
    op.create_table(
        "integrations",
        sa.Column("id",        sa.Integer, primary_key=True),
        sa.Column("provider",  sa.String,  nullable=False),
        sa.Column("config",    JSONB,      nullable=True),
        sa.Column("enabled",   sa.Boolean, default=True),
        sa.Column("tenant_id", sa.Integer, sa.ForeignKey("tenants.id"), nullable=False),
    )

    op.create_table(
        "sync_logs",
        sa.Column("id",            sa.Integer,  primary_key=True),
        sa.Column("resource_type", sa.String,   nullable=False),
        sa.Column("external_id",   sa.String,   nullable=True),
        sa.Column("status",        sa.String,   nullable=False),
        sa.Column("message",       sa.Text,     nullable=True),
        sa.Column("metadata",      JSONB,       nullable=True),
        sa.Column("created_at",    sa.DateTime, nullable=True),
        sa.Column("tenant_id",     sa.Integer,  sa.ForeignKey("tenants.id"), nullable=False),
    )

    # ── automation ────────────────────────────────────────────────────────────
    op.create_table(
        "automation_rules",
        sa.Column("id",             sa.Integer, primary_key=True),
        sa.Column("name",           sa.String,  nullable=False),
        sa.Column("trigger_type",   sa.String,  nullable=False),
        sa.Column("trigger_config", JSONB,      nullable=True),
        sa.Column("action_config",  JSONB,      nullable=True),
        sa.Column("is_active",      sa.Boolean, default=True),
        sa.Column("tenant_id",      sa.Integer, sa.ForeignKey("tenants.id"), nullable=False),
    )

    # ── location & delivery ──────────────────────────────────────────────────
    op.create_table(
        "delivery_zones",
        sa.Column("id",        sa.Integer, primary_key=True),
        sa.Column("name",      sa.String,  nullable=False),
        sa.Column("zone_type", sa.String,  nullable=True),
        sa.Column("geojson",   JSONB,      nullable=True),
        sa.Column("tenant_id", sa.Integer, sa.ForeignKey("tenants.id"), nullable=False),
    )

    op.create_table(
        "shipping_fees",
        sa.Column("id",         sa.Integer, primary_key=True),
        sa.Column("city",       sa.String,  nullable=True),
        sa.Column("zone_name",  sa.String,  nullable=True),
        sa.Column("fee_amount", sa.String,  nullable=True),
        sa.Column("tenant_id",  sa.Integer, sa.ForeignKey("tenants.id"), nullable=False),
    )

    # ── knowledge & AI policy ─────────────────────────────────────────────────
    op.create_table(
        "knowledge_policies",
        sa.Column("id",                 sa.Integer, primary_key=True),
        sa.Column("allowed_categories", JSONB,      nullable=True),
        sa.Column("blocked_categories", JSONB,      nullable=True),
        sa.Column("escalation_rules",   JSONB,      nullable=True),
        sa.Column("owner_override",     JSONB,      nullable=True),
        sa.Column("tenant_id",          sa.Integer, sa.ForeignKey("tenants.id"), nullable=False),
    )

    # ── conversations & messages ──────────────────────────────────────────────
    op.create_table(
        "conversations",
        sa.Column("id",               sa.Integer, primary_key=True),
        sa.Column("external_id",      sa.String,  nullable=True),
        sa.Column("status",           sa.String,  nullable=False),
        sa.Column("customer_id",      sa.Integer, sa.ForeignKey("customers.id"), nullable=True),
        sa.Column("tenant_id",        sa.Integer, sa.ForeignKey("tenants.id"),   nullable=False),
        sa.Column("is_human_handoff", sa.Boolean, default=False),
        sa.Column("is_urgent",        sa.Boolean, default=False),
        sa.Column("paused_by_human",  sa.Boolean, default=False),
        sa.Column("metadata",         JSONB,      nullable=True),
    )
    op.create_index("ix_conversations_external_id", "conversations", ["external_id"])

    op.create_table(
        "message_events",
        sa.Column("id",              sa.Integer,  primary_key=True),
        sa.Column("conversation_id", sa.Integer,  sa.ForeignKey("conversations.id"), nullable=True),
        sa.Column("tenant_id",       sa.Integer,  sa.ForeignKey("tenants.id"),       nullable=False),
        sa.Column("direction",       sa.String,   nullable=True),
        sa.Column("body",            sa.Text,     nullable=True),
        sa.Column("event_type",      sa.String,   nullable=True),
        sa.Column("created_at",      sa.DateTime, nullable=True),
        sa.Column("metadata",        JSONB,       nullable=True),
    )

    # ── widget ────────────────────────────────────────────────────────────────
    op.create_table(
        "widget_settings",
        sa.Column("id",                  sa.Integer, primary_key=True),
        sa.Column("bot_name",            sa.String,  nullable=True),
        sa.Column("logo_url",            sa.String,  nullable=True),
        sa.Column("color",               sa.String,  nullable=True),
        sa.Column("welcome_text",        sa.Text,    nullable=True),
        sa.Column("show_nahla_branding", sa.Boolean, default=True),
        sa.Column("branding_text",       sa.String,  default="🐝 Powered by Nahla"),
        sa.Column("options",             JSONB,      nullable=True),
        sa.Column("tenant_id",           sa.Integer, sa.ForeignKey("tenants.id"), nullable=False),
    )

    # ── marketplace ───────────────────────────────────────────────────────────
    op.create_table(
        "developers",
        sa.Column("id",           sa.Integer,  primary_key=True),
        sa.Column("username",     sa.String,   nullable=False, unique=True),
        sa.Column("email",        sa.String,   nullable=False, unique=True),
        sa.Column("company_name", sa.String,   nullable=True),
        sa.Column("website",      sa.String,   nullable=True),
        sa.Column("metadata",     JSONB,       nullable=True),
        sa.Column("created_at",   sa.DateTime, nullable=True),
    )

    op.create_table(
        "apps",
        sa.Column("id",             sa.Integer,  primary_key=True),
        sa.Column("developer_id",   sa.Integer,  sa.ForeignKey("developers.id"), nullable=False),
        sa.Column("name",           sa.String,   nullable=False),
        sa.Column("slug",           sa.String,   nullable=False, unique=True),
        sa.Column("description",    sa.Text,     nullable=True),
        sa.Column("price_sar",      sa.Integer,  default=0),
        sa.Column("billing_model",  sa.String,   default="one_time"),
        sa.Column("commission_rate",sa.Float,    default=0.20),
        sa.Column("permissions",    JSONB,       nullable=True),
        sa.Column("categories",     JSONB,       nullable=True),
        sa.Column("icon_url",       sa.String,   nullable=True),
        sa.Column("metadata",       JSONB,       nullable=True),
        sa.Column("is_published",   sa.Boolean,  default=True),
        sa.Column("created_at",     sa.DateTime, nullable=True),
        sa.Column("updated_at",     sa.DateTime, nullable=True),
    )

    op.create_table(
        "app_installs",
        sa.Column("id",           sa.Integer,  primary_key=True),
        sa.Column("app_id",       sa.Integer,  sa.ForeignKey("apps.id"),    nullable=False),
        sa.Column("tenant_id",    sa.Integer,  sa.ForeignKey("tenants.id"), nullable=False),
        sa.Column("permissions",  JSONB,       nullable=True),
        sa.Column("config",       JSONB,       nullable=True),
        sa.Column("status",       sa.String,   default="installed"),
        sa.Column("enabled",      sa.Boolean,  default=True),
        sa.Column("installed_at", sa.DateTime, nullable=True),
        sa.Column("metadata",     JSONB,       nullable=True),
    )

    op.create_table(
        "app_payments",
        sa.Column("id",                    sa.Integer,  primary_key=True),
        sa.Column("tenant_id",             sa.Integer,  sa.ForeignKey("tenants.id"),   nullable=False),
        sa.Column("app_id",                sa.Integer,  sa.ForeignKey("apps.id"),      nullable=False),
        sa.Column("developer_id",          sa.Integer,  sa.ForeignKey("developers.id"),nullable=False),
        sa.Column("amount_sar",            sa.Integer,  nullable=False),
        sa.Column("currency",              sa.String,   default="SAR"),
        sa.Column("commission_rate",       sa.Float,    default=0.20),
        sa.Column("commission_amount_sar", sa.Integer,  default=0),
        sa.Column("gateway",               sa.String,   nullable=True),
        sa.Column("status",                sa.String,   default="pending"),
        sa.Column("transaction_reference", sa.String,   nullable=True),
        sa.Column("paid_at",               sa.DateTime, nullable=True),
        sa.Column("metadata",              JSONB,       nullable=True),
        sa.Column("created_at",            sa.DateTime, nullable=True),
    )

    # ── billing ───────────────────────────────────────────────────────────────
    op.create_table(
        "billing_plans",
        sa.Column("id",            sa.Integer,  primary_key=True),
        sa.Column("tenant_id",     sa.Integer,  sa.ForeignKey("tenants.id"), nullable=True),
        sa.Column("slug",          sa.String,   nullable=False, unique=True),
        sa.Column("name",          sa.String,   nullable=False),
        sa.Column("description",   sa.Text,     nullable=True),
        sa.Column("currency",      sa.String,   default="SAR"),
        sa.Column("price_sar",     sa.Integer,  nullable=False),
        sa.Column("billing_cycle", sa.String,   nullable=False),
        sa.Column("is_enterprise", sa.Boolean,  default=False),
        sa.Column("features",      JSONB,       nullable=True),
        sa.Column("limits",        JSONB,       nullable=True),
        sa.Column("metadata",      JSONB,       nullable=True),
        sa.Column("created_at",    sa.DateTime, nullable=True),
    )

    op.create_table(
        "billing_subscriptions",
        sa.Column("id",            sa.Integer,  primary_key=True),
        sa.Column("tenant_id",     sa.Integer,  sa.ForeignKey("tenants.id"),     nullable=False),
        sa.Column("plan_id",       sa.Integer,  sa.ForeignKey("billing_plans.id"),nullable=False),
        sa.Column("status",        sa.String,   default="active"),
        sa.Column("started_at",    sa.DateTime, nullable=True),
        sa.Column("ends_at",       sa.DateTime, nullable=True),
        sa.Column("trial_ends_at", sa.DateTime, nullable=True),
        sa.Column("auto_renew",    sa.Boolean,  default=True),
        sa.Column("metadata",      JSONB,       nullable=True),
    )

    op.create_table(
        "billing_payments",
        sa.Column("id",                    sa.Integer,  primary_key=True),
        sa.Column("tenant_id",             sa.Integer,  sa.ForeignKey("tenants.id"),              nullable=False),
        sa.Column("subscription_id",       sa.Integer,  sa.ForeignKey("billing_subscriptions.id"),nullable=True),
        sa.Column("amount_sar",            sa.Integer,  nullable=False),
        sa.Column("currency",              sa.String,   default="SAR"),
        sa.Column("gateway",               sa.String,   nullable=False),
        sa.Column("transaction_reference", sa.String,   nullable=True),
        sa.Column("status",                sa.String,   nullable=False),
        sa.Column("paid_at",               sa.DateTime, nullable=True),
        sa.Column("metadata",              JSONB,       nullable=True),
        sa.Column("created_at",            sa.DateTime, nullable=True),
    )

    op.create_table(
        "billing_invoices",
        sa.Column("id",               sa.Integer,  primary_key=True),
        sa.Column("tenant_id",        sa.Integer,  sa.ForeignKey("tenants.id"),              nullable=False),
        sa.Column("subscription_id",  sa.Integer,  sa.ForeignKey("billing_subscriptions.id"),nullable=True),
        sa.Column("amount_due_sar",   sa.Integer,  nullable=False),
        sa.Column("amount_paid_sar",  sa.Integer,  default=0),
        sa.Column("currency",         sa.String,   default="SAR"),
        sa.Column("status",           sa.String,   nullable=False),
        sa.Column("issued_date",      sa.DateTime, nullable=True),
        sa.Column("due_date",         sa.DateTime, nullable=True),
        sa.Column("line_items",       JSONB,       nullable=True),
        sa.Column("metadata",         JSONB,       nullable=True),
    )

    # ── audit ─────────────────────────────────────────────────────────────────
    op.create_table(
        "audit_logs",
        sa.Column("id",            sa.Integer,  primary_key=True),
        sa.Column("category",      sa.String,   nullable=False),
        sa.Column("resource_type", sa.String,   nullable=True),
        sa.Column("resource_id",   sa.String,   nullable=True),
        sa.Column("action",        sa.String,   nullable=True),
        sa.Column("details",       JSONB,       nullable=True),
        sa.Column("created_at",    sa.DateTime, nullable=True),
        sa.Column("tenant_id",     sa.Integer,  sa.ForeignKey("tenants.id"), nullable=False),
    )


def downgrade() -> None:
    # Drop in reverse dependency order
    for table in [
        "audit_logs", "billing_invoices", "billing_payments", "billing_subscriptions",
        "billing_plans", "app_payments", "app_installs", "apps", "developers",
        "widget_settings", "message_events", "conversations", "knowledge_policies",
        "shipping_fees", "delivery_zones", "sync_logs", "integrations",
        "coupon_rules", "coupons", "customer_addresses", "orders",
        "customers", "products", "automation_rules",
        "whatsapp_numbers", "users", "tenant_settings", "tenants",
    ]:
        op.drop_table(table)

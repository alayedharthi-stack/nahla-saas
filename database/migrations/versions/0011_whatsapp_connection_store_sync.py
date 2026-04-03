"""Add WhatsAppConnection, StoreSyncJob, StoreKnowledgeSnapshot tables.

Revision ID: 0011
Revises: 0010
Create Date: 2026-04-03

Adds:
  - whatsapp_connections     (per-tenant WA state machine + token storage)
  - store_sync_jobs          (tracks full / incremental sync runs)
  - store_knowledge_snapshots (normalised AI-ready store knowledge)
"""
from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import JSONB


revision      = "0011"
down_revision = "0010"
branch_labels = None
depends_on    = None


def upgrade() -> None:
    # ── whatsapp_connections ───────────────────────────────────────────────────
    op.create_table(
        "whatsapp_connections",
        sa.Column("id",           sa.Integer(), nullable=False, primary_key=True),
        sa.Column("tenant_id",    sa.Integer(), sa.ForeignKey("tenants.id"), nullable=False, unique=True),
        sa.Column("status",       sa.String(), nullable=False, server_default="not_connected"),
        # Meta identifiers
        sa.Column("meta_business_account_id",     sa.String(), nullable=True),
        sa.Column("whatsapp_business_account_id", sa.String(), nullable=True),
        sa.Column("phone_number_id",              sa.String(), nullable=True),
        sa.Column("phone_number",                 sa.String(), nullable=True),
        sa.Column("business_display_name",        sa.String(), nullable=True),
        sa.Column("business_manager_id",          sa.String(), nullable=True),
        # Token (server-only)
        sa.Column("access_token",     sa.String(), nullable=True),
        sa.Column("token_type",       sa.String(), nullable=True),
        sa.Column("token_expires_at", sa.DateTime(), nullable=True),
        # Audit
        sa.Column("connected_at",     sa.DateTime(), nullable=True),
        sa.Column("last_verified_at", sa.DateTime(), nullable=True),
        sa.Column("last_attempt_at",  sa.DateTime(), nullable=True),
        sa.Column("last_error",       sa.Text(), nullable=True),
        # Prerequisites
        sa.Column("webhook_verified", sa.Boolean(), nullable=True, server_default="false"),
        sa.Column("sending_enabled",  sa.Boolean(), nullable=True, server_default="false"),
        sa.Column("extra_metadata",   JSONB(), nullable=True),
        sa.Column("created_at",       sa.DateTime(), nullable=True),
        sa.Column("updated_at",       sa.DateTime(), nullable=True),
    )
    op.create_index("ix_whatsapp_connections_tenant_id", "whatsapp_connections", ["tenant_id"])

    # ── store_sync_jobs ────────────────────────────────────────────────────────
    op.create_table(
        "store_sync_jobs",
        sa.Column("id",           sa.Integer(), nullable=False, primary_key=True),
        sa.Column("tenant_id",    sa.Integer(), sa.ForeignKey("tenants.id"), nullable=False),
        sa.Column("status",       sa.String(), nullable=False, server_default="pending"),
        sa.Column("sync_type",    sa.String(), nullable=False, server_default="full"),
        sa.Column("triggered_by", sa.String(), nullable=True),
        sa.Column("started_at",   sa.DateTime(), nullable=True),
        sa.Column("completed_at", sa.DateTime(), nullable=True),
        sa.Column("products_synced",   sa.Integer(), nullable=True, server_default="0"),
        sa.Column("categories_synced", sa.Integer(), nullable=True, server_default="0"),
        sa.Column("orders_synced",     sa.Integer(), nullable=True, server_default="0"),
        sa.Column("shipping_synced",   sa.Integer(), nullable=True, server_default="0"),
        sa.Column("coupons_synced",    sa.Integer(), nullable=True, server_default="0"),
        sa.Column("customers_synced",  sa.Integer(), nullable=True, server_default="0"),
        sa.Column("error_message",     sa.Text(), nullable=True),
        sa.Column("extra_metadata",    JSONB(), nullable=True),
        sa.Column("created_at",        sa.DateTime(), nullable=True),
    )
    op.create_index("ix_store_sync_jobs_tenant_id",  "store_sync_jobs", ["tenant_id"])
    op.create_index("ix_store_sync_jobs_status",     "store_sync_jobs", ["status"])

    # ── store_knowledge_snapshots ──────────────────────────────────────────────
    op.create_table(
        "store_knowledge_snapshots",
        sa.Column("id",        sa.Integer(), nullable=False, primary_key=True),
        sa.Column("tenant_id", sa.Integer(), sa.ForeignKey("tenants.id"), nullable=False, unique=True),
        sa.Column("store_profile",    JSONB(), nullable=True),
        sa.Column("catalog_summary",  JSONB(), nullable=True),
        sa.Column("shipping_summary", JSONB(), nullable=True),
        sa.Column("policy_summary",   JSONB(), nullable=True),
        sa.Column("coupon_summary",   JSONB(), nullable=True),
        sa.Column("last_full_sync_at",        sa.DateTime(), nullable=True),
        sa.Column("last_incremental_sync_at", sa.DateTime(), nullable=True),
        sa.Column("product_count",  sa.Integer(), nullable=True, server_default="0"),
        sa.Column("category_count", sa.Integer(), nullable=True, server_default="0"),
        sa.Column("order_count",    sa.Integer(), nullable=True, server_default="0"),
        sa.Column("coupon_count",   sa.Integer(), nullable=True, server_default="0"),
        sa.Column("customer_count", sa.Integer(), nullable=True, server_default="0"),
        sa.Column("sync_version", sa.Integer(), nullable=True, server_default="0"),
        sa.Column("created_at",   sa.DateTime(), nullable=True),
        sa.Column("updated_at",   sa.DateTime(), nullable=True),
    )
    op.create_index("ix_store_knowledge_snapshots_tenant_id", "store_knowledge_snapshots", ["tenant_id"])


def downgrade() -> None:
    op.drop_table("store_knowledge_snapshots")
    op.drop_table("store_sync_jobs")
    op.drop_table("whatsapp_connections")

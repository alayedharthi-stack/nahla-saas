"""Dormant install-first ownership and browser transactions; no existing DDL changed.

Revision ID: 0124
Revises: 0123
Explicit future migration only. Not authorized for production by dormant S3.
"""
from alembic import op
import sqlalchemy as sa

revision = "0124"
down_revision = "0123"
branch_labels = None
depends_on = None


def upgrade():
    op.create_table(
        "shopify_owner_identities",
        sa.Column("connection_id", sa.BigInteger(), sa.ForeignKey("shopify_connections.id"), primary_key=True),
        sa.Column("shop_gid", sa.String(64), nullable=False, unique=True),
        sa.Column("shopify_user_id", sa.BigInteger(), nullable=False),
        sa.Column("tenant_id", sa.Integer(), sa.ForeignKey("tenants.id"), nullable=False),
        sa.Column("user_id", sa.Integer(), sa.ForeignKey("users.id"), nullable=False, unique=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint("shopify_user_id > 0", name="ck_shopify_owner_positive"),
    )
    op.create_table(
        "shopify_install_flows",
        sa.Column("id", sa.String(64), primary_key=True),
        sa.Column("state_hash", sa.String(64), unique=True, nullable=False),
        sa.Column("browser_hash", sa.String(64), nullable=False),
        sa.Column("shop_domain", sa.String(255), nullable=False),
        sa.Column("redirect_hash", sa.String(64), nullable=False),
        sa.Column("phase", sa.String(24), nullable=False),
        sa.Column("shop_gid", sa.String(64)),
        sa.Column("owner_id", sa.BigInteger()),
        sa.Column("online_token_enc", sa.Text()),
        sa.Column("owner_expires_at", sa.DateTime(timezone=True)),
        sa.Column("connection_id", sa.BigInteger(), sa.ForeignKey("shopify_connections.id")),
        sa.Column("generation", sa.BigInteger(), nullable=False),
        sa.Column("handoff_hash", sa.String(64), unique=True),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint("phase IN ('online','offline','exchanging_online','exchanging_offline','ready','redeeming','redeemed','failed')",
                           name="ck_shopify_install_phase"),
        sa.CheckConstraint("phase NOT IN ('redeemed','failed') OR online_token_enc IS NULL",
                           name="ck_shopify_install_terminal_secrets"),
    )
    op.create_index("ix_shopify_install_flows_expires_at", "shopify_install_flows", ["expires_at"])
    op.create_index("ix_shopify_install_created", "shopify_install_flows", ["created_at"])
    op.create_index("ix_shopify_install_shop_created", "shopify_install_flows", ["shop_domain", "created_at"])
    op.create_index("ix_shopify_install_browser_created", "shopify_install_flows", ["browser_hash", "created_at"])


def downgrade():
    op.drop_table("shopify_install_flows")
    op.drop_table("shopify_owner_identities")

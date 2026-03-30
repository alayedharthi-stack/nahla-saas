"""Add campaigns table

Revision ID: 0005
Revises: 0004
Create Date: 2026-03-30
"""
from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import JSONB

revision = "0005"
down_revision = "0004"
branch_labels = None
depends_on = None


def upgrade():
    op.create_table(
        "campaigns",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("tenant_id", sa.Integer(), sa.ForeignKey("tenants.id"), nullable=False),
        sa.Column("name", sa.String(), nullable=False),
        sa.Column("campaign_type", sa.String(), nullable=False),
        sa.Column("status", sa.String(), nullable=False, server_default="draft"),
        sa.Column("template_id", sa.String(), nullable=True),
        sa.Column("template_name", sa.String(), nullable=True),
        sa.Column("template_language", sa.String(), nullable=True, server_default="ar"),
        sa.Column("template_category", sa.String(), nullable=True),
        sa.Column("template_body", sa.Text(), nullable=True),
        sa.Column("template_variables", JSONB, nullable=True),
        sa.Column("audience_type", sa.String(), nullable=True),
        sa.Column("audience_count", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("schedule_type", sa.String(), nullable=True, server_default="immediate"),
        sa.Column("schedule_time", sa.DateTime(), nullable=True),
        sa.Column("delay_minutes", sa.Integer(), nullable=True),
        sa.Column("coupon_code", sa.String(), nullable=True),
        sa.Column("sent_count", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("delivered_count", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("read_count", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("clicked_count", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("converted_count", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("created_at", sa.DateTime(), nullable=True),
        sa.Column("updated_at", sa.DateTime(), nullable=True),
        sa.Column("launched_at", sa.DateTime(), nullable=True),
    )
    op.create_index("ix_campaigns_tenant_id", "campaigns", ["tenant_id"])


def downgrade():
    op.drop_index("ix_campaigns_tenant_id", table_name="campaigns")
    op.drop_table("campaigns")

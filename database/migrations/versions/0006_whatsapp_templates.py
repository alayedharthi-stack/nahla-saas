"""Add whatsapp_templates table

Revision ID: 0006
Revises: 0005
Create Date: 2026-03-30
"""
from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import JSONB

revision = "0006"
down_revision = "0005"
branch_labels = None
depends_on = None


def upgrade():
    op.create_table(
        "whatsapp_templates",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("tenant_id", sa.Integer(), sa.ForeignKey("tenants.id"), nullable=False),
        sa.Column("meta_template_id", sa.String(), nullable=True),
        sa.Column("name", sa.String(), nullable=False),
        sa.Column("language", sa.String(), nullable=False, server_default="ar"),
        sa.Column("category", sa.String(), nullable=False),
        sa.Column("status", sa.String(), nullable=False, server_default="PENDING"),
        sa.Column("rejection_reason", sa.Text(), nullable=True),
        sa.Column("components", JSONB, nullable=True),
        sa.Column("created_at", sa.DateTime(), nullable=True),
        sa.Column("updated_at", sa.DateTime(), nullable=True),
        sa.Column("synced_at", sa.DateTime(), nullable=True),
    )
    op.create_index("ix_whatsapp_templates_tenant_id", "whatsapp_templates", ["tenant_id"])
    op.create_index("ix_whatsapp_templates_status", "whatsapp_templates", ["status"])


def downgrade():
    op.drop_index("ix_whatsapp_templates_status", table_name="whatsapp_templates")
    op.drop_index("ix_whatsapp_templates_tenant_id", table_name="whatsapp_templates")
    op.drop_table("whatsapp_templates")

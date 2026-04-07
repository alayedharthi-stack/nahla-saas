"""Add whatsapp_usage table for monthly conversation tracking.

Revision ID: 0012
Revises: 0011
Create Date: 2026-04-07

Adds:
  - whatsapp_usage  (per-tenant monthly conversation counter + limit enforcement)
"""
from alembic import op
import sqlalchemy as sa

revision = '0012'
down_revision = '0011'
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        'whatsapp_usage',
        sa.Column('id',                   sa.Integer(),  primary_key=True),
        sa.Column('tenant_id',            sa.Integer(),  sa.ForeignKey('tenants.id'), nullable=False),
        sa.Column('year',                 sa.Integer(),  nullable=False),
        sa.Column('month',                sa.Integer(),  nullable=False),
        sa.Column('conversations_used',   sa.Integer(),  nullable=False, server_default='0'),
        sa.Column('conversations_limit',  sa.Integer(),  nullable=False, server_default='1000'),
        sa.Column('alert_80_sent',        sa.Boolean(),  nullable=False, server_default='false'),
        sa.Column('alert_100_sent',       sa.Boolean(),  nullable=False, server_default='false'),
        sa.Column('created_at',           sa.DateTime(), server_default=sa.text('NOW()')),
        sa.Column('updated_at',           sa.DateTime(), server_default=sa.text('NOW()')),
    )
    op.create_index('ix_whatsapp_usage_tenant_id',    'whatsapp_usage', ['tenant_id'])
    op.create_index('ix_whatsapp_usage_tenant_month', 'whatsapp_usage', ['tenant_id', 'year', 'month'], unique=True)


def downgrade() -> None:
    op.drop_index('ix_whatsapp_usage_tenant_month', table_name='whatsapp_usage')
    op.drop_index('ix_whatsapp_usage_tenant_id',    table_name='whatsapp_usage')
    op.drop_table('whatsapp_usage')

"""Create merchant_widgets table (Conversion Widgets System).

Revision ID: 0015
Revises: 0014
Create Date: 2026-04-08

Adds:
  - merchant_widgets  (per-tenant visual widget state + display rules)

widget_key examples:
  whatsapp_widget, discount_popup, slide_offer
"""
from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import JSONB

revision = '0015'
down_revision = '0014'
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        'merchant_widgets',
        sa.Column('id',            sa.Integer(),  primary_key=True, autoincrement=True),
        sa.Column('tenant_id',     sa.Integer(),  sa.ForeignKey('tenants.id'), nullable=False),
        sa.Column('widget_key',    sa.String(64), nullable=False),
        sa.Column('is_enabled',    sa.Boolean(),  nullable=False, server_default='false'),
        sa.Column('settings_json', JSONB(),        nullable=True),
        sa.Column('display_rules', JSONB(),        nullable=True),
        sa.Column('created_at',    sa.DateTime(), server_default=sa.text('NOW()')),
        sa.Column('updated_at',    sa.DateTime(), server_default=sa.text('NOW()')),
    )
    op.create_index('ix_merchant_widgets_tenant_id', 'merchant_widgets', ['tenant_id'])
    op.create_unique_constraint(
        'uq_merchant_widget_tenant_key',
        'merchant_widgets',
        ['tenant_id', 'widget_key'],
    )


def downgrade() -> None:
    op.drop_constraint('uq_merchant_widget_tenant_key', 'merchant_widgets', type_='unique')
    op.drop_index('ix_merchant_widgets_tenant_id', table_name='merchant_widgets')
    op.drop_table('merchant_widgets')

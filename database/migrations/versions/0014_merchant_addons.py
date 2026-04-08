"""Create merchant_addons table.

Revision ID: 0014
Revises: 0013
Create Date: 2026-04-08

Adds:
  - merchant_addons  (per-tenant addon state with settings JSON)
"""
from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import JSONB

revision = '0014'
down_revision = '0013'
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        'merchant_addons',
        sa.Column('id',            sa.Integer(),     primary_key=True, autoincrement=True),
        sa.Column('tenant_id',     sa.Integer(),     sa.ForeignKey('tenants.id'), nullable=False),
        sa.Column('addon_key',     sa.String(64),    nullable=False),
        sa.Column('is_enabled',    sa.Boolean(),     nullable=False, server_default='false'),
        sa.Column('settings_json', JSONB(),          nullable=True),
        sa.Column('created_at',    sa.DateTime(),    server_default=sa.text('NOW()')),
        sa.Column('updated_at',    sa.DateTime(),    server_default=sa.text('NOW()')),
    )
    op.create_index('ix_merchant_addons_tenant_id',  'merchant_addons', ['tenant_id'])
    op.create_unique_constraint(
        'uq_merchant_addon_tenant_key',
        'merchant_addons',
        ['tenant_id', 'addon_key'],
    )


def downgrade() -> None:
    op.drop_constraint('uq_merchant_addon_tenant_key', 'merchant_addons', type_='unique')
    op.drop_index('ix_merchant_addons_tenant_id', table_name='merchant_addons')
    op.drop_table('merchant_addons')

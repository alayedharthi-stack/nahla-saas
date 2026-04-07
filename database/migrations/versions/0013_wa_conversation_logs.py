"""Add wa_conversation_windows and conversation_logs tables.
Also update whatsapp_usage to split service/marketing counters.

Revision ID: 0013
Revises: 0012
Create Date: 2026-04-07

Adds:
  - wa_conversation_windows  (per-customer 24-h window state, FOR UPDATE safe)
  - conversation_logs        (immutable audit log per billable conversation)

Updates whatsapp_usage:
  - Replace conversations_used with service_conversations_used +
    marketing_conversations_used
"""
from alembic import op
import sqlalchemy as sa

revision = '0013'
down_revision = '0012'
branch_labels = None
depends_on = None


def upgrade() -> None:
    # ── wa_conversation_windows ───────────────────────────────────────────────
    op.create_table(
        'wa_conversation_windows',
        sa.Column('id',             sa.Integer(),  primary_key=True),
        sa.Column('tenant_id',      sa.Integer(),  sa.ForeignKey('tenants.id'), nullable=False),
        sa.Column('customer_phone', sa.String(),   nullable=False),
        sa.Column('window_start',   sa.DateTime(), nullable=False),
        sa.Column('category',       sa.String(),   nullable=False, server_default='service'),
        sa.Column('created_at',     sa.DateTime(), server_default=sa.text('NOW()')),
        sa.Column('updated_at',     sa.DateTime(), server_default=sa.text('NOW()')),
    )
    op.create_index('ix_wa_conv_windows_tenant',        'wa_conversation_windows', ['tenant_id'])
    op.create_index(
        'ix_wa_conv_windows_tenant_phone',
        'wa_conversation_windows',
        ['tenant_id', 'customer_phone'],
        unique=True,
    )

    # ── conversation_logs ─────────────────────────────────────────────────────
    op.create_table(
        'conversation_logs',
        sa.Column('id',                      sa.Integer(),  primary_key=True),
        sa.Column('tenant_id',               sa.Integer(),  sa.ForeignKey('tenants.id'), nullable=False),
        sa.Column('customer_phone',          sa.String(),   nullable=False),
        sa.Column('conversation_started_at', sa.DateTime(), nullable=False),
        sa.Column('source',                  sa.String(),   nullable=False, server_default='inbound'),
        sa.Column('category',                sa.String(),   nullable=False, server_default='service'),
        sa.Column('created_at',              sa.DateTime(), server_default=sa.text('NOW()')),
    )
    op.create_index('ix_conv_logs_tenant_id',    'conversation_logs', ['tenant_id'])
    op.create_index('ix_conv_logs_customer',     'conversation_logs', ['tenant_id', 'customer_phone'])
    op.create_index('ix_conv_logs_started_at',   'conversation_logs', ['tenant_id', 'conversation_started_at'])

    # ── whatsapp_usage — add split counters, keep old column as sum ───────────
    op.add_column('whatsapp_usage',
        sa.Column('service_conversations_used',   sa.Integer(), nullable=False, server_default='0'))
    op.add_column('whatsapp_usage',
        sa.Column('marketing_conversations_used', sa.Integer(), nullable=False, server_default='0'))
    # Migrate existing counts into service bucket, then drop old column
    op.execute(
        "UPDATE whatsapp_usage SET service_conversations_used = COALESCE(conversations_used, 0)"
    )
    op.drop_column('whatsapp_usage', 'conversations_used')


def downgrade() -> None:
    op.add_column('whatsapp_usage',
        sa.Column('conversations_used', sa.Integer(), nullable=False, server_default='0'))
    op.execute(
        "UPDATE whatsapp_usage "
        "SET conversations_used = service_conversations_used + marketing_conversations_used"
    )
    op.drop_column('whatsapp_usage', 'marketing_conversations_used')
    op.drop_column('whatsapp_usage', 'service_conversations_used')

    op.drop_index('ix_conv_logs_started_at',  table_name='conversation_logs')
    op.drop_index('ix_conv_logs_customer',    table_name='conversation_logs')
    op.drop_index('ix_conv_logs_tenant_id',   table_name='conversation_logs')
    op.drop_table('conversation_logs')

    op.drop_index('ix_wa_conv_windows_tenant_phone', table_name='wa_conversation_windows')
    op.drop_index('ix_wa_conv_windows_tenant',       table_name='wa_conversation_windows')
    op.drop_table('wa_conversation_windows')

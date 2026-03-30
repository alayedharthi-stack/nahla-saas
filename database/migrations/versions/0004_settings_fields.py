"""Add structured settings JSONB columns to tenant_settings.

Revision ID: 0004
Revises: 0003
Create Date: 2026-03-30
"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import JSONB

revision: str = "0004"
down_revision: Union[str, None] = "0003"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column("tenant_settings", sa.Column("whatsapp_settings", JSONB, nullable=True))
    op.add_column("tenant_settings", sa.Column("ai_settings", JSONB, nullable=True))
    op.add_column("tenant_settings", sa.Column("store_settings", JSONB, nullable=True))
    op.add_column("tenant_settings", sa.Column("notification_settings", JSONB, nullable=True))


def downgrade() -> None:
    op.drop_column("tenant_settings", "notification_settings")
    op.drop_column("tenant_settings", "store_settings")
    op.drop_column("tenant_settings", "ai_settings")
    op.drop_column("tenant_settings", "whatsapp_settings")

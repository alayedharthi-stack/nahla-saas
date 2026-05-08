"""0043 — Clear placeholder coexistence client_id values in JSON metadata

Revision ID: 0043
Revises: 0042
"""
from __future__ import annotations

from alembic import op

revision = "0043"
down_revision = "0042"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # whatsapp_connections.extra_metadata.provider_details.client_id lives in JSONB.
    # Bad values: UI labels / JS sentinels — must not block admin tooling.
    op.execute(
        """
        UPDATE whatsapp_connections
        SET extra_metadata = jsonb_set(
          COALESCE(extra_metadata, '{}'::jsonb),
          '{provider_details,client_id}',
          'null'::jsonb,
          true
        )
        WHERE connection_type = 'coexistence'
          AND (
            lower(trim(COALESCE(extra_metadata->'provider_details'->>'client_id', '')))
              IN ('verify', 'test', 'undefined', 'null')
            OR trim(COALESCE(extra_metadata->'provider_details'->>'client_id', '')) = ''
          );
        """
    )


def downgrade() -> None:
    pass

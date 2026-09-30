"""Prevent duplicate AI charges for the same provider response.

Requires review before production use. Existing duplicates deliberately block
the migration; this revision never deletes or recalculates historical rows.
"""
import sqlalchemy as sa
from alembic import op

revision = "0114"
down_revision = "0113"
branch_labels = None
depends_on = None


def upgrade():
    bind = op.get_bind()
    duplicates = bind.execute(sa.text("""
        SELECT COUNT(*) FROM (
            SELECT provider, request_id FROM ai_usage_events
            WHERE request_id IS NOT NULL
            GROUP BY provider, request_id HAVING COUNT(*) > 1
        ) AS duplicate_requests
    """)).scalar()
    if duplicates:
        raise RuntimeError(
            "AI usage contains duplicate provider responses; review reconciliation before 0114."
        )
    op.create_index("uq_ai_usage_provider_request", "ai_usage_events",
                    ["provider", "request_id"], unique=True)


def downgrade():
    op.drop_index("uq_ai_usage_provider_request", table_name="ai_usage_events")

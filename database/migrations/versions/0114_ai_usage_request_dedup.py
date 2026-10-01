"""Prevent duplicate AI charges for the same provider response.

Requires review before production use. Existing duplicates deliberately block
the migration; this revision never deletes or recalculates historical rows.
"""
from alembic import op

revision = "0114"
down_revision = "0113"
branch_labels = None
depends_on = None


def upgrade():
    from database.ai_usage_dedup_helpers import install_index
    install_index(op.get_bind())


def downgrade():
    op.drop_index("uq_ai_usage_provider_request", table_name="ai_usage_events")

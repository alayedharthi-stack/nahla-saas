"""Record irreversible WhatsApp send attempts on durable Salla webhook rows.

Revision ID: 0125
Revises: 0124
"""
from alembic import op
import sqlalchemy as sa

revision = "0125"
down_revision = "0124"
branch_labels = None
depends_on = None


def upgrade():
    op.create_table(
        "salla_communication_function_nonces",
        sa.Column("nonce", sa.String(32), primary_key=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
    )
    op.add_column("webhook_events", sa.Column("communication_send_state", sa.String(16)))
    op.add_column("webhook_events", sa.Column("communication_provider_message_id", sa.String(128)))
    op.add_column("webhook_events", sa.Column("communication_send_error_code", sa.String(64)))
    op.add_column("webhook_events", sa.Column("communication_send_attempted_at", sa.DateTime(timezone=True)))


def downgrade():
    op.drop_table("salla_communication_function_nonces")
    op.drop_column("webhook_events", "communication_send_attempted_at")
    op.drop_column("webhook_events", "communication_send_error_code")
    op.drop_column("webhook_events", "communication_provider_message_id")
    op.drop_column("webhook_events", "communication_send_state")

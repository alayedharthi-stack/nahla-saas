"""0103 — durable WhatsApp OAuth nonces (hashed, tenant-bound, single-use).

Revision ID: 0103
Revises:    0102

Stores HMAC fingerprints only. Never stores raw nonce, signed state, or IBAN.
Does not merge sibling head 0092. Normal bootstrap remains pinned at 0093.

Apply with ``alembic upgrade 0103`` (not ``head``).
"""
from __future__ import annotations

import sqlalchemy as sa
from alembic import op
from sqlalchemy import inspect

revision = "0103"
down_revision = "0102"
branch_labels = None
depends_on = None

_TABLE = "whatsapp_oauth_nonces"


def upgrade() -> None:
    bind = op.get_bind()
    inspector = inspect(bind)
    if _TABLE in inspector.get_table_names():
        return
    op.create_table(
        _TABLE,
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column("nonce_hash", sa.String(64), nullable=False),
        sa.Column("tenant_id", sa.Integer(), nullable=False),
        sa.Column("connection_mode", sa.String(32), nullable=False),
        sa.Column(
            "redirect_uri_fingerprint",
            sa.String(64),
            nullable=False,
        ),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("consumed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("CURRENT_TIMESTAMP"),
        ),
        sa.ForeignKeyConstraint(["tenant_id"], ["tenants.id"], ondelete="CASCADE"),
        sa.UniqueConstraint("nonce_hash", name="uq_whatsapp_oauth_nonces_hash"),
    )
    op.create_index("ix_whatsapp_oauth_nonces_expires_at", _TABLE, ["expires_at"])
    op.create_index("ix_whatsapp_oauth_nonces_tenant_id", _TABLE, ["tenant_id"])


def downgrade() -> None:
    bind = op.get_bind()
    inspector = inspect(bind)
    if _TABLE not in inspector.get_table_names():
        return
    op.drop_index("ix_whatsapp_oauth_nonces_tenant_id", table_name=_TABLE)
    op.drop_index("ix_whatsapp_oauth_nonces_expires_at", table_name=_TABLE)
    op.drop_table(_TABLE)

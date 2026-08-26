"""0102 — hashed one-time WhatsApp OAuth nonces (Salla branch).

Revision ID: 0102
Revises:    0100

Stores hashed Meta embedded-signup nonces so the same signed state cannot
be consumed twice, including concurrent callbacks.
"""
from __future__ import annotations

import sqlalchemy as sa
from alembic import op
from sqlalchemy import inspect

revision = "0102"
down_revision = "0100"
branch_labels = None
depends_on = None

_TABLE = "whatsapp_oauth_nonces"
_UQ = "uq_whatsapp_oauth_nonces_hash"
_IX_TENANT = "ix_whatsapp_oauth_nonces_tenant_id"
_IX_EXPIRES = "ix_whatsapp_oauth_nonces_expires_at"


def _has_table(bind, name: str) -> bool:
    try:
        return name in inspect(bind).get_table_names()
    except Exception:
        return False


def _has_index(bind, table: str, name: str) -> bool:
    if not _has_table(bind, table):
        return False
    try:
        return any(ix.get("name") == name for ix in inspect(bind).get_indexes(table))
    except Exception:
        return False


def upgrade() -> None:
    bind = op.get_bind()
    if not _has_table(bind, _TABLE):
        op.create_table(
            _TABLE,
            sa.Column("id", sa.Integer(), primary_key=True),
            sa.Column("nonce_hash", sa.String(length=64), nullable=False),
            sa.Column("tenant_id", sa.Integer(), nullable=False),
            sa.Column("connection_mode", sa.String(length=32), nullable=False),
            sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
            sa.Column("consumed_at", sa.DateTime(timezone=True), nullable=True),
            sa.Column("created_at", sa.DateTime(timezone=True), nullable=True),
            sa.ForeignKeyConstraint(
                ["tenant_id"],
                ["tenants.id"],
                name="fk_whatsapp_oauth_nonces_tenant",
                ondelete="CASCADE",
            ),
            sa.UniqueConstraint("nonce_hash", name=_UQ),
        )
    if not _has_index(bind, _TABLE, _IX_TENANT):
        op.create_index(_IX_TENANT, _TABLE, ["tenant_id"])
    if not _has_index(bind, _TABLE, _IX_EXPIRES):
        op.create_index(_IX_EXPIRES, _TABLE, ["expires_at"])


def downgrade() -> None:
    bind = op.get_bind()
    if not _has_table(bind, _TABLE):
        return
    for name in (_IX_EXPIRES, _IX_TENANT):
        if _has_index(bind, _TABLE, name):
            op.drop_index(name, table_name=_TABLE)
    op.drop_table(_TABLE)

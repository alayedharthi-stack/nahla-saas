"""0100 — durable OAuth-verified Salla embedded identity bindings.

Revision ID: 0100
Revises:    0099

Creates ``salla_embedded_identity_bindings`` with partial unique active
identity enforcement and lookup indexes. Revoked history is retained.
"""
from __future__ import annotations

import sqlalchemy as sa
from alembic import op
from sqlalchemy import inspect

revision = "0100"
down_revision = "0099"
branch_labels = None
depends_on = None

_TABLE = "salla_embedded_identity_bindings"
_UQ_ACTIVE = "uq_seib_active_identity"
_IX_LOOKUP = "ix_seib_lookup"
_IX_INTEGRATION = "ix_seib_integration_id"
_IX_TENANT = "ix_seib_tenant_id"
_IX_STORE = "ix_seib_canonical_store_id"


def _has_table(bind, name: str) -> bool:
    try:
        return name in inspect(bind).get_table_names()
    except Exception:
        return False


def _has_index(bind, table: str, name: str) -> bool:
    try:
        return name in {idx.get("name") for idx in inspect(bind).get_indexes(table)}
    except Exception:
        return False


def upgrade() -> None:
    bind = op.get_bind()
    if _has_table(bind, _TABLE):
        return

    op.create_table(
        _TABLE,
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column("provider", sa.String(), nullable=False),
        sa.Column("app_id", sa.String(), nullable=False),
        sa.Column("merchant_account_id", sa.String(), nullable=False),
        sa.Column("canonical_store_id", sa.String(), nullable=False),
        sa.Column("integration_id", sa.Integer(), sa.ForeignKey("integrations.id"), nullable=False),
        sa.Column("tenant_id", sa.Integer(), sa.ForeignKey("tenants.id"), nullable=False),
        sa.Column("verified_via", sa.String(), nullable=False),
        sa.Column("verified_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("status", sa.String(), nullable=False),
        sa.Column("revoked_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("revoked_reason", sa.String(), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
    )

    op.create_index(_IX_LOOKUP, _TABLE, ["provider", "app_id", "merchant_account_id", "status"])
    op.create_index(_IX_INTEGRATION, _TABLE, ["integration_id"])
    op.create_index(_IX_TENANT, _TABLE, ["tenant_id"])
    op.create_index(_IX_STORE, _TABLE, ["canonical_store_id"])
    op.create_index(
        _UQ_ACTIVE,
        _TABLE,
        ["provider", "app_id", "merchant_account_id"],
        unique=True,
        postgresql_where=sa.text("status = 'active'"),
    )


def downgrade() -> None:
    bind = op.get_bind()
    if not _has_table(bind, _TABLE):
        return
    for idx in (_UQ_ACTIVE, _IX_STORE, _IX_TENANT, _IX_INTEGRATION, _IX_LOOKUP):
        if _has_index(bind, _TABLE, idx):
            op.drop_index(idx, table_name=_TABLE)
    op.drop_table(_TABLE)

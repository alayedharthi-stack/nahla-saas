"""0094 — commerce lifecycle notification send audit columns.

Additive send-lifecycle audit fields for cross-worker outbound template
deduplication. Preserves UNIQUE(tenant_id, idempotency_key).
"""
from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0094"
down_revision = "0093"
branch_labels = None
depends_on = None

_TABLE = "commerce_lifecycle_notification_ledger"
_IX_SEND_STATE = "ix_lifecycle_ledger_tenant_send_state"


def _has_table(bind, name: str) -> bool:
    insp = sa.inspect(bind)
    try:
        return name in set(insp.get_table_names())
    except Exception:
        return False


def _has_column(bind, table: str, column: str) -> bool:
    insp = sa.inspect(bind)
    try:
        return column in {c["name"] for c in insp.get_columns(table)}
    except Exception:
        return False


def upgrade() -> None:
    bind = op.get_bind()
    if not _has_table(bind, _TABLE):
        return

    if not _has_column(bind, _TABLE, "send_state"):
        op.add_column(_TABLE, sa.Column("send_state", sa.String(length=32), nullable=True))
    if not _has_column(bind, _TABLE, "template_name"):
        op.add_column(_TABLE, sa.Column("template_name", sa.String(length=128), nullable=True))
    if not _has_column(bind, _TABLE, "template_service_key"):
        op.add_column(_TABLE, sa.Column("template_service_key", sa.String(length=64), nullable=True))
    if not _has_column(bind, _TABLE, "provider_message_id"):
        op.add_column(_TABLE, sa.Column("provider_message_id", sa.String(length=128), nullable=True))
    if not _has_column(bind, _TABLE, "send_attempted_at"):
        op.add_column(_TABLE, sa.Column("send_attempted_at", sa.DateTime(timezone=True), nullable=True))
    if not _has_column(bind, _TABLE, "send_completed_at"):
        op.add_column(_TABLE, sa.Column("send_completed_at", sa.DateTime(timezone=True), nullable=True))
    if not _has_column(bind, _TABLE, "send_error_code"):
        op.add_column(_TABLE, sa.Column("send_error_code", sa.String(length=64), nullable=True))
    if not _has_column(bind, _TABLE, "send_attempt_count"):
        op.add_column(
            _TABLE,
            sa.Column("send_attempt_count", sa.Integer(), nullable=False, server_default="0"),
        )
    if not _has_column(bind, _TABLE, "reclaim_count"):
        op.add_column(
            _TABLE,
            sa.Column("reclaim_count", sa.Integer(), nullable=False, server_default="0"),
        )
    if not _has_column(bind, _TABLE, "send_reserved_at"):
        op.add_column(_TABLE, sa.Column("send_reserved_at", sa.DateTime(timezone=True), nullable=True))
    if not _has_column(bind, _TABLE, "last_reclaimed_at"):
        op.add_column(_TABLE, sa.Column("last_reclaimed_at", sa.DateTime(timezone=True), nullable=True))

    insp = sa.inspect(bind)
    existing_indexes = {idx["name"] for idx in insp.get_indexes(_TABLE)}
    if _IX_SEND_STATE not in existing_indexes:
        op.create_index(_IX_SEND_STATE, _TABLE, ["tenant_id", "send_state"])


def downgrade() -> None:
    bind = op.get_bind()
    if not _has_table(bind, _TABLE):
        return

    insp = sa.inspect(bind)
    existing_indexes = {idx["name"] for idx in insp.get_indexes(_TABLE)}
    if _IX_SEND_STATE in existing_indexes:
        op.drop_index(_IX_SEND_STATE, table_name=_TABLE)

    for column in (
        "last_reclaimed_at",
        "send_reserved_at",
        "reclaim_count",
        "send_attempt_count",
        "send_error_code",
        "send_completed_at",
        "send_attempted_at",
        "provider_message_id",
        "template_service_key",
        "template_name",
        "send_state",
    ):
        if _has_column(bind, _TABLE, column):
            op.drop_column(_TABLE, column)

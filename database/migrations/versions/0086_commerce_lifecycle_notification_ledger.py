"""0086 — commerce lifecycle notification shadow ledger (PR 2B).

Additive table for lifecycle notification idempotency and audit metadata.
No backfill. Downgrade drops only this table and its indexes.
"""
from __future__ import annotations

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "0086"
down_revision = "0085"
branch_labels = None
depends_on = None

_TABLE = "commerce_lifecycle_notification_ledger"
_UQ = "uq_lifecycle_ledger_tenant_idempotency"
_IX_ORDER = "ix_lifecycle_ledger_tenant_order"
_IX_INTENT = "ix_lifecycle_ledger_tenant_intent"


def _has_table(bind, name: str) -> bool:
    insp = sa.inspect(bind)
    try:
        return name in set(insp.get_table_names())
    except Exception:
        return False


def upgrade() -> None:
    bind = op.get_bind()
    if _has_table(bind, _TABLE):
        return

    op.create_table(
        _TABLE,
        sa.Column("id", sa.Integer(), primary_key=True, nullable=False),
        sa.Column("tenant_id", sa.Integer(), sa.ForeignKey("tenants.id", ondelete="CASCADE"), nullable=False),
        sa.Column("order_id", sa.Integer(), nullable=False),
        sa.Column("business_intent", sa.String(length=64), nullable=False),
        sa.Column("channel", sa.String(length=32), nullable=False),
        sa.Column("destination_hash", sa.String(length=128), nullable=True),
        sa.Column("source_event_id", sa.String(length=128), nullable=True),
        sa.Column("transition_version", sa.String(length=64), nullable=True),
        sa.Column("idempotency_key", sa.String(length=512), nullable=False),
        sa.Column("outcome", sa.String(length=64), nullable=False),
        sa.Column("reason_code", sa.String(length=64), nullable=True),
        sa.Column("dispatch_decision_json", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
        sa.Column("capabilities_snapshot_json", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
        sa.Column("evidence_present_json", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
        sa.Column(
            "automation_execution_id",
            sa.Integer(),
            sa.ForeignKey("automation_executions.id", ondelete="SET NULL"),
            nullable=True,
        ),
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
        sa.UniqueConstraint("tenant_id", "idempotency_key", name=_UQ),
    )
    op.create_index(_IX_ORDER, _TABLE, ["tenant_id", "order_id"])
    op.create_index(_IX_INTENT, _TABLE, ["tenant_id", "business_intent"])


def downgrade() -> None:
    bind = op.get_bind()
    if not _has_table(bind, _TABLE):
        return
    op.drop_index(_IX_INTENT, table_name=_TABLE)
    op.drop_index(_IX_ORDER, table_name=_TABLE)
    op.drop_table(_TABLE)

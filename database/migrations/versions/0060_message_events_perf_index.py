"""0060 — message_events composite index for inbox perf.

Revision ID: 0060
Revises:    0059

Why this migration exists
─────────────────────────
The /conversations list endpoint runs three hot subqueries against
``message_events``:

* ``MAX(id) PER conversation_id``  (latest message body / type)
* ``MAX(created_at) PER conversation_id``  (last activity for ordering)
* ``COUNT(*) PER conversation_id WHERE created_at > last_read``  (unread)

All three filter on ``tenant_id`` first, then group/scan by
``conversation_id`` and order/compare by ``created_at``. With only the
default FK indexes (``conversation_id`` alone, ``tenant_id`` alone),
Postgres falls back to a bitmap heap scan + sort even after our 2026-05-13
N+1 fixes, which keeps the wall clock above 400ms for tenants with
thousands of message rows.

A composite index on ``(tenant_id, conversation_id, created_at DESC)``
lets every one of those subqueries use an index-only scan: the planner
walks the index in already-sorted order and pulls MAX/COUNT directly
from the index without touching the heap.

Idempotency
───────────
Same as 0056-0059: the index add is guarded by an inspector check so
re-running on a populated DB is a safe no-op.
"""
from __future__ import annotations

import sqlalchemy as sa
from alembic import op
from sqlalchemy import inspect

revision = "0060"
down_revision = "0059"
branch_labels = None
depends_on = None


def _has_table(bind, table_name: str) -> bool:
    return table_name in inspect(bind).get_table_names()


def _has_index(bind, table_name: str, index_name: str) -> bool:
    if not _has_table(bind, table_name):
        return False
    return any(
        ix["name"] == index_name
        for ix in inspect(bind).get_indexes(table_name)
    )


def upgrade() -> None:
    bind = op.get_bind()
    if not _has_table(bind, "message_events"):
        return

    if not _has_index(bind, "message_events", "ix_msg_events_tenant_conv_created"):
        dialect = bind.dialect.name
        # Postgres supports DESC in the index definition. SQLite ignores
        # the DESC keyword on a composite key here (works either way for
        # ORDER BY DESC LIMIT 1) — same column list still wins.
        if dialect == "postgresql":
            op.execute(
                "CREATE INDEX IF NOT EXISTS ix_msg_events_tenant_conv_created "
                "ON message_events (tenant_id, conversation_id, created_at DESC)"
            )
        else:
            op.create_index(
                "ix_msg_events_tenant_conv_created",
                "message_events",
                ["tenant_id", "conversation_id", "created_at"],
            )


def downgrade() -> None:
    bind = op.get_bind()
    if _has_index(bind, "message_events", "ix_msg_events_tenant_conv_created"):
        op.drop_index(
            "ix_msg_events_tenant_conv_created", "message_events",
        )

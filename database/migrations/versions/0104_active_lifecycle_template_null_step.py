"""0104 — one active lifecycle template per tenant+service_key when step is NULL.

Revision ID: 0104
Revises:    0103

Canonical production lineage parent is 0103 (main). Do not use
``alembic upgrade head`` — sibling historical heads still exist
(e.g. 0092). Apply with ``alembic upgrade 0104``.

Ledger semantic uniqueness remains UNIQUE(tenant_id, idempotency_key)
from 0086/0094. Idempotency_key hashes source_event_id + transition_version
which no longer include observer previous_status.

This revision closes the 0035 gap: that partial unique index only covered
``step_number IS NOT NULL``. Lifecycle slots use ``step_number IS NULL``.

Duplicate precheck fails closed. No production rows are deleted.
"""
from __future__ import annotations

import sqlalchemy as sa
from alembic import op

from migration_inspector_helpers import has_index, has_table

revision = "0104"
down_revision = "0103"
branch_labels = None
depends_on = None

_TABLE = "whatsapp_templates"
_INDEX = "uq_active_lifecycle_template_null_step"
ERROR_DUPLICATES = (
    "0104 refused: duplicate active lifecycle templates exist for "
    "(tenant_id, service_key) with step_number IS NULL. "
    "No rows were deleted."
)


def _duplicate_groups(bind) -> list[tuple]:
    rows = bind.execute(
        sa.text(
            """
            SELECT tenant_id, service_key, COUNT(*) AS n
            FROM whatsapp_templates
            WHERE is_active = true
              AND is_hidden = false
              AND service_key IS NOT NULL
              AND step_number IS NULL
            GROUP BY tenant_id, service_key
            HAVING COUNT(*) > 1
            """
        )
    ).fetchall()
    return [(int(r[0]), str(r[1]), int(r[2])) for r in rows]


def upgrade() -> None:
    bind = op.get_bind()
    if not has_table(bind, _TABLE):
        return
    if has_index(bind, _TABLE, _INDEX):
        return
    duplicates = _duplicate_groups(bind)
    if duplicates:
        raise RuntimeError(f"{ERROR_DUPLICATES} groups={duplicates!r}")
    op.create_index(
        _INDEX,
        _TABLE,
        ["tenant_id", "service_key"],
        unique=True,
        postgresql_where=sa.text(
            "is_active = true AND is_hidden = false "
            "AND service_key IS NOT NULL AND step_number IS NULL"
        ),
    )


def downgrade() -> None:
    bind = op.get_bind()
    if has_index(bind, _TABLE, _INDEX):
        op.drop_index(_INDEX, table_name=_TABLE)

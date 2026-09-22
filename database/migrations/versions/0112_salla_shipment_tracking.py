"""Persist tenant-scoped Salla shipment tracking evidence.

Revision ID: 0112
Revises: 0110

``0111`` is an independent runtime sibling of ``0110``.  This revision stays
on the application schema branch and must be applied explicitly with
``alembic upgrade 0112``; it deliberately does not merge or require the
dormant commerce-runtime branch.
"""
from __future__ import annotations

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import JSONB


revision = "0112"
down_revision = "0110"
branch_labels = None
depends_on = None

_TABLE = "order_shipments"
_UNIQUE = "uq_order_shipments_tenant_tracking_source_ref"


def _columns(bind) -> set[str]:
    return {column["name"] for column in sa.inspect(bind).get_columns(_TABLE)}


def _has_named(bind, name: str) -> bool:
    inspector = sa.inspect(bind)
    entries = inspector.get_unique_constraints(_TABLE)
    return any(entry.get("name") == name for entry in entries)


def upgrade() -> None:
    bind = op.get_bind()
    if _TABLE not in sa.inspect(bind).get_table_names():
        return

    existing = _columns(bind)
    additions = (
        ("tracking_data_source", sa.String()),
        ("external_shipment_id", sa.String()),
        ("carrier", sa.String()),
        ("tracking_url", sa.String()),
        ("latest_event", JSONB()),
        ("source_event_at", sa.DateTime(timezone=True)),
        ("last_verified_at", sa.DateTime(timezone=True)),
    )
    for name, column_type in additions:
        if name not in existing:
            op.add_column(_TABLE, sa.Column(name, column_type, nullable=True))

    if not _has_named(bind, _UNIQUE):
        op.create_unique_constraint(
            _UNIQUE,
            _TABLE,
            ["tenant_id", "tracking_data_source", "external_shipment_id"],
        )


def downgrade() -> None:
    bind = op.get_bind()
    if _TABLE not in sa.inspect(bind).get_table_names():
        return
    if _has_named(bind, _UNIQUE):
        op.drop_constraint(_UNIQUE, _TABLE, type_="unique")
    existing = _columns(bind)
    for name in (
        "last_verified_at", "source_event_at", "latest_event", "tracking_url",
        "carrier", "external_shipment_id", "tracking_data_source",
    ):
        if name in existing:
            op.drop_column(_TABLE, name)

"""Persist tenant-scoped Salla shipment tracking evidence.

Revision ID: 0112
Revises: 0110

``0111`` is an independent runtime sibling of ``0110``.  This revision stays
on the application schema branch and must be applied explicitly with
``alembic upgrade 0112``; it deliberately does not merge or require the
dormant commerce-runtime branch.

Deployment order is migration before code: an ORM built from the tracking
model selects these columns, so it is not compatible with an ``0110`` schema.
This migration verifies the pre-existing 0080 shipment foundation before it
alters anything. Missing or drifted foundations fail closed; it never stamps,
creates, drops, or guesses at a replacement table.
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
_FOUNDATION_UNIQUE = "uq_order_shipments_order_id"
_ERROR_SCHEMA_DRIFT = (
    "order_shipments does not match the required pre-0112 shipment foundation. "
    "No tracking columns, constraints, stamps, or destructive repairs were applied."
)

_FOUNDATION_COLUMNS = {
    "id": (False, "integer"),
    "tenant_id": (False, "integer"),
    "order_id": (False, "integer"),
    "provider": (False, "string"),
    "status": (False, "string"),
    "tracking_number": (True, "string"),
    "label_url": (True, "string"),
    "label_pdf_path": (True, "string"),
    "recipient_name": (True, "string"),
    "recipient_phone": (True, "string"),
    "address_type": (True, "string"),
    "address_text": (True, "text"),
    "address_url": (True, "string"),
    "latitude": (True, "string"),
    "longitude": (True, "string"),
    "cod_amount": (True, "string"),
    "created_at": (True, "datetime"),
    "updated_at": (True, "datetime"),
    "metadata": (True, "jsonb"),
}
_TRACKING_COLUMNS = {
    "tracking_data_source": (True, "string"),
    "external_shipment_id": (True, "string"),
    "carrier": (True, "string"),
    "tracking_url": (True, "string"),
    "latest_event": (True, "jsonb"),
    "source_event_at": (True, "timezone_datetime"),
    "last_verified_at": (True, "timezone_datetime"),
}


def _fail_closed() -> None:
    raise RuntimeError(_ERROR_SCHEMA_DRIFT)


def _is_expected_type(column_type: object, expected: str) -> bool:
    if expected == "integer":
        return isinstance(column_type, sa.Integer)
    if expected == "string":
        return isinstance(column_type, sa.String) and not isinstance(column_type, sa.Text)
    if expected == "text":
        return isinstance(column_type, sa.Text)
    if expected == "jsonb":
        return isinstance(column_type, JSONB)
    if expected == "datetime":
        return isinstance(column_type, sa.DateTime) and not bool(getattr(column_type, "timezone", False))
    if expected == "timezone_datetime":
        return isinstance(column_type, sa.DateTime) and bool(getattr(column_type, "timezone", False))
    return False


def _has_unique_columns(inspector, name: str, columns: tuple[str, ...]) -> bool:
    for entry in inspector.get_unique_constraints(_TABLE):
        if entry.get("name") == name:
            return tuple(entry.get("column_names") or ()) == columns
    return False


def _has_foreign_key(inspector, column: str, referred_table: str) -> bool:
    return any(
        tuple(entry.get("constrained_columns") or ()) == (column,)
        and entry.get("referred_table") == referred_table
        and tuple(entry.get("referred_columns") or ()) == ("id",)
        for entry in inspector.get_foreign_keys(_TABLE)
    )


def _assert_existing_table_matches_foundation(bind) -> None:
    inspector = sa.inspect(bind)
    if _TABLE not in inspector.get_table_names():
        _fail_closed()

    columns = {column["name"]: column for column in inspector.get_columns(_TABLE)}
    for name, (nullable, expected_type) in _FOUNDATION_COLUMNS.items():
        column = columns.get(name)
        if (
            column is None
            or bool(column.get("nullable")) is not nullable
            or not _is_expected_type(column["type"], expected_type)
        ):
            _fail_closed()

    # Startup create_all may have materialized some or all of the new ORM
    # columns before this explicit migration. They are safe only in this exact
    # nullable/type shape; otherwise do not silently adopt drift.
    for name, (nullable, expected_type) in _TRACKING_COLUMNS.items():
        column = columns.get(name)
        if column is not None and (
            bool(column.get("nullable")) is not nullable
            or not _is_expected_type(column["type"], expected_type)
        ):
            _fail_closed()

    if tuple(inspector.get_pk_constraint(_TABLE).get("constrained_columns") or ()) != ("id",):
        _fail_closed()
    if not _has_unique_columns(inspector, _FOUNDATION_UNIQUE, ("order_id",)):
        _fail_closed()
    if not _has_foreign_key(inspector, "tenant_id", "tenants"):
        _fail_closed()
    if not _has_foreign_key(inspector, "order_id", "orders"):
        _fail_closed()
    if any(entry.get("name") == _UNIQUE for entry in inspector.get_unique_constraints(_TABLE)) and not _has_unique_columns(
        inspector, _UNIQUE, ("tenant_id", "tracking_data_source", "external_shipment_id")
    ):
        _fail_closed()


def _has_named(bind, name: str) -> bool:
    return any(entry.get("name") == name for entry in sa.inspect(bind).get_unique_constraints(_TABLE))


def _columns(bind) -> set[str]:
    return {column["name"] for column in sa.inspect(bind).get_columns(_TABLE)}


def upgrade() -> None:
    bind = op.get_bind()
    _assert_existing_table_matches_foundation(bind)

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

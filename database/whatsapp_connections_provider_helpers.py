"""Shared idempotent DDL for whatsapp_connections.provider sibling migrations."""
from __future__ import annotations

import sqlalchemy as sa
from alembic import op

from migration_inspector_helpers import has_column, has_table

_TABLE = "whatsapp_connections"
_COLUMN = "provider"
_DEFAULT = "meta"
_COMPATIBLE_TYPE_NAMES = frozenset(
    {"String", "VARCHAR", "TEXT", "Text", "NVARCHAR", "Unicode", "UnicodeText"}
)


def _column_info(bind, table: str, column: str) -> dict | None:
    insp = sa.inspect(bind)
    try:
        for col in insp.get_columns(table):
            if col.get("name") == column:
                return col
    except Exception:
        return None
    return None


def _is_compatible_string_type(type_name: str | None) -> bool:
    return bool(type_name and type_name in _COMPATIBLE_TYPE_NAMES)


def _backfill_provider_defaults(bind) -> None:
    bind.execute(
        sa.text(
            f"UPDATE {_TABLE} SET {_COLUMN} = :default "
            f"WHERE {_COLUMN} IS NULL OR {_COLUMN} = ''"
        ),
        {"default": _DEFAULT},
    )


def ensure_whatsapp_connections_provider_column() -> None:
    """Add or reconcile ``whatsapp_connections.provider`` to the ORM contract."""
    bind = op.get_bind()
    if not has_table(bind, _TABLE):
        return

    if not has_column(bind, _TABLE, _COLUMN):
        op.add_column(
            _TABLE,
            sa.Column(
                _COLUMN,
                sa.String(),
                nullable=False,
                server_default=_DEFAULT,
            ),
        )
        _backfill_provider_defaults(bind)
        return

    col = _column_info(bind, _TABLE, _COLUMN)
    if col is None:
        raise RuntimeError(f"missing_column_metadata:{_TABLE}.{_COLUMN}")

    type_name = type(col.get("type")).__name__
    if not _is_compatible_string_type(type_name):
        raise RuntimeError(
            f"incompatible_existing_column:{_TABLE}.{_COLUMN}:type={type_name}"
        )

    _backfill_provider_defaults(bind)

    if col.get("nullable", True):
        op.alter_column(
            _TABLE,
            _COLUMN,
            existing_type=sa.String(),
            nullable=False,
            existing_server_default=col.get("default"),
            server_default=_DEFAULT,
        )

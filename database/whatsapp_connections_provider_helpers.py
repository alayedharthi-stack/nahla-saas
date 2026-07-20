"""Shared idempotent DDL for whatsapp_connections.provider sibling migrations."""
from __future__ import annotations

import sqlalchemy as sa
from alembic import op

from migration_inspector_helpers import has_column, has_table

_TABLE = "whatsapp_connections"
_COLUMN = "provider"
_DEFAULT = "meta"


class ProviderSchemaContractError(RuntimeError):
    """Safe migration failure containing schema metadata only."""


def _column_info(bind, table: str, column: str) -> dict | None:
    insp = sa.inspect(bind)
    try:
        for col in insp.get_columns(table):
            if col.get("name") == column:
                return col
    except Exception as exc:
        raise ProviderSchemaContractError(
            f"column_inspection_failed:{table}.{column}"
        ) from exc
    return None


def _normalized_server_default(value: object) -> str | None:
    """Normalize PostgreSQL inspector defaults without evaluating expressions."""
    if value is None:
        return None
    normalized = str(value).strip().lower()
    while normalized.startswith("(") and normalized.endswith(")"):
        normalized = normalized[1:-1].strip()
    if "::" in normalized:
        normalized = normalized.split("::", 1)[0].strip()
    while normalized.startswith("(") and normalized.endswith(")"):
        normalized = normalized[1:-1].strip()
    if len(normalized) >= 2 and normalized[0] == normalized[-1] == "'":
        normalized = normalized[1:-1].replace("''", "'")
    return normalized


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
        raise ProviderSchemaContractError(f"required_table_missing:{_TABLE}")

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
        raise ProviderSchemaContractError(
            f"missing_column_metadata:{_TABLE}.{_COLUMN}"
        )

    current_type = col.get("type")
    type_name = type(current_type).__name__
    if not isinstance(current_type, sa.String):
        raise ProviderSchemaContractError(
            f"incompatible_existing_column:{_TABLE}.{_COLUMN}:type={type_name}"
        )

    _backfill_provider_defaults(bind)

    alter_kwargs: dict[str, object] = {}
    # The ORM uses unbounded String (PostgreSQL VARCHAR). Reconcile other
    # compatible string representations without rewriting or dropping values.
    if type_name.upper() not in {"VARCHAR", "STRING"} or getattr(
        current_type, "length", None
    ) is not None:
        alter_kwargs["type_"] = sa.String()
    if col.get("nullable", True):
        alter_kwargs["nullable"] = False
    if _normalized_server_default(col.get("default")) != _DEFAULT:
        alter_kwargs["server_default"] = _DEFAULT

    if alter_kwargs:
        op.alter_column(
            _TABLE,
            _COLUMN,
            existing_type=current_type,
            existing_nullable=bool(col.get("nullable", True)),
            existing_server_default=col.get("default"),
            **alter_kwargs,
        )

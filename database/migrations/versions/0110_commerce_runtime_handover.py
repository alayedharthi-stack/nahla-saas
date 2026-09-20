"""Commerce runtime handover — durable barrier, worker fleet and deferred inbound.

Three new relations, no change to any existing one:

* ``commerce_runtime_handover_barrier`` — whether a tenant admits new work, and
  on which generation. It replaces a namespaced key inside
  ``tenant_settings.metadata``, a document unrelated writers read, modify and
  write whole; any of them could put back a copy taken before a drain and
  silently reopen it. The runtime's own state now lives where nothing else
  writes.
* ``commerce_runtime_handover_workers`` — one row per process that has evaluated
  a route, carrying the generation and state **it observed**. Retirement is an
  operator act with a name on it; silence never retires a worker.
* ``commerce_runtime_deferred_inbound`` — one accepted inbound nobody has
  finished, with the identity and payload a replay needs. It is written before
  the webhook acknowledges a pilot-scoped message, so an acknowledgement is a
  promise the database can keep.

Dormant by construction
=======================
The three tables belong to ``core.commerce_runtime.models.RuntimeBase``,
deliberately separate from the application ``models.Base``. Production startup
pins ``alembic upgrade 0093`` and materialises only ``models.Base`` through
``create_all`` (``backend/main.py``), so this revision changes no production
database unless an owner applies it explicitly.

Graph
=====
The repository intentionally carries two heads: ``0092`` (A1-Validate branch)
and the integration-bootstrap chain this revision extends (``0108`` → ``0109``
→ ``0110``). Do not use ``alembic upgrade head``. Apply with
``alembic upgrade 0110`` on a database at ``0109``; ``alembic downgrade 0109``
removes everything this revision created.

One source of truth
===================
The tables are created from the package's own metadata rather than from a
second hand-written copy of it, so the revision and
``core.commerce_runtime.handover_models`` cannot drift: there is only one
definition. A pre-existing relation is verified against that same metadata by
definition and refused when it differs — nothing is reconciled silently and
nothing is stamped.

Revision ID: 0110
Revises: 0109
"""

from __future__ import annotations

import os
import sys

from alembic import op
import sqlalchemy as sa

from migration_inspector_helpers import has_table

revision = "0110"
down_revision = "0109"
branch_labels = None
depends_on = None

_BARRIER = "commerce_runtime_handover_barrier"
_WORKERS = "commerce_runtime_handover_workers"
_DEFERRED = "commerce_runtime_deferred_inbound"
# Drop order is the reverse of create order; none of the three references
# another, so only the tenants stub matters and that is never touched here.
_TABLES = (_BARRIER, _WORKERS, _DEFERRED)


class IncompatibleSchema(RuntimeError):
    """A pre-existing relation does not match what this revision declares."""


def _handover_tables():
    """The package's own table objects — the single definition of this schema."""
    for path in (
        os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..", "..", "backend")),
        os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..", "..")),
    ):
        if path not in sys.path:
            sys.path.insert(0, path)
    from core.commerce_runtime.handover_models import HANDOVER_TABLE_OBJECTS  # noqa: PLC0415

    return list(HANDOVER_TABLE_OBJECTS)


def _compiled(column_type, dialect) -> str:
    """The type as this dialect spells it, so declared and reflected compare.

    A declared ``String(16)`` and a reflected ``VARCHAR(16)`` are the same
    column; comparing Python class names would call them different.
    """
    try:
        return str(column_type.compile(dialect=dialect)).upper()
    except Exception:  # noqa: BLE001 - an uncompilable type is compared by name
        return column_type.__class__.__name__.upper()


def _column_shape(table, dialect) -> dict:
    return {
        column.name: (_compiled(column.type, dialect), bool(column.nullable))
        for column in table.columns
    }


def _reflected_shape(bind, name: str) -> dict:
    inspector = sa.inspect(bind)
    return {
        column["name"]: (_compiled(column["type"], bind.dialect),
                         bool(column.get("nullable", True)))
        for column in inspector.get_columns(name)
    }


def _differences(bind, table) -> list:
    """How a pre-existing relation differs from the declared one, by name."""
    declared = _column_shape(table, bind.dialect)
    actual = _reflected_shape(bind, table.name)
    diffs = []
    for column, shape in sorted(declared.items()):
        if column not in actual:
            diffs.append(f"{table.name}.{column} is missing")
        elif actual[column] != shape:
            diffs.append(f"{table.name}.{column} is {actual[column]}, expected {shape}")
    for column in sorted(set(actual) - set(declared)):
        diffs.append(f"{table.name}.{column} is unexpected")
    return diffs


def upgrade() -> None:
    bind = op.get_bind()
    tables = _handover_tables()

    existing = [table for table in tables if has_table(bind, table.name)]
    diffs = []
    for table in existing:
        diffs.extend(_differences(bind, table))
    if diffs:
        raise IncompatibleSchema(
            "0110 refuses to reconcile an incompatible pre-existing schema: " + "; ".join(diffs)
        )

    missing = [table for table in tables if not has_table(bind, table.name)]
    if missing:
        tables[0].metadata.create_all(bind, tables=missing, checkfirst=False)

    remaining = []
    for table in tables:
        if not has_table(bind, table.name):
            remaining.append(f"{table.name} was not created")
        else:
            remaining.extend(_differences(bind, table))
    if remaining:
        raise IncompatibleSchema("0110 post-condition failed: " + "; ".join(remaining))


def downgrade() -> None:
    bind = op.get_bind()
    for name in reversed(_TABLES):
        if has_table(bind, name):
            op.drop_table(name)

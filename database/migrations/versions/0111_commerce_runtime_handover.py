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
This revision is numbered ``0111`` and revises ``0109``, **not** ``0110``.
``0110`` is taken by the customer-address provenance revision on a separate
branch, which also revises ``0109``; two revisions cannot share an id, and
renumbering somebody else's branch is not this change's to do. Taking ``0111``
as a *sibling* of ``0110`` rather than a child keeps both independent: either
can be applied without the other, in either order, and neither PR has to wait
for the other to merge.

The repository therefore carries these heads: ``0092`` (A1-Validate branch),
``0110`` (customer-address provenance, once merged) and ``0111`` (this one).
Do not use ``alembic upgrade head`` — it is ambiguous with more than one head,
which is why every runbook here names its target explicitly. Apply with
``alembic upgrade 0111`` on a database at ``0109`` **or** at ``0110``: the two
siblings are independent, and either order — runtime first, address first, or
both — is a valid applied state.

Rolling back **this revision only** is ``alembic downgrade 0111@-1``. The
branch-qualified spelling matters and was proved on real PostgreSQL rather than
assumed: with both siblings applied, ``alembic downgrade 0109`` and
``alembic downgrade 0111-1`` both resolve to the common ancestor and remove
``0110`` as well — the address provenance table with it — whereas ``0111@-1``
steps one revision back along this branch alone and leaves ``0110`` and its
table exactly as they were.

One source of truth
===================
The tables are created from the package's own metadata rather than from a
second hand-written copy of it, so the revision and
``core.commerce_runtime.handover_models`` cannot drift: there is only one
definition. A pre-existing relation is verified against that same metadata —
columns, **and the primary key, unique constraints, check constraints, foreign
keys and indexes that carry the guarantees, by definition and not by name** —
and refused when it differs. A table with the right columns and no unique index
on the inbound identity would deduplicate nothing while looking correct; one
whose unique constraint has the right name over the wrong columns, or whose
check has the right name and a wider expression, is the same defect wearing the
right label. So the declared table is created in a scratch schema on the same
server, reflected, and compared with the pre-existing relation as PostgreSQL
itself spells both — the reference is never a hand-typed copy of the intent.

Revision ID: 0111
Revises: 0109
"""

from __future__ import annotations

import os
import sys
import uuid

from alembic import op
import sqlalchemy as sa

from migration_inspector_helpers import has_table

revision = "0111"
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


def _normalized_sql(value) -> str:
    """One spelling for an expression — outside its quotes.

    Keywords and identifiers are case-insensitive and whitespace between them
    is not significant, so those are folded. A quoted literal or a quoted
    identifier is neither: ``'open'`` and ``'OPEN'`` are different values and
    ``'a b'`` and ``'a  b'`` are different values, so a check or a predicate
    that differs only inside its quotes is a different guarantee. The
    contents of every quoted segment are kept byte for byte (a doubled quote
    inside it is part of it), and only the SQL around them is normalised.
    """
    text = str(value or "")
    out = []
    i, n = 0, len(text)
    pending_space = False
    while i < n:
        ch = text[i]
        if ch in ("'", '"'):
            quote, j = ch, i + 1
            while j < n:
                if text[j] == quote:
                    if j + 1 < n and text[j + 1] == quote:
                        j += 2          # a doubled quote is part of the value
                        continue
                    break
                j += 1
            if pending_space and out:
                out.append(" ")
            pending_space = False
            out.append(text[i:j + 1])
            i = j + 1
            continue
        if ch.isspace():
            pending_space = True
        else:
            if pending_space and out:
                out.append(" ")
            pending_space = False
            out.append(ch.lower())
        i += 1
    return "".join(out).strip()


def _guarantees(bind, name: str, schema=None) -> dict:
    """The guarantees a relation carries, **by definition**.

    Primary key columns; unique constraints by name and columns; check
    constraints by name and expression; foreign keys by name, columns and
    target; indexes by name, columns, uniqueness and partial predicate. Names
    alone are what a relation created by hand to *look* right would match.
    """
    inspector = sa.inspect(bind)
    primary = inspector.get_pk_constraint(name, schema=schema) or {}
    unique = {str(c["name"]): tuple(c.get("column_names") or ())
              for c in inspector.get_unique_constraints(name, schema=schema) if c.get("name")}
    checks = {str(c["name"]): _normalized_sql(c.get("sqltext"))
              for c in inspector.get_check_constraints(name, schema=schema) if c.get("name")}
    foreign = {}
    for fk in inspector.get_foreign_keys(name, schema=schema):
        key = str(fk.get("name") or f"fk:{','.join(fk.get('constrained_columns') or ())}")
        foreign[key] = (tuple(fk.get("constrained_columns") or ()),
                        str(fk.get("referred_table") or ""),
                        tuple(fk.get("referred_columns") or ()))
    indexes = {}
    for index in inspector.get_indexes(name, schema=schema):
        if not index.get("name"):
            continue
        where = (index.get("dialect_options") or {}).get("postgresql_where")
        indexes[str(index["name"])] = (tuple(index.get("column_names") or ()),
                                       bool(index.get("unique")), _normalized_sql(where))
    return {
        "primary_key": tuple(primary.get("constrained_columns") or ()),
        "unique": unique, "check": checks, "foreign_key": foreign, "index": indexes,
    }


def _reference_guarantees(bind, table) -> dict:
    """The declared table's guarantees, as **this server** spells them.

    The declared definition is created in a scratch schema on the same
    database, reflected with the same inspector, and dropped again — so a
    declared ``state IN ('a', 'b')`` and the pre-existing relation's
    ``((state)::text = ANY (ARRAY[...]))`` are compared in one normal form
    rather than a Python string against PostgreSQL's rewrite of it.
    """
    scratch = f"nahla_0111_ref_{uuid.uuid4().hex[:8]}"
    bind.execute(sa.text(f'CREATE SCHEMA "{scratch}"'))
    try:
        reference = table.to_metadata(sa.MetaData(), schema=scratch)
        reference.create(bind)
        return _guarantees(bind, reference.name, schema=scratch)
    finally:
        bind.execute(sa.text(f'DROP SCHEMA "{scratch}" CASCADE'))


def _differences(bind, table) -> list:
    """How a pre-existing relation differs from the declared one.

    Columns, **and every guarantee by definition**: a relation whose unique
    constraint is named right over the wrong columns would accept the same
    provider message twice; a check with the right name and a wider expression
    would let a disposed row name no disposition; an index with the right name
    and no partial predicate would not be the pending index at all. None of
    those is a compatible schema, so none of them passes here. A guarantee the
    declaration does not make — an extra unique or check constraint — is
    refused as well: a stricter relation refuses rows the runtime writes.
    """
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

    want = _reference_guarantees(bind, table)
    have = _guarantees(bind, table.name)
    if want["primary_key"] != have["primary_key"]:
        diffs.append(f"{table.name} primary key is {have['primary_key']}, "
                     f"expected {want['primary_key']}")
    for kind in ("unique", "check", "foreign_key", "index"):
        for name in sorted(set(want[kind]) - set(have[kind])):
            diffs.append(f"{table.name} is missing the {kind} constraint {name}")
        for name in sorted(set(want[kind]) & set(have[kind])):
            if want[kind][name] != have[kind][name]:
                diffs.append(f"{table.name} {kind} constraint {name} is {have[kind][name]}, "
                             f"expected {want[kind][name]}")
        if kind in ("unique", "check", "foreign_key"):
            for name in sorted(set(have[kind]) - set(want[kind])):
                diffs.append(f"{table.name} carries an undeclared {kind} constraint {name}")
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
            "0111 refuses to reconcile an incompatible pre-existing schema: " + "; ".join(diffs)
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
        raise IncompatibleSchema("0111 post-condition failed: " + "; ".join(remaining))


def downgrade() -> None:
    bind = op.get_bind()
    for name in reversed(_TABLES):
        if has_table(bind, name):
            op.drop_table(name)

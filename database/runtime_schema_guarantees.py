"""Whether a pre-existing relation really carries the guarantees a model declares.

Not "is there a table with this name" — that is
``migration_inspector_helpers``. This answers the harder question a revision has
to ask before it trusts a relation it did not create: **does this relation, by
definition, make every promise the declared model makes, and no promise it does
not?**

A relation whose unique constraint is named right over the wrong columns would
accept a duplicate. A check with the right name and a wider expression would
admit a row the runtime treats as impossible. An index with the right name and
no partial predicate is not that index at all. None of those is a compatible
schema, and none of them passes here.

The comparison is done in one normal form rather than by string equality: the
declared table is created in a scratch schema on the *same server*, reflected
with the *same inspector*, and dropped again — so a declared
``state IN ('a', 'b')`` and a reflected
``((state)::text = ANY (ARRAY[...]))`` are compared as the same guarantee.
Quoted literals are preserved byte for byte while the SQL around them is folded,
because ``'open'`` and ``'OPEN'`` are different values.

Provenance
==========
This logic was written for revision ``0111`` and lives inline there. It is
lifted here so later revisions can hold the same standard instead of re-deriving
a weaker one. ``0111`` is deliberately **not** refactored to import it: it is an
applied revision, and an applied revision's body stays as it ran.

One correction was needed on the way, and only one. ``_reference_guarantees``
copies the declared table into a scratch schema, and SQLAlchemy rewrites the
copy's **foreign-key targets** into that schema as well — where they do not
exist, so the reference cannot be created. ``0111`` never met this because its
three relations declare no foreign keys at all; the first relation that does
(``commerce_runtime_navigation_snapshots``, revision ``0113``) fails outright
without the fix. The referred tables are now left where they are. Nothing else
differs from the original, and a case pins that the rest is unchanged.
"""
from __future__ import annotations

import uuid

import sqlalchemy as sa


class IncompatibleSchema(RuntimeError):
    """A pre-existing relation does not match what a revision declares."""


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


def _referred_tables(table):
    """Every table this one's foreign keys point at, from its own metadata."""
    out, seen = [], set()
    for constraint in table.foreign_key_constraints:
        for element in constraint.elements:
            name = element.target_fullname.rsplit(".", 1)[0]
            if name in seen:
                continue
            seen.add(name)
            target = table.metadata.tables.get(name)
            if target is not None and target is not table:
                out.append(target)
    return out


def _reference_guarantees(bind, table) -> dict:
    """The declared table's guarantees, as **this server** spells them.

    The declared definition is created in a scratch schema on the same
    database, reflected with the same inspector, and dropped again — so a
    declared ``state IN ('a', 'b')`` and the pre-existing relation's
    ``((state)::text = ANY (ARRAY[...]))`` are compared in one normal form
    rather than a Python string against PostgreSQL's rewrite of it.
    """
    scratch = f"nahla_schema_ref_{uuid.uuid4().hex[:8]}"
    bind.execute(sa.text(f'CREATE SCHEMA "{scratch}"'))
    try:
        # A foreign key points at a table that lives where it always lived.
        # Without this, copying the declaration into the scratch schema drags
        # its *targets* in too and the reference cannot be created at all —
        # which is why ``0111`` never needed it: its three relations declare no
        # foreign keys. Returning ``None`` leaves each referred table in the
        # default schema, so the copy carries the real constraint.
        staging = sa.MetaData()
        # The tables this one points at have to be *present* for the copy's
        # foreign keys to resolve, and *absent* from the DDL that follows —
        # they already exist, in the schema they always lived in. So they are
        # copied in unprefixed and simply never created.
        for target in _referred_tables(table):
            target.to_metadata(staging, schema=None,
                               referred_schema_fn=lambda *_a, **_k: None)
        reference = table.to_metadata(
            staging, schema=scratch,
            referred_schema_fn=lambda *_args, **_kw: None)
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


__all__ = ["IncompatibleSchema", "differences"]


# The public name. The underscore-prefixed original is kept above so the lifted
# body stays byte-identical to the revision it came from.
differences = _differences

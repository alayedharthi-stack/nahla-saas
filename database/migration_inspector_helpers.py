"""Shared inspector helpers for F16 idempotent Alembic migrations (0033–0049).

Guards check object presence by name only. They skip DDL when the named
object already exists and do not reconcile semantically inequivalent shapes.
"""
from __future__ import annotations

from sqlalchemy import inspect


def has_table(bind, table_name: str) -> bool:
    return table_name in inspect(bind).get_table_names()


def has_column(bind, table_name: str, column_name: str) -> bool:
    if not has_table(bind, table_name):
        return False
    return any(
        c["name"] == column_name
        for c in inspect(bind).get_columns(table_name)
    )


def has_index(bind, table_name: str, index_name: str) -> bool:
    if not has_table(bind, table_name):
        return False
    return any(
        ix["name"] == index_name
        for ix in inspect(bind).get_indexes(table_name)
    )


def has_unique_constraint(bind, table_name: str, constraint_name: str) -> bool:
    if not has_table(bind, table_name):
        return False
    insp = inspect(bind)
    try:
        for uc in insp.get_unique_constraints(table_name):
            if uc.get("name") == constraint_name:
                return True
    except NotImplementedError:
        for ix in insp.get_indexes(table_name):
            if ix.get("name") == constraint_name and ix.get("unique"):
                return True
    return False

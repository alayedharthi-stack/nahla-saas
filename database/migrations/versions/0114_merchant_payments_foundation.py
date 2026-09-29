"""Dormant tenant-bound marketplace payment references.

Revision ID: 0114
Revises: 0112

This is an explicit sibling of the dormant 0113 runtime branch. Apply only
after checking the target database's actual revision and schema. The metadata
is separate from the startup create_all Base; code deployment cannot create
these tables. This migration is NOT run as part of this PR.
"""
from __future__ import annotations

import os
import sys

import sqlalchemy as sa
from alembic import op

# Alembic is normally invoked from database/, and loads revisions before env.py.
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..", "..")))
from backend.payments.models import PAYMENT_TABLES, PaymentBase


revision = "0114"
down_revision = "0112"
branch_labels = None
depends_on = None


def upgrade() -> None:
    bind = op.get_bind()
    existing = set(sa.inspect(bind).get_table_names())
    overlap = existing.intersection(table.name for table in PAYMENT_TABLES)
    if overlap:
        raise RuntimeError(
            "Merchant payment schema already exists; inspect it before applying 0114: "
            + ", ".join(sorted(overlap))
        )
    PaymentBase.metadata.create_all(bind, tables=list(PAYMENT_TABLES), checkfirst=False)


def downgrade() -> None:
    raise RuntimeError("Financial history must not be dropped by an automatic downgrade")

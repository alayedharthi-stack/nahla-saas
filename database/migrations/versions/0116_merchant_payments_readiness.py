"""Dormant merchant payments readiness tables.

Revision ID: 0116
Revises: 0115

Extends the payments branch only. It creates the four readiness relations
(activation switch, onboarding audit, webhook delivery ledger, settlement
lines) and leaves the six 0115 foundation tables untouched. Like 0115 it is
never part of normal bootstrap (pinned to 0093) and must be applied explicitly,
after a database review, in a target that already carries 0115. Code
deployment cannot create these tables: the metadata is separate from the
startup ``Base``. This migration is NOT run in production by this PR.
"""
from __future__ import annotations

import os
import sys

import sqlalchemy as sa
from alembic import op

# Alembic is normally invoked from database/, and loads revisions before env.py.
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..", "..")))
from backend.payments.models import PAYMENT_READINESS_TABLES, PAYMENT_TABLES, PaymentBase


revision = "0116"
down_revision = "0115"
branch_labels = None
depends_on = None


def upgrade() -> None:
    bind = op.get_bind()
    existing = set(sa.inspect(bind).get_table_names())
    missing_foundation = {table.name for table in PAYMENT_TABLES} - existing
    if missing_foundation:
        raise RuntimeError(
            "Merchant payment foundation (0115) is not present; apply it explicitly first: "
            + ", ".join(sorted(missing_foundation))
        )
    overlap = existing.intersection(table.name for table in PAYMENT_READINESS_TABLES)
    if overlap:
        raise RuntimeError(
            "Merchant payment readiness schema already exists; inspect it before applying 0116: "
            + ", ".join(sorted(overlap))
        )
    PaymentBase.metadata.create_all(bind, tables=list(PAYMENT_READINESS_TABLES), checkfirst=False)


def downgrade() -> None:
    raise RuntimeError("Financial history must not be dropped by an automatic downgrade")

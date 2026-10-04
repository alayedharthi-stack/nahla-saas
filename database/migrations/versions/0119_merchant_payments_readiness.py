"""Dormant merchant payments readiness tables.

Revision ID: 0119
Revises: 0115

First drafted as ``0116``; that id was declared by two other open branches and
is retired (OTO connections are ``0117``, catalog channel retirements ``0118``).
Safety rests on the state Alembic reads, not on deployment history. A
database whose ``alembic_version`` still holds ``0116`` makes ``upgrade``,
``downgrade`` and ``current`` stop with "Can't locate revision identified by
'0116'" before any DDL runs, so it is never read as carrying this revision.
Any existing readiness table (from the former ``0116`` or anything else)
makes this upgrade raise instead of adopting it. Neither case is repaired
here: recovery is an owner decision with explicit authorization, and nothing
in this change writes ``alembic_version``. Apply it to a database only after
a read-only check of that database shows no ``0116`` stamp, the 0115 tables
present and none of the four readiness tables.

Extends the payments branch only. It creates the four readiness relations
(activation switch, onboarding audit, webhook delivery ledger, settlement
lines) and adds one tenant-bound unique index to ``merchant_payment_settlements``
(``uq_mps_tenant_settlement``) when a database created with the original 0115
DDL lacks it; the 0115 tables are otherwise untouched. That index is implied by
the existing ``(provider, environment, provider_settlement_ref)`` key, so it can
never conflict with stored rows. Like 0115 it is
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
from backend.payments.models import (
    PAYMENT_READINESS_TABLES,
    PAYMENT_TABLES,
    MerchantPaymentSettlement,
    PaymentBase,
)


revision = "0119"
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
            "Merchant payment readiness schema already exists; inspect it before applying 0119: "
            + ", ".join(sorted(overlap))
        )
    settlements = MerchantPaymentSettlement.__table__
    tenant_index = next(index for index in settlements.indexes if index.name == "uq_mps_tenant_settlement")
    present = {index["name"] for index in sa.inspect(bind).get_indexes(settlements.name)}
    if tenant_index.name not in present:
        tenant_index.create(bind)
    PaymentBase.metadata.create_all(bind, tables=list(PAYMENT_READINESS_TABLES), checkfirst=False)


def downgrade() -> None:
    raise RuntimeError("Financial history must not be dropped by an automatic downgrade")

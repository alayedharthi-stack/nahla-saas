"""Dormant OTO tenant connection storage. Apply after reviewing database head.

Revision ID: 0117
Revises: 0112

First drafted as ``0116``; that id was declared by two other open branches and
is retired (catalog channel retirements are ``0118``, payments readiness
``0119``).
Safety rests on the state Alembic reads, not on deployment history. A
database whose ``alembic_version`` still holds ``0116`` makes ``upgrade``,
``downgrade`` and ``current`` stop with "Can't locate revision identified by
'0116'" before any DDL runs, so it is never read as carrying this revision.
An existing ``oto_connections`` table (from the former ``0116`` or anything
else) makes this upgrade raise instead of adopting it. Neither case is
repaired here: recovery is an owner decision with explicit authorization, and
nothing in this change writes ``alembic_version``. Normal bootstrap (pinned
to 0093) never applies this revision. Apply it to a database only after a
read-only check of that database shows no ``0116`` stamp and no
``oto_connections`` table.
"""
from __future__ import annotations

import os
import sys

import sqlalchemy as sa
from alembic import op

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..", "..")))
from backend.oto.models import OtoBase, OtoConnection

revision = "0117"
down_revision = "0112"
branch_labels = None
depends_on = None


def upgrade() -> None:
    bind = op.get_bind()
    if OtoConnection.__tablename__ in sa.inspect(bind).get_table_names():
        raise RuntimeError("OTO connection table already exists; inspect before migration")
    OtoBase.metadata.create_all(bind, tables=[OtoConnection.__table__], checkfirst=False)


def downgrade() -> None:
    raise RuntimeError("OTO credentials require an explicit reviewed removal")

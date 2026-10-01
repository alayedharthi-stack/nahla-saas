"""Dormant OTO tenant connection storage. Apply after reviewing database head.

Revision ID: 0116
Revises: 0112
"""
from __future__ import annotations

import os
import sys

import sqlalchemy as sa
from alembic import op

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..", "..")))
from backend.oto.models import OtoBase, OtoConnection

revision = "0116"
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

"""0101 — one active local product row ↔ one Meta item.

Revision ID: 0101
Revises:    0100

Partial unique index on ``products (tenant_id, meta_item_id)`` for
``catalog_status = 'active'`` rows with a non-empty ``meta_item_id``.

Historical statuses may keep the same Meta id. If any active duplicate
group exists, upgrade fails with DUPLICATE_ACTIVE_META_BINDING_BLOCKED
and does not delete, merge, or pick a winner.

Apply with ``alembic upgrade 0101`` (not ``head``; ``0092`` remains a
sibling head). Normal bootstrap stays pinned at ``0093``.
"""
from __future__ import annotations

import sys
from pathlib import Path

import sqlalchemy as sa
from alembic import op
from sqlalchemy import inspect

_DATABASE_DIR = Path(__file__).resolve().parents[2]
if str(_DATABASE_DIR) not in sys.path:
    sys.path.insert(0, str(_DATABASE_DIR))

from sqlalchemy.exc import IntegrityError

from catalog_meta_item_uniqueness import (  # noqa: E402
    CREATE_UQ_PRODUCTS_ACTIVE_TENANT_META_ITEM_SQL,
    DROP_UQ_PRODUCTS_ACTIVE_TENANT_META_ITEM_SQL,
    ERROR_DUPLICATE_ACTIVE_META_BINDING_BLOCKED,
    UQ_PRODUCTS_ACTIVE_TENANT_META_ITEM,
    raise_if_duplicate_active_meta_bindings,
)

revision = "0101"
down_revision = "0100"
branch_labels = None
depends_on = None


def _has_index(bind, table: str, name: str) -> bool:
    try:
        insp = inspect(bind)
        return name in {idx.get("name") for idx in insp.get_indexes(table)}
    except Exception:
        return False


def upgrade() -> None:
    bind = op.get_bind()
    # Hold writers for the audit + CREATE INDEX window so a concurrent
    # insert cannot replace DUPLICATE_ACTIVE_META_BINDING_BLOCKED with
    # an unbranded unique_violation.
    op.execute(sa.text("LOCK TABLE products IN SHARE ROW EXCLUSIVE MODE"))
    raise_if_duplicate_active_meta_bindings(bind)
    if _has_index(bind, "products", UQ_PRODUCTS_ACTIVE_TENANT_META_ITEM):
        return
    try:
        op.execute(sa.text(CREATE_UQ_PRODUCTS_ACTIVE_TENANT_META_ITEM_SQL))
    except IntegrityError as exc:
        raise RuntimeError(
            ERROR_DUPLICATE_ACTIVE_META_BINDING_BLOCKED
            + "\nUnique index creation failed. "
            "No delete, merge, or winner selection was performed."
        ) from exc


def downgrade() -> None:
    op.execute(sa.text(DROP_UQ_PRODUCTS_ACTIVE_TENANT_META_ITEM_SQL))

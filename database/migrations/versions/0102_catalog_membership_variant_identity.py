"""0102 — unique catalog identity per Salla variant and Meta item.

Revision ID: 0102
Revises:    0101

Adds ``salla_variant_id`` on ``meta_catalog_memberships`` and partial
unique indexes that prevent:

- two active memberships sharing (tenant, catalog, product, salla_variant_id)
- two active memberships sharing (tenant, catalog, meta_item_id)

Existing ``uq_meta_catalog_memberships_tenant_catalog_retailer`` already
covers retailer_id. Upgrade fails closed on duplicates; no delete/merge.

Apply with ``alembic upgrade 0102`` (not ``head``).
"""
from __future__ import annotations

import sys
from pathlib import Path

import sqlalchemy as sa
from alembic import op
from sqlalchemy import inspect
from sqlalchemy.exc import IntegrityError

_DATABASE_DIR = Path(__file__).resolve().parents[2]
if str(_DATABASE_DIR) not in sys.path:
    sys.path.insert(0, str(_DATABASE_DIR))

from catalog_membership_uniqueness import (  # noqa: E402
    CREATE_UQ_MEMBERSHIP_META_ITEM_SQL,
    CREATE_UQ_MEMBERSHIP_VARIANT_KEY_SQL,
    DROP_UQ_MEMBERSHIP_META_ITEM_SQL,
    DROP_UQ_MEMBERSHIP_VARIANT_KEY_SQL,
    ERROR_DUPLICATE_CATALOG_IDENTITY_BLOCKED,
    UQ_MEMBERSHIP_META_ITEM,
    UQ_MEMBERSHIP_VARIANT_KEY,
    raise_if_duplicate_catalog_identities,
)

revision = "0102"
down_revision = "0101"
branch_labels = None
depends_on = None

_TABLE = "meta_catalog_memberships"


def _has_column(bind, table: str, name: str) -> bool:
    try:
        return name in {col["name"] for col in inspect(bind).get_columns(table)}
    except Exception:
        return False


def _has_index(bind, table: str, name: str) -> bool:
    try:
        return name in {idx.get("name") for idx in inspect(bind).get_indexes(table)}
    except Exception:
        return False


def upgrade() -> None:
    bind = op.get_bind()
    if not _has_column(bind, _TABLE, "salla_variant_id"):
        op.add_column(
            _TABLE,
            sa.Column("salla_variant_id", sa.String(length=64), nullable=True),
        )
    op.execute(sa.text(f"LOCK TABLE {_TABLE} IN SHARE ROW EXCLUSIVE MODE"))
    raise_if_duplicate_catalog_identities(bind)
    if not _has_index(bind, _TABLE, UQ_MEMBERSHIP_VARIANT_KEY):
        try:
            op.execute(sa.text(CREATE_UQ_MEMBERSHIP_VARIANT_KEY_SQL))
        except IntegrityError as exc:
            raise RuntimeError(
                ERROR_DUPLICATE_CATALOG_IDENTITY_BLOCKED
                + "\nUnique index on salla_variant_id failed. "
                "No delete, merge, or winner selection was performed."
            ) from exc
    if not _has_index(bind, _TABLE, UQ_MEMBERSHIP_META_ITEM):
        try:
            op.execute(sa.text(CREATE_UQ_MEMBERSHIP_META_ITEM_SQL))
        except IntegrityError as exc:
            raise RuntimeError(
                ERROR_DUPLICATE_CATALOG_IDENTITY_BLOCKED
                + "\nUnique index on meta_item_id failed. "
                "No delete, merge, or winner selection was performed."
            ) from exc


def downgrade() -> None:
    op.execute(sa.text(DROP_UQ_MEMBERSHIP_META_ITEM_SQL))
    op.execute(sa.text(DROP_UQ_MEMBERSHIP_VARIANT_KEY_SQL))
    bind = op.get_bind()
    if _has_column(bind, _TABLE, "salla_variant_id"):
        op.drop_column(_TABLE, "salla_variant_id")

"""0084 — Partial unique index on products (tenant_id, external_id).

Canonical Catalog Mapping PR #4. Enforces one non-empty store product id per
tenant at the DB level. NULL and empty ``external_id`` rows are excluded so
manual products without a platform id remain valid.

Pre-migration audit (``scripts/audit_catalog_product_identity_readonly.py``)
confirmed production has zero duplicate groups and zero empty-string ids.

Revision ID: 0084
Revises: 0083
Create Date: 2026-07-06
"""
from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0084"
down_revision = "0083"
branch_labels = None
depends_on = None

_INDEX_NAME = "uq_products_tenant_external_id_nonempty"


def upgrade() -> None:
    bind = op.get_bind()
    bind.execute(
        sa.text(
            f"""
            CREATE UNIQUE INDEX IF NOT EXISTS {_INDEX_NAME}
            ON products (tenant_id, external_id)
            WHERE external_id IS NOT NULL AND external_id != ''
            """
        )
    )


def downgrade() -> None:
    op.execute(f"DROP INDEX IF EXISTS {_INDEX_NAME}")

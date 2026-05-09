"""0050 — manual coupons + AI media library tables.

Two independent libraries used by the merchant brain when the
automatic coupons engine is off, the store isn't connected to Salla,
or the merchant is selling manually over WhatsApp only.

* ``manual_coupons``: merchant-curated coupon codes the brain can
  cite verbatim. Replaces the single ``manual_coupon_code`` knob.
* ``ai_media_library``: merchant-uploaded images / videos / PDFs /
  documents / audio that the brain can attach to its reply (e.g.
  bank-transfer barcode, product photos, shipping policy graphic).

Revision ID: 0050
Revises: 0049
"""
from __future__ import annotations

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "0050"
down_revision = "0049"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "manual_coupons",
        sa.Column("id", sa.Integer, primary_key=True),
        sa.Column("tenant_id", sa.Integer, sa.ForeignKey("tenants.id", ondelete="CASCADE"), nullable=False, index=True),
        sa.Column("code", sa.String(length=64), nullable=False),
        sa.Column("title", sa.String(length=255), nullable=True),
        sa.Column("description", sa.Text, nullable=True),
        sa.Column("discount_text", sa.String(length=255), nullable=True),
        sa.Column("usage_context", sa.Text, nullable=True),
        sa.Column("is_active", sa.Boolean, nullable=False, server_default="true"),
        sa.Column("priority", sa.Integer, nullable=False, server_default="100"),
        sa.Column("starts_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
    )
    op.create_index(
        "ix_manual_coupons_tenant_active_priority",
        "manual_coupons",
        ["tenant_id", "is_active", "priority"],
    )
    op.create_unique_constraint(
        "uq_manual_coupons_tenant_code",
        "manual_coupons",
        ["tenant_id", "code"],
    )

    op.create_table(
        "ai_media_library",
        sa.Column("id", sa.Integer, primary_key=True),
        sa.Column("tenant_id", sa.Integer, sa.ForeignKey("tenants.id", ondelete="CASCADE"), nullable=False, index=True),
        sa.Column("title", sa.String(length=255), nullable=False),
        sa.Column("description", sa.Text, nullable=True),
        # image | video | pdf | document | audio
        sa.Column("media_type", sa.String(length=32), nullable=False, server_default="image"),
        sa.Column("file_url", sa.Text, nullable=False),
        sa.Column("thumbnail_url", sa.Text, nullable=True),
        # Used by the brain to decide WHEN to attach this media.
        sa.Column("usage_context", sa.Text, nullable=True),
        # Tags as a JSON array; queryable via the API for the merchant
        # filter panel and used by the brain for retrieval.
        sa.Column("tags", postgresql.JSONB(astext_type=sa.Text()), nullable=False, server_default="[]"),
        sa.Column("is_active", sa.Boolean, nullable=False, server_default="true"),
        sa.Column("priority", sa.Integer, nullable=False, server_default="100"),
        # Bookkeeping for files that were uploaded via the multipart
        # endpoint (vs. an externally-hosted URL the merchant pasted).
        sa.Column("storage_kind", sa.String(length=16), nullable=False, server_default="external"),
        sa.Column("storage_path", sa.Text, nullable=True),
        sa.Column("mime_type", sa.String(length=128), nullable=True),
        sa.Column("file_size_bytes", sa.BigInteger, nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
    )
    op.create_index(
        "ix_ai_media_library_tenant_active_priority",
        "ai_media_library",
        ["tenant_id", "is_active", "priority"],
    )


def downgrade() -> None:
    op.drop_index("ix_ai_media_library_tenant_active_priority", table_name="ai_media_library")
    op.drop_table("ai_media_library")
    op.drop_constraint("uq_manual_coupons_tenant_code", "manual_coupons", type_="unique")
    op.drop_index("ix_manual_coupons_tenant_active_priority", table_name="manual_coupons")
    op.drop_table("manual_coupons")

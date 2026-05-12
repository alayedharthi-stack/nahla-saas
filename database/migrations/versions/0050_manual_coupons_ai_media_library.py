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

Idempotency (F16)
─────────────────
Some production databases were built before Alembic was wired into
the deploy path — tables were created via the legacy
``Base.metadata.create_all()`` at startup. As a result the deployed
schema can already contain ``manual_coupons`` / ``ai_media_library``
while ``alembic_version`` is still at 0049. A naive
``op.create_table`` then raises ``DuplicateTable`` and aborts the
whole upgrade chain.

Every ``create_table`` / ``create_index`` / ``create_unique_constraint``
in this revision is now guarded by an inspector check — the
operation is skipped when the object already exists. The behaviour
on a clean database is unchanged.
"""
from __future__ import annotations

import sqlalchemy as sa
from alembic import op
from sqlalchemy import inspect
from sqlalchemy.dialects import postgresql

revision = "0050"
down_revision = "0049"
branch_labels = None
depends_on = None


# ── Inspector helpers ───────────────────────────────────────────────
# All ``op.create_*`` calls in this migration are wrapped by these
# small helpers so re-running on a partially-migrated DB is safe.

def _has_table(bind, table_name: str) -> bool:
    return table_name in inspect(bind).get_table_names()


def _has_index(bind, table_name: str, index_name: str) -> bool:
    if not _has_table(bind, table_name):
        return False
    return any(
        ix["name"] == index_name
        for ix in inspect(bind).get_indexes(table_name)
    )


def _has_unique_constraint(bind, table_name: str, constraint_name: str) -> bool:
    if not _has_table(bind, table_name):
        return False
    insp = inspect(bind)
    try:
        cons = insp.get_unique_constraints(table_name)
    except NotImplementedError:
        return False
    return any(c.get("name") == constraint_name for c in cons)


def upgrade() -> None:
    bind = op.get_bind()

    if not _has_table(bind, "manual_coupons"):
        # Declare the UNIQUE constraint inline rather than via a
        # subsequent ``op.create_unique_constraint`` call so SQLite
        # (used by the test suite) doesn't need ALTER TABLE — SQLite
        # has no ALTER for constraints.  On Postgres both approaches
        # produce the same physical schema.
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
            sa.UniqueConstraint("tenant_id", "code", name="uq_manual_coupons_tenant_code"),
        )

    if not _has_index(bind, "manual_coupons", "ix_manual_coupons_tenant_active_priority"):
        op.create_index(
            "ix_manual_coupons_tenant_active_priority",
            "manual_coupons",
            ["tenant_id", "is_active", "priority"],
        )

    # Cover the drift case where the table exists from a legacy
    # ``Base.metadata.create_all()`` build that didn't include the
    # unique constraint. On Postgres this is a fast catalog op; on
    # SQLite the wrapper handles ALTER via batch mode.
    if (
        _has_table(bind, "manual_coupons")
        and not _has_unique_constraint(bind, "manual_coupons", "uq_manual_coupons_tenant_code")
        and bind.dialect.name != "sqlite"
    ):
        op.create_unique_constraint(
            "uq_manual_coupons_tenant_code",
            "manual_coupons",
            ["tenant_id", "code"],
        )

    if not _has_table(bind, "ai_media_library"):
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
            # ``with_variant`` keeps Postgres on JSONB (indexable, GIN-friendly)
            # while letting SQLite (test suite) render it as JSON/TEXT —
            # without this the migration can't run on the in-memory test DB.
            sa.Column(
                "tags",
                sa.JSON().with_variant(
                    postgresql.JSONB(astext_type=sa.Text()),
                    "postgresql",
                ),
                nullable=False,
                server_default="[]",
            ),
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

    if not _has_index(bind, "ai_media_library", "ix_ai_media_library_tenant_active_priority"):
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

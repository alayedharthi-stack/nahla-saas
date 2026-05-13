"""0059 — AI media library: add ``media_key`` for namespaced lookup.

Revision ID: 0059
Revises:    0058

Why this migration exists
─────────────────────────
The AI media library has shipped for months with id-based markers
(``[MEDIA:<id>]``) + relevance ranking against ``tags`` + ``title``
+ ``usage_context``. That works fine for ad-hoc uploads ("صورة
الموقع") but breaks down for assets whose meaning is *stable
across every merchant* — payment barcodes, QR codes, usage
videos, certificates. We want the AI to be able to say "send the
Rajhi barcode for this tenant" without having to guess which row
id that is per tenant.

This migration adds an optional ``media_key`` column scoped per
tenant. When set:
  * the resolver looks it up before falling back to relevance,
  * the LLM emits stable markers like
    ``[MEDIA_KEY:payment_rajhi_barcode]`` instead of brittle ids,
  * the merchant UI surfaces a dropdown of registry suggestions
    (see ``backend/services/media_key_registry.py``).

When NULL: the legacy behaviour is preserved exactly — the row is
still findable via id markers or relevance scoring against tags.

Idempotency
───────────
Same convention as 0056-0058: every column / index add is guarded
by an inspector check so re-running the migration on a populated
DB is a safe no-op.
"""
from __future__ import annotations

import sqlalchemy as sa
from alembic import op
from sqlalchemy import inspect

revision = "0059"
down_revision = "0058"
branch_labels = None
depends_on = None


def _has_table(bind, table_name: str) -> bool:
    return table_name in inspect(bind).get_table_names()


def _has_column(bind, table_name: str, column_name: str) -> bool:
    if not _has_table(bind, table_name):
        return False
    return any(
        c["name"] == column_name
        for c in inspect(bind).get_columns(table_name)
    )


def _has_index(bind, table_name: str, index_name: str) -> bool:
    if not _has_table(bind, table_name):
        return False
    return any(
        ix["name"] == index_name
        for ix in inspect(bind).get_indexes(table_name)
    )


def upgrade() -> None:
    bind = op.get_bind()

    if not _has_column(bind, "ai_media_library", "media_key"):
        op.add_column(
            "ai_media_library",
            sa.Column("media_key", sa.String(length=64), nullable=True),
        )

    # Tenant-scoped uniqueness ONLY when media_key is set. NULLs
    # remain allowed for legacy / unkeyed rows so we don't have to
    # backfill anything. Postgres + SQLite both support partial
    # indexes via the dialect-specific kwargs.
    if not _has_index(bind, "ai_media_library", "ix_ai_media_library_tenant_media_key"):
        dialect = bind.dialect.name
        kwargs = {}
        if dialect == "postgresql":
            kwargs["postgresql_where"] = sa.text("media_key IS NOT NULL")
        elif dialect == "sqlite":
            kwargs["sqlite_where"] = sa.text("media_key IS NOT NULL")
        op.create_index(
            "ix_ai_media_library_tenant_media_key",
            "ai_media_library",
            ["tenant_id", "media_key"],
            unique=True,
            **kwargs,
        )


def downgrade() -> None:
    bind = op.get_bind()
    if _has_index(bind, "ai_media_library", "ix_ai_media_library_tenant_media_key"):
        op.drop_index(
            "ix_ai_media_library_tenant_media_key", "ai_media_library",
        )
    if _has_column(bind, "ai_media_library", "media_key"):
        op.drop_column("ai_media_library", "media_key")

"""Widen ai_quality_events to also record pre-brain inbound drops + webhook
routing failures + dispatcher exceptions, not just brain-side answer-alignment
mismatches.

Why
───
``ai_quality_events`` was originally written for ONE thing: the post-compose
answer-alignment check inside ``modules/ai/brain/postprocess/answer_alignment``.
Production showed two gaps:

  1. The owner-dashboard panel "مراقبة جودة الذكاء" was showing all-zeros even
     when Tenant 33 was genuinely losing inbound messages (religious / video /
     reaction). The brain never sees those drops, so it never writes a row.
  2. We had no audit trail at all for the silent-drop sites in
     ``routers/whatsapp_webhook.py`` — ``INBOUND_IGNORED_UNSUPPORTED`` (line
     2661), ``INBOUND_IGNORED_EMPTY_TEXT`` (line 2716), the 5
     ``[UNROUTED_D360_WEBHOOK]`` branches (lines 1088-1330), the pre-brain
     handoff drop (line 3686), and the outer-exception path (line 7056). All
     of these only logged to Railway and disappeared on the next redeploy.

This migration is additive and reversible:

  * ``category`` column with default ``'ai_mismatch'`` so existing rows stay
    classified as "brain-side mismatch" without any backfill.
  * Composite index ``(tenant_id, category, created_at DESC)`` to power the
    owner dashboard's per-category tabs cheaply (the dashboard always filters
    by tenant_id + category, then orders by created_at).

The recorder lives in ``core.inbound_observability`` and the wiring is in
``routers.whatsapp_webhook``. The dashboard reader is
``routers.admin_ai_quality`` which now accepts ``?category=`` to gate each
tab. None of those layers depend on this migration *running* — they all
default the new column to ``'ai_mismatch'`` when reading older rows.

Revision ID: 0070
Revises: 0069
Create Date: 2026-05-22
"""
from alembic import op
import sqlalchemy as sa


revision = "0070"
down_revision = "0069"
branch_labels = None
depends_on = None


# ── Idempotency helpers (mirror 0066 / 0069 conventions) ────────────────────


def _has_column(bind, table: str, column: str) -> bool:
    insp = sa.inspect(bind)
    try:
        return any(c.get("name") == column for c in insp.get_columns(table))
    except Exception:
        return False


def _has_index(bind, table: str, index_name: str) -> bool:
    insp = sa.inspect(bind)
    try:
        return any(ix.get("name") == index_name for ix in insp.get_indexes(table))
    except Exception:
        return False


def upgrade() -> None:
    bind = op.get_bind()

    # The column. Server default keeps legacy rows pinned to the original
    # use-case so the dashboard's "AI mismatches" tab continues to surface
    # them without an UPDATE pass.
    if not _has_column(bind, "ai_quality_events", "category"):
        op.add_column(
            "ai_quality_events",
            sa.Column(
                "category",
                sa.String(length=32),
                nullable=False,
                server_default=sa.text("'ai_mismatch'"),
            ),
        )

    # The hot path the dashboard runs on every render: per-tenant, per-category,
    # newest-first. Without this the tab would scan the whole table.
    if not _has_index(bind, "ai_quality_events", "ix_aiq_tenant_category_created"):
        op.create_index(
            "ix_aiq_tenant_category_created",
            "ai_quality_events",
            ["tenant_id", "category", "created_at"],
        )


def downgrade() -> None:
    bind = op.get_bind()
    if _has_index(bind, "ai_quality_events", "ix_aiq_tenant_category_created"):
        op.drop_index(
            "ix_aiq_tenant_category_created",
            table_name="ai_quality_events",
        )
    if _has_column(bind, "ai_quality_events", "category"):
        op.drop_column("ai_quality_events", "category")

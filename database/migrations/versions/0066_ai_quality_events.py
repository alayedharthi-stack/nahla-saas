"""Add ai_quality_events table — AI Quality Monitor (May 2026 #12).

Append-only audit trail of answer-alignment mismatches surfaced by
``modules.ai.brain.postprocess.answer_alignment.check_alignment``.
Each row corresponds to a single ``[ALIGN_MISMATCH]`` log line and
powers the in-product "AI Quality Monitor" admin dashboard.

Privacy: ``customer_phone_masked`` stores a masked form
(``+9665***430``). ``inbound_preview`` / ``reply_preview`` are bounded
to 200 chars. Full bodies stay on ``message_events``.

Revision ID: 0066
Revises: 0065
Create Date: 2026-05-21
"""
from alembic import op
import sqlalchemy as sa


revision = "0066"
down_revision = "0065"
branch_labels = None
depends_on = None


# ── Idempotency helpers (mirror 0065 conventions) ───────────────────────────


def _has_table(bind, name: str) -> bool:
    insp = sa.inspect(bind)
    try:
        return name in set(insp.get_table_names())
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
    if not _has_table(bind, "ai_quality_events"):
        op.create_table(
            "ai_quality_events",
            sa.Column("id",                       sa.Integer(),  primary_key=True),
            sa.Column("tenant_id",                sa.Integer(),
                      sa.ForeignKey("tenants.id"), nullable=False),
            sa.Column("conversation_id",          sa.Integer(),
                      sa.ForeignKey("conversations.id"), nullable=True),
            # ── Privacy-safe identifiers ────────────────────────────
            sa.Column("customer_phone_masked",    sa.String(),   nullable=False),
            # ── Mismatch classification ─────────────────────────────
            sa.Column("mismatch_type",            sa.String(),   nullable=False),
            sa.Column("mismatch_reason",          sa.Text(),     nullable=True),
            # ── Brain context snapshot ──────────────────────────────
            sa.Column("detected_intent",          sa.String(),   nullable=True),
            sa.Column("social_category",          sa.String(),   nullable=True),
            sa.Column("action_taken",             sa.String(),   nullable=True),
            sa.Column("chosen_path",              sa.String(),   nullable=True),
            sa.Column("fallback_used",            sa.Boolean(),
                      nullable=True, server_default=sa.text("false")),
            sa.Column("order_status",             sa.String(),   nullable=True),
            sa.Column("awaiting_payment_receipt", sa.Boolean(),
                      nullable=True, server_default=sa.text("false")),
            sa.Column("model_used",               sa.String(),   nullable=True),
            sa.Column("turn",                     sa.Integer(),  nullable=True),
            # ── Truncated content (privacy-safe) ────────────────────
            sa.Column("inbound_preview",          sa.Text(),     nullable=True),
            sa.Column("reply_preview",            sa.Text(),     nullable=True),
            # ── Validator outcome ───────────────────────────────────
            sa.Column("alignment_passed",         sa.Boolean(),
                      nullable=False, server_default=sa.text("false")),
            sa.Column("regen_fired",              sa.Boolean(),
                      nullable=False, server_default=sa.text("false")),
            # ── Operator triage state ───────────────────────────────
            sa.Column("resolved_status",          sa.String(),
                      nullable=False, server_default=sa.text("'open'")),
            sa.Column("resolved_by",              sa.String(),   nullable=True),
            sa.Column("resolved_at",              sa.DateTime(), nullable=True),
            sa.Column("resolved_note",            sa.Text(),     nullable=True),
            # ── Append-only timestamps ──────────────────────────────
            sa.Column("created_at",               sa.DateTime(),
                      nullable=False, server_default=sa.text("NOW()")),
        )

    # Indexes — every filter the admin dashboard / scheduler needs.
    for index_name, columns in (
        ("ix_ai_quality_events_tenant_id",        ["tenant_id"]),
        ("ix_ai_quality_events_conversation_id",  ["conversation_id"]),
        ("ix_ai_quality_events_phone_masked",     ["customer_phone_masked"]),
        ("ix_ai_quality_events_mismatch_type",    ["mismatch_type"]),
        ("ix_ai_quality_events_resolved_status",  ["resolved_status"]),
        ("ix_ai_quality_events_created_at",       ["created_at"]),
        ("ix_ai_quality_events_tenant_created",   ["tenant_id", "created_at"]),
        ("ix_ai_quality_events_tenant_mismatch",  ["tenant_id", "mismatch_type"]),
    ):
        if not _has_index(bind, "ai_quality_events", index_name):
            op.create_index(index_name, "ai_quality_events", columns)


def downgrade() -> None:
    bind = op.get_bind()
    for index_name in (
        "ix_ai_quality_events_tenant_mismatch",
        "ix_ai_quality_events_tenant_created",
        "ix_ai_quality_events_created_at",
        "ix_ai_quality_events_resolved_status",
        "ix_ai_quality_events_mismatch_type",
        "ix_ai_quality_events_phone_masked",
        "ix_ai_quality_events_conversation_id",
        "ix_ai_quality_events_tenant_id",
    ):
        if _has_index(bind, "ai_quality_events", index_name):
            op.drop_index(index_name, table_name="ai_quality_events")
    if _has_table(bind, "ai_quality_events"):
        op.drop_table("ai_quality_events")

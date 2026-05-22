"""Add merchant knowledge drafts — Phase 2.

Stores the GPT classifier's proposed split of a free-form merchant
quick-update into structured sections, **before** the merchant has
clicked Approve. Each draft row carries:

* the raw input text + attached media ids,
* the proposed ops JSON (create / update / merge / link_media),
* the detected conflicts JSON (e.g. proposed price contradicts the
  Salla snapshot — the dashboard greys out approval for those ops),
* a lifecycle status (``pending`` → ``approved`` | ``rejected``).

Approved drafts are applied row-by-row to the existing
``merchant_knowledge_sections`` table (Phase 1) — no schema changes
needed there. We keep the draft row around (status=``approved``) for
audit / undo so the merchant can see "how did this get classified?".

Revision ID: 0068
Revises: 0067
Create Date: 2026-05-22
"""
from alembic import op
import sqlalchemy as sa


revision = "0068"
down_revision = "0067"
branch_labels = None
depends_on = None


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


def _jsonb():
    bind = op.get_bind()
    if bind.dialect.name == "postgresql":
        from sqlalchemy.dialects.postgresql import JSONB
        return JSONB
    return sa.JSON


def upgrade() -> None:
    bind = op.get_bind()
    JSONType = _jsonb()

    if not _has_table(bind, "merchant_knowledge_drafts"):
        op.create_table(
            "merchant_knowledge_drafts",
            sa.Column("id", sa.Integer(), primary_key=True),
            sa.Column(
                "tenant_id", sa.Integer(),
                sa.ForeignKey("tenants.id", ondelete="CASCADE"),
                nullable=False,
            ),
            sa.Column("raw_text", sa.Text(), nullable=False),
            # List of AIMediaItem ids the merchant attached alongside
            # the raw text. The classifier uses media titles + media
            # keys as additional signal when choosing a kind / role.
            sa.Column("attached_media_ids", JSONType, nullable=True),
            # ``pending`` | ``approved`` | ``rejected`` | ``failed``
            sa.Column(
                "status", sa.String(32),
                nullable=False, server_default=sa.text("'pending'"),
            ),
            # Structured proposal returned by the classifier. Schema:
            # {
            #   "proposed_ops": [
            #     { "op_id": "...", "op": "create|update|merge|link_media",
            #       "kind": "...", "title": "...", "body": "...",
            #       "metadata": {...}, "target_section_id": null,
            #       "link_role": "...", "media_id": null,
            #       "rationale": "..." }
            #   ],
            #   "confidence": 0.0,
            #   "model": "...",
            #   "fallback_used": bool
            # }
            sa.Column("proposal_json", JSONType, nullable=True),
            # [{ "with_section_id": ..., "with_field": "price|stock|...",
            #    "kind": "salla_price|salla_stock|existing_section",
            #    "explanation": "..." }, ...]
            sa.Column("conflicts_json", JSONType, nullable=True),
            sa.Column(
                "created_at", sa.DateTime(timezone=True),
                nullable=False, server_default=sa.text("NOW()"),
            ),
            sa.Column("decided_at", sa.DateTime(timezone=True), nullable=True),
            sa.Column(
                "decided_by_user_id", sa.Integer(),
                sa.ForeignKey("users.id", ondelete="SET NULL"),
                nullable=True,
            ),
            # Comma-separated op_ids the merchant explicitly approved
            # at decide time. Stored as a JSON list for forward-compat.
            sa.Column("applied_op_ids", JSONType, nullable=True),
        )

    for index_name, columns in (
        ("ix_mkd_tenant_id", ["tenant_id"]),
        ("ix_mkd_tenant_status_created", ["tenant_id", "status", "created_at"]),
    ):
        if not _has_index(bind, "merchant_knowledge_drafts", index_name):
            op.create_index(index_name, "merchant_knowledge_drafts", columns)


def downgrade() -> None:
    bind = op.get_bind()
    for index_name in ("ix_mkd_tenant_status_created", "ix_mkd_tenant_id"):
        if _has_index(bind, "merchant_knowledge_drafts", index_name):
            op.drop_index(index_name, table_name="merchant_knowledge_drafts")
    if _has_table(bind, "merchant_knowledge_drafts"):
        op.drop_table("merchant_knowledge_drafts")

"""Add merchant knowledge hub — Phase 1.

Introduces a structured "Smart Store Knowledge Hub" replacing the
legacy free-form ``ai_settings.manual_knowledge_base`` text blob with
two relational tables:

* ``merchant_knowledge_sections`` — one row per piece of merchant-curated
  knowledge (quick update, store story, payment policy, shipping zone,
  product usage tip, recipe, FAQ, …). The legacy text blob is preserved
  on ``ai_settings`` so existing tenants are never broken; the dedicated
  ``/knowledge/sections/migrate-from-legacy`` endpoint moves it across
  on first use of the redesigned page.
* ``merchant_knowledge_media`` — many-to-many link table between
  knowledge sections and the existing ``ai_media_library`` rows. We do
  *not* add a FK to ``ai_media_library`` itself because the same media
  asset can be reused across multiple sections and roles (a barcode
  image can back both ``payment_method`` and ``bank_transfer`` sections,
  for example).

Both tables follow the same idempotency pattern as previous migrations
in this project: every CREATE checks ``information_schema`` first so the
migration replays cleanly on partially-applied environments.

Revision ID: 0067
Revises: 0066
Create Date: 2026-05-22
"""
from alembic import op
import sqlalchemy as sa


revision = "0067"
down_revision = "0066"
branch_labels = None
depends_on = None


# ── Idempotency helpers ─────────────────────────────────────────────────────


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


# Use JSON on SQLite (test env) and JSONB on Postgres. Mirrors the pattern
# used in 0064/0066 — the runtime cares only about dict access, not about
# the underlying storage engine.
def _jsonb():
    bind = op.get_bind()
    if bind.dialect.name == "postgresql":
        from sqlalchemy.dialects.postgresql import JSONB
        return JSONB
    return sa.JSON


def upgrade() -> None:
    bind = op.get_bind()
    JSONType = _jsonb()

    # ── merchant_knowledge_sections ────────────────────────────────────
    if not _has_table(bind, "merchant_knowledge_sections"):
        op.create_table(
            "merchant_knowledge_sections",
            sa.Column("id", sa.Integer(), primary_key=True),
            sa.Column(
                "tenant_id", sa.Integer(),
                sa.ForeignKey("tenants.id", ondelete="CASCADE"),
                nullable=False,
            ),
            # Fixed registry — see services/knowledge_section_kinds.py for
            # the canonical list. We keep this as VARCHAR (not an enum
            # type) so we can extend the registry without DB migrations.
            sa.Column("kind", sa.String(64), nullable=False),
            sa.Column("title", sa.String(255), nullable=True),
            sa.Column("body", sa.Text(), nullable=False, server_default=sa.text("''")),
            # Free-form structured side-data (e.g. {"open":"08:00", ...}
            # for working_hours, branch list, dialect hints).
            sa.Column("metadata_json", JSONType, nullable=True),
            sa.Column(
                "priority", sa.Integer(),
                nullable=False, server_default=sa.text("100"),
            ),
            sa.Column(
                "is_active", sa.Boolean(),
                nullable=False, server_default=sa.text("true"),
            ),
            # ``manual``      = merchant typed it in directly.
            # ``ai_classified`` = produced by the Phase 2 GPT classifier.
            # ``imported``    = migrated from legacy manual_knowledge_base.
            sa.Column(
                "source", sa.String(32),
                nullable=False, server_default=sa.text("'manual'"),
            ),
            # Phase 2 lifecycle hooks — present from Phase 1 so the
            # follow-up migration is additive only.
            sa.Column(
                "ai_status", sa.String(32),
                nullable=False, server_default=sa.text("'approved'"),
            ),
            sa.Column("classification_confidence", sa.Float(), nullable=True),
            sa.Column("conflicts_json", JSONType, nullable=True),
            sa.Column(
                "created_at", sa.DateTime(timezone=True),
                nullable=False, server_default=sa.text("NOW()"),
            ),
            sa.Column(
                "updated_at", sa.DateTime(timezone=True),
                nullable=False, server_default=sa.text("NOW()"),
            ),
        )

    for index_name, columns in (
        ("ix_mks_tenant_id", ["tenant_id"]),
        ("ix_mks_tenant_kind_active", ["tenant_id", "kind", "is_active"]),
        ("ix_mks_tenant_priority", ["tenant_id", "priority"]),
        ("ix_mks_tenant_updated", ["tenant_id", "updated_at"]),
    ):
        if not _has_index(bind, "merchant_knowledge_sections", index_name):
            op.create_index(index_name, "merchant_knowledge_sections", columns)

    # ── merchant_knowledge_media (M2M) ─────────────────────────────────
    if not _has_table(bind, "merchant_knowledge_media"):
        op.create_table(
            "merchant_knowledge_media",
            sa.Column("id", sa.Integer(), primary_key=True),
            sa.Column(
                "section_id", sa.Integer(),
                sa.ForeignKey("merchant_knowledge_sections.id", ondelete="CASCADE"),
                nullable=False,
            ),
            sa.Column(
                "media_id", sa.Integer(),
                sa.ForeignKey("ai_media_library.id", ondelete="CASCADE"),
                nullable=False,
            ),
            # ``primary`` | ``evidence`` | ``barcode`` | ``tutorial_video``
            # | ``recipe_video`` | ``policy_pdf`` | ``certificate`` | ``map``
            sa.Column(
                "link_role", sa.String(32),
                nullable=False, server_default=sa.text("'primary'"),
            ),
            sa.Column(
                "created_at", sa.DateTime(timezone=True),
                nullable=False, server_default=sa.text("NOW()"),
            ),
            sa.UniqueConstraint(
                "section_id", "media_id", "link_role",
                name="uq_mkm_section_media_role",
            ),
        )

    for index_name, columns in (
        ("ix_mkm_section_id", ["section_id"]),
        ("ix_mkm_media_id",   ["media_id"]),
    ):
        if not _has_index(bind, "merchant_knowledge_media", index_name):
            op.create_index(index_name, "merchant_knowledge_media", columns)


def downgrade() -> None:
    bind = op.get_bind()

    for index_name in ("ix_mkm_media_id", "ix_mkm_section_id"):
        if _has_index(bind, "merchant_knowledge_media", index_name):
            op.drop_index(index_name, table_name="merchant_knowledge_media")
    if _has_table(bind, "merchant_knowledge_media"):
        op.drop_table("merchant_knowledge_media")

    for index_name in (
        "ix_mks_tenant_updated",
        "ix_mks_tenant_priority",
        "ix_mks_tenant_kind_active",
        "ix_mks_tenant_id",
    ):
        if _has_index(bind, "merchant_knowledge_sections", index_name):
            op.drop_index(index_name, table_name="merchant_knowledge_sections")
    if _has_table(bind, "merchant_knowledge_sections"):
        op.drop_table("merchant_knowledge_sections")

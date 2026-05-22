"""Link merchant knowledge sections to specific catalog products — Phase 3.

A section can be scoped to one or more products so the AI only injects
that section into the prompt when the conversation is actually about
those products. Example: a "كيفية الاستخدام" section for the Sidr
honey product should NOT leak into a chat about the wax candle.

The same section may stay un-scoped (no link rows → global) for
truly cross-product policies (return policy, shipping, store hours).
That's why we use an M2M link table instead of a FK on the section
itself.

Revision ID: 0069
Revises: 0068
Create Date: 2026-05-22
"""
from alembic import op
import sqlalchemy as sa


revision = "0069"
down_revision = "0068"
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


def upgrade() -> None:
    bind = op.get_bind()

    if not _has_table(bind, "merchant_knowledge_section_products"):
        op.create_table(
            "merchant_knowledge_section_products",
            sa.Column("id", sa.Integer(), primary_key=True),
            sa.Column(
                "section_id", sa.Integer(),
                sa.ForeignKey(
                    "merchant_knowledge_sections.id", ondelete="CASCADE",
                ),
                nullable=False,
            ),
            sa.Column(
                "product_id", sa.Integer(),
                sa.ForeignKey("products.id", ondelete="CASCADE"),
                nullable=False,
            ),
            # ``manual`` | ``ai_fuzzy_match`` | ``imported``
            sa.Column(
                "source", sa.String(32),
                nullable=False, server_default=sa.text("'manual'"),
            ),
            # Confidence is only meaningful for ai_fuzzy_match; NULL
            # for manual / imported links.
            sa.Column("confidence", sa.Float(), nullable=True),
            sa.Column(
                "created_at", sa.DateTime(timezone=True),
                nullable=False, server_default=sa.text("NOW()"),
            ),
            sa.UniqueConstraint(
                "section_id", "product_id",
                name="uq_mksp_section_product",
            ),
        )

    for index_name, columns in (
        ("ix_mksp_section_id", ["section_id"]),
        ("ix_mksp_product_id", ["product_id"]),
    ):
        if not _has_index(
            bind, "merchant_knowledge_section_products", index_name,
        ):
            op.create_index(
                index_name, "merchant_knowledge_section_products", columns,
            )


def downgrade() -> None:
    bind = op.get_bind()
    for index_name in ("ix_mksp_product_id", "ix_mksp_section_id"):
        if _has_index(bind, "merchant_knowledge_section_products", index_name):
            op.drop_index(
                index_name,
                table_name="merchant_knowledge_section_products",
            )
    if _has_table(bind, "merchant_knowledge_section_products"):
        op.drop_table("merchant_knowledge_section_products")

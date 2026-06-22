"""Catalog Intelligence Phase 1 — product groups, relations, rankings.

Platform-wide merchant catalog taxonomy foundation. No AI runtime wiring.

Revision ID: 0083
Revises: 0082
Create Date: 2026-06-22
"""
from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0083"
down_revision = "0082"
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

    if not _has_table(bind, "product_groups"):
        op.create_table(
            "product_groups",
            sa.Column("id", sa.Integer(), primary_key=True),
            sa.Column(
                "tenant_id",
                sa.Integer(),
                sa.ForeignKey("tenants.id", ondelete="CASCADE"),
                nullable=False,
            ),
            sa.Column("slug", sa.String(64), nullable=False),
            sa.Column("label", sa.String(255), nullable=False),
            sa.Column("description", sa.Text(), nullable=True),
            sa.Column("catalog_match", sa.String(255), nullable=True),
            sa.Column("priority", sa.Integer(), nullable=False, server_default=sa.text("100")),
            sa.Column("is_active", sa.Boolean(), nullable=False, server_default=sa.text("true")),
            sa.Column(
                "source",
                sa.String(32),
                nullable=False,
                server_default=sa.text("'manual'"),
            ),
            sa.Column("metadata_json", sa.JSON(), nullable=True),
            sa.Column(
                "created_at",
                sa.DateTime(timezone=True),
                nullable=False,
                server_default=sa.text("NOW()"),
            ),
            sa.Column(
                "updated_at",
                sa.DateTime(timezone=True),
                nullable=False,
                server_default=sa.text("NOW()"),
            ),
            sa.Column("deleted_at", sa.DateTime(timezone=True), nullable=True),
            sa.UniqueConstraint("tenant_id", "slug", name="uq_product_groups_tenant_slug"),
        )

    for index_name, columns in (
        ("ix_product_groups_tenant_active", ("tenant_id", "is_active")),
        ("ix_product_groups_tenant_priority", ("tenant_id", "priority")),
    ):
        if _has_table(bind, "product_groups") and not _has_index(bind, "product_groups", index_name):
            op.create_index(index_name, "product_groups", list(columns))

    if not _has_table(bind, "product_group_items"):
        op.create_table(
            "product_group_items",
            sa.Column("id", sa.Integer(), primary_key=True),
            sa.Column(
                "group_id",
                sa.Integer(),
                sa.ForeignKey("product_groups.id", ondelete="CASCADE"),
                nullable=False,
            ),
            sa.Column(
                "product_id",
                sa.Integer(),
                sa.ForeignKey("products.id", ondelete="CASCADE"),
                nullable=False,
            ),
            sa.Column(
                "variant_id",
                sa.Integer(),
                sa.ForeignKey("product_variants.id", ondelete="SET NULL"),
                nullable=True,
            ),
            sa.Column("priority", sa.Integer(), nullable=False, server_default=sa.text("0")),
            sa.Column("label_override", sa.String(255), nullable=False, server_default=sa.text("''")),
            sa.Column(
                "created_at",
                sa.DateTime(timezone=True),
                nullable=False,
                server_default=sa.text("NOW()"),
            ),
            sa.UniqueConstraint("group_id", "product_id", name="uq_product_group_items_group_product"),
        )

    if _has_table(bind, "product_group_items") and not _has_index(
        bind, "product_group_items", "ix_product_group_items_group_priority",
    ):
        op.create_index(
            "ix_product_group_items_group_priority",
            "product_group_items",
            ["group_id", "priority"],
        )

    if not _has_table(bind, "product_relations"):
        op.create_table(
            "product_relations",
            sa.Column("id", sa.Integer(), primary_key=True),
            sa.Column(
                "tenant_id",
                sa.Integer(),
                sa.ForeignKey("tenants.id", ondelete="CASCADE"),
                nullable=False,
            ),
            sa.Column(
                "source_product_id",
                sa.Integer(),
                sa.ForeignKey("products.id", ondelete="CASCADE"),
                nullable=False,
            ),
            sa.Column(
                "target_product_id",
                sa.Integer(),
                sa.ForeignKey("products.id", ondelete="CASCADE"),
                nullable=False,
            ),
            sa.Column("relation_type", sa.String(32), nullable=False),
            sa.Column("priority", sa.Integer(), nullable=False, server_default=sa.text("0")),
            sa.Column(
                "source",
                sa.String(32),
                nullable=False,
                server_default=sa.text("'manual'"),
            ),
            sa.Column(
                "created_at",
                sa.DateTime(timezone=True),
                nullable=False,
                server_default=sa.text("NOW()"),
            ),
            sa.UniqueConstraint(
                "tenant_id",
                "source_product_id",
                "target_product_id",
                "relation_type",
                name="uq_product_relations_tenant_pair_type",
            ),
        )

    for index_name, columns in (
        ("ix_product_relations_tenant_source", ("tenant_id", "source_product_id")),
        ("ix_product_relations_tenant_target", ("tenant_id", "target_product_id")),
    ):
        if _has_table(bind, "product_relations") and not _has_index(bind, "product_relations", index_name):
            op.create_index(index_name, "product_relations", list(columns))

    if not _has_table(bind, "product_rankings"):
        op.create_table(
            "product_rankings",
            sa.Column("id", sa.Integer(), primary_key=True),
            sa.Column(
                "tenant_id",
                sa.Integer(),
                sa.ForeignKey("tenants.id", ondelete="CASCADE"),
                nullable=False,
            ),
            sa.Column(
                "product_id",
                sa.Integer(),
                sa.ForeignKey("products.id", ondelete="CASCADE"),
                nullable=False,
            ),
            sa.Column("is_best_seller", sa.Boolean(), nullable=False, server_default=sa.text("false")),
            sa.Column("sales_rank", sa.Integer(), nullable=True),
            sa.Column("sales_score", sa.Float(), nullable=True),
            sa.Column("merchant_priority", sa.Integer(), nullable=False, server_default=sa.text("0")),
            sa.Column(
                "stats_source",
                sa.String(32),
                nullable=False,
                server_default=sa.text("'manual'"),
            ),
            sa.Column(
                "updated_at",
                sa.DateTime(timezone=True),
                nullable=False,
                server_default=sa.text("NOW()"),
            ),
            sa.UniqueConstraint("tenant_id", "product_id", name="uq_product_rankings_tenant_product"),
        )

    if _has_table(bind, "product_rankings") and not _has_index(
        bind, "product_rankings", "ix_product_rankings_tenant_best_seller",
    ):
        op.create_index(
            "ix_product_rankings_tenant_best_seller",
            "product_rankings",
            ["tenant_id", "is_best_seller"],
        )


def downgrade() -> None:
    bind = op.get_bind()
    for table in (
        "product_rankings",
        "product_relations",
        "product_group_items",
        "product_groups",
    ):
        if _has_table(bind, table):
            op.drop_table(table)

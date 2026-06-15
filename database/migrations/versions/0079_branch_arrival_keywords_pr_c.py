"""0079 — Branch arrival keywords and location/arrival modes (PR-C).

Revision ID: 0079
Revises:    0078
"""
from __future__ import annotations

import sqlalchemy as sa
from alembic import op


revision = "0079"
down_revision = "0078"
branch_labels = None
depends_on = None


def _has_column(bind, table: str, column: str) -> bool:
    insp = sa.inspect(bind)
    if table not in insp.get_table_names():
        return False
    return column in {c["name"] for c in insp.get_columns(table)}


def _has_table(bind, table: str) -> bool:
    return table in sa.inspect(bind).get_table_names()


def upgrade() -> None:
    bind = op.get_bind()

    if not _has_column(bind, "merchant_branches", "location_response_mode"):
        op.add_column(
            "merchant_branches",
            sa.Column(
                "location_response_mode",
                sa.String(32),
                nullable=False,
                server_default="location_only",
            ),
        )
    if not _has_column(bind, "merchant_branches", "arrival_response_mode"):
        op.add_column(
            "merchant_branches",
            sa.Column(
                "arrival_response_mode",
                sa.String(32),
                nullable=False,
                server_default="reception_only",
            ),
        )
    if not _has_column(bind, "merchant_branches", "location_instructions_text"):
        op.add_column(
            "merchant_branches",
            sa.Column("location_instructions_text", sa.Text(), nullable=True),
        )

    if not _has_table(bind, "branch_arrival_keywords"):
        op.create_table(
            "branch_arrival_keywords",
            sa.Column("id", sa.Integer(), primary_key=True),
            sa.Column(
                "branch_id",
                sa.Integer(),
                sa.ForeignKey("merchant_branches.id", ondelete="CASCADE"),
                nullable=False,
            ),
            sa.Column("phrase", sa.String(512), nullable=False),
            sa.Column("trigger_type", sa.String(32), nullable=False),
            sa.Column(
                "is_active",
                sa.Boolean(),
                nullable=False,
                server_default="true",
            ),
            sa.Column(
                "sort_order",
                sa.Integer(),
                nullable=False,
                server_default="0",
            ),
            sa.Column(
                "created_at",
                sa.DateTime(timezone=True),
                nullable=False,
                server_default=sa.text("now()"),
            ),
            sa.Column(
                "updated_at",
                sa.DateTime(timezone=True),
                nullable=False,
                server_default=sa.text("now()"),
            ),
        )
        op.create_index(
            "ix_branch_arrival_keywords_branch_active",
            "branch_arrival_keywords",
            ["branch_id", "is_active", "sort_order"],
        )


def downgrade() -> None:
    bind = op.get_bind()
    if _has_table(bind, "branch_arrival_keywords"):
        op.drop_index(
            "ix_branch_arrival_keywords_branch_active",
            table_name="branch_arrival_keywords",
        )
        op.drop_table("branch_arrival_keywords")
    for col in (
        "location_instructions_text",
        "arrival_response_mode",
        "location_response_mode",
    ):
        if _has_column(bind, "merchant_branches", col):
            op.drop_column("merchant_branches", col)

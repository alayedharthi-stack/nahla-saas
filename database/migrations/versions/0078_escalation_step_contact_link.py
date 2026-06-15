"""0078 — Link escalation steps to branch contacts (no duplicate staff data).

Revision ID: 0078
Revises:    0077
"""
from __future__ import annotations

import sqlalchemy as sa
from alembic import op


revision = "0078"
down_revision = "0077"
branch_labels = None
depends_on = None


def _has_column(bind, table: str, column: str) -> bool:
    insp = sa.inspect(bind)
    if table not in insp.get_table_names():
        return False
    return column in {c["name"] for c in insp.get_columns(table)}


def upgrade() -> None:
    bind = op.get_bind()
    if not _has_column(bind, "branch_escalation_steps", "contact_id"):
        op.add_column(
            "branch_escalation_steps",
            sa.Column(
                "contact_id",
                sa.Integer(),
                sa.ForeignKey("branch_contacts.id", ondelete="RESTRICT"),
                nullable=True,
            ),
        )
        op.create_index(
            "ix_branch_escalation_steps_contact",
            "branch_escalation_steps",
            ["contact_id"],
        )


def downgrade() -> None:
    bind = op.get_bind()
    if _has_column(bind, "branch_escalation_steps", "contact_id"):
        op.drop_index("ix_branch_escalation_steps_contact", "branch_escalation_steps")
        op.drop_column("branch_escalation_steps", "contact_id")

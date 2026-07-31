"""0096 — order-update template revision chain.

Adds supersedes_template_id + revision for WhatsApp templates so merchants
can edit APPROVED lifecycle templates via a successor draft while the prior
APPROVED row stays active until Meta approves the successor.
"""
from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0096"
down_revision = "0095"
branch_labels = None
depends_on = None

_TABLE = "whatsapp_templates"


def _has_table(bind, name: str) -> bool:
    insp = sa.inspect(bind)
    try:
        return name in set(insp.get_table_names())
    except Exception:
        return False


def _has_column(bind, table: str, column: str) -> bool:
    insp = sa.inspect(bind)
    try:
        return column in {c["name"] for c in insp.get_columns(table)}
    except Exception:
        return False


def upgrade() -> None:
    bind = op.get_bind()
    if not _has_table(bind, _TABLE):
        return
    if not _has_column(bind, _TABLE, "revision"):
        op.add_column(
            _TABLE,
            sa.Column("revision", sa.Integer(), nullable=False, server_default="1"),
        )
    if not _has_column(bind, _TABLE, "supersedes_template_id"):
        op.add_column(
            _TABLE,
            sa.Column(
                "supersedes_template_id",
                sa.Integer(),
                sa.ForeignKey("whatsapp_templates.id", ondelete="SET NULL"),
                nullable=True,
            ),
        )
        op.create_index(
            "ix_whatsapp_templates_supersedes",
            _TABLE,
            ["supersedes_template_id"],
        )


def downgrade() -> None:
    bind = op.get_bind()
    if not _has_table(bind, _TABLE):
        return
    insp = sa.inspect(bind)
    indexes = {idx["name"] for idx in insp.get_indexes(_TABLE)}
    if "ix_whatsapp_templates_supersedes" in indexes:
        op.drop_index("ix_whatsapp_templates_supersedes", table_name=_TABLE)
    if _has_column(bind, _TABLE, "supersedes_template_id"):
        op.drop_column(_TABLE, "supersedes_template_id")
    if _has_column(bind, _TABLE, "revision"):
        op.drop_column(_TABLE, "revision")

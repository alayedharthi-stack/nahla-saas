"""0076 — Operations Center: structured branches, contacts, escalation (PR-A).

Revision ID: 0076
Revises:    0074

Platform-wide structured configuration for branch locations, reception
contacts, and per-branch escalation ladders. Runtime reads these tables
when USE_STRUCTURED_BRANCH_CONTACTS is enabled; KB parsers remain as
fallback.
"""
from __future__ import annotations

import sqlalchemy as sa
from alembic import op


revision = "0076"
down_revision = "0074"
branch_labels = None
depends_on = None


def _has_table(bind, table: str) -> bool:
    insp = sa.inspect(bind)
    return table in insp.get_table_names()


def _json_type(bind):
    dialect = bind.dialect.name
    if dialect == "postgresql":
        return sa.dialects.postgresql.JSONB()
    return sa.JSON()


def upgrade() -> None:
    bind = op.get_bind()
    json_type = _json_type(bind)

    if not _has_table(bind, "merchant_branches"):
        op.create_table(
            "merchant_branches",
            sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
            sa.Column(
                "tenant_id",
                sa.Integer(),
                sa.ForeignKey("tenants.id", ondelete="CASCADE"),
                nullable=False,
            ),
            sa.Column("name", sa.String(length=255), nullable=False),
            sa.Column("city", sa.String(length=128), nullable=True),
            sa.Column("district", sa.String(length=128), nullable=True),
            sa.Column("address", sa.Text(), nullable=True),
            sa.Column("maps_url", sa.String(length=2048), nullable=True),
            sa.Column(
                "is_active",
                sa.Boolean(),
                nullable=False,
                server_default=sa.text("true"),
            ),
            sa.Column("hours_json", json_type, nullable=True),
            sa.Column(
                "sort_order",
                sa.Integer(),
                nullable=False,
                server_default="0",
            ),
            sa.Column(
                "created_at",
                sa.DateTime(timezone=True),
                server_default=sa.text("now()"),
                nullable=False,
            ),
            sa.Column(
                "updated_at",
                sa.DateTime(timezone=True),
                server_default=sa.text("now()"),
                nullable=False,
            ),
        )
        op.create_index(
            "ix_merchant_branches_tenant_active",
            "merchant_branches",
            ["tenant_id", "is_active"],
        )
        op.create_index(
            "ix_merchant_branches_tenant_sort",
            "merchant_branches",
            ["tenant_id", "sort_order"],
        )

    if not _has_table(bind, "branch_contacts"):
        op.create_table(
            "branch_contacts",
            sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
            sa.Column(
                "branch_id",
                sa.Integer(),
                sa.ForeignKey("merchant_branches.id", ondelete="CASCADE"),
                nullable=False,
            ),
            sa.Column("display_name", sa.String(length=255), nullable=False),
            sa.Column("role", sa.String(length=128), nullable=True),
            sa.Column("phone_e164", sa.String(length=32), nullable=False),
            sa.Column("whatsapp_e164", sa.String(length=32), nullable=True),
            sa.Column(
                "is_active",
                sa.Boolean(),
                nullable=False,
                server_default=sa.text("true"),
            ),
            sa.Column(
                "sort_order",
                sa.Integer(),
                nullable=False,
                server_default="0",
            ),
        )
        op.create_index(
            "ix_branch_contacts_branch_active",
            "branch_contacts",
            ["branch_id", "is_active"],
        )
        op.create_index(
            "ix_branch_contacts_branch_sort",
            "branch_contacts",
            ["branch_id", "sort_order"],
        )

    if not _has_table(bind, "branch_escalation_steps"):
        op.create_table(
            "branch_escalation_steps",
            sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
            sa.Column(
                "branch_id",
                sa.Integer(),
                sa.ForeignKey("merchant_branches.id", ondelete="CASCADE"),
                nullable=False,
            ),
            sa.Column(
                "escalation_level",
                sa.Integer(),
                nullable=False,
                server_default="1",
            ),
            sa.Column("display_name", sa.String(length=255), nullable=False),
            sa.Column("role", sa.String(length=128), nullable=True),
            sa.Column("phone_e164", sa.String(length=32), nullable=False),
            sa.Column(
                "is_active",
                sa.Boolean(),
                nullable=False,
                server_default=sa.text("true"),
            ),
            sa.Column(
                "sort_order",
                sa.Integer(),
                nullable=False,
                server_default="0",
            ),
        )
        op.create_index(
            "ix_branch_escalation_steps_branch_level",
            "branch_escalation_steps",
            ["branch_id", "escalation_level"],
        )
        op.create_index(
            "ix_branch_escalation_steps_branch_sort",
            "branch_escalation_steps",
            ["branch_id", "sort_order"],
        )


def downgrade() -> None:
    bind = op.get_bind()
    for table in (
        "branch_escalation_steps",
        "branch_contacts",
        "merchant_branches",
    ):
        if _has_table(bind, table):
            op.drop_table(table)

"""Durable, centralized provenance for the canonical customer name.

Backs ``core.customer_name_authority``: one row per
``(tenant_id, customer_id)`` recording which authority decided the
customer's name, what the WhatsApp-profile classifier said about it,
and what evidence backed a self-reported name.

The JSONB keys on ``customers.metadata`` remain in sync for existing
readers but are demoted to a cache — this table is the record.

Additive only. No existing column is altered and no customer row is
written by this migration.

Revision ID: 0107
Revises: 0106
Create Date: 2026-09-16
"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "0107"
down_revision: Union[str, None] = "0106"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "customer_name_provenance",
        sa.Column("id", sa.Integer, primary_key=True),
        sa.Column(
            "tenant_id", sa.Integer,
            sa.ForeignKey("tenants.id"), nullable=False,
        ),
        sa.Column(
            "customer_id", sa.Integer,
            sa.ForeignKey("customers.id"), nullable=False,
        ),
        sa.Column("canonical_name", sa.String, nullable=True),
        sa.Column(
            "authority", sa.String,
            nullable=False, server_default="UNKNOWN",
        ),
        sa.Column("source", sa.String, nullable=True),
        sa.Column("decision", sa.String, nullable=True),
        sa.Column("classification", sa.String, nullable=True),
        sa.Column("evidence_kind", sa.String, nullable=True),
        sa.Column("profile_hint", sa.String, nullable=True),
        sa.Column(
            "merchant_locked", sa.Boolean,
            nullable=False, server_default=sa.false(),
        ),
        sa.Column("previous_name", sa.String, nullable=True),
        sa.Column("previous_authority", sa.String, nullable=True),
        sa.Column("reason", sa.String, nullable=True),
        sa.Column(
            "created_at", sa.DateTime(timezone=True),
            nullable=False, server_default=sa.func.now(),
        ),
        sa.Column(
            "updated_at", sa.DateTime(timezone=True),
            nullable=False, server_default=sa.func.now(),
        ),
        sa.UniqueConstraint(
            "tenant_id", "customer_id",
            name="uq_customer_name_provenance_tenant_customer",
        ),
    )
    op.create_index(
        "ix_customer_name_provenance_tenant_authority",
        "customer_name_provenance",
        ["tenant_id", "authority"],
    )


def downgrade() -> None:
    op.drop_index(
        "ix_customer_name_provenance_tenant_authority",
        table_name="customer_name_provenance",
    )
    op.drop_table("customer_name_provenance")

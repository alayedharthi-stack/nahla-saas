"""Add external_store_id to integrations and enforce uniqueness.

Revision ID: 0017
Revises: 0016
Create Date: 2026-04-15
"""
from alembic import op
import sqlalchemy as sa


revision = "0017"
down_revision = "0016"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "integrations",
        sa.Column("external_store_id", sa.String(), nullable=True),
    )

    op.execute(
        """
        UPDATE integrations
        SET external_store_id = COALESCE(config->>'store_id', external_store_id)
        WHERE provider = 'salla'
        """
    )

    bind = op.get_bind()
    duplicate_rows = list(
        bind.execute(
            sa.text(
                """
                SELECT provider, external_store_id, COUNT(*) AS row_count
                FROM integrations
                WHERE provider = 'salla' AND external_store_id IS NOT NULL
                GROUP BY provider, external_store_id
                HAVING COUNT(*) > 1
                """
            )
        )
    )
    if duplicate_rows:
        formatted = ", ".join(
            f"{row.provider}:{row.external_store_id} ({row.row_count})"
            for row in duplicate_rows
        )
        raise RuntimeError(
            "Cannot apply unique constraint; duplicate Salla integrations still exist: "
            f"{formatted}. Run the duplicate cleanup first."
        )

    op.create_unique_constraint(
        "uq_integrations_provider_external_store_id",
        "integrations",
        ["provider", "external_store_id"],
    )


def downgrade() -> None:
    op.drop_constraint(
        "uq_integrations_provider_external_store_id",
        "integrations",
        type_="unique",
    )
    op.drop_column("integrations", "external_store_id")

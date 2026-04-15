"""Add deterministic customer intelligence fields to customer_profiles.

Revision ID: 0019
Revises: 0018
Create Date: 2026-04-15
"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "0019"
down_revision: Union[str, None] = "0018"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column("customer_profiles", sa.Column("first_order_at", sa.DateTime(), nullable=True))
    op.add_column("customer_profiles", sa.Column("customer_status", sa.String(), nullable=True))
    op.add_column("customer_profiles", sa.Column("rfm_recency_score", sa.Integer(), nullable=False, server_default="0"))
    op.add_column("customer_profiles", sa.Column("rfm_frequency_score", sa.Integer(), nullable=False, server_default="0"))
    op.add_column("customer_profiles", sa.Column("rfm_monetary_score", sa.Integer(), nullable=False, server_default="0"))
    op.add_column("customer_profiles", sa.Column("rfm_total_score", sa.Integer(), nullable=False, server_default="0"))
    op.add_column("customer_profiles", sa.Column("rfm_code", sa.String(), nullable=True))
    op.add_column("customer_profiles", sa.Column("rfm_segment", sa.String(), nullable=True))
    op.add_column("customer_profiles", sa.Column("metrics_computed_at", sa.DateTime(), nullable=True))
    op.add_column("customer_profiles", sa.Column("last_recomputed_reason", sa.String(), nullable=True))

    op.execute(
        """
        UPDATE customer_profiles
        SET
            first_order_at = COALESCE(first_order_at, first_seen_at),
            customer_status = COALESCE(
                customer_status,
                CASE
                    WHEN COALESCE(total_orders, 0) <= 0 THEN 'lead'
                    WHEN segment = 'churned' THEN 'inactive'
                    ELSE COALESCE(segment, 'lead')
                END
            ),
            rfm_segment = COALESCE(
                rfm_segment,
                CASE
                    WHEN COALESCE(total_orders, 0) <= 0 THEN 'lead'
                    WHEN segment = 'vip' THEN 'champions'
                    WHEN segment = 'active' THEN 'regulars'
                    WHEN segment = 'new' THEN 'new_customers'
                    WHEN segment = 'at_risk' THEN 'at_risk'
                    WHEN segment = 'churned' THEN 'lost_customers'
                    ELSE 'lead'
                END
            ),
            metrics_computed_at = COALESCE(metrics_computed_at, updated_at, CURRENT_TIMESTAMP),
            last_recomputed_reason = COALESCE(last_recomputed_reason, 'migration_0019_backfill')
        """
    )

    op.alter_column("customer_profiles", "customer_status", existing_type=sa.String(), nullable=False, server_default="lead")
    op.alter_column("customer_profiles", "rfm_segment", existing_type=sa.String(), nullable=False, server_default="lead")
    op.alter_column("customer_profiles", "rfm_recency_score", server_default=None)
    op.alter_column("customer_profiles", "rfm_frequency_score", server_default=None)
    op.alter_column("customer_profiles", "rfm_monetary_score", server_default=None)
    op.alter_column("customer_profiles", "rfm_total_score", server_default=None)


def downgrade() -> None:
    op.drop_column("customer_profiles", "last_recomputed_reason")
    op.drop_column("customer_profiles", "metrics_computed_at")
    op.drop_column("customer_profiles", "rfm_segment")
    op.drop_column("customer_profiles", "rfm_code")
    op.drop_column("customer_profiles", "rfm_total_score")
    op.drop_column("customer_profiles", "rfm_monetary_score")
    op.drop_column("customer_profiles", "rfm_frequency_score")
    op.drop_column("customer_profiles", "rfm_recency_score")
    op.drop_column("customer_profiles", "customer_status")
    op.drop_column("customer_profiles", "first_order_at")

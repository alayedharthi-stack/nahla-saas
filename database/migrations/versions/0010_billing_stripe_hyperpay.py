"""Add Stripe and HyperPay billing fields to tenants table.

Revision ID: 0010
Revises: 0009
Create Date: 2026-04-03

Adds:
  - billing_provider       (stripe | hyperpay)
  - stripe_customer_id
  - stripe_subscription_id
  - stripe_price_id
  - subscription_status    (trialing | active | past_due | canceled)
  - trial_started_at
  - trial_ends_at
  - current_period_end
  - hyperpay_payment_id
  - billing_status         (pending | paid | failed)
"""
from alembic import op
import sqlalchemy as sa


revision = "0010"
down_revision = "0009"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # ── Billing provider selector ─────────────────────────────────────────────
    op.add_column("tenants", sa.Column("billing_provider",       sa.String(), nullable=True, server_default="stripe"))

    # ── Stripe fields ─────────────────────────────────────────────────────────
    op.add_column("tenants", sa.Column("stripe_customer_id",     sa.String(), nullable=True))
    op.add_column("tenants", sa.Column("stripe_subscription_id", sa.String(), nullable=True))
    op.add_column("tenants", sa.Column("stripe_price_id",        sa.String(), nullable=True))
    op.add_column("tenants", sa.Column("subscription_status",    sa.String(), nullable=True))
    op.add_column("tenants", sa.Column("trial_started_at",       sa.DateTime(), nullable=True))
    op.add_column("tenants", sa.Column("trial_ends_at",          sa.DateTime(), nullable=True))
    op.add_column("tenants", sa.Column("current_period_end",     sa.DateTime(), nullable=True))

    # ── HyperPay fields ───────────────────────────────────────────────────────
    op.add_column("tenants", sa.Column("hyperpay_payment_id",    sa.String(), nullable=True))
    op.add_column("tenants", sa.Column("billing_status",         sa.String(), nullable=True))


def downgrade() -> None:
    op.drop_column("tenants", "billing_status")
    op.drop_column("tenants", "hyperpay_payment_id")
    op.drop_column("tenants", "current_period_end")
    op.drop_column("tenants", "trial_ends_at")
    op.drop_column("tenants", "trial_started_at")
    op.drop_column("tenants", "subscription_status")
    op.drop_column("tenants", "stripe_price_id")
    op.drop_column("tenants", "stripe_subscription_id")
    op.drop_column("tenants", "stripe_customer_id")
    op.drop_column("tenants", "billing_provider")

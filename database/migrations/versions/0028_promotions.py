"""Create promotions table.

Revision ID: 0028
Revises: 0027
Create Date: 2026-04-17

Why
───
Nahla previously had only one discount primitive — `coupons` — which holds
*one specific code* (e.g. ``NH4K7`` good for 15% off until tomorrow). That
primitive is fine for personal, conversational handouts ("here is your
recovery code") but it cannot model the broader merchant intent of
"run 15% off for everyone during White Friday" or "free shipping over
200 SAR" the way Shopify/Magento split the two concerns into:

  • Promotions  — automatic rules, no code needed (free shipping, BOGO,
                  threshold discount, % off all);
  • Coupons     — discrete codes the customer types or receives in chat.

This migration creates the `promotions` table that stores the *terms* of
a promotion. When an automation fires for a customer, the promotion engine
issues a personal `Coupon` row carrying those terms — that way the same
infrastructure works across every store backend (Salla / Zid / Shopify)
without depending on each platform's promotional API.

Schema
──────
  id              PK
  tenant_id       FK → tenants.id (indexed, scoped on every query)
  name            human label (shown in dashboard)
  description     long-form description
  promotion_type  percentage | fixed | free_shipping | threshold_discount
                  | buy_x_get_y    (open-ended String, validated in service)
  discount_value  Numeric(10,2)    nullable for types that don't need it
                                   (e.g. free_shipping)
  conditions      JSONB            min_order_amount, applicable_products,
                                   applicable_categories, customer_segments,
                                   x_product_ids, y_product_ids, x_quantity,
                                   y_quantity, etc.
  starts_at       DateTime         nullable → "starts immediately"
  ends_at         DateTime         nullable → "no end"
  status          draft | scheduled | active | paused | expired
  usage_count     bumped on each materialisation
  usage_limit     hard cap on total uses (nullable)
  extra_metadata  JSONB            free-form
  created_at / updated_at

Composite indexes on (tenant_id, status) and (tenant_id, promotion_type)
power the dashboard queries (`WHERE tenant_id=? AND status='active'`).
"""
from alembic import op
import sqlalchemy as sa


revision = "0028"
down_revision = "0027"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "promotions",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("tenant_id", sa.Integer(), nullable=False),
        sa.Column("name", sa.String(), nullable=False),
        sa.Column("description", sa.Text(), nullable=True),
        sa.Column("promotion_type", sa.String(), nullable=False),
        sa.Column("discount_value", sa.Numeric(10, 2), nullable=True),
        sa.Column("conditions", sa.JSON(), nullable=True),
        sa.Column("starts_at", sa.DateTime(), nullable=True),
        sa.Column("ends_at", sa.DateTime(), nullable=True),
        sa.Column(
            "status",
            sa.String(),
            nullable=False,
            server_default="draft",
        ),
        sa.Column(
            "usage_count",
            sa.Integer(),
            nullable=False,
            server_default="0",
        ),
        sa.Column("usage_limit", sa.Integer(), nullable=True),
        sa.Column("metadata", sa.JSON(), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(),
            nullable=False,
            server_default=sa.func.now(),
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(),
            nullable=False,
            server_default=sa.func.now(),
        ),
        sa.ForeignKeyConstraint(["tenant_id"], ["tenants.id"]),
    )
    op.create_index(
        "ix_promotions_tenant_id",
        "promotions",
        ["tenant_id"],
    )
    op.create_index(
        "ix_promotions_status",
        "promotions",
        ["status"],
    )
    op.create_index(
        "ix_promotions_tenant_status",
        "promotions",
        ["tenant_id", "status"],
    )
    op.create_index(
        "ix_promotions_tenant_type",
        "promotions",
        ["tenant_id", "promotion_type"],
    )

    # Drop server defaults once the table is created so the ORM is the
    # single source of truth (mirrors migration 0027's pattern).
    with op.batch_alter_table("promotions") as batch_op:
        batch_op.alter_column("status", server_default=None)
        batch_op.alter_column("usage_count", server_default=None)


def downgrade() -> None:
    op.drop_index("ix_promotions_tenant_type", table_name="promotions")
    op.drop_index("ix_promotions_tenant_status", table_name="promotions")
    op.drop_index("ix_promotions_status", table_name="promotions")
    op.drop_index("ix_promotions_tenant_id", table_name="promotions")
    op.drop_table("promotions")

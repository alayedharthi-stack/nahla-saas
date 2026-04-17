"""Add engine column to smart_automations.

Revision ID: 0027
Revises: 0026
Create Date: 2026-04-17

Why
───
The merchant-facing "Smart Autopilot" dashboard is being restructured around
four operational engines:

  • recovery     — abandoned cart, customer winback, unpaid order reminders
  • growth       — VIP rewards, predictive reorder, seasonal/salary offers
  • experience   — thank you, review request, cross-sell (placeholder)
  • intelligence — segmentation, send-time, AI rewriting (placeholder)

Until now the four buckets only existed as hard-coded sections in
`dashboard/src/pages/SmartAutomations.tsx`. Putting `engine` on the row
itself lets:

  • the API (`/automations/engines/summary` and `engine` field on
    `/automations`) drive the UI from the database;
  • a future per-engine toggle (`PUT /automations/engines/{engine}/toggle`)
    flip an entire bucket atomically;
  • new automations seeded later land in the right section automatically.

Backfill follows the canonical map in `backend/core/automations_seed.py`.
Anything not recognised falls back to `recovery` (the existing implicit
default) so legacy rows stay visible somewhere instead of disappearing.
"""
from alembic import op
import sqlalchemy as sa


revision = "0027"
down_revision = "0026"
branch_labels = None
depends_on = None


# Mirrors backend/core/automations_seed.ENGINE_BY_TYPE — kept inline so the
# migration is self-contained and does not depend on Python imports at
# upgrade time.
ENGINE_BY_TYPE = {
    "abandoned_cart": "recovery",
    "customer_winback": "recovery",
    "unpaid_order_reminder": "recovery",
    "vip_upgrade": "growth",
    "predictive_reorder": "growth",
    "new_product_alert": "growth",
    "back_in_stock": "growth",
    "seasonal_offer": "growth",
    "salary_payday_offer": "growth",
}


def upgrade() -> None:
    op.add_column(
        "smart_automations",
        sa.Column(
            "engine",
            sa.String(),
            nullable=False,
            server_default="recovery",
        ),
    )
    op.create_index(
        "ix_smart_automations_engine",
        "smart_automations",
        ["engine"],
        unique=False,
    )

    bind = op.get_bind()
    for automation_type, engine in ENGINE_BY_TYPE.items():
        bind.execute(
            sa.text(
                "UPDATE smart_automations SET engine = :engine "
                "WHERE automation_type = :automation_type"
            ),
            {"engine": engine, "automation_type": automation_type},
        )

    # Drop the server_default once the table is backfilled — the application
    # default ("recovery") is enforced in the ORM model.
    with op.batch_alter_table("smart_automations") as batch_op:
        batch_op.alter_column("engine", server_default=None)


def downgrade() -> None:
    op.drop_index("ix_smart_automations_engine", table_name="smart_automations")
    op.drop_column("smart_automations", "engine")

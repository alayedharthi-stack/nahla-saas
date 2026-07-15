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

Idempotency (F16)
─────────────────
``create_all`` may have pre-created ``smart_automations.engine`` while
``alembic_version`` lags behind 0027. Inspector-guarded DDL skips present
objects; the canonical ``ENGINE_BY_TYPE`` backfill still runs so rows are
not left on the wrong engine bucket.
"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op
from sqlalchemy import inspect

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


def _has_table(bind, table_name: str) -> bool:
    return table_name in inspect(bind).get_table_names()


def _has_column(bind, table_name: str, column_name: str) -> bool:
    if not _has_table(bind, table_name):
        return False
    return any(
        c["name"] == column_name
        for c in inspect(bind).get_columns(table_name)
    )


def _has_index(bind, table_name: str, index_name: str) -> bool:
    if not _has_table(bind, table_name):
        return False
    return any(
        ix["name"] == index_name
        for ix in inspect(bind).get_indexes(table_name)
    )


def upgrade() -> None:
    bind = op.get_bind()

    if not _has_column(bind, "smart_automations", "engine"):
        op.add_column(
            "smart_automations",
            sa.Column(
                "engine",
                sa.String(),
                nullable=False,
                server_default="recovery",
            ),
        )
    if not _has_index(bind, "smart_automations", "ix_smart_automations_engine"):
        op.create_index(
            "ix_smart_automations_engine",
            "smart_automations",
            ["engine"],
            unique=False,
        )

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
    if _has_column(bind, "smart_automations", "engine"):
        with op.batch_alter_table("smart_automations") as batch_op:
            batch_op.alter_column("engine", server_default=None)


def downgrade() -> None:
    bind = op.get_bind()
    if _has_index(bind, "smart_automations", "ix_smart_automations_engine"):
        op.drop_index("ix_smart_automations_engine", table_name="smart_automations")
    if _has_column(bind, "smart_automations", "engine"):
        op.drop_column("smart_automations", "engine")

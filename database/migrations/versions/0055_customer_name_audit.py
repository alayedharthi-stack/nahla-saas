"""0055 — customer_name_audit_logs: bulk name-cleanup audit trail.

Revision ID: 0055
Revises:    0054

Why this migration exists
─────────────────────────
The customers page now exposes a "تنظيف أسماء العملاء" button that
bulk-mutates ``customers.name`` for the current tenant — stripping
commercial filler tokens (``عميل``, ``customer`` …) and clearing
phone-only / non-human values.

That mutation is destructive: once applied, the previous raw value
on ``customers.name`` is gone. To stay debuggable, every change
writes a row to this audit table with:

  * ``customer_id``    — the row that was changed.
  * ``old_name``       — exactly what was on ``customers.name`` before.
  * ``new_name``       — the value written (``NULL`` when the cleanup
    cleared the row entirely).
  * ``reason``         — Arabic explanation shown to the merchant.
  * ``confidence``     — ``"high"`` (auto-eligible) or ``"low"`` (manual).
  * ``actor_user_id``  — which dashboard user pressed the button
    (nullable for service-account / future automation paths).
  * ``created_at``     — UTC timestamp.

Tenant isolation is enforced at the application layer (the apply
endpoint refuses to write a row for a customer that doesn't belong
to the requesting tenant), and reinforced here by the
``ix_customer_name_audit_tenant_created`` covering index so support
queries ``WHERE tenant_id = ? ORDER BY created_at DESC`` stay cheap.

Idempotency (F16)
─────────────────
Both ``create_table`` and ``create_index`` are guarded by inspector
checks so re-running on a DB where the table or index already exists
is a no-op rather than a DuplicateTable / DuplicateRelation error.
"""
from __future__ import annotations

import sqlalchemy as sa
from alembic import op
from sqlalchemy import inspect

revision = "0055"
down_revision = "0054"
branch_labels = None
depends_on = None


def _has_table(bind, table_name: str) -> bool:
    return table_name in inspect(bind).get_table_names()


def _has_index(bind, table_name: str, index_name: str) -> bool:
    if not _has_table(bind, table_name):
        return False
    return any(
        ix["name"] == index_name
        for ix in inspect(bind).get_indexes(table_name)
    )


def upgrade() -> None:
    bind = op.get_bind()

    if not _has_table(bind, "customer_name_audit_logs"):
        op.create_table(
            "customer_name_audit_logs",
            sa.Column("id", sa.Integer(), primary_key=True),
            sa.Column(
                "tenant_id", sa.Integer(),
                sa.ForeignKey("tenants.id"), nullable=False,
            ),
            sa.Column(
                "customer_id", sa.Integer(),
                sa.ForeignKey("customers.id"), nullable=False,
            ),
            sa.Column("old_name", sa.String(), nullable=True),
            sa.Column("new_name", sa.String(), nullable=True),
            sa.Column("reason", sa.String(), nullable=True),
            sa.Column("confidence", sa.String(), nullable=True),
            sa.Column(
                "actor_user_id", sa.Integer(),
                sa.ForeignKey("users.id"), nullable=True,
            ),
            sa.Column(
                "created_at",
                sa.DateTime(timezone=True),
                server_default=sa.text("CURRENT_TIMESTAMP"),
                nullable=False,
            ),
        )

    if not _has_index(
        bind, "customer_name_audit_logs",
        "ix_customer_name_audit_tenant_created",
    ):
        op.create_index(
            "ix_customer_name_audit_tenant_created",
            "customer_name_audit_logs",
            ["tenant_id", "created_at"],
        )

    if not _has_index(
        bind, "customer_name_audit_logs",
        "ix_customer_name_audit_customer",
    ):
        op.create_index(
            "ix_customer_name_audit_customer",
            "customer_name_audit_logs",
            ["customer_id"],
        )


def downgrade() -> None:
    op.drop_index(
        "ix_customer_name_audit_customer",
        table_name="customer_name_audit_logs",
    )
    op.drop_index(
        "ix_customer_name_audit_tenant_created",
        table_name="customer_name_audit_logs",
    )
    op.drop_table("customer_name_audit_logs")

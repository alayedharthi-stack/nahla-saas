"""Add Nahla display & management columns to whatsapp_templates.

Revision ID: 0035
Revises: 0034
Create Date: 2026-04-21

Adds columns for Arabic display names, service linking, multi-step
sequence metadata, and soft-management (active/hidden) so merchants
can organise templates within Nahla independently of Meta status.

Idempotency (F16)
─────────────────
Guarded by inspector checks — safe when forward-ORM drift pre-created
columns or the partial unique index while ``alembic_version`` is still
at 0034.
"""
from __future__ import annotations

from alembic import op
import sqlalchemy as sa

from migration_inspector_helpers import has_column, has_index

revision = "0035"
down_revision = "0034"
branch_labels = None
depends_on = None


def upgrade() -> None:
    bind = op.get_bind()

    for column_name, column in (
        ("display_name_ar", sa.Column("display_name_ar", sa.String(), nullable=True)),
        ("service_key", sa.Column("service_key", sa.String(), nullable=True)),
        ("nahla_source_key", sa.Column("nahla_source_key", sa.String(), nullable=True)),
        (
            "is_active",
            sa.Column("is_active", sa.Boolean(), nullable=False, server_default=sa.text("true")),
        ),
        (
            "is_hidden",
            sa.Column("is_hidden", sa.Boolean(), nullable=False, server_default=sa.text("false")),
        ),
        ("step_number", sa.Column("step_number", sa.Integer(), nullable=True)),
        (
            "has_coupon",
            sa.Column("has_coupon", sa.Boolean(), nullable=True, server_default=sa.text("false")),
        ),
        ("trigger_delay_hours", sa.Column("trigger_delay_hours", sa.Float(), nullable=True)),
    ):
        if not has_column(bind, "whatsapp_templates", column_name):
            op.add_column("whatsapp_templates", column)

    # Only one active template per (tenant, service, step) — enforced at DB level.
    # The partial index only covers rows where all three conditions hold,
    # so templates without a service_key or step_number are unconstrained.
    if not has_index(bind, "whatsapp_templates", "uq_active_template_per_service_step"):
        op.create_index(
            "uq_active_template_per_service_step",
            "whatsapp_templates",
            ["tenant_id", "service_key", "step_number"],
            unique=True,
            postgresql_where=sa.text(
                "is_active = true AND is_hidden = false "
                "AND service_key IS NOT NULL AND step_number IS NOT NULL"
            ),
        )


def downgrade() -> None:
    op.drop_index("uq_active_template_per_service_step", table_name="whatsapp_templates")
    op.drop_column("whatsapp_templates", "trigger_delay_hours")
    op.drop_column("whatsapp_templates", "has_coupon")
    op.drop_column("whatsapp_templates", "step_number")
    op.drop_column("whatsapp_templates", "is_hidden")
    op.drop_column("whatsapp_templates", "is_active")
    op.drop_column("whatsapp_templates", "nahla_source_key")
    op.drop_column("whatsapp_templates", "service_key")
    op.drop_column("whatsapp_templates", "display_name_ar")

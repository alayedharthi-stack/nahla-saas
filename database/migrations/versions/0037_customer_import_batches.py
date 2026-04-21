"""Add customer_import_batches table for the customer-import wizard.

Revision ID: 0037
Revises: 0036

(Originally authored as 0036 alongside the customer-import wizard
commit, but `0036_wa_connection_meta_tier` already claimed that slot
and Railway's `alembic upgrade head` died on multiple-heads. Bumped
to 0037 with `down_revision='0036'` so the chain is linear again.)

Backs the four-step customer import flow (upload → mapping → preview
→ commit). Each row represents one upload session and stores the
classified rows payload so the dashboard can resume / drill into any
step without re-parsing the file.
"""
from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import JSONB


revision = "0037"
down_revision = "0036"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "customer_import_batches",
        sa.Column("id",            sa.Integer(), primary_key=True),
        sa.Column("tenant_id",     sa.Integer(),
                  sa.ForeignKey("tenants.id"), nullable=False, index=True),
        sa.Column("created_by",    sa.Integer(),
                  sa.ForeignKey("users.id"), nullable=True),
        sa.Column("created_at",    sa.DateTime(timezone=True),
                  nullable=False, server_default=sa.text("now()")),
        sa.Column("committed_at",  sa.DateTime(timezone=True), nullable=True),

        sa.Column("filename",      sa.String(),  nullable=True),
        sa.Column("file_kind",     sa.String(),  nullable=True),
        sa.Column("status",        sa.String(),  nullable=False,
                  server_default=sa.text("'parsed'")),

        sa.Column("column_mapping", JSONB(),     nullable=True),

        sa.Column("total_rows",    sa.Integer(), nullable=False,
                  server_default=sa.text("0")),
        sa.Column("new_count",     sa.Integer(), nullable=False,
                  server_default=sa.text("0")),
        sa.Column("match_count",   sa.Integer(), nullable=False,
                  server_default=sa.text("0")),
        sa.Column("suspect_count", sa.Integer(), nullable=False,
                  server_default=sa.text("0")),
        sa.Column("invalid_count", sa.Integer(), nullable=False,
                  server_default=sa.text("0")),

        sa.Column("created_count", sa.Integer(), nullable=False,
                  server_default=sa.text("0")),
        sa.Column("updated_count", sa.Integer(), nullable=False,
                  server_default=sa.text("0")),
        sa.Column("skipped_count", sa.Integer(), nullable=False,
                  server_default=sa.text("0")),
        sa.Column("error_count",   sa.Integer(), nullable=False,
                  server_default=sa.text("0")),

        sa.Column("rows_payload",  JSONB(),      nullable=True),
        sa.Column("error_message", sa.Text(),    nullable=True),
    )

    op.create_index(
        "ix_customer_import_batches_tenant_status",
        "customer_import_batches",
        ["tenant_id", "status"],
    )


def downgrade() -> None:
    op.drop_index(
        "ix_customer_import_batches_tenant_status",
        table_name="customer_import_batches",
    )
    op.drop_table("customer_import_batches")

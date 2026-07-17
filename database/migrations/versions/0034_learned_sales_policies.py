"""
Learned cross-merchant sales policies (Phase 1.7).

Revision ID: 0034
Revises: 0033
Create Date: 2026-04-19

WHY
───
Phase 1.7 of the Sales Brain architecture introduces the first
read-only ``PolicyLearner`` that aggregates ``cross_merchant_signals``
into actionable hints for the per-merchant ``DecisionEngine``.  The
output of that learner is persisted in ``learned_sales_policies`` so
runtime lookups (``LearnedPolicyStore``) stay cheap and so a future
operator dashboard can inspect what the global / vertical brain has
learned without re-running aggregation.

CHANGES
───────
+ table learned_sales_policies
    id                 SERIAL PRIMARY KEY
    scope              VARCHAR(16)  NOT NULL  DEFAULT 'global'   -- global | vertical
    industry           VARCHAR(64)  NOT NULL  DEFAULT '*'        -- '*' for global tier
    intent             VARCHAR(64)  NOT NULL  DEFAULT 'unknown'
    recommended_action VARCHAR(64)  NOT NULL  DEFAULT 'unknown'
    recommended_ui     VARCHAR(32)  NOT NULL  DEFAULT 'unknown'
    confidence         FLOAT        NOT NULL  DEFAULT 0.0
    sample_size        INTEGER      NOT NULL  DEFAULT 0
    extra              JSONB        NULL
    updated_at         TIMESTAMPTZ  NOT NULL  DEFAULT now()

+ unique constraint
    uq_lsp_scope_industry_intent on (scope, industry, intent)

+ indexes
    ix_lsp_intent           on (intent)
    ix_lsp_industry_intent  on (industry, intent)

ANTI-LEAK GUARANTEES
────────────────────
* No tenant_id / customer_id columns — policies are tenant-agnostic.
* All categorical columns are validated upstream by the anonymized
  trace schema before they ever reach this table.
* ``industry`` is the same lower-cased tag used by ``CrossMerchantSignal``
  (or ``"*"`` for the global tier) — no raw merchant identifier.

Idempotency (F16)
─────────────────
Guarded by inspector checks — safe when forward-ORM drift pre-created
the table and/or indexes while ``alembic_version`` is still at 0033.
"""
from __future__ import annotations

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import JSONB

from migration_inspector_helpers import has_index, has_table, has_unique_constraint

revision = "0034"
down_revision = "0033"
branch_labels = None
depends_on = None


def upgrade() -> None:
    bind = op.get_bind()

    if not has_table(bind, "learned_sales_policies"):
        op.create_table(
            "learned_sales_policies",
            sa.Column("id", sa.Integer(), primary_key=True),
            sa.Column("scope", sa.String(length=16), nullable=False, server_default="global"),
            sa.Column("industry", sa.String(length=64), nullable=False, server_default="*"),
            sa.Column("intent", sa.String(length=64), nullable=False, server_default="unknown"),
            sa.Column("recommended_action", sa.String(length=64), nullable=False, server_default="unknown"),
            sa.Column("recommended_ui", sa.String(length=32), nullable=False, server_default="unknown"),
            sa.Column("confidence", sa.Float(), nullable=False, server_default="0"),
            sa.Column("sample_size", sa.Integer(), nullable=False, server_default="0"),
            sa.Column("extra", JSONB(), nullable=True),
            sa.Column(
                "updated_at",
                sa.DateTime(timezone=True),
                server_default=sa.text("now()"),
                nullable=False,
            ),
            sa.UniqueConstraint(
                "scope", "industry", "intent",
                name="uq_lsp_scope_industry_intent",
            ),
        )

    if (
        has_table(bind, "learned_sales_policies")
        and not has_unique_constraint(bind, "learned_sales_policies", "uq_lsp_scope_industry_intent")
        and bind.dialect.name != "sqlite"
    ):
        op.create_unique_constraint(
            "uq_lsp_scope_industry_intent",
            "learned_sales_policies",
            ["scope", "industry", "intent"],
        )

    for index_name, columns in (
        ("ix_lsp_intent", ("intent",)),
        ("ix_lsp_industry_intent", ("industry", "intent")),
    ):
        if has_table(bind, "learned_sales_policies") and not has_index(
            bind, "learned_sales_policies", index_name,
        ):
            op.create_index(index_name, "learned_sales_policies", list(columns))


def downgrade() -> None:
    op.drop_index("ix_lsp_industry_intent", table_name="learned_sales_policies")
    op.drop_index("ix_lsp_intent", table_name="learned_sales_policies")
    op.drop_table("learned_sales_policies")

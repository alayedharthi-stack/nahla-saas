"""Create offer_decisions ledger table.

Revision ID: 0029
Revises: 0028
Create Date: 2026-04-17

Why
───
Nahla now has a single shared decision layer (`OfferDecisionService`) that
sits between the trigger surfaces (automation engine, conversational
orchestrator, customer-intelligence segment-change autogen) and the
existing primitives (`Promotion`, `Coupon`). Every decision the policy
makes — issue a promotion, issue a coupon, or send no discount — needs to
be persisted with:

  • the inputs it saw at decision time (`signals_snapshot`),
  • the chosen output (`chosen_source` + value/validity/promotion/coupon),
  • a short, human-readable explanation (`reason_codes`),
  • and — once the order is paid — the realised outcome (`redeemed_at`,
    `order_id`, `revenue_amount`, `attributed`).

This table is the closing of the loop: it lets the merchant (and a future
bandit policy) answer questions like "did 15% beat 10% on cart abandonment
for at-risk customers in Riyadh?" without retrofitting metadata onto
Coupon rows or scraping logs.

The columns `policy_version` and `experiment_arm` are populated from day
one so a contextual-bandit policy can be added later behind the same
`OfferDecisionService.decide(...)` interface — without a schema change.

Schema
──────
  id                      PK
  tenant_id               FK → tenants.id (indexed)
  decision_id             UUID-string, unique (joined onto Coupon.extra_metadata.decision_id)
  surface                 automation | chat | segment_change
  automation_id           Optional (no FK — preserves history if upstream deleted)
  event_id                Optional
  customer_id             Optional
  signals_snapshot        JSONB
  chosen_source           promotion | coupon | none
  chosen_promotion_id     Optional
  chosen_coupon_id        Optional (filled after issuance)
  discount_type           percentage | fixed | free_shipping | NULL
  discount_value          Numeric(10,2)
  validity_days           Integer
  reason_codes            JSONB (list of short string codes)
  policy_version          String, NOT NULL — v1 always 'v1.0-deterministic'
  experiment_arm          Optional (future bandit/AB)
  redeemed_at             DateTime, nullable
  order_id                Integer, nullable
  revenue_amount          Numeric(12,2), nullable
  attributed              Boolean, default False
  created_at              DateTime

Composite indexes power the dashboard queries we will ship in Phase 5
(`/offers/decisions`): WHERE tenant_id=? AND created_at >= ?, grouped by
surface / chosen_source / attributed.

Idempotency (F16)
─────────────────
``create_all`` may have pre-created ``offer_decisions`` while
``alembic_version`` lags behind 0029. Inspector-guarded DDL skips present
objects and adds missing indexes/constraints; clean upgrades unchanged.
"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op
from sqlalchemy import inspect

revision = "0029"
down_revision = "0028"
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


def _has_unique_constraint(bind, table_name: str, constraint_name: str) -> bool:
    if not _has_table(bind, table_name):
        return False
    insp = inspect(bind)
    try:
        cons = insp.get_unique_constraints(table_name)
    except NotImplementedError:
        return False
    return any(c.get("name") == constraint_name for c in cons)


def upgrade() -> None:
    bind = op.get_bind()

    if not _has_table(bind, "offer_decisions"):
        op.create_table(
            "offer_decisions",
            sa.Column("id", sa.Integer(), primary_key=True),
            sa.Column("tenant_id", sa.Integer(), nullable=False),
            sa.Column("decision_id", sa.String(), nullable=False),
            sa.Column("surface", sa.String(), nullable=False),
            sa.Column("automation_id", sa.Integer(), nullable=True),
            sa.Column("event_id", sa.Integer(), nullable=True),
            sa.Column("customer_id", sa.Integer(), nullable=True),
            sa.Column("signals_snapshot", sa.JSON(), nullable=True),
            sa.Column("chosen_source", sa.String(), nullable=False),
            sa.Column("chosen_promotion_id", sa.Integer(), nullable=True),
            sa.Column("chosen_coupon_id", sa.Integer(), nullable=True),
            sa.Column("discount_type", sa.String(), nullable=True),
            sa.Column("discount_value", sa.Numeric(10, 2), nullable=True),
            sa.Column("validity_days", sa.Integer(), nullable=True),
            sa.Column("reason_codes", sa.JSON(), nullable=True),
            sa.Column(
                "policy_version",
                sa.String(),
                nullable=False,
                server_default="v1.0-deterministic",
            ),
            sa.Column("experiment_arm", sa.String(), nullable=True),
            sa.Column("redeemed_at", sa.DateTime(), nullable=True),
            sa.Column("order_id", sa.Integer(), nullable=True),
            sa.Column("revenue_amount", sa.Numeric(12, 2), nullable=True),
            sa.Column(
                "attributed",
                sa.Boolean(),
                nullable=False,
                server_default=sa.false(),
            ),
            sa.Column(
                "created_at",
                sa.DateTime(),
                nullable=False,
                server_default=sa.func.now(),
            ),
            sa.ForeignKeyConstraint(["tenant_id"], ["tenants.id"]),
        )
    if not _has_index(bind, "offer_decisions", "ix_offer_decisions_tenant_id"):
        op.create_index(
            "ix_offer_decisions_tenant_id",
            "offer_decisions",
            ["tenant_id"],
        )
    if not _has_index(bind, "offer_decisions", "ix_offer_decisions_decision_id"):
        op.create_index(
            "ix_offer_decisions_decision_id",
            "offer_decisions",
            ["decision_id"],
        )
    if not _has_index(bind, "offer_decisions", "ix_offer_decisions_automation_id"):
        op.create_index(
            "ix_offer_decisions_automation_id",
            "offer_decisions",
            ["automation_id"],
        )
    if not _has_index(bind, "offer_decisions", "ix_offer_decisions_customer_id"):
        op.create_index(
            "ix_offer_decisions_customer_id",
            "offer_decisions",
            ["customer_id"],
        )
    if not _has_index(bind, "offer_decisions", "ix_offer_decisions_tenant_created"):
        op.create_index(
            "ix_offer_decisions_tenant_created",
            "offer_decisions",
            ["tenant_id", "created_at"],
        )
    if not _has_index(bind, "offer_decisions", "ix_offer_decisions_tenant_surface"):
        op.create_index(
            "ix_offer_decisions_tenant_surface",
            "offer_decisions",
            ["tenant_id", "surface"],
        )
    if not _has_index(bind, "offer_decisions", "ix_offer_decisions_tenant_chosen"):
        op.create_index(
            "ix_offer_decisions_tenant_chosen",
            "offer_decisions",
            ["tenant_id", "chosen_source"],
        )
    if not _has_index(bind, "offer_decisions", "ix_offer_decisions_tenant_attributed"):
        op.create_index(
            "ix_offer_decisions_tenant_attributed",
            "offer_decisions",
            ["tenant_id", "attributed"],
        )
    if (
        _has_table(bind, "offer_decisions")
        and not _has_unique_constraint(bind, "offer_decisions", "uq_offer_decisions_decision_id")
        and bind.dialect.name != "sqlite"
    ):
        op.create_unique_constraint(
            "uq_offer_decisions_decision_id",
            "offer_decisions",
            ["decision_id"],
        )

    # Drop server defaults once the table is created so the ORM is the
    # single source of truth (mirrors migration 0028 / 0027 pattern).
    if _has_table(bind, "offer_decisions"):
        with op.batch_alter_table("offer_decisions") as batch_op:
            batch_op.alter_column("policy_version", server_default=None)
            batch_op.alter_column("attributed", server_default=None)


def downgrade() -> None:
    bind = op.get_bind()
    if _has_unique_constraint(bind, "offer_decisions", "uq_offer_decisions_decision_id"):
        op.drop_constraint(
            "uq_offer_decisions_decision_id",
            "offer_decisions",
            type_="unique",
        )
    for index_name in (
        "ix_offer_decisions_tenant_attributed",
        "ix_offer_decisions_tenant_chosen",
        "ix_offer_decisions_tenant_surface",
        "ix_offer_decisions_tenant_created",
        "ix_offer_decisions_customer_id",
        "ix_offer_decisions_automation_id",
        "ix_offer_decisions_decision_id",
        "ix_offer_decisions_tenant_id",
    ):
        if _has_index(bind, "offer_decisions", index_name):
            op.drop_index(index_name, table_name="offer_decisions")
    if _has_table(bind, "offer_decisions"):
        op.drop_table("offer_decisions")

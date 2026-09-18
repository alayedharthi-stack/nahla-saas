"""Commerce runtime foundation — dormant persistence for durable conversation
ownership, versioned state and immutable per-turn terminals.

Dormant by construction
=======================
The three tables belong to ``core.commerce_runtime.models.RuntimeBase``, a
metadata deliberately separate from the application ``models.Base``. Production
startup pins ``alembic upgrade 0093`` and materialises only ``models.Base``
through ``create_all`` (``backend/main.py``), so this revision changes no
production database unless an owner applies it explicitly. Nothing reads or
writes these tables at runtime; the only callers are the foundation tests.

Graph
=====
The repository intentionally carries two heads: ``0092`` (A1-Validate branch)
and the integration-bootstrap chain that this revision extends (``0107`` →
``0108``). Do not use ``alembic upgrade head``. Apply with
``alembic upgrade 0108`` on a database at ``0107``; ``alembic downgrade 0107``
removes everything this revision created.

Guards
======
Object presence is checked by name (``migration_inspector_helpers``), the
mechanism used by 0104–0107, so a database where the tables were created by
``core.commerce_runtime.models.create_runtime_tables`` reconciles additively:
missing tables, the index, the trigger function and the trigger are created;
nothing is dropped or rewritten.

Revision ID: 0108
Revises: 0107
"""
from __future__ import annotations

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from migration_inspector_helpers import has_index, has_table


revision = "0108"
down_revision = "0107"
branch_labels = None
depends_on = None

_CONVERSATIONS = "commerce_runtime_conversations"
_TURNS = "commerce_runtime_turns"
_TERMINALS = "commerce_runtime_turn_terminals"
_TERMINALS_INDEX = "ix_commerce_runtime_turn_terminals_conversation"
_TRIGGER_FUNCTION = "commerce_runtime_turn_terminals_immutable"
_TRIGGER = "trg_commerce_runtime_turn_terminals_immutable"

_NAMESPACE_SQL = "namespace IN ('live', 'shadow')"

_TRIGGER_FUNCTION_SQL = (
    f"CREATE OR REPLACE FUNCTION {_TRIGGER_FUNCTION}() RETURNS trigger "
    "LANGUAGE plpgsql AS $$ BEGIN "
    f"RAISE EXCEPTION '{_TERMINALS} rows are immutable' "
    "USING ERRCODE = 'integrity_constraint_violation'; END $$;"
)
_TRIGGER_SQL = (
    f"CREATE TRIGGER {_TRIGGER} BEFORE UPDATE OR DELETE ON {_TERMINALS} "
    f"FOR EACH ROW EXECUTE FUNCTION {_TRIGGER_FUNCTION}();"
)


def _conversations_columns() -> list:
    return [
        sa.Column("id", sa.BigInteger(), primary_key=True),
        sa.Column("tenant_id", sa.Integer(), nullable=False),
        sa.Column("namespace", sa.String(length=16), nullable=False),
        sa.Column("conversation_ref", sa.String(length=128), nullable=False),
        sa.Column("next_sequence", sa.BigInteger(), nullable=False, server_default=sa.text("1")),
        sa.Column("ownership_epoch", sa.BigInteger(), nullable=False, server_default=sa.text("0")),
        sa.Column("lease_owner", sa.String(length=128), nullable=True),
        sa.Column("lease_fence", sa.BigInteger(), nullable=False, server_default=sa.text("0")),
        sa.Column("lease_expires_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("state_revision", sa.BigInteger(), nullable=False, server_default=sa.text("0")),
        sa.Column("state_payload", postgresql.JSONB(), nullable=False, server_default=sa.text("'{}'::jsonb")),
        sa.Column("state_committed_fence", sa.BigInteger(), nullable=False, server_default=sa.text("0")),
        sa.Column("state_committed_epoch", sa.BigInteger(), nullable=False, server_default=sa.text("0")),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.text("now()")),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.text("now()")),
    ]


def _turns_columns() -> list:
    return [
        sa.Column("id", sa.BigInteger(), primary_key=True),
        sa.Column("tenant_id", sa.Integer(), nullable=False),
        sa.Column("namespace", sa.String(length=16), nullable=False),
        sa.Column("conversation_id", sa.BigInteger(), nullable=False),
        sa.Column("channel_connection_ref", sa.String(length=128), nullable=False),
        sa.Column("provider_message_id", sa.String(length=256), nullable=False),
        sa.Column("sequence", sa.BigInteger(), nullable=False),
        sa.Column("payload", postgresql.JSONB(), nullable=False, server_default=sa.text("'{}'::jsonb")),
        sa.Column("admitted_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.text("now()")),
    ]


def _terminals_columns() -> list:
    return [
        sa.Column("turn_id", sa.BigInteger(), primary_key=True, autoincrement=False),
        sa.Column("tenant_id", sa.Integer(), nullable=False),
        sa.Column("namespace", sa.String(length=16), nullable=False),
        sa.Column("conversation_id", sa.BigInteger(), nullable=False),
        sa.Column("processing_outcome", sa.String(length=32), nullable=False),
        sa.Column("transport_outcome", sa.String(length=32), nullable=False),
        sa.Column("customer_reach", sa.String(length=32), nullable=False),
        sa.Column("recorded_fence", sa.BigInteger(), nullable=False),
        sa.Column("recorded_epoch", sa.BigInteger(), nullable=False),
        sa.Column("recorded_by", sa.String(length=128), nullable=False),
        sa.Column("details", postgresql.JSONB(), nullable=False, server_default=sa.text("'{}'::jsonb")),
        sa.Column("recorded_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.text("now()")),
    ]


def _trigger_exists(bind) -> bool:
    return bool(
        bind.execute(
            sa.text("SELECT 1 FROM pg_trigger WHERE tgname = :name AND NOT tgisinternal"), {"name": _TRIGGER}
        ).scalar()
    )


def upgrade() -> None:
    bind = op.get_bind()

    if not has_table(bind, _CONVERSATIONS):
        op.create_table(
            _CONVERSATIONS,
            *_conversations_columns(),
            sa.ForeignKeyConstraint(["tenant_id"], ["tenants.id"], name="fk_commerce_runtime_conversations_tenant"),
            sa.UniqueConstraint(
                "tenant_id", "namespace", "conversation_ref", name="uq_commerce_runtime_conversations_identity",
            ),
            sa.UniqueConstraint("id", "tenant_id", "namespace", name="uq_commerce_runtime_conversations_scope"),
            sa.CheckConstraint(_NAMESPACE_SQL, name="ck_commerce_runtime_conversations_namespace"),
            sa.CheckConstraint("next_sequence >= 1", name="ck_commerce_runtime_conversations_next_sequence"),
            sa.CheckConstraint(
                "ownership_epoch >= 0 AND lease_fence >= 0 AND state_revision >= 0",
                name="ck_commerce_runtime_conversations_counters",
            ),
            sa.CheckConstraint(
                "(lease_owner IS NULL) = (lease_expires_at IS NULL)",
                name="ck_commerce_runtime_conversations_lease_pair",
            ),
        )

    if not has_table(bind, _TURNS):
        op.create_table(
            _TURNS,
            *_turns_columns(),
            sa.ForeignKeyConstraint(["tenant_id"], ["tenants.id"], name="fk_commerce_runtime_turns_tenant"),
            sa.ForeignKeyConstraint(
                ["conversation_id", "tenant_id", "namespace"],
                [f"{_CONVERSATIONS}.id", f"{_CONVERSATIONS}.tenant_id", f"{_CONVERSATIONS}.namespace"],
                name="fk_commerce_runtime_turns_conversation_scope",
            ),
            sa.UniqueConstraint(
                "tenant_id", "namespace", "channel_connection_ref", "provider_message_id",
                name="uq_commerce_runtime_turns_admission",
            ),
            sa.UniqueConstraint("conversation_id", "sequence", name="uq_commerce_runtime_turns_order"),
            sa.UniqueConstraint("id", "tenant_id", "namespace", name="uq_commerce_runtime_turns_scope"),
            sa.CheckConstraint(_NAMESPACE_SQL, name="ck_commerce_runtime_turns_namespace"),
            sa.CheckConstraint("sequence >= 1", name="ck_commerce_runtime_turns_sequence"),
        )

    if not has_table(bind, _TERMINALS):
        op.create_table(
            _TERMINALS,
            *_terminals_columns(),
            sa.ForeignKeyConstraint(
                ["tenant_id"], ["tenants.id"], name="fk_commerce_runtime_turn_terminals_tenant",
            ),
            sa.ForeignKeyConstraint(
                ["turn_id", "tenant_id", "namespace"],
                [f"{_TURNS}.id", f"{_TURNS}.tenant_id", f"{_TURNS}.namespace"],
                name="fk_commerce_runtime_turn_terminals_turn_scope",
            ),
            sa.ForeignKeyConstraint(
                ["conversation_id", "tenant_id", "namespace"],
                [f"{_CONVERSATIONS}.id", f"{_CONVERSATIONS}.tenant_id", f"{_CONVERSATIONS}.namespace"],
                name="fk_commerce_runtime_turn_terminals_conversation_scope",
            ),
            sa.CheckConstraint(_NAMESPACE_SQL, name="ck_commerce_runtime_turn_terminals_namespace"),
            sa.CheckConstraint(
                "processing_outcome IN ('completed', 'failed', 'abandoned')",
                name="ck_commerce_runtime_turn_terminals_processing",
            ),
            sa.CheckConstraint(
                "transport_outcome IN ('accepted', 'rejected_definitive', 'unknown', 'not_attempted')",
                name="ck_commerce_runtime_turn_terminals_transport",
            ),
            sa.CheckConstraint(
                "customer_reach IN ('reached', 'not_reached', 'unknown', 'not_applicable')",
                name="ck_commerce_runtime_turn_terminals_reach",
            ),
        )

    if not has_index(bind, _TERMINALS, _TERMINALS_INDEX):
        op.create_index(_TERMINALS_INDEX, _TERMINALS, ["conversation_id"])

    if bind.dialect.name == "postgresql":
        op.execute(sa.text(_TRIGGER_FUNCTION_SQL))
        if not _trigger_exists(bind):
            op.execute(sa.text(_TRIGGER_SQL))


def downgrade() -> None:
    bind = op.get_bind()
    if bind.dialect.name == "postgresql":
        if has_table(bind, _TERMINALS):
            op.execute(sa.text(f"DROP TRIGGER IF EXISTS {_TRIGGER} ON {_TERMINALS}"))
        op.execute(sa.text(f"DROP FUNCTION IF EXISTS {_TRIGGER_FUNCTION}()"))
    for table in (_TERMINALS, _TURNS, _CONVERSATIONS):
        if has_table(bind, table):
            op.drop_table(table)

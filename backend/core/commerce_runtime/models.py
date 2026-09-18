"""Dedicated tables of the dormant commerce runtime foundation.

These models live on their own ``RuntimeBase`` metadata, deliberately outside
the application's ``models.Base``: production startup materialises
``models.Base`` through ``create_all`` and pins ``alembic upgrade 0093``, so
nothing here reaches a production database until revision ``0108`` is
applied on purpose. The Alembic revision and this module declare the same
schema; ``tests/commerce_reliability/test_commerce_runtime_foundation_pg.py``
proves their equivalence on PostgreSQL.

PostgreSQL semantics are assumed (``now()``, row locks, JSONB, the
immutability trigger). SQLite is not a target of this package.
"""
from __future__ import annotations

from sqlalchemy import (
    BigInteger,
    CheckConstraint,
    Column,
    DateTime,
    DDL,
    ForeignKeyConstraint,
    Index,
    Integer,
    String,
    Table,
    UniqueConstraint,
    event,
    text,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import declarative_base

RuntimeBase = declarative_base()

# Reference-only stub so the foreign keys resolve inside this metadata. It is
# never created by this package: ``create_runtime_tables`` creates the three
# runtime tables only, and the application owns the real ``tenants`` table.
tenants_reference = Table("tenants", RuntimeBase.metadata, Column("id", Integer, primary_key=True))

CONVERSATIONS_TABLE = "commerce_runtime_conversations"
TURNS_TABLE = "commerce_runtime_turns"
TERMINALS_TABLE = "commerce_runtime_turn_terminals"

TERMINAL_IMMUTABLE_FUNCTION = "commerce_runtime_turn_terminals_immutable"
TERMINAL_IMMUTABLE_TRIGGER = "trg_commerce_runtime_turn_terminals_immutable"

_NAMESPACE_SQL = "namespace IN ('live', 'shadow')"


class RuntimeConversation(RuntimeBase):
    """Ownership head and versioned state of one conversation in one namespace."""

    __tablename__ = CONVERSATIONS_TABLE

    id = Column(BigInteger, primary_key=True)
    tenant_id = Column(Integer, nullable=False)
    namespace = Column(String(16), nullable=False)
    conversation_ref = Column(String(128), nullable=False)
    next_sequence = Column(BigInteger, nullable=False, server_default=text("1"))
    ownership_epoch = Column(BigInteger, nullable=False, server_default=text("0"))
    lease_owner = Column(String(128), nullable=True)
    lease_fence = Column(BigInteger, nullable=False, server_default=text("0"))
    lease_expires_at = Column(DateTime(timezone=True), nullable=True)
    state_revision = Column(BigInteger, nullable=False, server_default=text("0"))
    state_payload = Column(JSONB, nullable=False, server_default=text("'{}'::jsonb"))
    state_committed_fence = Column(BigInteger, nullable=False, server_default=text("0"))
    state_committed_epoch = Column(BigInteger, nullable=False, server_default=text("0"))
    created_at = Column(DateTime(timezone=True), nullable=False, server_default=text("now()"))
    updated_at = Column(DateTime(timezone=True), nullable=False, server_default=text("now()"))

    __table_args__ = (
        ForeignKeyConstraint(["tenant_id"], ["tenants.id"], name="fk_commerce_runtime_conversations_tenant"),
        UniqueConstraint(
            "tenant_id", "namespace", "conversation_ref", name="uq_commerce_runtime_conversations_identity",
        ),
        UniqueConstraint("id", "tenant_id", "namespace", name="uq_commerce_runtime_conversations_scope"),
        CheckConstraint(_NAMESPACE_SQL, name="ck_commerce_runtime_conversations_namespace"),
        CheckConstraint("next_sequence >= 1", name="ck_commerce_runtime_conversations_next_sequence"),
        CheckConstraint(
            "ownership_epoch >= 0 AND lease_fence >= 0 AND state_revision >= 0",
            name="ck_commerce_runtime_conversations_counters",
        ),
        CheckConstraint(
            "(lease_owner IS NULL) = (lease_expires_at IS NULL)",
            name="ck_commerce_runtime_conversations_lease_pair",
        ),
    )


class RuntimeTurn(RuntimeBase):
    """One durably admitted inbound message with its per-conversation sequence."""

    __tablename__ = TURNS_TABLE

    id = Column(BigInteger, primary_key=True)
    tenant_id = Column(Integer, nullable=False)
    namespace = Column(String(16), nullable=False)
    conversation_id = Column(BigInteger, nullable=False)
    channel_connection_ref = Column(String(128), nullable=False)
    provider_message_id = Column(String(256), nullable=False)
    sequence = Column(BigInteger, nullable=False)
    payload = Column(JSONB, nullable=False, server_default=text("'{}'::jsonb"))
    admitted_at = Column(DateTime(timezone=True), nullable=False, server_default=text("now()"))

    __table_args__ = (
        ForeignKeyConstraint(["tenant_id"], ["tenants.id"], name="fk_commerce_runtime_turns_tenant"),
        ForeignKeyConstraint(
            ["conversation_id", "tenant_id", "namespace"],
            [
                f"{CONVERSATIONS_TABLE}.id",
                f"{CONVERSATIONS_TABLE}.tenant_id",
                f"{CONVERSATIONS_TABLE}.namespace",
            ],
            name="fk_commerce_runtime_turns_conversation_scope",
        ),
        UniqueConstraint(
            "tenant_id", "namespace", "channel_connection_ref", "provider_message_id",
            name="uq_commerce_runtime_turns_admission",
        ),
        UniqueConstraint("conversation_id", "sequence", name="uq_commerce_runtime_turns_order"),
        UniqueConstraint("id", "tenant_id", "namespace", name="uq_commerce_runtime_turns_scope"),
        CheckConstraint(_NAMESPACE_SQL, name="ck_commerce_runtime_turns_namespace"),
        CheckConstraint("sequence >= 1", name="ck_commerce_runtime_turns_sequence"),
    )


class RuntimeTurnTerminal(RuntimeBase):
    """The single immutable terminal processing record of a turn."""

    __tablename__ = TERMINALS_TABLE

    turn_id = Column(BigInteger, primary_key=True, autoincrement=False)
    tenant_id = Column(Integer, nullable=False)
    namespace = Column(String(16), nullable=False)
    conversation_id = Column(BigInteger, nullable=False)
    processing_outcome = Column(String(32), nullable=False)
    transport_outcome = Column(String(32), nullable=False)
    customer_reach = Column(String(32), nullable=False)
    recorded_fence = Column(BigInteger, nullable=False)
    recorded_epoch = Column(BigInteger, nullable=False)
    recorded_by = Column(String(128), nullable=False)
    details = Column(JSONB, nullable=False, server_default=text("'{}'::jsonb"))
    recorded_at = Column(DateTime(timezone=True), nullable=False, server_default=text("now()"))

    __table_args__ = (
        ForeignKeyConstraint(["tenant_id"], ["tenants.id"], name="fk_commerce_runtime_turn_terminals_tenant"),
        ForeignKeyConstraint(
            ["turn_id", "tenant_id", "namespace"],
            [f"{TURNS_TABLE}.id", f"{TURNS_TABLE}.tenant_id", f"{TURNS_TABLE}.namespace"],
            name="fk_commerce_runtime_turn_terminals_turn_scope",
        ),
        ForeignKeyConstraint(
            ["conversation_id", "tenant_id", "namespace"],
            [
                f"{CONVERSATIONS_TABLE}.id",
                f"{CONVERSATIONS_TABLE}.tenant_id",
                f"{CONVERSATIONS_TABLE}.namespace",
            ],
            name="fk_commerce_runtime_turn_terminals_conversation_scope",
        ),
        CheckConstraint(_NAMESPACE_SQL, name="ck_commerce_runtime_turn_terminals_namespace"),
        CheckConstraint(
            "processing_outcome IN ('completed', 'failed', 'abandoned')",
            name="ck_commerce_runtime_turn_terminals_processing",
        ),
        CheckConstraint(
            "transport_outcome IN ('accepted', 'rejected_definitive', 'unknown', 'not_attempted')",
            name="ck_commerce_runtime_turn_terminals_transport",
        ),
        CheckConstraint(
            "customer_reach IN ('reached', 'not_reached', 'unknown', 'not_applicable')",
            name="ck_commerce_runtime_turn_terminals_reach",
        ),
        Index("ix_commerce_runtime_turn_terminals_conversation", "conversation_id"),
    )


# PostgreSQL immutability: a terminal row can be inserted once and never
# updated or deleted. The same DDL is issued by revision 0108.
TERMINAL_IMMUTABLE_FUNCTION_SQL = (
    f"CREATE OR REPLACE FUNCTION {TERMINAL_IMMUTABLE_FUNCTION}() RETURNS trigger "
    "LANGUAGE plpgsql AS $$ BEGIN "
    f"RAISE EXCEPTION '{TERMINALS_TABLE} rows are immutable' "
    "USING ERRCODE = 'integrity_constraint_violation'; END $$;"
)
TERMINAL_IMMUTABLE_TRIGGER_SQL = (
    f"CREATE TRIGGER {TERMINAL_IMMUTABLE_TRIGGER} BEFORE UPDATE OR DELETE ON {TERMINALS_TABLE} "
    f"FOR EACH ROW EXECUTE FUNCTION {TERMINAL_IMMUTABLE_FUNCTION}();"
)

event.listen(
    RuntimeTurnTerminal.__table__, "after_create",
    DDL(TERMINAL_IMMUTABLE_FUNCTION_SQL).execute_if(dialect="postgresql"),
)
event.listen(
    RuntimeTurnTerminal.__table__, "after_create",
    DDL(TERMINAL_IMMUTABLE_TRIGGER_SQL).execute_if(dialect="postgresql"),
)

RUNTIME_TABLES = (RuntimeConversation.__table__, RuntimeTurn.__table__, RuntimeTurnTerminal.__table__)


def create_runtime_tables(bind) -> None:
    """Create the three runtime tables (test and development use).

    The referenced ``tenants`` table must already exist. Production never
    calls this: the reviewed path is Alembic revision ``0108``.
    """
    RuntimeBase.metadata.create_all(bind, tables=list(RUNTIME_TABLES))


__all__ = [
    "CONVERSATIONS_TABLE", "RUNTIME_TABLES", "RuntimeBase", "RuntimeConversation", "RuntimeTurn",
    "RuntimeTurnTerminal", "TERMINALS_TABLE", "TERMINAL_IMMUTABLE_FUNCTION",
    "TERMINAL_IMMUTABLE_FUNCTION_SQL", "TERMINAL_IMMUTABLE_TRIGGER", "TERMINAL_IMMUTABLE_TRIGGER_SQL",
    "TURNS_TABLE", "create_runtime_tables", "tenants_reference",
]

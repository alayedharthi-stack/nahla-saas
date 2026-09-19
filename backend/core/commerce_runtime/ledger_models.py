"""Dedicated tables of the dormant effect and delivery ledgers.

They live on the foundation's ``RuntimeBase`` metadata (so their foreign
keys resolve to the foundation tables) and, like it, deliberately outside
the application's ``models.Base``: production startup materialises
``models.Base`` through ``create_all`` and pins ``alembic upgrade 0093``, so
nothing here reaches a production database until revision ``0109`` is
applied on purpose. The Alembic revision and this module declare the same
schema; the ledger proofs verify their equivalence on PostgreSQL.

Two head rows carry a mutable projection (``commerce_runtime_effects.status``,
``commerce_runtime_delivery_sequences.outcome``); every attempt, result and
receipt row is append-only and enforced so by a PostgreSQL trigger.
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
    UniqueConstraint,
    event,
    text,
)
from sqlalchemy.dialects.postgresql import JSONB

from core.commerce_runtime.models import CONVERSATIONS_TABLE, TURNS_TABLE, RuntimeBase

EFFECTS_TABLE = "commerce_runtime_effects"
EFFECT_ATTEMPTS_TABLE = "commerce_runtime_effect_attempts"
EFFECT_RESULTS_TABLE = "commerce_runtime_effect_results"
DELIVERY_SEQUENCES_TABLE = "commerce_runtime_delivery_sequences"
DELIVERY_ATTEMPTS_TABLE = "commerce_runtime_delivery_attempts"
DELIVERY_RECEIPTS_TABLE = "commerce_runtime_delivery_receipts"

LEDGER_IMMUTABLE_FUNCTION = "commerce_runtime_ledger_rows_immutable"
LEDGER_IMMUTABLE_TRIGGER = "trg_commerce_runtime_ledger_immutable"   # same name on every append-only relation

_NAMESPACE_SQL = "namespace IN ('live', 'shadow')"
_EFFECT_STATUS_SQL = "status IN ('reserved', 'dispatching', 'confirmed', 'rejected', 'unknown')"
_EFFECT_OUTCOME_SQL = "outcome IN ('confirmed', 'rejected', 'unknown')"
_DELIVERY_OUTCOME_SQL = "outcome IN ('pending', 'accepted', 'rejected', 'unknown')"
_DELIVERY_KIND_SQL = "kind IN ('rich', 'text')"
_INTENT_KIND_SQL = "intent_kind IN ('rich', 'text')"
_RECEIPT_KIND_SQL = "kind IN ('accepted', 'rejected', 'unknown', 'delivered', 'read', 'failed')"


class RuntimeEffect(RuntimeBase):
    """One reserved external mutation: business identity, binding and status projection."""

    __tablename__ = EFFECTS_TABLE

    id = Column(BigInteger, primary_key=True)
    tenant_id = Column(Integer, nullable=False)
    namespace = Column(String(16), nullable=False)
    conversation_id = Column(BigInteger, nullable=False)
    turn_id = Column(BigInteger, nullable=False)
    action_type = Column(String(64), nullable=False)
    idempotency_key = Column(String(128), nullable=False)
    payload = Column(JSONB, nullable=False, server_default=text("'{}'::jsonb"))
    payload_hash = Column(String(64), nullable=False)
    status = Column(String(32), nullable=False, server_default=text("'reserved'"))
    attempt_count = Column(BigInteger, nullable=False, server_default=text("0"))
    reserved_by = Column(String(128), nullable=False)
    reserved_fence = Column(BigInteger, nullable=False)
    reserved_epoch = Column(BigInteger, nullable=False)
    confirmed_result = Column(JSONB, nullable=True)
    created_at = Column(DateTime(timezone=True), nullable=False, server_default=text("now()"))
    updated_at = Column(DateTime(timezone=True), nullable=False, server_default=text("now()"))

    __table_args__ = (
        ForeignKeyConstraint(["tenant_id"], ["tenants.id"], name="fk_commerce_runtime_effects_tenant"),
        ForeignKeyConstraint(
            ["conversation_id", "tenant_id", "namespace"],
            [f"{CONVERSATIONS_TABLE}.id", f"{CONVERSATIONS_TABLE}.tenant_id", f"{CONVERSATIONS_TABLE}.namespace"],
            name="fk_commerce_runtime_effects_conversation_scope",
        ),
        ForeignKeyConstraint(
            ["turn_id", "tenant_id", "namespace"],
            [f"{TURNS_TABLE}.id", f"{TURNS_TABLE}.tenant_id", f"{TURNS_TABLE}.namespace"],
            name="fk_commerce_runtime_effects_turn_scope",
        ),
        UniqueConstraint("tenant_id", "namespace", "idempotency_key", name="uq_commerce_runtime_effects_key"),
        UniqueConstraint("id", "tenant_id", "namespace", "conversation_id", name="uq_commerce_runtime_effects_scope"),
        CheckConstraint(_NAMESPACE_SQL, name="ck_commerce_runtime_effects_namespace"),
        CheckConstraint(_EFFECT_STATUS_SQL, name="ck_commerce_runtime_effects_status"),
        CheckConstraint("attempt_count >= 0", name="ck_commerce_runtime_effects_attempt_count"),
        CheckConstraint(
            "(status = 'confirmed') = (confirmed_result IS NOT NULL)",
            name="ck_commerce_runtime_effects_confirmed_pair",
        ),
        Index("ix_commerce_runtime_effects_turn", "turn_id"),
    )


class RuntimeEffectAttempt(RuntimeBase):
    """One durable dispatch reservation of an effect (append-only)."""

    __tablename__ = EFFECT_ATTEMPTS_TABLE

    id = Column(BigInteger, primary_key=True)
    effect_id = Column(BigInteger, nullable=False)
    tenant_id = Column(Integer, nullable=False)
    namespace = Column(String(16), nullable=False)
    conversation_id = Column(BigInteger, nullable=False)
    attempt_no = Column(BigInteger, nullable=False)
    dispatch_key = Column(String(64), nullable=False)
    reserved_by = Column(String(128), nullable=False)
    reserved_fence = Column(BigInteger, nullable=False)
    reserved_epoch = Column(BigInteger, nullable=False)
    reserved_at = Column(DateTime(timezone=True), nullable=False, server_default=text("now()"))

    __table_args__ = (
        ForeignKeyConstraint(
            ["effect_id", "tenant_id", "namespace", "conversation_id"],
            [f"{EFFECTS_TABLE}.id", f"{EFFECTS_TABLE}.tenant_id", f"{EFFECTS_TABLE}.namespace",
             f"{EFFECTS_TABLE}.conversation_id"],
            name="fk_commerce_runtime_effect_attempts_effect_scope",
        ),
        UniqueConstraint("effect_id", "attempt_no", name="uq_commerce_runtime_effect_attempts_order"),
        UniqueConstraint("dispatch_key", name="uq_commerce_runtime_effect_attempts_dispatch_key"),
        UniqueConstraint("id", "effect_id", name="uq_commerce_runtime_effect_attempts_scope"),
        CheckConstraint(_NAMESPACE_SQL, name="ck_commerce_runtime_effect_attempts_namespace"),
        CheckConstraint("attempt_no >= 1", name="ck_commerce_runtime_effect_attempts_attempt_no"),
    )


class RuntimeEffectResult(RuntimeBase):
    """Append-only outcome evidence of one effect attempt."""

    __tablename__ = EFFECT_RESULTS_TABLE

    id = Column(BigInteger, primary_key=True)
    attempt_id = Column(BigInteger, nullable=False)
    effect_id = Column(BigInteger, nullable=False)
    result_no = Column(BigInteger, nullable=False)
    outcome = Column(String(32), nullable=False)
    evidence = Column(JSONB, nullable=False, server_default=text("'{}'::jsonb"))
    recorded_by = Column(String(128), nullable=False)
    recorded_at = Column(DateTime(timezone=True), nullable=False, server_default=text("now()"))

    __table_args__ = (
        ForeignKeyConstraint(
            ["attempt_id", "effect_id"],
            [f"{EFFECT_ATTEMPTS_TABLE}.id", f"{EFFECT_ATTEMPTS_TABLE}.effect_id"],
            name="fk_commerce_runtime_effect_results_attempt_scope",
        ),
        UniqueConstraint("attempt_id", "result_no", name="uq_commerce_runtime_effect_results_order"),
        CheckConstraint("result_no >= 1", name="ck_commerce_runtime_effect_results_result_no"),
        CheckConstraint(_EFFECT_OUTCOME_SQL, name="ck_commerce_runtime_effect_results_outcome"),
        Index("ix_commerce_runtime_effect_results_effect", "effect_id"),
    )


class RuntimeDeliverySequence(RuntimeBase):
    """The single logical outbound delivery of one turn, with its transport projection."""

    __tablename__ = DELIVERY_SEQUENCES_TABLE

    id = Column(BigInteger, primary_key=True)
    tenant_id = Column(Integer, nullable=False)
    namespace = Column(String(16), nullable=False)
    conversation_id = Column(BigInteger, nullable=False)
    turn_id = Column(BigInteger, nullable=False)
    intent_kind = Column(String(16), nullable=False)
    intent_payload = Column(JSONB, nullable=False, server_default=text("'{}'::jsonb"))
    intent_hash = Column(String(64), nullable=False)
    attempt_count = Column(BigInteger, nullable=False, server_default=text("0"))
    outcome = Column(String(32), nullable=False, server_default=text("'pending'"))
    reserved_by = Column(String(128), nullable=False)
    reserved_fence = Column(BigInteger, nullable=False)
    reserved_epoch = Column(BigInteger, nullable=False)
    created_at = Column(DateTime(timezone=True), nullable=False, server_default=text("now()"))
    updated_at = Column(DateTime(timezone=True), nullable=False, server_default=text("now()"))

    __table_args__ = (
        ForeignKeyConstraint(["tenant_id"], ["tenants.id"], name="fk_commerce_runtime_delivery_sequences_tenant"),
        ForeignKeyConstraint(
            ["conversation_id", "tenant_id", "namespace"],
            [f"{CONVERSATIONS_TABLE}.id", f"{CONVERSATIONS_TABLE}.tenant_id", f"{CONVERSATIONS_TABLE}.namespace"],
            name="fk_commerce_runtime_delivery_sequences_conversation_scope",
        ),
        ForeignKeyConstraint(
            ["turn_id", "tenant_id", "namespace"],
            [f"{TURNS_TABLE}.id", f"{TURNS_TABLE}.tenant_id", f"{TURNS_TABLE}.namespace"],
            name="fk_commerce_runtime_delivery_sequences_turn_scope",
        ),
        UniqueConstraint("turn_id", name="uq_commerce_runtime_delivery_sequences_turn"),
        UniqueConstraint(
            "id", "tenant_id", "namespace", "conversation_id", name="uq_commerce_runtime_delivery_sequences_scope",
        ),
        CheckConstraint(_NAMESPACE_SQL, name="ck_commerce_runtime_delivery_sequences_namespace"),
        CheckConstraint(_INTENT_KIND_SQL, name="ck_commerce_runtime_delivery_sequences_intent_kind"),
        CheckConstraint(_DELIVERY_OUTCOME_SQL, name="ck_commerce_runtime_delivery_sequences_outcome"),
        CheckConstraint("attempt_count >= 0", name="ck_commerce_runtime_delivery_sequences_attempt_count"),
    )


class RuntimeDeliveryAttempt(RuntimeBase):
    """One durable dispatch reservation inside a delivery sequence (append-only)."""

    __tablename__ = DELIVERY_ATTEMPTS_TABLE

    id = Column(BigInteger, primary_key=True)
    sequence_id = Column(BigInteger, nullable=False)
    tenant_id = Column(Integer, nullable=False)
    namespace = Column(String(16), nullable=False)
    conversation_id = Column(BigInteger, nullable=False)
    attempt_no = Column(BigInteger, nullable=False)
    kind = Column(String(16), nullable=False)
    dispatch_key = Column(String(64), nullable=False)
    payload = Column(JSONB, nullable=False, server_default=text("'{}'::jsonb"))
    reserved_by = Column(String(128), nullable=False)
    reserved_fence = Column(BigInteger, nullable=False)
    reserved_epoch = Column(BigInteger, nullable=False)
    reserved_at = Column(DateTime(timezone=True), nullable=False, server_default=text("now()"))

    __table_args__ = (
        ForeignKeyConstraint(
            ["sequence_id", "tenant_id", "namespace", "conversation_id"],
            [f"{DELIVERY_SEQUENCES_TABLE}.id", f"{DELIVERY_SEQUENCES_TABLE}.tenant_id",
             f"{DELIVERY_SEQUENCES_TABLE}.namespace", f"{DELIVERY_SEQUENCES_TABLE}.conversation_id"],
            name="fk_commerce_runtime_delivery_attempts_sequence_scope",
        ),
        UniqueConstraint("sequence_id", "attempt_no", name="uq_commerce_runtime_delivery_attempts_order"),
        UniqueConstraint("dispatch_key", name="uq_commerce_runtime_delivery_attempts_dispatch_key"),
        UniqueConstraint("id", "sequence_id", name="uq_commerce_runtime_delivery_attempts_scope"),
        CheckConstraint(_NAMESPACE_SQL, name="ck_commerce_runtime_delivery_attempts_namespace"),
        CheckConstraint(_DELIVERY_KIND_SQL, name="ck_commerce_runtime_delivery_attempts_kind"),
        CheckConstraint("attempt_no >= 1", name="ck_commerce_runtime_delivery_attempts_attempt_no"),
    )


class RuntimeDeliveryReceipt(RuntimeBase):
    """Append-only transport and reach evidence of one delivery attempt."""

    __tablename__ = DELIVERY_RECEIPTS_TABLE

    id = Column(BigInteger, primary_key=True)
    attempt_id = Column(BigInteger, nullable=False)
    sequence_id = Column(BigInteger, nullable=False)
    receipt_no = Column(BigInteger, nullable=False)
    kind = Column(String(32), nullable=False)
    provider_message_id = Column(String(256), nullable=True)
    evidence = Column(JSONB, nullable=False, server_default=text("'{}'::jsonb"))
    recorded_by = Column(String(128), nullable=False)
    recorded_at = Column(DateTime(timezone=True), nullable=False, server_default=text("now()"))

    __table_args__ = (
        ForeignKeyConstraint(
            ["attempt_id", "sequence_id"],
            [f"{DELIVERY_ATTEMPTS_TABLE}.id", f"{DELIVERY_ATTEMPTS_TABLE}.sequence_id"],
            name="fk_commerce_runtime_delivery_receipts_attempt_scope",
        ),
        UniqueConstraint("attempt_id", "receipt_no", name="uq_commerce_runtime_delivery_receipts_order"),
        CheckConstraint("receipt_no >= 1", name="ck_commerce_runtime_delivery_receipts_receipt_no"),
        CheckConstraint(_RECEIPT_KIND_SQL, name="ck_commerce_runtime_delivery_receipts_kind"),
        CheckConstraint(
            "kind <> 'accepted' OR provider_message_id IS NOT NULL",
            name="ck_commerce_runtime_delivery_receipts_accepted_id",
        ),
        Index("ix_commerce_runtime_delivery_receipts_sequence", "sequence_id"),
    )


# PostgreSQL append-only enforcement: an attempt, result or receipt row can be
# inserted once and never updated or deleted. The same DDL is issued by
# revision 0109. One function, one same-named trigger per relation.
LEDGER_IMMUTABLE_FUNCTION_SQL = (
    f"CREATE OR REPLACE FUNCTION {LEDGER_IMMUTABLE_FUNCTION}() RETURNS trigger "
    "LANGUAGE plpgsql AS $$ BEGIN "
    "RAISE EXCEPTION '% rows are append-only', TG_TABLE_NAME "
    "USING ERRCODE = 'integrity_constraint_violation'; END $$;"
)

APPEND_ONLY_TABLES = (EFFECT_ATTEMPTS_TABLE, EFFECT_RESULTS_TABLE, DELIVERY_ATTEMPTS_TABLE, DELIVERY_RECEIPTS_TABLE)


def ledger_immutable_trigger_sql(table: str) -> str:
    return (
        f"CREATE TRIGGER {LEDGER_IMMUTABLE_TRIGGER} BEFORE UPDATE OR DELETE ON {table} "
        f"FOR EACH ROW EXECUTE FUNCTION {LEDGER_IMMUTABLE_FUNCTION}();"
    )


# ``DDL`` applies ``%`` substitution to its statement, so the literal ``%`` of
# the RAISE format string is escaped here and nowhere else.
for _model in (RuntimeEffectAttempt, RuntimeEffectResult, RuntimeDeliveryAttempt, RuntimeDeliveryReceipt):
    event.listen(
        _model.__table__, "after_create",
        DDL(LEDGER_IMMUTABLE_FUNCTION_SQL.replace("%", "%%")).execute_if(dialect="postgresql"),
    )
    event.listen(
        _model.__table__, "after_create",
        DDL(ledger_immutable_trigger_sql(_model.__tablename__)).execute_if(dialect="postgresql"),
    )

LEDGER_TABLES = (
    RuntimeEffect.__table__, RuntimeEffectAttempt.__table__, RuntimeEffectResult.__table__,
    RuntimeDeliverySequence.__table__, RuntimeDeliveryAttempt.__table__, RuntimeDeliveryReceipt.__table__,
)


def create_ledger_tables(bind) -> None:
    """Create the six ledger tables (test and development use).

    The foundation tables and the referenced ``tenants`` table must already
    exist. Production never calls this: the reviewed path is revision ``0109``.
    """
    RuntimeBase.metadata.create_all(bind, tables=list(LEDGER_TABLES))


__all__ = [
    "APPEND_ONLY_TABLES", "DELIVERY_ATTEMPTS_TABLE", "DELIVERY_RECEIPTS_TABLE", "DELIVERY_SEQUENCES_TABLE",
    "EFFECTS_TABLE", "EFFECT_ATTEMPTS_TABLE", "EFFECT_RESULTS_TABLE", "LEDGER_IMMUTABLE_FUNCTION",
    "LEDGER_IMMUTABLE_FUNCTION_SQL", "LEDGER_IMMUTABLE_TRIGGER", "LEDGER_TABLES", "RuntimeDeliveryAttempt",
    "RuntimeDeliveryReceipt", "RuntimeDeliverySequence", "RuntimeEffect", "RuntimeEffectAttempt",
    "RuntimeEffectResult", "create_ledger_tables", "ledger_immutable_trigger_sql",
]

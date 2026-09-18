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

Reconciliation policy (explicit; nothing is stamped silently)
=============================================================
Exactly these pre-existing states are reconciled:

* a runtime table is absent → it is created as defined here;
* ``ix_commerce_runtime_turn_terminals_conversation`` is absent → created;
* the immutability trigger function is absent → created;
* the immutability trigger is absent **on the runtime terminal relation** →
  created (a same-named trigger on any other relation is irrelevant).

Every other pre-existing shape is verified **by definition** against the
schema this revision produces on a fresh database: every column's type,
length, nullability and default expression; every constraint's
``pg_get_constraintdef``; every index's ``pg_indexes.indexdef``; the trigger's
relation, enabled state, timing, events, row level and function
(``pg_get_triggerdef`` on the target relation); and the function body. Any
difference raises, the transaction aborts and ``alembic_version`` is not
advanced. The verifier (``schema_differences``) is also the post-condition
of a successful upgrade.

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

# Definitions of the schema this revision produces on a fresh database, as
# PostgreSQL reports them. Generated from a fresh ``alembic upgrade 0108``
# and used to verify any pre-existing shape by definition.
_EXPECTED = {
    'commerce_runtime_conversations': {
        "columns": (
        ('id', 'int8', None, 'NO', "nextval('commerce_runtime_conversations_id_seq'::regclass)"),
        ('tenant_id', 'int4', None, 'NO', None),
        ('namespace', 'varchar', 16, 'NO', None),
        ('conversation_ref', 'varchar', 128, 'NO', None),
        ('next_sequence', 'int8', None, 'NO', '1'),
        ('ownership_epoch', 'int8', None, 'NO', '0'),
        ('lease_owner', 'varchar', 128, 'YES', None),
        ('lease_fence', 'int8', None, 'NO', '0'),
        ('lease_expires_at', 'timestamptz', None, 'YES', None),
        ('state_revision', 'int8', None, 'NO', '0'),
        ('state_payload', 'jsonb', None, 'NO', "'{}'::jsonb"),
        ('state_committed_fence', 'int8', None, 'NO', '0'),
        ('state_committed_epoch', 'int8', None, 'NO', '0'),
        ('created_at', 'timestamptz', None, 'NO', 'now()'),
        ('updated_at', 'timestamptz', None, 'NO', 'now()'),
    ),
        "constraints": {
        'ck_commerce_runtime_conversations_counters': ('c', 'CHECK (((ownership_epoch >= 0) AND (lease_fence >= 0) AND (state_revision >= 0)))'),
        'ck_commerce_runtime_conversations_lease_pair': ('c', 'CHECK (((lease_owner IS NULL) = (lease_expires_at IS NULL)))'),
        'ck_commerce_runtime_conversations_namespace': ('c', "CHECK (((namespace)::text = ANY ((ARRAY['live'::character varying, 'shadow'::character varying])::text[])))"),
        'ck_commerce_runtime_conversations_next_sequence': ('c', 'CHECK ((next_sequence >= 1))'),
        'commerce_runtime_conversations_pkey': ('p', 'PRIMARY KEY (id)'),
        'fk_commerce_runtime_conversations_tenant': ('f', 'FOREIGN KEY (tenant_id) REFERENCES tenants(id)'),
        'uq_commerce_runtime_conversations_identity': ('u', 'UNIQUE (tenant_id, namespace, conversation_ref)'),
        'uq_commerce_runtime_conversations_scope': ('u', 'UNIQUE (id, tenant_id, namespace)'),
    },
        "indexes": {
        'commerce_runtime_conversations_pkey': 'CREATE UNIQUE INDEX commerce_runtime_conversations_pkey ON public.commerce_runtime_conversations USING btree (id)',
        'uq_commerce_runtime_conversations_identity': 'CREATE UNIQUE INDEX uq_commerce_runtime_conversations_identity ON public.commerce_runtime_conversations USING btree (tenant_id, namespace, conversation_ref)',
        'uq_commerce_runtime_conversations_scope': 'CREATE UNIQUE INDEX uq_commerce_runtime_conversations_scope ON public.commerce_runtime_conversations USING btree (id, tenant_id, namespace)',
    },
    },
    'commerce_runtime_turns': {
        "columns": (
        ('id', 'int8', None, 'NO', "nextval('commerce_runtime_turns_id_seq'::regclass)"),
        ('tenant_id', 'int4', None, 'NO', None),
        ('namespace', 'varchar', 16, 'NO', None),
        ('conversation_id', 'int8', None, 'NO', None),
        ('channel_connection_ref', 'varchar', 128, 'NO', None),
        ('provider_message_id', 'varchar', 256, 'NO', None),
        ('sequence', 'int8', None, 'NO', None),
        ('payload', 'jsonb', None, 'NO', "'{}'::jsonb"),
        ('admitted_at', 'timestamptz', None, 'NO', 'now()'),
    ),
        "constraints": {
        'ck_commerce_runtime_turns_namespace': ('c', "CHECK (((namespace)::text = ANY ((ARRAY['live'::character varying, 'shadow'::character varying])::text[])))"),
        'ck_commerce_runtime_turns_sequence': ('c', 'CHECK ((sequence >= 1))'),
        'commerce_runtime_turns_pkey': ('p', 'PRIMARY KEY (id)'),
        'fk_commerce_runtime_turns_conversation_scope': ('f', 'FOREIGN KEY (conversation_id, tenant_id, namespace) REFERENCES commerce_runtime_conversations(id, tenant_id, namespace)'),
        'fk_commerce_runtime_turns_tenant': ('f', 'FOREIGN KEY (tenant_id) REFERENCES tenants(id)'),
        'uq_commerce_runtime_turns_admission': ('u', 'UNIQUE (tenant_id, namespace, channel_connection_ref, provider_message_id)'),
        'uq_commerce_runtime_turns_order': ('u', 'UNIQUE (conversation_id, sequence)'),
        'uq_commerce_runtime_turns_scope': ('u', 'UNIQUE (id, tenant_id, namespace)'),
    },
        "indexes": {
        'commerce_runtime_turns_pkey': 'CREATE UNIQUE INDEX commerce_runtime_turns_pkey ON public.commerce_runtime_turns USING btree (id)',
        'uq_commerce_runtime_turns_admission': 'CREATE UNIQUE INDEX uq_commerce_runtime_turns_admission ON public.commerce_runtime_turns USING btree (tenant_id, namespace, channel_connection_ref, provider_message_id)',
        'uq_commerce_runtime_turns_order': 'CREATE UNIQUE INDEX uq_commerce_runtime_turns_order ON public.commerce_runtime_turns USING btree (conversation_id, sequence)',
        'uq_commerce_runtime_turns_scope': 'CREATE UNIQUE INDEX uq_commerce_runtime_turns_scope ON public.commerce_runtime_turns USING btree (id, tenant_id, namespace)',
    },
    },
    'commerce_runtime_turn_terminals': {
        "columns": (
        ('turn_id', 'int8', None, 'NO', None),
        ('tenant_id', 'int4', None, 'NO', None),
        ('namespace', 'varchar', 16, 'NO', None),
        ('conversation_id', 'int8', None, 'NO', None),
        ('processing_outcome', 'varchar', 32, 'NO', None),
        ('transport_outcome', 'varchar', 32, 'NO', None),
        ('customer_reach', 'varchar', 32, 'NO', None),
        ('recorded_fence', 'int8', None, 'NO', None),
        ('recorded_epoch', 'int8', None, 'NO', None),
        ('recorded_by', 'varchar', 128, 'NO', None),
        ('details', 'jsonb', None, 'NO', "'{}'::jsonb"),
        ('recorded_at', 'timestamptz', None, 'NO', 'now()'),
    ),
        "constraints": {
        'ck_commerce_runtime_turn_terminals_namespace': ('c', "CHECK (((namespace)::text = ANY ((ARRAY['live'::character varying, 'shadow'::character varying])::text[])))"),
        'ck_commerce_runtime_turn_terminals_processing': ('c', "CHECK (((processing_outcome)::text = ANY ((ARRAY['completed'::character varying, 'failed'::character varying, 'abandoned'::character varying])::text[])))"),
        'ck_commerce_runtime_turn_terminals_reach': ('c', "CHECK (((customer_reach)::text = ANY ((ARRAY['reached'::character varying, 'not_reached'::character varying, 'unknown'::character varying, 'not_applicable'::character varying])::text[])))"),
        'ck_commerce_runtime_turn_terminals_transport': ('c', "CHECK (((transport_outcome)::text = ANY ((ARRAY['accepted'::character varying, 'rejected_definitive'::character varying, 'unknown'::character varying, 'not_attempted'::character varying])::text[])))"),
        'commerce_runtime_turn_terminals_pkey': ('p', 'PRIMARY KEY (turn_id)'),
        'fk_commerce_runtime_turn_terminals_conversation_scope': ('f', 'FOREIGN KEY (conversation_id, tenant_id, namespace) REFERENCES commerce_runtime_conversations(id, tenant_id, namespace)'),
        'fk_commerce_runtime_turn_terminals_tenant': ('f', 'FOREIGN KEY (tenant_id) REFERENCES tenants(id)'),
        'fk_commerce_runtime_turn_terminals_turn_scope': ('f', 'FOREIGN KEY (turn_id, tenant_id, namespace) REFERENCES commerce_runtime_turns(id, tenant_id, namespace)'),
    },
        "indexes": {
        'commerce_runtime_turn_terminals_pkey': 'CREATE UNIQUE INDEX commerce_runtime_turn_terminals_pkey ON public.commerce_runtime_turn_terminals USING btree (turn_id)',
        'ix_commerce_runtime_turn_terminals_conversation': 'CREATE INDEX ix_commerce_runtime_turn_terminals_conversation ON public.commerce_runtime_turn_terminals USING btree (conversation_id)',
    },
    },
}
_EXPECTED_TRIGGER_DEF = 'CREATE TRIGGER trg_commerce_runtime_turn_terminals_immutable BEFORE DELETE OR UPDATE ON public.commerce_runtime_turn_terminals FOR EACH ROW EXECUTE FUNCTION commerce_runtime_turn_terminals_immutable()'
_EXPECTED_FUNCTION_BODY = "BEGIN RAISE EXCEPTION 'commerce_runtime_turn_terminals rows are immutable' USING ERRCODE = 'integrity_constraint_violation'; END"

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


def _norm(text_value: str) -> str:
    return " ".join(str(text_value or "").split())


def _table_regclass(table: str) -> str:
    return f"public.{table}"


def _actual_columns(bind, table: str):
    rows = bind.execute(sa.text(
        "SELECT column_name, udt_name, character_maximum_length, is_nullable, column_default "
        "FROM information_schema.columns WHERE table_schema = 'public' AND table_name = :t "
        "ORDER BY ordinal_position"), {"t": table}).all()
    return {r[0]: (r[1], r[2], r[3], r[4]) for r in rows}


def _actual_constraints(bind, table: str):
    rows = bind.execute(sa.text(
        "SELECT conname, contype, pg_get_constraintdef(oid) FROM pg_constraint "
        "WHERE conrelid = CAST(:t AS regclass)"), {"t": _table_regclass(table)}).all()
    return {r[0]: (str(r[1]), _norm(r[2])) for r in rows}


def _actual_indexes(bind, table: str):
    rows = bind.execute(sa.text(
        "SELECT indexname, indexdef FROM pg_indexes WHERE schemaname = 'public' AND tablename = :t"),
        {"t": table}).all()
    return {r[0]: _norm(r[1]) for r in rows}


def _trigger_row(bind):
    """The immutability trigger on the runtime terminal relation itself, or None."""
    return bind.execute(sa.text(
        "SELECT t.tgname, t.tgenabled, p.proname, pg_get_triggerdef(t.oid) "
        "FROM pg_trigger t JOIN pg_proc p ON p.oid = t.tgfoid "
        "WHERE NOT t.tgisinternal AND t.tgrelid = CAST(:t AS regclass) AND t.tgname = :name"),
        {"t": _table_regclass(_TERMINALS), "name": _TRIGGER}).one_or_none()


def _function_bodies(bind):
    rows = bind.execute(sa.text(
        "SELECT p.prosrc FROM pg_proc p JOIN pg_namespace n ON n.oid = p.pronamespace "
        "WHERE n.nspname = 'public' AND p.proname = :name"), {"name": _TRIGGER_FUNCTION}).all()
    return [_norm(r[0]) for r in rows]


def trigger_differences(bind) -> list:
    """Empty when the immutability trigger is present, enabled and defined as expected."""
    row = _trigger_row(bind)
    if row is None:
        return [f"{_TERMINALS}: trigger {_TRIGGER} absent on the runtime terminal relation"]
    diffs = []
    if str(row[1]) not in ("O", "A"):
        diffs.append(f"{_TERMINALS}: trigger {_TRIGGER} is not enabled (tgenabled={row[1]})")
    if row[2] != _TRIGGER_FUNCTION:
        diffs.append(f"{_TERMINALS}: trigger {_TRIGGER} executes {row[2]}, expected {_TRIGGER_FUNCTION}")
    if _norm(row[3]) != _norm(_EXPECTED_TRIGGER_DEF):
        diffs.append(f"{_TERMINALS}: trigger definition differs: {row[3]}")
    bodies = _function_bodies(bind)
    if not bodies or any(body != _norm(_EXPECTED_FUNCTION_BODY) for body in bodies):
        diffs.append(f"function {_TRIGGER_FUNCTION} absent or its body differs from this revision")
    return diffs


def schema_differences(bind, *, include_trigger: bool = True) -> list:
    """Every difference between the live schema and this revision's definition.

    Absent tables are reported as differences too; ``upgrade`` creates them
    before calling this, so on a successful upgrade the list is empty.
    """
    diffs = []
    for table, expected in _EXPECTED.items():
        if not has_table(bind, table):
            diffs.append(f"{table}: table absent")
            continue
        actual_cols = _actual_columns(bind, table)
        expected_cols = {c[0]: (c[1], c[2], c[3], c[4]) for c in expected["columns"]}
        for name, spec in expected_cols.items():
            if name not in actual_cols:
                diffs.append(f"{table}.{name}: column absent")
            elif actual_cols[name] != spec:
                diffs.append(f"{table}.{name}: (udt, length, nullable, default) is {actual_cols[name]}, expected {spec}")
        for name in actual_cols:
            if name not in expected_cols:
                diffs.append(f"{table}.{name}: unexpected column")
        actual_cons = _actual_constraints(bind, table)
        expected_cons = {k: (v[0], _norm(v[1])) for k, v in expected["constraints"].items()}
        for name, spec in expected_cons.items():
            if name not in actual_cons:
                diffs.append(f"{table}: constraint {name} absent")
            elif actual_cons[name] != spec:
                diffs.append(f"{table}: constraint {name} is {actual_cons[name]}, expected {spec}")
        for name in actual_cons:
            if name not in expected_cons:
                diffs.append(f"{table}: unexpected constraint {name}")
        actual_idx = _actual_indexes(bind, table)
        expected_idx = {k: _norm(v) for k, v in expected["indexes"].items()}
        for name, spec in expected_idx.items():
            if name not in actual_idx:
                diffs.append(f"{table}: index {name} absent")
            elif actual_idx[name] != spec:
                diffs.append(f"{table}: index {name} is {actual_idx[name]}, expected {spec}")
        for name in actual_idx:
            if name not in expected_idx:
                diffs.append(f"{table}: unexpected index {name}")
    if include_trigger and has_table(bind, _TERMINALS):
        diffs.extend(trigger_differences(bind))
    return diffs


class IncompatibleSchema(RuntimeError):
    """A pre-existing shape this revision refuses to reconcile or stamp."""


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

    # Verify every pre-existing (or just created) table by definition. A
    # difference is refused explicitly; nothing is stamped.
    diffs = schema_differences(bind, include_trigger=False)
    if diffs:
        raise IncompatibleSchema(
            "0108 refuses to reconcile an incompatible pre-existing schema: " + "; ".join(diffs)
        )

    if bind.dialect.name == "postgresql":
        bodies = _function_bodies(bind)
        if not bodies:
            op.execute(sa.text(_TRIGGER_FUNCTION_SQL))
        elif any(body != _norm(_EXPECTED_FUNCTION_BODY) for body in bodies):
            raise IncompatibleSchema(
                f"0108 refuses to replace an existing function {_TRIGGER_FUNCTION} whose body differs"
            )
        row = _trigger_row(bind)
        if row is None:
            op.execute(sa.text(_TRIGGER_SQL))
        else:
            trigger_diffs = trigger_differences(bind)
            if trigger_diffs:
                raise IncompatibleSchema(
                    "0108 refuses an incompatible immutability trigger: " + "; ".join(trigger_diffs)
                )
        remaining = schema_differences(bind, include_trigger=True)
        if remaining:
            raise IncompatibleSchema("0108 post-condition failed: " + "; ".join(remaining))


def downgrade() -> None:
    bind = op.get_bind()
    if bind.dialect.name == "postgresql":
        if has_table(bind, _TERMINALS):
            op.execute(sa.text(f"DROP TRIGGER IF EXISTS {_TRIGGER} ON {_TERMINALS}"))
    for table in (_TERMINALS, _TURNS, _CONVERSATIONS):
        if has_table(bind, table):
            op.drop_table(table)
    if bind.dialect.name == "postgresql":
        # Drop the function only when no other trigger still executes it.
        dependants = bind.execute(sa.text(
            "SELECT count(*) FROM pg_trigger t JOIN pg_proc p ON p.oid = t.tgfoid "
            "WHERE NOT t.tgisinternal AND p.proname = :name"), {"name": _TRIGGER_FUNCTION}).scalar()
        if not dependants:
            op.execute(sa.text(f"DROP FUNCTION IF EXISTS {_TRIGGER_FUNCTION}()"))

"""Commerce Agent V2 shadow observations.

Reconciliation with ``Base.metadata.create_all``
================================================
Production startup pins ``alembic upgrade 0093`` and materializes new ORM
tables through ``Base.metadata.create_all`` (``backend/main.py``). The
``commerce_agent_v2_shadow_runs`` table therefore usually EXISTS before
this revision is ever applied, in the ORM shape: same columns, same three
indexes, same foreign keys, but without the server-side defaults declared
here (the ORM defaults are Python-side). This revision is safe in both
states — the same additive pattern as 0107:

* Table absent → created exactly as before (State A).
* Table present (create_all) → reconciled additively: missing columns are
  added, the server defaults are set, NOT NULL is enforced only on columns
  that have a safe fill value (the four counters → 0, ``created_at`` →
  now()) and only after any NULLs are filled with it, and missing foreign
  keys / indexes are created. Nothing is dropped, no row is rewritten, and
  the revision is recorded (State B).

Reconciliation limit (fail-safe, additive): required columns with no safe
fill value — ``tenant_id``, ``conversation_id``, ``sdk_trace_id``,
``model``, ``status`` — are never forced NOT NULL. If a drifted table is
missing one of them it is re-added NULLABLE and existing rows keep NULL
there; if it exists but is nullable it stays nullable. No data is
invented. Such a table therefore ends nullable on that column rather than
at exact State A parity; the plain create_all shape (all of these already
NOT NULL) reaches full State A parity.

Guards check object presence by name (``migration_inspector_helpers``),
the same mechanism used by 0104, 0105 and 0107.

Do not use ``alembic upgrade head``. Apply with ``alembic upgrade 0106``
(or as a step of ``alembic upgrade 0107``). This revision does not apply
itself to Production.

Revision ID: 0106
Revises: 0105
"""
from __future__ import annotations

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from migration_inspector_helpers import has_index, has_table


revision = "0106"
down_revision = "0105"
branch_labels = None
depends_on = None

_TABLE = "commerce_agent_v2_shadow_runs"

# (index name, columns) — identical to the ORM ``__table_args__`` /
# ``index=True`` names so reconciliation never creates a duplicate index.
_INDEXES = (
    ("ix_commerce_v2_shadow_tenant_created", ["tenant_id", "created_at"]),
    ("ix_commerce_v2_shadow_conversation_created", ["conversation_id", "created_at"]),
    ("ix_commerce_agent_v2_shadow_runs_sdk_trace_id", ["sdk_trace_id"]),
)

# Foreign keys as (column, referred table, referred column, constraint name).
# The names match what PostgreSQL assigns to the unnamed ORM foreign keys
# created by ``Base.metadata.create_all`` so reconciliation never creates
# a second constraint over the same column.
_FOREIGN_KEYS = (
    ("tenant_id", "tenants", "id", f"{_TABLE}_tenant_id_fkey"),
    ("conversation_id", "conversations", "id", f"{_TABLE}_conversation_id_fkey"),
)

# Server defaults required by this revision, keyed by column. ``fill`` is
# the SQL used to fill pre-existing NULLs before NOT NULL is enforced.
_SERVER_DEFAULTS = {
    "latency_ms": {"server_default": "0", "fill": "0"},
    "input_tokens": {"server_default": "0", "fill": "0"},
    "output_tokens": {"server_default": "0", "fill": "0"},
    "total_tokens": {"server_default": "0", "fill": "0"},
    "created_at": {"server_default": sa.text("now()"), "fill": "now()"},
}


def _columns() -> list:
    """Fresh Column objects — the schema of this revision, in order."""
    return [
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("tenant_id", sa.Integer(), nullable=False),
        sa.Column("conversation_id", sa.Integer(), nullable=False),
        sa.Column("sdk_trace_id", sa.String(length=64), nullable=False),
        sa.Column("model", sa.String(length=128), nullable=False),
        sa.Column("status", sa.String(length=32), nullable=False),
        sa.Column("structured_output", postgresql.JSONB(), nullable=True),
        sa.Column("tool_trace", postgresql.JSONB(), nullable=True),
        sa.Column("guardrail_results", postgresql.JSONB(), nullable=True),
        sa.Column("latency_ms", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("input_tokens", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("output_tokens", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("total_tokens", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("failure_reason", sa.String(length=240), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
    ]


def _create_fresh() -> None:
    """State A — the table does not exist: create it as this revision defines it."""
    op.create_table(
        _TABLE,
        *_columns(),
        sa.ForeignKeyConstraint(["tenant_id"], ["tenants.id"]),
        sa.ForeignKeyConstraint(["conversation_id"], ["conversations.id"]),
    )
    for index_name, columns in _INDEXES:
        op.create_index(index_name, _TABLE, columns)


def _reconcile_existing(bind) -> None:
    """State B — the table already exists (``Base.metadata.create_all``).

    Additive only: no DROP, no re-create, no row rewritten beyond filling
    NULLs in columns that this revision declares NOT NULL with their own
    default value.
    """
    insp = sa.inspect(bind)
    existing = {c["name"]: c for c in insp.get_columns(_TABLE)}

    # 1. Missing columns are added. A column added with a server default
    #    and NOT NULL is filled by PostgreSQL for existing rows.
    for column in _columns():
        if column.name in existing:
            continue
        if column.nullable is False and column.server_default is None and not column.primary_key:
            # Cannot add NOT NULL without a default to a populated table;
            # add nullable first, fill from the default map, then enforce.
            # No safe fill value → the column stays NULLABLE (see the
            # reconciliation limit in the module docstring).
            fill = _SERVER_DEFAULTS.get(column.name, {}).get("fill")
            op.add_column(_TABLE, sa.Column(column.name, column.type, nullable=True))
            if fill is not None:
                op.execute(sa.text(f'UPDATE {_TABLE} SET "{column.name}" = {fill} WHERE "{column.name}" IS NULL'))
                op.alter_column(_TABLE, column.name, nullable=False)
        else:
            op.add_column(
                _TABLE,
                sa.Column(
                    column.name,
                    column.type,
                    nullable=column.nullable,
                    server_default=column.server_default,
                    primary_key=column.primary_key,
                ),
            )
    existing = {c["name"]: c for c in sa.inspect(bind).get_columns(_TABLE)}

    # 2. Server defaults: the ORM declares these as Python-side defaults
    #    only, so a create_all table has none. Set them; never overwrite an
    #    existing (non-NULL) default expression.
    for name, spec in _SERVER_DEFAULTS.items():
        if existing[name].get("default") is None:
            op.alter_column(_TABLE, name, server_default=spec["server_default"])

    # 3. NOT NULL on the columns this revision requires, after filling NULLs
    #    with the same default (no row loses data; only NULLs change).
    #    Columns with no safe fill value are left nullable — forcing
    #    NOT NULL on a populated table could fail, and inventing data is
    #    out of scope for an additive reconciliation.
    for column in _columns():
        if column.nullable is not False or column.primary_key:
            continue
        if not existing[column.name].get("nullable"):
            continue
        fill = _SERVER_DEFAULTS.get(column.name, {}).get("fill")
        if fill is None:
            continue
        op.execute(sa.text(f'UPDATE {_TABLE} SET "{column.name}" = {fill} WHERE "{column.name}" IS NULL'))
        op.alter_column(_TABLE, column.name, nullable=False)

    # 4. Foreign keys — by referred table + local column, whatever the name.
    present_fks = {
        (fk["referred_table"], tuple(fk["constrained_columns"]))
        for fk in insp.get_foreign_keys(_TABLE)
    }
    for local_col, ref_table, ref_col, fk_name in _FOREIGN_KEYS:
        if (ref_table, (local_col,)) in present_fks:
            continue
        op.create_foreign_key(fk_name, _TABLE, ref_table, [local_col], [ref_col])

    # 5. Indexes by name (same guard as 0104/0105/0107).
    for index_name, columns in _INDEXES:
        if not has_index(bind, _TABLE, index_name):
            op.create_index(index_name, _TABLE, columns)


def upgrade() -> None:
    bind = op.get_bind()
    if has_table(bind, _TABLE):
        _reconcile_existing(bind)
        return
    _create_fresh()


def downgrade() -> None:
    bind = op.get_bind()
    if not has_table(bind, _TABLE):
        return
    for index_name, _columns_ in reversed(_INDEXES):
        if has_index(bind, _TABLE, index_name):
            op.drop_index(index_name, table_name=_TABLE)
    op.drop_table(_TABLE)

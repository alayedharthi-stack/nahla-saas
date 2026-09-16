"""Durable, centralized provenance for the canonical customer name.

Backs ``core.customer_name_authority``: one row per
``(tenant_id, customer_id)``.

The row has two halves. CANONICAL fields describe the customer's current
canonical identity and change only when a name is actually applied.
ATTEMPT / HINT fields record the most recent attempt (including rejected
or blocked ones) so audits can see what was tried without a blocked
WhatsApp profile ever overwriting a Salla-verified provenance.

The JSONB keys on ``customers.metadata`` remain in sync for existing
readers but are demoted to a cache — this table is the record.

Reconciliation with ``Base.metadata.create_all``
================================================
Production startup pins ``alembic upgrade 0093`` and materializes new ORM
tables through ``Base.metadata.create_all`` (``backend/main.py``). The
``customer_name_provenance`` table therefore usually EXISTS before this
revision is ever applied, in the ORM shape: same columns, same unique
constraint, same index, but without the server-side defaults declared
here (the ORM defaults are Python-side). This revision is safe in both
states:

* Table absent → created exactly as before (State A).
* Table present (create_all) → reconciled additively: missing columns are
  added, the server defaults are set, NOT NULL is enforced only after any
  NULLs are filled with the same defaults, and missing foreign keys /
  unique constraint / index are created. Nothing is dropped, no row is
  rewritten, and the revision is recorded (State B).

Guards check object presence by name (``migration_inspector_helpers``),
the same mechanism used by 0104 and 0105.

Do not use ``alembic upgrade head``. Apply with ``alembic upgrade 0107``.
This revision does not apply itself to Production.

Revision ID: 0107
Revises: 0106
Create Date: 2026-09-16
"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import JSONB

from migration_inspector_helpers import has_index, has_table, has_unique_constraint

revision: str = "0107"
down_revision: Union[str, None] = "0106"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

_TABLE = "customer_name_provenance"
_UNIQUE = "uq_customer_name_provenance_tenant_customer"
_INDEX = "ix_customer_name_provenance_tenant_authority"

# Foreign keys as (column, referred table, referred column, constraint name).
# The names match what PostgreSQL assigns to the unnamed ORM foreign keys
# created by ``Base.metadata.create_all`` so reconciliation never creates
# a second constraint over the same column.
_FOREIGN_KEYS = (
    ("tenant_id", "tenants", "id", f"{_TABLE}_tenant_id_fkey"),
    ("customer_id", "customers", "id", f"{_TABLE}_customer_id_fkey"),
)

# Server defaults required by this revision, keyed by column. ``fill`` is
# the SQL used to fill pre-existing NULLs before NOT NULL is enforced.
_SERVER_DEFAULTS = {
    "authority": {"server_default": "UNKNOWN", "fill": "'UNKNOWN'"},
    "merchant_locked": {"server_default": sa.false(), "fill": "false"},
    "created_at": {"server_default": sa.func.now(), "fill": "now()"},
    "updated_at": {"server_default": sa.func.now(), "fill": "now()"},
}


def _columns() -> list:
    """Fresh Column objects — the schema of this revision, in order."""
    return [
        sa.Column("id", sa.Integer, primary_key=True),
        sa.Column("tenant_id", sa.Integer, sa.ForeignKey("tenants.id"), nullable=False),
        sa.Column("customer_id", sa.Integer, sa.ForeignKey("customers.id"), nullable=False),
        # ── Canonical half ────────────────────────────────────────
        sa.Column("canonical_name", sa.String, nullable=True),
        sa.Column("authority", sa.String, nullable=False, server_default="UNKNOWN"),
        sa.Column("source", sa.String, nullable=True),
        sa.Column("evidence_kind", sa.String, nullable=True),
        sa.Column("evidence_ref", JSONB, nullable=True),
        sa.Column("merchant_locked", sa.Boolean, nullable=False, server_default=sa.false()),
        sa.Column("previous_name", sa.String, nullable=True),
        sa.Column("previous_authority", sa.String, nullable=True),
        sa.Column("canonical_updated_at", sa.DateTime(timezone=True), nullable=True),
        # ── Hint half ─────────────────────────────────────────────
        sa.Column("profile_hint", sa.String, nullable=True),
        sa.Column("profile_hint_classification", sa.String, nullable=True),
        # ── Attempt half ──────────────────────────────────────────
        sa.Column("last_decision", sa.String, nullable=True),
        sa.Column("last_attempt_name", sa.String, nullable=True),
        sa.Column("last_attempt_authority", sa.String, nullable=True),
        sa.Column("last_attempt_source", sa.String, nullable=True),
        sa.Column("last_attempt_classification", sa.String, nullable=True),
        sa.Column("last_attempt_reason", sa.String, nullable=True),
        sa.Column("last_attempt_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
    ]


def _create_fresh() -> None:
    """State A — the table does not exist: create it as this revision defines it."""
    op.create_table(
        _TABLE,
        *_columns(),
        sa.UniqueConstraint("tenant_id", "customer_id", name=_UNIQUE),
    )
    op.create_index(_INDEX, _TABLE, ["tenant_id", "authority"])


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
    for column in _columns():
        if column.nullable is not False or column.primary_key:
            continue
        if existing[column.name].get("nullable"):
            fill = _SERVER_DEFAULTS.get(column.name, {}).get("fill")
            if fill is not None:
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

    # 5. Unique constraint and index by name (same guard as 0104/0105).
    if not has_unique_constraint(bind, _TABLE, _UNIQUE):
        op.create_unique_constraint(_UNIQUE, _TABLE, ["tenant_id", "customer_id"])
    if not has_index(bind, _TABLE, _INDEX):
        op.create_index(_INDEX, _TABLE, ["tenant_id", "authority"])


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
    if has_index(bind, _TABLE, _INDEX):
        op.drop_index(_INDEX, table_name=_TABLE)
    op.drop_table(_TABLE)

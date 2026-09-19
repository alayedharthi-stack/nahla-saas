"""Durable source binding, revision and explicit selection for customer addresses.

Backs ``core.customer_address_candidates``: one row per
``customer_addresses`` row.

``customer_addresses`` keeps the address CONTENT unchanged — this revision
adds no column to it. The new table records who the content came from
(``source``, ``source_ref``, ``integration_connection_id``), which provider
revision it reflects (``source_updated_at``, NULL when the provider sent
none — never a locally invented value — plus ``source_observed_at``, a
different fact), the content revision (``content_fingerprint``) and whether
the customer explicitly selected the address as their delivery address
(``selection_state``, ``selected_fingerprint``, ``selected_at``,
``selection_source``).

``source_country`` round-trips the observed country, which
``customer_addresses`` has no column for.
``selection_operation_ref`` records which selection OPERATION produced the
current selection, so a redelivered confirmation is idempotent while a new
choice of a previously approved address becomes current again.

``uq_customer_address_provenance_source_revision``
==================================================
One row per
``(tenant, customer, integration_connection_id, source, source_ref,
content_fingerprint)``. The store connection is part of the key because the
same provider customer reference can exist under two stores, and without it
one store's import would collide with — and be deduplicated into — another
store's row.
Two concurrent first imports of the same payload cannot both commit: the
loser receives an IntegrityError and re-reads the winner. Different
revisions of the same source still coexist, so a historical selected
revision is never displaced by a refresh. PostgreSQL treats NULLs as
distinct, so rows with no ``source_ref`` (confirmed-shipping provenance)
are not deduplicated by this constraint; that path deduplicates on address
content in ``core.customer_shipping_address_writer`` instead.

Activation boundary (explicit)
==============================
``backend/main.py`` calls ``Base.metadata.create_all(engine)`` at startup,
so a deployment that ships these ORM models MATERIALIZES this table even
without ``alembic upgrade 0110``. "No explicit alembic run" is therefore
NOT a guarantee of dormancy, and this revision does not claim one. What the
pinned bootstrap target (``0093``) does guarantee is that no *existing*
table is altered by Alembic here. The address readers and writers degrade
safely when the table is absent (reads classify rows from
``customer_addresses`` alone; writes attempt provenance inside a SAVEPOINT
so a missing table cannot abort the caller's transaction) — but the
feature is live wherever the models are deployed.

Rows that predate this revision have no provenance row. They are read as
legacy selections (they were only ever written on confirmed shipping
evidence), so existing reuse is unchanged. Nothing is backfilled here.

Reconciliation with ``Base.metadata.create_all``
================================================
Production startup pins ``alembic upgrade 0093`` and materializes new ORM
tables through ``Base.metadata.create_all`` (``backend/main.py``), so
``customer_address_provenance`` usually EXISTS before this revision is
applied, in the ORM shape: same columns, same unique constraint, same
index, but without the server-side defaults declared here (the ORM
defaults are Python-side). This revision is safe in both states:

* Table absent → created exactly as defined here (State A).
* Table present (create_all) → reconciled additively: missing columns are
  added, server defaults are set, NOT NULL is enforced only after any NULLs
  are filled with the same default, and missing foreign keys / unique
  constraint / index are created. Nothing is dropped and no row is
  rewritten (State B).

Guards check object presence by name (``migration_inspector_helpers``), the
same mechanism used by 0104, 0105 and 0107.

Do not use ``alembic upgrade head``. Apply with ``alembic upgrade 0110``.
This revision does not apply itself to Production.

Revision ID: 0110
Revises: 0109
Create Date: 2026-09-19
"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

from migration_inspector_helpers import has_index, has_table, has_unique_constraint

revision: str = "0110"
down_revision: Union[str, None] = "0109"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

_TABLE = "customer_address_provenance"
_UNIQUE = "uq_customer_address_provenance_address"
_UNIQUE_SOURCE_REVISION = "uq_customer_address_provenance_source_revision"
_INDEX = "ix_customer_address_provenance_source"

# Foreign keys as (column, referred table, referred column, constraint name).
# The names match what PostgreSQL assigns to the unnamed ORM foreign keys
# created by ``Base.metadata.create_all`` so reconciliation never creates a
# second constraint over the same column.
_FOREIGN_KEYS = (
    ("tenant_id", "tenants", "id", f"{_TABLE}_tenant_id_fkey"),
    ("customer_id", "customers", "id", f"{_TABLE}_customer_id_fkey"),
    (
        "customer_address_id",
        "customer_addresses",
        "id",
        f"{_TABLE}_customer_address_id_fkey",
    ),
)

# Server defaults required by this revision, keyed by column. ``fill`` is
# the SQL used to fill pre-existing NULLs before NOT NULL is enforced.
_SERVER_DEFAULTS = {
    "selection_state": {"server_default": "candidate", "fill": "'candidate'"},
    "created_at": {"server_default": sa.func.now(), "fill": "now()"},
    "updated_at": {"server_default": sa.func.now(), "fill": "now()"},
}

# ``source``, ``content_fingerprint`` and ``source_observed_at`` are NOT NULL
# with no default: the service always writes them, and the ORM column is
# NOT NULL too, so a pre-existing row cannot hold NULL there. Reconciliation
# therefore never has to invent one (see step 3).


def _columns() -> list:
    """Fresh Column objects — the schema of this revision, in order."""
    return [
        sa.Column("id", sa.Integer, primary_key=True),
        sa.Column("tenant_id", sa.Integer, sa.ForeignKey("tenants.id"), nullable=False),
        sa.Column("customer_id", sa.Integer, sa.ForeignKey("customers.id"), nullable=False),
        sa.Column(
            "customer_address_id",
            sa.Integer,
            sa.ForeignKey("customer_addresses.id"),
            nullable=False,
        ),
        # ── Source binding ────────────────────────────────────────
        sa.Column("source", sa.String, nullable=False),
        sa.Column("source_ref", sa.String, nullable=True),
        sa.Column("integration_connection_id", sa.Integer, nullable=True),
        sa.Column("source_country", sa.String, nullable=True),
        # ── Revision ──────────────────────────────────────────────
        sa.Column("content_fingerprint", sa.String, nullable=False),
        sa.Column("source_updated_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("source_observed_at", sa.DateTime(timezone=True), nullable=False),
        # ── Explicit selection ────────────────────────────────────
        sa.Column("selection_state", sa.String, nullable=False, server_default="candidate"),
        sa.Column("selected_fingerprint", sa.String, nullable=True),
        sa.Column("selected_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("selection_source", sa.String, nullable=True),
        sa.Column("selection_operation_ref", sa.String, nullable=True),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), nullable=False,
            server_default=sa.func.now(),
        ),
        sa.Column(
            "updated_at", sa.DateTime(timezone=True), nullable=False,
            server_default=sa.func.now(),
        ),
    ]


def _create_fresh() -> None:
    """State A — the table does not exist: create it as this revision defines it."""
    op.create_table(
        _TABLE,
        *_columns(),
        sa.UniqueConstraint("tenant_id", "customer_address_id", name=_UNIQUE),
    )
    _create_source_revision_unique_index()
    op.create_index(
        _INDEX, _TABLE, ["tenant_id", "customer_id", "source", "source_ref"],
    )


def _create_source_revision_unique_index() -> None:
    """One row per (customer, store connection, source, ref, revision).

    An expression index over ``COALESCE(integration_connection_id, -1)``,
    not a plain unique constraint: rows written before a verified
    connection existed hold NULL there, and SQL treats NULLs as distinct,
    so a bare unique key would stop deduplicating exactly those rows.
    """
    op.create_index(
        _UNIQUE_SOURCE_REVISION,
        _TABLE,
        [
            "tenant_id",
            "customer_id",
            sa.text("COALESCE(integration_connection_id, -1)"),
            "source",
            "source_ref",
            "content_fingerprint",
        ],
        unique=True,
    )


def _reconcile_existing(bind) -> None:
    """State B — the table already exists (``Base.metadata.create_all``).

    Additive only: no DROP, no re-create, no row rewritten beyond filling
    NULLs in columns that this revision declares NOT NULL with their own
    default value.
    """
    insp = sa.inspect(bind)
    existing = {c["name"]: c for c in insp.get_columns(_TABLE)}

    # 1. Missing columns are added. A column added with a server default and
    #    NOT NULL is filled by PostgreSQL for existing rows; a NOT NULL
    #    column without a default is added nullable and then enforced only
    #    when no NULL remains (see step 3), so reconciliation can never
    #    invent a source, a fingerprint or an observation time.
    for column in _columns():
        if column.name in existing:
            continue
        if column.nullable is False and column.server_default is None and not column.primary_key:
            op.add_column(_TABLE, sa.Column(column.name, column.type, nullable=True))
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

    # 3. NOT NULL on the columns this revision requires. Columns with a
    #    default fill their NULLs with that same default first. Columns
    #    without one are enforced only when the table holds no NULL — an
    #    enforcement that would have to invent a value raises instead.
    for column in _columns():
        if column.nullable is not False or column.primary_key:
            continue
        if not existing[column.name].get("nullable"):
            continue
        fill = _SERVER_DEFAULTS.get(column.name, {}).get("fill")
        if fill is not None:
            op.execute(
                sa.text(
                    f'UPDATE {_TABLE} SET "{column.name}" = {fill} '
                    f'WHERE "{column.name}" IS NULL'
                )
            )
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

    # 5. Unique constraint and index by name (same guard as 0104/0105/0107).
    if not has_unique_constraint(bind, _TABLE, _UNIQUE):
        op.create_unique_constraint(
            _UNIQUE, _TABLE, ["tenant_id", "customer_address_id"],
        )
    if not has_unique_constraint(bind, _TABLE, _UNIQUE_SOURCE_REVISION) and not has_index(
        bind, _TABLE, _UNIQUE_SOURCE_REVISION
    ):
        _create_source_revision_unique_index()
    if not has_index(bind, _TABLE, _INDEX):
        op.create_index(
            _INDEX, _TABLE, ["tenant_id", "customer_id", "source", "source_ref"],
        )


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

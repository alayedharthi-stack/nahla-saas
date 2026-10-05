"""Durable channel-retirement requests for deleted catalog products.

Revision ID: 0116
Revises: 0112

Creates ``catalog_channel_retirements`` (see ``database/models.py``
``CatalogChannelRetirement``). Additive only. An existing table (e.g. one the
startup ``create_all`` built) is adopted only when its definition matches the
one created here; any other table of that name makes the upgrade raise and
nothing is changed. Chained on 0112 as an explicit sibling of the dormant
0113/0114 runtime branch and the 0115 payments branch, exactly like 0115, so
applying it never pulls those branches in. No data is touched; downgrade drops
only this table.
"""
from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0116"
down_revision = "0112"
branch_labels = None
depends_on = None

TABLE = "catalog_channel_retirements"


# The definition this revision creates. An existing table is adopted only
# when it has exactly these columns, VARCHAR/INTEGER/timestamptz types and
# nullability (``catalog_id`` may still be NOT NULL from an earlier model and is
# relaxed), its primary key, the tenant foreign key to ``public.tenants`` with
# ON DELETE CASCADE, exactly one non-partial uniqueness on (tenant_id,
# catalog_id, retailer_id), a non-partial (tenant_id, status) index, and no
# other unique or check constraint. Server defaults and collations are not
# compared: the startup ``create_all`` builds the table without server defaults
# and the application always supplies those values. Anything else fails closed.
EXPECTED_COLUMNS = {
    "id": ("INTEGER", None, False),
    "tenant_id": ("INTEGER", None, False),
    "catalog_id": ("VARCHAR", 64, None),
    "retailer_id": ("VARCHAR", 255, False),
    "meta_item_id": ("VARCHAR", 128, True),
    "product_id": ("INTEGER", None, True),
    "reason": ("VARCHAR", 64, False),
    "status": ("VARCHAR", 32, False),
    "attempts": ("INTEGER", None, False),
    "next_attempt_at": ("TIMESTAMPTZ", None, True),
    "last_error": ("VARCHAR", 255, True),
    "created_at": ("TIMESTAMPTZ", None, False),
    "updated_at": ("TIMESTAMPTZ", None, True),
    "done_at": ("TIMESTAMPTZ", None, True),
}
UNIQUE_COLUMNS = ["tenant_id", "catalog_id", "retailer_id"]
INDEX_COLUMNS = ["tenant_id", "status"]


def _type_key(column_type) -> tuple[str, int | None]:
    if isinstance(column_type, sa.DateTime):
        return ("TIMESTAMPTZ" if column_type.timezone else "TIMESTAMP"), None
    if isinstance(column_type, sa.VARCHAR) and not isinstance(column_type, (sa.NVARCHAR,)):
        return "VARCHAR", column_type.length
    if isinstance(column_type, sa.Integer) and not isinstance(column_type, (sa.BigInteger, sa.SmallInteger)):
        return "INTEGER", None
    return type(column_type).__name__.upper(), None


def _partial(index) -> bool:
    return (index.get("dialect_options") or {}).get("postgresql_where") is not None


def existing_table_mismatches(inspector) -> list[str]:
    """Every way an existing table differs from what this revision creates."""
    problems = []
    columns = {c["name"]: c for c in inspector.get_columns(TABLE)}
    for name in sorted(set(columns) - set(EXPECTED_COLUMNS)):
        problems.append(f"unexpected column {name}")
    for name, (kind, length, nullable) in EXPECTED_COLUMNS.items():
        column = columns.get(name)
        if column is None:
            problems.append(f"missing column {name}")
            continue
        if _type_key(column["type"]) != (kind, length):
            problems.append(f"column {name} has type {column['type']}")
        if nullable is not None and bool(column.get("nullable", True)) != nullable:
            problems.append(f"column {name} nullability differs")
    if (inspector.get_pk_constraint(TABLE) or {}).get("constrained_columns") != ["id"]:
        problems.append("primary key is not (id)")
    if not any(
        fk.get("constrained_columns") == ["tenant_id"] and fk.get("referred_table") == "tenants"
        and fk.get("referred_schema") in (None, "public")
        and fk.get("referred_columns") == ["id"] and (fk.get("options") or {}).get("ondelete", "").upper() == "CASCADE"
        for fk in inspector.get_foreign_keys(TABLE)
    ):
        problems.append("missing tenant_id -> public.tenants(id) ON DELETE CASCADE")
    indexes = inspector.get_indexes(TABLE)
    uniques = [u.get("column_names") for u in inspector.get_unique_constraints(TABLE)]
    uniques += [i.get("column_names") for i in indexes
                if i.get("unique") and not i.get("duplicates_constraint") and not _partial(i)]
    if any(i.get("unique") and _partial(i) for i in indexes):
        problems.append("partial unique index present")
    if uniques != [UNIQUE_COLUMNS]:
        problems.append(f"uniqueness is {uniques}, expected exactly [{', '.join(UNIQUE_COLUMNS)}]")
    if not any(i.get("column_names") == INDEX_COLUMNS and not i.get("unique") and not _partial(i) for i in indexes):
        problems.append("missing non-partial index (tenant_id, status)")
    if inspector.get_check_constraints(TABLE):
        problems.append("unexpected check constraint")
    return problems


def upgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    if TABLE in set(inspector.get_table_names()):
        problems = existing_table_mismatches(inspector)
        if problems:
            raise RuntimeError(
                f"{TABLE} already exists with a different definition; inspect it before applying "
                f"this revision ({'; '.join(problems)})"
            )
        # A database that built the table from an earlier head's create_all may
        # still carry NOT NULL on catalog_id; relax it so a tenant without a
        # stamped catalog can record a retirement (the drain resolves it).
        for column in inspector.get_columns(TABLE):
            if column["name"] == "catalog_id" and not column.get("nullable", True):
                op.alter_column(TABLE, "catalog_id", existing_type=sa.String(length=64), nullable=True)
        return
    op.create_table(
        TABLE,
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("tenant_id", sa.Integer(), sa.ForeignKey("tenants.id", ondelete="CASCADE"), nullable=False),
        sa.Column("catalog_id", sa.String(length=64), nullable=True),
        sa.Column("retailer_id", sa.String(length=255), nullable=False),
        sa.Column("meta_item_id", sa.String(length=128), nullable=True),
        sa.Column("product_id", sa.Integer(), nullable=True),
        sa.Column("reason", sa.String(length=64), nullable=False),
        sa.Column("status", sa.String(length=32), nullable=False, server_default="pending"),
        sa.Column("attempts", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("next_attempt_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("last_error", sa.String(length=255), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("done_at", sa.DateTime(timezone=True), nullable=True),
        sa.UniqueConstraint(
            "tenant_id", "catalog_id", "retailer_id",
            name="uq_catalog_channel_retirements_tenant_catalog_retailer",
        ),
    )
    op.create_index(
        "ix_catalog_channel_retirements_tenant_status", TABLE, ["tenant_id", "status"],
    )


def downgrade() -> None:
    bind = op.get_bind()
    if TABLE not in set(sa.inspect(bind).get_table_names()):
        return
    op.drop_index("ix_catalog_channel_retirements_tenant_status", table_name=TABLE)
    op.drop_table(TABLE)

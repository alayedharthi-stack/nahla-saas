"""Verified catalog-only Meta consent authorizations.

Revision ID: 0120
Revises: 0118

Creates ``meta_catalog_authorizations`` (see ``database/models.py``
``MetaCatalogAuthorization``): one row per tenant, one tenant per catalog, the
user access token stored ``enc1:``-encrypted only. Additive only. An existing
table (e.g. one the startup ``create_all`` built) is adopted only when its
definition matches the one created here; any other table of that name makes
the upgrade raise and nothing is changed.

Chained on 0118 (catalog channel retirements) and replaces it as that branch's
head. It merges nothing: the 0092 validation, 0111 application, 0114 runtime
and 0115 payments heads are untouched, and normal bootstrap (pinned to 0093)
never applies it. ``0117`` and ``0119`` are taken by other open branches, so
this revision is ``0120``. Apply it only as an explicit, owner-approved step
after a read-only check of the target database. No data is touched;
downgrade drops only this table.
"""
from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0120"
down_revision = "0118"
branch_labels = None
depends_on = None

TABLE = "meta_catalog_authorizations"

# Adopted only with exactly these columns, types and nullability, primary key
# (id), the tenant foreign key to public.tenants ON DELETE CASCADE, and exactly
# the two non-partial uniquenesses (tenant_id) and (catalog_id). Server
# defaults are not compared: create_all builds the table without them and the
# application always supplies those values. Anything else fails closed.
EXPECTED_COLUMNS = {
    "id": ("INTEGER", None, False),
    "tenant_id": ("INTEGER", None, False),
    "catalog_id": ("VARCHAR", 64, False),
    "business_id": ("VARCHAR", 64, False),
    "meta_app_id": ("VARCHAR", 64, False),
    "meta_user_id": ("VARCHAR", 64, False),
    "access_token_enc": ("TEXT", None, False),
    "granted_scopes": ("JSON", None, False),
    "token_expires_at": ("TIMESTAMPTZ", None, True),
    "data_access_expires_at": ("TIMESTAMPTZ", None, True),
    "status": ("VARCHAR", 16, False),
    "verified_at": ("TIMESTAMPTZ", None, False),
    "created_at": ("TIMESTAMPTZ", None, False),
    "updated_at": ("TIMESTAMPTZ", None, False),
}
UNIQUE_COLUMN_SETS = sorted([["catalog_id"], ["tenant_id"]])


def _type_key(column_type) -> tuple[str, int | None]:
    if isinstance(column_type, sa.DateTime):
        return ("TIMESTAMPTZ" if column_type.timezone else "TIMESTAMP"), None
    if isinstance(column_type, sa.VARCHAR) and not isinstance(column_type, (sa.NVARCHAR,)):
        return "VARCHAR", column_type.length
    if isinstance(column_type, sa.Integer) and not isinstance(column_type, (sa.BigInteger, sa.SmallInteger)):
        return "INTEGER", None
    if isinstance(column_type, sa.Text):
        return "TEXT", None
    if isinstance(column_type, sa.JSON):
        return "JSON", None
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
        if bool(column.get("nullable", True)) != nullable:
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
    uniques = [list(u.get("column_names") or []) for u in inspector.get_unique_constraints(TABLE)]
    uniques += [list(i.get("column_names") or []) for i in indexes
                if i.get("unique") and not i.get("duplicates_constraint") and not _partial(i)]
    if any(i.get("unique") and _partial(i) for i in indexes):
        problems.append("partial unique index present")
    if sorted(uniques) != UNIQUE_COLUMN_SETS:
        problems.append(f"uniqueness is {sorted(uniques)}, expected exactly {UNIQUE_COLUMN_SETS}")
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
        return
    op.create_table(
        TABLE,
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("tenant_id", sa.Integer(), sa.ForeignKey("tenants.id", ondelete="CASCADE"), nullable=False),
        sa.Column("catalog_id", sa.String(length=64), nullable=False),
        sa.Column("business_id", sa.String(length=64), nullable=False),
        sa.Column("meta_app_id", sa.String(length=64), nullable=False),
        sa.Column("meta_user_id", sa.String(length=64), nullable=False),
        sa.Column("access_token_enc", sa.Text(), nullable=False),
        sa.Column("granted_scopes", sa.JSON(), nullable=False),
        sa.Column("token_expires_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("data_access_expires_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("status", sa.String(length=16), nullable=False),
        sa.Column("verified_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.UniqueConstraint("tenant_id", name="uq_meta_catalog_authorizations_tenant"),
        sa.UniqueConstraint("catalog_id", name="uq_meta_catalog_authorizations_catalog"),
    )


def downgrade() -> None:
    bind = op.get_bind()
    if TABLE not in set(sa.inspect(bind).get_table_names()):
        return
    op.drop_table(TABLE)

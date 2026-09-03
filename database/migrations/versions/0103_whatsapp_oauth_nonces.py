"""0103 — durable WhatsApp OAuth nonces (hashed, tenant-bound, single-use).

Revision ID: 0103
Revises:    0102

Stores HMAC fingerprints only. Never stores raw nonce, signed state, or IBAN.
Does not merge sibling head 0092. Normal bootstrap remains pinned at 0093.

If ``whatsapp_oauth_nonces`` already exists, the contract is verified and
the revision stamps only when it matches. Drift fails closed: no drop,
no destructive repair, no silent adoption of an incomplete table.

Apply with ``alembic upgrade 0103`` (not ``head``).
"""
from __future__ import annotations

import sqlalchemy as sa
from alembic import op
from sqlalchemy import inspect

revision = "0103"
down_revision = "0102"
branch_labels = None
depends_on = None

_TABLE = "whatsapp_oauth_nonces"
_UNIQUE = "uq_whatsapp_oauth_nonces_hash"
_IX_EXPIRES = "ix_whatsapp_oauth_nonces_expires_at"
_IX_TENANT = "ix_whatsapp_oauth_nonces_tenant_id"
ERROR_SCHEMA_DRIFT = (
    "whatsapp_oauth_nonces exists but does not match the 0103 contract. "
    "No drop, repair, or silent adoption was performed."
)

_REQUIRED_NULLABLE = {
    "id": False,
    "nonce_hash": False,
    "tenant_id": False,
    "connection_mode": False,
    "redirect_uri_fingerprint": False,
    "expires_at": False,
    "consumed_at": True,
    "created_at": False,
}
_VARCHAR_LENGTHS = {
    "nonce_hash": 64,
    "connection_mode": 32,
    "redirect_uri_fingerprint": 64,
}
_TZ_COLUMNS = ("expires_at", "consumed_at", "created_at")


def _fail_closed() -> None:
    raise RuntimeError(ERROR_SCHEMA_DRIFT)


def _varchar_length(col_type: object) -> int | None:
    length = getattr(col_type, "length", None)
    if length is None and hasattr(col_type, "impl"):
        length = getattr(col_type.impl, "length", None)
    try:
        return int(length) if length is not None else None
    except (TypeError, ValueError):
        return None


def _is_timezone_datetime(col_type: object) -> bool:
    if isinstance(col_type, sa.DateTime):
        return bool(getattr(col_type, "timezone", False))
    name = str(col_type).lower()
    return "timestamp" in name and "with time zone" in name


def _assert_existing_table_matches_contract(bind) -> None:
    insp = inspect(bind)
    cols = {c["name"]: c for c in insp.get_columns(_TABLE)}
    if set(_REQUIRED_NULLABLE) - set(cols):
        _fail_closed()
    for name, nullable in _REQUIRED_NULLABLE.items():
        if bool(cols[name].get("nullable")) is not bool(nullable):
            _fail_closed()
    for name, length in _VARCHAR_LENGTHS.items():
        if _varchar_length(cols[name]["type"]) != length:
            _fail_closed()
    for name in _TZ_COLUMNS:
        if not _is_timezone_datetime(cols[name]["type"]):
            _fail_closed()

    unique_ok = False
    for constraint in insp.get_unique_constraints(_TABLE):
        columns = tuple(constraint.get("column_names") or ())
        if columns == ("nonce_hash",) or constraint.get("name") == _UNIQUE:
            unique_ok = True
            break
    indexes = insp.get_indexes(_TABLE)
    if not unique_ok:
        unique_ok = any(
            bool(ix.get("unique")) and list(ix.get("column_names") or []) == ["nonce_hash"]
            for ix in indexes
        )
    if not unique_ok:
        _fail_closed()

    fk_ok = False
    for fk in insp.get_foreign_keys(_TABLE):
        options = fk.get("options") or {}
        if (
            list(fk.get("constrained_columns") or []) == ["tenant_id"]
            and fk.get("referred_table") == "tenants"
            and list(fk.get("referred_columns") or []) == ["id"]
            and str(options.get("ondelete") or "").upper() == "CASCADE"
        ):
            fk_ok = True
            break
    if not fk_ok:
        _fail_closed()

    index_names = {ix.get("name") for ix in indexes}
    column_sets = {tuple(ix.get("column_names") or ()) for ix in indexes}
    if _IX_EXPIRES not in index_names and ("expires_at",) not in column_sets:
        _fail_closed()
    if _IX_TENANT not in index_names and ("tenant_id",) not in column_sets:
        _fail_closed()


def upgrade() -> None:
    bind = op.get_bind()
    inspector = inspect(bind)
    if _TABLE in inspector.get_table_names():
        _assert_existing_table_matches_contract(bind)
        return
    op.create_table(
        _TABLE,
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column("nonce_hash", sa.String(64), nullable=False),
        sa.Column("tenant_id", sa.Integer(), nullable=False),
        sa.Column("connection_mode", sa.String(32), nullable=False),
        sa.Column(
            "redirect_uri_fingerprint",
            sa.String(64),
            nullable=False,
        ),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("consumed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("CURRENT_TIMESTAMP"),
        ),
        sa.ForeignKeyConstraint(["tenant_id"], ["tenants.id"], ondelete="CASCADE"),
        sa.UniqueConstraint("nonce_hash", name=_UNIQUE),
    )
    op.create_index(_IX_EXPIRES, _TABLE, ["expires_at"])
    op.create_index(_IX_TENANT, _TABLE, ["tenant_id"])


def downgrade() -> None:
    bind = op.get_bind()
    inspector = inspect(bind)
    if _TABLE not in inspector.get_table_names():
        return
    op.drop_index(_IX_TENANT, table_name=_TABLE)
    op.drop_index(_IX_EXPIRES, table_name=_TABLE)
    op.drop_table(_TABLE)

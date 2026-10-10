"""Dormant Shopify secure connection foundation tables.

Revision ID: 0121
Revises: 0120

Creates the four Shopify-owned tables declared on ``ShopifyBase`` in
``backend/services/shopify_connection/models.py`` (they are deliberately not
part of ``models.Base``, so startup ``create_all`` never builds them):

  shopify_connections       one row per shop, unconditional uniqueness on
                            shop_domain and shop_gid (ownership tombstone),
                            AES-GCM ciphertexts only, credential CHECK
  shopify_oauth_states      hashed single-use authorization states
  shopify_webhook_events    durable uninstall delivery record / dedupe
  shopify_connection_audit  append-only, secret-free lifecycle evidence

Additive only. If any of the four tables already exists the upgrade compares
its column names, types and nullability with the definition below and
raises on any difference without changing anything; an identical table is
adopted. Chained on 0120 and replaces it as that branch's head; it merges
nothing and normal bootstrap (pinned to 0093) never applies it. Apply only as
an explicit, owner-approved step. Downgrade drops only these four tables.
"""
from __future__ import annotations

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "0121"
down_revision = "0120"
branch_labels = None
depends_on = None

CONNECTIONS = "shopify_connections"
STATES = "shopify_oauth_states"
EVENTS = "shopify_webhook_events"
AUDIT = "shopify_connection_audit"
TABLES = (CONNECTIONS, STATES, EVENTS, AUDIT)

_CREDENTIAL = "'active', 'quarantined'"
_TOMBSTONE = "'reauth_required', 'disconnected', 'uninstalled'"


def _ts(name: str, nullable: bool = True, default: bool = False) -> sa.Column:
    kwargs = {"server_default": sa.text("now()")} if default else {}
    return sa.Column(name, sa.DateTime(timezone=True), nullable=nullable, **kwargs)


def _definitions() -> dict:
    meta = sa.MetaData()
    sa.Table("tenants", meta, sa.Column("id", sa.Integer, primary_key=True))
    sa.Table("users", meta, sa.Column("id", sa.Integer, primary_key=True))
    connections = sa.Table(
        CONNECTIONS, meta,
        sa.Column("id", sa.BigInteger(), primary_key=True),
        sa.Column("tenant_id", sa.Integer(), sa.ForeignKey("tenants.id"), nullable=False),
        sa.Column("shop_domain", sa.String(255), nullable=False),
        sa.Column("shop_gid", sa.String(64), nullable=False),
        sa.Column("status", sa.String(24), nullable=False),
        sa.Column("generation", sa.BigInteger(), nullable=False),
        sa.Column("credential_version", sa.BigInteger(), nullable=False),
        sa.Column("access_token_enc", sa.Text(), nullable=True),
        _ts("access_token_expires_at"),
        sa.Column("refresh_token_enc", sa.Text(), nullable=True),
        _ts("refresh_token_expires_at"),
        sa.Column("granted_scopes", sa.String(512), nullable=True),
        sa.Column("refresh_lease_id", sa.String(64), nullable=True),
        _ts("refresh_lease_expires_at"),
        sa.Column("connected_by_user_id", sa.Integer(), sa.ForeignKey("users.id", ondelete="SET NULL"),
                  nullable=True),
        _ts("connected_at"),
        _ts("disconnected_at"),
        sa.Column("disconnect_reason", sa.String(32), nullable=True),
        _ts("quarantined_at"),
        sa.Column("quarantine_reason", sa.String(32), nullable=True),
        _ts("revalidation_requested_at"),
        _ts("last_refreshed_at"),
        _ts("last_verified_at"),
        _ts("created_at", nullable=False, default=True),
        _ts("updated_at", nullable=False, default=True),
        sa.UniqueConstraint("shop_domain", name="uq_shopify_connections_shop_domain"),
        sa.UniqueConstraint("shop_gid", name="uq_shopify_connections_shop_gid"),
        sa.CheckConstraint(f"status IN ({_CREDENTIAL}, {_TOMBSTONE})", name="ck_shopify_connections_status"),
        sa.CheckConstraint(
            f"(status IN ({_CREDENTIAL})"
            " AND access_token_enc IS NOT NULL AND refresh_token_enc IS NOT NULL"
            " AND access_token_expires_at IS NOT NULL AND refresh_token_expires_at IS NOT NULL)"
            f" OR (status IN ({_TOMBSTONE})"
            " AND access_token_enc IS NULL AND refresh_token_enc IS NULL"
            " AND access_token_expires_at IS NULL AND refresh_token_expires_at IS NULL"
            " AND refresh_lease_id IS NULL AND refresh_lease_expires_at IS NULL)",
            name="ck_shopify_connections_credentials",
        ),
        sa.CheckConstraint("generation >= 1 AND credential_version >= 1", name="ck_shopify_connections_versions"),
        sa.Index("ix_shopify_connections_tenant_id", "tenant_id"),
    )
    states = sa.Table(
        STATES, meta,
        sa.Column("id", sa.BigInteger(), primary_key=True),
        sa.Column("state_hash", sa.String(64), nullable=False),
        sa.Column("tenant_id", sa.Integer(), sa.ForeignKey("tenants.id", ondelete="CASCADE"), nullable=False),
        sa.Column("actor_user_id", sa.Integer(), sa.ForeignKey("users.id", ondelete="CASCADE"), nullable=False),
        sa.Column("session_ref_hash", sa.String(64), nullable=False),
        sa.Column("shop_domain", sa.String(255), nullable=False),
        sa.Column("redirect_uri_fingerprint", sa.String(64), nullable=False),
        sa.Column("return_path", sa.String(64), nullable=False),
        sa.Column("connection_generation_at_start", sa.BigInteger(), nullable=False),
        sa.Column("status", sa.String(24), nullable=False),
        sa.Column("failure_code", sa.String(48), nullable=True),
        sa.Column("code_enc", sa.Text(), nullable=True),
        sa.Column("completion_handle_hash", sa.String(64), nullable=True),
        _ts("expires_at", nullable=False),
        _ts("completion_expires_at"),
        _ts("callback_at"),
        _ts("completed_at"),
        _ts("created_at", nullable=False, default=True),
        sa.UniqueConstraint("state_hash", name="uq_shopify_oauth_states_state_hash"),
        sa.UniqueConstraint("completion_handle_hash", name="uq_shopify_oauth_states_completion_handle_hash"),
        sa.CheckConstraint(
            "status IN ('pending', 'callback_received', 'exchanging', 'completed', 'failed')",
            name="ck_shopify_oauth_states_status",
        ),
        sa.CheckConstraint("code_enc IS NULL OR status = 'callback_received'",
                           name="ck_shopify_oauth_states_code_lifetime"),
        sa.Index("ix_shopify_oauth_states_tenant_status", "tenant_id", "status"),
        sa.Index("ix_shopify_oauth_states_expires_at", "expires_at"),
    )
    events = sa.Table(
        EVENTS, meta,
        sa.Column("id", sa.BigInteger(), primary_key=True),
        sa.Column("webhook_id", sa.String(128), nullable=True),
        sa.Column("topic", sa.String(64), nullable=False),
        sa.Column("shop_domain", sa.String(255), nullable=False),
        sa.Column("shop_gid", sa.String(64), nullable=False),
        sa.Column("body_sha256", sa.String(64), nullable=False),
        sa.Column("header_triggered_at", sa.String(64), nullable=True),
        sa.Column("connection_id", sa.BigInteger(), sa.ForeignKey(f"{CONNECTIONS}.id"), nullable=True),
        sa.Column("connection_generation", sa.BigInteger(), nullable=True),
        sa.Column("outcome", sa.String(32), nullable=False),
        sa.Column("resolution", sa.String(32), nullable=True),
        _ts("received_at", nullable=False, default=True),
        _ts("processed_at"),
        _ts("resolved_at"),
        sa.UniqueConstraint("webhook_id", name="uq_shopify_webhook_events_webhook_id"),
        sa.Index("ix_shopify_webhook_events_shop_digest", "shop_domain", "body_sha256"),
        sa.Index("ix_shopify_webhook_events_connection", "connection_id", "resolution"),
    )
    audit = sa.Table(
        AUDIT, meta,
        sa.Column("id", sa.BigInteger(), primary_key=True),
        sa.Column("tenant_id", sa.Integer(), sa.ForeignKey("tenants.id", ondelete="CASCADE"), nullable=False),
        sa.Column("connection_id", sa.BigInteger(), sa.ForeignKey(f"{CONNECTIONS}.id"), nullable=True),
        sa.Column("shop_domain", sa.String(255), nullable=False),
        sa.Column("action", sa.String(48), nullable=False),
        sa.Column("actor_user_id", sa.Integer(), nullable=True),
        sa.Column("generation", sa.BigInteger(), nullable=True),
        sa.Column("detail", postgresql.JSONB(), nullable=True),
        _ts("created_at", nullable=False, default=True),
        sa.Index("ix_shopify_connection_audit_tenant_created", "tenant_id", "created_at"),
    )
    return {CONNECTIONS: connections, STATES: states, EVENTS: events, AUDIT: audit}


def _type_key(column_type) -> tuple:
    if isinstance(column_type, sa.DateTime):
        return ("TIMESTAMPTZ" if column_type.timezone else "TIMESTAMP"), None
    if isinstance(column_type, sa.BigInteger):
        return "BIGINT", None
    if isinstance(column_type, sa.Integer):
        return "INTEGER", None
    if isinstance(column_type, sa.Text):
        return "TEXT", None
    if isinstance(column_type, sa.String):
        return "VARCHAR", column_type.length
    if isinstance(column_type, postgresql.JSONB):
        return "JSONB", None
    return type(column_type).__name__.upper(), None


def existing_table_mismatches(inspector, name: str, table: sa.Table) -> list:
    problems = []
    columns = {c["name"]: c for c in inspector.get_columns(name)}
    expected = {c.name: c for c in table.columns}
    for extra in sorted(set(columns) - set(expected)):
        problems.append(f"{name}: unexpected column {extra}")
    for col_name, col in expected.items():
        found = columns.get(col_name)
        if found is None:
            problems.append(f"{name}: missing column {col_name}")
            continue
        if _type_key(found["type"]) != _type_key(col.type):
            problems.append(f"{name}: column {col_name} has type {found['type']}")
        if bool(found.get("nullable", True)) != bool(col.nullable):
            problems.append(f"{name}: column {col_name} nullability differs")
    expected_uniques = sorted(sorted(c.name for c in u.columns) for u in table.constraints
                              if isinstance(u, sa.UniqueConstraint))
    found_uniques = sorted(sorted(u.get("column_names") or []) for u in inspector.get_unique_constraints(name))
    if found_uniques != expected_uniques:
        problems.append(f"{name}: uniqueness is {found_uniques}, expected {expected_uniques}")
    expected_checks = sorted(c.name for c in table.constraints if isinstance(c, sa.CheckConstraint))
    found_checks = sorted(c.get("name") or "" for c in inspector.get_check_constraints(name))
    if found_checks != expected_checks:
        problems.append(f"{name}: check constraints are {found_checks}, expected {expected_checks}")
    return problems


def upgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    present = set(inspector.get_table_names())
    definitions = _definitions()
    problems = []
    for name in TABLES:
        if name in present:
            problems.extend(existing_table_mismatches(inspector, name, definitions[name]))
    if problems:
        raise RuntimeError(
            "Shopify connection tables already exist with a different definition; inspect them before "
            f"applying this revision ({'; '.join(problems)})"
        )
    for name in TABLES:
        if name not in present:
            definitions[name].create(bind)


def downgrade() -> None:
    bind = op.get_bind()
    present = set(sa.inspect(bind).get_table_names())
    for name in reversed(TABLES):
        if name in present:
            op.drop_table(name)

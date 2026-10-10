"""
Shopify-owned tables on their own ``ShopifyBase`` metadata.

Deliberately outside ``models.Base``: production startup materialises
``models.Base`` through ``create_all``, so nothing here reaches a database
until revision ``0121`` is applied on purpose. The revision and this module
declare the same schema; ``backend/tests/test_shopify_connection_pg.py``
proves their equivalence on PostgreSQL.

Invariants enforced by the database itself:

  * one row per shop domain and one per Shopify shop id, unconditionally —
    a disconnected or uninstalled row stays as the ownership tombstone, so a
    different tenant can never take the shop over by reconnecting;
  * credentials exist exactly while the connection is ``active`` or
    ``quarantined``; every other status has no ciphertext
    (``ck_shopify_connections_credentials``);
  * at most one credential-producing Shopify call (code exchange or token
    refresh) per shop is in flight across all workers: ``shopify_shop_leases``
    holds one short, expiring lease row per shop. A lease is never ownership —
    it expires on its own, a stale one is taken over, and it is deleted in the
    same transaction that stores (or discards) the call's result;
  * an OAuth state carries an encrypted code only while ``callback_received``.

A connection row is only ever written after Shopify's authenticated identity
check and the tenant's authenticated completion succeed; starting, failing or
expiring an authorization writes only an ``shopify_oauth_states`` row, which
never reserves the shop.

PostgreSQL semantics are assumed (JSONB, row locks, ``RETURNING``).
"""
from __future__ import annotations

from datetime import datetime, timezone

from sqlalchemy import (
    BigInteger,
    CheckConstraint,
    Column,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    String,
    Table,
    Text,
    UniqueConstraint,
    text,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import declarative_base

ShopifyBase = declarative_base()

# Reference-only stubs so foreign keys resolve inside this metadata. They are
# never created from here: ``create_shopify_tables`` creates the
# Shopify tables only; the application owns ``tenants`` and ``users``.
tenants_reference = Table("tenants", ShopifyBase.metadata, Column("id", Integer, primary_key=True))
users_reference = Table("users", ShopifyBase.metadata, Column("id", Integer, primary_key=True))

CONNECTIONS_TABLE = "shopify_connections"
LEASES_TABLE = "shopify_shop_leases"
STATES_TABLE = "shopify_oauth_states"
WEBHOOK_EVENTS_TABLE = "shopify_webhook_events"
AUDIT_TABLE = "shopify_connection_audit"

STATUS_ACTIVE = "active"
STATUS_QUARANTINED = "quarantined"
STATUS_REAUTH_REQUIRED = "reauth_required"
STATUS_DISCONNECTED = "disconnected"
STATUS_UNINSTALLED = "uninstalled"
CREDENTIAL_STATUSES = (STATUS_ACTIVE, STATUS_QUARANTINED)
TOMBSTONE_STATUSES = (STATUS_REAUTH_REQUIRED, STATUS_DISCONNECTED, STATUS_UNINSTALLED)

STATE_PENDING = "pending"
STATE_CALLBACK_RECEIVED = "callback_received"
STATE_EXCHANGING = "exchanging"
STATE_COMPLETED = "completed"
STATE_FAILED = "failed"


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _in(values) -> str:
    return ", ".join(f"'{v}'" for v in values)


class ShopifyConnection(ShopifyBase):
    __tablename__ = CONNECTIONS_TABLE
    __table_args__ = (
        UniqueConstraint("shop_domain", name="uq_shopify_connections_shop_domain"),
        UniqueConstraint("shop_gid", name="uq_shopify_connections_shop_gid"),
        CheckConstraint(
            f"status IN ({_in(CREDENTIAL_STATUSES + TOMBSTONE_STATUSES)})",
            name="ck_shopify_connections_status",
        ),
        CheckConstraint(
            f"(status IN ({_in(CREDENTIAL_STATUSES)})"
            " AND access_token_enc IS NOT NULL AND refresh_token_enc IS NOT NULL"
            " AND access_token_expires_at IS NOT NULL AND refresh_token_expires_at IS NOT NULL)"
            f" OR (status IN ({_in(TOMBSTONE_STATUSES)})"
            " AND access_token_enc IS NULL AND refresh_token_enc IS NULL"
            " AND access_token_expires_at IS NULL AND refresh_token_expires_at IS NULL)",
            name="ck_shopify_connections_credentials",
        ),
        CheckConstraint("generation >= 1 AND credential_version >= 1", name="ck_shopify_connections_versions"),
        Index("ix_shopify_connections_tenant_id", "tenant_id"),
    )

    id = Column(BigInteger, primary_key=True)
    tenant_id = Column(Integer, ForeignKey("tenants.id"), nullable=False)
    shop_domain = Column(String(255), nullable=False)
    shop_gid = Column(String(64), nullable=False)
    status = Column(String(24), nullable=False)
    generation = Column(BigInteger, nullable=False)
    credential_version = Column(BigInteger, nullable=False)
    access_token_enc = Column(Text, nullable=True)
    access_token_expires_at = Column(DateTime(timezone=True), nullable=True)
    refresh_token_enc = Column(Text, nullable=True)
    refresh_token_expires_at = Column(DateTime(timezone=True), nullable=True)
    granted_scopes = Column(String(512), nullable=True)
    connected_by_user_id = Column(Integer, ForeignKey("users.id", ondelete="SET NULL"), nullable=True)
    connected_at = Column(DateTime(timezone=True), nullable=True)
    disconnected_at = Column(DateTime(timezone=True), nullable=True)
    disconnect_reason = Column(String(32), nullable=True)
    quarantined_at = Column(DateTime(timezone=True), nullable=True)
    quarantine_reason = Column(String(32), nullable=True)
    revalidation_requested_at = Column(DateTime(timezone=True), nullable=True)
    # Durable reconciliation schedule (quarantine / revalidation). A worker
    # claims a due row with a short lease; a crash leaves the lease to expire.
    # Bumped by every reconciliation request; a probe may only clear the
    # request version it snapshotted (a newer request stays pending).
    reconcile_request_version = Column(BigInteger, nullable=False, default=0, server_default=text("0"))
    reconcile_attempts = Column(Integer, nullable=False, default=0, server_default=text("0"))
    reconcile_next_at = Column(DateTime(timezone=True), nullable=True)
    reconcile_lease_id = Column(String(64), nullable=True)
    reconcile_lease_expires_at = Column(DateTime(timezone=True), nullable=True)
    last_refreshed_at = Column(DateTime(timezone=True), nullable=True)
    last_verified_at = Column(DateTime(timezone=True), nullable=True)
    created_at = Column(DateTime(timezone=True), nullable=False, default=_now, server_default=text("now()"))
    updated_at = Column(DateTime(timezone=True), nullable=False, default=_now, server_default=text("now()"))

    def __repr__(self) -> str:  # never the ciphertexts
        return (
            f"ShopifyConnection(id={self.id!r}, tenant_id={self.tenant_id!r}, shop_domain={self.shop_domain!r}, "
            f"status={self.status!r}, generation={self.generation!r}, credential_version={self.credential_version!r})"
        )


LEASE_EXCHANGE = "exchange"
LEASE_REFRESH = "refresh"


class ShopifyShopLease(ShopifyBase):
    """Short per-shop mutual exclusion for code exchange and token refresh.

    Shopify keeps one current expiring offline token per app and store: a new
    exchange or refresh retires the previous pair. Serialising those calls per
    shop keeps a losing call from retiring the credential a winner stores.
    """

    __tablename__ = LEASES_TABLE
    __table_args__ = (
        CheckConstraint("purpose IN ('exchange', 'refresh')", name="ck_shopify_shop_leases_purpose"),
    )

    shop_domain = Column(String(255), primary_key=True)
    lease_id = Column(String(64), nullable=False)
    purpose = Column(String(16), nullable=False)
    tenant_id = Column(Integer, nullable=False)
    acquired_at = Column(DateTime(timezone=True), nullable=False)
    expires_at = Column(DateTime(timezone=True), nullable=False)


class ShopifyOAuthState(ShopifyBase):
    __tablename__ = STATES_TABLE
    __table_args__ = (
        UniqueConstraint("state_hash", name="uq_shopify_oauth_states_state_hash"),
        UniqueConstraint("completion_handle_hash", name="uq_shopify_oauth_states_completion_handle_hash"),
        CheckConstraint(
            "status IN ('pending', 'callback_received', 'exchanging', 'completed', 'failed')",
            name="ck_shopify_oauth_states_status",
        ),
        CheckConstraint(
            "code_enc IS NULL OR status = 'callback_received'",
            name="ck_shopify_oauth_states_code_lifetime",
        ),
        Index("ix_shopify_oauth_states_tenant_status", "tenant_id", "status"),
        Index("ix_shopify_oauth_states_expires_at", "expires_at"),
    )

    id = Column(BigInteger, primary_key=True)
    state_hash = Column(String(64), nullable=False)
    tenant_id = Column(Integer, ForeignKey("tenants.id", ondelete="CASCADE"), nullable=False)
    actor_user_id = Column(Integer, ForeignKey("users.id", ondelete="CASCADE"), nullable=False)
    session_ref_hash = Column(String(64), nullable=False)
    shop_domain = Column(String(255), nullable=False)
    redirect_uri_fingerprint = Column(String(64), nullable=False)
    return_path = Column(String(64), nullable=False)
    connection_generation_at_start = Column(BigInteger, nullable=False)
    status = Column(String(24), nullable=False)
    failure_code = Column(String(48), nullable=True)
    code_enc = Column(Text, nullable=True)
    completion_handle_hash = Column(String(64), nullable=True)
    expires_at = Column(DateTime(timezone=True), nullable=False)
    completion_expires_at = Column(DateTime(timezone=True), nullable=True)
    callback_at = Column(DateTime(timezone=True), nullable=True)
    completed_at = Column(DateTime(timezone=True), nullable=True)
    created_at = Column(DateTime(timezone=True), nullable=False, default=_now, server_default=text("now()"))

    def __repr__(self) -> str:
        return (
            f"ShopifyOAuthState(id={self.id!r}, tenant_id={self.tenant_id!r}, shop_domain={self.shop_domain!r}, "
            f"status={self.status!r})"
        )


class ShopifyWebhookEvent(ShopifyBase):
    __tablename__ = WEBHOOK_EVENTS_TABLE
    __table_args__ = (
        UniqueConstraint("webhook_id", name="uq_shopify_webhook_events_webhook_id"),
        Index("ix_shopify_webhook_events_shop_digest", "shop_domain", "body_sha256"),
        Index("ix_shopify_webhook_events_connection", "connection_id", "resolution"),
    )

    id = Column(BigInteger, primary_key=True)
    # Unsigned delivery id: dedupe of redeliveries only, never evidence.
    webhook_id = Column(String(128), nullable=True)
    topic = Column(String(64), nullable=False)
    # Identity from the signed body.
    shop_domain = Column(String(255), nullable=False)
    shop_gid = Column(String(64), nullable=False)
    body_sha256 = Column(String(64), nullable=False)
    # Unsigned header, recorded for diagnostics only; never compared.
    header_triggered_at = Column(String(64), nullable=True)
    connection_id = Column(BigInteger, ForeignKey(f"{CONNECTIONS_TABLE}.id"), nullable=True)
    connection_generation = Column(BigInteger, nullable=True)
    outcome = Column(String(32), nullable=False)
    resolution = Column(String(32), nullable=True)
    received_at = Column(DateTime(timezone=True), nullable=False, default=_now, server_default=text("now()"))
    processed_at = Column(DateTime(timezone=True), nullable=True)
    resolved_at = Column(DateTime(timezone=True), nullable=True)


class ShopifyConnectionAudit(ShopifyBase):
    """Append-only, secret-free lifecycle evidence."""

    __tablename__ = AUDIT_TABLE
    __table_args__ = (Index("ix_shopify_connection_audit_tenant_created", "tenant_id", "created_at"),)

    id = Column(BigInteger, primary_key=True)
    tenant_id = Column(Integer, ForeignKey("tenants.id", ondelete="CASCADE"), nullable=False)
    connection_id = Column(BigInteger, ForeignKey(f"{CONNECTIONS_TABLE}.id"), nullable=True)
    shop_domain = Column(String(255), nullable=False)
    action = Column(String(48), nullable=False)
    actor_user_id = Column(Integer, nullable=True)
    generation = Column(BigInteger, nullable=True)
    detail = Column(JSONB, nullable=True)
    created_at = Column(DateTime(timezone=True), nullable=False, default=_now, server_default=text("now()"))


SHOPIFY_TABLES = (
    ShopifyConnection.__table__,
    ShopifyShopLease.__table__,
    ShopifyOAuthState.__table__,
    ShopifyWebhookEvent.__table__,
    ShopifyConnectionAudit.__table__,
)
SHOPIFY_TABLE_NAMES = tuple(t.name for t in SHOPIFY_TABLES)


def create_shopify_tables(bind) -> None:
    """Test/provisioning helper: the Shopify tables only (never tenants/users)."""
    ShopifyBase.metadata.create_all(bind, tables=list(SHOPIFY_TABLES))

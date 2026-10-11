"""
Shopify privacy compliance tables on their own ``ShopifyPrivacyBase`` metadata.

Deliberately outside ``models.Base`` (startup ``create_all``), the connection
foundation's ``ShopifyBase`` and the catalog mirror's ``ShopifyCatalogBase``:
nothing here reaches a database until revision ``0123`` is applied on
purpose. The revision and this module declare the same schema;
``backend/tests/test_shopify_privacy_webhooks_pg.py`` proves their equivalence
on PostgreSQL.

What is stored — and what is not
────────────────────────────────
One row per *logical* request (``shopify_privacy_requests``) keyed by the
topic, the shop identity **from the signed body** and the SHA-256 of the raw
signed body. No raw body, header, HMAC, customer email, phone, customer id or
order id is stored: only the customer reference *kind* (``id`` / ``email`` /
``phone`` / ``none``), the number of order ids and, for a data request, the
Shopify ``data_request.id``. Unsigned delivery headers are kept only in a
validated canonical form (a UUID webhook id, a parsed timestamp) and are
diagnostics, never evidence.

Tenant isolation: a request is attributed to a tenant only through the
connection that owns the signed shop identity, with the composite foreign
key ``(connection_id, tenant_id) → shopify_connections(id, tenant_id)``
(``uq_shopify_connections_id_tenant``, added by revision 0122). An unknown or
conflicting identity is stored without any tenant.

Lifecycle: every state except ``completed`` is open. A request that is
assessed, blocked on policy or routed to manual review keeps its deadline
visible; nothing here closes a request because the installation is active or
was reinstalled. ``completed`` requires completion evidence and an approved
policy version (CHECK), and is reachable only through the operator-only,
policy-gated executor.

PostgreSQL semantics are assumed (JSONB, partial indexes, row locks).
"""
from __future__ import annotations

from datetime import datetime, timezone

from sqlalchemy import (
    BigInteger,
    CheckConstraint,
    Column,
    DateTime,
    ForeignKey,
    ForeignKeyConstraint,
    Index,
    Integer,
    String,
    Table,
    UniqueConstraint,
    text,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import declarative_base

ShopifyPrivacyBase = declarative_base()

# Reference-only stubs so foreign keys resolve inside this metadata. Never
# created from here: the application owns ``tenants``; revision 0121 owns
# ``shopify_connections`` and revision 0122 adds its (id, tenant_id) key.
tenants_reference = Table("tenants", ShopifyPrivacyBase.metadata, Column("id", Integer, primary_key=True))
connections_reference = Table(
    "shopify_connections", ShopifyPrivacyBase.metadata,
    Column("id", BigInteger, primary_key=True),
    Column("tenant_id", Integer, nullable=False),
    UniqueConstraint("id", "tenant_id", name="uq_shopify_connections_id_tenant"),
)

REQUESTS_TABLE = "shopify_privacy_requests"
DELIVERIES_TABLE = "shopify_privacy_deliveries"
EVENTS_TABLE = "shopify_privacy_request_events"

TOPIC_CUSTOMERS_DATA_REQUEST = "customers/data_request"
TOPIC_CUSTOMERS_REDACT = "customers/redact"
TOPIC_SHOP_REDACT = "shop/redact"
TOPICS = (TOPIC_CUSTOMERS_DATA_REQUEST, TOPIC_CUSTOMERS_REDACT, TOPIC_SHOP_REDACT)
CUSTOMER_TOPICS = (TOPIC_CUSTOMERS_DATA_REQUEST, TOPIC_CUSTOMERS_REDACT)

IDENTITY_MATCHED = "matched"
IDENTITY_UNKNOWN = "unknown"
IDENTITY_CONFLICT = "conflict"
IDENTITY_STATES = (IDENTITY_MATCHED, IDENTITY_UNKNOWN, IDENTITY_CONFLICT)

REF_ID = "id"
REF_EMAIL = "email"
REF_PHONE = "phone"
REF_NONE = "none"
CUSTOMER_REF_KINDS = (REF_ID, REF_EMAIL, REF_PHONE, REF_NONE)

STATE_RECEIVED = "received"
STATE_AWAITING_EVIDENCE = "awaiting_evidence"
STATE_ASSESSED = "assessed"
STATE_BLOCKED_POLICY = "blocked_policy"
STATE_MANUAL_REVIEW = "manual_review"
STATE_COMPLETED = "completed"
STATES = (STATE_RECEIVED, STATE_AWAITING_EVIDENCE, STATE_ASSESSED, STATE_BLOCKED_POLICY,
          STATE_MANUAL_REVIEW, STATE_COMPLETED)
# Due for the worker (next_attempt_at set). Every state but ``completed`` is open.
DUE_STATES = (STATE_RECEIVED, STATE_AWAITING_EVIDENCE)
OPEN_STATES = tuple(s for s in STATES if s != STATE_COMPLETED)

SHOP_DOMAIN_RE = r"^[a-z0-9][a-z0-9-]{0,62}\.myshopify\.com$"
SHOP_GID_RE = r"^gid://shopify/Shop/[1-9][0-9]{0,19}$"
DIGEST_RE = r"^[0-9a-f]{64}$"
WEBHOOK_ID_RE = r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$"

MAX_DELIVERY_ROWS_PER_REQUEST = 20


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _in(values) -> str:
    return ", ".join(f"'{v}'" for v in values)


def _ts(name: str, nullable: bool = True, default: bool = False) -> Column:
    kwargs = {"default": _now, "server_default": text("now()")} if default else {}
    return Column(name, DateTime(timezone=True), nullable=nullable, **kwargs)


class ShopifyPrivacyRequest(ShopifyPrivacyBase):
    __tablename__ = REQUESTS_TABLE
    __table_args__ = (
        ForeignKeyConstraint(["connection_id", "tenant_id"], ["shopify_connections.id", "shopify_connections.tenant_id"],
                             name="fk_shopify_privacy_requests_connection"),
        CheckConstraint(f"topic IN ({_in(TOPICS)})", name="ck_shopify_privacy_requests_topic"),
        CheckConstraint(f"shop_domain ~ '{SHOP_DOMAIN_RE}'", name="ck_shopify_privacy_requests_shop_domain"),
        CheckConstraint(f"shop_gid ~ '{SHOP_GID_RE}'", name="ck_shopify_privacy_requests_shop_gid"),
        CheckConstraint(f"body_sha256 ~ '{DIGEST_RE}'", name="ck_shopify_privacy_requests_digest"),
        CheckConstraint(f"identity_state IN ({_in(IDENTITY_STATES)})", name="ck_shopify_privacy_requests_identity"),
        # A tenant only through the owning connection; matched exactly when attributed.
        CheckConstraint(
            "(connection_id IS NULL) = (tenant_id IS NULL)"
            " AND (identity_state = 'matched') = (connection_id IS NOT NULL)"
            " AND (receipt_generation IS NULL OR connection_id IS NOT NULL)"
            " AND (last_delivery_generation IS NULL OR connection_id IS NOT NULL)",
            name="ck_shopify_privacy_requests_attribution",
        ),
        CheckConstraint(f"customer_ref_kind IS NULL OR customer_ref_kind IN ({_in(CUSTOMER_REF_KINDS)})",
                        name="ck_shopify_privacy_requests_ref_kind"),
        CheckConstraint(
            "(topic = 'shop/redact' AND customer_ref_kind IS NULL AND orders_count IS NULL"
            "  AND data_request_id IS NULL)"
            # Explicit IS NOT NULL: a NULL comparison would satisfy a CHECK.
            " OR (topic = 'customers/redact' AND customer_ref_kind IS NOT NULL AND orders_count IS NOT NULL"
            "  AND orders_count >= 0 AND data_request_id IS NULL)"
            " OR (topic = 'customers/data_request' AND customer_ref_kind IS NOT NULL AND orders_count IS NOT NULL"
            "  AND orders_count >= 0 AND data_request_id IS NOT NULL AND data_request_id >= 1)",
            name="ck_shopify_privacy_requests_topic_fields",
        ),
        CheckConstraint(f"state IN ({_in(STATES)})", name="ck_shopify_privacy_requests_state"),
        CheckConstraint(
            f"(state IN ({_in(DUE_STATES)})) = (next_attempt_at IS NOT NULL)"
            f" AND (lease_id IS NULL OR state IN ({_in(DUE_STATES)}))"
            " AND (lease_id IS NULL) = (lease_expires_at IS NULL)",
            name="ck_shopify_privacy_requests_schedule",
        ),
        CheckConstraint("state IN ('received', 'completed') OR reason IS NOT NULL",
                        name="ck_shopify_privacy_requests_reason"),
        # An assessment names its audit scope and carries its counts-only inventory.
        CheckConstraint(
            "state NOT IN ('assessed', 'blocked_policy', 'completed')"
            " OR (audit_version IS NOT NULL AND assessed_at IS NOT NULL AND proposed_action IS NOT NULL)",
            name="ck_shopify_privacy_requests_assessment",
        ),
        CheckConstraint(
            "state <> 'blocked_policy' OR (topic = 'shop/redact' AND connection_id IS NOT NULL"
            " AND eligible_generation IS NOT NULL AND evaluated_delivery_count IS NOT NULL)",
            name="ck_shopify_privacy_requests_blocked_policy",
        ),
        # Completion is never fabricated: it needs an approved policy and evidence,
        # and no other state may carry either.
        CheckConstraint(
            "(state = 'completed' AND completed_at IS NOT NULL AND policy_version IS NOT NULL"
            "  AND completion_evidence IS NOT NULL AND topic = 'shop/redact' AND connection_id IS NOT NULL)"
            " OR (state <> 'completed' AND completed_at IS NULL AND policy_version IS NULL"
            "  AND completion_evidence IS NULL)",
            name="ck_shopify_privacy_requests_completion",
        ),
        CheckConstraint(
            "attempts >= 0 AND delivery_count >= 1 AND deadline_at > received_at"
            " AND last_delivery_at >= received_at"
            " AND (probe_delivery_count IS NULL OR probe_delivery_count BETWEEN 1 AND delivery_count)"
            " AND (evaluated_delivery_count IS NULL OR evaluated_delivery_count BETWEEN 1 AND delivery_count)"
            " AND (probe_delivery_count IS NULL) = (probe_requested_at IS NULL)"
            " AND (probe_delivery_count IS NULL) = (probe_reconcile_version IS NULL)",
            name="ck_shopify_privacy_requests_counters",
        ),
        # One open request per signed body and shop identity: concurrent and
        # repeated deliveries of the same body resolve to one row.
        Index("uq_shopify_privacy_requests_open", "topic", "shop_domain", "shop_gid", "body_sha256", unique=True,
              postgresql_where=text(f"state IN ({_in(OPEN_STATES)})")),
        Index("ix_shopify_privacy_requests_due", "next_attempt_at",
              postgresql_where=text(f"state IN ({_in(DUE_STATES)})")),
        Index("ix_shopify_privacy_requests_tenant", "tenant_id", "connection_id", "state"),
        Index("ix_shopify_privacy_requests_shop", "shop_domain", "shop_gid"),
    )

    id = Column(BigInteger, primary_key=True)
    topic = Column(String(32), nullable=False)
    shop_domain = Column(String(255), nullable=False)
    shop_gid = Column(String(64), nullable=False)
    body_sha256 = Column(String(64), nullable=False)
    identity_state = Column(String(16), nullable=False)
    tenant_id = Column(Integer, ForeignKey("tenants.id"), nullable=True)
    connection_id = Column(BigInteger, nullable=True)
    # Connection generation observed at the first and at the latest delivery
    # (our own state, read when the delivery was stored — never a header).
    receipt_generation = Column(BigInteger, nullable=True)
    last_delivery_generation = Column(BigInteger, nullable=True)
    customer_ref_kind = Column(String(8), nullable=True)
    orders_count = Column(Integer, nullable=True)
    data_request_id = Column(BigInteger, nullable=True)
    state = Column(String(24), nullable=False)
    reason = Column(String(48), nullable=True)
    audit_version = Column(String(48), nullable=True)
    proposed_action = Column(JSONB, nullable=True)
    eligible_generation = Column(BigInteger, nullable=True)
    evaluated_delivery_count = Column(Integer, nullable=True)
    # The foundation revalidation this request asked for: the delivery count it
    # covers and the reconcile request version it produced.
    probe_requested_at = _ts("probe_requested_at")
    probe_delivery_count = Column(Integer, nullable=True)
    probe_reconcile_version = Column(BigInteger, nullable=True)
    attempts = Column(Integer, nullable=False, default=0, server_default=text("0"))
    next_attempt_at = _ts("next_attempt_at")
    lease_id = Column(String(64), nullable=True)
    lease_expires_at = _ts("lease_expires_at")
    delivery_count = Column(Integer, nullable=False, default=1, server_default=text("1"))
    received_at = _ts("received_at", nullable=False)
    last_delivery_at = _ts("last_delivery_at", nullable=False)
    deadline_at = _ts("deadline_at", nullable=False)
    assessed_at = _ts("assessed_at")
    completed_at = _ts("completed_at")
    policy_version = Column(String(64), nullable=True)
    completion_evidence = Column(JSONB, nullable=True)
    created_at = _ts("created_at", nullable=False, default=True)
    updated_at = _ts("updated_at", nullable=False, default=True)

    def __repr__(self) -> str:
        return (f"ShopifyPrivacyRequest(id={self.id!r}, topic={self.topic!r}, state={self.state!r}, "
                f"tenant_id={self.tenant_id!r}, connection_id={self.connection_id!r})")


class ShopifyPrivacyDelivery(ShopifyPrivacyBase):
    """Capped per-request delivery log. Unsigned header values are kept only
    in validated canonical form (diagnostics; never evidence)."""

    __tablename__ = DELIVERIES_TABLE
    __table_args__ = (
        UniqueConstraint("request_id", "webhook_id", name="uq_shopify_privacy_deliveries_webhook"),
        CheckConstraint(f"webhook_id IS NULL OR webhook_id ~ '{WEBHOOK_ID_RE}'",
                        name="ck_shopify_privacy_deliveries_webhook_id"),
        Index("ix_shopify_privacy_deliveries_request", "request_id", "id"),
    )

    id = Column(BigInteger, primary_key=True)
    request_id = Column(BigInteger, ForeignKey(f"{REQUESTS_TABLE}.id", ondelete="CASCADE"), nullable=False)
    webhook_id = Column(String(36), nullable=True)
    header_triggered_at = _ts("header_triggered_at")
    connection_generation = Column(BigInteger, nullable=True)
    received_at = _ts("received_at", nullable=False, default=True)


class ShopifyPrivacyRequestEvent(ShopifyPrivacyBase):
    """Append-only transitions: fixed codes and counts only (no PII)."""

    __tablename__ = EVENTS_TABLE
    __table_args__ = (Index("ix_shopify_privacy_request_events_request", "request_id", "id"),)

    id = Column(BigInteger, primary_key=True)
    request_id = Column(BigInteger, ForeignKey(f"{REQUESTS_TABLE}.id", ondelete="CASCADE"), nullable=False)
    tenant_id = Column(Integer, ForeignKey("tenants.id"), nullable=True)
    action = Column(String(48), nullable=False)
    state = Column(String(24), nullable=True)
    reason = Column(String(48), nullable=True)
    detail = Column(JSONB, nullable=True)
    created_at = _ts("created_at", nullable=False, default=True)


PRIVACY_TABLES = (
    ShopifyPrivacyRequest.__table__,
    ShopifyPrivacyDelivery.__table__,
    ShopifyPrivacyRequestEvent.__table__,
)
PRIVACY_TABLE_NAMES = tuple(t.name for t in PRIVACY_TABLES)


def create_privacy_tables(bind) -> None:
    """Test/provisioning helper: the privacy tables only (never tenants, the
    foundation or the mirror). The connection key must already exist."""
    ShopifyPrivacyBase.metadata.create_all(bind, tables=list(PRIVACY_TABLES))

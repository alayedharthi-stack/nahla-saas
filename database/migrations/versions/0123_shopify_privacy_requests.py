"""Dormant Shopify privacy compliance request tables (intake + evidence, flags off).

Revision ID: 0123
Revises: 0122

Creates the three tables declared on ``ShopifyPrivacyBase`` in
``backend/services/shopify_privacy/models.py`` (not part of ``models.Base``,
so startup ``create_all`` never builds them):

  shopify_privacy_requests          one row per logical compliance request
                                    (topic + signed shop identity + body
                                    digest); tenant only through the owning
                                    connection's (id, tenant_id) key; state,
                                    counts-only proposed action, worker lease,
                                    30-day deadline, completion evidence CHECK
  shopify_privacy_deliveries        capped delivery log (canonical UUID webhook
                                    id / parsed timestamp only)
  shopify_privacy_request_events    append-only, PII-free transitions

No raw body, header, HMAC, email, phone, customer id or order id has a column.
Nothing existing is altered: the composite foreign key reuses
``uq_shopify_connections_id_tenant`` from revision 0122.

Fail closed:
  * the foundation (0121) and catalog mirror (0122) tables and the
    ``shopify_connections(id, tenant_id)`` key must exist, else the upgrade
    refuses;
  * if any privacy table already exists — complete, partial or malformed —
    the upgrade refuses and changes nothing: they are new here, so a
    pre-existing one was not built by this revision.

Chained on 0122 and replaces it as that branch's head; normal bootstrap
(pinned to 0093) never applies it. Apply only as an explicit, owner-approved
step. Downgrade drops only these three tables.
"""
from __future__ import annotations

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "0123"
down_revision = "0122"
branch_labels = None
depends_on = None

CONNECTIONS = "shopify_connections"
CONNECTION_KEY = "uq_shopify_connections_id_tenant"
REQUESTS = "shopify_privacy_requests"
DELIVERIES = "shopify_privacy_deliveries"
EVENTS = "shopify_privacy_request_events"
TABLES = (REQUESTS, DELIVERIES, EVENTS)
FOUNDATION_TABLES = ("shopify_connections", "shopify_shop_leases", "shopify_oauth_states",
                     "shopify_webhook_events", "shopify_connection_audit")
CATALOG_TABLES = ("shopify_catalog_import_runs", "shopify_catalog_staged_records", "shopify_catalog_products",
                  "shopify_catalog_inventory_items", "shopify_catalog_variants", "shopify_catalog_locations",
                  "shopify_catalog_inventory_levels")

_TOPICS = "'customers/data_request', 'customers/redact', 'shop/redact'"
_STATES = "'received', 'awaiting_evidence', 'assessed', 'blocked_policy', 'manual_review', 'completed'"
_OPEN = "'received', 'awaiting_evidence', 'assessed', 'blocked_policy', 'manual_review'"
_DUE = "'received', 'awaiting_evidence'"


def _ts(name: str, nullable: bool = True, default: bool = False) -> sa.Column:
    kwargs = {"server_default": sa.text("now()")} if default else {}
    return sa.Column(name, sa.DateTime(timezone=True), nullable=nullable, **kwargs)


def _definitions() -> dict:
    meta = sa.MetaData()
    sa.Table("tenants", meta, sa.Column("id", sa.Integer, primary_key=True))
    sa.Table(CONNECTIONS, meta, sa.Column("id", sa.BigInteger, primary_key=True),
             sa.Column("tenant_id", sa.Integer, nullable=False),
             sa.UniqueConstraint("id", "tenant_id", name=CONNECTION_KEY))
    requests = sa.Table(
        REQUESTS, meta,
        sa.Column("id", sa.BigInteger(), primary_key=True),
        sa.Column("topic", sa.String(32), nullable=False),
        sa.Column("shop_domain", sa.String(255), nullable=False),
        sa.Column("shop_gid", sa.String(64), nullable=False),
        sa.Column("body_sha256", sa.String(64), nullable=False),
        sa.Column("identity_state", sa.String(16), nullable=False),
        sa.Column("tenant_id", sa.Integer(), sa.ForeignKey("tenants.id"), nullable=True),
        sa.Column("connection_id", sa.BigInteger(), nullable=True),
        sa.Column("receipt_generation", sa.BigInteger(), nullable=True),
        sa.Column("last_delivery_generation", sa.BigInteger(), nullable=True),
        sa.Column("customer_ref_kind", sa.String(8), nullable=True),
        sa.Column("orders_count", sa.Integer(), nullable=True),
        sa.Column("data_request_id", sa.BigInteger(), nullable=True),
        sa.Column("state", sa.String(24), nullable=False),
        sa.Column("reason", sa.String(48), nullable=True),
        sa.Column("audit_version", sa.String(48), nullable=True),
        sa.Column("proposed_action", postgresql.JSONB(), nullable=True),
        sa.Column("eligible_generation", sa.BigInteger(), nullable=True),
        sa.Column("evaluated_delivery_count", sa.Integer(), nullable=True),
        _ts("probe_requested_at"),
        sa.Column("probe_delivery_count", sa.Integer(), nullable=True),
        sa.Column("probe_reconcile_version", sa.BigInteger(), nullable=True),
        sa.Column("attempts", sa.Integer(), nullable=False, server_default=sa.text("0")),
        _ts("next_attempt_at"),
        sa.Column("lease_id", sa.String(64), nullable=True),
        _ts("lease_expires_at"),
        sa.Column("delivery_count", sa.Integer(), nullable=False, server_default=sa.text("1")),
        _ts("received_at", nullable=False),
        _ts("last_delivery_at", nullable=False),
        _ts("deadline_at", nullable=False),
        _ts("assessed_at"),
        _ts("completed_at"),
        sa.Column("policy_version", sa.String(64), nullable=True),
        sa.Column("completion_evidence", postgresql.JSONB(), nullable=True),
        _ts("created_at", nullable=False, default=True),
        _ts("updated_at", nullable=False, default=True),
        sa.ForeignKeyConstraint(["connection_id", "tenant_id"], [f"{CONNECTIONS}.id", f"{CONNECTIONS}.tenant_id"],
                                name="fk_shopify_privacy_requests_connection"),
        sa.CheckConstraint(f"topic IN ({_TOPICS})", name="ck_shopify_privacy_requests_topic"),
        sa.CheckConstraint("shop_domain ~ '^[a-z0-9][a-z0-9-]{0,62}\\.myshopify\\.com$'",
                           name="ck_shopify_privacy_requests_shop_domain"),
        sa.CheckConstraint("shop_gid ~ '^gid://shopify/Shop/[1-9][0-9]{0,19}$'",
                           name="ck_shopify_privacy_requests_shop_gid"),
        sa.CheckConstraint("body_sha256 ~ '^[0-9a-f]{64}$'", name="ck_shopify_privacy_requests_digest"),
        sa.CheckConstraint("identity_state IN ('matched', 'unknown', 'conflict')",
                           name="ck_shopify_privacy_requests_identity"),
        sa.CheckConstraint(
            "(connection_id IS NULL) = (tenant_id IS NULL)"
            " AND (identity_state = 'matched') = (connection_id IS NOT NULL)"
            " AND (receipt_generation IS NULL OR connection_id IS NOT NULL)"
            " AND (last_delivery_generation IS NULL OR connection_id IS NOT NULL)",
            name="ck_shopify_privacy_requests_attribution",
        ),
        sa.CheckConstraint("customer_ref_kind IS NULL OR customer_ref_kind IN ('id', 'email', 'phone', 'none')",
                           name="ck_shopify_privacy_requests_ref_kind"),
        sa.CheckConstraint(
            "(topic = 'shop/redact' AND customer_ref_kind IS NULL AND orders_count IS NULL"
            "  AND data_request_id IS NULL)"
            " OR (topic = 'customers/redact' AND customer_ref_kind IS NOT NULL AND orders_count IS NOT NULL"
            "  AND orders_count >= 0 AND data_request_id IS NULL)"
            " OR (topic = 'customers/data_request' AND customer_ref_kind IS NOT NULL AND orders_count IS NOT NULL"
            "  AND orders_count >= 0 AND data_request_id IS NOT NULL AND data_request_id >= 1)",
            name="ck_shopify_privacy_requests_topic_fields",
        ),
        sa.CheckConstraint(f"state IN ({_STATES})", name="ck_shopify_privacy_requests_state"),
        sa.CheckConstraint(
            f"(state IN ({_DUE})) = (next_attempt_at IS NOT NULL)"
            f" AND (lease_id IS NULL OR state IN ({_DUE}))"
            " AND (lease_id IS NULL) = (lease_expires_at IS NULL)",
            name="ck_shopify_privacy_requests_schedule",
        ),
        sa.CheckConstraint("state IN ('received', 'completed') OR reason IS NOT NULL",
                           name="ck_shopify_privacy_requests_reason"),
        sa.CheckConstraint(
            "state NOT IN ('assessed', 'blocked_policy', 'completed')"
            " OR (audit_version IS NOT NULL AND assessed_at IS NOT NULL AND proposed_action IS NOT NULL)",
            name="ck_shopify_privacy_requests_assessment",
        ),
        sa.CheckConstraint(
            "state <> 'blocked_policy' OR (topic = 'shop/redact' AND connection_id IS NOT NULL"
            " AND eligible_generation IS NOT NULL AND evaluated_delivery_count IS NOT NULL)",
            name="ck_shopify_privacy_requests_blocked_policy",
        ),
        sa.CheckConstraint(
            "(state = 'completed' AND completed_at IS NOT NULL AND policy_version IS NOT NULL"
            "  AND completion_evidence IS NOT NULL AND topic = 'shop/redact' AND connection_id IS NOT NULL)"
            " OR (state <> 'completed' AND completed_at IS NULL AND policy_version IS NULL"
            "  AND completion_evidence IS NULL)",
            name="ck_shopify_privacy_requests_completion",
        ),
        sa.CheckConstraint(
            "attempts >= 0 AND delivery_count >= 1 AND deadline_at > received_at"
            " AND last_delivery_at >= received_at"
            " AND (probe_delivery_count IS NULL OR probe_delivery_count BETWEEN 1 AND delivery_count)"
            " AND (evaluated_delivery_count IS NULL OR evaluated_delivery_count BETWEEN 1 AND delivery_count)"
            " AND (probe_delivery_count IS NULL) = (probe_requested_at IS NULL)"
            " AND (probe_delivery_count IS NULL) = (probe_reconcile_version IS NULL)",
            name="ck_shopify_privacy_requests_counters",
        ),
        sa.Index("uq_shopify_privacy_requests_open", "topic", "shop_domain", "shop_gid", "body_sha256", unique=True,
                 postgresql_where=sa.text(f"state IN ({_OPEN})")),
        sa.Index("ix_shopify_privacy_requests_due", "next_attempt_at", postgresql_where=sa.text(f"state IN ({_DUE})")),
        sa.Index("ix_shopify_privacy_requests_tenant", "tenant_id", "connection_id", "state"),
        sa.Index("ix_shopify_privacy_requests_shop", "shop_domain", "shop_gid"),
    )
    deliveries = sa.Table(
        DELIVERIES, meta,
        sa.Column("id", sa.BigInteger(), primary_key=True),
        sa.Column("request_id", sa.BigInteger(), sa.ForeignKey(f"{REQUESTS}.id", ondelete="CASCADE"), nullable=False),
        sa.Column("webhook_id", sa.String(36), nullable=True),
        _ts("header_triggered_at"),
        sa.Column("connection_generation", sa.BigInteger(), nullable=True),
        _ts("received_at", nullable=False, default=True),
        sa.UniqueConstraint("request_id", "webhook_id", name="uq_shopify_privacy_deliveries_webhook"),
        sa.CheckConstraint(
            "webhook_id IS NULL OR webhook_id ~ '^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$'",
            name="ck_shopify_privacy_deliveries_webhook_id",
        ),
        sa.Index("ix_shopify_privacy_deliveries_request", "request_id", "id"),
    )
    events = sa.Table(
        EVENTS, meta,
        sa.Column("id", sa.BigInteger(), primary_key=True),
        sa.Column("request_id", sa.BigInteger(), sa.ForeignKey(f"{REQUESTS}.id", ondelete="CASCADE"), nullable=False),
        sa.Column("tenant_id", sa.Integer(), sa.ForeignKey("tenants.id"), nullable=True),
        sa.Column("action", sa.String(48), nullable=False),
        sa.Column("state", sa.String(24), nullable=True),
        sa.Column("reason", sa.String(48), nullable=True),
        sa.Column("detail", postgresql.JSONB(), nullable=True),
        _ts("created_at", nullable=False, default=True),
        sa.Index("ix_shopify_privacy_request_events_request", "request_id", "id"),
    )
    return {REQUESTS: requests, DELIVERIES: deliveries, EVENTS: events}


def _connection_key_present(inspector) -> bool:
    return any(c.get("name") == CONNECTION_KEY for c in inspector.get_unique_constraints(CONNECTIONS))


def upgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    names = set(inspector.get_table_names())
    missing = sorted((set(FOUNDATION_TABLES) | set(CATALOG_TABLES)) - names)
    if missing:
        raise RuntimeError(
            "Shopify foundation / catalog tables are missing (" + ", ".join(missing) + "); revisions 0121 and 0122 "
            "must be applied before 0123."
        )
    if not _connection_key_present(inspector):
        raise RuntimeError(f"{CONNECTIONS} has no {CONNECTION_KEY} key (revision 0122); refusing 0123.")
    present = sorted(names & set(TABLES))
    if present:
        # New in this revision: a pre-existing table was not built here and its
        # constraints (tenant-carrying key, completion evidence, open-request
        # uniqueness) cannot be trusted. Refuse without changing anything.
        raise RuntimeError(
            "Shopify privacy tables already exist (" + ", ".join(present) + "); they were not created by revision "
            "0123. Inspect and remove them manually before applying this revision."
        )
    definitions = _definitions()
    for name in TABLES:
        definitions[name].create(bind)


def downgrade() -> None:
    bind = op.get_bind()
    present = set(sa.inspect(bind).get_table_names())
    for name in reversed(TABLES):
        if name in present:
            op.drop_table(name)

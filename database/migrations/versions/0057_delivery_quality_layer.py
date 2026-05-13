"""0057 — Delivery Quality Intelligence Layer: 4 new tables.

Revision ID: 0057
Revises:    0056

Why this migration exists
─────────────────────────
The platform's WhatsApp deliverability used to be observed in two
narrow places: a single ``meta_quality_rating`` column on
``whatsapp_connections`` (overwritten on every Meta sync, no
history) and per-recipient timestamps on ``campaign_send_logs``
(first-occurrence only, campaign-scoped only).

That is enough to debug an individual campaign but not enough to:
  * reconstruct why a WABA's quality drifted over time,
  * stop sending to a phone that consistently fails delivery,
  * compute a Quality Score per number,
  * surface a tenant-wide dashboard of delivery health,
  * or replay a Meta webhook months later for forensic analysis.

This migration creates the four append-only / upsert-only tables
that back the Delivery Quality Intelligence Layer:

  1. ``wa_webhook_raw``
     Raw archive of every Meta / 360dialog webhook payload we
     receive. Pure observability — we never retry from it and
     never delete from it.

  2. ``message_delivery_events``
     Append-only per-status event log. ``CampaignSendLog`` only
     stamps the FIRST ``delivered_at`` / ``read_at`` / ``failed_at``
     timestamp; this table keeps the FULL transition history,
     including for automation / inbox / order-event sends that
     ``CampaignSendLog`` doesn't cover at all.

  3. ``customer_suppressions``
     First-class suppression list. One row per
     (tenant_id, normalized_phone) the Suppression Engine has
     blocked. Reasons accumulate in ``reasons`` JSONB; rows are
     marked ``is_active=False`` (never deleted) when the
     auto-reinstate logic fires on inbound messages.

  4. ``wa_number_quality_snapshots``
     Periodic snapshots of a WhatsApp Business number's quality.
     Meta-reported rating + Nahla-computed score on the same row
     so the dashboard can plot them side by side. Driven by the
     scheduler every ~30 min plus inline writes on critical events.

Idempotency
───────────
Every ``create_table`` / ``create_index`` is guarded by an
inspector check so re-running this migration on a DB where any of
the four tables already exist is a no-op — matches the project's
F16 convention across all recent migrations.
"""
from __future__ import annotations

import sqlalchemy as sa
from alembic import op
from sqlalchemy import inspect
from sqlalchemy.dialects.postgresql import JSONB

revision = "0057"
down_revision = "0056"
branch_labels = None
depends_on = None


def _has_table(bind, table_name: str) -> bool:
    return table_name in inspect(bind).get_table_names()


def _has_index(bind, table_name: str, index_name: str) -> bool:
    if not _has_table(bind, table_name):
        return False
    return any(
        ix["name"] == index_name
        for ix in inspect(bind).get_indexes(table_name)
    )


def upgrade() -> None:
    bind = op.get_bind()

    # SQLite test envs use JSON; Postgres uses JSONB.
    jsonb_type = JSONB().with_variant(sa.JSON(), "sqlite")
    bigint_type = sa.BigInteger().with_variant(sa.Integer(), "sqlite")

    # ── 1. wa_webhook_raw ──────────────────────────────────────────
    if not _has_table(bind, "wa_webhook_raw"):
        op.create_table(
            "wa_webhook_raw",
            sa.Column("id", bigint_type, primary_key=True),
            sa.Column(
                "tenant_id", sa.Integer(),
                sa.ForeignKey("tenants.id"), nullable=True,
            ),
            sa.Column("provider", sa.String(length=32), nullable=False),
            sa.Column("source_path", sa.String(length=255), nullable=True),
            sa.Column("wamid", sa.String(length=255), nullable=True),
            sa.Column("status", sa.String(length=32), nullable=False),
            sa.Column("raw_error_code", sa.String(length=32), nullable=True),
            sa.Column("raw_error_subcode", sa.String(length=32), nullable=True),
            sa.Column("classified_key", sa.String(length=64), nullable=True),
            sa.Column("quality_tier", sa.String(length=16), nullable=True),
            sa.Column("raw_body", sa.Text(), nullable=True),
            sa.Column("raw_headers", jsonb_type, nullable=True),
            sa.Column("parsed_payload", jsonb_type, nullable=True),
            sa.Column("campaign_send_log_id", sa.Integer(), nullable=True),
            sa.Column("automation_execution_id", sa.Integer(), nullable=True),
            sa.Column(
                "received_at",
                sa.DateTime(timezone=True),
                server_default=sa.text("CURRENT_TIMESTAMP"),
                nullable=False,
            ),
        )
    for ix_name, cols in (
        ("ix_wa_webhook_raw_received_at",     ["received_at"]),
        ("ix_wa_webhook_raw_tenant_received", ["tenant_id", "received_at"]),
        ("ix_wa_webhook_raw_wamid",           ["wamid"]),
        ("ix_wa_webhook_raw_classified_key",  ["classified_key"]),
    ):
        if not _has_index(bind, "wa_webhook_raw", ix_name):
            op.create_index(ix_name, "wa_webhook_raw", cols)

    # ── 2. message_delivery_events ─────────────────────────────────
    if not _has_table(bind, "message_delivery_events"):
        op.create_table(
            "message_delivery_events",
            sa.Column("id", bigint_type, primary_key=True),
            sa.Column(
                "tenant_id", sa.Integer(),
                sa.ForeignKey("tenants.id"), nullable=False,
            ),
            sa.Column("wamid", sa.String(length=255), nullable=False),
            sa.Column("phone_e164", sa.String(length=32), nullable=True),
            sa.Column("status", sa.String(length=32), nullable=False),
            sa.Column("error_code", sa.String(length=64), nullable=True),
            sa.Column("error_message", sa.Text(), nullable=True),
            sa.Column("raw_code", sa.String(length=32), nullable=True),
            sa.Column("raw_subcode", sa.String(length=32), nullable=True),
            sa.Column("quality_tier", sa.String(length=16), nullable=True),
            sa.Column(
                "suppress_on_repeat", sa.Boolean(),
                server_default=sa.text("false"), nullable=False,
            ),
            sa.Column(
                "occurred_at",
                sa.DateTime(timezone=True),
                server_default=sa.text("CURRENT_TIMESTAMP"),
                nullable=False,
            ),
            sa.Column("campaign_send_log_id", sa.Integer(), nullable=True),
            sa.Column("automation_execution_id", sa.Integer(), nullable=True),
            sa.Column("template_id", sa.Integer(), nullable=True),
            sa.Column(
                "source", sa.String(length=32),
                server_default=sa.text("'meta'"), nullable=False,
            ),
            sa.Column("raw_id", bigint_type, nullable=True),
            sa.UniqueConstraint(
                "wamid", "status",
                name="uq_message_delivery_events_wamid_status",
            ),
        )
    for ix_name, cols in (
        ("ix_message_delivery_events_tenant_occurred",
            ["tenant_id", "occurred_at"]),
        ("ix_message_delivery_events_phone_occurred",
            ["tenant_id", "phone_e164", "occurred_at"]),
        ("ix_message_delivery_events_quality_tier",
            ["tenant_id", "quality_tier", "occurred_at"]),
        ("ix_message_delivery_events_error_code",
            ["tenant_id", "error_code", "occurred_at"]),
    ):
        if not _has_index(bind, "message_delivery_events", ix_name):
            op.create_index(ix_name, "message_delivery_events", cols)

    # ── 3. customer_suppressions ───────────────────────────────────
    if not _has_table(bind, "customer_suppressions"):
        op.create_table(
            "customer_suppressions",
            sa.Column("id", sa.Integer(), primary_key=True),
            sa.Column(
                "tenant_id", sa.Integer(),
                sa.ForeignKey("tenants.id"), nullable=False,
            ),
            sa.Column(
                "customer_id", sa.Integer(),
                sa.ForeignKey("customers.id"), nullable=True,
            ),
            sa.Column("normalized_phone", sa.String(length=32), nullable=False),
            sa.Column("reason_primary", sa.String(length=64), nullable=False),
            sa.Column("reasons", jsonb_type, nullable=True),
            sa.Column(
                "failure_count", sa.Integer(),
                server_default=sa.text("0"), nullable=False,
            ),
            sa.Column(
                "source", sa.String(length=32),
                server_default=sa.text("'auto'"), nullable=False,
            ),
            sa.Column(
                "is_active", sa.Boolean(),
                server_default=sa.text("true"), nullable=False,
            ),
            sa.Column(
                "suppressed_at",
                sa.DateTime(timezone=True),
                server_default=sa.text("CURRENT_TIMESTAMP"),
                nullable=False,
            ),
            sa.Column("last_failure_at", sa.DateTime(timezone=True), nullable=True),
            sa.Column("reinstated_at", sa.DateTime(timezone=True), nullable=True),
            sa.Column("reinstate_reason", sa.String(length=64), nullable=True),
            sa.Column("expires_at", sa.DateTime(timezone=True), nullable=True),
            sa.Column("extra_metadata", jsonb_type, nullable=True),
            sa.UniqueConstraint(
                "tenant_id", "normalized_phone",
                name="uq_customer_suppressions_tenant_phone",
            ),
        )
    for ix_name, cols in (
        ("ix_customer_suppressions_tenant_active",
            ["tenant_id", "is_active"]),
        ("ix_customer_suppressions_last_failure",
            ["tenant_id", "last_failure_at"]),
    ):
        if not _has_index(bind, "customer_suppressions", ix_name):
            op.create_index(ix_name, "customer_suppressions", cols)

    # ── 4. wa_number_quality_snapshots ─────────────────────────────
    if not _has_table(bind, "wa_number_quality_snapshots"):
        op.create_table(
            "wa_number_quality_snapshots",
            sa.Column("id", bigint_type, primary_key=True),
            sa.Column(
                "tenant_id", sa.Integer(),
                sa.ForeignKey("tenants.id"), nullable=False,
            ),
            sa.Column("connection_id", sa.Integer(), nullable=False),
            sa.Column(
                "taken_at",
                sa.DateTime(timezone=True),
                server_default=sa.text("CURRENT_TIMESTAMP"),
                nullable=False,
            ),
            sa.Column("meta_quality_rating", sa.String(length=16), nullable=True),
            sa.Column("meta_messaging_limit", sa.String(length=32), nullable=True),
            sa.Column("nahla_quality_score", sa.Float(), nullable=True),
            sa.Column("nahla_quality_tier", sa.String(length=16), nullable=True),
            sa.Column(
                "metrics_window_hours", sa.Integer(),
                server_default=sa.text("168"), nullable=False,
            ),
            sa.Column("delivery_rate", sa.Float(), nullable=True),
            sa.Column("read_rate", sa.Float(), nullable=True),
            sa.Column("failure_rate", sa.Float(), nullable=True),
            sa.Column("suppress_rate", sa.Float(), nullable=True),
            sa.Column("complaint_rate", sa.Float(), nullable=True),
            sa.Column("sample_size", sa.Integer(), nullable=True),
            sa.Column("raw_metrics", jsonb_type, nullable=True),
            sa.Column("triggered_by", sa.String(length=64), nullable=True),
        )
    for ix_name, cols in (
        ("ix_wa_quality_snap_connection_taken", ["connection_id", "taken_at"]),
        ("ix_wa_quality_snap_tenant_taken",     ["tenant_id", "taken_at"]),
    ):
        if not _has_index(bind, "wa_number_quality_snapshots", ix_name):
            op.create_index(ix_name, "wa_number_quality_snapshots", cols)


def downgrade() -> None:
    # Drop indexes first, then tables — reverse order of upgrade.
    for ix_name, table in (
        ("ix_wa_quality_snap_tenant_taken",     "wa_number_quality_snapshots"),
        ("ix_wa_quality_snap_connection_taken", "wa_number_quality_snapshots"),
        ("ix_customer_suppressions_last_failure", "customer_suppressions"),
        ("ix_customer_suppressions_tenant_active", "customer_suppressions"),
        ("ix_message_delivery_events_error_code", "message_delivery_events"),
        ("ix_message_delivery_events_quality_tier", "message_delivery_events"),
        ("ix_message_delivery_events_phone_occurred", "message_delivery_events"),
        ("ix_message_delivery_events_tenant_occurred", "message_delivery_events"),
        ("ix_wa_webhook_raw_classified_key", "wa_webhook_raw"),
        ("ix_wa_webhook_raw_wamid",          "wa_webhook_raw"),
        ("ix_wa_webhook_raw_tenant_received","wa_webhook_raw"),
        ("ix_wa_webhook_raw_received_at",    "wa_webhook_raw"),
    ):
        op.drop_index(ix_name, table_name=table)

    op.drop_table("wa_number_quality_snapshots")
    op.drop_table("customer_suppressions")
    op.drop_table("message_delivery_events")
    op.drop_table("wa_webhook_raw")

"""
Multi-tenant customer isolation + source tracking.

Revision ID: 0031
Revises: 0030
Create Date: 2026-04-18

WHY
───
Before this migration customer isolation was purely application-level.
Two problems existed:

1. No DB-enforced uniqueness on (tenant_id, phone) or (tenant_id, salla_id).
   A second code path could have created a duplicate customer row for the
   same phone within the same tenant.

2. Customer source/origin was stored inconsistently in JSONB metadata,
   with no indexed first-class column.  Different code paths used
   'source', 'salla_id', 'external_id' keys interchangeably with no
   schema guarantee.

CHANGES
───────
customers table:

  salla_customer_id  VARCHAR  — promoted from metadata->>'salla_id'.
                                First-class column, indexed.
  acquisition_channel VARCHAR — first channel that created the row:
                                 'salla_sync' | 'whatsapp_inbound' |
                                 'order' | 'manual'
  first_seen_at      TIMESTAMPTZ — when the customer was first created
  last_interaction_at TIMESTAMPTZ — updated on every inbound WA message

Constraints/indexes added:

  ix_customers_tenant_phone_partial
      UNIQUE INDEX ON customers (tenant_id, phone)
      WHERE phone IS NOT NULL AND phone != ''
      — Guarantees one customer per (tenant, normalised phone).
        NULL / empty phones are excluded so rows with no phone can coexist.

  ix_customers_tenant_salla_id
      UNIQUE INDEX ON customers (tenant_id, salla_customer_id)
      WHERE salla_customer_id IS NOT NULL AND salla_customer_id != ''
      — Guarantees one Salla customer per (tenant, salla store customer id).

  ix_customers_tenant_id  (plain B-tree)
      — Fast tenant-scoped list queries.

integrations table (belt-and-suspenders on top of 0017):

  uq_integrations_provider_external_store_id already exists as a standard
  UNIQUE (provider, external_store_id) constraint from migration 0017.
  PostgreSQL UNIQUE constraints treat NULL as not-equal so multiple
  (provider, NULL) rows are allowed — that is the correct behaviour.
  We add an explicit PARTIAL UNIQUE INDEX as documentation / extra safety:

  ix_integrations_provider_store_notnull
      UNIQUE INDEX ON integrations (provider, external_store_id)
      WHERE external_store_id IS NOT NULL
      — Belt-and-suspenders on top of the 0017 constraint; makes the
        intent explicit and is required for the ON CONFLICT DO UPDATE path.

BACKFILL
────────
  salla_customer_id  ← COALESCE(metadata->>'salla_id', metadata->>'external_id')
  acquisition_channel ← COALESCE(metadata->>'source', metadata->>'acquisition_channel')

BACKWARD COMPATIBILITY
──────────────────────
  All new columns are nullable with no NOT NULL enforcement.
  Existing code that writes to metadata JSONB continues to work.
  New code reads salla_customer_id / acquisition_channel first, falls
  back to metadata JSONB for legacy rows.

  The PARTIAL UNIQUE INDEX on (tenant_id, phone) means any INSERT that
  violates it will receive a DB error instead of creating a silent
  duplicate.  Application code must use ON CONFLICT (upsert) patterns —
  CustomerIntelligenceService already does this.
"""
from alembic import op
import sqlalchemy as sa


revision      = "0031"
down_revision = "0030"
branch_labels = None
depends_on    = None


# ─────────────────────────────────────────────────────────────────────────────
# UPGRADE
# ─────────────────────────────────────────────────────────────────────────────

def upgrade() -> None:
    bind = op.get_bind()

    # ── 1. Pre-flight: fail loudly if duplicates exist ──────────────────────
    dup_phone = list(bind.execute(sa.text("""
        SELECT tenant_id, phone, COUNT(*) AS cnt
        FROM customers
        WHERE phone IS NOT NULL AND phone != ''
        GROUP BY tenant_id, phone
        HAVING COUNT(*) > 1
    """)))
    if dup_phone:
        formatted = "; ".join(
            f"tenant={r.tenant_id} phone={r.phone} ({r.cnt} rows)"
            for r in dup_phone
        )
        raise RuntimeError(
            f"BLOCKED: duplicate (tenant_id, phone) rows exist — cannot add unique index. "
            f"Run deduplication first: {formatted}"
        )

    dup_salla = list(bind.execute(sa.text("""
        SELECT tenant_id, metadata->>'salla_id' AS salla_id, COUNT(*) AS cnt
        FROM customers
        WHERE metadata->>'salla_id' IS NOT NULL AND metadata->>'salla_id' != ''
        GROUP BY tenant_id, metadata->>'salla_id'
        HAVING COUNT(*) > 1
    """)))
    if dup_salla:
        formatted = "; ".join(
            f"tenant={r.tenant_id} salla_id={r.salla_id} ({r.cnt} rows)"
            for r in dup_salla
        )
        raise RuntimeError(
            f"BLOCKED: duplicate (tenant_id, salla_id) in JSONB metadata — "
            f"cannot add unique index: {formatted}"
        )

    # ── 2. Add new columns to customers ────────────────────────────────────
    op.add_column("customers", sa.Column("salla_customer_id", sa.String(), nullable=True))
    op.add_column("customers", sa.Column("acquisition_channel", sa.String(), nullable=True))
    op.add_column("customers", sa.Column(
        "first_seen_at",
        sa.DateTime(timezone=True),
        nullable=True,
    ))
    op.add_column("customers", sa.Column(
        "last_interaction_at",
        sa.DateTime(timezone=True),
        nullable=True,
    ))

    # ── 3. Backfill from JSONB metadata ────────────────────────────────────
    op.execute(sa.text("""
        UPDATE customers
        SET salla_customer_id = COALESCE(
            NULLIF(metadata->>'salla_id', ''),
            NULLIF(metadata->>'external_id', '')
        )
        WHERE salla_customer_id IS NULL
    """))

    op.execute(sa.text("""
        UPDATE customers
        SET acquisition_channel = COALESCE(
            NULLIF(metadata->>'source', ''),
            NULLIF(metadata->>'acquisition_channel', '')
        )
        WHERE acquisition_channel IS NULL
    """))

    # ── 4. Indexes on customers ─────────────────────────────────────────────

    # Plain index for fast tenant-scoped list queries
    op.create_index("ix_customers_tenant_id", "customers", ["tenant_id"])

    # Partial unique index: (tenant_id, phone) where phone is populated
    op.execute(sa.text("""
        CREATE UNIQUE INDEX ix_customers_tenant_phone_partial
        ON customers (tenant_id, phone)
        WHERE phone IS NOT NULL AND phone != ''
    """))

    # Partial unique index: (tenant_id, salla_customer_id) where id is populated
    op.execute(sa.text("""
        CREATE UNIQUE INDEX ix_customers_tenant_salla_id
        ON customers (tenant_id, salla_customer_id)
        WHERE salla_customer_id IS NOT NULL AND salla_customer_id != ''
    """))

    # Index for acquisition_channel to allow analytics queries
    op.create_index("ix_customers_acquisition_channel", "customers", ["acquisition_channel"])

    # ── 5. Integrations: partial unique index (belt-and-suspenders) ─────────
    # 0017 already has UNIQUE(provider, external_store_id) — add a partial
    # index explicitly named for use in ON CONFLICT DO UPDATE.
    op.execute(sa.text("""
        CREATE UNIQUE INDEX IF NOT EXISTS ix_integrations_provider_store_notnull
        ON integrations (provider, external_store_id)
        WHERE external_store_id IS NOT NULL
    """))


# ─────────────────────────────────────────────────────────────────────────────
# DOWNGRADE
# ─────────────────────────────────────────────────────────────────────────────

def downgrade() -> None:
    op.execute(sa.text("DROP INDEX IF EXISTS ix_integrations_provider_store_notnull"))
    op.execute(sa.text("DROP INDEX IF EXISTS ix_customers_tenant_salla_id"))
    op.execute(sa.text("DROP INDEX IF EXISTS ix_customers_tenant_phone_partial"))
    op.drop_index("ix_customers_acquisition_channel", table_name="customers")
    op.drop_index("ix_customers_tenant_id", table_name="customers")
    op.drop_column("customers", "last_interaction_at")
    op.drop_column("customers", "first_seen_at")
    op.drop_column("customers", "acquisition_channel")
    op.drop_column("customers", "salla_customer_id")

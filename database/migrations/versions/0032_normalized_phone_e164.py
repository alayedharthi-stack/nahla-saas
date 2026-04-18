"""
Add normalized_phone (E.164) to customers; replace phone unique index.

Revision ID: 0032
Revises: 0031
Create Date: 2026-04-18

WHY
───
Migration 0031 added UNIQUE(tenant_id, phone) — but raw `phone` values
arrive in inconsistent formats:
  - 0570000000
  - 966570000000
  - +966570000000
  - 00966570000000

Any two of these can bypass the old unique constraint because they are
lexically different strings.  This migration introduces `normalized_phone`
(E.164 format: +[country_code][local_number]) as the canonical identity key,
replacing the raw-phone unique constraint.

CHANGES
───────
customers table:
  + normalized_phone  VARCHAR  — E.164 (e.g. '+966570000000')

Indexes:
  DROP   ix_customers_tenant_phone_partial   (raw phone, from 0031)
  CREATE ix_customers_tenant_normalized_phone
           UNIQUE ON (tenant_id, normalized_phone)
           WHERE normalized_phone IS NOT NULL AND normalized_phone != ''

  ix_customers_tenant_id is kept (plain, already exists from 0031).

BACKFILL (SQL-level E.164 normalization)
────────────────────────────────────────
Covers the most common patterns without requiring Python:

  Already E.164:   +966XXXXXXX  → kept as-is
  00-prefixed:     00966XXX     → +966XXX
  966-prefixed:    966XXXXXXXXX → +966XXXXXXXXX (12 digits total)
  Saudi local 05:  05XXXXXXXX   → +9665XXXXXXXX (10 digits, starts 05)
  Saudi local 5:   5XXXXXXXX    → +9665XXXXXXXX (9 digits, starts 5)
  Other with +:    +XXXXXXXXXX  → kept as-is (international)
  Anything else:   stored as-is (app will re-normalize on next write)

BACKWARD COMPATIBILITY
──────────────────────
  `phone` column is retained as the raw display value.
  All lookups must use `normalized_phone` going forward.
  Legacy rows without normalized_phone are repaired on first write.
"""
from alembic import op
import sqlalchemy as sa


revision      = "0032"
down_revision = "0031"
branch_labels = None
depends_on    = None

# ── SQL normalization expression (same logic as phone_utils.normalize_to_e164) ──
# Applied during backfill.  More exotic formats will be fixed on next app write.
_NORM_SQL = """
    CASE
        -- Already E.164
        WHEN phone ~ '^\\+[1-9][0-9]{6,14}$'
            THEN phone
        -- 00xxx → +xxx
        WHEN phone ~ '^00[1-9][0-9]{6,14}$'
            THEN '+' || substring(phone FROM 3)
        -- 966XXXXXXXXX (Saudi without +)
        WHEN phone ~ '^966[5][0-9]{8}$'
            THEN '+' || phone
        -- 05XXXXXXXX (Saudi local, 10 digits)
        WHEN phone ~ '^05[0-9]{8}$'
            THEN '+966' || substring(phone FROM 2)
        -- 5XXXXXXXX (Saudi mobile, 9 digits)
        WHEN phone ~ '^5[0-9]{8}$'
            THEN '+966' || phone
        -- International with +, keep as-is
        WHEN phone ~ '^\\+[1-9][0-9]+$' AND length(phone) BETWEEN 8 AND 16
            THEN phone
        -- Unknown — leave NULL; app will repair on next write
        ELSE NULL
    END
"""


def upgrade() -> None:
    bind = op.get_bind()

    # ── 1. Pre-flight: detect normalization conflicts ─────────────────────────
    # Check whether two rows in the same tenant would normalize to the same E.164.
    dup_check = list(bind.execute(sa.text(f"""
        SELECT tenant_id,
               ({_NORM_SQL}) AS norm,
               COUNT(*) AS cnt,
               array_agg(id ORDER BY id) AS ids
        FROM customers
        WHERE phone IS NOT NULL
          AND ({_NORM_SQL}) IS NOT NULL
        GROUP BY tenant_id, ({_NORM_SQL})
        HAVING COUNT(*) > 1
    """)))

    if dup_check:
        details = "; ".join(
            f"tenant={r.tenant_id} norm={r.norm} ids={list(r.ids)}"
            for r in dup_check
        )
        raise RuntimeError(
            "BLOCKED: normalization would create duplicate (tenant_id, normalized_phone) "
            f"rows. Deduplicate first: {details}"
        )

    # ── 2. Add the new column ────────────────────────────────────────────────
    op.add_column(
        "customers",
        sa.Column("normalized_phone", sa.String(), nullable=True),
    )

    # ── 3. Backfill E.164 values ─────────────────────────────────────────────
    op.execute(sa.text(f"""
        UPDATE customers
        SET normalized_phone = ({_NORM_SQL})
        WHERE phone IS NOT NULL
          AND normalized_phone IS NULL
    """))

    # ── 4. Create the new UNIQUE partial index on normalized_phone ───────────
    op.execute(sa.text("""
        CREATE UNIQUE INDEX ix_customers_tenant_normalized_phone
        ON customers (tenant_id, normalized_phone)
        WHERE normalized_phone IS NOT NULL AND normalized_phone != ''
    """))

    # Plain index for fast value lookups
    op.create_index(
        "ix_customers_normalized_phone",
        "customers",
        ["normalized_phone"],
    )

    # ── 5. Drop the old raw-phone unique index (replaced by normalized_phone) ─
    op.execute(sa.text(
        "DROP INDEX IF EXISTS ix_customers_tenant_phone_partial"
    ))


def downgrade() -> None:
    op.execute(sa.text("DROP INDEX IF EXISTS ix_customers_tenant_normalized_phone"))
    op.drop_index("ix_customers_normalized_phone", table_name="customers")
    op.drop_column("customers", "normalized_phone")

    # Restore old raw-phone partial unique index
    op.execute(sa.text("""
        CREATE UNIQUE INDEX ix_customers_tenant_phone_partial
        ON customers (tenant_id, phone)
        WHERE phone IS NOT NULL AND phone != ''
    """))

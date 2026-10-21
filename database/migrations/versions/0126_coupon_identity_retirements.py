"""Persistent coupon quarantine; survives application rollback. No data repair.

Revision ID: 0126
Revises: 0124
"""
from alembic import op
import sqlalchemy as sa

revision = "0126"
down_revision = "0124"
branch_labels = None
depends_on = None

GUARD_SQL = r"""
CREATE FUNCTION guard_coupon_retirement() RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
  IF TG_OP <> 'INSERT' AND EXISTS (
    SELECT 1 FROM coupon_identity_retirements WHERE retired_id = OLD.id
  ) THEN
    RAISE EXCEPTION 'coupon_retired' USING ERRCODE = '23514';
  END IF;
  IF TG_OP = 'DELETE' THEN RETURN OLD; END IF;
  IF EXISTS (
    SELECT 1 FROM coupon_identity_retirements r
    WHERE r.tenant_id = NEW.tenant_id AND r.canonical_id <> NEW.id
    AND (translate(NEW.code, 'ABCDEFGHIJKLMNOPQRSTUVWXYZ', 'abcdefghijklmnopqrstuvwxyz') = r.code_key
      OR ((NEW.metadata->>'salla_coupon_id' = r.provider_id OR NEW.metadata->>'external_id' = r.provider_id)
        AND NOT EXISTS (
          SELECT 1 FROM integrations i WHERE i.tenant_id = NEW.tenant_id
            AND i.id::text = NEW.metadata->'salla_identity'->>'integration_id'
            AND NEW.metadata->'salla_identity'->>'tenant_id' = NEW.tenant_id::text
            AND i.provider = 'salla' AND i.enabled
            AND coalesce(nullif(i.external_store_id,''), i.config->>'store_id', i.config->>'merchant_id')
                = NEW.metadata->'salla_identity'->>'store_id'
            AND NEW.metadata->'salla_identity'->>'store_id' <> r.store_id
        )))
  ) THEN
    RAISE EXCEPTION 'coupon_retired_identity_reserved' USING ERRCODE = '23514';
  END IF;
  RETURN NEW;
END $$;
CREATE TRIGGER coupon_retirement_guard BEFORE INSERT OR UPDATE OR DELETE ON coupons
FOR EACH ROW EXECUTE FUNCTION guard_coupon_retirement();
CREATE FUNCTION guard_coupon_retirement_audit() RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
  RAISE EXCEPTION 'coupon_retirement_audit_immutable' USING ERRCODE = '23514';
END $$;
CREATE TRIGGER coupon_retirement_audit_guard BEFORE UPDATE OR DELETE ON coupon_identity_retirements
FOR EACH ROW EXECUTE FUNCTION guard_coupon_retirement_audit();
"""


def upgrade():
    op.create_table(
        "coupon_identity_retirements",
        sa.Column("retired_id", sa.Integer(), sa.ForeignKey("coupons.id"), primary_key=True),
        sa.Column("canonical_id", sa.Integer(), sa.ForeignKey("coupons.id"), nullable=False),
        sa.Column("tenant_id", sa.Integer(), sa.ForeignKey("tenants.id"), nullable=False),
        sa.Column("integration_id", sa.Integer(), sa.ForeignKey("integrations.id"), nullable=False),
        sa.Column("store_id", sa.Text(), nullable=False),
        sa.Column("provider_id", sa.Text(), nullable=False),
        sa.Column("code_key", sa.Text(), nullable=False),
        sa.Column("audit", sa.JSON(), nullable=False),
        sa.CheckConstraint("retired_id <> canonical_id", name="ck_coupon_retirement_distinct"),
    )
    op.create_index("ix_coupon_retirement_identity", "coupon_identity_retirements", ["tenant_id", "provider_id"])
    op.execute(GUARD_SQL)


def downgrade():
    # An older application must retain the quarantine. Never silently unfreeze it.
    if op.get_bind().execute(sa.text("SELECT 1 FROM coupon_identity_retirements LIMIT 1")).first():
        raise RuntimeError("coupon_retirements_require_persistent_rollback_fence")
    op.execute("DROP TRIGGER coupon_retirement_guard ON coupons")
    op.execute("DROP FUNCTION guard_coupon_retirement()")
    op.execute("DROP TRIGGER coupon_retirement_audit_guard ON coupon_identity_retirements")
    op.execute("DROP FUNCTION guard_coupon_retirement_audit()")
    op.drop_table("coupon_identity_retirements")

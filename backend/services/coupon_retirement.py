"""Explicit, reviewed reconciliation primitive. No endpoint, CLI or auto-repair.

Caller owns maintenance fencing, approval, current provider GET and transaction.
The immutable DB audit is both the before-image and the rollback write fence.
"""
from copy import deepcopy
from datetime import datetime, timezone
from hashlib import sha256
import json

from sqlalchemy import text

from services.coupon_identity import (CouponIdentityConflict, code_key, provider_id,
    binding_matches, same_code, validate_import_binding)


def is_retired(meta):
    # Any marker, including malformed/unknown versions, fails closed.
    return "salla_retirement" in (meta or {})


def assert_operational(coupon):
    if is_retired(coupon.extra_metadata):
        raise CouponIdentityConflict("coupon_retired")


def snapshot(row):
    return {column.key: deepcopy(getattr(row, column.key)) for column in row.__mapper__.column_attrs}


def digest(value):
    return sha256(json.dumps(value, sort_keys=True, separators=(",", ":"), default=str).encode()).hexdigest()


def reconcile_duplicate(db, binding, canonical_id, retired_id, raw, expected_digest, inventory):
    """Stage an atomic repair; never commit or call a provider.

    Requires PostgreSQL0126, exact locked before-images, inactive provider truth,
    no contrary pending intent, and a reviewed reference inventory tied to those
    images. The inventory is an external review input, NOT an inferred zero-ref
    claim. Caller must read provider detail under a bounded maintenance fence.
    """
    from models import Coupon, Tenant
    from services.coupon_active_sync import normalized_active
    if db.bind.dialect.name != "postgresql":
        raise CouponIdentityConflict("persistent_quarantine_required")
    validate_import_binding(db, binding)
    db.query(Tenant.id).filter_by(id=binding["tenant_id"]).with_for_update().one()
    rows = (db.query(Coupon).filter(Coupon.tenant_id == binding["tenant_id"],
            Coupon.id.in_([canonical_id, retired_id])).order_by(Coupon.id)
            .populate_existing().with_for_update().all())
    by_id = {r.id: r for r in rows}
    if canonical_id == retired_id or set(by_id) != {canonical_id, retired_id}:
        raise CouponIdentityConflict("repair_row_scope_mismatch")
    original, duplicate = by_id[canonical_id], by_id[retired_id]
    before = {str(r.id): snapshot(r) for r in rows}
    if digest(before) != expected_digest:
        raise CouponIdentityConflict("repair_snapshot_changed")
    if (not inventory or inventory.get("snapshot_digest") != expected_digest
            or inventory.get("unresolved_references") != 0 or not inventory.get("review_reference")
            or inventory.get("canonical_id") != canonical_id
            or inventory.get("retired_id") != retired_id):
        raise CouponIdentityConflict("repair_reference_review_required")
    from services.coupon_active_sync import timestamp
    observed = timestamp(inventory.get("provider_observed_at"))
    age = (datetime.now(timezone.utc) - observed).total_seconds() if observed else None
    if (age is None or not -5 <= age <= 30 or inventory.get("provider_digest") != digest(raw)
            or not inventory.get("maintenance_fence_reference")):
        raise CouponIdentityConflict("repair_fresh_fenced_provider_evidence_required")
    pid = str(raw.get("id") or "")
    if not pid.isdigit() or normalized_active(raw) is not False:
        raise CouponIdentityConflict("repair_provider_must_be_inactive")
    for row in rows:
        assert_operational(row)
        meta = row.extra_metadata or {}
        if (provider_id(meta) != pid or not binding_matches(meta, binding)
                or not same_code(row.code, raw.get("code"))):
            raise CouponIdentityConflict("repair_identity_mismatch")
        pending = meta.get("active_sync_pending") or {}
        if pending and (pending.get("desired") is not False
                or pending.get("integration_id") != binding["integration_id"]
                or str(pending.get("store_id")) != binding["store_id"]):
            raise CouponIdentityConflict("repair_conflicting_intent")
    if original.source_type != "manual" or duplicate.source_type != "imported":
        raise CouponIdentityConflict("repair_authorship_review_required")
    others = db.query(Coupon).filter_by(tenant_id=binding["tenant_id"]).all()
    if any(r.id not in by_id and (same_code(r.code, original.code) or
            (provider_id(r.extra_metadata) == pid and binding_matches(r.extra_metadata, binding))) for r in others):
        raise CouponIdentityConflict("repair_third_identity")
    # Use existing normalization so amount/type/expiry semantics do not diverge.
    from services.store_sync import _normalise_coupon
    normal = _normalise_coupon(raw)
    if normal["discount_type"] != "percentage" or not raw.get("amount"):
        raise CouponIdentityConflict("repair_terms_review_required")
    now = datetime.now(timezone.utc).isoformat()
    meta = deepcopy(original.extra_metadata or {})
    meta.pop("active_sync_pending", None)
    meta.update(active=False, salla_identity=binding, salla_provider_code=raw["code"],
                sync_status="synced", sync_error=None, salla_synced=True,
                active_applied_at=now, active_observed_at=now, last_synced_at=now)
    meta.pop("active_source_updated_at", None)
    if timestamp(raw.get("updated_at")):
        meta["active_source_updated_at"] = timestamp(raw["updated_at"]).isoformat()
    original.extra_metadata = meta
    original.discount_type, original.discount_value = normal["discount_type"], normal["discount_value"]
    # Preserve ID/code/source/rules/references; remove operational IDs, not history.
    old = deepcopy(duplicate.extra_metadata or {})
    old.pop("salla_coupon_id", None)
    old.pop("external_id", None)
    old.pop("active_sync_pending", None)
    old.update(active=False, ai_allocatable=False, salla_synced=False,
               sync_status="retired", salla_retirement={"version": 1,
                   "canonical_id": canonical_id, "binding": binding, "provider_id": pid,
                   "original_code": duplicate.code, "retired_at": now})
    duplicate.extra_metadata = old
    db.flush()  # fence is installed only after both scoped updates succeed
    audit = {"before": before, "before_digest": expected_digest, "inventory": inventory,
             "provider_observation": deepcopy(raw), "recorded_at": now,
             "after": {str(r.id): snapshot(r) for r in rows}}
    db.execute(text("""INSERT INTO coupon_identity_retirements
        (retired_id,canonical_id,tenant_id,integration_id,store_id,provider_id,code_key,audit)
        VALUES (:rid,:cid,:tid,:iid,:store,:pid,:code,CAST(:audit AS JSON))"""),
        dict(rid=retired_id,cid=canonical_id,tid=binding["tenant_id"],iid=binding["integration_id"],
             store=binding["store_id"],pid=pid,code=code_key(original.code),
             audit=json.dumps(audit, default=str)))
    return {"canonical_id": canonical_id, "retired_id": retired_id, "before_digest": expected_digest}

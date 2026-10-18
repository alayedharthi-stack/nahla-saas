"""Salla coupon identity: store-bound provider ID, ASCII-case-only code aliasing.

No automatic row merging: historical ambiguities require an audited repair.
"""
from sqlalchemy import func, or_


class CouponIdentityConflict(ValueError):
    pass


def code_key(value):
    # Do not equate Unicode lookalikes, punctuation, whitespace, or expansions.
    return value.translate(str.maketrans("ABCDEFGHIJKLMNOPQRSTUVWXYZ", "abcdefghijklmnopqrstuvwxyz")) if isinstance(value, str) else ""


def same_code(left, right):
    return bool(isinstance(left, str) and isinstance(right, str) and left and right and code_key(left) == code_key(right))


def provider_id(meta):
    meta = meta or {}
    primary, alternate = meta.get("salla_coupon_id"), meta.get("external_id")
    if primary and alternate and str(primary) != str(alternate):
        raise CouponIdentityConflict("provider_alias_conflict")
    return str(primary or alternate or "")


def adapter_binding(adapter, tenant_id, connection_id=None):
    values = [getattr(adapter, name, None) for name in ("_tenant_id", "_integration_id", "store_id")]
    if all(value is None for value in values):
        return None  # Legacy callers retain exact-code behavior only.
    tid, iid, store = values
    if tid != tenant_id or not isinstance(iid, int) or not store or (connection_id is not None and iid != connection_id):
        raise CouponIdentityConflict("adapter_binding_unavailable")
    return {"tenant_id": tenant_id, "integration_id": iid, "store_id": str(store)}


def binding_matches(meta, binding):
    prior = (meta or {}).get("salla_identity")
    return prior is None or prior == binding


def legacy_scope_is_unique(db, binding):
    from models import Integration
    rows = db.query(Integration).filter_by(tenant_id=binding["tenant_id"], provider="salla", enabled=True).all()
    # An unbound legacy record cannot be adopted across multiple merchant stores.
    stores = {str(row.external_store_id or (row.config or {}).get("store_id") or (row.config or {}).get("merchant_id") or "") for row in rows}
    return stores == {binding["store_id"]} and any(row.id == binding["integration_id"] for row in rows)


def validate_import_binding(db, binding):
    from models import Integration
    row = db.query(Integration).filter_by(id=binding["integration_id"], tenant_id=binding["tenant_id"], provider="salla", enabled=True).first()
    cfg = (row.config or {}) if row else {}
    store = str(row.external_store_id or cfg.get("store_id") or cfg.get("merchant_id") or "") if row else ""
    if store != binding["store_id"] or (cfg.get("store_id") and str(cfg["store_id"]) != store):
        raise CouponIdentityConflict("adapter_binding_unavailable")


def resolve_import_coupon(db, tenant_id, raw, binding):
    from models import Coupon
    incoming_id, code = str(raw.get("id") or raw.get("coupon_id") or raw.get("salla_coupon_id") or ""), raw.get("code")
    if binding is None:
        return db.query(Coupon).filter_by(tenant_id=tenant_id, code=code).populate_existing().with_for_update().first()
    if not incoming_id.isdigit() or not isinstance(code, str) or not code:
        raise CouponIdentityConflict("provider_identity_unavailable")
    candidates = (db.query(Coupon).filter(Coupon.tenant_id == tenant_id).filter(or_(
        Coupon.extra_metadata["salla_coupon_id"].as_string() == incoming_id,
        Coupon.extra_metadata["external_id"].as_string() == incoming_id,
        func.lower(Coupon.code) == code.lower(),
    )).populate_existing().with_for_update().all())
    matched, collisions = [], []
    for row in candidates:
        meta = row.extra_metadata or {}
        if "salla_retirement" in meta:
            continue  # Immutable historical row; never an operational candidate.
        pid = provider_id(meta)
        if not binding_matches(meta, binding):
            if same_code(row.code, code):
                collisions.append(row)
            continue
        if pid == incoming_id:
            if not meta.get("salla_identity") and not legacy_scope_is_unique(db, binding):
                raise CouponIdentityConflict("legacy_store_ambiguous")
            # A genuine provider rename is not a case-only identity transition.
            if not same_code(row.code, code):
                raise CouponIdentityConflict("provider_code_changed")
            matched.append(row)
        elif same_code(row.code, code):
            # Never attach an unbound/local row or a different provider ID by code.
            collisions.append(row)
    if collisions or len(matched) > 1:
        raise CouponIdentityConflict("coupon_identity_ambiguous")
    return matched[0] if matched else None


def assert_dispatch_identity(db, row, binding, incoming_id, code):
    from models import Coupon
    if "salla_retirement" in (row.extra_metadata or {}):
        raise CouponIdentityConflict("coupon_retired")
    if not binding_matches(row.extra_metadata, binding) or provider_id(row.extra_metadata) != incoming_id or not same_code(code, row.code):
        raise CouponIdentityConflict("provider_coupon_identity_mismatch")
    if not (row.extra_metadata or {}).get("salla_identity") and not legacy_scope_is_unique(db, binding):
        raise CouponIdentityConflict("legacy_store_ambiguous")
    duplicates = (db.query(Coupon).filter(Coupon.tenant_id == row.tenant_id, Coupon.id != row.id).filter(or_(
        Coupon.extra_metadata["salla_coupon_id"].as_string() == incoming_id,
        Coupon.extra_metadata["external_id"].as_string() == incoming_id,
    )).all())
    if any("salla_retirement" not in (other.extra_metadata or {}) and binding_matches(other.extra_metadata, binding) for other in duplicates):
        raise CouponIdentityConflict("coupon_identity_ambiguous")

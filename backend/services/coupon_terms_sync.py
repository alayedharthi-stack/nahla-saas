"""Confirm Salla coupon value/expiry edits before changing local terms."""
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation
import re

from sqlalchemy.orm.attributes import flag_modified
from core.salla_order_fidelity import _riyadh_tz

from services.coupon_expiry import provider_expiry_utc_naive
from services.coupon_identity import (
    CouponIdentityConflict, adapter_binding, assert_dispatch_identity,
    provider_id, same_code, validate_import_binding,
)


class CouponTermsError(ValueError):
    pass


def _amount(value, discount_type):
    if not re.fullmatch(r"\d+(?:\.\d+)?", str(value)):
        raise CouponTermsError("invalid_coupon_value")
    try:
        amount = Decimal(str(value))
    except InvalidOperation as exc:
        raise CouponTermsError("invalid_coupon_value") from exc
    if not amount.is_finite() or amount <= 0 or (discount_type == "percentage" and amount > 100):
        raise CouponTermsError("invalid_coupon_value")
    return format(amount, "f")


def _riyadh_day(value):
    if not isinstance(value, str) or not value:
        raise CouponTermsError("coupon_expiry_required")
    if re.fullmatch(r"\d{4}-\d{2}-\d{2}", value):
        try:
            return datetime.fromisoformat(value).date().isoformat()
        except ValueError as exc:
            raise CouponTermsError("invalid_coupon_expiry") from exc
    try:
        instant = datetime.fromisoformat(value.replace("Z", "+00:00"))
        if instant.tzinfo is None:
            raise ValueError("timezone required")
        return instant.astimezone(_riyadh_tz()).date().isoformat()
    except ValueError as exc:
        raise CouponTermsError("invalid_coupon_expiry") from exc


def desired_terms(row, fields):
    if not fields or set(fields) - {"value", "expires"}:
        raise CouponTermsError("invalid_coupon_terms_patch")
    if row.source_type == "system" or "salla_retirement" in (row.extra_metadata or {}):
        raise CouponTermsError("coupon_terms_not_editable")
    result = {}
    if "value" in fields:
        result["amount"] = _amount(fields["value"], row.discount_type)
    if "expires" in fields:
        result["expiry_date"] = _riyadh_day(fields["expires"])
    return result


def bound_integration(db, row):
    from models import Integration

    meta = row.extra_metadata or {}
    binding = meta.get("salla_identity")
    if not isinstance(binding, dict) or binding.get("tenant_id") != row.tenant_id:
        raise CouponTermsError("coupon_identity_unverified")
    integration = db.query(Integration).filter_by(
        id=binding.get("integration_id"), tenant_id=row.tenant_id,
        provider="salla", enabled=True,
    ).first()
    if integration is None:
        raise CouponTermsError("integration_binding_unavailable")
    try:
        validate_import_binding(db, binding)
        pid = provider_id(meta)
    except CouponIdentityConflict as exc:
        raise CouponTermsError(str(exc)) from exc
    if not pid.isdigit() or not (integration.config or {}).get("api_sync_enabled"):
        raise CouponTermsError("integration_binding_unavailable")
    assert_dispatch_identity(db, row, binding, pid, row.code)
    return integration, binding, pid


def _remote_matches(raw, desired):
    if "amount" in desired:
        try:
            amount = Decimal(str(raw.get("amount")))
            if not amount.is_finite() or amount != Decimal(desired["amount"]):
                return False
        except (InvalidOperation, TypeError):
            return False
    if "expiry_date" in desired:
        expiry = raw.get("expiry_date") or raw.get("expire_date") or raw.get("expires_at")
        try:
            stamp = provider_expiry_utc_naive(expiry)
        except ValueError:
            return False
        if stamp is None or stamp.replace(tzinfo=timezone.utc).astimezone(_riyadh_tz()).date().isoformat() != desired["expiry_date"]:
            return False
    return True


def _check_remote(db, row, binding, pid, raw):
    if not isinstance(raw, dict) or str(raw.get("id") or "") != pid or not same_code(raw.get("code"), row.code):
        raise CouponTermsError("provider_coupon_identity_mismatch")
    raw_type = str(raw.get("type") or raw.get("discount_type") or "").lower()
    if raw_type == "amount":
        raw_type = "fixed"
    elif raw_type == "percent":
        raw_type = "percentage"
    if raw_type != str(row.discount_type or "").lower():
        raise CouponTermsError("provider_coupon_identity_mismatch")
    assert_dispatch_identity(db, row, binding, pid, raw.get("code"))


async def apply_bound_terms(db, row, fields, adapter):
    """Durable intent, GET/targeted PUT/GET, then confirmed local commit."""
    integration, binding, pid = bound_integration(db, row)
    desired = desired_terms(row, fields)
    if adapter_binding(adapter, row.tenant_id, integration.id) != binding:
        raise CouponTermsError("adapter_binding_unavailable")
    meta = dict(row.extra_metadata or {})
    if meta.get("active_sync_pending"):
        raise CouponTermsError("coupon_change_already_pending")
    pending = meta.get("terms_sync_pending")
    if pending and (pending.get("desired") != desired or pending.get("provider_id") != pid):
        raise CouponTermsError("coupon_change_already_pending")
    meta["terms_sync_pending"] = {"desired": desired, "provider_id": pid, "binding": binding}
    meta.update(sync_status="pending", salla_synced=False, sync_error=None)
    row.extra_metadata = meta
    flag_modified(row, "extra_metadata")
    db.commit()

    try:
        raw = await adapter.get_coupon_by_id(pid)
        _check_remote(db, row, binding, pid, raw)
        if not _remote_matches(raw, desired):
            await adapter.update_coupon_terms(pid, **desired)
            raw = await adapter.get_coupon_by_id(pid)
        _check_remote(db, row, binding, pid, raw)
        if not _remote_matches(raw, desired):
            raise CouponTermsError("provider_terms_unconfirmed")
        if "amount" in desired:
            row.discount_value = format(Decimal(str(raw["amount"])), "f")
        if "expiry_date" in desired:
            stamp = provider_expiry_utc_naive(raw.get("expiry_date") or raw.get("expire_date") or raw.get("expires_at"))
            if stamp is None:
                raise CouponTermsError("provider_terms_unconfirmed")
            row.expires_at = stamp
        meta = dict(row.extra_metadata or {})
        meta.pop("terms_sync_pending", None)
        meta.update(sync_status="synced", salla_synced=True, sync_error=None,
                    terms_confirmed_at=datetime.now(timezone.utc).isoformat())
        row.extra_metadata = meta
        flag_modified(row, "extra_metadata")
        db.commit()
        return True, meta
    except Exception as exc:
        reason = str(exc) if isinstance(exc, (CouponTermsError, CouponIdentityConflict)) else type(exc).__name__
        meta = dict(row.extra_metadata or {})
        meta.update(sync_status="failed", salla_synced=False, sync_error=reason)
        row.extra_metadata = meta
        flag_modified(row, "extra_metadata")
        db.commit()
        return False, meta

"""Durable, tenant-bound Salla coupon value/expiry sync (no type/code/active/rules changes)."""
from __future__ import annotations

import re
from datetime import datetime, date, timedelta, timezone
from decimal import Decimal, InvalidOperation
from time import monotonic
from uuid import uuid4
import logging
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from sqlalchemy.orm.attributes import flag_modified
from services.coupon_identity import same_code, assert_dispatch_identity, provider_id as _provider_id, CouponIdentityConflict
from services.coupon_active_sync import utcnow, timestamp, normalized_active

logger = logging.getLogger(__name__)

try:
    _RIYADH = ZoneInfo("Asia/Riyadh")
except Exception:
    # Fallback: fixed UTC+3 offset (no new package required)
    _RIYADH = timezone(timedelta(hours=3))


class CouponTermsError(ValueError):
    pass


# ---------------------------------------------------------------------------
# Public expiry helper (used by importer via same module)
# ---------------------------------------------------------------------------

def provider_expiry_utc(raw_expiry):
    """Convert a provider coupon expiry value to an aware UTC datetime.

    Flat naive date/datetime strings are treated as Asia/Riyadh (documented
    coupon-specific assumption; not a global Salla rule).  Explicit
    offset/timezone is honored.  Unknown forms or unrecognised zones return None.

    Returns an aware UTC datetime, or None on failure.
    """
    if raw_expiry is None:
        return None
    if isinstance(raw_expiry, datetime):
        if raw_expiry.tzinfo is not None:
            return raw_expiry.astimezone(timezone.utc)
        return raw_expiry.replace(tzinfo=_RIYADH).astimezone(timezone.utc)
    if isinstance(raw_expiry, date):
        dt = datetime(raw_expiry.year, raw_expiry.month, raw_expiry.day, tzinfo=_RIYADH)
        return dt.astimezone(timezone.utc)
    if isinstance(raw_expiry, dict):
        zone_str = raw_expiry.get("timezone")
        inner = raw_expiry.get("date") or raw_expiry.get("datetime")
        if not inner:
            return None
        if zone_str:
            inner_s = str(inner).strip()
            # If inner already carries an explicit offset, verify it agrees with zone_str.
            try:
                dt = datetime.fromisoformat(inner_s.replace("Z", "+00:00"))
                if dt.tzinfo is not None:
                    try:
                        tz = _RIYADH if zone_str == "Asia/Riyadh" else ZoneInfo(zone_str)
                        if tz.utcoffset(dt.replace(tzinfo=None)) != dt.utcoffset():
                            return None  # conflicting zones
                    except (ZoneInfoNotFoundError, KeyError):
                        return None  # unknown zone
                    return dt.astimezone(timezone.utc)
            except (ValueError, TypeError):
                pass
            # Naive inner: apply the dict-supplied explicit zone.
            if zone_str == "Asia/Riyadh":
                tz = _RIYADH
            else:
                try:
                    tz = ZoneInfo(zone_str)
                except (ZoneInfoNotFoundError, KeyError):
                    return None  # unknown zone
            for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%d %H:%M:%S.%f", "%Y-%m-%d"):
                try:
                    d = datetime.strptime(inner_s, fmt)
                    return d.replace(tzinfo=tz).astimezone(timezone.utc)
                except ValueError:
                    pass
            return None
        return provider_expiry_utc(inner)
    s = str(raw_expiry).strip()
    if not s:
        return None
    # Plain date YYYY-MM-DD
    try:
        d = datetime.strptime(s, "%Y-%m-%d")
        return d.replace(tzinfo=_RIYADH).astimezone(timezone.utc)
    except ValueError:
        pass
    # Naive datetime (common provider format '2026-10-31 00:00:00')
    for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%d %H:%M:%S.%f"):
        try:
            d = datetime.strptime(s, fmt)
            return d.replace(tzinfo=_RIYADH).astimezone(timezone.utc)
        except ValueError:
            pass
    # ISO with explicit timezone
    try:
        dt = datetime.fromisoformat(s.replace("Z", "+00:00"))
        if dt.tzinfo is None:
            # Naive ISO: treat as Riyadh
            return dt.replace(tzinfo=_RIYADH).astimezone(timezone.utc)
        return dt.astimezone(timezone.utc)
    except (ValueError, TypeError):
        pass
    return None


def _provider_expiry_riyadh_date(raw_expiry):
    """Return YYYY-MM-DD in Riyadh calendar for a provider expiry, or None."""
    utc = provider_expiry_utc(raw_expiry)
    if utc is None:
        return None
    return utc.astimezone(_RIYADH).strftime("%Y-%m-%d")


# ---------------------------------------------------------------------------
# Validation helpers
# ---------------------------------------------------------------------------

_PLAIN_DECIMAL_RE = re.compile(r'^[+-]?(\d+\.?\d*|\.\d+)$')
_STRICT_DATE_RE = re.compile(r'^\d{4}-\d{2}-\d{2}$')


def _parse_value(raw_value, discount_type):
    """Return canonical Decimal string; raise CouponTermsError on any fault."""
    s = str(raw_value).strip()
    if not _PLAIN_DECIMAL_RE.match(s):
        raise CouponTermsError("invalid_value")
    try:
        d = Decimal(s)
    except (InvalidOperation, TypeError):
        raise CouponTermsError("invalid_value")
    if not d.is_finite():
        raise CouponTermsError("invalid_value")
    if d <= 0:
        raise CouponTermsError("invalid_value")
    if discount_type == "percentage" and d > 100:
        raise CouponTermsError("invalid_value_percentage_exceeds_100")
    # Preserve exact precision supplied; never scientific notation.
    return format(d, "f")


def _parse_expiry_user(raw_expiry, allow_clear=False):
    """Validate a user-supplied expiry date.

    Accepts strict real YYYY-MM-DD or explicit-timezone ISO datetime (-> Riyadh day).
    Returns YYYY-MM-DD string, or None when allow_clear and raw is empty.
    """
    if raw_expiry is None or raw_expiry == "":
        if allow_clear:
            return None
        raise CouponTermsError("cannot_clear_expiry_on_bound_coupon")
    if isinstance(raw_expiry, date) and not isinstance(raw_expiry, datetime):
        return raw_expiry.strftime("%Y-%m-%d")
    s = str(raw_expiry).strip()
    # Strict real YYYY-MM-DD — require zero-padded month/day before strptime.
    if _STRICT_DATE_RE.match(s):
        try:
            return datetime.strptime(s, "%Y-%m-%d").strftime("%Y-%m-%d")
        except ValueError:
            raise CouponTermsError("invalid_expiry")
    # ISO datetime with explicit timezone -> Riyadh calendar day
    try:
        dt = datetime.fromisoformat(s.replace("Z", "+00:00"))
        if dt.tzinfo is None:
            raise CouponTermsError("expiry_datetime_missing_timezone")
        return dt.astimezone(_RIYADH).strftime("%Y-%m-%d")
    except (ValueError, TypeError):
        raise CouponTermsError("invalid_expiry")


_ALLOWED_PATCH_KEYS = frozenset({"value", "expires"})
_SUPPORTED_DISCOUNT_TYPES = frozenset({"percentage", "fixed"})


def validate_terms(row, patch):
    """Validate ``value`` and/or ``expires`` from *patch*.

    Input patch keys: ``value`` (amount) and/or ``expires`` (expiry date).
    Output keys in returned dict: ``amount`` (exact Decimal string) and/or
    ``expiry_date`` (YYYY-MM-DD).  Raises CouponTermsError on any fault.
    """
    unknown = set(patch) - _ALLOWED_PATCH_KEYS
    if unknown:
        raise CouponTermsError("unknown_patch_keys")

    meta = row.extra_metadata or {}
    if "salla_retirement" in meta or meta.get("is_system_coupon"):
        raise CouponTermsError("retired_or_system_coupon")

    # Raw column source_type: reject 'system' unconditionally before display classifier
    if getattr(row, "source_type", None) == "system":
        raise CouponTermsError("retired_or_system_coupon")

    # source_type eligibility: only manual/imported coupons
    from services.coupon_sync_visibility import resolve_coupon_source_type as _rcs
    source_type = _rcs(
        column_source_type=getattr(row, "source_type", None),
        meta=meta,
        origin=str(meta.get("source") or ""),
    )
    if source_type == "system":
        raise CouponTermsError("retired_or_system_coupon")

    # Raw metadata automation cues: only manual/imported coupons allowed
    _auto_src = str(meta.get("source") or "").lower()
    if (
        _auto_src in {"source_auto", "auto", "pool", "automation", "system", "promotion", "widget", "auto_generated"}
        or meta.get("auto_generated") is True
        or meta.get("vip")
        or meta.get("category_vip")
        or str(meta.get("category") or "").lower() == "vip"
    ):
        raise CouponTermsError("retired_or_system_coupon")

    discount_type = (getattr(row, "discount_type", None) or "").lower()
    is_bound = bool(meta.get("salla_coupon_id") or meta.get("external_id") or meta.get("salla_identity"))
    result = {}

    if "value" in patch:
        result["amount"] = _parse_value(patch["value"], discount_type)

    if "expires" in patch:
        allow_clear = not is_bound
        result["expiry_date"] = _parse_expiry_user(patch["expires"], allow_clear=allow_clear)

    if not result:
        raise CouponTermsError("empty_terms_update")

    return result


# ---------------------------------------------------------------------------
# Integration resolution
# ---------------------------------------------------------------------------

def resolve_terms_integration(db, row):
    """Return the single enabled full-API Salla integration for this row, or None."""
    from models import Integration
    meta = row.extra_metadata or {}
    identity = meta.get("salla_identity")
    if identity:
        iid = identity.get("integration_id")
        ig = db.query(Integration).filter_by(
            id=iid, tenant_id=row.tenant_id, provider="salla", enabled=True
        ).first()
        if ig and not (ig.config or {}).get("api_sync_enabled"):
            return None
        return ig
    # Legacy unbound path: unique enabled full-API integration for this tenant.
    rows = db.query(Integration).filter_by(
        tenant_id=row.tenant_id, provider="salla", enabled=True
    ).all()
    eligible = [r for r in rows if (r.config or {}).get("api_sync_enabled")]
    return eligible[0] if len(eligible) == 1 else None


# ---------------------------------------------------------------------------
# Queue
# ---------------------------------------------------------------------------

def queue_terms_change(row, integration, desired):
    """Attach a pending terms intent to *row*.  Caller must commit.

    *desired* is a dict with canonical ``amount`` and/or ``expiry_date`` keys
    already validated by :func:`validate_terms`.
    """
    meta = dict(row.extra_metadata or {})
    if "salla_retirement" in meta or meta.get("is_system_coupon"):
        raise CouponTermsError("retired_or_system_coupon")

    # Reject integration/tenant drift before any mutation
    if str(integration.tenant_id) != str(row.tenant_id):
        raise CouponTermsError("integration_binding_unavailable")

    # Full tuple identity guard: if salla_identity is recorded, all three fields must match exactly
    _identity = meta.get("salla_identity")
    if _identity:
        _cfg = integration.config or {}
        _int_store_id = str(
            integration.external_store_id
            or _cfg.get("store_id")
            or _cfg.get("merchant_id")
            or ""
        )
        if (
            str(_identity.get("tenant_id") or "") != str(row.tenant_id)
            or str(_identity.get("integration_id") or "") != str(integration.id)
            or str(_identity.get("store_id") or "") != _int_store_id
        ):
            raise CouponTermsError("integration_binding_unavailable")

    # Validate desired keys (amount/expiry_date only) and re-canonicalize via validate_terms
    _allowed_desired = frozenset({"amount", "expiry_date"})
    if set(desired) - _allowed_desired:
        raise CouponTermsError("unknown_patch_keys")
    _patch = {}
    if "amount" in desired:
        _patch["value"] = desired["amount"]
    if "expiry_date" in desired:
        _patch["expires"] = desired["expiry_date"]
    desired = validate_terms(row, _patch)

    # Reject if an active sync is already in flight globally
    if meta.get("active_sync_pending") or meta.get("sync_pending"):
        raise CouponTermsError("terms_change_already_pending")

    cfg = integration.config or {}
    store_id = str(
        integration.external_store_id
        or cfg.get("store_id")
        or cfg.get("merchant_id")
        or ""
    )

    try:
        current_pid = str(_provider_id(meta) or "")
    except CouponIdentityConflict as exc:
        raise CouponTermsError("provider_alias_conflict") from exc
    pending = meta.get("terms_sync_pending") or {}
    if pending:
        existing_desired = pending.get("desired") or {}
        existing_iid = pending.get("integration_id")
        if (
            existing_desired == desired
            and existing_iid == integration.id
            and pending.get("store_id") == store_id
            and pending.get("provider_id", current_pid) == current_pid
            and pending.get("tenant_id") == str(row.tenant_id)
        ):
            # Identical intent already queued: NOOP, preserve operation_id.
            return
        # Different pending intent: explicit conflict.
        raise CouponTermsError("terms_change_already_pending")

    meta["terms_sync_pending"] = {
        "operation_id": uuid4().hex,
        "desired": desired,
        "integration_id": integration.id,
        "tenant_id": str(row.tenant_id),
        "store_id": store_id,
        "provider_id": current_pid,
        "requested_at": utcnow().isoformat(),
        "attempts": 0,
    }
    meta["terms_sync_status"] = "pending"
    meta["terms_sync_error"] = None
    # Global status must also reflect pending so salla_synced is never stale-true.
    meta["sync_status"] = "pending"
    meta["salla_synced"] = False
    meta["sync_error"] = None
    row.extra_metadata = meta
    flag_modified(row, "extra_metadata")


# ---------------------------------------------------------------------------
# Dispatch
# ---------------------------------------------------------------------------

def _assert_integration_ok(integration, tenant_id, pending):
    cfg = integration.config or {}
    store_id = str(
        integration.external_store_id
        or cfg.get("store_id")
        or cfg.get("merchant_id")
        or ""
    )
    if (
        integration.tenant_id != tenant_id
        or integration.id != pending["integration_id"]
        or store_id != pending["store_id"]
        or not store_id
        or integration.provider != "salla"
        or not integration.enabled
        or (cfg.get("store_id") and str(cfg["store_id"]) != store_id)
        or cfg.get("needs_reauth")
        or not cfg.get("api_sync_enabled")
        or not cfg.get("refresh_token")
    ):
        raise CouponTermsError("integration_binding_unavailable")
    return store_id


def _assert_adapter_ok(adapter, tenant_id, integration, store_id):
    cfg = integration.config or {}
    if (
        getattr(adapter, "_tenant_id", None) != tenant_id
        or getattr(adapter, "_integration_id", None) != integration.id
        or str(getattr(adapter, "store_id", "")) != str(cfg.get("store_id") or store_id)
    ):
        raise CouponTermsError("adapter_tenant_mismatch")


def _raw_to_terms(raw):
    """Extract amount/expiry from a provider raw coupon dict."""
    amount = raw.get("amount") or raw.get("value")
    if isinstance(amount, dict):
        amount = amount.get("amount") or amount.get("value")
    expiry = raw.get("expiry_date") or raw.get("expires_at") or raw.get("expire_date")
    return amount, expiry


def _terms_match(raw, desired):
    """Check whether provider raw already reflects *desired* (amount/expiry_date)."""
    amount, expiry = _raw_to_terms(raw)
    if "amount" in desired:
        try:
            if Decimal(str(amount)) != Decimal(desired["amount"]):
                return False
        except (InvalidOperation, TypeError):
            return False
    if "expiry_date" in desired:
        prov_date = _provider_expiry_riyadh_date(expiry)
        if prov_date != desired.get("expiry_date"):
            return False
    return True


_SAFE_REASONS = frozenset({
    "integration_binding_unavailable",
    "adapter_tenant_mismatch",
    "provider_coupon_identity_mismatch",
    "provider_terms_unconfirmed",
    "legacy_store_ambiguous",
    "coupon_identity_ambiguous",
    "provider_alias_conflict",
    "retired_or_system_coupon",
    "terms_change_already_pending",
})


async def dispatch_terms_change(db, tenant_id, coupon_id, integration, adapter):
    """Acquire row-lock, issue GET-before-PUT, confirm with GET-after-PUT."""
    from models import Coupon
    row = (
        db.query(Coupon)
        .filter_by(tenant_id=tenant_id, id=coupon_id)
        .populate_existing()
        .with_for_update(skip_locked=True)
        .first()
    )
    if row is None:
        return
    meta = dict(row.extra_metadata or {})
    if "salla_retirement" in meta or meta.get("is_system_coupon"):
        return
    from services.coupon_sync_visibility import resolve_coupon_source_type as _rcs
    if _rcs(
        column_source_type=getattr(row, "source_type", None),
        meta=meta,
        origin=str(meta.get("source") or ""),
    ) == "system":
        return
    pending = dict(meta.get("terms_sync_pending") or {})
    if not pending:
        return
    next_at = timestamp(pending.get("next_attempt_at"))
    if next_at and utcnow() < next_at:
        return

    started = monotonic()
    try:
        provider_id_val = str(_provider_id(meta) or "")
        if pending.get("tenant_id") and str(pending["tenant_id"]) != str(row.tenant_id):
            raise CouponTermsError("integration_binding_unavailable")
        if meta.get("active_sync_pending"):
            raise CouponTermsError("terms_change_already_pending")
        queued_pid = pending.get("provider_id")
        if queued_pid is not None and provider_id_val != queued_pid:
            raise CouponTermsError("integration_binding_unavailable")
        store_id = _assert_integration_ok(integration, tenant_id, pending)
        _assert_adapter_ok(adapter, tenant_id, integration, store_id)
        if not provider_id_val.isdigit():
            raise CouponTermsError("integration_binding_unavailable")

        # Validate DB import binding before any provider call
        from services.coupon_identity import validate_import_binding
        validate_import_binding(
            db,
            {"tenant_id": tenant_id, "integration_id": integration.id, "store_id": store_id},
        )

        binding = {"tenant_id": tenant_id, "integration_id": integration.id, "store_id": store_id}
        assert_dispatch_identity(db, row, binding, provider_id_val, row.code)

        # Validate desired via validate_terms before any network call
        _d = pending.get("desired") or {}
        _unknown_desired = set(_d) - {"amount", "expiry_date"}
        if _unknown_desired:
            raise CouponTermsError("unknown_patch_keys")
        _dp = {}
        if "amount" in _d:
            _dp["value"] = _d["amount"]
        if "expiry_date" in _d:
            _dp["expires"] = _d["expiry_date"]
        desired = validate_terms(row, _dp)
        pending["desired"] = desired

        raw = await adapter.get_coupon_by_id(provider_id_val)
        if str(raw.get("id") or raw.get("coupon_id") or "") != provider_id_val:
            raise CouponTermsError("provider_coupon_identity_mismatch")

        binding = {"tenant_id": tenant_id, "integration_id": integration.id, "store_id": store_id}
        assert_dispatch_identity(db, row, binding, provider_id_val, raw.get("code"))

        # Check type compatibility BEFORE any write; missing or unsupported type rejects.
        prov_type = str(raw.get("type") or raw.get("discount_type") or "").lower()
        local_type = (getattr(row, "discount_type", None) or "").lower()
        if (
            not prov_type
            or not local_type
            or prov_type not in _SUPPORTED_DISCOUNT_TYPES
            or local_type not in _SUPPORTED_DISCOUNT_TYPES
            or prov_type != local_type
        ):
            raise CouponTermsError("provider_coupon_identity_mismatch")

        desired = pending["desired"]

        if not _terms_match(raw, desired):
            await adapter.update_coupon_terms(provider_id_val, **desired)
            raw = await adapter.get_coupon_by_id(provider_id_val)

        # Validate readback identity.
        if str(raw.get("id") or raw.get("coupon_id") or "") != provider_id_val or not same_code(raw.get("code"), row.code):
            raise CouponTermsError("provider_terms_unconfirmed")
        assert_dispatch_identity(db, row, binding, provider_id_val, raw.get("code"))

        # Validate readback type stability – same requirements as first GET.
        prov_type = str(raw.get("type") or raw.get("discount_type") or "").lower()
        if (
            not prov_type
            or not local_type
            or prov_type not in _SUPPORTED_DISCOUNT_TYPES
            or local_type not in _SUPPORTED_DISCOUNT_TYPES
            or prov_type != local_type
        ):
            raise CouponTermsError("provider_terms_unconfirmed")

        if not _terms_match(raw, desired):
            raise CouponTermsError("provider_terms_unconfirmed")

        now = utcnow()
        amount_raw, expiry_raw = _raw_to_terms(raw)

        # Update actual row fields from confirmed readback only
        if "amount" in desired:
            try:
                row.discount_value = format(Decimal(str(amount_raw)), "f")
            except (InvalidOperation, TypeError):
                raise CouponTermsError("provider_terms_unconfirmed")
            meta["confirmed_value"] = desired["amount"]

        if "expiry_date" in desired:
            utc_aware = provider_expiry_utc(expiry_raw)
            if utc_aware is not None:
                # Store as naive UTC as the existing DateTime column expects
                row.expires_at = utc_aware.replace(tzinfo=None)
                meta["terms_source_updated_at_tz"] = "UTC"
            meta["confirmed_expiry_date"] = desired["expiry_date"]

        meta["terms_applied_at"] = now.isoformat()
        meta.update(
            terms_sync_status="synced",
            terms_sync_error=None,
            terms_last_synced_at=now.isoformat(),
            terms_api_to_apply_ms=int((monotonic() - started) * 1000),
            terms_request_to_apply_ms=int(
                (now - timestamp(pending["requested_at"])).total_seconds() * 1000
            ),
            salla_identity=binding,
            salla_provider_code=raw["code"],
            # Global sync state
            sync_status="synced",
            salla_synced=True,
            sync_error=None,
        )
        source_at = timestamp(raw.get("updated_at"))
        if source_at:
            meta["terms_source_updated_at"] = source_at.isoformat()
        meta.pop("terms_sync_pending", None)

    except Exception as exc:
        if (
            isinstance(exc, CouponTermsError)
            and str(exc) == "terms_change_already_pending"
            and meta.get("active_sync_pending")
        ):
            # Dual pending conflict: preserve both pending states, do not set global failed
            meta.update(
                terms_sync_pending=pending,
                terms_sync_status="pending",
                terms_sync_error=None,
            )
        else:
            pending["attempts"] = int(pending.get("attempts", 0)) + 1
            pending["next_attempt_at"] = (
                utcnow() + timedelta(seconds=min(300, 10 * 2 ** min(pending["attempts"], 5)))
            ).isoformat()
            err = (
                str(exc)
                if isinstance(exc, (CouponTermsError, ValueError)) and str(exc) in _SAFE_REASONS
                else type(exc).__name__
            )
            meta.update(
                terms_sync_pending=pending,
                terms_sync_status="failed",
                terms_sync_error=err,
                sync_status="failed",
                salla_synced=False,
                sync_error=err,
            )

    row.extra_metadata = meta
    flag_modified(row, "extra_metadata")
    db.commit()

    from core.coupon_log_privacy import hash_identifier
    logger.info(
        "coupon_terms_sync_committed tenant_hash=%s coupon_hash=%s outcome=%s",
        hash_identifier(tenant_id),
        hash_identifier(coupon_id),
        meta.get("terms_sync_status"),
    )


async def retry_terms_changes(db, integration):
    from models import Coupon
    from store_integration.registry import adapter_for_integration

    rows = (
        db.query(Coupon)
        .filter_by(tenant_id=integration.tenant_id)
        .filter(Coupon.extra_metadata["terms_sync_pending"].as_string().isnot(None))
        .all()
    )
    now = utcnow()
    due = []
    for row in rows:
        row_meta = row.extra_metadata or {}
        if "salla_retirement" in row_meta or row_meta.get("is_system_coupon"):
            continue
        from services.coupon_sync_visibility import resolve_coupon_source_type as _rcs
        if _rcs(
            column_source_type=getattr(row, "source_type", None),
            meta=row_meta,
            origin=str(row_meta.get("source") or ""),
        ) == "system":
            continue
        pending = row_meta.get("terms_sync_pending") or {}
        next_at = timestamp(pending.get("next_attempt_at"))
        if pending.get("integration_id") == integration.id and (next_at is None or next_at <= now):
            due.append((next_at or timestamp(pending.get("requested_at")) or now, row.id))

    ids = [cid for _, cid in sorted(due)]
    if not ids:
        return
    adapter = adapter_for_integration(integration)
    for coupon_id in ids[:20]:
        await dispatch_terms_change(db, integration.tenant_id, coupon_id, integration, adapter)
    db.commit()


# ---------------------------------------------------------------------------
# Reconcile imported terms
# ---------------------------------------------------------------------------

def _timezone_known_now():
    return utcnow().isoformat()


async def reconcile_imported_terms(db, row, raw, observed_at, adapter, binding):
    """Called by importer after ordinary import.

    Returns (raw_for_terms_or_None, metadata_delta) where *raw_for_terms_or_None*
    is the dict the importer should use to apply value/type/expiry fields, and
    *metadata_delta* is a dict of watermark/audit fields to merge after import.
    When None is returned, the importer must not update value, type, or expiry.
    """
    meta = row.extra_metadata or {}

    # Active sync in flight: skip terms application/detail GET, preserve intent and global state.
    if meta.get("active_sync_pending"):
        return None, {
            "sync_status": meta.get("sync_status", "pending"),
            "sync_error": meta.get("sync_error"),
            "salla_synced": False,
        }

    # Never regress a pending or failed outstanding sync; preserve global state.
    if meta.get("terms_sync_pending"):
        return None, {
            "sync_status": meta.get("sync_status", "pending"),
            "sync_error": meta.get("terms_sync_error") or meta.get("sync_error"),
            "salla_synced": False,
        }

    source_at = timestamp(raw.get("updated_at"))
    prior_at = timestamp(meta.get("terms_source_updated_at"))
    applied_at = timestamp(meta.get("terms_applied_at"))

    # Reject observations older than confirmed applied timestamp.
    if applied_at and observed_at < applied_at:
        return None, {}

    # Reject source older than prior watermark.
    if source_at and prior_at and source_at < prior_at:
        return None, {}

    # Determine whether a canonical detail GET is needed.
    need_detail = False
    if prior_at or applied_at:
        if source_at is None:
            # No source timestamp after a known watermark — cannot order safely.
            need_detail = True
        elif source_at == prior_at:
            # Equal watermark: check for any value/expiry/type conflict.
            amount_raw, expiry_raw = _raw_to_terms(raw)
            try:
                incoming_decimal = Decimal(str(amount_raw))
            except (InvalidOperation, TypeError):
                incoming_decimal = None
            # Compare against actual locked row fields numerically, not as formatted strings.
            try:
                row_decimal = Decimal(str(row.discount_value)) if row.discount_value is not None else None
            except (InvalidOperation, TypeError):
                row_decimal = None
            value_conflict = (
                incoming_decimal is not None
                and row_decimal is not None
                and incoming_decimal != row_decimal
            )
            expiry_raw_present = expiry_raw is not None and expiry_raw != ""
            incoming_expiry_utc = provider_expiry_utc(expiry_raw) if expiry_raw_present else None
            row_expires_at = row.expires_at
            expiry_conflict = False
            if incoming_expiry_utc is not None and row_expires_at is not None:
                row_expires_utc = (
                    row_expires_at.replace(tzinfo=timezone.utc)
                    if row_expires_at.tzinfo is None
                    else row_expires_at
                )
                expiry_conflict = incoming_expiry_utc != row_expires_utc
            elif expiry_raw_present and incoming_expiry_utc is None:
                # Nonempty expiry that failed to parse → cannot safely compare.
                expiry_conflict = True
            elif incoming_expiry_utc is not None and row_expires_at is None:
                # Provider has expiry, row does not (presence mismatch).
                expiry_conflict = True
            elif not expiry_raw_present and row_expires_at is not None:
                # Provider has no expiry, row does (presence mismatch).
                expiry_conflict = True
            local_type = (getattr(row, "discount_type", None) or "").lower()
            prov_type = str(raw.get("type") or raw.get("discount_type") or "").lower()
            type_conflict = bool(prov_type and local_type and prov_type != local_type)
            if value_conflict or expiry_conflict or type_conflict:
                need_detail = True

    if need_detail:
        try:
            if not binding:
                # Watermark exists but no binding: fail closed.
                return None, {"sync_status": "failed", "sync_error": "coupon_identity_ambiguous", "salla_synced": False}
            from services.coupon_identity import validate_import_binding, assert_dispatch_identity
            validate_import_binding(db, binding)
            _locked_pid = str(_provider_id(meta) or "")
            _incoming_id = str(raw.get("id") or raw.get("coupon_id") or "")
            if _locked_pid and _incoming_id != _locked_pid:
                return None, {"sync_status": "failed", "sync_error": "provider_coupon_identity_mismatch", "salla_synced": False}
            assert_dispatch_identity(db, row, binding, _incoming_id, raw.get("code"))
            detail_id = _incoming_id
            from services.coupon_identity import adapter_binding as _ab
            if _ab(adapter, row.tenant_id) != binding:
                return None, {"sync_status": "failed", "sync_error": "adapter_tenant_mismatch", "salla_synced": False}
            detail = await adapter.get_coupon_by_id(detail_id)
            # Validate identity against locked row
            if str(detail.get("id")) != str(raw.get("id")) or not same_code(detail.get("code"), raw.get("code")):
                return None, {"sync_status": "failed", "sync_error": "provider_terms_unconfirmed", "salla_synced": False}
            # assert_dispatch_identity again after detail GET
            assert_dispatch_identity(db, row, binding, str(detail.get("id") or detail.get("coupon_id") or ""), detail.get("code"))
            # Validate against binding: optional store_id present and mismatched → fail
            b_sid = str(binding.get("store_id") or "")
            d_sid = str(detail.get("store_id") or "")
            if b_sid and d_sid and d_sid != b_sid:
                return None, {"sync_status": "failed", "sync_error": "provider_terms_unconfirmed", "salla_synced": False}
            # Require known matching supported type (not optional)
            prov_type = str(detail.get("type") or detail.get("discount_type") or "").lower()
            local_type = (getattr(row, "discount_type", None) or "").lower()
            if (
                not prov_type
                or not local_type
                or prov_type not in _SUPPORTED_DISCOUNT_TYPES
                or local_type not in _SUPPORTED_DISCOUNT_TYPES
                or prov_type != local_type
            ):
                return None, {"sync_status": "failed", "sync_error": "provider_terms_unconfirmed", "salla_synced": False}
            # Reject detail timestamp older than prior watermark
            detail_source_at = timestamp(detail.get("updated_at"))
            if prior_at and detail_source_at and detail_source_at < prior_at:
                return None, {"sync_status": "failed", "sync_error": "provider_terms_unconfirmed", "salla_synced": False}
            delta = {}
            if detail_source_at:
                delta["terms_source_updated_at"] = detail_source_at.isoformat()
            delta["terms_applied_at"] = _timezone_known_now()
            return detail, delta
        except Exception:
            return None, {"sync_status": "failed", "sync_error": "provider_terms_unconfirmed", "salla_synced": False}

    # Bound normal path: validate binding+adapter+identity before accepting raw.
    if binding:
        try:
            from services.coupon_identity import validate_import_binding, assert_dispatch_identity
            from services.coupon_identity import adapter_binding as _ab
            validate_import_binding(db, binding)
            _locked_pid = str(_provider_id(meta) or "")
            _incoming_id = str(raw.get("id") or raw.get("coupon_id") or "")
            if _locked_pid and _incoming_id != _locked_pid:
                return None, {"sync_status": "failed", "sync_error": "provider_coupon_identity_mismatch", "salla_synced": False}
            assert_dispatch_identity(db, row, binding, _incoming_id, raw.get("code"))
            if _ab(adapter, row.tenant_id) != binding:
                return None, {"sync_status": "failed", "sync_error": "adapter_tenant_mismatch", "salla_synced": False}
        except Exception:
            return None, {"sync_status": "failed", "sync_error": "provider_terms_unconfirmed", "salla_synced": False}

    # Normal path: accept incoming raw.
    delta = {}
    if source_at:
        # Only write watermark when timestamp is present; never erase previous.
        delta["terms_source_updated_at"] = source_at.isoformat()
    delta["terms_applied_at"] = _timezone_known_now()
    return raw, delta

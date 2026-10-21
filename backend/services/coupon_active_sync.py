"""Durable, tenant-bound Salla coupon status changes (no coupon creation)."""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from time import monotonic
from uuid import uuid4
import logging
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from sqlalchemy.orm.attributes import flag_modified
from services.coupon_identity import same_code, assert_dispatch_identity

logger = logging.getLogger(__name__)


def utcnow():
    return datetime.now(timezone.utc)


def timestamp(value):
    zone = None
    if isinstance(value, dict):
        zone = value.get("timezone")
        value = value.get("date")
    try:
        dt = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
        if dt.tzinfo is None:
            # A timezone-free provider timestamp is not reliable latency evidence.
            if not zone:
                return None
            try:
                tz = ZoneInfo(zone)
            except ZoneInfoNotFoundError:
                if zone != "Asia/Riyadh":
                    return None
                from core.salla_order_fidelity import _riyadh_tz
                tz = _riyadh_tz()
            dt = dt.replace(tzinfo=tz)
        return dt.astimezone(timezone.utc)
    except (ValueError, TypeError, ZoneInfoNotFoundError):
        return None


def normalized_active(raw):
    """Absent or unrecognised state is unknown, never an implicit activation."""
    status = raw.get("status")
    if isinstance(status, str) and status.strip().lower() in {"active", "inactive"}:
        return status.strip().lower() == "active"
    value = raw.get("active")
    if isinstance(value, bool):
        return value
    return None


def imported_state(raw, existing, observed_at):
    """Keep an outstanding local intent and reject older provider observations."""
    if existing.get("active_sync_pending"):
        return {}
    source_at = timestamp(raw.get("updated_at"))
    prior_at = timestamp(existing.get("active_source_updated_at"))
    applied_at = timestamp(existing.get("active_applied_at"))
    if source_at and prior_at and source_at < prior_at:
        return {}
    if applied_at and observed_at < applied_at:
        return {}
    active = normalized_active(raw)
    if active is None:
        return {}
    result = {"active": active, "active_applied_at": utcnow().isoformat(),
              "active_observed_at": observed_at.isoformat()}
    if source_at:
        result["active_source_updated_at"] = source_at.isoformat()
        result["active_source_to_apply_ms"] = max(0, int((utcnow() - source_at).total_seconds() * 1000))
    return result


async def reconcile_imported_state(raw, existing, observed_at, adapter):
    source_at = timestamp(raw.get("updated_at"))
    prior_at = timestamp(existing.get("active_source_updated_at"))
    if (source_at and source_at == prior_at and normalized_active(raw) is not None
            and normalized_active(raw) is not existing.get("active")):
        # Second-resolution provider timestamps cannot order conflicting changes.
        # Read current detail rather than trusting a replayed list/event payload.
        try:
            detail = await adapter.get_coupon_by_id(str(raw["id"]))
            if str(detail.get("id")) != str(raw["id"]) or not same_code(detail.get("code"), raw.get("code")):
                return {"sync_status": "failed", "sync_error": "provider_state_unconfirmed", "salla_synced": False}
            return imported_state(detail, existing, observed_at)
        except Exception:
            return {"sync_status": "failed", "sync_error": "provider_state_unconfirmed", "salla_synced": False}
    return imported_state(raw, existing, observed_at)


def queue_active_change(coupon, integration, active):
    from services.coupon_retirement import assert_operational
    assert_operational(coupon)
    meta = dict(coupon.extra_metadata or {})
    pending = meta.get("active_sync_pending") or {}
    if pending.get("desired") == active and pending.get("integration_id") == integration.id:
        return
    if not pending and meta.get("active") is active and meta.get("sync_status") == "synced":
        return
    meta.update(active_sync_pending={"operation_id": uuid4().hex, "desired": active,
                "integration_id": integration.id, "store_id": str(integration.external_store_id or
                    (integration.config or {}).get("store_id") or (integration.config or {}).get("merchant_id") or ""),
                "requested_at": utcnow().isoformat(), "attempts": 0},
                sync_status="pending", sync_error=None, salla_synced=False)
    coupon.extra_metadata = meta
    flag_modified(coupon, "extra_metadata")


async def dispatch_active_change(db, tenant_id, coupon_id, integration, adapter):
    """Serialize a row across workers; replay sets status, never creates a coupon.

    Pending intent is committed by the caller before dispatch. A timeout after a
    successful PUT is safe to replay: GET first, PUT only when still different.
    """
    from models import Coupon
    row = (db.query(Coupon).filter_by(tenant_id=tenant_id, id=coupon_id)
           .populate_existing().with_for_update(skip_locked=True).first())
    if row is None:
        return
    if "salla_retirement" in (row.extra_metadata or {}):
        return  # Do not mutate frozen history or replay its former pending intent.
    meta = dict(row.extra_metadata or {})
    pending = dict(meta.get("active_sync_pending") or {})
    if not pending:
        return
    next_at = timestamp(pending.get("next_attempt_at"))
    if next_at and utcnow() < next_at:
        return
    provider_id = str(meta.get("salla_coupon_id") or meta.get("external_id") or "")
    started = monotonic()
    try:
        cfg = integration.config or {}
        store_id = str(integration.external_store_id or cfg.get("store_id") or cfg.get("merchant_id") or "")
        if (integration.tenant_id != tenant_id or integration.id != pending["integration_id"]
                or store_id != pending["store_id"] or not store_id
                or integration.provider != "salla" or not integration.enabled
                or (cfg.get("store_id") and str(cfg["store_id"]) != store_id)
                or cfg.get("needs_reauth") or not cfg.get("api_sync_enabled")
                or not cfg.get("refresh_token") or not provider_id.isdigit()):
            raise ValueError("integration_binding_unavailable")
        if (getattr(adapter, "_tenant_id", None) != tenant_id
                or getattr(adapter, "_integration_id", None) != integration.id
                or str(getattr(adapter, "store_id", "")) != str(cfg.get("store_id") or store_id)):
            raise ValueError("adapter_tenant_mismatch")
        raw = await adapter.get_coupon_by_id(provider_id)
        if str(raw.get("id")) != provider_id:
            raise ValueError("provider_coupon_identity_mismatch")
        binding = {"tenant_id": tenant_id, "integration_id": integration.id, "store_id": store_id}
        assert_dispatch_identity(db, row, binding, provider_id, raw.get("code"))
        desired = pending["desired"]
        if normalized_active(raw) is not desired:
            await adapter.set_coupon_active(provider_id, desired)
            raw = await adapter.get_coupon_by_id(provider_id)
        if (str(raw.get("id")) != provider_id or not same_code(raw.get("code"), row.code)
                or normalized_active(raw) is not desired):
            raise ValueError("provider_state_unconfirmed")
        now = utcnow()
        meta.update(active=desired, sync_status="synced", sync_error=None, salla_synced=True,
                    last_synced_at=now.isoformat(), active_applied_at=now.isoformat(),
                    active_api_to_apply_ms=int((monotonic() - started) * 1000),
                    active_request_to_apply_ms=int((now - timestamp(pending["requested_at"])).total_seconds() * 1000))
        meta.update(salla_identity=binding, salla_provider_code=raw["code"])
        source_at = timestamp(raw.get("updated_at"))
        if source_at:
            meta["active_source_updated_at"] = source_at.isoformat()
        meta.pop("active_sync_pending", None)
    except Exception as exc:
        # Persist only a safe class/reason; never provider bodies or credentials.
        pending["attempts"] = int(pending.get("attempts", 0)) + 1
        pending["next_attempt_at"] = (utcnow() + timedelta(seconds=min(300, 10 * 2 ** min(pending["attempts"], 5)))).isoformat()
        reasons = {"integration_binding_unavailable", "adapter_tenant_mismatch",
                   "provider_coupon_identity_mismatch", "provider_state_unconfirmed",
                   "legacy_store_ambiguous", "coupon_identity_ambiguous", "provider_alias_conflict"}
        meta.update(active_sync_pending=pending, sync_status="failed", salla_synced=False,
                    sync_error=str(exc) if isinstance(exc, ValueError) and str(exc) in reasons else type(exc).__name__)
    row.extra_metadata = meta
    flag_modified(row, "extra_metadata")
    db.commit()
    from core.coupon_log_privacy import hash_identifier
    logger.info("coupon_active_sync_committed tenant_hash=%s coupon_hash=%s outcome=%s api_to_db_commit_ms=%d request_to_db_commit_ms=%d",
                hash_identifier(tenant_id), hash_identifier(coupon_id), meta["sync_status"],
                int((monotonic() - started) * 1000),
                int((utcnow() - timestamp(pending["requested_at"])).total_seconds() * 1000))


async def retry_active_changes(db, integration):
    from models import Coupon
    from store_integration.registry import adapter_for_integration
    rows = (db.query(Coupon).filter_by(tenant_id=integration.tenant_id)
            .filter(Coupon.extra_metadata["active_sync_pending"].as_string().isnot(None)).all())
    now = utcnow()
    due = []
    for row in rows:
        if "salla_retirement" in (row.extra_metadata or {}):
            continue
        pending = (row.extra_metadata or {}).get("active_sync_pending") or {}
        next_at = timestamp(pending.get("next_attempt_at"))
        if (pending.get("integration_id") == integration.id
                and (next_at is None or next_at <= now)):
            due.append((next_at or timestamp(pending.get("requested_at")) or now, row.id))
    ids = [coupon_id for _, coupon_id in sorted(due)]
    if not ids:
        return
    adapter = adapter_for_integration(integration)
    for coupon_id in ids[:20]:
        await dispatch_active_change(db, integration.tenant_id, coupon_id, integration, adapter)
    # Release locks acquired for skipped/backoff rows, too.
    db.commit()

"""
core/trial_lifecycle.py
───────────────────────
Free-trial lifecycle: trial starts only after WhatsApp connects.

Duration sources (platform-wide constants — not per-plan):
  • Free trial window: ``core.billing.FREE_TRIAL_DAYS`` (14 days today)
  • Paid subscription period: ``SUBSCRIPTION_PERIOD_DAYS`` below (30 days monthly)
  • Salla App Store trials use a separate path in ``salla_subscription.py``

Registration / app open  → trial_pending_whatsapp (no countdown)
WhatsApp connected       → trial_active (FREE_TRIAL_DAYS window starts once)
"""
from __future__ import annotations

import logging
import os
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Optional

from sqlalchemy.orm import Session

from core.billing import FREE_TRIAL_DAYS, _coerce_utc, get_tenant_subscription

logger = logging.getLogger("nahla.trial_lifecycle")

TRIAL_STATUS_PENDING_WHATSAPP = "trial_pending_whatsapp"
TRIAL_STATUS_ACTIVE = "trial_active"
TRIAL_STATUS_EXPIRED = "trial_expired"

SUBSCRIPTION_PERIOD_DAYS = 30


def _conversation_quota_allows_service(db: Session, tenant_id: int) -> bool:
    """True when the tenant still has automated service-reply quota."""
    from core.billing_override import is_partner_testing_override_active  # noqa: PLC0415

    if is_partner_testing_override_active(db, tenant_id):
        return True

    try:
        from core.wa_usage import check_limit  # noqa: PLC0415

        return bool(check_limit(db, int(tenant_id), category="service").allowed)
    except Exception as exc:  # noqa: BLE001
        logger.warning(
            "[trial_lifecycle] conversation quota check failed tenant=%s err=%s",
            tenant_id,
            exc,
        )
        return True


def init_new_tenant_trial_state(tenant) -> None:
    """Apply to every newly created tenant — trial must not start yet."""
    tenant.subscription_status = TRIAL_STATUS_PENDING_WHATSAPP
    tenant.trial_started_at = None
    tenant.trial_ends_at = None
    tenant.first_whatsapp_connected_at = None


def _tenant_has_connected_whatsapp(db: Session, tenant_id: int) -> bool:
    from models import WhatsAppConnection  # noqa: PLC0415

    conn = (
        db.query(WhatsAppConnection)
        .filter(WhatsAppConnection.tenant_id == tenant_id)
        .first()
    )
    if not conn:
        return False
    return conn.status == "connected" and bool(conn.phone_number_id)


def _first_whatsapp_connection_at(db: Session, tenant_id: int) -> Optional[datetime]:
    from models import WhatsAppConnection  # noqa: PLC0415

    conn = (
        db.query(WhatsAppConnection)
        .filter(WhatsAppConnection.tenant_id == tenant_id)
        .first()
    )
    if not conn or conn.status != "connected":
        return None
    for candidate in (
        getattr(conn, "whatsapp_ai_live_since", None),
        conn.connected_at,
    ):
        coerced = _coerce_utc(candidate)
        if coerced:
            return coerced
    return None


def start_trial_on_whatsapp_connect(
    db: Session,
    tenant_id: int,
    *,
    connected_at: Optional[datetime] = None,
    commit: bool = True,
) -> bool:
    """
    Start the free trial once, on first successful WhatsApp connection.

    Returns True when trial was started by this call, False when idempotent skip.

    ``commit=False`` applies the same mutations without committing so a
    caller can persist connection + trial in one transaction.
    """
    from models import Tenant  # noqa: PLC0415

    tenant = db.query(Tenant).filter(Tenant.id == tenant_id).first()
    if not tenant:
        return False

    now = _coerce_utc(connected_at) or datetime.now(timezone.utc)
    first_wa_stamped = False

    if not tenant.first_whatsapp_connected_at:
        tenant.first_whatsapp_connected_at = now.replace(tzinfo=None)
        first_wa_stamped = True

    def _persist_skip() -> None:
        if commit and first_wa_stamped:
            db.commit()

    if tenant.trial_started_at is not None:
        logger.info(
            "[TrialLifecycle] skip start — trial already started tenant=%s started_at=%s",
            tenant_id,
            tenant.trial_started_at,
        )
        _persist_skip()
        return False

    if get_tenant_subscription(db, tenant_id):
        logger.info(
            "[TrialLifecycle] skip start — active paid subscription tenant=%s",
            tenant_id,
        )
        _persist_skip()
        return False

    tenant.trial_started_at = now.replace(tzinfo=None)
    tenant.trial_ends_at = (now + timedelta(days=FREE_TRIAL_DAYS)).replace(tzinfo=None)
    tenant.subscription_status = TRIAL_STATUS_ACTIVE
    if commit:
        db.commit()

    logger.info(
        "[TrialLifecycle] trial started tenant=%s ends_at=%s",
        tenant_id,
        tenant.trial_ends_at,
    )
    return True


# ── Historical trial reconciliation (WA-1) ──────────────────────────────────
# Some pre-canonical Coexistence/Embedded finalizes stamped WhatsApp
# connected_at without calling start_trial_on_whatsapp_connect().
# Reconcile from connection evidence, never from tenant IDs or last_webhook.

RECONCILE_ELIGIBLE = "eligible"
RECONCILE_SKIP_NO_CONNECTION = "skip_no_connection"
RECONCILE_SKIP_NO_PHONE_IDENTITY = "skip_no_phone_identity"
RECONCILE_SKIP_NO_AUTHORITATIVE_EVIDENCE = "skip_no_authoritative_connect_evidence"
RECONCILE_SKIP_ALREADY_STARTED = "skip_already_started"
RECONCILE_SKIP_EXPIRED_TRIAL = "skip_expired_trial"
RECONCILE_SKIP_PAID = "skip_paid"
RECONCILE_SKIP_PAID_HISTORY = "skip_paid_history"
RECONCILE_SKIP_SALLA_MANAGED = "skip_salla_managed"
RECONCILE_SKIP_GIFT = "skip_gift"
RECONCILE_SKIP_PARTNER_OVERRIDE = "skip_partner_override"
RECONCILE_SKIP_PLATFORM_TENANT = "skip_platform_tenant"
RECONCILE_SKIP_STATUS_NOT_ALLOWED = "skip_status_not_allowed"
RECONCILE_SKIP_AMBIGUOUS_PARTIAL_LIFECYCLE = "skip_ambiguous_partial_lifecycle"
RECONCILE_SKIP_AMBIGUOUS_CONNECTED_EVIDENCE = "skip_ambiguous_connected_evidence"

RECONCILE_DECISION_APPLY = "apply"
RECONCILE_DECISION_SKIP = "skip"
RECONCILE_DECISION_AMBIGUOUS = "ambiguous"

# Positive allowlist only. Unknown / cancelled / paid / legacy statuses never enter.
RECONCILE_ALLOWED_SUBSCRIPTION_STATUSES = frozenset({TRIAL_STATUS_PENDING_WHATSAPP})

# Historical connected_at is trusted only for Meta writers gated on real provider
# readiness (Embedded verified, Coexistence SMB accept, post-#845 finalizer).
# 360dialog / operator-backfill paths can stamp connected_at without the same
# readiness gate — those rows are ambiguous, not auto-repaired in WA-1.
RECONCILE_TRUSTED_CONNECTED_AT_PROVIDERS = frozenset({"meta"})

_SALLA_PROTECTED_BILLING = frozenset({"active", "trial", "trial_blocked"})


def authoritative_first_successful_whatsapp_at(conn) -> Optional[datetime]:
    """Return the canonical first-success timestamp, or None if not proven.

    Source of truth is ``WhatsAppConnection.connected_at`` plus a stored
    ``phone_number_id``. ``last_webhook_received_at`` is operational traffic,
    not first-connect time, and must not be used.

    Callers must still apply the WA-1 provider allowlist
    (``RECONCILE_TRUSTED_CONNECTED_AT_PROVIDERS``). This helper only proves
    that identity + timestamp exist; it does not certify every historical
    writer.
    """
    if conn is None:
        return None
    phone_id = str(getattr(conn, "phone_number_id", None) or "").strip()
    if not phone_id:
        return None
    return _coerce_utc(getattr(conn, "connected_at", None))


def _disallowed_subscription_status_reason(status: str) -> str:
    if status == TRIAL_STATUS_EXPIRED:
        return RECONCILE_SKIP_EXPIRED_TRIAL
    if status == TRIAL_STATUS_ACTIVE:
        return RECONCILE_SKIP_ALREADY_STARTED
    return RECONCILE_SKIP_STATUS_NOT_ALLOWED


def _lifecycle_triplet(tenant) -> tuple:
    return (
        getattr(tenant, "first_whatsapp_connected_at", None),
        getattr(tenant, "trial_started_at", None),
        getattr(tenant, "trial_ends_at", None),
    )


def _lifecycle_fully_missing(tenant) -> bool:
    first_wa, started, ends = _lifecycle_triplet(tenant)
    return first_wa is None and started is None and ends is None


def _settings_billing_blob(db: Session, tenant_id: int) -> Dict[str, Any]:
    from models import TenantSettings  # noqa: PLC0415

    row = (
        db.query(TenantSettings)
        .filter(TenantSettings.tenant_id == tenant_id)
        .first()
    )
    if row is None:
        return {}
    meta = row.extra_metadata or {}
    billing = meta.get("billing") if isinstance(meta, dict) else None
    return billing if isinstance(billing, dict) else {}


def _gift_grant_is_active(blob: Optional[Dict[str, Any]]) -> bool:
    """Match canonical gift activity, including never-expiring grants."""
    if not isinstance(blob, dict) or not blob.get("enabled") or blob.get("revoked_at"):
        return False
    now = datetime.now(timezone.utc)
    starts = blob.get("starts_at")
    try:
        if starts:
            start_dt = _coerce_utc(datetime.fromisoformat(str(starts).replace("Z", "+00:00")))
            if start_dt and start_dt > now:
                return False
    except (TypeError, ValueError):
        return False

    from core.manual_billing_grant import is_permanent_gift_blob  # noqa: PLC0415

    if is_permanent_gift_blob(blob):
        return True

    ends = blob.get("ends_at")
    try:
        if not ends:
            return False
        end_dt = _coerce_utc(datetime.fromisoformat(str(ends).replace("Z", "+00:00")))
        if not end_dt or end_dt <= now:
            return False
    except (TypeError, ValueError):
        return False
    return True


def _partner_override_is_active(blob: Optional[Dict[str, Any]]) -> bool:
    """Match canonical billing: enabled + unexpired.

    Malformed ``expires_at`` is treated as no expiry (still active), the same
    as ``core.billing_override.is_partner_testing_override_active``.
    """
    if not isinstance(blob, dict) or not blob.get("enabled"):
        return False
    raw = blob.get("expires_at")
    expires = None
    if raw:
        try:
            expires = _coerce_utc(datetime.fromisoformat(str(raw).replace("Z", "+00:00")))
        except (TypeError, ValueError):
            expires = None
    if expires and expires <= datetime.now(timezone.utc):
        return False
    return True


def _protected_salla_billing_status(db: Session, tenant_id: int) -> str:
    """Return a protected Salla billing_status if ANY integration row has one.

    Tenant+provider uniqueness is not enforced in the schema, so the first
    row is not authoritative.
    """
    from models import Integration  # noqa: PLC0415

    rows = (
        db.query(Integration)
        .filter(Integration.tenant_id == tenant_id, Integration.provider == "salla")
        .all()
    )
    for integration in rows:
        cfg = integration.config or {}
        status = str(cfg.get("billing_status") or "").strip().lower()
        if status in _SALLA_PROTECTED_BILLING:
            return status
    return ""


def _tenant_has_paid_billing_payment(db: Session, tenant_id: int) -> bool:
    """True when any paid BillingPayment exists, even without a subscription."""
    from models import BillingPayment  # noqa: PLC0415

    row = (
        db.query(BillingPayment.id)
        .filter(
            BillingPayment.tenant_id == tenant_id,
            BillingPayment.status == "paid",
        )
        .first()
    )
    return row is not None


def classify_missing_trial_after_whatsapp(db: Session, tenant) -> Dict[str, Any]:
    """Decide whether a tenant may receive historical trial reconciliation.

    Platform-wide. No tenant-id branches. Does not write.

    Auto-repair requires all of:
      * ``subscription_status == trial_pending_whatsapp``
      * ``first_whatsapp_connected_at``, ``trial_started_at``, and
        ``trial_ends_at`` all NULL
      * Meta ``connected_at`` + ``phone_number_id``
      * no paid / Salla / gift / partner-override protection
    """
    from models import WhatsAppConnection  # noqa: PLC0415

    tenant_id = int(getattr(tenant, "id", 0) or 0)
    result: Dict[str, Any] = {
        "tenant_id": tenant_id,
        "decision": RECONCILE_DECISION_SKIP,
        "reason": RECONCILE_SKIP_NO_CONNECTION,
        "historical_connected_at": None,
        "evidence": {},
    }
    if tenant_id <= 0:
        return result
    if getattr(tenant, "is_platform_tenant", False):
        result["reason"] = RECONCILE_SKIP_PLATFORM_TENANT
        return result

    status = str(getattr(tenant, "subscription_status", None) or "").strip()
    if status not in RECONCILE_ALLOWED_SUBSCRIPTION_STATUSES:
        result["reason"] = _disallowed_subscription_status_reason(status)
        return result

    first_wa, started, ends = _lifecycle_triplet(tenant)
    if not _lifecycle_fully_missing(tenant):
        if started is not None:
            result["reason"] = RECONCILE_SKIP_ALREADY_STARTED
            return result
        result["decision"] = RECONCILE_DECISION_AMBIGUOUS
        result["reason"] = RECONCILE_SKIP_AMBIGUOUS_PARTIAL_LIFECYCLE
        result["evidence"] = {
            "first_whatsapp_connected_at": _iso(_coerce_utc(first_wa)),
            "trial_started_at": _iso(_coerce_utc(started)),
            "trial_ends_at": _iso(_coerce_utc(ends)),
        }
        return result

    if get_tenant_subscription(db, tenant_id):
        result["reason"] = RECONCILE_SKIP_PAID
        return result
    if get_latest_paid_subscription(db, tenant_id) or _tenant_has_paid_billing_payment(db, tenant_id):
        result["reason"] = RECONCILE_SKIP_PAID_HISTORY
        return result

    salla_status = _protected_salla_billing_status(db, tenant_id)
    if salla_status:
        result["reason"] = RECONCILE_SKIP_SALLA_MANAGED
        result["evidence"] = {"salla_billing_status": salla_status}
        return result

    billing_blob = _settings_billing_blob(db, tenant_id)
    if _gift_grant_is_active(billing_blob.get("manual_gift_grant")):
        result["reason"] = RECONCILE_SKIP_GIFT
        return result
    if _partner_override_is_active(billing_blob.get("partner_testing_override")):
        result["reason"] = RECONCILE_SKIP_PARTNER_OVERRIDE
        return result

    conn = (
        db.query(WhatsAppConnection)
        .filter(WhatsAppConnection.tenant_id == tenant_id)
        .first()
    )
    if conn is None:
        return result

    phone_id = str(getattr(conn, "phone_number_id", None) or "").strip()
    provider = str(getattr(conn, "provider", None) or "").strip().lower()
    meta = dict(getattr(conn, "extra_metadata", None) or {})
    evidence = {
        "wa_status": getattr(conn, "status", None),
        "provider": getattr(conn, "provider", None),
        "connection_type": getattr(conn, "connection_type", None),
        "connection_mode": meta.get("connection_mode"),
        "connected_at": _iso(_coerce_utc(getattr(conn, "connected_at", None))),
        "whatsapp_ai_live_since": _iso(_coerce_utc(getattr(conn, "whatsapp_ai_live_since", None))),
        "webhook_verified": bool(getattr(conn, "webhook_verified", False)),
        "has_phone_number_id": bool(phone_id),
        "subscription_status": status,
    }
    result["evidence"] = evidence

    if not phone_id:
        result["reason"] = RECONCILE_SKIP_NO_PHONE_IDENTITY
        return result

    first_at = authoritative_first_successful_whatsapp_at(conn)
    if first_at is None:
        result["reason"] = RECONCILE_SKIP_NO_AUTHORITATIVE_EVIDENCE
        return result

    if provider not in RECONCILE_TRUSTED_CONNECTED_AT_PROVIDERS:
        result["decision"] = RECONCILE_DECISION_AMBIGUOUS
        result["reason"] = RECONCILE_SKIP_AMBIGUOUS_CONNECTED_EVIDENCE
        result["historical_connected_at"] = first_at.isoformat()
        return result

    result["decision"] = RECONCILE_DECISION_APPLY
    result["reason"] = RECONCILE_ELIGIBLE
    result["historical_connected_at"] = first_at.isoformat()
    return result


def discover_missing_trial_after_whatsapp(db: Session) -> List[Dict[str, Any]]:
    """Read-only classification of every merchant tenant."""
    from models import Tenant  # noqa: PLC0415

    rows: List[Dict[str, Any]] = []
    tenants = db.query(Tenant).filter(Tenant.is_platform_tenant == False).all()  # noqa: E712
    for tenant in tenants:
        rows.append(classify_missing_trial_after_whatsapp(db, tenant))
    return rows


def _reconcile_apply_payload(classification: Dict[str, Any], tenant, started: bool) -> Dict[str, Any]:
    return {
        **classification,
        "applied": bool(started),
        "subscription_status": tenant.subscription_status,
        "trial_started_at": _iso(_coerce_utc(tenant.trial_started_at)),
        "trial_ends_at": _iso(_coerce_utc(tenant.trial_ends_at)),
        "first_whatsapp_connected_at": _iso(_coerce_utc(tenant.first_whatsapp_connected_at)),
    }


def reconcile_missing_trial_after_whatsapp_connect(
    db: Session,
    tenant_id: int,
    *,
    commit: bool = True,
) -> Dict[str, Any]:
    """Idempotent historical trial start for one tenant.

    Anchors ``first_whatsapp_connected_at`` / trial window to the original
    ``connected_at``. Does not use reconciliation-clock "now". If that window
    has already elapsed, lifecycle becomes ``trial_expired`` (honest remaining
    days = 0), never a fresh 14-day grant.
    """
    from models import Tenant  # noqa: PLC0415

    tenant = db.query(Tenant).filter(Tenant.id == tenant_id).first()
    if tenant is None:
        return {
            "tenant_id": tenant_id,
            "decision": "skip",
            "reason": "skip_tenant_not_found",
            "applied": False,
        }

    classification = classify_missing_trial_after_whatsapp(db, tenant)
    if classification["decision"] != RECONCILE_DECISION_APPLY:
        logger.info(
            "[TrialLifecycle] reconcile skip tenant=%s reason=%s",
            tenant_id,
            classification["reason"],
        )
        return {**classification, "applied": False}

    first_at = datetime.fromisoformat(classification["historical_connected_at"])
    started = start_trial_on_whatsapp_connect(
        db, tenant_id, connected_at=first_at, commit=False,
    )

    if started:
        trial_end = _coerce_utc(tenant.trial_ends_at)
        now = datetime.now(timezone.utc)
        if trial_end and trial_end <= now:
            tenant.subscription_status = TRIAL_STATUS_EXPIRED

    payload = _reconcile_apply_payload(classification, tenant, started)
    if not commit:
        payload["persist_state"] = "uncommitted"
        return payload

    db.flush()
    payload = _reconcile_apply_payload(classification, tenant, started)
    db.commit()
    payload["persist_state"] = "committed"
    try:
        db.refresh(tenant)
        payload = _reconcile_apply_payload(classification, tenant, started)
        payload["persist_state"] = "committed"
    except Exception:
        logger.exception(
            "[TrialLifecycle] reconcile committed; refresh failed tenant=%s",
            tenant_id,
        )
        payload["refresh_failed"] = True
        payload["persist_state"] = "committed_refresh_failed"

    logger.info(
        "[TrialLifecycle] reconcile tenant=%s reason=%s started=%s "
        "first_wa=%s trial_end=%s status=%s persist_state=%s",
        tenant_id,
        classification["reason"],
        started,
        payload.get("first_whatsapp_connected_at"),
        payload.get("trial_ends_at"),
        payload.get("subscription_status"),
        payload.get("persist_state"),
    )
    return payload


def reconcile_missing_trials_after_whatsapp_connect(
    db: Session,
    *,
    dry_run: bool = True,
) -> Dict[str, Any]:
    """Batch historical trial reconciliation. Default is dry-run (no writes)."""
    discovered = discover_missing_trial_after_whatsapp(db)
    eligible = [row for row in discovered if row["decision"] == RECONCILE_DECISION_APPLY]
    ambiguous = [row for row in discovered if row["decision"] == RECONCILE_DECISION_AMBIGUOUS]
    applied: List[Dict[str, Any]] = []
    skipped = [row for row in discovered if row["decision"] == RECONCILE_DECISION_SKIP]

    if dry_run:
        return {
            "dry_run": True,
            "scanned": len(discovered),
            "eligible": len(eligible),
            "ambiguous": len(ambiguous),
            "skipped_count": len(skipped),
            "applied": [],
            "skipped": skipped,
            "ambiguous_rows": ambiguous,
            "candidates": eligible,
        }

    for row in eligible:
        tenant_id = int(row["tenant_id"])
        try:
            result = reconcile_missing_trial_after_whatsapp_connect(
                db, tenant_id, commit=True,
            )
            applied.append(result)
        except Exception:
            db.rollback()
            logger.exception(
                "[TrialLifecycle] reconcile failed tenant=%s before persist; rolled back",
                tenant_id,
            )
            skipped.append({
                **row,
                "reason": "skip_reconcile_error",
                "decision": RECONCILE_DECISION_SKIP,
                "persist_state": "rolled_back",
            })

    return {
        "dry_run": False,
        "scanned": len(discovered),
        "eligible": len(eligible),
        "ambiguous": len(ambiguous),
        "skipped_count": len(skipped),
        "applied": applied,
        "skipped": skipped,
        "ambiguous_rows": ambiguous,
        "candidates": eligible,
    }


def _warning_level(days_remaining: int, *, expired: bool) -> str:
    if expired:
        return "expired"
    if days_remaining <= 1:
        return "1d"
    if days_remaining <= 3:
        return "3d"
    if days_remaining <= 7:
        return "7d"
    return "none"


def _status_reason_ar(
    *,
    pending_whatsapp: bool,
    is_trial: bool,
    trial_expired: bool,
    has_subscription: bool,
    subscription_expired: bool,
) -> str:
    if has_subscription and not subscription_expired:
        return "اشتراك مدفوع نشط"
    if subscription_expired:
        return "انتهى الاشتراك المدفوع — يرجى التجديد"
    if pending_whatsapp:
        return "التجربة المجانية لم تبدأ بعد — اربط واتساب لبدء التجربة"
    if is_trial:
        return "تجربة مجانية نشطة"
    if trial_expired:
        return "انتهت التجربة المجانية — يرجى الاشتراك"
    return "لا يوجد اشتراك نشط"


def compute_trial_info(tenant) -> dict:
    """
    Unified trial computation.

    Trial never falls back to tenant.created_at. Without trial_started_at the
    merchant is in trial_pending_whatsapp (or expired if they had a trial that ended).
    """
    now = datetime.now(timezone.utc)
    status = getattr(tenant, "subscription_status", None) or ""

    trial_started = _coerce_utc(getattr(tenant, "trial_started_at", None))
    trial_end = _coerce_utc(getattr(tenant, "trial_ends_at", None))

    if trial_started and trial_end is None:
        trial_end = trial_started + timedelta(days=FREE_TRIAL_DAYS)

    pending_whatsapp = trial_started is None and status in (
        TRIAL_STATUS_PENDING_WHATSAPP,
        None,
        "",
    )

    if trial_started is None and status not in (TRIAL_STATUS_EXPIRED,):
        return {
            "is_trial": False,
            "trial_pending_whatsapp": True,
            "trial_days_remaining": 0,
            "trial_expired": False,
            "trial_end": None,
            "trial_started_at": None,
            "status": TRIAL_STATUS_PENDING_WHATSAPP,
            "status_reason_ar": _status_reason_ar(
                pending_whatsapp=True,
                is_trial=False,
                trial_expired=False,
                has_subscription=False,
                subscription_expired=False,
            ),
            "warning_level": "none",
        }

    if trial_end is None:
        return {
            "is_trial": False,
            "trial_pending_whatsapp": False,
            "trial_days_remaining": 0,
            "trial_expired": True,
            "trial_end": None,
            "trial_started_at": trial_started.isoformat() if trial_started else None,
            "status": TRIAL_STATUS_EXPIRED,
            "status_reason_ar": _status_reason_ar(
                pending_whatsapp=False,
                is_trial=False,
                trial_expired=True,
                has_subscription=False,
                subscription_expired=False,
            ),
            "warning_level": "expired",
        }

    remaining = (trial_end - now).total_seconds()
    days_left = max(0, int(remaining / 86400) + (1 if remaining > 0 else 0))
    is_active = remaining > 0
    expired = remaining <= 0

    effective_status = TRIAL_STATUS_ACTIVE if is_active else TRIAL_STATUS_EXPIRED

    return {
        "is_trial": is_active,
        "trial_pending_whatsapp": False,
        "trial_days_remaining": days_left,
        "trial_expired": expired,
        "trial_end": trial_end.isoformat(),
        "trial_started_at": trial_started.isoformat() if trial_started else None,
        "status": effective_status,
        "status_reason_ar": _status_reason_ar(
            pending_whatsapp=False,
            is_trial=is_active,
            trial_expired=expired,
            has_subscription=False,
            subscription_expired=False,
        ),
        "warning_level": _warning_level(days_left, expired=expired),
    }


def subscription_period_end(started_at: datetime) -> datetime:
    start = _coerce_utc(started_at) or datetime.now(timezone.utc)
    return start + timedelta(days=SUBSCRIPTION_PERIOD_DAYS)


def ensure_subscription_ends_at(sub) -> None:
    """Backfill ends_at on active subs that were activated without an expiry."""
    if sub is None or sub.status != "active":
        return
    if sub.ends_at:
        return
    paid_at = None
    meta = sub.extra_metadata or {}
    raw_paid = meta.get("paid_at")
    if raw_paid:
        try:
            paid_at = datetime.fromisoformat(str(raw_paid))
        except (TypeError, ValueError):
            paid_at = None
    anchor = _coerce_utc(paid_at) or _coerce_utc(sub.started_at) or datetime.now(timezone.utc)
    sub.ends_at = subscription_period_end(anchor).replace(tzinfo=None)


def _iso(dt: Optional[datetime]) -> Optional[str]:
    coerced = _coerce_utc(dt)
    return coerced.isoformat() if coerced else None


def _days_until(end: Optional[datetime]) -> int:
    coerced = _coerce_utc(end)
    if not coerced:
        return 0
    now = datetime.now(timezone.utc)
    remaining = (coerced - now).total_seconds()
    if remaining <= 0:
        return 0
    return max(0, int(remaining / 86400) + 1)


def _days_since(past: Optional[datetime]) -> int:
    coerced = _coerce_utc(past)
    if not coerced:
        return 0
    now = datetime.now(timezone.utc)
    elapsed = (now - coerced).total_seconds()
    if elapsed <= 0:
        return 0
    return max(0, int(elapsed / 86400))


def _effective_sub_ends_at(sub) -> Optional[datetime]:
    if sub is None:
        return None
    ends = _coerce_utc(sub.ends_at)
    if ends:
        return ends
    meta = sub.extra_metadata or {}
    raw_paid = meta.get("paid_at")
    anchor = None
    if raw_paid:
        try:
            anchor = _coerce_utc(datetime.fromisoformat(str(raw_paid)))
        except (TypeError, ValueError):
            anchor = None
    anchor = anchor or _coerce_utc(sub.started_at) or datetime.now(timezone.utc)
    return subscription_period_end(anchor)


def _sub_was_paid(sub) -> bool:
    if sub is None:
        return False
    meta = sub.extra_metadata or {}
    if meta.get("paid_at") or meta.get("moyasar_payment_id"):
        return True
    activated_by = meta.get("activated_by") or meta.get("activation_source") or ""
    return activated_by in ("dashboard", "demo_checkout", "webhook_invoice", "reconcile", "result_page_poll")


def get_latest_paid_subscription(db: Session, tenant_id: int):
    from models import BillingPayment, BillingSubscription  # noqa: PLC0415

    subs = (
        db.query(BillingSubscription)
        .filter(BillingSubscription.tenant_id == tenant_id)
        .order_by(BillingSubscription.id.desc())
        .all()
    )
    for sub in subs:
        if _sub_was_paid(sub):
            return sub

    payment = (
        db.query(BillingPayment)
        .filter(
            BillingPayment.tenant_id == tenant_id,
            BillingPayment.status == "paid",
        )
        .order_by(BillingPayment.paid_at.desc(), BillingPayment.id.desc())
        .first()
    )
    if payment and payment.subscription_id:
        return (
            db.query(BillingSubscription)
            .filter(BillingSubscription.id == payment.subscription_id)
            .first()
        )
    return None


def get_payment_history(db: Session, tenant_id: int, *, limit: int = 10) -> List[Dict[str, Any]]:
    from models import BillingPayment, BillingPlan, BillingSubscription  # noqa: PLC0415

    rows = (
        db.query(BillingPayment)
        .filter(BillingPayment.tenant_id == tenant_id)
        .order_by(BillingPayment.paid_at.desc(), BillingPayment.id.desc())
        .limit(limit)
        .all()
    )
    history: List[Dict[str, Any]] = []
    for row in rows:
        plan_name = "—"
        if row.subscription_id:
            sub = (
                db.query(BillingSubscription)
                .filter(BillingSubscription.id == row.subscription_id)
                .first()
            )
            if sub and sub.plan_id:
                plan = db.query(BillingPlan).filter(BillingPlan.id == sub.plan_id).first()
                if plan:
                    plan_name = (plan.extra_metadata or {}).get("name_ar") or plan.name
        history.append({
            "paid_at":    _iso(row.paid_at),
            "plan_name":  plan_name,
            "amount_sar": row.amount_sar,
            "status":     row.status,
            "gateway":    row.gateway or "unknown",
        })
    return history


def _load_plan_row(db: Session, sub) -> tuple:
    from models import BillingPlan  # noqa: PLC0415

    if not sub or not sub.plan_id:
        return None, "", ""
    plan = db.query(BillingPlan).filter(BillingPlan.id == sub.plan_id).first()
    if not plan:
        return None, "", ""
    meta = plan.extra_metadata or {}
    return plan, meta.get("name_ar") or plan.name, plan.slug


def _lifecycle_headline_ar(
    lifecycle_status: str,
    *,
    plan_name: str,
    trial_end: Optional[str],
    subscription_end: Optional[str],
    gift_end: Optional[str] = None,
    gift_permanent: bool = False,
) -> str:
    trial_date = (trial_end or "")[:10] or "—"
    sub_date = (subscription_end or "")[:10] or "—"
    gift_date = (gift_end or "")[:10] or "—"
    plan = plan_name or "الباقة"

    if lifecycle_status == "trial_pending_whatsapp":
        return "تجربتك المجانية لم تبدأ بعد — اربط واتساب لبدء التجربة المجانية"
    if lifecycle_status == "trial_active":
        return f"أنت الآن في التجربة المجانية — تنتهي بتاريخ: {trial_date}"
    if lifecycle_status == "trial_expired":
        return f"انتهت تجربتك المجانية بتاريخ: {trial_date} — اختر خطة للاشتراك ومتابعة تشغيل موظف المبيعات الذكي"
    if lifecycle_status == "gift_active":
        if gift_permanent or not gift_end:
            return f"تم تفعيل باقة {plan} كهدية دائمة بلا تاريخ انتهاء."
        return (
            f"تم تفعيل باقة {plan} كهدية حتى {gift_date}"
            " — يمكنك استخدام مزايا الباقة خلال فترة الهدية."
        )
    if lifecycle_status == "paid_active":
        return f"اشتراكك في باقة {plan} نشط — ينتهي بتاريخ: {sub_date}"
    if lifecycle_status == "paid_expired":
        return f"انتهى اشتراكك في باقة {plan} بتاريخ: {sub_date} — يرجى التجديد لاستمرار الردود الذكية وموظف المبيعات الذكي"
    return ""


def _lifecycle_status_label_ar(lifecycle_status: str) -> str:
    return {
        "trial_pending_whatsapp": "بانتظار ربط واتساب",
        "trial_active":           "تجربة مجانية",
        "trial_expired":          "انتهت التجربة المجانية",
        "gift_active":            "هدية",
        "paid_active":            "نشط",
        "paid_expired":           "منتهي",
    }.get(lifecycle_status, "—")


def resolve_billing_lifecycle(
    db: Session,
    tenant_id: int,
    tenant,
    *,
    active_sub=None,
) -> Dict[str, Any]:
    """Decide the merchant-facing lifecycle state for any tenant (trial vs paid, active vs expired)."""
    from core.billing import has_billing_access  # noqa: PLC0415

    trial_info = compute_trial_info(tenant)
    latest_paid_sub = get_latest_paid_subscription(db, tenant_id)
    payments = get_payment_history(db, tenant_id, limit=1)
    has_paid_history = latest_paid_sub is not None or bool(payments)

    record_sub = active_sub or latest_paid_sub
    _, plan_name_ar, plan_slug = _load_plan_row(db, record_sub)

    now = datetime.now(timezone.utc)
    sub_started = _coerce_utc(record_sub.started_at) if record_sub else None
    sub_ends = _effective_sub_ends_at(record_sub) if record_sub else None
    sub_expired = bool(sub_ends and sub_ends <= now) if record_sub else False
    gift_end_iso: Optional[str] = None
    gift_permanent = False

    if active_sub:
        lifecycle_status = "paid_active"
        days_remaining = _days_until(sub_ends)
        warning_level = _warning_level(days_remaining, expired=False)
        is_trial = False
        trial_expired = False
        subscription_expired = False
        has_subscription = True
    else:
        from core.manual_billing_grant import (  # noqa: PLC0415
            _read_grant_blob,
            get_manual_gift_grant_plan_slug,
            is_manual_gift_grant_active,
            is_permanent_gift_blob,
        )
        from core.plan_entitlements import PLAN_DEFINITIONS  # noqa: PLC0415

        if is_manual_gift_grant_active(db, tenant_id):
            gift_plan_slug = get_manual_gift_grant_plan_slug(db, tenant_id)
            gift_def = PLAN_DEFINITIONS.get(gift_plan_slug) or PLAN_DEFINITIONS["starter"]
            plan_name_ar = gift_def.name_ar
            plan_slug = gift_plan_slug
            blob = _read_grant_blob(db, tenant_id) or {}
            gift_end_iso = blob.get("ends_at")
            gift_permanent = is_permanent_gift_blob(blob)
            gift_ends_dt = _coerce_utc(gift_end_iso) if gift_end_iso else None
            lifecycle_status = "gift_active"
            days_remaining = (
                0 if gift_permanent else (_days_until(gift_ends_dt) if gift_ends_dt else 0)
            )
            warning_level = "none"
            is_trial = False
            trial_expired = False
            subscription_expired = False
            has_subscription = False
        elif has_paid_history and record_sub:
            lifecycle_status = "paid_expired"
            days_remaining = 0
            warning_level = "expired"
            is_trial = False
            trial_expired = False
            subscription_expired = True
            has_subscription = False
        elif trial_info.get("trial_pending_whatsapp"):
            lifecycle_status = "trial_pending_whatsapp"
            days_remaining = 0
            warning_level = "none"
            is_trial = False
            trial_expired = False
            subscription_expired = False
            has_subscription = False
        elif trial_info.get("is_trial"):
            lifecycle_status = "trial_active"
            days_remaining = trial_info["trial_days_remaining"]
            warning_level = trial_info.get("warning_level", "none")
            is_trial = True
            trial_expired = False
            subscription_expired = False
            has_subscription = False
        elif trial_info.get("trial_expired"):
            lifecycle_status = "trial_expired"
            days_remaining = 0
            warning_level = "expired"
            is_trial = False
            trial_expired = True
            subscription_expired = False
            has_subscription = False
        else:
            lifecycle_status = "trial_expired"
            days_remaining = 0
            warning_level = "expired"
            is_trial = False
            trial_expired = True
            subscription_expired = False
            has_subscription = False

    last_payment = payments[0] if payments else None
    payment_provider = "unknown"
    if last_payment:
        payment_provider = last_payment.get("gateway") or "unknown"
    elif record_sub:
        payment_provider = (record_sub.extra_metadata or {}).get("gateway") or "moyasar"

    headline = _lifecycle_headline_ar(
        lifecycle_status,
        plan_name=plan_name_ar,
        trial_end=trial_info.get("trial_end"),
        subscription_end=_iso(sub_ends),
        gift_end=gift_end_iso,
        gift_permanent=gift_permanent,
    )

    expired_since_days = 0
    if lifecycle_status == "paid_expired" and sub_ends:
        expired_since_days = _days_since(sub_ends)
    elif lifecycle_status == "trial_expired":
        trial_end_dt = _coerce_utc(getattr(tenant, "trial_ends_at", None))
        if trial_end_dt:
            expired_since_days = _days_since(trial_end_dt)

    return {
        "lifecycle_status":            lifecycle_status,
        "lifecycle_status_label_ar":   _lifecycle_status_label_ar(lifecycle_status),
        "headline_ar":                 headline,
        "plan_name":                   plan_name_ar or None,
        "plan_slug":                   plan_slug or None,
        "has_subscription":            has_subscription,
        "is_trial":                    is_trial,
        "trial_pending_whatsapp":      lifecycle_status == "trial_pending_whatsapp",
        "trial_expired":               trial_expired,
        "trial_days_remaining":        trial_info["trial_days_remaining"] if is_trial else 0,
        "trial_started_at":            trial_info.get("trial_started_at"),
        "trial_ends_at":               trial_info.get("trial_end"),
        "subscription_started_at":     _iso(sub_started),
        "subscription_ends_at":        _iso(sub_ends),
        "subscription_expired":        subscription_expired,
        "days_remaining":              days_remaining,
        "expired_since_days":          expired_since_days,
        "warning_level":               warning_level,
        "status_reason_ar":            headline,
        "has_paid_subscription_history": has_paid_history,
        "last_payment_at":             last_payment.get("paid_at") if last_payment else None,
        "last_payment_amount":         last_payment.get("amount_sar") if last_payment else 0,
        "payment_provider":            payment_provider,
        "payment_history":             get_payment_history(db, tenant_id),
        "ai_auto_replies_allowed":     (
            has_billing_access(db, tenant_id)
            and _conversation_quota_allows_service(db, tenant_id)
        ),
        "manual_replies_allowed":      True,
        "whatsapp_connected":          _tenant_has_connected_whatsapp(db, tenant_id),
        "record_sub":                  record_sub,
        "active_sub":                  active_sub,
    }


def _tenant_salla_integration(db: Session, tenant_id: int):
    from models import Integration  # noqa: PLC0415

    return (
        db.query(Integration)
        .filter(
            Integration.tenant_id == tenant_id,
            Integration.provider == "salla",
        )
        .first()
    )


def tenant_has_salla_integration(db: Session, tenant_id: int) -> bool:
    """True when the merchant installed Nahla via Salla (regardless of billing fields)."""
    return _tenant_salla_integration(db, tenant_id) is not None


def _salla_app_store_url() -> str:
    app_id = os.getenv("SALLA_APP_ID", os.getenv("SALLA_CLIENT_ID", ""))
    return os.getenv(
        "SALLA_APP_STORE_URL",
        f"https://s.salla.sa/apps/{app_id}" if app_id else "https://s.salla.sa/apps/nahla",
    )


def resolve_billing_renewal_info(
    db: Session,
    tenant_id: int,
    lifecycle: Dict[str, Any],
) -> Dict[str, Any]:
    """
    Decide how a merchant should renew — Salla App Store vs Nahla direct checkout.

    Platform-wide: do not assume every merchant is Salla-managed.
    """
    salla_integ = _tenant_salla_integration(db, tenant_id)
    is_salla_managed = bool(salla_integ)

    payment_provider = str(lifecycle.get("payment_provider") or "unknown").lower()
    has_paid_history = bool(lifecycle.get("has_paid_subscription_history"))

    if is_salla_managed:
        billing_channel = "salla"
        renewal_method = "salla_app"
        can_renew_directly = False
        renewal_url = _salla_app_store_url()
    elif payment_provider == "moyasar":
        billing_channel = "moyasar"
        renewal_method = "direct_checkout"
        can_renew_directly = True
        renewal_url = "/billing"
    elif payment_provider == "manual":
        billing_channel = "manual"
        renewal_method = "direct_checkout"
        can_renew_directly = True
        renewal_url = "/billing"
    elif has_paid_history:
        billing_channel = "direct"
        renewal_method = "direct_checkout"
        can_renew_directly = True
        renewal_url = "/billing"
    else:
        billing_channel = "unknown"
        renewal_method = "direct_checkout"
        can_renew_directly = True
        renewal_url = "/billing"

    return {
        "billing_channel":      billing_channel,
        "renewal_method":       renewal_method,
        "can_renew_directly":   can_renew_directly,
        "renewal_url":          renewal_url,
        "is_salla_managed":     is_salla_managed,
        "campaigns_automations_allowed": lifecycle.get("ai_auto_replies_allowed", False),
    }


def build_billing_status_payload(
    db: Session,
    tenant_id: int,
    tenant,
    *,
    active_sub,
    conversations_used: int,
    usage_data: Dict[str, Any],
    integration_fee_sar: int,
) -> Dict[str, Any]:
    """Unified billing/status response for every merchant on the dashboard."""
    from core.billing import is_launch_discount_active  # noqa: PLC0415

    lifecycle = resolve_billing_lifecycle(db, tenant_id, tenant, active_sub=active_sub)
    record_sub = lifecycle.pop("record_sub")
    active_sub = lifecycle.pop("active_sub")

    payload: Dict[str, Any] = {
        "conversations_used":      conversations_used,
        "conversations_limit":     usage_data["conversations_limit"],
        "usage_pct":               usage_data["usage_pct"],
        "conversations_exceeded":  usage_data["exceeded"],
        "integration_fee_sar":     integration_fee_sar,
        "launch_discount_active":  False,
        "current_price_sar":       0,
        "plan":                    None,
        "status":                  lifecycle["lifecycle_status"],
        "started_at":              lifecycle.get("subscription_started_at"),
        **lifecycle,
        **{
            k: usage_data[k]
            for k in (
                "period_mode",
                "period_started_at",
                "period_ends_at",
                "current_period_started_at",
                "current_period_ends_at",
                "subscription_id",
                "lifetime_conversations_used",
                "today_conversations_count",
                "today_billable_conversations_count",
                "today_in_period_conversations_count",
                "today_messages_count",
                "today_pre_renewal_conversations_count",
                "analytics_timezone",
                "metric_kind_period_usage",
                "metric_kind_today_conversations",
                "metric_kind_today_messages",
                "remaining_conversations",
                "current_period_conversations_used",
                "current_period_conversations_limit",
            )
            if k in usage_data
        },
    }

    sub_for_plan = active_sub or record_sub
    if sub_for_plan:
        plan, plan_name_ar, _slug = _load_plan_row(db, sub_for_plan)
        if plan:
            meta = plan.extra_metadata or {}
            launch = is_launch_discount_active(sub_for_plan) if active_sub else False
            price = meta.get("launch_price_sar", plan.price_sar) if launch else plan.price_sar
            payload["plan"] = {
                "id":               plan.id,
                "slug":             plan.slug,
                "name":             plan.name,
                "name_ar":          plan_name_ar,
                "price_sar":        plan.price_sar,
                "launch_price_sar": meta.get("launch_price_sar", plan.price_sar),
                "features":         plan.features or [],
                "limits":           plan.limits or {},
            }
            payload["launch_discount_active"] = launch
            payload["current_price_sar"] = int(price)

    payload.update(resolve_billing_renewal_info(db, tenant_id, lifecycle))

    from core.billing_override import partner_testing_override_status  # noqa: PLC0415

    payload.update(partner_testing_override_status(db, tenant_id))

    from core.manual_billing_grant import manual_gift_grant_status  # noqa: PLC0415

    payload.update(manual_gift_grant_status(db, tenant_id))

    return payload


def audit_tenant_subscription(db: Session, tenant_id: int) -> Dict[str, Any]:
    """
    Read-only audit snapshot for any merchant's billing / trial state.

    Used for operator review after deploy and regression tests. Individual
    tenant IDs in tests (e.g. production regression examples) are fixtures
    only — the resolver applies platform-wide to every merchant.
    """
    from models import Tenant  # noqa: PLC0415

    tenant = db.query(Tenant).filter(Tenant.id == tenant_id).first()
    if not tenant:
        return {"tenant_id": tenant_id, "found": False}

    active_sub = get_tenant_subscription(db, tenant_id)
    latest_paid = get_latest_paid_subscription(db, tenant_id)
    lifecycle = resolve_billing_lifecycle(db, tenant_id, tenant, active_sub=active_sub)
    renewal = resolve_billing_renewal_info(db, tenant_id, lifecycle)
    trial = compute_trial_info(tenant)

    first_wa = _coerce_utc(getattr(tenant, "first_whatsapp_connected_at", None))
    if not first_wa:
        first_wa = _first_whatsapp_connection_at(db, tenant_id)

    raw_sub = active_sub or latest_paid

    try:
        from core.wa_usage import get_usage_this_month  # noqa: PLC0415
        usage_snapshot = get_usage_this_month(db, tenant_id)
    except Exception:
        usage_snapshot = {}

    return {
        "tenant_id": tenant_id,
        "found": True,
        "store_name": tenant.name,
        "tenant_created_at": tenant.created_at.isoformat() if tenant.created_at else None,
        "billing_channel": renewal.get("billing_channel"),
        "whatsapp_connected": lifecycle["whatsapp_connected"],
        "whatsapp_status": "connected" if lifecycle["whatsapp_connected"] else "not_connected",
        "first_whatsapp_connected_at": _iso(first_wa),
        "trial_started_at": tenant.trial_started_at.isoformat() if tenant.trial_started_at else None,
        "trial_ends_at": tenant.trial_ends_at.isoformat() if tenant.trial_ends_at else None,
        "subscription_started_at": lifecycle.get("subscription_started_at"),
        "subscription_ends_at": lifecycle.get("subscription_ends_at"),
        "days_remaining": lifecycle.get("days_remaining"),
        "expired_since_days": lifecycle.get("expired_since_days"),
        "subscription_status": tenant.subscription_status,
        "billing_subscription_status": raw_sub.status if raw_sub else None,
        "lifecycle_status": lifecycle["lifecycle_status"],
        "plan_name": lifecycle.get("plan_name"),
        "has_paid_subscription_history": lifecycle.get("has_paid_subscription_history"),
        "last_payment_at": lifecycle.get("last_payment_at"),
        "latest_payment_date": lifecycle.get("last_payment_at"),
        "last_payment_amount": lifecycle.get("last_payment_amount"),
        "latest_payment_amount": lifecycle.get("last_payment_amount"),
        "payment_provider": lifecycle.get("payment_provider"),
        "payment_history": lifecycle.get("payment_history"),
        "trial_info": trial,
        "subscription_expired": lifecycle["subscription_expired"],
        "trial_expired": lifecycle["trial_expired"],
        "ai_auto_replies_allowed": lifecycle["ai_auto_replies_allowed"],
        "campaigns_automations_allowed": renewal.get("campaigns_automations_allowed"),
        "paid_subscription_effective": lifecycle["has_subscription"],
        "manual_replies_allowed": lifecycle["manual_replies_allowed"],
        "dashboard_access_allowed": True,
        "is_salla_managed": renewal.get("is_salla_managed"),
        "renewal_method": renewal.get("renewal_method"),
        "usage_period_mode": usage_snapshot.get("period_mode"),
        "period_started_at": usage_snapshot.get("period_started_at"),
        "period_ends_at": usage_snapshot.get("period_ends_at"),
        "conversations_used_this_period": usage_snapshot.get("conversations_used"),
        "conversations_limit_this_period": usage_snapshot.get("conversations_limit"),
        "lifetime_conversations_used": usage_snapshot.get("lifetime_conversations_used"),
    }


def migrate_existing_tenant_trials(db: Session) -> List[Dict[str, Any]]:
    """
    Correct trial state for existing tenants.

    Rules:
      • Never connected WhatsApp → trial_pending_whatsapp, clear trial dates
      • Has active paid sub     → leave trial dates alone
      • Connected WhatsApp      → anchor trial to first connection if missing/wrong
    """
    from models import Tenant  # noqa: PLC0415

    changes: List[Dict[str, Any]] = []
    tenants = db.query(Tenant).filter(Tenant.is_platform_tenant == False).all()  # noqa: E712

    for tenant in tenants:
        before = {
            "subscription_status": tenant.subscription_status,
            "trial_started_at": tenant.trial_started_at,
            "trial_ends_at": tenant.trial_ends_at,
            "first_whatsapp_connected_at": getattr(tenant, "first_whatsapp_connected_at", None),
        }

        if get_tenant_subscription(db, tenant.id):
            first_wa = _first_whatsapp_connection_at(db, tenant.id)
            if first_wa and not tenant.first_whatsapp_connected_at:
                tenant.first_whatsapp_connected_at = first_wa.replace(tzinfo=None)
            if before != {
                "subscription_status": tenant.subscription_status,
                "trial_started_at": tenant.trial_started_at,
                "trial_ends_at": tenant.trial_ends_at,
                "first_whatsapp_connected_at": tenant.first_whatsapp_connected_at,
            }:
                changes.append(_change_row(tenant.id, before, tenant, "paid_sub_preserve"))
            continue

        if not _tenant_has_connected_whatsapp(db, tenant.id):
            tenant.subscription_status = TRIAL_STATUS_PENDING_WHATSAPP
            tenant.trial_started_at = None
            tenant.trial_ends_at = None
            tenant.first_whatsapp_connected_at = None
        else:
            first_wa = _first_whatsapp_connection_at(db, tenant.id)
            if not first_wa:
                continue

            if not tenant.first_whatsapp_connected_at:
                tenant.first_whatsapp_connected_at = first_wa.replace(tzinfo=None)

            trial_started = _coerce_utc(tenant.trial_started_at)
            needs_reanchor = (
                trial_started is None
                or trial_started < first_wa - timedelta(minutes=5)
            )

            if needs_reanchor:
                tenant.trial_started_at = first_wa.replace(tzinfo=None)
                tenant.trial_ends_at = (
                    first_wa + timedelta(days=FREE_TRIAL_DAYS)
                ).replace(tzinfo=None)

            now = datetime.now(timezone.utc)
            trial_end = _coerce_utc(tenant.trial_ends_at)
            if trial_end and trial_end > now:
                tenant.subscription_status = TRIAL_STATUS_ACTIVE
            elif trial_end:
                tenant.subscription_status = TRIAL_STATUS_EXPIRED

        after = {
            "subscription_status": tenant.subscription_status,
            "trial_started_at": tenant.trial_started_at,
            "trial_ends_at": tenant.trial_ends_at,
            "first_whatsapp_connected_at": tenant.first_whatsapp_connected_at,
        }
        if before != after:
            changes.append(_change_row(tenant.id, before, tenant, "migrated"))

    if changes:
        db.commit()
        for row in changes:
            logger.info("[TrialLifecycle] migrated tenant=%s action=%s", row["tenant_id"], row["action"])

    return changes


def _change_row(tenant_id: int, before: dict, tenant, action: str) -> Dict[str, Any]:
    return {
        "tenant_id": tenant_id,
        "action": action,
        "before": {k: (v.isoformat() if hasattr(v, "isoformat") else v) for k, v in before.items()},
        "after": {
            "subscription_status": tenant.subscription_status,
            "trial_started_at": tenant.trial_started_at.isoformat() if tenant.trial_started_at else None,
            "trial_ends_at": tenant.trial_ends_at.isoformat() if tenant.trial_ends_at else None,
            "first_whatsapp_connected_at": (
                tenant.first_whatsapp_connected_at.isoformat()
                if getattr(tenant, "first_whatsapp_connected_at", None)
                else None
            ),
        },
    }

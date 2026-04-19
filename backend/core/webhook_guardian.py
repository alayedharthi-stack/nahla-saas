"""
core/webhook_guardian.py
─────────────────────────
WhatsApp Webhook Reliability System — Guardian Background Worker

Responsibilities:
  1. Every 5 minutes: scan all active WhatsApp connections.
  2. Detect CRITICAL errors: webhook_verified=false while status=connected.
  3. Detect IDLE connections: no inbound event for >15 min while connected+sending_enabled.
  4. Auto-resubscribe broken connections via Meta subscribed_apps API.
  5. After each deployment, verify all merchant WABAs are still subscribed.
  6. Log every action to webhook_guardian_log (structured) + nahla.audit (text).

Deployment health check (run once at startup):
  - Verify platform WABA + all merchant WABAs are subscribed.
  - Re-subscribe any that are missing without operator intervention.
"""
from __future__ import annotations

import asyncio
from dataclasses import dataclass
import logging
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Optional

import httpx

logger = logging.getLogger("nahla.webhook_guardian")

_GUARDIAN_INTERVAL_SECONDS = 300       # run every 5 minutes
_IDLE_THRESHOLD_MINUTES    = 15        # classify as idle after 15 min silence
_STARTUP_DELAY_SECONDS     = 60        # wait for app to be fully up before first check
_META_GRAPH_VERSION        = "v19.0"   # overridden from config at runtime


@dataclass
class SubscriptionAttemptResult:
    success: bool
    subscribe_target: Optional[str]
    connection_type: Optional[str]
    token_source: Optional[str]
    waba_id: Optional[str]
    attempted_fallback: bool = False
    fallback_succeeded: bool = False
    status_code: Optional[int] = None
    error: Optional[str] = None


# ═══════════════════════════════════════════════════════════════════════════════
# Public entry points called from scheduler.py
# ═══════════════════════════════════════════════════════════════════════════════

async def run_webhook_guardian() -> None:
    """Main guardian loop — runs forever, wakes every 5 minutes."""
    await asyncio.sleep(_STARTUP_DELAY_SECONDS)
    logger.info(
        "[Guardian] Started — scanning every %ds, idle threshold=%dmin",
        _GUARDIAN_INTERVAL_SECONDS, _IDLE_THRESHOLD_MINUTES,
    )
    while True:
        try:
            await _scan_all_connections()
        except Exception as exc:
            logger.error("[Guardian] Unhandled error in scan cycle: %s", exc, exc_info=True)
        await asyncio.sleep(_GUARDIAN_INTERVAL_SECONDS)


async def run_startup_webhook_health_check() -> None:
    """
    Called once on startup (lifespan).
    Verifies the platform WABA and all merchant WABAs are subscribed.
    Runs in background so it never delays the healthcheck endpoint.
    """
    await asyncio.sleep(30)  # let DB migrations settle
    logger.info("[Guardian] Running startup webhook health check …")
    try:
        await _check_platform_waba()
        await _check_all_merchant_wabas()
    except Exception as exc:
        logger.error("[Guardian] Startup health check error: %s", exc, exc_info=True)


# ═══════════════════════════════════════════════════════════════════════════════
# Core scan
# ═══════════════════════════════════════════════════════════════════════════════

async def _scan_all_connections() -> None:
    """Inspect every connected WhatsApp tenant and remediate issues."""
    from core.database import SessionLocal  # noqa: PLC0415
    from database.models import WhatsAppConnection  # noqa: PLC0415

    try:
        db = SessionLocal()
    except Exception as exc:
        logger.error("[Guardian] Cannot open DB session: %s", exc)
        return

    try:
        connections: List[WhatsAppConnection] = (
            db.query(WhatsAppConnection)
            .filter(WhatsAppConnection.status == "connected")
            .all()
        )

        now = datetime.now(timezone.utc)
        idle_cutoff = now - timedelta(minutes=_IDLE_THRESHOLD_MINUTES)

        critical = 0
        idle     = 0
        healthy  = 0

        for conn in connections:
            try:
                health = await _inspect_connection(db, conn, now, idle_cutoff)
                if health == "critical":
                    critical += 1
                elif health == "idle":
                    idle += 1
                else:
                    healthy += 1
            except Exception as exc:
                logger.error(
                    "[Guardian] Error inspecting tenant=%s: %s", conn.tenant_id, exc,
                )

        logger.info(
            "[Guardian] Scan complete — total=%d healthy=%d idle=%d critical=%d",
            len(connections), healthy, idle, critical,
        )
    finally:
        try:
            db.close()
        except Exception:
            pass


async def _inspect_connection(db, conn, now: datetime, idle_cutoff: datetime) -> str:
    """
    Inspect one WhatsAppConnection.
    Returns one of: active | idle | critical.
    """
    tenant_id = conn.tenant_id
    phone_id  = conn.phone_number_id or "?"
    waba_id   = conn.whatsapp_business_account_id

    # ── Rule 1: CRITICAL — webhook_verified=false while status=connected ──────
    if not conn.webhook_verified and conn.status == "connected":
        logger.warning(
            "[Guardian] CRITICAL tenant=%s phone_id=%s — webhook_verified=false while connected",
            tenant_id, phone_id,
        )
        _guardian_log(db, tenant_id, phone_id, waba_id, "critical_error_detected", success=False,
                      detail="webhook_verified=false while status=connected — attempting resubscription")
        _audit("guardian_critical_error", tenant_id=tenant_id, phone_number_id=phone_id)
        result = await _resubscribe(db, conn)
        success = result.success
        _guardian_log(
            db, tenant_id, phone_id, waba_id,
            "webhook_resubscribed" if success else "webhook_verification_failed",
            success=success,
            detail=_format_guardian_detail(
                "Auto-resubscription after critical error",
                subscribe_target=result.subscribe_target,
                connection_type=result.connection_type,
                token_source=result.token_source,
                waba_id=result.waba_id,
                fallback_succeeded=result.fallback_succeeded,
                error=result.error,
            ),
        )
        if success:
            conn.webhook_verified = True
            conn.updated_at = now
            db.commit()
            return "active"
        return "critical"

    # ── Rule 2: IDLE — silence is not failure ─────────────────────────────────
    if _classify_connection_health(conn, now, idle_cutoff) == "idle":
        minutes_silent = _minutes_since_last_inbound(conn, now)
        logger.info(
            "[Guardian] IDLE tenant=%s phone_id=%s minutes_silent=%s "
            "connection_type=%s waba_id=%s",
            tenant_id,
            phone_id,
            minutes_silent if minutes_silent is not None else "never",
            _normalize_connection_type(getattr(conn, "connection_type", None)),
            waba_id,
        )
        return "idle"

    return "active"


# ═══════════════════════════════════════════════════════════════════════════════
# Startup: platform WABA + all merchant WABAs
# ═══════════════════════════════════════════════════════════════════════════════

async def _check_platform_waba() -> None:
    """
    Verify the platform-level subscription (Nahla's own number). Prefer
    PHONE_NUMBER_ID over WABA_ID per Meta Cloud API spec.
    """
    import os  # noqa: PLC0415
    from core.config import META_GRAPH_API_VERSION  # noqa: PLC0415

    token    = os.getenv("WA_TOKEN") or os.getenv("WHATSAPP_TOKEN", "")
    phone_id = os.getenv("PHONE_NUMBER_ID", "")
    waba_id  = os.getenv("WA_BUSINESS_ACCOUNT_ID", "")
    if not token or (not phone_id and not waba_id):
        logger.info("[Guardian] Platform WhatsApp not configured — skipping")
        return

    subscribed = await _check_subscribed(
        phone_id,
        waba_id,
        token,
        META_GRAPH_API_VERSION,
        connection_type="direct",
        token_source="platform",
        tenant_id=None,
    )
    if subscribed:
        logger.info(
            "[Guardian] Platform WhatsApp phone=%s waba=%s — subscription OK",
            phone_id, waba_id,
        )
        return

    logger.warning(
        "[Guardian] Platform WhatsApp phone=%s waba=%s not subscribed — resubscribing …",
        phone_id, waba_id,
    )
    result = await _subscribe_phone(
        phone_id,
        waba_id,
        token,
        META_GRAPH_API_VERSION,
        connection_type="direct",
        token_source="platform",
        tenant_id=None,
    )
    logger.info(
        "[Guardian] Platform resubscription: %s | subscribe_target=%s token_source=%s "
        "fallback_succeeded=%s",
        "OK" if result.success else "FAILED",
        result.subscribe_target,
        result.token_source,
        result.fallback_succeeded,
    )


async def _check_all_merchant_wabas() -> None:
    """Verify every merchant's WABA is subscribed; resubscribe if missing."""
    from core.database import SessionLocal  # noqa: PLC0415
    from database.models import WhatsAppConnection  # noqa: PLC0415
    from core.config import META_GRAPH_API_VERSION  # noqa: PLC0415

    try:
        db = SessionLocal()
    except Exception as exc:
        logger.error("[Guardian] Cannot open DB for startup check: %s", exc)
        return

    try:
        conns: List[WhatsAppConnection] = (
            db.query(WhatsAppConnection)
            .filter(
                WhatsAppConnection.status == "connected",
                WhatsAppConnection.access_token.isnot(None),
                WhatsAppConnection.whatsapp_business_account_id.isnot(None),
            )
            .all()
        )
        logger.info("[Guardian] Startup check: %d merchant WABAs to verify", len(conns))

        ok_count = 0
        fixed_count = 0
        fail_count = 0

        for conn in conns:
            try:
                waba_id  = conn.whatsapp_business_account_id
                phone_id = conn.phone_number_id
                from services.whatsapp_platform.token_manager import get_token_context  # noqa: PLC0415

                token_ctx = get_token_context(conn)
                token = token_ctx.token
                if not token or (not waba_id and not phone_id):
                    logger.warning(
                        "[Guardian] Startup: tenant=%s missing usable token or ids | "
                        "connection_type=%s token_source=%s waba_id=%s phone_id=%s",
                        conn.tenant_id,
                        _normalize_connection_type(getattr(conn, "connection_type", None)),
                        token_ctx.source,
                        waba_id,
                        phone_id,
                    )
                    fail_count += 1
                    continue

                subscribed = await _check_subscribed(
                    phone_id,
                    waba_id,
                    token,
                    META_GRAPH_API_VERSION,
                    connection_type=getattr(conn, "connection_type", None),
                    token_source=token_ctx.source,
                    tenant_id=conn.tenant_id,
                )
                if subscribed:
                    if not conn.webhook_verified:
                        conn.webhook_verified = True
                        db.commit()
                    ok_count += 1
                    continue

                # Not subscribed → resubscribe using the connection's target order.
                logger.warning(
                    "[Guardian] Startup: tenant=%s phone=%s waba=%s not subscribed — resubscribing",
                    conn.tenant_id, phone_id, waba_id,
                )
                result = await _subscribe_phone(
                    phone_id,
                    waba_id,
                    token,
                    META_GRAPH_API_VERSION,
                    connection_type=getattr(conn, "connection_type", None),
                    token_source=token_ctx.source,
                    tenant_id=conn.tenant_id,
                )
                success = result.success
                _guardian_log(
                    db, conn.tenant_id, phone_id, waba_id,
                    "webhook_subscribed" if success else "webhook_verification_failed",
                    success=success,
                    detail=_format_guardian_detail(
                        "Startup health check resubscription",
                        subscribe_target=result.subscribe_target,
                        connection_type=result.connection_type,
                        token_source=result.token_source,
                        waba_id=result.waba_id,
                        fallback_succeeded=result.fallback_succeeded,
                        error=result.error,
                    ),
                )
                _audit(
                    "guardian_startup_resubscribed",
                    tenant_id=conn.tenant_id,
                    waba_id=waba_id,
                    success=success,
                )
                if success:
                    conn.webhook_verified = True
                    conn.updated_at = datetime.now(timezone.utc)
                    db.commit()
                    fixed_count += 1
                else:
                    fail_count += 1
            except Exception as exc:
                logger.error(
                    "[Guardian] Startup check error for tenant=%s: %s", conn.tenant_id, exc,
                )
                fail_count += 1

        logger.info(
            "[Guardian] Startup check done — ok=%d fixed=%d failed=%d",
            ok_count, fixed_count, fail_count,
        )
    finally:
        try:
            db.close()
        except Exception:
            pass


# ═══════════════════════════════════════════════════════════════════════════════
# Meta API helpers
# ═══════════════════════════════════════════════════════════════════════════════

async def _check_subscribed(
    phone_number_id: Optional[str],
    waba_id: Optional[str],
    token: str,
    graph_version: str,
    *,
    connection_type: Optional[str] = None,
    token_source: Optional[str] = None,
    tenant_id: Optional[int] = None,
) -> bool:
    """
    GET /{target_id}/subscribed_apps using the connection's correct target order
    and return True if our app_id appears.
    Falls back to True on any API error so we don't erroneously resubscribe.
    """
    import os  # noqa: PLC0415
    app_id = os.getenv("META_APP_ID", "")

    targets = _subscription_targets(connection_type, phone_number_id, waba_id)
    if not targets:
        return False

    attempted_fallback = False
    for idx, (target_kind, target_id) in enumerate(targets):
        url = f"https://graph.facebook.com/{graph_version}/{target_id}/subscribed_apps"
        fallback_target = targets[idx + 1][0] if idx + 1 < len(targets) else None
        try:
            async with httpx.AsyncClient(timeout=10) as client:
                resp = await client.get(url, headers={"Authorization": f"Bearer {token}"})
            if resp.status_code == 200:
                data = resp.json()
                apps: List[Dict[str, Any]] = data.get("data", [])
                subscribed = (
                    bool(apps)
                    if not app_id
                    else any(str(a.get("id") or a.get("app_id", "")) == app_id for a in apps)
                )
                logger.info(
                    "[Guardian] subscribed_apps check tenant=%s subscribe_target=%s "
                    "connection_type=%s token_source=%s waba_id=%s subscribed=%s "
                    "fallback_succeeded=%s",
                    tenant_id,
                    target_kind,
                    _normalize_connection_type(connection_type),
                    token_source or "unknown",
                    waba_id,
                    subscribed,
                    attempted_fallback,
                )
                return subscribed

            body_snippet = resp.text[:300]
            if fallback_target:
                attempted_fallback = True
                logger.info(
                    "[Guardian] subscribed_apps check primary failed — tenant=%s "
                    "subscribe_target=%s fallback_target=%s connection_type=%s "
                    "token_source=%s waba_id=%s status=%s",
                    tenant_id,
                    target_kind,
                    fallback_target,
                    _normalize_connection_type(connection_type),
                    token_source or "unknown",
                    waba_id,
                    resp.status_code,
                )
                continue

            logger.warning(
                "[Guardian] subscribed_apps check failed — tenant=%s subscribe_target=%s "
                "connection_type=%s token_source=%s waba_id=%s status=%s body=%s",
                tenant_id,
                target_kind,
                _normalize_connection_type(connection_type),
                token_source or "unknown",
                waba_id,
                resp.status_code,
                body_snippet,
            )
            return False
        except Exception as exc:
            if fallback_target:
                attempted_fallback = True
                logger.info(
                    "[Guardian] subscribed_apps check exception — tenant=%s "
                    "subscribe_target=%s fallback_target=%s connection_type=%s "
                    "token_source=%s waba_id=%s err=%s",
                    tenant_id,
                    target_kind,
                    fallback_target,
                    _normalize_connection_type(connection_type),
                    token_source or "unknown",
                    waba_id,
                    exc,
                )
                continue

            logger.warning(
                "[Guardian] subscribed_apps check failed — tenant=%s subscribe_target=%s "
                "connection_type=%s token_source=%s waba_id=%s err=%s",
                tenant_id,
                target_kind,
                _normalize_connection_type(connection_type),
                token_source or "unknown",
                waba_id,
                exc,
            )
            return True  # optimistic — don't blind-resubscribe on network error

    return False


async def _subscribe_phone(
    phone_number_id: Optional[str],
    waba_id: Optional[str],
    token: str,
    graph_version: str,
    *,
    connection_type: Optional[str] = None,
    token_source: Optional[str] = None,
    tenant_id: Optional[int] = None,
) -> SubscriptionAttemptResult:
    """
    POST /{target_id}/subscribed_apps using the connection's correct target order.
    Returns a structured result so callers can log the chosen target and whether
    any fallback path was needed.
    """
    targets = _subscription_targets(connection_type, phone_number_id, waba_id)
    normalized_type = _normalize_connection_type(connection_type)
    if not targets:
        return SubscriptionAttemptResult(
            success=False,
            subscribe_target=None,
            connection_type=normalized_type,
            token_source=token_source,
            waba_id=waba_id,
            error="missing_phone_and_waba_id",
        )

    attempted_fallback = False
    for idx, (target_kind, target_id) in enumerate(targets):
        url = f"https://graph.facebook.com/{graph_version}/{target_id}/subscribed_apps"
        fallback_target = targets[idx + 1][0] if idx + 1 < len(targets) else None
        try:
            async with httpx.AsyncClient(timeout=10) as client:
                resp = await client.post(
                    url,
                    headers={"Authorization": f"Bearer {token}"},
                    json={"subscribed_fields": ["messages", "messaging_postbacks", "message_echoes"]},
                )
            data = resp.json()
            success = resp.status_code == 200 and bool(data.get("success"))
            if success:
                logger.info(
                    "[Guardian] subscribe success — tenant=%s subscribe_target=%s "
                    "connection_type=%s token_source=%s waba_id=%s fallback_succeeded=%s",
                    tenant_id,
                    target_kind,
                    normalized_type,
                    token_source or "unknown",
                    waba_id,
                    attempted_fallback,
                )
                return SubscriptionAttemptResult(
                    success=True,
                    subscribe_target=target_kind,
                    connection_type=normalized_type,
                    token_source=token_source,
                    waba_id=waba_id,
                    attempted_fallback=attempted_fallback,
                    fallback_succeeded=attempted_fallback,
                    status_code=resp.status_code,
                )

            err_msg = (data.get("error") or {}).get("message") or f"HTTP {resp.status_code}"
            if fallback_target:
                attempted_fallback = True
                logger.info(
                    "[Guardian] subscribe primary failed — tenant=%s subscribe_target=%s "
                    "fallback_target=%s connection_type=%s token_source=%s "
                    "waba_id=%s status=%s error=%s",
                    tenant_id,
                    target_kind,
                    fallback_target,
                    normalized_type,
                    token_source or "unknown",
                    waba_id,
                    resp.status_code,
                    err_msg,
                )
                continue

            logger.warning(
                "[Guardian] subscribe failed — tenant=%s subscribe_target=%s "
                "connection_type=%s token_source=%s waba_id=%s status=%s error=%s",
                tenant_id,
                target_kind,
                normalized_type,
                token_source or "unknown",
                waba_id,
                resp.status_code,
                err_msg,
            )
            return SubscriptionAttemptResult(
                success=False,
                subscribe_target=target_kind,
                connection_type=normalized_type,
                token_source=token_source,
                waba_id=waba_id,
                attempted_fallback=attempted_fallback,
                fallback_succeeded=False,
                status_code=resp.status_code,
                error=err_msg,
            )
        except Exception as exc:
            if fallback_target:
                attempted_fallback = True
                logger.info(
                    "[Guardian] subscribe primary exception — tenant=%s subscribe_target=%s "
                    "fallback_target=%s connection_type=%s token_source=%s "
                    "waba_id=%s err=%s",
                    tenant_id,
                    target_kind,
                    fallback_target,
                    normalized_type,
                    token_source or "unknown",
                    waba_id,
                    exc,
                )
                continue

            logger.error(
                "[Guardian] subscribe request failed — tenant=%s subscribe_target=%s "
                "connection_type=%s token_source=%s waba_id=%s err=%s",
                tenant_id,
                target_kind,
                normalized_type,
                token_source or "unknown",
                waba_id,
                exc,
            )
            return SubscriptionAttemptResult(
                success=False,
                subscribe_target=target_kind,
                connection_type=normalized_type,
                token_source=token_source,
                waba_id=waba_id,
                attempted_fallback=attempted_fallback,
                fallback_succeeded=False,
                error=str(exc),
            )

    return SubscriptionAttemptResult(
        success=False,
        subscribe_target=targets[-1][0] if targets else None,
        connection_type=normalized_type,
        token_source=token_source,
        waba_id=waba_id,
        attempted_fallback=attempted_fallback,
        fallback_succeeded=False,
        error="subscription_failed",
    )


async def _resubscribe(db, conn) -> SubscriptionAttemptResult:
    """
    Attempt to resubscribe the given WhatsAppConnection.
    Handles missing token / phone_number_id / WABA ID gracefully.
    """
    from core.config import META_GRAPH_API_VERSION  # noqa: PLC0415
    from services.whatsapp_platform.token_manager import get_token_context  # noqa: PLC0415

    waba_id  = conn.whatsapp_business_account_id
    phone_id = conn.phone_number_id
    token_ctx = get_token_context(conn)
    token    = token_ctx.token

    if not phone_id and not waba_id:
        logger.warning(
            "[Guardian] tenant=%s has no phone_number_id or WABA ID — cannot resubscribe",
            conn.tenant_id,
        )
        return SubscriptionAttemptResult(
            success=False,
            subscribe_target=None,
            connection_type=_normalize_connection_type(getattr(conn, "connection_type", None)),
            token_source=token_ctx.source,
            waba_id=waba_id,
            error="missing_phone_and_waba_id",
        )
    if not token:
        logger.warning(
            "[Guardian] tenant=%s has no usable token — cannot resubscribe | token_source=%s",
            conn.tenant_id,
            token_ctx.source,
        )
        return SubscriptionAttemptResult(
            success=False,
            subscribe_target=None,
            connection_type=_normalize_connection_type(getattr(conn, "connection_type", None)),
            token_source=token_ctx.source,
            waba_id=waba_id,
            error="missing_token",
        )

    return await _subscribe_phone(
        phone_id,
        waba_id,
        token,
        META_GRAPH_API_VERSION,
        connection_type=getattr(conn, "connection_type", None),
        token_source=token_ctx.source,
        tenant_id=conn.tenant_id,
    )


def _normalize_connection_type(connection_type: Optional[str]) -> str:
    raw = str(connection_type or "").strip().lower()
    return raw or "unknown"


def _subscription_targets(
    connection_type: Optional[str],
    phone_number_id: Optional[str],
    waba_id: Optional[str],
) -> List[tuple[str, str]]:
    """
    Return the ordered subscribed_apps targets for this connection.

    Embedded/coexistence connections are WABA-scoped in practice, so they must
    subscribe directly on the WABA and must not attempt phone-level first.
    Direct connections keep the historical phone-first behaviour with a WABA
    fallback only when needed.
    """
    normalized = _normalize_connection_type(connection_type)
    targets: List[tuple[str, str]] = []

    def add(kind: str, value: Optional[str]) -> None:
        if not value:
            return
        candidate = (kind, value)
        if candidate not in targets:
            targets.append(candidate)

    if normalized in {"embedded", "coexistence"}:
        add("waba", waba_id)
        if not targets:
            add("phone", phone_number_id)
        return targets

    add("phone", phone_number_id)
    add("waba", waba_id)
    return targets


def _as_utc(dt: Optional[datetime]) -> Optional[datetime]:
    if dt is None:
        return None
    if dt.tzinfo is None:
        return dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


def _minutes_since_last_inbound(conn, now: datetime) -> Optional[int]:
    last_received = _as_utc(getattr(conn, "last_webhook_received_at", None))
    if not last_received:
        return None
    return int((now - last_received).total_seconds() / 60)


def _classify_connection_health(conn, now: datetime, idle_cutoff: datetime) -> str:
    if getattr(conn, "status", None) != "connected":
        return "disconnected"
    if not getattr(conn, "webhook_verified", False):
        return "critical"
    if not getattr(conn, "sending_enabled", False):
        return "active"
    last_received = _as_utc(getattr(conn, "last_webhook_received_at", None))
    if last_received is None or last_received < idle_cutoff:
        return "idle"
    return "active"


def _health_reason(conn, now: datetime, idle_cutoff: datetime) -> str:
    health = _classify_connection_health(conn, now, idle_cutoff)
    if health == "disconnected":
        return "not_connected"
    if health == "critical":
        return "webhook_unverified"
    if health == "idle":
        minutes = _minutes_since_last_inbound(conn, now)
        return f"idle_no_inbound_{minutes if minutes is not None else 'never'}m"
    return "verified_recent_webhook_or_not_sending"


def _format_guardian_detail(
    base: str,
    *,
    subscribe_target: Optional[str] = None,
    connection_type: Optional[str] = None,
    token_source: Optional[str] = None,
    waba_id: Optional[str] = None,
    fallback_succeeded: Optional[bool] = None,
    error: Optional[str] = None,
) -> str:
    meta: List[str] = []
    if subscribe_target:
        meta.append(f"subscribe_target={subscribe_target}")
    if connection_type:
        meta.append(f"connection_type={connection_type}")
    if token_source:
        meta.append(f"token_source={token_source}")
    if waba_id:
        meta.append(f"waba_id={waba_id}")
    if fallback_succeeded is not None:
        meta.append(f"fallback_succeeded={str(fallback_succeeded).lower()}")
    if error:
        meta.append(f"error={error}")
    return f"{base} | {' '.join(meta)}" if meta else base


# ═══════════════════════════════════════════════════════════════════════════════
# Helpers: structured guardian log + audit shortcut
# ═══════════════════════════════════════════════════════════════════════════════

def _guardian_log(
    db,
    tenant_id: int,
    phone_number_id: Optional[str],
    waba_id: Optional[str],
    event: str,
    success: bool,
    detail: Optional[str] = None,
) -> None:
    """Write one row to webhook_guardian_log (best-effort, never raises)."""
    try:
        from database.models import WebhookGuardianLog  # noqa: PLC0415
        entry = WebhookGuardianLog(
            tenant_id=tenant_id,
            phone_number_id=phone_number_id,
            waba_id=waba_id,
            event=event,
            success=success,
            detail=detail,
        )
        db.add(entry)
        db.commit()
    except Exception as exc:
        logger.debug("[Guardian] Failed to write guardian log: %s", exc)
        try:
            db.rollback()
        except Exception:
            pass


def _audit(event: str, **ctx) -> None:
    """Emit an audit log line via the shared audit helper."""
    try:
        from core.audit import audit  # noqa: PLC0415
        audit(event, **ctx)
    except Exception:
        pass

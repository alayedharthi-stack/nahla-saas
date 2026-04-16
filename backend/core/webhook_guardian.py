"""
core/webhook_guardian.py
─────────────────────────
WhatsApp Webhook Reliability System — Guardian Background Worker

Responsibilities:
  1. Every 5 minutes: scan all active WhatsApp connections.
  2. Detect CRITICAL errors: webhook_verified=false while status=connected.
  3. Detect STALLED connections: no inbound event for >15 min while connected+sending_enabled.
  4. Auto-resubscribe stalled or broken connections via Meta subscribed_apps API.
  5. After each deployment, verify all merchant WABAs are still subscribed.
  6. Log every action to webhook_guardian_log (structured) + nahla.audit (text).

Deployment health check (run once at startup):
  - Verify platform WABA + all merchant WABAs are subscribed.
  - Re-subscribe any that are missing without operator intervention.
"""
from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Optional

import httpx

logger = logging.getLogger("nahla.webhook_guardian")

_GUARDIAN_INTERVAL_SECONDS = 300       # run every 5 minutes
_STALL_THRESHOLD_MINUTES   = 15        # flag as stalled after 15 min silence
_STARTUP_DELAY_SECONDS     = 60        # wait for app to be fully up before first check
_META_GRAPH_VERSION        = "v19.0"   # overridden from config at runtime


# ═══════════════════════════════════════════════════════════════════════════════
# Public entry points called from scheduler.py
# ═══════════════════════════════════════════════════════════════════════════════

async def run_webhook_guardian() -> None:
    """Main guardian loop — runs forever, wakes every 5 minutes."""
    await asyncio.sleep(_STARTUP_DELAY_SECONDS)
    logger.info(
        "[Guardian] Started — scanning every %ds, stall threshold=%dmin",
        _GUARDIAN_INTERVAL_SECONDS, _STALL_THRESHOLD_MINUTES,
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
        stall_cutoff = now - timedelta(minutes=_STALL_THRESHOLD_MINUTES)

        critical = 0
        stalled  = 0
        healthy  = 0

        for conn in connections:
            try:
                repaired = await _inspect_connection(db, conn, now, stall_cutoff)
                if repaired:
                    stalled += 1
                elif not conn.webhook_verified:
                    critical += 1
                else:
                    healthy += 1
            except Exception as exc:
                logger.error(
                    "[Guardian] Error inspecting tenant=%s: %s", conn.tenant_id, exc,
                )

        logger.info(
            "[Guardian] Scan complete — total=%d healthy=%d stalled=%d critical=%d",
            len(connections), healthy, stalled, critical,
        )
    finally:
        try:
            db.close()
        except Exception:
            pass


async def _inspect_connection(db, conn, now: datetime, stall_cutoff: datetime) -> bool:
    """
    Inspect one WhatsAppConnection. Returns True if a resubscription was triggered.
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
        success = await _resubscribe(db, conn)
        _guardian_log(
            db, tenant_id, phone_id, waba_id,
            "webhook_resubscribed" if success else "webhook_verification_failed",
            success=success,
            detail="Auto-resubscription after critical error",
        )
        if success:
            conn.webhook_verified = True
            conn.updated_at = now
            db.commit()
        return True

    # ── Rule 2: STALLED — no inbound for >15 min while sending_enabled ────────
    if conn.sending_enabled:
        last_received = conn.last_webhook_received_at
        if last_received:
            # Make naive timestamps timezone-aware for safe comparison
            if last_received.tzinfo is None:
                last_received = last_received.replace(tzinfo=timezone.utc)
            if last_received < stall_cutoff:
                minutes_silent = int((now - last_received).total_seconds() / 60)
                logger.warning(
                    "[Guardian] STALLED tenant=%s phone_id=%s — no inbound for %dmin",
                    tenant_id, phone_id, minutes_silent,
                )
                _guardian_log(
                    db, tenant_id, phone_id, waba_id, "webhook_stalled", success=False,
                    detail=f"No inbound webhook for {minutes_silent} minutes",
                )
                _audit("guardian_stalled", tenant_id=tenant_id, phone_number_id=phone_id,
                       minutes_silent=minutes_silent)
                success = await _resubscribe(db, conn)
                _guardian_log(
                    db, tenant_id, phone_id, waba_id,
                    "webhook_recovered" if success else "webhook_verification_failed",
                    success=success,
                    detail=f"Auto-resubscription after {minutes_silent}min stall",
                )
                if success:
                    conn.webhook_verified = True
                    conn.updated_at = now
                    db.commit()
                return True

    return False


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

    subscribed = await _check_subscribed(phone_id, waba_id, token, META_GRAPH_API_VERSION)
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
    ok = await _subscribe_phone(phone_id, waba_id, token, META_GRAPH_API_VERSION)
    logger.info("[Guardian] Platform resubscription: %s", "OK" if ok else "FAILED")


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
                token    = conn.access_token
                if not token or (not waba_id and not phone_id):
                    continue

                subscribed = await _check_subscribed(
                    phone_id, waba_id, token, META_GRAPH_API_VERSION,
                )
                if subscribed:
                    if not conn.webhook_verified:
                        conn.webhook_verified = True
                        db.commit()
                    ok_count += 1
                    continue

                # Not subscribed → resubscribe (phone-level, with WABA fallback)
                logger.warning(
                    "[Guardian] Startup: tenant=%s phone=%s waba=%s not subscribed — resubscribing",
                    conn.tenant_id, phone_id, waba_id,
                )
                success = await _subscribe_phone(
                    phone_id, waba_id, token, META_GRAPH_API_VERSION,
                )
                _guardian_log(
                    db, conn.tenant_id, phone_id, waba_id,
                    "webhook_subscribed" if success else "webhook_verification_failed",
                    success=success,
                    detail="Startup health check resubscription",
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
) -> bool:
    """
    GET /{phone_number_id}/subscribed_apps (preferred) or /{waba_id}/subscribed_apps
    (fallback) and return True if our app_id appears.
    Falls back to True on any API error so we don't erroneously resubscribe.
    """
    import os  # noqa: PLC0415
    app_id = os.getenv("META_APP_ID", "")

    target_id   = phone_number_id or waba_id
    target_kind = "phone" if phone_number_id else "waba"
    if not target_id:
        return False

    url = f"https://graph.facebook.com/{graph_version}/{target_id}/subscribed_apps"
    try:
        async with httpx.AsyncClient(timeout=10) as client:
            resp = await client.get(url, headers={"Authorization": f"Bearer {token}"})
        if resp.status_code != 200:
            logger.warning(
                "[Guardian] subscribed_apps GET returned %s for %s=%s",
                resp.status_code, target_kind, target_id,
            )
            # If phone-level GET 400s with "Unsupported", try WABA-level once.
            if (
                target_kind == "phone"
                and waba_id
                and resp.status_code == 400
                and "unsupported" in resp.text.lower()
            ):
                fb_url = f"https://graph.facebook.com/{graph_version}/{waba_id}/subscribed_apps"
                async with httpx.AsyncClient(timeout=10) as client:
                    fb_resp = await client.get(fb_url, headers={"Authorization": f"Bearer {token}"})
                if fb_resp.status_code == 200:
                    data = fb_resp.json()
                    apps = data.get("data", [])
                    if not app_id:
                        return bool(apps)
                    return any(str(a.get("id") or a.get("app_id", "")) == app_id for a in apps)
            return False
        data = resp.json()
        apps: List[Dict[str, Any]] = data.get("data", [])
        if not app_id:
            return bool(apps)
        return any(str(a.get("id") or a.get("app_id", "")) == app_id for a in apps)
    except Exception as exc:
        logger.warning(
            "[Guardian] subscribed_apps check failed for %s=%s: %s",
            target_kind, target_id, exc,
        )
        return True  # optimistic — don't blind-resubscribe on network error


async def _subscribe_phone(
    phone_number_id: Optional[str],
    waba_id: Optional[str],
    token: str,
    graph_version: str,
) -> bool:
    """
    POST /{phone_number_id}/subscribed_apps (preferred) or
    /{waba_id}/subscribed_apps (fallback) — returns True on success.

    Per Meta WhatsApp Cloud API docs the subscription belongs to the
    PHONE_NUMBER_ID, not the WABA_ID. The WABA endpoint frequently returns
    "Unsupported post request" for newer tokens, hence the preference order.
    """
    target_id   = phone_number_id or waba_id
    target_kind = "phone" if phone_number_id else "waba"
    if not target_id:
        return False

    url = f"https://graph.facebook.com/{graph_version}/{target_id}/subscribed_apps"
    try:
        async with httpx.AsyncClient(timeout=10) as client:
            resp = await client.post(
                url,
                headers={"Authorization": f"Bearer {token}"},
                json={"subscribed_fields": ["messages", "messaging_postbacks", "message_echoes"]},
            )
        data = resp.json()
        success = bool(data.get("success"))
        logger.info(
            "[Guardian] subscribe %s=%s status=%s result=%s",
            target_kind, target_id, resp.status_code, data,
        )
        if success:
            return True

        # Defensive fallback to WABA when phone-level 400s as "Unsupported".
        err_msg = (data.get("error") or {}).get("message", "")
        if (
            target_kind == "phone"
            and waba_id
            and resp.status_code == 400
            and "unsupported" in err_msg.lower()
        ):
            fb_url = f"https://graph.facebook.com/{graph_version}/{waba_id}/subscribed_apps"
            async with httpx.AsyncClient(timeout=10) as client:
                fb_resp = await client.post(
                    fb_url,
                    headers={"Authorization": f"Bearer {token}"},
                    json={"subscribed_fields": ["messages", "messaging_postbacks", "message_echoes"]},
                )
            fb_data = fb_resp.json()
            fb_success = bool(fb_data.get("success"))
            logger.info(
                "[Guardian] subscribe WABA fallback waba=%s status=%s result=%s",
                waba_id, fb_resp.status_code, fb_data,
            )
            return fb_success

        return False
    except Exception as exc:
        logger.error(
            "[Guardian] subscribe %s=%s failed: %s",
            target_kind, target_id, exc,
        )
        return False


async def _resubscribe(db, conn) -> bool:
    """
    Attempt to resubscribe the given WhatsAppConnection.
    Handles missing token / phone_number_id / WABA ID gracefully.
    """
    from core.config import META_GRAPH_API_VERSION  # noqa: PLC0415

    waba_id  = conn.whatsapp_business_account_id
    phone_id = conn.phone_number_id
    token    = conn.access_token

    if not phone_id and not waba_id:
        logger.warning(
            "[Guardian] tenant=%s has no phone_number_id or WABA ID — cannot resubscribe",
            conn.tenant_id,
        )
        return False
    if not token:
        logger.warning("[Guardian] tenant=%s has no access_token — cannot resubscribe", conn.tenant_id)
        return False

    return await _subscribe_phone(phone_id, waba_id, token, META_GRAPH_API_VERSION)


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

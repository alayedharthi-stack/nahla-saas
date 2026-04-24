"""
core/scheduler.py
──────────────────
Background scheduler for periodic Nahla platform tasks:
  • Subscription expiry warnings (7 days + 3 days before)
  • Expired subscription notifications
  • Trial ending warnings

Runs as an asyncio background task started from main.py lifespan.
"""
from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timedelta, timezone

from sqlalchemy.orm import Session

logger = logging.getLogger("nahla-scheduler")

_CHECK_INTERVAL_HOURS = 12   # subscription/trial checks every 12 hours
_SYNC_INTERVAL_SECONDS = 3600  # store sync every 1 hour
_COUPON_GEN_INTERVAL_SECONDS = 6 * 3600  # coupon pool refresh every 6 hours
_TOKEN_REFRESH_INTERVAL_SECONDS = 12 * 3600  # WhatsApp token refresh every 12 hours
_SALLA_TOKEN_REFRESH_SECONDS = 6 * 3600  # Salla token refresh every 6 hours
_AUTOMATION_POLL_SECONDS = 60  # automation engine poll interval
_TEMPLATE_SYNC_INTERVAL_SECONDS = 30 * 60  # WhatsApp template auto-sync every 30 min
_CAMPAIGN_POLL_SECONDS = 30  # check for scheduled/delayed campaigns every 30s


async def run_campaign_dispatcher_scheduler() -> None:
    """Poll for scheduled/delayed campaigns that are ready to send."""
    await asyncio.sleep(10)
    logger.info("[Campaign Dispatcher] Started — polling every %ss", _CAMPAIGN_POLL_SECONDS)
    while True:
        try:
            await _dispatch_due_campaigns()
        except Exception as exc:
            logger.error("[Campaign Dispatcher] Error: %s", exc, exc_info=True)
        await asyncio.sleep(_CAMPAIGN_POLL_SECONDS)


async def _dispatch_due_campaigns() -> None:
    """Find campaigns that are scheduled/delayed and past their due time, then dispatch."""
    import sys as _sys, os as _os
    _backend = _os.path.abspath(_os.path.join(_os.path.dirname(__file__), ".."))
    _db_dir = _os.path.abspath(_os.path.join(_backend, "..", "database"))
    for _p in (_backend, _db_dir):
        if _p not in _sys.path:
            _sys.path.insert(0, _p)

    from core.database import SessionLocal
    from database.models import Campaign

    db = SessionLocal()
    try:
        now = datetime.now(timezone.utc)
        due_campaigns = (
            db.query(Campaign)
            .filter(
                Campaign.status.in_(["scheduled", "draft"]),
                Campaign.schedule_type.in_(["scheduled", "delayed"]),
            )
            .all()
        )
        for c in due_campaigns:
            is_due = False
            if c.schedule_type == "scheduled" and c.schedule_time:
                is_due = c.schedule_time <= now
            elif c.schedule_type == "delayed" and c.created_at and c.delay_minutes:
                due_at = c.created_at + timedelta(minutes=c.delay_minutes)
                is_due = due_at <= now

            if is_due:
                logger.info(
                    "[Campaign Dispatcher] campaign=%d is due (type=%s), dispatching",
                    c.id, c.schedule_type,
                )
                from services.campaign_dispatcher import dispatch_campaign
                result = await dispatch_campaign(db, c.id)
                logger.info(
                    "[Campaign Dispatcher] campaign=%d done: sent=%s failed=%s",
                    c.id, result.get("sent"), result.get("failed"),
                )
    finally:
        db.close()


async def run_scheduler() -> None:
    """Main scheduler loop — runs forever in background."""
    logger.info("[Scheduler] Started — billing checks every %sh, store sync every %ss",
                _CHECK_INTERVAL_HOURS, _SYNC_INTERVAL_SECONDS)
    await asyncio.sleep(120)  # delay first run to avoid spam on rapid re-deploys
    while True:
        try:
            await _run_checks()
        except Exception as exc:
            logger.error("[Scheduler] Error in check cycle: %s", exc, exc_info=True)
        await asyncio.sleep(_CHECK_INTERVAL_HOURS * 3600)


# ── Daily report email ───────────────────────────────────────────────────────
_DAILY_REPORT_HOUR_UTC = 5   # 5 AM UTC ≈ 8 AM KSA (UTC+3)
_DAILY_REPORT_INTERVAL = 24 * 3600


async def run_daily_report_scheduler() -> None:
    """Send daily summary email to each merchant once per day (≈08:00 KSA)."""
    # Wait until the next scheduled hour before starting the cycle
    now_utc = datetime.now(timezone.utc)
    target   = now_utc.replace(hour=_DAILY_REPORT_HOUR_UTC, minute=0, second=0, microsecond=0)
    if target <= now_utc:
        target += timedelta(days=1)
    wait_secs = (target - now_utc).total_seconds()
    logger.info("[DailyReport] Starts in %.0f s (first dispatch at %s UTC)", wait_secs, target)
    await asyncio.sleep(wait_secs)

    while True:
        try:
            await _send_daily_reports()
        except Exception as exc:
            logger.error("[DailyReport] Error: %s", exc, exc_info=True)
        await asyncio.sleep(_DAILY_REPORT_INTERVAL)


async def _send_daily_reports() -> None:
    """Query per-tenant daily metrics and email each merchant."""
    import sys as _sys, os as _os
    _backend = _os.path.abspath(_os.path.join(_os.path.dirname(__file__), ".."))
    _db_dir  = _os.path.abspath(_os.path.join(_backend, "..", "database"))
    for _p in (_backend, _db_dir):
        if _p not in _sys.path:
            _sys.path.insert(0, _p)

    from core.database import SessionLocal          # noqa: PLC0415
    from database.models import User, Order, Tenant # noqa: PLC0415
    from services.email_service import send_email   # noqa: PLC0415

    db = SessionLocal()
    try:
        merchants = (
            db.query(User)
            .filter(User.role == "merchant", User.is_active.is_(True), User.email.isnot(None))
            .all()
        )
        today_utc = datetime.now(timezone.utc).date()
        yesterday = today_utc - timedelta(days=1)

        for merchant in merchants:
            try:
                if not merchant.email or "@" not in merchant.email:
                    continue

                # Simple metrics: orders created yesterday for this tenant
                day_orders = (
                    db.query(Order)
                    .filter(
                        Order.tenant_id == merchant.tenant_id,
                        Order.created_at >= datetime(yesterday.year, yesterday.month, yesterday.day, tzinfo=timezone.utc),
                        Order.created_at <  datetime(today_utc.year, today_utc.month, today_utc.day, tzinfo=timezone.utc),
                    )
                    .all()
                )
                revenue = sum(
                    float(o.total or 0)
                    for o in day_orders
                    if o.status not in ("cancelled", "refunded")
                )

                await send_email(
                    to=merchant.email,
                    subject=f"📊 تقرير نحلة اليومي — {yesterday.strftime('%Y-%m-%d')}",
                    template="daily_report",
                    sender_type="growth",
                    variables={
                        "merchant_name":    merchant.username or "",
                        "report_date":      yesterday.strftime("%A، %d %B %Y"),
                        "orders_count":     len(day_orders),
                        "conversations_count": 0,   # extend later via ConversationMessage query
                        "revenue":          f"{revenue:,.0f}",
                        "recovered_carts":  0,
                        "ai_response_rate": None,
                    },
                )
                logger.info("[DailyReport] Sent to merchant=%d email=%s", merchant.id, merchant.email)
            except Exception as m_exc:
                logger.warning("[DailyReport] Failed for merchant=%d: %s", merchant.id, m_exc)
    finally:
        db.close()


async def run_store_sync_scheduler() -> None:
    """Hourly full sync for all connected stores — runs as a separate background task."""
    await asyncio.sleep(120)  # let the app fully start before first sync
    logger.info("[StoreSync Scheduler] Started — syncing every %ss", _SYNC_INTERVAL_SECONDS)
    while True:
        try:
            await _sync_all_stores()
        except Exception as exc:
            logger.error("[StoreSync Scheduler] Error: %s", exc, exc_info=True)
        await asyncio.sleep(_SYNC_INTERVAL_SECONDS)


async def run_coupon_generator_scheduler() -> None:
    """Refresh coupon pools for all tenants every 6 hours."""
    await asyncio.sleep(180)
    logger.info("[Coupon Scheduler] Started — refreshing every %ss", _COUPON_GEN_INTERVAL_SECONDS)
    while True:
        try:
            await _generate_coupons_all_tenants()
        except Exception as exc:
            logger.error("[Coupon Scheduler] Error: %s", exc, exc_info=True)
        await asyncio.sleep(_COUPON_GEN_INTERVAL_SECONDS)


async def run_wa_token_refresh_scheduler() -> None:
    """Proactively refresh WhatsApp merchant tokens before they expire."""
    await asyncio.sleep(300)
    logger.info("[WA Token Refresh] Started — checking every %ss", _TOKEN_REFRESH_INTERVAL_SECONDS)
    while True:
        try:
            await _refresh_all_wa_tokens()
        except Exception as exc:
            logger.error("[WA Token Refresh] Error: %s", exc, exc_info=True)
        await asyncio.sleep(_TOKEN_REFRESH_INTERVAL_SECONDS)


async def run_automation_engine_scheduler() -> None:
    """Event-driven automation engine — polls every 60 s for unprocessed events."""
    from core.automation_engine import run_automation_engine_scheduler as _engine_loop  # noqa: PLC0415
    await _engine_loop()


async def run_automation_emitters_scheduler() -> None:
    """Time-based emitters: unpaid orders + predictive reorder + calendar events.

    These three emit `AutomationEvent` rows that the engine then processes
    on its next cycle. Kept as a separate task so a slow scan can't block
    the engine's ≤60 s polling loop.
    """
    from core.automation_emitters import run_automation_emitters_scheduler as _emitters_loop  # noqa: PLC0415
    await _emitters_loop()


async def run_webhook_guardian_scheduler() -> None:
    """WhatsApp Webhook Guardian — monitors health and auto-recovers every 5 min."""
    from core.webhook_guardian import run_webhook_guardian  # noqa: PLC0415
    await run_webhook_guardian()


async def run_salla_token_refresh_scheduler() -> None:
    """Proactively refresh Salla OAuth tokens and re-enable soft-disabled integrations."""
    await asyncio.sleep(240)
    logger.info("[Salla Token Refresh] Started — checking every %ss", _SALLA_TOKEN_REFRESH_SECONDS)
    while True:
        try:
            await _refresh_all_salla_tokens()
        except Exception as exc:
            logger.error("[Salla Token Refresh] Error: %s", exc, exc_info=True)
        await asyncio.sleep(_SALLA_TOKEN_REFRESH_SECONDS)


async def run_template_sync_scheduler() -> None:
    """Auto-sync WhatsApp templates from Meta for every connected tenant.

    Runs every `_TEMPLATE_SYNC_INTERVAL_SECONDS` (30 min). The merchant should
    NEVER need to click "Sync from Meta" manually — newly approved or rejected
    templates will appear in the dashboard automatically, and bindings between
    Meta templates and Nahla service slots are auto-created via the same
    library-match logic used by the manual endpoint.
    """
    await asyncio.sleep(30)  # brief wait for DB migrations to settle
    logger.info(
        "[Template Sync Scheduler] Started — first sync NOW, then every %ss",
        _TEMPLATE_SYNC_INTERVAL_SECONDS,
    )
    while True:
        try:
            await _sync_templates_all_tenants()
        except Exception as exc:
            logger.error("[Template Sync Scheduler] Error: %s", exc, exc_info=True)
        await asyncio.sleep(_TEMPLATE_SYNC_INTERVAL_SECONDS)


# ── Last cycle stats (in-process, for ops introspection) ─────────────────────
# Updated by `_sync_templates_all_tenants` at the end of every run. Exposed
# via `get_last_template_sync_cycle()` so an admin endpoint or healthcheck
# can read it without having to scrape logs.
_last_template_sync_cycle: dict = {
    "at":              None,
    "duration_ms":     None,
    "tenants_total":   0,
    "tenants_synced":  0,
    "tenants_failed":  0,
    "tenants_skipped": 0,
    "total_templates": 0,
    "auto_bound":      0,
}


def get_last_template_sync_cycle() -> dict:
    """Return a copy of the most recent template-sync cycle stats."""
    return dict(_last_template_sync_cycle)


async def _sync_templates_all_tenants() -> None:
    """One full cycle: pull Meta templates for every tenant with a WABA ID."""
    import sys as _sys, os as _os, time as _time  # noqa: PLC0415
    _sys.path.append(_os.path.abspath(_os.path.join(_os.path.dirname(__file__), "..")))

    from core.database import SessionLocal  # noqa: PLC0415
    from database.models import WhatsAppConnection  # noqa: PLC0415

    try:
        db = SessionLocal()
    except Exception as exc:
        logger.error("[Template Sync Scheduler] Cannot open DB: %s", exc)
        return

    synced_tenants = 0
    failed_tenants = 0
    skipped_tenants = 0
    total_templates = 0
    total_bound = 0
    started = _time.monotonic()
    started_at = datetime.now(timezone.utc)
    try:
        connections = (
            db.query(WhatsAppConnection)
            .filter(
                WhatsAppConnection.whatsapp_business_account_id.isnot(None),
                WhatsAppConnection.whatsapp_business_account_id != "",
            )
            .all()
        )
        logger.info(
            "[Template Sync Scheduler] Cycle started — %d connection(s) to check",
            len(connections),
        )

        # Lazy import to avoid circular dependency with routers package.
        from routers.templates import _sync_templates_for_tenant  # noqa: PLC0415

        for conn in connections:
            tenant_id = conn.tenant_id
            if not tenant_id:
                skipped_tenants += 1
                continue
            try:
                result = await _sync_templates_for_tenant(
                    db, tenant_id, source="scheduled",
                )
                if result.get("error"):
                    skipped_tenants += 1
                    logger.info(
                        "[Template Sync Scheduler] tenant=%s skipped: %s",
                        tenant_id, result.get("error"),
                    )
                else:
                    synced_tenants += 1
                    total_templates += int(result.get("synced", 0) or 0)
                    total_bound += int(result.get("auto_bound", 0) or 0)
            except Exception as exc:
                failed_tenants += 1
                logger.warning(
                    "[Template Sync Scheduler] tenant=%s failed: %s",
                    tenant_id, exc,
                )
                try:
                    db.rollback()
                except Exception:
                    pass

        duration_ms = int((_time.monotonic() - started) * 1000)
        logger.info(
            "[Template Sync Scheduler] Cycle complete in %dms — synced_tenants=%d "
            "failed=%d skipped=%d total_templates=%d auto_bound=%d",
            duration_ms, synced_tenants, failed_tenants, skipped_tenants,
            total_templates, total_bound,
        )

        _last_template_sync_cycle.update({
            "at":              started_at.isoformat(),
            "duration_ms":     duration_ms,
            "tenants_total":   len(connections),
            "tenants_synced":  synced_tenants,
            "tenants_failed":  failed_tenants,
            "tenants_skipped": skipped_tenants,
            "total_templates": total_templates,
            "auto_bound":      total_bound,
        })
    finally:
        db.close()


async def _refresh_all_wa_tokens() -> None:
    """Find all WhatsApp connections with tokens nearing expiry and refresh them."""
    import sys as _sys, os as _os
    _sys.path.append(_os.path.abspath(_os.path.join(_os.path.dirname(__file__), "..")))

    from core.database import SessionLocal
    from database.models import WhatsAppConnection

    try:
        db = SessionLocal()
    except Exception as exc:
        logger.error("[WA Token Refresh] Cannot open DB: %s", exc)
        return

    refreshed = 0
    failed = 0
    skipped = 0
    try:
        connections = (
            db.query(WhatsAppConnection)
            .filter(
                WhatsAppConnection.access_token.isnot(None),
                WhatsAppConnection.access_token != "",
                WhatsAppConnection.connection_type == "embedded",
            )
            .all()
        )
        logger.info("[WA Token Refresh] Found %d embedded connections to check", len(connections))

        now = datetime.now(timezone.utc)
        threshold = now + timedelta(days=14)

        for conn in connections:
            try:
                needs_refresh = False
                if not conn.token_expires_at:
                    needs_refresh = True
                elif conn.token_expires_at <= threshold:
                    needs_refresh = True

                if not needs_refresh:
                    skipped += 1
                    continue

                from services.whatsapp_platform.token_manager import (
                    _refresh_merchant_long_lived_token,
                )
                result = await _refresh_merchant_long_lived_token(conn)
                if result and result.token_status in ("healthy", "expiring_soon"):
                    db.commit()
                    refreshed += 1
                    logger.info(
                        "[WA Token Refresh] tenant=%s — refreshed OK, new_exp=%s",
                        conn.tenant_id, conn.token_expires_at,
                    )
                else:
                    db.rollback()
                    failed += 1
                    logger.warning(
                        "[WA Token Refresh] tenant=%s — refresh failed (token may be expired)",
                        conn.tenant_id,
                    )
            except Exception as exc:
                db.rollback()
                failed += 1
                logger.warning("[WA Token Refresh] tenant=%s error: %s", conn.tenant_id, exc)

        logger.info(
            "[WA Token Refresh] Done — refreshed=%d failed=%d skipped=%d total=%d",
            refreshed, failed, skipped, len(connections),
        )
    finally:
        db.close()


async def _refresh_all_salla_tokens() -> None:
    """Proactively refresh Salla OAuth tokens for all integrations that have a refresh_token.

    Also re-enables integrations that were soft-disabled by app.uninstalled
    if they still have valid credentials (api_key present).
    """
    import sys as _sys, os as _os  # noqa: PLC0415
    _sys.path.append(_os.path.abspath(_os.path.join(_os.path.dirname(__file__), "..")))

    from core.database import SessionLocal  # noqa: PLC0415
    from models import Integration  # noqa: PLC0415

    client_id = _os.environ.get("SALLA_CLIENT_ID", "")
    client_secret = _os.environ.get("SALLA_CLIENT_SECRET", "")

    try:
        db = SessionLocal()
    except Exception as exc:
        logger.error("[Salla Token Refresh] Cannot open DB: %s", exc)
        return

    refreshed = 0
    reactivated = 0
    failed = 0
    skipped = 0
    try:
        integrations = db.query(Integration).filter(
            Integration.provider == "salla",
        ).all()

        logger.info("[Salla Token Refresh] Found %d Salla integrations to check", len(integrations))

        for intg in integrations:
            cfg = dict(intg.config or {})
            api_key = cfg.get("api_key", "")
            refresh_token = cfg.get("refresh_token", "")

            # Skip permanently failed integrations — needs manual re-auth by merchant
            if cfg.get("needs_reauth"):
                logger.info(
                    "[Salla Token Refresh] tenant=%s — needs_reauth=True, skipping "
                    "(waiting for merchant to re-authorize)",
                    intg.tenant_id,
                )
                skipped += 1
                continue

            # Re-enable soft-disabled integrations that still have an api_key
            if not intg.enabled and cfg.get("soft_disabled") and api_key:
                intg.enabled = True
                cfg.pop("soft_disabled", None)
                cfg["reactivated_at"] = datetime.now(timezone.utc).isoformat()
                intg.config = cfg
                db.commit()
                reactivated += 1
                logger.info(
                    "[Salla Token Refresh] RE-ACTIVATED soft-disabled integration | tenant=%s store=%s",
                    intg.tenant_id, cfg.get("store_id", "?"),
                )
                continue

            if not intg.enabled or not api_key:
                skipped += 1
                continue

            # Try to refresh the token if we have refresh_token + client credentials
            if refresh_token and client_id and client_secret:
                try:
                    import httpx  # noqa: PLC0415
                    async with httpx.AsyncClient(timeout=15) as client:
                        resp = await client.post(
                            "https://accounts.salla.sa/oauth2/token",
                            data={
                                "grant_type": "refresh_token",
                                "client_id": client_id,
                                "client_secret": client_secret,
                                "refresh_token": refresh_token,
                            },
                            headers={
                                "Accept": "application/json",
                                "Content-Type": "application/x-www-form-urlencoded",
                            },
                        )
                        if resp.status_code == 200:
                            data = resp.json()
                            new_access = data.get("access_token", "")
                            new_refresh = data.get("refresh_token", refresh_token)
                            if new_access:
                                cfg["api_key"] = new_access
                                cfg["refresh_token"] = new_refresh
                                cfg["last_token_refresh"] = datetime.now(timezone.utc).isoformat()
                                intg.config = cfg
                                db.commit()
                                refreshed += 1
                                logger.info(
                                    "[Salla Token Refresh] tenant=%s — refreshed OK",
                                    intg.tenant_id,
                                )
                            else:
                                skipped += 1
                        else:
                            resp_text = resp.text[:300]
                            if resp.status_code == 400 and "invalid_grant" in resp_text:
                                # Refresh token revoked by Salla (app under review / reinstalled).
                                # Keep the integration ENABLED so the existing access_token
                                # continues to work for API calls.  Only remove the revoked
                                # refresh_token so we stop retrying and wasting requests.
                                logger.warning(
                                    "[Salla Token Refresh] INVALID_GRANT — refresh_token revoked | "
                                    "tenant=%s store=%s — removing refresh_token, keeping api_key active",
                                    intg.tenant_id, cfg.get("store_id", "?"),
                                )
                                cfg.pop("refresh_token", None)
                                cfg["no_auto_refresh"]        = True
                                cfg["no_auto_refresh_reason"] = "invalid_grant"
                                cfg["no_auto_refresh_at"]     = datetime.now(timezone.utc).isoformat()
                                # Clear any stale reauth flags so the UI doesn't show a blocker
                                cfg.pop("needs_reauth", None)
                                cfg.pop("needs_reauth_at", None)
                                cfg.pop("needs_reauth_reason", None)
                                intg.config  = cfg
                                intg.enabled = True   # keep active with existing access_token
                                db.commit()
                                skipped += 1
                            else:
                                failed += 1
                                logger.warning(
                                    "[Salla Token Refresh] tenant=%s — refresh failed %d: %s",
                                    intg.tenant_id, resp.status_code, resp_text,
                                )
                except Exception as exc:
                    db.rollback()
                    failed += 1
                    logger.warning("[Salla Token Refresh] tenant=%s error: %s", intg.tenant_id, exc)
            else:
                skipped += 1

        logger.info(
            "[Salla Token Refresh] Done — refreshed=%d reactivated=%d failed=%d skipped=%d total=%d",
            refreshed, reactivated, failed, skipped, len(integrations),
        )
    finally:
        db.close()


async def _generate_coupons_all_tenants() -> None:
    """Top up coupon pools for every tenant with an active Salla integration."""
    import sys as _sys, os as _os
    _sys.path.append(_os.path.abspath(_os.path.join(_os.path.dirname(__file__), "..")))

    from core.database import SessionLocal
    from models import Integration

    try:
        db = SessionLocal()
    except Exception as exc:
        logger.error("[Coupon Scheduler] Cannot open DB: %s", exc)
        return

    try:
        integrations = db.query(Integration).filter(
            Integration.provider == "salla",
            Integration.enabled == True,  # noqa: E712
        ).all()

        if not integrations:
            return

        logger.info("[Coupon Scheduler] Processing %d tenant(s)...", len(integrations))

        for intg in integrations:
            tenant_id = intg.tenant_id
            try:
                from services.coupon_generator import CouponGeneratorService
                svc = CouponGeneratorService(db, tenant_id)
                created = await svc.ensure_coupon_pool()
                total = sum(created.values())
                if total:
                    logger.info("[Coupon Scheduler] tenant=%s created %d coupons", tenant_id, total)
            except Exception as exc:
                logger.error("[Coupon Scheduler] tenant=%s failed: %s", tenant_id, exc)

        logger.info("[Coupon Scheduler] Cycle complete.")
    finally:
        db.close()


async def _sync_all_stores() -> None:
    """Sync all connected stores.

    Strategy:
      - First sync for a tenant → full historical sync (all pages, all data)
      - Subsequent syncs → incremental (only items updated since last sync)
    """
    import sys as _sys, os as _os  # noqa: PLC0415
    _sys.path.append(_os.path.abspath(_os.path.join(_os.path.dirname(__file__), "..")))

    from core.database import SessionLocal  # noqa: PLC0415
    from models import Integration  # noqa: PLC0415

    try:
        db = SessionLocal()
    except Exception as exc:
        logger.error("[StoreSync Scheduler] Cannot open DB: %s", exc)
        return

    try:
        integrations = db.query(Integration).filter(
            Integration.provider == "salla",
            Integration.enabled == True,  # noqa: E712
        ).all()

        if not integrations:
            logger.info("[StoreSync Scheduler] No active Salla integrations — skipping")
            return

        logger.info("[StoreSync Scheduler] Syncing %d store(s)...", len(integrations))

        for intg in integrations:
            tenant_id = intg.tenant_id
            cfg = intg.config or {}
            api_key = cfg.get("api_key", "")

            # Skip integrations that have been permanently flagged for re-auth
            if cfg.get("needs_reauth"):
                logger.warning(
                    "[StoreSync Scheduler] tenant=%s — needs_reauth=True, skipping sync "
                    "(merchant must re-authorize Salla app)",
                    tenant_id,
                )
                continue

            if not api_key:
                logger.warning("[StoreSync Scheduler] tenant=%s has empty api_key — skipping", tenant_id)
                continue
            try:
                from services.store_sync import StoreSyncService  # noqa: PLC0415
                svc = StoreSyncService(db, tenant_id)
                result = await svc.full_sync(triggered_by="scheduler", incremental=True)
                logger.info(
                    "[StoreSync Scheduler] tenant=%s sync %s (%s) | products=%s orders=%s customers=%s",
                    tenant_id, result.get("status"), result.get("sync_type", "?"),
                    result.get("products_synced", 0), result.get("orders_synced", 0),
                    result.get("customers_synced", 0),
                )
            except Exception as exc:
                logger.error("[StoreSync Scheduler] tenant=%s sync failed: %s", tenant_id, exc)

        logger.info("[StoreSync Scheduler] Cycle complete.")
    finally:
        db.close()


async def _run_checks() -> None:
    """Run all periodic checks."""
    logger.info("[Scheduler] Running periodic checks...")
    await _check_subscription_expiry()
    await _check_trial_expiry()
    await _maybe_reset_monthly_wa_usage()
    logger.info("[Scheduler] Checks complete.")


async def _check_subscription_expiry() -> None:
    """Send WhatsApp warnings for expiring/expired subscriptions."""
    import sys, os  # noqa: PLC0415
    sys.path.append(os.path.abspath(os.path.join(os.path.dirname(__file__), "../../database")))

    from core.database import SessionLocal  # noqa: PLC0415
    from core.wa_notify import (  # noqa: PLC0415
        notify_subscription_expired,
        notify_subscription_expiring,
    )

    try:
        db: Session = SessionLocal()
    except Exception as exc:
        logger.error("[Scheduler] Cannot open DB session: %s", exc)
        return

    try:
        from models import BillingSubscription, Tenant, User  # noqa: PLC0415
        from core.tenant import (  # noqa: PLC0415
            DEFAULT_STORE, DEFAULT_WHATSAPP,
            get_or_create_settings, merge_defaults,
        )
        from services.email_service import send_email as send_template_email  # noqa: PLC0415

        now = datetime.now(timezone.utc)

        active_subs = (
            db.query(BillingSubscription)
            .filter(BillingSubscription.status == "active")
            .all()
        )

        for sub in active_subs:
            if not sub.ends_at:
                continue

            _s         = get_or_create_settings(db, sub.tenant_id)
            _wa        = merge_defaults(_s.whatsapp_settings, DEFAULT_WHATSAPP)
            _st        = merge_defaults(_s.store_settings,    DEFAULT_STORE)
            phone      = _wa.get("owner_whatsapp_number", "")
            store_name = _st.get("store_name") or f"متجر #{sub.tenant_id}"
            plan_name  = sub.plan.name if sub.plan else "الباقة الحالية"

            merchant = db.query(User).filter(
                User.tenant_id == sub.tenant_id,
                User.role == "merchant",
                User.is_active == True,  # noqa: E712
            ).first()
            email_addr = getattr(merchant, "email", "") if merchant else ""
            merchant_name = getattr(merchant, "full_name", "") if merchant else ""

            ends_raw = sub.ends_at
            if ends_raw and ends_raw.tzinfo is None:
                ends_raw = ends_raw.replace(tzinfo=timezone.utc)
            days_left = (ends_raw - now).days if ends_raw else 999

            if days_left < 0:
                logger.info("[Scheduler] Sub %s expired for tenant %s", sub.id, sub.tenant_id)
                sub.status = "expired"
                db.commit()
                await notify_subscription_expired(phone, store_name)
                if email_addr:
                    await send_template_email(
                        to=email_addr,
                        subject=f"انتهى اشتراكك في {plan_name} — نحلة AI",
                        template="trial_expired",
                        variables={"store_name": store_name, "plan_name": plan_name},
                        sender_type="billing",
                    )

            elif days_left <= 3 and not _already_notified(sub, "warn_3"):
                await notify_subscription_expiring(phone, store_name, plan_name, days_left)
                if email_addr:
                    await send_template_email(
                        to=email_addr,
                        subject=f"اشتراكك ينتهي خلال {days_left} أيام — نحلة AI",
                        template="trial_expiring",
                        variables={
                            "store_name": store_name, "merchant_name": merchant_name,
                            "days_remaining": days_left, "plan_name": plan_name,
                        },
                        sender_type="billing",
                    )
                _mark_notified(db, sub, "warn_3")

            elif days_left <= 7 and not _already_notified(sub, "warn_7"):
                await notify_subscription_expiring(phone, store_name, plan_name, days_left)
                if email_addr:
                    await send_template_email(
                        to=email_addr,
                        subject=f"اشتراكك ينتهي خلال {days_left} أيام — نحلة AI",
                        template="trial_expiring",
                        variables={
                            "store_name": store_name, "merchant_name": merchant_name,
                            "days_remaining": days_left, "plan_name": plan_name,
                        },
                        sender_type="billing",
                    )
                _mark_notified(db, sub, "warn_7")

    finally:
        db.close()


_trial_sent_cache: dict[tuple[int, str], float] = {}

async def _check_trial_expiry() -> None:
    """Send WhatsApp warnings for expiring trials."""
    import sys, os  # noqa: PLC0415
    sys.path.append(os.path.abspath(os.path.join(os.path.dirname(__file__), "../../database")))

    from core.database import SessionLocal  # noqa: PLC0415
    from core.wa_notify import notify_trial_ending  # noqa: PLC0415
    from core.billing import FREE_TRIAL_DAYS  # noqa: PLC0415
    import time  # noqa: PLC0415

    DEDUP_WINDOW = 12 * 3600  # seconds — suppress duplicates within 12 hours

    try:
        db: Session = SessionLocal()
    except Exception as exc:
        logger.error("[Scheduler] Cannot open DB session: %s", exc)
        return

    try:
        from models import BillingSubscription, Tenant  # noqa: PLC0415
        from core.tenant import (  # noqa: PLC0415
            DEFAULT_STORE, DEFAULT_WHATSAPP,
            get_or_create_settings, merge_defaults,
        )

        now = datetime.now(timezone.utc)
        now_ts = time.time()

        subbed_tenants = {
            s.tenant_id for s in db.query(BillingSubscription)
            .filter(BillingSubscription.status == "active").all()
        }

        tenants = db.query(Tenant).filter(Tenant.is_active == True).all()  # noqa: E712

        for tenant in tenants:
            if tenant.id in subbed_tenants:
                continue

            _raw = tenant.created_at or now
            if _raw.tzinfo is None:
                trial_start = _raw.replace(tzinfo=timezone.utc)
            else:
                trial_start = _raw
            trial_elapsed  = (now - trial_start).days
            days_remaining = FREE_TRIAL_DAYS - trial_elapsed

            _s         = get_or_create_settings(db, tenant.id)
            _wa        = merge_defaults(_s.whatsapp_settings, DEFAULT_WHATSAPP)
            _st        = merge_defaults(_s.store_settings,    DEFAULT_STORE)
            phone      = _wa.get("owner_whatsapp_number", "")
            store_name = _st.get("store_name") or f"متجر #{tenant.id}"

            if not phone:
                continue

            from services.email_service import send_email as send_template_email  # noqa: PLC0415
            from models import User  # noqa: PLC0415
            merchant   = db.query(User).filter(
                User.tenant_id == tenant.id, User.role == "merchant",
                User.is_active == True,  # noqa: E712
            ).first()
            email_addr = getattr(merchant, "email", "") if merchant else ""
            merchant_name = getattr(merchant, "full_name", "") if merchant else ""

            db.refresh(_s)
            meta = (_s.extra_metadata or {}).get("_scheduler_flags", {})

            def _dedup_ok(flag: str) -> bool:
                key = (tenant.id, flag)
                last = _trial_sent_cache.get(key, 0)
                if now_ts - last < DEDUP_WINDOW:
                    logger.info("[Scheduler] tenant=%s flag=%s suppressed (in-memory dedup)", tenant.id, flag)
                    return False
                return True

            def _mark_sent(flag: str) -> None:
                _trial_sent_cache[(tenant.id, flag)] = now_ts

            _email_vars = {
                "store_name": store_name,
                "merchant_name": merchant_name,
                "days_remaining": days_remaining,
            }

            if days_remaining == 7 and not meta.get("trial_warn_7") and _dedup_ok("trial_warn_7"):
                await notify_trial_ending(phone, store_name, 7)
                if email_addr:
                    await send_template_email(
                        to=email_addr,
                        subject="تجربتك المجانية تنتهي خلال 7 أيام — نحلة AI",
                        template="trial_expiring",
                        variables={**_email_vars, "days_remaining": 7},
                        sender_type="billing",
                    )
                _update_tenant_flag(db, tenant.id, _s, "trial_warn_7")
                _mark_sent("trial_warn_7")
            elif days_remaining == 3 and not meta.get("trial_warn_3") and _dedup_ok("trial_warn_3"):
                await notify_trial_ending(phone, store_name, 3)
                if email_addr:
                    await send_template_email(
                        to=email_addr,
                        subject="تجربتك المجانية تنتهي خلال 3 أيام — نحلة AI",
                        template="trial_expiring",
                        variables={**_email_vars, "days_remaining": 3},
                        sender_type="billing",
                    )
                _update_tenant_flag(db, tenant.id, _s, "trial_warn_3")
                _mark_sent("trial_warn_3")
            elif days_remaining == 1 and not meta.get("trial_warn_1") and _dedup_ok("trial_warn_1"):
                await notify_trial_ending(phone, store_name, 1)
                if email_addr:
                    await send_template_email(
                        to=email_addr,
                        subject="آخر يوم في تجربتك المجانية — نحلة AI",
                        template="trial_expiring",
                        variables={**_email_vars, "days_remaining": 1},
                        sender_type="billing",
                    )
                _update_tenant_flag(db, tenant.id, _s, "trial_warn_1")
                _mark_sent("trial_warn_1")
            elif days_remaining <= 0 and not meta.get("trial_expired") and _dedup_ok("trial_expired"):
                from core.wa_notify import notify_subscription_expired  # noqa: PLC0415
                await notify_subscription_expired(phone, store_name)
                if email_addr:
                    await send_template_email(
                        to=email_addr,
                        subject="انتهت تجربتك المجانية — اشترك الآن",
                        template="trial_expired",
                        variables=_email_vars,
                        sender_type="billing",
                    )
                _update_tenant_flag(db, tenant.id, _s, "trial_expired")
                _mark_sent("trial_expired")

    finally:
        db.close()


# ── Helpers ────────────────────────────────────────────────────────────────────

def _already_notified(sub: object, flag: str) -> bool:
    meta = getattr(sub, "extra_metadata", None) or {}
    return bool(meta.get(f"notified_{flag}"))


def _mark_notified(db: Session, sub: object, flag: str) -> None:
    from sqlalchemy.orm.attributes import flag_modified  # noqa: PLC0415
    meta = dict(getattr(sub, "extra_metadata", None) or {})
    meta[f"notified_{flag}"] = True
    sub.extra_metadata = meta  # type: ignore[attr-defined]
    flag_modified(sub, "extra_metadata")
    db.commit()


def _update_tenant_flag(db: Session, tenant_id: int, settings_obj: object, flag: str) -> None:
    from sqlalchemy.orm.attributes import flag_modified  # noqa: PLC0415
    from core.tenant import get_or_create_settings  # noqa: PLC0415
    _s    = get_or_create_settings(db, tenant_id)
    meta  = dict(_s.extra_metadata or {})
    flags = dict(meta.get("_scheduler_flags") or {})
    flags[flag] = True
    meta["_scheduler_flags"] = flags
    _s.extra_metadata = meta
    flag_modified(_s, "extra_metadata")
    try:
        db.commit()
        logger.info("[Scheduler] tenant=%s flag=%s persisted", tenant_id, flag)
    except Exception as exc:
        logger.warning("[Scheduler] tenant=%s flag=%s commit failed: %s", tenant_id, flag, exc)
        db.rollback()


async def _maybe_reset_monthly_wa_usage() -> None:
    """
    Reset all tenants' WhatsApp conversation counters on the 1st of the month.
    Safe to call multiple times per day — uses DB unique index as guard.
    """
    from datetime import datetime, timezone  # noqa: PLC0415

    now = datetime.now(timezone.utc)
    if now.day != 1:
        return   # only act on the 1st of the month

    logger.info("[Scheduler] 1st of month — resetting WhatsApp usage counters")
    try:
        import sys as _sys, os as _os  # noqa: PLC0415
        _sys.path.append(_os.path.abspath(_os.path.join(_os.path.dirname(__file__), "../../database")))

        from core.database import SessionLocal  # noqa: PLC0415
        from core.wa_usage  import reset_all_monthly_usage  # noqa: PLC0415

        db = SessionLocal()
        try:
            n = reset_all_monthly_usage(db)
            logger.info("[Scheduler] WhatsApp usage reset | tenants_refreshed=%d", n)
        finally:
            db.close()
    except Exception as exc:
        logger.error("[Scheduler] WA usage reset failed: %s", exc, exc_info=True)

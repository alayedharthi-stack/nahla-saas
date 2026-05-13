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
import os
from datetime import datetime, timedelta, timezone
from typing import Any, Dict

from sqlalchemy.orm import Session

logger = logging.getLogger("nahla-scheduler")

_CHECK_INTERVAL_HOURS = 12   # subscription/trial checks every 12 hours
_SYNC_INTERVAL_SECONDS = 3600  # full store sync every 1 hour
_COUPON_GEN_INTERVAL_SECONDS = 6 * 3600  # coupon pool refresh every 6 hours
_TOKEN_REFRESH_INTERVAL_SECONDS = 12 * 3600  # WhatsApp token refresh every 12 hours
_SALLA_TOKEN_REFRESH_SECONDS = 24 * 3600  # Salla token refresh daily (smart conditions)
_AUTOMATION_POLL_SECONDS = 60  # automation engine poll interval
_TEMPLATE_SYNC_INTERVAL_SECONDS = 30 * 60  # WhatsApp template auto-sync every 30 min
_CAMPAIGN_POLL_SECONDS = 30  # check for scheduled/delayed campaigns every 30s
_STUCK_IMMEDIATE_THRESHOLD_SECONDS = 60  # rescue immediate campaigns whose
                                          # in-process asyncio task vanished
                                          # (uvicorn restart between commit
                                          # and create_task; uncaught
                                          # exception; OOM; etc.)


# ── Campaign dispatcher heartbeat (in-process diagnostics) ───────────
# Updated by run_campaign_dispatcher_scheduler and _dispatch_due_campaigns
# every cycle. Exposed via get_campaign_dispatcher_state() and read by
# the admin diagnostic endpoint GET /admin/debug/scheduler-health.
#
# Purpose: prove from outside Railway logs that:
#   (a) the FastAPI lifespan completed (started_at != None),
#   (b) the loop is alive (last_tick_at within last poll cycle),
#   (c) the rescue path is firing for stuck immediates.
#
# Process-local memory only — resets on uvicorn restart. That is
# intentional: a fresh start_at after a deploy is exactly the signal
# we want.
_campaign_dispatcher_state: dict = {
    "started_at":              None,  # set once when the loop logs "Started"
    "started_at_monotonic":    None,  # monotonic counterpart for uptime calc
    "last_tick_at":            None,  # set at the start of every cycle
    "last_tick_ok":            None,  # True if last tick finished without raise
    "last_tick_error":         None,  # repr() of the last exception, or None
    "ticks_total":             0,
    "ticks_failed":            0,
    "last_rescue_at":          None,  # set when at least one campaign was rescued
    "last_rescued_campaign_ids": [],  # last batch of rescued ids
    "rescue_invocations_total": 0,    # # of cycles that found ≥1 stuck campaign
    "rescue_campaigns_total":   0,    # cumulative rescued campaign count
    "poll_seconds":            _CAMPAIGN_POLL_SECONDS,
    "stuck_threshold_seconds": _STUCK_IMMEDIATE_THRESHOLD_SECONDS,
}


def get_campaign_dispatcher_state() -> dict:
    """Return a snapshot copy of the campaign dispatcher heartbeat.

    Used by the admin diagnostic endpoint. Returns a plain dict so
    the consumer cannot accidentally mutate process state."""
    snapshot = dict(_campaign_dispatcher_state)
    # Compute live "alive" verdict + age fields here so the
    # endpoint can stay dumb.
    now = datetime.now(timezone.utc)
    started_at = snapshot.get("started_at")
    last_tick_at = snapshot.get("last_tick_at")
    poll_s = snapshot.get("poll_seconds") or _CAMPAIGN_POLL_SECONDS

    if last_tick_at is not None:
        try:
            age = (now - last_tick_at).total_seconds()
        except Exception:
            age = None
        snapshot["last_tick_age_seconds"] = age
        # The loop is healthy if a tick fired within 3× the poll
        # period — gives us tolerance for one slow cycle.
        snapshot["alive"] = (
            age is not None and age <= (poll_s * 3)
        )
    else:
        snapshot["last_tick_age_seconds"] = None
        snapshot["alive"] = False

    snapshot["started"] = started_at is not None
    if started_at is not None:
        try:
            snapshot["uptime_seconds"] = (now - started_at).total_seconds()
        except Exception:
            snapshot["uptime_seconds"] = None
    else:
        snapshot["uptime_seconds"] = None

    # Format timestamps as ISO 8601 for the JSON response.
    for k in ("started_at", "last_tick_at", "last_rescue_at"):
        v = snapshot.get(k)
        if isinstance(v, datetime):
            snapshot[k] = v.isoformat()

    return snapshot


def evaluate_rescue_eligibility(
    campaign,
    *,
    send_logs_count: int,
    now=None,
    threshold_seconds: int = _STUCK_IMMEDIATE_THRESHOLD_SECONDS,
) -> dict:
    """Explain — in plain JSON — whether the rescue probe would
    pick up this specific campaign on its next tick, and exactly
    which of the four conditions pass / fail.

    Returns:
        {
          "would_rescue": bool,
          "blocked_by":   [str, ...]  // empty when would_rescue=True
          "explanation_ar": str,       // single sentence for the UI
          "conditions": {
            "status_is_active":        {"pass": bool, "value": "..."}
            "schedule_type_immediate": {"pass": bool, "value": "..."}
            "launched_at_set":         {"pass": bool, "value": "..."}
            "past_grace_window":       {"pass": bool, "value": "...", "age_seconds": float | None}
            "no_send_logs":            {"pass": bool, "value": int}
          }
        }

    This is the SAME logic encoded in ``_find_stuck_immediate_campaigns``'s
    SQL filter, but evaluated per-campaign for diagnostic display.
    The two MUST agree — if you change one, change the other (the
    tests in test_campaign_stuck_immediate_rescue.py + test_campaign_rescue_eligibility.py
    lock both halves)."""
    if now is None:
        now = datetime.now(timezone.utc)

    status_v       = getattr(campaign, "status", None) or ""
    schedule_v     = getattr(campaign, "schedule_type", None) or ""
    launched_at    = getattr(campaign, "launched_at", None)

    cond_status = {
        "pass":  status_v == "active",
        "value": status_v,
    }
    cond_sched = {
        "pass":  schedule_v == "immediate",
        "value": schedule_v,
    }
    cond_launched_set = {
        "pass":  launched_at is not None,
        "value": launched_at.isoformat() if launched_at else None,
    }
    if launched_at is not None:
        try:
            la = launched_at
            if la.tzinfo is None:
                la = la.replace(tzinfo=timezone.utc)
            age = (now - la).total_seconds()
        except Exception:
            age = None
        cond_grace = {
            "pass":  age is not None and age >= threshold_seconds,
            "value": (
                f"launched {age:.0f}s ago (threshold ≥ {threshold_seconds}s)"
                if age is not None else "unknown age"
            ),
            "age_seconds": age,
        }
    else:
        cond_grace = {
            "pass":  False,
            "value": "launched_at is null",
            "age_seconds": None,
        }
    cond_no_logs = {
        "pass":  int(send_logs_count) == 0,
        "value": int(send_logs_count),
    }

    blocked: list[str] = []
    if not cond_status["pass"]:
        blocked.append(
            f"status={status_v!r} (rescue requires 'active'; "
            "non-active campaigns are either in a terminal state "
            "or have not been launched yet)"
        )
    if not cond_sched["pass"]:
        blocked.append(
            f"schedule_type={schedule_v!r} (rescue only targets "
            "'immediate' — scheduled/delayed campaigns are handled "
            "by the regular due-time loop)"
        )
    if not cond_launched_set["pass"]:
        blocked.append(
            "launched_at is null (campaign was created but never "
            "transitioned to 'launched'; check POST /campaigns flow)"
        )
    elif not cond_grace["pass"]:
        age_s = cond_grace.get("age_seconds")
        if age_s is not None:
            blocked.append(
                f"within {threshold_seconds}s grace window "
                f"(launched {age_s:.0f}s ago) — the in-process "
                "asyncio task may still be running; rescue waits "
                "to avoid double-dispatch"
            )
        else:
            blocked.append("launched_at unreadable")
    if not cond_no_logs["pass"]:
        blocked.append(
            f"campaign_send_logs already has {send_logs_count} "
            "row(s) — campaign is in flight or completed; rescue "
            "skips to prevent double-send"
        )

    if not blocked:
        explanation = (
            "✅ ستلتقطها دورة الإنقاذ التالية (خلال ≤ "
            f"{_CAMPAIGN_POLL_SECONDS}s)."
        )
    else:
        explanation = "🚫 لن يلتقطها الإنقاذ — " + " ؛ ".join(blocked)

    return {
        "would_rescue":    not blocked,
        "blocked_by":      blocked,
        "explanation_ar":  explanation,
        "conditions": {
            "status_is_active":        cond_status,
            "schedule_type_immediate": cond_sched,
            "launched_at_set":         cond_launched_set,
            "past_grace_window":       cond_grace,
            "no_send_logs":            cond_no_logs,
        },
        "thresholds": {
            "stuck_after_seconds":  threshold_seconds,
            "poll_seconds":         _CAMPAIGN_POLL_SECONDS,
        },
    }


def _find_stuck_immediate_campaigns(
    db,
    *,
    now,
    threshold_seconds: int = _STUCK_IMMEDIATE_THRESHOLD_SECONDS,
):
    """Return Campaign rows whose immediate-dispatch asyncio task
    appears to have been dropped.

    Why this exists
    ───────────────
    Immediate campaigns (``schedule_type='immediate'``) are
    dispatched purely by an in-process ``asyncio.create_task`` fired
    from ``POST /campaigns``. There's no persistence around that
    task. If anything kills it — uvicorn restart between
    ``db.commit()`` of the Campaign row and ``create_task`` of the
    dispatcher, OOM, an uncaught exception that poisons the session
    so the failure-flip in the except handler can't write
    ``status='failed'`` — the campaign stays at ``status='active'``
    with ZERO ``campaign_send_logs`` rows. Dashboard shows
    "بانتظار بدء الإرسال" (lifecycle=pending_dispatch) forever.

    The narrow filter in ``_dispatch_due_campaigns``
    (``status IN ('scheduled','draft')``) excludes ``active``
    immediates from any rescue path. This helper widens the lens:

      * Campaign.status == 'active'
      * Campaign.schedule_type == 'immediate'
      * Campaign.launched_at <= now - threshold_seconds
      * NO ``campaign_send_logs`` row exists for this campaign

    The last condition is the critical safety: a campaign that's
    actively sending (snapshot rows exist, batches are flowing) MUST
    NOT be re-dispatched — that would double-send. We only rescue
    campaigns whose snapshot never landed.

    Idempotency
    ───────────
    ``services.campaign_dispatcher.dispatch_campaign`` is documented
    as idempotent on the same ``campaign_id`` thanks to the unique
    index on ``(tenant_id, campaign_id, customer_phone_e164)`` in
    ``campaign_send_logs``. So even if our "no rows yet" check races
    with a real-but-late snapshot commit, the re-run cannot
    duplicate work — at worst, the second snapshot is a no-op and
    the second send loop picks up whatever the first one left
    ``queued``.

    Returns a list of Campaign objects, never None.
    """
    from database.models import Campaign, CampaignSendLog  # noqa: PLC0415

    cutoff = now - timedelta(seconds=int(threshold_seconds))

    # Subquery: campaigns that already have at least one send-log
    # row. Excluded from rescue — they're either in-flight or
    # already completed.
    has_log_subq = (
        db.query(CampaignSendLog.campaign_id)
        .distinct()
        .subquery()
    )

    candidates = (
        db.query(Campaign)
        .outerjoin(has_log_subq, Campaign.id == has_log_subq.c.campaign_id)
        .filter(
            Campaign.status == "active",
            Campaign.schedule_type == "immediate",
            Campaign.launched_at.isnot(None),
            Campaign.launched_at <= cutoff,
            has_log_subq.c.campaign_id.is_(None),  # no log rows yet
        )
        .all()
    )
    return candidates


async def run_campaign_dispatcher_scheduler() -> None:
    """Poll for scheduled/delayed campaigns that are ready to send."""
    await asyncio.sleep(10)
    # ── Heartbeat: mark the loop as started ────────────────────
    # Visible from outside via GET /admin/debug/scheduler-health
    # so we can prove the lifespan completed without scraping
    # Railway logs.
    import time as _time  # noqa: PLC0415
    _campaign_dispatcher_state["started_at"] = datetime.now(timezone.utc)
    _campaign_dispatcher_state["started_at_monotonic"] = _time.monotonic()
    logger.info("[Campaign Dispatcher] Started — polling every %ss", _CAMPAIGN_POLL_SECONDS)
    while True:
        _campaign_dispatcher_state["last_tick_at"] = datetime.now(timezone.utc)
        _campaign_dispatcher_state["ticks_total"] += 1
        try:
            await _dispatch_due_campaigns()
            _campaign_dispatcher_state["last_tick_ok"] = True
            _campaign_dispatcher_state["last_tick_error"] = None
        except Exception as exc:
            _campaign_dispatcher_state["last_tick_ok"] = False
            _campaign_dispatcher_state["last_tick_error"] = repr(exc)[:400]
            _campaign_dispatcher_state["ticks_failed"] += 1
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

        # ── Rescue stuck immediate campaigns ────────────────────
        # See _find_stuck_immediate_campaigns docstring for full
        # rationale. Short version: if an immediate campaign's
        # in-process asyncio dispatch task vanished (uvicorn
        # restart, OOM, poisoned session in the except handler),
        # the campaign stays at status='active' with zero
        # send-log rows forever — the dashboard shows
        # "بانتظار بدء الإرسال" indefinitely. This loop is the
        # only path that can rescue it.
        try:
            stuck = _find_stuck_immediate_campaigns(db, now=now)
        except Exception as exc:  # noqa: BLE001
            logger.warning(
                "[Campaign Dispatcher] stuck-immediate probe failed: %s",
                exc,
            )
            stuck = []
        if stuck:
            # Record diagnostic state BEFORE the per-campaign loop
            # so a slow per-campaign dispatch doesn't hide that we
            # at least found stuck rows.
            _campaign_dispatcher_state["last_rescue_at"] = now
            _campaign_dispatcher_state["rescue_invocations_total"] += 1
            _campaign_dispatcher_state["rescue_campaigns_total"] += len(stuck)
            _campaign_dispatcher_state["last_rescued_campaign_ids"] = [
                c.id for c in stuck
            ][:20]
        for c in stuck:
            age_seconds = (
                (now - c.launched_at).total_seconds()
                if c.launched_at else None
            )
            logger.warning(
                "[CAMPAIGN_RESCUE_STUCK_IMMEDIATE] campaign=%d tenant=%s "
                "launched_at=%s age_seconds=%.0f — re-dispatching "
                "(idempotent via UNIQUE constraint on send_logs)",
                c.id, c.tenant_id, c.launched_at,
                age_seconds if age_seconds is not None else -1,
            )
            try:
                from services.campaign_dispatcher import dispatch_campaign  # noqa: PLC0415
                result = await dispatch_campaign(db, c.id)
                logger.info(
                    "[CAMPAIGN_RESCUE_STUCK_IMMEDIATE] campaign=%d "
                    "rescue done: sent=%s failed=%s skipped=%s",
                    c.id, result.get("sent"), result.get("failed"),
                    result.get("skipped"),
                )
            except Exception as exc:  # noqa: BLE001
                logger.error(
                    "[CAMPAIGN_RESCUE_STUCK_IMMEDIATE] campaign=%d "
                    "rescue raised: %s — will retry next cycle",
                    c.id, exc, exc_info=True,
                )
                # The dispatcher itself raised. Roll back so the
                # next loop iteration / cycle gets a clean
                # session.
                try:
                    db.rollback()
                except Exception:
                    pass
    finally:
        db.close()


# ──────────────────────────────────────────────────────────────────
# Wave / Batch sending scheduler
# ──────────────────────────────────────────────────────────────────
#
# This loop is the runtime arm of the Wave/Batch architecture (see
# ``services/wave_scheduler.py`` for the planning side). It wakes up
# every ``_WAVE_POLL_SECONDS`` and:
#
#   1. Picks every ``CampaignWave`` whose ``status='pending'`` and
#      ``scheduled_at <= now()`` (cheap range scan on
#      ``ix_campaign_waves_due``).
#   2. Flips each picked wave to ``dispatching``.
#   3. Calls ``services.campaign_dispatcher.dispatch_campaign``
#      with ``only_wave_id=wave.id`` — which short-circuits the
#      audience/snapshot/freq-cap stages and dispatches exactly
#      this wave's slice of the campaign's snapshot rows.
#   4. On success: marks the wave ``completed`` with its counters.
#      If this was the LAST wave of the campaign, flips the
#      parent ``Campaign.status`` to ``completed``.
#   5. On exception: marks the wave ``failed`` so the merchant can
#      inspect / retry from the UI.
#
# Why a separate loop (and not piggy-back on the dispatcher loop)
# ──────────────────────────────────────────────────────────────
# The existing campaign-dispatcher loop already polls every 30s
# for `scheduled` / `delayed` campaigns and rescues stuck immediate
# ones. Adding wave logic to it would entangle two very different
# state machines: "campaign-as-a-whole" vs "wave-as-an-instance".
# Keeping them in distinct loops:
#   * lets us tune the poll cadence independently (waves want
#     finer granularity so a 30-min-spaced wave fires close to
#     its scheduled time),
#   * isolates failure domains (a misbehaving wave can't break
#     the immediate-campaign rescue path),
#   * keeps the wave loop trivially deletable if we ever
#     decommission the Wave/Batch feature.

_WAVE_POLL_SECONDS = int(os.getenv("CAMPAIGN_WAVE_POLL_SECONDS", "30"))

_wave_scheduler_state: Dict[str, Any] = {
    "started_at":           None,
    "last_tick_at":         None,
    "last_tick_ok":         None,
    "last_tick_error":      None,
    "ticks_total":          0,
    "ticks_failed":         0,
    "waves_dispatched":     0,
    "waves_failed":         0,
}


def get_wave_scheduler_state() -> Dict[str, Any]:
    """Snapshot of the wave scheduler heartbeat — used by
    ``/admin/debug/scheduler-health`` and operator scripts."""
    return dict(_wave_scheduler_state)


async def run_campaign_wave_scheduler() -> None:
    """Poll for ``CampaignWave`` rows that are due and dispatch them.

    Runs as a single asyncio task in the FastAPI lifespan, identical
    in shape to ``run_campaign_dispatcher_scheduler``.
    """
    await asyncio.sleep(15)
    _wave_scheduler_state["started_at"] = datetime.now(timezone.utc)
    logger.info(
        "[Campaign Wave Scheduler] Started — polling every %ss",
        _WAVE_POLL_SECONDS,
    )
    while True:
        _wave_scheduler_state["last_tick_at"] = datetime.now(timezone.utc)
        _wave_scheduler_state["ticks_total"] += 1
        try:
            await _dispatch_due_waves()
            _wave_scheduler_state["last_tick_ok"] = True
            _wave_scheduler_state["last_tick_error"] = None
        except Exception as exc:  # noqa: BLE001
            _wave_scheduler_state["last_tick_ok"] = False
            _wave_scheduler_state["last_tick_error"] = repr(exc)[:400]
            _wave_scheduler_state["ticks_failed"] += 1
            logger.error(
                "[Campaign Wave Scheduler] Error: %s", exc, exc_info=True,
            )
        await asyncio.sleep(_WAVE_POLL_SECONDS)


async def _dispatch_due_waves() -> None:
    """One pass of the wave scheduler.

    Picks every due wave, marks it ``dispatching``, then calls the
    main dispatcher. Each wave is processed sequentially in a
    single tick — that's safe because the dispatcher is itself
    bounded by the campaign's ``audience_count`` and Meta's API
    has its own rate limits. If the queue grows we increase
    ``_WAVE_POLL_SECONDS`` rather than parallelise here.
    """
    # Defer imports so this module stays cheap on startup and the
    # test harness's path-bootstrapping doesn't fight us.
    import sys as _sys, os as _os  # noqa: PLC0415
    _backend = _os.path.abspath(_os.path.join(_os.path.dirname(__file__), ".."))
    _db_dir = _os.path.abspath(_os.path.join(_backend, "..", "database"))
    for _p in (_backend, _db_dir):
        if _p not in _sys.path:
            _sys.path.insert(0, _p)

    from core.database import SessionLocal  # noqa: PLC0415
    from database.models import Campaign, CampaignWave  # noqa: PLC0415
    from services import wave_scheduler as ws  # noqa: PLC0415
    from services.campaign_dispatcher import dispatch_campaign  # noqa: PLC0415

    db = SessionLocal()
    try:
        due_waves = ws.pick_due_waves(db=db)
        if not due_waves:
            return
        logger.info(
            "[Campaign Wave Scheduler] %d wave(s) due", len(due_waves),
        )

        for wave in due_waves:
            campaign_id = int(wave.campaign_id)
            wave_id = int(wave.id)
            # Re-check status under our own transaction — another
            # tick might have grabbed it (defensive even in
            # single-worker mode).
            ws.mark_wave_dispatching(db=db, wave=wave)
            db.commit()

            try:
                logger.info(
                    "[Campaign Wave Scheduler] dispatching wave=%d "
                    "campaign=%d (%d/%d, planned=%d)",
                    wave_id, campaign_id,
                    wave.wave_index, wave.total_waves,
                    wave.planned_recipients,
                )
                result = await dispatch_campaign(
                    db, campaign_id, only_wave_id=wave_id,
                )
                sent = int(result.get("sent") or 0)
                failed = int(result.get("failed") or 0)
                ws.complete_wave(
                    db=db, wave=wave,
                    sent=sent, failed=failed, success=True,
                )
                db.commit()
                _wave_scheduler_state["waves_dispatched"] += 1

                # If this was the last wave, finalise the parent
                # campaign's status. The legacy dispatch path also
                # writes ``Campaign.status='completed'`` at the end
                # of its single run; we mirror that here so the
                # campaign report card reflects "completed" once
                # every wave has terminated.
                remaining_pending = (
                    db.query(CampaignWave)
                    .filter(
                        CampaignWave.campaign_id == campaign_id,
                        CampaignWave.status.in_((
                            ws.WAVE_PENDING, ws.WAVE_DISPATCHING,
                        )),
                    )
                    .count()
                )
                if remaining_pending == 0:
                    camp = db.query(Campaign).filter(
                        Campaign.id == campaign_id,
                    ).first()
                    if camp and camp.status not in ("completed", "failed"):
                        camp.status = "completed"
                        camp.updated_at = datetime.now(timezone.utc)
                        db.commit()
                        logger.info(
                            "[Campaign Wave Scheduler] campaign=%d "
                            "all waves complete — marked campaign completed",
                            campaign_id,
                        )

            except Exception as exc:  # noqa: BLE001
                logger.error(
                    "[Campaign Wave Scheduler] wave=%d campaign=%d "
                    "dispatch raised: %s",
                    wave_id, campaign_id, exc, exc_info=True,
                )
                try:
                    db.rollback()
                except Exception:
                    pass
                # Pull a fresh handle on the wave row — the rollback
                # detached the original from the session.
                fresh = db.query(CampaignWave).filter(
                    CampaignWave.id == wave_id,
                ).first()
                if fresh:
                    ws.complete_wave(
                        db=db, wave=fresh,
                        sent=0, failed=0, success=False,
                    )
                    db.commit()
                _wave_scheduler_state["waves_failed"] += 1
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


# NOTE: The fast incremental order sweeper that used to live here was
# superseded by `services/salla_orders_poller.py`, a dedicated poller with
# per-tenant try/except, structured logging, idempotency, and a Postgres
# advisory lock for multi-worker safety. It is started from `main.py`
# lifespan via `run_salla_orders_poller_scheduler()`.


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
    """Proactively refresh Salla OAuth tokens — runs daily.

    Conditions checked per integration (either is enough to trigger refresh):
      • access_token expires within 5 days (expires_at set)
      • 10+ days since the last successful refresh (last_token_refresh_at set)
      • refresh_token exists but expires_at/last_refresh unknown → refresh now
    """
    await asyncio.sleep(240)
    logger.info("[Salla Token Refresh] Started — checking every %ss (daily)", _SALLA_TOKEN_REFRESH_SECONDS)
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
    """Daily proactive refresh for all Salla integrations with a refresh_token.

    Refresh is triggered when EITHER condition is met:
      • access_token expires within 5 days (expires_at known)
      • 10+ days since last successful refresh (last_token_refresh_at known)
      • refresh_token exists but expiry/refresh history unknown → refresh now

    On success  : saves new tokens + timestamps, sets token_refresh_status='success'.
    On failure  : records error + increments attempt counter — NEVER disables integration.
    invalid_grant: refresh_token revoked by Salla — remove it, keep access_token active.
    """
    import sys as _sys, os as _os  # noqa: PLC0415
    _sys.path.append(_os.path.abspath(_os.path.join(_os.path.dirname(__file__), "..")))

    from core.database import SessionLocal  # noqa: PLC0415
    from models import Integration  # noqa: PLC0415

    client_id     = _os.environ.get("SALLA_CLIENT_ID", "")
    client_secret = _os.environ.get("SALLA_CLIENT_SECRET", "")

    try:
        db = SessionLocal()
    except Exception as exc:
        logger.error("[Salla Token Refresh] Cannot open DB: %s", exc)
        return

    refreshed    = 0
    reactivated  = 0
    failed       = 0
    skipped      = 0
    due          = 0

    try:
        # Ascending id order: older rows are processed first so they can see
        # newer healthy siblings (find_superseding_integration only matches
        # candidates with id > intg.id, i.e. created later).
        integrations = (
            db.query(Integration)
            .filter(Integration.provider == "salla")
            .order_by(Integration.id.asc())
            .all()
        )

        logger.info("[Salla Token Refresh] Cycle started — %d Salla integrations to evaluate", len(integrations))

        now = datetime.now(timezone.utc)

        for intg in integrations:
            cfg           = dict(intg.config or {})
            api_key       = cfg.get("api_key", "")
            refresh_token = cfg.get("refresh_token", "")
            store_id      = cfg.get("store_id", "?")

            # Skip permanently failed integrations — needs manual re-auth
            if cfg.get("needs_reauth"):
                logger.info(
                    "[Salla Token Refresh] tenant=%s store=%s — needs_reauth=True, skipping",
                    intg.tenant_id, store_id,
                )
                skipped += 1
                continue

            # Re-enable soft-disabled integrations that still have an api_key
            if not intg.enabled and cfg.get("soft_disabled") and api_key:
                intg.enabled = True
                cfg.pop("soft_disabled", None)
                cfg["reactivated_at"] = now.isoformat()
                intg.config = cfg
                db.commit()
                reactivated += 1
                logger.info(
                    "[Salla Token Refresh] RE-ACTIVATED soft-disabled integration | tenant=%s store=%s",
                    intg.tenant_id, store_id,
                )
                continue

            if not intg.enabled or not api_key:
                skipped += 1
                continue

            if not refresh_token:
                skipped += 1
                continue

            if not client_id or not client_secret:
                logger.warning(
                    "[Salla Token Refresh] SALLA_CLIENT_ID/SECRET not configured — skipping all",
                )
                skipped += 1
                continue

            # ── Decide whether refresh is due ────────────────────────────────
            needs_refresh   = False
            refresh_reason  = ""
            days_until_exp  = None
            days_since_last = None

            # Condition 1: token expiry within 5 days
            _exp_raw = cfg.get("expires_at") or cfg.get("token_expires_at")
            if _exp_raw:
                try:
                    _exp_dt = datetime.fromisoformat(_exp_raw.replace("Z", "+00:00"))
                    if _exp_dt.tzinfo is None:
                        _exp_dt = _exp_dt.replace(tzinfo=timezone.utc)
                    days_until_exp = (_exp_dt - now).total_seconds() / 86400
                    if days_until_exp < 5:
                        needs_refresh  = True
                        refresh_reason = f"expires_in_{days_until_exp:.1f}_days"
                except Exception:
                    pass

            # Condition 2: 10+ days since last successful refresh
            _last_raw = cfg.get("last_token_refresh_at") or cfg.get("last_token_refresh")
            if _last_raw and not needs_refresh:
                try:
                    _last_dt = datetime.fromisoformat(_last_raw.replace("Z", "+00:00"))
                    if _last_dt.tzinfo is None:
                        _last_dt = _last_dt.replace(tzinfo=timezone.utc)
                    days_since_last = (now - _last_dt).total_seconds() / 86400
                    if days_since_last >= 10:
                        needs_refresh  = True
                        refresh_reason = f"last_refresh_{days_since_last:.1f}_days_ago"
                except Exception:
                    pass

            # Condition 3: refresh_token present but no history → refresh now
            if not needs_refresh and _exp_raw is None and _last_raw is None:
                needs_refresh  = True
                refresh_reason = "no_expiry_or_refresh_history"

            if not needs_refresh:
                skipped += 1
                logger.info(
                    "[Salla Token Refresh] tenant=%s store=%s — token OK "
                    "(days_until_exp=%s days_since_last=%s), skipping",
                    intg.tenant_id, store_id,
                    f"{days_until_exp:.1f}" if days_until_exp is not None else "unknown",
                    f"{days_since_last:.1f}" if days_since_last is not None else "unknown",
                )
                continue

            due += 1
            logger.info(
                "[SALLA TOKEN] refresh due | tenant=%s store=%s reason=%s",
                intg.tenant_id, store_id, refresh_reason,
            )

            # ── Acquire two-layer lock before touching OAuth endpoint ─────────
            from core.salla_token_lock import SallaTokenLock  # noqa: PLC0415
            _lock = SallaTokenLock(db, intg, caller="scheduler")
            _lock_acquired = await _lock.acquire()
            if not _lock_acquired:
                skipped += 1
                continue

            # ── Perform refresh ──────────────────────────────────────────────
            try:
                import httpx  # noqa: PLC0415
                async with httpx.AsyncClient(timeout=15) as http:
                    resp = await http.post(
                        "https://accounts.salla.sa/oauth2/token",
                        data={
                            "grant_type":    "refresh_token",
                            "client_id":     client_id,
                            "client_secret": client_secret,
                            "refresh_token": refresh_token,
                        },
                        headers={
                            "Accept":       "application/json",
                            "Content-Type": "application/x-www-form-urlencoded",
                        },
                    )

                # ── Import alert/metric helpers (local to avoid circular import) ──
                from core.salla_token_alerts import (  # noqa: PLC0415
                    should_escalate_to_needs_reauth,
                    maybe_send_reauth_alert,
                    log_metric_success,
                    log_metric_failed,
                    log_metric_needs_reauth,
                )

                if resp.status_code == 200:
                    data       = resp.json()
                    new_access = data.get("access_token", "")
                    # Guard: never overwrite refresh_token with null/empty
                    _raw_rt    = data.get("refresh_token")
                    new_refresh = _raw_rt if _raw_rt else refresh_token
                    new_exp_in  = data.get("expires_in")

                    if not new_access:
                        raise ValueError("Salla returned 200 but access_token is empty")

                    cfg["api_key"]               = new_access
                    cfg["refresh_token"]         = new_refresh
                    cfg["last_token_refresh"]    = now.isoformat()  # backward compat
                    cfg["last_token_refresh_at"] = now.isoformat()
                    cfg["token_refresh_status"]  = "success"
                    cfg["token_refresh_attempts"] = 0  # reset streak on success
                    cfg.pop("token_refresh_error",             None)
                    cfg.pop("token_refresh_failed_at",         None)
                    cfg.pop("token_refresh_first_failed_at",   None)
                    cfg.pop("needs_reauth",                    None)
                    cfg.pop("needs_reauth_at",                 None)
                    cfg.pop("needs_reauth_reason",             None)
                    # Reset alert cooldown so a new failure streak triggers a fresh email
                    cfg.pop("token_reauth_alert_sent_at",      None)

                    if new_exp_in:
                        try:
                            _new_exp_at = (now + timedelta(seconds=int(new_exp_in))).isoformat()
                            cfg["expires_at"]       = _new_exp_at
                            cfg["token_expires_at"] = _new_exp_at
                        except Exception:
                            pass

                    intg.config = cfg
                    db.commit()
                    refreshed += 1
                    logger.info(
                        "[SALLA TOKEN] refresh success | tenant=%s store=%s new_expires_in=%s",
                        intg.tenant_id, store_id, new_exp_in,
                    )
                    log_metric_success(intg.tenant_id, store_id)

                else:
                    resp_text = resp.text[:400]
                    if resp.status_code == 400 and "invalid_grant" in resp_text:
                        # Refresh token definitively revoked by Salla.
                        # Remove it (no point retrying) but keep access_token
                        # so existing API calls may still work until it expires.
                        logger.warning(
                            "[Salla Token Refresh] INVALID_GRANT — refresh_token revoked | "
                            "tenant=%s store=%s — removing refresh_token, keeping api_key",
                            intg.tenant_id, store_id,
                        )
                        # ── Counter invariants ───────────────────────────────
                        # invalid_grant IS a real, definitive refresh attempt.
                        # Stamp attempts/first_failed_at via shared helper so the
                        # alert email + dashboard never display "attempts=0 with
                        # last_error=invalid_grant" again.
                        from core.salla_token_alerts import (  # noqa: PLC0415
                            stamp_refresh_failure,
                            find_superseding_integration,
                            mark_superseded,
                        )
                        stamp_refresh_failure(cfg, error="invalid_grant", now=now)
                        cfg.pop("refresh_token",       None)
                        cfg["no_auto_refresh"]         = True
                        cfg["no_auto_refresh_reason"]  = "invalid_grant"
                        cfg["no_auto_refresh_at"]      = now.isoformat()

                        # ── Superseded check: skip needs_reauth + alert when
                        # a newer healthy integration already serves this store
                        superseder = find_superseding_integration(db, intg)
                        if superseder is not None:
                            mark_superseded(cfg, by_integration_id=superseder.id, now=now)
                            cfg.pop("needs_reauth",        None)
                            cfg.pop("needs_reauth_reason", None)
                            cfg.pop("needs_reauth_at",     None)
                            intg.config  = cfg
                            intg.enabled = False  # park the orphan record
                            db.commit()
                            skipped += 1
                            logger.warning(
                                "[SALLA TOKEN] orphan superseded by newer integration "
                                "| tenant=%s store=%s old_id=%s new_id=%s — alert suppressed",
                                intg.tenant_id, store_id, intg.id, superseder.id,
                            )
                            continue

                        cfg["needs_reauth"]            = True
                        cfg["needs_reauth_reason"]     = "invalid_grant"
                        cfg["needs_reauth_at"]         = now.isoformat()
                        intg.config  = cfg
                        intg.enabled = True
                        db.commit()
                        skipped += 1
                        logger.critical(
                            "[SALLA TOKEN] refresh_token revoked; needs reauth | "
                            "tenant=%s store=%s reason=invalid_grant attempts=%s",
                            intg.tenant_id, store_id, cfg.get("token_refresh_attempts"),
                        )
                        log_metric_needs_reauth(intg.tenant_id, store_id, "invalid_grant")
                        await maybe_send_reauth_alert(
                            tenant_id=intg.tenant_id,
                            integration_id=intg.id,
                            cfg=cfg,
                            now=now,
                        )
                        # Persist updated cfg (may have token_reauth_alert_sent_at)
                        intg.config = cfg
                        db.commit()

                    else:
                        # Transient or unknown error — log failure, do NOT disable yet
                        err_msg = f"HTTP {resp.status_code}: {resp_text}"
                        from core.salla_token_alerts import (  # noqa: PLC0415
                            stamp_refresh_failure,
                            find_superseding_integration,
                        )
                        stamp_refresh_failure(cfg, error=err_msg, now=now)
                        new_attempts = cfg["token_refresh_attempts"]
                        intg.config = cfg
                        db.commit()
                        failed += 1
                        logger.warning(
                            "[SALLA TOKEN] refresh failed | tenant=%s store=%s "
                            "error=%s attempts=%s",
                            intg.tenant_id, store_id, err_msg, new_attempts,
                        )
                        log_metric_failed(intg.tenant_id, store_id, new_attempts)

                        # ── Grace-window escalation ──────────────────────────
                        escalate, reauth_reason = should_escalate_to_needs_reauth(cfg, now)
                        if escalate and not cfg.get("needs_reauth"):
                            superseder = find_superseding_integration(db, intg)
                            cfg["needs_reauth"]        = True
                            cfg["needs_reauth_reason"] = reauth_reason
                            cfg["needs_reauth_at"]     = now.isoformat()
                            intg.config = cfg
                            db.commit()
                            logger.critical(
                                "[SALLA TOKEN] refresh failed 3 times; needs reauth | "
                                "tenant=%s store=%s attempts=%s reason=%s",
                                intg.tenant_id, store_id, new_attempts, reauth_reason,
                            )
                            log_metric_needs_reauth(intg.tenant_id, store_id, reauth_reason)
                            await maybe_send_reauth_alert(
                                tenant_id=intg.tenant_id,
                                integration_id=intg.id,
                                cfg=cfg,
                                now=now,
                                superseded_by=superseder.id if superseder else None,
                            )
                            # Persist updated cfg (token_reauth_alert_sent_at)
                            intg.config = cfg
                            db.commit()

            except Exception as exc:
                try:
                    db.rollback()
                except Exception:
                    pass
                # Persist failure state after rollback
                try:
                    from core.salla_token_alerts import (  # noqa: PLC0415
                        should_escalate_to_needs_reauth,
                        log_metric_failed,
                        log_metric_needs_reauth,
                    )
                    _cfg2 = dict(intg.config or {})
                    prev2 = _cfg2.get("token_refresh_attempts", 0)
                    new_attempts2 = prev2 + 1
                    if prev2 == 0:
                        _cfg2["token_refresh_first_failed_at"] = now.isoformat()
                    _cfg2["token_refresh_status"]    = "failed"
                    _cfg2["token_refresh_error"]     = str(exc)[:400]
                    _cfg2["token_refresh_failed_at"] = now.isoformat()
                    _cfg2["token_refresh_attempts"]  = new_attempts2
                    log_metric_failed(intg.tenant_id, store_id, new_attempts2)

                    escalate2, reason2 = should_escalate_to_needs_reauth(_cfg2, now)
                    if escalate2 and not _cfg2.get("needs_reauth"):
                        _cfg2["needs_reauth"]        = True
                        _cfg2["needs_reauth_reason"] = reason2
                        _cfg2["needs_reauth_at"]     = now.isoformat()
                        logger.critical(
                            "[SALLA TOKEN] refresh failed 3 times; needs reauth | "
                            "tenant=%s store=%s attempts=%s reason=%s",
                            intg.tenant_id, store_id, new_attempts2, reason2,
                        )
                        log_metric_needs_reauth(intg.tenant_id, store_id, reason2 or "exception")
                    intg.config = _cfg2
                    db.commit()
                except Exception:
                    pass
                failed += 1
                logger.warning(
                    "[SALLA TOKEN] refresh failed | tenant=%s store=%s error=%s",
                    intg.tenant_id, store_id, exc,
                )
            finally:
                await _lock.release()

        logger.info(
            "[Salla Token Refresh] Cycle complete — due=%d refreshed=%d "
            "reactivated=%d failed=%d skipped=%d total=%d",
            due, refreshed, reactivated, failed, skipped, len(integrations),
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

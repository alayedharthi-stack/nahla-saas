"""
core/salla_token_alerts.py
───────────────────────────
Production-hardening helpers for the Salla token refresh system.

Grace Window
────────────
``needs_reauth=True`` is only set when ALL three conditions hold simultaneously:
  1. ``token_refresh_attempts >= 3``
  2. The first failure in the current streak (``token_refresh_first_failed_at``)
     occurred within the last ``GRACE_HOURS`` (24 h). This means the 3
     failures are *clustered* in time — a sustained outage — rather than
     scattered transient errors spread over several days.
  3. The ``access_token`` is already expired OR will expire within
     ``EXPIRY_THRESHOLD_HOURS`` (24 h), meaning the merchant's store
     connectivity is actually at risk.

The grace window prevents false alarms from intermittent Salla outages or
brief network hiccups that self-heal before the token expires.

Internal Alert
──────────────
When ``needs_reauth`` transitions to ``True``, an email is sent to
``ALERT_EMAIL`` via the project's existing Resend integration.

Deduplication:
  • The alert is *not* re-sent within ``ALERT_COOLDOWN_HOURS`` (24 h).
  • After a successful refresh (which clears the flag), the cooldown resets so
    a new failure streak will trigger a fresh alert.
  • A reminder is sent if 24 h pass and the problem persists.

Metric Logs
───────────
Structured, searchable log lines for dashboards / log aggregators:

    [SALLA METRIC] token_refresh_success  tenant_id=X store_id=Y
    [SALLA METRIC] token_refresh_failed   tenant_id=X store_id=Y attempts=N
    [SALLA METRIC] token_needs_reauth     tenant_id=X store_id=Y reason=Z
"""
from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import Optional

logger = logging.getLogger("nahla.salla_alerts")

ALERT_EMAIL            = "alerts@nahlah.ai"
GRACE_HOURS: int       = 24   # failure cluster window to trigger escalation
EXPIRY_THRESHOLD_HOURS = 24.0 # escalate only when token expires within this window
ALERT_COOLDOWN_HOURS   = 24.0 # minimum hours between duplicate alert emails


# ── Grace Window ──────────────────────────────────────────────────────────────

def should_escalate_to_needs_reauth(
    cfg: dict,
    now: datetime,
) -> tuple[bool, Optional[str]]:
    """Return ``(escalate, reason)`` applying the 24-hour grace window.

    Returns ``(True, reason_str)`` only when all three conditions are met:
      1. ``token_refresh_attempts >= 3``
      2. Failures clustered within the last ``GRACE_HOURS``
         (``token_refresh_first_failed_at`` is within the window, or unknown)
      3. Token expired or expiring within ``EXPIRY_THRESHOLD_HOURS``

    Returns ``(False, None)`` when the token still has plenty of time or the
    failures are spread too far apart to indicate a real sustained problem.
    """
    attempts = cfg.get("token_refresh_attempts", 0)
    if attempts < 3:
        return False, None

    # ── Condition 2: are the failures clustered within the grace window? ──────
    first_raw = cfg.get("token_refresh_first_failed_at")
    if first_raw:
        try:
            first_dt = datetime.fromisoformat(first_raw.replace("Z", "+00:00"))
            if first_dt.tzinfo is None:
                first_dt = first_dt.replace(tzinfo=timezone.utc)
            hours_since_first = (now - first_dt).total_seconds() / 3600
            if hours_since_first > GRACE_HOURS:
                # Spread over > 24 h → intermittent, not a sustained outage
                return False, None
        except Exception:
            pass  # Unreadable timestamp → treat as within window (conservative)

    # ── Condition 3: token is expired or expiring very soon ──────────────────
    exp_raw = cfg.get("expires_at") or cfg.get("token_expires_at")
    if not exp_raw:
        # No expiry data → we cannot confirm impact on the merchant.
        # Do NOT escalate without evidence — wait for expiry info.
        return False, None

    try:
        exp_dt = datetime.fromisoformat(exp_raw.replace("Z", "+00:00"))
        if exp_dt.tzinfo is None:
            exp_dt = exp_dt.replace(tzinfo=timezone.utc)
        hours_until_exp = (exp_dt - now).total_seconds() / 3600
        if hours_until_exp <= EXPIRY_THRESHOLD_HOURS:
            reason = (
                f"refresh_failed_{attempts}x_"
                f"token_expires_in_{hours_until_exp:.1f}h"
            )
            return True, reason
        # Token still has time — grace window holds
        return False, None
    except Exception:
        return False, None


def should_send_alert(cfg: dict, now: datetime) -> bool:
    """Return ``True`` when a fresh alert email should be dispatched.

    Deduplication:
      • No prior alert in this reauth episode → send.
      • Alert sent within ``ALERT_COOLDOWN_HOURS`` → skip.
      • ``ALERT_COOLDOWN_HOURS`` elapsed since last alert → send a reminder.
    """
    sent_raw = cfg.get("token_reauth_alert_sent_at")
    if not sent_raw:
        return True
    try:
        sent_dt = datetime.fromisoformat(sent_raw.replace("Z", "+00:00"))
        if sent_dt.tzinfo is None:
            sent_dt = sent_dt.replace(tzinfo=timezone.utc)
        return (now - sent_dt).total_seconds() / 3600 >= ALERT_COOLDOWN_HOURS
    except Exception:
        return True  # Unreadable → send


# ── Internal alert email ──────────────────────────────────────────────────────

async def maybe_send_reauth_alert(
    *,
    tenant_id: int,
    integration_id: Optional[int],
    cfg: dict,
    now: datetime,
) -> bool:
    """Send an internal ops alert when an integration transitions to needs_reauth.

    Applies deduplication via ``should_send_alert``.
    Updates ``cfg`` in-place with ``token_reauth_alert_sent_at`` so the
    caller can persist the timestamp in the DB.

    Returns ``True`` if an email was actually dispatched.
    Fails silently (logs a warning) if RESEND_API_KEY is not set or the
    send fails — alert failures must not disrupt the token refresh flow.
    """
    if not should_send_alert(cfg, now):
        return False

    try:
        from core.notifications import send_email  # noqa: PLC0415
    except ImportError:
        logger.warning("[SALLA ALERT] send_email unavailable — alert skipped")
        return False

    store_id     = cfg.get("store_id",   "unknown")
    store_name   = cfg.get("store_name", "") or ""
    expires_at   = cfg.get("expires_at") or cfg.get("token_expires_at") or "unknown"
    last_refresh = cfg.get("last_token_refresh_at") or cfg.get("last_token_refresh") or "never"
    attempts     = cfg.get("token_refresh_attempts", 0)
    error_msg    = cfg.get("token_refresh_error", "—") or "—"
    reason       = cfg.get("needs_reauth_reason", "—") or "—"
    first_failed = cfg.get("token_refresh_first_failed_at") or "—"
    admin_url    = (
        "https://app.nahlah.ai/admin/salla/integrations/token-status"
        f"?tenant_id={tenant_id}"
    )

    html = f"""
<div style="font-family:Arial,sans-serif;max-width:600px;margin:0 auto;
            padding:28px;color:#1e293b;border:1px solid #fca5a5;
            border-radius:8px;background:#fff5f5">
  <h2 style="color:#dc2626;margin-top:0">⚠️ [SALLA TOKEN] Merchant Needs Reauth</h2>
  <p style="color:#475569">
    An integration can no longer refresh its Salla access token automatically.
    The merchant must reinstall or reauthorise the Nahla app.
  </p>
  <table style="width:100%;border-collapse:collapse;font-size:14px;margin-top:12px">
    <tr style="background:#f8fafc">
      <td style="padding:8px 12px;font-weight:bold;border:1px solid #e2e8f0;
                 white-space:nowrap">Tenant ID</td>
      <td style="padding:8px 12px;border:1px solid #e2e8f0">{tenant_id}</td>
    </tr>
    <tr>
      <td style="padding:8px 12px;font-weight:bold;border:1px solid #e2e8f0">Integration ID</td>
      <td style="padding:8px 12px;border:1px solid #e2e8f0">{integration_id or "—"}</td>
    </tr>
    <tr style="background:#f8fafc">
      <td style="padding:8px 12px;font-weight:bold;border:1px solid #e2e8f0">Store ID</td>
      <td style="padding:8px 12px;border:1px solid #e2e8f0">{store_id}</td>
    </tr>
    <tr>
      <td style="padding:8px 12px;font-weight:bold;border:1px solid #e2e8f0">Store Name</td>
      <td style="padding:8px 12px;border:1px solid #e2e8f0">{store_name or "—"}</td>
    </tr>
    <tr style="background:#f8fafc">
      <td style="padding:8px 12px;font-weight:bold;border:1px solid #e2e8f0">Token Expires At</td>
      <td style="padding:8px 12px;border:1px solid #e2e8f0">{expires_at}</td>
    </tr>
    <tr>
      <td style="padding:8px 12px;font-weight:bold;border:1px solid #e2e8f0">Last Successful Refresh</td>
      <td style="padding:8px 12px;border:1px solid #e2e8f0">{last_refresh}</td>
    </tr>
    <tr style="background:#f8fafc">
      <td style="padding:8px 12px;font-weight:bold;border:1px solid #e2e8f0">First Failure At</td>
      <td style="padding:8px 12px;border:1px solid #e2e8f0">{first_failed}</td>
    </tr>
    <tr>
      <td style="padding:8px 12px;font-weight:bold;border:1px solid #e2e8f0">Refresh Attempts</td>
      <td style="padding:8px 12px;border:1px solid #e2e8f0;color:#dc2626;
                 font-weight:bold">{attempts}</td>
    </tr>
    <tr style="background:#f8fafc">
      <td style="padding:8px 12px;font-weight:bold;border:1px solid #e2e8f0">Last Error</td>
      <td style="padding:8px 12px;border:1px solid #e2e8f0;
                 color:#dc2626">{error_msg}</td>
    </tr>
    <tr>
      <td style="padding:8px 12px;font-weight:bold;border:1px solid #e2e8f0">Escalation Reason</td>
      <td style="padding:8px 12px;border:1px solid #e2e8f0">{reason}</td>
    </tr>
  </table>
  <div style="margin-top:20px">
    <a href="{admin_url}"
       style="display:inline-block;background:#f59e0b;color:#fff;padding:11px 26px;
              border-radius:6px;text-decoration:none;font-weight:bold;font-size:14px">
      Open Token Status Dashboard
    </a>
  </div>
  <p style="color:#64748b;font-size:12px;margin-top:20px">
    The integration stays <strong>enabled</strong> and the existing access_token remains
    active until it expires. No immediate action is required from ops — but the merchant
    should reconnect soon to prevent service interruption.
  </p>
  <hr style="border:none;border-top:1px solid #fecaca;margin:20px 0">
  <p style="color:#94a3b8;font-size:11px">
    Nahla AI · Internal Ops Alert ·
    <a href="https://nahlah.ai" style="color:#f59e0b">nahlah.ai</a>
  </p>
</div>
"""

    subject = f"[SALLA TOKEN] Tenant {tenant_id} needs reauth — store {store_id}"
    sent = await send_email(to=ALERT_EMAIL, subject=subject, html=html)

    if sent:
        cfg["token_reauth_alert_sent_at"] = now.isoformat()
        logger.info(
            "[SALLA ALERT] reauth alert sent | tenant=%s integration_id=%s store=%s",
            tenant_id, integration_id, store_id,
        )
    else:
        logger.warning(
            "[SALLA ALERT] failed to send reauth alert | tenant=%s store=%s",
            tenant_id, store_id,
        )

    return sent


# ── Metric log helpers (structured, searchable) ───────────────────────────────

def log_metric_success(tenant_id: int, store_id: str) -> None:
    """Emit a structured success metric log."""
    logger.info(
        "[SALLA METRIC] token_refresh_success tenant_id=%s store_id=%s",
        tenant_id, store_id,
    )


def log_metric_failed(tenant_id: int, store_id: str, attempts: int) -> None:
    """Emit a structured failure metric log."""
    logger.warning(
        "[SALLA METRIC] token_refresh_failed tenant_id=%s store_id=%s attempts=%s",
        tenant_id, store_id, attempts,
    )


def log_metric_needs_reauth(tenant_id: int, store_id: str, reason: str) -> None:
    """Emit a structured needs_reauth metric log (CRITICAL level)."""
    logger.critical(
        "[SALLA METRIC] token_needs_reauth tenant_id=%s store_id=%s reason=%s",
        tenant_id, store_id, reason,
    )

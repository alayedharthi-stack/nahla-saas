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

Internal Ops Alert
──────────────────
When ``needs_reauth`` transitions to ``True``, an **internal ops-only**
email is sent to ``ALERT_EMAIL`` via the project's existing Resend
integration.  Merchants are **not** emailed on this path.

Merchant-facing reauth email is prepared separately in
``send_merchant_salla_reauth_email()`` but is **not** invoked by the
token refresh scheduler or admin refresh endpoints until recipient and
locale resolution are wired explicitly.

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
from typing import Any, Optional

logger = logging.getLogger("nahla.salla_alerts")

ALERT_EMAIL            = "alerts@nahlah.ai"
OPS_REAUTH_TAG         = "[OPS]"
GRACE_HOURS: int       = 24   # failure cluster window to trigger escalation
EXPIRY_THRESHOLD_HOURS = 24.0 # escalate only when token expires within this window
ALERT_COOLDOWN_HOURS   = 24.0 # minimum hours between duplicate alert emails

# Merchant copy — no technical fields; used only by send_merchant_salla_reauth_email().
_MERCHANT_REAUTH_COPY: dict[str, dict[str, str]] = {
    "ar": {
        "subject": "يلزم إعادة ربط متجر سلة مع نحلة",
        "body": (
            "تعذر تحديث اتصال متجر سلة تلقائيًا. لإبقاء الطلبات والمزامنة "
            "تعمل بشكل صحيح، يرجى إعادة تفويض تطبيق نحلة من سلة."
        ),
        "button": "إعادة الربط من سلة",
    },
    "en": {
        "subject": "Reconnect your Salla store to Nahla",
        "body": (
            "We could not refresh your Salla connection automatically. "
            "Please re-authorize the Nahla app from Salla to keep your store sync working."
        ),
        "button": "Reconnect Salla",
    },
}


# ── Counter normalisation ─────────────────────────────────────────────────────

def stamp_refresh_failure(
    cfg: dict,
    *,
    error: str,
    now: datetime,
    bump_attempts: bool = True,
) -> dict:
    """Persist refresh-failure metadata on ``cfg`` (in-place) consistently.

    Guarantees the following invariants used by the alert email + dashboard:
      • ``token_refresh_attempts`` is at least ``1`` whenever a real failure
        was observed (no more "attempts=0 with last_error=invalid_grant").
      • ``token_refresh_first_failed_at`` is set on the first failure of a
        streak (and preserved on subsequent failures).
      • ``token_refresh_status``, ``token_refresh_error``,
        ``token_refresh_failed_at`` are stamped.

    Returns the mutated ``cfg`` for chaining.
    """
    prev_attempts = int(cfg.get("token_refresh_attempts", 0) or 0)
    new_attempts  = prev_attempts + 1 if bump_attempts else max(prev_attempts, 1)
    if not cfg.get("token_refresh_first_failed_at"):
        cfg["token_refresh_first_failed_at"] = now.isoformat()
    cfg["token_refresh_status"]    = "failed"
    cfg["token_refresh_error"]     = (error or "")[:400]
    cfg["token_refresh_failed_at"] = now.isoformat()
    cfg["token_refresh_attempts"]  = new_attempts
    return cfg


# ── Superseded-integration detection ──────────────────────────────────────────

def find_superseding_integration(db, intg) -> Optional[Any]:
    """Return a *newer* healthy integration row that supersedes ``intg``.

    A row is considered "superseding" when **all** are true:
      • Same ``store_id`` (in ``config.store_id`` or ``external_store_id``).
      • Different DB id and ``id > intg.id`` (i.e. created later).
      • ``enabled = True``.
      • ``config.needs_reauth`` is falsy.
      • ``config.api_key`` is present.

    When such a sibling exists, the older row is effectively dead — the
    merchant has already re-installed/re-authorised and a newer record is
    serving traffic. We must not spam reauth alerts for the orphan record.
    """
    if intg is None:
        return None
    # Use the exact ORM class bound to ``intg`` so we hit the same mapper /
    # registry the caller used (``models.Integration`` and
    # ``database.models.Integration`` are two distinct classes loaded under
    # two module names; mixing them causes empty queries).
    Integration = type(intg)

    cfg = dict(intg.config or {})
    store_id = str(cfg.get("store_id") or getattr(intg, "external_store_id", "") or "").strip()
    if not store_id:
        return None

    try:
        candidates = (
            db.query(Integration)
            .filter(
                Integration.provider == "salla",
                Integration.tenant_id == intg.tenant_id,
                Integration.id != intg.id,
            )
            .order_by(Integration.id.desc())
            .all()
        )
    except Exception as exc:
        logger.debug("[SALLA ALERT] superseded lookup failed: %s", exc)
        return None

    for cand in candidates:
        if cand.id <= intg.id:
            continue
        if not cand.enabled:
            continue
        ccfg = cand.config or {}
        if ccfg.get("needs_reauth"):
            continue
        cand_store = str(ccfg.get("store_id") or getattr(cand, "external_store_id", "") or "").strip()
        if cand_store != store_id:
            continue
        if not ccfg.get("api_key"):
            continue
        return cand
    return None


def mark_superseded(cfg: dict, *, by_integration_id: int, now: datetime) -> dict:
    """Mark ``cfg`` as superseded by a newer integration (in-place)."""
    cfg["superseded"] = True
    cfg["superseded_by_integration_id"] = int(by_integration_id)
    cfg["superseded_at"] = now.isoformat()
    return cfg


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


# ── Ops alert copy helpers ────────────────────────────────────────────────────

def build_ops_reauth_subject(*, tenant_id: int, store_id: str) -> str:
    """Subject line for the internal ops reauth alert."""
    store = str(store_id)
    return (
        f"{OPS_REAUTH_TAG} Tenant {tenant_id} Salla token requires reauth "
        f"— store {store}"
    )


def normalize_merchant_reauth_locale(language: Optional[str]) -> str:
    """Map a merchant language hint to ``ar`` (default) or ``en``."""
    if not language:
        return "ar"
    lang = str(language).strip().lower().replace("_", "-")
    if lang.startswith("en"):
        return "en"
    return "ar"


def build_merchant_reauth_email(
    *,
    locale: str,
    reconnect_url: str,
) -> tuple[str, str]:
    """Return ``(subject, html)`` for a merchant-facing Salla reauth email.

    Deliberately excludes ops fields (tenant_id, integration_id,
    invalid_grant, refresh attempts, token metadata, admin links).
    """
    loc = normalize_merchant_reauth_locale(locale)
    copy = _MERCHANT_REAUTH_COPY[loc]
    rtl = loc == "ar"
    direction = "rtl" if rtl else "ltr"
    align = "right" if rtl else "left"
    font = "Arial,sans-serif"

    html = f"""
<div dir="{direction}" style="font-family:{font};max-width:520px;margin:0 auto;
            padding:28px;color:#1e293b;text-align:{align}">
  <h2 style="color:#1e293b;margin-top:0;font-size:20px">{copy["subject"]}</h2>
  <p style="color:#475569;font-size:15px;line-height:1.6">{copy["body"]}</p>
  <div style="margin-top:24px">
    <a href="{reconnect_url}"
       style="display:inline-block;background:#f59e0b;color:#fff;padding:12px 28px;
              border-radius:8px;text-decoration:none;font-weight:bold;font-size:14px">
      {copy["button"]}
    </a>
  </div>
  <hr style="border:none;border-top:1px solid #e2e8f0;margin:28px 0">
  <p style="color:#94a3b8;font-size:12px">نحلة AI · Nahla</p>
</div>
"""
    return copy["subject"], html


async def send_merchant_salla_reauth_email(
    *,
    to: str,
    reconnect_url: str,
    locale: str = "ar",
) -> bool:
    """Send a merchant-facing Salla reauth email.

    **Not wired** to ``maybe_send_reauth_alert()`` or the token refresh
    scheduler.  Call only when ``to`` and ``locale`` are resolved explicitly
    (e.g. merchant ``User.email`` + dashboard language).

    Returns ``True`` when the email was dispatched.
    """
    recipient = (to or "").strip()
    if not recipient:
        logger.warning("[SALLA ALERT] merchant reauth email skipped — empty recipient")
        return False
    if not (reconnect_url or "").strip():
        logger.warning("[SALLA ALERT] merchant reauth email skipped — empty reconnect_url")
        return False

    try:
        from core.notifications import send_email  # noqa: PLC0415
    except ImportError:
        logger.warning("[SALLA ALERT] send_email unavailable — merchant reauth skipped")
        return False

    subject, html = build_merchant_reauth_email(
        locale=locale,
        reconnect_url=reconnect_url.strip(),
    )
    sent = await send_email(to=recipient, subject=subject, html=html)
    if sent:
        logger.info(
            "[SALLA ALERT] merchant reauth email sent | to=%s locale=%s",
            recipient, normalize_merchant_reauth_locale(locale),
        )
    else:
        logger.warning(
            "[SALLA ALERT] merchant reauth email failed | to=%s locale=%s",
            recipient, normalize_merchant_reauth_locale(locale),
        )
    return sent


# ── Internal ops alert email ──────────────────────────────────────────────────

async def maybe_send_reauth_alert(
    *,
    tenant_id: int,
    integration_id: Optional[int],
    cfg: dict,
    now: datetime,
    superseded_by: Optional[int] = None,
) -> bool:
    """Send an internal ops alert when an integration transitions to needs_reauth.

    Applies deduplication via ``should_send_alert``.
    Updates ``cfg`` in-place with ``token_reauth_alert_sent_at`` so the
    caller can persist the timestamp in the DB.

    When ``superseded_by`` is provided (i.e. a newer healthy integration
    exists for the same store), the alert is **suppressed entirely** — the
    merchant has already reconnected and the old row is harmless noise.

    Returns ``True`` if an email was actually dispatched.
    Fails silently (logs a warning) if RESEND_API_KEY is not set or the
    send fails — alert failures must not disrupt the token refresh flow.
    """
    if superseded_by:
        cfg["alert_suppressed"] = True
        cfg["alert_suppressed_reason"] = "superseded_by_newer_integration"
        cfg["alert_suppressed_by_integration_id"] = int(superseded_by)
        cfg["alert_suppressed_at"] = now.isoformat()
        logger.info(
            "[SALLA ALERT] suppressed (superseded) | tenant=%s integration_id=%s "
            "newer_integration_id=%s store=%s",
            tenant_id, integration_id, superseded_by, cfg.get("store_id"),
        )
        return False
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
    # Safeguard: if a real failure was recorded the counter must be ≥1.
    # We never want the owner to see "Refresh Attempts = 0 / Last Error = invalid_grant"
    # again — clamp to 1 in the alert payload as a last-resort display fallback.
    raw_attempts = int(cfg.get("token_refresh_attempts", 0) or 0)
    attempts     = max(raw_attempts, 1) if cfg.get("token_refresh_error") else raw_attempts
    error_msg    = cfg.get("token_refresh_error", "—") or "—"
    reason       = cfg.get("needs_reauth_reason", "—") or "—"
    first_failed = cfg.get("token_refresh_first_failed_at") or "—"
    is_revoked   = error_msg == "invalid_grant" or reason == "invalid_grant"
    revoked_note = (
        '<p style="color:#b45309;background:#fef3c7;padding:10px 14px;'
        'border-radius:6px;font-size:13px">'
        '<strong>invalid_grant</strong> = Salla revoked this refresh_token '
        '(single, definitive rejection). One attempt fully exhausts retries '
        '— there is no point retrying. The merchant must reinstall the app.'
        '</p>'
    ) if is_revoked else ""
    admin_url    = (
        "https://app.nahlah.ai/admin/salla/integrations/token-status"
        f"?tenant_id={tenant_id}"
    )

    html = f"""
<div style="font-family:Arial,sans-serif;max-width:600px;margin:0 auto;
            padding:28px;color:#1e293b;border:1px solid #fca5a5;
            border-radius:8px;background:#fff5f5">
  <h2 style="color:#dc2626;margin-top:0">⚠️ Internal Salla Token Reauth Required {OPS_REAUTH_TAG}</h2>
  <p style="color:#475569">
    <strong>Internal Ops Alert</strong> — a tenant integration can no longer refresh
    its Salla access token automatically. The merchant must reinstall or
    reauthorise the Nahla app. This message is for Nahla ops only.
  </p>
  {revoked_note}
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

    subject = build_ops_reauth_subject(tenant_id=tenant_id, store_id=store_id)
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
    from core.coupon_log_privacy import hash_identifier  # noqa: PLC0415

    logger.info(
        "[SALLA METRIC] event=salla_token_refresh_success tenant_hash=%s store_hash=%s",
        hash_identifier(tenant_id),
        hash_identifier(store_id),
    )


def log_metric_failed(tenant_id: int, store_id: str, attempts: int) -> None:
    """Emit a structured failure metric log."""
    from core.coupon_log_privacy import hash_identifier  # noqa: PLC0415

    logger.warning(
        "[SALLA METRIC] event=salla_token_refresh_failed tenant_hash=%s store_hash=%s attempts=%s",
        hash_identifier(tenant_id),
        hash_identifier(store_id),
        attempts,
    )


def log_metric_needs_reauth(tenant_id: int, store_id: str, reason: str) -> None:
    """Emit a structured needs_reauth metric log (CRITICAL level)."""
    from core.coupon_log_privacy import hash_identifier  # noqa: PLC0415

    logger.critical(
        "[SALLA METRIC] event=salla_token_refresh_needs_reauth tenant_hash=%s store_hash=%s reason=%s",
        hash_identifier(tenant_id),
        hash_identifier(store_id),
        reason,
    )

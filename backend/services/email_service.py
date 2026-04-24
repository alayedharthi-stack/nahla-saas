"""
services/email_service.py
──────────────────────────
Async transactional email service for Nahla SaaS.

Features:
  • Multi-sender support via sender_type parameter
  • Resend HTTP API (primary — no SMTP port restrictions)
  • Zoho SMTP fallback (aiosmtplib, STARTTLS/SSL)
  • Jinja2 HTML templates from backend/templates/emails/
  • Automatic retry (3 attempts, exponential back-off via tenacity)
  • Structured logging for every send attempt
  • Fire-and-forget helper (enqueue) — never blocks an HTTP response

Usage:
    from services.email_service import enqueue_email

    enqueue_email(
        to="merchant@example.com",
        subject="مرحباً بك في نحلة 🐝",
        template="welcome_email",
        variables={"merchant_name": "أحمد", "store_name": "متجر أحمد"},
        sender_type="welcome",          # optional — auto-resolved from template if omitted
    )
"""
from __future__ import annotations

import asyncio
import email.mime.multipart
import email.mime.text
import logging
import pathlib
from typing import Any, Dict, Optional

logger = logging.getLogger("nahla-email")

# ── Multi-sender mapping ──────────────────────────────────────────────────────
# Each key maps to a dedicated @nahlah.ai mailbox.
# All mailboxes must be verified in Resend under the nahlah.ai domain.
# None key = default fallback used when sender_type is unknown / not given.

SENDER_MAP: Dict[Optional[str], str] = {
    "welcome":  "نحلة <welcome@nahlah.ai>",
    "system":   "نحلة <system@nahlah.ai>",
    "alerts":   "نحلة <alerts@nahlah.ai>",
    "billing":  "نحلة <billing@nahlah.ai>",
    "growth":   "نحلة <growth@nahlah.ai>",
    "security": "نحلة <security@nahlah.ai>",
    None:       "نحلة <support@nahlah.ai>",   # default / fallback
}

# ── Template → sender_type auto-mapping ──────────────────────────────────────
# When caller doesn't pass sender_type, we look up the template name here.

TEMPLATE_SENDER: Dict[str, str] = {
    "welcome_email":                 "welcome",
    "salla_connected":               "system",
    "salla_reconnect_required":      "alerts",
    "first_whatsapp_message":        "growth",
    "order_created_from_whatsapp":   "system",
    "abandoned_cart_triggered":      "growth",
    "trial_expiring":                "billing",
    "trial_expired":                 "alerts",
    "daily_report":                  "growth",
}


def _resolve_sender(sender_type: Optional[str], template: Optional[str] = None) -> str:
    """
    Return the From address for a send operation.

    Priority:
      1. Explicit *sender_type* argument
      2. Auto-lookup via *template* name in TEMPLATE_SENDER
      3. Fallback: support@nahlah.ai
    """
    resolved = sender_type or TEMPLATE_SENDER.get(template or "")
    return SENDER_MAP.get(resolved) or SENDER_MAP[None]


# ── Lazy Jinja2 environment ───────────────────────────────────────────────────

_JINJA_ENV = None  # populated on first use


def _get_jinja_env():
    global _JINJA_ENV
    if _JINJA_ENV is not None:
        return _JINJA_ENV
    from jinja2 import Environment, FileSystemLoader, select_autoescape  # noqa: PLC0415
    templates_dir = pathlib.Path(__file__).parent.parent / "templates" / "emails"
    _JINJA_ENV = Environment(
        loader=FileSystemLoader(str(templates_dir)),
        autoescape=select_autoescape(["html"]),
    )
    return _JINJA_ENV


# ── Config accessor ───────────────────────────────────────────────────────────

def _cfg():
    from core import config  # noqa: PLC0415
    return config


# ── Public API ────────────────────────────────────────────────────────────────

async def send_email(
    to: str,
    subject: str,
    template: str,
    variables: Optional[Dict[str, Any]] = None,
    *,
    sender_type: Optional[str] = None,
    cc: Optional[str] = None,
    reply_to: Optional[str] = None,
) -> bool:
    """
    Render *template*.html and send it via Resend API (preferred) or Zoho SMTP.

    *sender_type* selects the From address (welcome / system / alerts / billing /
    growth / security).  If omitted, the template name is used to auto-select the
    sender.  Falls back to support@nahlah.ai when neither resolves.

    Returns True on success, False on final failure.
    Never raises — all errors are caught and logged.
    """
    cfg = _cfg()

    if not cfg.EMAIL_ENABLED:
        logger.debug("[Email] Skipped (not configured): to=%s subject=%s", to, subject)
        return False

    from_address = _resolve_sender(sender_type, template)

    try:
        html = _render(template, variables or {})
    except Exception as exc:
        logger.error("[Email] Template render error: template=%s error=%s", template, exc)
        return False

    if cfg.RESEND_API_KEY:
        return await _send_via_resend(
            to=to, subject=subject, html=html,
            from_address=from_address, cc=cc, reply_to=reply_to,
        )

    return await _send_with_retry(
        to=to, subject=subject, html=html,
        from_address=from_address, cc=cc, reply_to=reply_to,
    )


def enqueue_email(
    to: str,
    subject: str,
    template: str,
    variables: Optional[Dict[str, Any]] = None,
    *,
    sender_type: Optional[str] = None,
    cc: Optional[str] = None,
    reply_to: Optional[str] = None,
) -> None:
    """
    Schedule an email send as a non-blocking asyncio task.

    Call from any sync or async context — never raises, never delays the caller.
    """
    if not to or "@" not in to:
        logger.debug("[Email] Skipped (invalid address): %r", to)
        return

    coro = send_email(
        to, subject, template, variables,
        sender_type=sender_type, cc=cc, reply_to=reply_to,
    )

    try:
        loop = asyncio.get_event_loop()
        if loop.is_running():
            asyncio.create_task(coro)
        else:
            loop.run_until_complete(coro)
    except RuntimeError:
        asyncio.run(coro)


# ── Internal senders ─────────────────────────────────────────────────────────

async def _send_via_resend(
    to: str,
    subject: str,
    html: str,
    from_address: str,
    cc: Optional[str] = None,
    reply_to: Optional[str] = None,
) -> bool:
    """Send via Resend HTTP API."""
    import httpx  # noqa: PLC0415
    cfg = _cfg()

    payload: Dict[str, Any] = {
        "from":    from_address,
        "to":      [to],
        "subject": subject,
        "html":    html,
    }
    if cc:
        payload["cc"] = [cc]
    if reply_to:
        payload["reply_to"] = reply_to

    try:
        async with httpx.AsyncClient(timeout=15.0) as client:
            resp = await client.post(
                "https://api.resend.com/emails",
                headers={
                    "Authorization": f"Bearer {cfg.RESEND_API_KEY}",
                    "Content-Type":  "application/json",
                },
                json=payload,
            )
        if resp.status_code in (200, 201):
            logger.info("[Email/Resend] ✅ Sent: from=%s to=%s subject=%s id=%s",
                        from_address, to, subject, resp.json().get("id"))
            return True
        logger.error("[Email/Resend] ❌ HTTP %s: to=%s body=%s",
                     resp.status_code, to, resp.text[:300])
        return False
    except Exception as exc:
        logger.error("[Email/Resend] ❌ Exception: to=%s error=%s", to, exc)
        return False


async def _send_with_retry(
    to: str,
    subject: str,
    html: str,
    from_address: str,
    cc: Optional[str] = None,
    reply_to: Optional[str] = None,
    max_attempts: int = 3,
) -> bool:
    """SMTP send with exponential back-off (fallback when Resend is unavailable)."""
    from tenacity import (  # noqa: PLC0415
        retry, stop_after_attempt, wait_exponential, retry_if_exception_type,
    )

    @retry(
        stop=stop_after_attempt(max_attempts),
        wait=wait_exponential(multiplier=2, min=2, max=30),
        retry=retry_if_exception_type(Exception),
        reraise=False,
    )
    async def _attempt() -> None:
        await _smtp_send(
            to=to, subject=subject, html=html,
            from_address=from_address, cc=cc, reply_to=reply_to,
        )

    try:
        await _attempt()
        logger.info("[Email/SMTP] ✅ Sent: from=%s to=%s subject=%s",
                    from_address, to, subject)
        return True
    except Exception as exc:
        logger.error("[Email/SMTP] ❌ All %d attempts failed: to=%s error=%s",
                     max_attempts, to, exc)
        return False


async def _smtp_send(
    to: str,
    subject: str,
    html: str,
    from_address: str,
    cc: Optional[str] = None,
    reply_to: Optional[str] = None,
) -> None:
    """Low-level MIME build + SMTP delivery."""
    import aiosmtplib  # noqa: PLC0415
    cfg = _cfg()

    msg = email.mime.multipart.MIMEMultipart("alternative")
    msg["Subject"] = subject
    msg["From"]    = from_address
    msg["To"]      = to
    if cc:
        msg["Cc"] = cc
    if reply_to:
        msg["Reply-To"] = reply_to

    msg.attach(email.mime.text.MIMEText(html, "html", "utf-8"))

    smtp_kwargs: Dict[str, Any] = {
        "hostname": cfg.SMTP_HOST,
        "port":     cfg.SMTP_PORT,
        "username": cfg.SMTP_USER,
        "password": cfg.SMTP_PASS,
        "timeout":  20,
    }

    if cfg.SMTP_PORT == 465:
        smtp_kwargs["use_tls"] = True
        await aiosmtplib.send(msg, **smtp_kwargs)
    else:
        smtp_kwargs["start_tls"] = True
        await aiosmtplib.send(msg, **smtp_kwargs)


# ── Template rendering ────────────────────────────────────────────────────────

def _render(template_name: str, variables: Dict[str, Any]) -> str:
    """Render *template_name*.html with *variables*."""
    env = _get_jinja_env()
    fname = template_name if template_name.endswith(".html") else f"{template_name}.html"
    tpl = env.get_template(fname)

    cfg = _cfg()
    ctx = {
        "dashboard_url": cfg.DASHBOARD_URL,
        "support_email": "support@nahlah.ai",
        "current_year":  2026,
        **variables,
    }
    return tpl.render(**ctx)

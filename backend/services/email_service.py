"""
services/email_service.py
──────────────────────────
Async transactional email service for Nahla SaaS.

Features:
  • Jinja2 HTML templates from backend/templates/emails/
  • Zoho SMTP via aiosmtplib (STARTTLS on port 587, or SSL on 465)
  • Automatic retry (3 attempts, exponential back-off via tenacity)
  • Structured logging for every send attempt
  • Fire-and-forget helper (enqueue) — never blocks an HTTP response

Usage:
    # In any async route / service:
    from services.email_service import enqueue_email

    enqueue_email(
        to="merchant@example.com",
        subject="مرحباً بك في نحلة 🐝",
        template="welcome_email",
        variables={"merchant_name": "أحمد", "store_name": "متجر أحمد"},
    )
"""
from __future__ import annotations

import asyncio
import email.mime.multipart
import email.mime.text
import logging
import os
import pathlib
from typing import Any, Dict, Optional

logger = logging.getLogger("nahla-email")

# ── Lazy imports (heavy libs — only imported when email is actually needed) ──

def _get_jinja_env():
    """Return (and lazily build) the shared Jinja2 Environment."""
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

_JINJA_ENV = None  # populated on first use

# ── Config ───────────────────────────────────────────────────────────────────

def _cfg():
    from core import config  # noqa: PLC0415
    return config

# ── Core send (with tenacity retry) ─────────────────────────────────────────

async def send_email(
    to: str,
    subject: str,
    template: str,
    variables: Optional[Dict[str, Any]] = None,
    *,
    cc: Optional[str] = None,
    reply_to: Optional[str] = None,
) -> bool:
    """
    Render *template*.html and send it to *to* via Zoho SMTP.

    Returns True on success, False on final failure (after retries).
    Never raises — all errors are caught and logged.
    """
    cfg = _cfg()

    if not cfg.EMAIL_ENABLED:
        logger.debug("[Email] Skipped (SMTP not configured): to=%s subject=%s", to, subject)
        return False

    try:
        html = _render(template, variables or {})
    except Exception as exc:
        logger.error("[Email] Template render error: template=%s error=%s", template, exc)
        return False

    return await _send_with_retry(
        to=to,
        subject=subject,
        html=html,
        cc=cc,
        reply_to=reply_to,
    )


async def _send_with_retry(
    to: str,
    subject: str,
    html: str,
    cc: Optional[str] = None,
    reply_to: Optional[str] = None,
    max_attempts: int = 3,
) -> bool:
    """
    Attempt SMTP send up to *max_attempts* times with exponential back-off.
    Delays: 2s → 4s → 8s …
    """
    from tenacity import retry, stop_after_attempt, wait_exponential, retry_if_exception_type  # noqa: PLC0415

    @retry(
        stop=stop_after_attempt(max_attempts),
        wait=wait_exponential(multiplier=2, min=2, max=30),
        retry=retry_if_exception_type(Exception),
        reraise=False,
    )
    async def _attempt() -> None:
        await _smtp_send(to=to, subject=subject, html=html, cc=cc, reply_to=reply_to)

    try:
        await _attempt()
        logger.info("[Email] ✅ Sent: to=%s subject=%s", to, subject)
        return True
    except Exception as exc:
        logger.error(
            "[Email] ❌ All %d attempts failed: to=%s subject=%s error=%s",
            max_attempts, to, subject, exc,
        )
        return False


async def _smtp_send(
    to: str,
    subject: str,
    html: str,
    cc: Optional[str] = None,
    reply_to: Optional[str] = None,
) -> None:
    """Low-level: build MIME message and send over SMTP."""
    import aiosmtplib  # noqa: PLC0415

    cfg = _cfg()

    msg = email.mime.multipart.MIMEMultipart("alternative")
    msg["Subject"] = subject
    msg["From"]    = cfg.EMAIL_FROM
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
        # SSL from the start
        smtp_kwargs["use_tls"] = True
        await aiosmtplib.send(msg, **smtp_kwargs)
    else:
        # STARTTLS upgrade (port 587)
        smtp_kwargs["start_tls"] = True
        await aiosmtplib.send(msg, **smtp_kwargs)

    logger.debug("[Email] SMTP deliver OK: to=%s subject=%s", to, subject)


# ── Template rendering ───────────────────────────────────────────────────────

def _render(template_name: str, variables: Dict[str, Any]) -> str:
    """Render *template_name*.html with *variables*."""
    env = _get_jinja_env()
    # normalise — accept "welcome_email" or "welcome_email.html"
    fname = template_name if template_name.endswith(".html") else f"{template_name}.html"
    tpl = env.get_template(fname)

    cfg = _cfg()
    # Inject global context available in every template
    ctx = {
        "dashboard_url": cfg.DASHBOARD_URL,
        "support_email": cfg.SMTP_USER or "support@nahlah.ai",
        "current_year":  2026,
        **variables,
    }
    return tpl.render(**ctx)


# ── Fire-and-forget helper ───────────────────────────────────────────────────

def enqueue_email(
    to: str,
    subject: str,
    template: str,
    variables: Optional[Dict[str, Any]] = None,
    *,
    cc: Optional[str] = None,
    reply_to: Optional[str] = None,
) -> None:
    """
    Schedule an email send as a non-blocking asyncio task.

    Call this from any sync or async context — it never raises and never
    delays the caller's response.
    """
    if not to or "@" not in to:
        logger.debug("[Email] Skipped (invalid address): %r", to)
        return

    try:
        loop = asyncio.get_event_loop()
        if loop.is_running():
            asyncio.create_task(
                send_email(to, subject, template, variables, cc=cc, reply_to=reply_to)
            )
        else:
            loop.run_until_complete(
                send_email(to, subject, template, variables, cc=cc, reply_to=reply_to)
            )
    except RuntimeError:
        # No event loop (e.g. called from a sync test) — best-effort
        asyncio.run(send_email(to, subject, template, variables, cc=cc, reply_to=reply_to))

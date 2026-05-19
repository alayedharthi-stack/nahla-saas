"""
core/observability_sentry.py
────────────────────────────
Phase 1A: Sentry initialisation with strict PII scrubbing.

Why a dedicated module
──────────────────────
* Centralises the ``before_send`` hook so every header / context scrub
  rule lives in one place. Adding a new sensitive header (e.g. a
  future ``X-Nahla-2FA-Token``) is a one-line change.
* Lets us no-op gracefully when ``SENTRY_DSN`` is unset OR the
  ``sentry-sdk`` package is not installed (e.g. in dev / CI).
* Keeps ``backend/main.py`` short — the lifespan startup hook only has
  to call ``init_sentry()`` once.

What gets scrubbed
──────────────────
* Request headers: ``Authorization``, ``Cookie``, ``Set-Cookie``,
  ``X-Nahla-Key``, ``X-Hub-Signature``, ``X-Hub-Signature-256``,
  ``X-Salla-Signature``, ``X-Zid-Signature``, ``Proxy-Authorization``.
* Cookie payload (raw): always replaced with ``[scrubbed]``.
* Server name / host info: kept (useful for Railway region triage).
* User context: ``send_default_pii=False`` keeps Sentry from capturing
  the IP address or the username; we set ``user_id`` + ``tenant_id``
  manually from the JWT claims via :func:`set_request_user`.
"""
from __future__ import annotations

import logging
import os
from typing import Any, Dict, Optional

logger = logging.getLogger("nahla.sentry")

_SENSITIVE_HEADERS = frozenset({
    "authorization",
    "cookie",
    "set-cookie",
    "proxy-authorization",
    "x-nahla-key",
    "x-hub-signature",
    "x-hub-signature-256",
    "x-salla-signature",
    "x-zid-signature",
    "x-meta-signature",
})

_INITIALISED = False


def _scrub_headers(headers: Dict[str, str]) -> Dict[str, str]:
    """Drop any header whose lower-case name matches the sensitive set."""
    out: Dict[str, str] = {}
    for k, v in (headers or {}).items():
        if str(k or "").lower() in _SENSITIVE_HEADERS:
            out[k] = "[scrubbed]"
        else:
            out[k] = v
    return out


def _before_send(event: Dict[str, Any], _hint: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    """
    Final guard on the outbound payload — scrub anything that smells
    like a token or PII before it leaves the worker.

    Sentry ALSO has its own server-side scrubbing; doing it here is
    defense-in-depth so an org-level scrubbing rule that gets disabled
    accidentally cannot leak a token.
    """
    try:
        request = event.get("request") or {}
        headers = request.get("headers") or {}
        if headers:
            request["headers"] = _scrub_headers(headers)

        # Cookies are forwarded separately from headers in some
        # integrations; never let the raw value through.
        if "cookies" in request:
            request["cookies"] = "[scrubbed]"

        # Some integrations include a ``data`` field with the raw POST
        # body. Login bodies contain plaintext passwords — drop them
        # entirely. We can't selectively strip individual JSON keys
        # without parsing, and a strip here is cheap.
        path = (request.get("url") or "")
        if isinstance(path, str) and ("/auth/login" in path or "/auth/reset-password" in path):
            request["data"] = "[scrubbed]"

        event["request"] = request
    except Exception as exc:  # noqa: BLE001
        logger.warning("[sentry] before_send scrub failed: %s — passing event through", exc)
    return event


def init_sentry() -> bool:
    """
    Initialise Sentry once per process. Returns ``True`` when the SDK
    was wired up, ``False`` when we deliberately stayed quiet (DSN
    unset, package missing, init crash).
    """
    global _INITIALISED  # noqa: PLW0603
    if _INITIALISED:
        return True

    dsn = (os.environ.get("SENTRY_DSN") or "").strip()
    if not dsn:
        logger.info("[sentry] SENTRY_DSN not set — error monitoring disabled.")
        return False

    try:
        import sentry_sdk  # noqa: PLC0415
        from sentry_sdk.integrations.fastapi import FastApiIntegration  # noqa: PLC0415
        from sentry_sdk.integrations.starlette import StarletteIntegration  # noqa: PLC0415
        from sentry_sdk.integrations.logging import LoggingIntegration  # noqa: PLC0415
    except ImportError as exc:
        logger.warning("[sentry] sentry-sdk not installed (%s) — disabling.", exc)
        return False

    env = (os.environ.get("ENVIRONMENT", "development") or "development").strip().lower()
    release = (
        os.environ.get("RAILWAY_GIT_COMMIT_SHA")
        or os.environ.get("GIT_COMMIT_SHA")
        or os.environ.get("COMMIT_SHA")
        or None
    )

    # Sample rate: keep traces lean to fit the free tier and avoid
    # burning quota on healthchecks / liveness probes. 10% is enough
    # to catch real performance regressions without being noisy.
    try:
        traces_sample_rate = float(os.environ.get("SENTRY_TRACES_SAMPLE_RATE", "0.1"))
    except ValueError:
        traces_sample_rate = 0.1

    try:
        sentry_sdk.init(
            dsn=dsn,
            environment=env,
            release=release,
            traces_sample_rate=traces_sample_rate,
            send_default_pii=False,        # never auto-send IP / cookies / username
            attach_stacktrace=True,
            max_breadcrumbs=50,
            before_send=_before_send,
            integrations=[
                StarletteIntegration(transaction_style="endpoint"),
                FastApiIntegration(transaction_style="endpoint"),
                LoggingIntegration(level=logging.INFO, event_level=logging.ERROR),
            ],
        )
        logger.info(
            "[sentry] initialised env=%s release=%s traces_sample_rate=%s",
            env, release or "(unset)", traces_sample_rate,
        )
        _INITIALISED = True
        return True
    except Exception as exc:  # noqa: BLE001
        logger.warning("[sentry] init failed: %s — error monitoring disabled.", exc)
        return False


def set_request_user(*, user_id, tenant_id, role: str | None = None) -> None:
    """
    Attach a minimal, PII-free user context to the current scope.
    Called from the JWT enforcement middleware.

    We NEVER include the email or phone number — Sentry only needs an
    opaque identifier to group events. ``role`` helps triage (admin
    incidents go to a separate alerting rule).
    """
    if not _INITIALISED:
        return
    try:
        import sentry_sdk  # noqa: PLC0415
        sentry_sdk.set_user({
            "id":        str(user_id) if user_id is not None else None,
            "tenant_id": str(tenant_id) if tenant_id is not None else None,
            "role":      role or "unknown",
        })
    except Exception:  # noqa: silent-ok — sentry context is telemetry; never propagate to the request path
        pass

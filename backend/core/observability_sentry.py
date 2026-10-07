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
* Request URL, query string and body; exception messages; stack-frame
  variables; breadcrumbs (also at capture time); log entries; extra,
  contexts and spans: redacted with ``core.log_redaction``. A failing
  scrub withholds the payload and sends a minimal event instead.
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
    """Drop any header whose lower-case name matches the sensitive set.

    Other values (e.g. ``Referer`` holding an OAuth callback URL) are kept
    but redacted with ``core.log_redaction``.
    """
    from core.log_redaction import redact_secrets  # noqa: PLC0415

    out: Dict[str, str] = {}
    for k, v in (headers or {}).items():
        if str(k or "").lower() in _SENSITIVE_HEADERS:
            out[k] = "[scrubbed]"
        else:
            out[k] = redact_secrets(v) if isinstance(v, str) else v
    return out


def _scrub_request(request: Dict[str, Any]) -> Dict[str, Any]:
    from core.log_redaction import redact_secrets, redact_value  # noqa: PLC0415

    headers = request.get("headers") or {}
    if headers:
        request["headers"] = _scrub_headers(headers)

    # Cookies are forwarded separately from headers in some
    # integrations; never let the raw value through.
    if "cookies" in request:
        request["cookies"] = "[scrubbed]"

    # The request URL and query string carry OAuth ``code`` / ``state`` on
    # callbacks (e.g. the catalog consent and embedded signup callbacks).
    url = request.get("url")
    if isinstance(url, str) and url:
        request["url"] = redact_secrets(url)
    query = request.get("query_string")
    if isinstance(query, (bytes, bytearray)):
        query = query.decode("latin-1")
    if isinstance(query, str) and query:
        from core.log_redaction import redact_raw_query  # noqa: PLC0415

        request["query_string"] = redact_secrets(redact_raw_query(query))
    elif query:
        request["query_string"] = redact_value(query)

    # Some integrations include a ``data`` field with the raw POST
    # body. Login bodies contain plaintext passwords — drop them
    # entirely. Other bodies are redacted key by key.
    path = (request.get("url") or "")
    if isinstance(path, str) and ("/auth/login" in path or "/auth/reset-password" in path):
        request["data"] = "[scrubbed]"
    elif request.get("data") is not None:
        request["data"] = redact_value(request["data"])
    return request


# Frames whose local variables routinely hold OAuth codes, state, tokens,
# app secrets or encryption keys. Their variables are withheld entirely
# rather than redacted by name: the stack trace (function, file, line) stays.
_WITHHELD_FRAME_MODULES = (
    "services.meta_catalog_consent",
    "routers.meta_catalog_consent",
    "core.meta_catalog_consent_config",
    "core.whatsapp_oauth_nonce",
    "core.wa_token_crypto",
    "core.totp_crypto",
    "core.secrets",
    "core.config",
    "core.review_environment",
    "routers.whatsapp_embedded",
    "services.meta_oauth_redirect",
    "services.whatsapp_platform.wa_connection_secrets",
    "services.whatsapp_platform.token_manager",
    "cryptography",
)
_WITHHELD_FRAME_FILES = tuple(
    "/" + m.replace(".", "/") + ext for m in _WITHHELD_FRAME_MODULES for ext in (".py", "/")
)
# Generic local names that hold key material or one-time secrets in any frame.
_SENSITIVE_LOCAL_NAMES = frozenset({
    "key", "raw_key", "dev_key", "wa_key", "totp_key", "seed", "plain", "plaintext",
    "nonce", "signature", "sig", "body_b64", "sig_b64", "proof",
})


def _withheld_frame(frame: Dict[str, Any]) -> bool:
    module = str(frame.get("module") or "")
    if any(module == m or module.startswith(m + ".") for m in _WITHHELD_FRAME_MODULES):
        return True
    paths = " ".join(str(frame.get(k) or "") for k in ("abs_path", "filename")).replace("\\", "/")
    return any(marker in paths for marker in _WITHHELD_FRAME_FILES)


def _scrub_frames(frames: Any) -> None:
    from core.log_redaction import redact_value  # noqa: PLC0415

    for frame in frames or []:
        if not isinstance(frame, dict) or not frame.get("vars"):
            continue
        if _withheld_frame(frame):
            frame["vars"] = {"[withheld]": "sensitive frame"}
            continue
        variables = frame["vars"]
        if isinstance(variables, dict):
            variables = {
                k: ("[scrubbed]" if str(k).strip().lower() in _SENSITIVE_LOCAL_NAMES else v)
                for k, v in variables.items()
            }
        frame["vars"] = redact_value(variables)


def _scrub_event(event: Dict[str, Any]) -> Dict[str, Any]:
    from core.log_redaction import redact_secrets, redact_value  # noqa: PLC0415

    event["request"] = _scrub_request(event.get("request") or {})

    for exc in ((event.get("exception") or {}).get("values") or []):
        if isinstance(exc, dict):
            if exc.get("value") is not None:
                exc["value"] = redact_secrets(exc["value"])
            _scrub_frames((exc.get("stacktrace") or {}).get("frames"))
    for thread in ((event.get("threads") or {}).get("values") or []):
        if isinstance(thread, dict):
            _scrub_frames((thread.get("stacktrace") or {}).get("frames"))

    crumbs = event.get("breadcrumbs")
    values = crumbs.get("values") if isinstance(crumbs, dict) else crumbs
    if isinstance(values, list):
        cleaned = [_scrub_breadcrumb(c) for c in values]
        cleaned = [c for c in cleaned if c is not None]
        if isinstance(crumbs, dict):
            crumbs["values"] = cleaned
        else:
            event["breadcrumbs"] = cleaned

    logentry = event.get("logentry")
    if isinstance(logentry, dict):
        for key in ("message", "formatted"):
            if logentry.get(key) is not None:
                logentry[key] = redact_secrets(logentry[key])
        if logentry.get("params") is not None:
            logentry["params"] = redact_value(logentry["params"])
    # ``transaction`` (also the name of performance events, which reuse this
    # hook) and ``culprit`` can be a request URL with its query string.
    for key in ("message", "transaction", "culprit"):
        if isinstance(event.get(key), str):
            event[key] = redact_secrets(event[key])
    for key in ("extra", "contexts", "tags"):
        if event.get(key):
            event[key] = redact_value(event[key])
    for span in event.get("spans") or []:
        if isinstance(span, dict):
            for key in ("description", "data"):
                if span.get(key) is not None:
                    span[key] = redact_value(span[key])
    return event


def _minimal_event(event: Dict[str, Any]) -> Dict[str, Any]:
    """What survives when scrubbing itself failed: no request, frames or text."""
    types = []
    try:
        for exc in ((event.get("exception") or {}).get("values") or []):
            if isinstance(exc, dict) and isinstance(exc.get("type"), str):
                types.append(exc["type"][:120])
    except Exception:  # noqa: BLE001
        types = []
    minimal: Dict[str, Any] = {
        "level": event.get("level") if isinstance(event.get("level"), str) else "error",
        "message": "[sentry] event scrub failed; payload withheld",
        "tags": {"scrub_failed": "true"},
    }
    if types:
        minimal["extra"] = {"exception_types": types}
    for key in ("environment", "release", "platform", "timestamp", "event_id"):
        if isinstance(event.get(key), str):
            minimal[key] = event[key]
    return minimal


def _before_send(event: Dict[str, Any], _hint: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    """
    Final guard on the outbound payload — scrub anything that smells
    like a token or PII before it leaves the worker.

    Sentry ALSO has its own server-side scrubbing; doing it here is
    defense-in-depth so an org-level scrubbing rule that gets disabled
    accidentally cannot leak a token. Request URL / query / body,
    exception messages, stack-frame variables, breadcrumbs, log entries,
    extra/contexts and spans all go through ``core.log_redaction``. If
    scrubbing fails the original payload is withheld (fail closed) and a
    minimal event with only the exception types is sent instead.
    """
    try:
        return _scrub_event(event)
    except Exception as exc:  # noqa: BLE001
        logger.warning("[sentry] before_send scrub failed: %s — sending minimal event", type(exc).__name__)
        return _minimal_event(event)


def _scrub_breadcrumb(crumb: Any) -> Optional[Dict[str, Any]]:
    from core.log_redaction import redact_secrets, redact_value  # noqa: PLC0415

    if not isinstance(crumb, dict):
        return None
    try:
        out = dict(crumb)
        if out.get("message") is not None:
            out["message"] = redact_secrets(out["message"])
        if out.get("data") is not None:
            out["data"] = redact_value(out["data"])
        return out
    except Exception:  # noqa: silent-ok — a breadcrumb that cannot be scrubbed is dropped, never sent
        return None


def _before_breadcrumb(crumb: Dict[str, Any], _hint: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    """HTTP/logging breadcrumbs carry request URLs; redact at capture time."""
    return _scrub_breadcrumb(crumb)


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
            before_send_transaction=_before_send,
            before_breadcrumb=_before_breadcrumb,
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

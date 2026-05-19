"""
core/config.py
──────────────
All environment variable reads and module-level constants for the Nahla backend.
Import from here — never call os.environ.get() scattered across route files.

Phase 1A security: critical secrets fail-fast in production. The
``preflight_check.py`` script ALSO validates these before uvicorn binds,
so a misconfigured deploy never serves a single request.
"""
import os
import secrets as _secrets_mod

import logging as _logging
_cfg_logger = _logging.getLogger("nahla-backend")

# ── Environment posture (read first — used by guards below) ────────────────────
# Defined here so the security guards can fail-fast in production without
# blocking local dev. Re-exported at the bottom of the file as the
# canonical ENVIRONMENT / IS_PRODUCTION constants.
_ENVIRONMENT_RAW = os.environ.get("ENVIRONMENT", "development").strip().lower()
_IS_PRODUCTION = _ENVIRONMENT_RAW == "production"


def _fatal_or_warn(message: str) -> None:
    """Refuse to boot in production; warn loudly elsewhere."""
    if _IS_PRODUCTION:
        _cfg_logger.critical("[BOOT/secrets] %s — refusing to boot in production.", message)
        raise RuntimeError(f"SECURITY: {message}")
    _cfg_logger.warning("[BOOT/secrets] %s (allowed in non-production).", message)


# ── JWT ────────────────────────────────────────────────────────────────────────
def _safe_token_hex(nbytes: int = 32) -> str:
    token_hex = getattr(_secrets_mod, "token_hex", None)
    if callable(token_hex):
        return token_hex(nbytes)
    return os.urandom(nbytes).hex()


# Recognised dev/placeholder values that MUST never reach production. Keeping
# this list central so preflight_check.py and config.py agree on what counts
# as "still the example value".
_FORBIDDEN_JWT_SECRETS = frozenset({
    "",
    "change-me",
    "change-me-to-a-long-random-string",
    "secret",
    "dev",
    "dev-secret",
    "nahla-dev",
})

_jwt_secret_env = (os.environ.get("JWT_SECRET", "") or "").strip()
if (not _jwt_secret_env) or _jwt_secret_env.lower() in _FORBIDDEN_JWT_SECRETS:
    _fatal_or_warn(
        "JWT_SECRET is missing or set to a known dev placeholder. "
        "Set a 64-char random value in Railway → Variables."
    )
# In non-production we generate an ephemeral random secret so the worker
# boots, but warn loudly that all sessions die on restart.
JWT_SECRET    = _jwt_secret_env or _safe_token_hex(32)
JWT_ALGORITHM = "HS256"
# Phase 1A: tightened from 168h (7 days) to 24h. Refresh-token rotation
# arrives in Phase 2 and will let us drop access tokens to 15 minutes.
JWT_EXPIRE_H  = int(os.environ.get("JWT_EXPIRE_HOURS", "24"))

# ── Registration gate ──────────────────────────────────────────────────────────
REQUIRE_INVITE  = os.environ.get("REQUIRE_INVITE", "true").lower() != "false"
INVITE_EXPIRE_H = 168  # 7 days

# ── Admin bootstrap credentials ────────────────────────────────────────────────
ADMIN_EMAIL    = os.environ.get("ADMIN_EMAIL",    "admin@nahlah.ai")
_admin_pass_env = (os.environ.get("ADMIN_PASSWORD", "") or "").strip()

# Forbidden defaults — anything that has shipped in docs / .env.example or
# has been flagged in the past as a placeholder. Production refuses to boot
# when ADMIN_PASSWORD matches one of these.
_FORBIDDEN_ADMIN_PASSWORDS = frozenset({
    "",
    "change-me",
    "nahla-admin-2026",
    "12345678",
    "admin",
    "password",
})
if _admin_pass_env.lower() in _FORBIDDEN_ADMIN_PASSWORDS:
    _fatal_or_warn(
        "ADMIN_PASSWORD is empty or matches a forbidden placeholder. "
        "Set a strong, random ADMIN_PASSWORD in Railway → Variables. "
        "Until 2FA ships in Phase 2 the admin login is single-factor."
    )
ADMIN_PASSWORD = _admin_pass_env or ""

# ── Notification services ──────────────────────────────────────────────────────
RESEND_API_KEY = os.environ.get("RESEND_API_KEY", "")
EMAIL_FROM     = os.environ.get("EMAIL_FROM", "نحلة <support@nahlah.ai>")
DASHBOARD_URL  = os.environ.get("DASHBOARD_URL", "https://app.nahlah.ai")

# ── Zoho SMTP (transactional email) ───────────────────────────────────────────
# Set all four vars in Railway / .env to enable outbound email.
# Zoho SA:  host=smtp.zoho.sa  port=587  (STARTTLS)
# Zoho COM: host=smtp.zoho.com port=587  (STARTTLS) or port=465 (SSL)
SMTP_HOST      = os.environ.get("SMTP_HOST", "smtp.zoho.com")
SMTP_PORT      = int(os.environ.get("SMTP_PORT", "587"))
SMTP_USER      = os.environ.get("SMTP_USER", "")       # e.g. hello@nahlah.ai
SMTP_PASS      = os.environ.get("SMTP_PASS", "")       # App-specific password
SMTP_USE_TLS   = os.environ.get("SMTP_USE_TLS", "true").lower() != "false"
EMAIL_ENABLED  = bool(RESEND_API_KEY or (SMTP_USER and SMTP_PASS))

if not EMAIL_ENABLED:
    _cfg_logger.warning(
        "[Email] Email is DISABLED — set RESEND_API_KEY (recommended) "
        "or SMTP_USER + SMTP_PASS in environment variables."
    )
elif RESEND_API_KEY:
    _cfg_logger.info("[Email] Using Resend API for outbound email.")
else:
    _cfg_logger.info("[Email] Using SMTP for outbound email.")

# WhatsApp Business (Meta Cloud API)
# WA_TOKEN: platform-level token used as fallback by token_manager and for
#   platform notifications (wa_notify.py).  NOT used for per-tenant operations.
WA_TOKEN           = os.environ.get("WHATSAPP_TOKEN", "")
# WA_PHONE_ID: Nahla's own phone — only for platform-to-merchant notifications.
WA_PHONE_ID        = os.environ.get("PHONE_NUMBER_ID", "")
_wa_verify_env = (os.environ.get("WHATSAPP_VERIFY_TOKEN", "") or "").strip()
# Forbidden defaults: the 2025 placeholder that previously shipped in
# docs / .env.example. Anyone hitting Meta with this value would be
# trivially impersonating Nahla, so production refuses to boot with it.
_FORBIDDEN_WA_VERIFY_TOKENS = frozenset({"", "nahla2025", "verify-me", "test"})
if _wa_verify_env.lower() in _FORBIDDEN_WA_VERIFY_TOKENS:
    _fatal_or_warn(
        "WHATSAPP_VERIFY_TOKEN is empty or set to a known placeholder. "
        "Generate a long random string in Railway → Variables and re-register "
        "the webhook in the Meta Business app dashboard."
    )
WA_VERIFY_TOKEN    = _wa_verify_env or "dev-verify-token-do-not-use-in-prod"
# WA_BUSINESS_ACCOUNT_ID: kept for the legacy "direct" connection flow only.
# Embedded Signup tenants use their own WABA stored in whatsapp_connections.
WA_BUSINESS_ACCOUNT_ID = os.environ.get("WA_BUSINESS_ACCOUNT_ID", "")

# ── Salla OAuth ────────────────────────────────────────────────────────────────
SALLA_CLIENT_ID      = os.environ.get("SALLA_CLIENT_ID", "")
SALLA_CLIENT_SECRET  = os.environ.get("SALLA_CLIENT_SECRET", "")
SALLA_REDIRECT_URI   = os.environ.get(
    "SALLA_REDIRECT_URI",
    "https://api.nahlah.ai/oauth/salla/callback",
)
SALLA_WEBHOOK_SECRET = os.environ.get("SALLA_WEBHOOK_SECRET", "")
# ── Salla webhook signature enforcement ───────────────────────────────────
# Production launch: set SALLA_WEBHOOK_ENFORCE_SIGNATURE=true
#                    and SALLA_WEBHOOK_ALLOW_MISSING_SIGNATURE=false
SALLA_WEBHOOK_ENFORCE_SIGNATURE      = os.environ.get("SALLA_WEBHOOK_ENFORCE_SIGNATURE", "false").lower() == "true"
SALLA_WEBHOOK_ALLOW_MISSING_SIGNATURE = os.environ.get("SALLA_WEBHOOK_ALLOW_MISSING_SIGNATURE", "true").lower() == "true"

# ── Salla OAuth (Sync) app — webhook secret (Dual Architecture) ──────────────
# The SECOND Salla app (SALLA_OAUTH_CLIENT_ID below) has its OWN webhook
# secret in Partner Portal, completely separate from SALLA_WEBHOOK_SECRET.
# It is consumed by POST /webhook/salla-oauth — NEVER mixed with the
# Communication App's webhook handler at POST /webhook/salla.
SALLA_OAUTH_WEBHOOK_SECRET = os.environ.get("SALLA_OAUTH_WEBHOOK_SECRET", "")

# ── Salla TEST app (separate credentials — does not affect production app) ──
SALLA_TEST_CLIENT_ID     = os.environ.get("SALLA_TEST_CLIENT_ID", "")
SALLA_TEST_CLIENT_SECRET = os.environ.get("SALLA_TEST_CLIENT_SECRET", "")
SALLA_TEST_REDIRECT_URI  = os.environ.get(
    "SALLA_TEST_REDIRECT_URI",
    "https://api.nahlah.ai/oauth/salla/test/callback",
)

# ── Salla OAuth (Sync) app — separate Custom OAuth app ──────────────────────
# Dual Integration Architecture:
#   • SALLA_CLIENT_ID above       → Communication App (embedded iframe + introspect).
#                                   Cannot deliver offline_access / refresh_token.
#   • SALLA_OAUTH_CLIENT_ID here  → SECOND, completely separate General/Custom
#                                   OAuth app whose ONLY job is to deliver a
#                                   long-lived refresh_token so that
#                                   StoreSyncService / orders poller /
#                                   background automations can keep calling
#                                   https://api.salla.dev/admin/v2/* even when
#                                   the merchant is not actively in the iframe.
#
# These two sets of credentials MUST stay isolated — never share or fall
# back between them at runtime.  The legacy names SALLA_API_CLIENT_ID /
# SALLA_API_CLIENT_SECRET / SALLA_API_REDIRECT_URI are still read here as a
# transitional fallback so that an existing deployment does not break the
# moment this rename ships, but new deployments should set the
# SALLA_OAUTH_* names exclusively.
SALLA_OAUTH_CLIENT_ID     = (
    os.environ.get("SALLA_OAUTH_CLIENT_ID", "")
    or os.environ.get("SALLA_API_CLIENT_ID", "")
)
SALLA_OAUTH_CLIENT_SECRET = (
    os.environ.get("SALLA_OAUTH_CLIENT_SECRET", "")
    or os.environ.get("SALLA_API_CLIENT_SECRET", "")
)
SALLA_OAUTH_REDIRECT_URI  = (
    os.environ.get("SALLA_OAUTH_REDIRECT_URI", "")
    or os.environ.get("SALLA_API_REDIRECT_URI", "")
    or "https://api.nahlah.ai/api/salla/oauth/callback"
)

# Where to redirect after Salla OAuth completes (the embedded app landing page).
# Set SALLA_EMBEDDED_URL in Railway env to override.
# For Salla embedded apps this is typically the partner app iframe URL.
SALLA_EMBEDDED_URL = os.environ.get(
    "SALLA_EMBEDDED_URL",
    "https://app.nahlah.ai",
)

# ── Zid OAuth ──────────────────────────────────────────────────────────────────
ZID_CLIENT_ID      = os.environ.get("ZID_CLIENT_ID", "")
ZID_CLIENT_SECRET  = os.environ.get("ZID_CLIENT_SECRET", "")
ZID_REDIRECT_URI   = os.environ.get("ZID_REDIRECT_URI", "https://api.nahlah.ai/zid/redirect")
ZID_WEBHOOK_SECRET = os.environ.get("ZID_WEBHOOK_SECRET", "")

# ── API key protection ─────────────────────────────────────────────────────────
API_SECRET_KEY = os.environ.get("API_SECRET_KEY", "")

# ── AI orchestrator ────────────────────────────────────────────────────────────
ORCHESTRATOR_URL = os.environ.get("ORCHESTRATOR_URL", "http://localhost:8016")
ENVIRONMENT      = _ENVIRONMENT_RAW or "development"
IS_PRODUCTION    = _IS_PRODUCTION

# ── Moyasar ────────────────────────────────────────────────────────────────────
MOYASAR_SECRET_KEY      = os.environ.get("MOYASAR_SECRET_KEY", "")
MOYASAR_PUBLISHABLE_KEY = os.environ.get("MOYASAR_PUBLISHABLE_KEY", "")
MOYASAR_WEBHOOK_SECRET  = os.environ.get("MOYASAR_WEBHOOK_SECRET", "")

# ── HyperPay ───────────────────────────────────────────────────────────────────
HYPERPAY_ACCESS_TOKEN   = os.environ.get("HYPERPAY_ACCESS_TOKEN", "")
HYPERPAY_ENTITY_ID      = os.environ.get("HYPERPAY_ENTITY_ID", "")
HYPERPAY_WEBHOOK_SECRET = os.environ.get("HYPERPAY_WEBHOOK_SECRET", "")
HYPERPAY_LIVE_MODE      = os.environ.get("HYPERPAY_LIVE_MODE", "false").lower() == "true"

# ── Meta / WhatsApp Embedded Signup ────────────────────────────────────────────
META_APP_ID              = os.environ.get("META_APP_ID", "")
META_APP_SECRET          = os.environ.get("META_APP_SECRET", "")
META_GRAPH_API_VERSION   = os.environ.get("META_GRAPH_API_VERSION", "v20.0")
# Configuration ID from Meta Business Manager → WhatsApp → Embedded Signup
# (Required for FB Login for Business / WhatsApp Embedded Signup).
#
# May 2026 — we accept TWO env names for the same value so existing
# Railway deployments don't break when teams rename their secrets:
#   * ``META_EMBEDDED_SIGNUP_CONFIG_ID``  → preferred name (matches
#     Meta's own documentation for FB Login for Business config IDs).
#   * ``META_WA_CONFIG_ID``               → legacy name kept for
#     backwards compatibility.
# The first non-empty value wins; both are exported as the same Python
# symbol so call sites don't have to choose.
META_EMBEDDED_SIGNUP_CONFIG_ID = (
    os.environ.get("META_EMBEDDED_SIGNUP_CONFIG_ID", "")
    or os.environ.get("META_WA_CONFIG_ID", "")
)
META_WA_CONFIG_ID        = META_EMBEDDED_SIGNUP_CONFIG_ID  # legacy alias

# Where Meta redirects after the user approves the OAuth dialog in
# the SERVER-SIDE embedded-signup flow (``GET /whatsapp/embedded/
# oauth/callback``). Must be present in the "Valid OAuth Redirect
# URIs" list under FB Login for Business settings, otherwise Meta
# rejects the redirect with ``redirect_uri mismatch``. Defaults to
# the Nahla backend public URL with the callback path appended; can
# be overridden when running in a non-prod tunnel.
META_REDIRECT_URI        = (
    os.environ.get("META_REDIRECT_URI")
    or f"{os.environ.get('BACKEND_URL', '').rstrip('/')}/whatsapp/embedded/oauth/callback"
)

# Feature flag — when ``False`` (or when ``META_EMBEDDED_SIGNUP_CONFIG_ID``
# is empty) the dashboard hides the "ربط مع Meta" tab and the
# ``/embedded/config`` endpoint returns ``embedded_signup_enabled=False``
# along with a merchant-friendly Arabic disabled-reason string.
#
# Read by ``is_meta_embedded_signup_enabled()`` below — call sites
# should use that helper rather than reading the env var directly so
# the config-id presence check stays consistent in one place.
_META_DIRECT_SIGNUP_FORCE_ENV = os.environ.get("META_EMBEDDED_SIGNUP_ENABLED", "")


def is_meta_embedded_signup_enabled() -> bool:
    """Return True iff the dashboard should expose the FB-Login /
    Embedded Signup tab.

    Rules:
      * If the merchant did NOT set ``META_EMBEDDED_SIGNUP_CONFIG_ID``
        (or the legacy ``META_WA_CONFIG_ID``) → always disabled. We
        will not open a Meta OAuth popup that we know Meta is going
        to reject with the generic ``BSPs or TPs`` entitlement error.
      * If they DID set the config id AND ``META_APP_ID`` /
        ``META_APP_SECRET`` are present → enabled by default.
      * If ``META_EMBEDDED_SIGNUP_ENABLED`` is explicitly set to
        ``false`` (or ``0``, ``no``, ``off``) → force-disabled even
        when the config id is present. Used by ops as a kill switch
        while the Meta entitlement is still under review.
    """
    if not META_EMBEDDED_SIGNUP_CONFIG_ID:
        return False
    if not META_APP_ID or not META_APP_SECRET:
        return False
    forced = (_META_DIRECT_SIGNUP_FORCE_ENV or "").strip().lower()
    if forced in {"0", "false", "no", "off", "disabled"}:
        return False
    return True


def meta_embedded_disabled_reason() -> str:
    """Arabic merchant-facing message explaining WHY the Meta tab is
    hidden / disabled. Returned alongside the config payload so the
    dashboard can render the same wording everywhere.
    """
    if not META_APP_ID or not META_APP_SECRET:
        return (
            "إعدادات تطبيق Meta غير مكتملة على الخادم. "
            "الربط المباشر مع Meta غير مفعّل بعد."
        )
    if not META_EMBEDDED_SIGNUP_CONFIG_ID:
        return (
            "الربط المباشر مع Meta غير مفعّل بعد. "
            "الرجاء ضبط META_EMBEDDED_SIGNUP_CONFIG_ID على الخادم. "
            "حتى ذلك الحين، استخدم الربط عبر 360dialog."
        )
    forced = (_META_DIRECT_SIGNUP_FORCE_ENV or "").strip().lower()
    if forced in {"0", "false", "no", "off", "disabled"}:
        return (
            "الربط المباشر مع Meta قيد التفعيل من قِبل فريق نحلة. "
            "استخدم الربط عبر 360dialog حالياً."
        )
    return ""

# ── 360dialog / WhatsApp Coexistence ───────────────────────────────────────────
# Internal / platform-managed provider configuration. Never expose these values
# to merchants in the dashboard.
BACKEND_URL = os.environ.get(
    "BACKEND_URL",
    "https://nahla-saas-production.up.railway.app",
)
D360_API_BASE_URL = os.environ.get("D360_API_BASE_URL", "https://waba-v2.360dialog.io")
D360_PARTNER_HUB_BASE = os.environ.get("D360_PARTNER_HUB_BASE", "https://hub.360dialog.com")
# Partner API key — used to generate channel API keys on behalf of merchants.
D360_PARTNER_API_KEY = os.environ.get("D360_PARTNER_API_KEY", "")
# Partner ID visible in the hub URL: hub.360dialog.com/dashboard/app/{PARTNER_ID}
D360_PARTNER_ID = os.environ.get("D360_PARTNER_ID", "")
# Internal shared secret sent by 360dialog via custom webhook header configured
# by Nahla during channel activation.
D360_WEBHOOK_INTERNAL_SECRET = os.environ.get("D360_WEBHOOK_INTERNAL_SECRET", "")
# Beta rollout flags
D360_COHOST_ENABLED = os.environ.get("D360_COHOST_ENABLED", "false").lower() == "true"
D360_COHOST_ALLOW_SELF_REQUEST = os.environ.get("D360_COHOST_ALLOW_SELF_REQUEST", "true").lower() == "true"

# ── Phase 1B — webhook signature enforcement flags ─────────────────────────────
# Default for every new flag is "audit-only" (verify + record telemetry but do
# NOT reject). After 7 days of clean audit telemetry per provider, ops promote
# the flag to ENFORCE=true via Railway env. See
# `docs/security/WEBHOOK_SECURITY.md` (added in Phase 1B-cleanup) for the
# operator runbook.
def _bool_env(name: str, default: str = "false") -> bool:
    return (os.environ.get(name, default) or default).strip().lower() in (
        "1", "true", "yes", "on",
    )


META_WEBHOOK_ENFORCE_SIGNATURE       = _bool_env("META_WEBHOOK_ENFORCE_SIGNATURE", "false")
META_WEBHOOK_ALLOW_MISSING_SIGNATURE = _bool_env("META_WEBHOOK_ALLOW_MISSING_SIGNATURE", "true")

# Zid: handler is a no-op stub today, so we ship audit-only first and only
# require ZID_WEBHOOK_SECRET at boot once Phase 2 lights up real ingest.
ZID_WEBHOOK_ENFORCE_SIGNATURE = _bool_env("ZID_WEBHOOK_ENFORCE_SIGNATURE", "false")
ZID_WEBHOOK_REQUIRED_AT_BOOT  = _bool_env("ZID_WEBHOOK_REQUIRED_AT_BOOT", "false")

# Moyasar / HyperPay default to false until audit windows close.
MOYASAR_WEBHOOK_REQUIRE_VERIFIED  = _bool_env("MOYASAR_WEBHOOK_REQUIRE_VERIFIED", "false")
HYPERPAY_WEBHOOK_REQUIRE_VERIFIED = _bool_env("HYPERPAY_WEBHOOK_REQUIRE_VERIFIED", "false")

# Replay protection: opt-in only — staged per provider once ops confirm
# legitimate retry rates. See core.webhook_security.check_replay.
#
# Two-stage rollout:
#   * WEBHOOK_REPLAY_PROTECTION_ENABLED=true → run the body-hash dedup
#     and record audit telemetry, but DO NOT reject duplicates. Operators
#     watch the resulting "replay" counter to confirm legitimate retry
#     rate is below the rejection threshold (typically <1% per merchant).
#   * WEBHOOK_REPLAY_REJECT_ENABLED=true → in addition to logging, actually
#     drop replays with a 200 "ignored" response. Both flags must be true
#     for rejection.
WEBHOOK_REPLAY_PROTECTION_ENABLED = _bool_env("WEBHOOK_REPLAY_PROTECTION_ENABLED", "false")
WEBHOOK_REPLAY_REJECT_ENABLED     = _bool_env("WEBHOOK_REPLAY_REJECT_ENABLED",     "false")

# ── Store Sync ─────────────────────────────────────────────────────────────────
STORE_SYNC_MAX_PRODUCTS  = int(os.environ.get("STORE_SYNC_MAX_PRODUCTS", "500"))
STORE_SYNC_MAX_ORDERS    = int(os.environ.get("STORE_SYNC_MAX_ORDERS", "200"))

# ── AI / Claude ────────────────────────────────────────────────────────────────
# Priority: CLAUDE_API_KEY (used by nahla-bot) → ANTHROPIC_API_KEY
ANTHROPIC_API_KEY = (
    os.environ.get("CLAUDE_API_KEY") or
    os.environ.get("ANTHROPIC_API_KEY", "")
)
CLAUDE_MODEL      = os.environ.get("CLAUDE_MODEL", "claude-haiku-4-5")

# ── AI / OpenAI-compatible helpers ────────────────────────────────────────────
# Used by optional voice transcription and compatible fallback providers.
OPENAI_API_KEY    = os.environ.get("OPENAI_API_KEY", "")
OPENAI_API_BASE   = os.environ.get("OPENAI_API_BASE", "https://api.openai.com/v1")
OPENAI_MODEL      = os.environ.get("OPENAI_MODEL", "gpt-4o-mini")
OPENAI_AUDIO_MODEL = os.environ.get("OPENAI_AUDIO_MODEL", "whisper-1")
# Vision model for describing inbound WhatsApp images. Must be a
# chat-completions endpoint that accepts ``image_url`` parts (default
# ``gpt-4o-mini`` — same family as ``OPENAI_MODEL`` so a single billing
# bucket covers both turns).
OPENAI_VISION_MODEL = os.environ.get("OPENAI_VISION_MODEL", "gpt-4o-mini")
# Default STT language hint we send to Whisper for Saudi-dialect voice
# notes. Whisper accepts ISO-639-1 codes; Arabic = "ar". Operators can
# override per tenant or globally without redeploying the worker.
NAHLA_STT_LANGUAGE = os.environ.get("NAHLA_STT_LANGUAGE", "ar")
# Hard cap on how large an inbound voice note / image we'll download
# from Meta before we bail (defense-in-depth — Meta itself caps at 16MB
# for media, but a misconfigured proxy could still hand us a huge file).
INBOUND_MEDIA_MAX_BYTES = int(os.environ.get("INBOUND_MEDIA_MAX_BYTES", str(20 * 1024 * 1024)))

# ── Cross-Merchant Learning (anonymized signals only) ─────────────────────────
# Salt used by TenantIsolationLayer to derive non-reversible tenant hashes
# before any signal is written to the cross-merchant learning store.  This
# value MUST be stable across deploys for aggregations to remain comparable;
# it MUST also stay private — leaking the salt re-enables tenant correlation.
CROSS_MERCHANT_ANON_SALT = os.environ.get("CROSS_MERCHANT_ANON_SALT", "")
if not CROSS_MERCHANT_ANON_SALT:
    _cfg_logger.warning(
        "CROSS_MERCHANT_ANON_SALT is not set. Tenant hashes for cross-merchant "
        "signals will fall back to a deterministic local salt. Set this in "
        "production to prevent salt-prediction attacks."
    )
    CROSS_MERCHANT_ANON_SALT = "nahla-local-dev-salt-do-not-use-in-prod"

# Master switch — when False, no signal is ever written to the cross-merchant
# learning store, even if the rest of the AI pipeline runs normally.
CROSS_MERCHANT_LEARNING_ENABLED = (
    os.environ.get("CROSS_MERCHANT_LEARNING_ENABLED", "true").lower() == "true"
)

# ── Learned Sales Policies (Phase 1.7 Global / Vertical Learner) ──────────────
# Master switch — when False, the PolicyLearner never runs and the
# PolicyOverrideLayer becomes a no-op (returns the inner decision unchanged).
# Defaults to False so a fresh deploy never auto-influences merchant behavior
# until an operator explicitly enables it.
LEARNED_POLICY_ENABLED = (
    os.environ.get("LEARNED_POLICY_ENABLED", "false").lower() == "true"
)

# Minimum signals required for an aggregate to become a published policy.
# Below this threshold the (intent[, industry]) bucket is skipped — keeping
# noise out of the runtime store and protecting against early-stage bias.
LEARNED_POLICY_MIN_SAMPLE_SIZE = int(
    os.environ.get("LEARNED_POLICY_MIN_SAMPLE_SIZE", "30")
)

# Minimum dominance ratio for the winning action over its bucket.  At 0.6
# the leading action must account for at least 60% of positive outcomes
# before it is published as a recommendation.
LEARNED_POLICY_MIN_CONFIDENCE = float(
    os.environ.get("LEARNED_POLICY_MIN_CONFIDENCE", "0.6")
)

# ── Soft-bias readiness gates (Phase 1.8) ─────────────────────────────────────
# These thresholds gate the future Soft Bias rollout (phase >= 1.9).  They are
# evaluated by ``modules.ai.learning.readiness.ReadinessGate`` against the
# adoption report computed from anonymized signals.
#
# Hard rules:
#   * ``MIN_SAMPLE_SIZE`` is per (intent, industry) bucket — not global.
#   * ``MIN_UPLIFT`` is the conversion delta between aligned and not-aligned
#     turns.  Buckets below the threshold are blocked even if alignment is
#     high — high alignment alone does not prove the hint is *useful*.
#   * Negative uplift on a sensitive intent (checkout / payment / handoff /
#     objection / abandon) blocks readiness immediately, no override path.
LEARNED_POLICY_BIAS_MIN_SAMPLE_SIZE = int(
    os.environ.get("LEARNED_POLICY_BIAS_MIN_SAMPLE_SIZE", "100")
)
LEARNED_POLICY_BIAS_MIN_UPLIFT = float(
    os.environ.get("LEARNED_POLICY_BIAS_MIN_UPLIFT", "0.05")
)
LEARNED_POLICY_BIAS_MIN_ALIGNMENT = float(
    os.environ.get("LEARNED_POLICY_BIAS_MIN_ALIGNMENT", "0.30")
)

# ── Soft Policy Bias rollout (Phase 1.9) ──────────────────────────────────────
# Master switch.  Defaults to ``False`` so a fresh deploy never silently steers
# any merchant; an operator must opt in explicitly.  Even when ``True`` the
# bias layer remains a no-op for any (intent, industry) bucket whose
# readiness verdict is not ready.
LEARNED_POLICY_BIAS_ENABLED = (
    os.environ.get("LEARNED_POLICY_BIAS_ENABLED", "false").lower() == "true"
)

# Per-intent allowlist — only intents in this set may receive a soft bias.
# Sensitive intents (checkout / payment / objection / handoff / abandon /
# complaint) are *additionally* hard-coded as protected inside
# ``modules.ai.learning.bias`` and cannot be enabled here.
LEARNED_POLICY_BIAS_INTENTS = os.environ.get(
    "LEARNED_POLICY_BIAS_INTENTS",
    "ask_product,greeting,faq,browse,product_inquiry,recommendation",
)

# Per-industry rollout filter.  ``"*"`` enables every industry (including the
# global tier).  Comma-separated list otherwise (e.g. "fashion,electronics").
LEARNED_POLICY_BIAS_INDUSTRIES = os.environ.get(
    "LEARNED_POLICY_BIAS_INDUSTRIES",
    "*",
)

# Process-level TTL for the readiness verdict cache used by the bias layer.
# Recomputing the readiness summary requires scanning anonymized signals, so
# the cache must be long enough to amortize that cost across many turns but
# short enough to react to a learner re-run within minutes.
LEARNED_POLICY_BIAS_REGISTRY_TTL_SECONDS = int(
    os.environ.get("LEARNED_POLICY_BIAS_REGISTRY_TTL_SECONDS", "600")
)

# ── Soft-bias rollout (Phase 1.9 → narrow staging trial) ─────────────────────
# Comma-separated list of environments where bias may fire, even when
# ``LEARNED_POLICY_BIAS_ENABLED=true``.  Defaults to "staging" so a misplaced
# enable in production is automatically inert.  ``"*"`` allows every env.
LEARNED_POLICY_BIAS_ENVIRONMENTS = os.environ.get(
    "LEARNED_POLICY_BIAS_ENVIRONMENTS", "staging"
)

# Per-component master switches.  The bias layer applies a component only
# when the corresponding flag is True.  This lets us roll out
# ``preferred_ui_mode`` first, observe production behavior, then enable the
# rest one at a time.
LEARNED_POLICY_BIAS_ALLOW_UI_MODE = (
    os.environ.get("LEARNED_POLICY_BIAS_ALLOW_UI_MODE", "true").lower() == "true"
)
LEARNED_POLICY_BIAS_ALLOW_CHOICE_COUNT = (
    os.environ.get("LEARNED_POLICY_BIAS_ALLOW_CHOICE_COUNT", "true").lower() == "true"
)
# Disabled by default — enable manually after the first metrics window.
LEARNED_POLICY_BIAS_ALLOW_RECOMMENDATION_STYLE = (
    os.environ.get("LEARNED_POLICY_BIAS_ALLOW_RECOMMENDATION_STYLE", "false").lower() == "true"
)

# ── Merchant Brain (Phase 1 Commerce Decision Engine) ──────────────────────────
# Global flag — activates Brain for ALL merchant tenants when true.
#
# Default flipped to "true" because the Brain path is the only one with
# proper intent classification, greeting/identity templates, dedup
# guards, and stage-aware routing. The legacy LLM path has none of
# these protections — leaving it as the default produced regressions:
# bots that re-greeted on every turn, ignored "من أنت" / "السلام
# عليكم", and repeated automation messages verbatim. An operator who
# explicitly wants the legacy fallback can still set
# MERCHANT_BRAIN_ENABLED=false at deploy time.
MERCHANT_BRAIN_ENABLED = os.environ.get("MERCHANT_BRAIN_ENABLED", "true").lower() == "true"

# Per-tenant opt-in — comma-separated tenant IDs (e.g. "1,5,12").
# Allows enabling the Brain for specific stores without a global rollout.
# A tenant listed here uses the Brain even when MERCHANT_BRAIN_ENABLED=false.
MERCHANT_BRAIN_TENANT_IDS: set = {
    int(x.strip())
    for x in os.environ.get("MERCHANT_BRAIN_TENANT_IDS", "").split(",")
    if x.strip().isdigit()
}


# ── Manual marketing campaign — anti-spam frequency cap ────────────────────
#
# Manual campaigns (broadcast / promotion / custom etc.) skip a
# recipient when the same tenant already sent them a marketing campaign
# within the last N days. This protects:
#   * Meta sender reputation (high opt-out / blocked rate kills tier).
#   * Customer experience (no two pushes in the same week).
#   * Merchant credibility (a crashed campaign that restarts won't
#     re-spam the recipients who already received the message).
#
# Default 14 days at first launch — wide enough that no realistic
# weekly cadence trips the guard. Operators can tighten to 7 once we
# have telemetry showing it's safe. Override via env:
#   MARKETING_CAMPAIGN_FREQUENCY_CAP_DAYS=7
#
# Setting to 0 disables the cap entirely (admin-only escape hatch —
# should NEVER be 0 in production).
MARKETING_CAMPAIGN_FREQUENCY_CAP_DAYS = max(
    0,
    int(os.environ.get("MARKETING_CAMPAIGN_FREQUENCY_CAP_DAYS", "14") or "14"),
)

# Batch size for the dispatch loop. Each batch commits its rows then
# pauses briefly so the WhatsApp Cloud API doesn't see a sustained
# burst that would trigger rate-limiting (Meta enforces ~80 msg/sec
# per phone-number for Tier-1 senders; 100 per batch with a small
# inter-batch sleep keeps us comfortably under that ceiling).
MARKETING_CAMPAIGN_BATCH_SIZE = max(
    10,
    int(os.environ.get("MARKETING_CAMPAIGN_BATCH_SIZE", "100") or "100"),
)

# Inter-batch pause in seconds. 1.5s/message inside a batch + this
# pause between batches gives a steady, human-paced send rate.
MARKETING_CAMPAIGN_BATCH_PAUSE_SECONDS = max(
    0.0,
    float(os.environ.get("MARKETING_CAMPAIGN_BATCH_PAUSE_SECONDS", "2.0") or "2.0"),
)

# ── Legacy conversational fallback ─────────────────────────────────────────
# When the Brain pipeline raises (or is disabled), the merchant message
# handler used to fall back to a free-form `generate_ai_reply()` call. The
# legacy path has none of the Brain's intent / handoff / dedup
# protections, so a single Brain hiccup could swap the entire
# conversation into an unprotected LLM and produce repeat handoff
# messages, mis-classified greetings, or runaway token spend.
#
# Default is now FALSE: if Brain fails we send a polite canned reply
# and stop. Operators who explicitly need the legacy fallback while
# diagnosing a Brain regression can flip
# `MERCHANT_BRAIN_ALLOW_LEGACY_FALLBACK=true` for the duration of the
# investigation. New tenants should never see legacy behaviour.
MERCHANT_BRAIN_ALLOW_LEGACY_FALLBACK = (
    os.environ.get("MERCHANT_BRAIN_ALLOW_LEGACY_FALLBACK", "false").lower() == "true"
)

# ── SPL National Address API (Saudi Address Resolution) ───────────────────────
# Used by services/address_resolution.py to resolve national short address codes
# (e.g. RIYD1234) and GPS coordinates into city/district/street/postal_code.
# Get a key from: https://address.gov.sa/en/developer
SPL_NATIONAL_ADDRESS_API_KEY = os.environ.get("SPL_NATIONAL_ADDRESS_API_KEY", "").strip()
SPL_NATIONAL_ADDRESS_BASE_URL = os.environ.get(
    "SPL_NATIONAL_ADDRESS_BASE_URL",
    "https://apina.address.gov.sa/NationalAddress/v3.1",
)
if not SPL_NATIONAL_ADDRESS_API_KEY:
    _cfg_logger.warning(
        "[Address] SPL_NATIONAL_ADDRESS_API_KEY is not set — "
        "national short address codes and GPS coordinates from Google Maps links "
        "will NOT be auto-resolved into city/district/postal fields. "
        "Checkout will still work (the raw code/URL is forwarded in order notes) "
        "but address auto-fill will be disabled. "
        "Get a free key from https://address.gov.sa/en/developer"
    )

# ── CORS ───────────────────────────────────────────────────────────────────────
# IMPORTANT:
#   Never allow an environment override to DROP the canonical Nahla origins.
#   We always merge required origins with any custom env origins.
_required_cors_origins = [
    "http://localhost:3000",
    "http://localhost:5173",
    "https://nahlah.ai",
    "https://www.nahlah.ai",
    "https://app.nahlah.ai",       # dashboard
    "https://api.nahlah.ai",       # backend self-calls / health checks
    "https://store.salla.sa",      # Salla embedded app
    "https://salla.sa",
    "https://s.salla.sa",
    "https://apps.salla.sa",
    "https://zid.sa",
    "https://web.zid.sa",
    "https://partner.zid.sa",
]
_env_cors_origins = [
    o.strip()
    for o in os.environ.get("CORS_ORIGINS", "").split(",")
    if o.strip()
]

CORS_ORIGINS: list[str] = []
_seen_cors: set[str] = set()
for _origin in [*_required_cors_origins, *_env_cors_origins]:
    if _origin not in _seen_cors:
        CORS_ORIGINS.append(_origin)
        _seen_cors.add(_origin)

# Optional regex for additional first-party subdomains / preview hosts.
# Safe with credentials because FastAPI reflects the matched Origin instead of "*".
CORS_ORIGIN_REGEX = os.environ.get(
    "CORS_ORIGIN_REGEX",
    r"https://([a-z0-9-]+\.)?nahlah\.ai",
)

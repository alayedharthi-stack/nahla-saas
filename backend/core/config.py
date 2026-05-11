"""
core/config.py
──────────────
All environment variable reads and module-level constants for the Nahla backend.
Import from here — never call os.environ.get() scattered across route files.
"""
import os
import secrets as _secrets_mod

import logging as _logging
_cfg_logger = _logging.getLogger("nahla-backend")

# ── JWT ────────────────────────────────────────────────────────────────────────
def _safe_token_hex(nbytes: int = 32) -> str:
    token_hex = getattr(_secrets_mod, "token_hex", None)
    if callable(token_hex):
        return token_hex(nbytes)
    return os.urandom(nbytes).hex()


_jwt_secret_env = os.environ.get("JWT_SECRET", "")
if not _jwt_secret_env:
    _cfg_logger.critical(
        "SECURITY: JWT_SECRET is not set in environment. "
        "Generating a random secret — all sessions will be invalidated on restart. "
        "Set JWT_SECRET in Railway environment variables immediately."
    )
JWT_SECRET    = _jwt_secret_env or _safe_token_hex(32)
JWT_ALGORITHM = "HS256"
JWT_EXPIRE_H  = int(os.environ.get("JWT_EXPIRE_HOURS", "168"))  # 7 days

# ── Registration gate ──────────────────────────────────────────────────────────
REQUIRE_INVITE  = os.environ.get("REQUIRE_INVITE", "true").lower() != "false"
INVITE_EXPIRE_H = 168  # 7 days

# ── Admin bootstrap credentials ────────────────────────────────────────────────
ADMIN_EMAIL    = os.environ.get("ADMIN_EMAIL",    "admin@nahlah.ai")
_admin_pass_env = os.environ.get("ADMIN_PASSWORD", "")
if not _admin_pass_env:
    _cfg_logger.critical(
        "SECURITY: ADMIN_PASSWORD is not set in environment. "
        "Set ADMIN_PASSWORD in Railway environment variables immediately."
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
WA_VERIFY_TOKEN    = os.environ.get("WHATSAPP_VERIFY_TOKEN", "nahla2025")
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
ENVIRONMENT      = os.environ.get("ENVIRONMENT", "development")
IS_PRODUCTION    = ENVIRONMENT == "production"

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
# (Optional but recommended — ensures correct permissions/features are requested)
META_WA_CONFIG_ID        = os.environ.get("META_WA_CONFIG_ID", "")

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

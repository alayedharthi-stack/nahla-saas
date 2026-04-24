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
EMAIL_ENABLED  = bool(SMTP_USER and SMTP_PASS)         # auto-disables if unconfigured

if not EMAIL_ENABLED:
    _cfg_logger.warning(
        "[Email] SMTP_USER / SMTP_PASS not set — outbound email is DISABLED. "
        "Set SMTP_HOST, SMTP_PORT, SMTP_USER, SMTP_PASS in environment variables."
    )

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

# ── Salla TEST app (separate credentials — does not affect production app) ──
SALLA_TEST_CLIENT_ID     = os.environ.get("SALLA_TEST_CLIENT_ID", "")
SALLA_TEST_CLIENT_SECRET = os.environ.get("SALLA_TEST_CLIENT_SECRET", "")
SALLA_TEST_REDIRECT_URI  = os.environ.get(
    "SALLA_TEST_REDIRECT_URI",
    "https://api.nahlah.ai/oauth/salla/test/callback",
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
BACKEND_URL = os.environ.get("BACKEND_URL", "https://api.nahlah.ai")
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
MERCHANT_BRAIN_ENABLED = os.environ.get("MERCHANT_BRAIN_ENABLED", "false").lower() == "true"

# Per-tenant opt-in — comma-separated tenant IDs (e.g. "1,5,12").
# Allows enabling the Brain for specific stores without a global rollout.
# A tenant listed here uses the Brain even when MERCHANT_BRAIN_ENABLED=false.
MERCHANT_BRAIN_TENANT_IDS: set = {
    int(x.strip())
    for x in os.environ.get("MERCHANT_BRAIN_TENANT_IDS", "").split(",")
    if x.strip().isdigit()
}

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

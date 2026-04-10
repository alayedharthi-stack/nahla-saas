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
_jwt_secret_env = os.environ.get("JWT_SECRET", "")
if not _jwt_secret_env:
    _cfg_logger.critical(
        "SECURITY: JWT_SECRET is not set in environment. "
        "Generating a random secret — all sessions will be invalidated on restart. "
        "Set JWT_SECRET in Railway environment variables immediately."
    )
JWT_SECRET    = _jwt_secret_env or _secrets_mod.token_hex(32)
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
EMAIL_FROM     = os.environ.get("EMAIL_FROM", "نحلة <noreply@nahlah.ai>")
DASHBOARD_URL  = os.environ.get("DASHBOARD_URL", "https://app.nahlah.ai")

# WhatsApp Business (Meta Cloud API — platform-level, not per-tenant)
WA_TOKEN           = os.environ.get("WHATSAPP_TOKEN", "")
WA_PHONE_ID        = os.environ.get("PHONE_NUMBER_ID", "100253985293107")
WA_VERIFY_TOKEN    = os.environ.get("WHATSAPP_VERIFY_TOKEN", "nahla2025")
WA_BUSINESS_ACCOUNT_ID = os.environ.get("WA_BUSINESS_ACCOUNT_ID", "1650794559682412")

# ── Salla OAuth ────────────────────────────────────────────────────────────────
SALLA_CLIENT_ID      = os.environ.get("SALLA_CLIENT_ID", "")
SALLA_CLIENT_SECRET  = os.environ.get("SALLA_CLIENT_SECRET", "")
SALLA_REDIRECT_URI   = os.environ.get(
    "SALLA_REDIRECT_URI",
    "https://api.nahlah.ai/oauth/salla/callback",
)
SALLA_WEBHOOK_SECRET = os.environ.get("SALLA_WEBHOOK_SECRET", "")

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

# ── CORS ───────────────────────────────────────────────────────────────────────
# Canonical list — no duplicates. Override via CORS_ORIGINS env-var (comma-separated).
_default_origins = ",".join([
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
])
CORS_ORIGINS: list[str] = [
    o.strip()
    for o in os.environ.get("CORS_ORIGINS", _default_origins).split(",")
    if o.strip()
]

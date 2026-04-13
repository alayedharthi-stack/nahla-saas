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
D360_PARTNER_API_KEY = os.environ.get("D360_PARTNER_API_KEY", "")
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

"""
core/config.py
──────────────
All environment variable reads and module-level constants for the Nahla backend.
Import from here — never call os.environ.get() scattered across route files.
"""
import os
import secrets as _secrets_mod

# ── JWT ────────────────────────────────────────────────────────────────────────
JWT_SECRET    = os.environ.get("JWT_SECRET") or _secrets_mod.token_hex(32)
JWT_ALGORITHM = "HS256"
JWT_EXPIRE_H  = int(os.environ.get("JWT_EXPIRE_HOURS", "168"))  # 7 days

# ── Registration gate ──────────────────────────────────────────────────────────
REQUIRE_INVITE  = os.environ.get("REQUIRE_INVITE", "true").lower() != "false"
INVITE_EXPIRE_H = 168  # 7 days

# ── Admin bootstrap credentials ────────────────────────────────────────────────
ADMIN_EMAIL    = os.environ.get("ADMIN_EMAIL",    "admin@nahlah.ai")
ADMIN_PASSWORD = os.environ.get("ADMIN_PASSWORD", "nahla-admin-2026")

# ── Notification services ──────────────────────────────────────────────────────
RESEND_API_KEY = os.environ.get("RESEND_API_KEY", "")
EMAIL_FROM     = os.environ.get("EMAIL_FROM", "نحلة <noreply@nahlah.ai>")
DASHBOARD_URL  = os.environ.get("DASHBOARD_URL", "https://app.nahlah.ai")

# WhatsApp Business (Meta Cloud API — platform-level, not per-tenant)
WA_TOKEN        = os.environ.get("WHATSAPP_TOKEN", "")
WA_PHONE_ID     = os.environ.get("PHONE_NUMBER_ID", "")
WA_VERIFY_TOKEN = os.environ.get("WHATSAPP_VERIFY_TOKEN", "nahla2025")

# ── Salla OAuth ────────────────────────────────────────────────────────────────
SALLA_CLIENT_ID      = os.environ.get("SALLA_CLIENT_ID", "")
SALLA_CLIENT_SECRET  = os.environ.get("SALLA_CLIENT_SECRET", "")
SALLA_REDIRECT_URI   = os.environ.get(
    "SALLA_REDIRECT_URI",
    "https://api.nahlah.ai/oauth/salla/callback",
)
SALLA_WEBHOOK_SECRET = os.environ.get("SALLA_WEBHOOK_SECRET", "")

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

# ── Store Sync ─────────────────────────────────────────────────────────────────
STORE_SYNC_MAX_PRODUCTS  = int(os.environ.get("STORE_SYNC_MAX_PRODUCTS", "500"))
STORE_SYNC_MAX_ORDERS    = int(os.environ.get("STORE_SYNC_MAX_ORDERS", "200"))

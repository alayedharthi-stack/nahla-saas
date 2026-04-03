import os
import hmac
import secrets
from datetime import datetime, timedelta
from typing import Optional, Any, Dict, List, Tuple
from sqlalchemy.orm import Session
from fastapi import Depends, HTTPException, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.security import HTTPBearer, HTTPAuthorizationCredentials
from pydantic import BaseModel
import sys
sys.path.append(os.path.abspath(os.path.join(os.path.dirname(__file__), '../database')))
from session import SessionLocal
from models import (
    Tenant, User, WhatsAppNumber, TenantSettings,
    Campaign, WhatsAppTemplate,
    SmartAutomation, AutomationEvent, PredictiveReorderEstimate,
    Customer, Product, CustomerProfile,
    Order, Coupon, HandoffSession, PaymentSession,
    SystemEvent, ConversationTrace,
    BillingPlan, BillingSubscription, BillingPayment,
    Conversation,
)
import httpx
import time as _time

from fastapi import FastAPI
from fastapi.responses import JSONResponse, RedirectResponse, HTMLResponse
import logging

# ── JWT auth setup ─────────────────────────────────────────────────────────────
try:
    from jose import JWTError, jwt as _jwt
    _JWT_AVAILABLE = True
except ImportError:
    _JWT_AVAILABLE = False

_JWT_SECRET    = os.environ.get("JWT_SECRET") or secrets.token_hex(32)
_JWT_ALGORITHM = "HS256"
_JWT_EXPIRE_H  = int(os.environ.get("JWT_EXPIRE_HOURS", "168"))   # 7 days default

# ── Salla OAuth app credentials ────────────────────────────────────────────────
_SALLA_CLIENT_ID     = os.environ.get("SALLA_CLIENT_ID", "")
_SALLA_CLIENT_SECRET = os.environ.get("SALLA_CLIENT_SECRET", "")
_SALLA_REDIRECT_URI  = os.environ.get(
    "SALLA_REDIRECT_URI",
    "https://api.nahlaai.com/oauth/salla/callback",
)

_ADMIN_EMAIL    = os.environ.get("ADMIN_EMAIL",    "admin@nahlaai.com")
_ADMIN_PASSWORD = os.environ.get("ADMIN_PASSWORD", "nahla-admin-2026")

# ── Notification credentials ────────────────────────────────────────────────────
# Email: Resend API (https://resend.com) — no extra library, uses httpx
_RESEND_API_KEY  = os.environ.get("RESEND_API_KEY", "")
_EMAIL_FROM      = os.environ.get("EMAIL_FROM", "نحلة <noreply@nahlaai.com>")
_DASHBOARD_URL   = os.environ.get("DASHBOARD_URL", "https://api.nahlaai.com")

# WhatsApp: Meta Cloud API (platform account, not tenant store)
_WA_TOKEN        = os.environ.get("WHATSAPP_TOKEN", "")
_WA_PHONE_ID     = os.environ.get("PHONE_NUMBER_ID", "")

# ── Registration gate ───────────────────────────────────────────────────────────
# When REQUIRE_INVITE=true (default in production), /auth/register requires a
# valid invitation token issued by an admin. Set to "false" only for local dev.
_REQUIRE_INVITE = os.environ.get("REQUIRE_INVITE", "true").lower() != "false"
_INVITE_EXPIRE_H = 168  # 7 days

_bearer_scheme = HTTPBearer(auto_error=False)

# ── Audit logger ────────────────────────────────────────────────────────────────
_audit_logger = logging.getLogger("nahla.audit")

def _audit(event: str, **ctx) -> None:
    """Emit a structured audit log line. Always goes to a dedicated logger."""
    parts = " ".join(f"{k}={v}" for k, v in ctx.items())
    _audit_logger.info("AUDIT event=%s %s", event, parts)


def _create_token(email: str, role: str, tenant_id: int) -> str:
    payload = {
        "sub":       email,
        "role":      role,
        "tenant_id": tenant_id,
        "exp":       datetime.utcnow() + timedelta(hours=_JWT_EXPIRE_H),
    }
    return _jwt.encode(payload, _JWT_SECRET, algorithm=_JWT_ALGORITHM)

def _create_invite_token(email: str, tenant_id_hint: Optional[int] = None) -> str:
    """Create a short-lived invitation JWT (type=invite)."""
    payload = {
        "type":            "invite",
        "invited_email":   email,
        "tenant_id_hint":  tenant_id_hint,
        "exp":             datetime.utcnow() + timedelta(hours=_INVITE_EXPIRE_H),
    }
    return _jwt.encode(payload, _JWT_SECRET, algorithm=_JWT_ALGORITHM)

def _hash_password(password: str) -> str:
    """Hash password with bcrypt. Truncates to 72 bytes (bcrypt limit)."""
    import bcrypt as _bcrypt_lib
    hashed = _bcrypt_lib.hashpw(password[:72].encode("utf-8"), _bcrypt_lib.gensalt())
    return hashed.decode("utf-8")

def _create_verify_token(email: str) -> str:
    """24-hour email verification JWT."""
    payload = {
        "type":  "verify_email",
        "sub":   email,
        "exp":   datetime.utcnow() + timedelta(hours=24),
    }
    return _jwt.encode(payload, _JWT_SECRET, algorithm=_JWT_ALGORITHM)

def _create_reset_token(email: str) -> str:
    """1-hour password reset JWT."""
    payload = {
        "type":  "password_reset",
        "sub":   email,
        "exp":   datetime.utcnow() + timedelta(hours=1),
    }
    return _jwt.encode(payload, _JWT_SECRET, algorithm=_JWT_ALGORITHM)

def _decode_token(token: str) -> Dict[str, Any] | None:
    if not _JWT_AVAILABLE:
        return None
    try:
        return _jwt.decode(token, _JWT_SECRET, algorithms=[_JWT_ALGORITHM])
    except JWTError:
        return None

def get_current_user(
    creds: HTTPAuthorizationCredentials = Depends(_bearer_scheme),
) -> Dict[str, Any]:
    """Dependency — raises 401 if token is missing or invalid."""
    if not creds:
        raise HTTPException(status_code=401, detail="Authentication required")
    payload = _decode_token(creds.credentials)
    if not payload:
        raise HTTPException(status_code=401, detail="Invalid or expired token")
    return payload

def require_admin(
    request: Request,
    creds: HTTPAuthorizationCredentials = Depends(_bearer_scheme),
) -> Dict[str, Any]:
    """Dependency — requires a valid JWT with role=admin. Logs every denial."""
    client_ip = request.headers.get("X-Real-IP") or (
        request.client.host if request.client else "unknown"
    )
    user = get_current_user(creds)
    if user.get("role") != "admin":
        _audit(
            "admin_access_denied",
            path=str(request.url.path),
            method=request.method,
            role=user.get("role"),
            sub=user.get("sub"),
            tenant_id=user.get("tenant_id"),
            ip=client_ip,
        )
        raise HTTPException(status_code=403, detail="Admin access required")
    _audit(
        "admin_access_granted",
        path=str(request.url.path),
        method=request.method,
        sub=user.get("sub"),
        ip=client_ip,
    )
    return user

def require_authenticated(request: Request) -> Dict[str, Any]:
    """
    Dependency — returns the JWT payload for the current authenticated request.
    Reads ONLY from request.state.jwt_payload (set by jwt_enforcement_middleware).
    Never falls back to headers — prevents tenant escape via forged X-Tenant-ID.
    """
    payload = getattr(request.state, "jwt_payload", None)
    if not payload:
        raise HTTPException(status_code=401, detail="Authentication required")
    return payload

def get_jwt_tenant_id(request: Request) -> int:
    """
    Strict tenant resolver — reads tenant_id ONLY from the validated JWT.
    Use this for all data endpoints to prevent tenant escape.
    """
    payload = require_authenticated(request)
    tid = payload.get("tenant_id")
    if tid is None:
        raise HTTPException(status_code=401, detail="Token missing tenant_id claim")
    return int(tid)

# Configure logging
logging.basicConfig(level=logging.INFO, format='%(asctime)s %(levelname)s %(message)s')
logger = logging.getLogger("nahla-backend")


# ── Notification helpers ────────────────────────────────────────────────────────

async def _send_email(to: str, subject: str, html: str) -> bool:
    """Send a transactional email via Resend API. Returns True on success."""
    if not _RESEND_API_KEY:
        logger.warning("_send_email: RESEND_API_KEY not set — skipping email to %s", to)
        return False
    try:
        async with httpx.AsyncClient(timeout=15.0) as client:
            resp = await client.post(
                "https://api.resend.com/emails",
                headers={"Authorization": f"Bearer {_RESEND_API_KEY}",
                         "Content-Type": "application/json"},
                json={"from": _EMAIL_FROM, "to": [to],
                      "subject": subject, "html": html},
            )
            if resp.status_code in (200, 201):
                logger.info("Email sent: to=%s subject=%s", to, subject)
                return True
            else:
                logger.error("Email failed: to=%s status=%s body=%s",
                             to, resp.status_code, resp.text[:200])
                return False
    except Exception as exc:
        logger.exception("Email error: to=%s exc=%s", to, exc)
        return False


async def _send_whatsapp(to: str, text: str) -> bool:
    """Send a WhatsApp text message via Meta Cloud API. Returns True on success."""
    if not _WA_TOKEN or not _WA_PHONE_ID:
        logger.warning("_send_whatsapp: WHATSAPP_TOKEN or PHONE_NUMBER_ID not set — skipping")
        return False
    # Normalize number — remove spaces/dashes, ensure no leading zeros
    phone = to.strip().replace(" ", "").replace("-", "").lstrip("0")
    if not phone.startswith("+"):
        phone = "+" + phone
    phone = phone.lstrip("+")  # Meta API expects digits only without +
    try:
        async with httpx.AsyncClient(timeout=15.0) as client:
            resp = await client.post(
                f"https://graph.facebook.com/v19.0/{_WA_PHONE_ID}/messages",
                headers={"Authorization": f"Bearer {_WA_TOKEN}",
                         "Content-Type": "application/json"},
                json={
                    "messaging_product": "whatsapp",
                    "to": phone,
                    "type": "text",
                    "text": {"body": text},
                },
            )
            if resp.status_code == 200:
                logger.info("WhatsApp sent: to=%s", phone)
                return True
            else:
                logger.error("WhatsApp failed: to=%s status=%s body=%s",
                             phone, resp.status_code, resp.text[:200])
                return False
    except Exception as exc:
        logger.exception("WhatsApp error: to=%s exc=%s", phone, exc)
        return False


# ── Email templates ─────────────────────────────────────────────────────────────

def _email_verify(store_name: str, verify_url: str) -> str:
    return f"""
<div dir="rtl" style="font-family:Arial,sans-serif;max-width:520px;margin:0 auto;padding:24px;color:#1e293b">
  <h2 style="color:#f59e0b">🐝 نحلة AI</h2>
  <h3>مرحباً بك في نحلة، {store_name}!</h3>
  <p>أنشأت حسابك بنجاح. أكّد بريدك الإلكتروني للبدء:</p>
  <a href="{verify_url}"
     style="display:inline-block;background:#f59e0b;color:#fff;padding:12px 28px;
            border-radius:8px;text-decoration:none;font-weight:bold;margin:16px 0">
    تأكيد البريد الإلكتروني
  </a>
  <p style="color:#64748b;font-size:13px">الرابط صالح لمدة 24 ساعة.</p>
  <hr style="border:none;border-top:1px solid #e2e8f0;margin:20px 0">
  <p style="color:#94a3b8;font-size:12px">مدعوم بواسطة نحلة AI</p>
</div>"""

def _email_welcome(store_name: str, dashboard_url: str) -> str:
    return f"""
<div dir="rtl" style="font-family:Arial,sans-serif;max-width:520px;margin:0 auto;padding:24px;color:#1e293b">
  <h2 style="color:#f59e0b">🐝 نحلة AI</h2>
  <h3>تم تفعيل حسابك بنجاح 🎉</h3>
  <p>مرحباً بك في <strong>{store_name}</strong>! يمكنك الآن الدخول للوحة التحكم وبدء ربط متجرك.</p>
  <a href="{dashboard_url}"
     style="display:inline-block;background:#f59e0b;color:#fff;padding:12px 28px;
            border-radius:8px;text-decoration:none;font-weight:bold;margin:16px 0">
    الدخول إلى لوحة التحكم
  </a>
  <hr style="border:none;border-top:1px solid #e2e8f0;margin:20px 0">
  <p style="color:#94a3b8;font-size:12px">مدعوم بواسطة نحلة AI</p>
</div>"""

def _email_reset(reset_url: str) -> str:
    return f"""
<div dir="rtl" style="font-family:Arial,sans-serif;max-width:520px;margin:0 auto;padding:24px;color:#1e293b">
  <h2 style="color:#f59e0b">🐝 نحلة AI</h2>
  <h3>إعادة تعيين كلمة المرور</h3>
  <p>استلمنا طلباً لإعادة تعيين كلمة مرور حسابك. انقر على الزر أدناه:</p>
  <a href="{reset_url}"
     style="display:inline-block;background:#ef4444;color:#fff;padding:12px 28px;
            border-radius:8px;text-decoration:none;font-weight:bold;margin:16px 0">
    إعادة تعيين كلمة المرور
  </a>
  <p style="color:#64748b;font-size:13px">الرابط صالح لمدة ساعة واحدة فقط.</p>
  <p style="color:#64748b;font-size:13px">إذا لم تطلب هذا، تجاهل الرسالة.</p>
  <hr style="border:none;border-top:1px solid #e2e8f0;margin:20px 0">
  <p style="color:#94a3b8;font-size:12px">مدعوم بواسطة نحلة AI</p>
</div>"""

def _email_subscription(store_name: str, plan_name: str, ends_at: str) -> str:
    return f"""
<div dir="rtl" style="font-family:Arial,sans-serif;max-width:520px;margin:0 auto;padding:24px;color:#1e293b">
  <h2 style="color:#f59e0b">🐝 نحلة AI</h2>
  <h3>تم تفعيل اشتراكك ✅</h3>
  <p>مرحباً <strong>{store_name}</strong>،</p>
  <p>تم تفعيل خطة <strong>{plan_name}</strong> بنجاح.</p>
  <p style="color:#64748b">ينتهي الاشتراك في: <strong>{ends_at}</strong></p>
  <hr style="border:none;border-top:1px solid #e2e8f0;margin:20px 0">
  <p style="color:#94a3b8;font-size:12px">مدعوم بواسطة نحلة AI</p>
</div>"""


app = FastAPI(title="Nahla SaaS Backend", description="Multi-tenant SaaS API server.")

# CORS – allow dashboard dev server
app.add_middleware(
    CORSMiddleware,
    allow_origins=[
        "http://localhost:3000",
        "http://127.0.0.1:3000",
        "https://app.nahlaai.com",
        "https://api.nahlaai.com",
        "https://creative-intuition-production-c193.up.railway.app",
    ],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# Multi-tenant middleware
@app.middleware("http")
async def multi_tenant_middleware(request: Request, call_next):
    # In production, replace this with JWT-based tenant resolution.
    # The X-Tenant-ID header is used for dev/demo only.
    # Any request without this header is treated as tenant 1 (demo store).
    raw = request.headers.get("X-Tenant-ID", "1")
    try:
        tenant_id = str(int(raw))   # reject non-integer values
    except (ValueError, TypeError):
        tenant_id = "1"
    request.state.tenant_id = tenant_id
    response = await call_next(request)
    return response

# ── API key protection ──────────────────────────────────────────────────────────
# Set API_SECRET_KEY env var to enable. Requests must include X-Nahla-Key header.
# Exempt: /health, /webhook/* (WhatsApp webhooks from Meta).
_API_SECRET_KEY = os.environ.get("API_SECRET_KEY", "")

@app.middleware("http")
async def api_key_middleware(request: Request, call_next):
    if _API_SECRET_KEY:
        path = request.url.path
        if not (path.startswith("/health") or path.startswith("/webhook") or path.startswith("/auth")):
            provided = request.headers.get("X-Nahla-Key", "")
            if provided != _API_SECRET_KEY:
                return JSONResponse(status_code=401, content={"detail": "Unauthorized"})
    return await call_next(request)

# ── Global rate limiting ────────────────────────────────────────────────────────
# 300 requests per minute per IP. Exempt: /health.
import sys as _sys
_sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), 'observability')))
from rate_limiter import check_rate_limit as _check_rate_limit

@app.middleware("http")
async def global_rate_limit_middleware(request: Request, call_next):
    if not (request.url.path.startswith("/health") or request.url.path.startswith("/auth")):
        client_ip = request.headers.get("X-Real-IP") or (request.client.host if request.client else "unknown")
        if not _check_rate_limit(f"global:{client_ip}", max_count=300, window_seconds=60):
            return JSONResponse(status_code=429, content={"detail": "Too many requests"})
    return await call_next(request)

# ── HTTP request logging ────────────────────────────────────────────────────────
@app.middleware("http")
async def request_logging_middleware(request: Request, call_next):
    start = _time.monotonic()
    response = await call_next(request)
    duration_ms = round((_time.monotonic() - start) * 1000)
    tenant_id = getattr(request.state, "tenant_id", "-")
    client_ip = request.headers.get("X-Real-IP") or (request.client.host if request.client else "unknown")
    logger.info(
        "%s %s %d %dms tenant=%s ip=%s",
        request.method, request.url.path, response.status_code, duration_ms, tenant_id, client_ip,
    )
    return response

# ── JWT enforcement middleware ──────────────────────────────────────────────────
# Requires a valid JWT for all routes except public ones.
# When valid: attaches payload to request.state.jwt_payload and
#             overrides tenant_id from the token (prevents header spoofing).
_JWT_PUBLIC_PREFIXES = ("/health", "/webhook", "/auth", "/oauth")

@app.middleware("http")
async def jwt_enforcement_middleware(request: Request, call_next):
    path = request.url.path

    # CORS preflight — browser sends OPTIONS without Authorization; let it through
    if request.method == "OPTIONS":
        return await call_next(request)

    # Always allow public endpoints without a token
    if any(path.startswith(p) for p in _JWT_PUBLIC_PREFIXES):
        return await call_next(request)

    # If jose isn't installed yet (e.g. first deploy), fail open with a warning
    if not _JWT_AVAILABLE:
        logger.warning("JWT enforcement skipped — python-jose not installed")
        return await call_next(request)

    auth_header = request.headers.get("Authorization", "")
    if not auth_header.startswith("Bearer "):
        return JSONResponse(
            status_code=401,
            content={"detail": "Authentication required", "code": "missing_token"},
        )

    payload = _decode_token(auth_header[7:])
    if not payload:
        return JSONResponse(
            status_code=401,
            content={"detail": "Token expired or invalid", "code": "invalid_token"},
        )

    # Attach to request state so downstream can read role/tenant without re-decoding
    request.state.jwt_payload = payload
    # Override tenant_id from JWT — the token is the authoritative source,
    # not the X-Tenant-ID header (which could be spoofed by a caller).
    request.state.tenant_id = str(payload.get("tenant_id", 1))
    return await call_next(request)

# Dependency for DB session
def get_db():
    db = SessionLocal()
    try:
        yield db
    except Exception:
        db.rollback()
        raise
    finally:
        db.close()

def _resolve_tenant_id(request: Request) -> int:
    """
    Resolve tenant_id for the current request.
    Priority: JWT payload (authoritative) > X-Tenant-ID header (dev fallback) > 1.
    """
    jwt_payload = getattr(request.state, "jwt_payload", None)
    if jwt_payload:
        return int(jwt_payload.get("tenant_id", 1))
    try:
        return int(request.state.tenant_id)
    except (ValueError, AttributeError):
        return 1

def _get_or_create_tenant(db: Session, tenant_id: int) -> Tenant:
    """Get existing tenant or create a default one for dev."""
    tenant = db.query(Tenant).filter(Tenant.id == tenant_id).first()
    if not tenant:
        tenant = Tenant(
            id=tenant_id,
            name=f"متجر رقم {tenant_id}",
            domain=f"store-{tenant_id}.nahla.sa",
            is_active=True,
            created_at=datetime.utcnow(),
        )
        db.add(tenant)
        db.flush()
    return tenant

def _get_or_create_settings(db: Session, tenant_id: int) -> TenantSettings:
    """Get existing TenantSettings or create with defaults."""
    _get_or_create_tenant(db, tenant_id)
    settings = db.query(TenantSettings).filter(TenantSettings.tenant_id == tenant_id).first()
    if not settings:
        settings = TenantSettings(
            tenant_id=tenant_id,
            show_nahla_branding=True,
            branding_text="🐝 Powered by Nahla",
            created_at=datetime.utcnow(),
            updated_at=datetime.utcnow(),
        )
        db.add(settings)
        db.flush()
    return settings

# ── Default values ─────────────────────────────────────────────────────────────

DEFAULT_WHATSAPP: Dict[str, Any] = {
    "business_display_name": "",
    "phone_number": "",
    "phone_number_id": "",
    "access_token": "",
    "verify_token": "",
    "webhook_url": "https://app.nahla.ai/webhook/whatsapp",
    "store_button_label": "زيارة المتجر",
    "store_button_url": "",
    "owner_contact_label": "تواصل مع المالك",
    "owner_whatsapp_number": "",
    "auto_reply_enabled": True,
    "transfer_to_owner_enabled": True,
    # AI Template Generation mode:
    #   draft_approval — save as DRAFT, merchant reviews before submitting to Meta
    #   auto_submit    — generate and submit to Meta immediately, notify merchant
    "template_submission_mode": "draft_approval",
}

DEFAULT_AI: Dict[str, Any] = {
    "assistant_name": "نحلة",
    "assistant_role": "مساعدة ذكية لخدمة عملاء المتجر",
    "reply_tone": "friendly",
    "reply_length": "medium",
    "default_language": "arabic",
    "owner_instructions": "",
    "coupon_rules": "",
    "escalation_rules": "",
    "allowed_discount_levels": "10",
    "recommendations_enabled": True,
}

DEFAULT_STORE: Dict[str, Any] = {
    "store_name": "",
    "store_logo_url": "",
    "store_url": "",
    "platform_type": "salla",
    "salla_client_id": "",
    "salla_client_secret": "",
    "salla_access_token": "",
    "zid_client_id": "",
    "zid_client_secret": "",
    "shopify_shop_domain": "",
    "shopify_access_token": "",
    "shipping_provider": "",
    "google_maps_location": "",
    "instagram_url": "",
    "twitter_url": "",
    "snapchat_url": "",
    "tiktok_url": "",
}

DEFAULT_NOTIFICATIONS: Dict[str, Any] = {
    "whatsapp_alerts": True,
    "email_alerts": True,
    "system_alerts": True,
    "failed_webhook_alerts": True,
    "low_balance_alerts": True,
}

def _merge_defaults(stored: Optional[Dict], defaults: Dict) -> Dict:
    """Merge stored values over defaults so new keys always have a value."""
    result = dict(defaults)
    if stored:
        result.update(stored)
    return result


# ── Billing plan definitions ────────────────────────────────────────────────────

INTEGRATION_FEE_SAR = 59          # Fee charged via Salla/Zid marketplace listing
LAUNCH_PROMO_MONTHS = 2           # First N months at launch price
LAUNCH_PROMO_UNTIL  = datetime(2026, 6, 30, 23, 59, 59)  # Promo window end date
FREE_TRIAL_DAYS     = 14          # Free trial period for new tenants

BILLING_PLANS_SEED: List[Dict[str, Any]] = [
    {
        "slug": "starter",
        "name": "Starter",
        "name_ar": "المبتدئ",
        "description": "للمتاجر الصغيرة التي تبدأ رحلة الأتمتة",
        "price_sar": 899,
        "launch_price_sar": 449,
        "billing_cycle": "monthly",
        "features": [
            "ردود ذكاء اصطناعي تلقائية",
            "حتى 1,000 محادثة/شهر",
            "3 أتمتات فعّالة",
            "حملتان/شهر",
            "تحليلات أساسية",
        ],
        "limits": {
            "conversations_per_month": 1000,
            "automations": 3,
            "campaigns_per_month": 2,
        },
    },
    {
        "slug": "growth",
        "name": "Growth",
        "name_ar": "النمو",
        "description": "للمتاجر المتنامية التي تريد تحقيق أقصى مبيعات",
        "price_sar": 1699,
        "launch_price_sar": 849,
        "billing_cycle": "monthly",
        "features": [
            "ردود ذكاء اصطناعي تلقائية",
            "حتى 5,000 محادثة/شهر",
            "أتمتات غير محدودة",
            "10 حملات/شهر",
            "تحليلات متقدمة",
            "أولوية الدعم",
        ],
        "limits": {
            "conversations_per_month": 5000,
            "automations": -1,
            "campaigns_per_month": 10,
        },
    },
    {
        "slug": "scale",
        "name": "Scale",
        "name_ar": "التوسع",
        "description": "للمتاجر الكبيرة والعلامات التجارية المتسارعة",
        "price_sar": 2999,
        "launch_price_sar": 1499,
        "billing_cycle": "monthly",
        "features": [
            "ردود ذكاء اصطناعي تلقائية",
            "محادثات غير محدودة",
            "أتمتات غير محدودة",
            "حملات غير محدودة",
            "تحليلات متقدمة + تقارير مخصصة",
            "دعم مخصص 24/7",
            "وصول API كامل",
        ],
        "limits": {
            "conversations_per_month": -1,
            "automations": -1,
            "campaigns_per_month": -1,
        },
    },
]


def _ensure_billing_plans(db: Session) -> None:
    """Seed the system billing plans on first use."""
    for seed in BILLING_PLANS_SEED:
        if not db.query(BillingPlan).filter(BillingPlan.slug == seed["slug"]).first():
            plan = BillingPlan(
                tenant_id=None,
                slug=seed["slug"],
                name=seed["name"],
                description=seed["description"],
                currency="SAR",
                price_sar=seed["price_sar"],
                billing_cycle=seed["billing_cycle"],
                features=seed["features"],
                limits=seed["limits"],
                extra_metadata={
                    "name_ar": seed["name_ar"],
                    "launch_price_sar": seed["launch_price_sar"],
                },
            )
            db.add(plan)
    db.commit()


def _get_tenant_subscription(db: Session, tenant_id: int) -> Optional[BillingSubscription]:
    """Return the active subscription for a tenant."""
    return (
        db.query(BillingSubscription)
        .filter(
            BillingSubscription.tenant_id == tenant_id,
            BillingSubscription.status == "active",
        )
        .order_by(BillingSubscription.started_at.desc())
        .first()
    )


def _is_launch_discount_active(sub: BillingSubscription) -> bool:
    """True if the subscription is still within the launch promo window."""
    if not sub.started_at:
        return False
    now = datetime.utcnow()
    months_active = (now.year - sub.started_at.year) * 12 + (now.month - sub.started_at.month)
    return months_active < LAUNCH_PROMO_MONTHS and sub.started_at <= LAUNCH_PROMO_UNTIL


def _require_subscription(db: Session, tenant_id: int) -> None:
    """Raise HTTP 402 if the tenant has no active Nahla subscription."""
    if not _get_tenant_subscription(db, tenant_id):
        raise HTTPException(
            status_code=402,
            detail="الرجاء اختيار خطة نحلة لتفعيل الطيار الآلي للمبيعات.",
        )

# ── Routers ────────────────────────────────────────────────────────────────────
# Each router is extracted from this file incrementally.
# Only move code here — never rewrite logic during extraction.

from routers.health    import router as _health_router
from routers.admin     import router as _admin_router
from routers.auth      import router as _auth_router
from routers.settings  import router as _settings_router

app.include_router(_health_router)
app.include_router(_admin_router)
app.include_router(_auth_router)
app.include_router(_settings_router)
from routers.templates    import router as _templates_router
from routers.campaigns    import router as _campaigns_router
from routers.automations  import router as _automations_router
from routers.automations  import (
    _get_autopilot_settings as _get_autopilot_settings,
    _log_autopilot_event    as _log_autopilot_event,
)
from routers.intelligence import router as _intelligence_router

app.include_router(_templates_router)
app.include_router(_campaigns_router)
app.include_router(_automations_router)
app.include_router(_intelligence_router)


# ── AI Sales Agent ─────────────────────────────────────────────────────────────

DEFAULT_AI_SALES_AGENT: Dict[str, Any] = {
    "enable_ai_sales_agent": False,
    "allow_product_recommendations": True,
    "allow_order_creation": True,
    "allow_address_collection": True,
    "allow_payment_link_sending": True,
    "allow_cod_confirmation_flow": True,
    "allow_human_handoff": True,
    "confidence_threshold": 0.55,
    "handoff_phrases": ["موظف", "بشري", "انسان", "تكلم مع شخص", "تواصل مع الدعم", "شخص حقيقي"],
}

DEFAULT_MOYASAR: Dict[str, Any] = {
    "enabled": False,
    "secret_key": "",
    "publishable_key": "",
    "webhook_secret": "",
    "callback_url": "",
    "success_url": "",
    "error_url": "",
}

DEFAULT_HANDOFF: Dict[str, Any] = {
    "notification_method": "webhook",   # "webhook" | "whatsapp" | "both" | "none"
    "webhook_url": "",
    "staff_whatsapp": "",
    "auto_pause_ai": True,
}

ORCHESTRATOR_URL = os.getenv("ORCHESTRATOR_URL", "http://localhost:8016")

ENVIRONMENT   = os.getenv("ENVIRONMENT", "development")
IS_PRODUCTION = ENVIRONMENT == "production"

if IS_PRODUCTION:
    logger.info("Running in PRODUCTION mode — demo fallbacks disabled, strict logging active")
else:
    logger.info(f"Running in {ENVIRONMENT} mode")

# ── Production startup guard ────────────────────────────────────────────────────
# Fail fast and loudly if critical secrets are missing in production.
# Set these in Railway → Variables before deploying.
_REQUIRED_PROD_VARS = ("JWT_SECRET", "ADMIN_EMAIL", "ADMIN_PASSWORD")
if IS_PRODUCTION:
    _missing = [v for v in _REQUIRED_PROD_VARS if not os.environ.get(v)]
    if _missing:
        logger.critical(
            "STARTUP ABORTED — required env vars not configured: %s\n"
            "Set them in Railway → Variables and redeploy.",
            ", ".join(_missing),
        )
        sys.exit(1)
    if os.environ.get("ADMIN_PASSWORD") == "nahla-admin-2026":
        logger.critical(
            "STARTUP ABORTED — default ADMIN_PASSWORD 'nahla-admin-2026' must not be used in production.\n"
            "Set a strong unique password in Railway → Variables."
        )
        sys.exit(1)
    if os.environ.get("JWT_SECRET", "").startswith("dev-"):
        logger.critical(
            "STARTUP ABORTED — JWT_SECRET looks like a dev placeholder. Set a random 64-char secret."
        )
        sys.exit(1)
    logger.info("Production secrets validated — JWT_SECRET, ADMIN_EMAIL, ADMIN_PASSWORD are set.")


def _rate_limit(key: str, max_count: int, window_seconds: int) -> None:
    """Raise HTTP 429 if the rate limit is exceeded."""
    sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), ".")))
    from observability.rate_limiter import check_rate_limit
    if not check_rate_limit(key, max_count, window_seconds):
        raise HTTPException(
            status_code=429,
            detail=f"Rate limit exceeded. Max {max_count} requests per {window_seconds}s.",
        )


# Intent definitions – each has detection keywords + the response strategy it maps to
AI_SALES_INTENTS: Dict[str, Dict[str, Any]] = {
    "ask_product": {
        "label":         "استفسار عن منتج",
        "keywords":      ["منتج", "عندكم", "يوجد", "متوفر", "ما هو", "ما هي", "اريد اعرف", "أريد أعرف", "show me", "do you have"],
        "response_type": "product_info",
        "emoji":         "📦",
    },
    "ask_price": {
        "label":         "استفسار عن السعر",
        "keywords":      ["سعر", "بكم", "كم", "تكلف", "يكلف", "ثمن", "سعره", "سعرها", "price", "cost", "how much"],
        "response_type": "price_info",
        "emoji":         "💰",
    },
    "ask_recommendation": {
        "label":         "طلب توصية",
        "keywords":      ["وصّيني", "اوصيني", "انصحني", "انصحيني", "ايش تنصح", "ماذا تنصح", "ايش تقترح", "اقترح", "recommend", "suggest"],
        "response_type": "recommendation",
        "emoji":         "⭐",
    },
    "ask_shipping": {
        "label":         "استفسار عن الشحن",
        "keywords":      ["شحن", "توصيل", "كم يوم", "متى يوصل", "رسوم", "مجاني", "سريع", "delivery", "shipping"],
        "response_type": "shipping_info",
        "emoji":         "🚚",
    },
    "ask_offer": {
        "label":         "استفسار عن العروض",
        "keywords":      ["عرض", "خصم", "تخفيض", "عروض", "تنزيل", "كوبون", "كود", "offer", "discount", "coupon"],
        "response_type": "offer_info",
        "emoji":         "🏷️",
    },
    "order_product": {
        "label":         "طلب شراء منتج",
        "keywords":      ["ابي", "أبي", "اطلب", "أطلب", "اشتري", "أشتري", "ابغى", "أبغى", "اريد اطلب", "بدي", "عايز", "order", "buy", "purchase"],
        "response_type": "start_order_flow",
        "emoji":         "🛍️",
    },
    "pay_now": {
        "label":         "الدفع الإلكتروني",
        "keywords":      ["ادفع", "دفع", "فيزا", "بطاقة", "اون لاين", "اونلاين", "الكتروني", "مدى", "pay", "visa", "card", "online"],
        "response_type": "payment_link",
        "emoji":         "💳",
    },
    "cash_on_delivery": {
        "label":         "الدفع عند الاستلام",
        "keywords":      ["كاش", "نقد", "عند الاستلام", "cod", "دفع عند", "عند الوصول", "نقدي"],
        "response_type": "cod_flow",
        "emoji":         "💵",
    },
    "track_order": {
        "label":         "تتبع الطلب",
        "keywords":      ["تتبع", "وين طلبي", "طلبي", "وصل", "موعد استلام", "رقم طلب", "track", "order status", "where is"],
        "response_type": "order_tracking",
        "emoji":         "📍",
    },
    "talk_to_human": {
        "label":         "التحدث مع موظف",
        "keywords":      ["موظف", "بشري", "انسان", "تكلم مع", "تواصل مع", "شخص حقيقي", "support", "دعم", "مساعدة بشرية"],
        "response_type": "human_handoff",
        "emoji":         "👤",
    },
}


def _get_ai_sales_settings(db: Session, tenant_id: int) -> Dict[str, Any]:
    """Read AI Sales Agent config from TenantSettings.extra_metadata['ai_sales_agent']."""
    s = _get_or_create_settings(db, tenant_id)
    meta = s.extra_metadata or {}
    stored = meta.get("ai_sales_agent", {})
    return _merge_defaults(stored, DEFAULT_AI_SALES_AGENT)


def _save_ai_sales_settings(db: Session, tenant_id: int, patch: Dict[str, Any]) -> Dict[str, Any]:
    """Deep-merge patch into existing config and persist."""
    from sqlalchemy.orm.attributes import flag_modified
    s = _get_or_create_settings(db, tenant_id)
    meta = dict(s.extra_metadata or {})
    current = _merge_defaults(meta.get("ai_sales_agent", {}), DEFAULT_AI_SALES_AGENT)
    current.update(patch)
    meta["ai_sales_agent"] = current
    s.extra_metadata = meta
    s.updated_at = datetime.utcnow()
    flag_modified(s, "extra_metadata")   # guarantee SQLAlchemy 2.0 tracks the JSONB change
    return current


def _detect_intent(message: str, settings: Dict[str, Any]) -> Tuple[str, float, str]:
    """
    Keyword-based intent detection.
    Returns (intent_key, confidence_score, response_type).
    In production this is replaced by an LLM classification call.
    """
    msg = message.lower()
    best_intent = "general"
    best_score = 0.0
    best_response_type = "general"

    # Check handoff phrases first (highest priority)
    for phrase in settings.get("handoff_phrases", DEFAULT_AI_SALES_AGENT["handoff_phrases"]):
        if phrase.lower() in msg:
            return "talk_to_human", 0.95, "human_handoff"

    for intent_key, meta in AI_SALES_INTENTS.items():
        hits = sum(1 for kw in meta["keywords"] if kw.lower() in msg)
        if hits > 0:
            # Score = fraction of keywords matched, capped at 1.0
            score = min(hits / max(len(meta["keywords"]) * 0.25, 1.0), 1.0)
            if score > best_score:
                best_score = score
                best_intent = intent_key
                best_response_type = meta["response_type"]

    if best_intent == "general":
        best_score = 0.15  # low-confidence fallback

    return best_intent, round(best_score, 2), best_response_type


async def _call_orchestrator(
    tenant_id: int,
    customer_phone: str,
    message: str,
) -> Optional[Dict[str, Any]]:
    """
    Route the message through the AI Orchestrator pipeline.
    Returns the orchestrator response dict, or None if unavailable.
    The orchestrator applies: FactGuard, PolicyGuard, Claude reasoning.
    """
    try:
        async with httpx.AsyncClient(timeout=30.0) as client:
            resp = await client.post(
                f"{ORCHESTRATOR_URL}/orchestrate",
                json={
                    "tenant_id": tenant_id,
                    "customer_phone": customer_phone,
                    "message": message,
                },
            )
            resp.raise_for_status()
            return resp.json()
    except Exception as exc:
        logger.warning(
            f"[Orchestrator] Call failed for tenant={tenant_id}: {exc} "
            f"— falling back to keyword engine"
        )
        return None


def _get_handoff_settings(db: Session, tenant_id: int) -> Dict[str, Any]:
    s = _get_or_create_settings(db, tenant_id)
    meta = s.extra_metadata or {}
    return _merge_defaults(meta.get("handoff_settings", {}), DEFAULT_HANDOFF)


def _get_moyasar_settings(db: Session, tenant_id: int) -> Dict[str, Any]:
    s = _get_or_create_settings(db, tenant_id)
    meta = s.extra_metadata or {}
    return _merge_defaults(meta.get("moyasar", {}), DEFAULT_MOYASAR)


async def _get_product_catalog(db: Session, tenant_id: int) -> List[Dict[str, Any]]:
    """
    Fetch product catalog. Priority:
    1. Real store adapter (Salla, etc.) if configured
    2. Nahla DB products (synced or manually added)
    Returns [] — never invents products.
    """
    import sys as _sys
    _sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), ".")))
    from store_integration.product_service import fetch_products, normalize_db_product
    from store_integration.registry import get_adapter

    # Try real store first
    adapter = get_adapter(tenant_id)
    if adapter:
        try:
            live_products = await fetch_products(tenant_id)
            if live_products:
                raw_live = [p.dict() for p in live_products]
                from ranking.product_ranker import rank_products
                return rank_products(raw_live, db, tenant_id)
        except Exception:
            pass  # fall through to DB

    # DB fallback
    products = db.query(Product).filter(Product.tenant_id == tenant_id).all()
    raw = [normalize_db_product(p) for p in products]
    from ranking.product_ranker import rank_products
    return rank_products(raw, db, tenant_id)


def _build_response_context(db: Session, tenant_id: int) -> Dict[str, Any]:
    """
    Collect all store-specific data needed to build dynamic AI Sales responses.
    All values come from the DB — no hardcoded store assumptions.

    Returns a context dict with:
      store_name        — from store_settings (empty string if unset)
      store_url         — from store_settings
      assistant_name    — from ai_settings
      coupon_rules      — free-text field from ai_settings, filled by store owner
      escalation_rules  — free-text field from ai_settings
      shipping_lines    — list[str] built from ShippingFee table rows
      payment_methods   — ["cod", "online"] based on enabled permissions
    """
    from models import ShippingFee
    s = _get_or_create_settings(db, tenant_id)
    store = _merge_defaults(s.store_settings, DEFAULT_STORE)
    ai    = _merge_defaults(s.ai_settings,    DEFAULT_AI)

    # Build shipping info lines from the ShippingFee table
    fees = db.query(ShippingFee).filter(ShippingFee.tenant_id == tenant_id).all()
    shipping_lines: List[str] = []
    for f in fees[:8]:          # cap at 8 rows to keep responses concise
        label = f.city or f.zone_name or "—"
        amount = f.fee_amount or "—"
        shipping_lines.append(f"• {label}: {amount}")

    return {
        "store_name":       store.get("store_name") or "",
        "store_url":        store.get("store_url") or "",
        "assistant_name":   ai.get("assistant_name") or "نحلة",
        "coupon_rules":     ai.get("coupon_rules") or "",
        "escalation_rules": ai.get("escalation_rules") or "",
        "shipping_lines":   shipping_lines,
    }


def _format_product_list(products: List[Dict[str, Any]], max_items: int = 5) -> str:
    """Format products into a WhatsApp-friendly bullet list using only real catalog data."""
    lines = []
    for p in products[:max_items]:
        price_part = f" — {p['price']}" if p.get("price") else ""
        lines.append(f"• *{p['title']}*{price_part}")
        if p.get("description"):
            lines.append(f"  _{p['description'][:100]}_")
    return "\n".join(lines)


def _build_ai_sales_response(
    intent: str,
    response_type: str,
    products: List[Dict[str, Any]],
    permissions: Dict[str, Any],
    ctx: Dict[str, Any],
    customer_name: str,
    payment_link_url: Optional[str],
) -> Tuple[str, bool, bool]:
    """
    Build a dynamic Arabic sales response driven entirely by store data.
    No content is hardcoded — all values come from ctx (store settings + DB).

    Parameters:
      products         — real catalog rows from _get_product_catalog()
      permissions      — AI Sales Agent settings (allow_* flags)
      ctx              — store context from _build_response_context()
      customer_name    — resolved customer display name
      payment_link_url — generated payment URL (None if not applicable)

    Returns (response_text, order_started, handoff_triggered).
    """
    store_ref = f" في {ctx['store_name']}" if ctx.get("store_name") else ""

    # ── Human handoff ──────────────────────────────────────────────────────────
    if response_type == "human_handoff":
        if not permissions.get("allow_human_handoff", True):
            return (
                f"مرحباً {customer_name}! خدمة التحويل لموظف غير متاحة حالياً."
                " يسعدنا مساعدتك مباشرة! 😊",
                False, False,
            )
        return (
            f"مرحباً {customer_name} 👋\n"
            f"سأحوّلك الآن إلى أحد الموظفين{store_ref}. انتظر لحظة من فضلك. 🙏",
            False, True,
        )

    # ── Product info ───────────────────────────────────────────────────────────
    if response_type == "product_info":
        if not products:
            return (
                f"يسعدني مساعدتك {customer_name} 😊\n"
                "يبدو أنني لا أستطيع الوصول إلى قائمة المنتجات حالياً.\n\n"
                "هل يمكنك إخباري أكثر عن المنتج الذي تبحث عنه؟\n"
                "مثل النوع أو المواصفات أو الفئة التي تهمك، وسأحاول مساعدتك بأفضل شكل 🙏",
                False, False,
            )
        header = f"مرحباً {customer_name}! 😊 إليك المنتجات المتاحة{store_ref}:\n"
        return (
            header + _format_product_list(products) +
            "\n\nهل تودّ الاستفسار عن أحد هذه المنتجات أو طلبه؟ 🛍️",
            False, False,
        )

    # ── Price info ─────────────────────────────────────────────────────────────
    if response_type == "price_info":
        priced = [p for p in products if p.get("price")]
        if not priced:
            return (
                f"مرحباً {customer_name}! تواصل معنا{store_ref} للاطلاع على الأسعار الحالية.",
                False, False,
            )
        lines = [f"*الأسعار المتاحة{store_ref}* 💰\n"]
        for p in priced[:5]:
            lines.append(f"• {p['title']}: *{p['price']}*")
        lines.append("\nهل تريد إتمام طلب؟")
        return "\n".join(lines), False, False

    # ── Recommendation ─────────────────────────────────────────────────────────
    if response_type == "recommendation":
        if not permissions.get("allow_product_recommendations", True):
            return (
                f"مرحباً {customer_name}! خاصية التوصيات غير مفعّلة حالياً.",
                False, False,
            )
        if not products:
            return (
                f"يسعدني مساعدتك في إيجاد المناسب لك {customer_name} 😊\n"
                "المنتجات غير متاحة للعرض الآن، لكن يمكنني مساعدتك بشكل أفضل إذا أخبرتني:\n\n"
                "• ما الفئة أو النوع الذي تبحث عنه؟\n"
                "• ما المواصفات أو الاستخدام الذي تحتاجه؟\n"
                "• هل هناك ميزانية معينة تفكر فيها؟\n\n"
                "سأبذل قصارى جهدي لتوجيهك نحو الخيار الأمثل 🛍️",
                False, False,
            )
        # Pick highest-priority product (first in catalog; in production: sort by stats_converted)
        top = products[0]
        lines = [f"بناءً على منتجاتنا المتاحة{store_ref} ⭐\n", f"*{top['title']}*"]
        if top.get("price"):
            lines.append(f"السعر: {top['price']}")
        if top.get("description"):
            lines.append(top["description"][:150])
        lines.append("\nهل تريد طلبه الآن؟ 😊")
        return "\n".join(lines), False, False

    # ── Shipping info — sourced from ShippingFee table ─────────────────────────
    if response_type == "shipping_info":
        lines = [f"مرحباً {customer_name}! 🚚 *معلومات التوصيل{store_ref}:*\n"]
        if ctx.get("shipping_lines"):
            lines.extend(ctx["shipping_lines"])
        else:
            lines.append("• تواصل معنا لمعرفة رسوم التوصيل لمنطقتك")
        lines.append("\nهل تريد معرفة رسوم التوصيل لمنطقة محددة؟")
        return "\n".join(lines), False, False

    # ── Offer info — sourced from ai_settings.coupon_rules ────────────────────
    if response_type == "offer_info":
        coupon_rules = ctx.get("coupon_rules", "").strip()
        lines = [f"مرحباً {customer_name}! 🏷️ *العروض والخصومات{store_ref}:*\n"]
        if coupon_rules:
            # Store owner has configured coupon rules — use them directly
            lines.append(coupon_rules)
        else:
            lines.append("• تواصل معنا للاطلاع على العروض الحالية")
        lines.append("\nهل تريد الاستفادة من أحد هذه العروض؟ 🎁")
        return "\n".join(lines), False, False

    # ── Order flow initiation ──────────────────────────────────────────────────
    if response_type == "start_order_flow":
        if not permissions.get("allow_order_creation", True):
            store_url = ctx.get("store_url", "")
            suffix = f" {store_url}" if store_url else ""
            return (
                f"خدمة الطلب عبر الدردشة غير متاحة حالياً."
                f" يرجى الطلب من متجرنا مباشرة.{suffix}",
                False, False,
            )
        lines = [f"ممتاز {customer_name}! سأساعدك في إتمام طلبك{store_ref}. 🛍️\n"]
        if products:
            lines.append("*المنتجات المتاحة:*")
            lines.append(_format_product_list(products, max_items=4))
        lines.append("\nمن فضلك أخبرني:\n1️⃣ أي المنتجات تريد؟\n2️⃣ الكمية المطلوبة")
        return "\n".join(lines), True, False

    # ── Payment link ───────────────────────────────────────────────────────────
    if response_type == "payment_link":
        if not permissions.get("allow_payment_link_sending", True):
            return "خدمة الدفع الإلكتروني غير متاحة حالياً.", False, False
        lines = [f"💳 *رابط الدفع الآمن{store_ref}*\n"]
        if payment_link_url:
            lines.append(f"يمكنك إتمام الدفع عبر الرابط التالي:\n{payment_link_url}")
            lines.append("\nالرابط صالح لمدة 24 ساعة.")
        else:
            lines.append("سيتم إرسال رابط الدفع إليك قريباً.")
        return "\n".join(lines), False, False

    # ── COD flow ───────────────────────────────────────────────────────────────
    if response_type == "cod_flow":
        if not permissions.get("allow_cod_confirmation_flow", True):
            return "الدفع عند الاستلام غير متاح حالياً. يرجى اختيار الدفع الإلكتروني.", False, False
        collect_address = permissions.get("allow_address_collection", True)
        lines = [
            f"💵 *الدفع عند الاستلام*\n",
            f"ممتاز {customer_name}! سنُعدّ طلبك{store_ref} بالدفع عند الاستلام.\n",
            "أرسل لنا البيانات التالية:\n",
            "من فضلك أكّد:",
            "1️⃣ المنتج والكمية",
            "2️⃣ الاسم الكامل (الاسم الأول والأخير)",
            "3️⃣ رقم الجوال",
        ]
        if collect_address:
            lines += [
                "4️⃣ *العنوان الوطني:*",
                "   • رقم المبنى",
                "   • اسم الشارع",
                "   • الحي",
                "   • الرمز البريدي",
                "   • المدينة",
            ]
        lines.append("\nبعد التأكيد سنرسل لك رقم الطلب ✅")
        return "\n".join(lines), True, False

    # ── Order tracking ─────────────────────────────────────────────────────────
    if response_type == "order_tracking":
        store_url = ctx.get("store_url", "")
        lines = [
            f"مرحباً {customer_name}! 📍",
            f"لتتبع طلبك{store_ref}، أرسل لنا رقم الطلب وسنرسل لك التحديث الفوري.",
        ]
        if store_url:
            lines.append(f"يمكنك أيضاً تتبع طلبك مباشرة من: {store_url}")
        return "\n".join(lines), False, False

    # ── General / low-confidence fallback ─────────────────────────────────────
    lines = [f"مرحباً {customer_name}! 👋 كيف يمكنني مساعدتك{store_ref}؟"]
    if products:
        lines.append(f"\nلدينا {len(products)} منتج متاح. اسألني عن أي منتج أو سعر أو عرض! 😊")
    else:
        lines.append("\nتواصل معنا وسنكون سعداء بمساعدتك!")
    return "\n".join(lines), False, False


def _log_ai_sales_event(
    db: Session,
    tenant_id: int,
    customer_phone: str,
    customer_name: str,
    message: str,
    intent: str,
    confidence: float,
    response_text: str,
    product_used: bool,
    order_created: bool,
    payment_link_sent: bool,
    handoff_triggered: bool,
    order_id: Optional[int] = None,
) -> AutomationEvent:
    """Write a structured AI Sales log entry as an AutomationEvent row."""
    event = AutomationEvent(
        tenant_id=tenant_id,
        event_type="ai_sales_log",
        customer_id=None,
        payload={
            "customer_phone":    customer_phone,
            "customer_name":     customer_name,
            "message":           message[:500],
            "intent":            intent,
            "confidence":        confidence,
            "response_text":     response_text[:500],
            "product_used":      product_used,
            "order_created":     order_created,
            "payment_link_sent": payment_link_sent,
            "handoff_triggered": handoff_triggered,
            "order_id":          order_id,
        },
        processed=True,
        created_at=datetime.utcnow(),
    )
    db.add(event)
    return event


# ── Pydantic models for AI Sales ──────────────────────────────────────────────

class AiSalesProcessMessageIn(BaseModel):
    customer_phone: str
    message: str
    customer_name: Optional[str] = None


class AiSalesSettingsIn(BaseModel):
    enable_ai_sales_agent:        Optional[bool] = None
    allow_product_recommendations: Optional[bool] = None
    allow_order_creation:         Optional[bool] = None
    allow_address_collection:     Optional[bool] = None
    allow_payment_link_sending:   Optional[bool] = None
    allow_cod_confirmation_flow:  Optional[bool] = None
    allow_human_handoff:          Optional[bool] = None
    confidence_threshold:         Optional[float] = None


class AiSalesCreateOrderIn(BaseModel):
    customer_phone:   str
    customer_name:    str = ""
    product_id:       Optional[int] = None
    product_name:     str = ""
    variant_id:       Optional[int] = None
    quantity:         int = 1
    # Saudi national address fields
    building_number:  str = ""   # رقم المبنى
    street:           str = ""   # اسم الشارع
    district:         str = ""   # الحي
    postal_code:      str = ""   # الرمز البريدي
    city:             str = ""   # المدينة
    address:          str = ""   # عنوان نصي إضافي (اختياري)
    payment_method:   str = "cod"  # "cod" | "pay_now"
    notes:            str = ""


# ── AI Sales endpoints ─────────────────────────────────────────────────────────

@app.get("/ai-sales/settings")
async def get_ai_sales_settings(request: Request, db: Session = Depends(get_db)):
    """Return AI Sales Agent settings for this tenant."""
    tenant_id = _resolve_tenant_id(request)
    _get_or_create_tenant(db, tenant_id)
    return {"settings": _get_ai_sales_settings(db, tenant_id)}


@app.put("/ai-sales/settings")
async def put_ai_sales_settings(
    body: AiSalesSettingsIn,
    request: Request,
    db: Session = Depends(get_db),
):
    """Save AI Sales Agent settings (partial update — only provided fields)."""
    try:
        tenant_id = _resolve_tenant_id(request)
        _get_or_create_tenant(db, tenant_id)
        # model_dump() is the Pydantic v2 API; body.dict() is deprecated and
        # can behave inconsistently with FastAPI 0.111.0 + Pydantic v2.
        patch = {k: v for k, v in body.model_dump().items() if v is not None}
        updated = _save_ai_sales_settings(db, tenant_id, patch)
        db.commit()
        logger.info(f"[AI Sales] settings updated for tenant={tenant_id} keys={list(patch.keys())}")
        return {"settings": updated}
    except Exception as exc:
        logger.error(f"[AI Sales] PUT /ai-sales/settings failed: {exc}", exc_info=True)
        raise HTTPException(status_code=500, detail="Failed to save AI Sales settings")


@app.post("/ai-sales/process-message")
async def ai_sales_process_message(body: AiSalesProcessMessageIn, request: Request, db: Session = Depends(get_db)):
    """
    Process an incoming WhatsApp message through the AI Sales Agent.

    Body: { customer_phone, message, customer_name? }
    Returns: { intent, confidence, response_text, products_used,
               order_started, payment_link, handoff_triggered }
    """
    tenant_id = _resolve_tenant_id(request)
    _get_or_create_tenant(db, tenant_id)

    settings = _get_ai_sales_settings(db, tenant_id)
    if not settings.get("enable_ai_sales_agent", False):
        return {
            "intent": "disabled",
            "confidence": 0.0,
            "response_text": "وكيل المبيعات الذكي غير مفعّل.",
            "products_used": False,
            "order_started": False,
            "payment_link": None,
            "handoff_triggered": False,
        }

    message      = body.message.strip()
    cust_phone   = body.customer_phone
    cust_name    = (body.customer_name or "عزيزي العميل").strip() or "عزيزي العميل"

    if not message:
        raise HTTPException(status_code=422, detail="message field is required")

    # Rate limit: 20 messages/min per customer
    _rate_limit(f"msg:{tenant_id}:{cust_phone}", max_count=20, window_seconds=60)

    _msg_start = _time.monotonic()

    # 1. Check for active human handoff — AI is paused for this conversation
    from handoff.manager import get_active_handoff, create_handoff_session
    from handoff.notifier import notify_handoff
    active_handoff = get_active_handoff(db, tenant_id, cust_phone)
    if active_handoff:
        return {
            "intent": "handoff_active",
            "intent_label": "محادثة مع موظف",
            "confidence": 1.0,
            "response_text": (
                f"مرحباً {cust_name}! 👋\n"
                "محادثتك حالياً مع أحد موظفينا. سيرد عليك في أقرب وقت. 🙏"
            ),
            "products_used": False,
            "order_started": False,
            "payment_link": None,
            "handoff_triggered": False,
            "handoff_active": True,
        }

    # 2. Detect intent (keyword engine — used for handoff priority and logging)
    intent, confidence, response_type = _detect_intent(message, settings)

    # 3. Immediate handoff — handle before orchestrator call
    if response_type == "human_handoff" and settings.get("allow_human_handoff", True):
        handoff_settings = _get_handoff_settings(db, tenant_id)
        handoff_session = create_handoff_session(
            db, tenant_id, cust_phone, cust_name, message,
            reason="customer_request",
        )
        if not handoff_session.notification_sent:
            sent = await notify_handoff(
                handoff_session.id, tenant_id, cust_phone, cust_name,
                message, handoff_settings,
            )
            if sent:
                handoff_session.notification_sent = True
        _log_ai_sales_event(
            db, tenant_id, cust_phone, cust_name, message,
            "talk_to_human", 0.95,
            f"مرحباً {cust_name} 👋\nسأحوّلك الآن إلى أحد موظفينا. انتظر لحظة من فضلك. 🙏",
            False, False, False, True,
        )
        from observability.event_logger import log_event
        log_event(
            db, tenant_id,
            category="handoff",
            event_type="handoff.triggered",
            summary=f"تحويل بشري: {cust_phone} — '{message[:60]}'",
            severity="info",
            payload={"customer_phone": cust_phone, "session_id": handoff_session.id},
            reference_id=str(handoff_session.id),
        )
        db.commit()
        return {
            "intent": "talk_to_human",
            "intent_label": "التحدث مع موظف",
            "confidence": 0.95,
            "response_text": f"مرحباً {cust_name} 👋\nسأحوّلك الآن إلى أحد موظفينا. انتظر لحظة من فضلك. 🙏",
            "products_used": False,
            "order_started": False,
            "payment_link": None,
            "handoff_triggered": True,
            "handoff_active": False,
            "handoff_session_id": handoff_session.id,
        }

    # 4. Load product catalog and store context
    sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), ".")))
    from store_integration.shipping_service import get_shipping_options, format_shipping_lines
    from store_integration.payment_service import generate_payment_link as store_payment_link

    products = await _get_product_catalog(db, tenant_id)
    ctx = _build_response_context(db, tenant_id)

    # Override shipping lines with live store data when adapter is configured
    if response_type == "shipping_info":
        live_shipping = await get_shipping_options(tenant_id)
        if live_shipping:
            ctx["shipping_lines"] = format_shipping_lines(live_shipping)

    products_used = response_type in ("product_info", "price_info", "recommendation", "start_order_flow", "cod_flow")

    # 5. Resolve payment link
    payment_link = None
    if response_type == "payment_link" and settings.get("allow_payment_link_sending", True):
        payment_link = await store_payment_link(tenant_id, str(tenant_id), 0.0)

    # 6. Route through AI Orchestrator (Claude + FactGuard + PolicyGuard)
    #    Falls back to keyword engine if orchestrator is unavailable.
    orch_result = await _call_orchestrator(tenant_id, cust_phone, message)

    if orch_result and orch_result.get("reply"):
        response_text = orch_result["reply"]
        # Detect order intent from orchestrator actions
        order_started = any(
            a.get("type") in ("propose_order", "create_draft_order") and a.get("executable", False)
            for a in (orch_result.get("actions") or [])
        )
        handoff_triggered = False
        logger.info(
            f"[AISales] Orchestrator response used | tenant={tenant_id} "
            f"model={orch_result.get('model_used', '?')} "
            f"fact_guard_modified={orch_result.get('fact_guard', {}).get('was_modified', False)}"
        )
    else:
        # Fallback: keyword-based response builder
        threshold = settings.get("confidence_threshold", 0.55)
        if confidence < threshold and intent not in ("general",):
            response_text = (
                f"مرحباً {cust_name}! 😊 لم أفهم طلبك تماماً.\n"
                "هل تريد:\n• معرفة منتجاتنا وأسعارها؟\n• إتمام طلب؟\n• التواصل مع موظف؟\n\n"
                "أجبني وسأكون سعيداً بمساعدتك! 🌟"
            )
            order_started = False
            handoff_triggered = False
        else:
            response_text, order_started, handoff_triggered = _build_ai_sales_response(
                intent, response_type, products, settings, ctx, cust_name, payment_link,
            )

    # 4. Log event + observability
    _log_ai_sales_event(
        db, tenant_id, cust_phone, cust_name, message,
        intent, confidence, response_text,
        products_used, order_started, payment_link is not None, handoff_triggered,
    )

    _latency = int((_time.monotonic() - _msg_start) * 1000)
    _orch_used = bool(orch_result and orch_result.get("reply"))
    _fg = orch_result.get("fact_guard", {}) if orch_result else {}

    sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), ".")))
    from observability.event_logger import log_event, write_trace

    write_trace(
        db, tenant_id, cust_phone,
        message=message,
        detected_intent=intent,
        confidence=confidence,
        response_type=response_type,
        response_text=response_text,
        orchestrator_used=_orch_used,
        model_used=orch_result.get("model_used", "") if orch_result else "keyword",
        fact_guard_modified=_fg.get("was_modified", False),
        fact_guard_claims=_fg.get("claims_detected", []),
        actions_triggered=[
            {"type": a.get("type"), "executable": a.get("executable")}
            for a in (orch_result.get("actions") or [])
        ] if orch_result else [],
        order_started=order_started,
        payment_link_sent=payment_link is not None,
        handoff_triggered=handoff_triggered,
        latency_ms=_latency,
    )

    log_event(
        db, tenant_id,
        category="ai_sales",
        event_type="ai_sales.message_processed",
        summary=f"[{intent}] {cust_phone}: {message[:60]}",
        severity="info",
        payload={
            "intent": intent, "confidence": confidence,
            "orchestrator": _orch_used, "latency_ms": _latency,
            "handoff": handoff_triggered, "order": order_started,
        },
        reference_id=cust_phone,
    )

    db.commit()

    return {
        "intent":            intent,
        "intent_label":      AI_SALES_INTENTS.get(intent, {}).get("label", "عام"),
        "confidence":        confidence,
        "response_text":     response_text,
        "products_used":     products_used,
        "order_started":     order_started,
        "payment_link":      payment_link,
        "handoff_triggered": handoff_triggered,
    }


@app.post("/ai-sales/create-order")
async def ai_sales_create_order(
    body: AiSalesCreateOrderIn,
    request: Request,
    db: Session = Depends(get_db),
):
    """
    Create an order draft from an AI sales conversation.
    Supports payment_method = 'cod' | 'pay_now'.
    - COD → status pending_confirmation, triggers COD confirmation flow
    - pay_now → status payment_pending, returns payment link
    """
    tenant_id = _resolve_tenant_id(request)
    _get_or_create_tenant(db, tenant_id)
    settings = _get_ai_sales_settings(db, tenant_id)

    if not settings.get("allow_order_creation", True):
        raise HTTPException(status_code=403, detail="Order creation is disabled for this tenant")

    # Rate limit: 5 orders/hour per customer
    _rate_limit(f"order:{tenant_id}:{body.customer_phone}", max_count=5, window_seconds=3600)

    if body.payment_method == "cash_on_delivery" or body.payment_method == "cod":
        if not settings.get("allow_cod_confirmation_flow", True):
            raise HTTPException(status_code=403, detail="COD orders are disabled for this tenant")
        order_status = "pending_confirmation"
        payment_link = None
    else:
        if not settings.get("allow_payment_link_sending", True):
            raise HTTPException(status_code=403, detail="Online payment is disabled for this tenant")
        order_status = "payment_pending"
        payment_link = None  # will be resolved below via store adapter or placeholder

    # Try to create order in the real store first
    sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), ".")))
    from store_integration.order_service import create_order as store_create_order
    from store_integration.models import OrderInput as StoreOrderInput, OrderItemInput as StoreOrderItem
    from store_integration.payment_service import generate_payment_link as store_payment_link, has_real_payment

    store_order = None
    if body.payment_method in ("pay_now",):
        store_order_input = StoreOrderInput(
            customer_name=body.customer_name,
            customer_phone=body.customer_phone,
            building_number=body.building_number or "",
            street=body.street or "",
            district=body.district or "",
            postal_code=body.postal_code or "",
            city=body.city or "",
            address=body.address or "",
            payment_method=body.payment_method,
            items=[StoreOrderItem(
                product_id=str(body.product_id) if body.product_id else "0",
                variant_id=str(body.variant_id) if body.variant_id else None,
                quantity=body.quantity,
            )],
            notes=body.notes,
        )
        store_order = await store_create_order(tenant_id, store_order_input)

    if store_order:
        # Real store order created — use its ID and payment link
        external_order_id = store_order.id
        payment_link = store_order.payment_link
        if not payment_link and order_status == "payment_pending":
            payment_link = await store_payment_link(tenant_id, external_order_id, 0.0)
    else:
        external_order_id = None
        if order_status == "payment_pending":
            payment_link = await store_payment_link(tenant_id, str(tenant_id), 0.0)

    # Find or create customer
    customer = db.query(Customer).filter(
        Customer.phone == body.customer_phone,
        Customer.tenant_id == tenant_id,
    ).first()
    if not customer:
        customer = Customer(
            tenant_id=tenant_id,
            phone=body.customer_phone,
            name=body.customer_name or body.customer_phone,
        )
        db.add(customer)
        db.flush()

    # Look up product name if only id provided
    product_display = body.product_name
    if not product_display and body.product_id:
        prod = db.query(Product).filter(
            Product.id == body.product_id,
            Product.tenant_id == tenant_id,
        ).first()
        if prod:
            product_display = prod.title

    # Build line items
    line_items = [{
        "product_id":   body.product_id,
        "product_name": product_display or "منتج غير محدد",
        "variant_id":   body.variant_id,
        "quantity":     body.quantity,
    }]

    # Calculate a rough total from product price
    total_str = "—"
    if body.product_id:
        prod = db.query(Product).filter(
            Product.id == body.product_id,
            Product.tenant_id == tenant_id,
        ).first()
        if prod and prod.price:
            try:
                total_str = f"{float(prod.price.replace('ر.س','').replace(',','').strip()) * body.quantity:.2f} ر.س"
            except Exception:
                total_str = prod.price

    order = Order(
        tenant_id=tenant_id,
        status=order_status,
        total=total_str,
        external_id=external_order_id,
        customer_info={
            "name":            body.customer_name,
            "phone":           body.customer_phone,
            "building_number": body.building_number,
            "street":          body.street,
            "district":        body.district,
            "postal_code":     body.postal_code,
            "city":            body.city,
            "address":         body.address,
        },
        line_items=line_items,
        checkout_url=payment_link,
        extra_metadata={
            "source":         "ai_sales_agent",
            "payment_method": body.payment_method,
            "notes":          body.notes,
            "created_via":    "whatsapp_conversation",
        },
    )
    db.add(order)
    db.flush()

    # If COD → emit COD confirmation event so autopilot picks it up
    if order_status == "pending_confirmation":
        ap = _get_autopilot_settings(db, tenant_id)
        if ap.get("enabled") and ap.get("cod_confirmation", {}).get("enabled"):
            _log_autopilot_event(
                db, tenant_id, "cod_confirmation",
                customer.id,
                {"order_id": order.id, "source": "ai_sales_agent"},
            )

    # Log AI sales order creation
    _log_ai_sales_event(
        db, tenant_id, body.customer_phone, body.customer_name,
        f"[order_created] product={product_display} qty={body.quantity} method={body.payment_method}",
        "order_product", 1.0,
        f"تم إنشاء طلب رقم #{order.id} بنجاح",
        product_used=True, order_created=True,
        payment_link_sent=(payment_link is not None),
        handoff_triggered=False,
        order_id=order.id,
    )

    from observability.event_logger import log_event
    log_event(
        db, tenant_id,
        category="order",
        event_type="order.created",
        summary=f"طلب #{order.id} — {product_display or 'منتج'} x{body.quantity} [{body.payment_method}]",
        severity="info",
        payload={
            "order_id": order.id, "status": order_status,
            "product": product_display, "qty": body.quantity,
            "method": body.payment_method, "external_id": external_order_id,
        },
        reference_id=str(order.id),
    )
    db.commit()

    return {
        "order_id":     order.id,
        "order_status": order_status,
        "payment_link": payment_link,
        "customer_id":  customer.id,
        "total":        total_str,
        "message": (
            f"تم إنشاء الطلب #{order.id} بنجاح ✅ "
            + ("رابط الدفع أُرسل إليك." if payment_link else "سيتواصل معك فريقنا لتأكيد الطلب.")
        ),
    }


@app.get("/ai-sales/logs")
async def get_ai_sales_logs(
    request: Request,
    db: Session = Depends(get_db),
    limit: int = 50,
    offset: int = 0,
):
    """
    Return AI Sales Agent conversation logs for this tenant.
    Logs are stored as AutomationEvent rows with event_type='ai_sales_log'.
    """
    tenant_id = _resolve_tenant_id(request)
    _get_or_create_tenant(db, tenant_id)

    rows = (
        db.query(AutomationEvent)
        .filter(
            AutomationEvent.tenant_id == tenant_id,
            AutomationEvent.event_type == "ai_sales_log",
        )
        .order_by(AutomationEvent.created_at.desc())
        .offset(offset)
        .limit(limit)
        .all()
    )

    total = (
        db.query(AutomationEvent)
        .filter(
            AutomationEvent.tenant_id == tenant_id,
            AutomationEvent.event_type == "ai_sales_log",
        )
        .count()
    )

    logs = []
    for r in rows:
        p = r.payload or {}
        logs.append({
            "id":                r.id,
            "customer_phone":    p.get("customer_phone", "—"),
            "customer_name":     p.get("customer_name", "—"),
            "message":           p.get("message", ""),
            "intent":            p.get("intent", "general"),
            "intent_label":      AI_SALES_INTENTS.get(p.get("intent",""), {}).get("label", "عام"),
            "confidence":        p.get("confidence", 0),
            "response_text":     p.get("response_text", ""),
            "product_used":      p.get("product_used", False),
            "order_created":     p.get("order_created", False),
            "payment_link_sent": p.get("payment_link_sent", False),
            "handoff_triggered": p.get("handoff_triggered", False),
            "order_id":          p.get("order_id"),
            "timestamp":         r.created_at.isoformat() if r.created_at else None,
        })

    return {"logs": logs, "total": total, "offset": offset, "limit": limit}


# ── Moyasar Payment Gateway ────────────────────────────────────────────────────

class MoyasarSettingsIn(BaseModel):
    enabled: bool = False
    secret_key: str = ""
    publishable_key: str = ""
    webhook_secret: str = ""
    callback_url: str = ""
    success_url: str = ""
    error_url: str = ""


@app.get("/moyasar/settings")
async def get_moyasar_settings(request: Request, db: Session = Depends(get_db)):
    """Return Moyasar settings for this tenant (keys masked)."""
    tenant_id = _resolve_tenant_id(request)
    _get_or_create_tenant(db, tenant_id)
    cfg = _get_moyasar_settings(db, tenant_id)
    return {
        "enabled": cfg.get("enabled", False),
        "publishable_key": cfg.get("publishable_key", ""),
        "secret_key_hint": ("***" + cfg.get("secret_key", "")[-4:]) if cfg.get("secret_key") else "",
        "webhook_secret_set": bool(cfg.get("webhook_secret")),
        "callback_url": cfg.get("callback_url", ""),
        "success_url": cfg.get("success_url", ""),
        "error_url": cfg.get("error_url", ""),
    }


@app.put("/moyasar/settings")
async def put_moyasar_settings(
    body: MoyasarSettingsIn,
    request: Request,
    db: Session = Depends(get_db),
):
    tenant_id = _resolve_tenant_id(request)
    _get_or_create_tenant(db, tenant_id)
    s = _get_or_create_settings(db, tenant_id)
    meta = dict(s.extra_metadata or {})
    meta["moyasar"] = body.dict()
    s.extra_metadata = meta
    db.add(s)
    db.commit()
    return {"status": "saved"}


@app.post("/payments/create-session")
async def create_payment_session(
    request: Request,
    db: Session = Depends(get_db),
):
    """
    Create a Moyasar payment session for an order.
    Body: { order_id, amount_sar, description? }
    Returns: { payment_link, session_id, gateway }
    """
    body = await request.json()
    tenant_id = _resolve_tenant_id(request)
    _get_or_create_tenant(db, tenant_id)

    order_id = body.get("order_id")
    amount_sar = float(body.get("amount_sar", 0))
    description = str(body.get("description", f"طلب #{order_id}"))

    if amount_sar <= 0:
        raise HTTPException(status_code=422, detail="amount_sar must be > 0")

    # Guard: verify order belongs to this tenant before proceeding
    if order_id:
        _order_guard = db.query(Order).filter(
            Order.id == order_id,
            Order.tenant_id == tenant_id,
        ).first()
        if not _order_guard:
            raise HTTPException(status_code=404, detail="Order not found")

    # Rate limit: 3 payment sessions/hour per order_id
    _rate_limit(f"pay:{tenant_id}:{order_id or 'anon'}", max_count=3, window_seconds=3600)

    cfg = _get_moyasar_settings(db, tenant_id)

    if cfg.get("enabled") and cfg.get("secret_key"):
        # Real Moyasar payment session
        sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), ".")))
        from payment_gateways.moyasar import MoyasarClient

        client = MoyasarClient(
            secret_key=cfg["secret_key"],
            publishable_key=cfg.get("publishable_key", ""),
        )
        try:
            invoice = await client.create_invoice(
                amount_sar=amount_sar,
                description=description,
                callback_url=cfg.get("callback_url") or f"https://api.nahla.ai/payments/webhook/moyasar",
                success_url=cfg.get("success_url", ""),
                error_url=cfg.get("error_url", ""),
                metadata={"order_id": str(order_id), "tenant_id": str(tenant_id)},
            )
            gateway_id = invoice.get("id", "")
            payment_link = invoice.get("url", "")
            gateway = "moyasar"
        except Exception as exc:
            logger.error(f"[Moyasar] create_invoice failed for tenant={tenant_id}: {exc}")
            raise HTTPException(status_code=502, detail=f"Payment gateway error: {exc}")
    else:
        # Placeholder — Moyasar not configured
        gateway_id = ""
        payment_link = f"https://pay.nahla.ai/checkout/{tenant_id}-{datetime.utcnow().strftime('%Y%m%d%H%M%S')}"
        gateway = "placeholder"
        logger.warning(f"[Payment] Moyasar not configured for tenant={tenant_id}, returning placeholder")

    # Persist PaymentSession
    session = PaymentSession(
        tenant_id=tenant_id,
        order_id=order_id,
        gateway=gateway,
        gateway_payment_id=gateway_id,
        amount_sar=amount_sar,
        currency="SAR",
        status="pending",
        payment_link=payment_link,
        created_at=datetime.utcnow(),
        updated_at=datetime.utcnow(),
    )
    db.add(session)

    # Attach payment link to Order (ownership already verified above)
    if order_id:
        _order_guard.checkout_url = payment_link

    from observability.event_logger import log_event
    log_event(
        db, tenant_id,
        category="payment",
        event_type="payment.session_created",
        summary=f"رابط دفع بقيمة {amount_sar} ر.س [{gateway}]",
        severity="info" if gateway != "placeholder" else "warning",
        payload={"amount_sar": amount_sar, "gateway": gateway, "order_id": order_id},
        reference_id=str(order_id) if order_id else None,
    )
    db.commit()

    return {
        "session_id": session.id,
        "payment_link": payment_link,
        "gateway": gateway,
        "amount_sar": amount_sar,
    }


# ── Salla Webhook ──────────────────────────────────────────────────────────────
_SALLA_WEBHOOK_SECRET = os.environ.get("SALLA_WEBHOOK_SECRET", "")

@app.post("/webhook/salla")
async def salla_webhook(request: Request, db: Session = Depends(get_db)):
    """
    Receive event notifications from Salla.
    Verifies HMAC-SHA256 signature when SALLA_WEBHOOK_SECRET is set.
    Logs every event so we can see them in Railway logs.

    Salla sends:  X-Salla-Signature: sha256=<hex>
    """
    raw_body = await request.body()
    client_ip = request.headers.get("X-Real-IP") or (
        request.client.host if request.client else "unknown"
    )

    # ── Signature verification ─────────────────────────────────────────────────
    if _SALLA_WEBHOOK_SECRET:
        sig_header = request.headers.get("X-Salla-Signature", "")
        # Header format: "sha256=<hex_digest>"
        if sig_header.startswith("sha256="):
            received_sig = sig_header[7:]
        else:
            received_sig = sig_header

        expected_sig = hmac.new(
            _SALLA_WEBHOOK_SECRET.encode(),
            raw_body,
            "sha256",
        ).hexdigest()

        if not hmac.compare_digest(received_sig, expected_sig):
            logger.warning(
                "Salla webhook: invalid signature | ip=%s sig_received=%s",
                client_ip, sig_header[:20],
            )
            _audit("salla_webhook_invalid_signature", ip=client_ip)
            return JSONResponse(status_code=401, content={"detail": "Invalid signature"})
    else:
        logger.warning("Salla webhook: SALLA_WEBHOOK_SECRET not set — skipping signature check")

    # ── Parse payload ──────────────────────────────────────────────────────────
    try:
        payload = await request.json()
    except Exception:
        payload = {}

    event      = payload.get("event", "unknown")
    store_id   = payload.get("merchant", payload.get("store_id", "unknown"))
    created_at = payload.get("created_at", "")

    logger.info(
        "Salla webhook received | event=%s store_id=%s created_at=%s ip=%s",
        event, store_id, created_at, client_ip,
    )
    _audit("salla_webhook", event=event, store_id=store_id, ip=client_ip)

    # ── Route by event type ────────────────────────────────────────────────────
    # All events are logged above. Specific handling will be added per event type.
    data = payload.get("data", {})

    if event == "order.created":
        order_id = data.get("id") or data.get("reference_id", "")
        logger.info("Salla order.created | order_id=%s store=%s", order_id, store_id)

    elif event == "order.updated":
        order_id = data.get("id") or data.get("reference_id", "")
        status   = data.get("status", {})
        logger.info("Salla order.updated | order_id=%s status=%s store=%s", order_id, status, store_id)

    elif event == "shipment.created":
        shipment_id = data.get("id", "")
        logger.info("Salla shipment.created | shipment_id=%s store=%s", shipment_id, store_id)

    elif event == "customer.created":
        customer_email = data.get("email", "")
        logger.info("Salla customer.created | email=%s store=%s", customer_email, store_id)

    elif event == "app.installed":
        logger.info("Salla app.installed | store=%s", store_id)

    elif event == "app.uninstalled":
        logger.info("Salla app.uninstalled | store=%s", store_id)

    else:
        logger.info("Salla webhook unhandled event=%s store=%s | data=%s", event, store_id, str(data)[:200])

    return {"status": "ok", "event": event}


@app.post("/payments/webhook/moyasar")
async def moyasar_webhook(request: Request, db: Session = Depends(get_db)):
    """
    Handle Moyasar payment webhook callbacks.
    Verifies HMAC-SHA256 signature and updates Order + PaymentSession status.
    """
    raw_body = await request.body()
    signature = request.headers.get("signature", "")

    # We process the event even without tenant context (it comes from Moyasar)
    try:
        data = await request.json()
    except Exception:
        raise HTTPException(status_code=400, detail="Invalid JSON body")

    # Extract metadata to find tenant + order
    meta = data.get("metadata") or {}
    tenant_id = int(meta.get("tenant_id", 0))
    order_id_str = meta.get("order_id", "")
    payment_id = data.get("id", "")
    payment_status = data.get("status", "")   # paid | failed | authorized

    if tenant_id:
        cfg = _get_moyasar_settings(db, tenant_id)
        webhook_secret = cfg.get("webhook_secret", "")

        # Verify signature when secret is configured
        if webhook_secret and signature:
            sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), ".")))
            from payment_gateways.moyasar import MoyasarClient
            client = MoyasarClient(secret_key=cfg.get("secret_key", ""))
            if not client.verify_webhook_signature(raw_body, signature, webhook_secret):
                logger.warning(f"[Moyasar Webhook] Invalid signature for tenant={tenant_id}")
                raise HTTPException(status_code=401, detail="Invalid webhook signature")

        # Update PaymentSession
        ps = (
            db.query(PaymentSession)
            .filter(
                PaymentSession.gateway_payment_id == payment_id,
                PaymentSession.tenant_id == tenant_id,
            )
            .first()
        )
        if ps:
            ps.status = "paid" if payment_status in ("paid", "authorized") else "failed"
            ps.callback_data = data
            ps.updated_at = datetime.utcnow()

        # Update Order status
        if order_id_str:
            try:
                oid = int(order_id_str)
                order = db.query(Order).filter(Order.id == oid, Order.tenant_id == tenant_id).first()
                if order:
                    if payment_status in ("paid", "authorized"):
                        order.status = "paid"
                        logger.info(
                            f"[Moyasar Webhook] Order #{oid} marked paid for tenant={tenant_id}"
                        )
                    elif payment_status == "failed":
                        order.status = "payment_failed"
            except (ValueError, TypeError):
                pass

        from observability.event_logger import log_event
        _sev = "info" if payment_status in ("paid", "authorized") else "warning"
        log_event(
            db, tenant_id,
            category="payment",
            event_type=f"payment.{payment_status}",
            summary=f"Moyasar {payment_status}: payment {payment_id}",
            severity=_sev,
            payload={"payment_id": payment_id, "status": payment_status, "order_id": order_id_str},
            reference_id=order_id_str or payment_id,
        )
        db.commit()

    logger.info(
        f"[Moyasar Webhook] id={payment_id} status={payment_status} tenant={tenant_id}"
    )
    return {"received": True}


# ── Human Handoff Management ───────────────────────────────────────────────────

class HandoffSettingsIn(BaseModel):
    notification_method: str = "webhook"
    webhook_url: str = ""
    staff_whatsapp: str = ""
    auto_pause_ai: bool = True


@app.get("/handoff/settings")
async def get_handoff_settings_endpoint(request: Request, db: Session = Depends(get_db)):
    tenant_id = _resolve_tenant_id(request)
    _get_or_create_tenant(db, tenant_id)
    return {"settings": _get_handoff_settings(db, tenant_id)}


@app.put("/handoff/settings")
async def put_handoff_settings_endpoint(
    body: HandoffSettingsIn,
    request: Request,
    db: Session = Depends(get_db),
):
    tenant_id = _resolve_tenant_id(request)
    _get_or_create_tenant(db, tenant_id)
    s = _get_or_create_settings(db, tenant_id)
    meta = dict(s.extra_metadata or {})
    meta["handoff_settings"] = body.dict()
    s.extra_metadata = meta
    db.add(s)
    db.commit()
    return {"settings": body.dict()}


@app.get("/handoff/sessions")
async def list_handoff_sessions(
    request: Request,
    db: Session = Depends(get_db),
    status: str = "active",
    limit: int = 50,
    offset: int = 0,
):
    """List handoff sessions for the staff queue."""
    tenant_id = _resolve_tenant_id(request)
    query = (
        db.query(HandoffSession)
        .filter(HandoffSession.tenant_id == tenant_id)
    )
    if status in ("active", "resolved"):
        query = query.filter(HandoffSession.status == status)
    total = query.count()
    rows = query.order_by(HandoffSession.created_at.desc()).offset(offset).limit(limit).all()
    return {
        "sessions": [
            {
                "id": r.id,
                "customer_phone": r.customer_phone,
                "customer_name": r.customer_name or "—",
                "status": r.status,
                "handoff_reason": r.handoff_reason,
                "last_message": r.last_message,
                "notification_sent": r.notification_sent,
                "resolved_by": r.resolved_by,
                "resolved_at": r.resolved_at.isoformat() if r.resolved_at else None,
                "created_at": r.created_at.isoformat() if r.created_at else None,
            }
            for r in rows
        ],
        "total": total,
        "offset": offset,
        "limit": limit,
    }


@app.put("/handoff/sessions/{session_id}/resolve")
async def resolve_handoff_session_endpoint(
    session_id: int,
    request: Request,
    db: Session = Depends(get_db),
):
    """
    Mark a handoff session as resolved and resume AI responses for the customer.
    Body: { resolved_by? }
    """
    tenant_id = _resolve_tenant_id(request)
    body = {}
    try:
        body = await request.json()
    except Exception:
        pass
    resolved_by = body.get("resolved_by", "staff")

    from handoff.manager import resolve_handoff_session
    session = resolve_handoff_session(db, session_id, tenant_id, resolved_by)
    if not session:
        raise HTTPException(status_code=404, detail="Handoff session not found")
    from observability.event_logger import log_event
    log_event(
        db, tenant_id,
        category="handoff",
        event_type="handoff.resolved",
        summary=f"تم حل التحويل #{session_id} بواسطة {resolved_by}",
        severity="info",
        payload={"session_id": session_id, "resolved_by": resolved_by},
        reference_id=str(session_id),
    )
    db.commit()
    return {
        "session_id": session.id,
        "status": session.status,
        "resolved_by": session.resolved_by,
        "resolved_at": session.resolved_at.isoformat() if session.resolved_at else None,
    }


# ── Store Integration settings ────────────────────────────────────────────────

class StoreIntegrationSettingsIn(BaseModel):
    platform: str = "salla"
    api_key: str
    store_id: str = ""
    webhook_secret: str = ""
    enabled: bool = True


@app.get("/store-integration/settings")
async def get_store_integration_settings(request: Request, db: Session = Depends(get_db)):
    """Return store integration config for this tenant (api_key masked)."""
    tenant_id = _resolve_tenant_id(request)
    _get_or_create_tenant(db, tenant_id)
    from models import Integration
    integration = db.query(Integration).filter(
        Integration.tenant_id == tenant_id,
        Integration.provider.in_(["salla"]),
    ).first()
    if not integration:
        return {"configured": False, "platform": None, "store_id": "", "enabled": False}
    cfg = integration.config or {}
    return {
        "configured": True,
        "platform": integration.provider,
        "store_id": cfg.get("store_id", ""),
        "api_key_hint": ("***" + cfg.get("api_key", "")[-4:]) if cfg.get("api_key") else "",
        "enabled": integration.enabled,
    }


@app.put("/store-integration/settings")
async def put_store_integration_settings(
    body: StoreIntegrationSettingsIn,
    request: Request,
    db: Session = Depends(get_db),
):
    """Save or update store integration credentials."""
    tenant_id = _resolve_tenant_id(request)
    _get_or_create_tenant(db, tenant_id)
    from models import Integration
    integration = db.query(Integration).filter(
        Integration.tenant_id == tenant_id,
        Integration.provider == body.platform,
    ).first()
    new_config = {
        "api_key": body.api_key,
        "store_id": body.store_id,
        "webhook_secret": body.webhook_secret,
    }
    if integration:
        integration.config = new_config
        integration.enabled = body.enabled
    else:
        integration = Integration(
            tenant_id=tenant_id,
            provider=body.platform,
            config=new_config,
            enabled=body.enabled,
        )
        db.add(integration)
    db.commit()
    return {"status": "saved", "platform": body.platform, "enabled": body.enabled}


@app.delete("/store-integration/settings")
async def delete_store_integration_settings(request: Request, db: Session = Depends(get_db)):
    """Disable store integration for this tenant."""
    tenant_id = _resolve_tenant_id(request)
    from models import Integration
    integration = db.query(Integration).filter(
        Integration.tenant_id == tenant_id,
    ).first()
    if integration:
        integration.enabled = False
        db.commit()
    return {"status": "disabled"}


@app.get("/store-integration/test")
async def test_store_integration(request: Request):
    """Test connectivity to the configured store adapter."""
    tenant_id = _resolve_tenant_id(request)
    sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), ".")))
    from store_integration.registry import get_adapter
    adapter = get_adapter(tenant_id)
    if not adapter:
        return {"status": "not_configured", "message": "No store integration configured"}
    try:
        products = await adapter.get_products()
        return {
            "status": "ok",
            "platform": adapter.platform,
            "products_found": len(products),
            "sample": products[0].dict() if products else None,
        }
    except Exception as exc:
        return {"status": "error", "platform": adapter.platform, "error": str(exc)}


# ── System Observability ───────────────────────────────────────────────────────

@app.get("/system/health")
async def system_health(request: Request, db: Session = Depends(get_db)):
    """
    Comprehensive health check for all system components.
    Returns component status + overall system status.
    """
    from observability.health import (
        check_database, check_orchestrator, check_moyasar, check_salla, overall_status
    )
    tenant_id = _resolve_tenant_id(request)
    _get_or_create_tenant(db, tenant_id)

    moyasar_cfg = _get_moyasar_settings(db, tenant_id)

    components = {
        "database":     await check_database(db),
        "orchestrator": await check_orchestrator(ORCHESTRATOR_URL),
        "moyasar":      check_moyasar(moyasar_cfg),
        "salla":        check_salla(tenant_id),
    }

    return {
        "status":      overall_status(components),
        "environment": ENVIRONMENT,
        "production":  IS_PRODUCTION,
        "components":  components,
        "timestamp":   datetime.utcnow().isoformat() + "Z",
    }


@app.get("/system/events")
async def list_system_events(
    request: Request,
    db: Session = Depends(get_db),
    category: str = "",
    severity: str = "",
    limit: int = 100,
    offset: int = 0,
):
    """Return paginated System Event Timeline for this tenant."""
    tenant_id = _resolve_tenant_id(request)
    _get_or_create_tenant(db, tenant_id)

    query = db.query(SystemEvent).filter(SystemEvent.tenant_id == tenant_id)
    if category:
        query = query.filter(SystemEvent.category == category)
    if severity:
        query = query.filter(SystemEvent.severity == severity)

    total = query.count()
    rows = (
        query.order_by(SystemEvent.created_at.desc())
        .offset(offset)
        .limit(min(limit, 200))
        .all()
    )

    return {
        "events": [
            {
                "id":           r.id,
                "category":     r.category,
                "event_type":   r.event_type,
                "severity":     r.severity,
                "summary":      r.summary,
                "reference_id": r.reference_id,
                "payload":      r.payload,
                "created_at":   r.created_at.isoformat() if r.created_at else None,
            }
            for r in rows
        ],
        "total":  total,
        "offset": offset,
        "limit":  limit,
    }


@app.get("/conversations/trace/{customer_phone}")
async def get_conversation_trace(
    customer_phone: str,
    request: Request,
    db: Session = Depends(get_db),
    limit: int = 50,
):
    """Return conversation trace turns for a specific customer (latest session first)."""
    tenant_id = _resolve_tenant_id(request)
    _get_or_create_tenant(db, tenant_id)

    rows = (
        db.query(ConversationTrace)
        .filter(
            ConversationTrace.tenant_id == tenant_id,
            ConversationTrace.customer_phone == customer_phone,
        )
        .order_by(ConversationTrace.created_at.desc())
        .limit(limit)
        .all()
    )

    return {
        "customer_phone": customer_phone,
        "turns": [
            {
                "id":                  r.id,
                "session_id":          r.session_id,
                "turn":                r.turn,
                "message":             r.message,
                "detected_intent":     r.detected_intent,
                "confidence":          r.confidence,
                "response_type":       r.response_type,
                "response_text":       r.response_text,
                "orchestrator_used":   r.orchestrator_used,
                "model_used":          r.model_used,
                "fact_guard_modified": r.fact_guard_modified,
                "fact_guard_claims":   r.fact_guard_claims,
                "actions_triggered":   r.actions_triggered,
                "order_started":       r.order_started,
                "payment_link_sent":   r.payment_link_sent,
                "handoff_triggered":   r.handoff_triggered,
                "latency_ms":          r.latency_ms,
                "created_at":          r.created_at.isoformat() if r.created_at else None,
            }
            for r in rows
        ],
    }


# ── Billing ────────────────────────────────────────────────────────────────────

@app.get("/billing/plans")
async def list_billing_plans(db: Session = Depends(get_db)):
    """Return all available Nahla SaaS subscription plans."""
    _ensure_billing_plans(db)
    plans = (
        db.query(BillingPlan)
        .filter(BillingPlan.tenant_id == None)  # noqa: E711 — SQLAlchemy requires ==
        .order_by(BillingPlan.price_sar)
        .all()
    )
    result = []
    for p in plans:
        meta = p.extra_metadata or {}
        result.append({
            "id":               p.id,
            "slug":             p.slug,
            "name":             p.name,
            "name_ar":          meta.get("name_ar", p.name),
            "description":      p.description,
            "price_sar":        p.price_sar,
            "launch_price_sar": meta.get("launch_price_sar", p.price_sar),
            "billing_cycle":    p.billing_cycle,
            "features":         p.features or [],
            "limits":           p.limits or {},
        })
    return {"plans": result, "integration_fee_sar": INTEGRATION_FEE_SAR}


@app.get("/billing/status")
async def get_billing_status(request: Request, db: Session = Depends(get_db)):
    """Return the current subscription status for the tenant."""
    tenant_id = _resolve_tenant_id(request)
    _ensure_billing_plans(db)

    sub = _get_tenant_subscription(db, tenant_id)

    # Count conversations (all-time since no created_at on Conversation)
    conversations_used = (
        db.query(Conversation)
        .filter(Conversation.tenant_id == tenant_id)
        .count()
    )

    # Compute trial status from tenant creation date
    tenant = _get_or_create_tenant(db, tenant_id)
    now = datetime.utcnow()
    trial_start = tenant.created_at or now
    trial_elapsed = (now - trial_start).days
    trial_days_remaining = max(0, FREE_TRIAL_DAYS - trial_elapsed)
    is_trial = sub is None and trial_days_remaining > 0
    trial_expired = sub is None and trial_days_remaining == 0

    if sub is None:
        return {
            "has_subscription":       False,
            "plan":                   None,
            "status":                 "trial" if is_trial else "none",
            "is_trial":               is_trial,
            "trial_days_remaining":   trial_days_remaining,
            "trial_expired":          trial_expired,
            "conversations_used":     conversations_used,
            "conversations_limit":    100,  # basic limit during trial
            "launch_discount_active": False,
            "current_price_sar":      0,
            "integration_fee_sar":    INTEGRATION_FEE_SAR,
        }

    plan = db.query(BillingPlan).filter(BillingPlan.id == sub.plan_id).first()
    meta  = plan.extra_metadata or {} if plan else {}
    launch = _is_launch_discount_active(sub)
    price  = meta.get("launch_price_sar", plan.price_sar) if launch else plan.price_sar
    limits = plan.limits or {}

    return {
        "has_subscription":        True,
        "plan": {
            "id":               plan.id,
            "slug":             plan.slug,
            "name":             plan.name,
            "name_ar":          meta.get("name_ar", plan.name),
            "price_sar":        plan.price_sar,
            "launch_price_sar": meta.get("launch_price_sar", plan.price_sar),
            "features":         plan.features or [],
            "limits":           limits,
        },
        "status":                  sub.status,
        "is_trial":                False,
        "trial_days_remaining":    0,
        "trial_expired":           False,
        "started_at":              sub.started_at.isoformat() if sub.started_at else None,
        "conversations_used":      conversations_used,
        "conversations_limit":     limits.get("conversations_per_month", -1),
        "launch_discount_active":  launch,
        "current_price_sar":       price,
        "integration_fee_sar":     INTEGRATION_FEE_SAR,
    }


class SubscribeRequest(BaseModel):
    plan_slug: str


@app.post("/billing/subscribe")
async def subscribe_to_plan(
    body: SubscribeRequest,
    request: Request,
    db: Session = Depends(get_db),
):
    """Activate a Nahla subscription plan for the tenant."""
    tenant_id = _resolve_tenant_id(request)
    _ensure_billing_plans(db)

    plan = (
        db.query(BillingPlan)
        .filter(BillingPlan.slug == body.plan_slug, BillingPlan.tenant_id == None)  # noqa: E711
        .first()
    )
    if not plan:
        raise HTTPException(status_code=404, detail="Plan not found")

    # Cancel any existing active subscriptions
    db.query(BillingSubscription).filter(
        BillingSubscription.tenant_id == tenant_id,
        BillingSubscription.status == "active",
    ).update({"status": "cancelled"}, synchronize_session=False)

    now = datetime.utcnow()
    sub = BillingSubscription(
        tenant_id=tenant_id,
        plan_id=plan.id,
        status="active",
        started_at=now,
        auto_renew=True,
        extra_metadata={"activated_by": "dashboard"},
    )
    db.add(sub)
    db.commit()
    db.refresh(sub)

    meta   = plan.extra_metadata or {}
    launch = _is_launch_discount_active(sub)
    price  = meta.get("launch_price_sar", plan.price_sar) if launch else plan.price_sar

    logger.info(f"[Billing] Tenant {tenant_id} subscribed to plan '{body.plan_slug}' (launch={launch})")
    return {
        "success":               True,
        "subscription_id":       sub.id,
        "plan_slug":             plan.slug,
        "launch_discount_active": launch,
        "current_price_sar":     price,
    }


# ── Billing Checkout (Moyasar) ─────────────────────────────────────────────────

def _get_billing_gateway(db: Session, tenant_id: int):
    """
    Return the configured payment gateway client for billing, or None.
    Currently supports Moyasar; Stripe can be added here later.
    """
    cfg = _get_moyasar_settings(db, tenant_id)
    if cfg.get("enabled") and cfg.get("secret_key"):
        sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), ".")))
        from payment_gateways.moyasar import MoyasarClient
        return MoyasarClient(
            secret_key=cfg["secret_key"],
            publishable_key=cfg.get("publishable_key", ""),
        ), "moyasar", cfg
    # Future: check for Stripe config here
    return None, "demo", {}


class CheckoutRequest(BaseModel):
    plan_slug:   str
    success_url: Optional[str] = None   # base URL to redirect after payment
    error_url:   Optional[str] = None


@app.post("/billing/checkout")
async def create_billing_checkout(
    body: CheckoutRequest,
    request: Request,
    db: Session = Depends(get_db),
):
    """
    Create a payment checkout session for a Nahla subscription plan.

    Flow:
    - If Moyasar is configured → create a hosted invoice, return checkout_url.
      Subscription is created as 'pending_payment' and activated by the webhook.
    - If no gateway is configured (demo/test) → activate the subscription
      immediately and return demo_mode=True.

    Architecture is gateway-agnostic: replace _get_billing_gateway() to add Stripe.
    """
    tenant_id = _resolve_tenant_id(request)
    _ensure_billing_plans(db)

    plan = (
        db.query(BillingPlan)
        .filter(BillingPlan.slug == body.plan_slug, BillingPlan.tenant_id == None)  # noqa: E711
        .first()
    )
    if not plan:
        raise HTTPException(status_code=404, detail="Plan not found")

    plan_meta = plan.extra_metadata or {}
    now        = datetime.utcnow()

    # Apply launch discount if still within the promo window
    is_launch = now <= LAUNCH_PROMO_UNTIL
    price_sar  = int(plan_meta.get("launch_price_sar", plan.price_sar)) if is_launch else int(plan.price_sar)

    # Build redirect URLs (frontend sends its own origin so we stay domain-agnostic)
    base_success = (body.success_url or "").rstrip("/") or "https://app.nahlaai.com"
    base_error   = (body.error_url   or "").rstrip("/") or base_success

    gateway_client, gateway_name, gateway_cfg = _get_billing_gateway(db, tenant_id)

    # ── Real Moyasar flow ──────────────────────────────────────────────────────
    if gateway_client is not None:
        # Cancel stale pending_payment subscriptions for this tenant
        db.query(BillingSubscription).filter(
            BillingSubscription.tenant_id == tenant_id,
            BillingSubscription.status == "pending_payment",
        ).update({"status": "cancelled"}, synchronize_session=False)

        # Create subscription in pending state — activated by webhook on payment
        sub = BillingSubscription(
            tenant_id=tenant_id,
            plan_id=plan.id,
            status="pending_payment",
            started_at=now,
            auto_renew=True,
            extra_metadata={
                "gateway": gateway_name,
                "price_charged_sar": price_sar,
                "launch_discount": is_launch,
            },
        )
        db.add(sub)
        db.flush()   # get sub.id before commit

        success_redirect = f"{base_success}?status=paid&sub_id={sub.id}"
        error_redirect   = f"{base_error}?status=failed&sub_id={sub.id}"

        try:
            invoice = await gateway_client.create_invoice(
                amount_sar=float(price_sar),
                description=f"نحلة — خطة {plan_meta.get('name_ar', plan.name)} (شهري)",
                callback_url="https://api.nahlaai.com/billing/webhook/moyasar/subscription",
                success_url=success_redirect,
                error_url=error_redirect,
                metadata={
                    "subscription_id": str(sub.id),
                    "tenant_id":       str(tenant_id),
                    "plan_slug":       plan.slug,
                },
            )
        except Exception as exc:
            db.rollback()
            logger.error(f"[Billing] Moyasar invoice error tenant={tenant_id}: {exc}")
            raise HTTPException(status_code=502, detail=f"Payment gateway error: {exc}")

        invoice_id   = invoice.get("id", "")
        checkout_url = invoice.get("url", "")

        meta = dict(sub.extra_metadata or {})
        meta["moyasar_invoice_id"] = invoice_id
        sub.extra_metadata = meta
        db.commit()

        logger.info(
            f"[Billing] Checkout created tenant={tenant_id} plan={plan.slug} "
            f"amount={price_sar} SAR invoice={invoice_id}"
        )
        return {
            "subscription_id": sub.id,
            "checkout_url":    checkout_url,
            "gateway":         gateway_name,
            "amount_sar":      price_sar,
            "plan_slug":       plan.slug,
            "demo_mode":       False,
        }

    # ── Demo / no-gateway flow ─────────────────────────────────────────────────
    # No payment gateway configured — activate the subscription immediately.
    db.query(BillingSubscription).filter(
        BillingSubscription.tenant_id == tenant_id,
        BillingSubscription.status == "active",
    ).update({"status": "cancelled"}, synchronize_session=False)

    sub = BillingSubscription(
        tenant_id=tenant_id,
        plan_id=plan.id,
        status="active",
        started_at=now,
        auto_renew=True,
        extra_metadata={
            "activated_by":     "demo_checkout",
            "price_charged_sar": price_sar,
            "launch_discount":   is_launch,
        },
    )
    db.add(sub)
    db.commit()
    db.refresh(sub)

    logger.info(f"[Billing] Demo checkout: tenant={tenant_id} plan={plan.slug} activated directly")
    return {
        "subscription_id": sub.id,
        "checkout_url":    None,
        "gateway":         "demo",
        "amount_sar":      price_sar,
        "plan_slug":       plan.slug,
        "demo_mode":       True,
        "success":         True,
        "launch_discount_active": is_launch,
        "current_price_sar": price_sar,
    }


_MOYASAR_FAIL_STATUSES = frozenset({"failed", "expired", "canceled", "voided", "refunded"})
_BILLING_ACTIVATABLE   = frozenset({"pending_payment"})


@app.post("/billing/webhook/moyasar/subscription")
async def billing_webhook_moyasar(request: Request, db: Session = Depends(get_db)):
    """
    Moyasar payment webhook handler for subscription payments.
    Activates the BillingSubscription and records a BillingPayment on success.
    Hardened: idempotency, signature verification, full status handling, race protection.
    """
    import json as _json
    body_bytes = await request.body()
    signature  = request.headers.get("x-moyasar-signature", "")

    try:
        event = _json.loads(body_bytes)
    except Exception:
        raise HTTPException(status_code=400, detail="Invalid JSON")

    payment_id   = event.get("id", "")
    status       = event.get("status", "")
    amount_h     = int(event.get("amount", 0))   # halalas
    amount_sar   = amount_h // 100
    payment_meta = event.get("metadata") or {}

    subscription_id = payment_meta.get("subscription_id")
    tenant_id_raw   = payment_meta.get("tenant_id")

    logger.info(
        f"[Billing Webhook] event id={payment_id} status={status} "
        f"sub={subscription_id} tenant={tenant_id_raw}"
    )

    if not subscription_id:
        logger.warning("[Billing Webhook] No subscription_id in metadata, ignoring")
        return {"received": True}

    sub = db.query(BillingSubscription).filter(
        BillingSubscription.id == int(subscription_id)
    ).first()

    if not sub:
        logger.warning(f"[Billing Webhook] Subscription {subscription_id} not found")
        return {"received": True}

    # ── Signature verification (when webhook_secret is configured) ─────────────
    cfg = _get_moyasar_settings(db, sub.tenant_id)
    webhook_secret = cfg.get("webhook_secret", "")
    if webhook_secret:
        sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), ".")))
        from payment_gateways.moyasar import MoyasarClient
        client = MoyasarClient(secret_key=cfg.get("secret_key", ""))
        if not client.verify_webhook_signature(body_bytes, signature, webhook_secret):
            logger.warning(
                f"[Billing Webhook] Invalid signature for sub={subscription_id} "
                f"tenant={sub.tenant_id}"
            )
            raise HTTPException(status_code=401, detail="Invalid webhook signature")

    if status == "paid":
        # ── Idempotency: guard duplicate delivery ──────────────────────────────
        if payment_id:
            existing = db.query(BillingPayment).filter(
                BillingPayment.transaction_reference == payment_id,
                BillingPayment.gateway == "moyasar",
            ).first()
            if existing:
                logger.info(
                    f"[Billing Webhook] Duplicate delivery payment_id={payment_id} "
                    f"— idempotent, ignoring"
                )
                return {"received": True, "idempotent": True}

        # ── Race / already-active guard ────────────────────────────────────────
        if sub.status == "active":
            logger.info(
                f"[Billing Webhook] Sub {subscription_id} already active "
                f"— duplicate webhook ignored"
            )
            return {"received": True, "already_active": True}

        if sub.status not in _BILLING_ACTIVATABLE:
            logger.warning(
                f"[Billing Webhook] Sub {subscription_id} in unexpected status "
                f"{sub.status!r} — skipping activation"
            )
            return {"received": True, "skipped": True}

        sub.status = "active"
        m = dict(sub.extra_metadata or {})
        m["moyasar_payment_id"] = payment_id
        m["paid_at"] = datetime.utcnow().isoformat()
        sub.extra_metadata = m

        billing_payment = BillingPayment(
            tenant_id=sub.tenant_id,
            subscription_id=sub.id,
            amount_sar=amount_sar or int(m.get("price_charged_sar", 0)),
            currency="SAR",
            gateway="moyasar",
            transaction_reference=payment_id,
            status="paid",
            paid_at=datetime.utcnow(),
            extra_metadata={"moyasar_event": event},
        )
        db.add(billing_payment)
        db.flush()
        logger.info(
            f"[Billing Webhook] Subscription {subscription_id} ACTIVATED "
            f"for tenant {sub.tenant_id} (payment {payment_id})"
        )

        # ── Notify merchant via email + WhatsApp ───────────────────────────────
        try:
            tenant_obj  = db.query(Tenant).filter(Tenant.id == sub.tenant_id).first()
            merchant    = db.query(User).filter(
                User.tenant_id == sub.tenant_id,
                User.role == "merchant",
                User.is_active == True,
            ).first()
            plan_obj    = sub.plan if hasattr(sub, "plan") and sub.plan else None
            plan_name   = plan_obj.name if plan_obj else payment_meta.get("plan_slug", "")
            store_name  = tenant_obj.name if tenant_obj else f"Tenant {sub.tenant_id}"
            ends_str    = sub.ends_at.strftime("%Y-%m-%d") if sub.ends_at else "—"

            if merchant:
                import asyncio
                asyncio.ensure_future(_send_email(
                    to      = merchant.email,
                    subject = f"تم تفعيل اشتراك {plan_name} — نحلة AI",
                    html    = _email_subscription(store_name, plan_name, ends_str),
                ))
                asyncio.ensure_future(_send_whatsapp(
                    to   = merchant.username,   # username field may hold phone
                    text = (
                        f"🐝 نحلة AI\n"
                        f"مرحباً {store_name}!\n"
                        f"تم تفعيل خطة {plan_name} بنجاح ✅\n"
                        f"ينتهي الاشتراك في: {ends_str}"
                    ),
                ))
        except Exception as notify_exc:
            logger.warning("[Billing Webhook] Notification error: %s", notify_exc)

    elif status in _MOYASAR_FAIL_STATUSES:
        # Never downgrade an already-active subscription
        if sub.status == "active":
            logger.warning(
                f"[Billing Webhook] Ignoring {status!r} webhook for active sub "
                f"{subscription_id} — not downgrading"
            )
            return {"received": True, "protected": True}
        sub.status = "payment_failed"
        logger.info(
            f"[Billing Webhook] Payment {status!r} for subscription {subscription_id}"
        )

    else:
        logger.info(
            f"[Billing Webhook] Unhandled status {status!r} for sub {subscription_id} "
            f"— no action taken"
        )

    db.commit()
    return {"received": True}


@app.get("/billing/payment-result")
async def billing_payment_result(
    request: Request,
    db: Session = Depends(get_db),
    sub_id: Optional[int] = None,
    status: Optional[str] = None,
):
    """
    Return subscription status for the payment-result page after Moyasar redirect.
    Frontend polls this endpoint to confirm activation.
    """
    if not sub_id:
        return {"activated": False, "status": "unknown"}

    tenant_id = _resolve_tenant_id(request)
    sub = db.query(BillingSubscription).filter(BillingSubscription.id == sub_id).first()
    if not sub:
        return {"activated": False, "status": "not_found"}

    # Ownership guard — prevent tenants from polling each other's subscriptions
    if sub.tenant_id != int(tenant_id):
        raise HTTPException(status_code=403, detail="Access denied")

    plan      = db.query(BillingPlan).filter(BillingPlan.id == sub.plan_id).first()
    plan_meta = plan.extra_metadata or {} if plan else {}

    return {
        "subscription_id": sub.id,
        "status":          sub.status,
        "activated":       sub.status == "active",
        "plan_slug":       plan.slug if plan else None,
        "plan_name_ar":    plan_meta.get("name_ar", plan.name if plan else ""),
        "amount_sar":      (sub.extra_metadata or {}).get("price_charged_sar"),
    }


# ── Storefront Snippet & Tracking ─────────────────────────────────────────────

@app.get("/snippet.js")
async def serve_snippet():
    """
    Serve the Nahla storefront tracking snippet.
    Merchants add one <script> tag pointing here; the script handles all event
    tracking (page view, product view, add to cart, cart abandon, checkout).
    """
    snippet_path = os.path.join(os.path.dirname(__file__), "snippet.js")
    try:
        with open(snippet_path, "r", encoding="utf-8") as f:
            js = f.read()
    except FileNotFoundError:
        js = "/* Nahla snippet not found */"
    from fastapi.responses import Response
    return Response(
        content=js,
        media_type="application/javascript",
        headers={"Cache-Control": "public, max-age=300"},
    )


class StorefrontEventIn(BaseModel):
    event_type:     str
    tenant_id:      Optional[str] = None
    store_id:       Optional[str] = None
    payload:        Optional[Dict[str, Any]] = None
    url:            Optional[str] = None
    referrer:       Optional[str] = None
    ts:             Optional[int] = None


@app.post("/track/event")
async def track_storefront_event(
    body: StorefrontEventIn,
    request: Request,
    db: Session = Depends(get_db),
):
    """
    Receive storefront events from the Nahla snippet.

    Supported event types:
      page_view, product_view, add_to_cart, cart_update,
      begin_checkout, order_created, cart_abandon

    cart_abandon events are forwarded to the autopilot engine
    as abandoned_cart signals for WhatsApp recovery flows.
    """
    # Resolve tenant — snippet sends tenant_id in body; fallback to header
    raw_tid = body.tenant_id or request.headers.get("X-Tenant-ID", "1")
    try:
        tenant_id = int(raw_tid)
    except (ValueError, TypeError):
        tenant_id = 1

    _get_or_create_tenant(db, tenant_id)

    payload = body.payload or {}
    payload["url"]      = body.url
    payload["referrer"] = body.referrer
    payload["store_id"] = body.store_id

    event = AutomationEvent(
        tenant_id=tenant_id,
        event_type=f"storefront_{body.event_type}",
        customer_id=None,
        payload=payload,
        processed=False,
    )
    db.add(event)
    db.commit()

    logger.info(
        f"[Snippet] tenant={tenant_id} event={body.event_type} "
        f"store={body.store_id} url={body.url}"
    )

    # For cart_abandon: also emit an autopilot-compatible abandoned_cart event
    # so the existing WhatsApp recovery automation can pick it up.
    if body.event_type == "cart_abandon":
        customer_phone = payload.get("customer_phone")
        if customer_phone:
            # Try to match to a known customer
            customer = db.query(Customer).filter(
                Customer.tenant_id == tenant_id,
                Customer.phone.like(f"%{customer_phone[-9:]}%"),
            ).first()
            cart_event = AutomationEvent(
                tenant_id=tenant_id,
                event_type="abandoned_cart",
                customer_id=customer.id if customer else None,
                payload={
                    "source":       "storefront_snippet",
                    "cart_total":   payload.get("cart_total"),
                    "items":        payload.get("items"),
                    "phone":        customer_phone,
                    "url":          body.url,
                },
                processed=False,
            )
            db.add(cart_event)
            db.commit()
            logger.info(
                f"[Snippet] cart_abandon → autopilot event "
                f"tenant={tenant_id} phone={customer_phone}"
            )

    return {"received": True, "event_type": body.event_type}


# ── Auth routes removed — now served by routers/auth.py ────────────────────────

# ── OAuth callbacks ─────────────────────────────────────────────────────────────
# These endpoints are PUBLIC (no JWT) — they receive redirects from external
# OAuth providers (Salla, etc.) and exchange the code for an access token.

@app.get("/api/salla/authorize")
async def salla_authorize(request: Request):
    """Return the Salla OAuth authorization URL for this tenant."""
    tenant_id = _resolve_tenant_id(request)
    if not _SALLA_CLIENT_ID:
        raise HTTPException(status_code=503, detail="SALLA_CLIENT_ID not configured")
    import urllib.parse
    params = urllib.parse.urlencode({
        "client_id":     _SALLA_CLIENT_ID,
        "redirect_uri":  _SALLA_REDIRECT_URI,
        "response_type": "code",
        "scope":         "offline_access",
        "state":         str(tenant_id),
    })
    return {"url": f"https://accounts.salla.sa/oauth2/auth?{params}"}


@app.get("/oauth/salla/callback")
async def salla_oauth_callback(
    request: Request,
    code:    str = None,
    state:   str = None,
    error:   str = None,
    db:      Session = Depends(get_db),
):
    """
    Salla OAuth 2.0 callback — full token exchange flow.

    Salla redirects here after the merchant authorises the app.
    We exchange the code for tokens, fetch store info, save to DB,
    then redirect to the dashboard success or error page.
    """
    client_ip = request.headers.get("X-Real-IP") or (
        request.client.host if request.client else "unknown"
    )
    logger.info(
        "Salla OAuth callback received | code=%s state=%s error=%s ip=%s",
        bool(code), state, error, client_ip,
    )

    # ── Decode state → tenant_id ───────────────────────────────────────────────
    try:
        tenant_id = int(state) if state else 1
    except (ValueError, TypeError):
        tenant_id = 1
    logger.info("Salla OAuth: resolved tenant_id=%s", tenant_id)

    # ── Provider-level error ───────────────────────────────────────────────────
    if error:
        logger.warning("Salla OAuth provider error: %s", error)
        return RedirectResponse(
            url=f"/integrations/salla/error?reason={error}",
            status_code=302,
        )

    if not code:
        logger.warning("Salla OAuth callback: missing code")
        return RedirectResponse(
            url="/integrations/salla/error?reason=missing_code",
            status_code=302,
        )

    # ── Exchange code for access_token ─────────────────────────────────────────
    if not _SALLA_CLIENT_ID or not _SALLA_CLIENT_SECRET:
        logger.error("Salla OAuth: SALLA_CLIENT_ID or SALLA_CLIENT_SECRET not set")
        return RedirectResponse(
            url="/integrations/salla/error?reason=app_not_configured",
            status_code=302,
        )

    try:
        async with httpx.AsyncClient(timeout=30.0) as client:
            logger.info("Salla OAuth: exchanging code for token …")
            token_resp = await client.post(
                "https://accounts.salla.sa/oauth2/token",
                data={
                    "grant_type":    "authorization_code",
                    "client_id":     _SALLA_CLIENT_ID,
                    "client_secret": _SALLA_CLIENT_SECRET,
                    "code":          code,
                    "redirect_uri":  _SALLA_REDIRECT_URI,
                },
                headers={"Accept": "application/json"},
            )
            logger.info(
                "Salla token endpoint response: status=%s", token_resp.status_code
            )
            if token_resp.status_code != 200:
                logger.error(
                    "Salla token exchange failed: %s %s",
                    token_resp.status_code, token_resp.text[:500],
                )
                return RedirectResponse(
                    url="/integrations/salla/error?reason=token_exchange_failed",
                    status_code=302,
                )

            token_data   = token_resp.json()
            access_token  = token_data.get("access_token", "")
            refresh_token = token_data.get("refresh_token", "")
            expires_in    = token_data.get("expires_in", 0)
            logger.info(
                "Salla OAuth: token exchange succeeded | expires_in=%s", expires_in
            )

            # ── Fetch store info ───────────────────────────────────────────────
            logger.info("Salla OAuth: fetching store info …")
            store_resp = await client.get(
                "https://api.salla.dev/admin/v2/store",
                headers={
                    "Authorization": f"Bearer {access_token}",
                    "Accept":        "application/json",
                },
            )
            logger.info(
                "Salla store info response: status=%s", store_resp.status_code
            )
            store_data = {}
            salla_store_id = ""
            store_name     = ""
            if store_resp.status_code == 200:
                store_json     = store_resp.json()
                store_data     = store_json.get("data", {})
                salla_store_id = str(store_data.get("id", ""))
                store_name     = store_data.get("name", "")
                logger.info(
                    "Salla store info: id=%s name=%s", salla_store_id, store_name
                )
            else:
                logger.warning(
                    "Salla store info fetch failed: %s", store_resp.status_code
                )

    except Exception as exc:
        logger.exception("Salla OAuth: unexpected error during token exchange: %s", exc)
        return RedirectResponse(
            url="/integrations/salla/error?reason=network_error",
            status_code=302,
        )

    # ── Save to integrations table ─────────────────────────────────────────────
    try:
        from models import Integration
        _get_or_create_tenant(db, tenant_id)
        integration = db.query(Integration).filter(
            Integration.tenant_id == tenant_id,
            Integration.provider  == "salla",
        ).first()

        new_config = {
            "api_key":       access_token,
            "store_id":      salla_store_id,
            "refresh_token": refresh_token,
            "store_name":    store_name,
            "expires_in":    expires_in,
            "connected_at":  datetime.utcnow().isoformat(),
        }

        if integration:
            integration.config  = new_config
            integration.enabled = True
            logger.info("Salla OAuth: updated existing integration for tenant %s", tenant_id)
        else:
            integration = Integration(
                tenant_id=tenant_id,
                provider="salla",
                config=new_config,
                enabled=True,
            )
            db.add(integration)
            logger.info("Salla OAuth: created new integration for tenant %s", tenant_id)

        db.commit()
        logger.info(
            "Salla OAuth: integration saved | tenant=%s store_id=%s store_name=%s",
            tenant_id, salla_store_id, store_name,
        )
    except Exception as exc:
        logger.exception("Salla OAuth: failed to save integration: %s", exc)
        return RedirectResponse(
            url="/integrations/salla/error?reason=db_save_failed",
            status_code=302,
        )

    # ── Success — redirect to dashboard ───────────────────────────────────────
    logger.info("Salla OAuth: flow complete — redirecting to success page")
    return RedirectResponse(
        url=f"/integrations/salla/success?store={salla_store_id}&name={store_name}",
        status_code=302,
    )


# ── Salla data API endpoints ───────────────────────────────────────────────────

@app.get("/api/salla/store")
async def get_salla_store(
    request:   Request,
    db:        Session = Depends(get_db),
    tenant_id: int     = Depends(get_jwt_tenant_id),
):
    """Return saved Salla store info for this tenant (JWT tenant_id only)."""
    _audit("salla_store_read", tenant_id=tenant_id)
    from models import Integration
    integration = db.query(Integration).filter(
        Integration.tenant_id == tenant_id,
        Integration.provider  == "salla",
        Integration.enabled   == True,
    ).first()
    if not integration:
        raise HTTPException(status_code=404, detail="Salla integration not configured")
    cfg = integration.config or {}
    return {
        "configured":  True,
        "store_id":    cfg.get("store_id", ""),
        "store_name":  cfg.get("store_name", ""),
        "connected_at": cfg.get("connected_at"),
        "api_key_hint": ("***" + cfg.get("api_key", "")[-4:]) if cfg.get("api_key") else "",
    }


@app.get("/api/salla/products")
async def get_salla_products(
    request:   Request,
    tenant_id: int = Depends(get_jwt_tenant_id),
):
    """Fetch live products from the tenant's Salla store (JWT tenant_id only)."""
    _audit("salla_products_fetched", tenant_id=tenant_id)
    sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), ".")))
    from store_integration.registry import get_adapter
    adapter = get_adapter(tenant_id)
    if not adapter:
        raise HTTPException(status_code=404, detail="Salla integration not configured")
    try:
        products = await adapter.get_products()
        return {"products": [p.dict() for p in products], "count": len(products)}
    except Exception as exc:
        logger.error("Salla products fetch error tenant=%s: %s", tenant_id, exc)
        raise HTTPException(status_code=502, detail=f"Salla API error: {exc}")


@app.post("/api/salla/test-coupon")
async def test_salla_coupon(
    request:   Request,
    tenant_id: int = Depends(get_jwt_tenant_id),
):
    """Validate a coupon code against the tenant's Salla store (JWT tenant_id only)."""
    body = await request.json()
    coupon_code = body.get("coupon_code", "").strip()
    if not coupon_code:
        raise HTTPException(status_code=400, detail="coupon_code is required")
    _audit("salla_coupon_test", tenant_id=tenant_id, coupon=coupon_code)
    sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), ".")))
    from store_integration.registry import get_adapter
    adapter = get_adapter(tenant_id)
    if not adapter:
        raise HTTPException(status_code=404, detail="Salla integration not configured")
    try:
        offer = await adapter.validate_coupon(coupon_code)
        if offer:
            return {"valid": True, "coupon": offer.dict()}
        return {"valid": False, "reason": "coupon not found or expired"}
    except Exception as exc:
        logger.error("Salla coupon error tenant=%s: %s", tenant_id, exc)
        raise HTTPException(status_code=502, detail=f"Salla API error: {exc}")


# ── Admin + tenant routes removed — now served by routers/admin.py ─────────────

# ══════════════════════════════════════════════════════════════════════════════
# Stripe Billing — subscription lifecycle managed by Stripe webhooks
# ══════════════════════════════════════════════════════════════════════════════

_STRIPE_SECRET_KEY      = os.environ.get("STRIPE_SECRET_KEY", "")
_STRIPE_WEBHOOK_SECRET  = os.environ.get("STRIPE_WEBHOOK_SECRET", "")
_STRIPE_PRICE_ID        = os.environ.get("STRIPE_PRICE_ID", "")   # Monthly plan price ID
_STRIPE_TRIAL_DAYS      = int(os.environ.get("STRIPE_TRIAL_DAYS", "14"))


def _get_stripe_client():
    """Return a configured StripeClient, or raise if not configured."""
    if not _STRIPE_SECRET_KEY:
        raise HTTPException(
            status_code=503,
            detail="Stripe is not configured. Set STRIPE_SECRET_KEY.",
        )
    from payment_gateways.stripe_client import StripeClient
    return StripeClient(
        secret_key=_STRIPE_SECRET_KEY,
        webhook_secret=_STRIPE_WEBHOOK_SECRET,
    )


class StripeSetupIntentRequest(BaseModel):
    email: str
    name:  str


@app.post("/billing/stripe/setup-intent")
async def stripe_create_setup_intent(
    body:    StripeSetupIntentRequest,
    request: Request,
    db:      Session = Depends(get_db),
):
    """
    Step 1 of Stripe flow: create (or reuse) a Stripe Customer, then create a
    SetupIntent so the frontend can collect a card via Stripe Elements without
    charging the merchant yet.

    Returns: { stripe_customer_id, setup_intent_id, client_secret }
    """
    tenant_id = _resolve_tenant_id(request)
    tenant    = _get_or_create_tenant(db, tenant_id)
    stripe    = _get_stripe_client()

    # Reuse existing Stripe customer or create a new one
    if not tenant.stripe_customer_id:
        customer = stripe.create_customer(
            email=body.email,
            name=body.name,
            metadata={"tenant_id": str(tenant_id)},
        )
        tenant.stripe_customer_id = customer["id"]
        db.commit()
        logger.info(f"[Stripe] New customer {customer['id']} for tenant {tenant_id}")

    si = stripe.create_setup_intent(
        customer_id=tenant.stripe_customer_id,
        metadata={"tenant_id": str(tenant_id)},
    )
    return {
        "stripe_customer_id": tenant.stripe_customer_id,
        "setup_intent_id":    si["setup_intent_id"],
        "client_secret":      si["client_secret"],
    }


class StripeSubscribeRequest(BaseModel):
    payment_method_id: str          # pm_xxx returned by Stripe Elements after SetupIntent
    price_id:          Optional[str] = None   # override; defaults to STRIPE_PRICE_ID env


@app.post("/billing/stripe/subscribe")
async def stripe_create_subscription(
    body:    StripeSubscribeRequest,
    request: Request,
    db:      Session = Depends(get_db),
):
    """
    Step 2 of Stripe flow: create a Stripe Subscription with a 14-day trial.
    The merchant is not charged until the trial ends.

    Requires: the tenant must already have stripe_customer_id (call setup-intent first).
    Returns: { subscription_id, status, trial_ends_at }
    """
    tenant_id = _resolve_tenant_id(request)
    tenant    = _get_or_create_tenant(db, tenant_id)
    stripe    = _get_stripe_client()

    if not tenant.stripe_customer_id:
        raise HTTPException(
            status_code=400,
            detail="No Stripe customer found. Call /billing/stripe/setup-intent first.",
        )

    price_id = body.price_id or _STRIPE_PRICE_ID
    if not price_id:
        raise HTTPException(status_code=503, detail="STRIPE_PRICE_ID is not configured.")

    sub = stripe.create_subscription(
        customer_id=tenant.stripe_customer_id,
        price_id=price_id,
        trial_period_days=_STRIPE_TRIAL_DAYS,
        payment_method_id=body.payment_method_id,
        metadata={"tenant_id": str(tenant_id)},
    )

    now = datetime.utcnow()
    trial_end = now + timedelta(days=_STRIPE_TRIAL_DAYS)

    tenant.stripe_subscription_id = sub["id"]
    tenant.stripe_price_id         = price_id
    tenant.subscription_status     = sub.get("status", "trialing")
    tenant.billing_provider        = "stripe"
    tenant.trial_started_at        = now
    tenant.trial_ends_at           = trial_end
    db.commit()

    logger.info(
        f"[Stripe] Tenant {tenant_id} subscribed: sub={sub['id']} "
        f"status={sub.get('status')} trial_ends={trial_end.date()}"
    )
    return {
        "success":        True,
        "subscription_id": sub["id"],
        "status":         sub.get("status"),
        "trial_ends_at":  trial_end.isoformat(),
    }


@app.post("/webhook/stripe")
async def stripe_webhook(request: Request, db: Session = Depends(get_db)):
    """
    Stripe webhook endpoint — source of truth for subscription state changes.

    Handled events:
      invoice.paid                → activate / keep account active
      invoice.payment_failed      → mark past_due, notify merchant
      customer.subscription.deleted → cancel, disable account
    """
    payload    = await request.body()
    sig_header = request.headers.get("stripe-signature", "")

    stripe_client = _get_stripe_client()
    try:
        event = stripe_client.construct_webhook_event(payload, sig_header)
    except Exception as exc:
        logger.warning(f"[Stripe] Webhook signature verification failed: {exc}")
        raise HTTPException(status_code=400, detail="Invalid Stripe webhook signature")

    event_type = event["type"]
    data_obj   = event["data"]["object"]

    # Resolve tenant from Stripe customer ID or subscription metadata
    customer_id    = data_obj.get("customer")
    subscription_id = data_obj.get("id") if event_type.startswith("customer.subscription") \
                      else data_obj.get("subscription")

    tenant = None
    if customer_id:
        tenant = db.query(Tenant).filter(Tenant.stripe_customer_id == customer_id).first()
    if tenant is None and subscription_id:
        tenant = db.query(Tenant).filter(Tenant.stripe_subscription_id == subscription_id).first()

    if tenant is None:
        # Unknown tenant — could be a test event, acknowledge and ignore
        logger.info(f"[Stripe] Webhook {event_type}: no tenant found for customer={customer_id}")
        return {"received": True}

    # ── Handle events ──────────────────────────────────────────────────────────

    if event_type == "invoice.paid":
        period_end_ts = data_obj.get("lines", {}).get("data", [{}])[0] \
                            .get("period", {}).get("end")
        if period_end_ts:
            tenant.current_period_end = datetime.utcfromtimestamp(period_end_ts)
        tenant.subscription_status = "active"
        tenant.is_active           = True
        tenant.billing_status      = "paid"
        db.commit()
        logger.info(f"[Stripe] invoice.paid → tenant {tenant.id} activated")

    elif event_type == "invoice.payment_failed":
        tenant.subscription_status = "past_due"
        tenant.billing_status      = "failed"
        db.commit()
        logger.warning(f"[Stripe] invoice.payment_failed → tenant {tenant.id} marked past_due")
        # TODO: send notification email / WhatsApp to merchant

    elif event_type == "customer.subscription.deleted":
        tenant.subscription_status = "canceled"
        tenant.is_active           = False
        tenant.billing_status      = "failed"
        db.commit()
        logger.warning(f"[Stripe] subscription.deleted → tenant {tenant.id} disabled")

    elif event_type == "customer.subscription.updated":
        new_status = data_obj.get("status")
        if new_status:
            tenant.subscription_status = new_status
        period_end_ts = data_obj.get("current_period_end")
        if period_end_ts:
            tenant.current_period_end = datetime.utcfromtimestamp(period_end_ts)
        db.commit()
        logger.info(f"[Stripe] subscription.updated → tenant {tenant.id} status={new_status}")

    return {"received": True}


# ══════════════════════════════════════════════════════════════════════════════
# HyperPay Billing — Saudi local payment methods (MADA, Apple Pay, STC Pay)
# ══════════════════════════════════════════════════════════════════════════════

_HYPERPAY_ACCESS_TOKEN  = os.environ.get("HYPERPAY_ACCESS_TOKEN", "")
_HYPERPAY_ENTITY_ID     = os.environ.get("HYPERPAY_ENTITY_ID", "")
_HYPERPAY_WEBHOOK_SECRET = os.environ.get("HYPERPAY_WEBHOOK_SECRET", "")
_HYPERPAY_LIVE_MODE     = os.environ.get("HYPERPAY_LIVE_MODE", "false").lower() == "true"


def _get_hyperpay_client():
    """Return a configured HyperPayClient, or raise if not configured."""
    if not _HYPERPAY_ACCESS_TOKEN or not _HYPERPAY_ENTITY_ID:
        raise HTTPException(
            status_code=503,
            detail="HyperPay is not configured. Set HYPERPAY_ACCESS_TOKEN and HYPERPAY_ENTITY_ID.",
        )
    from payment_gateways.hyperpay_client import HyperPayClient
    return HyperPayClient(
        access_token=_HYPERPAY_ACCESS_TOKEN,
        entity_id=_HYPERPAY_ENTITY_ID,
        webhook_secret=_HYPERPAY_WEBHOOK_SECRET,
        live_mode=_HYPERPAY_LIVE_MODE,
    )


class HyperPayPaymentLinkRequest(BaseModel):
    amount_sar:   float
    brand:        str = "MADA"   # MADA | APPLEPAY | STC_PAY | VISA | MASTER
    description:  str = "Nahla SaaS Monthly Subscription"


@app.post("/billing/hyperpay/payment-link")
async def hyperpay_create_payment_link(
    body:    HyperPayPaymentLinkRequest,
    request: Request,
    db:      Session = Depends(get_db),
):
    """
    Create a HyperPay checkout session for Saudi local payment methods.

    The frontend receives `checkout_id` and the `payment_widget_url`, then
    either embeds the HyperPay widget or redirects the merchant to the
    hosted payment page to complete the payment.

    After payment: HyperPay sends a webhook to POST /webhook/hyperpay.
    Returns: { checkout_id, payment_widget_url }
    """
    tenant_id = _resolve_tenant_id(request)
    tenant    = _get_or_create_tenant(db, tenant_id)
    hp        = _get_hyperpay_client()

    result = await hp.create_checkout(
        amount=body.amount_sar,
        currency="SAR",
        brand=body.brand,
        merchant_transaction_id=f"nahla-{tenant_id}-{int(datetime.utcnow().timestamp())}",
        description=body.description,
        metadata={"tenant_id": str(tenant_id)},
    )

    checkout_id = result.get("id", "")
    result_code = result.get("result", {}).get("code", "")

    # Store the pending payment reference on the tenant
    tenant.hyperpay_payment_id = checkout_id
    tenant.billing_provider    = "hyperpay"
    tenant.billing_status      = "pending"
    db.commit()

    logger.info(
        f"[HyperPay] Checkout created for tenant {tenant_id}: "
        f"id={checkout_id} brand={body.brand} amount={body.amount_sar} SAR"
    )
    return {
        "checkout_id":        checkout_id,
        "result_code":        result_code,
        "payment_widget_url": hp.build_payment_page_url(checkout_id, body.brand),
    }


@app.post("/webhook/hyperpay")
async def hyperpay_webhook(request: Request, db: Session = Depends(get_db)):
    """
    HyperPay webhook endpoint — confirms payment success for local Saudi methods.

    On success:
      - subscription_status → 'active'
      - current_period_end  → now + 30 days
      - billing_status      → 'paid'

    On failure:
      - billing_status → 'failed'
    """
    payload = await request.body()
    iv        = request.headers.get("X-Initialization-Vector", "")
    signature = request.headers.get("X-Authentication-Tag", "")

    hp = _get_hyperpay_client()

    # Verify signature only if the webhook secret is configured
    if _HYPERPAY_WEBHOOK_SECRET:
        if not hp.verify_webhook_signature(payload, iv, signature):
            logger.warning("[HyperPay] Webhook signature verification failed")
            raise HTTPException(status_code=400, detail="Invalid HyperPay webhook signature")

    try:
        import json as _json
        data = _json.loads(payload)
    except Exception:
        raise HTTPException(status_code=400, detail="Invalid JSON payload")

    checkout_id = data.get("id", "")
    result_code = data.get("result", {}).get("code", "")
    payment_id  = data.get("id", checkout_id)

    # Resolve tenant from stored checkout ID
    tenant = db.query(Tenant).filter(Tenant.hyperpay_payment_id == checkout_id).first()
    if tenant is None:
        logger.info(f"[HyperPay] Webhook: no tenant found for checkout_id={checkout_id}")
        return {"received": True}

    if hp.is_payment_successful(data):
        now = datetime.utcnow()
        tenant.subscription_status = "active"
        tenant.billing_status      = "paid"
        tenant.is_active           = True
        tenant.current_period_end  = now + timedelta(days=30)
        tenant.hyperpay_payment_id = payment_id
        db.commit()
        logger.info(
            f"[HyperPay] Payment SUCCESS for tenant {tenant.id}: "
            f"code={result_code} period_end={tenant.current_period_end.date()}"
        )
    else:
        tenant.billing_status = "failed"
        db.commit()
        logger.warning(
            f"[HyperPay] Payment FAILED for tenant {tenant.id}: "
            f"code={result_code} desc={data.get('result', {}).get('description', '')}"
        )

    return {"received": True}


if __name__ == "__main__":
    import uvicorn
    port = int(os.environ.get("PORT", 8000))
    logger.info(f"Starting Nahla SaaS Backend API server on port {port}...")
    uvicorn.run("main:app", host="0.0.0.0", port=port, reload=True)

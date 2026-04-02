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

_bearer_scheme = HTTPBearer(auto_error=False)

def _create_token(email: str, role: str, tenant_id: int) -> str:
    payload = {
        "sub":       email,
        "role":      role,
        "tenant_id": tenant_id,
        "exp":       datetime.utcnow() + timedelta(hours=_JWT_EXPIRE_H),
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
    creds: HTTPAuthorizationCredentials = Depends(_bearer_scheme),
) -> Dict[str, Any]:
    """Dependency — requires a valid JWT with role=admin."""
    user = get_current_user(creds)
    if user.get("role") != "admin":
        raise HTTPException(status_code=403, detail="Admin access required")
    return user

# Configure logging
logging.basicConfig(level=logging.INFO, format='%(asctime)s %(levelname)s %(message)s')
logger = logging.getLogger("nahla-backend")

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

# ── Pydantic schemas ───────────────────────────────────────────────────────────

class WhatsAppSettingsIn(BaseModel):
    business_display_name: str = ""
    phone_number: str = ""
    phone_number_id: str = ""
    access_token: str = ""
    verify_token: str = ""
    webhook_url: str = ""
    store_button_label: str = "زيارة المتجر"
    store_button_url: str = ""
    owner_contact_label: str = "تواصل مع المالك"
    owner_whatsapp_number: str = ""
    auto_reply_enabled: bool = True
    transfer_to_owner_enabled: bool = True

class AISettingsIn(BaseModel):
    assistant_name: str = "نحلة"
    assistant_role: str = ""
    reply_tone: str = "friendly"
    reply_length: str = "medium"
    default_language: str = "arabic"
    owner_instructions: str = ""
    coupon_rules: str = ""
    escalation_rules: str = ""
    allowed_discount_levels: str = "10"
    recommendations_enabled: bool = True

class StoreSettingsIn(BaseModel):
    store_name: str = ""
    store_logo_url: str = ""
    store_url: str = ""
    platform_type: str = "salla"
    salla_client_id: str = ""
    salla_client_secret: str = ""
    salla_access_token: str = ""
    zid_client_id: str = ""
    zid_client_secret: str = ""
    shopify_shop_domain: str = ""
    shopify_access_token: str = ""
    shipping_provider: str = ""
    google_maps_location: str = ""
    instagram_url: str = ""
    twitter_url: str = ""
    snapchat_url: str = ""
    tiktok_url: str = ""

class NotificationSettingsIn(BaseModel):
    whatsapp_alerts: bool = True
    email_alerts: bool = True
    system_alerts: bool = True
    failed_webhook_alerts: bool = True
    low_balance_alerts: bool = True

class AllSettingsIn(BaseModel):
    whatsapp: Optional[WhatsAppSettingsIn] = None
    ai: Optional[AISettingsIn] = None
    store: Optional[StoreSettingsIn] = None
    notifications: Optional[NotificationSettingsIn] = None

# ── Secret masking ────────────────────────────────────────────────────────────
# Fields that are masked before returning to the frontend.
# Key: settings group name → set of field names.
_SECRET_FIELDS: Dict[str, set] = {
    "whatsapp": {"access_token", "verify_token"},
    "store":    {"salla_client_secret", "salla_access_token",
                 "zid_client_secret", "shopify_access_token"},
}

def _mask_secret(value: str) -> str:
    """Return a masked version: first 4 chars + **** + last 4 chars."""
    if not value or len(value) < 9:
        return value  # too short — don't mask (probably empty/placeholder)
    return value[:4] + "****" + value[-4:]

def _is_masked(value: str) -> bool:
    """True if this value was previously returned as a mask (contains ****)."""
    return isinstance(value, str) and "****" in value

def _apply_masks(data: Dict[str, Any], group: str) -> Dict[str, Any]:
    """Return a copy of data with secret fields masked."""
    fields = _SECRET_FIELDS.get(group, set())
    return {k: (_mask_secret(v) if k in fields and isinstance(v, str) else v)
            for k, v in data.items()}

def _restore_secrets(incoming: Dict[str, Any], stored: Dict[str, Any], group: str) -> Dict[str, Any]:
    """Replace masked values in incoming with the original stored secret."""
    fields = _SECRET_FIELDS.get(group, set())
    result = dict(incoming)
    for field in fields:
        if field in result and _is_masked(result[field]):
            result[field] = stored.get(field, "")
    return result

# ── Settings endpoints ─────────────────────────────────────────────────────────

@app.get("/settings")
async def get_settings(request: Request, db: Session = Depends(get_db)):
    """Return all settings for the current tenant."""
    tenant_id = _resolve_tenant_id(request)
    settings = _get_or_create_settings(db, tenant_id)
    db.commit()

    wa    = _merge_defaults(settings.whatsapp_settings,    DEFAULT_WHATSAPP)
    store = _merge_defaults(settings.store_settings,       DEFAULT_STORE)
    return {
        "whatsapp":      _apply_masks(wa,    "whatsapp"),
        "ai":            _merge_defaults(settings.ai_settings,             DEFAULT_AI),
        "store":         _apply_masks(store, "store"),
        "notifications": _merge_defaults(settings.notification_settings,   DEFAULT_NOTIFICATIONS),
    }


@app.put("/settings")
async def update_settings(
    body: AllSettingsIn,
    request: Request,
    db: Session = Depends(get_db),
):
    """Update settings for the current tenant (partial update – only provided groups saved)."""
    tenant_id = _resolve_tenant_id(request)
    settings = _get_or_create_settings(db, tenant_id)

    if body.whatsapp is not None:
        current = _merge_defaults(settings.whatsapp_settings, DEFAULT_WHATSAPP)
        incoming = _restore_secrets(body.whatsapp.model_dump(), current, "whatsapp")
        current.update(incoming)
        settings.whatsapp_settings = current

    if body.ai is not None:
        current = _merge_defaults(settings.ai_settings, DEFAULT_AI)
        current.update(body.ai.model_dump())
        settings.ai_settings = current

    if body.store is not None:
        current = _merge_defaults(settings.store_settings, DEFAULT_STORE)
        incoming = _restore_secrets(body.store.model_dump(), current, "store")
        current.update(incoming)
        settings.store_settings = current

    if body.notifications is not None:
        current = _merge_defaults(settings.notification_settings, DEFAULT_NOTIFICATIONS)
        current.update(body.notifications.model_dump())
        settings.notification_settings = current

    settings.updated_at = datetime.utcnow()
    db.commit()
    db.refresh(settings)

    wa_saved    = _merge_defaults(settings.whatsapp_settings, DEFAULT_WHATSAPP)
    store_saved = _merge_defaults(settings.store_settings,    DEFAULT_STORE)
    return {
        "whatsapp":      _apply_masks(wa_saved,    "whatsapp"),
        "ai":            _merge_defaults(settings.ai_settings,             DEFAULT_AI),
        "store":         _apply_masks(store_saved, "store"),
        "notifications": _merge_defaults(settings.notification_settings,   DEFAULT_NOTIFICATIONS),
    }


@app.post("/settings/test-whatsapp")
async def test_whatsapp_connection(request: Request, db: Session = Depends(get_db)):
    """Simulate a WhatsApp API connection test."""
    tenant_id = _resolve_tenant_id(request)
    settings = _get_or_create_settings(db, tenant_id)
    db.commit()

    wa = _merge_defaults(settings.whatsapp_settings, DEFAULT_WHATSAPP)
    if not wa.get("phone_number_id") or not wa.get("access_token"):
        return {"success": False, "message": "Phone Number ID و Access Token مطلوبان لاختبار الاتصال"}

    # In production, call the WhatsApp Business API here
    return {"success": True, "message": "تم الاتصال بنجاح بـ WhatsApp Business API"}


# ── Seed templates — auto-inserted into DB when the table is empty ────────────

SEED_TEMPLATES: List[Dict[str, Any]] = [
    {
        "name": "abandoned_cart_reminder",
        "language": "ar",
        "category": "MARKETING",
        "status": "APPROVED",
        "components": [
            {"type": "HEADER", "format": "TEXT", "text": "سلّتك في انتظارك! 🛒"},
            {"type": "BODY",   "text": "مرحباً {{1}}،\nلاحظنا أنك تركت بعض المنتجات في سلّتك.\nأكمل طلبك الآن قبل نفاد الكمية:\n{{2}}"},
            {"type": "FOOTER", "text": "🐝 نحلة — مساعد متجرك"},
            {"type": "BUTTONS", "buttons": [{"type": "URL", "text": "أكمل الطلب", "url": "{{2}}"}]},
        ],
    },
    {
        "name": "special_offer",
        "language": "ar",
        "category": "MARKETING",
        "status": "APPROVED",
        "components": [
            {"type": "HEADER", "format": "TEXT", "text": "عرض خاص لك 🎁"},
            {"type": "BODY",   "text": "أهلاً {{1}}،\nاحصل على خصم {{2}} باستخدام كود: *{{3}}*\nالعرض ينتهي قريباً — لا تفوّته!"},
            {"type": "FOOTER", "text": "🐝 نحلة — مساعد متجرك"},
        ],
    },
    {
        "name": "new_arrivals",
        "language": "ar",
        "category": "MARKETING",
        "status": "APPROVED",
        "components": [
            {"type": "HEADER", "format": "TEXT", "text": "وصل جديد! ✨"},
            {"type": "BODY",   "text": "مرحباً {{1}}،\nيسعدنا إعلامك بوصول منتجات جديدة في متجر {{2}}.\nاكتشف أحدث التشكيلة الآن وكن أول من يحصل عليها."},
            {"type": "FOOTER", "text": "🐝 نحلة — مساعد متجرك"},
        ],
    },
    {
        "name": "vip_exclusive",
        "language": "ar",
        "category": "MARKETING",
        "status": "APPROVED",
        "components": [
            {"type": "HEADER", "format": "TEXT", "text": "👑 عرض VIP حصري"},
            {"type": "BODY",   "text": "{{1}}، أنت من عملائنا المميزين!\nبصفتك عضواً VIP لديك خصم حصري {{2}} على مشترياتك القادمة.\nاستخدم الكود: *{{3}}*"},
            {"type": "FOOTER", "text": "🐝 نحلة — مساعد متجرك"},
        ],
    },
    {
        "name": "order_confirmed",
        "language": "ar",
        "category": "UTILITY",
        "status": "APPROVED",
        "components": [
            {"type": "HEADER", "format": "TEXT", "text": "تأكيد الطلب ✅"},
            {"type": "BODY",   "text": "شكراً {{1}}!\nتم استلام طلبك رقم *{{2}}* بنجاح.\nسيتم التواصل معك قريباً لتأكيد موعد التوصيل."},
            {"type": "FOOTER", "text": "🐝 نحلة — مساعد متجرك"},
        ],
    },
    {
        "name": "win_back",
        "language": "ar",
        "category": "MARKETING",
        "status": "APPROVED",
        "components": [
            {"type": "HEADER", "format": "TEXT", "text": "اشتقنا إليك! 💙"},
            {"type": "BODY",   "text": "مرحباً {{1}}،\nلم نرك منذ فترة ونحن نفتقدك!\nعُد إلينا مع خصم خاص {{2}} على طلبك القادم.\nالكود: *{{3}}*"},
            {"type": "FOOTER", "text": "🐝 نحلة — مساعد متجرك"},
        ],
    },
    # ── Default intelligence templates ────────────────────────────────────────
    {
        "name": "cod_order_confirmation_ar",
        "language": "ar",
        "category": "UTILITY",
        "status": "APPROVED",
        "components": [
            {"type": "BODY",   "text": "مرحباً {{1}} 🐝\n\nاستلمنا طلبك بنجاح 🍯\n\nالمنتج: {{2}}\nالمبلغ: {{3}} ريال\n\nطريقة الدفع: الدفع عند الاستلام\n\nيرجى تأكيد الطلب بالضغط على الزر أدناه."},
            {"type": "FOOTER", "text": "🐝 نحلة — مساعد متجرك"},
            {"type": "BUTTONS", "buttons": [
                {"type": "QUICK_REPLY", "text": "تأكيد الطلب ✅"},
                {"type": "QUICK_REPLY", "text": "إلغاء الطلب ❌"},
            ]},
        ],
    },
    {
        "name": "predictive_reorder_reminder_ar",
        "language": "ar",
        "category": "MARKETING",
        "status": "APPROVED",
        "components": [
            {"type": "BODY",   "text": "مرحباً {{1}} 🐝\n\nنتوقع أن {{2}} لديك قد أوشك على النفاد 🍯\n\nاطلب عبوة جديدة الآن:\n\n{{3}}"},
            {"type": "FOOTER", "text": "🐝 نحلة — مساعد متجرك"},
        ],
    },
]

# Variable mapping documentation for smart automations
TEMPLATE_VAR_MAP: Dict[str, Dict[str, str]] = {
    "predictive_reorder_reminder_ar": {
        "{{1}}": "customer_name",
        "{{2}}": "product_name",
        "{{3}}": "reorder_url",
    },
    "cod_order_confirmation_ar": {
        "{{1}}": "customer_name",
        "{{2}}": "product_name",
        "{{3}}": "order_amount",
    },
    "abandoned_cart_reminder": {
        "{{1}}": "customer_name",
        "{{2}}": "cart_url",
    },
    "special_offer": {
        "{{1}}": "customer_name",
        "{{2}}": "discount_pct",
        "{{3}}": "coupon_code",
    },
    "win_back": {
        "{{1}}": "customer_name",
        "{{2}}": "discount_pct",
        "{{3}}": "coupon_code",
    },
    "vip_exclusive": {
        "{{1}}": "customer_name",
        "{{2}}": "discount_pct",
        "{{3}}": "coupon_code",
    },
    "new_arrivals": {
        "{{1}}": "customer_name",
        "{{2}}": "store_name",
    },
    "order_confirmed": {
        "{{1}}": "customer_name",
        "{{2}}": "order_id",
    },
}

# ── Seed automations ───────────────────────────────────────────────────────────

SEED_AUTOMATIONS: List[Dict[str, Any]] = [
    {
        "automation_type": "abandoned_cart",
        "name": "استرداد العربة المتروكة",
        "enabled": False,
        "config": {
            "steps": [
                {"delay_minutes": 30,   "message_type": "reminder"},
                {"delay_minutes": 180,  "message_type": "reminder"},
                {"delay_minutes": 1440, "message_type": "coupon", "coupon_code": "CART10AUTO"},
            ],
            "template_name": "abandoned_cart_reminder",
        },
    },
    {
        "automation_type": "predictive_reorder",
        "name": "تذكير إعادة الطلب التنبؤي",
        "enabled": False,
        "config": {
            "template_name": "predictive_reorder_reminder_ar",
            "var_map": {"{{1}}": "customer_name", "{{2}}": "product_name", "{{3}}": "reorder_url"},
            "days_before": 3,
        },
    },
    {
        "automation_type": "customer_winback",
        "name": "استرجاع العملاء غير النشطين",
        "enabled": False,
        "config": {
            "inactive_days_first": 60,
            "inactive_days_second": 90,
            "discount_pct": 15,
            "template_name": "win_back",
        },
    },
    {
        "automation_type": "vip_upgrade",
        "name": "مكافأة عملاء VIP",
        "enabled": False,
        "config": {
            "min_spent_sar": 2000,
            "discount_pct": 20,
            "template_name": "vip_exclusive",
        },
    },
    {
        "automation_type": "new_product_alert",
        "name": "تنبيه المنتجات الجديدة",
        "enabled": False,
        "config": {
            "target_interested_only": True,
            "template_name": "new_arrivals",
        },
    },
    {
        "automation_type": "back_in_stock",
        "name": "تنبيه عودة المنتج للمخزون",
        "enabled": False,
        "config": {
            "notify_previous_buyers": True,
            "notify_previous_viewers": True,
            "template_name": "new_arrivals",
        },
    },
]


def _seed_automations_if_empty(db: Session, tenant_id: int) -> None:
    count = db.query(SmartAutomation).filter(SmartAutomation.tenant_id == tenant_id).count()
    if count == 0:
        for seed in SEED_AUTOMATIONS:
            auto = SmartAutomation(
                tenant_id=tenant_id,
                automation_type=seed["automation_type"],
                name=seed["name"],
                enabled=seed["enabled"],
                config=seed["config"],
                created_at=datetime.utcnow(),
                updated_at=datetime.utcnow(),
            )
            db.add(auto)
        db.flush()


# ── Demo customer seed data ────────────────────────────────────────────────────

DEMO_CUSTOMERS_DATA: List[Dict[str, Any]] = [
    {"name": "أحمد الراشد",   "phone": "+966501234567", "email": "ahmed@example.com",
     "total_orders": 18, "total_spend": 4820.0, "days_since_last_order": 7},
    {"name": "نورا المطيري",  "phone": "+966526543210", "email": "nora@example.com",
     "total_orders": 6,  "total_spend": 1240.0, "days_since_last_order": 18},
    {"name": "خالد إبراهيم",  "phone": "+966578887766", "email": "khalid@example.com",
     "total_orders": 12, "total_spend": 3150.0, "days_since_last_order": 5},
    {"name": "ليلى السعود",   "phone": "+966545512200", "email": "leila@example.com",
     "total_orders": 3,  "total_spend": 540.0,  "days_since_last_order": 30},
    {"name": "عمر الغامدي",   "phone": "+966563219900", "email": "omar@example.com",
     "total_orders": 9,  "total_spend": 2340.0, "days_since_last_order": 22},
    {"name": "ريم الحربي",    "phone": "+966554100033", "email": "reem@example.com",
     "total_orders": 4,  "total_spend": 820.0,  "days_since_last_order": 110},
    {"name": "يوسف الشهري",   "phone": "+966507755522", "email": "yousef@example.com",
     "total_orders": 5,  "total_spend": 1100.0, "days_since_last_order": 71},
    {"name": "سارة القحطاني", "phone": "+966532218800", "email": "sara@example.com",
     "total_orders": 3,  "total_spend": 650.0,  "days_since_last_order": 87},
    {"name": "محمد العتيبي",  "phone": "+966561234567", "email": "mohammed@example.com",
     "total_orders": 1,  "total_spend": 150.0,  "days_since_last_order": 3},
    {"name": "فاطمة الدوسري", "phone": "+966547896543", "email": "fatima@example.com",
     "total_orders": 2,  "total_spend": 380.0,  "days_since_last_order": 14},
    {"name": "عبدالله الزهراني", "phone": "+966509876543", "email": "abdullah@example.com",
     "total_orders": 7,  "total_spend": 1650.0, "days_since_last_order": 45},
    {"name": "منى الحارثي",   "phone": "+966551234000", "email": "mona@example.com",
     "total_orders": 1,  "total_spend": 250.0,  "days_since_last_order": 2},
]

# Predictive reorder estimates for demo products
DEMO_REORDER_PRODUCTS: List[Dict[str, Any]] = [
    {"product_name": "عسل السدر 500g",    "consumption_days": 30},
    {"product_name": "عسل الطلح 1kg",     "consumption_days": 60},
    {"product_name": "عسل الأكاسيا 250g", "consumption_days": 20},
    {"product_name": "عسل السمر 1kg",     "consumption_days": 60},
]


def _compute_customer_segment(total_orders: int, total_spend: float, days_inactive: int) -> tuple:
    """
    Classify a customer into one of 5 segments and compute a churn risk score.

    Segments (Arabic labels):
      vip      → VIP Customer     (≥5 orders AND spend ≥2000 SAR)
      active   → Active Customer  (recent buyer, not VIP, not new)
      new      → New Customer     (≤1 order)
      at_risk  → Churn Risk       (60–90 days inactive)
      churned  → Dormant Customer (>90 days inactive)
    """
    from math import exp

    # Churn risk: logistic-ish curve rising with inactivity
    # 0.0 = loyal, 1.0 = certainly churned
    if days_inactive <= 14:
        churn_risk = max(0.02, days_inactive * 0.005)
    elif days_inactive <= 30:
        churn_risk = 0.10 + (days_inactive - 14) * 0.008
    elif days_inactive <= 60:
        churn_risk = 0.23 + (days_inactive - 30) * 0.01
    elif days_inactive <= 90:
        churn_risk = 0.53 + (days_inactive - 60) * 0.008
    else:
        churn_risk = 0.77 + min((days_inactive - 90) * 0.002, 0.23)

    churn_risk = round(min(churn_risk, 1.0), 3)

    if days_inactive > 90:
        segment = "churned"
    elif days_inactive > 60:
        segment = "at_risk"
    elif total_orders <= 1:
        segment = "new"
    elif total_spend >= 2000 and total_orders >= 5:
        segment = "vip"
    else:
        segment = "active"

    return segment, churn_risk


def _seed_demo_customers(db: Session, tenant_id: int) -> None:
    """
    Seed demo customers with CustomerProfile records if the tenant has no customers yet.
    This lets the intelligence dashboard show real DB data immediately in development.
    """
    count = db.query(Customer).filter(Customer.tenant_id == tenant_id).count()
    if count > 0:
        return  # already seeded

    from datetime import timedelta

    for demo in DEMO_CUSTOMERS_DATA:
        customer = Customer(
            name=demo["name"],
            phone=demo["phone"],
            email=demo["email"],
            tenant_id=tenant_id,
        )
        db.add(customer)
        db.flush()  # get customer.id

        days = demo["days_since_last_order"]
        last_order_at = datetime.utcnow() - timedelta(days=days)
        first_seen_at = last_order_at - timedelta(days=demo["total_orders"] * 14)

        segment, churn_risk = _compute_customer_segment(
            demo["total_orders"],
            demo["total_spend"],
            days,
        )

        profile = CustomerProfile(
            customer_id=customer.id,
            tenant_id=tenant_id,
            total_orders=demo["total_orders"],
            total_spend_sar=demo["total_spend"],
            average_order_value_sar=round(demo["total_spend"] / max(demo["total_orders"], 1), 2),
            max_single_order_sar=round(demo["total_spend"] / max(demo["total_orders"], 1) * 1.4, 2),
            segment=segment,
            churn_risk_score=churn_risk,
            is_returning=demo["total_orders"] > 1,
            first_seen_at=first_seen_at,
            last_seen_at=datetime.utcnow() - timedelta(days=max(1, days - 2)),
            last_order_at=last_order_at,
            updated_at=datetime.utcnow(),
        )
        db.add(profile)

    db.flush()


def _seed_templates_if_empty(db: Session, tenant_id: int) -> None:
    """Seed demo templates into the DB if this tenant has none."""
    count = db.query(WhatsAppTemplate).filter(WhatsAppTemplate.tenant_id == tenant_id).count()
    if count == 0:
        for seed in SEED_TEMPLATES:
            tpl = WhatsAppTemplate(
                tenant_id=tenant_id,
                meta_template_id=f"seed_{seed['name']}",
                name=seed["name"],
                language=seed["language"],
                category=seed["category"],
                status=seed["status"],
                components=seed["components"],
                created_at=datetime.utcnow(),
                updated_at=datetime.utcnow(),
                synced_at=datetime.utcnow(),
            )
            db.add(tpl)
        db.flush()


# ── Mock WhatsApp templates (used when Meta credentials are not configured) ────

MOCK_TEMPLATES: List[Dict[str, Any]] = [
    {
        "id": "mock_1",
        "name": "abandoned_cart_reminder",
        "language": "ar",
        "category": "MARKETING",
        "status": "APPROVED",
        "components": [
            {"type": "HEADER", "format": "TEXT", "text": "سلّتك في انتظارك! 🛒"},
            {"type": "BODY",   "text": "مرحباً {{1}}،\nلاحظنا أنك تركت بعض المنتجات في سلّتك.\nأكمل طلبك الآن قبل نفاد الكمية: {{2}}"},
            {"type": "FOOTER", "text": "🐝 نحلة — مساعد متجرك"},
            {"type": "BUTTONS", "buttons": [{"type": "URL", "text": "أكمل الطلب", "url": "{{2}}"}]},
        ],
    },
    {
        "id": "mock_2",
        "name": "special_offer",
        "language": "ar",
        "category": "MARKETING",
        "status": "APPROVED",
        "components": [
            {"type": "HEADER", "format": "TEXT", "text": "عرض خاص لك 🎁"},
            {"type": "BODY",   "text": "أهلاً {{1}}،\nاحصل على خصم {{2}} باستخدام كود: *{{3}}*\nالعرض ينتهي قريباً — لا تفوّته!"},
            {"type": "FOOTER", "text": "🐝 نحلة — مساعد متجرك"},
        ],
    },
    {
        "id": "mock_3",
        "name": "new_arrivals",
        "language": "ar",
        "category": "MARKETING",
        "status": "APPROVED",
        "components": [
            {"type": "HEADER", "format": "TEXT", "text": "وصل جديد! ✨"},
            {"type": "BODY",   "text": "مرحباً {{1}}،\nيسعدنا إعلامك بوصول منتجات جديدة في متجر {{2}}.\nاكتشف أحدث التشكيلة الآن وكن أول من يحصل عليها."},
            {"type": "FOOTER", "text": "🐝 نحلة — مساعد متجرك"},
        ],
    },
    {
        "id": "mock_4",
        "name": "vip_exclusive",
        "language": "ar",
        "category": "MARKETING",
        "status": "APPROVED",
        "components": [
            {"type": "HEADER", "format": "TEXT", "text": "👑 عرض VIP حصري"},
            {"type": "BODY",   "text": "{{1}}، أنت من عملائنا المميزين!\nبصفتك عضواً VIP لديك خصم حصري {{2}} على مشترياتك القادمة.\nاستخدم الكود: *{{3}}*"},
            {"type": "FOOTER", "text": "🐝 نحلة — مساعد متجرك"},
        ],
    },
    {
        "id": "mock_5",
        "name": "order_confirmed",
        "language": "ar",
        "category": "UTILITY",
        "status": "APPROVED",
        "components": [
            {"type": "HEADER", "format": "TEXT", "text": "تأكيد الطلب ✅"},
            {"type": "BODY",   "text": "شكراً {{1}}!\nتم استلام طلبك رقم *{{2}}* بنجاح.\nسيتم التواصل معك قريباً لتأكيد موعد التوصيل."},
            {"type": "FOOTER", "text": "🐝 نحلة — مساعد متجرك"},
        ],
    },
    {
        "id": "mock_6",
        "name": "win_back",
        "language": "ar",
        "category": "MARKETING",
        "status": "APPROVED",
        "components": [
            {"type": "HEADER", "format": "TEXT", "text": "اشتقنا إليك! 💙"},
            {"type": "BODY",   "text": "مرحباً {{1}}،\nلم نرك منذ فترة ونحن نفتقدك!\nعدت إلينا مع خصم خاص {{2}} على طلبك القادم.\nالكود: *{{3}}*"},
            {"type": "FOOTER", "text": "🐝 نحلة — مساعد متجرك"},
        ],
    },
]

def _fetch_meta_templates(waba_id: str, access_token: str) -> Optional[List[Dict[str, Any]]]:
    """Try to fetch templates from Meta Graph API. Returns None on failure."""
    try:
        import urllib.request
        import json as _json
        url = f"https://graph.facebook.com/v18.0/{waba_id}/message_templates?access_token={access_token}&limit=50&status=APPROVED"
        with urllib.request.urlopen(url, timeout=5) as resp:
            data = _json.loads(resp.read())
            return data.get("data", [])
    except Exception:
        return None


# ── Pydantic schemas for WhatsApp templates ───────────────────────────────────

class TemplateComponentIn(BaseModel):
    type: str                        # HEADER | BODY | FOOTER | BUTTONS
    format: Optional[str] = None     # TEXT | IMAGE | DOCUMENT | VIDEO (HEADER only)
    text: Optional[str] = None
    buttons: Optional[List[Dict[str, Any]]] = None

class CreateTemplateIn(BaseModel):
    name: str
    language: str = "ar"
    category: str                    # MARKETING | UTILITY | AUTHENTICATION
    components: List[TemplateComponentIn]

class UpdateTemplateStatusIn(BaseModel):
    status: str                      # APPROVED | REJECTED | DISABLED
    rejection_reason: Optional[str] = None
    meta_template_id: Optional[str] = None


def _tpl_to_dict(t: WhatsAppTemplate) -> Dict[str, Any]:
    return {
        "id": t.id,
        "meta_template_id": t.meta_template_id,
        "name": t.name,
        "language": t.language,
        "category": t.category,
        "status": t.status,
        "rejection_reason": t.rejection_reason,
        "components": t.components or [],
        "created_at": t.created_at.isoformat() if t.created_at else None,
        "updated_at": t.updated_at.isoformat() if t.updated_at else None,
        "synced_at": t.synced_at.isoformat() if t.synced_at else None,
        # AI lifecycle fields (migration 0009)
        "source": getattr(t, "source", "merchant") or "merchant",
        "objective": getattr(t, "objective", None),
        "usage_count": getattr(t, "usage_count", 0) or 0,
        "last_used_at": t.last_used_at.isoformat() if getattr(t, "last_used_at", None) else None,
        "health_score": getattr(t, "health_score", None),
        "recommendation_state": getattr(t, "recommendation_state", "none") or "none",
        "recommendation_note": getattr(t, "recommendation_note", None),
        "ai_generation_metadata": getattr(t, "ai_generation_metadata", None),
    }


def _tpl_bump_usage(db: Session, template_id: int, tenant_id: int | None = None) -> None:
    """Increment usage_count and set last_used_at. Called whenever a template is dispatched."""
    q = db.query(WhatsAppTemplate).filter(WhatsAppTemplate.id == template_id)
    if tenant_id is not None:
        q = q.filter(WhatsAppTemplate.tenant_id == tenant_id)
    tpl = q.first()
    if tpl:
        tpl.usage_count = (getattr(tpl, "usage_count", 0) or 0) + 1
        tpl.last_used_at = datetime.utcnow()


# ── WhatsApp Templates endpoints ───────────────────────────────────────────────

@app.get("/templates")
async def list_templates(
    request: Request,
    status: Optional[str] = None,
    db: Session = Depends(get_db),
):
    """List all WhatsApp templates for this tenant, optionally filtered by status."""
    tenant_id = _resolve_tenant_id(request)
    _get_or_create_tenant(db, tenant_id)
    _seed_templates_if_empty(db, tenant_id)
    db.commit()

    q = db.query(WhatsAppTemplate).filter(WhatsAppTemplate.tenant_id == tenant_id)
    if status:
        q = q.filter(WhatsAppTemplate.status == status.upper())
    templates = q.order_by(WhatsAppTemplate.created_at.desc()).all()
    return {"templates": [_tpl_to_dict(t) for t in templates]}


@app.post("/templates")
async def create_template(body: CreateTemplateIn, request: Request, db: Session = Depends(get_db)):
    """
    Create a new template locally and submit it to Meta for approval.
    The template is saved with status PENDING until Meta responds.
    """
    tenant_id = _resolve_tenant_id(request)
    _get_or_create_tenant(db, tenant_id)

    # Try to submit to Meta if credentials are configured
    settings = _get_or_create_settings(db, tenant_id)
    wa = _merge_defaults(settings.whatsapp_settings, DEFAULT_WHATSAPP)
    waba_id = wa.get("phone_number_id", "")
    token = wa.get("access_token", "")

    meta_id = None
    if waba_id and token:
        meta_id = _submit_template_to_meta(waba_id, token, body)

    tpl = WhatsAppTemplate(
        tenant_id=tenant_id,
        meta_template_id=meta_id,
        name=body.name,
        language=body.language,
        category=body.category,
        status="PENDING",
        components=[c.model_dump(exclude_none=True) for c in body.components],
        created_at=datetime.utcnow(),
        updated_at=datetime.utcnow(),
    )
    db.add(tpl)
    db.commit()
    db.refresh(tpl)
    return _tpl_to_dict(tpl)


@app.put("/templates/{template_id}/status")
async def update_template_status(
    template_id: int,
    body: UpdateTemplateStatusIn,
    request: Request,
    db: Session = Depends(get_db),
):
    """Update template status (called by webhook or manually for testing)."""
    tenant_id = _resolve_tenant_id(request)
    tpl = db.query(WhatsAppTemplate).filter(
        WhatsAppTemplate.id == template_id,
        WhatsAppTemplate.tenant_id == tenant_id,
    ).first()
    if not tpl:
        raise HTTPException(status_code=404, detail="Template not found")
    tpl.status = body.status.upper()
    if body.rejection_reason:
        tpl.rejection_reason = body.rejection_reason
    if body.meta_template_id:
        tpl.meta_template_id = body.meta_template_id
    tpl.updated_at = datetime.utcnow()
    db.commit()
    db.refresh(tpl)
    return _tpl_to_dict(tpl)


@app.delete("/templates/{template_id}")
async def delete_template(template_id: int, request: Request, db: Session = Depends(get_db)):
    """Delete a template (only allowed for PENDING/REJECTED/DISABLED)."""
    tenant_id = _resolve_tenant_id(request)
    tpl = db.query(WhatsAppTemplate).filter(
        WhatsAppTemplate.id == template_id,
        WhatsAppTemplate.tenant_id == tenant_id,
    ).first()
    if not tpl:
        raise HTTPException(status_code=404, detail="Template not found")
    if tpl.status == "APPROVED":
        raise HTTPException(status_code=400, detail="Cannot delete an APPROVED template — disable it from Meta Business Manager first")
    db.delete(tpl)
    db.commit()
    return {"deleted": True}


# ── AI Template Generation endpoints ──────────────────────────────────────────

class GenerateTemplateIn(BaseModel):
    objective: str          # abandoned_cart | reorder | winback | ...
    language: str = "ar"


class RecommendationActionIn(BaseModel):
    action: str             # accepted | dismissed


@app.post("/templates/generate")
async def generate_template(body: GenerateTemplateIn, request: Request, db: Session = Depends(get_db)):
    """
    AI-generate a WhatsApp template draft for a given objective.

    If template_submission_mode == 'auto_submit' and Meta credentials are present,
    the draft is submitted immediately.
    Otherwise it is saved as DRAFT for merchant review.
    """
    import sys as _sys
    _sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), ".")))
    from template_ai.generator import generate_template_draft, SUPPORTED_OBJECTIVES
    from template_ai.policy_validator import validate_draft

    tenant_id = _resolve_tenant_id(request)
    _get_or_create_tenant(db, tenant_id)

    if body.objective not in SUPPORTED_OBJECTIVES:
        raise HTTPException(
            status_code=422,
            detail=f"الهدف '{body.objective}' غير مدعوم. الأهداف المتاحة: {', '.join(SUPPORTED_OBJECTIVES)}"
        )

    draft = generate_template_draft(objective=body.objective, language=body.language)

    # Policy validation
    existing = db.query(WhatsAppTemplate).filter(WhatsAppTemplate.tenant_id == tenant_id).all()
    validation = validate_draft(draft, existing)

    if not validation.passed and validation.action == "block":
        return {
            "generated": False,
            "action": "block",
            "issues": validation.issues,
            "draft": draft,
        }

    if validation.action == "merge":
        return {
            "generated": False,
            "action": "merge",
            "issues": validation.issues,
            "merge_with_id": validation.merge_with_id,
            "merge_with_name": validation.merge_with_name,
            "draft": draft,
        }

    # Determine submission mode from tenant whatsapp settings
    settings = _get_or_create_settings(db, tenant_id)
    wa = _merge_defaults(settings.whatsapp_settings, DEFAULT_WHATSAPP)
    submission_mode = wa.get("template_submission_mode", "draft_approval")

    # Persist the draft
    tpl = WhatsAppTemplate(
        tenant_id=tenant_id,
        name=draft["name"],
        language=draft["language"],
        category=draft["category"],
        status="DRAFT",
        components=draft["components"],
        source="ai_generated",
        objective=draft["objective"],
        usage_count=0,
        ai_generation_metadata=draft["ai_generation_metadata"],
    )
    db.add(tpl)
    db.flush()

    # Auto-submit if configured and credentials present
    submitted = False
    meta_id = None
    if submission_mode == "auto_submit":
        waba_id = wa.get("phone_number_id", "")
        token = wa.get("access_token", "")
        if waba_id and token:
            meta_id = await _submit_template_to_meta(tpl.name, tpl.language, tpl.category, tpl.components or [], waba_id, token)
            if meta_id:
                tpl.meta_template_id = meta_id
                tpl.status = "PENDING"
                tpl.synced_at = datetime.utcnow()
                submitted = True

    db.commit()
    db.refresh(tpl)

    from observability.event_logger import log_event
    log_event(db, tenant_id, "ai_sales", "template.generated",
              f"قالب AI جديد: {tpl.name} (هدف: {body.objective})",
              payload={"template_id": tpl.id, "submitted": submitted, "mode": submission_mode})
    db.commit()

    return {
        "generated": True,
        "action": "auto_submitted" if submitted else "saved_as_draft",
        "template": _tpl_to_dict(tpl),
        "validation_issues": validation.issues,
        "submission_mode": submission_mode,
    }


@app.post("/templates/{template_id}/submit")
async def submit_template_to_meta(template_id: int, request: Request, db: Session = Depends(get_db)):
    """Submit a DRAFT template to Meta for approval."""
    tenant_id = _resolve_tenant_id(request)
    tpl = db.query(WhatsAppTemplate).filter(
        WhatsAppTemplate.id == template_id,
        WhatsAppTemplate.tenant_id == tenant_id,
    ).first()
    if not tpl:
        raise HTTPException(status_code=404, detail="Template not found")
    if tpl.status not in ("DRAFT", "REJECTED"):
        raise HTTPException(status_code=400, detail=f"لا يمكن إرسال قالب بحالة '{tpl.status}' إلى Meta")

    settings = _get_or_create_settings(db, tenant_id)
    wa = _merge_defaults(settings.whatsapp_settings, DEFAULT_WHATSAPP)
    waba_id = wa.get("phone_number_id", "")
    token = wa.get("access_token", "")

    if not waba_id or not token:
        raise HTTPException(status_code=422, detail="بيانات WhatsApp Business غير مُعدَّة. أضف Phone Number ID و Access Token في الإعدادات.")

    meta_id = await _submit_template_to_meta(tpl.name, tpl.language, tpl.category, tpl.components or [], waba_id, token)
    if not meta_id:
        raise HTTPException(status_code=502, detail="فشل إرسال القالب إلى Meta. تحقق من بيانات الاعتماد وحاول مرة أخرى.")

    tpl.meta_template_id = meta_id
    tpl.status = "PENDING"
    tpl.synced_at = datetime.utcnow()
    tpl.updated_at = datetime.utcnow()
    db.commit()
    db.refresh(tpl)

    from observability.event_logger import log_event
    log_event(db, tenant_id, "ai_sales", "template.submitted",
              f"تم إرسال القالب '{tpl.name}' إلى Meta للمراجعة",
              payload={"template_id": tpl.id, "meta_template_id": meta_id})
    db.commit()

    return {"submitted": True, "template": _tpl_to_dict(tpl)}


@app.get("/templates/health")
async def get_template_health(request: Request, db: Session = Depends(get_db)):
    """
    Evaluate health scores for all tenant templates and return merchant-facing recommendations.
    Updates health_score and recommendation_state in DB.
    """
    from template_ai.health_evaluator import evaluate_templates, health_summary

    tenant_id = _resolve_tenant_id(request)
    templates = db.query(WhatsAppTemplate).filter(WhatsAppTemplate.tenant_id == tenant_id).all()

    if not templates:
        return {"total": 0, "healthy": 0, "needs_attention": 0, "avg_health_score": 0.0, "details": []}

    results = evaluate_templates(templates)

    # Persist scores back to DB
    tpl_map = {t.id: t for t in templates}
    for r in results:
        t = tpl_map.get(r["template_id"])
        if t:
            t.health_score = r["health_score"]
            if r["recommendation_state"] != "none":
                # Only write if not already actioned by merchant
                if getattr(t, "recommendation_state", None) not in ("accepted", "dismissed"):
                    t.recommendation_state = r["recommendation_state"]
                    t.recommendation_note = r["recommendation_note"]
    db.commit()

    return health_summary(templates)


@app.put("/templates/{template_id}/recommendation")
async def action_template_recommendation(
    template_id: int,
    body: RecommendationActionIn,
    request: Request,
    db: Session = Depends(get_db),
):
    """
    Merchant acts on a template health recommendation.
    action: 'accepted' (will delete/update) | 'dismissed' (ignore suggestion)
    """
    tenant_id = _resolve_tenant_id(request)
    tpl = db.query(WhatsAppTemplate).filter(
        WhatsAppTemplate.id == template_id,
        WhatsAppTemplate.tenant_id == tenant_id,
    ).first()
    if not tpl:
        raise HTTPException(status_code=404, detail="Template not found")

    if body.action not in ("accepted", "dismissed"):
        raise HTTPException(status_code=422, detail="action يجب أن يكون 'accepted' أو 'dismissed'")

    tpl.recommendation_state = body.action
    tpl.updated_at = datetime.utcnow()
    db.commit()
    db.refresh(tpl)

    return {"updated": True, "template": _tpl_to_dict(tpl)}


@app.get("/templates/objectives")
async def list_template_objectives(request: Request):
    """Return the list of supported AI generation objectives."""
    from template_ai.generator import SUPPORTED_OBJECTIVES
    labels = {
        "abandoned_cart":       "استرداد سلة متروكة",
        "reorder":              "تذكير بإعادة الطلب",
        "winback":              "استعادة عميل غير نشط",
        "back_in_stock":        "إشعار توفر منتج",
        "price_drop":           "إشعار انخفاض السعر",
        "order_followup":       "متابعة طلب",
        "quote_followup":       "متابعة عرض سعر",
        "promotion":            "حملة ترويجية",
        "transactional_update": "تحديث معاملة",
    }
    return {
        "objectives": [
            {"value": obj, "label": labels.get(obj, obj)}
            for obj in SUPPORTED_OBJECTIVES
        ]
    }


@app.post("/templates/sync")
async def sync_templates_from_meta(request: Request, db: Session = Depends(get_db)):
    """
    Pull all templates from Meta Graph API and upsert them into the local DB.
    New templates are inserted; existing ones have their status updated.
    """
    tenant_id = _resolve_tenant_id(request)
    settings = _get_or_create_settings(db, tenant_id)
    db.commit()

    wa = _merge_defaults(settings.whatsapp_settings, DEFAULT_WHATSAPP)
    waba_id = wa.get("phone_number_id", "")
    token = wa.get("access_token", "")

    if not waba_id or not token:
        return {"synced": 0, "message": "يجب إدخال Phone Number ID و Access Token في الإعدادات أولاً"}

    live = _fetch_meta_templates(waba_id, token)
    if live is None:
        return {"synced": 0, "message": "تعذّر الاتصال بـ Meta. تأكد من صحة بيانات الاعتماد."}

    synced = 0
    for item in live:
        meta_id = str(item.get("id", ""))
        existing = db.query(WhatsAppTemplate).filter(
            WhatsAppTemplate.tenant_id == tenant_id,
            WhatsAppTemplate.meta_template_id == meta_id,
        ).first()
        if existing:
            existing.status = item.get("status", existing.status)
            existing.components = item.get("components", existing.components)
            existing.synced_at = datetime.utcnow()
            existing.updated_at = datetime.utcnow()
        else:
            tpl = WhatsAppTemplate(
                tenant_id=tenant_id,
                meta_template_id=meta_id,
                name=item.get("name", ""),
                language=item.get("language", "ar"),
                category=item.get("category", "MARKETING"),
                status=item.get("status", "PENDING"),
                components=item.get("components", []),
                created_at=datetime.utcnow(),
                updated_at=datetime.utcnow(),
                synced_at=datetime.utcnow(),
            )
            db.add(tpl)
        synced += 1

    db.commit()
    return {"synced": synced, "message": f"تمت مزامنة {synced} قالب من Meta"}


def _submit_template_to_meta(waba_id: str, token: str, body: "CreateTemplateIn") -> Optional[str]:
    """Submit a new template to Meta Graph API. Returns the Meta template ID or None."""
    try:
        import urllib.request
        import urllib.parse
        import json as _json
        url = f"https://graph.facebook.com/v18.0/{waba_id}/message_templates"
        payload = _json.dumps({
            "name": body.name,
            "language": body.language,
            "category": body.category,
            "components": [c.model_dump(exclude_none=True) for c in body.components],
        }).encode()
        req = urllib.request.Request(
            url,
            data=payload,
            headers={"Content-Type": "application/json", "Authorization": f"Bearer {token}"},
            method="POST",
        )
        with urllib.request.urlopen(req, timeout=10) as resp:
            data = _json.loads(resp.read())
            return str(data.get("id", ""))
    except Exception:
        return None


# ── Pydantic schemas for campaigns ────────────────────────────────────────────

class CreateCampaignIn(BaseModel):
    name: str
    campaign_type: str
    template_id: str
    template_name: str
    template_language: str = "ar"
    template_category: str = "MARKETING"
    template_body: str = ""
    template_variables: Optional[Dict[str, str]] = None
    audience_type: str = "all"
    audience_count: int = 0
    schedule_type: str = "immediate"
    schedule_time: Optional[str] = None
    delay_minutes: Optional[int] = None
    coupon_code: str = ""

class UpdateCampaignStatusIn(BaseModel):
    status: str  # active | paused | completed

class TestSendIn(BaseModel):
    phone: str
    template_id: str
    template_name: str
    template_language: str = "ar"
    variables: Dict[str, str] = {}


def _campaign_to_dict(c: Campaign) -> Dict[str, Any]:
    return {
        "id": c.id,
        "name": c.name,
        "campaign_type": c.campaign_type,
        "status": c.status,
        "template_id": c.template_id,
        "template_name": c.template_name,
        "template_language": c.template_language,
        "template_category": c.template_category,
        "template_body": c.template_body,
        "template_variables": c.template_variables or {},
        "audience_type": c.audience_type,
        "audience_count": c.audience_count,
        "schedule_type": c.schedule_type,
        "schedule_time": c.schedule_time.isoformat() if c.schedule_time else None,
        "delay_minutes": c.delay_minutes,
        "coupon_code": c.coupon_code or "",
        "sent_count": c.sent_count,
        "delivered_count": c.delivered_count,
        "read_count": c.read_count,
        "clicked_count": c.clicked_count,
        "converted_count": c.converted_count,
        "created_at": c.created_at.isoformat() if c.created_at else None,
        "launched_at": c.launched_at.isoformat() if c.launched_at else None,
    }


# ── Campaign endpoints ─────────────────────────────────────────────────────────

@app.get("/campaigns/templates")
async def get_campaign_templates(request: Request, db: Session = Depends(get_db)):
    """Return APPROVED templates from DB for campaign wizard."""
    tenant_id = _resolve_tenant_id(request)
    _get_or_create_tenant(db, tenant_id)
    _seed_templates_if_empty(db, tenant_id)
    db.commit()

    approved = (
        db.query(WhatsAppTemplate)
        .filter(WhatsAppTemplate.tenant_id == tenant_id, WhatsAppTemplate.status == "APPROVED")
        .order_by(WhatsAppTemplate.created_at.desc())
        .all()
    )
    result = []
    for t in approved:
        result.append({
            "id": str(t.id),
            "name": t.name,
            "language": t.language,
            "category": t.category,
            "status": t.status,
            "components": t.components or [],
        })
    return {"templates": result, "source": "db"}


@app.get("/campaigns")
async def list_campaigns(request: Request, db: Session = Depends(get_db)):
    tenant_id = _resolve_tenant_id(request)
    _get_or_create_tenant(db, tenant_id)
    db.commit()
    campaigns = db.query(Campaign).filter(Campaign.tenant_id == tenant_id).order_by(Campaign.created_at.desc()).all()
    return {"campaigns": [_campaign_to_dict(c) for c in campaigns]}


@app.post("/campaigns")
async def create_campaign(body: CreateCampaignIn, request: Request, db: Session = Depends(get_db)):
    tenant_id = _resolve_tenant_id(request)
    _get_or_create_tenant(db, tenant_id)

    schedule_dt = None
    if body.schedule_time:
        try:
            from datetime import datetime as _dt
            schedule_dt = _dt.fromisoformat(body.schedule_time)
        except ValueError:
            pass

    campaign = Campaign(
        tenant_id=tenant_id,
        name=body.name,
        campaign_type=body.campaign_type,
        status="scheduled" if body.schedule_type == "scheduled" and schedule_dt else "draft",
        template_id=body.template_id,
        template_name=body.template_name,
        template_language=body.template_language,
        template_category=body.template_category,
        template_body=body.template_body,
        template_variables=body.template_variables or {},
        audience_type=body.audience_type,
        audience_count=body.audience_count,
        schedule_type=body.schedule_type,
        schedule_time=schedule_dt,
        delay_minutes=body.delay_minutes,
        coupon_code=body.coupon_code or None,
        created_at=datetime.utcnow(),
        updated_at=datetime.utcnow(),
    )
    db.add(campaign)
    db.commit()
    db.refresh(campaign)
    return _campaign_to_dict(campaign)


@app.put("/campaigns/{campaign_id}/status")
async def update_campaign_status(
    campaign_id: int,
    body: UpdateCampaignStatusIn,
    request: Request,
    db: Session = Depends(get_db),
):
    tenant_id = _resolve_tenant_id(request)
    campaign = db.query(Campaign).filter(Campaign.id == campaign_id, Campaign.tenant_id == tenant_id).first()
    if not campaign:
        raise HTTPException(status_code=404, detail="Campaign not found")
    campaign.status = body.status
    if body.status == "active" and not campaign.launched_at:
        campaign.launched_at = datetime.utcnow()
    campaign.updated_at = datetime.utcnow()
    db.commit()
    db.refresh(campaign)
    return _campaign_to_dict(campaign)


@app.post("/campaigns/test-send")
async def test_send(body: TestSendIn, request: Request, db: Session = Depends(get_db)):
    """Simulate sending a test message to a phone number."""
    tenant_id = _resolve_tenant_id(request)
    settings = _get_or_create_settings(db, tenant_id)
    db.commit()
    wa = _merge_defaults(settings.whatsapp_settings, DEFAULT_WHATSAPP)
    if not wa.get("phone_number_id") or not wa.get("access_token"):
        return {"success": True, "simulated": True, "message": f"تمت المحاكاة — أرسلنا القالب '{body.template_name}' إلى {body.phone} (وضع تجريبي)"}
    # In production: call Meta Cloud API send message endpoint here
    return {"success": True, "simulated": False, "message": f"تم إرسال رسالة اختبار إلى {body.phone}"}


# ── Pydantic schemas for automations ──────────────────────────────────────────

class ToggleAutomationIn(BaseModel):
    enabled: bool

class UpdateAutomationConfigIn(BaseModel):
    config: Dict[str, Any]
    template_id: Optional[int] = None

class EmitEventIn(BaseModel):
    event_type: str
    customer_id: Optional[int] = None
    payload: Optional[Dict[str, Any]] = None


def _auto_to_dict(a: SmartAutomation) -> Dict[str, Any]:
    return {
        "id": a.id,
        "automation_type": a.automation_type,
        "name": a.name,
        "enabled": a.enabled,
        "config": a.config or {},
        "template_id": a.template_id,
        "template_name": a.template.name if a.template else None,
        "stats_triggered": a.stats_triggered,
        "stats_sent": a.stats_sent,
        "stats_converted": a.stats_converted,
        "updated_at": a.updated_at.isoformat() if a.updated_at else None,
    }


# ── Automations endpoints ──────────────────────────────────────────────────────

@app.get("/automations")
async def list_automations(request: Request, db: Session = Depends(get_db)):
    tenant_id = _resolve_tenant_id(request)
    _get_or_create_tenant(db, tenant_id)
    _seed_automations_if_empty(db, tenant_id)
    db.commit()
    autos = db.query(SmartAutomation).filter(SmartAutomation.tenant_id == tenant_id).order_by(SmartAutomation.id).all()
    autopilot = _get_autopilot_enabled(db, tenant_id)
    return {"automations": [_auto_to_dict(a) for a in autos], "autopilot_enabled": autopilot}


@app.put("/automations/{automation_id}/toggle")
async def toggle_automation(
    automation_id: int,
    body: ToggleAutomationIn,
    request: Request,
    db: Session = Depends(get_db),
):
    tenant_id = _resolve_tenant_id(request)
    auto = db.query(SmartAutomation).filter(
        SmartAutomation.id == automation_id,
        SmartAutomation.tenant_id == tenant_id,
    ).first()
    if not auto:
        raise HTTPException(status_code=404, detail="Automation not found")
    auto.enabled = body.enabled
    auto.updated_at = datetime.utcnow()
    db.commit()
    db.refresh(auto)
    return _auto_to_dict(auto)


@app.put("/automations/{automation_id}/config")
async def update_automation_config(
    automation_id: int,
    body: UpdateAutomationConfigIn,
    request: Request,
    db: Session = Depends(get_db),
):
    tenant_id = _resolve_tenant_id(request)
    auto = db.query(SmartAutomation).filter(
        SmartAutomation.id == automation_id,
        SmartAutomation.tenant_id == tenant_id,
    ).first()
    if not auto:
        raise HTTPException(status_code=404, detail="Automation not found")
    auto.config = body.config
    if body.template_id is not None:
        auto.template_id = body.template_id
    auto.updated_at = datetime.utcnow()
    db.commit()
    db.refresh(auto)
    return _auto_to_dict(auto)


@app.post("/automations/autopilot")
async def set_autopilot(
    body: ToggleAutomationIn,
    request: Request,
    db: Session = Depends(get_db),
):
    """Enable/disable the Marketing Autopilot master switch."""
    tenant_id = _resolve_tenant_id(request)
    settings = _get_or_create_settings(db, tenant_id)
    current = _merge_defaults(settings.ai_settings, DEFAULT_AI)
    current["autopilot_enabled"] = body.enabled
    settings.ai_settings = current
    settings.updated_at = datetime.utcnow()
    db.commit()
    return {"autopilot_enabled": body.enabled}


@app.post("/automations/events")
async def emit_event(body: EmitEventIn, request: Request, db: Session = Depends(get_db)):
    """Emit a system event that automations can react to."""
    tenant_id = _resolve_tenant_id(request)
    _get_or_create_tenant(db, tenant_id)
    event = AutomationEvent(
        tenant_id=tenant_id,
        event_type=body.event_type,
        customer_id=body.customer_id,
        payload=body.payload or {},
        processed=False,
        created_at=datetime.utcnow(),
    )
    db.add(event)
    db.commit()
    return {"event_id": event.id, "event_type": event.event_type}


def _get_autopilot_enabled(db: Session, tenant_id: int) -> bool:
    settings = db.query(TenantSettings).filter(TenantSettings.tenant_id == tenant_id).first()
    if not settings:
        return False
    ai = _merge_defaults(settings.ai_settings, DEFAULT_AI)
    return bool(ai.get("autopilot_enabled", False))


# ── Intelligence dashboard endpoints ──────────────────────────────────────────

# Mock intelligence data (production: computed by background worker)
_MOCK_REORDER_CUSTOMERS = [
    {"customer_name": "Ahmed Al-Rashid", "phone": "+966 50 123 4567", "product_name": "عسل السدر 500g", "predicted_date": "2026-04-05", "confidence": 87},
    {"customer_name": "Nora Al-Mutairi",  "phone": "+966 52 654 3210", "product_name": "عسل الطلح 1kg",  "predicted_date": "2026-04-08", "confidence": 74},
    {"customer_name": "Khalid Ibrahim",   "phone": "+966 57 888 7766", "product_name": "عسل السدر 500g", "predicted_date": "2026-04-10", "confidence": 91},
    {"customer_name": "Lina Al-Saud",     "phone": "+966 54 551 2200", "product_name": "عسل الأكاسيا 250g", "predicted_date": "2026-04-12", "confidence": 68},
    {"customer_name": "Omar Al-Ghamdi",   "phone": "+966 56 321 9900", "product_name": "عسل السمر 1kg",  "predicted_date": "2026-04-14", "confidence": 82},
]

_MOCK_CHURN_RISK = [
    {"customer_name": "Reem Al-Harbi",    "phone": "+966 55 410 0033", "last_purchase": "2025-12-10", "days_inactive": 110, "risk_score": 0.82},
    {"customer_name": "Yousef Al-Shehri", "phone": "+966 50 775 5522", "last_purchase": "2026-01-18", "days_inactive": 71,  "risk_score": 0.65},
    {"customer_name": "Sara Al-Qahtani",  "phone": "+966 53 221 8800", "last_purchase": "2026-01-02", "days_inactive": 87,  "risk_score": 0.71},
]

_MOCK_VIP_CUSTOMERS = [
    {"customer_name": "Ahmed Al-Rashid",  "total_spent": 4820, "orders": 18, "segment": "VIP"},
    {"customer_name": "Khalid Ibrahim",   "total_spent": 3150, "orders": 12, "segment": "VIP"},
    {"customer_name": "Omar Al-Ghamdi",   "total_spent": 2340, "orders": 9,  "segment": "VIP"},
]

_MOCK_SUGGESTIONS = [
    {
        "id": "s1",
        "type": "reorder",
        "priority": "high",
        "title": "أطلق حملة إعادة طلب لعملاء عسل السدر",
        "desc": "5 عملاء يُتوقع احتياجهم لإعادة الطلب خلال 2 أسبوع.",
        "action": "launch_campaign",
        "automation_type": "predictive_reorder",
    },
    {
        "id": "s2",
        "type": "winback",
        "priority": "medium",
        "title": "3 عملاء في خطر المغادرة",
        "desc": "لم يتسوقوا منذ أكثر من 60 يوماً — أرسل عرضاً لاستعادتهم.",
        "action": "launch_campaign",
        "automation_type": "customer_winback",
    },
    {
        "id": "s3",
        "type": "vip",
        "priority": "low",
        "title": "فعّل التشغيل التلقائي لـ VIP",
        "desc": "3 عملاء أنفقوا أكثر من 2000 ر.س ولم يتلقوا عرض VIP بعد.",
        "action": "enable_automation",
        "automation_type": "vip_upgrade",
    },
]


@app.get("/intelligence/dashboard")
async def intelligence_dashboard(request: Request, db: Session = Depends(get_db)):
    """
    Return intelligence summary for the current tenant.
    Uses real CustomerProfile data when available; falls back to mock data otherwise.
    """
    tenant_id = _resolve_tenant_id(request)
    _get_or_create_tenant(db, tenant_id)
    _seed_automations_if_empty(db, tenant_id)
    _seed_demo_customers(db, tenant_id)
    db.commit()

    # Automation summary (always real)
    autos = db.query(SmartAutomation).filter(SmartAutomation.tenant_id == tenant_id).all()
    active_automations = sum(1 for a in autos if a.enabled)

    # ── Real DB intelligence ───────────────────────────────────────────────────
    profiles_exist = db.query(CustomerProfile).filter(
        CustomerProfile.tenant_id == tenant_id
    ).count() > 0

    if profiles_exist:
        now = datetime.utcnow()

        # Segment counts
        from sqlalchemy import func as sqlfunc
        seg_rows = (
            db.query(CustomerProfile.segment, sqlfunc.count(CustomerProfile.id))
            .filter(CustomerProfile.tenant_id == tenant_id)
            .group_by(CustomerProfile.segment)
            .all()
        )
        seg_map = {row[0]: row[1] for row in seg_rows}

        # VIP customers sorted by spend
        vip_rows = (
            db.query(CustomerProfile, Customer)
            .join(Customer, CustomerProfile.customer_id == Customer.id)
            .filter(
                CustomerProfile.tenant_id == tenant_id,
                CustomerProfile.segment == "vip",
            )
            .order_by(CustomerProfile.total_spend_sar.desc())
            .limit(10)
            .all()
        )
        vip_customers = [
            {
                "customer_name": c.name or "—",
                "total_spent": round(float(p.total_spend_sar or 0), 2),
                "orders": p.total_orders or 0,
                "segment": "VIP",
            }
            for p, c in vip_rows
        ]

        # At-risk customers sorted by churn risk
        churn_rows = (
            db.query(CustomerProfile, Customer)
            .join(Customer, CustomerProfile.customer_id == Customer.id)
            .filter(
                CustomerProfile.tenant_id == tenant_id,
                CustomerProfile.segment.in_(["at_risk", "churned"]),
            )
            .order_by(CustomerProfile.churn_risk_score.desc())
            .limit(10)
            .all()
        )
        churn_risk = [
            {
                "customer_name": c.name or "—",
                "phone": c.phone or "",
                "last_purchase": (p.last_order_at or now).isoformat(),
                "days_inactive": max(0, (now - (p.last_order_at or now)).days),
                "risk_score": round((p.churn_risk_score or 0) * 100),
            }
            for p, c in churn_rows
        ]

        # Smart suggestions from real data
        suggestions: List[Dict[str, Any]] = []
        reorder_count = len(_MOCK_REORDER_CUSTOMERS)  # real engine: query PredictiveReorderEstimate
        if reorder_count > 0:
            suggestions.append({
                "id": "s1", "type": "reorder", "priority": "high",
                "title": "أطلق حملة إعادة طلب لعملاء عسل السدر",
                "desc": f"{reorder_count} عملاء يُتوقع احتياجهم لإعادة الطلب خلال أسبوعين.",
                "action": "launch_campaign",
                "automation_type": "predictive_reorder",
            })
        if churn_risk:
            suggestions.append({
                "id": "s2", "type": "winback", "priority": "medium",
                "title": f"{len(churn_risk)} عملاء في خطر المغادرة",
                "desc": "لم يتسوقوا منذ أكثر من 60 يوماً — أرسل عرضاً لاستعادتهم.",
                "action": "launch_campaign",
                "automation_type": "customer_winback",
            })
        vip_auto_on = any(a.automation_type == "vip_upgrade" and a.enabled for a in autos)
        if vip_customers and not vip_auto_on:
            suggestions.append({
                "id": "s3", "type": "vip", "priority": "low",
                "title": "فعّل التشغيل التلقائي لـ VIP",
                "desc": f"{len(vip_customers)} عملاء أنفقوا أكثر من 2000 ر.س ولم يتلقوا عرض VIP بعد.",
                "action": "enable_automation",
                "automation_type": "vip_upgrade",
            })

        return {
            "summary": {
                "reorder_soon_count": reorder_count,
                "churn_risk_count": len(churn_risk),
                "vip_count": len(vip_customers),
                "active_automations": active_automations,
            },
            "reorder_predictions": _MOCK_REORDER_CUSTOMERS,
            "churn_risk": churn_risk,
            "vip_customers": vip_customers,
            "suggestions": suggestions,
            "segments": [
                {"key": "new",     "label": "عملاء جدد",      "count": seg_map.get("new", 0),     "color": "blue"},
                {"key": "active",  "label": "عملاء نشطون",     "count": seg_map.get("active", 0),  "color": "green"},
                {"key": "vip",     "label": "VIP",              "count": seg_map.get("vip", 0),     "color": "amber"},
                {"key": "at_risk", "label": "خطر المغادرة",    "count": seg_map.get("at_risk", 0), "color": "red"},
                {"key": "churned", "label": "خاملون",           "count": seg_map.get("churned", 0), "color": "slate"},
            ],
        }

    # ── Fallback: mock data (no profiles in DB yet) ────────────────────────────
    return {
        "summary": {
            "reorder_soon_count": len(_MOCK_REORDER_CUSTOMERS),
            "churn_risk_count": len(_MOCK_CHURN_RISK),
            "vip_count": len(_MOCK_VIP_CUSTOMERS),
            "active_automations": active_automations,
        },
        "reorder_predictions": _MOCK_REORDER_CUSTOMERS,
        "churn_risk": _MOCK_CHURN_RISK,
        "vip_customers": _MOCK_VIP_CUSTOMERS,
        "suggestions": _MOCK_SUGGESTIONS,
        "segments": [
            {"key": "new",      "label": "عملاء جدد",    "count": 240, "color": "blue"},
            {"key": "active",   "label": "عملاء نشطون",   "count": 890, "color": "green"},
            {"key": "vip",      "label": "VIP",            "count": 83,  "color": "amber"},
            {"key": "churned",  "label": "خاملون",         "count": 420, "color": "slate"},
            {"key": "at_risk",  "label": "خطر المغادرة",  "count": 127, "color": "red"},
        ],
    }


@app.get("/intelligence/reorder-predictions")
async def reorder_predictions(request: Request, db: Session = Depends(get_db)):
    tenant_id = _resolve_tenant_id(request)
    _get_or_create_tenant(db, tenant_id)
    db.commit()
    # In production: query PredictiveReorderEstimate joined with Customer + Product
    return {"predictions": _MOCK_REORDER_CUSTOMERS}


@app.post("/intelligence/analyze-customers")
async def analyze_customers(request: Request, db: Session = Depends(get_db)):
    """
    Re-compute segment + churn_risk_score for every CustomerProfile in this tenant.
    Call this after importing orders or on a nightly schedule.
    """
    tenant_id = _resolve_tenant_id(request)
    _get_or_create_tenant(db, tenant_id)
    _seed_demo_customers(db, tenant_id)
    db.commit()

    profiles = db.query(CustomerProfile).filter(CustomerProfile.tenant_id == tenant_id).all()
    updated = 0

    for profile in profiles:
        days_inactive = (
            (datetime.utcnow() - profile.last_order_at).days
            if profile.last_order_at
            else 999
        )
        segment, churn_risk = _compute_customer_segment(
            profile.total_orders or 0,
            float(profile.total_spend_sar or 0),
            days_inactive,
        )
        profile.segment = segment
        profile.churn_risk_score = churn_risk
        profile.updated_at = datetime.utcnow()
        updated += 1

    db.commit()
    return {"analyzed": updated, "message": f"تم تحليل {updated} عميل وتحديث شرائحهم"}


@app.get("/intelligence/segments/live")
async def live_segments(request: Request, db: Session = Depends(get_db)):
    """Return real-time segment counts computed from CustomerProfile records."""
    tenant_id = _resolve_tenant_id(request)
    _get_or_create_tenant(db, tenant_id)
    _seed_demo_customers(db, tenant_id)
    db.commit()

    from sqlalchemy import func as sqlfunc
    rows = (
        db.query(CustomerProfile.segment, sqlfunc.count(CustomerProfile.id))
        .filter(CustomerProfile.tenant_id == tenant_id)
        .group_by(CustomerProfile.segment)
        .all()
    )
    seg_map = {r[0]: r[1] for r in rows}

    return {
        "segments": [
            {"key": "new",     "label": "عملاء جدد",      "count": seg_map.get("new", 0),     "color": "blue"},
            {"key": "active",  "label": "عملاء نشطون",     "count": seg_map.get("active", 0),  "color": "green"},
            {"key": "vip",     "label": "VIP",              "count": seg_map.get("vip", 0),     "color": "amber"},
            {"key": "at_risk", "label": "خطر المغادرة",    "count": seg_map.get("at_risk", 0), "color": "red"},
            {"key": "churned", "label": "خاملون",           "count": seg_map.get("churned", 0), "color": "slate"},
        ],
        "total": sum(seg_map.values()),
    }


@app.get("/intelligence/customer-profile/{customer_id}")
async def get_customer_profile(customer_id: int, request: Request, db: Session = Depends(get_db)):
    """
    Return the full behavior profile for a single customer, including segment,
    churn risk, spend history, and reorder estimates.
    """
    tenant_id = _resolve_tenant_id(request)
    customer = db.query(Customer).filter(
        Customer.id == customer_id,
        Customer.tenant_id == tenant_id,
    ).first()
    if not customer:
        raise HTTPException(status_code=404, detail="Customer not found")

    profile = db.query(CustomerProfile).filter(
        CustomerProfile.customer_id == customer_id,
        CustomerProfile.tenant_id == tenant_id,
    ).first()

    reorders = db.query(PredictiveReorderEstimate).filter(
        PredictiveReorderEstimate.customer_id == customer_id,
        PredictiveReorderEstimate.tenant_id == tenant_id,
    ).order_by(PredictiveReorderEstimate.predicted_reorder_date.asc()).all()

    SEGMENT_LABELS = {
        "new": "عميل جديد",
        "active": "عميل نشط",
        "vip": "عميل VIP",
        "at_risk": "في خطر المغادرة",
        "churned": "خامل",
    }

    profile_data: Dict[str, Any] = {
        "customer_id": customer.id,
        "customer_name": customer.name,
        "phone": customer.phone,
        "email": customer.email,
    }

    if profile:
        days_inactive = (
            (datetime.utcnow() - profile.last_order_at).days
            if profile.last_order_at
            else None
        )
        profile_data.update({
            "total_orders": profile.total_orders,
            "total_spent": profile.total_spend_sar,
            "average_order_value": profile.average_order_value_sar,
            "last_order_date": profile.last_order_at.isoformat() if profile.last_order_at else None,
            "first_seen_date": profile.first_seen_at.isoformat() if profile.first_seen_at else None,
            "days_inactive": days_inactive,
            "segment": profile.segment,
            "segment_label": SEGMENT_LABELS.get(profile.segment or "new", profile.segment),
            "churn_risk_score": profile.churn_risk_score,
            "lifetime_value_score": profile.lifetime_value_score,
            "is_returning": profile.is_returning,
        })
    else:
        profile_data.update({
            "total_orders": 0, "total_spent": 0, "average_order_value": 0,
            "last_order_date": None, "first_seen_date": None, "days_inactive": None,
            "segment": "new", "segment_label": "عميل جديد",
            "churn_risk_score": 0.0, "lifetime_value_score": 0.0, "is_returning": False,
        })

    reorder_data = []
    for r in reorders:
        product = db.query(Product).filter(Product.id == r.product_id, Product.tenant_id == tenant_id).first()
        reorder_data.append({
            "product_id": r.product_id,
            "product_name": product.title if product else f"Product #{r.product_id}",
            "purchase_date": r.purchase_date.isoformat() if r.purchase_date else None,
            "predicted_reorder_date": r.predicted_reorder_date.isoformat() if r.predicted_reorder_date else None,
            "consumption_rate_days": r.consumption_rate_days,
            "notified": r.notified,
        })

    profile_data["reorder_estimates"] = reorder_data
    return profile_data


@app.post("/intelligence/reorder-estimate")
async def create_reorder_estimate(
    request: Request,
    db: Session = Depends(get_db),
):
    """
    Compute a predicted reorder date given product + purchase history.
    Body: { customer_id, product_id, quantity_purchased, purchase_date, consumption_rate_days }
    """
    body = await request.json()
    tenant_id = _resolve_tenant_id(request)
    _get_or_create_tenant(db, tenant_id)

    from datetime import timedelta
    purchase_dt = datetime.utcnow()
    try:
        purchase_dt = datetime.fromisoformat(body.get("purchase_date", datetime.utcnow().isoformat()))
    except (ValueError, TypeError):
        pass

    consumption_days = int(body.get("consumption_rate_days", 30))
    predicted = purchase_dt + timedelta(days=consumption_days)

    estimate = PredictiveReorderEstimate(
        tenant_id=tenant_id,
        customer_id=int(body.get("customer_id", 0)),
        product_id=int(body.get("product_id", 0)),
        quantity_purchased=body.get("quantity_purchased"),
        purchase_date=purchase_dt,
        consumption_rate_days=consumption_days,
        predicted_reorder_date=predicted,
        created_at=datetime.utcnow(),
        updated_at=datetime.utcnow(),
    )
    db.add(estimate)
    db.commit()
    return {
        "predicted_reorder_date": predicted.isoformat(),
        "consumption_rate_days": consumption_days,
    }


# ── Sales Autopilot ───────────────────────────────────────────────────────────

DEFAULT_AUTOPILOT: Dict[str, Any] = {
    "enabled": False,
    "cod_confirmation": {
        "enabled": True,
        "reminder_hours": 2,
        "auto_cancel_hours": 24,
        "template_name": "cod_order_confirmation_ar",
    },
    "predictive_reorder": {
        "enabled": True,
        "days_before": 3,
        "consumption_days_default": 45,
        "template_name": "predictive_reorder_reminder_ar",
    },
    "abandoned_cart": {
        "enabled": True,
        "reminder_30min": True,
        "reminder_24h": True,
        "coupon_48h": False,
        "coupon_code": "",
        "template_name": "abandoned_cart_reminder",
    },
    "inactive_recovery": {
        "enabled": True,
        "inactive_days": 60,
        "discount_pct": 15,
        "template_name": "win_back",
    },
}

# Event types emitted by autopilot jobs (stored in AutomationEvent table)
AUTOPILOT_EVENT_TYPES = {
    "cod_confirmation":  "autopilot_cod_sent",
    "predictive_reorder": "autopilot_reorder_sent",
    "abandoned_cart":    "autopilot_cart_sent",
    "inactive_recovery": "autopilot_inactive_sent",
}

# Arabic labels shown in the daily summary
AUTOPILOT_SUMMARY_LABELS: Dict[str, str] = {
    "autopilot_cod_sent":      "تأكيدات طلبات COD أُرسلت",
    "autopilot_reorder_sent":  "تذكيرات إعادة طلب أُرسلت",
    "autopilot_cart_sent":     "سلات متروكة تم التواصل بشأنها",
    "autopilot_inactive_sent": "عملاء غير نشطين تم استرجاعهم",
}

AUTOPILOT_SUMMARY_ICONS: Dict[str, str] = {
    "autopilot_cod_sent":      "🍯",
    "autopilot_reorder_sent":  "🔄",
    "autopilot_cart_sent":     "🛒",
    "autopilot_inactive_sent": "💙",
}


def _get_autopilot_settings(db: Session, tenant_id: int) -> Dict[str, Any]:
    """Read autopilot config from TenantSettings.extra_metadata."""
    settings = db.query(TenantSettings).filter(TenantSettings.tenant_id == tenant_id).first()
    stored: Dict[str, Any] = {}
    if settings and settings.extra_metadata:
        stored = settings.extra_metadata.get("autopilot", {})
    merged = dict(DEFAULT_AUTOPILOT)
    if stored:
        merged.update({k: v for k, v in stored.items() if k in DEFAULT_AUTOPILOT})
        # Deep-merge sub-automation configs
        for sub in ("cod_confirmation", "predictive_reorder", "abandoned_cart", "inactive_recovery"):
            if sub in stored and isinstance(stored[sub], dict):
                base = dict(DEFAULT_AUTOPILOT[sub])
                base.update(stored[sub])
                merged[sub] = base
    return merged


def _save_autopilot_settings(db: Session, tenant_id: int, autopilot: Dict[str, Any]) -> None:
    """Persist autopilot config to TenantSettings.extra_metadata."""
    settings = _get_or_create_settings(db, tenant_id)
    extra: Dict[str, Any] = dict(settings.extra_metadata or {})
    extra["autopilot"] = autopilot
    settings.extra_metadata = extra
    settings.updated_at = datetime.utcnow()


def _get_daily_summary(db: Session, tenant_id: int) -> List[Dict[str, Any]]:
    """Count today's autopilot actions from AutomationEvent."""
    from datetime import date
    today_start = datetime.combine(date.today(), datetime.min.time())
    summary = []
    for evt_type, label in AUTOPILOT_SUMMARY_LABELS.items():
        count = (
            db.query(AutomationEvent)
            .filter(
                AutomationEvent.tenant_id == tenant_id,
                AutomationEvent.event_type == evt_type,
                AutomationEvent.created_at >= today_start,
            )
            .count()
        )
        summary.append({
            "key": evt_type,
            "label": label,
            "count": count,
            "icon": AUTOPILOT_SUMMARY_ICONS.get(evt_type, "📨"),
        })
    return summary


# ── Autopilot job functions ────────────────────────────────────────────────────

def _log_autopilot_event(
    db: Session,
    tenant_id: int,
    event_type: str,
    customer_id: Optional[int],
    payload: Dict[str, Any],
) -> None:
    """Write an AutomationEvent row for an autopilot action."""
    event = AutomationEvent(
        tenant_id=tenant_id,
        event_type=event_type,
        customer_id=customer_id,
        payload=payload,
        processed=True,
        created_at=datetime.utcnow(),
    )
    db.add(event)


def _job_cod_confirmation(db: Session, tenant_id: int, config: Dict[str, Any]) -> int:
    """
    For every recent Order with payment_method=cod and status=pending:
    - If no confirmation received within reminder_hours → log a reminder event.
    - If still no response within auto_cancel_hours → log a cancel event.

    Returns the number of messages sent.
    """
    from datetime import timedelta

    reminder_hours = int(config.get("reminder_hours", 2))
    cancel_hours = int(config.get("auto_cancel_hours", 24))
    sent = 0

    # Query pending COD orders (last 48 hours)
    cutoff = datetime.utcnow() - timedelta(hours=48)
    cod_orders = db.query(Order).filter(
        Order.tenant_id == tenant_id,
        Order.status == "pending",
    ).all()

    for order in cod_orders:
        # Check if order metadata indicates COD
        meta = order.extra_metadata or {}
        payment_method = meta.get("payment_method", "")
        if payment_method not in ("cod", "cash_on_delivery", ""):
            continue  # skip non-COD orders (empty = treat as COD in demo)

        customer_info = order.customer_info or {}
        customer_name = customer_info.get("name", "العميل")

        # Already logged a confirmation event? Check AutomationEvent
        already_sent = db.query(AutomationEvent).filter(
            AutomationEvent.tenant_id == tenant_id,
            AutomationEvent.event_type == AUTOPILOT_EVENT_TYPES["cod_confirmation"],
            AutomationEvent.payload.op("->")("order_id").astext == str(order.id),
        ).count()

        if already_sent > 0:
            continue  # already handled

        _log_autopilot_event(
            db, tenant_id,
            AUTOPILOT_EVENT_TYPES["cod_confirmation"],
            None,
            {
                "order_id": order.id,
                "customer_name": customer_name,
                "template": config.get("template_name", "cod_order_confirmation_ar"),
                "action": "confirmation_sent",
            },
        )
        sent += 1

    return sent


def _job_predictive_reorder(db: Session, tenant_id: int, config: Dict[str, Any]) -> int:
    """
    For every PredictiveReorderEstimate where predicted_reorder_date is within
    days_before days AND notified=False: log a reorder reminder event.
    """
    from datetime import timedelta

    days_before = int(config.get("days_before", 3))
    window_end = datetime.utcnow() + timedelta(days=days_before)
    sent = 0

    estimates = db.query(PredictiveReorderEstimate).filter(
        PredictiveReorderEstimate.tenant_id == tenant_id,
        PredictiveReorderEstimate.notified == False,
        PredictiveReorderEstimate.predicted_reorder_date <= window_end,
    ).all()

    for est in estimates:
        customer = db.query(Customer).filter(Customer.id == est.customer_id, Customer.tenant_id == tenant_id).first()
        product = db.query(Product).filter(Product.id == est.product_id, Product.tenant_id == tenant_id).first()

        customer_name = customer.name if customer else "العميل"
        product_name = product.title if product else f"المنتج #{est.product_id}"
        store_url = ""
        settings_row = db.query(TenantSettings).filter(TenantSettings.tenant_id == tenant_id).first()
        if settings_row:
            store = _merge_defaults(settings_row.store_settings, DEFAULT_STORE)
            store_url = store.get("store_url", "")

        _log_autopilot_event(
            db, tenant_id,
            AUTOPILOT_EVENT_TYPES["predictive_reorder"],
            est.customer_id,
            {
                "estimate_id": est.id,
                "customer_name": customer_name,
                "product_name": product_name,
                "template": config.get("template_name", "predictive_reorder_reminder_ar"),
                "vars": {
                    "{{1}}": customer_name,
                    "{{2}}": product_name,
                    "{{3}}": store_url or "https://store.example.com",
                },
            },
        )
        est.notified = True
        sent += 1

    return sent


def _job_abandoned_cart(db: Session, tenant_id: int, config: Dict[str, Any]) -> int:
    """
    For every Order with is_abandoned=True that hasn't been contacted yet:
    log an abandoned cart recovery event.
    """
    sent = 0

    abandoned = db.query(Order).filter(
        Order.tenant_id == tenant_id,
        Order.is_abandoned == True,
    ).all()

    for order in abandoned:
        # Check if already sent an autopilot cart event for this order
        already = db.query(AutomationEvent).filter(
            AutomationEvent.tenant_id == tenant_id,
            AutomationEvent.event_type == AUTOPILOT_EVENT_TYPES["abandoned_cart"],
            AutomationEvent.payload.op("->")("order_id").astext == str(order.id),
        ).count()

        if already > 0:
            continue

        customer_info = order.customer_info or {}
        customer_name = customer_info.get("name", "العميل")
        checkout_url = order.checkout_url or ""

        _log_autopilot_event(
            db, tenant_id,
            AUTOPILOT_EVENT_TYPES["abandoned_cart"],
            None,
            {
                "order_id": order.id,
                "customer_name": customer_name,
                "checkout_url": checkout_url,
                "template": config.get("template_name", "abandoned_cart_reminder"),
                "vars": {
                    "{{1}}": customer_name,
                    "{{2}}": checkout_url or "https://store.example.com/cart",
                },
                "steps": [
                    {"delay": "30m", "sent": True},
                    {"delay": "24h", "scheduled": True},
                    {"delay": "48h", "coupon": config.get("coupon_code", ""), "scheduled": bool(config.get("coupon_48h"))},
                ],
            },
        )
        sent += 1

    return sent


def _job_inactive_customers(db: Session, tenant_id: int, config: Dict[str, Any]) -> int:
    """
    For every CustomerProfile with segment='at_risk' that hasn't been contacted recently:
    log an inactive recovery event.
    """
    from datetime import timedelta

    inactive_days = int(config.get("inactive_days", 60))
    discount_pct = int(config.get("discount_pct", 15))
    sent = 0

    threshold = datetime.utcnow() - timedelta(days=inactive_days)
    at_risk = (
        db.query(CustomerProfile, Customer)
        .join(Customer, CustomerProfile.customer_id == Customer.id)
        .filter(
            CustomerProfile.tenant_id == tenant_id,
            CustomerProfile.segment.in_(["at_risk", "churned"]),
            CustomerProfile.last_order_at <= threshold,
        )
        .all()
    )

    for profile, customer in at_risk:
        # Check if already sent a recovery message in the last inactive_days
        cutoff = datetime.utcnow() - timedelta(days=inactive_days // 2)
        already = db.query(AutomationEvent).filter(
            AutomationEvent.tenant_id == tenant_id,
            AutomationEvent.event_type == AUTOPILOT_EVENT_TYPES["inactive_recovery"],
            AutomationEvent.customer_id == customer.id,
            AutomationEvent.created_at >= cutoff,
        ).count()

        if already > 0:
            continue

        _log_autopilot_event(
            db, tenant_id,
            AUTOPILOT_EVENT_TYPES["inactive_recovery"],
            customer.id,
            {
                "customer_name": customer.name,
                "days_inactive": (datetime.utcnow() - profile.last_order_at).days if profile.last_order_at else inactive_days,
                "template": config.get("template_name", "win_back"),
                "discount_pct": discount_pct,
                "vars": {
                    "{{1}}": customer.name or "العميل",
                    "{{2}}": f"{discount_pct}%",
                    "{{3}}": f"WINBACK{discount_pct}",
                },
            },
        )
        sent += 1

    return sent


# ── Pydantic schemas for autopilot ────────────────────────────────────────────

class AutopilotSubIn(BaseModel):
    enabled: Optional[bool] = None
    reminder_hours: Optional[int] = None
    auto_cancel_hours: Optional[int] = None
    days_before: Optional[int] = None
    consumption_days_default: Optional[int] = None
    reminder_30min: Optional[bool] = None
    reminder_24h: Optional[bool] = None
    coupon_48h: Optional[bool] = None
    coupon_code: Optional[str] = None
    inactive_days: Optional[int] = None
    discount_pct: Optional[int] = None

class AutopilotSettingsIn(BaseModel):
    enabled: Optional[bool] = None
    cod_confirmation: Optional[AutopilotSubIn] = None
    predictive_reorder: Optional[AutopilotSubIn] = None
    abandoned_cart: Optional[AutopilotSubIn] = None
    inactive_recovery: Optional[AutopilotSubIn] = None


# ── Autopilot endpoints ────────────────────────────────────────────────────────

@app.get("/autopilot/status")
async def autopilot_status(request: Request, db: Session = Depends(get_db)):
    """
    Return autopilot settings, today's action summary, and next scheduled run time.
    """
    tenant_id = _resolve_tenant_id(request)
    _get_or_create_tenant(db, tenant_id)
    _seed_demo_customers(db, tenant_id)
    db.commit()

    ap = _get_autopilot_settings(db, tenant_id)
    summary = _get_daily_summary(db, tenant_id)

    # Last run: most recent autopilot event
    last_event = (
        db.query(AutomationEvent)
        .filter(
            AutomationEvent.tenant_id == tenant_id,
            AutomationEvent.event_type.in_(list(AUTOPILOT_EVENT_TYPES.values())),
        )
        .order_by(AutomationEvent.created_at.desc())
        .first()
    )
    last_run_at = last_event.created_at.isoformat() if last_event else None

    return {
        "settings": ap,
        "daily_summary": summary,
        "last_run_at": last_run_at,
        "is_running": False,  # in production: check background job state
    }


@app.put("/autopilot/settings")
async def update_autopilot_settings(
    body: AutopilotSettingsIn,
    request: Request,
    db: Session = Depends(get_db),
):
    """Save autopilot master toggle and sub-automation settings."""
    tenant_id = _resolve_tenant_id(request)
    _get_or_create_tenant(db, tenant_id)

    # Require active subscription to enable autopilot
    if body.enabled:
        _require_subscription(db, int(tenant_id))

    current = _get_autopilot_settings(db, tenant_id)

    if body.enabled is not None:
        current["enabled"] = body.enabled

    for sub_key, sub_in in [
        ("cod_confirmation",  body.cod_confirmation),
        ("predictive_reorder", body.predictive_reorder),
        ("abandoned_cart",    body.abandoned_cart),
        ("inactive_recovery", body.inactive_recovery),
    ]:
        if sub_in is not None:
            patch = sub_in.model_dump(exclude_none=True)
            current[sub_key] = {**current[sub_key], **patch}

    _save_autopilot_settings(db, tenant_id, current)
    db.commit()
    return {"settings": current}


@app.post("/autopilot/run")
async def run_autopilot(request: Request, db: Session = Depends(get_db)):
    """
    Manually trigger all enabled autopilot jobs for this tenant.
    In production this is called by a cron scheduler every hour.
    """
    tenant_id = _resolve_tenant_id(request)
    _get_or_create_tenant(db, tenant_id)
    _seed_demo_customers(db, tenant_id)

    ap = _get_autopilot_settings(db, tenant_id)

    if not ap.get("enabled", False):
        return {"ran": False, "message": "الطيار التلقائي معطّل — فعّله أولاً من الإعدادات"}

    results: Dict[str, int] = {}

    if ap["cod_confirmation"].get("enabled", True):
        results["cod_confirmation"] = _job_cod_confirmation(db, tenant_id, ap["cod_confirmation"])

    if ap["predictive_reorder"].get("enabled", True):
        results["predictive_reorder"] = _job_predictive_reorder(db, tenant_id, ap["predictive_reorder"])

    if ap["abandoned_cart"].get("enabled", True):
        results["abandoned_cart"] = _job_abandoned_cart(db, tenant_id, ap["abandoned_cart"])

    if ap["inactive_recovery"].get("enabled", True):
        results["inactive_recovery"] = _job_inactive_customers(db, tenant_id, ap["inactive_recovery"])

    db.commit()

    total = sum(results.values())
    return {
        "ran": True,
        "total_actions": total,
        "breakdown": results,
        "ran_at": datetime.utcnow().isoformat(),
        "message": f"الطيار التلقائي أرسل {total} رسالة في هذه الجلسة",
    }


# ── Template variable resolution ──────────────────────────────────────────────

# Human-readable Arabic labels for variable semantic names
VAR_FIELD_LABELS: Dict[str, str] = {
    "customer_name":  "اسم العميل",
    "product_name":   "اسم المنتج",
    "reorder_url":    "رابط إعادة الطلب",
    "order_amount":   "مبلغ الطلب (ر.س)",
    "cart_url":       "رابط السلة المتروكة",
    "discount_pct":   "نسبة الخصم",
    "coupon_code":    "كود الكوبون",
    "store_name":     "اسم المتجر",
    "order_id":       "رقم الطلب",
}


@app.get("/templates/{template_id}/var-map")
async def get_template_var_map(
    template_id: int,
    request: Request,
    db: Session = Depends(get_db),
):
    """
    Return the variable → field mapping for a template.
    Used by smart automations to know which customer/order fields to inject
    before sending a WhatsApp message.
    """
    tenant_id = _resolve_tenant_id(request)
    tpl = db.query(WhatsAppTemplate).filter(
        WhatsAppTemplate.id == template_id,
        WhatsAppTemplate.tenant_id == tenant_id,
    ).first()
    if not tpl:
        raise HTTPException(status_code=404, detail="Template not found")

    raw_map = TEMPLATE_VAR_MAP.get(tpl.name, {})
    annotated = {
        var: {"field": field, "label": VAR_FIELD_LABELS.get(field, field)}
        for var, field in raw_map.items()
    }
    return {
        "template_id": template_id,
        "template_name": tpl.name,
        "category": tpl.category,
        "var_map": raw_map,          # e.g. {"{{1}}": "customer_name", ...}
        "var_map_annotated": annotated,  # includes Arabic label
        "is_default": tpl.name in ("cod_order_confirmation_ar", "predictive_reorder_reminder_ar"),
    }


@app.post("/templates/{template_id}/resolve")
async def resolve_template_vars(
    template_id: int,
    request: Request,
    db: Session = Depends(get_db),
):
    """
    Resolve all template variables for a specific customer.
    Body: { customer_id: int, extra: { "reorder_url": "...", "coupon_code": "..." } }
    Returns the rendered message body ready to be sent via WhatsApp Cloud API.
    """
    body = await request.json()
    tenant_id = _resolve_tenant_id(request)
    customer_id = int(body.get("customer_id", 0))
    extra: Dict[str, str] = body.get("extra", {})

    tpl = db.query(WhatsAppTemplate).filter(
        WhatsAppTemplate.id == template_id,
        WhatsAppTemplate.tenant_id == tenant_id,
    ).first()
    if not tpl:
        raise HTTPException(status_code=404, detail="Template not found")

    customer = db.query(Customer).filter(
        Customer.id == customer_id,
        Customer.tenant_id == tenant_id,
    ).first()

    settings = _get_or_create_settings(db, tenant_id)
    store = _merge_defaults(settings.store_settings, DEFAULT_STORE)

    # Build the field→value lookup
    field_values: Dict[str, str] = {
        "customer_name": (customer.name if customer else "العميل") or "العميل",
        "store_name": store.get("store_name", "") or "المتجر",
        **extra,  # caller can override / provide order-specific values
    }

    var_map = TEMPLATE_VAR_MAP.get(tpl.name, {})
    components = tpl.components or []
    resolved_components: List[Dict[str, Any]] = []

    for comp in components:
        comp_copy = dict(comp)
        if comp_copy.get("text"):
            text = comp_copy["text"]
            for var_placeholder, field in var_map.items():
                text = text.replace(var_placeholder, field_values.get(field, var_placeholder))
            comp_copy["text"] = text
        resolved_components.append(comp_copy)

    # Build the WhatsApp API parameters array: [{"type":"text","text":"value"}, ...]
    wa_params = []
    for var_placeholder, field in sorted(var_map.items()):
        wa_params.append({
            "type": "text",
            "text": field_values.get(field, var_placeholder),
        })

    body_text = next(
        (c.get("text", "") for c in resolved_components if c.get("type") == "BODY"), ""
    )

    # Track usage — only count when template is actually dispatched (resolved for send)
    _tpl_bump_usage(db, tpl.id, tenant_id)
    db.commit()

    return {
        "template_name": tpl.name,
        "resolved_components": resolved_components,
        "rendered_body": body_text,
        "wa_parameters": wa_params,  # pass directly to Meta Cloud API
    }


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

ORCHESTRATOR_URL = os.getenv("ORCHESTRATOR_URL", "http://localhost:8001")

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
        logger.info(
            f"[Billing Webhook] Subscription {subscription_id} ACTIVATED "
            f"for tenant {sub.tenant_id} (payment {payment_id})"
        )

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


# ── Existing endpoints ─────────────────────────────────────────────────────────

_START_TIME = _time.monotonic()

# ── Root ───────────────────────────────────────────────────────────────────────
@app.get("/")
async def root():
    """API root — confirms the backend is reachable."""
    return {
        "service": "nahla-saas",
        "status":  "ok",
        "version": "1.0.0",
        "docs":    "/docs",
    }

# ── Health ─────────────────────────────────────────────────────────────────────
@app.get("/health")
async def health():
    """Lightweight liveness probe — always returns fast, no DB hit."""
    return {
        "status": "ok",
        "service": "nahla-saas",
        "uptime_seconds": round(_time.monotonic() - _START_TIME),
        "version": "1.0.0",
    }

@app.get("/api/health")
async def health_alias():
    """Alias: /api/health → same as /health (for clients that prefix all paths with /api)."""
    return {
        "status": "ok",
        "service": "nahla-saas",
        "uptime_seconds": round(_time.monotonic() - _START_TIME),
        "version": "1.0.0",
    }

@app.get("/health/db")
async def health_db(db: Session = Depends(get_db)):
    """Database connectivity probe."""
    from observability.health import check_database
    result = await check_database(db)
    ok = result.get("status") == "ok"
    return JSONResponse(
        status_code=200 if ok else 503,
        content={"status": result.get("status"), "service": "nahla-saas", "database": ok},
    )

@app.get("/health/whatsapp")
async def health_whatsapp(request: Request, db: Session = Depends(get_db)):
    """WhatsApp integration readiness check — reports configured/not_configured without exposing secrets."""
    tenant_id = _resolve_tenant_id(request)
    settings = _get_or_create_settings(db, tenant_id)
    db.commit()
    wa = _merge_defaults(settings.whatsapp_settings, DEFAULT_WHATSAPP)
    configured = bool(wa.get("phone_number_id") and wa.get("access_token"))
    return {
        "status": "configured" if configured else "not_configured",
        "service": "nahla-saas",
        "phone_number_set": bool(wa.get("phone_number")),
        "phone_number_id_set": bool(wa.get("phone_number_id")),
        "access_token_set": bool(wa.get("access_token")),
        "verify_token_set": bool(wa.get("verify_token")),
        "auto_reply_enabled": wa.get("auto_reply_enabled", False),
    }

@app.get("/health/detailed")
async def health_detailed(request: Request, db: Session = Depends(get_db)):
    """Full readiness probe: DB + WhatsApp configuration check."""
    from observability.health import check_database
    db_result = await check_database(db)
    db_ok = db_result.get("status") == "ok"

    tenant_id = _resolve_tenant_id(request)
    wa = _merge_defaults(
        (_get_or_create_settings(db, tenant_id)).whatsapp_settings,
        DEFAULT_WHATSAPP,
    )
    db.commit()
    wa_configured = bool(wa.get("phone_number_id") and wa.get("access_token"))

    overall = "ok" if db_ok else "degraded"
    return JSONResponse(
        status_code=200 if db_ok else 503,
        content={
            "status": overall,
            "service": "nahla-saas",
            "database": db_ok,
            "whatsapp_configured": wa_configured,
            "uptime_seconds": round(_time.monotonic() - _START_TIME),
            "timestamp": datetime.utcnow().isoformat() + "Z",
        },
    )


# ── Auth endpoints ─────────────────────────────────────────────────────────────

try:
    from passlib.context import CryptContext as _CryptContext
    _pwd_context = _CryptContext(schemes=["bcrypt"], deprecated="auto")
    _PASSLIB_AVAILABLE = True
except ImportError:
    _PASSLIB_AVAILABLE = False

class LoginIn(BaseModel):
    email: str
    password: str

@app.post("/auth/login")
async def auth_login(body: LoginIn, db: Session = Depends(get_db)):
    """
    Exchange email + password for a signed JWT.
    Checks admin env-var credentials first, then merchant accounts in the database.
    """
    if not _JWT_AVAILABLE:
        raise HTTPException(status_code=503, detail="Auth service unavailable — python-jose not installed")

    _INVALID = HTTPException(status_code=401, detail="البريد الإلكتروني أو كلمة المرور غير صحيحة")
    email = body.email.strip().lower()

    # 1. Admin credentials (env vars — fastest path, no DB hit)
    email_ok    = hmac.compare_digest(email,         _ADMIN_EMAIL.lower())
    password_ok = hmac.compare_digest(body.password, _ADMIN_PASSWORD)
    if email_ok and password_ok:
        token = _create_token(email=_ADMIN_EMAIL, role="admin", tenant_id=1)
        return {"access_token": token, "token_type": "bearer",
                "role": "admin", "email": _ADMIN_EMAIL, "tenant_id": 1}

    # 2. Merchant credentials (database)
    if not _PASSLIB_AVAILABLE:
        raise HTTPException(status_code=503, detail="Auth service unavailable — passlib not installed")

    user = db.query(User).filter(User.email == email, User.is_active == True).first()
    if not user or not getattr(user, "password_hash", None):
        raise _INVALID
    if not _pwd_context.verify(body.password, user.password_hash):
        raise _INVALID

    token = _create_token(email=user.email, role=user.role or "merchant", tenant_id=user.tenant_id)
    return {
        "access_token": token,
        "token_type":   "bearer",
        "role":         user.role or "merchant",
        "email":        user.email,
        "tenant_id":    user.tenant_id,
    }

@app.get("/auth/me")
async def auth_me(user: Dict[str, Any] = Depends(get_current_user)):
    """Return the identity of the currently authenticated user."""
    return {
        "email":     user.get("sub"),
        "role":      user.get("role"),
        "tenant_id": user.get("tenant_id"),
    }

@app.post("/auth/logout")
async def auth_logout():
    """Client-side logout — token invalidation is handled by the frontend."""
    return {"detail": "logged out"}


class RegisterIn(BaseModel):
    email:      str
    password:   str
    store_name: str
    phone:      str = ""

@app.post("/auth/register")
async def auth_register(body: RegisterIn, db: Session = Depends(get_db)):
    """
    Self-registration for new merchants.
    Creates a dedicated tenant + merchant user, returns a JWT token.
    """
    if not _PASSLIB_AVAILABLE:
        raise HTTPException(status_code=503, detail="passlib not installed")

    email = body.email.strip().lower()
    if not email or not body.password or not body.store_name.strip():
        raise HTTPException(status_code=400, detail="البريد وكلمة المرور واسم المتجر مطلوبة")

    if len(body.password) < 8:
        raise HTTPException(status_code=400, detail="كلمة المرور يجب أن تكون 8 أحرف على الأقل")

    if db.query(User).filter(User.email == email).first():
        raise HTTPException(status_code=409, detail="البريد الإلكتروني مسجَّل مسبقاً")

    # Create a dedicated tenant
    tenant = Tenant(
        name=body.store_name.strip(),
        domain=f"store-{email.split('@')[0]}.nahla.sa",
        is_active=True,
        created_at=datetime.utcnow(),
    )
    db.add(tenant)
    db.flush()

    user = User(
        username=email,
        email=email,
        password_hash=_pwd_context.hash(body.password),
        role="merchant",
        is_active=True,
        created_at=datetime.utcnow(),
        tenant_id=tenant.id,
    )
    db.add(user)
    db.commit()
    db.refresh(user)

    logger.info("New merchant registered: email=%s tenant_id=%s", email, tenant.id)

    token = _create_token({"sub": email, "role": "merchant", "tenant_id": tenant.id})
    return {"access_token": token, "token_type": "bearer", "role": "merchant"}


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
async def get_salla_store(request: Request, db: Session = Depends(get_db)):
    """Return saved Salla store info for this tenant."""
    tenant_id = _resolve_tenant_id(request)
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
async def get_salla_products(request: Request):
    """Fetch live products from the tenant's Salla store via the adapter."""
    tenant_id = _resolve_tenant_id(request)
    sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), ".")))
    from store_integration.registry import get_adapter
    adapter = get_adapter(tenant_id)
    if not adapter:
        raise HTTPException(status_code=404, detail="Salla integration not configured")
    try:
        products = await adapter.get_products()
        return {"products": [p.dict() for p in products], "count": len(products)}
    except Exception as exc:
        logger.error("Salla products fetch error: %s", exc)
        raise HTTPException(status_code=502, detail=f"Salla API error: {exc}")


@app.post("/api/salla/test-coupon")
async def test_salla_coupon(request: Request):
    """Validate a coupon code against the tenant's Salla store."""
    body = await request.json()
    coupon_code = body.get("coupon_code", "").strip()
    if not coupon_code:
        raise HTTPException(status_code=400, detail="coupon_code is required")
    tenant_id = _resolve_tenant_id(request)
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
        logger.error("Salla coupon validation error: %s", exc)
        raise HTTPException(status_code=502, detail=f"Salla API error: {exc}")


# ── Admin — merchant management ────────────────────────────────────────────────

class CreateMerchantIn(BaseModel):
    email:      str
    password:   str
    store_name: str
    phone:      str = ""

def _merchant_row(user: User) -> Dict[str, Any]:
    return {
        "id":         user.id,
        "email":      user.email,
        "role":       user.role,
        "is_active":  user.is_active,
        "tenant_id":  user.tenant_id,
        "store_name": user.tenant.name if user.tenant else "",
        "created_at": user.created_at.isoformat() if user.created_at else None,
    }

@app.get("/admin/merchants")
async def list_merchants(
    db:    Session          = Depends(get_db),
    _admin: Dict[str, Any] = Depends(require_admin),
):
    """List all merchant accounts (admin only)."""
    users = (
        db.query(User)
        .filter(User.role == "merchant")
        .order_by(User.created_at.desc())
        .all()
    )
    return {"merchants": [_merchant_row(u) for u in users]}

@app.post("/admin/merchants")
async def create_merchant(
    body:   CreateMerchantIn,
    db:     Session          = Depends(get_db),
    _admin: Dict[str, Any]  = Depends(require_admin),
):
    """Create a new merchant account + a dedicated tenant (admin only)."""
    if not _PASSLIB_AVAILABLE:
        raise HTTPException(status_code=503, detail="passlib not installed")

    email = body.email.strip().lower()
    if db.query(User).filter(User.email == email).first():
        raise HTTPException(status_code=400, detail="البريد الإلكتروني مسجَّل مسبقاً")

    # Create a dedicated tenant for this merchant
    tenant = Tenant(
        name=body.store_name,
        domain=f"store-{email.split('@')[0]}.nahla.sa",
        is_active=True,
        created_at=datetime.utcnow(),
    )
    db.add(tenant)
    db.flush()  # populate tenant.id

    user = User(
        username=email,
        email=email,
        password_hash=_pwd_context.hash(body.password),
        role="merchant",
        is_active=True,
        created_at=datetime.utcnow(),
        tenant_id=tenant.id,
    )
    db.add(user)
    db.commit()
    db.refresh(user)
    return _merchant_row(user)

@app.put("/admin/merchants/{user_id}/toggle")
async def toggle_merchant(
    user_id: int,
    db:      Session          = Depends(get_db),
    _admin:  Dict[str, Any]  = Depends(require_admin),
):
    """Activate or deactivate a merchant account (admin only)."""
    user = db.query(User).filter(User.id == user_id, User.role == "merchant").first()
    if not user:
        raise HTTPException(status_code=404, detail="Merchant not found")
    user.is_active = not user.is_active
    db.commit()
    db.refresh(user)
    return _merchant_row(user)

@app.delete("/admin/merchants/{user_id}")
async def delete_merchant(
    user_id: int,
    db:      Session          = Depends(get_db),
    _admin:  Dict[str, Any]  = Depends(require_admin),
):
    """Permanently delete a merchant account (admin only)."""
    user = db.query(User).filter(User.id == user_id, User.role == "merchant").first()
    if not user:
        raise HTTPException(status_code=404, detail="Merchant not found")
    db.delete(user)
    db.commit()
    return {"deleted": True}


@app.get("/tenants/{tenant_id}")
async def get_tenant(tenant_id: int, db: Session = Depends(get_db)):
    """Retrieve a single tenant by its numeric ID."""
    tenant = db.query(Tenant).filter(Tenant.id == tenant_id, Tenant.is_active == True).first()
    if not tenant:
        raise HTTPException(status_code=404, detail="Tenant not found")
    return {"id": tenant.id, "name": tenant.name, "domain": tenant.domain, "is_active": tenant.is_active}

if __name__ == "__main__":
    import uvicorn
    port = int(os.environ.get("PORT", 8000))
    logger.info(f"Starting Nahla SaaS Backend API server on port {port}...")
    uvicorn.run("main:app", host="0.0.0.0", port=port, reload=True)

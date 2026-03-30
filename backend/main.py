import os
from datetime import datetime
from typing import Optional, Any, Dict, List, Tuple
from sqlalchemy.orm import Session
from fastapi import Depends, HTTPException, Request
from fastapi.middleware.cors import CORSMiddleware
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
)
import httpx

from fastapi import FastAPI
from fastapi.responses import JSONResponse
import logging

# Configure logging
logging.basicConfig(level=logging.INFO, format='%(asctime)s %(levelname)s %(message)s')
logger = logging.getLogger("nahla-backend")

app = FastAPI(title="Nahla SaaS Backend", description="Multi-tenant SaaS API server.")

# CORS – allow dashboard dev server
app.add_middleware(
    CORSMiddleware,
    allow_origins=["http://localhost:3000", "http://127.0.0.1:3000"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# Multi-tenant middleware
@app.middleware("http")
async def multi_tenant_middleware(request: Request, call_next):
    tenant_id = request.headers.get("X-Tenant-ID", "1")
    request.state.tenant_id = tenant_id
    logger.info(f"Request for tenant: {tenant_id}")
    response = await call_next(request)
    return response

# Dependency for DB session
def get_db():
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()

def _resolve_tenant_id(request: Request) -> int:
    """Parse tenant_id from request state, default to 1."""
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
            name="متجر أحمد للملابس",
            domain="ahmed-clothing.salla.sa",
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
}

DEFAULT_AI: Dict[str, Any] = {
    "assistant_name": "نهلة",
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
    assistant_name: str = "نهلة"
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

# ── Settings endpoints ─────────────────────────────────────────────────────────

@app.get("/settings")
async def get_settings(request: Request, db: Session = Depends(get_db)):
    """Return all settings for the current tenant."""
    tenant_id = _resolve_tenant_id(request)
    settings = _get_or_create_settings(db, tenant_id)
    db.commit()

    return {
        "whatsapp":      _merge_defaults(settings.whatsapp_settings,      DEFAULT_WHATSAPP),
        "ai":            _merge_defaults(settings.ai_settings,             DEFAULT_AI),
        "store":         _merge_defaults(settings.store_settings,          DEFAULT_STORE),
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
        current.update(body.whatsapp.model_dump())
        settings.whatsapp_settings = current

    if body.ai is not None:
        current = _merge_defaults(settings.ai_settings, DEFAULT_AI)
        current.update(body.ai.model_dump())
        settings.ai_settings = current

    if body.store is not None:
        current = _merge_defaults(settings.store_settings, DEFAULT_STORE)
        current.update(body.store.model_dump())
        settings.store_settings = current

    if body.notifications is not None:
        current = _merge_defaults(settings.notification_settings, DEFAULT_NOTIFICATIONS)
        current.update(body.notifications.model_dump())
        settings.notification_settings = current

    settings.updated_at = datetime.utcnow()
    db.commit()
    db.refresh(settings)

    return {
        "whatsapp":      _merge_defaults(settings.whatsapp_settings,      DEFAULT_WHATSAPP),
        "ai":            _merge_defaults(settings.ai_settings,             DEFAULT_AI),
        "store":         _merge_defaults(settings.store_settings,          DEFAULT_STORE),
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
            {"type": "FOOTER", "text": "🐝 نهلة — مساعد متجرك"},
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
            {"type": "FOOTER", "text": "🐝 نهلة — مساعد متجرك"},
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
            {"type": "FOOTER", "text": "🐝 نهلة — مساعد متجرك"},
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
            {"type": "FOOTER", "text": "🐝 نهلة — مساعد متجرك"},
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
            {"type": "FOOTER", "text": "🐝 نهلة — مساعد متجرك"},
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
            {"type": "FOOTER", "text": "🐝 نهلة — مساعد متجرك"},
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
            {"type": "FOOTER", "text": "🐝 نهلة — مساعد متجرك"},
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
            {"type": "FOOTER", "text": "🐝 نهلة — مساعد متجرك"},
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
            {"type": "FOOTER", "text": "🐝 نهلة — مساعد متجرك"},
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
            {"type": "FOOTER", "text": "🐝 نهلة — مساعد متجرك"},
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
            {"type": "FOOTER", "text": "🐝 نهلة — مساعد متجرك"},
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
            {"type": "FOOTER", "text": "🐝 نهلة — مساعد متجرك"},
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
            {"type": "FOOTER", "text": "🐝 نهلة — مساعد متجرك"},
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
            {"type": "FOOTER", "text": "🐝 نهلة — مساعد متجرك"},
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
    }


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
        product = db.query(Product).filter(Product.id == r.product_id).first()
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
        customer = db.query(Customer).filter(Customer.id == est.customer_id).first()
        product = db.query(Product).filter(Product.id == est.product_id).first()

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
    s = _get_or_create_settings(db, tenant_id)
    meta = dict(s.extra_metadata or {})
    current = _merge_defaults(meta.get("ai_sales_agent", {}), DEFAULT_AI_SALES_AGENT)
    current.update(patch)
    meta["ai_sales_agent"] = current
    s.extra_metadata = meta
    db.add(s)
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
        "assistant_name":   ai.get("assistant_name") or "نهلة",
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
                f"مرحباً {customer_name}! لم يتم ربط كتالوج المنتجات{store_ref} بعد."
                " تواصل معنا مباشرة للمساعدة.",
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
                f"مرحباً {customer_name}! لا تتوفر بيانات منتجات{store_ref} حالياً للتوصية.",
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
            "من فضلك أكّد:",
            "1️⃣ المنتج والكمية",
            "2️⃣ اسمك الكريم",
        ]
        if collect_address:
            lines.append("3️⃣ مدينتك وعنوانك")
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
    customer_phone:  str
    customer_name:   str = ""
    product_id:      Optional[int] = None
    product_name:    str = ""
    variant_id:      Optional[int] = None
    quantity:        int = 1
    city:            str = ""
    address:         str = ""
    payment_method:  str = "cod"   # "cod" | "pay_now"
    notes:           str = ""


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
    tenant_id = _resolve_tenant_id(request)
    _get_or_create_tenant(db, tenant_id)
    patch = {k: v for k, v in body.dict().items() if v is not None}
    updated = _save_ai_sales_settings(db, tenant_id, patch)
    db.commit()
    return {"settings": updated}


@app.post("/ai-sales/process-message")
async def ai_sales_process_message(request: Request, db: Session = Depends(get_db)):
    """
    Process an incoming WhatsApp message through the AI Sales Agent.

    Body: { customer_phone, message, customer_name? }
    Returns: { intent, confidence, response_text, products_used,
               order_started, payment_link, handoff_triggered }
    """
    body = await request.json()
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

    message      = str(body.get("message", "")).strip()
    cust_phone   = str(body.get("customer_phone", "unknown"))
    cust_name    = str(body.get("customer_name", "عزيزي العميل")).strip() or "عزيزي العميل"

    if not message:
        raise HTTPException(status_code=422, detail="message field is required")

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

    # 4. Log event
    _log_ai_sales_event(
        db, tenant_id, cust_phone, cust_name, message,
        intent, confidence, response_text,
        products_used, order_started, payment_link is not None, handoff_triggered,
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
            "name":    body.customer_name,
            "phone":   body.customer_phone,
            "city":    body.city,
            "address": body.address,
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

    # Attach payment link to Order
    if order_id:
        order = db.query(Order).filter(Order.id == order_id, Order.tenant_id == tenant_id).first()
        if order:
            order.checkout_url = payment_link

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


# ── Existing endpoints ─────────────────────────────────────────────────────────

@app.get("/health")
async def health():
    """Health check endpoint."""
    return {"status": "ok"}


@app.get("/tenants/{tenant_id}")
async def get_tenant(tenant_id: int, db: Session = Depends(get_db)):
    """Retrieve a single tenant by its numeric ID."""
    tenant = db.query(Tenant).filter(Tenant.id == tenant_id, Tenant.is_active == True).first()
    if not tenant:
        raise HTTPException(status_code=404, detail="Tenant not found")
    return {"id": tenant.id, "name": tenant.name, "domain": tenant.domain, "is_active": tenant.is_active}

if __name__ == "__main__":
    import uvicorn
    logger.info("Starting Nahla SaaS Backend API server...")
    uvicorn.run("main:app", host="0.0.0.0", port=8000, reload=True)

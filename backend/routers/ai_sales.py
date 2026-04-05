"""
routers/ai_sales.py
────────────────────
AI Sales Agent endpoints.

Routes
  GET  /ai-sales/settings
  PUT  /ai-sales/settings
  POST /ai-sales/process-message
  POST /ai-sales/create-order
  GET  /ai-sales/logs
"""
from __future__ import annotations

import logging
import os
import time as _time
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional, Tuple

import httpx
from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel
from sqlalchemy.orm import Session

# ── Path setup ────────────────────────────────────────────────────────────────
from models import (  # noqa: E402
    AutomationEvent,
    Customer,
    Order,
    Product,
)

from core.database import get_db
from core.tenant import (
    DEFAULT_AI,
    DEFAULT_STORE,
    get_or_create_settings,
    get_or_create_tenant,
    merge_defaults,
    resolve_tenant_id,
)
from core.billing import get_moyasar_settings
from core.config import ORCHESTRATOR_URL
from core.middleware import rate_limit

logger = logging.getLogger("nahla-backend")

router = APIRouter(prefix="/ai-sales", tags=["AI Sales Agent"])

# ── Default configs ────────────────────────────────────────────────────────────

DEFAULT_AI_SALES_AGENT: Dict[str, Any] = {
    "enable_ai_sales_agent": False,
    "allow_product_recommendations": True,
    "allow_order_creation": True,
    "allow_address_collection": True,
    "allow_payment_link_sending": True,
    "allow_cod_confirmation_flow": True,
    "allow_human_handoff": True,
    "confidence_threshold": 0.55,
    "handoff_phrases": [
        "موظف", "بشري", "انسان", "تكلم مع شخص",
        "تواصل مع الدعم", "شخص حقيقي",
    ],
}

# ── Intent definitions ─────────────────────────────────────────────────────────

AI_SALES_INTENTS: Dict[str, Dict[str, Any]] = {
    "ask_product": {
        "label": "استفسار عن منتج",
        "keywords": ["منتج", "عندكم", "يوجد", "متوفر", "ما هو", "ما هي",
                     "اريد اعرف", "أريد أعرف", "show me", "do you have"],
        "response_type": "product_info",
        "emoji": "📦",
    },
    "ask_price": {
        "label": "استفسار عن السعر",
        "keywords": ["سعر", "بكم", "كم", "تكلف", "يكلف", "ثمن", "سعره",
                     "سعرها", "price", "cost", "how much"],
        "response_type": "price_info",
        "emoji": "💰",
    },
    "ask_recommendation": {
        "label": "طلب توصية",
        "keywords": ["وصّيني", "اوصيني", "انصحني", "انصحيني", "ايش تنصح",
                     "ماذا تنصح", "ايش تقترح", "اقترح", "recommend", "suggest"],
        "response_type": "recommendation",
        "emoji": "⭐",
    },
    "ask_shipping": {
        "label": "استفسار عن الشحن",
        "keywords": ["شحن", "توصيل", "كم يوم", "متى يوصل", "رسوم",
                     "مجاني", "سريع", "delivery", "shipping"],
        "response_type": "shipping_info",
        "emoji": "🚚",
    },
    "ask_offer": {
        "label": "استفسار عن العروض",
        "keywords": ["عرض", "خصم", "تخفيض", "عروض", "تنزيل",
                     "كوبون", "كود", "offer", "discount", "coupon"],
        "response_type": "offer_info",
        "emoji": "🏷️",
    },
    "order_product": {
        "label": "طلب شراء منتج",
        "keywords": ["ابي", "أبي", "اطلب", "أطلب", "اشتري", "أشتري",
                     "ابغى", "أبغى", "اريد اطلب", "بدي", "عايز",
                     "order", "buy", "purchase"],
        "response_type": "start_order_flow",
        "emoji": "🛍️",
    },
    "pay_now": {
        "label": "الدفع الإلكتروني",
        "keywords": ["ادفع", "دفع", "فيزا", "بطاقة", "اون لاين",
                     "اونلاين", "الكتروني", "مدى", "pay", "visa", "card", "online"],
        "response_type": "payment_link",
        "emoji": "💳",
    },
    "cash_on_delivery": {
        "label": "الدفع عند الاستلام",
        "keywords": ["كاش", "نقد", "عند الاستلام", "cod", "دفع عند",
                     "عند الوصول", "نقدي"],
        "response_type": "cod_flow",
        "emoji": "💵",
    },
    "track_order": {
        "label": "تتبع الطلب",
        "keywords": ["تتبع", "وين طلبي", "طلبي", "وصل", "موعد استلام",
                     "رقم طلب", "track", "order status", "where is"],
        "response_type": "order_tracking",
        "emoji": "📍",
    },
    "talk_to_human": {
        "label": "التحدث مع موظف",
        "keywords": ["موظف", "بشري", "انسان", "تكلم مع", "تواصل مع",
                     "شخص حقيقي", "support", "دعم", "مساعدة بشرية"],
        "response_type": "human_handoff",
        "emoji": "👤",
    },
}


# ── Pydantic models ───────────────────────────────────────────────────────────

class AiSalesProcessMessageIn(BaseModel):
    customer_phone: str
    message: str
    customer_name: Optional[str] = None


class AiSalesSettingsIn(BaseModel):
    enable_ai_sales_agent:         Optional[bool]  = None
    allow_product_recommendations: Optional[bool]  = None
    allow_order_creation:          Optional[bool]  = None
    allow_address_collection:      Optional[bool]  = None
    allow_payment_link_sending:    Optional[bool]  = None
    allow_cod_confirmation_flow:   Optional[bool]  = None
    allow_human_handoff:           Optional[bool]  = None
    confidence_threshold:          Optional[float] = None


class AiSalesCreateOrderIn(BaseModel):
    customer_phone:  str
    customer_name:   str  = ""
    product_id:      Optional[int] = None
    product_name:    str  = ""
    variant_id:      Optional[int] = None
    quantity:        int  = 1
    building_number: str  = ""
    street:          str  = ""
    district:        str  = ""
    postal_code:     str  = ""
    city:            str  = ""
    address:         str  = ""
    payment_method:  str  = "cod"
    notes:           str  = ""


# ── Helper functions ──────────────────────────────────────────────────────────

def _get_ai_sales_settings(db: Session, tenant_id: int) -> Dict[str, Any]:
    s = get_or_create_settings(db, tenant_id)
    meta = s.extra_metadata or {}
    return merge_defaults(meta.get("ai_sales_agent", {}), DEFAULT_AI_SALES_AGENT)


def _save_ai_sales_settings(db: Session, tenant_id: int, patch: Dict[str, Any]) -> Dict[str, Any]:
    from sqlalchemy.orm.attributes import flag_modified  # noqa: PLC0415
    s = get_or_create_settings(db, tenant_id)
    meta = dict(s.extra_metadata or {})
    current = merge_defaults(meta.get("ai_sales_agent", {}), DEFAULT_AI_SALES_AGENT)
    current.update(patch)
    meta["ai_sales_agent"] = current
    s.extra_metadata = meta
    s.updated_at = datetime.now(timezone.utc)
    flag_modified(s, "extra_metadata")
    return current


def _detect_intent(message: str, settings: Dict[str, Any]) -> Tuple[str, float, str]:
    """Keyword-based intent detection. Returns (intent_key, confidence, response_type)."""
    msg = message.lower()
    best_intent, best_score, best_response_type = "general", 0.0, "general"

    for phrase in settings.get("handoff_phrases", DEFAULT_AI_SALES_AGENT["handoff_phrases"]):
        if phrase.lower() in msg:
            return "talk_to_human", 0.95, "human_handoff"

    for intent_key, meta in AI_SALES_INTENTS.items():
        hits = sum(1 for kw in meta["keywords"] if kw.lower() in msg)
        if hits > 0:
            score = min(hits / max(len(meta["keywords"]) * 0.25, 1.0), 1.0)
            if score > best_score:
                best_score = score
                best_intent = intent_key
                best_response_type = meta["response_type"]

    if best_intent == "general":
        best_score = 0.15
    return best_intent, round(best_score, 2), best_response_type


async def _call_orchestrator(tenant_id: int, customer_phone: str, message: str) -> Optional[Dict[str, Any]]:
    """Route message through AI Orchestrator. Returns None if unavailable."""
    try:
        async with httpx.AsyncClient(timeout=30.0) as client:
            resp = await client.post(
                f"{ORCHESTRATOR_URL}/orchestrate",
                json={"tenant_id": tenant_id, "customer_phone": customer_phone, "message": message},
            )
            resp.raise_for_status()
            return resp.json()
    except Exception as exc:
        logger.warning(
            "[Orchestrator] Call failed for tenant=%s: %s — falling back to keyword engine",
            tenant_id, exc,
        )
        return None


def _build_response_context(db: Session, tenant_id: int) -> Dict[str, Any]:
    """Collect store-specific data needed to build dynamic AI Sales responses."""
    sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))
    from models import ShippingFee  # noqa: PLC0415
    s = get_or_create_settings(db, tenant_id)
    store = merge_defaults(s.store_settings, DEFAULT_STORE)
    ai    = merge_defaults(s.ai_settings, DEFAULT_AI)

    fees = db.query(ShippingFee).filter(ShippingFee.tenant_id == tenant_id).all()
    shipping_lines: List[str] = []
    for f in fees[:8]:
        label  = f.city or f.zone_name or "—"
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


async def _get_product_catalog(db: Session, tenant_id: int) -> List[Dict[str, Any]]:
    """Fetch product catalog from real store adapter or DB fallback."""
    sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))
    from store_integration.product_service import fetch_products, normalize_db_product  # noqa: PLC0415
    from store_integration.registry import get_adapter  # noqa: PLC0415

    adapter = get_adapter(tenant_id)
    if adapter:
        try:
            live_products = await fetch_products(tenant_id)
            if live_products:
                raw_live = [p.dict() for p in live_products]
                from ranking.product_ranker import rank_products  # noqa: PLC0415
                return rank_products(raw_live, db, tenant_id)
        except Exception:
            pass

    products = db.query(Product).filter(Product.tenant_id == tenant_id).all()
    raw = [normalize_db_product(p) for p in products]
    from ranking.product_ranker import rank_products  # noqa: PLC0415
    return rank_products(raw, db, tenant_id)


def _format_product_list(products: List[Dict[str, Any]], max_items: int = 5) -> str:
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
    """Build a dynamic Arabic sales response driven entirely by store data."""
    store_ref = f" في {ctx['store_name']}" if ctx.get("store_name") else ""

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

    if response_type == "recommendation":
        if not permissions.get("allow_product_recommendations", True):
            return (f"مرحباً {customer_name}! خاصية التوصيات غير مفعّلة حالياً.", False, False)
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
        top = products[0]
        lines = [f"بناءً على منتجاتنا المتاحة{store_ref} ⭐\n", f"*{top['title']}*"]
        if top.get("price"):
            lines.append(f"السعر: {top['price']}")
        if top.get("description"):
            lines.append(top["description"][:150])
        lines.append("\nهل تريد طلبه الآن؟ 😊")
        return "\n".join(lines), False, False

    if response_type == "shipping_info":
        lines = [f"مرحباً {customer_name}! 🚚 *معلومات التوصيل{store_ref}:*\n"]
        if ctx.get("shipping_lines"):
            lines.extend(ctx["shipping_lines"])
        else:
            lines.append("• تواصل معنا لمعرفة رسوم التوصيل لمنطقتك")
        lines.append("\nهل تريد معرفة رسوم التوصيل لمنطقة محددة؟")
        return "\n".join(lines), False, False

    if response_type == "offer_info":
        coupon_rules = ctx.get("coupon_rules", "").strip()
        lines = [f"مرحباً {customer_name}! 🏷️ *العروض والخصومات{store_ref}:*\n"]
        lines.append(coupon_rules if coupon_rules else "• تواصل معنا للاطلاع على العروض الحالية")
        lines.append("\nهل تريد الاستفادة من أحد هذه العروض؟ 🎁")
        return "\n".join(lines), False, False

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

    if response_type == "order_tracking":
        store_url = ctx.get("store_url", "")
        lines = [
            f"مرحباً {customer_name}! 📍",
            f"لتتبع طلبك{store_ref}، أرسل لنا رقم الطلب وسنرسل لك التحديث الفوري.",
        ]
        if store_url:
            lines.append(f"يمكنك أيضاً تتبع طلبك مباشرة من: {store_url}")
        return "\n".join(lines), False, False

    # General fallback
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
        created_at=datetime.now(timezone.utc),
    )
    db.add(event)
    return event


# ── Routes ─────────────────────────────────────────────────────────────────────

@router.get("/settings")
async def get_ai_sales_settings(request: Request, db: Session = Depends(get_db)):
    tenant_id = resolve_tenant_id(request)
    get_or_create_tenant(db, tenant_id)
    return {"settings": _get_ai_sales_settings(db, tenant_id)}


@router.put("/settings")
async def put_ai_sales_settings(
    body: AiSalesSettingsIn,
    request: Request,
    db: Session = Depends(get_db),
):
    try:
        tenant_id = resolve_tenant_id(request)
        get_or_create_tenant(db, tenant_id)
        patch = {k: v for k, v in body.model_dump().items() if v is not None}
        updated = _save_ai_sales_settings(db, tenant_id, patch)
        db.commit()
        logger.info("[AI Sales] settings updated for tenant=%s keys=%s", tenant_id, list(patch.keys()))
        return {"settings": updated}
    except Exception as exc:
        logger.error("[AI Sales] PUT /ai-sales/settings failed: %s", exc, exc_info=True)
        raise HTTPException(status_code=500, detail="Failed to save AI Sales settings")


@router.post("/process-message")
async def ai_sales_process_message(
    body: AiSalesProcessMessageIn,
    request: Request,
    db: Session = Depends(get_db),
):
    """Process an incoming WhatsApp message through the AI Sales Agent."""
    tenant_id = resolve_tenant_id(request)
    get_or_create_tenant(db, tenant_id)

    settings = _get_ai_sales_settings(db, tenant_id)
    if not settings.get("enable_ai_sales_agent", False):
        return {
            "intent": "disabled", "confidence": 0.0,
            "response_text": "وكيل المبيعات الذكي غير مفعّل.",
            "products_used": False, "order_started": False,
            "payment_link": None, "handoff_triggered": False,
        }

    message    = body.message.strip()
    cust_phone = body.customer_phone
    cust_name  = (body.customer_name or "عزيزي العميل").strip() or "عزيزي العميل"

    if not message:
        raise HTTPException(status_code=422, detail="message field is required")

    rate_limit(f"msg:{tenant_id}:{cust_phone}", max_count=20, window_seconds=60)
    _msg_start = _time.monotonic()

    # 1. Check for active human handoff
    sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))
    from handoff.manager import get_active_handoff, create_handoff_session  # noqa: PLC0415
    from handoff.notifier import notify_handoff  # noqa: PLC0415

    active_handoff = get_active_handoff(db, tenant_id, cust_phone)
    if active_handoff:
        return {
            "intent": "handoff_active", "intent_label": "محادثة مع موظف",
            "confidence": 1.0,
            "response_text": (
                f"مرحباً {cust_name}! 👋\n"
                "محادثتك حالياً مع أحد موظفينا. سيرد عليك في أقرب وقت. 🙏"
            ),
            "products_used": False, "order_started": False,
            "payment_link": None, "handoff_triggered": False, "handoff_active": True,
        }

    # 2. Detect intent
    intent, confidence, response_type = _detect_intent(message, settings)

    # 3. Immediate handoff
    if response_type == "human_handoff" and settings.get("allow_human_handoff", True):
        from core.billing import get_moyasar_settings as _gms  # noqa: PLC0415
        s = get_or_create_settings(db, tenant_id)
        meta = s.extra_metadata or {}
        handoff_settings = merge_defaults(
            meta.get("handoff_settings", {}),
            {"notification_method": "webhook", "webhook_url": "", "staff_whatsapp": "", "auto_pause_ai": True},
        )
        handoff_session = create_handoff_session(
            db, tenant_id, cust_phone, cust_name, message, reason="customer_request",
        )
        if not handoff_session.notification_sent:
            sent = await notify_handoff(
                handoff_session.id, tenant_id, cust_phone, cust_name, message, handoff_settings,
            )
            if sent:
                handoff_session.notification_sent = True

        _log_ai_sales_event(
            db, tenant_id, cust_phone, cust_name, message, "talk_to_human", 0.95,
            f"مرحباً {cust_name} 👋\nسأحوّلك الآن إلى أحد موظفينا. انتظر لحظة من فضلك. 🙏",
            False, False, False, True,
        )
        from observability.event_logger import log_event  # noqa: PLC0415
        log_event(
            db, tenant_id, category="handoff", event_type="handoff.triggered",
            summary=f"تحويل بشري: {cust_phone} — '{message[:60]}'",
            severity="info",
            payload={"customer_phone": cust_phone, "session_id": handoff_session.id},
            reference_id=str(handoff_session.id),
        )
        db.commit()
        return {
            "intent": "talk_to_human", "intent_label": "التحدث مع موظف",
            "confidence": 0.95,
            "response_text": f"مرحباً {cust_name} 👋\nسأحوّلك الآن إلى أحد موظفينا. انتظر لحظة من فضلك. 🙏",
            "products_used": False, "order_started": False,
            "payment_link": None, "handoff_triggered": True,
            "handoff_active": False, "handoff_session_id": handoff_session.id,
        }

    # 4. Load product catalog and store context
    from store_integration.shipping_service import get_shipping_options, format_shipping_lines  # noqa: PLC0415
    from store_integration.payment_service import generate_payment_link as store_payment_link  # noqa: PLC0415

    products = await _get_product_catalog(db, tenant_id)
    ctx = _build_response_context(db, tenant_id)

    if response_type == "shipping_info":
        live_shipping = await get_shipping_options(tenant_id)
        if live_shipping:
            ctx["shipping_lines"] = format_shipping_lines(live_shipping)

    products_used = response_type in (
        "product_info", "price_info", "recommendation", "start_order_flow", "cod_flow"
    )

    # 5. Resolve payment link
    payment_link = None
    if response_type == "payment_link" and settings.get("allow_payment_link_sending", True):
        payment_link = await store_payment_link(tenant_id, str(tenant_id), 0.0)

    # 6. Route through AI Orchestrator (falls back to keyword engine)
    orch_result = await _call_orchestrator(tenant_id, cust_phone, message)

    if orch_result and orch_result.get("reply"):
        response_text = orch_result["reply"]
        order_started = any(
            a.get("type") in ("propose_order", "create_draft_order") and a.get("executable", False)
            for a in (orch_result.get("actions") or [])
        )
        handoff_triggered = False
        logger.info(
            "[AISales] Orchestrator response used | tenant=%s model=%s fact_guard_modified=%s",
            tenant_id,
            orch_result.get("model_used", "?"),
            orch_result.get("fact_guard", {}).get("was_modified", False),
        )
    else:
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

    _log_ai_sales_event(
        db, tenant_id, cust_phone, cust_name, message,
        intent, confidence, response_text,
        products_used, order_started, payment_link is not None, handoff_triggered,
    )

    _latency = int((_time.monotonic() - _msg_start) * 1000)
    _orch_used = bool(orch_result and orch_result.get("reply"))
    _fg = orch_result.get("fact_guard", {}) if orch_result else {}

    from observability.event_logger import log_event, write_trace  # noqa: PLC0415
    write_trace(
        db, tenant_id, cust_phone,
        message=message, detected_intent=intent, confidence=confidence,
        response_type=response_type, response_text=response_text,
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
        db, tenant_id, category="ai_sales", event_type="ai_sales.message_processed",
        summary=f"[{intent}] {cust_phone}: {message[:60]}",
        severity="info",
        payload={"intent": intent, "confidence": confidence, "orchestrator": _orch_used,
                 "latency_ms": _latency, "handoff": handoff_triggered, "order": order_started},
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


@router.post("/create-order")
async def ai_sales_create_order(
    body: AiSalesCreateOrderIn,
    request: Request,
    db: Session = Depends(get_db),
):
    """Create an order draft from an AI sales conversation."""
    tenant_id = resolve_tenant_id(request)
    get_or_create_tenant(db, tenant_id)
    settings = _get_ai_sales_settings(db, tenant_id)

    if not settings.get("allow_order_creation", True):
        raise HTTPException(status_code=403, detail="Order creation is disabled for this tenant")

    rate_limit(f"order:{tenant_id}:{body.customer_phone}", max_count=5, window_seconds=3600)

    if body.payment_method in ("cash_on_delivery", "cod"):
        if not settings.get("allow_cod_confirmation_flow", True):
            raise HTTPException(status_code=403, detail="COD orders are disabled for this tenant")
        order_status = "pending_confirmation"
        payment_link = None
    else:
        if not settings.get("allow_payment_link_sending", True):
            raise HTTPException(status_code=403, detail="Online payment is disabled for this tenant")
        order_status = "payment_pending"
        payment_link = None

    sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))
    from store_integration.order_service import create_order as store_create_order  # noqa: PLC0415
    from store_integration.models import OrderInput as StoreOrderInput, OrderItemInput as StoreOrderItem  # noqa: PLC0415
    from store_integration.payment_service import generate_payment_link as store_payment_link  # noqa: PLC0415

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
        external_order_id = store_order.id
        payment_link = store_order.payment_link
        if not payment_link and order_status == "payment_pending":
            payment_link = await store_payment_link(tenant_id, external_order_id, 0.0)
    else:
        external_order_id = None
        if order_status == "payment_pending":
            payment_link = await store_payment_link(tenant_id, str(tenant_id), 0.0)

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

    product_display = body.product_name
    if not product_display and body.product_id:
        prod = db.query(Product).filter(
            Product.id == body.product_id, Product.tenant_id == tenant_id,
        ).first()
        if prod:
            product_display = prod.title

    line_items = [{
        "product_id": body.product_id,
        "product_name": product_display or "منتج غير محدد",
        "variant_id": body.variant_id,
        "quantity": body.quantity,
    }]

    total_str = "—"
    if body.product_id:
        prod = db.query(Product).filter(
            Product.id == body.product_id, Product.tenant_id == tenant_id,
        ).first()
        if prod and prod.price:
            try:
                total_str = (
                    f"{float(prod.price.replace('ر.س','').replace(',','').strip()) * body.quantity:.2f} ر.س"
                )
            except Exception:
                total_str = prod.price

    order = Order(
        tenant_id=tenant_id,
        status=order_status,
        total=total_str,
        external_id=external_order_id,
        customer_info={
            "name": body.customer_name, "phone": body.customer_phone,
            "building_number": body.building_number, "street": body.street,
            "district": body.district, "postal_code": body.postal_code,
            "city": body.city, "address": body.address,
        },
        line_items=line_items,
        checkout_url=payment_link,
        extra_metadata={
            "source": "ai_sales_agent", "payment_method": body.payment_method,
            "notes": body.notes, "created_via": "whatsapp_conversation",
        },
    )
    db.add(order)
    db.flush()

    if order_status == "pending_confirmation":
        from routers.automations import (  # noqa: PLC0415
            _get_autopilot_settings as _get_ap,
            _log_autopilot_event as _log_ap,
        )
        ap = _get_ap(db, tenant_id)
        if ap.get("enabled") and ap.get("cod_confirmation", {}).get("enabled"):
            _log_ap(db, tenant_id, "cod_confirmation", customer.id,
                    {"order_id": order.id, "source": "ai_sales_agent"})

    _log_ai_sales_event(
        db, tenant_id, body.customer_phone, body.customer_name,
        f"[order_created] product={product_display} qty={body.quantity} method={body.payment_method}",
        "order_product", 1.0,
        f"تم إنشاء طلب رقم #{order.id} بنجاح",
        product_used=True, order_created=True,
        payment_link_sent=(payment_link is not None),
        handoff_triggered=False, order_id=order.id,
    )

    from observability.event_logger import log_event  # noqa: PLC0415
    log_event(
        db, tenant_id, category="order", event_type="order.created",
        summary=f"طلب #{order.id} — {product_display or 'منتج'} x{body.quantity} [{body.payment_method}]",
        severity="info",
        payload={"order_id": order.id, "status": order_status, "product": product_display,
                 "qty": body.quantity, "method": body.payment_method, "external_id": external_order_id},
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


@router.get("/logs")
async def get_ai_sales_logs(
    request: Request,
    db: Session = Depends(get_db),
    limit: int = 50,
    offset: int = 0,
):
    """Return AI Sales Agent conversation logs for this tenant."""
    tenant_id = resolve_tenant_id(request)
    get_or_create_tenant(db, tenant_id)

    rows = (
        db.query(AutomationEvent)
        .filter(
            AutomationEvent.tenant_id == tenant_id,
            AutomationEvent.event_type == "ai_sales_log",
        )
        .order_by(AutomationEvent.created_at.desc())
        .offset(offset).limit(limit).all()
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
            "intent_label":      AI_SALES_INTENTS.get(p.get("intent", ""), {}).get("label", "عام"),
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
